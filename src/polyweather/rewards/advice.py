"""Hourly LLM weather calibration for the five-minute reward-farmer loop.

The model returns one event-level temperature adjustment and participation recommendation. It never
chooses an order or calls the executor. Cached advice adjusts the deterministic fair/lean calculation;
missing, stale, mismatched, or malformed advice falls back to the station/regime calibration.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import httpx

from .. import agent
from ..obslog import log_failure
from ..realized.feature_store import DATA_DIR
from ..realized.residuals import bucket_fair_yes, recent_bias
from ..util.atomicio import dump_json_atomic
from .scan import our_yes_lean, score

UTC = timezone.utc
ADVICE_FILE = DATA_DIR / "reward_llm_advice.json"
ADVICE_LOG = DATA_DIR / "reward_llm_advice.jsonl"
REFRESH_MIN = 60
MAX_AGE_MIN = 180
MAX_ABS_ADJUSTMENT_C = 5.0
MAX_FORECAST_DRIFT_C = 1.5
MIN_PARTICIPATE_CONFIDENCE = 0.5
DEFAULT_MODEL = "gpt-5.4-mini"

SYSTEM = """You are the hourly meteorological calibration adviser for a Polymarket temperature liquidity
farmer. For EACH station/date event supplied, estimate one `adjustment_C` where:

    expected actual daily maximum = current Open-Meteo forecast peak + adjustment_C

Positive means Open-Meteo is likely too low; negative means it is likely too high. Analyze the current
forecast peak weather/regime and the station's recent forecast-minus-actual residuals, especially comparable
regimes. Respect sample count and variance: shrink noisy or thin regime evidence toward the all-weather
station result, and do not mechanically copy a small-sample mean. The deterministic calibration is a useful
baseline, not an instruction.

Set `participate=false` when the evidence is too unstable, the current regime lacks useful analogues, or the
temperature distribution is too uncertain to support liquidity farming. Market prices are context only;
you do not choose a bucket, price, size, or order. Keep adjustment_C in [-5, 5]. Return exactly one record for
every supplied event through the tool, with a short evidence-based reason."""

SCHEMA = {
    "type": "object",
    "properties": {
        "advice": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "station_id": {"type": "string"},
                    "market_date": {"type": "string"},
                    "adjustment_C": {"type": "number", "minimum": -5, "maximum": 5},
                    "participate": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                },
                "required": ["station_id", "market_date", "adjustment_C", "participate",
                             "confidence", "reason"],
            },
        },
    },
    "required": ["advice"],
}
TOOL = {"type": "function", "function": {
    "name": "emit_farmer_advice",
    "description": "Emit hourly event-level temperature calibration and participation advice.",
    "parameters": SCHEMA,
}}


def _model() -> str:
    model = (os.environ.get("POLY_FARM_AGENT_MODEL")
             or os.environ.get("POLY_AGENT_MODEL") or DEFAULT_MODEL)
    return DEFAULT_MODEL if model.startswith("claude") else model


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def load_cache() -> dict:
    try:
        value = json.loads(ADVICE_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    dump_json_atomic(ADVICE_FILE, cache, ensure_ascii=False, indent=2)


def _append_log(record: dict) -> None:
    with ADVICE_LOG.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _refresh_due(cache: dict, now: datetime) -> bool:
    attempted = _parse_time(cache.get("last_attempt_at"))
    return attempted is None or (now - attempted).total_seconds() >= REFRESH_MIN * 60


def _history_packet(station_id: str, regime: str) -> dict | None:
    history = recent_bias(station_id, regime=regime)
    if not history:
        return None
    return {key: history.get(key) for key in ("d7", "d14", "d30", "matched_regime", "by_regime")}


def build_event_facts(targets: list[dict]) -> list[dict]:
    """Collapse bucket rows into one compact fact packet per station/date event."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for target in targets:
        if target.get("forecast_peak") is None:
            continue
        grouped.setdefault((target["station_id"], target["market_date"]), []).append(target)
    facts = []
    for (station_id, market_date), rows in sorted(grouped.items()):
        first = rows[0]
        regime = ((first.get("forecast_regime") or {}).get("primary") or "unknown")
        calibration = first.get("calibration") or {}
        ladder = [{"bucket": row["bucket"], "mid": row.get("mid"),
                   "reward_daily": row.get("daily_rate"), "spread": row.get("spread")}
                  for row in sorted(rows, key=lambda row: (row["comparator"], row["threshold"]))]
        facts.append({
            "station_id": station_id, "city": first["city"], "market_date": market_date,
            "forecast_peak_C": first["forecast_peak"], "peak_utc": first.get("peak_utc"),
            "hours_to_peak": first.get("hours_to_peak"), "forecast_regime": first.get("forecast_regime"),
            "deterministic_adjustment_C": (round(-calibration["bias"], 3)
                                             if calibration.get("bias") is not None else None),
            "deterministic_sigma_C": calibration.get("sigma"),
            "recent_residuals_forecast_minus_actual": _history_packet(station_id, regime),
            "reward_buckets": ladder,
        })
    return facts


