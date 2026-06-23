"""Open-Meteo FORECAST API — queried at the resolution station's coordinates so the temperature
forecast can be bias-corrected to that station's METAR.

This is the LIVE, high-frequency call (every city, every scan). With `OPEN_METEO_API_KEY` set (a paid
Standard+ subscription) it uses the commercial `customer-` endpoint + apikey (no rate cap); otherwise the
free open-access endpoint (600/min · 5000/h · 10k/day, no key). The Forecast API is included in Standard.
The HISTORICAL maintainer profile_cities (archive-api / ERA5) is NOT in Standard and deliberately stays on
its FREE open-access endpoint (no key)."""
from __future__ import annotations

import os
from datetime import date, datetime

import httpx

OPEN_METEO_FREE = "https://api.open-meteo.com/v1/forecast"          # open-access (rate-limited, no key)
OPEN_METEO_PAID = "https://customer-api.open-meteo.com/v1/forecast"  # commercial (Standard+) — needs apikey
OPEN_METEO = OPEN_METEO_FREE                                        # back-compat alias
PREV_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"  # past model runs — FREE open-access, NO apikey (not in Standard)


def _forecast_endpoint() -> tuple[str, dict]:
    """(url, auth_params) for the Forecast API: the paid `customer-` endpoint + apikey when
    OPEN_METEO_API_KEY is set, else the free open-access endpoint with no key."""
    key = (os.environ.get("OPEN_METEO_API_KEY") or "").strip()
    return (OPEN_METEO_PAID, {"apikey": key}) if key else (OPEN_METEO_FREE, {})


def _parse_minutely_15(payload: dict) -> list[tuple[datetime, float]]:
    m = payload.get("minutely_15") or {}
    return [(datetime.fromisoformat(ts), tp)
            for ts, tp in zip(m.get("time") or [], m.get("temperature_2m") or [])
            if tp is not None]


async def fetch_hourly(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    tz: str,
    *,
    past_days: int = 0,
    forecast_days: int = 1,
    model: str | None = None,
) -> list[tuple[datetime, float]]:
    """Return [(naive local datetime, temp_C)] hourly. Times are local to `tz`.
    `model` (e.g. 'icon_seamless') pins the OpenMeteo model via &models=; None ⇒ best_match (default).
    The minimal (time, temp) projection the forecast/analysis path consumes.

    (The main scan uses `fetch_minutely_15` for a sharper peak TIME; this hourly fetch is
    still used by the met snapshot. The daily-MAX value — the bet bucket — is granularity-
    invariant, verified Δval≈0 across regions, so 15-min vs hourly changes only the peak
    TIME precision, never the bucket.)"""
    url, auth = _forecast_endpoint()                       # paid customer endpoint + apikey, or free
    r = await client.get(
        url,
        params={
            **auth,
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "timezone": tz,
            "past_days": past_days,
            "forecast_days": forecast_days,
            **({"models": model} if model else {}),
        },
    )
    r.raise_for_status()
    d = r.json()
    times = d["hourly"]["time"]
    temps = d["hourly"]["temperature_2m"]
    return [
        (datetime.fromisoformat(ts), tp) for ts, tp in zip(times, temps) if tp is not None
    ]


async def fetch_minutely_15(
    client: httpx.AsyncClient, lat: float, lon: float, tz: str, *, forecast_days: int = 2,
    model: str | None = None,
) -> list[tuple[datetime, float]]:
    """15-minute `temperature_2m` forecast → [(naive local dt, temp_C)]. Same paid/free routing as
    fetch_hourly (it's the Forecast API). Used by BOTH the main scan (`scan_city`) and the strike-timing
    stage for a sharper peak TIME — NOT to change the bet bucket (the daily-max VALUE is granularity-
    invariant; see the fetch_hourly note). `model` pins the OpenMeteo model (None ⇒ best_match)."""
    url, auth = _forecast_endpoint()
    r = await client.get(url, params={**auth, "latitude": lat, "longitude": lon,
                                      "minutely_15": "temperature_2m", "timezone": tz,
                                      "forecast_days": forecast_days,
                                      **({"models": model} if model else {})})
    r.raise_for_status()
    return _parse_minutely_15(r.json())


# Meteorologist variables (hourly) — the atmospheric picture the strike/decision LLMs reason over like a
# forecaster (timing / uncertainty / NWP bias-direction). request-name → compact output-key. cloud_cover and
# surface_pressure are HOURLY-only (not in minutely_15), so the met snapshot is hourly (the daily-max VALUE is
# granularity-invariant anyway — see fetch_hourly). All names verified live against the Forecast API.
MET_VARS = {"temperature_2m": "temp_C", "cloud_cover": "cloud_pct", "wind_speed_10m": "wind_kmh",
            "wind_direction_10m": "wind_dir", "wind_gusts_10m": "gust_kmh", "dew_point_2m": "dewpoint_C",
            "relative_humidity_2m": "rh_pct", "surface_pressure": "pressure_hPa",
            "precipitation": "precip_mm", "shortwave_radiation": "shortwave_wm2"}


