"""Solar geometry via the NOAA solar-position equations (self-contained, no
external library). Deterministic from lat/lon/date; never fails for non-polar
latitudes. Drives the peak-timing intuition: temp peak ≈ solar noon + thermal lag.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone

_UTC = timezone.utc


def _decl_eot(d: date) -> tuple[float, float]:
    """(solar declination [rad], equation of time [minutes]) — NOAA fourier approx."""
    n = d.timetuple().tm_yday
    g = 2.0 * math.pi / 365.0 * (n - 1 + 0.5)   # fractional-year angle (rad)
    eot = 229.18 * (
        0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g)
    )
    decl = (
        0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
        - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g)
    )
    return decl, eot


def solar_noon_utc(lon: float, d: date) -> datetime:
    """UTC time of solar noon at a longitude (east positive)."""
    _, eot = _decl_eot(d)
    minutes = 720.0 - 4.0 * lon - eot
    return datetime.combine(d, time(0, 0), tzinfo=_UTC) + timedelta(minutes=minutes)


def day_sun(lat: float, lon: float, d: date, tz: str | None = None):
    """(sunrise_utc, solar_noon_utc, sunset_utc, daylength_h) for a date.
    `tz` is accepted for call-compatibility but unused (all times are UTC)."""
    decl, _ = _decl_eot(d)
    latr = math.radians(lat)
    # hour angle at sunrise/sunset (sun centre at -0.833°, includes refraction)
    cos_h = (math.cos(math.radians(90.833)) / (math.cos(latr) * math.cos(decl))
             - math.tan(latr) * math.tan(decl))
    cos_h = max(-1.0, min(1.0, cos_h))           # clamp -> polar day/night safe
    h_deg = math.degrees(math.acos(cos_h))
    noon = solar_noon_utc(lon, d)
    sr = noon - timedelta(minutes=4.0 * h_deg)
    ss = noon + timedelta(minutes=4.0 * h_deg)
    return sr, noon, ss, round((ss - sr).total_seconds() / 3600.0, 2)


def clamp_strike(strike: datetime, solar_noon: datetime, sunset: datetime,
                 *, min_lag_h: float = 0.0) -> datetime:
    """Force a strike time into the DAYTIME solar window [solar_noon + min_lag_h, sunset]. The core
    safety guard for the strike-timing stage: a spurious early-morning (or post-sunset) suggestion —
    from the LLM OR the forecast — can NEVER schedule a strike outside the liquid daytime window.
    Negative-lag cities still get a daytime strike (the floor is solar_noon-based, not the peak)."""
    lo = solar_noon + timedelta(hours=min_lag_h)
    if lo > sunset:                  # degenerate (very short day) — don't invert the window
        lo = sunset
    return min(max(strike, lo), sunset)


def solar_elevation_deg(lat: float, lon: float, elev_m: float | None, dt_utc: datetime) -> float:
    """Sun elevation angle (degrees) at a UTC instant. elev_m accepted but unused."""
    decl, eot = _decl_eot(dt_utc.date())
    latr = math.radians(lat)
    utc_min = dt_utc.hour * 60 + dt_utc.minute + dt_utc.second / 60.0
    tst = (utc_min + 4.0 * lon + eot) % 1440.0          # true solar time (min)
    ha = math.radians(tst / 4.0 - 180.0)                # hour angle (rad)
    sin_elev = (math.sin(latr) * math.sin(decl)
                + math.cos(latr) * math.cos(decl) * math.cos(ha))
    return round(math.degrees(math.asin(max(-1.0, min(1.0, sin_elev)))), 2)
