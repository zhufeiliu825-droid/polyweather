"""End-to-end DRY_RUN for ONE city — thin CLI over runtime.analyze.analyze_city:

  Gamma event → buckets+tokens · AWC → running_max (full local day, freshness) ·
  Open-Meteo curve → peak/mkt_day + raw forecast peak · the agent chooses the bucket
  (or round(peak) fallback) → validate_proposal/decide() → BET/ESCALATE ·
  BET → clob_executor (DRY_RUN only).

This is an INSPECTION / DRY tool — it NEVER submits a live order even with POLY_DRY_RUN=0 (the
daemon is the sole live placer, with its single-instance lock + idempotent state). Running it live
would otherwise place a 2nd untracked order alongside the daemon (audit), so it's blocked here.

    uv run python run/run_once.py --station EGLC
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

import httpx

from polyweather import clob_executor as ex
from polyweather.runtime.analyze import analyze_city
from polyweather.stations import load_stations

HDR = {"User-Agent": "Mozilla/5.0 polyweather/0.1"}


async def main(station_id: str) -> None:
    stn = load_stations()[station_id]
    now = datetime.now(timezone.utc)
    async with httpx.AsyncClient(timeout=40, headers=HDR) as client:
        t = await analyze_city(client, stn, now)

    print(f"=== {t.city} ({t.station_id})  now={now:%Y-%m-%d %H:%MZ}  mkt_day={t.mkt_day} ===")
    peak = f"{t.peak_utc:%H:%MZ}" if t.peak_utc else "—"
    print(f"forecast   : peak {t.predicted_max}°C @ {peak} · forecast_peak(raw)="
          f"{None if t.remaining is None else round(t.remaining, 1)}")
    print(f"AWC running_max today: {t.running_max}°C  fresh={t.fresh}  (dead-bucket floor)")
    if t.note:
        print(f"note       : {t.note}")
    d = t.decision
    if d is None:
        print("decide → None (peak passed / forecast below realized / no data)")
        return
    print("bucket ladder:", [(r["b"], r["mkt"], "alive" if r["alive"] else "dead") for r in t._dist()[:5]])
    print(f"DECISION   : {d.action}  bucket={d.label}  conf={d.confidence}  "
          f"mkt={d.market_yes} maker={d.maker_price} health={d.health_ok}")
    print(f"reason     : {d.reason}")
    if d.action == "BET":
        if not ex._dry_run():        # NEVER place live from run_once — only the daemon (locked + stateful) does
            print(f"EXECUTOR   : would BET {d.label} @ {d.maker_price} ({d.size_shares} sh) — but run_once does NOT "
                  f"submit live orders; the daemon is the sole live placer. No order sent.")
        else:
            res = ex.place_from_decision(d, now_iso=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            print(f"EXECUTOR   : {res.detail}")
    else:
        print("EXECUTOR   : (ESCALATE → Telegram; not placed)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", default="EGLC")
    asyncio.run(main(ap.parse_args().station))
