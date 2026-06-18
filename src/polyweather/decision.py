"""Per-city decision — the AGENT chooses the bucket; the deterministic layer only VETOES an unsafe bet.

The bucket the day's HIGH lands in is a JUDGMENT call (a 25.9 forecast → ==26, adjusted by the realized
climb + the atmosphere). That judgment belongs to the agent (`validate_proposal`), NOT a hard-coded scoring
formula. When the agent is unavailable, `decide()` is a simple deterministic fallback: round the forecast
peak to the nearest integer → that bucket (floored to the realized running_max).

Either way the SAME hard gates bound every bet (they only ever VETO → ESCALATE, never choose the bucket):
  - ALIVE        : EQ/LE below running_max are dead; GE at/below running_max has already won (no upside)
  - price        : fav market price < PRICE_CEILING (≥ → no upside left)
  - health       : de-vig bucket-price sum ~1, target priced > ~0 (a bucket the market deems impossible is
                   rejected — a natural sanity bound), spread not absurd
  - size / floor : ≥ exchange-min shares, ≤ notional cap, a resting maker ≥ the min tick

Only PRE-PEAK (now < the strike-extended cutoff); after it the daemon opens nothing new. No daily cap, no
forced bet.
"""
from __future__ import annotations

import math
from datetime import datetime

from .models import Comparator, Decision, EventState
from .params import (DEAD_BUCKET_MARGIN, HEALTH_SUM_HI, HEALTH_SUM_LO, MAKER_FLOOR, MAKER_UNDERCUT,
                     MAX_SIZE_USD, MAX_SPREAD, MIN_ORDER_SHARES, MIN_ORDER_USD, PRICE_CEILING,
                     SIZE_SHARES)  # tunable knobs

HEALTH_SUM = (HEALTH_SUM_LO, HEALTH_SUM_HI)    # acceptable band for the de-vigged bucket-price sum


def _bucket_alive(comparator: Comparator, threshold: int, running_max) -> bool:
    """Can this bucket still be the FINAL daily max? The max is monotonic non-decreasing, so:
    EQ/LE → alive iff threshold ≥ running_max (a lower bucket already lost).
    GE   → alive iff threshold > running_max — a GE bucket whose threshold ≤ running_max has
           ALREADY won (p≈1, no upside), so we never open a new position on it either."""
    if running_max is None:
        return True
    if comparator is Comparator.GE:
        return threshold > running_max
    return threshold >= running_max


def bucket_price_sum(state: EventState) -> float:
    """Sum the 11 mutually-exclusive bucket Yes-prices. A clean book sums ~1; a sum far off 1
    (or 0) flags a stale/broken market. We bet OUR fc_probs (never a market-de-vigged dist), so
    only the SUM is used — for the health gate. (Was `devig` in the legacy engine.py.)"""
    return sum((b.implied_p if b.implied_p is not None else 0.0) for b in state.buckets)


def market_health(state: EventState, key: tuple) -> tuple[bool, str]:
    """Sanity-check the market for the target bucket. NOT an edge check, NOT the
    0.85 ceiling (that is a separate gate)."""
    raw_sum = bucket_price_sum(state)
    if not (HEALTH_SUM[0] <= raw_sum <= HEALTH_SUM[1]):
        return False, f"bucket-price sum {raw_sum:.2f} off 1 (stale/broken market)"
    b = next((x for x in state.buckets if (x.comparator, x.threshold) == key), None)
    if b is None:
        return False, "target bucket not listed"
    p = b.implied_p
    if p is None:
        return False, "no price for target bucket"
    if p <= 0.005:
        return False, f"market prices target ~0 ({p:.3f}) — disagrees hard with our call"
    if b.best_bid is None or b.best_ask is None:
        # one-sided / illiquid book → implied_p fell back to a (possibly stale) last_trade /
        # gamma price. Never AUTO-bet off a stale price into a thin book — escalate (audit L1).
        return False, "one-sided/illiquid book (no live bid+ask) — only a stale price"
    if (b.best_ask - b.best_bid) > MAX_SPREAD:
        return False, f"spread {b.best_ask - b.best_bid:.2f} too wide"
    return True, "ok"


def top_dist(state: EventState, k: int = 3) -> list[dict]:
    """Top-k buckets by MARKET price, each flagged alive/dead — attached to a Decision so the escalation
    message shows what the market thinks is likely (and which buckets are still live)."""
    rows = [{"label": f"{b.comparator.value}{b.threshold}",
             "mkt": (round(b.implied_p, 3) if b.implied_p is not None else None),
             "alive": _bucket_alive(b.comparator, b.threshold, state.running_max)}
            for b in state.buckets]
    rows.sort(key=lambda r: (r["mkt"] is not None, r["mkt"] or 0.0), reverse=True)
    return rows[:k]


