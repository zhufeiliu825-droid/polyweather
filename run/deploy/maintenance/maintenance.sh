#!/bin/bash
# Polyweather scheduled maintenance — recurring work codified so it is NEVER manual.
# Invoked by launchd StartCalendarInterval plists (deploy/com.polyweather.<job>.plist):
#     maintenance.sh backup        # daily 02:00 — archive data/ (irreplaceable) + optional off-site
#     maintenance.sh logrotate     # daily 00:10 — gzip text logs + rotate oversized JSONL
#     maintenance.sh residuals     # daily (pre-trading) — rolling forecast−actual bias → the agent's reference
#     maintenance.sh leadtime      # weekly Sun 03:30 — rebuild FIXED-lead σ → the farmer's change-A calibration
# Idempotent + safe to re-run. Each job logs to logs/maintenance.{out,err}.log via launchd.
# (the monthly `calib` job was retired 2026-06-16 — no historical calibration in the live path.)
set -euo pipefail
cd "$(dirname "$0")/../../.." || exit 1              # repo root (run/deploy/maintenance -> ../../..)
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
[ -f .env ] && { set -a; . ./.env; set +a; }         # POLY_BACKUP_DIR / POLY_BACKUP_DEST if set
ts() { date -u +%FT%TZ; }

case "${1:-}" in
  backup)
    stamp=$(date -u +%Y%m%d)
    dest="${POLY_BACKUP_DIR:-$HOME/polyweather-backups}"
    mkdir -p "$dest"
    archive="$dest/data-$stamp.tar.gz"
    tar -czf "$archive" data/ config/ 2>/dev/null || tar -czf "$archive" data/
    echo "[backup $(ts)] wrote $archive ($(wc -c < "$archive") bytes)"
    ls -1t "$dest"/data-*.tar.gz 2>/dev/null | tail -n +31 | xargs -r rm -f   # keep 30 local
    if [ -n "${POLY_BACKUP_DEST:-}" ]; then           # optional off-site (irreplaceable data)
      if [[ "$POLY_BACKUP_DEST" == rclone:* ]] && command -v rclone >/dev/null; then
        rclone copy "$archive" "${POLY_BACKUP_DEST#rclone:}" && echo "[backup $(ts)] off-site rclone ok"
      else
        rsync -a "$archive" "$POLY_BACKUP_DEST" && echo "[backup $(ts)] off-site rsync ok"
      fi
    else
      echo "[backup $(ts)] POLY_BACKUP_DEST unset — LOCAL ONLY (set it for off-site; odds/orders are irreplaceable)"
    fi
    ;;

  logrotate)
    stamp=$(date -u +%Y%m%d-%H%M%S)
    mkdir -p logs/archive data/archive
    for f in logs/*.log; do                           # text logs: gzip-copy then truncate (launchd holds the fd)
      [ -s "$f" ] || continue
      gzip -c "$f" > "logs/archive/$(basename "$f").$stamp.gz" && : > "$f"
      echo "[logrotate $(ts)] archived $f"
    done
    for f in data/decisions.jsonl data/orders.jsonl data/telegram.jsonl data/odds_log.jsonl \
             data/reward_llm_advice.jsonl; do
      [ -f "$f" ] || continue                          # rotate only when oversized (>50MB); copytruncate
      sz=$(wc -c < "$f")
      if [ "$sz" -gt 52428800 ]; then
        gzip -c "$f" > "data/archive/$(basename "$f").$stamp.gz" && : > "$f"
        echo "[logrotate $(ts)] rotated $f ($sz bytes) — archived to data/archive"
      fi
    done
    echo "[logrotate $(ts)] done"
    ;;

  settle)
    echo "[settle $(ts)] capturing resolved outcomes → data/settle.jsonl (for offline 复盘)"
    uv run python maintain/settle_event.py --days 3
    echo "[settle $(ts)] done"
    ;;

  residuals)
    echo "[residuals $(ts)] rolling forecast−actual residuals → data/forecast_residuals.jsonl (agent bias ref)"
    uv run python maintain/build_residuals.py
    echo "[residuals $(ts)] done"
    ;;

  leadtime)
    # FIXED-lead (Previous Runs ECMWF + IEM) residuals → the farmer's honest 24/48h σ (change A). σ is slow-
    # moving, so weekly; rolling 24 mo bounds cost; build to a temp file then atomic-swap so a 5-min farmer
    # cycle never reads a half-written file (residuals._leadtime_index reloads on the new mtime).
    echo "[leadtime $(ts)] rebuilding fixed-lead residuals → data/leadtime_residuals.jsonl"
    uv run python maintain/build_leadtime_dataset.py --out leadtime_residuals.tmp.jsonl \
        --start "$(date -u -v-24m +%Y-%m-01)"
    mv -f data/leadtime_residuals.tmp.jsonl data/leadtime_residuals.jsonl
    echo "[leadtime $(ts)] done ($(wc -l < data/leadtime_residuals.jsonl) rows)"
    ;;

  latency)
    # READ-ONLY paper observer of the locked-max latency-arb (places NO orders). Every 10 min snapshots, per MVP
    # city, whether the daily max is LOCKED and the ==max bucket's live ask + depth<0.90 → data/latency_paper.jsonl.
    echo "[latency $(ts)] paper-observing locked-max latency-arb (read-only) → data/latency_paper.jsonl"
    uv run python maintain/latency_paper.py
    echo "[latency $(ts)] done"
    ;;

  *)
    echo "usage: maintenance.sh <backup|logrotate|settle|residuals|leadtime|latency>" >&2
    exit 2
    ;;
esac
