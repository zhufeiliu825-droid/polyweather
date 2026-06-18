from datetime import date, datetime, timezone

from polyweather.decision import validate_proposal
from polyweather.models import Bucket, Comparator, EventState

LADDER = [
    (Comparator.LE, 19), (Comparator.EQ, 20), (Comparator.EQ, 21), (Comparator.EQ, 22),
    (Comparator.EQ, 23), (Comparator.EQ, 24), (Comparator.EQ, 25), (Comparator.EQ, 26),
    (Comparator.EQ, 27), (Comparator.EQ, 28), (Comparator.GE, 29),
]
NOW = datetime(2026, 6, 14, 15, 0, tzinfo=timezone.utc)
PEAK = datetime(2026, 6, 14, 16, 30, tzinfo=timezone.utc)   # future → pre-peak
PAST = datetime(2026, 6, 14, 14, 0, tzinfo=timezone.utc)    # peak already passed


def make_state(prices, running_max=21):
    bs = []
    for c, t in LADDER:
        p = prices.get(t, 0.01)
        bs.append(Bucket(question=str(t), comparator=c, threshold=t, unit="C",
                         token_yes="y", token_no="n", condition_id="x",
                         price_yes=p, best_bid=p, best_ask=p, last_trade=p))
    st = EventState(city="london", local_date=date(2026, 6, 14),
                    buckets=bs, running_max=running_max)
    st.order_buckets()
    return st


def prop(action, comp=None, thr=None, reason="x"):
    return {"action": action, "comparator": comp, "threshold": thr, "reason": reason, "confidence": 0.7}


HEALTHY = {21: 0.4, 22: 0.5}   # de-vig sum ~0.9 → healthy


