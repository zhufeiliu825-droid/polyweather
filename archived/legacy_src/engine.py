"""Edge engine. Pure functions over an EventState — stateless and directly
unit-testable / backtest-drivable.

The realized daily max is monotonic non-decreasing, so:
  * buckets strictly below the running max are impossible -> fair value 0;
  * the only uncertainty is whether remaining hours exceed the running max.
The riskless signals here assert exactly those hard constraints. They are gated
on the realized max being fresh and the city verified, because a stale or wrong
max would manufacture false "dead" buckets — the failure mode to fear most.
"""
from __future__ import annotations

from .config import SignalThresholds
from .models import Bucket, Confidence, EventState, Regime, Signal, SignalType

# Regime -> (UPSIDE confidence, edge-threshold multiplier). Riskless signals are
# never gated by regime — only the probabilistic UPSIDE signal is.
_REGIME_GATE: dict[Regime, tuple[Confidence, float]] = {
    Regime.CLEAR_STABLE: (Confidence.MEDIUM, 1.0),
    Regime.OVERCAST: (Confidence.MEDIUM, 1.0),
    Regime.SHOWERY: (Confidence.LOW, 1.5),
    Regime.CONVECTIVE: (Confidence.LOW, 2.0),
}


def _label(b: Bucket) -> str:
    return f"{b.comparator.value}{b.threshold}{b.unit}"


def devig(state: EventState) -> tuple[dict[tuple, float], float]:
    """Normalize the 11 mutually-exclusive bucket Yes-prices into a probability
    distribution. Returns (normalized {key: p}, raw_sum). raw_sum != 1 measures
    the vig / book staleness."""
    raw: dict[tuple, float] = {}
    for b in state.buckets:
        p = b.implied_p
        raw[(b.comparator, b.threshold)] = p if p is not None else 0.0
    total = sum(raw.values())
    if total <= 0:
        return {k: 0.0 for k in raw}, 0.0
    return {k: v / total for k, v in raw.items()}, total


def evaluate(
    state: EventState,
    th: SignalThresholds,
    *,
    local_hour: int,
    wu_poll_age_s: float | None,
    peak_passed: bool | None = None,
) -> list[Signal]:
    """Compute current edge signals for an event. `peak_passed` (from the
    conditions layer) overrides the fixed peak_hour_end when provided.

    DEPRECATED on the live trading path (2026-06-15): this (and `evaluate_upside`)
    is the LEGACY read-only monitor's riskless dead/locked-bucket arb. The live
    strategy uses NO arb — see decision.py. `devig()` above is still shared/current;
    these signal functions are not on the new path."""
    signals: list[Signal] = []
    fresh = wu_poll_age_s is not None and wu_poll_age_s <= th.wu_staleness_sec
    # riskless signals require: verified city, fresh realized max, a known max.
    if not (state.verified and fresh and state.running_max is not None):
        return signals

    norm, raw_sum = devig(state)
    data_ok = th.devig_sum_lo <= raw_sum <= th.devig_sum_hi

    def mk(typ, b, price, fair, action, conf, note=""):
        return Signal(
            type=typ, city=state.city, local_date=state.local_date,
            label=_label(b), comparator=b.comparator, threshold=b.threshold,
            market_price=round(price, 4), fair=fair, edge=round(abs(price - fair), 4),
            confidence=conf, action=action, running_max=state.running_max, note=note,
        )

    # (A) DEAD_BUCKET — impossible bucket with a live bid you can sell into.
    for b in state.buckets:
        if not state.is_dead(b):
            continue
        bid = b.best_bid or 0.0
        if bid >= th.dead_bucket_min_price and (b.liquidity or 0.0) >= th.min_liquidity:
            signals.append(mk(
                SignalType.DEAD_BUCKET, b, bid, 0.0, "SELL YES", Confidence.HIGH,
                note=f"impossible (max already {state.running_max:g}); bid={bid:g}",
            ))

    # (B) LOCKED_CUMULATIVE — dead buckets still holding de-vigged probability.
    if data_ok:
        dead_mass = sum(
            norm[(b.comparator, b.threshold)] for b in state.buckets if state.is_dead(b)
        )
        if dead_mass >= th.locked_cumulative_gap:
            cur = state.current_bucket()
            if cur is not None:
                signals.append(mk(
                    SignalType.LOCKED_CUMULATIVE, cur, 1.0 - dead_mass, 1.0,
                    "BUY live buckets", Confidence.MEDIUM,
                    note=f"dead buckets hold {dead_mass:.1%} of de-vigged mass",
                ))

    # (C) LOCKED_SINGLE — past the local peak, the current-max bucket -> ~1.
    peaked = peak_passed if peak_passed is not None else (local_hour >= th.peak_hour_end)
    cur = state.current_bucket()
    if cur is not None and peaked:
        p = cur.implied_p or 0.0
        if p <= th.locked_single_max_price and (cur.liquidity or 0.0) >= th.min_liquidity:
            signals.append(mk(
                SignalType.LOCKED_SINGLE, cur, p, 1.0, "BUY YES", Confidence.HIGH,
                note=f"past peak hour {th.peak_hour_end}; max locked at {state.running_max:g}",
            ))
    return signals


def evaluate_upside(
    state: EventState,
    fc_probs: dict[tuple, float],
    th: SignalThresholds,
    *,
    regime: Regime | None = None,
) -> list[Signal]:
    """Forecast-driven signals: where the forecast-implied bucket probability
    disagrees with the (de-vigged) market by >= upside_edge_min. Only for live
    (not-dead) buckets. Lower confidence — this is prediction, not a hard
    constraint. `regime` (from the conditions layer) sets confidence and widens
    the edge threshold on showery/convective days."""
    out: list[Signal] = []
    market, raw_sum = devig(state)
    if not (th.devig_sum_lo <= raw_sum <= th.devig_sum_hi):
        return out
    conf, mult = _REGIME_GATE.get(regime, (Confidence.MEDIUM, 1.0))
    threshold = th.upside_edge_min * mult
    regime_note = f", regime={regime.value}" if regime is not None else ""
    for b in state.buckets:
        if state.is_dead(b):
            continue
        key = (b.comparator, b.threshold)
        fc = fc_probs.get(key)
        mk = market.get(key)
        if fc is None or mk is None:
            continue
        diff = fc - mk
        if abs(diff) >= threshold:
            action = "BUY YES" if diff > 0 else "SELL YES"
            out.append(Signal(
                type=SignalType.UPSIDE, city=state.city, local_date=state.local_date,
                label=_label(b), comparator=b.comparator, threshold=b.threshold,
                market_price=round(mk, 4), fair=round(fc, 4), edge=round(abs(diff), 4),
                confidence=conf, action=action, running_max=state.running_max,
                note=f"forecast {fc:.0%} vs market {mk:.0%}{regime_note}",
            ))
    return out


class SignalTracker:
    """Edge-triggered dedup so the monitor isn't chatty: emit a signal only when
    it newly appears, and report when one clears."""

    def __init__(self) -> None:
        self.active: set[tuple] = set()

    def diff(self, signals: list[Signal]) -> tuple[list[Signal], set[tuple]]:
        keys = {s.key() for s in signals}
        new = [s for s in signals if s.key() not in self.active]
        cleared = self.active - keys
        self.active = keys
        return new, cleared
