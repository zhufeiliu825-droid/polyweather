"""Runtime decision AGENT — the daemon calls an LLM at each activation to add JUDGMENT on top
of the deterministic facts, and gets back a STRUCTURED JSON decision only. The agent merely
PROPOSES; `decision.validate_proposal` is the authority that hard-checks the proposal against the
facts before anything is placed.

OpenAI Chat Completions (function-calling). Set `OPENAI_API_KEY`. Two-pass escalating models (daemon-driven):
  - PASS 1 (daily): `POLY_AGENT_MODEL`       (default `gpt-5.4-mini`).
  - PASS 2 (retry): `POLY_AGENT_MODEL_RETRY`  (default `gpt-5.4` — the daemon passes it on the +20min retry).
  A stale `claude-…` model name is ignored (falls back to the default), so a leftover can't break the call.

Safety / boundaries:
  - No `OPENAI_API_KEY` in env → `propose()` returns None → the daemon falls back to deterministic `decide()`.
    So the system runs fully without any LLM (and for FREE).
  - Any API error / malformed reply → an ESCALATE proposal (never a silent BET).
  - The model is FORCED to emit the `emit_decision` tool (structured output) — no free-form bypass.
  - The agent NEVER places an order; the deterministic validator + clob_executor do, under the
    operator's control (DRY_RUN default, env creds).
  - NOTE: a "free shared" OpenAI tier may share prompts with OpenAI. The agent context is weather +
    market numbers only — NO credentials, wallet, or PII — so sharing it is benign.
"""
from __future__ import annotations

import json
import os

import httpx

from .obslog import log_failure

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-5.4-mini"        # PASS-1 default; POLY_AGENT_MODEL overrides
DEFAULT_RETRY_MODEL = "gpt-5.4"       # PASS-2 (stronger); POLY_AGENT_MODEL_RETRY overrides (the daemon's +20min retry)

SYSTEM = """You are the decision agent for a low-frequency Polymarket daily-high-temperature \
trading bot. Each city has 11 mutually-exclusive integer-°C buckets (e.g. <=19, ==20, … , >=29). \
Every market resolves in Celsius. YOU CHOOSE which bucket the day's HIGH temperature will land in, and \
whether to BET it now or ESCALATE/SKIP. There is NO model telling you the bucket — the judgment is yours \
(this is why you exist: a hard-coded formula once saw a 25.9 forecast and bet ==25, which is absurd).

START from `forecast_peak_C` — the raw forecast peak — and round it to the nearest integer bucket (25.9 → \
==26, 24.2 → ==24). That is your anchor. Then ADJUST it with judgment a fixed formula can't, using the \
verified FACTS in the context:
  - NOWCAST / REALIZED CLIMB: `realized_running_max_C` is today's max so far; compare it to \
    `forecast_curve_local` for the hours ALREADY past. Running hotter than forecast, or already near/above \
    the forecast peak with time left → the HIGHER bucket is live. You can NEVER pick below running_max; \
    an ==running_max bucket remains possible only if the day does not warm further.
  - DEAD-BUCKET-AT-MAX: betting EXACTLY ==`realized_running_max_C` wins ONLY if the temp does NOT rise above \
    it. If the peak is still AHEAD and the forecast peak / climb suggests the max will go higher, do NOT bet \
    ==running_max — bet the higher bucket. Bet ==running_max only when you believe the day is essentially \
    DONE rising (peak passed, or the forecast peak ≈ running_max). (A deterministic check escalates a \
    ==running_max bet when the forecast peak is ≥0.5° above it.)
  - FORECAST REVISION (`forecast_peak_fan`): the peak VALUE across today's successive forecast issues \
    (`series`), the net `trend_C`, and whether it has `converged`. Revised UP through the day and still \
    rising → trust the LATEST/higher number, do NOT anchor to an early low run. Still drifting (not \
    converged) → the peak is uncertain → lean ESCALATE.
  - METEOROLOGIST BIAS-DIRECTION: from `realized_surface_met` + `forecast_peak_fan.met_at_peak_latest` \
    (cloud, wind, dewpoint/RH, surface pressure, precip). NWP systematically UNDER-calls the afternoon max \
    under a CLEAR sky + LIGHT wind + HIGH/rising pressure + DRY air (low dewpoint) — strong insolation + a \
    deep dry mixed layer → the real max often lands ABOVE the model: lean the HIGHER adjacent bucket. \
    OVERCAST / showery / windy / moist (high dewpoint, low cloud base) caps the max → same or LOWER.
  - RECENT FORECAST BIAS (`recent_forecast_bias`): the EMPIRICAL version of the above — `residual = \
    forecast − actual` for THIS station over the last 3/7/14/30 days. Each window includes count, mean, \
    median, sample variance/SD, MAE/RMSE, range, and under/over rates. `forecast_regime` labels the current \
    peak weather (clear/cloudy/rainy/mixed + wind/humidity/sun tags); `matched_regime` and `by_regime` show \
    how this station's errors behaved under comparable peak weather. Treat small `n` as weak evidence. \
    A NEGATIVE mean = Open-Meteo has been running LOW here (the real max came in HIGHER) → today's forecast \
    likely under-calls → lean a HIGHER bucket by roughly |mean|. POSITIVE = running high → lean lower. Weigh \
    it by `n` and variance: a stable repeated direction is stronger than the same mean with a wide SD. \
    Prefer the matched-regime evidence when it has enough samples; otherwise shrink mentally toward the \
    all-weather 14/30-day station mean. This is a recent, seasonal nudge, NOT a hard rule.
  - PEAK SHARPNESS: a sharp single-hour peak = a confident bucket; a flat/broad afternoon straddling two \
    integers, or a forecast sitting right on an X.5 boundary → lean ESCALATE.
  - RECENT RELIABILITY: if `recent_outcomes` shows this city's forecast has missed lately, be more cautious; \
    a run of hits = more trust.
  - PRICE MOMENTUM: `recent_momentum` is how far the favorite's market price has fallen over the last few \
    minutes (`drop_pct`, `is_big_drop`). A big recent drop means the book may be repricing against your \
    bucket (news/flow); it is a HARD VETO in either pass, and the exact selected token is checked again \
    immediately before submit. Prefer ESCALATE when this flag is set.

`buckets` is the market ladder you choose from: each `{b, mkt, alive}` is a bucket, its market mid, and \
whether it is still possible given running_max. HARD SAFETY gates re-check every BET and REJECT a violation — \
they only VETO, they NEVER pick your bucket, so choose freely but know a bet is refused if: the bucket is \
below `realized_running_max_C` (dead), an already-won >= bucket, its market price ≥ 0.85 (no upside), the \
market is unhealthy, or \
the market prices it ~0 (it deems your bucket impossible — reconsider). When you are genuinely unsure, the \
forecast is unreliable/convective, or you cannot justify your bucket against the market → ESCALATE or SKIP. \
This is FULLY AUTOMATED — on a no-bet a stronger model re-checks ~20min later, then it is skipped; no human \
is asked, so don't "defer to a human", make the best call on the facts. EXECUTION is automatic and NOT your \
choice (always a resting maker bid). Prefer caution: a missed bet costs nothing, a \
bad bet loses money.

Output ONLY via the emit_decision tool. `comparator`+`threshold` name your chosen bucket (required if \
action=BET). `confidence` 0-1. `reason` = a 2–4 sentence PLAIN-LANGUAGE explanation a human will read in \
this city's log: what bucket you chose and the key why — the forecast peak vs the realized running_max vs \
the market price, the atmosphere, and any flag. Readable prose, not jargon. `flags` optional short tags \
(e.g. "clear-hot","revised-up","boundary","market-disagree")."""

