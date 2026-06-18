from datetime import date, datetime

from polyweather.config import ConditionsConfig
from polyweather.forecast.conditions import (
    classify_regime,
    expected_peak,
    peak_passed,
    solar_noon,
    thermal_lag_hours,
)
from polyweather.forecast.open_meteo import ConditionsBundle, DailySolar, HourlyConditions
from polyweather.models import Regime

CFG = ConditionsConfig()
TARGET = date(2026, 6, 13)


def _solar(sr_h, sr_m, ss_h, ss_m):
    return DailySolar(
        day=TARGET,
        sunrise=datetime(2026, 6, 13, sr_h, sr_m),
        sunset=datetime(2026, 6, 13, ss_h, ss_m),
    )


def _bundle(hours, sr=(4, 42), ss=(21, 17)):
    # hours: (hour, temp, cloud, precip, precip_prob, shortwave)
    hourly = [
        HourlyConditions(time=datetime(2026, 6, 13, h), temp_c=t, cloud_cover=c,
                         precip=p, precip_prob=pp, shortwave=sw)
        for (h, t, c, p, pp, sw) in hours
    ]
    return ConditionsBundle(hourly=hourly, solar={TARGET: _solar(*sr, *ss)})


def test_solar_noon_is_sunrise_sunset_midpoint():
    # verified EGLC 2026-06-13: sunrise 04:42, sunset 21:17 -> noon 12:59:30
    assert solar_noon(_solar(4, 42, 21, 17)) == datetime(2026, 6, 13, 12, 59, 30)


def test_thermal_lag_scales_with_daylength():
    solstice = _solar(4, 42, 21, 17)   # ~16.6h daylight
    equinox = _solar(6, 0, 18, 0)      # 12h daylight
    assert abs(thermal_lag_hours(solstice, CFG) - 4.0) < 0.2
    assert 2.5 <= thermal_lag_hours(equinox, CFG) <= 3.0


def test_expected_peak_today_is_late_afternoon():
    # matches the verified ~17:00 temperature peak
    assert expected_peak(_solar(4, 42, 21, 17), CFG).hour in (16, 17)


def test_peak_not_passed_before_floor():
    b = _bundle([(h, 18, 50, 0, 0, 500) for h in range(8, 22)])
    ps = peak_passed(b, TARGET, datetime(2026, 6, 13, 14, 0), CFG)
    assert ps.peak_passed is False and "floor" in ps.reason


def test_peak_passed_after_window():
    b = _bundle([(h, 18, 50, 0, 0, 500) for h in range(8, 22)])
    ps = peak_passed(b, TARGET, datetime(2026, 6, 13, 20, 0), CFG)
    assert ps.peak_passed is True


def test_rain_cap_locks_after_peak():
    hours = [(h, 18, 50, (3.0 if h == 17 else 0.0), 0, 500) for h in range(8, 22)]
    ps = peak_passed(_bundle(hours), TARGET, datetime(2026, 6, 13, 18, 0), CFG)
    assert ps.peak_passed is True and "rain" in ps.reason


def test_regime_clear_stable():
    b = _bundle([(h, 21, 20, 0, 0, 700) for h in range(8, 19)])
    assert classify_regime(b, TARGET, CFG) is Regime.CLEAR_STABLE


def test_regime_overcast():
    b = _bundle([(h, 16, 95, 0, 10, 200) for h in range(8, 19)])
    assert classify_regime(b, TARGET, CFG) is Regime.OVERCAST


def test_regime_showery():
    hours = [(h, 17, (20 if h % 2 else 90), 0.2, 50, 400) for h in range(8, 19)]
    assert classify_regime(_bundle(hours), TARGET, CFG) is Regime.SHOWERY


def test_regime_convective():
    hours = [(h, 24, (30 if h % 2 else 95), 0.5, 70, 500) for h in range(8, 19)]
    assert classify_regime(_bundle(hours), TARGET, CFG) is Regime.CONVECTIVE
