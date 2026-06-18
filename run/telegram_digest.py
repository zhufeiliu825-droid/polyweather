"""Hourly Telegram digest — the message sent to the user after each argmax scan
(24/day work report). ALL 32 cities, one line each, SORTED BY UTC OFFSET (utc-12 → utc+14,
west→east), ties broken by the UTC peak time.

Per-line format (set by the user):
  pending          : {YYYYMMDD}{city} utc{offset} {HHMM}Z ({HHMM}当地)
  peak passed, no bet: {YYYYMMDD}{city} utc{offset} {HHMM}Z ({HHMM}当地) 未下注
  order            : {YYYYMMDD}{city} utc{offset} {HHMM}Z ({HHMM}当地) {挂单中|已成交} @{bucket}°C @{price}

  {YYYYMMDD} = the EVENT date = the city's LOCAL clock day (switches at local 01:00).
  {city}     = lowercased, spaces removed.
  {offset}   = the city's UTC offset at the peak (DST-aware): utc+1 / utc-3 / utc+5:30.
  Times: the FIRST (…Z) is the UTC peak time = the sort key (so the column reads in order); the
  parenthetical (…当地) is the city's local peak time. The list is sorted by the UTC peak time ascending.

Bet status persists until that city's local next-day 01:00 (new event). The hourly
run is scheduled a few minutes PAST the hour (never :00). This module is a PURE
RENDERER imported by run_daemon (render_digest) — not a standalone script; the
Telegram send (needs the bot token) is wired in the daemon.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def _parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%MZ").replace(tzinfo=timezone.utc)


def _offset(td) -> str:
    secs = td.total_seconds()
    sign = "+" if secs >= 0 else "-"
    secs = abs(secs)
    h, m = int(secs // 3600), int((secs % 3600) // 60)
    return f"utc{sign}{h}" if m == 0 else f"utc{sign}{h}:{m:02d}"


def render_digest(pool: dict, tz_by_station: dict, bets: dict, now: datetime) -> str:
    """bets = {station_id: {"bucket": "22", "price": 0.49, "status": "挂单中"|"已成交"}} for cities with an order.
    now = digest time (UTC). ALL 32 cities, one line each, SORTED BY UTC OFFSET (utc-12 → utc+14,
    west→east; ties by UTC peak time) so the utc±X column reads in order. Each line shows the UTC
    peak time (…Z) + the local peak time (…当地)."""
    rows = []
    for c in pool.get("cities", []) + pool.get("excluded", []):     # ALL 32, none omitted
        z = ZoneInfo(tz_by_station[c["station"]])
        peak_utc = _parse(c["strike_utc"]) + timedelta(hours=1)     # Strike = peak − 1h
        local_peak = peak_utc.astimezone(z)                         # convert to local FIRST, then read the
        off = local_peak.utcoffset()                               # DST-aware offset AT the peak instant
        ut = f"{peak_utc:%H%M}"                                    # UTC peak time, HHMM — THE SORT KEY (show it!)
        lt = f"{local_peak:%H%M}"                                   # local peak time, HHMM
        head = f"{c['market_date'].replace('-', '')}{c['city'].lower().replace(' ', '')} {_offset(off)}"
        when = f"{ut}Z ({lt}当地)"                                  # UTC first (so the column reads in sort order) + local
        b = bets.get(c["station"])
        if b is not None:                                          # 挂单中 (resting/dry) | 已成交 (filled)
            line = f"{head} {when} {b['status']} @{b['bucket']}°C @{b['price']}"
        elif peak_utc <= now:
            line = f"{head} {when} 未下注"
        else:
            line = f"{head} {when}"
        rows.append((off, peak_utc, line))                         # sort by UTC OFFSET (utc-12 → +14, W→E)

    rows.sort(key=lambda r: (r[0], r[1]))                          # offset ascending (west→east); ties by UTC peak time
    header = f"Polyweather {now:%Y-%m-%d %H:%M}Z · {len(rows)}城"
    return header + "\n" + "\n".join(line for *_k, line in rows)
