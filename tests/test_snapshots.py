"""Part 2 — intraday forecast snapshots (the convergence fan)."""
import asyncio
from datetime import date, datetime, timezone

from polyweather.forecast import snapshots as S
from polyweather.runtime import analyze as A

UTC = timezone.utc
MKT = date(2026, 6, 16)


class _Stn:
    station_id = "EGLC"
    city = "London"
    latitude = 51.5
    longitude = -0.1
    timezone = "Europe/London"
    forecast_model = None            # snapshot_city passes stn.forecast_model since the 2026-06-22 model pinning


def _scan():
    return A.Scan("EGLC", "London", MKT, datetime(2026, 6, 16, 15, 0, tzinfo=UTC), 26.0)


def _tmp_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "SNAP_DIR", tmp_path / "forecast_snapshots")


# ---- store round-trip ----

def test_append_read_roundtrip(monkeypatch, tmp_path):
    _tmp_dir(monkeypatch, tmp_path)
    S.append_snapshot("EGLC", MKT, {"issue_utc": "2026-06-16T08:00Z", "peak_C": 25.9, "peak_local": "15:00"})
    S.append_snapshot("EGLC", MKT, {"issue_utc": "2026-06-16T09:00Z", "peak_C": 26.5, "peak_local": "15:15"})
    rows = S.read_snapshots("EGLC", MKT)
    assert [r["peak_C"] for r in rows] == [25.9, 26.5]          # issue order preserved


def test_read_missing_is_empty(monkeypatch, tmp_path):
    _tmp_dir(monkeypatch, tmp_path)
    assert S.read_snapshots("EGLC", MKT) == []
    assert S.fan_summary("EGLC", MKT) is None


def test_corrupt_line_skipped(monkeypatch, tmp_path):
    _tmp_dir(monkeypatch, tmp_path)
    S.SNAP_DIR.mkdir(parents=True, exist_ok=True)
    S._path("EGLC", MKT).write_text('{"peak_C": 25.9}\nNOT JSON\n{"peak_C": 26.5}\n')
    assert [r["peak_C"] for r in S.read_snapshots("EGLC", MKT)] == [25.9, 26.5]


# ---- fan summary: the convergence signal ----

def test_fan_summary_trend_and_convergence(monkeypatch, tmp_path):
    _tmp_dir(monkeypatch, tmp_path)
    for iss, pc, at, cloud in [("06:00Z", 25.9, "15:00", 60), ("10:00Z", 26.3, "15:15", 30),
                               ("13:00Z", 26.5, "15:15", 10), ("14:00Z", 26.5, "15:15", 5)]:
        S.append_snapshot("EGLC", MKT, {"issue_utc": iss, "peak_C": pc, "peak_local": at,
                                        "met_at_peak": {"cloud_pct": cloud}})
    fan = S.fan_summary("EGLC", MKT)
    assert fan["n"] == 4
    assert fan["peak_C_first"] == 25.9 and fan["peak_C_latest"] == 26.5
    assert fan["peak_C_min"] == 25.9 and fan["peak_C_max"] == 26.5
    assert fan["trend_C"] == 0.6                                # revised UP since the first issue
    assert fan["converged"] is True                            # last 3 (26.3/26.5/26.5) within 0.3
    assert fan["met_at_peak_latest"] == {"cloud_pct": 5}       # the freshest met
    assert fan["regime_latest"]["primary"] == "clear"


def test_fan_not_converged_when_still_drifting(monkeypatch, tmp_path):
    _tmp_dir(monkeypatch, tmp_path)
    for iss, pc in [("06:00Z", 24.0), ("10:00Z", 25.0), ("14:00Z", 26.5)]:
        S.append_snapshot("EGLC", MKT, {"issue_utc": iss, "peak_C": pc, "peak_local": "15:00"})
    fan = S.fan_summary("EGLC", MKT)
    assert fan["converged"] is False                           # last 3 span 2.5° → still moving
    assert fan["trend_C"] == 2.5


# ---- snapshot_city: fetch → write one record ----

def test_snapshot_city_writes_peak(monkeypatch, tmp_path):
    _tmp_dir(monkeypatch, tmp_path)

    async def _met(*a, **k):
        # market-day hourly with the max at 15:00; an adjacent day row must be ignored
        return [
            {"t": datetime(2026, 6, 16, 12, 0), "temp_C": 24.0, "cloud_pct": 40, "wind_kmh": 10},
            {"t": datetime(2026, 6, 16, 15, 0), "temp_C": 26.5, "cloud_pct": 10, "wind_kmh": 12},
            {"t": datetime(2026, 6, 17, 15, 0), "temp_C": 30.0, "cloud_pct": 0, "wind_kmh": 5},   # next day — ignore
        ]
    monkeypatch.setattr(A, "fetch_hourly_met", _met)
    rec = asyncio.run(A.snapshot_city(None, _Stn(), _scan(), datetime(2026, 6, 16, 9, 0, tzinfo=UTC)))
    assert rec["peak_C"] == 26.5 and rec["peak_local"] == "15:00"
    assert rec["issue_utc"] == "2026-06-16T09:00Z"
    assert rec["met_at_peak"]["cloud_pct"] == 10 and "t" not in rec["met_at_peak"]
    assert rec["regime"]["primary"] == "clear"
    assert S.read_snapshots("EGLC", MKT)[0]["peak_C"] == 26.5   # persisted


def test_snapshot_city_no_market_day_returns_none(monkeypatch, tmp_path):
    _tmp_dir(monkeypatch, tmp_path)

    async def _met(*a, **k):
        return [{"t": datetime(2026, 6, 17, 15, 0), "temp_C": 30.0}]   # only the next day
    monkeypatch.setattr(A, "fetch_hourly_met", _met)
    assert asyncio.run(A.snapshot_city(None, _Stn(), _scan(), datetime(2026, 6, 16, 9, 0, tzinfo=UTC))) is None
