"""Weather Underground historical observations (the exact Polymarket resolution
source). Public web apiKey; no user auth."""
from __future__ import annotations

import httpx

from ..models import Obs

WU = "https://api.weather.com/v1/location"


def units_for(unit: str) -> str:
    """Market resolution unit -> WU units param. Fetch in the resolution unit to
    avoid a ℃->°F double-rounding mismatch at bucket boundaries."""
    return "e" if unit.upper() == "F" else "m"


async def fetch_observations(
    client: httpx.AsyncClient,
    location_key: str,
    api_key: str,
    yyyymmdd: str,
    unit: str,
) -> list[Obs]:
    r = await client.get(
        f"{WU}/{location_key}/observations/historical.json",
        params={"apiKey": api_key, "units": units_for(unit), "startDate": yyyymmdd},
    )
    r.raise_for_status()
    data = r.json()
    return [
        Obs(valid_time_gmt=o["valid_time_gmt"], temp=o["temp"])
        for o in data.get("observations", [])
        if o.get("temp") is not None and o.get("valid_time_gmt") is not None
    ]
