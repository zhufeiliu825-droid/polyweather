import asyncio
from datetime import datetime, timedelta, timezone

from polyweather import clob_executor as ex
from polyweather.rewards import farmer

UTC = timezone.utc
NOW = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)


def _order(token="T1", city="seoul", price=0.22, size=50.0, capital=11.0, peak_utc=None):
    return farmer.FarmOrder(city=city, station_id="RKSI", comparator="==", threshold=27, market_date="2026-06-18",
                            bucket="==27C", token_yes=token, price=price, size=size, capital=capital,
                            daily_rate=192.0, competition=400.0, est_share=0.11, est_daily_reward=21.3,
                            lean=0.7, hours_to_peak=20.0, mid=price, peak_utc=peak_utc, fair_yes=None)


def _resting(token="T1", placed_min_ago=10, held_at_place=0.0, status="RESTING", capital=11.0):
    return {token: {"status": status, "order_id": "o1", "city": "seoul", "bucket": "==27C", "capital": capital,
                    "price": 0.22, "size": 50.0, "held_at_place": held_at_place,
                    "placed_at": (NOW - timedelta(minutes=placed_min_ago)).isoformat()}}


def _held(token="T1", size=50.0):
    return {token: {"size": size, "title": "", "redeemable": False}}


def _filled(token="T1", cost=0.22, held_at_place=0.0, held_at_fill=50.0, last_mid=0.22, size=50.0,
            sell_order_id=None, sell_pending=False, sell_price=None, sell_mode=None, sell_size=0.0, flatten_since=None):
    return {token: {"status": "FILLED", "order_id": "b1", "city": "seoul", "bucket": "==27C",
                    "station_id": "RKSI", "comparator": "==", "threshold": 27, "market_date": "2026-06-18",
                    "price": cost, "size": size, "capital": round(size * cost, 2), "held_at_place": held_at_place,
                    "cost": cost, "held_at_fill": held_at_fill, "last_mid": last_mid,
                    "sell_order_id": sell_order_id, "sell_pending": sell_pending, "sell_price": sell_price,
                    "sell_mode": sell_mode, "sell_size": sell_size, "flatten_since": flatten_since}}


def _ambig_placeholder(token="T1"):
    return {token: {"status": "RESTING", "order_id": None, "ambiguous": True, "city": "seoul", "bucket": "==27C",
                    "capital": 11.0, "price": 0.22, "size": 50.0, "held_at_place": 0.0, "station_id": "RKSI",
                    "comparator": "==", "threshold": 27, "market_date": "2026-06-18", "mid": 0.22, "peak_utc": None}}


def _t(**over):
    base = dict(city="seoul", station_id="RKSI", comparator="==", threshold=27, market_date="2026-06-18",
                bucket="==27C", daily_rate=192.0, min_size=50.0, max_spread=4.5, mid=0.22, competition=400.0,
                hours_to_peak=5.0, our_yes_lean=0.7, token_yes="0xtok", score=1.0, spread=0.01, devig_sum=1.0,
                fair_yes=0.30)
    base.update(over)
    return base


def test_select_picks_edge_aligned_and_computes_economics():
    orders = farmer.select([_t()], capital_cap=50, max_targets=5)
    assert len(orders) == 1
    o = orders[0]
    assert o.city == "seoul" and o.bucket == "==27C" and o.token_yes == "0xtok"
    assert abs(o.price - 0.20) < 1e-9 and o.size == 50.0          # passive bid: min(best_bid 0.215, mid−2¢ 0.20) → 0.20
    assert abs(o.capital - 10.0) < 1e-9                            # 50 × 0.20
    assert o.est_share == round(50 / 450, 4)                       # 50 / (competition 400 + 50), rounded
    assert o.est_daily_reward == round(50 / 450 * 192, 2)          # share × the pool


def test_select_skips_low_lean_and_extreme_price():
    assert farmer.select([_t(our_yes_lean=0.2)], capital_cap=50, max_targets=5) == []    # below the lean floor
    assert farmer.select([_t(mid=0.05)], capital_cap=50, max_targets=5) == []            # extreme price → needs two-sided
    assert farmer.select([_t(mid=0.95)], capital_cap=50, max_targets=5) == []
    # no h2peak gate any more; entry_eligible enforces tomorrow+ and held positions use separate context
    assert len(farmer.select([_t(hours_to_peak=1.0)], capital_cap=50, max_targets=5)) == 1


def test_select_rejects_today_valuation_context_for_new_buy():
    assert farmer.select([_t(entry_eligible=False)], capital_cap=50, max_targets=5) == []


def test_select_skips_unhealthy_book():
    assert farmer.select([_t(spread=0.40)], capital_cap=50, max_targets=5) == []         # placeholder book (wide spread)
    assert farmer.select([_t(spread=None)], capital_cap=50, max_targets=5) == []         # one-sided/empty book
    assert farmer.select([_t(devig_sum=5.5)], capital_cap=50, max_targets=5) == []       # empty market (de-vig ≫ 1)
    assert farmer.select([_t(devig_sum=None)], capital_cap=50, max_targets=5) == []      # no de-vig → fail closed


def test_select_ev_gate_drops_rich_bucket():
    # mid 0.40 → bid ≈ 0.38, but calibrated fair is only 0.24 → negative edge → drop
    assert farmer.select([_t(mid=0.40, spread=0.01, fair_yes=0.24)], capital_cap=50, max_targets=5) == []


def test_select_edge_gate_keeps_bid_three_cents_cheap():
    # mid 0.40 → passive bid 0.38; fair 0.41 gives the required +3¢ edge.
    assert len(farmer.select([_t(mid=0.40, spread=0.01, fair_yes=0.41)], capital_cap=50, max_targets=5)) == 1


def test_select_edge_gate_fails_closed_without_fair():
    # No calibrated value means we cannot prove that the bid is cheap.
    assert farmer.select([_t(mid=0.40, spread=0.01, fair_yes=None)], capital_cap=50, max_targets=5) == []


def test_select_sizes_up_tight_high_edge_bucket():
    # big edge (0.10) + tight σ (0.5) → conviction caps at MAX_SIZE_MULT → size = reward min × MAX
    t = _t(fair_yes=0.30)                                                    # bid 0.20 → edge 0.10
    t["calibration"] = {"sigma": 0.5, "bias": 0.0}
    o = farmer.select([t], capital_cap=1000, max_targets=5)[0]
    assert o.size == round(50 * farmer.MAX_SIZE_MULT)


def test_select_floors_noisy_bucket_to_reward_min():
    # min edge (0.03) + noisy σ (1.6, Seoul-like) → conviction floored at 1× → size stays the reward min
    t = _t(fair_yes=0.23)                                                    # bid 0.20 → edge 0.03
    t["calibration"] = {"sigma": 1.6, "bias": 0.0}
    o = farmer.select([t], capital_cap=1000, max_targets=5)[0]
    assert o.size == 50.0


def test_select_no_calibration_keeps_reward_min():
    # no σ available → no conviction tilt → size = reward min (the prior default behavior)
    o = farmer.select([_t()], capital_cap=1000, max_targets=5)[0]            # _t() carries no calibration
    assert o.size == 50.0


