"""Forecast calibration check: over the last N days, compare Open-Meteo's daily
max (at the station coords) to the WU/METAR actual, and report bias, sigma, and
±2σ coverage. Read-only.

    uv run python scripts/forecast_calib.py --city london --days 14
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from polyweather.config import load_config
from polyweather.forecast.open_meteo import fetch_hourly
from polyweather.forecast.upside import _forecast_daily_max
from polyweather.realized.daily_max import compute_running_max
from polyweather.realized.wu_client import fetch_observations


async def main(city_name: str, days: int) -> None:
    cfg = load_config()
    city = cfg.cities[city_name]
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "polyweather-calib/0.1"}) as client:
        hourly = await fetch_hourly(
            client, city.latitude, city.longitude, city.timezone,
            past_days=days, forecast_days=0,
        )
        fc_by_day = _forecast_daily_max(hourly)
        today = datetime.now(ZoneInfo(city.timezone)).date()

        print(f"{'day':12} {'forecast':>9} {'actual':>7} {'err(fc-act)':>12}")
        residuals = []
        for i in range(1, days + 1):
            d = today - timedelta(days=i)
            if d not in fc_by_day:
                continue
            obs = await fetch_observations(
                client, city.location_key, cfg.defaults.wu_api_key,
                d.strftime("%Y%m%d"), city.unit,
            )
            actual, n, _ = compute_running_max(obs, city.timezone, d)
            if actual is None:
                continue
            err = fc_by_day[d] - actual
            residuals.append(err)
            print(f"{d.isoformat():12} {fc_by_day[d]:9.1f} {actual:7.0f} {err:+12.1f}")

        if residuals:
            bias = statistics.fmean(residuals)
            sigma = statistics.pstdev(residuals) if len(residuals) > 1 else 0.0
            within2 = sum(abs(r - bias) <= 2 * sigma for r in residuals) / len(residuals)
            print(f"\n  n={len(residuals)}  bias={bias:+.2f}°  sigma={sigma:.2f}°  "
                  f"±2σ coverage={within2:.0%}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="london")
    ap.add_argument("--days", type=int, default=14)
    asyncio.run(main(ap.parse_args().city, ap.parse_args().days))
