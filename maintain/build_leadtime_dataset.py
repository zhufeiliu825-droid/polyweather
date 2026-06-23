"""Fixed-LEAD-TIME forecast-vs-actual dataset — the training/backtest base for a lead-aware MOS.

Unlike `build_residuals.py` (which uses the Historical Forecast API — a near-analysis series stitched
from the FIRST hours of successive runs, i.e. lead≈0), this builder uses the **Previous Runs API**
(`temperature_2m_previous_dayN`), which is the curve as the model run N days earlier ACTUALLY forecast it
— a true fixed lead-time of N×24h. That is the right error structure for the FARMER (bets tomorrow+, lead
~24–48h) and the honest base for any lead-time MOS / generative model.

Output: `data/leadtime_residuals.jsonl`, one row per (station, date, lead_days):
  {station, city, date, lead_days, model, forecast_peak, actual_max, residual, regime, regime_tags, regime_met}
  residual = forecast_peak − actual_max  (negative ⇒ the run forecast LOW; real max came in higher)

Data notes (verified live, 2026-06-19):
  - Pass an explicit `--model` (e.g. ecmwf_ifs025): default best_match has gaps in the previous-run archive.
  - Coverage: ecmwf_ifs025 / icon_seamless from 2024-01; gfs_seamless from 2021-03; jma_* from 2018.
  - The met-at-lead vars (cloud_cover_previous_dayN, …) are returned alongside the temperature, so the
    weather regime is reconstructed AT THE FORECAST'S OWN LEAD (not from a later analysis).
  - actual_max = IEM archived METAR integer max — the SAME parse+round+clamp as the live AWC running_max
    and as build_residuals, so residuals are directly comparable across the two datasets.

Examples:
  # quick validation (4 pilot stations, ~3.5 months, day1+day2):
  uv run python maintain/build_leadtime_dataset.py --stations EGLC,RKSI,RJTT,CYYZ \
      --start 2026-03-01 --end 2026-06-15 --model ecmwf_ifs025 --leads 1,2
  # full backfill (all verified stations, ECMWF archive start → yesterday):
  uv run python maintain/build_leadtime_dataset.py --start 2024-01-01 --resume
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import warnings
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from polyweather.forecast.regime import classify_regime
from polyweather.realized.feature_store import DATA_DIR
from polyweather.realized.metar_parse import parse_metar
from polyweather.realized.metar_source import fetch_history_iem
from polyweather.stations import load_stations

PREV_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"  # FREE open-access, NO apikey
OUT = DATA_DIR / "leadtime_residuals.jsonl"
UTC = timezone.utc
warnings.filterwarnings("ignore", category=RuntimeWarning, module="metar")

# request-name (un-suffixed) → compact regime key. Each gets a `_previous_day{lead}` suffix per lead.
_MET_SRC = {"cloud_cover": "cloud_pct", "precipitation": "precip_mm", "wind_speed_10m": "wind_kmh",
            "wind_gusts_10m": "gust_kmh", "relative_humidity_2m": "rh_pct",
            "shortwave_radiation": "shortwave_wm2"}


def _hourly_vars(leads: list[int]) -> str:
    """The comma-joined hourly variable list for all requested leads (temperature + the regime met)."""
    names = []
    for lead in leads:
        names.append(f"temperature_2m_previous_day{lead}")
        names.extend(f"{src}_previous_day{lead}" for src in _MET_SRC)
    return ",".join(names)


def _peaks_for_lead(hourly: dict, lead: int) -> dict[str, dict]:
    """Per-local-day forecast peak + regime AT THIS LEAD, from one Previous-Runs payload."""
    temp_key = f"temperature_2m_previous_day{lead}"
    times = hourly.get("time") or []
    temps = hourly.get(temp_key) or []
    byday: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for i, (t, v) in enumerate(zip(times, temps)):
        if v is not None:
            byday[t[:10]].append((i, float(v)))
    out: dict[str, dict] = {}
    for day, values in byday.items():
        idx, peak = max(values, key=lambda x: x[1])
        met = {}
        for src, dst in _MET_SRC.items():
            col = hourly.get(f"{src}_previous_day{lead}") or []
            if idx < len(col) and col[idx] is not None:
                met[dst] = col[idx]
        regime = classify_regime(met)
        out[day] = {"forecast_peak": round(peak, 1),
                    "regime": (regime or {}).get("primary"),
                    "regime_tags": (regime or {}).get("tags", []),
                    "regime_met": (regime or {}).get("features", {})}
    return out


async def _forecast_peaks(client, lat, lon, tz, start: date, end: date, leads: list[int],
                          model: str, chunk_days: int) -> dict[int, dict[str, dict]]:
    """{lead → {local_date → peak/regime}} over [start, end], chunked to keep responses small."""
    acc: dict[int, dict[str, dict]] = {lead: {} for lead in leads}
    cur = start
    hvars = _hourly_vars(leads)
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        r = await client.get(PREV_RUNS, params={"latitude": lat, "longitude": lon, "hourly": hvars,
                                                "models": model, "timezone": tz,
                                                "start_date": str(cur), "end_date": str(chunk_end)},
                             timeout=60)
        r.raise_for_status()
        hourly = r.json().get("hourly") or {}
        for lead in leads:
            acc[lead].update(_peaks_for_lead(hourly, lead))
        cur = chunk_end + timedelta(days=1)
    return acc


async def _actual_maxes(client, icao, tz, start: date, end: date, chunk_days: int) -> dict[str, int]:
    """Per-local-day integer max from IEM METAR over [start, end] (chunked; padded for tz boundaries)."""
    z = ZoneInfo(tz)
    byday: dict[str, int] = {}
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        reps = await fetch_history_iem(client, icao, (cur - timedelta(days=1)).strftime("%Y%m%d"),
                                       (chunk_end + timedelta(days=2)).strftime("%Y%m%d"))
        for raw, t in reps:
            try:
                o = parse_metar(raw, icao, t)
            except Exception:
                continue
            if o.temp_c is None or not (-60.0 <= o.temp_c <= 60.0):
                continue
            local_day = t.astimezone(z).date()
            if not (start <= local_day <= end):
                continue
            d = local_day.isoformat()
            v = round(o.temp_c)
            byday[d] = v if d not in byday else max(byday[d], v)
        cur = chunk_end + timedelta(days=1)
    return byday


async def station_rows(client, stn, start: date, end: date, leads: list[int], model: str,
                       chunk_days: int) -> list[dict]:
    """All (date, lead) residual rows for one station over [start, end]."""
    try:
        fc = await _forecast_peaks(client, stn.latitude, stn.longitude, stn.timezone,
                                   start, end, leads, model, chunk_days)
        ac = await _actual_maxes(client, stn.station_id, stn.timezone, start, end, chunk_days=180)
    except Exception as e:
        print(f"  {stn.station_id}: fetch failed ({e!r}) — skipped", flush=True)
        return []
    rows: list[dict] = []
    for lead in leads:
        for d in sorted(set(fc[lead]) & set(ac)):
            rows.append({"station": stn.station_id, "city": stn.city, "date": d, "lead_days": lead,
                         "model": model, **fc[lead][d], "actual_max": ac[d],
                         "residual": round(fc[lead][d]["forecast_peak"] - ac[d], 1)})
    return rows


def _done_stations(path) -> set[str]:
    """Stations already present in an existing output file (for --resume)."""
    done: set[str] = set()
    if not path.exists():
        return done
    for line in path.read_text().splitlines():
        try:
            done.add(json.loads(line)["station"])
        except (json.JSONDecodeError, KeyError):
            continue
    return done


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stations", default="", help="comma ICAOs; default all verified")
    ap.add_argument("--start", default="2024-01-01", help="YYYY-MM-DD (ecmwf/icon archive starts 2024-01)")
    ap.add_argument("--end", default="", help="YYYY-MM-DD; default yesterday UTC")
    ap.add_argument("--model", default="ecmwf_ifs025", help="Open-Meteo model id with a previous-run archive")
    ap.add_argument("--leads", default="1,2", help="comma lead-days (1=24h, 2=48h)")
    ap.add_argument("--chunk-days", type=int, default=90, help="date-range chunk per Previous-Runs request")
    ap.add_argument("--sleep", type=float, default=0.3, help="seconds between stations (politeness)")
    ap.add_argument("--resume", action="store_true", help="append, skipping stations already in --out")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    leads = [int(x) for x in args.leads.split(",") if x.strip()]
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else (datetime.now(UTC).date() - timedelta(days=1))
    out_path = Path(args.out) if args.out.startswith("/") else DATA_DIR / args.out

    stns = [s for s in load_stations().values() if s.verified]
    if args.stations:
        want = {x.strip().upper() for x in args.stations.split(",")}
        stns = [s for s in stns if s.station_id.upper() in want]

    done = _done_stations(out_path) if args.resume else set()
    if not args.resume and out_path.exists():
        out_path.unlink()
    todo = [s for s in stns if s.station_id not in done]
    print(f"leadtime backfill · {len(todo)}/{len(stns)} stations · {start}→{end} · model={args.model} "
          f"· leads={leads} → {out_path}", flush=True)

    total = 0
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "polyweather/0.1"}) as client:
        for stn in todo:
            rows = await station_rows(client, stn, start, end, leads, args.model, args.chunk_days)
            if rows:
                with out_path.open("a") as f:
                    f.write("".join(json.dumps(r) + "\n" for r in rows))
                total += len(rows)
                for lead in leads:
                    rs = [r["residual"] for r in rows if r["lead_days"] == lead]
                    if rs:
                        sd = round(statistics.stdev(rs), 2) if len(rs) > 1 else float("nan")
                        print(f"  {stn.station_id} {stn.city} d{lead}: {len(rs)}d · "
                              f"mean {round(statistics.mean(rs), 2):+} · σ {sd}", flush=True)
            await asyncio.sleep(args.sleep)
    print(f"wrote {total} rows ({len(todo)} stations) → {out_path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
