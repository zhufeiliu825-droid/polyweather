import asyncio
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "run"))

import run_daemon as rd  # noqa: E402
from polyweather.runtime.analyze import CityTick, Scan  # noqa: E402
from polyweather.models import Bucket, Comparator, Decision, EventState  # noqa: E402

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def _scan(peak_min_ahead):
    peak = None if peak_min_ahead is None else NOW.fromtimestamp(
        NOW.timestamp() + peak_min_ahead * 60, tz=timezone.utc)
    return Scan("EGLC", "London", date(2026, 6, 15), peak, 23.0)


def test_is_active_within_window():
    assert rd.is_active(_scan(20), NOW) is True           # 20min ahead → within the 50min window → active


def test_is_active_just_outside_window():
    assert rd.is_active(_scan(55), NOW) is False          # 55min ahead → outside the 50min window → not yet


def test_is_active_peak_passed():
    assert rd.is_active(_scan(-10), NOW) is False          # peak passed → not active


def test_is_active_no_peak():
    assert rd.is_active(_scan(None), NOW) is False


def test_is_active_strike_mode():
    from datetime import timedelta
    strike = NOW + timedelta(minutes=60)                  # 13:00Z
    cutoff = NOW + timedelta(minutes=180)                 # 15:00Z
    sc = _scan(30)                                        # peak 12:30Z — irrelevant in strike mode
    assert rd.is_active(sc, NOW, strike_at=strike, cutoff_at=cutoff) is False        # before strike−grace
    assert rd.is_active(sc, strike + timedelta(minutes=5), strike_at=strike, cutoff_at=cutoff) is True
    assert rd.is_active(sc, cutoff + timedelta(minutes=1), strike_at=strike, cutoff_at=cutoff) is False  # past cutoff


def test_stage1_strike_fires_only_within_confirm_lead(monkeypatch):
    # Part 3: stage-1 must fire at peak−CONFIRM_LEAD (~1h out), NOT at first sight. Far from the peak →
    # no strike decision yet; within CONFIRM_LEAD → decide_strike runs once and sets strike_at.
    import types
    from datetime import timedelta
    calls = []

    async def _spy(client, stn, sc, now):
        calls.append(now)
        return (now + timedelta(minutes=120), now + timedelta(minutes=200), "llm")   # far ahead → stays inactive
    monkeypatch.setattr(rd, "decide_strike", _spy)
    monkeypatch.setattr(rd, "save_state", lambda *a, **k: None)
    stn = types.SimpleNamespace(station_id="EGLC", city="London", verified=True)

    state = {}                                            # peak beyond CONFIRM_LEAD → must NOT fire
    asyncio.run(rd.decision_pass(None, [stn], {"EGLC": _scan(rd.STRIKE_CONFIRM_LEAD_MIN + 30)}, NOW, state))
    assert calls == [] and state["EGLC"].strike_decided is False

    state = {}                                            # peak within CONFIRM_LEAD → fires once
    asyncio.run(rd.decision_pass(None, [stn], {"EGLC": _scan(rd.STRIKE_CONFIRM_LEAD_MIN - 10)}, NOW, state))
    assert len(calls) == 1 and state["EGLC"].strike_decided is True and state["EGLC"].strike_at is not None


def test_build_pool_strike_is_peak_minus_1h():
    scans = {"EGLC": _scan(60)}                            # peak 13:00Z
    pool = rd.build_pool(scans)
    assert pool["cities"][0]["strike_utc"] == "2026-06-15 12:00Z"   # peak − 1h
    assert pool["cities"][0]["market_date"] == "2026-06-15"
    assert not pool["_dropped"]


def test_build_pool_drops_peakless():
    pool = rd.build_pool({"EGLC": _scan(None)})
    assert pool["cities"] == [] and pool["_dropped"] == ["EGLC"]


def test_state_persist_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "STATE_FILE", tmp_path / "city_state.json")
    state = {"EGLC": rd.CityState(mkt_day=date(2026, 6, 15), status="PLACED", order_id="0xabc",
                                  threshold=23, comparator=">=", label=">=23C", price=0.49,
                                  size_shares=10.0,
                                  last_tick=NOW, escalated=True, escalated_at=NOW,
                                  strike_at=NOW, cutoff_at=NOW, strike_decided=True, strike_src="llm")}
    rd.save_state(state)
    cs = rd.load_state()["EGLC"]
    assert cs.status == "PLACED" and cs.order_id == "0xabc" and cs.threshold == 23
    assert cs.comparator == ">=" and cs.mkt_day == date(2026, 6, 15) and cs.last_tick == NOW
    assert cs.size_shares == 10.0
    assert cs.escalated_at == NOW            # escalation time survives restart → stale replies stay detectable (H1)
    assert cs.strike_at == NOW and cs.cutoff_at == NOW and cs.strike_decided and cs.strike_src == "llm"


