from polyweather.forecast.regime import classify_regime


def test_primary_regimes_and_tags():
    clear = classify_regime({"cloud_pct": 10, "precip_mm": 0, "wind_kmh": 8,
                             "rh_pct": 35, "shortwave_wm2": 700})
    assert clear["primary"] == "clear"
    assert {"clear", "dry", "strong_sun"} <= set(clear["tags"])

    rainy = classify_regime({"cloud_pct": 95, "precip_mm": 0.3, "gust_kmh": 35, "rh_pct": 90})
    assert rainy["primary"] == "rainy"
    assert {"rainy", "windy", "humid"} <= set(rainy["tags"])

    assert classify_regime({"cloud_pct": 85, "precip_mm": 0})["primary"] == "cloudy"
    assert classify_regime({"cloud_pct": 50, "precip_mm": 0})["primary"] == "mixed"


def test_missing_meteorology_has_no_regime():
    assert classify_regime(None) is None
    assert classify_regime({"temp_C": 20}) is None
