"""Unified raw-METAR sources. Both return raw report strings + their UTC obs
time, so one parser (metar_parse) handles both.

- IEM ASOS: historical archive (free, no key). 503-retry (IEM over-capacity).
- AWC (NOAA): recent reports (free, no key) — the live running-max source.
"""
from __future__ import annotations

import asyncio
import csv
import io
import re
from datetime import datetime, timedelta, timezone

import httpx

IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
AWC = "https://aviationweather.gov/api/data/metar"  # NOAA Aviation Weather Center


async def fetch_history_iem(
    client: httpx.AsyncClient,
    station_id: str,
    start_yyyymmdd: str,
    end_yyyymmdd: str,
    *,
    retries: int = 5,
) -> list[tuple[str, datetime]]:
    """All raw reports over [start, end) as (raw_metar, obs_time_utc)."""
    s = datetime.strptime(start_yyyymmdd, "%Y%m%d")
    e = datetime.strptime(end_yyyymmdd, "%Y%m%d")
    params = {
        "station": station_id, "data": "metar",
        "year1": s.year, "month1": s.month, "day1": s.day,
        "year2": e.year, "month2": e.month, "day2": e.day,
        "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no",
        "missing": "M", "trace": "T", "direct": "no",
        "report_type": ["3", "4"],   # routine + special
    }
    text = ""
    for attempt in range(retries):
        resp = await client.get(IEM, params=params)
        if resp.status_code == 200 and len(resp.text) > 50 and not resp.text.startswith("ERROR"):
            text = resp.text
            break
        if attempt < retries - 1:
            await asyncio.sleep(2.0)   # IEM over-capacity is transient

    out: list[tuple[str, datetime]] = []
    for row in csv.DictReader(io.StringIO(text)):
        raw = (row.get("metar") or "").strip()
        valid = (row.get("valid") or "").strip()
        if not raw or raw == "M" or not valid:
            continue
        try:
            t = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out.append((raw, t))
    return out


def _reconstruct_utc(now: datetime, dd: int, hh: int, mm: int) -> datetime | None:
    """METAR carries only day-of-month + HHMM; anchor it to the nearest past
    date relative to `now` (handles month/year rollover)."""
    for off in (0, -1):
        yr, mo = now.year, now.month + off
        if mo < 1:
            yr, mo = yr - 1, 12
        try:
            t = datetime(yr, mo, dd, hh, mm, tzinfo=timezone.utc)
        except ValueError:
            continue
        if t <= now + timedelta(minutes=90):   # small slack for clock skew
            return t
    return None


async def fetch_recent_awc(
    client: httpx.AsyncClient, station_id: str, *, hours: int = 24, now: datetime | None = None
) -> list[tuple[str, datetime]]:
    """Full recent METAR series (routine + SPECI) from NOAA AWC — free, no key,
    not quota-limited. Use this (not the CheckWX latest-only snapshot) to build the
    running daily max LIVE, then recompute max over the COMPLETE series each tick so
    no intermediate/SPECI report is missed. Returns [(raw, obs_time_utc)] sorted by
    time, same shape as the other sources (one parser serves all)."""
    r = await client.get(AWC, params={"ids": station_id, "format": "raw", "hours": hours})
    r.raise_for_status()
    now = now or datetime.now(timezone.utc)
    out: list[tuple[str, datetime]] = []
    for line in r.text.splitlines():
        raw = re.sub(r"^(METAR|SPECI)\s+", "", line.strip())
        if not raw.startswith(station_id):
            continue
        m = re.search(r"\b(\d{2})(\d{2})(\d{2})Z\b", raw)
        if not m:
            continue
        dd, hh, mm = (int(x) for x in m.groups())
        t = _reconstruct_utc(now, dd, hh, mm)
        if t is not None:
            out.append((raw, t))
    out.sort(key=lambda x: x[1])
    return out
