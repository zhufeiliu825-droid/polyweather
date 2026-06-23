"""CLUSTER AUTO-TRADER (LIVE-capable, ISOLATED) — auto-buys the forecast temperature bucket(s) the model is
confident in, MAKER-FIRST (rests a bid to capture the spread) with a TAKER fallback near the peak, HOLDS TO
SETTLE. SEPARATE process + state (data/cluster_state.json) + lock (data/cluster.lock) from the daemon/farmer —
a cluster bug can never touch them. DRY by default (POLY_DRY_RUN=1); LIVE only when the user sets POLY_DRY_RUN=0.

BUCKET SELECTION — DYNAMIC, per-situation (NOT a hardcoded city list). Each city has measured (μ, σ): the daily
max ≈ N(center, σ) with center = max(F+μ, running_max). For each integer bucket n we score its win probability
P(n) = Φ((n+0.5−center)/σ) − Φ((n−0.5−center)/σ) and BUY every bucket with P(n) ≥ P_FLOOR AND P(n)−ask ≥ MIN_EDGE.
The count emerges from the day: a forecast that nails one integer → 1 bucket; a forecast straddling two → 2;
a city too fuzzy (large σ, e.g. Madrid/Sao Paulo) → P never clears the floor → it ABSTAINS. (Backtested ~70d/city
vs Gamma payout: P≥0.30 + edge≥0.05 → ROC ~+0.97, 3.3× the hardcoded accuracy-split; the floor is REQUIRED — the
naive Gaussian over-predicts cheap longshots which are flat-to-negative below P≈0.30.)

ORDER MECHANISM — maker-first, taker fallback (per bucket, idempotent on (city,date,bucket); survives restart):
  new   → before escalate: rest a GTC maker bid (≈best-bid, captures the spread). past escalate: taker now.
  RESTING → filled_size>0 → FILLED · now≥escalate_at → confirm-not-filled then cancel+taker · else leave resting.
  Position-keeping is SELF-CONTAINED: idempotency from cluster_state.json (rebuilt from the append-only
  cluster_orders.jsonl journal if the state file is lost) and fills from the order's OWN filled_size + cancel
  semantics — it NEVER reads the shared wallet, so it can't mis-attribute the daemon's/farmer's/manual positions
  to itself. (Root fix for the shared wallet = a separate funder.) NEVER sells (hold to settle).
Caps: ≤$CLUSTER_MAX_ORDER_USD/order, within depth, within the TOTAL cap (incl. open positions).

  POLY_DRY_RUN=1 uv run python run/run_cluster.py --once     # DRY preview (no orders, no state write)
  POLY_DRY_RUN=0 uv run python run/run_cluster.py --cap 1200 --interval 20   # LIVE loop (user's explicit action)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from polyweather import agent                           # reuse the OpenAI key/model for the cluster regime call
from polyweather import clob_executor as ex
from polyweather.models import Comparator, Decision
from polyweather.params import HEALTH_SUM_HI, HEALTH_SUM_LO, MAX_SPREAD, MIN_ORDER_SHARES, MIN_ORDER_USD
from polyweather.forecast.open_meteo import _forecast_endpoint   # keyed customer-api when OPEN_METEO_API_KEY set, else free
from polyweather.stations import load_stations
_FCMODEL = {s.station_id: s.forecast_model for s in load_stations().values()}  # icao -> pinned OpenMeteo model (None ⇒ best_match)

UA = {"User-Agent": "polyweather-cluster/1.0"}
STATE = Path(__file__).resolve().parents[1] / "data" / "cluster_state.json"
JOURNAL = Path(__file__).resolve().parents[1] / "data" / "cluster_orders.jsonl"   # append-only own-record (state-loss recovery)
TSFMT = "%Y-%m-%dT%H:%M:%SZ"           # second-granular (lexicographic-comparable) timestamps
MIN_EDGE = 0.05                        # P(bucket) − ask must clear this (lower gate)
MAX_EDGE = 0.45                        # P(bucket) − ask ABOVE this = our model wildly disagrees with the market →
#                                        likely OUR error (the Gaussian over-states confidence on cumulative TAIL
#                                        buckets, and the market integrates more info) → skip. (user directive
#                                        2026-06-22: trade tails but cap implausible edges. Above the observed
#                                        validated == edges ≈0.32, so it only trips the wild-disagreement cases.)
P_FLOOR = 0.35                         # below this the Gaussian over-predicts (longshots flat-to-negative); 0.35 (vs 0.30)
#                                        trims the off-by-one tail losses — OOS ROC is monotone-better at higher floors
#                                        (London/Toronto walk-forward, 2026-06-21 deepening study) for ~half the volume.
COVER_COST_MARGIN = 0.02               # the equal-share cover-cost guarantee needs Σask of all co-funded legs (held+new) < 1
#                                        (win any leg redeems s×$1 ≥ the s·Σask combined cost). We trip a loud SKIP at
#                                        Σask ≥ 1−this so a future P_FLOOR/MIN_EDGE drop or bucket_prob change that lets ≥3
#                                        buckets clear or admits overlapping mass FAILS LOUDLY, not silently (max Σask today
#                                        ≈0.85 with P_FLOOR=0.35, so 0.98 never false-trips current selection).
CLUSTER_MAX_ORDER_USD = 30.0          # $ backstop per order (≤ executor MAX_SIZE_USD=50); the 20-share cap below binds first
MAX_ORDER_SHARES = 50                 # per-order BINDING cap (user directive 2026-06-22): ≤50 shares/leg. A leg below the
#                                       $1 exchange min is SKIPPED, not forced (don't trust an edge so big it only shows on a sub-$1 quote)
# ── ENTRY SIZING (user directive 2026-06-22): all-taker; scale the equal-share cover-cost MAGNITUDE by CONFIDENCE ──
# = edge-strength × distance-from-bucket-boundary × market-agreement × lead-ramp. Far/early ⇒ small starter; fill toward
# 50/leg only close to the peak AND when confident. The cover-cost EQUAL-share invariant is preserved (only the magnitude
# scales). Below MIN_ORDER_SHARES the existing skip waits for confidence to firm. (Storminess/LLM volatile-gate + ±2 lotto
# are a SEPARATE layer.)
TAKER_ONLY = True                     # "现在都是 taker" — never rest a maker; always cross at the ask
CONF_FLOOR = 0.40                     # min confidence multiplier for a pick that clears the gates (never 0× a valid pick)
CONF_EDGE_FULL = 0.25                 # best-pick edge at/above which edge-confidence saturates to 1.0 (MIN_EDGE → CONF_FLOOR)
LEAD_RAMP = ((18.0, 0.50), (9.0, 0.75))   # hours-to-peak >18 ⇒ ×0.50, 9–18 ⇒ ×0.75, ≤9 ⇒ ×1.0 (scale-in as the forecast firms)
MKT_DISAGREE_GAP = 0.15               # our top-P bucket priced this far BELOW the market favorite ⇒ disagreement…
MKT_DISAGREE_DIST = 2                 # …but ONLY if the favorite is ≥2 buckets away. An ADJACENT (±1) favorite is the
#                                       normal off-by-one our 2-bucket window already covers — it must NOT trip
#                                       volatile / halve conf (else we sit out every normal day, as observed 2026-06-23).
MKT_DISAGREE_MULT = 0.5               # real disagreement ⇒ halve confidence (daemon-journey: tracks days the bet loses)
# ── REGIME-VOLATILE gate + ±2 LOTTO (user directive 2026-06-22) ──
# A stormy/convective/frontal peak (or a strong model-vs-market disagreement) makes the daily MAX unpredictable. Then
# we DON'T place the confident 2-bucket bet; from peak−9h we instead take a cheap ±2 longshot "lottery": every == bucket
# within ±2 of our top-P bucket priced <5c, ~$1 each. SEPARATE accounting — NOT in the cover-cost invariant, NOT counted
# against the budget cap (a few $ total). (The LLM regime layer, added next, refines `regime_volatile`.)
STORM_PRECIP_MM = 0.3                 # ≥ this precip at the peak hour ⇒ showery/convective ⇒ volatile
STORM_GUST_KMH = 45.0                 # ≥ this wind gust ⇒ frontal/windy ⇒ volatile
STORM_CLOUD_PCT = 85.0                # heavy cloud …
STORM_WIND_KMH = 25.0                 # … together with breezy ⇒ unstable mixing ⇒ volatile
LOTTO_LEAD_H = 9.0                    # take the lottery only within this many hours of the forecast peak
LOTTO_MAX_PRICE = 0.05               # only buckets priced < 5c (cheap longshots)
LOTTO_USD_PER_BUCKET = 1.0           # ~$1 per qualifying bucket (1c ⇒ 100 sh — NOT capped at 50; cost negligible)
LOTTO_SPREAD = 2                     # ±this many integer buckets around our top-P bucket
# VOLATILE regime ≠ skip (user 2026-06-23): it means DON'T bet the uncertain forecast EARLY — DEFER the normal 2-bucket
# bet to NEAR the peak, where the realized running_max resolves the bucket. Far + volatile ⇒ wait (+ lotto in its window);
# ≤ this lead ⇒ bet normally at reduced size. (Clear/predictable days still bet earlier via the lead-ramp.)
VOLATILE_ENTRY_LEAD_H = 3.0          # a volatile city only places its NORMAL bet within this many hours of the peak
VOLATILE_CONF_MULT = 0.7            # …and at this confidence multiplier (volatile day = bet a bit smaller even near peak)
MAKER_UNDERCUT = 0.01                 # post 1¢ better than the best bid (still a strict maker; post_only enforces it)
MAKER_FALLBACK_BELOW_ASK = 0.02       # one-sided book (no bid): post this far below the ask
ESCALATE_LEAD_MIN = 45                # open the taker-escalation window this many min before the forecast peak. Anchored
#                                       to the daemon's decision window (it confirms at peak−60, strikes ~peak−30): late =
#                                       mature running_max (dead buckets gone) + more maker fill-time; 45 leaves room for
#                                       the ~20-min cycle to fire before the peak. At escalation we RE-EVALUATE on fresh
#                                       data and only cross if the edge still holds (never chase a converged price).
DEFAULT_MAKER_WAIT_MIN = 90           # if the peak time is unknown: open the escalation window this many min after posting
RESERVE_STATUSES = ("FILLED", "PENDING", "RESTING", "AMBIGUOUS")   # statuses that tie up capital
MANAGE_STATUSES = ("RESTING", "AMBIGUOUS", "PENDING")              # statuses the manage loop reconciles
MONS = {mn: i for i, mn in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
# name, series, lat, lon, tz, icao, mu (center = F + mu), sigma (empirical forecast-error std vs resolved integer)
# Top 6 cities by FORECAST ACCURACY (user directive 2026-06-21, the 38-city MAE ranking): London 0.43 / Munich 0.51 /
# Toronto 0.56 / Milan 0.56 / Paris 0.57 / Madrid 0.69 — the tightest forecast-vs-actual tracking. Dropped Wellington
# (MAE 0.96) + Sao Paulo (0.99). NOTE μ/σ below are from the original ~70d cluster_legs; the fresh 130d run gives
# different σ (esp. Munich 1.25→~0.53) — recalibrate before trusting Munich/Madrid's abstain behavior.
CITIES = [
    # μ/σ RECALIBRATED 2026-06-21 from the clean day-ahead residual (resolved−F) over ==-resolved days only.
    # The prior σ (Munich 1.25 / Madrid 1.34 / Milan 0.94) were too wide — they wrongly included tail-resolved
    # (>=/<=) days and silently made Munich+Madrid ABSTAIN (max bucket-P never cleared 0.35). The true ==-day σ
    # are ~0.55-0.86 → all 7 now trade. (The real-time intraday nowcast was tested + found marginal vs this
    # recalibrated static — fair-baseline beats it; near-peak alpha collapses as the market prices the realized max.)
    # FINAL 7-city list (user 2026-06-22): the 6 strongest by 365d bucket-hit + Amsterdam (54%, beat half the old set).
    # Dropped Ankara (44%) + Helsinki (46%). μ RE-FIT 2026-06-23 to the PINNED model's (stations.json forecast_model)
    # lead0 bias = −bias (the systematic offset is lead-stable; σ grows with lead so σ is LEFT AS-IS = the old day-ahead
    # ==-day fit). First pass — a full day-ahead μ/σ re-fit waits for the daemon to accrue pinned-model residuals.
    ("Toronto",   10743, 43.67594, -79.62942, "America/Toronto", "CYYZ",  0.16, 0.70),   # gfs   (lead0 bias −0.16)
    ("London",    10006, 51.5053,  0.05528,   "Europe/London",   "EGLC",  0.13, 0.57),   # ukmo  (−0.13)
    ("Paris",     11168, 48.96228, 2.43654,   "Europe/Paris",    "LFPB",  0.00, 0.75),   # icon  (−0.00)
    ("Milan",     11343, 45.6306,  8.72811,   "Europe/Rome",     "LIMC",  0.41, 0.63),   # icon  (−0.41)
    ("Munich",    11272, 48.3538,  11.7861,   "Europe/Berlin",   "EDDM",  0.46, 0.57),   # icon  (−0.46)
    ("Madrid",    11345, 40.49341, -3.57225,  "Europe/Madrid",   "LEMD",  0.14, 0.86),   # meteofrance (−0.14; was 0.59 for ecmwf)
    ("Amsterdam", 11507, 52.3086,  4.76389,   "Europe/Amsterdam","EHAM",  0.12, 0.78),   # icon  (−0.12); σ PROVISIONAL (~Madrid-tier)
]


def rhu(x: float) -> int:
    return int(math.floor(x + 0.5))


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_prob(n: int, center: float, sigma: float) -> float:
    """P(resolved integer == n) under round(N(center, sigma))."""
    s = max(sigma, 1e-6)
    return norm_cdf((n + 0.5 - center) / s) - norm_cdf((n - 0.5 - center) / s)


def prob_for(comp: str, n: int, center: float, sigma: float) -> float:
    """P(this bucket WINS) under round(N(center, sigma)) for the three bucket kinds the cluster trades:
    '=='  → max rounds to n;  '<=' → max rounds to ≤ n (the low tail);  '>=' → max rounds to ≥ n (the high tail)."""
    s = max(sigma, 1e-6)
    if comp == "<=":
        return norm_cdf((n + 0.5 - center) / s)
    if comp == ">=":
        return 1.0 - norm_cdf((n - 0.5 - center) / s)
    return bucket_prob(n, center, sigma)


def bucket_alive(comp: str, n: int, rmax) -> bool:
    """Can this bucket still WIN given the realized running-max so far? (rmax None → alive.) Mirrors the daemon's
    _bucket_alive: '=='/'<=' alive iff n ≥ round(rmax) (a lower outcome already lost / the max hasn't passed n);
    '>=' alive iff n > round(rmax) (if the realized max already ≥ n it has WON — no upside, never open a new bet)."""
    if rmax is None:
        return True
    fb = rhu(rmax)
    return (n > fb) if comp == ">=" else (n >= fb)


def _reconstruct_from_journal() -> dict:
    """Rebuild state from OUR OWN append-only order journal when cluster_state.json is lost/corrupt — so we never
    re-buy our own bucket after a state-file loss, WITHOUT ever consulting the shared wallet. Last line per key
    wins (the file is append-ordered)."""
    s = {}
    if not JOURNAL.exists():
        return s
    try:
        for line in JOURNAL.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            k = r.pop("key", None)
            if k:
                s[k] = r
    except Exception:
        return {}
    return s


def load_state() -> dict:
    if STATE.exists():
        try:
            txt = STATE.read_text()
            if txt.strip():
                return json.loads(txt)
        except Exception:
            pass
    return _reconstruct_from_journal()          # state lost/empty → rebuild from our own journal (never the wallet)


def save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.replace(STATE)


def _journal_append(key: str, entry: dict) -> None:
    """Append this key's current entry to the order journal (our durable own-record; survives a state-file loss)."""
    try:
        with JOURNAL.open("a") as f:
            f.write(json.dumps({"key": key, **entry}, default=str) + "\n")
    except Exception:
        pass


def _save(state, key, log) -> bool:
    """Journal this key (durable own-record) then persist the state, or STOP the cycle on a write failure
    (proceeding would place orders we can't record → double-buy next cycle). Returns False on failure."""
    if key in state:
        _journal_append(key, state[key])
    try:
        save_state(state)
        return True
    except Exception as e:
        log.append(f"  FATAL: save_state failed ({e!r}) — STOPPING cycle, placing no more orders")
        return False


def forecast_peak(lat, lon, tz, day, model=None):
    """(peak_temp_C, peak_utc) for `day` from OpenMeteo hourly, or (None, None). `model` pins &models= (None ⇒ best_match)."""
    try:
        url, auth = _forecast_endpoint()                   # paid customer-api + apikey when OPEN_METEO_API_KEY set, else free
        j = httpx.get(url, params={**auth, "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m", "forecast_days": 4, "timezone": tz,
            **({"models": model} if model else {})}, headers=UA, timeout=25).json()
        h = j.get("hourly", {})
        best_v, best_t = None, None
        for t, v in zip(h.get("time", []), h.get("temperature_2m", [])):
            if v is None or t[:10] != day.isoformat():
                continue
            if best_v is None or v > best_v:
                best_v, best_t = v, t
        if best_v is None:
            return None, None
        peak_local = dt.datetime.fromisoformat(best_t).replace(tzinfo=ZoneInfo(tz))
        return best_v, peak_local.astimezone(dt.timezone.utc)
    except Exception:
        return None, None


def peak_storminess(lat, lon, tz, day, model=None):
    """(stormy: bool, met: dict) at the day's forecast-peak hour — showery/convective/frontal signals that make the
    daily MAX unpredictable. (False, {}) on any failure (never raises). One Forecast-API call, pinned model."""
    try:
        url, auth = _forecast_endpoint()
        vrs = "temperature_2m,cloud_cover,wind_speed_10m,wind_gusts_10m,precipitation"
        p = {**auth, "latitude": lat, "longitude": lon, "hourly": vrs, "forecast_days": 4, "timezone": tz}
        if model:
            p["models"] = model
        h = httpx.get(url, params=p, headers=UA, timeout=25).json().get("hourly", {})
        times, temps = h.get("time", []), h.get("temperature_2m", [])
        bi = None
        for i, (t, v) in enumerate(zip(times, temps)):
            if v is None or t[:10] != day.isoformat():
                continue
            if bi is None or v > temps[bi]:
                bi = i
        if bi is None:
            return False, {}
        def g(k):
            arr = h.get(k) or []
            return arr[bi] if bi < len(arr) else None
        met = {"cloud": g("cloud_cover"), "wind": g("wind_speed_10m"), "gust": g("wind_gusts_10m"), "precip": g("precipitation")}
        stormy = ((met["precip"] or 0) >= STORM_PRECIP_MM or (met["gust"] or 0) >= STORM_GUST_KMH
                  or ((met["cloud"] or 0) >= STORM_CLOUD_PCT and (met["wind"] or 0) >= STORM_WIND_KMH))
        return bool(stormy), met
    except Exception:
        return False, {}


# ── LLM REGIME refinement (item 6, user 2026-06-22): the agent (SAME OpenAI key/model as the daemon) makes the final
# predictable-vs-volatile call from the city's climate profile + peak meteorology + model-vs-market agreement. Reuses the
# daemon "journey" that empirically declined ~87% of would-lose days. None (no key / any error) ⇒ caller keeps the
# mechanical (storminess OR market-disagree) verdict. The cluster still picks buckets mechanically — the LLM only judges
# the REGIME (bet the 2-bucket window, or sit out / take the cheap ±2 lotto). ──
_CITY_REGIME = {
    "Toronto":   "Continental, on Lake Ontario — a summer lake-breeze can cap/delay the afternoon max; isolated convection possible; otherwise well-behaved.",
    "London":    "Maritime temperate (Thames estuary) — usually a smooth single afternoon peak; occasional North-Sea sea-breeze or frontal cloud caps it; forecast reliable.",
    "Paris":     "Semi-continental (Le Bourget, inland NE of the city) — reliable in settled weather; summer afternoon convection can cap/sharpen the peak.",
    "Milan":     "Po Valley basin, humid/stagnant — FREQUENT summer afternoon thunderstorms/convection that abruptly cap the max; one of the harder cities in summer.",
    "Munich":    "Pre-Alpine continental — summer convection/thunderstorms common; foehn can spike the max; peak often uncertain on convective days.",
    "Madrid":    "High continental plateau (~650 m), hot dry summers — usually VERY predictable (clear, dry, strong insolation, sharp peak); only rare storms disturb it.",
    "Amsterdam": "Maritime, North-Sea influence (Schiphol, low/flat) — frequent cloud, wind and sea-breeze; the max is often capped and a touch less predictable than inland.",
}
_REGIME_SYSTEM = """You judge whether TODAY's daily-HIGH-temperature for one city is PREDICTABLE enough for a \
low-frequency bot to bet the 2 integer-°C buckets nearest the forecast peak, or too VOLATILE to bet. You do NOT pick \
buckets — only the REGIME. Call it VOLATILE when the daily MAX is hard to pin: convective/showery/thundery afternoon, a \
front or strong wind, OR a strong model-vs-market disagreement (our most-likely bucket priced well BELOW the market \
favorite — the book may know something our single model misses). Call it PREDICTABLE on a clear, calm, well-mixed day \
where forecast and market agree. Weigh the city's CLIMATE PROFILE against the peak METEOROLOGY given (cloud/wind/gust/\
precip). Output ONLY via emit_regime: volatile (bool), confidence 0-1, reason (1-2 short plain sentences for the log)."""
_REGIME_SCHEMA = {"type": "object", "properties": {
    "volatile": {"type": "boolean"}, "confidence": {"type": "number"},
    "reason": {"type": "string", "description": "1-2 sentence plain-language why (shown in the city log)"}},
    "required": ["volatile", "reason"]}
_REGIME_TOOL = {"type": "function", "function": {"name": "emit_regime",
                "description": "Emit the regime verdict (predictable vs volatile). The only allowed output.",
                "parameters": _REGIME_SCHEMA}}


def _llm_regime(ctx: dict) -> dict | None:
    """Sync OpenAI call → {volatile, confidence, reason} or None (no OPENAI_API_KEY / any error ⇒ caller uses the
    mechanical verdict). Mirrors agent._propose_openai (same URL/auth/model/tool-forcing) but synchronous + 20s cap."""
    if not agent.enabled():
        return None
    try:
        body = {"model": agent._model(),
                "messages": [{"role": "system", "content": _REGIME_SYSTEM},
                             {"role": "user", "content": json.dumps(ctx, ensure_ascii=False, default=str)}],
                "tools": [_REGIME_TOOL], "tool_choice": {"type": "function", "function": {"name": "emit_regime"}}}
        headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "content-type": "application/json"}
        r = httpx.post(agent.OPENAI_URL, headers=headers, json=body, timeout=20.0)
        if r.status_code != 200:
            return None
        tc = r.json()["choices"][0]["message"]["tool_calls"][0]
        if tc["function"]["name"] == "emit_regime":
            return dict(json.loads(tc["function"]["arguments"]))
    except Exception:
        return None
    return None


