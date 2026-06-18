from datetime import datetime, timezone

from polyweather.realized.metar_parse import parse_metar

T = datetime(2026, 6, 13, 16, 20, tzinfo=timezone.utc)


def test_auto_ncd_clear():
    o = parse_metar("METAR EGLC 131620Z AUTO 27013KT 9999 NCD 22/08 Q1020", "EGLC", T)
    assert o.temp_c == 22 and o.dewpoint_c == 8
    assert o.wind_dir_deg == 270 and o.wind_speed_kt == 13
    assert o.cloud_cover_okta == 0.0          # NCD -> clear
    assert o.cloud_base_ft is None
    assert o.is_precip is False and o.precip_intensity == 0
    assert o.qnh_hpa == 1020
    assert o.rh_pct is not None and 30 < o.rh_pct < 60   # 22/08 ~ 40%


def test_overcast_ceiling():
    o = parse_metar("EGLC 112350Z AUTO 24014KT 9999 OVC015 16/14 Q1017", "EGLC", T)
    assert o.cloud_cover_okta == 8.0
    assert o.cloud_base_ft == 1500            # OVC015 = 1500 ft


def test_light_rain_layers():
    o = parse_metar("METAR EGLC 120050Z AUTO 25012KT 9999 -RA BKN014 OVC020 16/14 Q1017", "EGLC", T)
    assert o.is_precip is True and o.precip_intensity == 1   # '-RA' = light
    assert o.cloud_cover_okta == 8.0          # OVC present
    assert o.cloud_base_ft == 1400            # lowest of BKN014/OVC020


def test_wind_components_and_calm():
    o = parse_metar("EGLC 120050Z AUTO 09010KT 9999 NCD 16/14 Q1017", "EGLC", T)
    # wind FROM 090 (east) -> u negative (blowing toward west), v ~ 0
    assert o.wind_u is not None and o.wind_u < 0 and abs(o.wind_v) < 0.1
    calm = parse_metar("EGLC 120050Z AUTO 00000KT 9999 NCD 16/14 Q1017", "EGLC", T)
    assert calm.wind_u == 0.0 and calm.wind_v == 0.0


def test_thunderstorm_flag():
    o = parse_metar("EGLC 120050Z AUTO 25012KT 9999 TSRA BKN014 18/14 Q1015", "EGLC", T)
    assert o.is_thunder is True and o.is_precip is True
