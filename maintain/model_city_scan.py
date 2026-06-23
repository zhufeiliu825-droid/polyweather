"""Per-city model+point scan — find the best OpenMeteo model (and forecast point)
for each verified station, scored against the METAR resolution truth.

WHY: `best_match` resolves to a different underlying model per location AND per API
(Seoul -> JMA on forecast/historical, GFS on single-runs), and the picked model is
often a coarse, marine-cold-prone grid (JMA/GFS @ coastal airports). To pin a stable,
correct model per city we measure every candidate model's lead~0 daily-max against the
archived METAR integer max (the same parse/round/clamp the market resolves on).

Truth: data/leadtime_residuals.jsonl `actual_max` (IEM METAR integer max), all 32 stns.
Forecast: historical-forecast-api (FREE, no key, lead~0 stitch) — isolates the model's
grid/representation bias, which is exactly the coordinate/marine-cold question.

READ-ONLY. Resumable: appends per (station,model,point) to the out jsonl, skips done on
re-run (so a 429 -> VPN-switch -> re-run continues). On persistent 429 it stops and says so.

  uv run python maintain/model_city_scan.py --days 365 --out /tmp/model_scan_p1.jsonl
  uv run python maintain/model_city_scan.py --summary /tmp/model_scan_p1.jsonl   # print table
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics as st
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import httpx

from polyweather.stations import load_stations

HF = "https://historical-forecast-api.open-meteo.com/v1/forecast"
RESID = Path("data/leadtime_residuals.jsonl")
DEFAULT_MODELS = [
    "best_match", "ecmwf_ifs025", "ecmwf_aifs025", "icon_seamless", "gfs_seamless",
    "gem_seamless", "jma_seamless", "ukmo_seamless", "meteofrance_seamless",
    "cma_grapes_global",
]
# REGIONAL high-res models -> ONLY the verified stations they actually cover (per the
# provider region table). US-only models (HRRR/NAM/NBM) cover none of our 32 cities -> omitted.
REGIONAL = {
    "italia_meteo_arpae_icon_2i": ["LIMC"],                  # Southern Europe 2km
    "knmi_harmonie_arome_netherlands": ["EHAM"],             # NL/BE 2km
    "metno_nordic": ["EFHK"],                                # Nordics 1km
    "meteofrance_arome_france_hd": ["LFPB"],                 # France 1.5km
    "gem_hrdps_continental": ["CYYZ"],                       # Canada/N.US 2.5km
    "dmi_harmonie_arome_europe": ["EDDM", "EPWA", "EHAM"],   # Central/N Europe
    "meteoswiss_icon_ch2": ["EDDM"],                         # Central Europe 2km (since 2025-07)
    "bom_access_global": ["NZWN", "SBGR", "SAEZ", "FACT"],   # global, SH-tuned
}


def load_actuals() -> dict[str, dict[str, int]]:
    """station -> {date -> actual_max}. (residual rows repeat actual per lead; dedupe.)"""
    act: dict[str, dict[str, int]] = defaultdict(dict)
    for ln in RESID.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        r = json.loads(ln)
        act[r["station"]][r["date"]] = r["actual_max"]
    return act


async def fetch_daymax(client, lat, lon, tz, model, d0, d1, retries=4):
    """Return {date -> daymax_float} or raise RuntimeError('429') on persistent throttle."""
    p = {"latitude": lat, "longitude": lon, "hourly": "temperature_2m",
         "start_date": d0, "end_date": d1, "timezone": tz}
    if model != "best_match":
        p["models"] = model
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
        j = r.json()
        h = j.get("hourly", {})
        dm: dict[str, float] = {}
        for t, v in zip(h.get("time", []), h.get("temperature_2m", [])):
            if v is not None:
                day = t[:10]
                dm[day] = v if day not in dm else max(dm[day], v)
        return dm
    raise RuntimeError("429")


def score(dm: dict[str, float], act: dict[str, int]):
    e = b = 0.0; hit = n = 0
    for d, f in dm.items():
        if d in act:
            e += abs(f - act[d]); b += (f - act[d]); hit += round(f) == act[d]; n += 1
    if n < 30:
        return None
    return {"mae": round(e / n, 3), "bias": round(b / n, 3), "hit": round(100 * hit / n, 1), "n": n}


def done_keys(out: Path) -> set[tuple]:
    if not out.exists():
        return set()
    s = set()
    for ln in out.read_text().splitlines():
        if ln.strip():
            r = json.loads(ln)
            s.add((r["station"], r["model"], r.get("point", "station")))
    return s


async def run(args):
    stations = load_stations()
    actuals = load_actuals()
    end = date(2026, 6, 20)
    start = end - timedelta(days=args.days - 1)
    d0, d1 = start.isoformat(), end.isoformat()
    out = Path(args.out)
    done = done_keys(out)
    only = set(args.stations.split(",")) if args.stations else None

    # Build the (station, model) work-list.
    pairs: list[tuple] = []
    if args.regional:
        for model, sids in REGIONAL.items():
            for sid in sids:
                if sid in stations:
                    pairs.append((stations[sid], model))
    else:
        models = args.models.split(",") if args.models else DEFAULT_MODELS
        for stn in stations.values():
            for m in models:
                pairs.append((stn, m))

    tasks = [(stn, m) for stn, m in pairs
             if (only is None or stn.station_id in only)
             and (stn.station_id, m, "station") not in done]
    n_models = len({m for _, m in pairs})
    print(f"scan: {len(tasks)} (city,model) to do ({len(done)} cached), {n_models} models"
          f"{' [regional]' if args.regional else ''}, {d0}->{d1} | concurrency {args.concurrency}", flush=True)

    sem = asyncio.Semaphore(args.concurrency)
    throttled = asyncio.Event()
    fh = out.open("a")
    counts = {"ok": 0, "empty": 0}

    async with httpx.AsyncClient(headers={"User-Agent": "polyweather-modelscan/1"}) as client:
        async def one(stn, model):
            if throttled.is_set():
                return
            async with sem:
                if throttled.is_set():
                    return
                try:
                    dm = await fetch_daymax(client, stn.latitude, stn.longitude, stn.timezone, model, d0, d1)
                except RuntimeError:
                    throttled.set(); return
                sc = score(dm, actuals.get(stn.station_id, {}))
                rec = {"station": stn.station_id, "city": stn.city, "model": model,
                       "point": "station", **(sc or {"mae": None, "note": "n<30/empty"})}
                fh.write(json.dumps(rec) + "\n"); fh.flush()
                counts["ok" if sc else "empty"] += 1
                if (counts["ok"] + counts["empty"]) % 25 == 0:
                    print(f"  ...{counts['ok']+counts['empty']}/{len(tasks)} done", flush=True)

        await asyncio.gather(*(one(stn, m) for stn, m in tasks))
    fh.close()

    if throttled.is_set():
        print(f"\n*** RATE LIMITED (429) *** stopped at {counts['ok']+counts['empty']}/{len(tasks)}. "
              f"Switch VPN and re-run the SAME command — it resumes from {out}.", flush=True)
    else:
        print(f"\nDONE. ok={counts['ok']} empty={counts['empty']}. Summary: "
              f"uv run python maintain/model_city_scan.py --summary {out}", flush=True)


def summary(path):
    rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    by_city: dict[str, dict] = defaultdict(dict)
    order = []
    for r in rows:
        if r["city"] not in by_city:
            order.append(r["city"])
        if r.get("mae") is not None:
            by_city[r["city"]][r["model"]] = r
    print(f"{'city':14}{'best model':20}{'MAE':6}{'bias':7}{'hit%':6}  best_match: model(MAE) | gap-to-best")
    flags = []
    for city in order:
        ms = by_city[city]
        if not ms:
            print(f"{city:14}(no data)"); continue
        best = min(ms.values(), key=lambda r: r["mae"])
        bm = ms.get("best_match")
        # which model does best_match match (closest MAE+similar)?
        bmtag = ""
        if bm:
            twin = min((r for r in ms.values() if r["model"] != "best_match"),
                       key=lambda r: abs(r["mae"] - bm["mae"]), default=None)
            gap = bm["mae"] - best["mae"]
            bmtag = f"{bm['mae']:.2f} (~{twin['model'] if twin else '?'}) | +{gap:.2f} vs best"
        flag = ""
        if best["mae"] > 2.0 or abs(best["bias"]) > 1.5:
            flag = "  <== inland-shift candidate"
            flags.append((city, best))
        print(f"{city:14}{best['model']:20}{best['mae']:<6.2f}{best['bias']:<+7.2f}{best['hit']:<6.1f}  {bmtag}{flag}")
    if flags:
        print(f"\n阶段2 候选(最优模型仍 MAE>2 或 |bias|>1.5,疑似海格/取点问题): "
              f"{', '.join(c for c,_ in flags)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--models", default="")
    ap.add_argument("--stations", default="", help="comma-sep station_ids to limit to")
    ap.add_argument("--regional", action="store_true", help="run the REGIONAL high-res map only")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--out", default="/tmp/model_scan_p1.jsonl")
    ap.add_argument("--summary", default="")
    args = ap.parse_args()
    if args.summary:
        summary(args.summary)
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
