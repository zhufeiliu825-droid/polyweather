"""Station registry — the single source of truth mapping a Polymarket daily-temp
series to its physical resolution station. Stored as JSON (script/AI-friendly),
one object per station. The registry's `verified` flag controls which stations are eligible."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

STATIONS_FILE = Path(__file__).resolve().parents[2] / "config" / "stations.json"


class Station(BaseModel):
    station_id: str          # ICAO, e.g. EGLC
    name: str
    city: str                # Polymarket city label
    latitude: float
    longitude: float
    elevation_m: float | None = None
    timezone: str            # IANA, e.g. Europe/London (DST handled by zoneinfo)
    country_code: str
    pm_city_slug: str        # used in event slug
    pm_series_ticker: str    # Gamma series ticker
    pm_series_id: int | None = None
    resolution_unit: str = "C"   # market resolution unit; auto-read/verified from the market
    report_interval_min: int | None = None   # METAR cadence (30 or 60); info only
    tier: str = "breadth"        # watchlist | breadth
    verified: bool = False       # passed station+unit+tz verification


@lru_cache(maxsize=1)
def load_stations(path: str | None = None) -> dict[str, Station]:
    p = Path(path) if path else STATIONS_FILE
    data = json.loads(p.read_text())
    return {s["station_id"]: Station(**s) for s in data}
