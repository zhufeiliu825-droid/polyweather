#!/bin/bash
# Polyweather 24/7 daemon launcher — called by launchd (com.polyweather.daemon.plist).
# Loads .env creds into the process env, then execs the daemon. The daemon has its own
# internal hourly/30-min loop; launchd only keeps THIS process alive (restart on crash/reboot).
set -euo pipefail
cd "$(dirname "$0")/../.." || exit 1               # repo root (run/deploy -> ../..)
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
if [ -f .env ]; then
    bash -n .env || { echo "ERROR: .env has shell syntax errors — fix it; not starting."; exit 1; }
    set -a
    . ./.env                                       # POLY_*, CLOB_*, TELEGRAM_* — never committed
    set +a
fi
exec uv run python run/run_daemon.py
