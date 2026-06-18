"""Fetch the latest CheckWX report for a station and show the parsed feature row.
Verifies the live source + parser end to end.

    CHECKWX_API_KEY=... uv run python scripts/checkwx_live.py EGLC
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

from polyweather.realized.feature_store import obs_row
from polyweather.realized.metar_parse import parse_metar
from polyweather.realized.metar_source import fetch_latest_checkwx
from polyweather.stations import get_station


async def main(station_id: str) -> None:
    st = get_station(station_id)
    key = os.environ.get("CHECKWX_API_KEY")
    if not key:
        print("set CHECKWX_API_KEY"); return
    async with httpx.AsyncClient(timeout=20.0) as client:
        res = await fetch_latest_checkwx(client, station_id, key)
    if not res:
        print("no data"); return
    raw, t = res
    row = obs_row(parse_metar(raw, station_id, t), st.timezone)
    print(json.dumps(row, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "EGLC"))
