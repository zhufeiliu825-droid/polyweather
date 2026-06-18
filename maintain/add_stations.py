"""APPEND new °C / WU-source cities to config/stations.json WITHOUT touching the
existing rows (preserves their `verified` flags). Coordinates + elevation come
from CheckWX; pm_series_id is read live from Polymarket Gamma. New rows are
written with verified=false, tier="candidate" — they must pass maintain/verify_new.py
before being traded.

    CHECKWX_API_KEY=... uv run python maintain/add_stations.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

import httpx

from polyweather.util.atomicio import write_text_atomic

# (city, ICAO, IANA tz, ISO country) — all WU-source, integer-°C, non-US.
# Removed 2026-06-15: Karachi/OPMR (Masroor Air Base) — no IEM history, unverifiable;
# see data/removed_stations.json for the record + re-add steps.
NEW = [
    ("Buenos Aires", "SAEZ", "America/Argentina/Buenos_Aires", "AR"),
    ("Cape Town",     "FACT", "Africa/Johannesburg",           "ZA"),
    ("Jeddah",        "OEJN", "Asia/Riyadh",                   "SA"),
    ("Mexico City",   "MMMX", "America/Mexico_City",           "MX"),
    ("Amsterdam",     "EHAM", "Europe/Amsterdam",              "NL"),
    ("Milan",         "LIMC", "Europe/Rome",                   "IT"),
    ("Helsinki",      "EFHK", "Europe/Helsinki",               "FI"),
    ("Toronto",       "CYYZ", "America/Toronto",               "CA"),
    ("Qingdao",       "ZSQD", "Asia/Shanghai",                 "CN"),
    ("Busan",         "RKPK", "Asia/Seoul",                    "KR"),
    ("Manila",        "RPLL", "Asia/Manila",                   "PH"),
    ("Kuala Lumpur",  "WMKK", "Asia/Kuala_Lumpur",             "MY"),
    ("Panama City",   "MPMG", "America/Panama",                "PA"),
]

GAMMA = "https://gamma-api.polymarket.com/events"
TAG = 104596
HDR = {"User-Agent": "Mozilla/5.0 polyweather/0.1", "Accept": "application/json"}


def gamma_series_map() -> dict[str, dict]:
    """city(lower) -> {series_id, slug} from live + recent Polymarket events."""
    out: dict[str, dict] = {}
    for closed in ("false", "true"):
        off = 0
        while True:
            q = urllib.parse.urlencode({"tag_id": TAG, "closed": closed, "limit": 100,
                                        "offset": off, "order": "endDate", "ascending": "false"})
            req = urllib.request.Request(f"{GAMMA}?{q}", headers=HDR)
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    batch = json.load(r)
            except Exception:
                break
            if not batch:
                break
            for ev in batch:
                m = re.search(r'(?:temperature|temp)\s+in\s+(.+?)\s+on', ev.get("title", "") or "", re.I)
                if not m:
                    continue
                key = m.group(1).strip().lower()
                if key in out:
                    continue
                ser = (ev.get("series") or [{}])[0]
                out[key] = {"series_id": ser.get("id"), "slug": ser.get("slug") or key.replace(" ", "-")}
            if len(batch) < 100:
                break
            off += 100
            if off > 1500:
                break
    return out


async def fetch_meta(client: httpx.AsyncClient, icao: str, key: str) -> dict:
    r = await client.get(f"https://api.checkwx.com/station/{icao}", headers={"X-API-Key": key})
    r.raise_for_status()
    data = r.json().get("data") or []
    if not data:
        return {"name": None, "lat": None, "lon": None, "elev": None}
    o = data[0]
    geo = (o.get("geometry") or {}).get("coordinates") or [None, None]
    return {"name": o.get("name"), "lon": geo[0], "lat": geo[1],
            "elev": (o.get("elevation") or {}).get("meters")}


async def main() -> None:
    key = os.environ.get("CHECKWX_API_KEY")
    if not key:
        print("set CHECKWX_API_KEY")
        return

    path = Path(__file__).resolve().parents[1] / "config" / "stations.json"
    existing = json.loads(path.read_text())
    have_icao = {s["station_id"] for s in existing}
    have_city = {(s.get("city") or "").lower() for s in existing}

    smap = gamma_series_map()
    added, failed = [], []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for city, icao, tz, cc in NEW:
            if icao in have_icao or city.lower() in have_city:
                print(f"  skip {icao} {city} (already present)")
                continue
            try:
                m = await fetch_meta(client, icao, key)
            except Exception as e:
                failed.append((city, icao, f"checkwx error: {e}"))
                continue
            if m["lat"] is None:
                failed.append((city, icao, "no coords in CheckWX (station unknown)"))
                continue
            ser = smap.get(city.lower(), {})
            slug = ser.get("slug") or city.lower().replace(" ", "-")
            row = {
                "station_id": icao,
                "name": m["name"] or f"{city} Airport",
                "city": city,
                "latitude": round(m["lat"], 5),
                "longitude": round(m["lon"], 5),
                "elevation_m": round(m["elev"], 1) if m["elev"] is not None else None,
                "timezone": tz,
                "country_code": cc,
                "pm_city_slug": slug,
                "pm_series_ticker": f"{slug}-daily-weather",
                "pm_series_id": ser.get("series_id"),
                "resolution_unit": "C",
                "report_interval_min": 30,   # default; verify_city refines if hourly
                "tier": "candidate",
                "verified": False,
            }
            existing.append(row)
            added.append(row)

    write_text_atomic(path, json.dumps(existing, indent=2, ensure_ascii=False) + "\n")
    print(f"\nADDED {len(added)} stations (total now {len(existing)}):")
    for s in added:
        print(f"  {s['station_id']} {s['city']:14} {s['latitude']},{s['longitude']} "
              f"elev={s['elevation_m']}m {s['timezone']:28} series={s['pm_series_id']}")
    if failed:
        print(f"\nFAILED {len(failed)} (need manual coords / different ICAO):")
        for city, icao, why in failed:
            print(f"  {icao} {city}: {why}")


if __name__ == "__main__":
    asyncio.run(main())
