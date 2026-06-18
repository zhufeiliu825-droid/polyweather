"""Append-only per-station realized-METAR store — `data/{ICAO}_obs.jsonl`.

Accumulates the PARSED obs (temp / dewpoint / wind / cloud / …) for offline analysis. The LIVE decision
path uses a fresh AWC pull each tick (running_max) and throws the obs away; this PERSISTS what's fetched so
the realized feature history is actually kept. Deduped by `obs_time_utc`, idempotent (re-fetching an
overlapping window appends nothing new). Runs OFF the order path — a capture failure is logged, never
raised. Solar geometry + Open-Meteo enrichment are deterministic/derivable offline, so they are NOT stored
here (keep the live write cheap)."""
from __future__ import annotations

import asyncio
import json

import httpx

from ..obslog import log_failure
from .feature_store import DATA_DIR
from .metar_parse import parse_metar
from .metar_source import fetch_recent_awc

CAPTURE_HOURS = 24          # AWC window per capture — covers a full local day + restart gaps (dedup → idempotent)

_LAST: dict[str, str] = {}  # icao -> newest obs_time_utc (ISO) already persisted; in-memory cache, lazily seeded


def _path(icao: str):
    return DATA_DIR / f"{icao}_obs.jsonl"


def _last_persisted(icao: str) -> str:
    """Newest `obs_time_utc` already in the store (ISO string; '' if none / unreadable). Cached after the
    first read so we don't re-scan the file every capture."""
    if icao in _LAST:
        return _LAST[icao]
    last = ""
    p = _path(icao)
    if p.exists():
        try:
            for line in p.read_text().splitlines():
                line = line.strip()
                if line:
                    ts = str(json.loads(line).get("obs_time_utc", ""))
                    if ts > last:
                        last = ts
        except Exception:
            last = ""
    _LAST[icao] = last
    return last


async def capture_city(client: httpx.AsyncClient, icao: str, tz: str, *, hours: int = CAPTURE_HOURS) -> int:
    """Fetch AWC, parse, and append every obs NEWER than the last persisted (dedup by `obs_time_utc`).
    Returns the number of new rows appended. Reuses the same fetch+parse as `running_max_today`."""
    from zoneinfo import ZoneInfo
    reps = await fetch_recent_awc(client, icao, hours=hours)
    last = _last_persisted(icao)
    z = ZoneInfo(tz)
    rows: list[dict] = []
    newest = last
    for raw, t in sorted(reps, key=lambda r: r[1]):
        ts = t.isoformat()
        if ts <= last:                                   # already stored (or older) → skip
            continue
        try:
            o = parse_metar(raw, icao, t)
        except Exception:
            continue
        row = o.model_dump()
        row["obs_time_utc"] = ts                         # normalize to the exact ISO we dedup on
        row["local_date"] = t.astimezone(z).date().isoformat()
        rows.append(row)
        if ts > newest:
            newest = ts
    if rows:
        with _path(icao).open("a") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        _LAST[icao] = newest
    return len(rows)


async def capture_all(client: httpx.AsyncClient, stations) -> int:
    """Persist new realized obs for EVERY station concurrently. OFF the order path: one city's failure is
    logged + skipped, never raised, so obs capture can never block trading. Returns total new rows."""
    async def _one(stn) -> int:
        try:
            return await capture_city(client, stn.station_id, stn.timezone)
        except Exception as e:
            log_failure("obs_store", kind="capture_failed", detail=str(e), station=stn.station_id,
                        city=getattr(stn, "city", stn.station_id), consequence="obs not persisted this scan")
            return 0
    counts = await asyncio.gather(*[_one(s) for s in stations])
    return sum(counts)
