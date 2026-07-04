"""Locked-max sniper — pure decision logic + the per-city state machine (fakes; no network, no orders)."""
import asyncio
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "run"))

import run_snipe as rs  # noqa: E402
from polyweather.clob_executor import OrderResult  # noqa: E402
from polyweather.models import Bucket, Comparator  # noqa: E402

D = datetime(2026, 6, 15, 16, 0)          # naive local 16:00
PEAK = datetime(2026, 6, 15, 14, 30)


def _b(comp, thr, tok):
    return Bucket(question=f"{comp}{thr}", comparator=comp, threshold=thr, unit="C",
                  token_yes=tok, token_no=f"n{tok}", condition_id="c")


LADDER = ([_b(Comparator.LE, 20, "T20")]
          + [_b(Comparator.EQ, t, f"T{t}") for t in range(21, 30)]
          + [_b(Comparator.GE, 30, "T30")])


# ---- lock_decision (the forward-false-lock gate) ----

def test_lock_happy_path():
    ok, why = rs.lock_decision(D, PEAK, rem_max=24.0, running_max=26, fresh=True)
    assert ok and why == "locked"


def test_lock_rejects_forecast_can_still_reach():
    ok, why = rs.lock_decision(D, PEAK, rem_max=26.0, running_max=26, fresh=True)
    assert not ok and "still reach" in why                  # rem_max ≥ M → a later rise could re-bucket


def test_lock_rejects_stale_metar_and_missing_inputs():
    assert not rs.lock_decision(D, PEAK, 24.0, 26, fresh=False)[0]
    assert not rs.lock_decision(D, PEAK, 24.0, None, fresh=True)[0]
    assert not rs.lock_decision(D, None, 24.0, 26, fresh=True)[0]
    assert not rs.lock_decision(D, PEAK, None, 26, fresh=True)[0]


def test_lock_rejects_peak_not_passed_or_suspect():
    ok, _ = rs.lock_decision(datetime(2026, 6, 15, 14, 0), PEAK, 24.0, 26, True)
    assert not ok                                           # now ≤ peak
    ok, why = rs.lock_decision(D, datetime(2026, 6, 15, 7, 0), 24.0, 26, True)
    assert not ok and "suspect" in why                      # a 07:00 "peak" = rollover/garbage curve
    ok, _ = rs.lock_decision(datetime(2026, 6, 15, 12, 30), datetime(2026, 6, 15, 11, 0), 24.0, 26, True)
    assert not ok                                           # before 13:00 local never locks


# ---- winning_bucket / is_certain ----

def test_winning_bucket_eq_le_ge():
    assert rs.winning_bucket(LADDER, 26).token_yes == "T26"
    assert rs.winning_bucket(LADDER, 19).token_yes == "T20"     # ≤20 tail
    assert rs.winning_bucket(LADDER, 33).token_yes == "T30"     # ≥30 tail
    assert rs.winning_bucket([], 26) is None


def test_ge_already_reached_is_certain_eq_is_not():
    assert rs.is_certain(rs.winning_bucket(LADDER, 31), 31) is True    # GE reached → monotonic max can't lose
    assert rs.is_certain(rs.winning_bucket(LADDER, 26), 26) is False   # EQ needs the full lock
    assert rs.is_certain(rs.winning_bucket(LADDER, 19), 19) is False   # LE needs the full lock


# ---- plan_size (fee, ceiling, budgets, exchange minimums) ----

def test_plan_size_happy_and_fee_math():
    shares, why = rs.plan_size(0.80, depth_shares=100, spent_event=0.0, spent_day=0.0)
    assert why == "ok" and shares == 31                     # floor($25 / 0.80) = 31, within depth
    assert abs(rs.taker_fee(0.80) - 0.05 * 0.20) < 1e-12    # weather_fees: 5% × min(p, 1−p)