def test_load_state_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "STATE_FILE", tmp_path / "nope.json")
    assert rd.load_state() == {}


def test_offset_persist_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "OFFSET_FILE", tmp_path / "tg_offset.txt")
    assert rd.load_offset() is None                 # no file yet
    rd.save_offset(4242)
    assert rd.load_offset() == 4242                 # survives a restart → no replayed replies (H1)


# ---- _place_once: crash-safe persist (C1) / DRY_PLACED (M1) / ambiguous→UNRESOLVED (H2) ----

class _Stn:
    def __init__(self, sid, city):
        self.station_id, self.city = sid, city


def _bet():
    return Decision(city="seoul", station_id="RKSI", local_date=date(2026, 6, 15),
                    label="==25C", comparator=Comparator.EQ, threshold=25, token_yes="0x1",
                    action="BET", confidence=0.7,
                    market_yes=0.5, maker_price=0.49, health_ok=True, reason="agent picks 25")


def _place_setup(monkeypatch, tmp_path, *, dry, result):
    monkeypatch.setattr(rd, "STATE_FILE", tmp_path / "s.json")
    monkeypatch.setattr(rd.ex, "_dry_run", lambda: dry)
    monkeypatch.setattr(rd.ex, "place_from_decision", lambda d, now_iso: result)
    async def momentum_ok(client, decision, now):
        return True, {"available": True, "drop_pct": 0.0, "is_big_drop": False, "reason": None}
    monkeypatch.setattr(rd, "enforce_order_momentum", momentum_ok)
    sent = []

    async def fake_send(c, t, **k):
        sent.append((t, k.get("kind")))
        return True

    monkeypatch.setattr(rd.tg, "send", fake_send)
    return sent


def test_place_once_live_maker_rests_with_placed_at(tmp_path, monkeypatch):
    _place_setup(monkeypatch, tmp_path, dry=False,
                 result=rd.ex.OrderResult(True, False, "ok", order_id="OID1"))
    cs = rd.CityState(mkt_day=date(2026, 6, 15))
    state = {"RKSI": cs}
    asyncio.run(rd._place_once(None, _Stn("RKSI", "seoul"), cs, _bet(), "t", state, NOW))
    assert cs.status == "RESTING" and cs.order_id == "OID1" and cs.placed_at == NOW   # live GTD maker → polled
    assert cs.size_shares == _bet().size_shares
    # persisted INSIDE _place_once (status + order_id + placed_at) → a crash now can't re-place (C1) + the TTL clock survives
    rs = rd.load_state()["RKSI"]
    assert rs.status == "RESTING" and rs.order_id == "OID1" and rs.placed_at == NOW


def test_place_once_dry_marks_dry_placed(tmp_path, monkeypatch):
    _place_setup(monkeypatch, tmp_path, dry=True, result=rd.ex.OrderResult(True, True, "DRY_RUN"))
    cs = rd.CityState(mkt_day=date(2026, 6, 15))
    asyncio.run(rd._place_once(None, _Stn("RKSI", "seoul"), cs, _bet(), "t", {"RKSI": cs}, NOW))
    assert cs.status == "DRY_PLACED" and cs.order_id is None     # not "PLACED" → won't poison the live path (M1)


def test_place_once_ambiguous_becomes_unresolved(tmp_path, monkeypatch):
    sent = _place_setup(monkeypatch, tmp_path, dry=False,
                        result=rd.ex.OrderResult(False, False, "lost response", ambiguous=True))
    cs = rd.CityState(mkt_day=date(2026, 6, 15))
    asyncio.run(rd._place_once(None, _Stn("RKSI", "seoul"), cs, _bet(), "t", {"RKSI": cs}, NOW))
    assert cs.status == "UNRESOLVED" and cs.escalated         # maybe-live order → never auto-re-placed (H2)
    assert any("UNRESOLVED" in t for t, _ in sent)


