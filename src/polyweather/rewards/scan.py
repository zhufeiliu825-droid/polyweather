"""Reward SCANNER (read-only) — surfaces temperature buckets that pay liquidity rewards and ranks
edge-aligned farming targets: high `rewardsDailyRate` × our forecast lean × LOW competition.

The reward pool per bucket is Gamma's `clobRewards[].rewardsDailyRate` (USDC/day). It's SHARED by
liquidity share, so the resting size within ±max_spread of mid (the "competition") is what we'd split it
with. We only care about buckets in cities we forecast (edge). NO ORDERS — writes data/reward_targets.jsonl.
Reuses `discovery._bucket_from_market` (correct YES-token mapping) + `fetch_minutely_15` (forecast peak)."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from ..discovery import _bucket_from_market
from ..forecast.open_meteo import fetch_minutely_15_with_hourly_met
from ..forecast.regime import classify_regime
from ..realized.feature_store import DATA_DIR
from ..realized.residuals import bucket_fair_yes, fair_calibration

GAMMA = "https://gamma-api.polymarket.com/events"
CLOB = "https://clob.polymarket.com"
TEMP_TAG = 104596
MIN_RATE = 20.0                       # only rank buckets paying ≥ this USDC/day (skip dust)
TARGETS_LOG = DATA_DIR / "reward_targets.jsonl"
_CITY_RE = re.compile(r"temperature in (.+?) on ", re.IGNORECASE)


def _norm_city(s: str) -> str:
    return s.strip().lower().replace(" ", "")


def _is_future_event(market_date: str, now: datetime, tz: str) -> bool:
    """True iff this event's date is AFTER the city's current LOCAL date. The farmer ONLY touches
    tomorrow-and-later events — kept fully separate from the directional bot's same-day (today) market."""
    try:
        return date.fromisoformat(market_date) > now.astimezone(ZoneInfo(tz)).date()
    except Exception:
        return False


def _is_current_or_future_event(market_date: str, now: datetime, tz: str) -> bool:
    """True for today's or a later local-date event. Today is valuation context only; `_is_future_event`
    remains the new-entry rule."""
    try:
        return date.fromisoformat(market_date) >= now.astimezone(ZoneInfo(tz)).date()
    except Exception:
        return False


@dataclass
class RewardBucket:
    city: str                         # normalized (matches station.city normalized)
    market_date: str                  # YYYY-MM-DD (event endDate)
    comparator: str                   # "<=" | "==" | ">="
    threshold: int
    token_yes: str
    daily_rate: float                 # USDC/day reward pool for THIS bucket
    min_size: float                   # rewardsMinSize (shares)
    max_spread: float                 # rewardsMaxSpread (cents)
    devig_sum: float | None = None    # Σ of ALL the event's bucket Yes-prices (~1 real; ≫1 = empty/fake book)


def reward_buckets(event: dict, *, include_tokens: set[str] | None = None) -> list[RewardBucket]:
    """The reward-bearing buckets of one temp event (clobRewards rate > 0). [] if none. Each carries the
    event's `devig_sum` (Σ of ALL buckets' Yes-prices, ~1 in a real book) for the farmer's health gate — a
    sum far off 1 (an empty placeholder book sitting at ~0.5 each → ~5.5) flags 'no liquidity / fake price'."""
    cm = _CITY_RE.search(event.get("title", "") or "")
    city = _norm_city(cm.group(1)) if cm else ""
    mkt_date = (event.get("endDate") or "")[:10]
    parsed = [(m, _bucket_from_market(m)) for m in (event.get("markets") or [])]
    yes_px = [b.price_yes for _m, b in parsed if b is not None and b.price_yes is not None]
    devig = round(sum(yes_px), 3) if yes_px else None
    include_tokens = include_tokens or set()
    out: list[RewardBucket] = []
    for m, b in parsed:
        rate = max((float(r.get("rewardsDailyRate") or 0) for r in (m.get("clobRewards") or [])), default=0.0)
        if b is None or (rate <= 0 and b.token_yes not in include_tokens):
            continue
        out.append(RewardBucket(city, mkt_date, b.comparator.value, b.threshold, b.token_yes, rate,
                                float(m.get("rewardsMinSize") or 0), float(m.get("rewardsMaxSpread") or 0),
                                devig_sum=devig))
    return out