def running_max(icao, tz, day):
    try:
        ms = httpx.get("https://aviationweather.gov/api/data/metar",
                       params={"ids": icao, "format": "json", "hours": 16}, headers=UA, timeout=20).json()
    except Exception:
        return None
    z = ZoneInfo(tz)
    temps = []
    for m in ms if isinstance(ms, list) else []:
        t, ot = m.get("temp"), m.get("obsTime")
        if t is None or ot is None:
            continue
        try:
            if dt.datetime.fromtimestamp(int(ot), dt.timezone.utc).astimezone(z).date() == day:
                temps.append(float(t))
        except (ValueError, OSError):
            continue
    return max(temps) if temps else None


def upcoming_event(series, today):
    try:
        evs = httpx.get("https://gamma-api.polymarket.com/events", params={
            "series_id": series, "closed": "false", "limit": 4, "order": "endDate", "ascending": "true"},
            headers=UA, timeout=25).json()
    except Exception:
        return None, None, {}
    for e in evs if isinstance(evs, list) else []:
        m = re.search(r"-on-([a-z]+)-(\d+)-(\d{4})", e.get("slug", ""))
        if not m or m.group(1) not in MONS:
            continue
        day = dt.date(int(m.group(3)), MONS[m.group(1)], int(m.group(2)))
        if day < today - dt.timedelta(days=1):             # keep ≥(UTC today−1) so a station-local-today event is NEVER
            continue                                       # skipped by the UTC/local offset (closed:false already drops settled)
        buckets = {}                                      # == integer buckets: {n: token}
        tails = []                                        # tail buckets: [(comparator, threshold, token)] — bought AND summed for health
        for mk in e.get("markets", []):
            t = mk.get("groupItemTitle", "") or ""
            low = t.lower()
            toks = mk.get("clobTokenIds")
            if isinstance(toks, str):
                toks = json.loads(toks)
            if not toks:
                continue
            mm = re.search(r"(-?\d+)", t)
            if "above" in low or "below" in low or "higher" in low or "lower" in low or "+" in t:
                if mm:                                    # "X or below" → <=X ; "X or higher"/"X+" → >=X
                    comp = ">=" if ("above" in low or "higher" in low or "+" in t) else "<="
                    tails.append((comp, int(mm.group(1)), toks[0]))
                continue
            if mm:
                buckets[int(mm.group(1))] = toks[0]
        return e, day, buckets, tails
    return None, None, {}, []