def test_select_never_below_reward_min_even_with_small_size_mult():
    # size_mult < 1 must NOT push the bid below the market's reward min (else the bid earns no reward)
    o = farmer.select([_t()], capital_cap=1000, max_targets=5, size_mult=0.5)[0]
    assert o.size == 50.0                                                    # round(50×0.5)=25 → floored back up to 50


def test_select_edge_participates_in_reward_ranking():
    # B has a lower raw reward score but much more price edge, so combined score×edge ranks it first.
    a = _t(city="a", token_yes="A", score=1.0, fair_yes=0.24)   # bid .20, edge .04
    b = _t(city="b", token_yes="B", score=0.6, fair_yes=0.32)   # bid .20, edge .12
    orders = farmer.select([a, b], capital_cap=10, max_targets=1)
    assert [o.token_yes for o in orders] == ["B"]


def test_select_keeps_top2_buckets_per_event():
    # three buckets, SAME event (seoul 6-18) → keep the 2 highest-lean, drop the 3rd (the peak's straddling buckets)
    ts = [_t(threshold=27, bucket="==27C", token_yes="A", our_yes_lean=0.6),
          _t(threshold=28, bucket="==28C", token_yes="B", our_yes_lean=0.4),
          _t(threshold=26, bucket="==26C", token_yes="C", our_yes_lean=0.35)]
    orders = farmer.select(ts, capital_cap=1000, max_targets=10)
    assert {o.token_yes for o in orders} == {"A", "B"}                                    # 0.35 one dropped (top-2 only)


def test_select_two_events_each_get_their_own_top2():
    def mk(city, thr, tok, lean):
        return _t(city=city, threshold=thr, bucket=f"=={thr}C", token_yes=tok, our_yes_lean=lean)
    ts = [mk("seoul", 27, "S27", 0.6), mk("seoul", 28, "S28", 0.4), mk("seoul", 26, "S26", 0.3),
          mk("paris", 20, "P20", 0.7), mk("paris", 21, "P21", 0.45), mk("paris", 19, "P19", 0.35)]
    orders = farmer.select(ts, capital_cap=1000, max_targets=10)
    assert {o.token_yes for o in orders} == {"S27", "S28", "P20", "P21"}                  # each event contributes its top-2


def test_select_respects_capital_cap():
    # two ~$11 buckets; a $15 cap fits only the first (greedy by score order)
    orders = farmer.select([_t(city="a"), _t(city="b")], capital_cap=15, max_targets=5)
    assert len(orders) == 1 and orders[0].city == "a"


def test_select_respects_max_targets():
    orders = farmer.select([_t(city="a"), _t(city="b"), _t(city="c")], capital_cap=1000, max_targets=2)
    assert len(orders) == 2


def test_select_caps_price_below_executor_ceiling():
    # a reward bucket priced ≥0.85 is skipped — the executor's _validate rejects a bid ≥ the 0.85 price ceiling
    assert farmer.select([_t(mid=0.88)], capital_cap=1000, max_targets=5) == []


def test_to_decision_is_a_maker_buy_yes():
    o = farmer.select([_t()], capital_cap=50, max_targets=1)[0]
    d = farmer.to_decision(o)
    assert d.action == "BET" and d.side == "BUY_YES" and d.order_type == "maker"
    assert d.token_yes == "0xtok" and d.threshold == 27 and d.comparator.value == "=="
    assert d.maker_price == o.price and d.size_shares == o.size


def test_run_cycle_requests_today_context_without_buying_it(monkeypatch):
    calls = []

    async def fake_scan(client, stations, now, **kwargs):
        calls.append(kwargs)
        return [_t(entry_eligible=False)]

    async def fake_advice(client, targets, now):
        return {}, ""

    monkeypatch.setattr(farmer, "scan", fake_scan)
    monkeypatch.setattr(farmer, "hourly_advice", fake_advice)
    monkeypatch.setattr(farmer, "apply_advice", lambda targets, cache, now: targets)
    monkeypatch.setattr(farmer, "load_state", lambda: _filled(token="0xtok"))
    monkeypatch.setattr(farmer, "save_state", lambda state: None)
    monkeypatch.setattr(farmer.ex, "_dry_run", lambda: True)
    monkeypatch.setattr(farmer.ex, "positions_held", lambda: {})
    state, _log = asyncio.run(farmer.run_cycle(None, [], NOW, capital_cap=50, max_targets=5))
    assert calls == [{"context_tokens": {"0xtok"}}]
    assert state == {}


def test_place_orders_dry_logs_no_live(monkeypatch):
    # DRY: place_orders goes through the executor in DRY_RUN → an ok+dry result, no live submit
    monkeypatch.setenv("POLY_DRY_RUN", "1")
    o = farmer.select([_t()], capital_cap=50, max_targets=1)[0]
    [(order, res)] = farmer.place_orders([o], now_iso="t")
    assert order is o and res.ok and res.dry_run


# ---- plan_cycle: BUY side (RESTING) ----

def test_plan_cycle_places_new_target_under_cap():
    _ns, to_place, to_cancel, to_sell, _log = farmer.plan_cycle([_order()], held={}, state={}, capital_cap=50)
    assert [o.token_yes for o in to_place] == ["T1"] and to_cancel == [] and to_sell == []


def test_plan_cycle_leaves_an_in_band_resting_target_alone():
    ns, to_place, to_cancel, _ts, _log = farmer.plan_cycle([_order(price=0.22)], held={}, state=_resting(), capital_cap=50)
    assert to_place == [] and to_cancel == [] and ns["T1"]["status"] == "RESTING"


def test_plan_cycle_marks_filled_and_snapshots_stage2():
    ns, to_place, to_cancel, to_sell, log = farmer.plan_cycle([_order()], held=_held(size=50),
                                                              state=_resting(held_at_place=0.0), capital_cap=50)
    e = ns["T1"]
    assert e["status"] == "FILLED" and to_place == [] and to_cancel == [] and to_sell == []
    assert e["cost"] == 0.22 and e["held_at_fill"] == 50.0 and e["sell_order_id"] is None and any("FILLED" in ln for ln in log)
    assert e["buy_cancel_pending"] is True and e["buy_order_id"] == "o1"


def test_resting_fill_snapshots_current_hourly_advice_not_old_entry_fair():
    context = {"T1": {"mid": 0.23, "fair_yes": 0.35, "forecast_peak_adjusted": 29.0,
                      "llm_advice": {"adjustment_C": 2.0},
                      "calibration": {"source": "llm_hourly", "bias": -2.0}}}
    ns, _tp, _tc, _ts, _log = farmer.plan_cycle([], held=_held(size=50), state=_resting(),
                                                capital_cap=50, market_context=context)
    entry = ns["T1"]
    assert entry["status"] == "FILLED" and entry["fair_at_fill"] == 0.35
    assert entry["llm_advice"]["adjustment_C"] == 2.0
    assert entry["forecast_peak_adjusted"] == 29.0


