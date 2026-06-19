import asyncio
import json
from datetime import date, datetime, timezone

from polyweather.models import Bucket, Comparator, Decision
from polyweather.runtime import analyze as A


class _FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d


class _FakeClient:
    def __init__(self, data, status=200):
        self._d, self._status = data, status

    async def post(self, url, json=None, timeout=None):
        return _FakeResp(self._d, self._status)


def _bucket(thr, tok, bid, ask):
    return Bucket(question=str(thr), comparator=Comparator.EQ, threshold=thr, unit="C",
                  token_yes=tok, token_no="n", condition_id="x", best_bid=bid, best_ask=ask,
                  last_trade=bid)


def test_apply_clob_prices_overwrites_with_live_book():
    buckets = [_bucket(27, "t27", 0.08, 0.11), _bucket(28, "t28", 0.41, 0.42)]
    books = [
        {"asset_id": "t27", "bids": [{"price": "0.07", "size": "10"}], "asks": [{"price": "0.10", "size": "5"}]},
        {"asset_id": "t28", "bids": [{"price": "0.44", "size": "3"}], "asks": [{"price": "0.45", "size": "2"}]},
    ]
    ok = asyncio.run(A.apply_clob_prices(_FakeClient(books), buckets))
    assert ok
    assert buckets[0].best_bid == 0.07 and buckets[0].best_ask == 0.10   # CLOB, NOT Gamma 0.08/0.11
    assert buckets[1].best_bid == 0.44 and buckets[1].best_ask == 0.45
    assert abs(buckets[0].implied_p - 0.085) < 1e-9                      # mid off the live book


def test_apply_clob_prices_empty_book_sets_none():
    # no CLOB liquidity → both None → market_health's two-sided gate (L1) will reject an auto-bet
    buckets = [_bucket(25, "t25", 0.004, 0.01)]
    asyncio.run(A.apply_clob_prices(_FakeClient([{"asset_id": "t25", "bids": [], "asks": []}]), buckets))
    assert buckets[0].best_bid is None and buckets[0].best_ask is None


def test_apply_clob_prices_partial_omission_nulls_target():
    # r4 audit 硬伤: a successful /books response that OMITS our_top's token must NOT leave its stale
    # two-sided Gamma quote alive (which would pass the L1 gate and auto-bet off a lagging price).
    our_top = _bucket(27, "t27", 0.55, 0.57)        # Gamma two-sided quote on the favorite
    other = _bucket(28, "t28", 0.20, 0.22)
    books = [{"asset_id": "t28", "bids": [{"price": "0.19"}], "asks": [{"price": "0.21"}]}]   # t27 OMITTED
    ok = asyncio.run(A.apply_clob_prices(_FakeClient(books), [our_top, other]))
    assert ok                                        # CLOB responded (t28 priced)
    assert our_top.best_bid is None and our_top.best_ask is None   # omitted → nulled → L1 gate → ESCALATE
    assert our_top.implied_p == 0.55                 # falls back to last_trade, never a fake live book
    assert other.best_bid == 0.19 and other.best_ask == 0.21       # the returned token IS CLOB-priced


def test_apply_clob_prices_failure_nulls_to_escalate():
    # CLOB wholesale outage (non-200) → CLOB-or-None invariant: null the two-sided quote (escalate-safe),
    # never trade our_top off a stale Gamma book while the live book is unreachable ("都要用clob").
    buckets = [_bucket(27, "t27", 0.08, 0.11)]
    ok = asyncio.run(A.apply_clob_prices(_FakeClient([], status=500), buckets))
    assert ok is False
    assert buckets[0].best_bid is None and buckets[0].best_ask is None   # CLOB down → escalate, not auto-bet


def test_apply_clob_prices_no_tokens_noop():
    buckets = [_bucket(27, "", 0.08, 0.11)]                              # no token_yes
    assert asyncio.run(A.apply_clob_prices(_FakeClient([]), buckets)) is False