def best_book(token):
    """(best_bid, best_ask, ask_depth_usd) for a token. depth = $ resting within 5¢ of the best ask. Any field
    None on an empty side; (None, None, 0.0) on an HTTP/parse error."""
    try:
        bk = httpx.get("https://clob.polymarket.com/book", params={"token_id": token}, headers=UA, timeout=20).json()
    except Exception:
        return None, None, 0.0
    asks = bk.get("asks", []) or []
    bids = bk.get("bids", []) or []
    ba = min((float(a["price"]) for a in asks), default=None)
    bb = max((float(b["price"]) for b in bids), default=None)
    cap = sum(float(a["size"]) * float(a["price"]) for a in asks if float(a["price"]) <= ba + 0.05) if ba is not None else 0.0
    return bb, ba, cap


def maker_price_for(bid, ask):
    """A strict-maker BUY limit: 1¢ better than the best bid but below the ask (post_only floors+guards it too).
    One-sided book (no bid) → MAKER_FALLBACK_BELOW_ASK under the ask."""
    if bid is not None and bid > 0:
        p = min(bid + MAKER_UNDERCUT, ask - 0.01)
    else:
        p = ask - MAKER_FALLBACK_BELOW_ASK
    return round(max(0.01, p), 2)


def outstanding(state, today):
    """Capital tied in still-open (not-yet-settled) cluster positions = market_date >= today, in a reserve status."""
    tot = 0.0
    for k, e in state.items():
        try:
            d = dt.date.fromisoformat(k.split("|")[1])
        except (ValueError, IndexError):
            continue
        if d >= today and e.get("status") in RESERVE_STATUSES:
            tot += float(e.get("cost") or 0.0)
    return tot