def test_plan_cycle_cancels_buy_remainder_before_any_sell():
    st = _filled()
    st["T1"].update(buy_cancel_pending=True, buy_order_id="b1")
    _ns, _tp, to_cancel, to_sell, log = farmer.plan_cycle([], held=_held(size=20), state=st, capital_cap=50)
    assert to_sell == []
    assert len(to_cancel) == 1 and to_cancel[0]["kind"] == "buy_after_fill"
    assert any("BUY-REMAINDER-CANCEL" in ln for ln in log)


def test_plan_cycle_fill_ignores_preexisting_holdings():
    ns, _tp, to_cancel, _ts, _log = farmer.plan_cycle([_order()], held=_held(size=50),
                                                      state=_resting(held_at_place=50.0), capital_cap=50)
    assert ns["T1"]["status"] == "RESTING" and to_cancel == []


def test_plan_cycle_reprices_when_bid_drifts_past_threshold():
    _ns, _tp, to_cancel, _ts, log = farmer.plan_cycle([_order(price=0.30)], held={}, state=_resting(), capital_cap=50)
    assert len(to_cancel) == 1 and to_cancel[0]["token"] == "T1" and to_cancel[0]["replace"].price == 0.30
    assert any("REPRICE" in ln for ln in log)


def test_plan_cycle_small_drift_leaves_it():
    _ns, _tp, to_cancel, _ts, _log = farmer.plan_cycle([_order(price=0.23)], held={}, state=_resting(), capital_cap=50)
    assert to_cancel == []


def test_ambiguous_buy_placeholder_held_while_target():
    # ambiguous placeholder (no order_id) + still a target → HELD (not repriced → no double-bet) (verify-r3 #5)
    ns, _tp, to_cancel, _ts, _log = farmer.plan_cycle([_order(price=0.30)], held={},
                                                      state=_ambig_placeholder(), capital_cap=50)
    assert "T1" in ns and to_cancel == []                                       # kept; no second bid stacked


def test_ambiguous_buy_placeholder_reaped_when_target_drops():
    # once the bucket drops from targets, the ambiguous placeholder is EXITed (→ _execute pops it) → can't leak forever (verify-r4)
    _ns, _tp, to_cancel, _ts, log = farmer.plan_cycle([], held={}, state=_ambig_placeholder(), capital_cap=50)
    assert len(to_cancel) == 1 and to_cancel[0]["replace"] is None and any("EXIT" in ln for ln in log)


def test_ambiguous_buy_placeholder_still_adopts_a_fill():
    # the placeholder is held, but if positions show it FILLED it is still adopted into Stage 2 (not stuck forever)
    ns, _tp, _tc, _ts, log = farmer.plan_cycle([], held=_held(size=50), state=_ambig_placeholder(), capital_cap=50)
    assert ns["T1"]["status"] == "FILLED" and any("FILLED" in ln for ln in log)


def test_plan_cycle_exits_when_no_longer_a_target():
    _ns, _tp, to_cancel, _ts, log = farmer.plan_cycle([], held={}, state=_resting(), capital_cap=50)
    assert len(to_cancel) == 1 and to_cancel[0]["token"] == "T1" and to_cancel[0]["replace"] is None
    assert any("EXIT" in ln for ln in log)


def test_plan_cycle_respects_capital_cap():
    _ns, to_place, _tc, _ts, _log = farmer.plan_cycle([_order(token="A"), _order(token="B")], held={},
                                                      state=_resting(token="A"), capital_cap=15)
    assert to_place == []


def test_plan_cycle_filled_capital_counts_toward_cap():
    # a FILLED position (cost 0.22 × 50 = $11 held) counts toward the cap → a fresh $10 bid no longer fits a $15 cap
    _ns, to_place, _tc, _ts, _log = farmer.plan_cycle([_order(token="B", price=0.20, capital=10.0)],
                                                      held=_held("A", 50), state=_filled(token="A"), capital_cap=15)
    assert to_place == []                                                        # $11 held + $10 bid = $21 > $15 → skip
    # but with room for both ($25 cap), the fresh bid still places
    _ns2, to_place2, _tc2, _ts2, _log2 = farmer.plan_cycle([_order(token="B", price=0.20, capital=10.0)],
                                                           held=_held("A", 50), state=_filled(token="A"), capital_cap=25)
    assert [o.token_yes for o in to_place2] == ["B"]


# ---- plan_cycle: FILLED-position Stage 2 (scalp / flatten / close) ----

def test_filled_legacy_without_fair_uses_minimum_take_profit():
    _ns, _tp, _tc, to_sell, _log = farmer.plan_cycle([_order(price=0.22)], held=_held(size=50), state=_filled(), capital_cap=50)
    assert len(to_sell) == 1 and to_sell[0]["mode"] == "TAKE_PROFIT"
    assert abs(to_sell[0]["price"] - 0.24) < 1e-9 and to_sell[0]["size"] == 50.0


def test_filled_does_not_flatten_when_not_in_top_n():
    # Top-N controls new buys only. A held bucket stays on its take-profit thesis when ranking drops it.
    _ns, _tp, _tc, to_sell, _log = farmer.plan_cycle([], held=_held(size=50), state=_filled(last_mid=0.20), capital_cap=50)
    assert len(to_sell) == 1 and to_sell[0]["mode"] == "TAKE_PROFIT" and abs(to_sell[0]["price"] - 0.24) < 1e-9


def test_filled_flattens_when_fresh_fair_breaks_thesis():
    # Price weakness alone is not a stop; fresh model fair below cost−3¢ invalidates the thesis.
    context = {"T1": {"mid": 0.10, "fair_yes": 0.15}}
    _ns, _tp, _tc, to_sell, _log = farmer.plan_cycle([], held=_held(size=50),
                                                     state=_filled(cost=0.22), capital_cap=50,
                                                     market_context=context)
    assert len(to_sell) == 1 and to_sell[0]["mode"] == "FLATTEN" and abs(to_sell[0]["price"] - 0.10) < 1e-9


def test_filled_dynamic_take_profit_captures_half_entry_edge():
    st = _filled(cost=0.20)
    st["T1"].update(fair_at_fill=0.32, fair_yes=0.32, model_edge=0.12)
    context = {"T1": {"mid": 0.22, "fair_yes": 0.32}}
    _ns, _tp, _tc, to_sell, _log = farmer.plan_cycle([], held=_held(size=50), state=st,
                                                     capital_cap=50, market_context=context)
    assert to_sell[0]["mode"] == "TAKE_PROFIT"
    assert abs(to_sell[0]["price"] - 0.26) < 1e-9               # cost + half of 12¢ edge


def test_filled_closes_on_full_sell_fill():
    ns, _tp, to_cancel, to_sell, log = farmer.plan_cycle([], held={}, state=_filled(sell_order_id="s1", sell_size=50.0), capital_cap=50)
    assert "T1" not in ns and to_sell == [] and any("CLOSED" in ln for ln in log)
    assert len(to_cancel) == 1 and to_cancel[0].get("kind") == "sell"


