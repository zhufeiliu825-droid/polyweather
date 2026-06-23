#!/bin/bash
# Polyweather CLUSTER AUTO-TRADER launcher — called by launchd (com.polyweather.cluster.plist).
# Loads .env.snipe creds into the process env (user 2026-06-23: the cluster now runs on the SEPARATE .env.snipe
# wallet, isolated from the farmer .env wallet), then execs the continuous cluster loop: TAKER-buys the 2 adjacent
# forecast-nearest temperature buckets in the GO cities (gates: cost<0.85 · not-both-<10¢ · edge≥+0.05 ·
# running_max-respecting), HOLDS TO SETTLE (never sells). LIVE iff .env.snipe has POLY_DRY_RUN=0 (the SAME switch as
# the daemon/farmer). SEPARATE process + state (data/cluster_state.json) + single-instance lock
# (data/cluster.lock) from the daemon/farmer — a cluster bug can never touch them. launchd only keeps THIS
# process alive (restart on crash/reboot); the loop has its own --interval cycle.
set -euo pipefail
cd "$(dirname "$0")/../.." || exit 1                # repo root (run/deploy -> ../..)
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
if [ -f .env.snipe ]; then
    bash -n .env.snipe || { echo "ERROR: .env.snipe has shell syntax errors — fix it; not starting."; exit 1; }
    set -a
    . ./.env.snipe                                 # NEW cluster wallet: POLY_*, CLOB_*, OPEN_METEO_API_KEY — never committed
    set +a
else
    echo "ERROR: .env.snipe not found — the cluster's new wallet creds are missing; not starting." >&2
    exit 1
fi
exec uv run python run/run_cluster.py --cap 300 --interval 20
