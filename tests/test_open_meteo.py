import asyncio
from datetime import date

from polyweather.forecast import open_meteo as om


class _Resp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class _CaptureClient:
    def __init__(self, data):
        self._d, self.url, self.params = data, None, None

    async def get(self, url, params=None, timeout=None):
        self.url, self.params = url, params
        return _Resp(self._d)


def test_previous_runs_uses_free_endpoint_no_apikey(monkeypatch):
    # previous-runs is FREE/open-access (not in Standard) → must NOT send apikey even with the paid key set
    monkeypatch.setenv("OPEN_METEO_API_KEY", "k_paid")
    c = _CaptureClient({"hourly": {"time": ["2026-06-16T17:00", "2026-06-16T18:00"],
                                   "temperature_2m": [30.0, 29.0],
                                   "temperature_2m_previous_day1": [33.2, 32.0]}})
    out = asyncio.run(om.fetch_previous_runs(c, 51.5, -0.1, "Europe/London", date(2026, 6, 16)))
    assert c.url == om.PREV_RUNS and "apikey" not in (c.params or {})
    assert out["peak_C_max"] == 33.2 and out["peak_C_min"] == 30.0          # latest 30 @17:00, day1 33.2 @17:00
    assert {r["run"] for r in out["runs"]} == {"latest", "day1"}


def test_previous_runs_empty_on_failure():
    class _Boom:
        async def get(self, *a, **k):
            raise RuntimeError("down")
    assert asyncio.run(om.fetch_previous_runs(_Boom(), 51.5, -0.1, "Europe/London", date(2026, 6, 16))) == {}


def test_minutely15_uses_forecast_endpoint_paid(monkeypatch):
    # minutely_15 is the Forecast API → paid-routed when the key is set (like fetch_hourly)
    monkeypatch.setenv("OPEN_METEO_API_KEY", "k_paid")
    c = _CaptureClient({"minutely_15": {"time": ["2026-06-16T16:30"], "temperature_2m": [29.3]}})
    out = asyncio.run(om.fetch_minutely_15(c, 51.5, -0.1, "Europe/London"))
    assert c.url == om.OPEN_METEO_PAID and c.params.get("apikey") == "k_paid"
    assert out and out[0][1] == 29.3


def test_forecast_endpoint_free_by_default(monkeypatch):
    # no key → free open-access endpoint, no apikey param (the historical maintainers also stay free)
    monkeypatch.delenv("OPEN_METEO_API_KEY", raising=False)
    url, auth = om._forecast_endpoint()
    assert url == om.OPEN_METEO_FREE and auth == {}


def test_forecast_endpoint_paid_with_key(monkeypatch):
    # OPEN_METEO_API_KEY set (paid Standard+) → commercial customer endpoint + apikey (no rate cap)
    monkeypatch.setenv("OPEN_METEO_API_KEY", "k_123")
    url, auth = om._forecast_endpoint()
    assert url == om.OPEN_METEO_PAID and auth == {"apikey": "k_123"}
    assert "customer-" in url
