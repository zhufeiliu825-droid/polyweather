"""Real-time near-peak NOWCAST paper observer — STANDALONE, READ-ONLY, places NO orders.

Does NOT touch the live daemon/farmer. It exists to TEST the one edge this session actually measured:
day-ahead temp forecasting has no edge vs the efficient market; the ONLY window with real bucket-
predictability is the last ~60-90 min before the daily peak, and the only cities where we see data the
market does not are the four with a free sub-hourly 0.1C feed CO-LOCATED with the resolution station.

Per city per run it: (1) reads the real-time 0.1C running max from the co-located national feed,
(2) builds a per-bucket probability distribution (running-max floor + Open-Meteo remaining-rise +
lead-shrinking sigma), (3) fetches the live de-vigged Polymarket bucket prices, and logs BOTH ->
data/rt_nowcast.jsonl. The offline scorer (rt_nowcast_score.py) joins to settle and measures whether
the nowcast BEATS the market (Brier / CLV) by lead. NO orders are ever placed.

Per-city strategy is pluggable: each city names a FEED adapter; adding a new city when it gets a real-time
co-located feed = add one entry to CITIES (+ a feed function). Cities with no such feed are intentionally
absent (everywhere else is the same integer hourly/half-hourly METAR the market already sees -> no edge).

  Tokyo  RJTT  JMA AMeDAS 44166         10-min 0.1C   no key
  Seoul  RKSI  KMA AMOS apihub stn 113  1-min  0.1C   needs free env KMA_APIHUB_KEY (+ one-time 활용신청)
  Hong Kong   HKO since-midnight-max csv  10-min 0.1C  no key — the feed IS the resolution gauge (HKO HQ urban)
  Munich EDDM DWD 10-min station 01262   10-min 0.1C   no key (München-Flughafen)
  Helsinki EFHK FMI WFS fmisid 100968    10-min 0.1C   no key (Helsinki-Vantaa)
  (Busan RKPK has NO public minute-AMOS feed — Gimhae is not one of amos.php's 7 airports — so it is omitted.)
  (US cities give NO edge: standard ASOS reports temp only hourly ~:51; sub-hourly products are day-delayed or
   coarser than the hourly METAR — so US real-time data IS the same hourly METAR the market already prices.)

  uv run python maintain/rt_nowcast.py      # one snapshot of all AVAILABLE cities -> append to jsonl
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
import re
import zipfile
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx

from polyweather.realized.feature_store import DATA_DIR
from polyweather.stations import load_stations

UTC = timezone.utc
OUT = DATA_DIR / "rt_nowcast.jsonl"
GAMMA = "https://gamma-api.polymarket.com/events"
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

# Per-city config: which co-located real-time feed + its station id. lat/lon/tz/pm_series_id come from
# load_stations() at runtime so this stays in sync with the live config without duplicating it.
CITIES: dict[str, dict] = {
    "RJTT": {"city": "Tokyo", "feed": "amedas", "amedas_id": "44166"},
    "RKSI": {"city": "Seoul", "feed": "amos", "amos_stn": "113"},  # Incheon AMOS, 1-min 0.1C (key + 활용신청)
    # Hong Kong: not in load_stations -> carries its own tz/lat/lon/series. The HKO feed IS the resolution gauge
    # (HKO HQ urban, 0.1C), so it hands us the running daily max directly — the cleanest of all four.
    "HKHQ": {"city": "Hong Kong", "feed": "hko", "tz": "Asia/Hong_Kong",
             "lat": 22.302, "lon": 114.174, "series": "11312"},
    "EDDM": {"city": "Munich", "feed": "dwd", "dwd_stn": "01262"},     # DWD 10-min 0.1C, no key (München-Flughafen)
    "EFHK": {"city": "Helsinki", "feed": "fmi", "fmi_id": "100968"},   # FMI WFS 10-min 0.1C, no key (Vantaa)
    # Busan RKPK: omitted — Gimhae is NOT among amos.php's 7 airports, so no public minute feed exists.
}


# ---------------------------------------------------------------- feeds (each -> [(utc_dt, temp_c)] today)
async def feed_amedas(client: httpx.AsyncClient, cfg: dict, today_local: date, tz: ZoneInfo) -> list[tuple]:
    """JMA AMeDAS point files: 3-hour blocks YYYYMMDD_HH.json keyed by YYYYMMDDHHMMSS, temp=[val, flag]."""
    sid = cfg["amedas_id"]
    ymd = today_local.strftime("%Y%m%d")
    now_h = datetime.now(tz).hour
    out: list[tuple] = []
    for hh in [h for h in range(0, 24, 3) if h <= now_h]:
        try:
            j = (await client.get(
                f"https://www.jma.go.jp/bosai/amedas/data/point/{sid}/{ymd}_{hh:02d}.json", timeout=15)).json()
        except Exception:
            continue
        for k, v in j.items():
            t = v.get("temp")
            if not t or t[0] is None:
                continue
            dtj = datetime.strptime(k, "%Y%m%d%H%M%S").replace(tzinfo=tz)
            out.append((dtj.astimezone(UTC), float(t[0])))
    return out


# Singapore (WSSS) DROPPED 2026-06-20: data.gov.sg has NO Changi sensor; the nearest station S24 (Upper Changi Rd)
# diverged 3.2°C from the live WSSS METAR — too big for a 1°C-bucket market. No clean co-located feed exists.


def _parse_amos(text: str, tz: ZoneInfo) -> list[tuple]:
    """KMA apihub amos.php (typ01): '#'-commented header, then whitespace data rows with FIXED columns
    S TM L_VIS R_VIS L_RVR R_RVR CH_MIN TA TD ...  ->  field[1]=TM (LST, YYYYMMDDHHMM), field[7]=TA in 0.1C.
    Verified live 2026-06-20 against RKSI stn 113. Parsed by POSITION (the header line drops the S label, so a
    header-name zip misaligns)."""
    out: list[tuple] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        p = s.split()
        if len(p) < 8:
            continue
        try:
            ta = float(p[7])
        except ValueError:
            continue
        if ta <= -900 or ta >= 900:           # missing sentinel (e.g. -99999)
            continue
        ta /= 10.0                            # TA is tenths of a degree (213 -> 21.3C)
        if not -90.0 < ta < 60.0:
            continue
        try:
            dtl = datetime.strptime(p[1][:12], "%Y%m%d%H%M").replace(tzinfo=tz)
        except ValueError:
            continue
        out.append((dtl.astimezone(UTC), ta))
    return out


async def feed_amos(client: httpx.AsyncClient, cfg: dict, today_local: date, tz: ZoneInfo) -> list[tuple] | None:
    """KMA AMOS (Seoul/Busan) via apihub amos.php — needs free env KMA_APIHUB_KEY; returns None if unset."""
    key = os.getenv("KMA_APIHUB_KEY", "").strip()
    if not key:
        return None
    tm = datetime.now(tz).strftime("%Y%m%d%H%M")  # KST now; amos.php caps dtm at 60 -> previous 60 min only
    try:
        r = await client.get("https://apihub.kma.go.kr/api/typ01/url/amos.php", params={
            "tm": tm, "dtm": "60", "stn": cfg["amos_stn"], "help": "0", "authKey": key}, timeout=20)
        if '"status"' in r.text:  # JSON error envelope (401 bad key / 403 not yet 활용신청)
            return None
        return _parse_amos(r.text, tz)
    except Exception:
        return None


async def feed_hko(client: httpx.AsyncClient, cfg: dict, today_local: date, tz: ZoneInfo) -> list[tuple] | None:
    """Hong Kong: the HKO 'since-midnight max/min' CSV gives the running daily MAX of the EXACT resolution gauge
    (HKO HQ urban, 0.1C) directly, refreshed ~10 min, no key — so we return it as a single running-max point."""
    try:
        r = await client.get("https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather/"
                             "latest_since_midnight_maxmin.csv", timeout=15)
    except Exception:
        return None
    for ln in r.text.splitlines():
        parts = [x.strip() for x in ln.split(",")]
        if len(parts) >= 3 and parts[1] == "HK Observatory":
            try:
                ts = datetime.strptime(parts[0][:12], "%Y%m%d%H%M").replace(tzinfo=tz)
                return [(ts.astimezone(UTC), float(parts[2]))]
            except (ValueError, IndexError):
                return None
    return None


async def feed_dwd(client: httpx.AsyncClient, cfg: dict, today_local: date, tz: ZoneInfo) -> list[tuple] | None:
    """Munich (EDDM): DWD 10-min 'now' file for station 01262 (München-Flughafen = the resolution station).
    Zip of a ';'-CSV; field[1]=MESS_DATUM (UTC YYYYMMDDHHMM), field[4]=TT_10 (°C, 0.1). No key."""
    url = ("https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/"
           f"air_temperature/now/10minutenwerte_TU_{cfg['dwd_stn']}_now.zip")
    try:
        r = await client.get(url, timeout=30)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        lines = z.read(next(n for n in z.namelist() if n.startswith("produkt"))).decode().splitlines()
    except Exception:
        return None
    out = []
    for ln in lines[1:]:
        p = ln.split(";")
        if len(p) < 5:
            continue
        try:
            t = float(p[4])
            dtu = datetime.strptime(p[1].strip(), "%Y%m%d%H%M").replace(tzinfo=UTC)
        except (ValueError, IndexError):
            continue
        if t <= -900 or dtu.astimezone(tz).date() != today_local:
            continue
        out.append((dtu, t))
    return out


async def feed_fmi(client: httpx.AsyncClient, cfg: dict, today_local: date, tz: ZoneInfo) -> list[tuple] | None:
    """Helsinki (EFHK): FMI WFS 10-min t2m for fmisid 100968 (Helsinki-Vantaa = the resolution station). No key."""
    start = datetime(today_local.year, today_local.month, today_local.day, tzinfo=tz).astimezone(UTC)
    try:
        r = await client.get("https://opendata.fmi.fi/wfs", params={
            "service": "WFS", "version": "2.0.0", "request": "getFeature",
            "storedquery_id": "fmi::observations::weather::simple", "fmisid": cfg["fmi_id"],
            "parameters": "t2m", "starttime": start.strftime("%Y-%m-%dT%H:%M:%SZ")}, timeout=30)
        xml = r.text
    except Exception:
        return None
    out = []
    for t, v in zip(re.findall(r"<BsWfs:Time>(.*?)</BsWfs:Time>", xml),
                    re.findall(r"<BsWfs:ParameterValue>(.*?)</BsWfs:ParameterValue>", xml)):
        if v.strip().lower() == "nan":
            continue
        try:
            dtu = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(UTC)
            val = float(v)
        except ValueError:
            continue
        if dtu.astimezone(tz).date() == today_local:
            out.append((dtu, val))
    return out


FEEDS = {"amedas": feed_amedas, "amos": feed_amos, "hko": feed_hko, "dwd": feed_dwd, "fmi": feed_fmi}


# ---------------------------------------------------------------- forecast curve + nowcast distribution
async def om_curve(client: httpx.AsyncClient, lat: float, lon: float, tzname: str) -> list[tuple]:
    try:
        j = (await client.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "hourly": "temperature_2m",
            "past_days": 1, "forecast_days": 1, "timezone": tzname}, timeout=20)).json()
        return [(datetime.fromisoformat(t), tp) for t, tp in
                zip(j["hourly"]["time"], j["hourly"]["temperature_2m"]) if tp is not None]
    except Exception:
        return []


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def nowcast_probs(running_max: float, om_remaining: float | None, lead_h: float | None,
                  buckets: list[tuple]) -> dict:
    """Per-bucket P over the market's bucket labels. final ~ Normal(mu, sigma) truncated below at the
    observed running max. sigma shrinks toward the peak (anchored on the measured remaining-rise: p90 ~1C
    at <1h, ~3C at 2-3h). TODO: replace the sigma model with the empirical remaining-rise-by-lead fit."""
    floor = running_max
    mu = max(running_max, om_remaining if om_remaining is not None else running_max)
    lh = max(lead_h or 0.0, 0.0)
    sigma = max(0.45, 0.45 + 0.55 * lh)          # ~0.45C at the peak, ~2.1C at 3h out
    z = 1.0 - _ncdf((floor - mu) / sigma)         # truncation mass above the floor
    if z <= 1e-9:
        z = 1e-9
    probs: dict = {}
    for comp, n in buckets:
        if comp == "==":
            lo, hi = max(n - 0.5, floor), n + 0.5
            p = (_ncdf((hi - mu) / sigma) - _ncdf((lo - mu) / sigma)) if hi > lo else 0.0
        elif comp == ">=":
            lo = max(n - 0.5, floor)
            p = 1.0 - _ncdf((lo - mu) / sigma)
        else:  # "<="
            hi = n + 0.5
            p = (_ncdf((hi - mu) / sigma) - _ncdf((floor - mu) / sigma)) if hi > floor else 0.0
        probs[f"{comp}{n}"] = max(0.0, p) / z
    s = sum(probs.values()) or 1.0
    return {k: round(v / s, 4) for k, v in probs.items()}


# ---------------------------------------------------------------- live market (de-vigged bucket prices)
def _parse_bucket(m: dict) -> tuple | None:
    t = (m.get("groupItemTitle") or m.get("question") or "")
    mm = re.search(r"(-?\d+)", t)
    if not mm:
        return None
    low = t.lower()
    comp = ">=" if ("above" in low or "higher" in low) else "<=" if ("below" in low or "lower" in low) else "=="
    return comp, int(mm.group(1))


def _event_date(ev: dict) -> date | None:
    m = re.search(r"-on-([a-z]+)-(\d+)-(\d{4})", ev.get("slug", "").lower())
    if not m or m.group(1) not in _MONTHS:
        return None
    return date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))


async def market_dist(client: httpx.AsyncClient, series_id, today_local: date):
    try:
        evs = (await client.get(GAMMA, params={"series_id": series_id, "closed": "false", "limit": 10}, timeout=20)).json()
    except Exception:
        return None, None, None
    ev = next((e for e in evs if _event_date(e) == today_local), None)
    if not ev:
        return None, None, None
    raw, buckets = {}, []
    for m in ev.get("markets", []):
        pb = _parse_bucket(m)
        if pb is None:
            continue
        px = json.loads(m.get("outcomePrices") or "[]")
        mid = float(px[0]) if px else None
        if mid is None:
            continue
        raw[f"{pb[0]}{pb[1]}"] = round(mid, 4)
        buckets.append(pb)
    if not raw:
        return None, None, None
    tot = sum(raw.values()) or 1.0
    devig = {k: round(v / tot, 4) for k, v in raw.items()}
    return devig, raw, buckets


# ---------------------------------------------------------------- per-city observe + main loop
async def observe(client: httpx.AsyncClient, icao: str, cfg: dict, stn, now: datetime) -> dict | None:
    tz = ZoneInfo(stn.timezone)
    today_local = now.astimezone(tz).date()
    now_local = now.astimezone(tz).replace(tzinfo=None)
    series = await FEEDS[cfg["feed"]](client, cfg, today_local, tz)
    if not series:  # feed unavailable (e.g. AMOS without a key) -> skip silently
        return None
    series.sort()
    running_max = max(t for _, t in series)
    latest_utc, latest_t = series[-1]

    curve = await om_curve(client, stn.latitude, stn.longitude, stn.timezone)
    today_curve = [(t, tp) for t, tp in curve if t.date() == today_local]
    fut = [tp for t, tp in today_curve if t.replace(tzinfo=None) > now_local]
    om_rem = max(fut) if fut else None
    peak_dt = max(today_curve, key=lambda x: x[1])[0] if today_curve else None
    lead_h = ((peak_dt.replace(tzinfo=None) - now_local).total_seconds() / 3600.0) if peak_dt else None

    devig, raw, buckets = await market_dist(client, stn.pm_series_id, today_local)
    nc = nowcast_probs(running_max, om_rem, lead_h, buckets) if buckets else None

    return {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "city": cfg["city"], "icao": icao,
        "date": str(today_local), "feed": cfg["feed"],
        "running_max": round(running_max, 1), "latest_temp": round(latest_t, 1),
        "latest_obs": latest_utc.strftime("%H:%M:%SZ"), "n_obs": len(series),
        "om_remaining_max": (round(om_rem, 1) if om_rem is not None else None),
        "peak_local": (peak_dt.strftime("%H:%M") if peak_dt else None),
        "lead_h": (round(lead_h, 2) if lead_h is not None else None),
        "nowcast": nc, "market_devig": devig, "market_raw": raw,
    }


def _station(icao: str, cfg: dict, stns: dict):
    """Resolve tz/lat/lon/pm_series_id from load_stations, or from the city's inline cfg (e.g. Hong Kong)."""
    s = stns.get(icao)
    if s is not None:
        return s
    return SimpleNamespace(station_id=icao, timezone=cfg["tz"], latitude=cfg["lat"],
                           longitude=cfg["lon"], pm_series_id=cfg["series"])


