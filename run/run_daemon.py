"""24/7 orchestrator — the analysis→order pipeline as a daemon.

  HOURLY (a few min past the hour, never :00): scan all 32 cities' Open-Meteo
  local-day curve → peak/predicted_max → render the user-format digest → Telegram.
  ACTIVATION: a city goes active when 0 < (peak − now) < ACTIVATE_WINDOW_MIN (default 50min).
  PER-CITY LOOP: pass 1, then one stronger retry after AGENT_RETRY_MIN (default 20min): analyze_city →
  the agent CHOOSES the bucket,
  validate_proposal hard-gates it (decision.decide is the agent-down fallback) → BET / ESCALATE.
  ORDER MGMT — PHASE 2 (user-set 2026-06-17): on BET, place a resting maker @ market−0.1¢ (cheap
  buckets sized UP to clear $1, never a taker) as a GTD that AUTO-EXPIRES after MAKER_TTL_MIN; poll
  order_status → a fill is held to settlement, a CONFIRMED expiry → re-evaluate + re-place until
  filled or the peak cutoff (4-state Telegram: 已下单/已成交/已退单/未下单). The exchange removes
  the GTD at expiry — we NEVER cancel, so no cancel/replace double-bet window. One LIVE order per
  (city,event) at a time, persisted crash-safely:
  a PLACING intent is written BEFORE the live submit and the PLACED status IMMEDIATELY
  after, so a crash/restart in the window can never re-place (a leftover PLACING →
  UNRESOLVED for the human, never auto-fired). A single-instance flock blocks a 2nd copy.
  LEGACY TWO-WAY: a persisted ESCALATED state can still accept «city bet [bucket]» after fresh
  re-validation; normal full-auto decisions no longer create that state or prompt the operator.
  Every tick → data/decisions.jsonl; orders → data/orders.jsonl; msgs → telegram.jsonl.

SAFETY: clob_executor defaults DRY_RUN (POLY_DRY_RUN=1) and Telegram is DRY without
a token — so the whole daemon runs end-to-end with NO live order / NO send until the
user puts creds in env and flips POLY_DRY_RUN=0. The LLM never submits live.

    uv run python run/run_daemon.py                          # 24/7
    uv run python run/run_daemon.py --once                   # one full pass, exit
    uv run python run/run_daemon.py --once --only EGLC,RCSS --activate-all  # DRY smoke
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from polyweather import agent
from polyweather import clob_executor as ex
from polyweather.decision import validate_proposal
from polyweather.notify import telegram as tg
from polyweather.obslog import log_failure, recent_failure_summary
from polyweather.params import (ACTIVATE_WINDOW_MIN, AGENT_RETRY_MIN, MAKER_TTL_MIN, RECHECK_MIN,  # tunable knobs
                                SCAN_INTERVAL_MIN, SCAN_MIN_PAST_HOUR, STRIKE_CONFIRM_LEAD_MIN,
                                STRIKE_ENABLED, STRIKE_GRACE_MIN, TICK_SECONDS)
from polyweather.realized import obs_store
from polyweather.realized.feature_store import DATA_DIR
from polyweather.runtime.analyze import (Scan, analyze_city, decide_strike, enforce_order_momentum,
                                         scan_city, snapshot_city)
from polyweather.stations import load_stations

sys.path.insert(0, str(Path(__file__).parent))
from telegram_digest import render_digest  # noqa: E402  (sibling script, user-defined format)

HDR = {"User-Agent": "Mozilla/5.0 polyweather/0.1"}
UTC = timezone.utc
DECISIONS_LOG = DATA_DIR / "decisions.jsonl"
FILLS_LOG = DATA_DIR / "fills.jsonl"
ORDERS_LOG = DATA_DIR / "orders.jsonl"
STATE_FILE = DATA_DIR / "city_state.json"
OFFSET_FILE = DATA_DIR / "tg_offset.txt"   # persisted Telegram getUpdates offset (audit H1)
LOCK_FILE = DATA_DIR / "daemon.lock"       # single-instance flock (audit M3)
JOURNAL_DIR = DATA_DIR.parent / "logs" / "journal"   # human-readable per-city .txt decision log
# ACTIVATE_WINDOW_MIN / RECHECK_MIN / SCAN_MIN_PAST_HOUR / TICK_SECONDS ← polyweather.params (POLY_* env)

# A city is acted on at most ONCE per event; these statuses are never re-touched by the decision
# pass (ESCALATED can still be acted on by an explicit Telegram reply — that's the only exception).
#   PLACING = live order in flight (intent persisted before post_order; a restart finding this can't
#             know if the order landed → reconciled to UNRESOLVED). UNRESOLVED = maybe-live order the
#             human must check on the CLOB dashboard. DRY_PLACED = a DRY "order" (reset to PENDING on
#             a live restart so the live path can actually place — audit M1).
# TERMINAL = done for this (city, event), never re-acted. RESTING is NOT terminal — it's a live GTD maker the
# daemon polls (→ FILLED on a fill, or re-places on expiry). FILLED/DRY_PLACED = held to settlement.
# PLACED = LEGACY phase-1 held order (the new path never sets it); kept terminal so a leftover PLACED in a
# pre-phase-2 state file is never re-bet on reload (double-bet guard across the lifecycle migration).
TERMINAL = ("FILLED", "DRY_PLACED", "ESCALATED", "SKIP", "UNRESOLVED", "PLACING", "PLACED")
# Order-lifecycle safety margins (NOT user knobs — they bound the no-double-bet window):
EXPIRY_BUFFER_MIN = 2     # re-place only this long AFTER the GTD's TTL — guarantees the exchange already removed the old order
STUCK_GRACE_MIN   = 10    # if order_status can't be confirmed this long past expiry → ALERT (never re-place on unknown)


@dataclass
class CityState:
    mkt_day: date | None = None
    status: str = "PENDING"          # PENDING/RETRY/PLACING/RESTING/FILLED/DRY_PLACED/ESCALATED/SKIP/UNRESOLVED
    order_id: str | None = None
    token_yes: str | None = None     # CLOB token id we BOUGHT — confirms a FILL via our positions (get_order can't)
    placed_at: datetime | None = None  # when the resting GTD maker was posted — drives the TTL expiry clock
    threshold: int | None = None
    comparator: str | None = None    # escalated bucket comparator ("<="/"=="/">=") — for reply re-validation
    label: str | None = None
    price: float | None = None
    size_shares: float | None = None  # planned order size; persisted so a later fill can be bounded/audited
    last_tick: datetime | None = None
    escalated: bool = False
    escalated_at: datetime | None = None  # when the escalation was sent — a «bet» reply sent BEFORE this is a stale replay, rejected (audit H1)
    pass1_at: datetime | None = None      # when pass-1 (gpt-5.4-mini) ran for a RETRY city — gates the +AGENT_RETRY_MIN pass-2
    strike_at: datetime | None = None     # stage-1 chosen strike time (clamped to the daytime window) — when to run the bet
    cutoff_at: datetime | None = None     # stage-1 extended post-peak cutoff (covers a late real peak), capped at sunset
    strike_decided: bool = False          # stage-1 ran once for this (city, mkt_day) — never re-fire (+1 mini call/day)
    strike_src: str | None = None         # "llm" | "fallback_lag" | "fallback_argmax" | "no_peak" — for the journal/log


def _log_decision(row: dict) -> None:
    with DECISIONS_LOG.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _journal(tick) -> None:
    """Append a human-readable record to logs/journal/<city>.txt — ONE file per city, every
    activation (bet or not) + the agent's prose WHY. The operator reads + prunes these manually."""
    d = tick.decision
    action = d.action if d else "NO-ACTION"
    peak = f"{tick.peak_utc:%H:%MZ}" if tick.peak_utc else "—"
    if d and d.action == "BET":
        line = f"PLACED maker BUY {d.label} @ {d.maker_price} (conf {d.confidence} vs mkt {d.market_yes}), {d.size_shares} sh"
    elif d and d.action == "ESCALATE":
        line = f"NO BET → ESCALATE {d.label} (conf {d.confidence} vs mkt {d.market_yes})"
    else:
        line = f"NO BET ({tick.note or 'peak passed / below realized / no data'})"
    reason = (d.reason if (d and d.reason) else tick.note) or "—"
    block = (f"─── {tick.now:%Y-%m-%d %H:%MZ} · {tick.city} · {action} ───\n"
             f"  forecast peak {tick.predicted_max}°C @{peak} · forecast_peak(raw) {tick.remaining}°C · running_max {tick.running_max}°C\n"
             f"  {line}\n"
             f"  reason: {reason}\n\n")
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    with (JOURNAL_DIR / f"{tick.city.lower().replace(' ', '')}.txt").open("a") as f:
        f.write(block)