def test_place_once_definite_failure_returns_no_order(tmp_path, monkeypatch):
    # full-auto: a definite no-order is NOT terminalized in _place_once (handle decides RETRY/SKIP); it only
    # ALERTS the account/order anomaly and must NOT escalate-to-human (no ESCALATED).
    sent = _place_setup(monkeypatch, tmp_path, dry=False,
                        result=rd.ex.OrderResult(False, False, "rejected: bad", ambiguous=False))
    cs = rd.CityState(mkt_day=date(2026, 6, 15))
    outcome = asyncio.run(rd._place_once(None, _Stn("RKSI", "seoul"), cs, _bet(), "t", {"RKSI": cs}, NOW))
    assert outcome == "no_order" and cs.status != "ESCALATED"
    assert any("未下单" in t for t, _ in sent)                  # the account/order anomaly is surfaced (未下单)


def test_place_once_momentum_block_never_calls_executor(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(rd, "STATE_FILE", tmp_path / "s.json")
    monkeypatch.setattr(rd.ex, "place_from_decision", lambda *a, **k: called.append(True))

    async def momentum_blocked(client, decision, now):
        decision.action = "ESCALATE"
        return False, {"available": True, "drop_pct": 0.5, "is_big_drop": True, "reason": None}

    monkeypatch.setattr(rd, "enforce_order_momentum", momentum_blocked)
    cs = rd.CityState(mkt_day=date(2026, 6, 15))
    outcome = asyncio.run(rd._place_once(None, _Stn("RKSI", "seoul"), cs, _bet(), "t", {"RKSI": cs}, NOW))
    assert outcome == "momentum_blocked" and called == []
    assert cs.status == "PENDING" and not (tmp_path / "s.json").exists()


# ---- poll_resting: the GTD maker lifecycle (fill / expiry+re-place / fail-closed) ----

_NO_HELD = object()   # sentinel → default held = {} (positions queried OK, we hold nothing) without a mutable default


def _poll_resting(monkeypatch, *, placed_min_ago, status_ret, cutoff_min_ahead=120,
                  held_ret=_NO_HELD, status="RESTING", token_yes="TOK22", threshold=22,
                  fill_records=None):
    from datetime import timedelta
    held = {} if held_ret is _NO_HELD else held_ret           # {}=not-held · {tok:..}=held · None=query failed
    cs = rd.CityState(mkt_day=date(2026, 6, 15), status=status, order_id="OID9", label="==22C",
                      token_yes=token_yes, threshold=threshold,
                      size_shares=10.0,
                      placed_at=NOW - timedelta(minutes=placed_min_ago),
                      cutoff_at=NOW + timedelta(minutes=cutoff_min_ahead))
    sent = []

    async def fake_send(c, t, **k):
        sent.append((t, k.get("kind")))
        return True
    monkeypatch.setattr(rd, "save_state", lambda *a, **k: None)
    monkeypatch.setattr(rd.ex, "order_status", lambda oid: status_ret)
    monkeypatch.setattr(rd.ex, "filled_size", lambda oid: None)
    monkeypatch.setattr(rd.ex, "positions_held", lambda: held)
    monkeypatch.setattr(rd, "_log_fill_once", lambda rec: fill_records.append(rec) if fill_records is not None else None)
    monkeypatch.setattr(rd.tg, "send", fake_send)
    asyncio.run(rd.poll_resting(None, [_Stn("RKSI", "seoul")], {}, {"RKSI": cs}, NOW))
    return cs, sent


def test_poll_position_held_marks_filled(monkeypatch):
    # GROUND TRUTH: get_order returns GONE for a filled order (verified live), but we HOLD the token →
    # positions are authoritative → FILLED (not 已退单/re-bid). This is the fix for "filled but shows 挂单中".
    held = {"TOK22": {"size": 10.0, "title": "…", "redeemable": False}}
    cs, sent = _poll_resting(monkeypatch, placed_min_ago=18, status_ret="GONE", held_ret=held)
    assert cs.status == "FILLED" and any("已成交" in t for t, _ in sent)


def test_poll_confirmed_fill_emits_structured_record(monkeypatch):
    held = {"TOK22": {"size": 7.125678, "title": "…", "redeemable": False}}
    records = []
    cs, _sent = _poll_resting(monkeypatch, placed_min_ago=3, status_ret="PARTIAL",
                              held_ret=held, fill_records=records)
    assert cs.status == "FILLED" and records == [{
        "ts": "2026-06-15T12:00:00Z", "strategy": "directional", "station": "RKSI", "city": "seoul",
        "market_date": "2026-06-15", "bucket": "==22C", "order_id": "OID9", "token": "TOK22",
        "status": "PARTIAL", "order_status": "PARTIAL", "planned_shares": 10.0,
        "filled_shares": 7.125678, "confirmed_by": "positions",
    }]


def test_fill_log_is_idempotent_by_order_id(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "FILLS_LOG", tmp_path / "fills.jsonl")
    rec = {"strategy": "directional", "order_id": "OID9", "status": "FILLED"}
    rd._log_fill_once(rec)
    rd._log_fill_once(rec)
    assert len(rd.FILLS_LOG.read_text().splitlines()) == 1


def test_reconcile_fill_logs_backfills_persisted_filled_state(tmp_path, monkeypatch):
    monkeypatch.setattr(rd.ex, "_dry_run", lambda: False)
    monkeypatch.setattr(rd.ex, "positions_held", lambda: {
        "TOK22": {"size": 7.125678, "title": "", "redeemable": False}})
    monkeypatch.setattr(rd, "ORDERS_LOG", tmp_path / "orders.jsonl")
    rd.ORDERS_LOG.write_text(json.dumps({
        "order_id": "OID9", "status": "LIVE", "side": "BUY", "type": "GTD-maker", "shares": 10.0}) + "\n")
    records = []
    monkeypatch.setattr(rd, "_log_fill_once", records.append)
    cs = rd.CityState(mkt_day=date(2026, 6, 15), status="FILLED", order_id="OID9", token_yes="TOK22",
                      label="==22C")
    changed = rd.reconcile_fill_logs({"RKSI": cs}, [_Stn("RKSI", "seoul")], NOW)
    assert changed and cs.size_shares == 10.0
    assert records[0]["status"] == "PARTIAL" and records[0]["filled_shares"] == 7.125678
    assert records[0]["confirmed_by"] == "positions"


def test_poll_legacy_placed_held_marks_filled(monkeypatch):
    # a legacy phase-1 PLACED with no stored token → matched by TITLE (city + bucket + day) → FILLED when held
    held = {"Z": {"size": 10.0, "title": "Will the highest temperature in Seoul be 22°C on June 15?",
                  "redeemable": False}}
    cs, sent = _poll_resting(monkeypatch, placed_min_ago=18, status_ret="GONE",
                             held_ret=held, status="PLACED", token_yes=None)
    assert cs.status == "FILLED" and any("已成交" in t for t, _ in sent)


def test_poll_positions_unknown_failclosed(monkeypatch):
    # NO-DOUBLE-BET: positions query failed (None) + GONE → can't confirm not-held → NEVER re-place (wait)
    cs, sent = _poll_resting(monkeypatch, placed_min_ago=rd.MAKER_TTL_MIN + rd.EXPIRY_BUFFER_MIN + 1,
                             status_ret="GONE", held_ret=None)
    assert cs.status == "RESTING" and cs.order_id == "OID9" and not sent


def test_poll_fill_marks_filled(monkeypatch):
    cs, sent = _poll_resting(monkeypatch, placed_min_ago=3, status_ret="FILLED")   # filled within the TTL
    assert cs.status == "FILLED" and any("已成交" in t for t, _ in sent)


def test_poll_within_ttl_waits(monkeypatch):
    cs, sent = _poll_resting(monkeypatch, placed_min_ago=3, status_ret="OPEN")     # 3min < TTL+2 → still resting
    assert cs.status == "RESTING" and not sent


def test_poll_expired_rebids_before_cutoff(monkeypatch):
    # only an AFFIRMATIVELY-TERMINAL status (EXPIRED/GONE/CANCELLED) past TTL+buffer re-bids — the prior order is gone
    cs, sent = _poll_resting(monkeypatch, placed_min_ago=18, status_ret="EXPIRED", cutoff_min_ahead=60)
    assert cs.status == "PENDING" and cs.order_id is None and cs.placed_at is None  # → re-place next decision pass
    assert any("已退单" in t for t, _ in sent)


def test_poll_open_past_expiry_never_rebids(monkeypatch):
    # NO-DOUBLE-BET regression: OPEN = the order is STILL LIVE on the book. Even past TTL+buffer (where a terminal
    # status WOULD re-bid), an OPEN order must NOT be re-placed — that would open a second live order. (Was: OPEN
    # was in the re-place set, so a GTD reported OPEN past TTL got a second order stacked on the live one.)
    age = rd.MAKER_TTL_MIN + rd.EXPIRY_BUFFER_MIN + 1                     # past the re-bid threshold, before stuck grace
    cs, sent = _poll_resting(monkeypatch, placed_min_ago=age, status_ret="OPEN")
    assert cs.status == "RESTING" and cs.order_id == "OID9" and not sent  # still live → wait, never re-placed


def test_poll_open_past_grace_alerts_not_replaces(monkeypatch):
    # still OPEN this long past its GTD = the exchange hasn't expired it → fail-closed: freeze + alert, never re-bid
    age = rd.MAKER_TTL_MIN + rd.STUCK_GRACE_MIN + 1
    cs, sent = _poll_resting(monkeypatch, placed_min_ago=age, status_ret="OPEN")
    assert cs.status == "UNRESOLVED" and cs.order_id == "OID9"            # frozen, NOT re-placed
    assert any("STUCK" in t for t, _ in sent)


def test_poll_expired_past_cutoff_skips(monkeypatch):
    cs, sent = _poll_resting(monkeypatch, placed_min_ago=18, status_ret="GONE", cutoff_min_ahead=-5)
    assert cs.status == "SKIP" and any("已退单" in t for t, _ in sent)             # past the peak → no re-place


def test_poll_error_within_grace_never_replaces(monkeypatch):
    # NO-DOUBLE-BET: past the TTL but the status is unconfirmable (ERROR) → wait, NEVER re-place
    age = rd.MAKER_TTL_MIN + rd.EXPIRY_BUFFER_MIN + 1                     # past expiry, before the stuck grace
    cs, sent = _poll_resting(monkeypatch, placed_min_ago=age, status_ret="ERROR")
    assert cs.status == "RESTING" and not sent


def test_poll_error_past_grace_alerts_not_replaces(monkeypatch):
    age = rd.MAKER_TTL_MIN + rd.STUCK_GRACE_MIN + 1                       # past the stuck grace → alert, frozen
    cs, sent = _poll_resting(monkeypatch, placed_min_ago=age, status_ret="ERROR")
    assert cs.status == "UNRESOLVED" and any("STUCK" in t for t, _ in sent)


def test_bets_for_digest_status_mapping():
    # the digest shows 挂单中 (RESTING/DRY_PLACED/legacy PLACED) vs 已成交 (FILLED); everything else → 未下注
    state = {
        "A": rd.CityState(mkt_day=date(2026, 6, 15), status="RESTING", threshold=22, price=0.49),
        "B": rd.CityState(mkt_day=date(2026, 6, 15), status="FILLED", threshold=23, price=0.30),
        "C": rd.CityState(mkt_day=date(2026, 6, 15), status="PENDING", threshold=24, price=0.20),
        "D": rd.CityState(mkt_day=date(2026, 6, 15), status="DRY_PLACED", threshold=25, price=0.10),
        "E": rd.CityState(mkt_day=date(2026, 6, 15), status="PLACED", threshold=26, price=0.40),
    }
    bets = rd._bets_for_digest(state)
    assert bets["A"]["status"] == "挂单中" and bets["B"]["status"] == "已成交"
    assert bets["D"]["status"] == "挂单中" and "C" not in bets            # PENDING → renders 未下注
    assert bets["E"]["status"] == "挂单中"                                # legacy phase-1 PLACED → still "挂单中", not 未下注


def test_legacy_placed_is_terminal():
    # a leftover phase-1 PLACED in a pre-phase-2 state file must be TERMINAL so reload never re-bets it (double-bet guard)
    assert "PLACED" in rd.TERMINAL


# ---- two-pass escalating-model flow: pass1(mini) no-bet → RETRY → pass2(gpt-5.4) no-bet → SKIP ----

def _tick(decision, *, peak_ahead_min=30):
    peak = NOW.fromtimestamp(NOW.timestamp() + peak_ahead_min * 60, tz=timezone.utc)
    return CityTick(station_id="RKSI", city="seoul", now=NOW, mkt_day=date(2026, 6, 15),
                    peak_utc=peak, predicted_max=24.0, remaining=24.0, running_max=21, fresh=True,
                    decision=decision)


def test_handle_pass1_nobet_arms_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "STATE_FILE", tmp_path / "s.json")
    cs = rd.CityState(mkt_day=date(2026, 6, 15))
    asyncio.run(rd.handle(None, _Stn("RKSI", "seoul"), _tick(None), cs, NOW, {"RKSI": cs}, 1))
    assert cs.status == "RETRY" and cs.pass1_at == NOW         # pass-1 declined → armed for the +20min pass-2


