"""Dynamic scoreboard / diagnostic (NOT the live orchestrator anymore).

Scores ALL verified cities and shows each city's peak (Open-Meteo local-day curve
argmax), Strike (=peak−1h), the 6-factor score (now a DIAGNOSTIC, not a selector),
and whether it is ACTIVE right now (peak within 90min ahead). NO top-N cap and NO
00:00Z batch — the live runtime is the hourly-scan/activate daemon (peak−90min
activation, then bet whoever passes the 3 gates: confidence / health / price<0.85).
This script is just the human-readable snapshot of that state at a point in time;
its `fetch_curve` / `f1..f6` / bucket helpers are what the daemon reuses.

Superseded design notes (kept for context): this used to be a 00:00Z batch that
picked the TOP-20 by composite score. That model is gone (see the plan's
TIMING/SELECTION REVISED section).

Peak/Strike: peak = argmax of the market-day's local-clock-day temperature curve
(inherently daily-adaptive & condition-aware); Strike = peak - 1h. NO solar+lag.
Analysis window is always the station's LOCAL clock day (= Polymarket's resolution
window, exactly one peak); UTC is used only for scheduling. solar.day_sun is kept
solely for the sunset+30min deadline.

Score (designed + data-validated 2026-06-14):
  CORE = 0.46*F1_base + 0.21*F2_micro + 0.19*F3_stab + 0.14*F4_peak     (additive)
  SCORE = CORE * M_liq * M_reg                                          (gentle guards)
where
  F1 macro    = (0.70*nSig + 0.20*nMst)/0.90  from city_profiles month σ + max_std
  F2 micro    = the MARKET-DAY's solar-weighted cloud-var / wind-var / precip cleanliness
  F3 stab     = exp(-(0.65*cross-model-sd + 0.35*day-step)/1.2)  (ECMWF/GFS/ICON Tmax)
  F4 peak     = recent-7d STABILITY of the actual peak hour from our own METAR
  M_liq       = 0.90 + 0.10*liq_norm  (hard floor excludes dead markets)
  M_reg       = 1 - 0.5*(1 - GENTLE[regime])   (deliberately the gentlest term)
Cities with NO full-year profile are EXCLUDED (not imputed) — σ is the base.

    uv run python scripts/daily_pool.py                 # scoreboard for NOW
    uv run python scripts/daily_pool.py --at 2026-06-15T08:00
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics as st
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from polyweather.config import load_config
from polyweather.forecast.conditions import classify_regime
from polyweather.forecast.open_meteo import fetch_conditions
from polyweather.realized.feature_store import DATA_DIR
from polyweather.realized.metar_parse import parse_metar
from polyweather.realized.metar_source import fetch_history_iem
from polyweather.realized.solar import day_sun, solar_noon_utc
from polyweather.stations import load_stations

ACTIVATE_WINDOW_MIN = 90   # a city is "active" when its peak is within this many minutes ahead
SEM = asyncio.Semaphore(5)
OM = "https://api.open-meteo.com/v1/forecast"
GAMMA = "https://gamma-api.polymarket.com/events"
HDR = {"User-Agent": "Mozilla/5.0 polyweather/0.1"}
MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless"]
UTC = ZoneInfo("UTC")
# regime -> gentle base, then softened by beta=0.5 in M_reg (span 0.90..1.00)
GENTLE = {"clear_stable": 1.00, "overcast": 0.97, "showery": 0.93, "convective": 0.80}
WEIGHTS = {"base": 0.46, "micro": 0.21, "stab": 0.19, "peak": 0.14}
LIQ_FLOOR, LIQ_SAT = 5000.0, 80000.0
FLAT_C = 2.0              # curve (max-min) below this -> no clear peak -> nominal Strike
NEAR_TIE_C = 0.5         # peaks within this of the max are "near-equal" -> solar tie-break
NOMINAL_PEAK_HOUR = 14   # local-time fallback peak when the curve is flat / missing
PEAK_SD_MAX = 3.0        # hours; recent peak-hour stdev that maps F4 to 0


def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def f1_base(sigma: float, max_std: float) -> float:
    n_sig = clip01((3.0 - sigma) / (3.0 - 0.8))
    n_mst = clip01((5.0 - max_std) / (5.0 - 1.0))
    return (0.70 * n_sig + 0.20 * n_mst) / 0.90


def _micro(h: dict) -> float | None:
    """F2 cleanliness from a local-day hourly block (0..1, higher=cleaner)."""
    cc = h.get("cloud_cover") or []
    ws = h.get("wind_speed_10m") or []
    pp = h.get("precipitation_probability") or []
    sw = h.get("shortwave_radiation") or []
    if not sw:
        return None
    w = [max(0.0, x or 0.0) for x in sw]
    W = sum(w) or 1.0

    def wmean(v):
        return sum((vi or 0.0) * wi for vi, wi in zip(v, w)) / W

    def wstd(v):
        m = wmean(v)
        return math.sqrt(sum(wi * ((vi or 0.0) - m) ** 2 for vi, wi in zip(v, w)) / W)

    day_pp = [p or 0.0 for p, wi in zip(pp, w) if wi > 0] or [p or 0.0 for p in pp]
    pp_max = max(day_pp) if day_pp else 0.0
    s_precip = 1.0 - pp_max / 100.0
    s_cloud = 1.0 - clip01(wstd(cc) / 35.0)
    s_wind = 1.0 - clip01(wstd(ws) / 12.0)
    return clip01(0.45 * s_precip + 0.35 * s_cloud + 0.20 * s_wind)


def _analyze_curve(h: dict, day: date, tz: str, expected_peak_utc: datetime | None = None) -> dict | None:
    """Pure: from the LOCAL-day hourly block, derive the peak time + predicted max +
    F2 cleanliness + flat flag. Peak VALUE = full-day max (drives the bucket). Peak
    TIME = the argmax, EXCEPT when several hours are within NEAR_TIE_C of the max (a
    flat day, or two near-equal peaks — e.g. a midnight carry-over vs the afternoon
    diurnal peak): then pick the near-tie hour CLOSEST to the expected solar peak
    (solar_noon+lag). This stabilises the Strike on flat days and prefers the real
    diurnal (afternoon) peak. A genuine cooling day (midnight clearly highest, diff
    > NEAR_TIE_C) keeps the midnight peak. Offline-testable."""
    temp = h.get("temperature_2m") or []
    times = h.get("time") or []
    valid = [(i, v) for i, v in enumerate(temp) if v is not None]
    if not valid or len(times) != len(temp):
        return None
    z = ZoneInfo(tz)
    gmax = max(v for _, v in valid)
    flat = (gmax - min(v for _, v in valid)) < FLAT_C
    near = [i for i, v in valid if v >= gmax - NEAR_TIE_C]

    def _utc(i):
        return datetime.fromisoformat(times[i]).replace(tzinfo=z).astimezone(UTC)

    if len(near) > 1 and expected_peak_utc is not None:        # near-tie / flat -> solar tie-break
        ipk = min(near, key=lambda i: abs((_utc(i) - expected_peak_utc).total_seconds()))
        peak_local = datetime.fromisoformat(times[ipk]).replace(tzinfo=z)
    elif flat:                                                 # flat + no solar ref -> nominal afternoon
        peak_local = datetime.combine(day, time(NOMINAL_PEAK_HOUR)).replace(tzinfo=z)
    else:                                                      # single clear peak
        imax = max(valid, key=lambda x: x[1])[0]
        peak_local = datetime.fromisoformat(times[imax]).replace(tzinfo=z)
    return {"micro": _micro(h), "peak_utc": peak_local.astimezone(UTC),
            "pred_max": gmax, "flat": flat}


async def fetch_curve(client, lat, lon, tz, day, expected_peak_utc=None) -> dict | None:
    """Fetch the market-day's LOCAL-clock-day hourly curve and analyze it.
    expected_peak_utc (= solar_noon+lag) is the near-tie tie-breaker for the peak time."""
    try:
        r = await client.get(OM, params={
            "latitude": lat, "longitude": lon, "timezone": tz,
            "start_date": day.isoformat(), "end_date": day.isoformat(),
            "hourly": "temperature_2m,cloud_cover,wind_speed_10m,precipitation_probability,shortwave_radiation"})
        r.raise_for_status()
        h = r.json()["hourly"]
    except Exception:
        return None
    return _analyze_curve(h, day, tz, expected_peak_utc)


async def f3_stab(client, lat, lon, tz, mkt_day) -> float | None:
    """Cross-model + adjacent-day agreement on the market-day's Tmax (0..1, higher=tighter)."""
    try:
        r = await client.get(OM, params={
            "latitude": lat, "longitude": lon, "timezone": tz,
            "daily": "temperature_2m_max", "models": ",".join(MODELS),
            "start_date": (mkt_day - timedelta(days=1)).isoformat(),
            "end_date": (mkt_day + timedelta(days=1)).isoformat()})
        r.raise_for_status()
        d = r.json()["daily"]
    except Exception:
        return None
    times = d.get("time") or []
    if mkt_day.isoformat() not in times:
        return None
    iD = times.index(mkt_day.isoformat())
    cols = {m: d.get(f"temperature_2m_max_{m}") for m in MODELS}
    valsD = [cols[m][iD] for m in MODELS if cols.get(m) and cols[m][iD] is not None]
    if len(valsD) < 2:
        return None
    sd_cross = st.pstdev(valsD)
    ens = []
    for i in range(len(times)):
        vs = [cols[m][i] for m in MODELS if cols.get(m) and cols[m][i] is not None]
        ens.append(st.fmean(vs) if vs else None)
    steps = []
    if iD - 1 >= 0 and ens[iD - 1] is not None:
        steps.append(abs(ens[iD] - ens[iD - 1]))
    if iD + 1 < len(ens) and ens[iD + 1] is not None:
        steps.append(abs(ens[iD + 1] - ens[iD]))
    day_step = st.fmean(steps) if steps else 0.0
    return math.exp(-(0.65 * sd_cross + 0.35 * day_step) / 1.2)