def test_filled_partial_sell_reposts_smaller():
    # sell of 50 resting; holding now 30 (sold 20) → NOT closed; sell_size shrinks → reprice to 30
    st = _filled(sell_order_id="s1", sell_price=0.24, sell_mode="SCALP", sell_size=50.0)
    _ns, _tp, to_cancel, _ts, _log = farmer.plan_cycle([_order(price=0.22)], held=_held(size=30), state=st, capital_cap=50)
    assert len(to_cancel) == 1 and to_cancel[0].get("kind") == "sell" and to_cancel[0]["resell"]["size"] == 30.0


def test_filled_sell_size_floors_fractional_position():
    # Partial fills leave six-decimal token balances. Never round 14.058825 up to an unsellable 14.06.
    _ns, _tp, _tc, to_sell, _log = farmer.plan_cycle(
        [_order(price=0.22)], held=_held(size=14.058825), state=_filled(held_at_fill=20.0), capital_cap=50)
    assert to_sell[0]["size"] == 14.05


def test_filled_no_oversell_or_false_close_on_shared_funder():
    # a 20-share sell rests (held_at_fill frozen 20); a co-hold inflates cur to 80 → no false CLOSE, no oversell, no churn
    st = _filled(held_at_place=0.0, held_at_fill=20.0, sell_order_id="s1", sell_price=0.24,
                 sell_mode="TAKE_PROFIT", sell_size=20.0)
    ns, _tp, to_cancel, _ts, _log = farmer.plan_cycle([_order(price=0.22)], held=_held(size=80), state=st, capital_cap=50)
    assert "T1" in ns and to_cancel == []


def test_filled_ambiguous_sell_blocks_repost():
    st = _filled(sell_pending=True, sell_price=0.24, sell_mode="SCALP", sell_size=50.0)
    _ns, _tp, to_cancel, to_sell, log = farmer.plan_cycle([_order(price=0.22)], held=_held(size=50), state=st, capital_cap=50)
    assert to_sell == [] and to_cancel == [] and any("PENDING" in ln for ln in log)


def test_filled_fail_closed_when_positions_none():
    _ns, _tp, to_cancel, to_sell, _log = farmer.plan_cycle([_order(price=0.22)], held=None, state=_filled(), capital_cap=50)
    assert to_sell == [] and to_cancel == []


def test_filled_skips_sub_min_shares():
    _ns, _tp, _tc, to_sell, log = farmer.plan_cycle([_order(price=0.22)], held=_held(size=2),
                                                    state=_filled(held_at_fill=2.0), capital_cap=50)
    assert to_sell == [] and any("SELL-SKIP" in ln for ln in log)


def test_take_profit_holds_partial_below_reward_minimum():
    st = _filled(held_at_fill=10.0, size=20.0)
    st["T1"]["reward_min_size"] = 20.0
    _ns, _tp, _tc, to_sell, log = farmer.plan_cycle([], held=_held(size=10), state=st, capital_cap=50)
    assert to_sell == []
    assert any("SELL-SKIP-REWARD-MIN" in ln for ln in log)


def test_filled_skips_sub_min_notional():
    # own_max 8 shares, flatten price 0.10 → 8×0.10 = $0.80 < $1 → skip to settlement
    st = _filled(cost=0.13, held_at_fill=8.0, last_mid=0.10)
    context = {"T1": {"mid": 0.10, "fair_yes": 0.05}}
    _ns, _tp, _tc, to_sell, log = farmer.plan_cycle([], held=_held(size=8), state=st, capital_cap=50,
                                                    market_context=context)
    assert to_sell == [] and any("SELL-SKIP" in ln for ln in log)


def test_filled_flatten_ratchets_down_when_dropped():
    # FLATTEN resting on a dropped bucket; flatten_since 3 → next ask = last_mid − 3 ticks → reprice
    st = _filled(last_mid=0.20, sell_order_id="s1", sell_price=0.20, sell_mode="FLATTEN", sell_size=50.0, flatten_since=3)
    _ns, _tp, to_cancel, _ts, _log = farmer.plan_cycle([], held=_held(size=50), state=st, capital_cap=50)
    assert len(to_cancel) == 1 and to_cancel[0].get("kind") == "sell"
    assert abs(to_cancel[0]["resell"]["price"] - 0.17) < 1e-9                   # 0.20 − 3×0.01


def test_filled_leaves_correct_resting_sell_alone():
    st = _filled(sell_order_id="s1", sell_price=0.24, sell_mode="TAKE_PROFIT", sell_size=50.0)
    _ns, _tp, to_cancel, to_sell, _log = farmer.plan_cycle([_order(price=0.22)], held=_held(size=50), state=st, capital_cap=50)
    assert to_cancel == [] and to_sell == []


def test_filled_stale_pre_b3b_entry_is_left_alone():
    # an OLD FILLED entry (no cost / station_id) must NOT crash the new branch — skipped for manual handling
    stale = {"T1": {"status": "FILLED", "order_id": "b1", "city": "seoul", "bucket": "==27C",
                    "price": 0.22, "size": 50.0, "capital": 11.0, "held_at_place": 0.0}}
    _ns, _tp, to_cancel, to_sell, log = farmer.plan_cycle([], held=_held(size=50), state=stale, capital_cap=50)
    assert to_sell == [] and to_cancel == [] and any("STALE-FILLED" in ln for ln in log)


# ---- plan_cycle FILLED: live-book ask flooring + FLATTEN hysteresis (fixes for the live SELL-FAILs) ----

def test_filled_take_profit_ask_floored_above_live_bid():
    # market ran up to bid 0.30 — a 0.24 target would sit BELOW the bid (crosses → post_only reject);
    # floor it to best_bid+1tick = 0.31 so it stays a maker
    _ns, _tp, _tc, to_sell, _log = farmer.plan_cycle([_order(price=0.22)], held=_held(size=50),
                                                     state=_filled(cost=0.22), capital_cap=50,
                                                     books={"T1": (0.30, 0.34)})
    assert len(to_sell) == 1 and to_sell[0]["mode"] == "TAKE_PROFIT" and abs(to_sell[0]["price"] - 0.31) < 1e-9


def test_filled_thesis_break_flatten_uses_live_book_not_stale_mid():
    # A broken thesis uses the live 0.32 mid, not stale 0.20, for its defensive maker exit.
    context = {"T1": {"mid": 0.20, "fair_yes": 0.10}}
    _ns, _tp, _tc, to_sell, _log = farmer.plan_cycle([], held=_held(size=50),
                                                     state=_filled(last_mid=0.20), capital_cap=50,
                                                     books={"T1": (0.30, 0.34)}, market_context=context)
    assert len(to_sell) == 1 and to_sell[0]["mode"] == "FLATTEN" and abs(to_sell[0]["price"] - 0.32) < 1e-9


def test_filled_flatten_is_sticky_no_flap_to_scalp():
    # A FLATTEN sell rests; fair/price recovers but hysteresis keeps it FLATTEN (no mode churn).
    st = _filled(cost=0.22, last_mid=0.30, sell_order_id="s1", sell_price=0.20, sell_mode="FLATTEN", sell_size=50.0)
    _ns, _tp, to_cancel, _ts, _log = farmer.plan_cycle([_order(price=0.30)], held=_held(size=50), state=st, capital_cap=50)
    assert len(to_cancel) == 1 and to_cancel[0]["resell"]["mode"] == "FLATTEN"