def test_handle_pass2_nobet_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "STATE_FILE", tmp_path / "s.json")
    cs = rd.CityState(mkt_day=date(2026, 6, 15), status="RETRY", pass1_at=NOW)
    asyncio.run(rd.handle(None, _Stn("RKSI", "seoul"), _tick(None), cs, NOW, {"RKSI": cs}, 2))
    assert cs.status == "SKIP"                                 # pass-2 also declined → real skip (silent)


def test_handle_peak_passed_skips_not_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "STATE_FILE", tmp_path / "s.json")
    cs = rd.CityState(mkt_day=date(2026, 6, 15))
    asyncio.run(rd.handle(None, _Stn("RKSI", "seoul"), _tick(None, peak_ahead_min=-5), cs, NOW, {"RKSI": cs}, 1))
    assert cs.status == "SKIP"                                 # window already closed → skip, no pointless retry


def test_handle_bet_places_no_retry(tmp_path, monkeypatch):
    _place_setup(monkeypatch, tmp_path, dry=True, result=rd.ex.OrderResult(True, True, "DRY_RUN"))
    cs = rd.CityState(mkt_day=date(2026, 6, 15))
    asyncio.run(rd.handle(None, _Stn("RKSI", "seoul"), _tick(_bet()), cs, NOW, {"RKSI": cs}, 1))
    assert cs.status == "DRY_PLACED"                           # a BET in pass-1 places immediately, no retry


