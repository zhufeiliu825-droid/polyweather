"""Rich per-observation feature record parsed from a raw METAR. Time is always
UTC (from the data source); local_date is derived per the station timezone at
storage time."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class MetarObs(BaseModel):
    station_id: str
    obs_time_utc: datetime            # canonical UTC timestamp from the source
    temp_c: float | None
    dewpoint_c: float | None
    rh_pct: float | None
    wind_dir_deg: float | None        # raw direction (None for VRB/calm)
    wind_speed_kt: float | None
    wind_gust_kt: float | None
    wind_u: float | None              # eastward component (= -speed·sin(dir))
    wind_v: float | None              # northward component (= -speed·cos(dir))
    cloud_cover_okta: float | None    # 0..8 (max layer)
    cloud_base_ft: float | None       # lowest BKN/OVC/VV base (ceiling)
    is_precip: bool = False
    precip_intensity: int = 0         # 0 none, 1 light(-), 2 moderate, 3 heavy(+)
    is_thunder: bool = False
    is_fog: bool = False
    visibility_m: float | None = None
    qnh_hpa: float | None = None
    raw_metar: str = ""