def test_plan_size_rejects_ask_over_ceiling_and_thin_edge():
    assert rs.plan_size(0.95, 100, 0, 0)[0] == 0            # > ceiling 0.90
    assert "edge" in rs.plan_size(0.94, 100, 0, 0)[1] or "ceiling" in rs.plan_size(0.94, 100, 0, 0)[1]


def test_plan_size_respects_budgets_and_depth():
    assert rs.plan_size(0.80, 100, spent_event=24.5, spent_day=0.0)[0] == 0     # event budget gone
    assert rs.plan_size(0.80, 100, 0.0, spent_day=149.5)[0] == 0                # daily cap gone
    shares, _ = rs.plan_size(0.80, depth_shares=7, spent_event=0.0, spent_day=0.0)
    assert shares == 7                                      # clamped to visible depth
    assert rs.plan_size(0.80, 4, 0, 0)[0] == 0              # < 5-share exchange minimum


# ---- state machine (fakes; no network) ----

def _stn():
    return SimpleNamespace(station_id="EGLC", city="London", timezone="UTC", latitude=51.5,
                           longitude=0.05, forecast_model=None, pm_series_id="10006", verified=True)


def _wire(monkeypatch, tmp_path, *, M=26, fresh=(True, 30.0), rem=24.0, ask=(0.80, 100.0),
          place=None):
    monkeypatch.setattr(rs, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(rs, "JOURNAL", tmp_path / "journal.jsonl")

    async def fake_rmax(client, icao, tz, day):
        return M, fresh[0], fresh[1]

    async def fake_curve(client, lat, lon, tz, forecast_days=2, model=None):
        day = date(2026, 6, 15)
        pts = [(datetime(day.year, day.month, day.day, 14, 30), 25.9)]
        if rem is not None:
            pts.append((datetime(day.year, day.month, day.day, 17, 0), rem))
        return pts

    async def fake_buckets(client, stn, today):
        return LADDER

    async def fake_book(client, token):
        return ask

    async def fake_tg(client, text, kind=""):
        return None

    monkeypatch.setattr(rs, "running_max_today", fake_rmax)
    monkeypatch.setattr(rs, "fetch_minutely_15", fake_curve)
    monkeypatch.setattr(rs, "todays_buckets", fake_buckets)
    monkeypatch.setattr(rs, "best_ask_depth", fake_book)
    monkeypatch.setattr(rs.tg, "send", fake_tg)
    if place is not None:
        monkeypatch.setattr(rs.ex, "place_from_decision", lambda d, now_iso, ttl_min=None: place)
    return datetime(2026, 6, 15, 16, 0, tzinfo=timezone.utc)   # UTC station → local 16:00


def _run(state, now, dry, log=None):
    asyncio.run(rs.snipe_city(None, _stn(), state, now, dry, log if log is not None else []))


def test_confirm_streak_gates_the_buy(monkeypatch, tmp_path):
    now = _wire(monkeypatch, tmp_path)
    state = {}
    _run(state, now, dry=True)
    key = "EGLC|2026-06-15"
    assert state[key]["streak"] == 1 and not (tmp_path / "journal.jsonl").exists()   # confirming, no action
    _run(state, now, dry=True)
    assert state[key]["streak"] == 2
    rows = [json.loads(x) for x in (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert rows[-1]["status"] == "DRY_WOULD_BUY" and rows[-1]["bucket"] == "==26C"


def test_false_lock_resets_streak(monkeypatch, tmp_path):
    now = _wire(monkeypatch, tmp_path)
    state = {"EGLC|2026-06-15": {"day": "2026-06-15", "streak": 1, "label": "==26C"}}
    monkeypatch.setattr(rs, "fetch_minutely_15",
                        lambda *a, **k: _coro([(datetime(2026, 6, 15, 14, 30), 25.9),
                                               (datetime(2026, 6, 15, 17, 0), 26.5)]))   # can still reach 26.5 ≥ 26
    _run(state, now, dry=True)
    assert state["EGLC|2026-06-15"]["streak"] == 0          # not locked → confirmation restarts


def _coro(v):
    async def _c(*a, **k):
        return v
    return _c(  )


def test_winner_moving_restarts_confirmation(monkeypatch, tmp_path):
    now = _wire(monkeypatch, tmp_path, M=27)                # max climbed 26 → 27 since last tick
    state = {"EGLC|2026-06-15": {"day": "2026-06-15", "streak": 3, "label": "==26C"}}
    _run(state, now, dry=True)
    st = state["EGLC|2026-06-15"]
    assert st["label"] == "==27C" and st["streak"] == 1     # new winner → confirm from scratch


def test_ge_already_reached_actionable_without_lock(monkeypatch, tmp_path):
    now = _wire(monkeypatch, tmp_path, M=31, rem=32.0)      # forecast still rising → NOT locked, but ≥30 is won
    state = {}
    _run(state, now, dry=True)
    st = state["EGLC|2026-06-15"]
    assert st["streak"] == 1 and st["label"] == ">=30C" and not st["locked"]


def test_live_fill_marks_done_and_journals(monkeypatch, tmp_path):
    place = OrderResult(True, False, "posted FAK", order_id="o1", filled=31.0)
    now = _wire(monkeypatch, tmp_path, place=place)
    state = {"EGLC|2026-06-15": {"day": "2026-06-15", "streak": 2, "label": "==26C"}}
    _run(state, now, dry=False)
    st = state["EGLC|2026-06-15"]
    assert st["status"] == "DONE" and st["filled"] == 31.0 and abs(st["cost"] - 24.8) < 1e-6
    rows = [json.loads(x) for x in (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert rows[-1]["status"] == "FILLED" and rows[-1]["order_id"] == "o1"
    _run(state, now, dry=False)                              # DONE → never touched again
    assert len((tmp_path / "journal.jsonl").read_text().splitlines()) == len(rows)


def test_live_ambiguous_freezes_key(monkeypatch, tmp_path):
    place = OrderResult(True, False, "posted FAK", order_id=None, filled=None)      # ok but unconfirmed
    now = _wire(monkeypatch, tmp_path, place=place)
    state = {"EGLC|2026-06-15": {"day": "2026-06-15", "streak": 2, "label": "==26C"}}
    _run(state, now, dry=False)
    st = state["EGLC|2026-06-15"]
    assert st["status"] == "AMBIGUOUS" and st["cost"] > 0    # full reserve held, key frozen
    _run(state, now, dry=False)
    assert st["status"] == "AMBIGUOUS"                       # never retried


def test_live_nofill_frees_reserve_and_retries(monkeypatch, tmp_path):
    place = OrderResult(False, False, "rejected: whatever")
    now = _wire(monkeypatch, tmp_path, place=place)
    state = {"EGLC|2026-06-15": {"day": "2026-06-15", "streak": 2, "label": "==26C"}}
    _run(state, now, dry=False)
    st = state["EGLC|2026-06-15"]
    assert st["status"] == "OPEN" and st["cost"] == 0.0 and st["attempts"] == 1
    for _ in range(rs.MAX_ATTEMPTS):
        _run(state, now, dry=False)
    assert state["EGLC|2026-06-15"]["status"] == "DONE"      # attempts exhausted → give up for the day


def test_startup_reconciles_crashed_pending(monkeypatch, tmp_path):
    monkeypatch.setattr(rs, "STATE", tmp_path / "state.json")
    (tmp_path / "state.json").write_text(json.dumps(
        {"EGLC|2026-06-15": {"day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                             "status": "PENDING", "cost": 24.8}}))
    st = rs.load_state()
    assert st["EGLC|2026-06-15"]["status"] == "AMBIGUOUS"    # crash mid-place → frozen, never re-fired
    assert st["EGLC|2026-06-15"]["cost"] == 24.8             # reserve kept (counts against the caps)


def test_outside_window_is_noop(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    state = {}
    asyncio.run(rs.snipe_city(None, _stn(), state, datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc), True, []))
    assert state == {}                                       # 09:00 local → outside 12-23 → untouched