def test_running_max_clamps_implausible_temp(monkeypatch):
    # r6: a transposed-digit "92/14" parses to 92°C but must NOT inflate the dead-bucket floor (which
    # would mark every bucket dead → decide() None → the city silently disabled all day).
    reps = [
        ("EGLC 151120Z AUTO 24010KT 9999 NCD 21/14 Q1018", datetime(2026, 6, 15, 11, 20, tzinfo=timezone.utc)),
        ("EGLC 151220Z AUTO 24010KT 9999 NCD 22/14 Q1018", datetime(2026, 6, 15, 12, 20, tzinfo=timezone.utc)),
        ("EGLC 151235Z AUTO 24010KT 9999 NCD 92/14 Q1018", datetime(2026, 6, 15, 12, 35, tzinfo=timezone.utc)),
        ("EGLC 151250Z AUTO 24010KT 9999 NCD 23/14 Q1018", datetime(2026, 6, 15, 12, 50, tzinfo=timezone.utc)),
    ]

    async def fake_awc(client, icao, hours=30):
        return reps

    monkeypatch.setattr(A, "fetch_recent_awc", fake_awc)
    mx, _fresh, _age = asyncio.run(A.running_max_today(None, "EGLC", "Europe/London", date(2026, 6, 15)))
    assert mx == 23          # 92 clamped out → true max 23, not a bogus 92 floor


def test_running_max_without_usable_temperature_is_not_fresh(monkeypatch):
    reps = [
        ("EGLC 151220Z AUTO 24010KT 9999 NCD XX/14 Q1018", datetime(2026, 6, 15, 12, 20, tzinfo=timezone.utc)),
        ("EGLC 151235Z AUTO 24010KT 9999 NCD 92/14 Q1018", datetime(2026, 6, 15, 12, 35, tzinfo=timezone.utc)),
    ]

    async def fake_awc(client, icao, hours=30):
        return reps

    monkeypatch.setattr(A, "fetch_recent_awc", fake_awc)
    mx, fresh, age = asyncio.run(A.running_max_today(None, "EGLC", "Europe/London", date(2026, 6, 15)))
    assert mx is None and fresh is False and age is None


def test_analyze_city_fails_closed_when_running_max_missing(monkeypatch):
    async def no_floor(*args, **kwargs):
        return None, True, 1.0       # defense in depth: even an inconsistent source result must not reach Gamma/orders

    monkeypatch.setattr(A, "running_max_today", no_floor)

    class _Stn:
        station_id, city, timezone = "EGLC", "London", "Europe/London"

    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    peak = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    scan = A.Scan("EGLC", "London", date(2026, 6, 15), peak, 25.0,
                  curve=[(datetime(2026, 6, 15, 14, 0), 25.0)])
    tick = asyncio.run(A.analyze_city(None, _Stn(), now, scan))
    assert tick.decision is None and tick.running_max is None and "running_max unavailable" in tick.note


def test_scan_city_logs_open_meteo_failure(tmp_path, monkeypatch):
    # a curve-fetch failure must land a queryable record in source_failures.jsonl (audit follow-up)
    from polyweather import obslog
    monkeypatch.setattr(obslog, "FAILURE_LOG", tmp_path / "f.jsonl")

    async def _boom(*a, **k):
        raise RuntimeError("open-meteo down")
    monkeypatch.setattr(A, "fetch_minutely_15", _boom)

    class _Stn:
        station_id, city = "EGLC", "London"
        latitude, longitude, timezone = 51.5, 0.05, "Europe/London"
    sc = asyncio.run(A.scan_city(None, _Stn(), datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)))
    assert sc.peak_utc is None and "curve fetch failed" in sc.note
    rec = json.loads((tmp_path / "f.jsonl").read_text().strip())
    assert rec["source"] == "open_meteo" and rec["consequence"] == "skipped" and rec["station"] == "EGLC"