def _peak_hours_from(byday: dict, z: ZoneInfo) -> list[float]:
    """Per local day, the median local hour of the max-achieving obs (plateau-robust)."""
    hours = []
    for _d, obs in byday.items():
        if len(obs) < 6:
            continue
        mx = max(c for c, _ in obs)
        loc = [t.astimezone(z) for c, t in obs if c >= mx - 0.05]
        hh = [t.hour + t.minute / 60.0 for t in loc]
        if hh:
            hours.append(st.median(hh))
    return hours


def _saved_peak_hours(icao: str, end_day: date, tz: str, days: int = 7) -> list[float]:
    """Recent per-day peak hours from the saved METAR store, if it exists."""
    path = DATA_DIR / f"{icao}_obs.jsonl"
    if not path.exists():
        return []
    z = ZoneInfo(tz)
    start = end_day - timedelta(days=days)
    byday: dict[date, list] = {}
    for line in path.read_text().splitlines():
        try:
            o = json.loads(line)
        except Exception:
            continue
        ts, tc = o.get("obs_time_utc"), o.get("temp_c")
        if not ts or tc is None:
            continue
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        d = t.astimezone(z).date()
        if start <= d < end_day:
            byday.setdefault(d, []).append((tc, t))
    return _peak_hours_from(byday, z)


