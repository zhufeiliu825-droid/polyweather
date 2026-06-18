"""Verify candidate stations the same way Polymarket resolves: for each recent
CLOSED event, compute our integer-°C daily max from IEM METAR over the LOCAL day
and check it lands in the bucket Polymarket actually resolved YES.

Flips `verified` to true in config/stations.json for cities that map cleanly
(>= --threshold of recent events match). Targets tier=="candidate" rows by
default, or pass --station ICAO[,ICAO...].

    uv run python maintain/verify_new.py                 # all candidates, last 8 events
    uv run python maintain/verify_new.py --station OPMR --n 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from polyweather.discovery import parse_bucket_question
from polyweather.models import Bucket
from polyweather.realized.metar_parse import parse_metar
from polyweather.realized.metar_source import fetch_history_iem
from polyweather.util.atomicio import write_text_atomic
from polyweather.util.pmjson import parse_prices, parse_str_list

GAMMA = "https://gamma-api.polymarket.com/events"
HDR = {"User-Agent": "Mozilla/5.0 polyweather/0.1", "Accept": "application/json"}
STATIONS = Path(__file__).resolve().parents[1] / "config" / "stations.json"
_MON = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_DATE_RE = re.compile(r"on (\d{1,2}) (\w{3}) '(\d{2})")


def resolved_date(ev: dict):
    """Local market date from the rules text ('on 16 Jun '26')."""
    for m in ev.get("markets", []):
        mm = _DATE_RE.search(m.get("description", "") or "")
        if mm:
            d, mon, yy = mm.groups()
            if mon[:3].title() in _MON:
                return datetime(2000 + int(yy), _MON[mon[:3].title()], int(d)).date()
    return None


def winner(ev: dict) -> Bucket | None:
    for m in ev.get("markets", []):
        pr = parse_prices(m.get("outcomePrices"))
        # Pick the resolved YES by the `outcomes` array, not pr[0] positionally: a No-first market
        # would make a LOSING bucket (NO=1) look like the winner → wrong verification verdict (audit r8,
        # same fix as discovery._bucket_from_market). Require a clean {yes,no} pair.
        outcomes = [str(o).strip().lower() for o in parse_str_list(m.get("outcomes"))]
        if len(outcomes) != 2 or set(outcomes) != {"yes", "no"} or len(pr) < 2:
            continue
        yi = outcomes.index("yes")
        if abs(pr[yi] - 1.0) < 1e-9:                 # this bucket's YES resolved to 1 → it won
            try:
                comp, thr, unit = parse_bucket_question(m.get("question", ""))
            except ValueError:
                return None
            return Bucket(question=m.get("question", ""), comparator=comp, threshold=thr,
                          unit=unit, token_yes="", token_no="", condition_id="")
    return None


async def fetch_closed(client, series_id, n) -> list[dict]:
    r = await client.get(GAMMA, params={"series_id": series_id, "closed": "true",
                                        "limit": n, "order": "endDate", "ascending": "false"})
    r.raise_for_status()
    return r.json()


async def local_day_max(client, icao, tz, d):
    """Integer-°C max over the local day d, from IEM METAR (UTC window padded ±1d)."""
    start = (d - timedelta(days=1)).strftime("%Y%m%d")
    end = (d + timedelta(days=2)).strftime("%Y%m%d")
    reports = await fetch_history_iem(client, icao, start, end, retries=8)
    z = ZoneInfo(tz)
    temps = []
    for raw, t in reports:
        if t.astimezone(z).date() != d:
            continue
        try:
            obs = parse_metar(raw, icao, t)
        except Exception:
            continue
        if obs.temp_c is not None:
            temps.append(round(obs.temp_c))
    return (max(temps) if temps else None), len(temps)


async def verify_station(client, stn, n, sem) -> dict:
    icao, tz = stn["station_id"], stn["timezone"]
    async with sem:
        try:
            events = await fetch_closed(client, stn["pm_series_id"], n)
        except Exception as e:
            return {"city": stn["city"], "icao": icao, "error": f"gamma: {e}"}
    rows, ok = [], 0
    for ev in events:
        d = resolved_date(ev)
        win = winner(ev)
        if d is None or win is None:
            continue
        async with sem:
            mx, cnt = await local_day_max(client, icao, tz, d)
        if mx is None:
            rows.append((d, win, None, cnt, None))
            continue
        match = win.contains(mx)
        ok += int(match)
        rows.append((d, win, mx, cnt, match))
    judged = [r for r in rows if r[4] is not None]
    pct = (ok / len(judged) * 100) if judged else 0.0
    return {"city": stn["city"], "icao": icao, "tz": tz, "rows": rows,
            "judged": len(judged), "ok": ok, "pct": pct, "nodata": sum(1 for r in rows if r[3] == 0)}


async def main(targets, n, threshold, apply_) -> None:
    stations = json.loads(STATIONS.read_text())
    if targets:
        want = set(targets.split(","))
        sel = [s for s in stations if s["station_id"] in want]
    else:
        sel = [s for s in stations if s.get("tier") == "candidate"]
    print(f"verifying {len(sel)} stations, last {n} closed events each\n")

    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=60.0, headers=HDR) as client:
        results = await asyncio.gather(*[verify_station(client, s, n, sem) for s in sel])

    passed = []
    for res in sorted(results, key=lambda r: r.get("pct", -1), reverse=True):
        if res.get("error"):
            print(f"  {res['icao']} {res['city']:14} ERROR {res['error']}")
            continue
        flag = "PASS" if res["pct"] >= threshold and res["judged"] >= 3 else "fail"
        if flag == "PASS":
            passed.append(res["icao"])
        nd = f" nodata={res['nodata']}" if res["nodata"] else ""
        print(f"  [{flag}] {res['icao']} {res['city']:14} {res['ok']}/{res['judged']} "
              f"= {res['pct']:.0f}%{nd}")
        for d, win, mx, cnt, match in res["rows"]:
            sym = "✓" if match else ("·" if match is None else "✗")
            wl = f"{win.comparator.value}{win.threshold}"
            print(f"        {sym} {d}  resolved {wl:>6}  our_max={mx if mx is not None else '--':>4}  ({cnt} obs)")

    if apply_ and passed:
        for s in stations:
            if s["station_id"] in passed:
                s["verified"] = True
        write_text_atomic(STATIONS, json.dumps(stations, indent=2, ensure_ascii=False) + "\n")
        print(f"\napplied verified=true to: {', '.join(passed)}")
    else:
        print(f"\nwould pass ({len(passed)}): {', '.join(passed) or '(none)'}   "
              f"[re-run with --apply to flip verified]")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--station")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--threshold", type=float, default=85.0)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.station, a.n, a.threshold, a.apply))