def our_yes_lean(comparator: str, threshold: int, peak: float | None) -> float | None:
    """Rough [0,1] lean that this bucket resolves YES given our forecast peak (None → no forecast). A
    targeting SIGNAL for ranking only — the farmer/agent still makes the actual bet call (no hard formula)."""
    if peak is None:
        return None
    if comparator == "==":
        return max(0.0, 1.0 - abs(peak - threshold))          # 1 at peak==N, → 0 by ±1°
    if comparator == ">=":
        return 1.0 if peak >= threshold + 0.5 else (0.5 if peak >= threshold - 0.5 else 0.1)
    return 1.0 if peak <= threshold - 0.5 else (0.5 if peak <= threshold + 0.5 else 0.1)   # "<="


def book_mid_competition(book: dict, max_spread_c: float) -> tuple[float | None, float, float | None]:
    """(mid, resting shares within ±max_spread of mid, bid-ask spread). mid/spread None if the book is
    one-sided/empty. `competition` = the reward liquidity we'd split the pool with; `spread` feeds the health
    gate (a wide spread = placeholder/illiquid book whose mid is a fake price)."""
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return None, 0.0, None
    best_bid = max(float(b["price"]) for b in bids)
    best_ask = min(float(a["price"]) for a in asks)
    mid = (best_bid + best_ask) / 2.0
    band = (max_spread_c or 4.5) / 100.0
    comp = (sum(float(b["size"]) for b in bids if abs(float(b["price"]) - mid) <= band)
            + sum(float(a["size"]) for a in asks if abs(float(a["price"]) - mid) <= band))
    return mid, comp, round(best_ask - best_bid, 4)


def score(rate: float, lean: float | None, competition: float, min_size: float) -> float:
    """Rank key: high reward × we-lean-YES ÷ competition. 0 when we have no forecast lean."""
    return (rate * (lean or 0.0)) / (competition + (min_size or 1.0))


