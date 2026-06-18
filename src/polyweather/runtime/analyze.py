"""Per-city analysis — the shared core used by both `run_once.py` (one city, CLI)
and `run_daemon.py` (24/7 loop).

  scan_city   : LIGHT — one Open-Meteo 15-min curve → peak_utc / predicted_max / mkt_day.
                Used by the periodic scan (every SCAN_INTERVAL_MIN; digest + activation), ~1 call/city.
  analyze_city: FULL — curve + AWC running_max + Gamma event → the AGENT chooses the
                bucket (raw forecast peak + fan + realized climb + atmosphere + market
                ladder); decision.validate_proposal() hard-checks only the SAFETY gates.
                No agent → decision.decide() = round(forecast peak) fallback. Runs at/after
                activation.

The RAW forecast peak + realized same-day data + live atmosphere drive the call. Recent
station/regime residuals are included as a soft reference; they never replace the live nowcast.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .. import agent
from ..obslog import log_failure
from ..params import (AWC_FRESH_MIN, MOMENTUM_DROP_PCT, MOMENTUM_FAIL_CLOSED,
                      MOMENTUM_WINDOW_MIN, STRIKE_CONFIRM_LEAD_MIN,
                      STRIKE_CUTOFF_BUFFER_MIN, STRIKE_FALLBACK_LEAD_MIN, STRIKE_MAX_LAG_MIN,
                      STRIKE_MIN_LAG_H, STRIKE_USE_MINUTELY15)
from ..decision import _bucket_alive, decide, validate_proposal
from ..discovery import _bucket_from_market
from ..forecast.open_meteo import (fetch_hourly_met, fetch_minutely_15,
                                   fetch_previous_runs)
from ..forecast.regime import classify_regime
from ..forecast.snapshots import append_snapshot, fan_summary
from ..forecast.upside import remaining_corrected_max
from ..models import Decision, EventState
from ..realized.feature_store import DATA_DIR
from ..realized.metar_parse import parse_metar
from ..realized.metar_source import fetch_recent_awc
from ..realized.residuals import recent_bias
from ..realized.solar import day_sun

GAMMA = "https://gamma-api.polymarket.com/events"
CLOB_BOOKS = "https://clob.polymarket.com/books"   # live orderbook (the book we actually trade against)
CLOB_PRICES_HISTORY = "https://clob.polymarket.com/prices-history"  # token price series → momentum (drawdown) check
UTC = timezone.utc
_MON = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_DATE_RE = re.compile(r"on (\d{1,2}) (\w{3}) '(\d{2})")




def _momentum_guard(decision, momentum: dict):
    """Record momentum and hard-block a BET on a large recent drawdown in either pass."""
    if decision is None:
        return None
    decision.momentum_5m = momentum.get("drop_pct")
    if decision.action == "BET" and momentum.get("is_big_drop"):
        pct = momentum.get("drop_pct") or 0.0
        decision.action = "ESCALATE"
        decision.reason = (f"momentum: favorite −{pct:.0%} in {momentum.get('window_min')}min → "
                           f"order blocked | " + decision.reason)
    return decision


def event_date(ev: dict) -> date | None:
    for m in ev.get("markets", []):
        mm = _DATE_RE.search(m.get("description", "") or "")
        if mm:
            d, mon, yy = mm.groups()
            return date(2000 + int(yy), _MON[mon[:3].title()], int(d))
    return None


async def running_max_today(client: httpx.AsyncClient, icao: str, tz: str, today: date,
                            hours: int = 30):
    """AWC integer-°C max over the FULL local clock day + freshness. Returns
    (running_max:int|None, fresh:bool, newest_age_min:float|None). `hours` widens the AWC
    pull for settlement of a past day (D-1..D-3); the live daemon uses the default 30h."""
    reps = await fetch_recent_awc(client, icao, hours=hours)
    z = ZoneInfo(tz)
    mx = None
    newest_valid = None
    for raw, t in reps:
        if t.astimezone(z).date() != today:
            continue
        try:
            o = parse_metar(raw, icao, t)
        except Exception:
            continue
        # Plausibility clamp (audit r6): a corrupted-but-syntactically-valid METAR (digit transposition
        # "92/14", a stray sign flip, a garbled RMK T-group) parses fine; a HIGH outlier adopted by max()
        # would inflate running_max → every bucket dead → decide() returns None → the city is SILENTLY
        # disabled all day. Treat any temp outside the Earth-surface range of our traded cities as a
        # missing reading (skip it), exactly like a None temp.
        if o.temp_c is not None and -60.0 <= o.temp_c <= 60.0:
            mx = round(o.temp_c) if mx is None else max(mx, round(o.temp_c))
            if newest_valid is None or t > newest_valid:
                newest_valid = t
    age_min = None
    fresh = False
    if newest_valid is not None:
        age_min = (datetime.now(UTC) - newest_valid).total_seconds() / 60.0
        fresh = age_min < AWC_FRESH_MIN   # trust running_max only if newest obs is fresh (POLY_AWC_FRESH_MIN)
    return mx, fresh, age_min


async def latest_metar_met(client: httpx.AsyncClient, icao: str, tz: str, today: date) -> dict | None:
    """The newest TODAY METAR's surface met — the REALIZED atmospheric state for the decision agent's
    meteorologist context (clear/dry/light-wind under a ridge → NWP under-calls the afternoon max → lean a
    HIGHER bucket; overcast/showery → the reverse). None if no usable ob. NEVER raises (caller logs)."""
    try:
        reps = await fetch_recent_awc(client, icao, hours=12)
    except Exception:
        return None
    z = ZoneInfo(tz)
    best = None                                            # newest (raw, t) for today's local clock day
    for raw, t in reps:
        if t.astimezone(z).date() == today and (best is None or t > best[1]):
            best = (raw, t)
    if best is None:
        return None
    try:
        o = parse_metar(best[0], icao, best[1])
    except Exception:
        return None
    return {"obs_utc": best[1].strftime("%Y-%m-%dT%H:%MZ"), "temp_C": o.temp_c, "dewpoint_C": o.dewpoint_c,
            "rh_pct": o.rh_pct, "wind_dir": o.wind_dir_deg, "wind_kt": o.wind_speed_kt, "gust_kt": o.wind_gust_kt,
            "cloud_okta": o.cloud_cover_okta, "cloud_base_ft": o.cloud_base_ft, "precip": o.is_precip,
            "thunder": o.is_thunder, "fog": o.is_fog, "visibility_m": o.visibility_m}


def _clob_best(ob: dict) -> tuple[float | None, float | None]:
    bids = [float(x["price"]) for x in ob.get("bids", []) if x.get("price") is not None]
    asks = [float(x["price"]) for x in ob.get("asks", []) if x.get("price") is not None]
    return (max(bids) if bids else None, min(asks) if asks else None)


async def apply_clob_prices(client: httpx.AsyncClient, buckets: list) -> bool:
    """Set each bucket's best_bid/best_ask from the LIVE CLOB orderbook — the book we actually place
    orders into and the ONLY two-sided quote we gate/auto-bet on (Gamma's quotes lag; CLOB is the truth
    for BOTH pricing and execution). One batch POST /books for all bucket tokens. CLOB is AUTHORITATIVE:
    for EVERY token we sent we write the live book, or None/None if /books returned no two-sided book for
    it — whether the token was OMITTED from the response, returned an EMPTY book, or the whole call failed
    (non-200 / hiccup). A bucket left at None/None trips market_health's two-sided gate (audit L1) →
    ESCALATE, so we NEVER auto-bet our_top off a STALE Gamma quote that merely happens to be two-sided
    (the exact partial-failure hole the r4 audit found). Gamma's price survives only in price_yes/
    last_trade (used for the health SUM + display fallback, never as a 'live book'). Returns True iff at
    least one live CLOB book was applied. (To restore the old trade-off-Gamma-on-CLOB-outage behavior,
    one would have to deliberately keep the Gamma quotes here — intentionally NOT done: stale-quote
    auto-bets are the failure mode we're guarding against.)"""
    toks = [b.token_yes for b in buckets if b.token_yes]
    if not toks:
        return False
    try:
        r = await client.post(CLOB_BOOKS, json=[{"token_id": t} for t in toks], timeout=20.0)
        books = r.json() if r.status_code == 200 else None
    except Exception:
        books = None                                   # CLOB hiccup → NO live book for any token (escalate-safe)
    by_tok = ({(ob.get("asset_id") or ob.get("token_id")): _clob_best(ob)
               for ob in books if isinstance(ob, dict)} if isinstance(books, list) else {})
    for b in buckets:
        if b.token_yes:                                # CLOB-or-None: a sent token with no live book → None/None
            b.best_bid, b.best_ask = by_tok.get(b.token_yes, (None, None))
    return bool(by_tok)


async def _momentum(client: httpx.AsyncClient, token: str, now_utc: datetime) -> dict:
    """Recent price drawdown of `token` over the last MOMENTUM_WINDOW_MIN minutes, from the live CLOB
    prices-history (fidelity=1 ≈ 1-min points). Returns {window_min, drop_pct, is_big_drop} where
    drop_pct = (peak price in the window − latest) / peak. `available` distinguishes a verified calm
    result from an endpoint failure or thin history; the final pre-submit gate can therefore fail closed."""
    out = {"window_min": MOMENTUM_WINDOW_MIN, "drop_pct": None, "is_big_drop": False,
           "available": False, "reason": "no_token"}
    if not token:
        return out
    end = int(now_utc.timestamp())
    start = end - (MOMENTUM_WINDOW_MIN + 2) * 60                # +2min slack so a ~window-old point exists
    try:
        r = await client.get(CLOB_PRICES_HISTORY,
                             params={"market": token, "startTs": start, "endTs": end, "fidelity": 1},
                             timeout=15.0)
        if r.status_code != 200:
            out["reason"] = f"http_{r.status_code}"
            log_failure("clob", kind="fetch_failed", detail=f"prices-history: HTTP {r.status_code}",
                        consequence="momentum unavailable")
            return out
        hist = r.json().get("history") or []
    except Exception as e:
        out["reason"] = "fetch_failed"
        log_failure("clob", kind="fetch_failed", detail=f"prices-history: {e}",
                    consequence="momentum unavailable")
        return out
    pts = sorted((int(h["t"]), float(h["p"])) for h in hist
                 if h.get("t") is not None and h.get("p") is not None)
    if len(pts) < 2:
        out["reason"] = "thin_history"
        return out
    cutoff = end - MOMENTUM_WINDOW_MIN * 60
    if pts[-1][0] < cutoff:
        out["reason"] = "stale_history"
        return out
    window = [p for t, p in pts if t >= cutoff]
    reference = max(window)                                    # peak over the window — robust to spike-then-drop
    if reference <= 0:
        out["reason"] = "invalid_reference"
        return out
    drop = (reference - pts[-1][1]) / reference
    out["drop_pct"] = round(drop, 4)
    out["is_big_drop"] = drop >= MOMENTUM_DROP_PCT
    out["available"] = True
    out["reason"] = None
    return out


async def enforce_order_momentum(client: httpx.AsyncClient, decision, now_utc: datetime) -> tuple[bool, dict]:
    """Refresh momentum for the exact token immediately before submit and apply the hard gate.

    Unlike the earlier agent-context check, this is a safety check: when configured fail-closed, an
    unavailable/thin history cannot silently pass an unverified live order.
    """
    momentum = await _momentum(client, decision.token_yes, now_utc)
    _momentum_guard(decision, momentum)
    if (decision.action == "BET" and MOMENTUM_FAIL_CLOSED
            and not momentum.get("available")):
        decision.action = "ESCALATE"
        decision.reason = (f"momentum unavailable ({momentum.get('reason')}) → order blocked | "
                           + decision.reason)
    return decision.action == "BET", momentum


@dataclass
class Scan:
    station_id: str
    city: str
    mkt_day: date
    peak_utc: datetime | None
    predicted_max: float | None
    curve: list = field(default_factory=list)         # the day's forecast (time, temp_C) points — 15-min granularity
    note: str = ""


async def scan_city(client: httpx.AsyncClient, stn, now_utc: datetime) -> Scan:
    """LIGHT: Open-Meteo local-day 15-min curve → peak_utc + predicted_max for the market day. The 15-min
    granularity sharpens the peak TIME (vs hourly); the peak VALUE / bet bucket is granularity-invariant."""
    z = ZoneInfo(stn.timezone)
    mkt_day = now_utc.astimezone(z).date()
    try:
        curve = await fetch_minutely_15(client, stn.latitude, stn.longitude, stn.timezone, forecast_days=2)
    except Exception as e:
        log_failure("open_meteo", kind="fetch_failed", detail=str(e),
                    station=stn.station_id, city=stn.city, consequence="skipped")
        return Scan(stn.station_id, stn.city, mkt_day, None, None, note=f"curve fetch failed: {e}")
    day = [(dt, tp) for dt, tp in curve if dt.date() == mkt_day]
    if not day:
        return Scan(stn.station_id, stn.city, mkt_day, None, None, curve, "no curve for mkt_day")
    peak_dt, peak_t = max(day, key=lambda x: x[1])
    peak_utc = peak_dt.replace(tzinfo=z).astimezone(UTC)
    return Scan(stn.station_id, stn.city, mkt_day, peak_utc, peak_t, curve)


@dataclass
class CityTick:
    station_id: str
    city: str
    now: datetime
    mkt_day: date
    peak_utc: datetime | None
    predicted_max: float | None
    remaining: float | None
    running_max: int | None
    fresh: bool
    market_probs: dict = field(default_factory=dict)   # label -> market mid (de-vig done in analysis)
    agent: dict = field(default_factory=dict)          # the agent's raw JSON proposal (empty = deterministic path)
    momentum: dict = field(default_factory=dict)       # recent-drawdown signal {window_min, drop_pct, is_big_drop}
    decision: Decision | None = None
    state: EventState | None = None                    # the built event state (for reply re-validation)
    note: str = ""

    def _dist(self) -> list[dict]:
        """The bucket ladder the chooser saw — per-bucket {market mid, alive}, sorted by market price. The
        record for 复盘/paper-trade. (No model our_p: the bucket is the agent's JUDGMENT, not a formula.)"""
        if self.state is None:
            return []
        rows = [{"b": f"{b.comparator.value}{b.threshold}",
                 "mkt": (round(b.implied_p, 3) if b.implied_p is not None else None),
                 "alive": _bucket_alive(b.comparator, b.threshold, self.state.running_max)}
                for b in self.state.buckets]
        rows.sort(key=lambda r: (r["mkt"] is not None, r["mkt"] or 0.0), reverse=True)
        return rows

    def log_row(self) -> dict:
        d = self.decision
        return {
            "ts": self.now.strftime("%Y-%m-%dT%H:%M:%SZ"), "station": self.station_id,
            "city": self.city, "mkt_day": str(self.mkt_day),
            "peak_utc": self.peak_utc.strftime("%Y-%m-%dT%H:%MZ") if self.peak_utc else None,
            "predicted_max": self.predicted_max, "forecast_peak_C": self.remaining,
            "running_max": self.running_max, "fresh": self.fresh,
            "action": (d.action if d else None), "our_top": (d.label if d else None),
            "confidence": (d.confidence if d else None),
            "market": (d.market_yes if d else None), "maker": (d.maker_price if d else None),
            "order_type": (d.order_type if d else None),   # always "maker" (a resting GTD bid)
            "health": (d.health_ok if d else None), "note": self.note,
            "reason": (d.reason if d else None),
            "agent": (self.agent or None),   # the LLM proposal (None = deterministic path)
            "momentum": (self.momentum or None),  # recent-drawdown signal {window_min, drop_pct, is_big_drop}
            "dist": self._dist(),            # the bucket ladder (market mid + alive) the chooser saw
        }


async def analyze_city(client: httpx.AsyncClient, stn, now_utc: datetime,
                       scan: Scan | None = None, *, agent_model: str | None = None,
                       cutoff_utc: datetime | None = None) -> CityTick:
    """FULL per-city decision tick → CityTick (with .decision = BET/ESCALATE/None). `cutoff_utc` (from the
    strike stage) extends the post-peak no-new-positions cutoff past the early forecast argmax; None →
    decide/validate default to scan.peak_utc (unchanged behaviour for run_once / the reply path)."""
    z = ZoneInfo(stn.timezone)
    if scan is None:
        scan = await scan_city(client, stn, now_utc)
    mkt_day = scan.mkt_day
    tick = CityTick(stn.station_id, stn.city, now_utc, mkt_day, scan.peak_utc,
                    scan.predicted_max, None, None, True)
    if scan.peak_utc is None:
        tick.note = scan.note or "no peak"
        return tick

    now_local = now_utc.astimezone(z).replace(tzinfo=None)
    # RAW forecast peak (no historical bias): the realized same-day data + the live atmosphere drive the call,
    # not last-year's residuals. The agent judges any NWP bias direction from the met context.
    tick.remaining = remaining_corrected_max(scan.curve, now_local, mkt_day, 0.0)

    try:
        rmax, fresh, _age = await running_max_today(client, stn.station_id, stn.timezone, mkt_day)
    except Exception as e:                   # AWC down → no realized floor → never bet (logged, not a crash)
        log_failure("awc", kind="fetch_failed", detail=str(e),
                    station=stn.station_id, city=stn.city, consequence="skipped")
        tick.note = f"AWC fetch failed: {e}"
        return tick
    tick.running_max = rmax
    tick.fresh = fresh
    if rmax is None:
        log_failure("awc", kind="empty", detail="no usable temperature for the local market day",
                    station=stn.station_id, city=stn.city, consequence="stale_no_bet")
        tick.note = "no usable AWC temperature — running_max unavailable"
        return tick                          # no realized floor → fail closed, never guess that every bucket is alive
    if not fresh:
        log_failure("awc", kind="stale", detail=f"newest obs older than {AWC_FRESH_MIN}min",
                    station=stn.station_id, city=stn.city, consequence="stale_no_bet")
        tick.note = "stale AWC — running_max not trusted"
        return tick                          # don't bet on a stale dead-bucket floor

    try:
        r = await client.get(GAMMA, params={"series_id": stn.pm_series_id, "closed": "false", "limit": 10})
        evs = r.json()
    except Exception as e:
        log_failure("gamma", kind="fetch_failed", detail=str(e),
                    station=stn.station_id, city=stn.city, consequence="skipped")
        tick.note = f"gamma fetch failed: {e}"
        return tick
    # MUST match the market day exactly — a wrong-day event would analyze the wrong peak.
    # Do NOT fall back to evs[0] (could be tomorrow's event); skip instead.
    ev = next((e for e in evs if event_date(e) == mkt_day), None)
    if not ev:
        log_failure("gamma", kind="no_event", detail=f"{len(evs)} open events, none for {mkt_day}",
                    station=stn.station_id, city=stn.city, consequence="skipped")
        tick.note = f"no open event for mkt_day {mkt_day} ({len(evs)} open events)"
        return tick
    buckets = [b for b in (_bucket_from_market(m) for m in ev.get("markets", [])) if b]
    if not await apply_clob_prices(client, buckets):   # no live CLOB book applied (outage/empty) →
        log_failure("clob", kind="fetch_failed", detail="no live CLOB book applied",
                    station=stn.station_id, city=stn.city, consequence="escalated")  # health gate ESCALATEs
    state = EventState(city=stn.city, local_date=mkt_day,
                       verified=stn.verified, buckets=buckets, running_max=rmax)
    state.order_buckets()
    tick.state = state                                 # exposed for the Telegram reply re-validation path
    tick.market_probs = {f"{b.comparator.value}{b.threshold}":
                         (round(b.implied_p, 3) if b.implied_p is not None else None)
                         for b in state.buckets}

    # Deterministic BASELINE = round(forecast peak) → nearest alive bucket (the no-agent fallback + the
    # momentum anchor + the agent's reference). The agent, when available, CHOOSES the bucket itself.
    baseline = decide(state, tick.remaining, stn.station_id, now=now_utc, peak_utc=scan.peak_utc,
                      cutoff_utc=cutoff_utc)

    # MOMENTUM: recent drawdown of the rounded favorite is agent context. After the agent chooses, the hard
    # guard is applied to the actual selected token (and the daemon refreshes it once more before submit).
    momentum = {"window_min": MOMENTUM_WINDOW_MIN, "drop_pct": None, "is_big_drop": False}
    if baseline is not None and baseline.token_yes:
        momentum = await _momentum(client, baseline.token_yes, now_utc)
    tick.momentum = momentum

    # AGENT PATH: if an OpenAI key is set, the agent CHOOSES the bucket (judgment) and validate_proposal
    # hard-checks only the SAFETY gates. No key → the deterministic round(peak) baseline. Either way → the
    # momentum guard (a big drop hard-blocks either pass).
    if agent.enabled() and baseline is not None:
        realized_met = await latest_metar_met(client, stn.station_id, stn.timezone, mkt_day)   # surface met now
        fan = fan_summary(stn.station_id, mkt_day)          # the day's peak-convergence fan + freshest forecast met
        ctx = _agent_context(stn, tick, baseline, scan, momentum, realized_met=realized_met, fan=fan)
        proposal = await agent.propose(client, ctx, model=agent_model)
        if proposal is not None:
            tick.agent = proposal          # logged either way (incl. the error trail)
        if proposal is not None and not proposal.get("_error"):
            decision = validate_proposal(proposal, state, tick.remaining, stn.station_id,
                                         now=now_utc, peak_utc=scan.peak_utc, cutoff_utc=cutoff_utc)
            decision_momentum = momentum
            if (decision is not None and decision.token_yes
                    and decision.token_yes != baseline.token_yes):
                decision_momentum = await _momentum(client, decision.token_yes, now_utc)
            tick.momentum = decision_momentum
            tick.decision = _momentum_guard(decision, decision_momentum)
            return tick
        # agent unavailable / API error (no credits, timeout, bad response) →
        # fall through to the deterministic baseline (still safe), never escalate-everything.
    tick.decision = _momentum_guard(baseline, momentum)
    return tick


def _recent_outcomes(station_id: str, n: int = 5) -> list[dict]:
    """This city's last `n` SETTLED results (from settle.jsonl) — the agent's recent track record
    (was the forecast reliable / off lately?). Graceful → [] if the file/rows are absent."""
    p = DATA_DIR / "settle.jsonl"
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("station") == station_id and r.get("bet_bucket") is not None:
                out.append({"date": r.get("market_date"), "our_bet": r.get("bet_bucket"),
                            "resolved": r.get("resolved_bucket"), "actual_max": r.get("actual_max_awc"),
                            "won": r.get("won")})
    except Exception:
        return []
    return out[-n:]


def _forecast_curve(scan: "Scan", mkt_day: date) -> list:
    """The market-day local forecast as [HH:MM, °C] (15-min points) — lets the agent see peak SHARPNESS / shape."""
    return [[dt.strftime("%H:%M"), round(tp, 1)] for dt, tp in (scan.curve or []) if dt.date() == mkt_day]


def _agent_context(stn, tick: "CityTick", baseline, scan: "Scan", momentum: dict | None = None,
                   *, realized_met: dict | None = None, fan: dict | None = None) -> dict:
    """The fact packet handed to the agent — it CHOOSES the bucket from these facts. The realized
    running_max is the HARD floor (a deterministic gate rejects buckets below it; an equal EQ/LE bucket
    remains possible but is guarded when more warming is forecast). `buckets` is the
    market ladder (mid + alive) it picks from; `deterministic_baseline` is the obvious round(peak) pick for
    reference (the agent may agree or, with reason, differ)."""
    regime = ((fan or {}).get("regime_latest")
              or classify_regime((fan or {}).get("met_at_peak_latest")))
    primary_regime = (regime or {}).get("primary")
    return {
        "city": stn.city, "market_date": str(tick.mkt_day),
        "now_utc": tick.now.strftime("%Y-%m-%dT%H:%MZ"),
        "peak_utc": tick.peak_utc.strftime("%Y-%m-%dT%H:%MZ") if tick.peak_utc else None,
        "forecast_peak_C": tick.remaining,             # the RAW forecast peak — your primary anchor (25.9 → ==26)
        "realized_running_max_C": tick.running_max,    # HARD floor — never bet a bucket below this
        "awc_fresh": tick.fresh,
        "forecast_curve_local": _forecast_curve(scan, tick.mkt_day),   # [HH:MM, °C] — peak sharpness/shape
        "buckets": tick._dist(),                       # the market ladder you choose from: [{b, mkt, alive}]
        "recent_outcomes": _recent_outcomes(stn.station_id),   # this city's last ~5 settled days
        "recent_momentum": momentum or {},             # favorite's recent drawdown {window_min, drop_pct, is_big_drop}
        # MET (meteorologist context): the forecast atmosphere at the peak (fan.met_at_peak_latest) + the
        # intraday peak-convergence fan + the realized surface ob → judge NWP bias-direction (clear/dry/
        # high-pressure → under-call → lean higher) on top of the gates. Absent → today's behaviour.
        "forecast_peak_fan": fan or None,
        "forecast_regime": regime,
        "realized_surface_met": realized_met or None,
        # RECENT FORECAST BIAS: rolling residual = forecast − actual over 3/7/14/30d (mean/n/lo/hi). The
        # EMPIRICAL, seasonally-adaptive version of the bias-direction — a soft nudge, the agent weighs it.
        "recent_forecast_bias": recent_bias(stn.station_id, regime=primary_regime),
        "deterministic_baseline": {                    # the obvious round(forecast peak) pick, for reference
            "action": baseline.action, "bucket": baseline.label,
            "market": baseline.market_yes, "reason": baseline.reason},
    }


# ── STRIKE-TIMING stage (stage 1): decide WHEN to run the bet, never WHAT ──

def _lag_h(station: str, month: int) -> float | None:
    """Per-city/month thermal lag (hour-of-max − solar-noon) from city_profiles.json — the deterministic
    strike anchor (solar_noon + lag) + the fail-safe fallback. None if the city/month is absent."""
    p = DATA_DIR / "city_profiles.json"
    if not p.exists():
        return None
    try:
        m = ((json.loads(p.read_text()).get(station) or {}).get("months") or {}).get(str(month))
        return float(m["lag_h"]) if m and m.get("lag_h") is not None else None
    except Exception:
        return None


def _strike_context(stn, scan: "Scan", *, sunrise, solar_noon, sunset, lag_h, expected_peak,
                    prev_runs: dict, m15_peak_local, running_max, now_utc, lo, hi, fan=None) -> dict:
    """Fact packet for the strike-timing LLM (it picks WHEN, never WHAT). Weather/market numbers only."""
    def _z(dt):
        return dt.strftime("%Y-%m-%dT%H:%MZ") if dt else None
    return {
        "city": stn.city, "market_date": str(scan.mkt_day), "now_utc": _z(now_utc),
        "solar_noon_utc": _z(solar_noon), "sunrise_utc": _z(sunrise), "sunset_utc": _z(sunset),
        "lag_h": lag_h,
        "forecast_peak_utc": _z(scan.peak_utc),        # the ANCHOR — strike = this ± your offset
        "expected_peak_utc": _z(expected_peak),        # solar_noon + lag — a deterministic cross-check
        "multi_run_peak_spread": prev_runs or None,    # value + local-time range across recent model runs
        "minutely15_peak_local": m15_peak_local,
        "forecast_peak_fan": fan or None,              # intraday peak convergence (settled?) + freshest met-at-peak
        "realized_running_max_C": running_max,
        "strike_window_utc": [_z(lo), _z(hi)],         # the hard clamp box — your output is forced into this
    }


async def decide_strike(client: httpx.AsyncClient, stn, scan: "Scan",
                        now_utc: datetime) -> tuple[datetime | None, datetime | None, str]:
    """STAGE 1: pick the strike time (when to run the bet) + the extended post-peak cutoff for ONE city.
    Returns (strike_utc, cutoff_utc, src). LLM (gpt-5.4-mini) when OPENAI_API_KEY is set, else a
    deterministic solar+lag fallback; the result is HARD-CLAMPED into [solar_noon+MIN_LAG, sunset]. NEVER
    raises, NEVER chooses a bucket/price — it only proposes a TIME (then clamped). All fallbacks are logged."""
    if scan.peak_utc is None:
        return None, None, "no_peak"
    z = ZoneInfo(stn.timezone)
    mkt_day = scan.mkt_day
    sunrise, solar_noon, sunset, _dl = day_sun(stn.latitude, stn.longitude, mkt_day)
    lag_h = _lag_h(stn.station_id, mkt_day.month)
    expected_peak = solar_noon + timedelta(hours=lag_h) if lag_h is not None else None
    # The strike output box = a window around the FORECAST PEAK (the anchor) ∩ the daytime window. The mini
    # only picks the OFFSET within this; it can never schedule pre-dawn or overshoot far past the peak.
    peak = scan.peak_utc
    daa_lo = min(solar_noon + timedelta(hours=STRIKE_MIN_LAG_H), sunset)        # daytime floor
    win_lo = max(daa_lo, peak - timedelta(minutes=STRIKE_CONFIRM_LEAD_MIN))
    win_hi = max(win_lo, min(sunset, peak + timedelta(minutes=STRIKE_MAX_LAG_MIN)))

    prev_runs, late_peak_utc = {}, None                # multi-run spread (FREE) + its late-bound peak time
    try:
        prev_runs = await fetch_previous_runs(client, stn.latitude, stn.longitude, stn.timezone, mkt_day)
        if prev_runs.get("peak_time_max"):
            hh, mm = (int(x) for x in prev_runs["peak_time_max"].split(":"))
            late_peak_utc = datetime.combine(mkt_day, time(hh, mm), tzinfo=z).astimezone(UTC)
    except Exception as e:
        log_failure("open_meteo", kind="fetch_failed", detail=f"previous-runs: {e}",
                    station=stn.station_id, city=stn.city, consequence="fallback")
    m15_peak_local = None
    if STRIKE_USE_MINUTELY15:
        try:
            m15 = await fetch_minutely_15(client, stn.latitude, stn.longitude, stn.timezone, forecast_days=2)
            day = [(dt, tp) for dt, tp in m15 if dt.date() == mkt_day]
            if day:
                m15_peak_local = max(day, key=lambda x: x[1])[0].strftime("%H:%M")
        except Exception as e:
            log_failure("open_meteo", kind="fetch_failed", detail=f"minutely_15: {e}",
                        station=stn.station_id, city=stn.city, consequence="fallback")
    running_max = None
    try:
        running_max, _fr, _ag = await running_max_today(client, stn.station_id, stn.timezone, mkt_day)
    except Exception:
        pass

    fan = fan_summary(stn.station_id, mkt_day)         # intraday peak-convergence fan + freshest forecast met

    strike, src = None, ""
    if agent.enabled():
        ctx = _strike_context(stn, scan, sunrise=sunrise, solar_noon=solar_noon, sunset=sunset,
                              lag_h=lag_h, expected_peak=expected_peak, prev_runs=prev_runs,
                              m15_peak_local=m15_peak_local, running_max=running_max, now_utc=now_utc,
                              lo=win_lo, hi=win_hi, fan=fan)
        prop = await agent.propose_strike(client, ctx)
        if prop is not None and not prop.get("_error") and prop.get("strike_utc"):
            try:
                strike = datetime.fromisoformat(str(prop["strike_utc"]).replace("Z", "+00:00")).astimezone(UTC)
                src = "llm"
            except (ValueError, TypeError):
                strike = None
        if strike is None:
            log_failure("agent", kind="exception", detail="strike: no usable strike_utc → fallback",
                        station=stn.station_id, city=stn.city, consequence="fallback")
    if strike is None:                                 # deterministic fail-safe fallback = peak − FALLBACK_LEAD
        strike, src = peak - timedelta(minutes=STRIKE_FALLBACK_LEAD_MIN), "fallback_peak"

    strike = min(max(strike, win_lo), win_hi)          # bound to the peak-anchored daytime box (subsumes the clamp)
    # extended cutoff = latest plausible peak (strike / forecast peak / solar+lag / multi-run late bound) +
    # buffer, capped at sunset — a late real peak isn't cut off, but we never trade deep into a resolved evening.
    late = max(d for d in (strike, peak, expected_peak, late_peak_utc) if d is not None)
    cutoff = min(late + timedelta(minutes=STRIKE_CUTOFF_BUFFER_MIN), sunset)
    return strike, max(cutoff, strike), src


# ── INTRADAY SNAPSHOT (Part 2): one hourly forecast snapshot per (city, mkt_day) → the convergence fan ──

async def snapshot_city(client: httpx.AsyncClient, stn, scan: "Scan", now_utc: datetime) -> dict | None:
    """Fetch the hourly-met forecast and append ONE compact snapshot {issue_utc, peak_C, peak_local,
    met_at_peak} for `scan.mkt_day`. Accumulated hourly → the day's peak-convergence fan (read by the strike +
    decision LLMs). Returns the record (or None if no usable market-day forecast). NEVER raises (caller logs)."""
    met = await fetch_hourly_met(client, stn.latitude, stn.longitude, stn.timezone, forecast_days=2)
    day = [h for h in met if h["t"].date() == scan.mkt_day]
    if not day:
        return None
    peak = max(day, key=lambda h: h["temp_C"])
    record = {"issue_utc": now_utc.strftime("%Y-%m-%dT%H:%MZ"), "mkt_day": scan.mkt_day.isoformat(),
              "peak_local": peak["t"].strftime("%H:%M"), "peak_C": round(float(peak["temp_C"]), 1),
              "met_at_peak": {k: v for k, v in peak.items() if k != "t"}}
    record["regime"] = classify_regime(record["met_at_peak"])
    append_snapshot(stn.station_id, scan.mkt_day, record)
    return record
