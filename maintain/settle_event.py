"""Settlement capture — the 复盘 ground truth, saved locally for offline analysis.

For each recently-RESOLVED Gamma event of a verified city: record the winning bucket, the
AWC actual daily-max (cross-check), and — by reconciling confirmed directional fills against
orders.jsonl / decisions.jsonl — whether WE filled, on which bucket, did we win, and the real P&L. The runner just
produces data/settle.jsonl on a schedule; Claude + the user analyse it later to improve the model.

    uv run python maintain/settle_event.py [--days 3]

Idempotent: skips (city, market_date) already recorded.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import date, datetime, timedelta, timezone

import httpx

from polyweather.discovery import _bucket_from_market
from polyweather.realized.feature_store import DATA_DIR
from polyweather.runtime.analyze import event_date, running_max_today
from polyweather.stations import load_stations

GAMMA = "https://gamma-api.polymarket.com/events"
HDR = {"User-Agent": "Mozilla/5.0 polyweather/0.1"}
UTC = timezone.utc
SETTLE = DATA_DIR / "settle.jsonl"
ORDERS = DATA_DIR / "orders.jsonl"
DECISIONS = DATA_DIR / "decisions.jsonl"
FILLS = DATA_DIR / "fills.jsonl"
_SLUG_DATE = re.compile(r"-on-([a-z]+)-(\d{1,2})-(\d{4})$")
_MON3 = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _read_jsonl(p) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def evt_date(ev: dict):
    """Resolution date — from the slug (`...-on-june-14-2026`, reliable) then the description
    regex as fallback (the slug form is the stable one across event vintages)."""
    mm = _SLUG_DATE.search(ev.get("slug", "") or "")
    if mm:
        mon, day, yr = mm.groups()
        mi = _MON3.get(mon[:3])
        if mi:
            try:
                return date(int(yr), mi, int(day))
            except ValueError:
                pass
    return event_date(ev)


def pnl_if_filled(won: bool, shares: float, price: float) -> float:
    """Per-share P&L of a maker BUY YES assumed filled at `price` (orders are sized in SHARES):
    win → +shares×(1−price) (each YES share pays $1, the cost was `price`), loss → −shares×price
    (lose the cost). e.g. 10sh @0.83 lost = −8.30; the same won = +1.70."""
    return round(shares * (1 - price), 2) if won else round(-shares * price, 2)


def confirmed_directional_fill(orders: list[dict], fills: list[dict], station: str,
                               city: str, market_date: str) -> tuple[dict | None, dict | None]:
    """Return the confirmed main-bot fill and its submitted GTD BUY order.

    A successful LIVE submission is not a fill. Filtering both the fill strategy and the order contract keeps
    expired directional GTDs and reward-farmer GTC/SELL orders out of realized performance.
    """
    fill = next((f for f in reversed(fills)
                 if f.get("strategy") == "directional" and f.get("station") == station
                 and f.get("market_date") == market_date and f.get("status") in ("FILLED", "PARTIAL")), None)
    if fill is None:
        return None, None
    order = next((o for o in reversed(orders)
                  if o.get("order_id") == fill.get("order_id") and o.get("station") == station
                  and o.get("city") == city and o.get("market_date") == market_date
                  and o.get("status") == "LIVE" and o.get("side") == "BUY"
                  and o.get("type") == "GTD-maker"), None)
    return fill, order


def resolved_bucket(ev: dict):
    """Winning bucket of a CLOSED event = the one whose YES price RESOLVED to ~1.
    Uses price_yes ONLY (last_trade is stale during the resolution-lag window). Requires
    EXACTLY ONE bucket ≥0.9 AND all markets parseable — else (ambiguous / not-yet-resolved /
    a parse gap that might hide the true winner) → None, so we skip and retry next run."""
    markets = ev.get("markets", [])
    parsed = [b for b in (_bucket_from_market(m) for m in markets) if b is not None]
    winners = [(f"{b.comparator.value}{b.threshold}", round(b.price_yes, 3))
               for b in parsed if b.price_yes is not None and b.price_yes >= 0.9]
    if len(parsed) < len(markets) or len(winners) != 1:
        return None, (winners[0][1] if len(winners) == 1 else None)
    return winners[0]


async def settle_city(client, stn, days: int, already: set) -> list[dict]:
    orders, decisions, fills = _read_jsonl(ORDERS), _read_jsonl(DECISIONS), _read_jsonl(FILLS)
    today = datetime.now(UTC).date()
    r = await client.get(GAMMA, params={"series_id": stn.pm_series_id, "closed": "true",
                                        "limit": 15, "order": "endDate", "ascending": "false"})
    out = []
    for ev in r.json():
        d = evt_date(ev)
        if not d or d >= today or d < today - timedelta(days=days):
            continue
        if (stn.city, d.isoformat()) in already:
            continue
        rb, rp = resolved_bucket(ev)
        if rb is None:
            continue
        amax, _, _ = await running_max_today(client, stn.station_id, stn.timezone, d,
                                             hours=24 * (days + 2))   # wide enough to cover a past day
        fill, order = confirmed_directional_fill(orders, fills, stn.station_id, stn.city, d.isoformat())
        dec = next((x for x in reversed(decisions) if x.get("station") == stn.station_id
                    and x.get("mkt_day") == d.isoformat() and x.get("action")), None)
        bucket = (fill.get("bucket") if fill else None) or (order.get("bucket") if order else None)
        filled_shares = fill.get("filled_shares") if fill else None
        row = {"city": stn.city, "station": stn.station_id, "market_date": d.isoformat(),
               "resolved_bucket": rb, "resolved_yes_price": round(rp, 3), "actual_max_awc": amax,
               "bet": bool(fill), "bet_bucket": bucket,
               "bet_price": (order.get("price") if order else None),
               "bet_status": (fill.get("status") if fill else None),
               "fill_order_id": (fill.get("order_id") if fill else None),
               "filled_shares": filled_shares,
               "our_p": (dec.get("our_p") if dec else None),
               "market_at_bet": (dec.get("market") if dec else None)}
        if fill:
            bb = (bucket or "").removesuffix("C")    # "==23C" -> "==23" (match resolved label)
            won = bb == rb
            row["won"] = won
            if order is not None and filled_shares is not None:
                row["pnl"] = pnl_if_filled(won, float(filled_shares), float(order.get("price") or 0.0))
                row["fill_note"] = "confirmed directional fill; actual matched shares at submitted maker price"
            else:
                row["pnl"] = None
                row["fill_note"] = "fill confirmed, but matched shares or submitted order price is unavailable"
        out.append(row)
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="look back this many resolved days")
    a = ap.parse_args()
    already = {(r["city"], r["market_date"]) for r in _read_jsonl(SETTLE)}
    stns = [s for s in load_stations().values() if s.verified]
    async with httpx.AsyncClient(timeout=40, headers=HDR) as client:
        results = await asyncio.gather(*[settle_city(client, s, a.days, already) for s in stns],
                                       return_exceptions=True)
    n, bet_n = 0, 0
    with SETTLE.open("a") as f:
        for res in results:
            if isinstance(res, Exception):
                continue
            for row in res:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                n += 1
                bet_n += int(row["bet"])
    print(f"settled {n} new (city,date), of which {bet_n} we bet → {SETTLE.name}")


if __name__ == "__main__":
    asyncio.run(main())
