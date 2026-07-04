#!/bin/bash
# Polyweather LOCKED-MAX SNIPER launcher — called by launchd (com.polyweather.snipe.plist).
# Loads .env.snipe (the SAME isolated wallet as the cluster — both bots only BUY and hold to settle, and both
# keep order-level accounting, so they can never mis-attribute each other's positions), then execs the snipe
# loop: once a city's daily max is LOCKED (past peak + fresh METAR + forecast can't beat the realized max,
# confirmed twice), TAKER-buys the winning bucket at ask ≤ 0.90 and HOLDS TO SETTLE. LIVE iff .env.snipe has
# POLY_DRY_RUN=0 (the same switch as every other bot). SEPARATE process + state (data/snipe_state.json) +
# single-instance lock (data/snipe.lock) — a snipe bug can never touch the daemon/farmer/cluster.
set -euo pipefail
cd "$(dirname "$0")/../.." || exit 1                # repo root (run/deploy -> ../..)
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
if [ -f .env.snipe ]; then
    bash -n .env.snipe || { echo "ERROR: .env.snipe has shell syntax errors — fix it; not starting."; exit 1; }
    set -a
    . ./.env.snipe                                 # snipe/cluster wallet: POLY_*, CLOB_* — never committed
    set +a
else
    echo "ERROR: .env.snipe not found — the snipe wallet creds are missing; not starting." >&2
    exit 1
fi
# The executor's BUY price ceiling must clear sniping asks (POLY_SNIPE_ASK_CEILING, default 0.90). This is
# per-PROCESS: the daemon/farmer keep their own 0.85 ceiling in their own processes.
export POLY_PRICE_CEILING="${POLY_PRICE_CEILING:-0.92}"
exec uv run python run/run_snipe.py --interval 5