def test_journal_writes_current_citytick_fields(tmp_path, monkeypatch):
    # regression: _journal referenced the REMOVED CityTick.calib_src → AttributeError live, losing the
    # journal entry + spamming a daemon failure (the order still placed). It must use only current fields.
    monkeypatch.setattr(rd, "JOURNAL_DIR", tmp_path / "journal")
    rd._journal(_tick(_bet()))                                  # must NOT raise on a current CityTick
    txt = (tmp_path / "journal" / "seoul.txt").read_text()
    assert "BET" in txt and "==25C" in txt and "running_max" in txt and "calib" not in txt


# ---- apply_replies: honor the escalated bucket + re-validate every gate (C2) ----

def _state_with(prices, running_max=21):
    bs = [Bucket(question=str(t), comparator=Comparator.EQ, threshold=t, unit="C", token_yes="y",
                 token_no="n", condition_id="x", price_yes=prices.get(t, 0.01),
                 best_bid=prices.get(t, 0.01), best_ask=prices.get(t, 0.01),
                 last_trade=prices.get(t, 0.01)) for t in range(20, 29)]
    st = EventState(city="seoul", local_date=date(2026, 6, 15),
                    buckets=bs, running_max=running_max)
    st.order_buckets()
    return st


def _reply_setup(monkeypatch, tmp_path, *, reply, state_obj, running_max=21, dry=True):
    monkeypatch.setattr(rd, "STATE_FILE", tmp_path / "s.json")
    monkeypatch.setattr(rd, "OFFSET_FILE", tmp_path / "off.txt")   # apply_replies persists the offset durably
    monkeypatch.setattr(rd.ex, "_dry_run", lambda: dry)
    placed = []
    monkeypatch.setattr(rd.ex, "place_from_decision",
                        lambda d, now_iso: placed.append(d) or rd.ex.OrderResult(True, dry, "DRY"))
    async def momentum_ok(client, decision, now):
        return True, {"available": True, "drop_pct": 0.0, "is_big_drop": False, "reason": None}
    monkeypatch.setattr(rd, "enforce_order_momentum", momentum_ok)
    sent = []

    async def fake_send(c, t, **k):
        sent.append((t, k.get("kind")))
        return True

    async def fake_poll(c, o):
        return [reply], 9

    peak = datetime(2026, 6, 15, 16, 0, tzinfo=timezone.utc)

    async def fake_analyze(c, stn, now):
        return CityTick(station_id="RKSI", city="seoul", now=now, mkt_day=date(2026, 6, 15),
                        peak_utc=peak, predicted_max=24.0, remaining=24.0, running_max=running_max,
                        fresh=True, state=state_obj)

    monkeypatch.setattr(rd.tg, "send", fake_send)
    monkeypatch.setattr(rd.tg, "poll_replies", fake_poll)
    monkeypatch.setattr(rd, "analyze_city", fake_analyze)
    return placed, sent