def test_agent_context_gives_the_agent_the_facts_to_choose(tmp_path, monkeypatch):
    # the agent gets the RAW forecast peak + curve + recent outcomes + momentum + the round(peak) baseline
    # for reference — these are the facts it CHOOSES the bucket from (no model our_p / σ anymore).
    monkeypatch.setattr(A, "DATA_DIR", tmp_path)
    (tmp_path / "settle.jsonl").write_text(json.dumps({
        "station": "EGLC", "market_date": "2026-06-15", "bet_bucket": "==23C",
        "resolved_bucket": "==21", "actual_max_awc": 21, "won": False}) + "\n")

    class _Stn:
        station_id, city = "EGLC", "London"
    mkt = date(2026, 6, 16)
    now = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
    peak = datetime(2026, 6, 16, 14, 0, tzinfo=timezone.utc)
    tick = A.CityTick("EGLC", "London", now, mkt, peak, 25.0, 25.4, 23, True)
    scan = A.Scan("EGLC", "London", mkt, peak, 25.0,
                  curve=[(datetime(2026, 6, 16, 13, 0), 24.0), (datetime(2026, 6, 16, 14, 0), 25.6)])
    baseline = Decision(city="London", station_id="EGLC", local_date=mkt, label="==25C",
                        comparator=Comparator.EQ, threshold=25, confidence=None,
                        market_yes=0.15, reason="round(25.4)→25")
    ctx = A._agent_context(_Stn(), tick, baseline, scan,
                           {"window_min": 5, "drop_pct": 0.4, "is_big_drop": True})
    assert ctx["forecast_peak_C"] == 25.4                                       # the RAW forecast peak (anchor)
    assert ctx["forecast_curve_local"] == [["13:00", 24.0], ["14:00", 25.6]]   # peak shape, from scan.curve
    assert "calib" not in ctx                                                   # no historical calibration block
    assert ctx["deterministic_baseline"]["bucket"] == "==25C"                   # the round(peak) reference pick
    assert ctx["recent_momentum"]["is_big_drop"] is True and ctx["recent_momentum"]["drop_pct"] == 0.4
    assert ctx["recent_outcomes"] == [{"date": "2026-06-15", "our_bet": "==23C",
                                       "resolved": "==21", "actual_max": 21, "won": False}]


def test_recent_inputs_graceful_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "DATA_DIR", tmp_path)                 # no settle.jsonl
    assert A._recent_outcomes("EGLC") == []


# ---- momentum (recent-drawdown) source + both-pass/final guards ----

class _HistClient:                                       # a .get returning a CLOB prices-history payload
    def __init__(self, history, status=200):
        self._h, self._status = history, status

    async def get(self, url, params=None, timeout=None):
        return _FakeResp({"history": self._h}, self._status)


class _BoomClient:
    async def get(self, url, params=None, timeout=None):
        raise RuntimeError("clob prices-history down")


_MNOW = datetime(2026, 6, 16, 13, 0, tzinfo=timezone.utc)


def test_momentum_big_drop_detected():
    end = int(_MNOW.timestamp())
    hist = [{"t": end - 300, "p": 0.05}, {"t": end - 120, "p": 0.05}, {"t": end, "p": 0.02}]
    m = asyncio.run(A._momentum(_HistClient(hist), "tok", _MNOW))
    assert m["available"] is True
    assert m["is_big_drop"] is True and m["drop_pct"] >= 0.30   # (0.05−0.02)/0.05 = 0.6


def test_momentum_stable_no_drop():
    end = int(_MNOW.timestamp())
    hist = [{"t": end - 300, "p": 0.050}, {"t": end, "p": 0.049}]
    m = asyncio.run(A._momentum(_HistClient(hist), "tok", _MNOW))
    assert m["is_big_drop"] is False                            # 2% drop < 30%


def test_momentum_stale_history_is_unavailable():
    end = int(_MNOW.timestamp())
    hist = [{"t": end - 420, "p": 0.050}, {"t": end - 360, "p": 0.049}]
    m = asyncio.run(A._momentum(_HistClient(hist), "tok", _MNOW))
    assert m["available"] is False and m["reason"] == "stale_history"


def test_momentum_reports_unavailable_on_error(tmp_path, monkeypatch):
    # The source function reports unavailable; the final pre-submit gate decides whether to fail closed.
    from polyweather import obslog
    monkeypatch.setattr(obslog, "FAILURE_LOG", tmp_path / "f.jsonl")
    m = asyncio.run(A._momentum(_BoomClient(), "tok", _MNOW))
    assert m["is_big_drop"] is False and m["drop_pct"] is None and m["available"] is False
    rec = json.loads((tmp_path / "f.jsonl").read_text().strip())
    assert rec["source"] == "clob" and rec["consequence"] == "momentum unavailable"


def _mbet():
    return Decision(city="London", station_id="EGLC", local_date=date(2026, 6, 16), label="==25C",
                    comparator=Comparator.EQ, threshold=25, confidence=0.6,
                    market_yes=0.05, maker_price=0.055, action="BET")


def test_momentum_guard_blocks_both_passes():
    big = {"window_min": 5, "drop_pct": 0.5, "is_big_drop": True, "available": True}
    d1 = A._momentum_guard(_mbet(), big)                        # pass-1 → hard block
    assert d1.action == "ESCALATE" and "momentum" in d1.reason and d1.momentum_5m == 0.5
    d2 = A._momentum_guard(_mbet(), big)                        # pass-2 cannot override the hard gate
    assert d2.action == "ESCALATE" and d2.momentum_5m == 0.5
    calm = {"window_min": 5, "drop_pct": 0.05, "is_big_drop": False}
    d3 = A._momentum_guard(_mbet(), calm)                       # no big drop → BET stands even at pass-1
    assert d3.action == "BET" and d3.momentum_5m == 0.05


