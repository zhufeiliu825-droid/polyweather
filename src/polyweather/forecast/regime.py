"""Small, shared weather-regime classifier for forecast-bias conditioning.

The primary regime describes conditions at the forecast daily-temperature peak. Using the same
Open-Meteo fields for settled history and live forecasts keeps the conditioning observable before
the outcome is known; it is not a post-settlement label leaked into a live decision.
"""
from __future__ import annotations

from typing import Any


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_regime(met: dict | None) -> dict | None:
    """Classify peak-time forecast meteorology into a primary regime plus orthogonal tags.

    Primary regimes are deliberately broad so a rolling station sample can fill: rainy, clear,
    cloudy, or mixed. Tags preserve useful detail without fragmenting the calibration sample.
    """
    if not met:
        return None
    cloud = _number(met.get("cloud_pct"))
    precip = _number(met.get("precip_mm"))
    wind = _number(met.get("wind_kmh"))
    gust = _number(met.get("gust_kmh"))
    rh = _number(met.get("rh_pct"))
    radiation = _number(met.get("shortwave_wm2"))
    if all(x is None for x in (cloud, precip, wind, gust, rh, radiation)):
        return None

    if precip is not None and precip >= 0.1:
        primary = "rainy"
    elif cloud is not None and cloud <= 30.0:
        primary = "clear"
    elif cloud is not None and cloud >= 70.0:
        primary = "cloudy"
    else:
        primary = "mixed"

    tags = [primary]
    strongest_wind = max((x for x in (wind, gust) if x is not None), default=None)
    if strongest_wind is not None and strongest_wind >= 25.0:
        tags.append("windy")
    if rh is not None:
        if rh >= 75.0:
            tags.append("humid")
        elif rh <= 45.0:
            tags.append("dry")
    if radiation is not None and radiation >= 500.0:
        tags.append("strong_sun")

    features = {"cloud_pct": cloud, "precip_mm": precip, "wind_kmh": wind, "gust_kmh": gust,
                "rh_pct": rh, "shortwave_wm2": radiation}
    return {"primary": primary, "tags": tags,
            "features": {k: round(v, 2) for k, v in features.items() if v is not None}}