# Shared JSON schema for the forced structured output.
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BET", "ESCALATE", "SKIP"]},
        "comparator": {"type": "string", "enum": ["<=", "==", ">="]},
        "threshold": {"type": "integer"},
        "confidence": {"type": "number"},
        "reason": {"type": "string", "description": "2–4 sentence human-readable explanation (shown in the per-city .txt log)"},
        "flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action", "reason"],
}
_TOOL_DESC = "Emit the trading decision as structured JSON. The only allowed output."
OPENAI_TOOL = {"type": "function",
               "function": {"name": "emit_decision", "description": _TOOL_DESC, "parameters": SCHEMA}}

# ── STRIKE-TIMING stage (decides WHEN to run the bet, NOT what to bet) ──
STRIKE_SYSTEM = """You decide WHEN to evaluate a daily-high-temperature bet for ONE city — you do NOT \
decide what to bet. Output the single UTC time (`strike_utc`) at which the bet decision should run.

ANCHOR the strike to the forecast peak (`forecast_peak_utc`): strike = that peak ± a small OFFSET you \
choose. Aim to strike AT or just BEFORE the real peak — late enough that the realized obs + the intraday \
forecast have matured, but NOT past the peak (overshoot = the temperature has already fallen, the top bucket \
is dead). You are given: `forecast_peak_utc` (the anchor), `expected_peak_utc` (solar_noon+lag, a cross-check), \
`solar_noon_utc`/`sunset_utc`/`lag_h`, the MULTI-RUN peak spread (`multi_run_peak_spread`: value + local-time \
range across recent runs), an optional `minutely15_peak_local`, the intraday `forecast_peak_fan` (how the \
peak VALUE converged today: `series`, `trend_C`, `converged`, and `met_at_peak_latest`), and \
`realized_running_max_C`. Choose the offset from:
  - CONVERGENCE: if `forecast_peak_fan.converged` (recent issues agree), the peak time is trustworthy → \
    strike right at it. If the peak is still drifting (not converged), it's uncertain → strike a little \
    EARLIER so a sudden afternoon drop can't cut us off.
  - PEAK-TIME AGREEMENT: a TIGHT `multi_run_peak_spread` time range → strike at the peak; a WIDE range → \
    bias slightly earlier within the window.
  - METEOROLOGY (`forecast_peak_fan.met_at_peak_latest`: cloud, wind, dewpoint, pressure): a CLEAR, \
    light-wind, high-pressure afternoon = a sharp solar-driven peak → strike right at it. Cloud CLEARING \
    through the afternoon can push the peak LATER (small positive offset). A front / wind-shift / convective \
    or showery signal = the peak can collapse early and unpredictably → strike EARLIER to lock it in.
  - REALIZED: if the day is already running hot near the forecast peak → strike soon; still climbing → keep \
    the strike at/just before the forecast peak.
Do NOT systematically prefer the late edge. Ignore a low-probability pre-dawn peak (the clamp removes it). A \
deterministic clamp FORCES `strike_utc` into `strike_window_utc` regardless of your output. Output ONLY via \
the `emit_strike` tool. `reason` = 1–2 plain sentences (shown in this city's log)."""
STRIKE_SCHEMA = {
    "type": "object",
    "properties": {
        "strike_utc": {"type": "string", "description": "ISO-8601 UTC instant to run the decision, e.g. 2026-06-16T16:30Z"},
        "confidence": {"type": "number"},
        "reason": {"type": "string", "description": "1–2 sentence plain-language why (shown in the city log)"},
    },
    "required": ["strike_utc", "reason"],
}
STRIKE_TOOL = {"type": "function",
               "function": {"name": "emit_strike",
                            "description": "Emit the strike TIME (when to run the bet) as structured JSON. The only allowed output.",
                            "parameters": STRIKE_SCHEMA}}


def enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _pick(env_var: str, default: str) -> str:
    """The model from `env_var`, else `default`. A stale 'claude-…' name (left over from the old
    Anthropic path) is ignored so it can't break the OpenAI call."""
    m = os.environ.get(env_var) or ""
    return m if (m and not m.startswith("claude")) else default


def _model() -> str:
    return _pick("POLY_AGENT_MODEL", DEFAULT_MODEL)              # PASS-1 (default gpt-5.4-mini)


def retry_model() -> str:
    return _pick("POLY_AGENT_MODEL_RETRY", DEFAULT_RETRY_MODEL)  # PASS-2 (default gpt-5.4)


def _err(reason: str) -> dict:
    # every agent failure funnels through here → one structured record (the daemon falls back to the
    # deterministic baseline, so consequence=fallback). City context lives in decisions.jsonl's agent field.
    log_failure("agent", kind="exception", detail=reason, consequence="fallback")
    return {"action": "ESCALATE", "reason": reason, "_error": True}


async def _propose_openai(client: httpx.AsyncClient, context: dict, model: str) -> dict:
    # No temperature / max_tokens param → compatible across gpt-4o/4.1/5 + o-series (some reject those).
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)}],
        "tools": [OPENAI_TOOL],
        "tool_choice": {"type": "function", "function": {"name": "emit_decision"}},
    }
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "content-type": "application/json"}
    r = await client.post(OPENAI_URL, headers=headers, json=body, timeout=60.0)
    if r.status_code != 200:
        return _err(f"agent API {r.status_code}: {r.text[:200]}")
    try:
        tc = r.json()["choices"][0]["message"]["tool_calls"][0]
        if tc["function"]["name"] == "emit_decision":
            out = dict(json.loads(tc["function"]["arguments"]))
            out["_model"] = model
            return out
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return _err("agent returned no emit_decision tool call")


async def propose(client: httpx.AsyncClient, context: dict, *, model: str | None = None) -> dict | None:
    """Ask the OpenAI agent for a structured decision. None → no OPENAI_API_KEY (fall back to
    deterministic). On error → an ESCALATE proposal (safe). `model` overrides the default (the daemon
    passes the pass-2 retry model on the +20min retry). Otherwise the validated-downstream proposal dict."""
    if not enabled():
        return None
    try:
        return await _propose_openai(client, context, model or _model())
    except Exception as e:
        return _err(f"agent error: {e!r}")


async def _propose_strike_openai(client: httpx.AsyncClient, context: dict, model: str) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": STRIKE_SYSTEM},
                     {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)}],
        "tools": [STRIKE_TOOL],
        "tool_choice": {"type": "function", "function": {"name": "emit_strike"}},
    }
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "content-type": "application/json"}
    r = await client.post(OPENAI_URL, headers=headers, json=body, timeout=60.0)
    if r.status_code != 200:
        return _err(f"strike API {r.status_code}: {r.text[:200]}")
    try:
        tc = r.json()["choices"][0]["message"]["tool_calls"][0]
        if tc["function"]["name"] == "emit_strike":
            out = dict(json.loads(tc["function"]["arguments"]))
            out["_model"] = model
            return out
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return _err("strike returned no emit_strike tool call")


async def propose_strike(client: httpx.AsyncClient, context: dict, *, model: str | None = None) -> dict | None:
    """Stage-1 strike-timing call (gpt-5.4-mini). None → no OPENAI_API_KEY; on error → an `_error` dict.
    Either way the CALLER falls back to the deterministic solar+lag strike (this NEVER places an order or
    chooses a bucket — it only proposes a TIME, which is then hard-clamped to the daytime window)."""
    if not enabled():
        return None
    try:
        return await _propose_strike_openai(client, context, model or _model())
    except Exception as e:
        return _err(f"strike error: {e!r}")
