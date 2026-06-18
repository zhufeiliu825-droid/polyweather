"""Independent odds logger — forward-captures Polymarket temperature-market odds.

Gamma keeps NO history, so this is the ONLY way to accumulate the market-relative data
needed to ever validate the edge: our_P-vs-market, fill-rate (does maker−1¢ actually fill),
de-vig/overround dynamics. Read-only public Gamma, NO creds, NO orders. **Start it NOW** —
the gap only fills going forward; nothing recovers the odds you didn't log.

    uv run python maintain/odds_logger.py --once           # one snapshot, exit
    uv run python maintain/odds_logger.py --interval 30     # snapshot every 30 min, 24/7

One JSON line per live temperature event per snapshot → data/odds_log.jsonl.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone

import httpx

from polyweather.discovery import _bucket_from_market
from polyweather.realized.feature_store import DATA_DIR
from polyweather.runtime.analyze import event_date

GAMMA = "https://gamma-api.polymarket.com/events"
HDR = {"User-Agent": "Mozilla/5.0 polyweather/0.1"}
TAG = 104596                       # "Highest temperature" markets
LOG = DATA_DIR / "odds_log.jsonl"


async def fetch_temp_events(client: httpx.AsyncClient) -> list[dict]:
    out, offset = [], 0
    while True:
        r = await client.get(GAMMA, params={"tag_id": TAG, "closed": "false", "limit": 100, "offset": offset})
        page = r.json()
        if not isinstance(page, list) or not page:
            break
        out += page
        if len(page) < 100:
            break
        offset += 100
    return out


def snapshot(events: list[dict], ts: str) -> list[dict]:
    rows = []
    for ev in events:
        buckets = [b for b in (_bucket_from_market(m) for m in ev.get("markets", [])) if b]
        if not buckets:
            continue
        bs, devig = [], 0.0
        for b in buckets:
            mid = b.implied_p
            if mid is not None:
                devig += mid
            bs.append({"b": f"{b.comparator.value}{b.threshold}", "bid": b.best_bid, "ask": b.best_ask,
                       "mid": (round(mid, 4) if mid is not None else None),
                       "last": b.last_trade, "vol": b.volume})
        d = event_date(ev)
        rows.append({"ts": ts, "event_id": str(ev.get("id", "")), "slug": ev.get("slug", ""),
                     "market_date": (d.isoformat() if d else None),
                     "devig_sum": round(devig, 4), "buckets": bs})
    return rows


async def one_snapshot(client: httpx.AsyncClient) -> int:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = snapshot(await fetch_temp_events(client), ts)
    with LOG.open("a") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one snapshot then exit")
    ap.add_argument("--interval", type=int, default=30, help="minutes between snapshots")
    a = ap.parse_args()
    async with httpx.AsyncClient(timeout=40, headers=HDR) as client:
        while True:
            try:
                n = await one_snapshot(client)
                print(f"[{datetime.now(timezone.utc):%H:%MZ}] logged {n} events → {LOG.name}", flush=True)
            except Exception as e:
                print(f"odds snapshot error (continuing): {e!r}", flush=True)
            if a.once:
                break
            await asyncio.sleep(a.interval * 60)


if __name__ == "__main__":
    asyncio.run(main())