# ---- plan_cycle FILLED: 关门时间 taker force-flatten (peak−FORCE_FLATTEN_LEAD_MIN) ----

def _filled_with_peak(minutes_to_peak, **over):
    st = _filled(**over)
    st["T1"]["peak_utc"] = (NOW + timedelta(minutes=minutes_to_peak)).isoformat()
    return st


def test_filled_force_flatten_taker_at_cutoff():
    # Within the user-confirmed six-hour cutoff + a live book → TAKER at the bid.
    st = _filled_with_peak(60, cost=0.22)
    _ns, _tp, _tc, to_sell, _log = farmer.plan_cycle([], held=_held(size=50), state=st, capital_cap=50,
                                                     books={"T1": (0.30, 0.34)}, now=NOW)
    assert len(to_sell) == 1 and to_sell[0]["mode"] == "FORCE" and to_sell[0]["taker"] is True
    assert abs(to_sell[0]["price"] - 0.30) < 1e-9                               # AT the best bid (marketable), not floored above


def test_filled_no_force_well_before_cutoff():
    # Peak still 7h out → before the user-confirmed 6h cutoff, so it stays a maker.
    st = _filled_with_peak(420, cost=0.22)
    _ns, _tp, _tc, to_sell, _log = farmer.plan_cycle([_order(price=0.22)], held=_held(size=50), state=st,
                                                     capital_cap=50, books={"T1": (0.30, 0.34)}, now=NOW)
    assert to_sell and to_sell[0]["taker"] is False and to_sell[0]["mode"] == "TAKE_PROFIT"


def test_filled_force_starts_at_six_hour_cutoff():
    st = _filled_with_peak(360, cost=0.22)
    _ns, _tp, _tc, to_sell, _log = farmer.plan_cycle([], held=_held(size=50), state=st,
                                                     capital_cap=50, books={"T1": (0.30, 0.34)}, now=NOW)
    assert to_sell and to_sell[0]["mode"] == "FORCE" and to_sell[0]["taker"] is True


def test_filled_force_cancels_resting_maker_and_resells_taker():
    # a maker FLATTEN rests when the cutoff hits → cancel it, re-sell as a taker FORCE
    st = _filled_with_peak(60, cost=0.22, sell_order_id="s1", sell_price=0.31, sell_mode="FLATTEN", sell_size=50.0)
    _ns, _tp, to_cancel, _ts, _log = farmer.plan_cycle([], held=_held(size=50), state=st, capital_cap=50,
                                                       books={"T1": (0.30, 0.34)}, now=NOW)
    assert len(to_cancel) == 1 and to_cancel[0]["kind"] == "sell"
    assert to_cancel[0]["resell"]["mode"] == "FORCE" and to_cancel[0]["resell"]["taker"] is True


def test_filled_force_skips_when_book_fetch_failed():
    # cutoff reached but the book fetch FAILED (token absent from books) → can't price a marketable taker and a
    # stale-mid maker would just reject → skip + FORCE-NO-BID, retry next cycle when the fetch recovers (review #2)
    st = _filled_with_peak(60, cost=0.22, last_mid=0.20)
    _ns, _tp, to_cancel, to_sell, log = farmer.plan_cycle([], held=_held(size=50), state=st, capital_cap=50, now=NOW)
    assert to_sell == [] and to_cancel == [] and any("FORCE-NO-BID" in ln for ln in log)


def test_filled_force_closes_when_flat_after_taker():
    # a taker sold our 50 → holding back to baseline → CLOSE even with no resting sell id
    st = _filled(cost=0.22, sell_mode="FORCE", sell_size=50.0, sell_order_id=None)
    ns, _tp, to_cancel, to_sell, log = farmer.plan_cycle([], held=_held(size=0), state=st, capital_cap=50, now=NOW)
    assert "T1" not in ns and to_sell == [] and to_cancel == [] and any("CLOSED" in ln for ln in log)


def test_filled_force_taker_fires_on_one_sided_book():
    # ONE-SIDED book (bid 0.30, NO ask — going-YES near resolution) → the taker STILL sells into the bid (review #2)
    st = _filled_with_peak(60, cost=0.22)
    _ns, _tp, _tc, to_sell, _log = farmer.plan_cycle([], held=_held(size=50), state=st, capital_cap=50,
                                                     books={"T1": (0.30, None)}, now=NOW)
    assert len(to_sell) == 1 and to_sell[0]["mode"] == "FORCE" and to_sell[0]["taker"] is True
    assert abs(to_sell[0]["price"] - 0.30) < 1e-9


def test_filled_force_no_bid_logs_and_skips():
    # asks only, NO bid (going-NO near resolution) → can't sell our YES; logged FORCE-NO-BID, no order this cycle (review #2)
    st = _filled_with_peak(60, cost=0.22, last_mid=0.20)
    _ns, _tp, to_cancel, to_sell, log = farmer.plan_cycle([], held=_held(size=50), state=st, capital_cap=50,
                                                          books={"T1": (None, 0.34)}, now=NOW)
    assert any("FORCE-NO-BID" in ln for ln in log) and to_sell == [] and to_cancel == []   # skip; a stale-mid maker would just reject


def test_filled_force_no_refire_after_partial_with_cohold():
    # a FORCE taker already fired (sell_mode=FORCE, held_at_fill decremented to base=0); a co-hold inflates cur=60.
    # MUST close on the private ceiling (own_max=0), NOT re-fire a crossing taker for shares we no longer hold (verify #1)
    st = _filled_with_peak(60, cost=0.22, held_at_place=0.0, held_at_fill=0.0, sell_mode="FORCE",
                           sell_size=50.0, sell_order_id=None)
    ns, _tp, to_cancel, to_sell, log = farmer.plan_cycle([], held=_held(size=60), state=st, capital_cap=50,
                                                         books={"T1": (0.30, 0.34)}, now=NOW)
    assert "T1" not in ns and to_sell == [] and to_cancel == [] and any("CLOSED" in ln for ln in log)


def test_filled_force_does_not_override_ambiguous_sell_pending():
    # an ambiguous maker (sell_pending, id-less → can't be cancelled) must NOT get a 关门时间 taker fired on top of it
    # (taker + maybe-live maker = double-sell). It WAITS for positions to resolve it. (verify-r3 #1/#2)
    st = _filled_with_peak(60, cost=0.22, sell_pending=True, sell_price=0.31, sell_mode="FLATTEN", sell_size=50.0)
    _ns, _tp, to_cancel, to_sell, log = farmer.plan_cycle([], held=_held(size=50), state=st, capital_cap=50,
                                                          books={"T1": (0.30, 0.34)}, now=NOW)
    assert to_cancel == [] and to_sell == [] and any("SELL-PENDING" in ln for ln in log)


def test_filled_one_sided_book_refreshes_mid_from_raw_context():
    # A one-sided book falls back to the raw scan, independent of top-N, and fresh fair breaks the thesis.
    o = _order(price=0.10)                                                       # market crashed: target mid 0.10 vs cost 0.22
    context = {"T1": {"mid": 0.10, "fair_yes": 0.15}}
    _ns, _tp, _tc, to_sell, _log = farmer.plan_cycle([o], held=_held(size=50),
                                                     state=_filled(cost=0.22, last_mid=0.22), capital_cap=50,
                                                     books={"T1": (0.05, None)}, market_context=context)
    assert len(to_sell) == 1 and to_sell[0]["mode"] == "FLATTEN"


