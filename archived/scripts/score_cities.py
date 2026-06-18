"""Rank Polymarket daily-temperature cities by liquidity, split by resolution
unit (°C vs °F). Used to pick the top-N non-Fahrenheit cities to monitor.

    uv run python scripts/score_cities.py --top 20
"""
from __future__ import annotations

import argparse
import asyncio
import re

import httpx

GAMMA = "https://gamma-api.polymarket.com"
TEMP_TAG = 104596


def _unit_of(event: dict) -> str | None:
    for m in event.get("markets", []):
        q = m.get("question", "")
        if "°C" in q or re.search(r"\d\s*C\b", q):
            return "C"
        if "°F" in q or re.search(r"\d\s*F\b", q):
            return "F"
    return None


def _city(event: dict, ticker: str) -> str:
    m = re.search(r"\bin (.+?) on\b", event.get("title", ""))
    if m:
        return m.group(1)
    return ticker.replace("-daily-weather", "").replace("-", " ").title()


async def fetch_all(client: httpx.AsyncClient) -> list[dict]:
    out, offset = [], 0
    while True:
        r = await client.get(f"{GAMMA}/events", params={
            "tag_id": TEMP_TAG, "closed": "false", "limit": 100, "offset": offset})
        r.raise_for_status()
        batch = r.json()
        out.extend(batch)
        if len(batch) < 100:
            return out
        offset += 100


async def recent_mean_volume(client: httpx.AsyncClient, series_id, n: int) -> dict:
    """Mean final volume over a city's last n RESOLVED daily events — a
    time-of-day-unbiased liquidity proxy (each is a complete day)."""
    r = await client.get(f"{GAMMA}/events", params={
        "series_id": series_id, "closed": "true", "limit": n,
        "order": "endDate", "ascending": "false"})
    r.raise_for_status()
    evs = r.json()
    vols = [e.get("volume") or 0.0 for e in evs]
    return {
        "mean_vol": sum(vols) / len(vols) if vols else 0.0,
        "n_days": len(vols),
        "last_close": evs[0].get("endDate", "")[:10] if evs else None,
    }


async def main(top: int, ndays: int) -> None:
    async with httpx.AsyncClient(timeout=40.0, headers={"User-Agent": "polyweather/0.1"}) as client:
        events = await fetch_all(client)

        cities: dict[str, dict] = {}
        for ev in events:
            series = (ev.get("series") or [{}])[0]
            ticker = series.get("ticker")
            if not ticker:
                continue
            c = cities.setdefault(ticker, {
                "city": _city(ev, ticker), "ticker": ticker, "unit": _unit_of(ev),
                "series_id": series.get("id"),
            })
            if not c["unit"]:
                c["unit"] = _unit_of(ev)

        cels = [c for c in cities.values() if c["unit"] == "C"]
        fahr = [c for c in cities.values() if c["unit"] == "F"]

        # fair metric: mean final volume over the last `ndays` resolved events
        stats = await asyncio.gather(
            *[recent_mean_volume(client, c["series_id"], ndays) for c in cels]
        )
        for c, s in zip(cels, stats):
            c.update(s)

    cels.sort(key=lambda c: c["mean_vol"], reverse=True)
    print(f"events={len(events)}  cities={len(cities)}  °C={len(cels)}  °F={len(fahr)}")
    print(f"ranked by mean final volume over last {ndays} resolved days\n")
    print(f"{'#':>2}  {'city':16} {'mean_vol':>11} {'days':>4}  {'series_id'}")
    for i, c in enumerate(cels[:top], 1):
        mark = "  <- London (pilot)" if c["city"].lower() == "london" else ""
        print(f"{i:>2}  {c['city']:16} {c['mean_vol']:>11,.0f} {c['n_days']:>4}  {c['series_id']}{mark}")
    # show London even if outside top
    lon = next((c for c in cels if c["city"].lower() == "london"), None)
    if lon and cels.index(lon) >= top:
        print(f"...\n    London rank #{cels.index(lon)+1}  mean_vol {lon['mean_vol']:,.0f}")
    print(f"\n(°F excluded: {', '.join(sorted(c['city'] for c in fahr))})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    asyncio.run(main(a.top, a.days))
