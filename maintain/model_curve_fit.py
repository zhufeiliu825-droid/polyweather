"""Full-day hourly CURVE-FIT scan — rank models by how well their whole diurnal
curve matches the real station, not just the daily max (a model can luck into the
peak while running cold all day; the daily max hides that).

Truth: actual hourly METAR (IEM ASOS, same source as the resolution max), aligned to
each local hour. Forecast: historical-forecast-api hourly (lead~0). For each (city,model)
over a ~90d window we report hourly MAE/RMSE/bias/correlation, AND split by regime
(hot-tercile vs cool-tercile days) to expose marine-cold grids (cold ALL day, worst on
hot days) — the failure the daily max understates.

READ-ONLY. Resumable (append per (station,model), skip done). 429-aware -> stop + report.

  uv run python maintain/model_curve_fit.py --stations RKSI,RJTT,... --out /tmp/curve.jsonl
  uv run python maintain/model_curve_fit.py --summary /tmp/curve.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics as st
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from polyweather.realized.metar_parse import parse_metar
from polyweather.realized.metar_source import fetch_history_iem
from polyweather.stations import load_stations

HF = "https://historical-forecast-api.open-meteo.com/v1/forecast"
GLOBAL_MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless", "gem_seamless",
                 "jma_seamless", "ukmo_seamless", "meteofrance_seamless"]
# regional high-res to ALSO test, only on the station(s) it covers
REGIONAL = {
    "LIMC": ["italia_meteo_arpae_icon_2i"],
    "EFHK": ["metno_nordic"],
    "LFPB": ["meteofrance_arome_france_hd"],
    "CYYZ": ["gem_hrdps_continental"],
    "EDDM": ["meteoswiss_icon_ch2", "dmi_harmonie_arome_europe"],
}


async def actual_hourly(client, icao, tz, d0, d1) -> dict[str, float]:
    """{'YYYY-MM-DDTHH'(local, nearest obs to the hour mark): temp_c}."""
    Z = ZoneInfo(tz)
    end = (date.fromisoformat(d1) + timedelta(days=1)).strftime("%Y%m%d")
    reps = await fetch_history_iem(client, icao, d0.replace("-", ""), end)
    by: dict[str, tuple[float, float]] = {}
    for raw, t in reps:
        try:
            o = parse_metar(raw, icao, t)
        except Exception:
            continue
        if o.temp_c is None or not (-60 < o.temp_c < 60):
            continue
        lt = t.astimezone(Z)
        hr = (lt + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
        dist = abs((lt - hr).total_seconds()) / 60
        key = hr.strftime("%Y-%m-%dT%H")
        if key not in by or dist < by[key][0]:
            by[key] = (dist, round(o.temp_c, 1))
    return {k: v[1] for k, v in by.items()}


async def model_hourly(client, lat, lon, tz, model, d0, d1, retries=4) -> dict[str, float]:
    p = {"latitude": lat, "longitude": lon, "hourly": "temperature_2m",
         "start_date": d0, "end_date": d1, "timezone": tz, "models": model}
    backoff = 5.0
    for attempt in range(retries):
        try:
            r = await client.get(HF, params=p, timeout=60)
        except (httpx.TimeoutException, httpx.TransportError):
            await asyncio.sleep(backoff); backoff *= 2; continue
        if r.status_code == 429:
            if attempt == retries - 1:
                raise RuntimeError("429")
            await asyncio.sleep(backoff); backoff *= 2; continue
        h = r.json().get("hourly", {})
        return {t[:13]: v for t, v in zip(h.get("time", []), h.get("temperature_2m", [])) if v is not None}
    raise RuntimeError("429")


def curve_score(actual: dict, mh: dict):
    def daymax(d):
        out = defaultdict(lambda: -99.0)
        for k, v in d.items():
            out[k[:10]] = max(out[k[:10]], v)
        return out
    a_dm = daymax(actual)
    days = sorted(a_dm)
    if len(days) < 20:
        return None
    sd = sorted(days, key=lambda d: a_dm[d])
    n3 = max(1, len(sd) // 3)
    cool, hot = set(sd[:n3]), set(sd[-n3:])
    pairs = [(mh[k], actual[k], k[:10]) for k in actual if k in mh]
    if len(pairs) < 200:
        return None

    def sub(sel):
        e = [abs(f - a) for f, a, d in pairs if sel(d)]
        b = [f - a for f, a, d in pairs if sel(d)]
        return (round(st.mean(e), 2), round(st.mean(b), 2)) if e else (None, None)

    mae, bias = sub(lambda d: True)
    b_all = [f - a for f, a, _ in pairs]
    rmse = round((sum(x * x for x in b_all) / len(b_all)) ** 0.5, 2)
    r = round(st.correlation([f for f, _, _ in pairs], [a for _, a, _ in pairs]), 3)
    hot_mae, hot_bias = sub(lambda d: d in hot)
    cool_mae, cool_bias = sub(lambda d: d in cool)
    m_dm = daymax(mh)
    dmp = [(m_dm[d], a_dm[d]) for d in a_dm if d in m_dm]
    dm_mae = round(st.mean([abs(f - a) for f, a in dmp]), 2)
    dm_bias = round(st.mean([f - a for f, a in dmp]), 2)
    return {"n_hours": len(pairs), "mae": mae, "rmse": rmse, "bias": bias, "r": r,
            "mae_hot": hot_mae, "bias_hot": hot_bias, "mae_cool": cool_mae, "bias_cool": cool_bias,
            "daymax_mae": dm_mae, "daymax_bias": dm_bias}


def done_keys(out: Path) -> set:
    if not out.exists():
        return set()
    return {(json.loads(l)["station"], json.loads(l)["model"])
            for l in out.read_text().splitlines() if l.strip()}


async def run(args):
    stations = load_stations()
    only = set(args.stations.split(",")) if args.stations else None
    sids = [s for s in stations if (only is None or s in only)]
    end = date(2026, 6, 20)
    d0, d1 = (end - timedelta(days=args.days - 1)).isoformat(), end.isoformat()
    out = Path(args.out)
    done = done_keys(out)
    fh = out.open("a")
    print(f"curve-fit: {len(sids)} cities, {d0}->{d1} | concurrency {args.concurrency}", flush=True)

    async with httpx.AsyncClient(headers={"User-Agent": "polyweather-curvefit/1"}) as client:
        for sid in sids:
            stn = stations[sid]
            models = GLOBAL_MODELS + REGIONAL.get(sid, [])
            todo = [m for m in models if (sid, m) not in done]
            if not todo:
                continue
            try:
                act = await actual_hourly(client, sid, stn.timezone, d0, d1)
            except Exception as e:
                print(f"  {stn.city}: IEM fetch failed ({e}); skipping", flush=True)
                continue
            if len(act) < 200:
                print(f"  {stn.city}: only {len(act)} actual hours; skipping", flush=True)
                continue

            sem = asyncio.Semaphore(args.concurrency)
            stop = asyncio.Event()

            async def one(model):
                if stop.is_set():
                    return
                async with sem:
                    try:
                        mh = await model_hourly(client, stn.latitude, stn.longitude, stn.timezone, model, d0, d1)
                    except RuntimeError:
                        stop.set(); return
                    sc = curve_score(act, mh)
                    rec = {"station": sid, "city": stn.city, "model": model,
                           **(sc or {"mae": None, "note": "insufficient"})}
                    fh.write(json.dumps(rec) + "\n"); fh.flush()

            await asyncio.gather(*(one(m) for m in todo))
            if stop.is_set():
                fh.close()
                print(f"\n*** RATE LIMITED (429) at {stn.city} *** switch VPN and re-run the SAME "
                      f"command — it resumes from {out}.", flush=True)
                return
            print(f"  {stn.city}: {len(act)} actual hrs, {len(todo)} models done", flush=True)
    fh.close()
    print(f"\nDONE. Summary: uv run python maintain/model_curve_fit.py --summary {out}", flush=True)


def summary(path):
    rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    by_city = defaultdict(dict)
    order = []
    for r in rows:
        if r["city"] not in by_city:
            order.append(r["city"])
        if r.get("mae") is not None:
            by_city[r["city"]][r["model"]] = r
    print(f"{'city':12}{'best(curve)':22}{'MAE':6}{'RMSE':6}{'bias':7}{'r':7}"
          f"{'热天MAE/偏差':14}{'凉天MAE/偏差':14}{'日峰MAE':8}")
    for city in order:
        ms = by_city[city]
        if not ms:
            continue
        best = min(ms.values(), key=lambda r: r["mae"])
        hot = f"{best['mae_hot']}/{best['bias_hot']:+}" if best.get('mae_hot') is not None else "-"
        cool = f"{best['mae_cool']}/{best['bias_cool']:+}" if best.get('mae_cool') is not None else "-"
        print(f"{city:12}{best['model']:22}{best['mae']:<6}{best['rmse']:<6}{best['bias']:<+7}"
              f"{best['r']:<7}{hot:14}{cool:14}{best['daymax_mae']:<8}")
    # full per-city model ranking
    print("\n--- 每城各模型逐小时 MAE(↑越小越好)---")
    for city in order:
        ms = by_city[city]
        if not ms:
            continue
        rk = sorted(ms.values(), key=lambda r: r["mae"])
        print(f"{city:12} " + "  ".join(f"{r['model'].replace('_seamless','').replace('_',''):10}{r['mae']}" for r in rk))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--stations", default="")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default="/tmp/curve.jsonl")
    ap.add_argument("--summary", default="")
    args = ap.parse_args()
    if args.summary:
        summary(args.summary)
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