NOW_RUN = datetime(2026, 6, 15, 15, 0, tzinfo=timezone.utc)   # pre-peak
ESC_AT = datetime(2026, 6, 15, 14, 50, tzinfo=timezone.utc)              # when the escalation was sent
FRESH = int(datetime(2026, 6, 15, 14, 55, tzinfo=timezone.utc).timestamp())  # a reply AFTER the escalation


def test_reply_bet_places_the_escalated_bucket(tmp_path, monkeypatch):
    st = _state_with({22: 0.5, 21: 0.4})
    placed, _ = _reply_setup(monkeypatch, tmp_path, state_obj=st, reply={"city": "seoul", "action": "bet", "bucket": None, "msg_epoch": FRESH})
    cs = rd.CityState(mkt_day=date(2026, 6, 15), status="ESCALATED", threshold=22, comparator="==",
                      escalated_at=ESC_AT)
    off = asyncio.run(rd.apply_replies(None, {"seoul": _Stn("RKSI", "seoul")}, {"RKSI": cs}, NOW_RUN, None))
    assert off == 9
    assert len(placed) == 1 and placed[0].threshold == 22 and placed[0].action == "BET"
    assert cs.status == "DRY_PLACED"


def test_reply_bet_mismatched_bucket_refuses(tmp_path, monkeypatch):
    st = _state_with({22: 0.5, 21: 0.4})
    placed, sent = _reply_setup(monkeypatch, tmp_path, reply={"city": "seoul", "action": "bet", "bucket": "24"},
                                state_obj=st)
    cs = rd.CityState(mkt_day=date(2026, 6, 15), status="ESCALATED", threshold=22, comparator="==")
    asyncio.run(rd.apply_replies(None, {"seoul": _Stn("RKSI", "seoul")}, {"RKSI": cs}, NOW_RUN, None))
    assert placed == []                                       # typed 24 ≠ escalated 22 → no order
    assert any(k == "reply-mismatch" for _, k in sent) and cs.status == "ESCALATED"


