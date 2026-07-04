"""LOCKED-MAX SNIPER (LIVE-capable, ISOLATED) — buys the bucket the METAR has already decided, while the
market is still repricing it. DRY by default (POLY_DRY_RUN=1); LIVE only when the user flips POLY_DRY_RUN=0.

THE EDGE (measured, 2026-07 public-API study over ~1,000 settled June events): the eventual winning bucket
trades at a mean of ~0.59 at 15:00 local / ~0.71 at 16:00 / ~0.83 at 17:00 — the market converges 1-2 hours
AFTER the daily max is physically locked. Day-ahead forecasts have NO edge vs this market (buying the
Open-Meteo forecast bucket loses ~5-12% net) and its calibration shows no longshot bias to farm; the
convergence lag is the one anomaly that survives fees. Taker fee (weather_fees) = 5% × min(p, 1−p) per
share — at ask 0.80 that's 1¢, tiny relative to the measured gap.

WHAT IT DOES, per verified city, every --interval minutes inside the local 12:00-23:00 window:
  1. running_max M from AWC METAR (integer °C over the full local day) + freshness;
  2. today's Open-Meteo 15-min curve (per-city pinned model) → peak time + remaining forecast max;
  3. LOCK = past a genuine afternoon peak (10:00-18:00 local), local clock 13:00-23:00, fresh METAR, AND
     the forecast remaining max strictly below M (no path to a higher bucket) — the same forward-false-lock
     gate latency_paper.py hardened. A GE bucket with M already ≥ its threshold is CERTAIN (monotonic max)
     and needs no lock; an EQ/LE winner needs the full lock.
  4. Require SNIPE_CONFIRM_TICKS consecutive locked observations (a single bad METAR/curve fetch can't buy);
  5. Winning bucket = the one containing M → live CLOB book → TAKER FAK at the best ask if
     ask ≤ SNIPE_ASK_CEILING AND net-of-fee profit/share ≥ SNIPE_MIN_EDGE, sized to visible depth and
     budget caps. HOLD TO SETTLE — never sells (settlement redeems $1).

ISOLATION + STABILITY (the cluster's proven pattern): own state (data/snipe_state.json), own append-only
journal (data/snipe_orders.jsonl), own single-instance flock (data/snipe.lock). Idempotent per
(city, day): a PENDING intent is persisted BEFORE any live submit, so a crash can never double-buy (worst
case = a missed buy). An AMBIGUOUS submit freezes the key (human reconciles; Telegram alert). Every cycle
survives per-city errors; DRY cycles journal WOULD-BUY rows (a forward paper track with real sizing).

WALLET: run/deploy/run_snipe.sh loads .env.snipe (the cluster's isolated wallet — snipe and cluster both
only BUY and hold to settle, so they can never mis-attribute each other's positions; order-level accounting
only, positions_held is never read). The launcher exports POLY_PRICE_CEILING=0.92 for THIS process so the
executor's BUY ceiling clears sniping asks up to SNIPE_ASK_CEILING.

  POLY_DRY_RUN=1 uv run python run/run_snipe.py --once            # DRY pass: prints/journals would-buys
  POLY_DRY_RUN=1 uv run python run/run_snipe.py --interval 5      # DRY loop (paper track)
  (LIVE = the user sets POLY_DRY_RUN=0 in .env.snipe and loads com.polyweather.snipe.plist)
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from polyweather import clob_executor as ex
from polyweather.discovery import _bucket_from_market
from polyweather.models import Bucket, Comparator, Decision
from polyweather.notify import telegram as tg
from polyweather.realized.feature_store import DATA_DIR
from polyweather.runtime.analyze import GAMMA, event_date, running_max_today
from polyweather.forecast.open_meteo import fetch_minutely_15
from polyweather.stations import load_stations

UTC = timezone.utc
UA = {"User-Agent": "polyweather-snipe/1.0"}
CLOB = "https://clob.polymarket.com"
STATE = DATA_DIR / "snipe_state.json"
JOURNAL = DATA_DIR / "snipe_orders.jsonl"
LOCK_FILE = DATA_DIR / "snipe.lock"
TSFMT = "%Y-%m-%dT%H:%M:%SZ"


def _envf(name: str, default: float, lo: float, hi: float) -> float:
    """Env float with a sane-range clamp — a fat-finger can't disable a gate (params.py convention)."""
    try:
        v = float(os.environ.get(name, "") or default)
    except ValueError:
        v = default
    return min(max(v, lo), hi)