def storage_health(state, today) -> list[str]:
    """Startup audit of OUR OWN data storage — confirm the state file + journal are present, parseable and
    mutually consistent, and print the open book we're about to act on, so a bad/empty load is VISIBLE in the
    log before any order is placed (not discovered after a double-buy)."""
    out = ["  ── storage health ──",
           f"  state: {'present' if STATE.exists() else 'MISSING'} ({STATE})",
           f"  journal: {'present' if JOURNAL.exists() else 'MISSING'} ({JOURNAL})"]
    bad, open_pos = 0, []
    for k, e in state.items():
        try:
            d = dt.date.fromisoformat(k.split("|")[1])
        except (ValueError, IndexError):
            bad += 1
            continue
        if d >= today and e.get("status") in RESERVE_STATUSES:
            open_pos.append((k, e.get("status"), float(e.get("cost") or 0.0)))
    out.append(f"  entries: {len(state)} ({bad} unparseable) · open (≥today, reserved): {len(open_pos)}")
    for k, s, c in sorted(open_pos):
        out.append(f"      HOLD {k}  {s}  ${c:.2f}")
    j = _reconstruct_from_journal()                       # state keys MUST be a subset of journal keys (we journal before save)
    missing = [k for k in state if k not in j]
    out.append(f"  journal keys: {len(j)} · state keys: {len(state)} · "
               + ("consistent ✓" if not missing else f"⚠ {len(missing)} state keys NOT in journal: {missing[:3]}"))
    if bad:
        out.append(f"  ⚠ {bad} unparseable state keys — inspect data/cluster_state.json")
    return out


def cluster_for(name, series, lat, lon, tz, icao, mu, sigma, today):
    """Return {event, day, picks[{comp,n,label,token,ask,bid,depth,P,edge}], buckets, peak_utc, icao, city, center}
    or None (no event) / {"skip": reason}. comp ∈ {'==','<=','>='}: the cluster now trades the == integer buckets
    AND the two tail buckets (low '<=n', high '>=n'), so edge-of-range days are no longer skipped wholesale."""
    e, day, buckets, tails = upcoming_event(series, today)
    if not e or not buckets:
        return None
    # ENTRY-TIMING GATE (user-confirmed 2026-06-22): never order for resolution day D before D−1 12:00 CITY-LOCAL.
    # Earlier than that the next-day forecast is unreliable (the bucket pick flips ~52% beyond ~24-30h lead) and
    # the book barely moves — this stops the "bought ~38h early" case (e.g. Milan opening the day-after-next).
    _z = ZoneInfo(tz)
    _now_local = dt.datetime.now(_z)
    _today_local = _now_local.date()                       # STATION-local date — the day/today comparisons below MUST be in
    #   the station's own calendar (UTC `today` is off-by-one in the station's local off-hours → running_max mis-gated).
    _entry_open = dt.datetime.combine(day - dt.timedelta(days=1), dt.time(12, 0), tzinfo=_z)
    if _now_local < _entry_open:
        return {"skip": f"too early (entry opens D−1 12:00 local = {_entry_open:%m-%d %H:%M} {tz})",
                "city": name, "day": day}
    F, peak_utc = forecast_peak(lat, lon, tz, day, _FCMODEL.get(icao))
    if F is None:
        return None
    rmax = running_max(icao, tz, day) if day == _today_local else None   # realized max only when it IS the station's today
    if day == _today_local and rmax is None:               # AWC down → can't verify realized floor → don't risk a dead bucket
        return {"skip": "no running_max (AWC down)", "city": name, "day": day}
    center = F + mu
    if rmax is not None:
        center = max(center, rmax)
    # MARKET HEALTH (mirror the daemon): fetch every bucket's book ONCE — == keyed ('==', n), tails keyed (comp, T)
    # — reused for both the health-sum and the candidate edges. The YES-prices of ALL buckets (== PLUS the tails)
    # sum to ~1 on a clean book; off [HEALTH_SUM_LO, HEALTH_SUM_HI] = stale/broken/empty book → skip the city.
    # (== alone biases LOW on an edge-of-range day where a tail holds the mass, so we sum the FULL book.)
    tokens = {("==", n): tok for n, tok in buckets.items()}        # (comp, n) → token, for == and tail buckets alike
    tokens.update({(comp, n): tok for comp, n, tok in tails})
    books = {k: best_book(t) for k, t in tokens.items()}

    def _mid(bb, ba):
        return ((bb + ba) / 2.0) if (bb is not None and ba is not None) else ba   # mid where two-sided, else the ask
    psum = sum(m for m in (_mid(bb, ba) for bb, ba, _cap in books.values()) if m is not None)
    if not (HEALTH_SUM_LO <= psum <= HEALTH_SUM_HI):
        return {"skip": f"unhealthy book: Σbucket-price {psum:.2f} off [{HEALTH_SUM_LO:.2f},{HEALTH_SUM_HI:.2f}] (stale/broken)",
                "city": name, "day": day}
    # CANDIDATES = the == integer buckets near the center PLUS the two tails. The cluster now trades ==n / <=n (low
    # tail) / >=n (high tail), so an edge-of-range day (cool → "X or below", hot → "X or higher") is no longer
    # skipped wholesale. bucket_alive drops a bucket the realized max already lost/won; MAX_EDGE drops an
    # implausibly-large disagreement (likely our error / Gaussian tail over-confidence — user directive 2026-06-22).
    cands = [("==", n) for n in range(int(math.floor(center)) - 2, int(math.ceil(center)) + 3) if n in buckets]
    cands += [(comp, n) for comp, n, _tok in tails]
    picks = []
    for comp, n in cands:
        if not bucket_alive(comp, n, rmax):                # realized max already lost/won this bucket → don't open
            continue
        P = prob_for(comp, n, center, sigma)
        if P < P_FLOOR:                                    # forecast doesn't concentrate enough here → skip
            continue
        bb, ba, cap = books[(comp, n)]                     # reuse the health-pass fetch (no double call)
        if ba is None:
            continue
        if bb is not None and (ba - bb) > MAX_SPREAD:      # book too wide on this bucket → illiquid/unreliable → skip it
            continue
        edge = P - ba
        if edge < MIN_EDGE:                                # priced too rich vs our probability → skip
            continue
        if edge > MAX_EDGE:                                # wild disagreement (we ≫ market) → likely OUR error → skip
            continue
        picks.append({"comp": comp, "n": n, "label": f"{comp}{n}", "token": tokens[(comp, n)],
                      "ask": ba, "bid": bb, "depth": cap, "P": P, "edge": edge})
    if not picks:
        return {"skip": f"no bucket clears P≥{P_FLOOR:.2f} & {MIN_EDGE:.2f}≤edge≤{MAX_EDGE:.2f} (center {center:.1f})", "city": name, "day": day}
    # CONFIDENCE for sizing (Q1): edge-strength × distance-from-bucket-boundary × market-agreement. Scales only the
    # equal-share MAGNITUDE (cover-cost invariant untouched); the lead-ramp is applied by the caller (it has now vs peak).
    best_edge = max(pk["edge"] for pk in picks)
    edge_conf = min(1.0, CONF_FLOOR + max(0.0, best_edge - MIN_EDGE) / (CONF_EDGE_FULL - MIN_EDGE) * (1.0 - CONF_FLOOR))
    boundary_factor = min(1.0, 0.6 + (0.5 - abs(center - round(center))) * 0.8)   # on an X.5 boundary→0.6, on an integer→1.0
    top = max(picks, key=lambda pk: pk["P"])                       # our most-likely bucket
    fav_k, fav_mid = None, -1.0
    for k, (bb, ba, _c) in books.items():
        m = _mid(bb, ba)
        if m is not None and m > fav_mid:
            fav_k, fav_mid = k, m
    top_bb, top_ba, _ = books[(top["comp"], top["n"])]
    top_mid = _mid(top_bb, top_ba)
    disagree = bool(fav_k is not None and abs(fav_k[1] - top["n"]) >= MKT_DISAGREE_DIST   # ≥2 buckets away = real
                    and top_mid is not None and top_mid < fav_mid - MKT_DISAGREE_GAP)     # (±1 adjacent ⇒ window covers it)
    conf_base = edge_conf * boundary_factor * (MKT_DISAGREE_MULT if disagree else 1.0)
    # REGIME-VOLATILE (mechanical; the LLM layer refines this next): a stormy/convective peak OR a strong model-vs-market
    # disagreement ⇒ the daily MAX is unpredictable ⇒ no confident 2-bucket bet (caller does the ±2 lotto near the peak).
    stormy, met = peak_storminess(lat, lon, tz, day, _FCMODEL.get(icao))
    mech_volatile = bool(stormy or disagree)
    # LLM REFINES the verdict (item 6): city climate profile + peak met + our-top-vs-market favorite → predictable/volatile.
    # None (no OPENAI key / error) ⇒ keep the mechanical verdict.
    llm = _llm_regime({
        "city": name, "climate_profile": _CITY_REGIME.get(name, ""),
        "forecast_peak_C": round(F, 1), "running_max_C": rmax, "met_at_peak": met, "mechanical_stormy": stormy,
        "our_top_bucket": top["label"], "our_top_P": round(top["P"], 2),
        "market_favorite_bucket": (f"{fav_k[0]}{fav_k[1]}" if fav_k else None),
        "our_top_price": (round(top_mid, 2) if top_mid is not None else None),
        "market_favorite_price": (round(fav_mid, 2) if fav_mid >= 0 else None),
        "model_vs_market_disagree": disagree,
        "candidate_buckets": [{"b": pk["label"], "P": round(pk["P"], 2), "price": round(pk["ask"], 2)} for pk in picks],
    })
    regime_volatile = bool(llm["volatile"]) if (llm and "volatile" in llm) else mech_volatile
    llm_reason = llm.get("reason") if llm else None
    # ±2 LOTTO candidates around our top-P bucket: still-alive == buckets priced <5c (used only when volatile + near peak).
    lotto = []
    for nn in range(top["n"] - LOTTO_SPREAD, top["n"] + LOTTO_SPREAD + 1):
        bk = books.get(("==", nn))
        if bk and bucket_alive("==", nn, rmax) and bk[1] is not None and bk[1] < LOTTO_MAX_PRICE:
            lotto.append({"comp": "==", "n": nn, "label": f"=={nn}", "token": tokens[("==", nn)], "ask": bk[1], "depth": bk[2]})
    return {"event": e, "day": day, "picks": picks, "buckets": buckets, "peak_utc": peak_utc,
            "icao": icao, "city": name, "center": center, "F": F, "rmax": rmax,
            "conf_base": round(conf_base, 3), "disagree": disagree,
            "regime_volatile": regime_volatile, "stormy": stormy, "met": met, "lotto": lotto, "llm_reason": llm_reason}


