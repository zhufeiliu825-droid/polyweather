"""Offline tests for the pure-curve selection helpers in scripts/daily_pool.py.
Loaded via importlib since scripts/ is not an installed package."""
import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_spec = importlib.util.spec_from_file_location(
    "daily_pool", Path(__file__).resolve().parents[1] / "scripts" / "daily_pool.py")
dp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dp)

_SW = [0, 0, 0, 0, 0, 0, 50, 200, 400, 600, 700, 750, 780, 770, 740, 680, 500, 300, 120, 20, 0, 0, 0, 0]


def _hourly(temps, day="2026-06-15"):
    n = len(temps)
    return {
        "time": [f"{day}T{h:02d}:00" for h in range(n)],
        "temperature_2m": temps,
        "cloud_cover": [10] * n,
        "wind_speed_10m": [5] * n,
        "precipitation_probability": [0] * n,
        "shortwave_radiation": _SW[:n],
    }


def test_peak_from_curve_argmax():
    # unique max (26) at index 17 -> 17:00 local
    temps = [14, 14, 13, 13, 13, 14, 15, 16, 18, 19, 20, 21, 22, 23, 24, 24, 25, 26, 24, 22, 20, 18, 16, 15]
    r = dp._analyze_curve(_hourly(temps), date(2026, 6, 15), "Europe/London")
    assert r is not None and r["flat"] is False
    assert r["pred_max"] == 26
    # London is BST (UTC+1) in June -> 17:00 local == 16:00Z; Strike = peak - 1h = 15:00Z
    assert r["peak_utc"] == datetime(2026, 6, 15, 16, 0, tzinfo=timezone.utc)
    assert (r["peak_utc"].astimezone(timezone.utc).hour) == 16


def test_flat_curve_uses_nominal_afternoon():
    temps = [20.0 + (i % 2) * 0.5 for i in range(24)]   # range 0.5C < FLAT_C -> flat
    r = dp._analyze_curve(_hourly(temps), date(2026, 6, 15), "Europe/London")
    assert r is not None and r["flat"] is True
    # nominal peak = local 14:00 BST == 13:00Z
    assert r["peak_utc"] == datetime(2026, 6, 15, 13, 0, tzinfo=timezone.utc)


def test_peak_hours_median_plateau_robust():
    z = ZoneInfo("Europe/London")
    # one day, max 22C achieved at 16:00Z and 17:00Z (plateau) plus cooler obs -> median 16.5 local-hour
    base = datetime(2026, 6, 10, tzinfo=timezone.utc)
    obs = [(18.0, base.replace(hour=10)), (20.0, base.replace(hour=13)),
           (22.0, base.replace(hour=16)), (22.0, base.replace(hour=17)),
           (21.0, base.replace(hour=19)), (19.0, base.replace(hour=21))]
    hours = dp._peak_hours_from({date(2026, 6, 10): obs}, z)
    assert len(hours) == 1
    # 16:00Z & 17:00Z -> BST 17:00 & 18:00 -> median 17.5
    assert abs(hours[0] - 17.5) < 1e-6


def test_too_few_obs_skipped():
    z = ZoneInfo("Europe/London")
    base = datetime(2026, 6, 10, tzinfo=timezone.utc)
    obs = [(20.0, base.replace(hour=13)), (22.0, base.replace(hour=16))]   # <6 obs
    assert dp._peak_hours_from({date(2026, 6, 10): obs}, z) == []


def test_near_tie_uses_solar_tiebreak():
    # two near-equal peaks: midnight carry-over (00:00) vs afternoon diurnal (15:00)
    temps = [18.0] * 24
    temps[0] = 21.0
    temps[15] = 21.0
    expected = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)            # 15:00 BST
    # with the solar tie-break -> picks the afternoon peak (15:00 local = 14:00Z)
    r = dp._analyze_curve(_hourly(temps), date(2026, 6, 15), "Europe/London", expected)
    assert r["peak_utc"] == datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    assert r["pred_max"] == 21.0
    # without it -> plain argmax takes the first max (the midnight carry-over)
    r2 = dp._analyze_curve(_hourly(temps), date(2026, 6, 15), "Europe/London", None)
    assert r2["peak_utc"] == datetime(2026, 6, 14, 23, 0, tzinfo=timezone.utc)   # 00:00 BST


def test_genuine_cooling_day_keeps_midnight():
    # midnight clearly highest (diff > NEAR_TIE_C) -> NOT a near-tie -> keeps midnight
    temps = [25.0] + [18.0] * 23
    expected = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    r = dp._analyze_curve(_hourly(temps), date(2026, 6, 15), "Europe/London", expected)
    assert r["peak_utc"] == datetime(2026, 6, 14, 23, 0, tzinfo=timezone.utc)   # midnight kept
    assert r["pred_max"] == 25.0
