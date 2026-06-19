"""Latency-arb PAPER observer (READ-ONLY, places NO orders). The locked-max latency arb: once the daily max is
LOCKED (past the local peak AND the forecast can no longer beat the realized running max), the ==max bucket is
the known winner — but the market reprices it slowly (backtest: still ~0.50 for a median ~67 min). This observer,
per MVP city per run, computes realized running-max (AWC METAR) + forecast remaining-max/peak (Open-Meteo),
decides 'locked', and for the ==max bucket logs the LIVE CLOB ask + ask-depth<0.90 → data/latency_paper.jsonl.
A later offline join to settle.jsonl turns each logged row into a realized fillable edge (did the bucket win;
would buying at <=0.90 have paid; how much depth). This VALIDATES the edge is real-time exploitable in OUR
cities + measures the one thing the backtest could not (fillable size + forward false-lock) before any capital.

  uv run python maintain/latency_paper.py            # run once (append a snapshot)
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from polyweather.forecast.open_meteo import fetch_minutely_15
from polyweather.realized.feature_store import DATA_DIR
from polyweather.runtime.analyze import GAMMA, event_date, running_max_today
from polyweather.stations import load_stations

CLOB = "https://clob.polymarket.com"
OUT = DATA_DIR / "latency_paper.jsonl"
UTC = timezone.utc
# MVP cities (synthesis: widest+deepest lag windows, tradeable across UTC hours): Mexico City/Seoul/Taipei +
# wide-window Americas + the Chinese cities cmcbrown proved + Singapore/Manila.
MVP = {"MMMX", "RKSI", "RCSS", "CYYZ", "MPMG", "SBGR", "SAEZ", "ZSPD", "ZUCK", "ZSQD", "ZGGG", "WSSS", "RPLL", "RKPK"}


def _parse_bucket(m: dict) -> tuple[str, int] | None:
    """(comparator, threshold) from a Gamma market title — '27°C' (==), '30°C or above' (>=), '20 or below' (<=)."""
    t = (m.get("groupItemTitle") or m.get("question") or "")
    mm = re.search(r"(-?\d+)", t)
    if not mm:
        return None
    low = t.lower()
    comp = ">=" if ("above" in low or "higher" in low) else "<=" if ("below" in low or "lower" in low) else "=="
    return comp, int(mm.group(1))


async def _book(client: httpx.AsyncClient, token: str) -> tuple[float, float] | None:
    try:
        b = (await client.get(f"{CLOB}/book", params={"token_id": token}, timeout=15)).json()
    except Exception:
        return None
    asks = sorted((b.get("asks") or []), key=lambda a: float(a["price"]))
    if not asks:
        return None
    dep90 = round(sum(float(a["size"]) * float(a["price"]) for a in asks if float(a["price"]) < 0.90), 1)
    return float(asks[0]["price"]), dep90


async def observe_city(client: httpx.AsyncClient, stn, now: datetime) -> dict | None:
    z = ZoneInfo(stn.timezone)
    today_local = now.astimezone(z).date()
    now_local = now.astimezone(z).replace(tzinfo=None)
    try:
        M, fresh, _ = await running_max_today(client, stn.station_id, stn.timezone, today_local)
        curve = await fetch_minutely_15(client, stn.latitude, stn.longitude, stn.timezone)
    except Exception:
        return None
    if M is None:
        return None
    day = [(dt, tp) for dt, tp in curve if dt.date() == today_local]
    if not day:
        return None
    peak_dt, _ = max(day, key=lambda x: x[1])
    rem = [tp for dt, tp in day if dt > now_local]
    rem_max = max(rem) if rem else None
    past_peak = now_local > peak_dt
    # FORWARD-FALSE-LOCK GATE (paper run #1 caught a Seoul day-rollover false lock + a borderline Mexico City):
    # require a GENUINE afternoon peak that has passed, the local clock in the afternoon/evening, AND the forecast
    # remaining max strictly below the realized bucket (no path to a higher bucket). Conservative on purpose.
    locked = (past_peak and 10 <= peak_dt.hour <= 18 and 13 <= now_local.hour <= 23
              and rem_max is not None and rem_max < M)

    bucket = market_yes = ask = dep = None
    try:
        evs = (await client.get(GAMMA, params={"series_id": stn.pm_series_id, "closed": "false", "limit": 10})).json()
    except Exception:
        evs = []
    ev = next((e for e in evs if event_date(e) == today_local), None)
    if ev:
        for m in ev.get("markets", []):
            if _parse_bucket(m) == ("==", M):
                bucket = f"=={M}C"
                px = json.loads(m.get("outcomePrices") or "[]")
                market_yes = float(px[0]) if px else None
                toks = json.loads(m.get("clobTokenIds") or "[]")
                if toks and locked and (bk := await _book(client, toks[0])):
                    ask, dep = bk
                break
    return {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "city": stn.city, "station": stn.station_id,
            "date": str(today_local), "running_max": M, "fresh": fresh, "peak_local": peak_dt.strftime("%H:%M"),
            "past_peak": past_peak, "rem_forecast_max": (round(rem_max, 1) if rem_max is not None else None),
            "locked": locked, "locked_bucket": (bucket if locked else None), "market_yes": market_yes,
            "best_ask": ask, "depth_lt90": dep, "would_buy": bool(locked and ask is not None and ask <= 0.90)}


async def main() -> None:
    stns = [s for s in load_stations().values() if s.verified and s.station_id in MVP]
    now = datetime.now(UTC)
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "polyweather/0.1"}) as c:
        rows = await asyncio.gather(*[observe_city(c, s, now) for s in stns], return_exceptions=True)
    out = [r for r in rows if isinstance(r, dict)]
    with OUT.open("a") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    for r in sorted(out, key=lambda r: (r["locked"], r["depth_lt90"] or 0), reverse=True):
        flag = "LOCKED" if r["locked"] else "  --  "
        buy = "  <<< BUY" if r["would_buy"] else ""
        print(f"{flag} {r['city']:13} max={r['running_max']} peak={r['peak_local']} past={int(r['past_peak'])} "
              f"rem={r['rem_forecast_max']} | {r['locked_bucket'] or '-'} mkt={r['market_yes']} "
              f"ask={r['best_ask']} dep<.9=${r['depth_lt90']}{buy}")
    print(f"\nwrote {len(out)} rows → {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