def test_reply_bet_on_now_dead_bucket_refuses(tmp_path, monkeypatch):
    # escalated ==22, but by reply time running_max has risen to 23 → ==22 is DEAD → refuse, no order
    st = _state_with({22: 0.5, 24: 0.3}, running_max=23)
    placed, sent = _reply_setup(monkeypatch, tmp_path, state_obj=st, running_max=23,
                                reply={"city": "seoul", "action": "bet", "bucket": None, "msg_epoch": FRESH})
    cs = rd.CityState(mkt_day=date(2026, 6, 15), status="ESCALATED", threshold=22, comparator="==",
                      escalated_at=ESC_AT)
    asyncio.run(rd.apply_replies(None, {"seoul": _Stn("RKSI", "seoul")}, {"RKSI": cs}, NOW_RUN, None))
    assert placed == []                                       # dead bucket → hard gate refuses
    assert any(k == "reply-rejected" for _, k in sent) and cs.status == "ESCALATED"


def test_reply_skip_marks_skip(tmp_path, monkeypatch):
    st = _state_with({22: 0.5, 21: 0.4})
    placed, _ = _reply_setup(monkeypatch, tmp_path, reply={"city": "seoul", "action": "skip"},
                             state_obj=st)
    cs = rd.CityState(mkt_day=date(2026, 6, 15), status="ESCALATED", threshold=22, comparator="==")
    asyncio.run(rd.apply_replies(None, {"seoul": _Stn("RKSI", "seoul")}, {"RKSI": cs}, NOW_RUN, None))
    assert placed == [] and cs.status == "SKIP"


def test_reply_bet_on_pending_city_never_places(tmp_path, monkeypatch):
    # THE BLOCKER from the re-audit: a «bet» (e.g. a replayed/old message) on a never-escalated PENDING
    # city must NEVER place — only an ESCALATED city (with an escalated_at freshness anchor) is actionable.
    st = _state_with({22: 0.5, 21: 0.4})
    placed, _ = _reply_setup(monkeypatch, tmp_path, state_obj=st, reply={"city": "seoul", "action": "bet", "bucket": "22", "msg_epoch": FRESH})
    cs = rd.CityState(mkt_day=date(2026, 6, 15), status="PENDING")   # never escalated → no anchor
    asyncio.run(rd.apply_replies(None, {"seoul": _Stn("RKSI", "seoul")}, {"RKSI": cs}, NOW_RUN, None))
    assert placed == [] and cs.status == "PENDING"


def test_reply_bet_missing_send_time_rejected(tmp_path, monkeypatch):
    # fail-closed: a «bet» with no verifiable Telegram send-time can't be confirmed fresh → refuse (audit H1)
    st = _state_with({22: 0.5, 21: 0.4})
    placed, sent = _reply_setup(monkeypatch, tmp_path, state_obj=st, reply={"city": "seoul", "action": "bet", "bucket": None})   # no msg_epoch
    cs = rd.CityState(mkt_day=date(2026, 6, 15), status="ESCALATED", threshold=22, comparator="==",
                      escalated_at=ESC_AT)
    asyncio.run(rd.apply_replies(None, {"seoul": _Stn("RKSI", "seoul")}, {"RKSI": cs}, NOW_RUN, None))
    assert placed == [] and any(k == "reply-stale" for _, k in sent)