def test_filled_empty_book_refreshes_mid_from_raw_context():
    # An empty book likewise falls back to full raw context rather than selected rank.
    o = _order(price=0.10)
    context = {"T1": {"mid": 0.10, "fair_yes": 0.15}}
    _ns, _tp, _tc, to_sell, _log = farmer.plan_cycle([o], held=_held(size=50),
                                                     state=_filled(cost=0.22, last_mid=0.22), capital_cap=50,
                                                     books={"T1": (None, None)}, market_context=context)
    assert len(to_sell) == 1 and to_sell[0]["mode"] == "FLATTEN"


# ---- _execute: BUY-side safe cancel ----

def _ok(oid):
    return ex.OrderResult(True, True, "ok", order_id=oid)


def _fake_place(placed):
    def place_orders(orders, *, now_iso, ttl_min=None):
        placed.extend(o.token_yes for o in orders)
        return [(o, ex.OrderResult(True, True, "dry", order_id=None)) for o in orders]
    return place_orders


def test_execute_reprice_replaces_after_confirmed_cancel(monkeypatch):
    placed = []
    monkeypatch.setattr(ex, "cancel_order", lambda oid, **k: _ok(oid))
    monkeypatch.setattr(farmer, "place_orders", _fake_place(placed))
    to_cancel = [{"token": "T1", "order_id": "o1", "replace": _order(price=0.30)}]
    ns = farmer._execute(dict(_resting()), [], to_cancel, [], held={}, now=NOW, now_iso="t", log=[])
    assert placed == ["T1"] and ns["T1"]["price"] == 0.30


def test_execute_unconfirmed_cancel_does_not_replace(monkeypatch):
    placed = []
    monkeypatch.setattr(ex, "cancel_order", lambda oid, **k: ex.OrderResult(False, False, "unconfirmed", order_id=oid))
    monkeypatch.setattr(farmer, "place_orders", _fake_place(placed))
    to_cancel = [{"token": "T1", "order_id": "o1", "replace": _order(price=0.30)}]
    ns = farmer._execute(dict(_resting()), [], to_cancel, [], held={}, now=NOW, now_iso="t", log=[])
    assert placed == [] and ns["T1"]["price"] == 0.22


def test_execute_fill_during_cancel_window_is_caught(monkeypatch):
    placed = []
    monkeypatch.setattr(ex, "cancel_order", lambda oid, **k: _ok(oid))
    monkeypatch.setattr(farmer, "place_orders", _fake_place(placed))
    to_cancel = [{"token": "T1", "order_id": "o1", "replace": _order(price=0.30)}]
    ns = farmer._execute(dict(_resting(held_at_place=0.0)), [], to_cancel, [], held=_held(size=50), now=NOW, now_iso="t", log=[])
    assert placed == [] and ns["T1"]["status"] == "FILLED"


def test_execute_buy_remainder_cancel_freezes_order_level_fill(monkeypatch):
    monkeypatch.setattr(ex, "cancel_order", lambda oid, **k: _ok(oid))
    monkeypatch.setattr(ex, "filled_size", lambda oid: 12.25)
    st = dict(_filled(held_at_fill=8.0))
    st["T1"].update(buy_cancel_pending=True, buy_order_id="b1")
    to_cancel = [{"token": "T1", "order_id": "b1", "replace": None, "kind": "buy_after_fill"}]
    log = []
    ns = farmer._execute(st, [], to_cancel, [], held=_held(size=80), now=NOW, now_iso="t", log=log)
    assert ns["T1"]["held_at_fill"] == 12.25                            # order fill, not shared-funder 80
    assert ns["T1"]["buy_cancel_pending"] is False
    assert any("BUY-REMAINDER-CANCELLED" in ln for ln in log)


def test_execute_unconfirmed_buy_remainder_cancel_blocks_sell(monkeypatch):
    monkeypatch.setattr(ex, "cancel_order", lambda oid, **k: ex.OrderResult(False, False, "unconfirmed", order_id=oid))
    st = dict(_filled())
    st["T1"].update(buy_cancel_pending=True, buy_order_id="b1")
    to_cancel = [{"token": "T1", "order_id": "b1", "replace": None, "kind": "buy_after_fill"}]
    ns = farmer._execute(st, [], to_cancel, [], held=_held(size=50), now=NOW, now_iso="t", log=[])
    assert ns["T1"]["buy_cancel_pending"] is True


def test_execute_buy_place_stores_seed_fields(monkeypatch):
    monkeypatch.setattr(farmer, "place_orders", _fake_place([]))
    ns = farmer._execute({}, [_order()], [], [], held={}, now=NOW, now_iso="t", log=[])
    e = ns["T1"]
    assert e["station_id"] == "RKSI" and e["comparator"] == "==" and e["threshold"] == 27
    assert e["market_date"] == "2026-06-18" and e["mid"] == 0.22


def test_execute_buy_ambiguous_records_blocking_placeholder(monkeypatch):
    def fake(orders, *, now_iso, ttl_min=None):
        return [(o, ex.OrderResult(False, False, "boom", ambiguous=True)) for o in orders]
    monkeypatch.setattr(farmer, "place_orders", fake)
    ns = farmer._execute({}, [_order()], [], [], held={}, now=NOW, now_iso="t", log=[])
    assert ns["T1"]["ambiguous"] is True and ns["T1"]["order_id"] is None
    _ns2, to_place2, _tc, _ts, _log = farmer.plan_cycle([_order()], held={}, state=ns, capital_cap=50)
    assert to_place2 == []                                                      # placeholder blocks the re-place


# ---- _execute: SELL-side ----

def _mock_sell(monkeypatch, order_id="s9", ok=True, ambiguous=False, capture=None):
    def fake(d, *, now_iso, ttl_min=None):
        if capture is not None:
            capture.append(d.size_shares)
        return ex.OrderResult(ok, False, "x", order_id=order_id, ambiguous=ambiguous)
    monkeypatch.setattr(ex, "place_from_decision", fake)


def test_sell_decision_is_maker_sell_yes():
    d = farmer._sell_decision(_filled()["T1"], token="tok", price=0.24, size=50.0)
    assert d.action == "BET" and d.side == "SELL_YES" and d.order_type == "maker"
    assert d.maker_price == 0.24 and d.size_shares == 50.0 and d.comparator.value == "==" and d.threshold == 27


def test_execute_sell_place_records_fields(monkeypatch):
    _mock_sell(monkeypatch, order_id="s9")
    to_sell = [{"token": "T1", "mode": "SCALP", "price": 0.24, "size": 50.0}]
    ns = farmer._execute(dict(_filled()), [], [], to_sell, held=_held(size=50), now=NOW, now_iso="t", log=[])
    e = ns["T1"]
    assert e["sell_order_id"] == "s9" and e["sell_price"] == 0.24 and e["sell_mode"] == "SCALP" and e["sell_size"] == 50.0


