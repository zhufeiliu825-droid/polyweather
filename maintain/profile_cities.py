"""Full-year per-city climate profile for city/season selection.

For each station, over the last ~year, computes per MONTH:
  - actual daily-max stats from IEM METAR (mean, std = day-to-day volatility)
  - diurnal range (daily max-min) mean
  - thermal lag: hour-of-max minus solar noon (for per-city Solar Peak calibration)
  - model-vs-station error σ from Open-Meteo ERA5 archive (predictability proxy)
  - precip-day % (wet/convective frequency = volatility driver)

Output: data/city_profiles.json (+ summary table). Read-only.

    uv run python maintain/profile_cities.py
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import statistics as st
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx

from polyweather.realized.feature_store import DATA_DIR
from polyweather.realized.solar import day_sun
from polyweather.util.atomicio import dump_json_atomic
from polyweather.stations import load_stations
from polyweather.util.timewin import local_date_of

IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
SEM = asyncio.Semaphore(2)   # be polite to IEM + Open-Meteo (free rate limits)


async def iem_tmpc(client, icao, start: date, end: date, retries=12, chunk_days=60):
    """Fetch in <=chunk_days windows and concatenate — a single full-year IEM
    request is often rejected (over-capacity / size), while chunks succeed."""
    out = []
    cur = start
    while cur <= end:
        c_end = min(cur + timedelta(days=chunk_days), end)
        params = {
            "station": icao, "data": "tmpc",
            "year1": cur.year, "month1": cur.month, "day1": cur.day,
            "year2": c_end.year, "month2": c_end.month, "day2": c_end.day,
            "tz": "Etc/UTC", "format": "onlycomma", "missing": "M", "trace": "T", "direct": "no",
        }
        text = ""
        for i in range(retries):
            async with SEM:
                r = await client.get(IEM, params=params)
            if r.status_code == 200 and len(r.text) > 80 and not r.text.startswith("ERROR"):
                text = r.text
                break
            await asyncio.sleep(min(3.0 * (i + 1), 25.0))
        for row in csv.DictReader(io.StringIO(text)):
            v = (row.get("tmpc") or "").strip()
            valid = (row.get("valid") or "").strip()
            if v in ("", "M") or not valid:
                continue
            try:
                t = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                out.append((t, float(v)))
            except ValueError:
                continue
        cur = c_end + timedelta(days=1)
    return out


async def era5_daily(client, lat, lon, tz, start: date, end: date, retries=6):
    params = {
        "latitude": lat, "longitude": lon, "timezone": "GMT",
        "hourly": "temperature_2m,precipitation",
        "start_date": start.isoformat(), "end_date": end.isoformat()}
    r = None
    for i in range(retries):
        async with SEM:
            r = await client.get(ARCHIVE, params=params)
        if r.status_code == 200:
            break
        if r.status_code in (429, 500, 503):
            await asyncio.sleep(5.0 * (i + 1))
            continue
        r.raise_for_status()
    if r is None or r.status_code != 200:
        return {}
    H = r.json().get("hourly", {})
    times = H.get("time", [])
    temp = H.get("temperature_2m", [])
    prec = H.get("precipitation", [])
    by_day_t = defaultdict(list)
    by_day_p = defaultdict(float)
    for i, ts in enumerate(times):
        t = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        d = local_date_of(int(t.timestamp()), tz)
        if temp[i] is not None:
            by_day_t[d].append(temp[i])
        if prec[i] is not None:
            by_day_p[d] += prec[i]
    return {d: (max(v), by_day_p.get(d, 0.0)) for d, v in by_day_t.items()}


def _season_of(month: int, southern: bool) -> str:
    n = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
         6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}[month]
    if southern:  # flip hemispheres
        n = {"DJF": "JJA", "JJA": "DJF", "MAM": "SON", "SON": "MAM"}[n]
    return n


async def profile(client, stn, start: date, end: date) -> dict:
    try:
        return await _profile(client, stn, start, end)
    except Exception as e:
        return {"city": stn.city, "station": stn.station_id, "error": repr(e)[:100]}


async def _profile(client, stn, start: date, end: date) -> dict:
    obs = await iem_tmpc(client, stn.station_id, start, end)
    model = await era5_daily(client, stn.latitude, stn.longitude, stn.timezone, start, end)
    if not obs:
        return {"city": stn.city, "station": stn.station_id, "error": "no IEM data"}

    # actual per local day: max, min, time-of-max
    day = defaultdict(list)
    for t, v in obs:
        day[local_date_of(int(t.timestamp()), stn.timezone)].append((t, v))
    per_day = {}
    noon_cache = {}
    for d, lst in day.items():
        temps = [v for _, v in lst]
        tmax = max(temps)
        tmax_time = min(t for t, v in lst if v == tmax)
        if d not in noon_cache:
            noon_cache[d] = day_sun(stn.latitude, stn.longitude, d, stn.timezone)[1]
        lag = (tmax_time - noon_cache[d]).total_seconds() / 3600.0
        per_day[d] = {"max": tmax, "rng": tmax - min(temps), "lag": lag,
                      "merr": (model[d][0] - tmax) if d in model else None,
                      "wet": (model[d][1] >= 0.2) if d in model else None}

    southern = stn.latitude < 0
    by_m = defaultdict(list)
    for d, r in per_day.items():
        by_m[d.month].append(r)

    months = {}
    for m, rs in by_m.items():
        maxes = [r["max"] for r in rs]
        merrs = [r["merr"] for r in rs if r["merr"] is not None]
        lags = [r["lag"] for r in rs]
        wets = [r["wet"] for r in rs if r["wet"] is not None]
        months[m] = {
            "season": _season_of(m, southern),
            "n": len(rs),
            "max_mean": round(st.fmean(maxes), 1),
            "max_std": round(st.pstdev(maxes), 2) if len(maxes) > 1 else 0,
            "diurnal": round(st.fmean([r["rng"] for r in rs]), 1),
            "lag_h": round(st.fmean(lags), 2),
            "model_sigma": round(st.pstdev(merrs), 2) if len(merrs) > 1 else None,
            "precip_day_pct": round(100 * sum(wets) / len(wets)) if wets else None,
        }
    allmax = [r["max"] for r in per_day.values()]
    allmerr = [r["merr"] for r in per_day.values() if r["merr"] is not None]
    return {
        "city": stn.city, "station": stn.station_id, "tz": stn.timezone,
        "elev_m": stn.elevation_m, "verified": stn.verified,
        "n_days": len(per_day),
        "annual_max_std": round(st.pstdev(allmax), 2) if len(allmax) > 1 else 0,
        "annual_model_sigma": round(st.pstdev(allmerr), 2) if len(allmerr) > 1 else None,
        "months": months,
    }


async def main() -> None:
    stations = load_stations()
    end = date.today() - timedelta(days=6)      # ERA5 lag
    start = end - timedelta(days=365)
    path = DATA_DIR / "city_profiles.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    out = json.loads(path.read_text()) if path.exists() else {}

    def good(r: dict) -> bool:
        return isinstance(r, dict) and not r.get("error") and r.get("n_days")

    todo = [s for s in stations.values() if not good(out.get(s.station_id, {}))]
    print(f"profiling {len(todo)} cities (keeping {len(stations) - len(todo)} cached) ...")
    async with httpx.AsyncClient(timeout=180.0, headers={"User-Agent": "polyweather/0.1"}) as client:
        fresh = await asyncio.gather(*[profile(client, s, start, end) for s in todo])
    for r in fresh:
        out[r["station"]] = r
    results = list(out.values())
    dump_json_atomic(path, out, indent=2, ensure_ascii=False)   # daemon reads this (FALLBACK σ) → atomic write
    print(f"window {start}..{end}  -> {path}\n")
    print(f"{'city':12}{'days':>5}{'annModelσ':>10}{'annMaxStd':>10}{'JJA σ':>7}{'JJA lag':>8}{'JJA wet%':>9}")
    for r in sorted(results, key=lambda r: (r.get('annual_model_sigma') or 9)):
        if r.get("error"):
            print(f"{r['city']:12}  {r['error']}")
            continue
        jja = next((mm for mm in r["months"].values() if mm["season"] == "JJA"), {})
        print(f"{r['city']:12}{r['n_days']:>5}{str(r['annual_model_sigma']):>10}{r['annual_max_std']:>10}"
              f"{str(jja.get('model_sigma')):>7}{str(jja.get('lag_h')):>8}{str(jja.get('precip_day_pct')):>9}")


if __name__ == "__main__":
    asyncio.run(main())
