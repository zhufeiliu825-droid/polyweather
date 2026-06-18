"""Load TOML config into validated models."""
from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class CityConfig(BaseModel):
    name: str
    wu_station: str
    country_code: str
    location_key: str
    latitude: float
    longitude: float
    timezone: str
    unit: str            # "C" | "F"
    pm_ticker: str
    series_id: int | None = None   # Gamma series id, for cheap per-city event fetch
    tier: str = "breadth"
    verified: bool = False


class Defaults(BaseModel):
    wu_api_key: str
    temp_tag_id: int = 104596


class SignalThresholds(BaseModel):
    dead_bucket_min_price: float = 0.02
    locked_cumulative_gap: float = 0.03
    locked_single_max_price: float = 0.95
    min_liquidity: float = 5000.0
    devig_sum_lo: float = 0.9
    devig_sum_hi: float = 1.15
    wu_staleness_sec: int = 1200
    peak_hour_end: int = 18
    upside_edge_min: float = 0.10   # fire UPSIDE if |forecast_p - market_p| >= this
    sigma_floor: float = 0.5        # min forecast-error std (°)
    bias_days: int = 7              # days of forecast-vs-METAR residuals for bias/sigma


class ConditionsConfig(BaseModel):
    use_conditions: bool = True
    lock_floor_hour: int = 16          # never lock earlier than this local hour
    lag_min_h: float = 2.0
    lag_max_h: float = 4.5
    peak_halfwidth_h: float = 1.5
    peak_cloud_widen_h: float = 1.0
    peak_cloud_widen_pct: float = 70.0
    rain_cap_mm: float = 0.5
    cloud_overcast_pct: float = 75.0
    cloud_std_showery: float = 25.0
    precip_prob_showery: float = 40.0
    precip_prob_convective: float = 60.0
    convective_shortwave_min: float = 300.0
    shrinkage_k: float = 8.0           # pseudo-obs for σ shrinkage to global
    fit_days: int = 90


class AppConfig(BaseModel):
    defaults: Defaults
    cities: dict[str, CityConfig]
    thresholds: SignalThresholds
    conditions: ConditionsConfig


@lru_cache(maxsize=1)
def load_config(config_dir: str | None = None) -> AppConfig:
    d = Path(config_dir) if config_dir else CONFIG_DIR
    cities_raw = tomllib.loads((d / "cities.toml").read_text())
    signals_raw = tomllib.loads((d / "signals.toml").read_text())

    defaults = Defaults(**cities_raw["defaults"])
    cities = {
        name: CityConfig(name=name, **conf)
        for name, conf in cities_raw.get("cities", {}).items()
    }
    merged = {**signals_raw.get("thresholds", {}), **signals_raw.get("upside", {})}
    thresholds = SignalThresholds(**merged)
    conditions = ConditionsConfig(**signals_raw.get("conditions", {}))
    return AppConfig(
        defaults=defaults, cities=cities, thresholds=thresholds, conditions=conditions
    )