_COMP = {"==": Comparator.EQ, "<=": Comparator.LE, ">=": Comparator.GE}


def _decision(name, icao, day, n, token, label, price, shares, taker, comp="=="):
    return Decision(city=name, station_id=icao, local_date=day, label=label, comparator=_COMP[comp],
                    threshold=n, token_yes=token, action="BET", side="BUY_YES",
                    order_type=("taker" if taker else "maker"),
                    confidence=None, market_yes=round(price, 4), maker_price=round(price, 4),
                    size_shares=float(shares), health_ok=True)


def _taker_result(res, shares, ask):
    """Interpret a taker FAK OrderResult → (status, filled_shares, cost). An ok-but-unconfirmed fill (lost
    response, or ok with no/zero parsed amount) is AMBIGUOUS — keep the full reserve (conservative); it stays
    counted and outstanding() releases it once its market_date rolls past. Never silently assume a fill we can't see."""
    filled = res.filled
    if res.ambiguous or (res.ok and (filled is None or filled <= 0)):
        return "AMBIGUOUS", float(shares), round(shares * ask, 2)
    if res.ok and filled and filled > 0:
        return "FILLED", filled, round(filled * ask, 2)
    return "NOFILL", 0.0, 0.0


def place_lotto(sel, state, iso, now, dry, log) -> None:
    """Independent LOTTO placer (complex-regime longshot). Each ±2 cheap bucket (<5c) bought ~$1 TAKER. SEPARATE
    accounting: keys 'LOTTO|city|day|label', NOT in the cover-cost invariant, NOT counted against the budget cap
    (a few $ total). Idempotent — skips a leg already placed this day. Never raises into the cycle."""
    name, day = sel["city"], sel["day"]
    for c in sel.get("lotto", []):
        key = f"LOTTO|{name}|{day.isoformat()}|{c['label']}"
        if key in state:                                    # already hold this lotto leg
            continue
        ask = c["ask"]
        shares = math.ceil(LOTTO_USD_PER_BUCKET / ask) if ask and ask > 0 else 0   # ~$1; 1c ⇒ 100 sh (no 50 cap)
        if shares <= 0:
            continue
        cost = round(shares * ask, 2)
        if dry:
            log.append(f"  {name} LOTTO {c['label']} {'WARMUP/observe' if (dry and not ex._dry_run()) else 'DRY'} "
                       f"would TAKER-BUY {shares}sh @ {ask:.2f} (${cost:.2f})")
            continue
        state[key] = {"status": "PENDING", "order_id": None, "token": c["token"], "ask": ask, "shares": float(shares),
                      "cost": cost, "ts": iso, "city": name, "station_id": sel["icao"], "comparator": "==",
                      "threshold": c["n"], "lotto": True}
        if not _save(state, key, log):
            return
        res = ex.place_from_decision(_decision(name, sel["icao"], day, c["n"], c["token"], c["label"], ask, shares, taker=True),
                                     now_iso=iso, ttl_min=None)
        status, filled, cst = _taker_result(res, shares, ask)
        state[key].update(status=status, order_id=res.order_id, filled=filled, cost=cst)
        _save(state, key, log)
        log.append(f"  {name} LOTTO {c['label']} TAKER-BUY {shares}sh @ {ask:.2f} → {status} filled={filled}")


def _reeval(name, comp, n, token, day, today):
    """Fresh re-evaluation of one bucket at escalation time: {alive, ask, P, edge} or None (forecast/book down).
    alive=False ⇒ the bucket can no longer WIN (the realized max already lost it for ==/<=, or post-peak it can't
    reach n for ==/>=) → give up the remainder. Used to gate the taker fallback so we only cross at the CURRENT
    ask when the edge still holds — never chase a price that converged past us. (MAX_EDGE is NOT applied here: at
    escalation we have the realized trajectory backing a large edge, unlike the forecast-only open in cluster_for.)"""
    row = next((c for c in CITIES if c[0] == name), None)
    if row is None:
        return None
    _, _series, lat, lon, tz, icao, mu, sigma = row
    F, peak_utc = forecast_peak(lat, lon, tz, day, _FCMODEL.get(icao))
    if F is None:
        return None
    rmax = running_max(icao, tz, day) if day == dt.datetime.now(ZoneInfo(tz)).date() else None  # station-local, not UTC today
    center = F + mu
    if rmax is not None:
        center = max(center, rmax)
    if not bucket_alive(comp, n, rmax):                                # realized max already lost (==/<=) / won (>=) it
        return {"alive": False, "ask": None, "P": 0.0, "edge": -1.0}
    if (comp in ("==", ">=") and peak_utc is not None and rmax is not None and n > rhu(rmax)
            and dt.datetime.now(dt.timezone.utc) > peak_utc):          # post-peak & still needs the max to reach n → can't rise → dead
        return {"alive": False, "ask": None, "P": 0.0, "edge": -1.0}    # (don't escalate a committed leg into a fake post-peak dump)
    bb, ba, cap = best_book(token)
    if ba is None:
        return None
    P = prob_for(comp, n, center, sigma)
    return {"alive": True, "ask": ba, "P": P, "edge": P - ba}


def _siblings(state, key):
    """(combined cost, min redemption-shares) of OTHER reserve-status legs of the SAME city|date, so a leg's
    escalation taker can be sized to keep the PER-DAY cover-cost invariant (win any leg ⇒ redeem ≥ combined cost)
    — not just its own. Redemption-shares = filled (if FILLED) else the reserved shares (a partial fill redeems
    only what filled)."""
    prefix = "|".join(key.split("|")[:2]) + "|"
    cost, min_sh = 0.0, float("inf")
    for k, e in state.items():
        if k == key or not k.startswith(prefix):
            continue
        if e.get("status") in RESERVE_STATUSES:
            cost += float(e.get("cost") or 0.0)
            min_sh = min(min_sh, float(e.get("filled") or e.get("shares") or 0.0))
    return cost, min_sh