def test_execute_sell_ambiguous_sets_pending(monkeypatch):
    _mock_sell(monkeypatch, order_id=None, ok=False, ambiguous=True)
    to_sell = [{"token": "T1", "mode": "SCALP", "price": 0.24, "size": 50.0}]
    ns = farmer._execute(dict(_filled()), [], [], to_sell, held=_held(size=50), now=NOW, now_iso="t", log=[])
    e = ns["T1"]
    assert e["sell_pending"] is True and e["sell_order_id"] is None and e["sell_size"] == 50.0


def test_execute_sell_reprice_safe_cancel_then_repost(monkeypatch):
    monkeypatch.setattr(ex, "cancel_order", lambda oid, **k: _ok(oid))
    _mock_sell(monkeypatch, order_id="s2")
    st = dict(_filled(sell_order_id="s1", sell_price=0.20, sell_mode="FLATTEN", sell_size=50.0))
    to_cancel = [{"token": "T1", "order_id": "s1", "replace": None, "kind": "sell",
                  "resell": {"token": "T1", "mode": "FLATTEN", "price": 0.17, "size": 50.0}}]
    ns = farmer._execute(st, [], to_cancel, [], held=_held(size=50), now=NOW, now_iso="t", log=[])
    assert ns["T1"]["status"] == "FILLED" and ns["T1"]["sell_order_id"] == "s2" and ns["T1"]["sell_price"] == 0.17


def test_execute_sell_cancel_decrements_ceiling_by_maker_fill(monkeypatch):
    # cancelling a partially-filled maker (sold 30 of 50) shrinks held_at_fill by the ORDER-level fill → own_max=20,
    # so the resell clamps to 20 even though a co-hold inflates cur to 80 (would otherwise oversell to 50) (verify-r3 #3)
    monkeypatch.setattr(ex, "cancel_order", lambda oid, **k: _ok(oid))
    monkeypatch.setattr(ex, "filled_size", lambda oid: 30.0)
    captured = []
    monkeypatch.setattr(ex, "place_from_decision", lambda d, **k: captured.append(d.size_shares) or _ok("s2"))
    st = dict(_filled(held_at_place=0.0, held_at_fill=50.0, sell_order_id="s1", sell_price=0.24, sell_mode="SCALP", sell_size=50.0))
    to_cancel = [{"token": "T1", "order_id": "s1", "replace": None, "kind": "sell",
                  "resell": {"token": "T1", "mode": "FLATTEN", "price": 0.20, "size": 50.0, "taker": False}}]
    ns = farmer._execute(st, [], to_cancel, [], held=_held(size=80), now=NOW, now_iso="t", log=[])
    assert ns["T1"]["held_at_fill"] == 20.0 and captured == [20.0]              # ceiling shrank to the true remaining; no oversell


def test_execute_sell_unconfirmed_cancel_keeps_sell(monkeypatch):
    monkeypatch.setattr(ex, "cancel_order", lambda oid, **k: ex.OrderResult(False, False, "unconfirmed", order_id=oid))
    st = dict(_filled(sell_order_id="s1", sell_price=0.24, sell_mode="SCALP", sell_size=50.0))
    to_cancel = [{"token": "T1", "order_id": "s1", "replace": None, "kind": "sell",
                  "resell": {"token": "T1", "mode": "SCALP", "price": 0.25, "size": 50.0}}]
    ns = farmer._execute(st, [], to_cancel, [], held=_held(size=50), now=NOW, now_iso="t", log=[])
    assert ns["T1"]["sell_order_id"] == "s1"                                    # unconfirmed → sell untouched, no resell


def test_execute_sell_fill_during_cancel_window_closes(monkeypatch):
    monkeypatch.setattr(ex, "cancel_order", lambda oid, **k: _ok(oid))
    st = dict(_filled(sell_order_id="s1", sell_size=50.0))
    to_cancel = [{"token": "T1", "order_id": "s1", "replace": None, "kind": "sell"}]
    ns = farmer._execute(st, [], to_cancel, [], held={}, now=NOW, now_iso="t", log=[])
    assert "T1" not in ns                                                       # sold in the window → CLOSED, popped


def test_execute_sell_oversell_guard(monkeypatch):
    captured = []
    _mock_sell(monkeypatch, order_id="s9", capture=captured)
    st = dict(_filled(held_at_place=0.0, held_at_fill=30.0))                    # only 30 of our order filled
    to_sell = [{"token": "T1", "mode": "SCALP", "price": 0.24, "size": 50.0}]   # plan asked 50
    farmer._execute(st, [], [], to_sell, held=_held(size=30), now=NOW, now_iso="t", log=[])
    assert captured == [30.0]                                                   # clamped to our delta, never 50


def test_execute_sell_size_never_rounds_above_exact_balance(monkeypatch):
    captured = []
    _mock_sell(monkeypatch, order_id="s9", capture=captured)
    st = dict(_filled(held_at_fill=20.0))
    to_sell = [{"token": "T1", "mode": "FLATTEN", "price": 0.20, "size": 20.0}]
    farmer._execute(st, [], [], to_sell, held=_held(size=17.435899), now=NOW, now_iso="t", log=[])
    assert captured == [17.43]


def _gone(oid):
    return ex.OrderResult(True, False, "matched/gone", order_id=oid, gone=True)


def test_execute_sell_gone_cancel_skips_resell(monkeypatch):
    # a 'matched' (gone) sell-cancel means the maker MAY have filled → do NOT fire a crossing taker resell on
    # stale positions (the naked-short the review caught). Clear sell state; re-confirm next cycle.
    placed = []
    monkeypatch.setattr(ex, "cancel_order", lambda oid, **k: _gone(oid))
    monkeypatch.setattr(ex, "place_from_decision", lambda d, **k: placed.append(d.side) or _ok("s2"))
    st = dict(_filled(sell_order_id="s1", sell_price=0.31, sell_mode="FLATTEN", sell_size=50.0))
    to_cancel = [{"token": "T1", "order_id": "s1", "replace": None, "kind": "sell",
                  "resell": {"token": "T1", "mode": "FORCE", "price": 0.30, "size": 50.0, "taker": True}}]
    ns = farmer._execute(st, [], to_cancel, [], held=_held(size=50), now=NOW, now_iso="t", log=[])
    assert placed == [] and ns["T1"]["sell_order_id"] is None                   # NO taker fired; sell state cleared


def test_execute_sell_clean_cancel_still_resells(monkeypatch):
    # a CLEAN cancel (gone=False) proves the maker was OPEN (didn't fill) → we still hold → the resell is safe
    placed = []
    monkeypatch.setattr(ex, "cancel_order", lambda oid, **k: _ok(oid))          # _ok → gone defaults False
    monkeypatch.setattr(ex, "place_from_decision", lambda d, **k: placed.append(d.order_type) or _ok("s2"))
    st = dict(_filled(sell_order_id="s1", sell_price=0.31, sell_mode="FLATTEN", sell_size=50.0))
    to_cancel = [{"token": "T1", "order_id": "s1", "replace": None, "kind": "sell",
                  "resell": {"token": "T1", "mode": "FORCE", "price": 0.30, "size": 50.0, "taker": True}}]
    farmer._execute(st, [], to_cancel, [], held=_held(size=50), now=NOW, now_iso="t", log=[])
    assert placed == ["taker"]                                                  # clean cancel → taker resell fires