FEE_RATE = 0.05                                             # weather_fees: taker pays RATE × min(p, 1−p) / share
ASK_CEILING = _envf("POLY_SNIPE_ASK_CEILING", 0.90, 0.50, 0.97)   # never chase above this (edge gone / fee floor)
MIN_EDGE = _envf("POLY_SNIPE_MIN_EDGE", 0.05, 0.01, 0.50)   # net-of-fee profit per SHARE (1 − ask − fee) must clear this
MAX_USD_EVENT = _envf("POLY_SNIPE_MAX_USD", 25.0, 1.0, 200.0)     # $ cap per (city, day) across attempts
DAILY_CAP_USD = _envf("POLY_SNIPE_DAILY_CAP_USD", 150.0, 5.0, 2000.0)  # $ cap per UTC day across all cities
CONFIRM_TICKS = int(_envf("POLY_SNIPE_CONFIRM_TICKS", 2, 1, 10))  # consecutive locked observations before buying
FRESH_MIN = _envf("POLY_SNIPE_FRESH_MIN", 75.0, 10.0, 180.0)      # newest METAR must be younger than this (min)
MAX_ATTEMPTS = int(_envf("POLY_SNIPE_MAX_ATTEMPTS", 3, 1, 10))    # FAK attempts per (city, day) — partial depth re-tries
WINDOW = (12, 23)                                           # local-hour loop window (lock itself needs ≥13:00)
PEAK_HOURS = (10, 18)                                       # a genuine afternoon peak (outside → suspect curve/rollover)


# ---------------------------------------------------------------- pure decision logic (unit-tested)

def lock_decision(now_local: datetime, peak_local: datetime | None, rem_max: float | None,
                  running_max: int | None, fresh: bool) -> tuple[bool, str]:
    """The forward-false-lock gate (latency_paper heritage): LOCKED iff the day's max can no longer change.
    Requires a GENUINE afternoon peak that has PASSED, the local clock in the afternoon/evening, a FRESH
    METAR floor, and the forecast remaining max STRICTLY below the realized integer max (a future temp would
    need ≥ M+0.5 to round the bucket up — strict `< M` keeps a full 0.5°C safety margin)."""
    if running_max is None:
        return False, "no running_max"
    if not fresh:
        return False, "stale METAR"
    if peak_local is None:
        return False, "no forecast peak"
    if not (PEAK_HOURS[0] <= peak_local.hour <= PEAK_HOURS[1]):
        return False, f"peak {peak_local:%H:%M} outside {PEAK_HOURS[0]}-{PEAK_HOURS[1]}h (suspect)"
    if now_local <= peak_local:
        return False, "peak not passed"
    if now_local.hour < 13:
        return False, "too early (<13:00 local)"
    if rem_max is None:
        return False, "no remaining forecast"
    if rem_max >= running_max:
        return False, f"forecast can still reach {rem_max:.1f} ≥ max {running_max}"
    return True, "locked"


def winning_bucket(buckets: list[Bucket], running_max: int) -> Bucket | None:
    """The bucket the day resolves to IF the max stays at `running_max` (EQ exact, else the LE/GE tail)."""
    for b in buckets:
        if b.comparator is Comparator.EQ and b.threshold == running_max:
            return b
    for b in buckets:
        if ((b.comparator is Comparator.LE and running_max <= b.threshold)
                or (b.comparator is Comparator.GE and running_max >= b.threshold)):
            return b
    return None


def is_certain(bucket: Bucket, running_max: int) -> bool:
    """A GE bucket already reached CANNOT lose (the daily max is monotonic) — no lock needed.
    EQ/LE winners still need the full lock (a later rise re-buckets them)."""
    return bucket.comparator is Comparator.GE and running_max >= bucket.threshold


def taker_fee(price: float) -> float:
    return FEE_RATE * min(price, 1.0 - price)


