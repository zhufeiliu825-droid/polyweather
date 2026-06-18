"""Audit the resolution station of every Polymarket daily-temperature city:
extract the station name from the market rules and flag whether it's an airport
METAR station (vs an observatory / non-METAR source).

    uv run python scripts/audit_stations.py
"""
from __future__ import annotations

import asyncio
import re

import httpx

GAMMA = "https://gamma-api.polymarket.com"
TEMP_TAG = 104596

_STATION_RES = [
    re.compile(r"recorded at the (.+?) in degrees", re.I),
    re.compile(r"for the (.+?),? available", re.I),
    re.compile(r"for the (.+? Station)\b", re.I),
]


def _station(ev: dict) -> str | None:
    for m in ev.get("markets", []):
        d = m.get("description", "") or ""
        for rx in _STATION_RES:
            mm = rx.search(d)
            if mm:
                return mm.group(1).strip().rstrip(".")
    return None


def _unit(ev: dict) -> str | None:
    for m in ev.get("markets", []):
        q = m.get("question", "")
        if "°C" in q:
            return "C"
        if "°F" in q:
            return "F"
    return None


def _city(ev: dict, ticker: str) -> str:
    m = re.search(r"\bin (.+?) on\b", ev.get("title", ""))
    return m.group(1) if m else ticker.replace("-daily-weather", "")


async def main() -> None:
    async with httpx.AsyncClient(timeout=40.0, headers={"User-Agent": "polyweather/0.1"}) as client:
        events, offset = [], 0
        while True:
            r = await client.get(f"{GAMMA}/events", params={
                "tag_id": TEMP_TAG, "closed": "false", "limit": 100, "offset": offset})
            r.raise_for_status()
            b = r.json()
            events += b
            if len(b) < 100:
                break
            offset += 100

    seen: dict[str, dict] = {}
    for ev in events:
        s = (ev.get("series") or [{}])[0]
        tk = s.get("ticker")
        if not tk:
            continue
        if tk not in seen:
            seen[tk] = {"city": _city(ev, tk), "unit": _unit(ev), "station": _station(ev)}

    cels = sorted([c for c in seen.values() if c["unit"] == "C"], key=lambda c: c["city"])
    non_airport = []
    print(f"{'city':16} {'airport?':9} station")
    for c in cels:
        st = c["station"] or "(not found)"
        is_air = bool(c["station"]) and "airport" in c["station"].lower()
        if not is_air:
            non_airport.append(c["city"])
        print(f"{c['city']:16} {'YES' if is_air else '*** NO':9} {st}")
    print(f"\n°C cities: {len(cels)}   airport-METAR: {len(cels) - len(non_airport)}   "
          f"NOT airport: {len(non_airport)} -> {non_airport}")


if __name__ == "__main__":
    asyncio.run(main())