def _manage(state, key, e, iso, today, log, remaining) -> float:
    """Reconcile one RESTING/AMBIGUOUS/PENDING entry using OUR OWN signals only — the order's matched size
    (filled_size, NOT co-hold-confounded) + cancel semantics. NEVER reads the shared wallet, so it can't
    mis-attribute the daemon's / farmer's / your manual positions to the cluster. Returns the delta to add to
    `out`. FAIL-CLOSED on escalation: only cross with a taker once a clean cancel proves the maker was still OPEN."""
    st = e.get("status")
    oid = e.get("order_id")
    shares = float(e.get("shares") or 0)
    mprice = float(e.get("maker_price") or e.get("ask") or 0)
    prev_cost = float(e.get("cost") or 0)
    esc = e.get("escalate_at")
    past = bool(esc) and iso >= esc

    # our OWN order's matched size is the clean fill signal (works while the order is live / partially filled)
    already = float(e.get("filled") or 0.0)                 # shares matched on a PRIOR partial (0 normally)
    if oid:
        m = ex.filled_size(oid)
        if m is not None and m > 0:
            already = m
            if m >= shares - 1e-9:                          # FULLY filled → done
                newcost = round(m * mprice, 2)
                e.update(status="FILLED", filled=m, cost=newcost)
                log.append(f"  {e.get('city')} {key}: maker FILLED {m}sh")
                return newcost - prev_cost
            if not past:                                    # PARTIAL before escalate → record it, KEEP RESTING (re-poll next cycle)
                newcost = round(m * mprice, 2)
                e.update(filled=m, cost=newcost)            # status stays RESTING (still in MANAGE_STATUSES)
                log.append(f"  {e.get('city')} {key}: maker PARTIAL {m:.0f}/{int(shares)}sh — recorded, remainder resting")
                return newcost - prev_cost
            # PARTIAL + past escalate → complete the remainder below (already = m)

    # PENDING (a crash lost the order_id) / AMBIGUOUS-without-id: we can't confirm an off-book fill from our own
    # signals, so we neither taker (would risk a double) nor free (conservative) — it stays counted and
    # outstanding() releases it once its market_date rolls past. (Reading the shared wallet here is exactly the
    # cross-bot mis-attribution bug we removed; the clean fix is a separate funder.)
    if st in ("PENDING", "AMBIGUOUS"):
        return 0.0

    # RESTING maker:
    if not past:
        return 0.0                                         # before escalate, unfilled → leave it resting
    # ESCALATE (near the peak): re-evaluate on FRESH data, cancel, then cross a taker for the REMAINDER (shares-already)
    # ONLY if the edge still holds. A partial fill is never zeroed — if we give up completing, we KEEP what filled.
    already_cost = round(already * mprice, 2)               # value of the partial we already hold

    def _keep_partial(reason):                             # give up the remainder but PRESERVE any partial position
        if already > 0:
            e.update(status="FILLED", filled=already, cost=already_cost)
            log.append(f"  {e.get('city')} {key}: {reason} → keep PARTIAL {already:.0f}sh (${already_cost:.2f})")
            return already_cost - prev_cost
        e.update(status="NOFILL", cost=0.0)
        log.append(f"  {e.get('city')} {key}: {reason} → NOFILL (freed)")
        return 0.0 - prev_cost

    day = dt.date.fromisoformat(key.split("|")[1])
    n = int(e.get("threshold"))
    comp = e.get("comparator", "==")
    ev = _reeval(e.get("city"), comp, n, e.get("token") or "", day, today)
    if ev is None:                                         # forecast/book unavailable → can't decide → leave resting
        log.append(f"  {e.get('city')} {key}: escalate deferred — re-eval unavailable")
        return 0.0
    c = ex.cancel_order(oid, now_iso=iso, reason="cluster escalate") if oid else None
    if c is not None and not c.ok:                         # cancel unconfirmed → order may still be live → DON'T taker
        log.append(f"  {e.get('city')} {key}: escalate deferred — cancel unconfirmed ({c.detail})")
        return 0.0
    if c is not None and c.gone:                           # terminal: usually MATCHED, but `gone` also covers already-cancelled/not-found
        fsz = ex.filled_size(oid)
        if fsz and fsz > 0:                                # CONFIRMED fill size → trust it
            newcost = round(fsz * mprice, 2)
            e.update(status="FILLED", filled=fsz, cost=newcost)
            log.append(f"  {e.get('city')} {key}: maker matched at escalate → FILLED {fsz:.0f}sh")
            return newcost - prev_cost
        if already > 0:                                    # at least the recorded partial is real → keep it
            e.update(status="FILLED", filled=already, cost=already_cost)
            log.append(f"  {e.get('city')} {key}: gone, size unconfirmed → keep PARTIAL {already:.0f}sh (${already_cost:.2f})")
            return already_cost - prev_cost
        e.update(status="AMBIGUOUS")                       # gone but NO confirmed fill → do NOT assume a full buy; hold the reserve
        log.append(f"  {e.get('city')} {key}: gone but fill unconfirmed (no partial) → AMBIGUOUS (reserve held)")
        return 0.0
    # clean cancel (remainder was OPEN ⇒ did NOT fill) → decide on FRESH data; PRESERVE any partial already held
    if not ev["alive"]:                                    # bucket now below the realized max → dead → give up the rest
        return _keep_partial("bucket dead at escalate")
    if ev["edge"] < MIN_EDGE:                              # price converged past our edge → don't chase the rest
        return _keep_partial(f"edge gone (edge{ev['edge']:+.2f} @ ask {ev['ask']:.2f})")
    fresh_ask = ev["ask"]
    target_rem = max(0, int(shares) - int(already))        # shares still needed to reach the cover-cost target
    if target_rem < MIN_ORDER_SHARES:                      # essentially complete already → keep what we have
        return _keep_partial("partial already at target")
    eff_remaining = remaining + prev_cost                  # this leg's OWN reserve is being re-deployed, not new capital
    s_cap = (min(target_rem, MAX_ORDER_SHARES - int(already), math.floor(CLUSTER_MAX_ORDER_USD / fresh_ask),
                 math.floor(eff_remaining / fresh_ask)) if fresh_ask > 0 else 0)   # complete-only + ≤50 total + ≤$30 + ≤ cap
    sib_cost, sib_min = _siblings(state, key)
    if sib_min < float("inf"):                             # siblings exist → per-day cover-cost, with this leg's already-held portion folded in
        if fresh_ask >= 1.0 - COVER_COST_MARGIN:           # would break the equal-share guarantee → keep the partial
            return _keep_partial(f"escalate ask {fresh_ask:.2f} too high to co-fund")
        # this leg redeems (already + s2), costs (already_cost + s2·fresh_ask); each leg must cover the day total
        s_lo = math.ceil((sib_cost + already_cost - already) / (1.0 - fresh_ask))   # this leg redeems ≥ full-day cost
        s_hi = math.floor((sib_min - sib_cost - already_cost) / fresh_ask) if fresh_ask > 0 else s_cap   # smallest sibling covers total
        shares2 = min(s_cap, s_hi)
        if shares2 < max(MIN_ORDER_SHARES, s_lo):
            return _keep_partial(f"can't complete within cover-cost (sib ${sib_cost:.2f}/{sib_min:.0f}sh, s∈[{s_lo},{s_hi}], cap {s_cap})")
    else:
        shares2 = s_cap                                    # no siblings; fresh_ask<1 (edge gate) ⇒ invariant auto-holds
        if shares2 < MIN_ORDER_SHARES:
            return _keep_partial(f"re-size {shares2}sh < min @ {fresh_ask:.2f}")
    res = ex.place_from_decision(
        _decision(e.get("city"), e.get("station_id", ""), day, n, e.get("token") or "", key.split("|")[-1], fresh_ask, shares2, taker=True, comp=comp),
        now_iso=iso, ttl_min=None)
    status, t_filled, t_cost = _taker_result(res, shares2, fresh_ask)
    tot_filled = round(already + t_filled, 4)
    tot_cost = round(already_cost + t_cost, 2)
    if status == "NOFILL" and already > 0:                  # taker added nothing, but the partial is REAL → keep it FILLED, never zero it
        status = "FILLED"
    e.update(status=status, order_id=(res.order_id or oid), filled=tot_filled, cost=tot_cost, ask=fresh_ask)
    log.append(f"  {e.get('city')} {key}: escalated TAKER {shares2}sh @ {fresh_ask:.2f} (P{ev['P']:.2f})"
               + (f" + partial {already:.0f}sh" if already > 0 else "") + f" → {status} total filled={tot_filled:.0f}")
    return tot_cost - prev_cost


