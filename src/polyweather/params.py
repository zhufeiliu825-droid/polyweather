"""Tunable strategy + runtime parameters — ALL overridable via env (`POLY_*`); the defaults
below are the current values. One place to change a knob without touching code.

Read ONCE at import → edit `.env` then RESTART the daemon (launchd KeepAlive restarts it) to
apply. Every money-safety knob is CLAMPED to a sane hard range at parse time: an out-of-range or
non-finite (nan/inf) override is REJECTED (a warning to stderr + the safe default) so a single
fat-finger can never silently disable a gate (e.g. `POLY_PRICE_CEILING=1.5`, `POLY_MAX_SIZE_USD=nan`,
`POLY_MIN_ORDER_USD=0`). These are NOT secrets — they can live in `.env` (gitignored) or be exported.
"""
from __future__ import annotations

import math
import os
import sys


def _warn(msg: str) -> None:
    print(f"[params] {msg}", file=sys.stderr, flush=True)


def _f(name: str, default: float) -> float:
    """Float env override; fall back to `default` on junk OR a non-finite value. Rejecting nan/inf is a
    money-safety must: a `notional > nan` / `spread > nan` comparison is silently False → the cap would
    FAIL OPEN (audit r5)."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        _warn(f"{name}={raw!r} not a number → using default {default}")
        return default
    if not math.isfinite(v):
        _warn(f"{name}={raw!r} is non-finite → using default {default}")
        return default
    return v


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        _warn(f"{name}={raw!r} not an int → using default {default}")
        return default


def _fb(name: str, default: float, lo: float, hi: float) -> float:
    """Bounded float knob: an override outside [lo, hi] → fall back to the safe default (a fat-finger
    must never disable a money-safety gate). Defaults are always in range."""
    v = _f(name, default)
    if not (lo <= v <= hi):
        _warn(f"{name}={v} out of range [{lo}, {hi}] → using default {default}")
        return default
    return v


def _ib(name: str, default: int, lo: int, hi: int) -> int:
    v = _i(name, default)
    if not (lo <= v <= hi):
        _warn(f"{name}={v} out of range [{lo}, {hi}] → using default {default}")
        return default
    return v


# --- decision gates ---  (range = hard safety bound, NOT the tuning sweet-spot)
PRICE_CEILING       = _fb("POLY_PRICE_CEILING", 0.85, 0.01, 0.99)  # fav price ≥ this → escalate; also the live hard cap
DEAD_BUCKET_MARGIN  = _fb("POLY_DEAD_BUCKET_MARGIN", 0.5, 0.1, 2.0) # betting ==running_max: if the remaining-hours forecast
#                                              peak is ≥ running_max + this, the temp is expected to RISE past it → that bucket
#                                              likely DIES → escalate (don't auto-bet the about-to-die at-the-current-max bucket)
HEALTH_SUM_LO       = _fb("POLY_HEALTH_SUM_LO", 0.85, 0.50, 1.0)   # acceptable band for the de-vigged bucket-price sum
HEALTH_SUM_HI       = _fb("POLY_HEALTH_SUM_HI", 1.20, 1.0, 2.0)
MAX_SPREAD          = _fb("POLY_MAX_SPREAD", 0.25, 0.01, 0.5)      # widest acceptable bid-ask on the target bucket

# --- order / sizing ---
# MAKER bets are a FIXED share count (SIZE_SHARES); a TAKER bet is sized to ~MIN_ORDER_USD of notional
# (so a cheap bucket still clears the $1 platform minimum — see the TAKER block). MAX_SIZE_USD is the hard
# upper $ notional cap; MIN_ORDER_USD is the platform's lower $ floor (verified: $1).
SIZE_SHARES         = _fb("POLY_SIZE_SHARES", 10.0, 5.0, 1000.0)   # per-MAKER-bet share count (the test size)
MAX_SIZE_USD        = _fb("POLY_MAX_SIZE_USD", 50.0, 0.5, 500.0)   # hard per-order $ notional CAP (shares×price)
MIN_ORDER_USD       = _fb("POLY_MIN_ORDER_USD", 1.0, 0.5, 50.0)    # Polymarket min order = $1 notional (verified by the user)
MIN_ORDER_SHARES    = _fb("POLY_MIN_ORDER_SHARES", 5.0, 5.0, 1000.0)  # AND a 5-SHARE exchange minimum (both must hold)
MAKER_UNDERCUT      = _fb("POLY_MAKER_UNDERCUT", 0.001, 0.0, 0.10)  # undercut the market by this; the executor SNAPS to the
#                                                        market's real tick → effectively −0.1¢ on a 0.001-tick market, −1¢ on a 0.01-tick one
MAKER_FLOOR         = _fb("POLY_MAKER_FLOOR", 0.001, 0.001, 0.10)   # min posted maker price (finest Polymarket tick)
MAKER_TTL_MIN       = _ib("POLY_MAKER_TTL_MIN", 5, 1, 240)         # resting maker = a GTD that auto-expires after N min (exchange-side; no manual cancel)

# --- MOMENTUM guard ---
# (Cheap buckets are ALWAYS a resting maker now — no taker: a 10-share maker only clears the $1 min at ≥10¢,
#  so below that `decision._order_plan` SIZES UP the maker share count instead of crossing the spread. Makers
#  earn liquidity rewards + pay 0 fee + never cross — strictly better than the old FAK taker.)
# Momentum guard: if the selected token fell ≥ MOMENTUM_DROP_PCT over the last MOMENTUM_WINDOW_MIN
# minutes, either pass is blocked. The exact token is refreshed once more immediately before submit.
MOMENTUM_DROP_PCT   = _fb("POLY_MOMENTUM_DROP_PCT", 0.30, 0.01, 1.0)  # ≥ this relative drop in the window = a "big drop"
MOMENTUM_WINDOW_MIN = _ib("POLY_MOMENTUM_WINDOW_MIN", 5, 1, 60)       # look-back window for the drop check (minutes)
MOMENTUM_FAIL_CLOSED = _ib("POLY_MOMENTUM_FAIL_CLOSED", 1, 0, 1)     # final pre-submit check unavailable → block the order

# --- strike-timing stage-1 (an LLM, or a deterministic solar+lag fallback, decides WHEN to run the bet) ---
# Stage-1 ONLY chooses the strike time; it can never touch the bucket/price/safety gates. The strike is HARD-
# CLAMPED into the daytime solar window so a spurious early-morning peak can never schedule a pre-dawn strike.
STRIKE_ENABLED          = _ib("POLY_STRIKE_ENABLED", 1, 0, 1)            # master switch; 0 → exact pre-strike behaviour (peak−window)
STRIKE_MIN_LAG_H        = _fb("POLY_STRIKE_MIN_LAG_H", 0.0, -2.0, 6.0)   # strike window lower bound = solar_noon + this (0 = solar noon)
STRIKE_CUTOFF_BUFFER_MIN = _ib("POLY_STRIKE_CUTOFF_BUFFER_MIN", 45, 0, 180)  # buffer past the expected/late-bound peak before the clock cutoff (then clamped to sunset)
STRIKE_GRACE_MIN        = _ib("POLY_STRIKE_GRACE_MIN", 10, 0, 60)        # activation grace around the strike time (clock jitter)
STRIKE_USE_MINUTELY15   = _ib("POLY_STRIKE_USE_MINUTELY15", 1, 0, 1)     # include the minutely_15 peak in stage-1 context (a paid Forecast call)
# Peak-anchored strike (Part 3): fire stage-1 at peak−CONFIRM_LEAD (the "confirm" time), and bound the
# strike the mini outputs to [peak−CONFIRM_LEAD, peak+MAX_LAG] (then clamped to the daytime window). The
# anchor is the FORECAST PEAK; the mini only picks the offset. Fallback (no LLM) = peak−FALLBACK_LEAD.
STRIKE_CONFIRM_LEAD_MIN = _ib("POLY_STRIKE_CONFIRM_LEAD_MIN", 60, 5, 180)  # fire stage-1 when peak−now ≤ this; also the strike's earliest offset before the peak
STRIKE_MAX_LAG_MIN      = _ib("POLY_STRIKE_MAX_LAG_MIN", 90, 0, 240)       # latest strike = peak + this (capped at sunset) — never overshoot far past the peak (the 19:15 bug)
STRIKE_FALLBACK_LEAD_MIN = _ib("POLY_STRIKE_FALLBACK_LEAD_MIN", 30, 0, 120)  # no-LLM fallback strike = peak − this

# --- timing / loop ---
ACTIVATE_WINDOW_MIN = _ib("POLY_ACTIVATE_WINDOW_MIN", 50, 1, 120)  # activate when 0 < peak−now < this (pass-1 ≈ peak−50)
AGENT_RETRY_MIN     = _ib("POLY_AGENT_RETRY_MIN", 20, 1, 120)      # gap before the pass-2 (gpt-5.4) retry, after pass-1
RECHECK_MIN         = _ib("POLY_RECHECK_MIN", 30, 1, 240)          # throttle a pass-1 RE-run if pass-1 raised (not gating pass-2)
SCAN_INTERVAL_MIN   = _ib("POLY_SCAN_INTERVAL_MIN", 30, 1, 120)    # recompute every city's peak time this often (15-min curve absorbs the slack)
AWC_FRESH_MIN       = _ib("POLY_AWC_FRESH_MIN", 90, 15, 360)       # trust running_max only if newest AWC obs < N min old
SCAN_MIN_PAST_HOUR  = _ib("POLY_SCAN_MIN_PAST_HOUR", 7, 0, 59)     # send the Telegram digest at :NN past the hour (never :00)
TICK_SECONDS        = _ib("POLY_TICK_SECONDS", 60, 5, 600)         # daemon loop sleep

# --- cross-knob invariants (reconcile after the per-knob clamps) ---
if HEALTH_SUM_LO >= HEALTH_SUM_HI:                 # an inverted band would never trip the health gate
    _warn(f"HEALTH_SUM_LO {HEALTH_SUM_LO} >= HEALTH_SUM_HI {HEALTH_SUM_HI} → resetting to 0.85/1.20")
    HEALTH_SUM_LO, HEALTH_SUM_HI = 0.85, 1.20
if SIZE_SHARES < MIN_ORDER_SHARES:                 # an order below the exchange min would always reject
    _warn(f"SIZE_SHARES {SIZE_SHARES} < MIN_ORDER_SHARES {MIN_ORDER_SHARES} → raising SIZE_SHARES to the min")
    SIZE_SHARES = MIN_ORDER_SHARES