def test_final_momentum_check_fails_closed_when_unavailable(monkeypatch):
    async def unavailable(*args, **kwargs):
        return {"window_min": 5, "drop_pct": None, "is_big_drop": False,
                "available": False, "reason": "thin_history"}

    monkeypatch.setattr(A, "_momentum", unavailable)
    d = _mbet()
    allowed, m = asyncio.run(A.enforce_order_momentum(None, d, _MNOW))
    assert allowed is False and d.action == "ESCALATE"
    assert m["reason"] == "thin_history" and "unavailable" in d.reason


def _conv_dec(**over):
    base = dict(city="seoul", station_id="RKSI", local_date=date(2026, 6, 19), label=">=30C",
                comparator=Comparator.GE, threshold=30, confidence=0.8,
                market_yes=0.50, maker_price=0.50, size_shares=10.0, action="BET")
    base.update(over)
    return Decision(**base)


def test_conviction_sizes_up_confident_cheap_bet(monkeypatch):
    # forecast peak 32 → >=30 is ~certain (fair ~0.99); huge edge × conf 0.8 → cap at 3× → 30 shares
    monkeypatch.setattr(A, "fair_calibration", lambda st, regime=None: {"bias": 0.0, "sigma": 1.0})
    assert A._conviction_size(_conv_dec(), forecast_peak=32.0, regime="clear") == 30.0


def test_conviction_keeps_base_when_model_disagrees(monkeypatch):
    # price 0.70 above our fair (~0.69) → edge ≤ 0 → never upsize a model-rich bet → base 10
    monkeypatch.setattr(A, "fair_calibration", lambda st, regime=None: {"bias": 0.0, "sigma": 1.0})
    assert A._conviction_size(_conv_dec(maker_price=0.70), forecast_peak=30.0, regime="clear") == 10.0


def test_conviction_keeps_base_without_confidence_or_calibration(monkeypatch):
    monkeypatch.setattr(A, "fair_calibration", lambda st, regime=None: {"bias": 0.0, "sigma": 1.0})
    assert A._conviction_size(_conv_dec(confidence=None), forecast_peak=32.0, regime="clear") == 10.0
    monkeypatch.setattr(A, "fair_calibration", lambda st, regime=None: None)
    assert A._conviction_size(_conv_dec(), forecast_peak=32.0, regime="clear") == 10.0


def test_conviction_keeps_dollar_cleared_base_for_cheap_bucket(monkeypatch):
    # a sub-10¢ bucket's base is already $1-cleared (e.g. 17 sh ≈ $1); a model-rich bet (edge≤0) must keep 17,
    # never collapse to a sub-$1 size — the $1 platform minimum holds through conviction sizing
    monkeypatch.setattr(A, "fair_calibration", lambda st, regime=None: {"bias": 0.0, "sigma": 2.0})
    d = _conv_dec(maker_price=0.80, size_shares=17.0)        # GE30 @ peak30 → fair ~0.60 < 0.80 → edge ≤ 0 → no upsize
    assert A._conviction_size(d, forecast_peak=30.0, regime="clear") == 17.0


def test_ladder_annotates_model_p(monkeypatch):
    # each market-ladder row gets model_p = calibrated P(bucket); a soft reference vs market mkt
    monkeypatch.setattr(A, "fair_calibration", lambda st, regime=None: {"bias": 0.0, "sigma": 1.0})
    ladder = [{"b": ">=30", "mkt": 0.5, "alive": True}, {"b": "==25", "mkt": 0.2, "alive": True}]
    out = A._ladder_with_model_p(ladder, "RKSI", peak=32.0, regime="clear")
    assert out[0]["model_p"] > 0.99 and 0.0 <= out[1]["model_p"] <= 1.0    # >=30 at peak 32 → ~certain
    # no calibration → ladder left unannotated (graceful)
    monkeypatch.setattr(A, "fair_calibration", lambda st, regime=None: None)
    assert "model_p" not in A._ladder_with_model_p([{"b": ">=30", "mkt": 0.5}], "RKSI", 32.0, "clear")[0]