def _parse_hourly_met(payload: dict) -> list[dict]:
    hourly = payload.get("hourly") or {}
    out = []
    for i, ts in enumerate(hourly.get("time") or []):
        temps = hourly.get("temperature_2m") or []
        if i >= len(temps) or temps[i] is None:
            continue
        row = {"t": datetime.fromisoformat(ts)}
        for req, key in MET_VARS.items():
            vals = hourly.get(req) or []
            row[key] = vals[i] if i < len(vals) else None
        out.append(row)
    return out


async def fetch_hourly_met(client: httpx.AsyncClient, lat: float, lon: float, tz: str,
                           *, forecast_days: int = 2, model: str | None = None) -> list[dict]:
    """Hourly forecast WITH the meteorologist variables → [{t: naive-local-dt, temp_C, cloud_pct, wind_kmh,
    wind_dir, gust_kmh, dewpoint_C, rh_pct, pressure_hPa, precip_mm, shortwave_wm2}]. Same paid/free routing
    as fetch_hourly (it's the Forecast API). Powers the intraday snapshot fan + the met context for both LLMs.
    `model` pins the OpenMeteo model (None ⇒ best_match)."""
    url, auth = _forecast_endpoint()
    r = await client.get(url, params={**auth, "latitude": lat, "longitude": lon,
                                      "hourly": ",".join(MET_VARS), "timezone": tz,
                                      "forecast_days": forecast_days,
                                      **({"models": model} if model else {})})
    r.raise_for_status()
    return _parse_hourly_met(r.json())


async def fetch_minutely_15_with_hourly_met(
    client: httpx.AsyncClient, lat: float, lon: float, tz: str, *, forecast_days: int = 2,
    model: str | None = None,
) -> tuple[list[tuple[datetime, float]], list[dict]]:
    """Fetch the 15-minute temperature curve and hourly meteorology in one API request.

    Reward farming needs tomorrow's sharper peak time and its weather regime every cycle; combining the
    projections avoids doubling Open-Meteo traffic for every city. `model` pins the OpenMeteo model.
    """
    url, auth = _forecast_endpoint()
    r = await client.get(url, params={**auth, "latitude": lat, "longitude": lon,
                                      "minutely_15": "temperature_2m",
                                      "hourly": ",".join(MET_VARS), "timezone": tz,
                                      "forecast_days": forecast_days,
                                      **({"models": model} if model else {})})
    r.raise_for_status()
    payload = r.json()
    return _parse_minutely_15(payload), _parse_hourly_met(payload)


async def fetch_previous_runs(client: httpx.AsyncClient, lat: float, lon: float, tz: str,
                              target: date, *, model: str | None = None) -> dict:
    """Multi-model-run PEAK SPREAD for the `target` local day, from the FREE Previous-Model-Runs API
    (open-access, NO apikey — this API is NOT in the Standard tier, like the other historical endpoints).
    `temperature_2m_previous_dayN` is the curve as the run N days ago forecast it, on the SAME time axis as
    the latest run → a compact uncertainty + peak-time-RANGE signal for the strike-timing LLM (the latest
    single run is noisy/early; the spread is the usable signal). Returns {} on any failure (never raises).
    `model` pins the model so the run-to-run spread is for ONE model (None ⇒ best_match — mixes models)."""
    keys = ["temperature_2m", "temperature_2m_previous_day1",
            "temperature_2m_previous_day2", "temperature_2m_previous_day3"]
    try:
        r = await client.get(PREV_RUNS, params={"latitude": lat, "longitude": lon,
                                                "hourly": ",".join(keys), "timezone": tz,
                                                "forecast_days": 2,
                                                **({"models": model} if model else {})}, timeout=30.0)
        r.raise_for_status()
        H = r.json().get("hourly") or {}
    except Exception:
        return {}
    times = H.get("time") or []
    iso = target.isoformat()
    runs = []
    for k, label in zip(keys, ["latest", "day1", "day2", "day3"]):
        vals = H.get(k)
        if not vals:
            continue
        day = [(t, v) for t, v in zip(times, vals) if t[:10] == iso and v is not None]
        if not day:
            continue
        pt, pv = max(day, key=lambda x: x[1])
        runs.append({"run": label, "peak_C": round(float(pv), 1), "peak_local": pt[11:16]})
    if not runs:
        return {}
    pcs = [x["peak_C"] for x in runs]
    pts = [x["peak_local"] for x in runs]
    return {"runs": runs, "peak_C_min": min(pcs), "peak_C_max": max(pcs),
            "peak_time_min": min(pts), "peak_time_max": max(pts)}