def plan_size(ask: float, depth_shares: float, spent_event: float, spent_day: float) -> tuple[float, str]:
    """Shares to FAK-buy at `ask`, honoring every cap — or (0, reason). Gates: ask ceiling, net-of-fee
    per-share edge, per-event and daily $ budgets, visible depth, the exchange 5-share / $1 minimums."""
    if ask <= 0 or depth_shares <= 0:
        return 0.0, "no ask/depth"
    if ask > ASK_CEILING + 1e-9:
        return 0.0, f"ask {ask:.3f} > ceiling {ASK_CEILING}"
    edge = 1.0 - ask - taker_fee(ask)
    if edge < MIN_EDGE:
        return 0.0, f"net edge {edge:.3f} < {MIN_EDGE}"
    budget = min(MAX_USD_EVENT - spent_event, DAILY_CAP_USD - spent_day)
    if budget < 1.0:                                        # can't clear the $1 platform minimum
        return 0.0, f"budget exhausted (${budget:.2f} left)"
    shares = min(math.floor(budget / ask), math.floor(depth_shares))
    if shares < 5 or shares * ask < 1.0:                    # exchange minimums (5 shares AND $1 notional)
        return 0.0, f"{shares}sh below exchange minimum at {ask:.3f}"
    return float(shares), "ok"


# ---------------------------------------------------------------- state + journal (cluster pattern)

def load_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        st = json.loads(STATE.read_text())
    except (ValueError, OSError):
        print("WARN: snipe_state.json unreadable — starting empty (journal is the audit)", flush=True)
        return {}
    keep = _shift_days(datetime.now(UTC).strftime("%Y-%m-%d"), -3)
    out = {k: v for k, v in st.items() if (v.get("day") or "9999") >= keep}          # keep ~3 days
    for k, v in out.items():
        if v.get("status") == "PENDING":                    # crashed mid-place → can't know if the FAK landed →
            v["status"] = "AMBIGUOUS"                       # freeze (cost keeps the full reserve); human reconciles
            print(f"startup reconcile: {k} PENDING→AMBIGUOUS (check the CLOB dashboard)", flush=True)
    return out


def _shift_days(iso_day: str, n: int) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(iso_day) + timedelta(days=n)).isoformat()


def save_state(state: dict) -> bool:
    tmp = STATE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, default=str))
        tmp.replace(STATE)
        return True
    except OSError as e:
        print(f"ERROR: cannot persist snipe state ({e!r}) — placing nothing further this cycle", flush=True)
        return False


def journal(rec: dict) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def spent_for(state: dict, key_prefix: str) -> float:
    """$ committed under keys starting with `key_prefix` — FILLED cost + AMBIGUOUS full reserve (conservative)."""
    return sum(float(v.get("cost") or 0.0) for k, v in state.items() if k.startswith(key_prefix))


def day_spent(state: dict, utc_day: str) -> float:
    return sum(float(v.get("cost") or 0.0) for v in state.values() if v.get("utc_day") == utc_day)


# ---------------------------------------------------------------- market I/O

async def best_ask_depth(client: httpx.AsyncClient, token: str) -> tuple[float | None, float]:
    """(best ask, shares offered AT that level) from the live CLOB book — the size a FAK at the ask can take."""
    try:
        b = (await client.get(f"{CLOB}/book", params={"token_id": token}, timeout=15)).json()
    except Exception:
        return None, 0.0
    asks = [(float(a["price"]), float(a["size"])) for a in (b.get("asks") or [])
            if a.get("price") is not None and a.get("size") is not None]
    if not asks:
        return None, 0.0
    best = min(p for p, _ in asks)
    return best, sum(s for p, s in asks if abs(p - best) < 1e-9)


async def todays_buckets(client: httpx.AsyncClient, stn, today_local) -> list[Bucket]:
    try:
        evs = (await client.get(GAMMA, params={"series_id": stn.pm_series_id, "closed": "false", "limit": 10},
                                timeout=20)).json()
    except Exception:
        return []
    ev = next((e for e in evs if event_date(e) == today_local), None)
    if not ev:
        return []
    return [b for b in (_bucket_from_market(m) for m in ev.get("markets", [])) if b and b.token_yes]


# ---------------------------------------------------------------- per-city tick

