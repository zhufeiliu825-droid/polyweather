"""Build config/stations.json for the 20 target cities. Coordinates + elevation
are fetched (verified) from CheckWX; timezone/country/series are the curated
per-city config. Timezone is IANA and used ONLY to derive the local market-day
from UTC — the canonical timeline everywhere is UTC.

    CHECKWX_API_KEY=... uv run python maintain/build_stations.py
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx

from polyweather.util.atomicio import write_text_atomic

# (city, ICAO, IANA tz, ISO country, pm_series_id, pm_city_slug)
CITIES = [
    ("Seoul", "RKSI", "Asia/Seoul", "KR", 10742, "seoul"),
    ("London", "EGLC", "Europe/London", "GB", 10006, "london"),
    ("Shanghai", "ZSPD", "Asia/Shanghai", "CN", 10741, "shanghai"),
    ("Beijing", "ZBAA", "Asia/Shanghai", "CN", 11363, "beijing"),
    ("Tokyo", "RJTT", "Asia/Tokyo", "JP", 10740, "tokyo"),
    ("Paris", "LFPB", "Europe/Paris", "FR", 11168, "paris"),
    ("Taipei", "RCSS", "Asia/Taipei", "TW", 11346, "taipei"),
    ("Singapore", "WSSS", "Asia/Singapore", "SG", 11314, "singapore"),
    ("Guangzhou", "ZGGG", "Asia/Shanghai", "CN", 11529, "guangzhou"),
    ("Madrid", "LEMD", "Europe/Madrid", "ES", 11345, "madrid"),
    ("Munich", "EDDM", "Europe/Berlin", "DE", 11272, "munich"),
    ("Chongqing", "ZUCK", "Asia/Shanghai", "CN", 11362, "chongqing"),
    ("Chengdu", "ZUUU", "Asia/Shanghai", "CN", 11365, "chengdu"),
    ("Warsaw", "EPWA", "Europe/Warsaw", "PL", 11342, "warsaw"),
    ("Wellington", "NZWN", "Pacific/Auckland", "NZ", 10902, "wellington"),
    ("Wuhan", "ZHHH", "Asia/Shanghai", "CN", 11364, "wuhan"),
    ("Ankara", "LTAC", "Europe/Istanbul", "TR", 10900, "ankara"),
    ("Lucknow", "VILK", "Asia/Kolkata", "IN", 11271, "lucknow"),
    ("Sao Paulo", "SBGR", "America/Sao_Paulo", "BR", 11169, "sao-paulo"),
]

# 60-min (hourly) METAR stations — flagged for extra verify_city scrutiny.
HOURLY = {"ZUCK", "ZUUU", "ZHHH", "SBGR"}


async def fetch_meta(client: httpx.AsyncClient, icao: str, key: str) -> dict:
    r = await client.get(f"https://api.checkwx.com/station/{icao}", headers={"X-API-Key": key})
    r.raise_for_status()
    o = (r.json().get("data") or [{}])[0]
    geo = (o.get("geometry") or {}).get("coordinates") or [None, None]
    return {
        "name": o.get("name"),
        "lon": geo[0],
        "lat": geo[1],
        "elev": (o.get("elevation") or {}).get("meters"),
    }


async def main() -> None:
    key = os.environ.get("CHECKWX_API_KEY")
    if not key:
        print("set CHECKWX_API_KEY")
        return
    out = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for city, icao, tz, cc, sid, slug in CITIES:
            m = await fetch_meta(client, icao, key)
            out.append({
                "station_id": icao,
                "name": m["name"] or f"{city} Airport",
                "city": city,
                "latitude": round(m["lat"], 5) if m["lat"] is not None else None,
                "longitude": round(m["lon"], 5) if m["lon"] is not None else None,
                "elevation_m": round(m["elev"], 1) if m["elev"] is not None else None,
                "timezone": tz,
                "country_code": cc,
                "pm_city_slug": slug,
                "pm_series_ticker": f"{slug}-daily-weather",
                "pm_series_id": sid,
                "resolution_unit": "C",
                "report_interval_min": 60 if icao in HOURLY else 30,
                "tier": "watchlist",
                "verified": city == "London",
            })

    path = Path(__file__).resolve().parents[1] / "config" / "stations.json"
    write_text_atomic(path, json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(out)} stations -> {path}\n")
    for s in out:
        miss = "" if s["latitude"] is not None else "  *** NO COORDS"
        print(f"  {s['station_id']} {s['city']:12} {s['latitude']},{s['longitude']} "
              f"elev={s['elevation_m']}m {s['timezone']} {s['report_interval_min']}min{miss}")


if __name__ == "__main__":
    asyncio.run(main())
