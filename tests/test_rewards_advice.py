import asyncio
import json
from datetime import datetime, timedelta, timezone

from polyweather.rewards import advice

UTC = timezone.utc
NOW = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)


def _target(**over):
    target = {
        "city": "seoul", "station_id": "RKSI", "market_date": "2026-06-19",
        "bucket": "==28C", "comparator": "==", "threshold": 28,
        "forecast_peak": 27.0, "peak_utc": "2026-06-19T06:00:00+00:00", "hours_to_peak": 18.0,
        "forecast_regime": {"primary": "clear", "tags": ["clear", "dry"]},
        "calibration": {"sigma": 1.5, "bias": -3.0, "source": "regime_shrunk"},
        "daily_rate": 100.0, "min_size": 20.0, "competition": 100.0,
        "mid": 0.20, "spread": 0.02, "entry_eligible": True,
        "fair_yes": 0.2, "our_yes_lean": 0.0,
    }
    target.update(over)
    return target


class _Response:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Client:
    def __init__(self, arguments):
        self.arguments = arguments
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        return _Response({"choices": [{"message": {"tool_calls": [{"function": {
            "name": "emit_farmer_advice", "arguments": json.dumps(self.arguments)}}]}}]})


def _cache_record(**over):
    record = {
        "station_id": "RKSI", "market_date": "2026-06-19", "adjustment_C": 1.0,
        "participate": True, "confidence": 0.8, "reason": "clear analogue",
        "regime": "clear", "forecast_peak_at_advice": 27.0,
        "generated_at": "2026-06-18T12:00:00Z", "model": "gpt-5.4-mini",
    }
    record.update(over)
    return {"records": {"RKSI|2026-06-19": record}}


def test_build_event_facts_collapses_bucket_rows(monkeypatch):
    monkeypatch.setattr(advice, "recent_bias", lambda station, regime: {"d30": {"n": 30, "mean": -3.0}})
    facts = advice.build_event_facts([_target(), _target(bucket="==29C", threshold=29)])
    assert len(facts) == 1 and len(facts[0]["reward_buckets"]) == 2
    assert facts[0]["deterministic_adjustment_C"] == 3.0
    assert facts[0]["recent_residuals_forecast_minus_actual"]["d30"]["mean"] == -3.0


def test_hourly_advice_calls_once_then_uses_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(advice, "ADVICE_FILE", tmp_path / "advice.json")
    monkeypatch.setattr(advice, "ADVICE_LOG", tmp_path / "advice.jsonl")
    client = _Client({"advice": [{"station_id": "RKSI", "market_date": "2026-06-19",
                                   "adjustment_C": 1.2, "participate": True,
                                   "confidence": 0.7, "reason": "clear days ran warm"}]})
    cache, log = asyncio.run(advice.hourly_advice(client, [_target()], NOW))
    assert client.calls == 1 and "refreshed 1/1" in log
    assert cache["records"]["RKSI|2026-06-19"]["adjustment_C"] == 1.2

    cached, log2 = asyncio.run(advice.hourly_advice(client, [_target()], NOW + timedelta(minutes=5)))
    assert client.calls == 1 and log2 == "" and cached == cache


def test_apply_advice_recomputes_peak_lean_fair_and_calibration():
    [target] = advice.apply_advice([_target()], _cache_record(), NOW)
    assert target["forecast_peak_adjusted"] == 28.0
    assert target["our_yes_lean"] == 1.0
    assert target["fair_yes"] > 0.2
    assert target["entry_eligible"] is True
    assert target["calibration"]["source"] == "llm_hourly"
    assert target["calibration"]["bias"] == -1.0
    assert target["calibration"]["deterministic_bias"] == -3.0


def test_apply_advice_participation_is_a_new_entry_gate_but_keeps_fair():
    cache = _cache_record(participate=False, adjustment_C=-1.0)
    [target] = advice.apply_advice([_target()], cache, NOW)
    assert target["entry_eligible"] is False
    assert target["forecast_peak_adjusted"] == 26.0 and target["fair_yes"] is not None
    assert target["llm_advice"]["effective_participate"] is False


def test_stale_or_regime_mismatched_advice_falls_back():
    stale_now = NOW + timedelta(minutes=advice.MAX_AGE_MIN + 1)
    [stale] = advice.apply_advice([_target()], _cache_record(), stale_now)
    assert stale["llm_advice"] is None and stale["fair_yes"] == 0.2

    [mismatch] = advice.apply_advice([_target(forecast_regime={"primary": "rainy"})],
                                     _cache_record(), NOW)
    assert mismatch["llm_advice_status"] == "deterministic_fallback"