# ---- stale-replay guard: a «bet» re-returned after a restart must not place a 2nd order (audit H1) ----

def test_stale_bet_reply_not_re_placed_after_restart(tmp_path, monkeypatch):
    """The reported bug: after a restart the getUpdates offset is None, so Telegram re-returns the
    last unconfirmed «bet». For a city already acted on (PLACED) it must NOT place a 2nd order."""
    st = _state_with({22: 0.5, 21: 0.4})
    placed, _ = _reply_setup(monkeypatch, tmp_path,
                             reply={"city": "seoul", "action": "bet", "bucket": "22",
                                    "msg_epoch": int(NOW_RUN.timestamp())},
                             state_obj=st)
    cs = rd.CityState(mkt_day=date(2026, 6, 15), status="PLACED", order_id="0xLIVE",
                      threshold=22, comparator="==", escalated_at=NOW_RUN)
    off = asyncio.run(rd.apply_replies(None, {"seoul": _Stn("RKSI", "seoul")}, {"RKSI": cs},
                                       NOW_RUN, None))     # offset=None ⇒ simulated post-restart re-poll
    assert off == 9                                        # offset still advances (won't re-poll this again)
    assert placed == []                                    # already PLACED → ignored, no second order
    assert cs.status == "PLACED" and cs.order_id == "0xLIVE"


def test_stale_bet_reply_predating_escalation_rejected(tmp_path, monkeypatch):
    """Defense-in-depth when the city is still ESCALATED: a «bet» whose Telegram send-time is BEFORE
    the escalation (a replayed older reply, e.g. a prior day's confirmation re-surfacing) is rejected
    before any fresh analysis or order — using the message send-time, not the poll-time (audit H1)."""
    st = _state_with({22: 0.5, 21: 0.4})
    stale_epoch = int(NOW_RUN.timestamp()) - 3600          # sent an hour BEFORE the escalation
    placed, sent = _reply_setup(monkeypatch, tmp_path,
                                reply={"city": "seoul", "action": "bet", "bucket": "22",
                                       "msg_epoch": stale_epoch},
                                state_obj=st)
    cs = rd.CityState(mkt_day=date(2026, 6, 15), status="ESCALATED", threshold=22,
                      comparator="==", escalated_at=NOW_RUN)
    asyncio.run(rd.apply_replies(None, {"seoul": _Stn("RKSI", "seoul")}, {"RKSI": cs}, NOW_RUN, None))
    assert placed == []                                    # stale replay → no order
    assert any(k == "reply-stale" for _, k in sent)        # operator told why, with a re-confirm path
    assert cs.status == "ESCALATED"                        # still open for a genuine fresh confirmation


def test_fresh_bet_reply_after_escalation_still_places(tmp_path, monkeypatch):
    """Control: a «bet» sent AFTER the escalation (msg_epoch ≥ escalated_at) still places — the
    staleness guard must not block a genuine confirmation."""
    st = _state_with({22: 0.5, 21: 0.4})
    placed, _ = _reply_setup(monkeypatch, tmp_path,
                             reply={"city": "seoul", "action": "bet", "bucket": "22",
                                    "msg_epoch": int(NOW_RUN.timestamp()) + 60},
                             state_obj=st)
    cs = rd.CityState(mkt_day=date(2026, 6, 15), status="ESCALATED", threshold=22,
                      comparator="==", escalated_at=NOW_RUN)
    asyncio.run(rd.apply_replies(None, {"seoul": _Stn("RKSI", "seoul")}, {"RKSI": cs}, NOW_RUN, None))
    assert len(placed) == 1 and placed[0].threshold == 22 and placed[0].action == "BET"
    assert cs.status == "DRY_PLACED"


def test_log_row_dist_is_market_ladder_with_alive_flags():
    # the record now: each bucket's MARKET mid + alive flag (no model our_p), sorted by market price desc
    st = _state_with({26: 0.45, 25: 0.30, 24: 0.10}, running_max=25)
    t = CityTick(station_id="EGLC", city="London", now=NOW, mkt_day=date(2026, 6, 15),
                 peak_utc=NOW, predicted_max=26.0, remaining=25.9, running_max=25, fresh=True, state=st)
    dist = t.log_row()["dist"]
    assert dist[0]["b"] == "==26" and dist[0]["mkt"] == 0.45 and dist[0]["alive"] is True   # sorted by mkt desc
    by = {d["b"]: d for d in dist}
    assert by["==25"]["alive"] is True and by["==24"]["alive"] is False   # EQ alive iff threshold ≥ running_max
    assert "our_p" not in dist[0]                                         # no model probability anymore
