"""NOWCAST hypothesis probe (main bot). Near the daily peak the main bot estimates the day's max as
max(realized_so_far, forecast_remaining). HYPOTHESIS: today's intraday forecast error (realized − forecast
for the hours already elapsed) predicts the remaining-day error, so shifting the remaining forecast by
β·(error-so-far) should beat that baseline. Tests it OOS over ~1yr × diverse cities at decision leads
peak−1/2/3h. If it clearly wins → build the full nowcast feature + wire into analyze.py; if parity → stop
(same discipline as the shelved generative-MOS).

  uv run python maintain/nowcast_probe.py
"""
from __future__ import annotations

import asyncio
import statistics as st
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import httpx

from polyweather.realized.metar_parse import parse_metar
from polyweather.realized.metar_source import fetch_history_iem
from polyweather.stations import load_stations

HF = "https://historical-forecast-api.open-meteo.com/v1/forecast"   # hourly forecast (near-analysis ≈ short lead)
SAMPLE = ["EGLC", "LEMD", "RKSI", "RJTT", "CYYZ", "SBGR", "WSSS", "MMMX"]


async def hourly_forecast(client, s, start, end) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = defaultdict(dict)
    cur = start
    while cur <= end:
        ce = min(cur + timedelta(days=89), end)
        r = await client.get(HF, params={"latitude": s.latitude, "longitude": s.longitude,
            "hourly": "temperature_2m", "timezone": s.timezone,
            "start_date": str(cur), "end_date": str(ce)}, timeout=60)
        r.raise_for_status()
        h = r.json().get("hourly", {})
        for t, v in zip(h.get("time", []), h.get("temperature_2m", [])):
            if v is not None:
                out[t[:10]][int(t[11:13])] = float(v)
        cur = ce + timedelta(days=1)
    return out


async def realized_hourly(client, sid, tz, start, end) -> dict[str, list[tuple[float, float]]]:
    z = ZoneInfo(tz)
    out: dict[str, list[tuple[float, float]]] = defaultdict(list)
    cur = start
    while cur <= end:
        ce = min(cur + timedelta(days=179), end)
        reps = await fetch_history_iem(client, sid, (cur - timedelta(days=1)).strftime("%Y%m%d"),
                                       (ce + timedelta(days=2)).strftime("%Y%m%d"))
        for raw, t in reps:
            try:
                o = parse_metar(raw, sid, t)
            except Exception:
                continue
            if o.temp_c is None or not (-60 <= o.temp_c <= 60):
                continue
            lt = t.astimezone(z)
            out[lt.date().isoformat()].append((lt.hour + lt.minute / 60.0, o.temp_c))
        cur = ce + timedelta(days=1)
    return out


def _nearest(realized: list[tuple[float, float]], h: float, tol: float = 0.75) -> float | None:
    best, bd = None, tol
    for rh, tp in realized:
        d = abs(rh - h)
        if d <= bd:
            best, bd = tp, d
    return best


async def main() -> None:
    stns = {s.station_id: s for s in load_stations().values()}
    start, end = date(2025, 6, 1), date(2026, 6, 15)
    rows = []  # (station, lead, realized_so_far, fc_remaining, intraday_err, actual_max)
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "pw"}) as c:
        for sid in SAMPLE:
            n0 = len(rows)
            try:
                s = stns[sid]
                fc = await hourly_forecast(c, s, start, end)
                rz = await realized_hourly(c, sid, s.timezone, start, end)
            except Exception as e:
                print(f"  {sid}: FETCH FAILED ({e!r}) — skipped", flush=True)
                continue
            for d, fch in fc.items():
                realized = rz.get(d)
                if not realized or len(fch) < 20:
                    continue
                actual_max = max(round(tp) for _, tp in realized)
                peak_hour = max(fch, key=fch.get)
                for lead in (1, 2, 3):
                    T = peak_hour - lead
                    if T < 5:
                        continue
                    rs = [tp for rh, tp in realized if rh <= T]
                    rem = [fch[hh] for hh in fch if T < hh <= 23]
                    if len(rs) < 3 or not rem:
                        continue
                    realized_so_far = max(round(tp) for tp in rs)
                    fc_remaining = max(rem)
                    errs = [rv - fch[hh] for hh in range(int(T) - 2, int(T) + 1)
                            if hh in fch and (rv := _nearest(realized, hh)) is not None]
                    if not errs:
                        continue
                    rows.append((sid, d, lead, realized_so_far, fc_remaining, st.mean(errs), actual_max))
            print(f"  {sid}: {len(rows) - n0} samples", flush=True)

    import json
    json.dump(rows, open("/tmp/nowcast_rows.json", "w"))
    # row = (station, date, lead, realized_so_far, fc_remaining, intraday_err, actual_max)
    def mae(sub, b): return st.mean(abs(max(r[3], r[4] + b * r[5]) - r[6]) for r in sub)
    def hit(sub, b): return st.mean(round(max(r[3], r[4] + b * r[5])) == r[6] for r in sub)

    print(f"\ntotal samples: {len(rows)} (saved /tmp/nowcast_rows.json)")
    for lead in (1, 2, 3):
        sub = [r for r in rows if r[2] == lead]
        b0, h0 = mae(sub, 0.0), hit(sub, 0.0)
        print(f"\n=== lead peak−{lead}h · {len(sub)}d · baseline MAE {b0:.3f} hit {h0:.3f} ===")
        for beta in (0.2, 0.3, 0.4, 0.5):
            print(f"   β={beta}: MAE {mae(sub,beta):.3f} ({(b0-mae(sub,beta))/b0*100:+.1f}%) "
                  f"hit {hit(sub,beta):.3f} ({(hit(sub,beta)-h0)*100:+.1f}pp)")

    # OUT-OF-SAMPLE: pick β on TRAIN (<2026-01), report on held-out TEST (>=2026-01), pooled
    train = [r for r in rows if r[1] < "2026-01-01"]
    test = [r for r in rows if r[1] >= "2026-01-01"]
    grid = [round(0.1 * i, 1) for i in range(8)]
    best = min(grid, key=lambda b: mae(train, b))
    print(f"\n=== OOS (train {len(train)} <2026-01 → test {len(test)} >=2026-01) ===")
    print(f"   β* picked on train = {best}")
    print(f"   test baseline:     MAE {mae(test,0.0):.3f}  hit {hit(test,0.0):.3f}")
    print(f"   test nowcast(β*):  MAE {mae(test,best):.3f} ({(mae(test,0.0)-mae(test,best))/mae(test,0.0)*100:+.1f}%)"
          f"  hit {hit(test,best):.3f} ({(hit(test,best)-hit(test,0.0))*100:+.1f}pp)")


if __name__ == "__main__":
    asyncio.run(main())
