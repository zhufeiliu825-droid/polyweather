from datetime import date, timedelta

from polyweather.realized.solar import day_sun, solar_elevation_deg

LAT, LON, ELEV = 51.5053, 0.05528, 6.0  # EGLC


def test_daylength_near_solstice():
    _sr, _noon, _ss, dl = day_sun(LAT, LON, date(2026, 6, 13), "Europe/London")
    assert 15.5 < dl < 17.0          # London ~16.6h near solstice


def test_elevation_peaks_at_solar_noon():
    _sr, noon, _ss, _dl = day_sun(LAT, LON, date(2026, 6, 13), "Europe/London")
    e_noon = solar_elevation_deg(LAT, LON, ELEV, noon)
    e_morning = solar_elevation_deg(LAT, LON, ELEV, noon - timedelta(hours=4))
    assert e_noon > e_morning
    assert 55 < e_noon < 65          # ~62° at London summer solar noon


def test_elevation_near_zero_at_sunrise():
    sr, _noon, _ss, _dl = day_sun(LAT, LON, date(2026, 6, 13), "Europe/London")
    assert abs(solar_elevation_deg(LAT, LON, ELEV, sr)) < 3.0
