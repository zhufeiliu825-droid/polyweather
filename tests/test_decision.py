from datetime import date, datetime, timezone

from polyweather.decision import decide, nearest_alive_bucket
from polyweather.models import Bucket, Comparator, EventState

LADDER = [
    (Comparator.LE, 19), (Comparator.EQ, 20), (Comparator.EQ, 21), (Comparator.EQ, 22),
    (Comparator.EQ, 23), (Comparator.EQ, 24), (Comparator.EQ, 25), (Comparator.EQ, 26),
    (Comparator.EQ, 27), (Comparator.EQ, 28), (Comparator.GE, 29),
]
NOW = datetime(2026, 6, 14, 15, 0, tzinfo=timezone.utc)
PEAK = datetime(2026, 6, 14, 16, 30, tzinfo=timezone.utc)   # in the future -> pre-peak


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


# ---- the deterministic fallback: round(forecast peak) → nearest alive bucket ----

def test_bet_rounds_forecast_to_nearest_bucket():
    state = make_state({22: 0.5, 21: 0.4})                       # sum ~0.99 healthy, fav < 0.85
    d = decide(state, 21.9, "EGLC", now=NOW, peak_utc=PEAK)      # 21.9 → ==22 (the obvious human pick)
    assert d is not None and d.action == "BET"
    assert d.threshold == 22 and d.side == "BUY_YES" and d.order_type == "maker"
    assert abs(d.maker_price - 0.499) < 1e-6                     # market 0.50 − 0.1¢ undercut (snapped to tick at place time)
    assert d.size_shares == 10.0 and d.confidence is None        # deterministic → no model prob


def test_escalate_on_half_degree_boundary():
    # 21.5 sits exactly between ==21 and ==22 → no agent to break the tie → ESCALATE, don't coin-flip
    state = make_state({22: 0.5, 21: 0.4})
    d = decide(state, 21.5, "EGLC", now=NOW, peak_utc=PEAK)
    assert d is not None and d.action == "ESCALATE" and "boundary" in d.reason.lower()


def test_escalate_when_fav_too_expensive():
    state = make_state({22: 0.90, 21: 0.06})                     # ==22 priced ≥ 0.85
    d = decide(state, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d is not None and d.action == "ESCALATE" and d.health_ok
    assert "0.85" in d.reason or "upside" in d.reason.lower()


def test_escalate_when_market_unhealthy():
    state = make_state({t: 0.5 for _c, t in LADDER})            # de-vig sum 5.5 -> broken
    d = decide(state, 22.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d is not None and d.action == "ESCALATE" and d.health_ok is False
    assert "unhealthy" in d.reason.lower()


def test_none_after_peak():
    state = make_state({22: 0.5, 21: 0.4})
    past_peak = datetime(2026, 6, 14, 14, 0, tzinfo=timezone.utc)   # peak already passed
    assert decide(state, 22.0, "EGLC", now=NOW, peak_utc=past_peak) is None


def test_cutoff_extends_past_peak():
    # the strike stage extends the cutoff: a BET past peak_utc but before cutoff_utc is STILL valid; past
    # cutoff → None.
    state = make_state({22: 0.5, 21: 0.4})
    cutoff = datetime(2026, 6, 14, 17, 30, tzinfo=timezone.utc)
    after_peak = datetime(2026, 6, 14, 16, 45, tzinfo=timezone.utc)        # past PEAK 16:30
    d = decide(state, 22.0, "EGLC", now=after_peak, peak_utc=PEAK, cutoff_utc=cutoff)
    assert d is not None and d.action == "BET" and d.threshold == 22
    assert decide(state, 22.0, "EGLC",
                  now=datetime(2026, 6, 14, 17, 45, tzinfo=timezone.utc), peak_utc=PEAK, cutoff_utc=cutoff) is None


# ---- the dead-bucket floor: EQ/LE below running_max are dead; equality can still settle ----

def test_floors_forecast_below_running_max():
    # forecast 20 but realized already 21 → never pick the dead ==20; floor up to the alive running_max bucket
    state = make_state({20: 0.6, 22: 0.3}, running_max=21)
    d = decide(state, 20.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d is None or d.threshold >= 21


def test_fallback_rejects_dead_ge_top_bucket():
    # running_max 29 already reached the top >=29 edge → that bucket has WON (p≈1, no upside). The agent-DOWN
    # fallback must NOT auto-bet it; the alive gate mirrors validate_proposal. (Regression: decide() had no
    # aliveness check, so it would post a live BET on the resolved bucket whenever the agent was unavailable.)
    state = make_state({29: 0.80, 28: 0.10}, running_max=29)         # >=29 priced 0.80 (<0.85 ceiling), healthy
    d = decide(state, 30.0, "EGLC", now=NOW, peak_utc=PEAK)
    assert d is not None and d.action == "ESCALATE"
    assert d.health_ok is True                                        # price/health were fine — the ALIVE gate vetoed
    assert "dead" in d.reason.lower() and "running_max" in d.reason


def test_bet_when_forecast_rounds_exactly_onto_running_max():
    # running_max 25, remaining 24.5 → floor(25.0)=25 == running_max → the floored CONFIDENT pick ==25, not a
    # coin-flip. (Regression: `floored` used strict `<` while the flooring uses max() (≤), so a round landing
    # exactly on running_max was mis-flagged as a boundary tie and ESCALATEd the sure thing.)
    state = make_state({25: 0.50, 24: 0.40}, running_max=25)
    d = decide(state, 24.5, "EGLC", now=NOW, peak_utc=PEAK)
    assert d is not None and d.action == "BET" and d.threshold == 25
    assert "boundary" not in d.reason.lower()


def test_nearest_alive_bucket_rounds_and_floors():
    state = make_state({}, running_max=21)
    assert nearest_alive_bucket(state, 25.9).threshold == 26          # round half up
    assert nearest_alive_bucket(state, 24.5).threshold == 25          # 24.5 → 25 (not banker's 24)
    assert nearest_alive_bucket(state, 20.0).threshold == 21          # 20 < running_max 21 → floored to 21
    assert nearest_alive_bucket(state, None) is None
    # the LE/GE edge buckets resolve when nothing floors them away (low/no running_max):
    low = make_state({}, running_max=None)
    assert nearest_alive_bucket(low, 19.2).comparator is Comparator.LE   # ≤19 edge holds 19
    assert nearest_alive_bucket(low, 30.0).comparator is Comparator.GE   # ≥29 edge holds 30
