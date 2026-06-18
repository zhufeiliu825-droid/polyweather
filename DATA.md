# Data stores & recurring maintenance (for the operator)

Everything lives under `data/` (+ `logs/`), gitignored. **Principle: every recurring task is a
scheduled script — never a manual step.** macOS launchd is the scheduler: long-running feeds use
`KeepAlive` agents; periodic maintenance uses one-shot `StartCalendarInterval` agents (in `run/deploy/`).

Two classes of store:
- **Irreplaceable, append-only** (odds/orders/decisions/telegram/obs) — Gamma keeps NO history, so a
  lost file is gone forever. These MUST be backed up off-site + rotated.
- **Regenerable artifacts** (profiles / sqlite / pool) — rebuilt from source on a schedule;
  losing one just means re-running its builder.

---

## 1. Store inventory

| Store | Writer | Purpose | Lifecycle | Maintenance |
|---|---|---|---|---|
| `data/odds_log.jsonl` | `odds_logger.py` (30 min) | forward odds history Gamma doesn't keep (per-bucket bid/ask/mid/last/vol + de-vig) | append-only, **IRREPLACEABLE** | **backup + rotate**; start the logger NOW |
| `data/decisions.jsonl` | `run_daemon.py` (every tick) | decision audit + per-bucket `dist` (`b`, market mid, alive) + the **agent's JSON proposal & reason** | append-only, **irreplaceable** | backup + rotate |
| `data/orders.jsonl` | `clob_executor.py` | every place/cancel (DRY/LIVE/REJECTED) — the money trail | append-only, **irreplaceable** | backup + rotate |
| `data/fills.jsonl` | `run_daemon.py` | confirmed directional-bot fills keyed by order id, including matched shares and confirmation source | append-only, **irreplaceable** | backup + rotate |
| `data/telegram.jsonl` | `notify/telegram.py` | digest/escalation/reply audit | append-only | backup + rotate |
| `logs/journal/<city>.txt` | `run_daemon._journal` | human-readable per-city decision log (bet-or-not + agent prose), one file/city | append-only | operator reads + prunes manually |
| `data/{ICAO}_obs.jsonl` | `build_history.py` + feature store | per-station realized METAR + features (calib/backtest source) | grows per station, **irreplaceable** | backup; per-city backfill as cities verify |
| `data/city_profiles.json` | `profile_cities.py` | per-month climatology (thermal lag for strike timing + scoring) | full-rewrite | refresh ~2×/yr or on new city |
| `data/forecast_residuals.jsonl` | `build_residuals.py` | 30-day Open-Meteo−IEM residual per station with peak-weather `regime`, tags, and met features; feeds detailed AI context + shrunk farmer fair-value calibration | full-rewrite, 30 d rolling | **daily rebuild** |
| `data/reward_llm_advice.json` | reward Farmer | current hourly LLM adjustment/participation cache; rejected after 3 h, regime mismatch, or >1.5°C forecast drift | atomic rewrite | regenerated automatically |
| `data/reward_llm_advice.jsonl` | reward Farmer | successful hourly advice batches with adjustment, confidence, participation, reason and model | append-only | backup + rotate |
| `data/polyweather.db` | `build_history.py` (feature store) | SQLite index for win-rate/reliability/re-fit queries | full rebuild from obs JSONL | nightly rebuild (TODO) |
| `data/daily_pool.json` | `daily_pool.py` | diagnostic scoreboard (digest input) | full-rewrite | optional daily; not gating |
| `config/stations.json` | `build_stations.py`/`add_stations.py`/`verify_new.py` | the 32-city registry (`verified` gates betting) | manual lifecycle | periodic re-verify (TODO) |
| `data/removed_stations.json` | manual | audit of removed cities + re-add steps | rewrite on removal | none |
| `data/snapshots.jsonl`, `data/signals.jsonl` | legacy `output/sink.py` | old monitor test artifacts | ephemeral | **exclude** from backup |
| `data/city_state.json` | `run_daemon.py` (per pass, atomic) | persisted CityState so a restart doesn't orphan resting orders / double-bet | rewrite per transition | none (rebuilt live; backup harmless) |
| `data/settle.jsonl` | `settle_event.py` (daily) | resolved bucket + AWC actual joined only to confirmed `fills.jsonl` records; real filled shares/P&L | append-only | backup; the input to offline model improvement |