async def snipe_city(client: httpx.AsyncClient, stn, state: dict, now: datetime, dry: bool,
                     log: list[str]) -> None:
    z = ZoneInfo(stn.timezone)
    now_local = now.astimezone(z)
    if not (WINDOW[0] <= now_local.hour < WINDOW[1]):
        return
    today_local = now_local.date()
    key = f"{stn.station_id}|{today_local.isoformat()}"
    st = state.get(key) or {}
    if st.get("status") in ("DONE", "AMBIGUOUS"):
        return

    M, fresh_ok, age_min = await running_max_today(client, stn.station_id, stn.timezone, today_local)
    fresh = bool(fresh_ok) and (age_min is not None and age_min < FRESH_MIN)
    curve = await fetch_minutely_15(client, stn.latitude, stn.longitude, stn.timezone,
                                    forecast_days=2, model=stn.forecast_model)
    day = [(dt, tp) for dt, tp in curve if dt.date() == today_local]
    peak_local = max(day, key=lambda x: x[1])[0].replace(tzinfo=z) if day else None
    naive_now = now_local.replace(tzinfo=None)
    rem = [tp for dt, tp in day if dt > naive_now]
    rem_max = max(rem) if rem else None

    locked, why = lock_decision(now_local.replace(tzinfo=None),
                                peak_local.replace(tzinfo=None) if peak_local else None,
                                rem_max, M, fresh)
    target = None
    if M is not None and fresh:
        buckets = await todays_buckets(client, stn, today_local)
        target = winning_bucket(buckets, M)
    actionable = target is not None and (locked or is_certain(target, M))

    streak = (st.get("streak") or 0) + 1 if actionable else 0
    prev_label = st.get("label")
    label = f"{target.comparator.value}{target.threshold}C" if target else None
    if actionable and prev_label and label != prev_label:
        streak = 1                                          # the winner MOVED (max climbed) → restart confirmation
    state[key] = st = {**st, "day": today_local.isoformat(), "utc_day": now.strftime("%Y-%m-%d"),
                       "city": stn.city, "streak": streak, "label": label,
                       "running_max": M, "locked": locked, "why": why, "ts": now.strftime(TSFMT)}
    if not actionable:
        return
    if streak < CONFIRM_TICKS:
        log.append(f"  {stn.city}: {label} candidate ({'certain' if is_certain(target, M) else 'locked'}) "
                   f"— confirming {streak}/{CONFIRM_TICKS}")
        return
    if (st.get("attempts") or 0) >= MAX_ATTEMPTS:
        st["status"] = "DONE"
        return

    ask, depth = await best_ask_depth(client, target.token_yes)
    spent_event = spent_for(state, key)
    spent_today = day_spent(state, now.strftime("%Y-%m-%d"))
    shares, reason = plan_size(ask or 0.0, depth, spent_event, spent_today)
    row = {"ts": now.strftime(TSFMT), "station": stn.station_id, "city": stn.city,
           "date": today_local.isoformat(), "bucket": label, "running_max": M,
           "locked": locked, "certain": is_certain(target, M), "ask": ask, "depth_sh": depth,
           "shares": shares, "reason": reason}
    if shares <= 0:
        if "budget exhausted" in reason:
            st["status"] = "DONE"
        if st.get("last_skip") != reason:                   # journal a skip once per distinct reason, not every cycle
            st["last_skip"] = reason
            journal({**row, "status": "SKIP"})
        return
    st.pop("last_skip", None)

    if dry:
        journal({**row, "status": "DRY_WOULD_BUY", "cost": round(shares * ask, 2)})
        log.append(f"  {stn.city} WOULD TAKER-BUY {label} {shares:.0f}sh @ {ask:.3f} "
                   f"(${shares * ask:.2f}, edge {1 - ask - taker_fee(ask):.3f}/sh)")
        return

    # LIVE: persist the PENDING intent BEFORE the network call (crash → never a double-buy)
    st.update(status="PENDING", attempts=(st.get("attempts") or 0) + 1,
              cost=round(spent_event + shares * ask, 2))    # reserve at the ask until the result is known
    if not save_state(state):
        return
    d = Decision(city=stn.city, station_id=stn.station_id, local_date=today_local, label=label,
                 comparator=target.comparator, threshold=target.threshold, token_yes=target.token_yes,
                 action="BET", side="BUY_YES", order_type="taker", confidence=None,
                 market_yes=round(ask, 4), maker_price=round(ask, 4), size_shares=shares, health_ok=True)
    res = ex.place_from_decision(d, now_iso=now.strftime(TSFMT), ttl_min=None)
    if res.ambiguous or (res.ok and (res.filled is None or res.filled <= 0)):
        st.update(status="AMBIGUOUS")                       # cost stays = full reserve (conservative)
        journal({**row, "status": "AMBIGUOUS", "detail": res.detail, "order_id": res.order_id})
        log.append(f"  🚨 {stn.city} {label} AMBIGUOUS taker result — frozen, check the dashboard: {res.detail}")
        await tg.send(client, f"🚨 SNIPE AMBIGUOUS {stn.city} {label}: {res.detail} — check the CLOB dashboard; "
                              f"snipe will NOT retry this (city, day).", kind="snipe-ambiguous")
    elif res.ok and res.filled and res.filled > 0:
        filled = min(float(res.filled), shares)
        cost = round(spent_event + filled * ask, 2)
        st.update(status=("DONE" if cost >= MAX_USD_EVENT - 1.0 else "OPEN"),
                  filled=round((st.get("filled") or 0.0) + filled, 4), cost=cost)
        journal({**row, "status": "FILLED", "filled": filled, "cost": cost, "order_id": res.order_id})
        log.append(f"  ✅ {stn.city} BOUGHT {label} {filled:.0f}sh @ {ask:.3f} (${filled * ask:.2f}) — hold to settle")
        await tg.send(client, f"🎯 SNIPE {stn.city} {label}: bought {filled:.0f}sh @ {ask:.3f} "
                              f"(${filled * ask:.2f}, edge {1 - ask - taker_fee(ask):.2f}/sh)", kind="snipe-fill")
    else:                                                   # clean NOFILL / reject → free the reserve, retry next cycle
        st.update(status="OPEN", cost=round(spent_event, 2))
        journal({**row, "status": "NOFILL", "detail": res.detail})
        log.append(f"  {stn.city} {label} taker NOFILL ({res.detail}) — attempt {st['attempts']}/{MAX_ATTEMPTS}")
    save_state(state)


