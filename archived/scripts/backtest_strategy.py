"""Realistic decision-point backtest across cities.

For each city × recent resolved day:
  - mapping check: does our IEM full-day max fall in Polymarket's resolved bucket?
    (validates station/unit/tz -> sets trust in the city)
  - REALISTIC replay at Strike (= Solar Peak - 1h): using ONLY obs up to Strike,
    is the day's max already locked, and how much does it still rise afterwards?

No look-ahead: running_max@Strike uses only obs with time <= Strike (UTC).

    uv run python scripts/backtest_strategy.py --days 7
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timedelta

import httpx

from polyweather.discovery import parse_bucket_question
from polyweather.models import Bucket
from polyweather.realized.metar_parse import parse_metar
from polyweather.realized.metar_source import fetch_history_iem
from polyweather.realized.solar import day_sun
from polyweather.stations import load_stations
from polyweather.util.pmjson import parse_prices
from polyweather.util.timewin import local_date_of

GAMMA = "https://gamma-api.polymarket.com"


def thermal_lag_h(daylength_h: float) -> float:
    return max(2.0, min(4.5, -0.51 + 0.272 * daylength_h))


async def resolved_winners(client, series_id, n) -> dict[date, tuple]:
    """{event local date -> (comparator, threshold)} for the last n resolved events."""
    r = await client.get(f"{GAMMA}/events", params={
        "series_id": series_id, "closed": "true", "limit": n,
        "order": "endDate", "ascending": "false"})
    r.raise_for_status()
    out = {}
    for ev in r.json():
        end = ev.get("endDate", "")
        if not end:
            continue
        d = datetime.fromisoformat(end.replace("Z", "+00:00")).date()
        for m in ev.get("markets", []):
            pr = parse_prices(m.get("outcomePrices"))
            if pr and abs(pr[0] - 1.0) < 1e-9:
                try:
                    comp, thr, _ = parse_bucket_question(m.get("question", ""))
                    out[d] = (comp, thr)
                except ValueError:
                    pass
                break
    return out


async def backtest_city(client, st, days) -> dict | None:
    winners = await resolved_winners(client, st.pm_series_id, days + 2)
    if not winners:
        return None
    lo = min(winners) - timedelta(days=1)
    hi = max(winners) + timedelta(days=2)
    raws = await fetch_history_iem(client, st.station_id,
                                   lo.strftime("%Y%m%d"), hi.strftime("%Y%m%d"))
    obs = []
    for raw, t in raws:
        try:
            o = parse_metar(raw, st.station_id, t)
            if o.temp_c is not None:
                obs.append((t, o.temp_c))
        except Exception:
            pass

    rows = []
    for d, (wc, wt) in sorted(winners.items()):
        day = [(t, tp) for (t, tp) in obs if local_date_of(t.timestamp().__int__(), st.timezone) == d]
        if not day:
            continue
        final_max = max(tp for _, tp in day)
        win = Bucket(question="", comparator=wc, threshold=wt, unit=st.resolution_unit,
                     token_yes="", token_no="", condition_id="")
        mapping_ok = win.contains(final_max)

        sr, noon, ss, dl = day_sun(st.latitude, st.longitude, d, st.timezone)
        strike = noon + timedelta(hours=thermal_lag_h(dl) - 1.0)
        upto = [tp for (t, tp) in day if t <= strike]
        rm_strike = max(upto) if upto else None
        rows.append((d, final_max, mapping_ok, rm_strike,
                     (final_max - rm_strike) if rm_strike is not None else None))
    return {"rows": rows}


async def main(days: int) -> None:
    stations = load_stations()
    async with httpx.AsyncClient(timeout=60.0, headers={"User-Agent": "polyweather/0.1"}) as client:
        print(f"{'city':12}{'days':>5}{'map_ok':>8}{'locked@strike':>14}{'meanUp':>8}{'maxUp':>7}")
        for st in stations.values():
            res = await backtest_city(client, st, days)
            if not res or not res["rows"]:
                print(f"{st.city:12}  (no data)")
                continue
            rows = res["rows"]
            n = len(rows)
            mok = sum(1 for r in rows if r[2]) / n
            locked = [r for r in rows if r[3] is not None]
            lk = sum(1 for r in locked if abs(r[4]) < 1e-9) / len(locked) if locked else 0
            ups = [r[4] for r in locked if r[4] is not None]
            meanup = sum(ups) / len(ups) if ups else 0
            maxup = max(ups) if ups else 0
            print(f"{st.city:12}{n:>5}{mok:>7.0%}{lk:>13.0%}{meanup:>8.1f}{maxup:>7.0f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    asyncio.run(main(ap.parse_args().days))
