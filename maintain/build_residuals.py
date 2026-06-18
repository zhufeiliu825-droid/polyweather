"""Rolling forecast-vs-actual residuals — how far Open-Meteo's forecast peak has missed the realized METAR
integer max, per city, over the last ~30 days. Fed to the DECISION agent as a RECENT, seasonally-adaptive
bias REFERENCE (NOT a hard formula): `residual = forecast_peak − actual_max`; a NEGATIVE mean = the forecast
ran LOW (the real max came in HIGHER) → the agent leans a higher bucket. Summer tends negative, winter
positive — exactly the seasonal drift the operator wanted captured live.

This is the recent / rolling / SOFT successor to the retired build_calib (which fit a STALE annual σ and
HARD-gated the bucket). Run daily (rolling, idempotent — rewrites data/forecast_residuals.jsonl):
  forecast = Open-Meteo historical-forecast-api (FREE, 30 d of daily peaks)
  actual   = IEM archived METAR integer max (same parse+round+clamp as the live AWC running_max). IEM is used
             because AWC caps long responses at ~400 reports, truncating the oldest local day at 30-min stations.

    uv run python maintain/build_residuals.py
"""
from __future__ import annotations

import asyncio
import json
import math
import warnings
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from polyweather.realized.feature_store import DATA_DIR
from polyweather.realized.metar_parse import parse_metar
from polyweather.realized.metar_source import fetch_history_iem
from polyweather.forecast.regime import classify_regime
from polyweather.stations import load_stations
from polyweather.util.atomicio import write_text_atomic

HF = "https://historical-forecast-api.open-meteo.com/v1/forecast"   # FREE open-access, NO apikey (not in Standard)
RESIDUALS = DATA_DIR / "forecast_residuals.jsonl"
UTC = timezone.utc
warnings.filterwarnings("ignore", category=RuntimeWarning, module="metar")  # the metar lib warns on non-std RMK groups


_HIST_VARS = ("temperature_2m,cloud_cover,precipitation,wind_speed_10m,wind_gusts_10m,"
              "relative_humidity_2m,shortwave_radiation")
_MET_KEYS = {"cloud_cover": "cloud_pct", "precipitation": "precip_mm",
             "wind_speed_10m": "wind_kmh", "wind_gusts_10m": "gust_kmh",
             "relative_humidity_2m": "rh_pct", "shortwave_radiation": "shortwave_wm2"}


async def _forecast_days(client, lat, lon, tz, start, end) -> dict[str, dict]:
    """Per-local-day historical forecast peak plus the forecast regime at that peak."""
    r = await client.get(HF, params={"latitude": lat, "longitude": lon, "hourly": _HIST_VARS,
                                     "timezone": tz, "start_date": str(start), "end_date": str(end)}, timeout=40)
    r.raise_for_status()
    h = r.json().get("hourly") or {}
    byday: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for i, (t, v) in enumerate(zip(h.get("time") or [], h.get("temperature_2m") or [])):
        if v is not None:
            byday[t[:10]].append((i, float(v)))
    out = {}
    for day, values in byday.items():
        idx, peak = max(values, key=lambda x: x[1])
        met = {}
        for src, dst in _MET_KEYS.items():
            values_for_field = h.get(src) or []
            if idx < len(values_for_field):
                met[dst] = values_for_field[idx]
        regime = classify_regime(met)
        out[day] = {"forecast_peak": round(peak, 1),
                    "regime": (regime or {}).get("primary"),
                    "regime_tags": (regime or {}).get("tags", []),
                    "regime_met": (regime or {}).get("features", {})}
    return out


async def _actual_maxes(client, icao, tz, start, end) -> dict[str, int]:
    """Complete per-local-day integer max from IEM METAR, padded for timezone boundaries."""
    reps = await fetch_history_iem(client, icao, (start - timedelta(days=1)).strftime("%Y%m%d"),
                                   (end + timedelta(days=2)).strftime("%Y%m%d"))
    z = ZoneInfo(tz)
    byday: dict[str, int] = {}
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
    return byday


async def city_rows(client, stn, today, days: int = 30) -> list[dict]:
    """One {station, city, date, forecast_peak, actual_max, residual} per SETTLED day with both values."""
    start, end = today - timedelta(days=days), today - timedelta(days=1)   # exclude today (not settled)
    try:
        fc = await _forecast_days(client, stn.latitude, stn.longitude, stn.timezone, start, end)
        ac = await _actual_maxes(client, stn.station_id, stn.timezone, start, end)
    except Exception as e:
        print(f"  {stn.station_id}: fetch failed ({e!r}) — skipped", flush=True)
        return []
    return [{"station": stn.station_id, "city": stn.city, "date": d,
             **fc[d], "actual_max": ac[d],
             "residual": round(fc[d]["forecast_peak"] - ac[d], 1)}
            for d in sorted(set(fc) & set(ac))]


def _local_today(now_utc: datetime, tz: str):
    """The station's current calendar date; the preceding local day is the newest settled sample."""
    return now_utc.astimezone(ZoneInfo(tz)).date()


async def main() -> None:
    now_utc = datetime.now(UTC)
    stns = [s for s in load_stations().values() if s.verified]
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=40, headers={"User-Agent": "polyweather/0.1"}) as client:
        for stn in stns:
            rows = await city_rows(client, stn, _local_today(now_utc, stn.timezone))
            out.extend(rows)
            if rows:
                rs = [r["residual"] for r in rows]
                print(f"  {stn.station_id} {stn.city}: {len(rows)}d · residual mean "
                      f"{round(sum(rs) / len(rs), 2):+} (range {min(rs):+}..{max(rs):+})", flush=True)
    covered = {row["station"] for row in out}
    minimum = math.ceil(len(stns) * 0.8)
    if len(covered) < minimum:
        raise RuntimeError(f"refusing to replace residuals: only {len(covered)}/{len(stns)} stations fetched "
                           f"(minimum {minimum})")
    write_text_atomic(RESIDUALS, "".join(json.dumps(r) + "\n" for r in out))
    print(f"wrote {len(out)} residual rows for {len(stns)} verified cities → {RESIDUALS}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
