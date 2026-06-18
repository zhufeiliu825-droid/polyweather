"""Enrich parsed METAR feature rows with what METAR lacks: solar geometry
(computed) and Open-Meteo model fields (shortwave radiation, cloud, precip,
model temp) joined by the hour."""
from __future__ import annotations

from datetime import date, datetime

from ..forecast.open_meteo import ConditionsBundle
from ..stations import Station
from .solar import day_sun, solar_elevation_deg


def _om_hour_index(bundle: ConditionsBundle | None) -> dict[datetime, object]:
    """Index Open-Meteo hourly by naive-UTC hour (bundle fetched with tz=GMT)."""
    if bundle is None:
        return {}
    return {h.time.replace(minute=0, second=0, microsecond=0): h for h in bundle.hourly}


def enrich_rows(rows: list[dict], station: Station, bundle: ConditionsBundle | None) -> list[dict]:
    om = _om_hour_index(bundle)
    sun_cache: dict[date, tuple] = {}
    for r in rows:
        t = datetime.fromisoformat(r["obs_time_utc"])              # aware UTC
        r["solar_elevation_deg"] = solar_elevation_deg(
            station.latitude, station.longitude, station.elevation_m, t
        )
        ld = date.fromisoformat(r["local_date"])
        if ld not in sun_cache:
            sun_cache[ld] = day_sun(station.latitude, station.longitude, ld, station.timezone)
        sr, noon, ss, dl = sun_cache[ld]
        r["sunrise_utc"] = sr.isoformat()
        r["solar_noon_utc"] = noon.isoformat()
        r["sunset_utc"] = ss.isoformat()
        r["daylength_h"] = dl
        r["min_after_solar_noon"] = round((t - noon).total_seconds() / 60.0, 1)

        h = om.get(t.replace(tzinfo=None, minute=0, second=0, microsecond=0))
        r["om_shortwave_wm2"] = h.shortwave if h else None
        r["om_cloud_cover"] = h.cloud_cover if h else None
        r["om_precip_mm"] = h.precip if h else None
        r["om_temp_c"] = h.temp_c if h else None
    return rows
