from datetime import date

from polyweather.config import SignalThresholds
from polyweather.engine import devig, evaluate
from polyweather.models import Bucket, Comparator, EventState, SignalType

TH = SignalThresholds()
LADDER = [
    (Comparator.LE, 19), (Comparator.EQ, 20), (Comparator.EQ, 21), (Comparator.EQ, 22),
    (Comparator.EQ, 23), (Comparator.EQ, 24), (Comparator.EQ, 25), (Comparator.EQ, 26),
    (Comparator.EQ, 27), (Comparator.EQ, 28), (Comparator.GE, 29),
]


def make_state(running_max, prices, *, verified=True, liq=10000.0):
    buckets = []
    for comp, thr in LADDER:
        p = prices.get(thr, 0.001)
        buckets.append(Bucket(
            question=str(thr), comparator=comp, threshold=thr, unit="C",
            token_yes="y", token_no="n", condition_id="c",
            price_yes=p, best_bid=p, best_ask=p, last_trade=p, liquidity=liq,
        ))
    st = EventState(
        city="london", slug="s", title="t", local_date=date(2026, 6, 13),
        unit="C", verified=verified, buckets=buckets, running_max=running_max,
    )
    st.order_buckets()
    return st


def test_devig_normalizes_to_one():
    st = make_state(21, {21: 0.42, 22: 0.50, 23: 0.08})
    norm, raw = st_devig(st)
    assert abs(sum(norm.values()) - 1.0) < 1e-9
    assert raw > 1.0  # has vig


def st_devig(st):
    return devig(st)


def test_dead_bucket_signal_fires_on_live_bid():
    # max already 23 -> buckets <=19,20,21,22 are dead. Only 21 has a real bid.
    st = make_state(23, {21: 0.10, 23: 0.80})
    sigs = evaluate(st, TH, local_hour=14, wu_poll_age_s=10)
    dead = [s for s in sigs if s.type is SignalType.DEAD_BUCKET]
    assert len(dead) == 1
    assert dead[0].threshold == 21 and dead[0].action == "SELL YES"
    assert abs(dead[0].edge - 0.10) < 1e-9


def test_no_dead_signal_without_bid():
    # dead buckets all at default 0.001 < threshold -> nothing actionable
    st = make_state(23, {23: 0.80})
    dead = [s for s in evaluate(st, TH, local_hour=14, wu_poll_age_s=10)
            if s.type is SignalType.DEAD_BUCKET]
    assert dead == []


def test_gating_unverified_and_stale():
    st = make_state(23, {21: 0.10})
    assert evaluate(st, TH, local_hour=14, wu_poll_age_s=10**9) == []      # stale
    st2 = make_state(23, {21: 0.10}, verified=False)
    assert evaluate(st2, TH, local_hour=14, wu_poll_age_s=10) == []        # unverified


def test_locked_single_eod():
    # evening, current-max bucket (22) underpriced at 0.80 -> LOCKED_SINGLE
    st = make_state(22, {22: 0.80})
    sigs = evaluate(st, TH, local_hour=20, wu_poll_age_s=10)
    locked = [s for s in sigs if s.type is SignalType.LOCKED_SINGLE]
    assert len(locked) == 1
    assert locked[0].threshold == 22 and locked[0].action == "BUY YES"
    assert abs(locked[0].edge - 0.20) < 1e-9


def test_no_locked_single_midday():
    st = make_state(22, {22: 0.80})
    locked = [s for s in evaluate(st, TH, local_hour=12, wu_poll_age_s=10)
              if s.type is SignalType.LOCKED_SINGLE]
    assert locked == []