def _bucket_for_temp(state: EventState, temp: int):
    """The single ladder bucket whose RANGE contains integer `temp`: EQ t, the LE edge (≤t), or the GE
    edge (≥t). The 11 buckets partition the temperature line, so exactly one matches (None if absent)."""
    for b in state.buckets:
        if b.comparator is Comparator.EQ and b.threshold == temp:
            return b
        if b.comparator is Comparator.LE and temp <= b.threshold:
            return b
        if b.comparator is Comparator.GE and temp >= b.threshold:
            return b
    return None


def nearest_alive_bucket(state: EventState, remaining: float | None):
    """The bucket the forecast peak rounds INTO (round-half-up), floored to the realized running_max — the
    day's max can't end below what already happened. The simple, obvious pick a human would make from a
    single forecast number (25.9 → ==26). None if no forecast / no matching bucket."""
    if remaining is None:
        return None
    target = math.floor(remaining + 0.5)                  # round half UP (not banker's): 25.5→26, 24.5→25
    if state.running_max is not None:
        target = max(target, state.running_max)
    return _bucket_for_temp(state, target)


def _order_plan(b, maker_size: float = SIZE_SHARES) -> tuple[str, float | None, float]:
    """The MAKER limit to POST + the share count for bucket `b`. ALWAYS a resting maker — it earns liquidity
    rewards, pays 0 fee, and never crosses; a bid at market − MAKER_UNDERCUT (snapped DOWN to the tick at place
    time), `maker_size` shares. On a bucket so cheap the default size is below the $1 platform minimum, SIZE UP
    the share count so it still clears $1. (Was: a <11¢ fav crossed to the ask as a $1 FAK taker — that paid the
    ask + the new Polymarket taker fee and earned no reward; a sized-up resting maker is strictly better.)

    PRECISION: the makerAmount (= size × price) must be ≤ 2 decimals or Polymarket rejects the order. The
    executor snaps the price DOWN to the live tick, so we size against the TICK-FLOORED price — ≥0.04 (0.01
    tick) → an integer count keeps size×price ≤2dp; <0.04 (0.001 tick) → a multiple of 10 keeps 10×(3dp)=≤2dp.
    Sizing against the floor (≤ the real snapped price) means clearing $1 can never break post-snap. Returns
    ("maker", limit_price, size_shares); limit_price is None only when there's no usable price."""
    price = b.implied_p if b else None
    if price is None:
        return "maker", None, maker_size
    maker_price = round(price - MAKER_UNDERCUT, 4)
    size = maker_size
    if maker_price > 0 and maker_price * maker_size < MIN_ORDER_USD - 1e-9:   # default size sub-$1 → size up to clear it
        if maker_price >= 0.04:                                               # 0.01-tick → floor to the cent, integer shares
            floor_p = math.floor(maker_price * 100) / 100
            size = float(math.ceil(MIN_ORDER_USD / floor_p))
        else:                                                                 # 0.001-tick → floor to the milli, shares in 10s
            floor_p = math.floor(maker_price * 1000) / 1000
            size = float(10 * math.ceil(MIN_ORDER_USD / (floor_p * 10))) if floor_p > 0 else maker_size
    return "maker", maker_price, size


