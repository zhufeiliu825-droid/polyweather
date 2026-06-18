"""Reward FARMER (pilot) — from the scanner's ranked targets, pick EDGE-ALIGNED reward buckets and rest
reward-qualifying maker BIDS. Selection (low fill risk + our edge): HEALTHY books only (real two-sided, sane
spread, de-vig ~1 — never a placeholder/fake price), priced in [0.10, 0.84], TOMORROW+ events only for new
buys (today remains available to value and maker-sell existing holdings), and —
per event — our forecast's TOP-2 most-likely buckets. An hourly batched LLM adviser estimates the current
forecast adjustment from station/regime residuals and can recommend no participation; the five-minute loop
reuses that cached value, with deterministic regime calibration as fallback. Each gets ONE passive maker bid, sized ≥ rewardsMinSize, greedy by score under a
hard capital cap. The bids are GTC (no
expiry — max in-band time = max reward); the loop tracks the moving band by CANCEL→confirm→REPLACE when the
bid drifts > REPRICE_DRIFT. On a FILL, Stage 2 rests a fair-aware maker SELL: capture 50% of entry edge (2–8¢),
flatten only when fresh fair invalidates the thesis,
then force-flat at peak−6h. Top-N churn never closes a held position. DRY by default; LIVE placement remains a
separate, gated (POLY_DRY_RUN=0) step."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from .. import clob_executor as ex
from ..models import Comparator, Decision
from ..params import HEALTH_SUM_HI, HEALTH_SUM_LO, MAKER_FLOOR as PRICE_FLOOR
from ..params import MAX_SPREAD, MIN_ORDER_SHARES, MIN_ORDER_USD
from ..realized.feature_store import DATA_DIR
from .advice import apply_advice, hourly_advice
from .scan import scan

FARM_LOG = DATA_DIR / "reward_orders.jsonl"
REWARD_STATE_FILE = DATA_DIR / "reward_state.json"
MIN_LEAN = 0.3                 # floor to be a top-2 candidate — the peak's two straddling buckets clear this when it sits genuinely between them
MAX_PER_EVENT = 2              # farm the ≤2 MOST-LIKELY buckets per event (the peak's floor+ceil) — user's design
MID_LO, MID_HI = 0.10, 0.84   # single-leg scores ≥0.10; cap <0.85 so the bid clears the executor's price ceiling
TICK = 0.01                   # every farmed bucket is in [0.10,0.84] → the 0.01 tick (50 sh × 2-dp price = ≤2-dp ✓)
REPRICE_DRIFT = 0.02          # reprice when the resting bid drifts > this from the live mid (score ≈31% at 2¢, ≈0 at the 4.5¢ band edge)
UNDERCUT_C = 0.02             # bid this far below mid AND never above the best bid → a PASSIVE maker (sits in the band, doesn't jump near the ask = instant fill)
MIN_MODEL_EDGE = 0.03          # only buy when calibrated fair P(YES) is at least 3¢ ABOVE our passive bid
SELL_MARKUP_C = 0.02           # minimum take-profit; larger model edges target a larger gain below
SELL_EDGE_CAPTURE = 0.50       # initially ask for half of the model edge
SELL_MAX_MARKUP_C = 0.08       # do not wait for more than 8¢ of price improvement before recycling capital
FAIR_EXIT_BUFFER_C = 0.02      # leave 2¢ of model upside to the buyer at our take-profit ask
THESIS_BREAK_C = 0.03          # fresh fair below cost by >3¢ means the forecast thesis failed → defensive maker exit
FORCE_FLATTEN_LEAD_MIN = 360   # user-confirmed 关门时间: within 6h of the forecast peak, force-flatten at the bid
_COMP = {"<=": Comparator.LE, "==": Comparator.EQ, ">=": Comparator.GE}
_SHARE_QUANTUM = Decimal("0.01")


@dataclass
class FarmOrder:
    city: str
    station_id: str
    comparator: str
    threshold: int
    market_date: str
    bucket: str
    token_yes: str
    price: float                  # the resting bid: mid floored to the tick (a maker, within ±max_spread, top score)
    size: float                   # ≥ rewardsMinSize shares
    capital: float                # size × price — USDC tied up while it rests
    daily_rate: float             # the bucket's reward pool, USDC/day
    competition: float            # resting shares within ±max_spread (what we split the pool with)
    est_share: float              # our rough share ≈ size / (competition + size)
    est_daily_reward: float       # est_share × daily_rate
    lean: float                   # our forecast YES-lean on this bucket
    hours_to_peak: float
    mid: float                    # live mid at placement — Stage 2 prices the scalp/flatten sell off cost & this
    peak_utc: str | None          # forecast-peak time (ISO) — 关门时间 anchor: taker force-flatten at peak−FORCE_FLATTEN_LEAD_MIN
    fair_yes: float | None        # calibrated P(YES) at placement; must exceed the passive bid by MIN_MODEL_EDGE
    model_edge: float = 0.0       # fair_yes − bid; always >= MIN_MODEL_EDGE for newly selected orders
    reward_min_size: float = 0.0  # minimum resting size required by this market's reward program
    reward_max_spread_c: float = 0.0
    forecast_regime: dict | None = None
    calibration: dict | None = None
    forecast_peak_adjusted: float | None = None
    llm_advice: dict | None = None


def _healthy(t: dict) -> bool:
    """Liquidity/price sanity (reuses the directional bot's market_health thresholds): a real two-sided book
    with a sane spread + a de-vig sum ~1. Rejects placeholder books (bid 0.01/ask 0.99 → wide spread) and
    empty markets (all buckets ~0.5 → de-vig sum ≫ 1) whose 'mid' is a fake price we must not bid against."""
    spread = t.get("spread")
    if spread is None or spread > MAX_SPREAD:
        return False
    dv = t.get("devig_sum")
    return dv is not None and HEALTH_SUM_LO <= dv <= HEALTH_SUM_HI


def _bid_price(t: dict) -> float:
    """The passive maker bid for a target: at/below the best bid AND ≥ UNDERCUT_C below the mid, floored to the
    tick. NEVER improves the bid (jumping toward the ask = instant fill, the bug we fixed)."""
    best_bid = t["mid"] - (t.get("spread") or 0.0) / 2.0                              # mid = (bid+ask)/2, spread = ask−bid → best_bid = mid − spread/2
    raw = min(best_bid, t["mid"] - UNDERCUT_C)                                        # sit at/below the best bid, ≥ undercut below mid
    return math.floor(raw / TICK + 1e-9) * TICK                                       # in the reward band, not the aggressive top-of-book


def _model_edge(t: dict) -> float | None:
    """Calibrated price edge at our passive bid. Missing fair data is not enough evidence to risk capital."""
    fair = t.get("fair_yes")
    return fair - _bid_price(t) if fair is not None else None


def _has_positive_edge(t: dict, *, min_edge: float = MIN_MODEL_EDGE) -> bool:
    edge = _model_edge(t)
    return edge is not None and edge >= min_edge - 1e-9


def select(targets: list[dict], *, capital_cap: float, max_targets: int, min_lean: float = MIN_LEAN,
           size_mult: float = 1.0, max_per_event: int = MAX_PER_EVENT) -> list[FarmOrder]:
    """Pick edge-aligned reward bids: HEALTHY book (real two-sided, de-vig ~1), in-band, CHEAP vs our
    calibrated fair value by at least `MIN_MODEL_EDGE`, and — per EVENT — our forecast's ≤`max_per_event`
    MOST-LIKELY buckets. Then rank by reward/competition score × model edge under the capital cap + max_targets.
    One maker bid each (≥ reward min_size)."""
    cand = [t for t in targets
            if t.get("entry_eligible", True)                                           # today may be valuation-only context
            and (t.get("our_yes_lean") or 0.0) >= min_lean                             # forecast favors it (top-2 floor)
            and t.get("mid") is not None and MID_LO <= t["mid"] <= MID_HI              # in single-leg scoring band
            and _healthy(t)                                                            # real book + sane price
            and _has_positive_edge(t)]                                                 # model says our bid is >=3¢ cheap
    by_event: dict[tuple, list[dict]] = {}                                            # keep the ≤2 most-likely per event
    for t in cand:
        by_event.setdefault((t["city"], t["market_date"]), []).append(t)
    kept: list[dict] = []
    for ev_ts in by_event.values():
        ev_ts.sort(key=lambda t: -(t.get("our_yes_lean") or 0.0))                     # most-likely first
        kept.extend(ev_ts[:max_per_event])
    kept.sort(key=lambda t: -(t["score"] * (_model_edge(t) or 0.0)))                  # reward quality × price edge
    picks: list[FarmOrder] = []
    spent = 0.0
    for t in kept:
        size = float(t["min_size"]) * size_mult
        price = _bid_price(t)                                                         # passive maker bid
        capital = round(size * price, 2)
        if spent + capital > capital_cap + 1e-9:                                      # over the cap → skip the next (cheaper) one
            continue
        share = size / (t["competition"] + size)
        picks.append(FarmOrder(
            city=t["city"], station_id=t["station_id"], comparator=t["comparator"], threshold=t["threshold"],
            market_date=t["market_date"], bucket=t["bucket"], token_yes=t["token_yes"],
            price=round(price, 4), size=size, capital=capital, daily_rate=t["daily_rate"],
            competition=t["competition"], est_share=round(share, 4),
            est_daily_reward=round(share * t["daily_rate"], 2),
            lean=t.get("our_yes_lean"), hours_to_peak=t.get("hours_to_peak"), mid=t["mid"],
            peak_utc=t.get("peak_utc"), fair_yes=t.get("fair_yes"),
            model_edge=round(_model_edge(t) or 0.0, 4), reward_min_size=float(t["min_size"]),
            reward_max_spread_c=float(t.get("max_spread") or 0.0),
            forecast_regime=t.get("forecast_regime"), calibration=t.get("calibration"),
            forecast_peak_adjusted=t.get("forecast_peak_adjusted"), llm_advice=t.get("llm_advice")))
        spent += capital
        if len(picks) >= max_targets:
            break
    return picks


def log_orders(orders: list[FarmOrder], now_iso: str, *, dry: bool = True) -> None:
    with FARM_LOG.open("a") as f:
        for o in orders:
            f.write(json.dumps({"ts": now_iso, "dry": dry, **asdict(o)}, ensure_ascii=False) + "\n")


def to_decision(o: FarmOrder) -> Decision:
    """The executor `Decision` for a reward bid: a resting maker BUY YES of `o.size` shares at `o.price`.
    Reuses the directional executor (validation + crash-safe logging) — same maker path, just a longer TTL."""
    return Decision(
        city=o.city, station_id=o.station_id, local_date=date.fromisoformat(o.market_date),
        label=o.bucket, comparator=_COMP[o.comparator], threshold=o.threshold, token_yes=o.token_yes,
        action="BET", side="BUY_YES", order_type="maker", confidence=None,
        market_yes=o.price, maker_price=o.price, size_shares=o.size, health_ok=True)


def _sell_decision(entry: dict, *, token: str, price: float, size: float, taker: bool = False) -> Decision:
    """The executor Decision for a Stage-2 SELL of `size` shares at `price`. `taker=False` (normal) → a resting
    maker SELL YES (ask), snapped UP. `taker=True` (关门时间 force-flatten) → a FAK SELL priced AT the bid that
    crosses for a guaranteed fill. Rebuilt from the FILLED state `entry` so it still works after the bucket
    dropped from the scan targets; mirror of `to_decision`, reusing `_COMP`."""
    return Decision(
        city=entry["city"], station_id=entry["station_id"], local_date=date.fromisoformat(entry["market_date"]),
        label=entry["bucket"], comparator=_COMP[entry["comparator"]], threshold=entry["threshold"], token_yes=token,
        action="BET", side="SELL_YES", order_type=("taker" if taker else "maker"), confidence=None,
        market_yes=price, maker_price=price, size_shares=size, health_ok=True)


def place_orders(orders: list[FarmOrder], *, now_iso: str, ttl_min: int | None = None):
    """Place each reward bid via the executor — DRY-AWARE (POLY_DRY_RUN=1 just logs the intended order, no live
    submit). `ttl_min=None` → a GTC that rests until filled or cancelled (the farmer's no-expire order; the loop
    tracks the band via cancel+replace, not expiry). Returns [(FarmOrder, OrderResult)]. Placement is the user's
    live action; this stays DRY until they flip it."""
    return [(o, ex.place_from_decision(to_decision(o), now_iso=now_iso, ttl_min=ttl_min)) for o in orders]


# ── continuous buy-side loop (Stage 1) ──────────────────────────────────────────────────────────────────

def _held_size(held: dict | None, token: str) -> float | None:
    """Shares of `token` we currently hold (0.0 if none) — None if positions couldn't be queried."""
    if held is None:
        return None
    info = held.get(str(token))
    return float(info["size"]) if info else 0.0


def _floor_sell_size(size: float) -> float:
    """Quantize a sell size down to 0.01 shares so it never exceeds the exact token balance."""
    return float(Decimal(str(max(0.0, size))).quantize(_SHARE_QUANTUM, rounding=ROUND_DOWN))


def load_state() -> dict:
    if not REWARD_STATE_FILE.exists():
        return {}
    try:
        return json.loads(REWARD_STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    tmp = REWARD_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(REWARD_STATE_FILE)


def _snapshot_filled(e: dict, cur: float) -> None:
    """On a RESTING→FILLED flip, seed the Stage-2 fields. `cost` = the fill price (the bid); `held_at_fill` = our
    holding now (grows UP to catch late partials until a sell rests); `last_mid` from the stored placement mid."""
    e["status"] = "FILLED"
    e["cost"] = e["price"]
    e["fair_at_fill"] = e.get("fair_yes")
    e["model_edge"] = (round(e["fair_at_fill"] - e["cost"], 4)
                       if e.get("fair_at_fill") is not None else e.get("model_edge", 0.0))
    e["buy_order_id"] = e.get("order_id")
    e["buy_cancel_pending"] = bool(e.get("order_id"))
    e["held_at_fill"] = min(cur, e.get("held_at_place", 0.0) + e["size"])         # cap at OUR order's max (exclude any co-hold)
    e["last_mid"] = e.get("mid", e["price"])
    e["sell_order_id"] = None
    e["sell_pending"] = False
    e["sell_price"] = None
    e["sell_mode"] = None
    e["sell_size"] = 0.0
    e["flatten_since"] = None


def _take_profit_price(e: dict, fair_now: float | None) -> float:
    """Fair-aware maker target: capture half the entry edge, bounded to 2–8¢, while leaving 2¢ model upside.
    Never deliberately quote below cost+2¢ while the forecast thesis remains valid."""
    cost = e["cost"]
    entry_fair = e.get("fair_at_fill", e.get("fair_yes"))
    edge = e.get("model_edge")
    if not isinstance(edge, (int, float)) or edge <= 0:
        edge = max(0.0, (entry_fair - cost) if isinstance(entry_fair, (int, float)) else 0.0)
    markup = min(max(edge * SELL_EDGE_CAPTURE, SELL_MARKUP_C), SELL_MAX_MARKUP_C)
    target = cost + markup
    if fair_now is not None and fair_now - FAIR_EXIT_BUFFER_C >= cost + SELL_MARKUP_C:
        target = min(target, fair_now - FAIR_EXIT_BUFFER_C)
    return round(target, 4)


def _refresh_model_context(entry: dict, observation: dict, *, update_entry_edge: bool) -> None:
    """Copy the current deterministic/LLM valuation into an existing order or holding state."""
    fair = observation.get("fair_yes")
    if fair is not None:
        entry["fair_yes"] = fair
        if update_entry_edge:
            entry["model_edge"] = round(fair - entry["price"], 4)
    for key in ("mid", "peak_utc", "forecast_regime", "calibration",
                "forecast_peak_adjusted", "llm_advice"):
        if key in observation:
            entry[key] = observation.get(key)


def plan_cycle(targets: list[FarmOrder], held: dict | None, state: dict,
               *, capital_cap: float, reprice_drift: float = REPRICE_DRIFT, books: dict | None = None,
               now: datetime | None = None, market_context: dict[str, dict] | None = None
               ) -> tuple[dict, list[FarmOrder], list[dict], list[dict], list[str]]:
    """PURE: decide the loop's moves from fresh `targets`, holdings `held` (token→info, or None), the live `books`
    (token→(best_bid,best_ask) for FILLED positions, or None → fall back to the stored mid), the clock `now` (for
    the 关门时间 cutoff; None → never force) and prior `state`. RESTING bid: filled (holding grew) → FILLED+snapshot
    Stage 2; not a target → EXIT; drifted > reprice_drift → REPRICE; else LEAVE. FILLED position (Stage 2): the
    posted sell fully filled (or holding back to baseline) → CLOSE; else rest/reprice a fair-aware TAKE_PROFIT
    maker. Falling out of top-N never invalidates a held position. Only a fresh model fair below cost−
    THESIS_BREAK_C starts a defensive maker FLATTEN. Within FORCE_FLATTEN_LEAD_MIN (6h) of peak_utc, escalate to
    a TAKER FAK at the bid. Partial-safe (never sell > held_at_fill-held_at_place) + fail-closed when positions=None.
    FLATTEN is sticky. `market_context` is the FULL raw scan, separate from `targets` (new-buy top-N)."""
    tgt = {o.token_yes: o for o in targets}
    if market_context is None:
        market_context = {o.token_yes: {"mid": o.mid, "fair_yes": o.fair_yes} for o in targets}
    new_state = {k: dict(v) for k, v in state.items()}
    to_cancel: list[dict] = []
    to_sell: list[dict] = []
    log: list[str] = []
    for token in list(new_state):
        e = new_state[token]
        cur = _held_size(held, token)
        if e["status"] != "FILLED":
            # ── RESTING buy management ────────────────────────────────────────────────
            observation = market_context.get(token)
            if observation is not None:
                _refresh_model_context(e, observation, update_entry_edge=True)
            if cur is not None and cur > e.get("held_at_place", 0.0) + 1e-9:      # our bid filled → hold + snapshot
                _snapshot_filled(e, cur)
                log.append(f"FILLED {e['city']} {e['bucket']}")
                continue
            o = tgt.get(token)
            if e.get("ambiguous") and o is not None:
                # ambiguous placeholder (NO order_id → its maybe-live bid can't be cancelled) AND still a target:
                # HOLD it — REPRICE would stack a second live bid (double-bet). The fill check above adopts it if it
                # fills. Once the bucket DROPS (o is None) it falls through to EXIT below → popped, so a never-landed
                # placeholder can't leak the cap forever (bounded to its new-entry lifetime). (verify-r4)
                continue
            if o is None:                                                        # dropped target → exit (reaps a stale/ambiguous placeholder too)
                to_cancel.append({"token": token, "order_id": e.get("order_id"), "replace": None})
                log.append(f"EXIT {e['city']} {e['bucket']} — no longer a target")
            elif abs(e["price"] - o.price) > reprice_drift + 1e-9:               # drifted → reprice
                to_cancel.append({"token": token, "order_id": e.get("order_id"), "replace": o})
                log.append(f"REPRICE {e['city']} {e['bucket']} {e['price']}→{o.price}")
            continue                                                             # else leave it resting

        # ── FILLED position management (Stage 2) ──────────────────────────────────────
        if "cost" not in e or "station_id" not in e:            # a pre-B3b FILLED entry (no Stage-2 fields) → can't manage
            log.append(f"STALE-FILLED {e.get('city')} {e.get('bucket')} — pre-B3b entry, sell it manually")
            continue
        if cur is None:                                                          # positions unqueryable → FAIL CLOSED
            continue
        base = e.get("held_at_place", 0.0)
        if e.get("buy_cancel_pending"):
            # A first partial fill does not prove the entire BUY filled. Cancel and confirm its remainder before
            # posting any SELL, otherwise late BUY fills race the sell and make the reward-minimum quantity unstable.
            to_cancel.append({"token": token, "order_id": e.get("buy_order_id") or e.get("order_id"),
                              "replace": None, "kind": "buy_after_fill"})
            log.append(f"BUY-REMAINDER-CANCEL {e['city']} {e['bucket']} — confirm size before sell")
            continue
        if not (e.get("sell_order_id") or e.get("sell_pending") or e.get("sell_mode") == "FORCE"):
            # catch late partials while no sell is committed; cap at our order's max. NOT after a FORCE taker —
            # held_at_fill was decremented by what it sold, and re-growing it off a co-hold-inflated `cur` would
            # re-open the oversell the FORCE just closed (verify-fix #1).
            e["held_at_fill"] = max(e.get("held_at_fill", e["size"]), min(cur, base + e["size"]))
        own_max = e.get("held_at_fill", e["size"]) - base                        # farmer-PRIVATE sell ceiling (never shared cur)
        posted = e.get("sell_size", 0.0)
        if cur <= base + 1e-9 or own_max <= 1e-9 or (posted > 0 and cur <= e.get("held_at_fill", e["size"]) - posted + 1e-9):
            if e.get("sell_order_id"):                                           # CLOSED: our delta is back to baseline (maker OR taker sold it out)
                to_cancel.append({"token": token, "order_id": e["sell_order_id"], "replace": None, "kind": "sell"})
            log.append(f"CLOSED {e['city']} {e['bucket']} (flat)")
            new_state.pop(token, None)
            continue
        o = tgt.get(token)
        observation = market_context.get(token)
        bb_ba = (books or {}).get(token)                                         # live book for the held token (each side may be None)
        best_bid, best_ask = bb_ba if bb_ba is not None else (None, None)
        floor_ask = round(best_bid + TICK, 4) if best_bid is not None else None   # a maker ask MUST sit above the bid (else it crosses → post_only reject)
        if best_bid is not None and best_ask is not None:
            e["last_mid"] = round((best_bid + best_ask) / 2.0, 4)                 # both sides → refresh mid from the LIVE book
        elif observation is not None and observation.get("mid") is not None:     # one-sided / no book → raw scan mid, independent of top-N
            e["last_mid"] = observation["mid"]
        mid = e.get("last_mid", e["cost"])
        fair_now = observation.get("fair_yes") if observation is not None else None
        if observation is not None:
            _refresh_model_context(e, observation, update_entry_edge=False)
        sell_size = _floor_sell_size(min(own_max, max(0.0, cur - base)))         # PARTIAL-safe, never round above the exact balance
        force = bool(now is not None and e.get("peak_utc")                       # 关门时间: within LEAD_MIN of the forecast peak
                     and now >= datetime.fromisoformat(e["peak_utc"]) - timedelta(minutes=FORCE_FLATTEN_LEAD_MIN))
        if force and best_bid is None:                                           # 关门时间 but NO live bid to sell into (no buyer / fetch failed)
            log.append(f"FORCE-NO-BID {e['city']} {e['bucket']} — no bid at 关门时间, can't flatten this cycle")
            continue                                                             # leave any resting maker; a doomed stale-mid maker would just reject. Retry when a bid returns
        thesis_broken = fair_now is not None and fair_now < e["cost"] - THESIS_BREAK_C - 1e-9
        flatten = thesis_broken or e.get("sell_mode") in ("FLATTEN", "FORCE")    # model invalidation; STICKY
        if force:                                                                # best_bid present → ESCALATE to a TAKER: sell INTO the bid (FAK crosses → guaranteed fill). One-sided book (bids, no ask) still forces
            mode, want, taker = "FORCE", best_bid, True
        elif flatten:
            mode, taker = "FLATTEN", False
            ratchet = (e.get("flatten_since") or 0) * TICK
            want = mid - ratchet
        else:
            mode, want, taker = "TAKE_PROFIT", _take_profit_price(e, fair_now), False
        if floor_ask is not None and not taker:                                  # a maker ask sits ABOVE the bid; the taker sits AT it (marketable)
            want = max(want, floor_ask)                                          # NEVER below the live best bid (would cross → reject)
        want = min(max(round(want, 4), PRICE_FLOOR), 0.99)                       # into the postable range
        reward_min = float(e.get("reward_min_size") or 0.0)
        if mode == "TAKE_PROFIT" and reward_min and sell_size < reward_min - 1e-9:
            log.append(f"SELL-SKIP-REWARD-MIN {e['city']} {e['bucket']} {sell_size:.2f}<{reward_min:.0f} → hold")
            continue
        if sell_size < MIN_ORDER_SHARES - 1e-9 or sell_size * want < MIN_ORDER_USD - 1e-9:
            log.append(f"SELL-SKIP {e['city']} {e['bucket']} {sell_size:.0f}×{want} < min → settlement")
            continue
        have_sell = bool(e.get("sell_order_id")) or e.get("sell_pending")        # a taker never rests → have_sell stays False → re-forced each cycle until flat
        current_mode = "TAKE_PROFIT" if e.get("sell_mode") == "SCALP" else e.get("sell_mode")  # migrate live legacy state
        needs_reprice = (current_mode != mode
                         or abs((e.get("sell_price") or 0.0) - want) > reprice_drift + 1e-9
                         or (e.get("sell_size") or 0.0) > sell_size + 1e-9)
        if not have_sell:
            to_sell.append({"token": token, "mode": mode, "price": want, "size": sell_size, "taker": taker})
        elif e.get("sell_pending"):
            # ambiguous maker (id-less → CANNOT be cancelled). NEVER fire a 关门时间 taker on top of it: the maker may
            # be live on the book, so taker + maker = two sells for the same shares (double-sell / naked short). Wait
            # for positions to resolve it (filled → CLOSE; the rare dead-ambiguous-maker position rides). (verify-r3)
            log.append(f"SELL-PENDING {e['city']} {e['bucket']} (ambiguous, await positions)")
        elif needs_reprice:                                                      # mode→FORCE cancels the resting maker, then re-sells as a taker
            to_cancel.append({"token": token, "order_id": e["sell_order_id"], "replace": None, "kind": "sell",
                              "resell": {"token": token, "mode": mode, "price": want, "size": sell_size, "taker": taker}})
            log.append(f"SELL-REPRICE {e['city']} {e['bucket']} {e.get('sell_price')}→{want} [{mode}]")
        else:
            e["flatten_since"] = (e.get("flatten_since") or 0) + 1 if mode == "FLATTEN" else None

    committed = sum(                                                             # cap bounds TOTAL exposure, not just resting bids
        e["capital"] if e["status"] == "RESTING"                                 # resting bid: size × price
        else e.get("cost", 0.0) * max(0.0, e.get("held_at_fill", e.get("size", 0.0)) - e.get("held_at_place", 0.0))
        for e in new_state.values())                                             # FILLED: cost × shares we still hold (frees up on CLOSE)
    to_place: list[FarmOrder] = []
    for o in targets:
        if o.token_yes in new_state:                                             # already resting / held / repricing
            continue
        if committed + o.capital > capital_cap + 1e-9:                           # over the cap → skip the next one
            continue
        to_place.append(o)
        committed += o.capital
        log.append(f"PLACE {o.city} {o.bucket} @{o.price}×{o.size:.0f} (${o.capital:.2f})")
    return new_state, to_place, to_cancel, to_sell, log


def _resting_entry(o: FarmOrder, now: datetime, held: dict | None, order_id: str | None, ambiguous: bool) -> dict:
    """A RESTING buy entry carrying the Stage-2 SEED fields (station_id/comparator/threshold/market_date/mid) so a
    SELL Decision can be rebuilt after the bucket drops from the scan. An `ambiguous` placeholder (order_id None)
    keeps a maybe-live order in state so the re-place loop + un-poison both skip it (no double-bet)."""
    return {"status": "RESTING", "order_id": order_id, "ambiguous": ambiguous, "placed_at": now.isoformat(),
            "price": o.price, "size": o.size, "capital": o.capital, "city": o.city, "bucket": o.bucket,
            "held_at_place": _held_size(held, o.token_yes) or 0.0,
            "station_id": o.station_id, "comparator": o.comparator, "threshold": o.threshold,
            "market_date": o.market_date, "mid": o.mid, "peak_utc": o.peak_utc,
            "fair_yes": o.fair_yes, "model_edge": o.model_edge,
            "reward_min_size": o.reward_min_size, "reward_max_spread_c": o.reward_max_spread_c,
            "forecast_peak_adjusted": o.forecast_peak_adjusted, "llm_advice": o.llm_advice,
            "forecast_regime": o.forecast_regime, "calibration": o.calibration}


def _execute(new_state: dict, to_place: list[FarmOrder], to_cancel: list[dict], to_sell: list[dict],
             held: dict | None, now: datetime, now_iso: str, log: list[str]) -> dict:
    """Apply plan_cycle's decisions via the executor (I/O). SAFE-CANCEL on both sides (confirm before drop/replace
    → no double-bet / double-sell). The BUY-fill re-check is GATED to RESTING (else a FILLED entry always trips
    it and blocks every sell cancel forever). Split out of run_cycle so this glue is testable."""
    for c in to_cancel:
        token, oid = c["token"], c["order_id"]
        ent = new_state.get(token)
        cur = _held_size(held, token)
        if c.get("kind") == "buy_after_fill":
            if ent is None:
                continue
            res = ex.cancel_order(oid, now_iso=now_iso, reason="buy-remainder-after-fill") if oid else None
            if oid and not (res and res.ok):
                log.append(f"BUY-REMAINDER-CANCEL-UNCONFIRMED {token}: {res.detail if res else '—'}")
                continue
            matched = ex.filled_size(oid) if oid else None
            base_ = ent.get("held_at_place", 0.0)
            if matched is not None:
                ent["held_at_fill"] = round(base_ + min(ent.get("size", matched), max(0.0, matched)), 2)
            else:
                fresh = _held_size(ex.positions_held(), token)
                final_cur = fresh if fresh is not None else cur
                if final_cur is not None:
                    ent["held_at_fill"] = min(max(base_, final_cur), base_ + ent.get("size", 0.0))
            ent.update(buy_cancel_pending=False, buy_order_id=None)
            log.append(f"BUY-REMAINDER-CANCELLED {token} — sell ceiling {ent.get('held_at_fill', 0.0):.2f}")
            continue
        if c.get("kind") == "sell":
            # ── SELL cancel (reprice / close cleanup) ──
            if ent is None:
                continue
            ceil_ = ent.get("held_at_fill", ent.get("size", 0.0))
            if cur is not None and cur <= ceil_ - ent.get("sell_size", 0.0) + 1e-9:   # sold in the window → CLOSE
                new_state.pop(token, None)
                log.append(f"CLOSED(pre-cancel) {token}")
                continue
            res = ex.cancel_order(oid, now_iso=now_iso, reason="sell-reprice") if oid else None
            if oid and not (res and res.ok):                                     # unconfirmed → keep the sell, no re-post
                log.append(f"SELL-CANCEL-UNCONFIRMED {token}: {res.detail if res else '—'}")
                continue
            if oid:                                                              # ORDER-LEVEL fill (co-hold-independent): shrink the private ceiling by what the
                sold = ex.filled_size(oid)                                       # maker actually sold, so a partial maker can't be re-sold for its full size (verify-r3 #3)
                if sold:
                    base_ = ent.get("held_at_place", 0.0)
                    ent["held_at_fill"] = round(max(base_, ent.get("held_at_fill", ent.get("size", 0.0)) - sold), 2)
            ent.update(sell_order_id=None, sell_pending=False, sell_price=None,
                       sell_mode=None, sell_size=0.0, flatten_since=None)
            # RESELL only on a CLEAN cancel (the order was OPEN → didn't fill → we still hold → safe). A GONE cancel
            # (matched/already-cancelled) means it MAY have filled; with a stale positions snapshot, reselling would
            # fire a CROSSING taker for shares we no longer hold (a naked short). Skip → re-flatten next cycle on
            # fresh positions (CLOSE if flat). (review 2026-06-18)
            if c.get("resell") and not (res and res.gone):
                to_sell.append(c["resell"])
            elif c.get("resell"):
                log.append(f"SELL-GONE-NO-RESELL {token} — maker terminal, re-confirm next cycle")
            continue
        # ── BUY cancel (reprice / exit) — gate the fill re-check to RESTING ──
        if ent and ent.get("status") == "RESTING" and cur is not None and cur > ent.get("held_at_place", 0.0) + 1e-9:
            _snapshot_filled(ent, cur)
            log.append(f"FILLED(pre-cancel) {token}")
            continue
        repl = c["replace"]
        res = ex.cancel_order(oid, now_iso=now_iso, reason=("reprice" if repl else "exit")) if oid else None
        if oid and not (res and res.ok):                                         # unconfirmed → keep, retry next cycle
            log.append(f"CANCEL-UNCONFIRMED {token}: {res.detail if res else '—'}")
            continue
        if res and res.gone and ent is not None:
            # GONE = the buy order is terminal but MAY have FILLED (matched); the once-per-cycle `held` snapshot is
            # provably stale for it (the pre-cancel fill check already missed it). Re-read positions: if we now hold
            # above our baseline, the bid FILLED → ADOPT it into Stage 2 (never orphan a held position, unmanaged,
            # into the directional window); else it was cancelled → drop it (verify-fix #3).
            fresh = _held_size(ex.positions_held(), token)
            if fresh is not None and fresh > ent.get("held_at_place", 0.0) + 1e-9:
                _snapshot_filled(ent, fresh)
                log.append(f"FILLED(gone-recheck) {token}")
                continue
        new_state.pop(token, None)                                               # clean cancel / cancelled-gone / DRY / no live id
        # REPLACE only on a CLEAN cancel. A GONE buy-cancel may have FILLED → don't add a fresh bid on top of a
        # possibly-held position (over-exposure); skip the replace and let the next cycle re-bid on fresh positions.
        if repl is not None and not (res and res.gone):
            to_place.append(repl)
    for o, res in place_orders(to_place, now_iso=now_iso, ttl_min=None):         # new BUY bids, GTC
        if res.ok:
            new_state[o.token_yes] = _resting_entry(o, now, held, res.order_id, False)
            log.append(f"PLACED {o.city} {o.bucket} @{o.price}×{o.size:.0f} (${o.capital:.2f})")
        elif res.ambiguous:                                                      # maybe live → blocking placeholder
            new_state[o.token_yes] = _resting_entry(o, now, held, None, True)
            log.append(f"PLACE-AMBIGUOUS {o.city} {o.bucket} (maybe live → blocks re-place)")
        else:
            log.append(f"PLACE-FAIL {o.city} {o.bucket}: {res.detail}")
    _place_sells(new_state, to_sell, held, now_iso, log)                         # new SELL asks
    return new_state


def _place_sells(new_state: dict, to_sell: list[dict], held: dict | None, now_iso: str, log: list[str]) -> None:
    """Place each Stage-2 SELL (GTC maker ask), size clamped to the farmer-private closeable delta (never
    oversell). Skip if a sell is already resting/pending (no double-post) or below the exchange min (→ rides to
    settlement). Ambiguous → sell_pending blocks any re-post until positions resolve it."""
    for s in to_sell:
        token = s["token"]
        ent = new_state.get(token)
        if ent is None or ent.get("sell_order_id") or ent.get("sell_pending"):   # gone / already have a sell
            continue
        cur = _held_size(held, token)
        if cur is None:                                                          # positions unqueryable → fail closed
            continue
        base = ent.get("held_at_place", 0.0)
        own_max = ent.get("held_at_fill", ent.get("size", 0.0)) - base
        size = _floor_sell_size(min(s["size"], own_max, max(0.0, cur - base)))
        price, taker = s["price"], s.get("taker", False)
        reward_min = float(ent.get("reward_min_size") or 0.0)
        if s.get("mode") == "TAKE_PROFIT" and reward_min and size < reward_min - 1e-9:
            log.append(f"SELL-SKIP-REWARD-MIN {ent['city']} {ent['bucket']} {size:.2f}<{reward_min:.0f} → hold")
            continue
        if size < MIN_ORDER_SHARES - 1e-9 or size * price < MIN_ORDER_USD - 1e-9:
            log.append(f"SELL-SKIP {ent['city']} {ent['bucket']} {size:.0f}×{price} < min → settlement")
            continue
        res = ex.place_from_decision(_sell_decision(ent, token=token, price=price, size=size, taker=taker),
                                     now_iso=now_iso, ttl_min=None)
        if not res.ok:
            if taker:                                                            # FAK failed/ambiguous → retry next cycle (size re-clamps to held → never double-sells)
                log.append(f"FORCE-SELL-RETRY {ent['city']} {ent['bucket']}: {res.detail}")
            elif res.ambiguous:                                                  # maker maybe live → block re-post
                ent.update(sell_pending=True, sell_order_id=None, sell_price=price, sell_mode=s["mode"], sell_size=size)
                log.append(f"SELL-AMBIGUOUS {ent['city']} {ent['bucket']} (maybe live → blocks)")
            else:
                log.append(f"SELL-FAIL {ent['city']} {ent['bucket']}: {res.detail}")
            continue
        if taker:                                                                # a FAK doesn't rest → leave no sell_order_id
            # CONSERVATIVELY assume the FAK sold its full `size` (the response carries no fill qty): shrink the
            # private ceiling so own_max→base → CLOSE next cycle, NEVER a second FORCE clamped to a co-hold-inflated
            # `cur` (the partial-fill naked short, verify-fix #1). A rare partial/killed FAK leaves a residual that
            # rides to settlement — the accepted maker-flatten residual risk, never an oversell.
            ent["held_at_fill"] = round(max(base, ent.get("held_at_fill", size) - size), 2)
            ent.update(sell_order_id=None, sell_pending=False, sell_price=price, sell_mode="FORCE", sell_size=size, flatten_since=None)
            log.append(f"FORCE-SELL {ent['city']} {ent['bucket']} (taker) @{price}×{size:.0f} → ceiling {ent['held_at_fill']:.0f}")
        else:
            ent.update(sell_order_id=res.order_id, sell_pending=False, sell_price=price,
                       sell_mode=s["mode"], sell_size=size, flatten_since=(0 if s["mode"] == "FLATTEN" else None))
            log.append(f"SELL {ent['city']} {ent['bucket']} {s['mode']} @{price}×{size:.0f}")


async def run_cycle(client, stations, now: datetime, *, capital_cap: float, max_targets: int,
                    reprice_drift: float = REPRICE_DRIFT) -> tuple[dict, list[str]]:
    """One farm cycle (I/O): poll holdings → scan+select → cancel/reprice/place → persist. DRY-AWARE via the
    executor. GTC bids (no expiry); the band is tracked by CANCEL→confirm→REPLACE (fail-safe: an unconfirmed
    cancel leaves the order be → no double-bet). Reuses positions_held (fill truth); NEVER touches the
    directional daemon's state. Returns (state, log)."""
    state = load_state()
    if not ex._dry_run():    # un-poison DRY leftovers, but KEEP live bids (order_id), FILLED positions (id may be
        state = {k: v for k, v in state.items()   # None after an ambiguous buy fills), and ambiguous placeholders
                 if v.get("order_id") or v.get("status") == "FILLED" or v.get("ambiguous")}
    held = ex.positions_held()
    books = {} if ex._dry_run() else {                                           # live (best_bid,best_ask) per FILLED token (None only on fetch failure; one-sided book kept)
        tok: bba for tok, v in state.items()
        if v.get("status") == "FILLED" and (bba := ex.book_bid_ask(tok)) is not None}
    context_tokens = {token for token, entry in state.items() if entry.get("status") == "FILLED"}
    scanned = await scan(client, stations, now, context_tokens=context_tokens)
    advice_cache, advice_log = await hourly_advice(client, scanned, now)
    scanned = apply_advice(scanned, advice_cache, now)
    targets = select(scanned, capital_cap=capital_cap, max_targets=max_targets)
    market_context = {t["token_yes"]: t for t in scanned}                       # valuation for HELD positions; not top-N filtered
    new_state, to_place, to_cancel, to_sell, log = plan_cycle(targets, held, state, capital_cap=capital_cap,
                                                              reprice_drift=reprice_drift, books=books, now=now,
                                                              market_context=market_context)
    if advice_log:
        log.insert(0, advice_log)
    try:
        new_state = _execute(new_state, to_place, to_cancel, to_sell, held, now, now.strftime("%Y-%m-%dT%H:%MZ"), log)
    finally:
        save_state(new_state)    # persist even on interrupt/crash mid-_execute (_execute mutates new_state in
        #                          place as it places) → a live order is NEVER orphaned (untracked) by a Ctrl-C
    return new_state, log