def test_execute_buy_gone_cancel_adopts_fill(monkeypatch):
    # a gone (matched) buy-cancel whose FRESH positions show the fill → ADOPT into Stage 2 (FILLED), never orphan (verify #3)
    placed = []
    monkeypatch.setattr(ex, "cancel_order", lambda oid, **k: _gone(oid))
    monkeypatch.setattr(ex, "positions_held", lambda: {"T1": {"size": 50.0, "title": "", "redeemable": False}})
    monkeypatch.setattr(farmer, "place_orders", _fake_place(placed))
    to_cancel = [{"token": "T1", "order_id": "o1", "replace": _order(price=0.30)}]
    ns = farmer._execute(dict(_resting(held_at_place=0.0)), [], to_cancel, [], held={}, now=NOW, now_iso="t", log=[])
    assert ns["T1"]["status"] == "FILLED" and placed == []                      # adopted (not orphaned), no fresh bid


def test_execute_buy_gone_cancel_drops_when_not_filled(monkeypatch):
    # a gone buy-cancel whose fresh positions show NO fill → it was cancelled → drop (no phantom, no replace)
    placed = []
    monkeypatch.setattr(ex, "cancel_order", lambda oid, **k: _gone(oid))
    monkeypatch.setattr(ex, "positions_held", lambda: {})                       # nothing held
    monkeypatch.setattr(farmer, "place_orders", _fake_place(placed))
    to_cancel = [{"token": "T1", "order_id": "o1", "replace": _order(price=0.30)}]
    ns = farmer._execute(dict(_resting(held_at_place=0.0)), [], to_cancel, [], held={}, now=NOW, now_iso="t", log=[])
    assert "T1" not in ns and placed == []                                      # dropped cleanly, no phantom, no replace


# ---- _execute / executor: 关门时间 taker force-flatten ----

def test_sell_decision_taker_is_taker_order_type():
    d = farmer._sell_decision(_filled()["T1"], token="tok", price=0.30, size=50.0, taker=True)
    assert d.side == "SELL_YES" and d.order_type == "taker" and d.maker_price == 0.30


def test_execute_taker_force_sell_records_force_no_rest(monkeypatch):
    captured = {}
    def fake(d, *, now_iso, ttl_min=None):
        captured["order_type"] = d.order_type
        return ex.OrderResult(True, False, "fak", order_id=None)                # FAK fills, no resting id
    monkeypatch.setattr(ex, "place_from_decision", fake)
    to_sell = [{"token": "T1", "mode": "FORCE", "price": 0.30, "size": 50.0, "taker": True}]
    ns = farmer._execute(dict(_filled()), [], [], to_sell, held=_held(size=50), now=NOW, now_iso="t", log=[])
    e = ns["T1"]
    assert captured["order_type"] == "taker"
    assert e["sell_mode"] == "FORCE" and e["sell_order_id"] is None and e["sell_size"] == 50.0
    assert e["held_at_fill"] == 0.0                                             # private ceiling shrank by the placed size → CLOSE next cycle, no re-fire


def test_execute_taker_force_sell_failure_retries_no_pending(monkeypatch):
    # a failed/ambiguous FAK must NOT set sell_pending (would stick) → retried next cycle, clamped to held
    monkeypatch.setattr(ex, "place_from_decision",
                        lambda d, **k: ex.OrderResult(False, False, "boom", ambiguous=True))
    to_sell = [{"token": "T1", "mode": "FORCE", "price": 0.30, "size": 50.0, "taker": True}]
    ns = farmer._execute(dict(_filled()), [], [], to_sell, held=_held(size=50), now=NOW, now_iso="t", log=[])
    assert ns["T1"]["sell_pending"] is False and ns["T1"]["sell_order_id"] is None


def test_executor_taker_sell_dry_ok(monkeypatch):
    monkeypatch.setenv("POLY_DRY_RUN", "1")
    d = farmer._sell_decision(_filled()["T1"], token="0xtok", price=0.30, size=50.0, taker=True)
    res = ex.place_from_decision(d, now_iso="t", ttl_min=None)
    assert res.ok and res.dry_run and "FAK-taker" in res.detail


def test_executor_allows_taker_buy(monkeypatch, tmp_path):
    # taker BUY = the cluster/snipe FAK entry (allowed since 2026-07-04; the farmer itself never sends one)
    from datetime import date as _date

    from polyweather.models import Comparator, Decision
    monkeypatch.setenv("POLY_DRY_RUN", "1")
    monkeypatch.setattr(ex, "ORDER_LOG", tmp_path / "orders.jsonl")
    d = Decision(city="seoul", station_id="RKSI", local_date=_date(2026, 6, 18), label="==27C",
                 comparator=Comparator.EQ, threshold=27, token_yes="0xtok", action="BET", side="BUY_YES",
                 order_type="taker", confidence=None, market_yes=0.30, maker_price=0.30, size_shares=50.0, health_ok=True)
    res = ex.place_from_decision(d, now_iso="t")
    assert res.ok and res.dry_run and "FAK-taker" in res.detail


# ---- clob_executor.cancel_order: GONE (matched/not-found) → ok=True (no infinite retry); transient → ok=False ----

class _FakeCancelClient:
    def __init__(self, resp):
        self._resp = resp
    def cancel_order(self, _payload):
        return self._resp


def _cancel_with(monkeypatch, resp):
    monkeypatch.setenv("POLY_DRY_RUN", "0")
    monkeypatch.setattr(ex, "_CLIENT", _FakeCancelClient(resp))
    return ex.cancel_order("oX", now_iso="t")


def test_cancel_order_matched_is_gone_ok(monkeypatch):
    # a filled order can't be cancelled — "matched" means it's off the book → ok=True, AND gone=True (terminal, not
    # a clean cancel) so the caller skips resell/replace on stale positions
    r = _cancel_with(monkeypatch, {"not_canceled": {"oX": "matched orders can't be cancelled"}})
    assert r.ok is True and r.gone is True


def test_cancel_order_not_found_is_gone_ok(monkeypatch):
    r = _cancel_with(monkeypatch, {"not_canceled": {"oX": "order can't be found - already canceled or matched"}})
    assert r.ok is True and r.gone is True


def test_cancel_order_transient_not_canceled_stays_unconfirmed(monkeypatch):
    # an unknown/transient reason is NOT proof the order is gone → ok=False (don't drop a maybe-still-live order)
    assert _cancel_with(monkeypatch, {"not_canceled": {"oX": "temporary server error, retry"}}).ok is False


def test_cancel_order_clean_success_ok(monkeypatch):
    # a clean cancel of a live OPEN order → ok=True AND gone=False (the order didn't fill → safe to resell/replace)
    r = _cancel_with(monkeypatch, {"canceled": ["oX"], "not_canceled": {}})
    assert r.ok is True and r.gone is False
