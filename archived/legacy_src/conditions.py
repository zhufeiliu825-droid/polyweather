"""Physical-environment layer: solar geometry, peak/lock timing, and weather
regime — all pure functions over a ConditionsBundle.

Anchored to verified EGLC facts: temperature peak ≈ solar noon + thermal lag,
lag ~3–4h near the solstice and shorter in winter (scaled by daylength); clouds
flatten/shift the peak; an afternoon rain event caps further warming.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..config import ConditionsConfig
from ..models import Regime
from .open_meteo import ConditionsBundle, DailySolar


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def solar_noon(s: DailySolar) -> datetime:
    return s.sunrise + (s.sunset - s.sunrise) / 2


def daylength_h(s: DailySolar) -> float:
    return (s.sunset - s.sunrise).total_seconds() / 3600.0


def thermal_lag_hours(s: DailySolar, cfg: ConditionsConfig) -> float:
    """Lag of temp peak behind solar noon, scaled by daylength. Calibrated to
    London: ~16.6h daylight (solstice) -> ~4h; ~12h (equinox) -> ~2.75h.

    DEPRECATED for peak/Strike timing (2026-06-15): the live model takes the peak
    DIRECTLY from the Open-Meteo local-day curve argmax (pure-curve), not solar+lag.
    Kept only for legacy scripts (check_timing.py, backtest_strategy.py) and the
    old monitor's peak_passed; not on the new path. `classify_regime` below IS still
    used (as a demoted diagnostic)."""
    lag = -0.51 + 0.272 * daylength_h(s)
    return _clamp(lag, cfg.lag_min_h, cfg.lag_max_h)


def expected_peak(s: DailySolar, cfg: ConditionsConfig) -> datetime:
    return solar_noon(s) + timedelta(hours=thermal_lag_hours(s, cfg))


def _daytime(bundle: ConditionsBundle, target: date) -> list:
    s = bundle.solar.get(target)
    if s is not None:
        return [h for h in bundle.hourly
                if h.time.date() == target and s.sunrise <= h.time <= s.sunset]
    return [h for h in bundle.hourly if h.time.date() == target and 8 <= h.time.hour <= 18]


def _mean(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else 0.0


def _pstd(xs: list[float]) -> float:
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def peak_window(
    bundle: ConditionsBundle, target: date, cfg: ConditionsConfig
) -> tuple[datetime, datetime] | None:
    s = bundle.solar.get(target)
    if s is None:
        return None
    exp = expected_peak(s, cfg)
    hw = cfg.peak_halfwidth_h
    clouds = [h.cloud_cover for h in _daytime(bundle, target) if h.cloud_cover is not None]
    shift = 0.0
    if clouds and _mean(clouds) >= cfg.peak_cloud_widen_pct:
        hw += cfg.peak_cloud_widen_h          # overcast -> broader plateau
        shift = 0.5                            # and tends to drift slightly later
    center = exp + timedelta(hours=shift)
    return center - timedelta(hours=hw), center + timedelta(hours=hw)


def _rain_cap(
    bundle: ConditionsBundle, target: date, after: datetime, now: datetime, cfg: ConditionsConfig
) -> bool:
    """A significant precip hour after the expected peak (and at/before now)
    signals further warming is done (downdraft/evaporative cooling)."""
    return any(
        h.time.date() == target and after < h.time <= now and (h.precip or 0.0) >= cfg.rain_cap_mm
        for h in bundle.hourly
    )


@dataclass(frozen=True)
class PeakState:
    peak_passed: bool
    expected_peak: datetime | None
    window: tuple[datetime, datetime] | None
    reason: str


def peak_passed(
    bundle: ConditionsBundle, target: date, now_local: datetime, cfg: ConditionsConfig
) -> PeakState:
    """Whether the daily max is locked. Replaces the hardcoded peak_hour_end; a
    hard floor (lock_floor_hour) guards against an early rain-cap mis-fire."""
    s = bundle.solar.get(target)
    win = peak_window(bundle, target, cfg)
    exp = expected_peak(s, cfg) if s is not None else None
    if win is None:
        return PeakState(False, exp, win, "no solar data")
    floor = datetime.combine(target, time(cfg.lock_floor_hour))
    if now_local < floor:
        return PeakState(False, exp, win, f"before floor {cfg.lock_floor_hour}:00")
    if _rain_cap(bundle, target, exp, now_local, cfg):
        return PeakState(True, exp, win, "rain-capped")
    if now_local > win[1]:
        return PeakState(True, exp, win, "after peak window")
    return PeakState(False, exp, win, "within peak window")


def classify_regime(bundle: ConditionsBundle, target: date, cfg: ConditionsConfig) -> Regime:
    day = _daytime(bundle, target)
    clouds = [h.cloud_cover for h in day if h.cloud_cover is not None]
    pp = [h.precip_prob for h in day if h.precip_prob is not None]
    precip = [h.precip for h in day if h.precip is not None]
    sw = [h.shortwave for h in day if h.shortwave is not None]
    cmean, cstd = _mean(clouds), _pstd(clouds)
    ppmax = max(pp) if pp else 0.0
    rain = sum(precip) if precip else 0.0
    swmax = max(sw) if sw else 0.0

    if ppmax >= cfg.precip_prob_convective and cstd >= cfg.cloud_std_showery and swmax >= cfg.convective_shortwave_min:
        return Regime.CONVECTIVE
    if ppmax >= cfg.precip_prob_showery or cstd >= cfg.cloud_std_showery or rain >= 1.0:
        return Regime.SHOWERY
    if cmean >= cfg.cloud_overcast_pct:
        return Regime.OVERCAST
    return Regime.CLEAR_STABLE