async def f4_peak(client, icao, tz, trigger) -> float | None:
    """Recent peak-time STABILITY from our own METAR (saved store first, else IEM).
    A consistent daily peak hour -> timing-predictable -> high. [0,1]. No Open-Meteo
    historical-forecast dependency."""
    z = ZoneInfo(tz)
    end_day = trigger.date()
    hours = _saved_peak_hours(icao, end_day, tz)
    if len(hours) < 4:
        try:
            reps = await fetch_history_iem(
                client, icao, (end_day - timedelta(days=7)).strftime("%Y%m%d"),
                end_day.strftime("%Y%m%d"), retries=4)
        except Exception:
            return None
        byday: dict[date, list] = {}
        for raw, t in reps:
            try:
                obs = parse_metar(raw, icao, t)
            except Exception:
                continue
            if obs.temp_c is None:
                continue
            byday.setdefault(t.astimezone(z).date(), []).append((obs.temp_c, t))
        hours = _peak_hours_from(byday, z)
    if len(hours) < 4:
        return None
    sd = st.pstdev(hours) if len(hours) > 1 else 0.0
    return clip01(1.0 - sd / PEAK_SD_MAX)


async def f5_liq(client, series_id) -> float | None:
    """Mean settled volume over last 7 closed events. None if no data; 'DEAD' if floored."""
    if series_id is None:
        return None
    try:
        r = await client.get(GAMMA, params={"series_id": str(series_id), "closed": "true",
                                            "limit": 7, "order": "endDate", "ascending": "false"})
        r.raise_for_status()
        evs = r.json()
    except Exception:
        return None
    vols = [sum(float(m.get("volume") or 0.0) for m in (ev.get("markets") or [])) for ev in evs]
    vols = [v for v in vols if v > 0]
    if not vols:
        return None
    mean_vol = st.fmean(vols)
    if mean_vol <= LIQ_FLOOR:
        return "DEAD"  # type: ignore[return-value]
    return 0.90 + 0.10 * min(1.0, math.log(mean_vol / LIQ_FLOOR) / math.log(LIQ_SAT / LIQ_FLOOR))