def test_valid_bet_passes():
    st = make_state(HEALTHY, running_max=21)
    d = validate_proposal(prop("BET", "==", 22), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d is not None and d.action == "BET" and d.threshold == 22
    assert abs(d.maker_price - 0.499) < 1e-6                     # 0.50 − 0.1¢


def test_dead_bucket_rejected():
    # propose a bucket BELOW running_max=21 → must NOT bet (escalate), no matter what the agent says
    st = make_state({20: 0.5, 22: 0.4}, running_max=21)
    d = validate_proposal(prop("BET", "==", 20), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d.action == "ESCALATE" and "DEAD" in d.reason.upper()


def test_dead_bucket_at_running_max_when_forecast_higher():
    # ==running_max wins ONLY if the temp doesn't rise; forecast peak ≥ running_max+0.5 → likely dies → ESCALATE
    st = make_state({25: 0.4, 26: 0.4}, running_max=25)
    d = validate_proposal(prop("BET", "==", 25), st, 26.3, "EGLC", now=NOW, peak_utc=PEAK)   # forecast 26.3 ≥ 25.5
    assert d.action == "ESCALATE" and "dead-bucket" in d.reason
    # but ==running_max is fine when the forecast ≈ running_max (the day is ~done rising)
    ok = validate_proposal(prop("BET", "==", 25), st, 25.1, "EGLC", now=NOW, peak_utc=PEAK)   # 25.1 < 25.5
    assert ok.action == "BET" and ok.threshold == 25


def test_price_ceiling_rejected():
    st = make_state({22: 0.90, 21: 0.05}, running_max=21)
    d = validate_proposal(prop("BET", "==", 22), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d.action == "ESCALATE" and "0.85" in d.reason


def test_bucket_not_in_event_rejected():
    st = make_state(HEALTHY, running_max=21)
    d = validate_proposal(prop("BET", "==", 99), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d.action == "ESCALATE" and "not in event" in d.reason


def test_unhealthy_market_rejected():
    st = make_state({t: 0.5 for _c, t in LADDER}, running_max=21)   # de-vig sum 5.5 → broken
    d = validate_proposal(prop("BET", "==", 22), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d.action == "ESCALATE" and not d.health_ok


def test_skip_returns_none():
    st = make_state(HEALTHY, running_max=21)
    assert validate_proposal(prop("SKIP"), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK) is None


def test_agent_escalate_passthrough():
    st = make_state(HEALTHY, running_max=21)
    d = validate_proposal(prop("ESCALATE", reason="convective"), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d.action == "ESCALATE" and "convective" in d.reason


def test_post_peak_none():
    st = make_state(HEALTHY, running_max=21)
    assert validate_proposal(prop("BET", "==", 22), st, 22.0, "EGLC", now=NOW, peak_utc=PAST) is None


def test_cutoff_extends_past_peak():
    # the strike stage extends the cutoff past the (early) forecast peak: a BET past peak_utc but before
    # cutoff_utc is STILL valid; past cutoff_utc → None. (Fixes the "shut off 1h before the real peak" bug.)
    st = make_state(HEALTHY, running_max=21)
    after_peak = datetime(2026, 6, 14, 16, 45, tzinfo=timezone.utc)    # past PEAK (16:30Z)
    cutoff = datetime(2026, 6, 14, 17, 30, tzinfo=timezone.utc)
    d = validate_proposal(prop("BET", "==", 22), st, 22.0, "EGLC",
                          now=after_peak, peak_utc=PEAK, cutoff_utc=cutoff)
    assert d is not None and d.action == "BET"
    assert validate_proposal(prop("BET", "==", 22), st, 22.0, "EGLC",
                             now=datetime(2026, 6, 14, 17, 45, tzinfo=timezone.utc),
                             peak_utc=PEAK, cutoff_utc=cutoff) is None     # past the cutoff → no new positions


def test_ge_already_won_rejected():
    # running_max 30 → >=29 has ALREADY won (no upside) → must NOT bet
    st = make_state({29: 0.5, 28: 0.4}, running_max=30)
    d = validate_proposal(prop("BET", ">=", 29), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d.action == "ESCALATE" and "DEAD" in d.reason.upper()


def test_ge_reachable_still_alive():
    # running_max 21 → >=29 is still REACHABLE (the max can rise) → a valid BET
    st = make_state({29: 0.5, 21: 0.4}, running_max=21)
    d = validate_proposal(prop("BET", ">=", 29), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d is not None and d.action == "BET" and d.threshold == 29


def test_cheap_fav_is_sized_up_maker():
    # a cheap favorite (<10¢) is a RESTING maker (never a taker), sized UP so it still clears the $1 platform
    # min — earns rewards, pays 0 fee, never crosses. Posted at market − undercut, not the ask.
    st = make_state({22: 0.015, 24: 0.9, 21: 0.05}, running_max=21)
    d = validate_proposal(prop("BET", "==", 22), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d is not None and d.action == "BET" and d.threshold == 22 and d.order_type == "maker"
    assert abs(d.maker_price - 0.014) < 1e-6           # 0.015 mid − 0.1¢ undercut (NOT the ask)
    notional = d.size_shares * d.maker_price
    assert notional >= 1.0 - 1e-9                       # share count sized up to clear $1
    assert abs(notional - round(notional, 2)) < 1e-9   # makerAmount ≤2 decimals (10s of shares on a 0.001-tick price)


def test_cheap_maker_makeramount_is_2_decimals():
    # Polymarket REJECTS a maker whose makerAmount (size×price) > 2 decimals. On a sub-0.04 (0.001-tick) bucket
    # the sized-up count is a MULTIPLE OF 10 so 10×(3-decimal price) stays ≤2 decimals. (Generalizes the old
    # taker-cap 400-fix to the all-maker size-up.)
    st = make_state({22: 0.037, 24: 0.9, 21: 0.05}, running_max=21)   # mid 0.037 → maker 0.036 (<0.04 → 0.001 tick)
    d = validate_proposal(prop("BET", "==", 22), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d is not None and d.action == "BET" and d.order_type == "maker"
    assert d.size_shares % 10 == 0                                    # multiple of 10 keeps the makerAmount clean
    notional = d.size_shares * d.maker_price
    assert abs(notional - round(notional, 2)) < 1e-9                  # makerAmount ≤2 decimals → accepted
    assert notional >= 1.0 - 1e-9                                     # still ≥ $1


def test_pricey_fav_is_ten_share_maker():
    # ≥11¢ rests as the normal 10-share MAKER at market − undercut (10 × ≥0.10 = ≥$1 clears the min)
    st = make_state({22: 0.50, 21: 0.4}, running_max=21)
    d = validate_proposal(prop("BET", "==", 22), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d is not None and d.action == "BET" and d.order_type == "maker"
    assert abs(d.maker_price - 0.499) < 1e-6 and d.size_shares == 10.0    # 0.50 − 0.1¢, 10 shares


def test_dollar_min_boundary_sizes_up_the_maker():
    # the $1-min boundary: at ≥10¢ a 10-share maker already clears $1 (10×≥0.10≈≥$1); below that the share
    # count is SIZED UP — both stay a resting maker (never a taker).
    ten = validate_proposal(prop("BET", "==", 22), make_state({22: 0.11, 21: 0.84}, running_max=21),
                            22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert ten.action == "BET" and ten.order_type == "maker" and ten.size_shares == 10.0    # 10×0.109 = $1.09 ≥ $1
    up = validate_proposal(prop("BET", "==", 22), make_state({22: 0.10, 21: 0.85}, running_max=21),
                           22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert up.action == "BET" and up.order_type == "maker" and up.size_shares > 10.0         # 10×0.099 sub-$1 → sized up
    assert up.size_shares * up.maker_price >= 1.0 - 1e-9                                      # clears the $1 minimum


def test_below_min_shares_escalated():
    # < 5 shares → below the exchange share minimum → escalate
    st = make_state({22: 0.5, 21: 0.4}, running_max=21)
    d = validate_proposal(prop("BET", "==", 22), st, 22.0, "EGLC",
                          now=NOW, peak_utc=PEAK, size_shares=3)
    assert d.action == "ESCALATE" and "share" in d.reason


def test_the_2590_bug_agent_picks_26_and_it_is_accepted():
    # THE live bug, locked: forecast 25.9 / realized running_max 25. The OLD σ-formula made ==25 the
    # "favorite" and require_top FORCED the agent there (bet 25 on a 25.9 forecast — absurd). Now the agent
    # proposes ==26 and the validator ACCEPTS it (only the safety gates apply).
    st = make_state({26: 0.45, 25: 0.30, 27: 0.10}, running_max=25)
    d = validate_proposal(prop("BET", "==", 26), st, 25.9, "EGLC", now=NOW, peak_utc=PEAK)
    assert d is not None and d.action == "BET" and d.threshold == 26


def test_chooser_may_pick_any_alive_bucket():
    # the WHOLE point of the redesign: the chooser (agent or operator) is NOT pinned to a model favorite —
    # a non-"obvious" but alive, affordable, healthy bucket (==24) is ACCEPTED. (Was H3 require_top → fixed.)
    st = make_state({22: 0.5, 24: 0.3}, running_max=21)
    d = validate_proposal(prop("BET", "==", 24), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d is not None and d.action == "BET" and d.threshold == 24


def test_chooser_still_blocked_by_dead_bucket_gate():
    # freedom to pick the bucket does NOT extend to a DEAD bucket (below running_max) — the hard floor holds.
    st = make_state({20: 0.5, 22: 0.4}, running_max=21)
    d = validate_proposal(prop("BET", "==", 20), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d.action == "ESCALATE" and "DEAD" in d.reason.upper()


def test_market_prices_target_zero_rejected():
    # a bucket the MARKET deems impossible (priced ~0) is rejected — at 0.001 the maker bid (0.001 − undercut)
    # falls below the min tick, so the maker-floor gate refuses it (an absurd tail bucket can't be bet).
    st = make_state({22: 0.5, 27: 0.001, 21: 0.4}, running_max=21)
    d = validate_proposal(prop("BET", "==", 27), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d.action == "ESCALATE" and "floor" in d.reason.lower()


def test_one_sided_book_unhealthy():
    # L1: a bucket with no live bid/ask (only a stale price) → not safe to auto-bet → ESCALATE.
    st = make_state({22: 0.5, 21: 0.4}, running_max=21)
    tgt = next(b for b in st.buckets if b.threshold == 22)
    tgt.best_bid = tgt.best_ask = None                  # one-sided / illiquid; implied_p falls back
    d = validate_proposal(prop("BET", "==", 22), st, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d.action == "ESCALATE" and not d.health_ok