def cycle(total_cap, force_dry=False, show_storage=False):
    # force_dry = WARMUP: compute + log the full plan but place NOTHING, even when POLY_DRY_RUN=0. main() runs the
    # first --warmup cycles this way so a fresh `launchctl load` never fires an order before the operator has seen
    # one full reasoning log (the "started and immediately traded" complaint).
    env_dry = ex._dry_run()
    dry = env_dry or force_dry
    warming = force_dry and not env_dry
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()
    iso = now.strftime(TSFMT)
    state = load_state()
    out = outstanding(state, today)
    mode = "WARMUP/observe-only" if warming else ("LIVE" if not dry else "DRY")
    log = [f"[{now:%Y-%m-%d %H:%MZ}] cluster cycle ({mode}) · outstanding ${out:.0f}/{total_cap:.0f}"]
    if show_storage:
        log += storage_health(state, today)

    # ── 1) RECONCILE our own maker lifecycle — OWN state + order ids only, never the shared wallet ──
    # Gated on env_dry (NOT dry): risk-management on already-LIVE orders (cancel a dead maker, escalate a due one)
    # must keep running during WARMUP — warmup only suppresses opening NEW positions, never managing committed ones.
    if not env_dry:
        for key, e in list(state.items()):
            if e.get("status") not in MANAGE_STATUSES:
                continue
            try:
                before = (e.get("status"), e.get("filled"), e.get("cost"))     # persist on ANY field change (a partial fill keeps status=RESTING but updates filled/cost)
                out += _manage(state, key, e, iso, today, log, max(0.0, total_cap - out))
                if (e.get("status"), e.get("filled"), e.get("cost")) != before and not _save(state, key, log):
                    return log
            except Exception as me:                        # a corrupt/partial entry must NOT abort the cycle
                log.append(f"  manage {key}: err {me!r} — left unchanged")

    # ── 2) SELECT + place for each city's upcoming event ──
    for c in CITIES:
        name = c[0]
        try:
            sel = cluster_for(*c, today)
        except Exception as ee:
            log.append(f"  {name}: err {ee!r}")
            continue
        if sel is None:
            continue
        if sel.get("skip"):
            log.append(f"  {name} {sel.get('day')} → SKIP ({sel['skip']})")
            continue
        escalate_at = ((sel["peak_utc"] - dt.timedelta(minutes=ESCALATE_LEAD_MIN)).strftime(TSFMT)
                       if sel["peak_utc"] is not None
                       else (now + dt.timedelta(minutes=DEFAULT_MAKER_WAIT_MIN)).strftime(TSFMT))
        past_window = (iso >= escalate_at) or TAKER_ONLY    # too late for a maker → taker (TAKER_ONLY ⇒ always taker)
        peak_passed = sel["peak_utc"] is not None and now > sel["peak_utc"]   # peak gone → max locked → forecast model
        #   over-weights the bucket above rmax and a post-peak price dump can look like fake "edge" → open NO new positions
        # CONFIDENCE → SIZING: lead-ramp (scale-in as the peak nears) × cluster_for's edge/boundary/market-agreement confidence.
        _hrs = ((sel["peak_utc"] - now).total_seconds() / 3600.0) if sel["peak_utc"] is not None else 9.0
        lead_ramp = 1.0
        for _h, _m in LEAD_RAMP:
            if _hrs > _h:
                lead_ramp = _m; break
        conf = sel.get("conf_base", 1.0) * lead_ramp
        log.append(f"  {name} {sel['day']}: conf {conf:.2f} (edge/boundary {sel.get('conf_base', 1.0):.2f} × lead×{lead_ramp:.2f}, "
                   f"{_hrs:.0f}h to peak" + (", MARKET-DISAGREE ½" if sel.get("disagree") else "") + ")")
        # REGIME-VOLATILE → don't bet the uncertain forecast EARLY; DEFER the normal bet to NEAR the peak (user 2026-06-23:
        # "not no-bet — bet near the peak, not early"), where the realized running_max resolves the bucket. NEAR-peak ⇒
        # fall through to a reduced-size normal bet. Still-FAR ⇒ wait (+ the cheap ±2 lotto in its window).
        if sel.get("regime_volatile"):
            why = sel.get("llm_reason") or ("stormy" if sel.get("stormy") else "market-disagree")
            if sel["peak_utc"] is not None and 0 < _hrs <= VOLATILE_ENTRY_LEAD_H:
                conf *= VOLATILE_CONF_MULT                      # near peak: running_max resolves the bucket → bet, smaller
                log.append(f"  {name} {sel['day']}: regime VOLATILE ({why}) but {_hrs:.0f}h≤{VOLATILE_ENTRY_LEAD_H:.0f}h to "
                           f"peak → near-peak bet @ conf {conf:.2f} (×{VOLATILE_CONF_MULT})")
                # fall through to the normal sizing/placement below
            elif sel["peak_utc"] is not None and VOLATILE_ENTRY_LEAD_H < _hrs <= LOTTO_LEAD_H and sel.get("lotto"):
                log.append(f"  {name} {sel['day']}: regime VOLATILE ({why}) & {_hrs:.0f}h to peak → LOTTO ±{LOTTO_SPREAD} "
                           f"<5c ${LOTTO_USD_PER_BUCKET:.0f}/bucket ({len(sel['lotto'])} cand, separate $)")
                place_lotto(sel, state, iso, now, dry, log)
                continue
            else:
                log.append(f"  {name} {sel['day']}: regime VOLATILE ({why}); {_hrs:.0f}h to peak → WAIT for near-peak "
                           f"(normal bet ≤{VOLATILE_ENTRY_LEAD_H:.0f}h to peak; lotto {VOLATILE_ENTRY_LEAD_H:.0f}–{LOTTO_LEAD_H:.0f}h)")
                continue
        # ── COVER-COST EQUAL SIZING ──────────────────────────────────────────────────────────────────────
        # Size all of this city's NEW picks to a COMMON share count `s` so that winning ANY single pick redeems
        # (s × $1) ≥ the COMBINED cost (Σ s·askᵢ) of every pick. Always achievable: two edge-gated buckets sum
        # to <0.9 (each P≥ask+0.05 and ΣP≤1 ⇒ Σask≤0.9), so s ≥ s·Σask. ⇒ the day PROFITS whenever the temp
        # lands in EITHER bought bucket, loses only when it misses both — fixing the old per-leg budget/ask
        # sizing that lost on a 2-bucket day when the smaller-funded bucket won. Held picks are left untouched.
        keys = {pk["label"]: f"{name}|{sel['day'].isoformat()}|{pk['label']}" for pk in sel["picks"]}
        new_picks = [pk for pk in sel["picks"] if keys[pk["label"]] not in state]
        # HELD = ALL same-city|date reserve-status legs we already hold and are NOT (re)placing now — INCLUDING
        # "orphans" whose P has since fallen below P_FLOOR so they dropped out of sel["picks"] but are STILL
        # filled/redeemable for THIS day. Fold them into the per-day cover-cost (mirrors _siblings used by the
        # escalation path); deriving held from sel["picks"] alone would miss orphans → undersize → loss-on-win.
        prefix = f"{name}|{sel['day'].isoformat()}|"
        new_keys = {keys[pk["label"]] for pk in new_picks}
        held_legs = [v for k, v in state.items()
                     if k.startswith(prefix) and k not in new_keys and v.get("status") in RESERVE_STATUSES]
        held_cost = sum(float(e.get("cost") or 0.0) for e in held_legs)
        held_ask = sum(float(e.get("ask") or 0.0) for e in held_legs)
        held_min_sh = min((float(e.get("filled") or e.get("shares") or 0.0) for e in held_legs), default=float("inf"))   # redeem = filled if FILLED, else reserved
        log.append(f"  {name} {sel['day']} center {sel['center']:.2f} (F {sel['F']:.1f}"
                   + (f", rmax {sel['rmax']:.1f}" if sel.get("rmax") is not None else "")
                   + ") picks " + ", ".join(f"{pk['label']}@{pk['ask']:.2f}/P{pk['P']:.2f}/e{pk['edge']:+.2f}" for pk in sel["picks"])
                   + (f" · held {len(held_legs)} leg(s) ${held_cost:.2f}/{held_min_sh:.0f}sh (incl orphans)" if held_legs else ""))
        if not new_picks:
            continue
        if peak_passed:                                     # peak gone → max locked → never open a NEW position (manage-only)
            log.append(f"  {name} {sel['day']}: peak passed ({sel['peak_utc']:%H:%MZ}) → no NEW positions (max locked; manage-only)")
            continue
        # COVER-COST INVARIANT GUARD (self-checking): the equal-share sizing below only profits-on-any-win while the
        # asks of ALL co-funded legs (new + held, incl orphans — every leg redeeming against THIS day's resolution)
        # sum to < 1. Holds today only because P_FLOOR=0.35 caps clearing buckets at ~2 (max Σask≈0.85); it is NOT
        # enforced by the sizing math. If a future P_FLOOR/MIN_EDGE drop lets ≥3 buckets clear, Σask could reach 1 and
        # the guarantee would break SILENTLY — trip a LOUD skip instead.
        cofunded_ask = sum(pk["ask"] for pk in new_picks) + held_ask    # new + held (incl orphans), all redeeming this day
        if cofunded_ask >= 1.0 - COVER_COST_MARGIN:
            log.append(f"  {name} {sel['day']}: ⚠ COVER-COST INVARIANT BROKEN — Σask {cofunded_ask:.3f} ≥ "
                       f"{1.0 - COVER_COST_MARGIN:.2f} across new+held co-funded leg(s); winning one leg "
                       f"would NOT cover the combined cost → SKIP city (check P_FLOOR/MIN_EDGE/bucket_prob)")
            continue
        sum_ask = sum(pk["ask"] for pk in new_picks)                     # Σask of the NEW legs only
        max_ask = max(pk["ask"] for pk in new_picks)
        remaining = max(0.0, total_cap - out)
        depth_lim = min((int(pk["depth"] / pk["ask"]) for pk in new_picks if pk["ask"] > 0), default=0)
        s_cap = min(MAX_ORDER_SHARES,                                     # ≤20 shares/leg (user cap)
                    int(remaining / sum_ask) if sum_ask > 0 else 0,       # NEW combined cost within remaining total cap
                    int(CLUSTER_MAX_ORDER_USD / max_ask) if max_ask > 0 else 0,   # per-leg $ backstop
                    depth_lim)                                            # each leg within its own book depth
        s_cap = int(round(s_cap * conf))                                  # CONFIDENCE scaling — equal-share MAGNITUDE only
        #   (cover-cost invariant unchanged); below MIN_ORDER_SHARES the existing skip waits for confidence to firm
        # PER-DAY cover-cost (held legs incl orphans folded in above): EVERY leg (held + new) must redeem (shares×$1)
        # ≥ the COMBINED day cost. With held legs the new legs must cover the held cost too (lower bound s_lo) AND be
        # small enough that the SMALLEST held leg still covers the grown total (upper bound s_hi); infeasible → skip.
        if held_legs:
            if sum_ask >= 1.0:                                            # edge gate makes this impossible; guard the division
                log.append(f"  {name} {sel['day']}: Σask {sum_ask:.2f} ≥ 1 with held legs → cannot cover → skip new")
                continue
            s_lo = math.ceil(held_cost / (1.0 - sum_ask))                # new legs must redeem ≥ full-day cost
            s_hi = math.floor((held_min_sh - held_cost) / sum_ask) if sum_ask > 0 else s_cap   # smallest held leg covers total
            s_common = min(s_cap, s_hi)
            if s_common < max(MIN_ORDER_SHARES, s_lo):
                log.append(f"  {name} {sel['day']}: can't extend held position within cover-cost "
                           f"(held ${held_cost:.2f}/{held_min_sh:.0f}sh, need s∈[{s_lo},{s_hi}], cap {s_cap}) → skip new")
                continue
        else:
            s_common = s_cap                                             # no held legs; Σask≤0.9 (edge gate) ⇒ invariant auto-holds
            if s_common < MIN_ORDER_SHARES:
                log.append(f"  {name} {sel['day']}: cover-size {s_common}sh/leg < min "
                           f"(Σask {sum_ask:.2f}, cap-left ${remaining:.0f}, depth {depth_lim}) → skip")
                continue
        total_cost = round(held_cost + s_common * sum_ask, 2)            # FULL day cost (held + new)
        min_leg = s_common if not held_legs else min(float(s_common), held_min_sh)
        ok = min_leg >= total_cost - 1e-9
        log.append(f"  {name} {sel['day']}: cover-size {s_common}sh × {len(new_picks)} new"
                   + (f" + held {held_min_sh:.0f}sh/${held_cost:.2f}" if held_legs else "")
                   + f" → day cost ${total_cost:.2f}; smallest winning leg redeems ${min_leg:.2f} ≥ ${total_cost:.2f} "
                   + ("✓" if ok else "✗ ABORT"))
        if not ok:                                                       # never place a position that can lose on a win
            continue
        for pk in new_picks:
            n = pk["n"]
            comp = pk["comp"]
            label = pk["label"]
            key = keys[label]
            token = pk["token"]
            ask = pk["ask"]
            shares = s_common
            if shares * ask < MIN_ORDER_USD - 1e-9:                       # a very cheap leg below the exchange min → skip it (rare)
                log.append(f"  {name} {label} @ {ask:.2f}: leg ${shares * ask:.2f} < min order → skip leg")
                continue
            entry = ask if past_window else maker_price_for(pk["bid"], ask)
            planned = round(shares * ask, 2)                # reserve at the ASK (the most we could pay if we escalate)
            kind = "TAKER" if past_window else "MAKER"
            if dry:
                log.append(f"  {name} {label} {'WARMUP/observe' if warming else 'DRY'} would {kind}-BUY {shares}sh @ {entry:.2f} "
                           f"(reserve ${planned:.0f}" + (f", escalate {escalate_at})" if not past_window else ")"))
                out += planned                              # observe/DRY preview respects the cap too
                continue
            # LIVE: persist INTENT (PENDING) BEFORE placing — a crash then leaves a PENDING marker (state + journal),
            # so the key is never re-bought (worst case = a missed buy / orphaned resting order, never a double-buy).
            state[key] = {"status": "PENDING", "order_id": None, "token": token, "ask": ask, "maker_price": entry,
                          "shares": float(shares), "cost": planned, "ts": iso, "escalate_at": escalate_at,
                          "P": round(pk["P"], 3), "edge": round(pk["edge"], 3), "city": name, "station_id": sel["icao"],
                          "comparator": comp, "threshold": n}
            if not _save(state, key, log):
                return log                                  # can't persist intent → place nothing further this cycle
            out += planned                                  # reserve immediately (conservative — at the ask)
            if past_window:
                res = ex.place_from_decision(_decision(name, sel["icao"], sel["day"], n, token, label, ask, shares, taker=True, comp=comp),
                                             now_iso=iso, ttl_min=None)
                status, filled, cost = _taker_result(res, shares, ask)
                state[key].update(status=status, order_id=res.order_id, filled=filled, cost=cost)
                out += (cost - planned)
                log.append(f"  {name} {label} TAKER-BUY {shares}sh @ {ask:.2f} → {status} filled={filled}")
            else:
                res = ex.place_from_decision(_decision(name, sel["icao"], sel["day"], n, token, label, entry, shares, taker=False, comp=comp),
                                             now_iso=iso, ttl_min=None)   # GTC maker (rests until fill or our escalate-cancel)
                if res.ok and res.order_id:
                    state[key].update(status="RESTING", order_id=res.order_id)   # cost stays = planned reserve (at the ask)
                    log.append(f"  {name} {label} MAKER-REST {shares}sh @ {entry:.2f} (reserve ${planned:.0f}, escalate {escalate_at})")
                elif res.ambiguous:
                    state[key].update(status="AMBIGUOUS", order_id=res.order_id)   # may be live → keep reserve, positions reconcile
                    log.append(f"  {name} {label} MAKER ambiguous ({res.detail}) → reserve held")
                else:
                    state[key].update(status="NOFILL", cost=0.0)   # rejected → no order, free the reserve
                    out += (0.0 - planned)
                    log.append(f"  {name} {label} MAKER rejected ({res.detail}) → freed")
            if not _save(state, key, log):                  # persist the outcome (journal + state) or HALT
                return log
    return log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=float, default=1200.0, help="TOTAL capital cap (USDC) incl. open positions")
    ap.add_argument("--interval", type=int, default=20, help="minutes between cycles (loop)")
    ap.add_argument("--warmup", type=int, default=1, help="observe-only (compute+log, place NO orders) for this many cycles after start")
    ap.add_argument("--once", action="store_true", help="run a single cycle then exit")
    a = ap.parse_args()
    import fcntl
    import time
    STATE.parent.mkdir(parents=True, exist_ok=True)
    lockf = open(STATE.parent / "cluster.lock", "w")       # single-instance lock: a 2nd process can't double-buy
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another run_cluster instance holds the lock — exiting (avoids double-buy)", flush=True)
        return
    import os
    lockf.write(str(os.getpid()))
    lockf.flush()
    n = 0
    while True:
        warm = n < a.warmup                                # first --warmup cycles: observe-only — never fire an order on a fresh load
        for ln in cycle(a.cap, force_dry=warm, show_storage=(n == 0)):
            print(ln, flush=True)
        if a.once:
            break
        n += 1
        time.sleep(a.interval * 60)


if __name__ == "__main__":
    main()