def decide(
    state: EventState,
    remaining: float | None,
    station_id: str,
    *,
    now: datetime,
    peak_utc: datetime,
    cutoff_utc: datetime | None = None,
    size_shares: float = SIZE_SHARES,
    price_ceiling: float = PRICE_CEILING,
) -> Decision | None:
    """Deterministic FALLBACK decision (used only when the agent is unavailable): bet the bucket the
    forecast peak `remaining` rounds into (floored to running_max), gated by the hard safety checks. An
    X.5 boundary (forecast ~halfway between two integers) → ESCALATE rather than bet a coin-flip with no
    agent to judge it. Returns a Decision (BET/ESCALATE), or None (no forecast / past the cutoff)."""
    if remaining is None:
        return None
    if now >= (cutoff_utc or peak_utc):   # past the (strike-extended) peak window → no new positions
        return None                       # (cutoff defaults to peak_utc — strike layer extends it to cover a late peak)
    b = nearest_alive_bucket(state, remaining)
    if b is None:
        return None
    key = (b.comparator, b.threshold)
    price = b.implied_p
    # ALIVE gate (mirrors validate_proposal / the agent path): nearest_alive_bucket floors to running_max,
    # but the top GE edge can still come back DEAD — `>=N` with N ≤ running_max has already WON (p≈1, no
    # upside). Gate it here exactly as _bucket_alive does, so the agent-down fallback can never auto-bet a
    # resolved bucket (the "dumb bucket" the agent path already rejects).
    alive = _bucket_alive(b.comparator, b.threshold, state.running_max)
    # boundary: the forecast sits ~halfway between two integers AND we're not floored to running_max. "Floored"
    # = the round lands AT OR BELOW running_max (≤, matching the max() flooring) → ==running_max is the confident
    # pick, not a coin-flip. Only an unfloored ~.5 forecast is a true tie with no agent to break it → ESCALATE.
    rounded = math.floor(remaining + 0.5)
    floored = state.running_max is not None and rounded <= state.running_max
    boundary = (not floored) and 0.35 < (remaining - math.floor(remaining)) < 0.65

    order_type, limit_price, plan_size = _order_plan(b, size_shares)   # always a maker (sized up if cheap → ≥$1)
    healthy, why = market_health(state, key)
    price_ok = price is not None and price < price_ceiling
    maker_ok = price is not None and (price - MAKER_UNDERCUT) >= MAKER_FLOOR   # the resting bid stays ≥ the min tick
    shares_ok = plan_size >= MIN_ORDER_SHARES           # ≥ 5-share exchange min
    notional = plan_size * limit_price if limit_price else 0.0
    usd_ok = notional >= MIN_ORDER_USD - 1e-9          # ≥ $1 platform min (the maker is sized to clear it)
    bet = (not boundary) and alive and healthy and price_ok and maker_ok and shares_ok and usd_ok
    # maker_price is the limit we'd POST (market − undercut), populated ONLY on a real BET. An ESCALATE carries
    # no order price, so the daemon's reply path can never mistake a non-BET decision for something placeable.
    maker_price = limit_price if (bet and price is not None) else None

    reasons = []
    if boundary:
        reasons.append(f"forecast {remaining:.1f} on a bucket boundary (~.5) — no agent to break the tie")
    if not alive:
        reasons.append(f"{b.comparator.value}{b.threshold} already resolved vs running_max {state.running_max} — dead bucket")
    if not healthy:
        reasons.append(f"market unhealthy ({why})")
    if price is None:
        reasons.append("no market price")
    elif price >= price_ceiling:
        reasons.append(f"fav price {price:.2f} ≥ {price_ceiling:.2f} (no upside)")
    elif not maker_ok:
        reasons.append(f"maker {round(price - MAKER_UNDERCUT, 4)} < floor {MAKER_FLOOR}")
    elif not shares_ok:
        reasons.append(f"size {plan_size}sh < {MIN_ORDER_SHARES}-share exchange min")
    elif not usd_ok:
        reasons.append(f"notional ${notional:.2f} < ${MIN_ORDER_USD} platform min")

    return Decision(
        city=state.city, station_id=station_id, local_date=state.local_date,
        label=f"{b.comparator.value}{b.threshold}{b.unit}",
        comparator=b.comparator, threshold=b.threshold, token_yes=b.token_yes,
        order_type=order_type,
        action=("BET" if bet else "ESCALATE"),
        confidence=None,                               # deterministic fallback — no model probability
        market_yes=(round(price, 3) if price is not None else None),
        maker_price=maker_price, size_shares=plan_size, health_ok=healthy,
        alts=top_dist(state),
        reason=(f"forecast {remaining:.1f} → {b.comparator.value}{b.threshold}, healthy, price {price:.2f} < "
                f"{price_ceiling:.2f} → fallback auto-bet {order_type} @ {maker_price:.2f}"
                if bet else "ESCALATE: " + "; ".join(reasons)),
    )


_COMP = {"<=": Comparator.LE, "==": Comparator.EQ, ">=": Comparator.GE}