def _log_fill_once(rec: dict) -> None:
    """Append one confirmed directional fill, idempotently by order id.

    This runs before the CityState transition is persisted: if the process crashes between the two writes,
    the next poll retries and the order-id check prevents a duplicate record.
    """
    order_id = rec.get("order_id")
    if not order_id:
        raise ValueError("confirmed fill is missing order_id")
    if FILLS_LOG.exists():
        for line in FILLS_LOG.read_text().splitlines():
            try:
                prior = json.loads(line)
            except (TypeError, ValueError):
                continue
            if prior.get("strategy") == "directional" and prior.get("order_id") == order_id:
                return
    with FILLS_LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _fill_record(sid: str, city: str, cs: "CityState", now: datetime,
                 position_size: float | None, order_state: str) -> dict:
    """Build the audit record for a confirmed fill, preferring order-level matched size when available."""
    matched = ex.filled_size(cs.order_id) if order_state in ("FILLED", "PARTIAL") else None
    source = "order"
    if matched is None or matched <= 0:
        matched = position_size if position_size is not None and position_size > 0 else None
        source = "positions"
    if matched is None and order_state == "FILLED" and cs.size_shares is not None:
        matched, source = cs.size_shares, "order_status"
    if matched is not None and cs.size_shares is not None:
        matched = min(float(matched), cs.size_shares)  # shared-funder co-holds must not inflate our recorded fill
    filled_shares = round(float(matched), 6) if matched is not None else None
    partial = order_state == "PARTIAL" or (filled_shares is not None and cs.size_shares is not None
                                            and filled_shares < cs.size_shares - 1e-9)
    return {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "strategy": "directional",
        "station": sid, "city": city, "market_date": cs.mkt_day.isoformat() if cs.mkt_day else None,
        "bucket": cs.label, "order_id": cs.order_id, "token": cs.token_yes,
        "status": "PARTIAL" if partial else "FILLED", "order_status": order_state,
        "planned_shares": cs.size_shares, "filled_shares": filled_shares, "confirmed_by": source,
    }