async def main() -> None:
    stns = load_stations()
    now = datetime.now(UTC)
    async with httpx.AsyncClient(headers={"User-Agent": "polyweather-rtnowcast/0.1"}) as c:
        rows = await asyncio.gather(
            *[observe(c, ic, cfg, _station(ic, cfg, stns), now) for ic, cfg in CITIES.items()],
            return_exceptions=True)
    out = [r for r in rows if isinstance(r, dict)]
    with OUT.open("a") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    for r in sorted(out, key=lambda r: r.get("lead_h") if r.get("lead_h") is not None else 99):
        nc, mk = r.get("nowcast") or {}, r.get("market_devig") or {}
        top_nc = max(nc, key=nc.get) if nc else "-"
        top_mk = max(mk, key=mk.get) if mk else "-"
        print(f"{r['city']:10} max={r['running_max']:>5}C (obs {r['latest_obs']}, n={r['n_obs']}) "
              f"lead={r['lead_h']}h peak~{r['peak_local']} | nowcast top {top_nc} ({nc.get(top_nc,'-')}) "
              f"| market top {top_mk} ({mk.get(top_mk,'-')})")
    avail = {r["city"] for r in out}
    skipped = [cfg["city"] for ic, cfg in CITIES.items() if cfg["city"] not in avail]
    print(f"\nwrote {len(out)} rows -> {OUT}" + (f"  | skipped (no feed/key): {skipped}" if skipped else ""))


if __name__ == "__main__":
    asyncio.run(main())