async def _request(client: httpx.AsyncClient, facts: list[dict], model: str) -> tuple[dict | None, str | None]:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": json.dumps({"events": facts}, ensure_ascii=False,
                                                               default=str)}],
        "tools": [TOOL],
        "tool_choice": {"type": "function", "function": {"name": "emit_farmer_advice"}},
    }
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
               "content-type": "application/json"}
    try:
        response = await client.post(agent.OPENAI_URL, headers=headers, json=body, timeout=90.0)
    except Exception as exc:
        return None, f"request failed: {exc!r}"
    if response.status_code != 200:
        return None, f"API {response.status_code}: {response.text[:200]}"
    try:
        call = response.json()["choices"][0]["message"]["tool_calls"][0]
        if call["function"]["name"] != "emit_farmer_advice":
            raise ValueError("wrong tool")
        return dict(json.loads(call["function"]["arguments"])), None
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None, "no valid emit_farmer_advice tool call"


def _validated_records(payload: dict, facts: list[dict], now: datetime, model: str) -> dict[str, dict]:
    known = {(fact["station_id"], fact["market_date"]): fact for fact in facts}
    records = {}
    for raw in payload.get("advice") or []:
        if not isinstance(raw, dict):
            continue
        key_tuple = (str(raw.get("station_id") or ""), str(raw.get("market_date") or ""))
        fact = known.get(key_tuple)
        if fact is None or not isinstance(raw.get("participate"), bool):
            continue
        try:
            adjustment = float(raw["adjustment_C"])
            confidence = float(raw["confidence"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-MAX_ABS_ADJUSTMENT_C <= adjustment <= MAX_ABS_ADJUSTMENT_C):
            continue
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            continue
        regime = ((fact.get("forecast_regime") or {}).get("primary") or "unknown")
        record = {
            "station_id": key_tuple[0], "market_date": key_tuple[1],
            "adjustment_C": round(adjustment, 3), "participate": raw["participate"],
            "confidence": round(min(max(confidence, 0.0), 1.0), 3), "reason": reason,
            "regime": regime, "forecast_peak_at_advice": fact["forecast_peak_C"],
            "generated_at": _iso(now), "model": model,
        }
        records[f"{key_tuple[0]}|{key_tuple[1]}"] = record
    return records


async def hourly_advice(client: httpx.AsyncClient, targets: list[dict], now: datetime) -> tuple[dict, str]:
    """Refresh at most hourly and return the cache plus a one-line cycle log message."""
    if not agent.enabled():
        return {}, ""
    cache = load_cache()
    if not _refresh_due(cache, now):
        return cache, ""
    facts = build_event_facts(targets)
    if not facts:
        return cache, ""
    model = _model()
    payload, error = await _request(client, facts, model)
    cache["last_attempt_at"] = _iso(now)
    if payload is not None:
        records = _validated_records(payload, facts, now, model)
        if records:
            cache = {"last_attempt_at": _iso(now), "generated_at": _iso(now),
                     "model": model, "records": records}
            _save_cache(cache)
            _append_log({"ts": _iso(now), "model": model, "records": list(records.values())})
            return cache, f"LLM-ADVICE refreshed {len(records)}/{len(facts)} events ({model})"
        error = "response contained no valid event records"
    cache["last_error"] = error
    _save_cache(cache)
    log_failure("agent", kind="exception", detail=f"farmer advice: {error}",
                consequence="deterministic farmer calibration")
    return cache, f"LLM-ADVICE failed ({error}); deterministic/cached fallback"


def _usable_record(target: dict, cache: dict, now: datetime) -> dict | None:
    record = (cache.get("records") or {}).get(f"{target['station_id']}|{target['market_date']}")
    if not isinstance(record, dict):
        return None
    generated = _parse_time(record.get("generated_at"))
    if generated is None or not (0 <= (now - generated).total_seconds() <= MAX_AGE_MIN * 60):
        return None
    regime = ((target.get("forecast_regime") or {}).get("primary") or "unknown")
    if record.get("regime") != regime:
        return None
    try:
        if abs(float(target["forecast_peak"]) - float(record["forecast_peak_at_advice"])) > MAX_FORECAST_DRIFT_C:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return record


def apply_advice(targets: list[dict], cache: dict, now: datetime) -> list[dict]:
    """Return target copies with usable hourly advice applied to fair value and new-entry eligibility."""
    out = []
    for original in targets:
        target = dict(original)
        record = _usable_record(target, cache, now)
        calibration = dict(target.get("calibration") or {})
        if record is None or target.get("forecast_peak") is None or calibration.get("sigma") is None:
            target["llm_advice"] = None
            target["llm_advice_status"] = "deterministic_fallback"
            out.append(target)
            continue
        adjusted_peak = float(target["forecast_peak"]) + float(record["adjustment_C"])
        confidence_ok = float(record["confidence"]) >= MIN_PARTICIPATE_CONFIDENCE
        effective_participate = bool(record["participate"] and confidence_ok)
        target["forecast_peak_adjusted"] = round(adjusted_peak, 3)
        target["our_yes_lean"] = round(our_yes_lean(target["comparator"], target["threshold"], adjusted_peak), 2)
        target["fair_yes"] = round(bucket_fair_yes(target["comparator"], target["threshold"],
                                                   adjusted_peak, float(calibration["sigma"])), 3)
        target["score"] = round(score(target["daily_rate"], target["our_yes_lean"],
                                      target["competition"], target["min_size"]), 4)
        target["entry_eligible"] = bool(target.get("entry_eligible") and effective_participate)
        target["llm_advice"] = {**record, "effective_participate": effective_participate}
        target["llm_advice_status"] = "applied"
        target["calibration"] = {**calibration, "source": "llm_hourly",
                                 "bias": round(-float(record["adjustment_C"]), 3),
                                 "deterministic_bias": calibration.get("bias")}
        out.append(target)
    return out