async def _fetch_events(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(GAMMA, params={"tag_id": TEMP_TAG, "closed": "false", "limit": 300})
    r.raise_for_status()
    evs = r.json()
    evs = evs if isinstance(evs, list) else evs.get("data", [])
    return [e for e in evs if "temperature" in (e.get("title") or "").lower()]


async def _fetch_book(client: httpx.AsyncClient, token: str) -> dict:
    try:
        r = await client.get(f"{CLOB}/book", params={"token_id": token})
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


async def _peak_for_date(client: httpx.AsyncClient, stn, target: date
                         ) -> tuple[float | None, datetime | None, dict | None]:
    """Peak value/time and the Open-Meteo weather regime at the nearest hourly peak point."""
    curve, met = await fetch_minutely_15_with_hourly_met(
        client, stn.latitude, stn.longitude, stn.timezone, forecast_days=3)
    day = [(dt, tp) for dt, tp in curve if dt.date() == target]
    if not day:
        return None, None, None
    pdt, pval = max(day, key=lambda x: x[1])
    met_day = [row for row in met if row["t"].date() == target]
    nearest = min(met_day, key=lambda row: abs((row["t"] - pdt).total_seconds())) if met_day else None
    regime = classify_regime(nearest)
    return pval, pdt.replace(tzinfo=ZoneInfo(stn.timezone)).astimezone(timezone.utc), regime


async def scan(client: httpx.AsyncClient, stations, now: datetime, *, min_rate: float = MIN_RATE,
               context_tokens: set[str] | None = None) -> list[dict]:
    """Rank edge-aligned reward targets. Only buckets in cities we forecast AND paying ≥ min_rate are
    costed (book + forecast fetched concurrently). `context_tokens` are also returned for held-position
    valuation even on the event day or after their reward rate falls. Returns best-first."""
    by_city = {_norm_city(s.city): s for s in stations}
    events = await _fetch_events(client)
    context_tokens = context_tokens or set()
    cands = [rb for ev in events for rb in reward_buckets(ev, include_tokens=context_tokens)
             if rb.city in by_city and (
                 (rb.daily_rate >= min_rate
                  and _is_future_event(rb.market_date, now, by_city[rb.city].timezone))
                 or (rb.token_yes in context_tokens
                     and _is_current_or_future_event(rb.market_date, now, by_city[rb.city].timezone)))]

    keys = {(rb.city, rb.market_date) for rb in cands}
    async def _peak(city, d):
        try:
            return (city, d), await _peak_for_date(client, by_city[city], date.fromisoformat(d))
        except Exception:
            return (city, d), (None, None, None)
    peaks = dict(await asyncio.gather(*[_peak(c, d) for c, d in keys]))
    books = await asyncio.gather(*[_fetch_book(client, rb.token_yes) for rb in cands])

    fair_cache: dict[tuple[str, str | None, int | None], dict | None] = {}
    targets = []
    for rb, book in zip(cands, books):
        pval, putc, regime = peaks.get((rb.city, rb.market_date), (None, None, None))
        h2p = round((putc - now).total_seconds() / 3600.0, 1) if putc is not None else None
        # Farmer bets tomorrow+: map hours-to-peak to the fixed lead whose honest σ we measured (day1=24h,
        # day2=48h). Same-day held positions (h2p<12h or passed) keep the short-lead near-analysis σ.
        lead_days = (1 if 12.0 <= h2p < 36.0 else 2 if h2p >= 36.0 else None) if h2p is not None else None
        mid, comp, spread = book_mid_competition(book, rb.max_spread)
        lean = our_yes_lean(rb.comparator, rb.threshold, pval)
        sid = by_city[rb.city].station_id
        regime_name = (regime or {}).get("primary")
        cache_key = (sid, regime_name, lead_days)
        if cache_key not in fair_cache:
            fair_cache[cache_key] = fair_calibration(sid, regime=regime_name, lead_days=lead_days)
        calibration = fair_cache[cache_key]
        fair = (bucket_fair_yes(rb.comparator, rb.threshold,
                                pval - calibration["bias"], calibration["sigma"])
                if (pval is not None and calibration is not None) else None)
        targets.append({
            "city": rb.city, "station_id": by_city[rb.city].station_id,
            "entry_eligible": (rb.daily_rate >= min_rate
                               and _is_future_event(rb.market_date, now, by_city[rb.city].timezone)),
            "comparator": rb.comparator, "threshold": rb.threshold,
            "market_date": rb.market_date, "bucket": f"{rb.comparator}{rb.threshold}C",
            "daily_rate": rb.daily_rate, "min_size": rb.min_size, "max_spread": rb.max_spread,
            "mid": (round(mid, 3) if mid is not None else None), "competition": round(comp),
            "spread": spread, "devig_sum": rb.devig_sum,
            "forecast_peak": (round(pval, 2) if pval is not None else None), "hours_to_peak": h2p,
            "peak_utc": (putc.isoformat() if putc is not None else None),   # 关门时间 anchor: taker force-flatten at peak−CUTOFF
            "forecast_regime": regime,
            "calibration": calibration,
            "fair_yes": (round(fair, 3) if fair is not None else None),     # calibrated P(YES) — select() drops buckets where the bid is rich vs this
            "our_yes_lean": (round(lean, 2) if lean is not None else None),
            "token_yes": rb.token_yes, "score": round(score(rb.daily_rate, lean, comp, rb.min_size), 4),
        })
    targets.sort(key=lambda t: -t["score"])
    return targets


def log_targets(targets: list[dict], now_iso: str) -> None:
    with TARGETS_LOG.open("a") as f:
        for t in targets:
            f.write(json.dumps({"ts": now_iso, **t}, ensure_ascii=False) + "\n")
