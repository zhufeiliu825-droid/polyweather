import asyncio
from datetime import date, datetime, timedelta, timezone

from polyweather import params as P
from polyweather.realized.solar import clamp_strike, day_sun
from polyweather.runtime import analyze as A

UTC = timezone.utc
MKT = date(2026, 6, 16)


class _Stn:
    station_id = "EGLC"
    city = "London"
    latitude = 51.5
    longitude = -0.1
    timezone = "Europe/London"


def _scan(peak_utc):
    return A.Scan("EGLC", "London", MKT, peak_utc, 30.0)


def _no_network(monkeypatch):
    async def _prev(*a, **k):
        return {}

    async def _m15(*a, **k):
        return []

    async def _rmax(*a, **k):
        return (None, True, None)
    monkeypatch.setattr(A, "fetch_previous_runs", _prev)
    monkeypatch.setattr(A, "fetch_minutely_15", _m15)
    monkeypatch.setattr(A, "running_max_today", _rmax)
    monkeypatch.setattr(A, "fan_summary", lambda *a, **k: None)   # no intraday snapshots in unit tests


# ---- clamp_strike: the core safety guard ----

def test_clamp_blocks_pre_dawn_and_post_sunset():
    noon = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    sunset = datetime(2026, 6, 16, 19, 0, tzinfo=UTC)
    assert clamp_strike(datetime(2026, 6, 16, 2, 0, tzinfo=UTC), noon, sunset) == noon      # pre-dawn → solar noon
    assert clamp_strike(datetime(2026, 6, 16, 22, 0, tzinfo=UTC), noon, sunset) == sunset   # post-sunset → sunset
    mid = datetime(2026, 6, 16, 16, 30, tzinfo=UTC)
    assert clamp_strike(mid, noon, sunset) == mid                                           # daytime → unchanged


def test_clamp_min_lag_floor():
    noon = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    sunset = datetime(2026, 6, 16, 19, 0, tzinfo=UTC)
    out = clamp_strike(datetime(2026, 6, 16, 12, 30, tzinfo=UTC), noon, sunset, min_lag_h=2.0)
    assert out == datetime(2026, 6, 16, 14, 0, tzinfo=UTC)        # floor = noon + 2h


# ---- decide_strike: deterministic fallback + the hard clamp on the LLM ----

def test_decide_strike_no_peak(monkeypatch):
    _no_network(monkeypatch)
    s, c, src = asyncio.run(A.decide_strike(None, _Stn(), _scan(None), datetime(2026, 6, 16, 8, 0, tzinfo=UTC)))
    assert s is None and c is None and src == "no_peak"


def test_decide_strike_fallback_is_peak_minus_lead(monkeypatch):
    # no LLM → deterministic fallback = forecast peak − FALLBACK_LEAD, bounded into the daytime window.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _no_network(monkeypatch)
    peak = datetime(2026, 6, 16, 13, 0, tzinfo=UTC)
    s, c, src = asyncio.run(A.decide_strike(None, _Stn(), _scan(peak), datetime(2026, 6, 16, 8, 0, tzinfo=UTC)))
    _sr, noon, ss, _ = day_sun(51.5, -0.1, MKT)
    assert src == "fallback_peak"
    assert s == peak - timedelta(minutes=P.STRIKE_FALLBACK_LEAD_MIN)   # 12:30 — in-window, unbounded
    assert noon <= s <= ss and s <= c <= ss


def test_decide_strike_fallback_anchored_to_peak_not_lag(monkeypatch):
    # a HUGE thermal lag must NOT move the strike — the anchor is the FORECAST PEAK now, not solar_noon+lag.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _no_network(monkeypatch)
    monkeypatch.setattr(A, "_lag_h", lambda st, m: 6.0)          # solar_noon+6h would be ~18:00 — must be ignored
    peak = datetime(2026, 6, 16, 13, 0, tzinfo=UTC)
    s, _c, src = asyncio.run(A.decide_strike(None, _Stn(), _scan(peak), datetime(2026, 6, 16, 8, 0, tzinfo=UTC)))
    assert src == "fallback_peak" and s == peak - timedelta(minutes=P.STRIKE_FALLBACK_LEAD_MIN)


def test_decide_strike_llm_pre_dawn_is_clamped(monkeypatch):
    # the LLM (junk) proposes a 02:00 strike → the hard box forces it into the peak-anchored daytime window:
    # stage-1 can only mis-TIME within [peak−CONFIRM_LEAD, peak+MAX_LAG]∩daytime, never a pre-dawn strike.
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    _no_network(monkeypatch)

    async def _junk(*a, **k):
        return {"strike_utc": "2026-06-16T02:00Z", "reason": "junk early"}
    monkeypatch.setattr(A.agent, "propose_strike", _junk)
    peak = datetime(2026, 6, 16, 13, 0, tzinfo=UTC)
    s, _c, src = asyncio.run(A.decide_strike(None, _Stn(), _scan(peak), datetime(2026, 6, 16, 8, 0, tzinfo=UTC)))
    _sr, noon, ss, _ = day_sun(51.5, -0.1, MKT)
    assert src == "llm" and s >= noon and s >= peak - timedelta(minutes=P.STRIKE_CONFIRM_LEAD_MIN)


def test_decide_strike_llm_overshoot_is_bounded(monkeypatch):
    # the 19:15-overshoot bug: the LLM proposes a strike FAR past the peak → bounded to peak+MAX_LAG (or
    # sunset). We never strike deep past the peak where the temperature has already fallen.
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    _no_network(monkeypatch)

    async def _late(*a, **k):
        return {"strike_utc": "2026-06-16T18:00Z", "reason": "way too late"}
    monkeypatch.setattr(A.agent, "propose_strike", _late)
    peak = datetime(2026, 6, 16, 13, 0, tzinfo=UTC)
    s, _c, src = asyncio.run(A.decide_strike(None, _Stn(), _scan(peak), datetime(2026, 6, 16, 8, 0, tzinfo=UTC)))
    _sr, _noon, ss, _ = day_sun(51.5, -0.1, MKT)
    assert src == "llm" and s == min(peak + timedelta(minutes=P.STRIKE_MAX_LAG_MIN), ss)


def test_decide_strike_llm_in_window_respected(monkeypatch):
    # an LLM strike INSIDE the peak-anchored box is kept verbatim (peak + 15min).
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    _no_network(monkeypatch)

    async def _ok(*a, **k):
        return {"strike_utc": "2026-06-16T13:15Z", "reason": "just after the peak"}
    monkeypatch.setattr(A.agent, "propose_strike", _ok)
    peak = datetime(2026, 6, 16, 13, 0, tzinfo=UTC)
    s, _c, src = asyncio.run(A.decide_strike(None, _Stn(), _scan(peak), datetime(2026, 6, 16, 8, 0, tzinfo=UTC)))
    assert src == "llm" and s == datetime(2026, 6, 16, 13, 15, tzinfo=UTC)
