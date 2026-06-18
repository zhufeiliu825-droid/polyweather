"""Build a station's historical feature table from IEM ASOS: pull raw METARs,
parse to features, dedupe, write JSONL + sqlite. Foundation for modeling.

    uv run python scripts/build_history.py --station EGLC --days 14
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from polyweather.forecast.open_meteo import fetch_conditions
from polyweather.realized.enrich import enrich_rows
from polyweather.realized.feature_store import DATA_DIR, obs_row, write_jsonl, write_sqlite
from polyweather.realized.metar_parse import parse_metar
from polyweather.realized.metar_source import fetch_history_iem
from polyweather.stations import get_station


async def main(station_id: str, days: int) -> None:
    st = get_station(station_id)
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    print(f"pulling {station_id} {start}..{end} from IEM ASOS ...")

    async with httpx.AsyncClient(
        timeout=60.0, headers={"User-Agent": "polyweather/0.1"}
    ) as client:
        raws = await fetch_history_iem(
            client, station_id, start.strftime("%Y%m%d"),
            (end + timedelta(days=1)).strftime("%Y%m%d"),
        )
        # Open-Meteo hourly (UTC) for solar radiation / model cross-check, joined by hour
        bundle = await fetch_conditions(
            client, st.latitude, st.longitude, "GMT", past_days=days, forecast_days=1
        )
    print(f"got {len(raws)} raw reports")

    rows_by_key: dict[tuple, dict] = {}
    skipped = 0
    for raw, t in raws:
        try:
            o = parse_metar(raw, station_id, t)
        except Exception:
            skipped += 1
            continue
        r = obs_row(o, st.timezone)
        rows_by_key[(r["station_id"], r["obs_time_utc"])] = r
    rows = sorted(rows_by_key.values(), key=lambda r: r["obs_time_utc"])
    rows = enrich_rows(rows, st, bundle)   # + solar geometry + Open-Meteo hourly

    jsonl = DATA_DIR / f"{station_id}_obs.jsonl"
    write_jsonl(rows, jsonl)
    write_sqlite(rows)
    print(f"wrote {len(rows)} rows (skipped {skipped}) -> {jsonl} + sqlite\n")

    by_day = defaultdict(list)
    for r in rows:
        if r["temp_c"] is not None:
            by_day[r["local_date"]].append(r["temp_c"])
    print(f"daily max ({st.timezone} local day):")
    print(f"{'local_date':12} {'n':>3} {'max°' + st.resolution_unit:>6}")
    for d in sorted(by_day)[-10:]:
        print(f"{d:12} {len(by_day[d]):>3} {max(by_day[d]):>6.0f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", default="EGLC")
    ap.add_argument("--days", type=int, default=14)
    a = ap.parse_args()
    asyncio.run(main(a.station, a.days))
