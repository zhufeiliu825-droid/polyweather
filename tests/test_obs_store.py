import asyncio
import json
from datetime import datetime, timezone

from polyweather.realized import obs_store

UTC = timezone.utc
RAW = "EGLC 171000Z AUTO 28007KT 9999 SCT044 18/08 Q1020"     # 18°C / dewpt 8


class _Stn:
    def __init__(self, sid, tz="Europe/London"):
        self.station_id, self.timezone, self.city = sid, tz, sid.lower()


def test_capture_appends_parses_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setattr(obs_store, "DATA_DIR", tmp_path)
    obs_store._LAST.clear()
    t1 = datetime(2026, 6, 17, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 6, 17, 10, 30, tzinfo=UTC)
    raw2 = "EGLC 171030Z AUTO 28008KT 9999 SCT044 19/08 Q1020"

    async def _fetch(client, icao, hours=24):
        return [(raw2, t2), (RAW, t1)]                        # UNSORTED → capture must order by time
    monkeypatch.setattr(obs_store, "fetch_recent_awc", _fetch)

    n = asyncio.run(obs_store.capture_city(None, "EGLC", "Europe/London"))
    assert n == 2
    lines = (tmp_path / "EGLC_obs.jsonl").read_text().splitlines()
    assert len(lines) == 2
    r0 = json.loads(lines[0])                                 # written in time order → 10:00 first
    assert r0["station_id"] == "EGLC" and r0["temp_c"] == 18.0
    assert r0["local_date"] == "2026-06-17" and r0["obs_time_utc"].startswith("2026-06-17T10:00")
    assert r0["raw_metar"].startswith("EGLC 171000Z")        # the full parsed feature row is persisted

    # re-fetch the SAME window → dedup by obs_time_utc → nothing new, file unchanged
    assert asyncio.run(obs_store.capture_city(None, "EGLC", "Europe/London")) == 0
    assert len((tmp_path / "EGLC_obs.jsonl").read_text().splitlines()) == 2


def test_dedup_survives_a_cold_cache(tmp_path, monkeypatch):
    # a daemon restart drops the in-memory cache; dedup must re-seed from the FILE, not re-append everything
    monkeypatch.setattr(obs_store, "DATA_DIR", tmp_path)
    obs_store._LAST.clear()
    t1 = datetime(2026, 6, 17, 10, 0, tzinfo=UTC)

    async def _fetch(client, icao, hours=24):
        return [(RAW, t1)]
    monkeypatch.setattr(obs_store, "fetch_recent_awc", _fetch)
    assert asyncio.run(obs_store.capture_city(None, "EGLC", "Europe/London")) == 1
    obs_store._LAST.clear()                                   # simulate a restart (cache gone, file remains)
    assert asyncio.run(obs_store.capture_city(None, "EGLC", "Europe/London")) == 0   # re-seeds from file → no dup


def test_capture_all_isolates_per_city_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(obs_store, "DATA_DIR", tmp_path)
    obs_store._LAST.clear()
    t1 = datetime(2026, 6, 17, 10, 0, tzinfo=UTC)

    async def _fetch(client, icao, hours=24):
        if icao == "BOOM":
            raise RuntimeError("awc down")
        return [(RAW, t1)]
    monkeypatch.setattr(obs_store, "fetch_recent_awc", _fetch)
    monkeypatch.setattr(obs_store, "log_failure", lambda *a, **k: None)

    total = asyncio.run(obs_store.capture_all(None, [_Stn("RKSI"), _Stn("BOOM"), _Stn("EGLC")]))
    assert total == 2                                         # RKSI + EGLC persisted; BOOM failed but didn't break the batch
    assert (tmp_path / "RKSI_obs.jsonl").exists() and (tmp_path / "EGLC_obs.jsonl").exists()
    assert not (tmp_path / "BOOM_obs.jsonl").exists()
