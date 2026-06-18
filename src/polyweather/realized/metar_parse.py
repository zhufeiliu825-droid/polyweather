"""Parse a raw METAR string into a MetarObs feature record. One parser serves
both sources (CheckWX live, IEM history) since both hand us the same raw report.

Uses the `metar` library for the heavy lifting; we extract our feature set and
compute derived fields (RH, wind u/v, cloud oktas, present-weather flags).
"""
from __future__ import annotations

import math
from datetime import datetime

from metar import Metar

from .metar_obs import MetarObs

# METAR cloud coverage code -> oktas (eighths of sky covered)
_OKTA = {
    "SKC": 0.0, "CLR": 0.0, "NSC": 0.0, "NCD": 0.0, "CAVOK": 0.0,
    "FEW": 1.5, "SCT": 3.5, "BKN": 6.0, "OVC": 8.0, "VV": 8.0, "OVCX": 8.0,
}
_PRECIP_CODES = {"RA", "SN", "DZ", "SG", "GR", "GS", "PL", "IC", "UP", "RASN"}


def _val(obj, *args):
    try:
        return obj.value(*args) if obj is not None else None
    except Exception:
        return None


def relative_humidity(temp_c, dew_c):
    """Magnus-formula RH (%) from temperature and dewpoint."""
    if temp_c is None or dew_c is None:
        return None
    a, b = 17.625, 243.04
    return round(100.0 * math.exp(a * dew_c / (b + dew_c)) / math.exp(a * temp_c / (b + temp_c)), 1)


def parse_metar(raw: str, station_id: str, obs_time_utc: datetime) -> MetarObs:
    m = Metar.Metar(raw, month=obs_time_utc.month, year=obs_time_utc.year, strict=False)

    temp_c = _val(m.temp, "C")
    dew_c = _val(m.dewpt, "C")
    wdir = _val(m.wind_dir)
    wspd = _val(m.wind_speed, "KT")
    gust = _val(m.wind_gust, "KT")

    if wdir is not None and wspd is not None:
        r = math.radians(wdir)
        wind_u = round(-wspd * math.sin(r), 2)
        wind_v = round(-wspd * math.cos(r), 2)
    else:  # calm or variable -> no defined advection vector
        wind_u = wind_v = 0.0

    okta = 0.0
    base = None
    for layer in (m.sky or []):
        cover = layer[0]
        dist = layer[1] if len(layer) > 1 else None
        okta = max(okta, _OKTA.get(cover, 0.0))
        if cover in ("BKN", "OVC", "VV") and dist is not None:
            ft = _val(dist, "FT")
            if ft is not None:
                base = ft if base is None else min(base, ft)

    is_precip = False
    intensity = 0
    is_thunder = False
    is_fog = False
    for wx in (m.weather or []):
        parts = (list(wx) + [None] * 5)[:5]
        inten, desc, prec, obsc, _other = parts
        if desc == "TS" or prec == "TS":
            is_thunder = True
        if prec in _PRECIP_CODES:
            is_precip = True
            intensity = max(intensity, 1 if inten == "-" else (3 if inten == "+" else 2))
        if obsc in ("FG", "BR") or prec in ("FG", "BR"):
            is_fog = True

    return MetarObs(
        station_id=station_id,
        obs_time_utc=obs_time_utc,
        temp_c=temp_c,
        dewpoint_c=dew_c,
        rh_pct=relative_humidity(temp_c, dew_c),
        wind_dir_deg=wdir,
        wind_speed_kt=wspd,
        wind_gust_kt=gust,
        wind_u=wind_u,
        wind_v=wind_v,
        cloud_cover_okta=okta,
        cloud_base_ft=base,
        is_precip=is_precip,
        precip_intensity=intensity,
        is_thunder=is_thunder,
        is_fog=is_fog,
        visibility_m=_val(m.vis, "M"),
        qnh_hpa=_val(m.press, "MB"),
        raw_metar=raw.strip(),
    )