def validate_proposal(
    proposal: dict | None,
    state: EventState,
    remaining: float | None,
    station_id: str,
    *,
    now: datetime,
    peak_utc: datetime,
    cutoff_utc: datetime | None = None,
    size_shares: float = SIZE_SHARES,
    price_ceiling: float = PRICE_CEILING,
) -> Decision | None:
    """Hard-validate the chooser's (agent OR Telegram operator) structured BET proposal against the FACTS.
    THIS is the authority on the HARD SAFETY floor — none of it is taken on the proposer's word. The
    proposer CHOOSES the bucket (judgment); a BET is accepted ONLY if that bucket is real, ALIVE (>
    running_max — dead-bucket backstop), priced < ceiling, in a healthy market (incl. the market not pricing
    it ~0 — a bucket the market deems impossible is rejected), and sized within the share/notional caps.

    The bucket is NOT second-guessed against any model favorite — that's the whole point of the agent's
    judgment (a 25.9 forecast → ==26, which a hard-coded scoring formula used to override). Anything failing
    a safety gate → ESCALATE (SKIP / post-peak → None). Same Decision type the executor consumes."""
    if now >= (cutoff_utc or peak_utc):               # past the (strike-extended) peak window → no new positions
        return None
    proposal = proposal or {}
    action = str(proposal.get("action", "")).upper()
    agent_reason = str(proposal.get("reason", ""))[:200]
    conf = proposal.get("confidence")

    comp = _COMP.get(proposal.get("comparator"))
    thr = proposal.get("threshold")
    b = (next((x for x in state.buckets if x.comparator is comp and x.threshold == thr), None)
         if comp is not None and isinstance(thr, int) else None)

    def _alive(key: tuple) -> bool:
        return _bucket_alive(key[0], key[1], state.running_max)

    def _fallback():                                  # bucket for ESCALATE context = the proposer's, else round(peak)
        return b if b is not None else nearest_alive_bucket(state, remaining)

    def _mk(act: str, bucket, reason: str, healthy: bool = True) -> Decision:
        price = bucket.implied_p if bucket else None
        order_type, limit_price, plan_size = (_order_plan(bucket, size_shares) if bucket
                                              else ("maker", None, size_shares))
        return Decision(
            city=state.city, station_id=station_id, local_date=state.local_date,
            label=(f"{bucket.comparator.value}{bucket.threshold}{bucket.unit}" if bucket else "?"),
            comparator=(bucket.comparator if bucket else Comparator.EQ),
            threshold=(bucket.threshold if bucket else 0),
            token_yes=(bucket.token_yes if bucket else ""),
            order_type=order_type,
            action=act,
            confidence=(round(float(conf), 3) if isinstance(conf, (int, float)) else None),
            market_yes=(round(price, 3) if price is not None else None),
            maker_price=(limit_price if (act == "BET" and price is not None) else None),
            size_shares=plan_size, health_ok=healthy, alts=top_dist(state), reason=reason,
        )

    if action == "SKIP":
        return None
    if action != "BET":                               # ESCALATE / unknown → hand to user
        return _mk("ESCALATE", _fallback(), f"agent ESCALATE: {agent_reason}")

    # action == BET → HARD checks; any failure DOWNGRADES to ESCALATE (never a bad live order)
    if b is None:
        return _mk("ESCALATE", _fallback(),
                   f"agent BET but bucket {proposal.get('comparator')}{thr} not in event → escalate")
    if not _alive((b.comparator, b.threshold)):
        return _mk("ESCALATE", b,
                   f"agent BET {b.threshold} but running_max {state.running_max} → DEAD bucket, rejected")
    # DEAD-BUCKET-AT-MAX: ==running_max wins ONLY if the temp does NOT rise above it. If the remaining-hours
    # forecast peak is ≥ running_max + margin, the temp is expected to climb PAST it → the bucket likely dies
    # → escalate (don't auto-bet the about-to-die at-the-current-max bucket). (`remaining` is future-hours only.)
    if (b.comparator is Comparator.EQ and state.running_max is not None and b.threshold == state.running_max
            and remaining is not None and remaining >= state.running_max + DEAD_BUCKET_MARGIN):
        return _mk("ESCALATE", b, f"agent BET =={b.threshold} = running_max but forecast peak {remaining:.1f} "
                                  f"≥ running_max+{DEAD_BUCKET_MARGIN} → temp likely rises past it (dead-bucket "
                                  f"risk) → escalate")
    price = b.implied_p
    if price is None or price >= price_ceiling:
        return _mk("ESCALATE", b, f"agent BET but price {price} ≥ {price_ceiling} → no upside, rejected")
    _, lim, plan_size = _order_plan(b, size_shares)    # always a maker (sized up if cheap → ≥$1)
    if price - MAKER_UNDERCUT < MAKER_FLOOR:            # the resting maker bid would be below the min tick
        return _mk("ESCALATE", b, f"agent BET but maker {round(price - MAKER_UNDERCUT, 4)} < floor {MAKER_FLOOR} → rejected")
    if plan_size < MIN_ORDER_SHARES:                   # exchange min is 5 SHARES
        return _mk("ESCALATE", b, f"agent BET but size {plan_size}sh < {MIN_ORDER_SHARES}-share exchange min → rejected")
    healthy, why = market_health(state, (b.comparator, b.threshold))
    if not healthy:
        return _mk("ESCALATE", b, f"agent BET but market unhealthy ({why}) → rejected", healthy=False)
    notional = plan_size * lim if lim else 0.0
    if not (plan_size > 0) or notional > MAX_SIZE_USD:   # $ notional safety ceiling
        return _mk("ESCALATE", b, f"notional {plan_size}×{lim} = ${notional:.2f} > ${MAX_SIZE_USD} cap → rejected")
    if notional < MIN_ORDER_USD - 1e-9:                  # $ platform minimum (the maker is sized to clear it)
        return _mk("ESCALATE", b, f"notional ${notional:.2f} < ${MIN_ORDER_USD} platform min → rejected")
    return _mk("BET", b, f"agent BET (validated): {agent_reason} [conf={conf}]")