async def score_city(client, stn, profiles, cfg, trigger) -> dict:
    prof = profiles.get(stn.station_id, {})
    months = prof.get("months") or {} if isinstance(prof, dict) else {}
    z = ZoneInfo(stn.timezone)

    async with SEM:
        # Current EVENT = the city's LOCAL clock day, switching at LOCAL MIDNIGHT (Open-Meteo
        # returns the new local day's curve once past 00:00, and the run is a few min offset).
        # NO rolling when the peak passes: a passed peak = "today done" (→ 未下注 / not active).
        local_now = trigger.astimezone(z)
        mkt_day = local_now.date()
        mon = months.get(str(mkt_day.month), {})
        lag = mon.get("lag_h")
        # expected diurnal peak (solar_noon + climatological lag) — used ONLY as the
        # near-tie tie-breaker for the peak time, NOT as the primary peak source.
        expected_peak = (solar_noon_utc(stn.longitude, mkt_day) + timedelta(hours=lag)
                         if lag is not None else None)
        curve = await fetch_curve(client, stn.latitude, stn.longitude, stn.timezone, mkt_day, expected_peak)
        if curve is None:                                        # network fallback: expected/nominal peak
            peak_utc = expected_peak or datetime.combine(
                mkt_day, time(NOMINAL_PEAK_HOUR)).replace(tzinfo=z).astimezone(UTC)
            curve = {"micro": None, "peak_utc": peak_utc, "pred_max": None, "flat": True}

        strike = curve["peak_utc"] - timedelta(hours=1.0)
        sigma = mon.get("model_sigma") or prof.get("annual_model_sigma")
        max_std = mon.get("max_std") or prof.get("annual_max_std") or 3.0
        iso = mkt_day.isoformat()

        async def get_regime():
            try:
                bundle = await fetch_conditions(client, stn.latitude, stn.longitude, stn.timezone,
                                                start_date=iso, end_date=iso)
                return classify_regime(bundle, mkt_day, cfg.conditions)
            except Exception:
                return None

        regime, stab, liq, peak = await asyncio.gather(
            get_regime(),
            f3_stab(client, stn.latitude, stn.longitude, stn.timezone, mkt_day),
            f5_liq(client, stn.pm_series_id),
            f4_peak(client, stn.station_id, stn.timezone, trigger))

    micro = curve["micro"]
    excluded, why = False, ""
    if sigma is None:
        excluded, why = True, "no profile (σ unknown)"
    elif liq == "DEAD":
        excluded, why = True, "dead market (<$5k/event)"

    base = f1_base(sigma, max_std) if sigma is not None else None
    f_micro = micro if micro is not None else 0.6     # neutral fallbacks
    f_stab = stab if stab is not None else 0.6
    f_peak = peak if peak is not None else 0.5
    core = None
    if base is not None:
        core = (WEIGHTS["base"] * base + WEIGHTS["micro"] * f_micro
                + WEIGHTS["stab"] * f_stab + WEIGHTS["peak"] * f_peak)
    m_liq = liq if isinstance(liq, float) else 0.95
    reg_label = regime.value if regime else "overcast"
    m_reg = 1.0 - 0.5 * (1.0 - GENTLE.get(reg_label, 0.95))
    score = round(core * m_liq * m_reg, 4) if core is not None else None

    _sr, _noon, ss, _dl = day_sun(stn.latitude, stn.longitude, mkt_day)
    return {
        "city": stn.city, "station": stn.station_id, "market_date": mkt_day.isoformat(),
        "strike_utc": strike.strftime("%Y-%m-%d %H:%MZ"), "_strike": strike,
        "deadline_utc": (ss + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%MZ"),
        "pred_max": round(curve["pred_max"], 1) if curve["pred_max"] is not None else None,
        "flat_curve": curve["flat"],
        "score": score, "excluded": excluded, "exclude_reason": why,
        "f": {"base": round(base, 3) if base is not None else None,
              "micro": round(f_micro, 3), "micro_live": micro is not None,
              "stab": round(f_stab, 3), "stab_live": stab is not None,
              "peak": round(f_peak, 3), "peak_live": peak is not None,
              "m_liq": round(m_liq, 3), "m_reg": round(m_reg, 3)},
        "sigma_month": round(sigma, 2) if sigma is not None else None,
        "regime": reg_label,
    }


async def main(at: str | None) -> None:
    cfg = load_config()
    stations = [s for s in load_stations().values() if s.verified]
    profiles = json.loads((DATA_DIR / "city_profiles.json").read_text())

    trigger = (datetime.fromisoformat(at).replace(tzinfo=ZoneInfo("UTC"))
               if at else datetime.now(ZoneInfo("UTC")))

    async with httpx.AsyncClient(timeout=60.0, headers=HDR) as client:
        scored = await asyncio.gather(*[score_city(client, s, profiles, cfg, trigger) for s in stations])

    ranked = sorted([c for c in scored if not c["excluded"] and c["score"] is not None],
                    key=lambda x: x["score"], reverse=True)
    excluded = [c for c in scored if c["excluded"]]
    for c in ranked:                       # ACTIVE = peak within the activation window ahead of `trigger`
        mins_to_peak = (c["_strike"] + timedelta(hours=1) - trigger).total_seconds() / 60.0
        c["mins_to_peak"] = round(mins_to_peak)
        c["active"] = 0 < mins_to_peak < ACTIVATE_WINDOW_MIN
    for c in scored:
        c.pop("_strike", None)

    out = {"trigger_utc": trigger.strftime("%Y-%m-%d %H:%MZ"),
           "activate_window_min": ACTIVATE_WINDOW_MIN, "cities": ranked, "excluded": excluded}
    (DATA_DIR / "daily_pool.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))

    active = [c for c in ranked if c["active"]]
    print(f"trigger {trigger:%Y-%m-%d %H:%MZ}   scored {len(ranked)} "
          f"(excluded {len(excluded)})   ACTIVE now: {len(active)} (peak ≤ {ACTIVATE_WINDOW_MIN}min ahead)\n")
    print(f"{'':2}{'city':13}{'score':>7}{'pmax':>6}{'→peak':>7}{'BASE':>6}{'MICRO':>7}{'STAB':>6}"
          f"{'PEAK':>6}{'Mliq':>6}{'Mreg':>6} {'regime':12}")
    for c in ranked:
        f = c["f"]
        liveflag = lambda k: "" if f[f"{k}_live"] else "~"   # noqa: E731
        pm = c["pred_max"]
        pmf = (f"{pm}" + ("F" if c["flat_curve"] else "")) if pm is not None else "--"
        mark = "▶ " if c["active"] else "  "
        mtp = c["mins_to_peak"]
        mtps = f"{mtp}m" if -180 < mtp < 1000 else "—"
        print(f"{mark}{c['city']:13}{c['score']:>7}{pmf:>6}{mtps:>7}{f['base']:>6}"
              f"{f['micro']:>6}{liveflag('micro'):1}{f['stab']:>5}{liveflag('stab'):1}"
              f"{f['peak']:>5}{liveflag('peak'):1}{f['m_liq']:>6}{f['m_reg']:>6} {c['regime']:12}")
    if excluded:
        print(f"\nexcluded: {', '.join(c['city'] + '(' + c['exclude_reason'] + ')' for c in excluded)}")
    print("  ▶ = ACTIVE (peak within 90min → would activate now);  ~ = factor fell back;  pmax 'F' = flat curve")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", help="trigger time ISO (UTC), e.g. 2026-06-14T00:00")
    asyncio.run(main(ap.parse_args().at))