# ---------------------------------------------------------------- main loop

async def cycle(client: httpx.AsyncClient, stations, state: dict, dry: bool) -> None:
    now = datetime.now(UTC)
    log: list[str] = []
    for stn in stations:
        try:
            await snipe_city(client, stn, state, now, dry, log)
        except Exception as e:                              # one city's error never aborts the cycle
            log.append(f"  {stn.city} error (skipped): {e!r}")
    save_state(state)
    if log:
        print(f"[{now:%H:%MZ}]", flush=True)
        for line in log:
            print(line, flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one pass then exit")
    ap.add_argument("--interval", type=float, default=5.0, help="minutes between cycles (default 5)")
    ap.add_argument("--only", help="comma-separated station ids")
    a = ap.parse_args()

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print("FATAL: another snipe instance holds the lock — exiting.", flush=True)
        sys.exit(1)

    stations = [s for s in load_stations().values() if s.verified]
    if a.only:
        want = set(a.only.split(","))
        stations = [s for s in stations if s.station_id in want]
    dry = ex._dry_run()
    state = load_state()
    print(f"snipe: {len(stations)} cities · DRY_RUN={dry} · ceiling {ASK_CEILING} · min edge {MIN_EDGE}/sh · "
          f"${MAX_USD_EVENT}/event · ${DAILY_CAP_USD}/day · confirm×{CONFIRM_TICKS} · "
          f"telegram={'on' if tg.enabled() else 'DRY'}", flush=True)
    async with httpx.AsyncClient(timeout=40, headers=UA) as client:
        while True:
            try:
                await cycle(client, stations, state, dry)
            except Exception as e:                          # a whole-cycle error logs and continues (24/7)
                print(f"cycle error (continuing): {e!r}", flush=True)
            if a.once:
                break
            await asyncio.sleep(a.interval * 60.0)


if __name__ == "__main__":
    asyncio.run(main())
