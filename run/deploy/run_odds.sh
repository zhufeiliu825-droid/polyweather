#!/bin/bash
# Polyweather odds logger launcher (launchd). Read-only Gamma, NO creds, NO orders —
# safe to run 24/7 starting NOW (before the trading daemon goes live) to accumulate the
# irreplaceable odds history (Gamma keeps none).
set -euo pipefail
cd "$(dirname "$0")/../.." || exit 1
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
exec uv run python maintain/odds_logger.py --interval 30
