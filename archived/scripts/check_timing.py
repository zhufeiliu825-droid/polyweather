"""Validate the peak/lock-timing layer against reality: for recent days, compare
the predicted expected-peak to the ACTUAL hour-of-max from WU obs, and assert
`peak_passed` never declares the max locked before it actually occurred
(locking early would falsely fire LOCKED signals). Read-only.

    uv run python scripts/check_timing.py --city london --days 6
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from polyweather.config import load_config
from polyweather.forecast.conditions import classify_regime, expected_peak, peak_passed
from polyweather.forecast.open_meteo import fetch_conditions
from polyweather.realized.wu_client import fetch_observations
from polyweather.util.timewin import epoch_to_local, in_local_day


async def main(city_name: str, days: int) -> None:
    cfg = load_config()
    city = cfg.cities[city_name]
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "pw-timing/0.1"}) as client:
        bundle = await fetch_conditions(
            client, city.latitude, city.longitude, city.timezone,
            past_days=days + 1, forecast_days=1,
        )
        today = datetime.now(ZoneInfo(city.timezone)).date()
        print(f"{'day':12} {'regime':13} {'exp_peak':9} {'actual_max@':12} {'max':4} {'lock_before_max?'}")
        bad = 0
        for i in range(1, days + 1):
            d = today - timedelta(days=i)
            s = bundle.solar.get(d)
            if not s:
                continue
            ep = expected_peak(s, cfg.conditions)
            regime = classify_regime(bundle, d, cfg.conditions)
            obs = await fetch_observations(
                client, city.location_key, cfg.defaults.wu_api_key,
                d.strftime("%Y%m%d"), city.unit,
            )
            day_obs = [
                (epoch_to_local(o.valid_time_gmt, city.timezone).replace(tzinfo=None), o.temp)
                for o in obs if in_local_day(o.valid_time_gmt, city.timezone, d)
            ]
            if not day_obs:
                continue
            mx = max(t for _, t in day_obs)
            tmax = min(dt for dt, t in day_obs if t == mx)   # first time it hit the max
            locked_early = any(
                peak_passed(bundle, d, dt, cfg.conditions).peak_passed
                for dt, _ in day_obs if dt < tmax
            )
            bad += locked_early
            flag = "YES (BAD)" if locked_early else "no"
            print(f"{d.isoformat():12} {regime.value:13} {ep.strftime('%H:%M'):9} "
                  f"{tmax.strftime('%H:%M'):12} {mx:4.0f} {flag}")
        print(f"\n  days with premature lock: {bad}  ->  {'FAIL' if bad else 'PASS ✅'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="london")
    ap.add_argument("--days", type=int, default=6)
    a = ap.parse_args()
    asyncio.run(main(a.city, a.days))
