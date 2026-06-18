"""End-to-end backtest against an already-resolved day.

Validates station + unit + timezone + local-day windowing jointly: the bucket
containing our WU-computed local-day max must equal the bucket Polymarket
actually resolved YES. Also replays the day chronologically and asserts the
eventual winner is never marked dead.

    uv run python scripts/backtest.py --city london              # yesterday
    uv run python scripts/backtest.py --city london --date 2026-06-12
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from polyweather.config import CityConfig, load_config
from polyweather.discovery import build_event_state, parse_bucket_question
from polyweather.models import Bucket
from polyweather.odds.gamma_feed import GAMMA
from polyweather.realized.daily_max import compute_running_max
from polyweather.realized.wu_client import fetch_observations
from polyweather.util.pmjson import parse_prices


def event_slug(city: CityConfig, d: date) -> str:
    month = d.strftime("%B").lower()
    return f"highest-temperature-in-{city.name}-on-{month}-{d.day}-{d.year}"


def winner_bucket(raw: dict) -> tuple[str, Bucket | None]:
    """Find the market Polymarket resolved YES (outcomePrices == [1, 0])."""
    for m in raw.get("markets", []):
        pr = parse_prices(m.get("outcomePrices"))
        if pr and abs(pr[0] - 1.0) < 1e-9:
            q = m.get("question", "")
            comp, thr, unit = parse_bucket_question(q)
            return q, Bucket(
                question=q, comparator=comp, threshold=thr, unit=unit,
                token_yes="", token_no="", condition_id="",
            )
    return "", None


async def fetch_event_by_slug(client: httpx.AsyncClient, slug: str) -> dict | None:
    r = await client.get(f"{GAMMA}/events", params={"slug": slug})
    r.raise_for_status()
    arr = r.json()
    return arr[0] if arr else None


async def backtest(city_name: str, target: date) -> bool:
    cfg = load_config()
    city = cfg.cities[city_name]
    slug = event_slug(city, target)
    print(f"== backtest {city_name} {target} ==\n  slug: {slug}")

    async with httpx.AsyncClient(
        timeout=20.0, headers={"User-Agent": "polyweather-backtest/0.1"}
    ) as client:
        raw = await fetch_event_by_slug(client, slug)
        if raw is None:
            print("  FAIL: event not found")
            return False
        if not raw.get("closed"):
            print("  WARN: event not closed yet — winner may be provisional")

        win_q, win = winner_bucket(raw)
        state = build_event_state(raw, city)

        obs = await fetch_observations(
            client, city.location_key, cfg.defaults.wu_api_key,
            target.strftime("%Y%m%d"), city.unit,
        )
        mx, n, last = compute_running_max(obs, city.timezone, target)
        print(f"  WU obs in local day: {n}   computed max: {mx}°{city.unit}")
        print(f"  Polymarket winner:   {win_q or '(none)'}")

        if mx is None or win is None:
            print("  FAIL: missing WU max or winner")
            return False

        # 1) bucket containing our computed max == Polymarket's winning bucket
        our = next((b for b in state.buckets if b.contains(mx)), None)
        match = our is not None and (our.comparator, our.threshold) == (win.comparator, win.threshold)
        print(f"  our max bucket:      {our.comparator.value}{our.threshold}°{city.unit}"
              if our else "  our max bucket:      (none)")
        print(f"  -> resolution match: {'PASS' if match else 'FAIL'}")

        # 2) replay: winner must never be marked dead at any interim prefix max
        day = sorted(
            (o for o in obs if compute_running_max([o], city.timezone, target)[0] is not None),
            key=lambda o: o.valid_time_gmt,
        )
        running = None
        never_dead = True
        for o in day:
            running = o.temp if running is None else max(running, o.temp)
            state.running_max = running
            if win and state.is_dead(win):
                never_dead = False
                print(f"  FAIL replay: winner marked dead at prefix max {running}")
                break
        print(f"  -> winner-never-dead: {'PASS' if never_dead else 'FAIL'}")

        ok = match and never_dead
        print(f"  RESULT: {'PASS ✅' if ok else 'FAIL ❌'}")
        return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="london")
    ap.add_argument("--date", help="YYYY-MM-DD (default: city-local yesterday)")
    args = ap.parse_args()
    cfg = load_config()
    tz = cfg.cities[args.city].timezone
    if args.date:
        target = date.fromisoformat(args.date)
    else:
        target = (datetime.now(ZoneInfo(tz)) - timedelta(days=1)).date()
    ok = asyncio.run(backtest(args.city, target))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
