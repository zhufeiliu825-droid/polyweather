#!/bin/bash
# Polyweather reward-FARMER launcher — called by launchd (com.polyweather.rewards.plist).
# Loads .env creds into the process env, then execs the continuous farm loop: passive maker bids on
# TOMORROW+ temperature reward buckets + Stage-2 scalp/flatten on fills. LIVE iff .env has POLY_DRY_RUN=0
# (the SAME switch as the daemon). The loop has its own 5-min cycle; launchd only keeps THIS process alive
# (restart on crash/reboot). SEPARATE process + state (reward_state.json) from the directional daemon.
set -euo pipefail
cd "$(dirname "$0")/../.." || exit 1               # repo root (run/deploy -> ../..)
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
if [ -f .env ]; then
    bash -n .env || { echo "ERROR: .env has shell syntax errors — fix it; not starting."; exit 1; }
    set -a
    . ./.env                                       # POLY_*, CLOB_* — never committed
    set +a
fi
exec uv run python run/run_rewards.py --farm-loop --cap 100 --max 10 --interval 5