---

## 2. Recurring jobs — the codified schedule

| Job | Cadence | How it's run | Status |
|---|---|---|---|
| **Trading daemon** | continuous | `run/deploy/run.sh` ← `com.polyweather.daemon.plist` (KeepAlive) | ✅ built |
| **Odds logger** | every 30 min | `run/deploy/run_odds.sh` ← `com.polyweather.oddslogger.plist` (KeepAlive) | ✅ built — **load it NOW** |
| **Backup** `data/`+`config/` | daily 02:00 | `maintenance.sh backup` ← `com.polyweather.backup.plist` (StartCalendarInterval) | ✅ built |
| **Log rotation** | daily 00:10 | `maintenance.sh logrotate` ← `com.polyweather.logrotate.plist` | ✅ built |
| **Settlement capture** | daily 01:30 UTC | `maintenance.sh settle` (= `settle_event.py`) ← `com.polyweather.settle.plist` | ✅ built (resolved-bucket == AWC actual verified 64/64) |
| **Forecast residuals** | daily 02:30 | `maintenance.sh residuals` (= `build_residuals.py`) ← `com.polyweather.residuals.plist` | ✅ built (IEM-complete local days; detailed station/regime statistics) |
| **SQLite rebuild** | nightly 03:00 | wrap `build_history.py` ← new StartCalendarInterval plist | ⏳ TODO (after obs ingestion flows) |
| **Profiles refresh** | semiannual | wrap `profile_cities.py` | ⏳ TODO (low priority) |
| **Registry re-verify + new-city scan** | monthly | extend `verify_new.py` | ⏳ TODO |
| **Cred-rotation reminder** | monthly alert | `cred_age_check.py` → Telegram (alert only) | ⏳ TODO |

### Enable the built jobs (run once on the Mac mini)
```sh
for p in oddslogger daemon backup logrotate settle residuals; do
  cp run/deploy/com.polyweather.$p.plist ~/Library/LaunchAgents/
  launchctl load ~/Library/LaunchAgents/com.polyweather.$p.plist
done
launchctl list | grep polyweather        # verify loaded
```
Off-site backup: set `POLY_BACKUP_DEST` in `.env` (`rclone:remote:bucket` or `user@host:/path`);
without it `maintenance.sh backup` keeps **local-only** archives in `~/polyweather-backups` (30 kept).

### Ordering / dependencies
`settlement (post-close) → backup (02:00) → sqlite rebuild (03:00)`.

---

## 3. The TODO jobs — how to codify each (per the "no manual work" principle)

- **Settlement capture** (`maintain/settle_event.py`): query closed Gamma events → compute the IEM
  actual daily-max → resolved bucket → join to `decisions.jsonl`/`orders.jsonl` → append
  `{city, date, our_top, bet?, fill?, actual_bucket, won?, pnl}` to `data/settle.jsonl`. This is the
  **复盘 ground truth** + the input to the rolling re-calibration. Then a `StartCalendarInterval` plist.
- **SQLite rebuild**: nightly `build_history.py` rebuild + indices on `(station_id, local_date)` and
  `(event_id, market_date)`; depends on obs + settlement rows existing.
- **Rolling re-calibration**: once `settle.jsonl` exists, re-fit bias/σ from *live* forecast-vs-realized
  residuals (rolling 30–60 d) and Telegram-alert when rolling bias diverges from the backfill (drift).
- **Registry re-verify**: monthly re-run of `verify_new.py` over verified cities + scan Gamma for new
  temp cities → escalate changes (a city that fails re-verification flips `verified=false` → digest-only).
- **Cred-rotation reminder**: alert-only; humans must rotate keys.

> Until `settle.jsonl` + the dashboard exist, you cannot compute realized win-rate / P&L — so the
> forward DRY paper-trade's verdict depends on building settlement capture. It is the next build.