def _submitted_size(order_id: str | None) -> float | None:
    """Recover the planned shares of a persisted pre-fill-log GTD order from the append-only order log."""
    if not order_id or not ORDERS_LOG.exists():
        return None
    for line in reversed(ORDERS_LOG.read_text().splitlines()):
        try:
            order = json.loads(line)
        except (TypeError, ValueError):
            continue
        if (order.get("order_id") == order_id and order.get("status") == "LIVE"
                and order.get("side") == "BUY" and order.get("type") == "GTD-maker"):
            try:
                return float(order["shares"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def reconcile_fill_logs(state: dict[str, "CityState"], stations, now: datetime) -> bool:
    """Backfill confirmed FILLED states created before fills.jsonl existed. Returns whether state changed."""
    if ex._dry_run():
        return False
    held = ex.positions_held()
    by_id = {s.station_id: s for s in stations}
    changed = False
    for sid, cs in state.items():
        if cs.status != "FILLED" or not cs.order_id:
            continue
        if cs.size_shares is None:
            cs.size_shares = _submitted_size(cs.order_id)
            changed = changed or cs.size_shares is not None
        city = by_id[sid].city if sid in by_id else sid
        position_size = _held_size(held, cs, city)
        rec = _fill_record(sid, city, cs, now, position_size, "PERSISTED_STATE")
        if rec["filled_shares"] is None:
            rec["confirmed_by"] = "persisted_state"
        _log_fill_once(rec)
    return changed


def save_state(state: dict[str, "CityState"]) -> None:
    """Persist CityState so a restart (launchd WILL restart) doesn't orphan resting orders
    or double-bet. Atomic write (tmp + replace)."""
    out = {sid: {"mkt_day": cs.mkt_day.isoformat() if cs.mkt_day else None, "status": cs.status,
                 "order_id": cs.order_id, "token_yes": cs.token_yes,
                 "threshold": cs.threshold, "comparator": cs.comparator,
                 "label": cs.label, "price": cs.price, "size_shares": cs.size_shares,
                 "last_tick": cs.last_tick.isoformat() if cs.last_tick else None,
                 "placed_at": cs.placed_at.isoformat() if cs.placed_at else None,
                 "escalated": cs.escalated,
                 "escalated_at": cs.escalated_at.isoformat() if cs.escalated_at else None,
                 "pass1_at": cs.pass1_at.isoformat() if cs.pass1_at else None,
                 "strike_at": cs.strike_at.isoformat() if cs.strike_at else None,
                 "cutoff_at": cs.cutoff_at.isoformat() if cs.cutoff_at else None,
                 "strike_decided": cs.strike_decided, "strike_src": cs.strike_src}
           for sid, cs in state.items()}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


def load_state() -> dict[str, "CityState"]:
    """Rehydrate CityState on startup so a restart never re-places an order already PLACED
    this event (idempotency). Phase-1 holds to settlement, so no per-tick order re-verification."""
    if not STATE_FILE.exists():
        return {}
    raw = json.loads(STATE_FILE.read_text())
    out = {}
    for sid, d in raw.items():
        out[sid] = CityState(
            mkt_day=date.fromisoformat(d["mkt_day"]) if d.get("mkt_day") else None,
            status=d.get("status", "PENDING"), order_id=d.get("order_id"), token_yes=d.get("token_yes"),
            threshold=d.get("threshold"), comparator=d.get("comparator"),
            label=d.get("label"), price=d.get("price"), size_shares=d.get("size_shares"),
            last_tick=datetime.fromisoformat(d["last_tick"]) if d.get("last_tick") else None,
            placed_at=datetime.fromisoformat(d["placed_at"]) if d.get("placed_at") else None,
            escalated=d.get("escalated", False),
            escalated_at=datetime.fromisoformat(d["escalated_at"]) if d.get("escalated_at") else None,
            pass1_at=datetime.fromisoformat(d["pass1_at"]) if d.get("pass1_at") else None,
            strike_at=datetime.fromisoformat(d["strike_at"]) if d.get("strike_at") else None,
            cutoff_at=datetime.fromisoformat(d["cutoff_at"]) if d.get("cutoff_at") else None,
            strike_decided=d.get("strike_decided", False), strike_src=d.get("strike_src"))
    return out


def save_offset(n: int | None) -> None:
    """Persist the Telegram getUpdates offset so a restart never re-polls (and replays) already-seen
    replies — a stale «bet» replay could place an unwanted order otherwise (audit H1)."""
    if n is None:
        return
    tmp = OFFSET_FILE.with_suffix(".tmp")
    tmp.write_text(str(n))
    tmp.replace(OFFSET_FILE)


def load_offset() -> int | None:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def is_active(scan: Scan, now: datetime, *, strike_at: datetime | None = None,
              cutoff_at: datetime | None = None) -> bool:
    if strike_at is not None:                        # STRIKE MODE: eligible from the strike time until the cutoff
        end = cutoff_at or (strike_at + timedelta(minutes=ACTIVATE_WINDOW_MIN))
        return (strike_at - timedelta(minutes=STRIKE_GRACE_MIN)) <= now < end
    if scan.peak_utc is None:                        # no strike (STRIKE_ENABLED=0 / no peak) → original peak−window
        return False
    dmin = (scan.peak_utc - now).total_seconds() / 60.0
    return 0 < dmin < ACTIVATE_WINDOW_MIN


def build_pool(scans: dict[str, Scan]) -> dict:
    """render_digest format: cities[] with station/city/market_date/strike_utc
    (= peak−1h). Cities whose curve fetch failed (no peak) are dropped + counted."""
    cities, dropped = [], []
    for s in scans.values():
        if s.peak_utc is None:
            dropped.append(s.station_id)
            continue
        # render_digest does _parse(strike_utc)+1h = peak, so store peak−1h:
        strike = s.peak_utc.astimezone(UTC).replace(second=0, microsecond=0) - timedelta(hours=1)
        cities.append({"station": s.station_id, "city": s.city,
                       "market_date": s.mkt_day.isoformat(),
                       "strike_utc": strike.strftime("%Y-%m-%d %H:%MZ")})
    return {"cities": cities, "excluded": [], "_dropped": dropped}


async def scan_all(client, stations, now) -> dict[str, Scan]:
    """Recompute every city's peak time (1 light Open-Meteo curve each). Run every SCAN_INTERVAL_MIN
    so the narrow activation window tracks a peak time that the forecast re-estimates through the day."""
    scans = await asyncio.gather(*[scan_city(client, s, now) for s in stations],
                                 return_exceptions=True)
    out = {}
    for s, sc in zip(stations, scans):
        out[s.station_id] = sc if isinstance(sc, Scan) else Scan(
            s.station_id, s.city, now.astimezone(ZoneInfo(s.timezone)).date(),
            None, None, note=f"scan error: {sc!r}")
    return out


def _bets_for_digest(state: dict[str, CityState]) -> dict:
    """{sid: {bucket, price, status}} for cities with an order, status = 已成交 (FILLED) | 挂单中 (RESTING /
    DRY_PLACED / legacy PLACED, a live maker not yet known-filled). Everything else renders as 未下注."""
    out = {}
    for sid, cs in state.items():
        if cs.threshold is None:
            continue
        if cs.status == "FILLED":
            out[sid] = {"bucket": str(cs.threshold), "price": cs.price, "status": "已成交"}
        elif cs.status in ("RESTING", "DRY_PLACED", "PLACED"):   # PLACED = legacy phase-1 held order (still an order)
            out[sid] = {"bucket": str(cs.threshold), "price": cs.price, "status": "挂单中"}
    return out


async def write_snapshots(client, stations, scans, now) -> None:
    """Hourly: append one intraday forecast snapshot per city → the day's peak-convergence fan (Part 2).
    Best-effort — a snapshot fetch failure logs + skips that city, never blocks the digest/decision pass."""
    for stn in stations:
        sc = scans.get(stn.station_id)
        if sc is None or sc.peak_utc is None:
            continue
        try:
            await snapshot_city(client, stn, sc, now)
        except Exception as e:
            log_failure("open_meteo", kind="fetch_failed", detail=f"snapshot: {e!r}",
                        station=stn.station_id, city=stn.city, consequence="skipped")


async def send_digest(client, scans, tz_by_station, state, now) -> None:
    pool = build_pool(scans)
    text = render_digest(pool, tz_by_station, _bets_for_digest(state), now)
    if pool["_dropped"]:
        text += f"\n(scan-failed, omitted: {', '.join(pool['_dropped'])})"
    fail = recent_failure_summary(60, now=now)         # surface recurring source failures in the hourly digest
    if fail:
        text += f"\n{fail}"
    await tg.send(client, text, kind="digest")


_STATUS_EMOJI = {"已下单": "📤", "已成交": "✅", "已退单": "⏰", "未下单": "❌"}


async def _order_broadcast(client, zh: str, city: str, label: str, detail: str = "") -> None:
    """Telegram the order-lifecycle status: 已下单 (placed) / 已成交 (filled) / 已退单 (expired) / 未下单 (not placed)."""
    await tg.send(client, f"{_STATUS_EMOJI.get(zh, '')} {zh} {city} {label}"
                          f"{(' · ' + detail) if detail else ''}", kind=f"order-{zh}")


async def _place_once(client, stn, cs: CityState, d, iso: str, state: dict[str, CityState],
                      now: datetime) -> str:
    """Place the order ONCE → 'placed' | 'unresolved' | 'no_order' | 'momentum_blocked'.

    Momentum is refreshed for the exact token immediately before any executor/state mutation. Every
    accepted order is a RESTING GTD maker
    (the daemon polls it → FILLED on a fill, or re-places on expiry); broadcasts 已下单 on placement,
    已成交 on a later fill; a definite no-order broadcasts 未下单.

    CRASH-SAFE (audit C1/C2/H2): persist a PLACING intent BEFORE post_order and the live status (RESTING/
    FILLED) + order_id + placed_at IMMEDIATELY after — a crash in the window reconciles PLACING→UNRESOLVED
    on startup (never a double-bet). 'unresolved' (maybe-live → human reconciles, NEVER retried) is terminal.
    'no_order' returns to handle() (RETRY pass-2 vs SKIP)."""
    momentum_ok, momentum = await enforce_order_momentum(client, d, now)
    if not momentum_ok:
        print(f"[{now:%H:%MZ}] {stn.station_id} order blocked by momentum: "
              f"{momentum.get('reason') or momentum.get('drop_pct')}", flush=True)
        return "momentum_blocked"
    live = not ex._dry_run()
    cs.threshold, cs.label, cs.comparator, cs.price = d.threshold, d.label, d.comparator.value, d.maker_price
    cs.size_shares = d.size_shares
    cs.token_yes = d.token_yes               # the token we're buying — lets poll_resting confirm a fill via positions
    if live:                                        # write intent BEFORE the network call
        cs.status = "PLACING"
        save_state(state)
    res = ex.place_from_decision(d, now_iso=iso)
    if res.ok and (res.order_id or res.dry_run):
        cs.order_id = res.order_id
        sz = f"{d.order_type} @ {d.maker_price} ×{d.size_shares}sh"
        if res.dry_run:
            cs.status = "DRY_PLACED"
            save_state(state)
            await _order_broadcast(client, "已下单", stn.city, d.label, f"{sz} (DRY)")
        else:                                       # every order is a resting GTD maker → poll for fill/expiry
            cs.status, cs.placed_at = "RESTING", now
            save_state(state)                       # persist RESTING + placed_at immediately (crash-safe TTL clock)
            await _order_broadcast(client, "已下单", stn.city, d.label, f"{sz} ({MAKER_TTL_MIN}min GTD)")
        return "placed"
    if res.ambiguous:                               # order MAY be resting on the book → human reconciles
        cs.status, cs.escalated, cs.escalated_at = "UNRESOLVED", True, now
        save_state(state)
        await tg.send(client, f"🚨 ORDER UNRESOLVED {stn.city} {d.label}: {res.detail}\n"
                              f"The order MAY be live — CHECK the CLOB dashboard. The daemon will NOT "
                              f"retry this (city,event).", kind="order-unresolved")
        return "unresolved"
    # definitely no order (rejected / creds / bad version) → 未下单; handle() sets RETRY/SKIP.
    await _order_broadcast(client, "未下单", stn.city, d.label, res.detail)
    return "no_order"


async def handle(client, stn, tick, cs: CityState, now: datetime, state: dict[str, CityState],
                 pass_num: int) -> None:
    """FULL-AUTO two-pass: BET → place the maker GTD ONCE (re-bid on expiry until filled/cutoff). NO order placed
    (no-bet / dead / post-peak / placement-rejected) → pass-1: arm RETRY for the +AGENT_RETRY_MIN pass-2
    (gpt-5.4); pass-2 (or a closed window): real SKIP. NO human prompt — a skip is SILENT (the hourly
    digest shows the city as 未下注); only account/order anomalies alert (inside _place_once)."""
    if cs.status in TERMINAL:
        return
    d = tick.decision
    cutoff = cs.cutoff_at or tick.peak_utc              # strike-extended cutoff (covers a late peak); else forecast peak
    peak_passed = cutoff is not None and now >= cutoff
    if not peak_passed and d is not None and d.action == "BET":
        iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        if await _place_once(client, stn, cs, d, iso, state, now) in ("placed", "unresolved"):
            return                                          # terminal already set (PLACED / DRY_PLACED / UNRESOLVED)
    # NO order placed (no-bet / dead / post-peak / placement-rejected):
    if pass_num == 1 and not peak_passed:
        cs.status, cs.pass1_at = "RETRY", now               # arm the pass-2 (gpt-5.4) retry in AGENT_RETRY_MIN
    else:                                                   # pass-2 declined, or the window closed → real SKIP (silent)
        cs.status = "SKIP"
    save_state(state)


def _held_size(held: dict | None, cs: "CityState", city: str) -> float | None:
    """Shares we currently HOLD for this city's bet (the ground-truth fill check) — None if positions
    couldn't be queried. Match by the stored token id (new orders), else by title for a LEGACY PLACED order
    with no token (city name + '<thr>°C' + the market day, space/case-normalized so 'kualalumpur' hits
    'Kuala Lumpur' and the day disambiguates a same-bucket position from another date)."""
    if held is None:
        return None
    if cs.token_yes:
        info = held.get(str(cs.token_yes))
        return info["size"] if info else 0.0
    if cs.threshold is None:
        return 0.0
    cnorm, deg = city.lower().replace(" ", ""), f"{cs.threshold}°c"
    day = (cs.mkt_day.strftime("%B").lower() + str(cs.mkt_day.day)) if cs.mkt_day else ""
    for info in held.values():
        t = info["title"].lower().replace(" ", "")
        if cnorm in t and deg in t and (not day or day in t):
            return info["size"]
    return 0.0


async def poll_resting(client, stations, scans, state: dict[str, CityState], now: datetime) -> None:
    """Poll every live maker (RESTING, or a LEGACY phase-1 PLACED) for its real fate: a FILL → FILLED (held
    to settlement, ✅ 已成交); a confirmed EXPIRY (RESTING only) → ⏰ 已退单 → re-evaluate (PENDING → re-place
    next decision pass) or, past the peak cutoff, a real SKIP.

    GROUND-TRUTH FILL = our POSITIONS, not order status: `get_order` returns GONE (404) for a filled order
    once it's off the book (verified live 2026-06-17), so a fill is confirmed by HOLDING the token. NO-DOUBLE-
    BET: a re-place fires ONLY when positions CONFIRM we hold nothing AND order_status is affirmatively gone
    (CANCELLED/EXPIRED/GONE — off the book) AND the GTD's TTL+buffer elapsed → never a 2nd live order. If
    positions can't be queried (None), or the order is still OPEN, or unconfirmable (ERROR), we NEVER re-place
    — wait, and past STUCK_GRACE alert + freeze (fail-closed). A legacy PLACED only ever upgrades to FILLED
    (held); it's never re-placed. DRY cities are DRY_PLACED (never RESTING/PLACED-live) → effectively a no-op."""
    relevant = [(sid, cs) for sid, cs in state.items() if cs.status in ("RESTING", "PLACED") and cs.order_id]
    if not relevant:
        return
    held = ex.positions_held()                                  # token_id -> {size,title,redeemable}, or None on failure
    by_id = {s.station_id: s for s in stations}
    for sid, cs in relevant:
        city = by_id[sid].city if sid in by_id else sid
        sz = _held_size(held, cs, city)                        # >0 held · 0 confirmed-not-held · None unknown
        st = ex.order_status(cs.order_id)
        if (sz or 0) > 0 or st in ("FILLED", "PARTIAL"):       # FILL — positions authoritative, order_status backs it up
            if cs.status != "FILLED":
                _log_fill_once(_fill_record(sid, city, cs, now, sz, st))
                cs.status = "FILLED"
                save_state(state)
                await _order_broadcast(client, "已成交", city, cs.label or "")
            continue
        if cs.status == "PLACED":                              # legacy GTC, not held → leave (clears at day rollover)
            continue
        if cs.placed_at is None or (now - cs.placed_at).total_seconds() / 60.0 < MAKER_TTL_MIN + EXPIRY_BUFFER_MIN:
            continue                                           # within the GTD window → wait
        age_min = (now - cs.placed_at).total_seconds() / 60.0
        if sz == 0 and st in ("CANCELLED", "EXPIRED", "GONE"):  # positions CONFIRM unfilled AND order off the book → re-bid
            await _order_broadcast(client, "已退单", city, cs.label or "", f"{MAKER_TTL_MIN}min未成交")
            sc = scans.get(sid)
            cutoff = cs.cutoff_at or (sc.peak_utc if sc else None)
            if cutoff is not None and now >= cutoff:           # past the peak window → stop, real SKIP
                cs.status = "SKIP"
            else:                                              # re-evaluate + re-place on the next decision pass
                cs.status, cs.order_id, cs.token_yes, cs.placed_at, cs.last_tick = "PENDING", None, None, None, None
            save_state(state)
        elif age_min >= MAKER_TTL_MIN + STUCK_GRACE_MIN:       # can't confirm gone+unfilled this long → fail-closed
            # OPEN = still live on the book; ERROR/positions-unknown = unconfirmable → re-placing could DOUBLE-BET.
            cs.status, cs.escalated, cs.escalated_at = "UNRESOLVED", True, now
            save_state(state)
            await tg.send(client, f"🚨 ORDER STUCK {city} {cs.label}: status {st}, positions "
                                  f"{'unknown' if held is None else 'not-held'} {STUCK_GRACE_MIN}min past the "
                                  f"{MAKER_TTL_MIN}min GTD — NOT re-placing. CHECK the CLOB dashboard.", kind="order-stuck")
        # else: gone-but-positions-unknown, or OPEN, within grace → wait, poll again next tick


async def decision_pass(client, stations, scans, now, state, *, activate_all=False) -> None:
    for stn in stations:
        if not stn.verified:                                        # unverified → digest only, never bet
            continue
        sc = scans[stn.station_id]
        cs = state.setdefault(stn.station_id, CityState(mkt_day=sc.mkt_day))
        if cs.mkt_day != sc.mkt_day:                                # new local day → fresh (any prior order holds to settle)
            if cs.status in ("UNRESOLVED", "PLACING", "RESTING"):   # don't silently drop a maybe-live order's reminder
                await tg.send(client, f"⚠️ {stn.city}: rolling to {sc.mkt_day} with a prior {cs.status} order "
                                      f"({cs.label or cs.threshold}) for {cs.mkt_day} — CHECK the CLOB dashboard; "
                                      f"it is no longer tracked.", kind="rollover-unresolved")
            state[stn.station_id] = cs = CityState(mkt_day=sc.mkt_day)
        if cs.status in TERMINAL:                                   # acted (or skipped) this event → done
            continue
        if cs.status == "RESTING":                                  # a live GTD maker — poll_resting owns it (fill/expiry/
            continue                                                # re-place); decision_pass must NOT place a 2nd order
        # STAGE 1 (once/city/day): at peak−CONFIRM_LEAD ("confirm" time, ~1h out) decide the strike time +
        # extended cutoff, BEFORE activation. Firing this LATE (not at first sight) lets the mini reason over
        # mature realized data + the full intraday fan. STRIKE_ENABLED=0 (or an error) leaves strike_at None →
        # activation/cutoff fall back to the original peak−window behaviour.
        if (STRIKE_ENABLED and not cs.strike_decided and sc.peak_utc is not None
                and (sc.peak_utc - now).total_seconds() / 60.0 <= STRIKE_CONFIRM_LEAD_MIN):
            try:
                cs.strike_at, cs.cutoff_at, cs.strike_src = await decide_strike(client, stn, sc, now)
            except Exception as e:                                  # never let strike-timing break the pass
                log_failure("daemon", kind="exception", detail=f"decide_strike: {e!r}",
                            station=stn.station_id, city=stn.city, consequence="fallback")
                cs.strike_at, cs.cutoff_at, cs.strike_src = None, None, "error"
            cs.strike_decided = True
            save_state(state)
        cutoff = cs.cutoff_at or sc.peak_utc                        # strike-extended cutoff; else the forecast peak
        peak_passed = cutoff is not None and now >= cutoff
        if cs.status == "RETRY":                                    # pass-1 declined → pass-2 (the stronger gpt-5.4)
            if not activate_all and (cs.pass1_at is None
                                     or (now - cs.pass1_at).total_seconds() / 60.0 < AGENT_RETRY_MIN):
                continue                                            # not yet AGENT_RETRY_MIN since pass-1
            if peak_passed:                                         # window closed → real SKIP, don't burn the gpt-5.4 quota
                cs.status = "SKIP"
                save_state(state)
                continue
            agent_model, pass_num = agent.retry_model(), 2
        else:                                                       # PENDING → pass-1 (the daily gpt-5.4-mini)
            if not (activate_all or is_active(sc, now, strike_at=cs.strike_at, cutoff_at=cs.cutoff_at)):
                continue
            if (cs.last_tick is not None and not activate_all
                    and (now - cs.last_tick).total_seconds() / 60.0 < RECHECK_MIN):
                continue                                            # throttle a pass-1 RE-run only if pass-1 raised
            agent_model, pass_num = None, 1                         # None → _model() default (gpt-5.4-mini)
        try:
            tick = await analyze_city(client, stn, now, sc, agent_model=agent_model, cutoff_utc=cs.cutoff_at)
            cs.last_tick = now                 # set AFTER a successful analysis — a transient raise below leaves
            #                                    the city retryable, not frozen out
            _log_decision(tick.log_row())      # machine-readable, all cities → data/decisions.jsonl
            await handle(client, stn, tick, cs, now, state, pass_num)
            _journal(tick)                     # human-readable prose → logs/journal/<city>.txt
        except Exception as e:                 # one city's transient error must NOT abort the rest of the pass
            log_failure("daemon", kind="exception", detail=repr(e),
                        station=stn.station_id, city=stn.city, consequence="skipped")
            print(f"[{now:%H:%MZ}] {stn.station_id} decision error (skipped, pass continues): {e!r}", flush=True)
            continue                           # (and apply_replies still runs this tick — it follows decision_pass)


async def _apply_one_reply(client, rp, by_slug, state, now, iso) -> None:
    """Process ONE «city bet [bucket]» / «city skip» reply. A «bet» does NOT blindly force a live
    order (audit C2): it authorizes a SPECIFIC bucket — the one we escalated (and the operator saw).
    We re-run the analysis and re-validate THAT exact bucket through validate_proposal (the hard
    alive/price/health/floor/size gates still apply — the operator picks the bucket, gates hold). The
    bet is placed only if it passes; if the bucket drifted, is now dead/over-ceiling/unhealthy, or
    the typed number disagrees with the escalation, we re-notify and place NOTHING. Raising here is
    caught by apply_replies so one bad reply can't abort the rest of an already-ACKed batch (audit r5)."""
    stn = by_slug.get(rp["city"])
    if not stn:
        return
    cs = state.get(stn.station_id)
    if cs is None or cs.status != "ESCALATED":   # ONLY an escalated city is reply-actionable — a «bet»
        return                                   # must answer an escalation (which sets escalated_at, the
        #                                          freshness anchor). A PENDING city has no anchor → never
        #                                          place off a reply (closes the PENDING stale-replay hole).
    if rp["action"] == "skip":
        cs.status = "SKIP"
        save_state(state)
        return
    # action == "bet" → confirm the bucket the operator authorizes (the escalated bucket, else typed)
    want_thr, want_comp = cs.threshold, cs.comparator
    if rp.get("bucket") is not None:
        typed = int(rp["bucket"])                # rp["bucket"] is pre-validated int-parseable by _parse_reply
        if want_thr is not None and typed != want_thr:          # typed a different number than escalated
            await tg.send(client, f"⚠️ {stn.city}: you said bet {typed} but the escalation was "
                                  f"{want_comp or ''}{want_thr}. Re-send «{rp['city']} bet {want_thr}» "
                                  f"to confirm, or «{rp['city']} skip».", kind="reply-mismatch")
            return
        want_thr = typed
    if want_thr is None:
        await tg.send(client, f"⚠️ {stn.city}: no bucket to bet — reply «{rp['city']} bet <number>».",
                      kind="reply-nobucket")
        return
    # FRESHNESS GUARD (audit H1) — FAIL CLOSED: place only if we can affirmatively confirm this «bet»
    # was sent AFTER the escalation it answers, using the Telegram send-time (msg_epoch), not poll-time.
    # A missing escalation baseline, a missing send-time, or a send-time predating the escalation all =
    # an unverifiable/replayed reply → refuse + ask to re-confirm. (escalated_at is always set on an
    # ESCALATED city, by handle() and _place_once; this also defends a re-surfaced old message.)
    sent_epoch = rp.get("msg_epoch")
    if (cs.escalated_at is None or sent_epoch is None
            or datetime.fromtimestamp(sent_epoch, tz=UTC) < cs.escalated_at):
        await tg.send(client, f"⚠️ {stn.city}: can't confirm «bet {want_thr}» is a fresh reply to the "
                              f"current {(want_comp or '')}{want_thr} escalation — ignoring (possible replay). "
                              f"Re-send «{rp['city']} bet {want_thr}» to confirm.", kind="reply-stale")
        return
    tick = await analyze_city(client, stn, now)                 # fresh facts (forecast/AWC/odds may have moved)
    if tick.state is None or tick.peak_utc is None:
        await tg.send(client, f"⚠️ {stn.city}: can't act on «bet» now ({tick.note or 'no live event/peak'}).",
                      kind="reply-noevent")
        return
    proposal = {"action": "BET", "comparator": (want_comp or "=="), "threshold": want_thr,
                "reason": "operator-confirmed via Telegram"}
    d = validate_proposal(proposal, tick.state, tick.remaining, stn.station_id,
                          now=now, peak_utc=tick.peak_utc)         # operator picks bucket; hard gates hold
    if d is not None and d.action == "BET":
        await _place_once(client, stn, cs, d, iso, state, now)
    else:                                                       # now dead / over-ceiling / unhealthy / gone
        reason = d.reason if d is not None else "peak passed — no new positions"
        await tg.send(client, f"⚠️ {stn.city}: can't place {(want_comp or '==')}{want_thr} — {reason}. "
                              f"Not bet.", kind="reply-rejected")


async def apply_replies(client, by_slug, state, now, offset):
    """Poll Telegram replies and dispatch each through _apply_one_reply. The polled batch is ACKed
    DURABLY (save_offset) BEFORE processing so a mid-pass error can't cause a re-poll/replay (audit H1);
    each reply is then isolated in try/except so a single malformed reply (or any unexpected error)
    can't abort the rest of that already-ACKed batch and silently lose a queued legit «bet» (audit r5)."""
    replies, new_offset = await tg.poll_replies(client, offset)
    save_offset(new_offset)
    iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for rp in replies:
        try:
            await _apply_one_reply(client, rp, by_slug, state, now, iso)
        except Exception as e:
            log_failure("daemon", kind="exception", detail=f"reply {rp.get('city')}: {e!r}", consequence="skipped")
            print(f"[{now:%H:%MZ}] reply error (skipped one, batch continues): {e!r}", flush=True)
    return new_offset


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one full pass then exit")
    ap.add_argument("--only", help="comma-separated station ids to limit to")
    ap.add_argument("--activate-all", action="store_true", help="force full analysis on all (DRY smoke)")
    a = ap.parse_args()

    try:                                                # init failures must log clearly, not crash-loop silently
        stations = list(load_stations().values())
        if a.only:
            want = set(a.only.split(","))
            stations = [s for s in stations if s.station_id in want]
        tz_by_station = {s.station_id: s.timezone for s in stations}
        by_slug = {s.city.lower().replace(" ", ""): s for s in stations}
    except Exception as e:
        print(f"FATAL init error (fix the registry/config, then restart): {e!r}", flush=True)
        sys.exit(1)

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)         # single-instance lock (audit M3)
    _lock_fh = open(LOCK_FILE, "w")                             # kept open for the process lifetime
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print("FATAL: another polyweather daemon instance holds the lock — exiting (prevents double-placing).",
              flush=True)
        sys.exit(1)

    state: dict[str, CityState] = load_state()      # rehydrate across restarts (idempotency)
    tg_offset = load_offset()                       # don't replay already-seen Telegram replies (audit H1)
    last_scan_at: datetime | None = None            # peak rescan cadence (every SCAN_INTERVAL_MIN)
    last_digest_hour: int | None = None             # Telegram digest cadence (hourly at :SCAN_MIN_PAST_HOUR)

    live = not ex._dry_run()
    reconciled = []
    for sid, cs in list(state.items()):
        if cs.status == "PLACING":                  # crashed mid-place → can't know if it landed → human checks
            cs.status, cs.escalated = "UNRESOLVED", True
            reconciled.append(f"{sid}:PLACING→UNRESOLVED")
        elif cs.status == "DRY_PLACED" and live:     # un-poison: a DRY 'order' must not block the live path (M1)
            state[sid] = CityState(mkt_day=cs.mkt_day)
            reconciled.append(f"{sid}:DRY_PLACED→PENDING")
    if reconciled:
        save_state(state)
        print(f"startup reconcile: {', '.join(reconciled)}", flush=True)
    if reconcile_fill_logs(state, stations, datetime.now(UTC)):
        save_state(state)                              # persist planned shares recovered for pre-fill-log positions

    resuming = sum(1 for cs in state.values() if cs.status in ("RESTING", "FILLED", "ESCALATED", "UNRESOLVED"))
    print(f"daemon: {len(stations)} cities · DRY_RUN={ex._dry_run()} · telegram={'on' if tg.enabled() else 'DRY'}"
          f" · resumed {resuming} open from state", flush=True)
    scans: dict[str, Scan] = {}
    async with httpx.AsyncClient(timeout=40, headers=HDR) as client:
        unresolved = [sid for sid, cs in state.items() if cs.status == "UNRESOLVED"]
        if unresolved:                                  # surface maybe-live orders to the operator, not just stdout
            await tg.send(client, f"🚨 STARTUP: {len(unresolved)} order(s) UNRESOLVED after a restart "
                                  f"({', '.join(unresolved)}) — they MAY be live; CHECK the CLOB dashboard. "
                                  f"The daemon will not retry them.", kind="startup-unresolved")
        while True:
            now = datetime.now(UTC)
            try:                                            # 24/7: one bad tick must NOT kill the loop
                # PEAK RESCAN — every SCAN_INTERVAL_MIN (keeps the narrow activation window drift-robust)
                do_scan = (a.once or last_scan_at is None
                           or (now - last_scan_at).total_seconds() / 60.0 >= SCAN_INTERVAL_MIN)
                if do_scan:
                    scans = await scan_all(client, stations, now)
                    last_scan_at = now
                    print(f"[{now:%H:%MZ}] scan {len(scans)} cities (peaks refreshed)", flush=True)
                    n_obs = await obs_store.capture_all(client, stations)   # persist realized obs (off the order path)
                    if n_obs:
                        print(f"[{now:%H:%MZ}] obs capture: +{n_obs} new", flush=True)
                if scans:                                   # need a scan before any decision pass
                    await poll_resting(client, stations, scans, state, now)   # fill/expiry on live GTD makers (re-place)
                    await decision_pass(client, stations, scans, now, state, activate_all=a.activate_all)
                    if any(cs.status == "ESCALATED" for cs in state.values()):
                        tg_offset = await apply_replies(client, by_slug, state, now, tg_offset)
                    save_state(state)                       # persist after every pass (crash/restart safe)
                    save_offset(tg_offset)                  # persist the Telegram offset (audit H1)
                    active = [sid for sid, cs in state.items()
                              if cs.status in ("RESTING", "FILLED", "DRY_PLACED", "ESCALATED", "UNRESOLVED")]
                    if active:
                        print(f"[{now:%H:%MZ}] open: "
                              + ", ".join(f"{s}:{state[s].status}" for s in active), flush=True)
                # DIGEST — hourly at :SCAN_MIN_PAST_HOUR; sent AFTER the decision pass so it reflects THIS tick's
                # placements/fills/expiries (a city just placed shows 已下注, not 未下注). (Was sent before → stale.)
                do_digest = a.once or (now.hour != last_digest_hour and now.minute >= SCAN_MIN_PAST_HOUR)
                if do_digest and scans:
                    await write_snapshots(client, stations, scans, now)   # accumulate the intraday peak-convergence fan
                    await send_digest(client, scans, tz_by_station, state, now)
                    last_digest_hour = now.hour
                    print(f"[{now:%H:%MZ}] digest sent ({len(scans)} cities)", flush=True)
            except Exception as e:                          # transient (network/API) → log + continue
                log_failure("daemon", kind="exception", detail=f"tick: {e!r}", consequence="skipped")
                print(f"[{now:%H:%MZ}] tick error (continuing): {e!r}", flush=True)
            if a.once:
                break
            await asyncio.sleep(TICK_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
