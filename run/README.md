# run/ — the live trading run-set

The autonomous daemon you run 24/7. It's self-contained: facts → analyze_city context → **OpenAI
decision agent** (2-pass gpt-5.4-mini → gpt-5.4; set `OPENAI_API_KEY`, else deterministic) →
**deterministic validator** → maker order. No separate runner service. The model/calibration code is `src/polyweather/` (shared library,
imported, never edited here) and `maintain/` (maintainer tooling). Dead code is in `archived/` — ignore it.

## What's here
- `run_daemon.py` — the 24/7 orchestrator (rescan peaks every 30 min · hourly digest · activate around
  peak−50 min · two-pass decision · manages GTD maker orders and fills). Imports `polyweather`.
- `run_once.py` — one-city end-to-end (DRY) for spot-checking.
- `run_rewards.py` — five-minute reward Farmer; one batched hourly LLM calibration is cached and reused by
  deterministic selection/execution, with station/regime calibration fallback.
- `run_snipe.py` — LOCKED-MAX SNIPER: once a city's daily max is physically locked (past peak + fresh METAR +
  the forecast can't beat the realized max, confirmed 2 cycles), taker-buys the winning bucket at ask ≤ 0.90
  and holds to settle. Motivated by the 2026-07 public-API study: the eventual winner still trades ~0.71 at
  16:00 local / ~0.83 at 17:00 — the one edge that survives fees (forecast-bucket buying does NOT). Isolated
  state/journal/lock (`data/snipe_*`), `.env.snipe` wallet, DRY by default; the DRY loop journals WOULD-BUY
  rows → a forward paper track with real sizing. `POLY_DRY_RUN=1 uv run python run/run_snipe.py --once`.
- `telegram_digest.py` — the user-format digest renderer (used by run_daemon).
- `deploy/` — launchd: `run.sh` (daemon), `run_odds.sh` (odds logger), `maintenance/maintenance.sh`
  (backup / logrotate / settle), and the `com.polyweather.*.plist` agents.

## Run it (paths are repo-root-relative; run from the repo root)
```sh
uv run python run/run_daemon.py --once --only EGLC --activate-all   # DRY smoke (no real order)
uv run python run/run_daemon.py                                     # 24/7 (DRY until POLY_DRY_RUN=0)
```

## Go live (config + run only — no code to write)
1. `uv add py-clob-client`
2. Fill creds in `.env` (repo root, gitignored); **rotate the chat-pasted CLOB creds**; set `POLY_SIGNATURE_TYPE`
   (+ `POLY_FUNDER` for a proxy wallet). Keep `POLY_DRY_RUN=1` to start.
3. Enable the launchd jobs (start the odds logger FIRST):
   ```sh
   for p in oddslogger daemon backup logrotate settle residuals; do
     cp run/deploy/com.polyweather.$p.plist ~/Library/LaunchAgents/
     launchctl load ~/Library/LaunchAgents/com.polyweather.$p.plist
   done
   ```
4. Watch `logs/daemon.out.log` + `data/orders.jsonl` (all `DRY_RUN`).
5. Flip `POLY_DRY_RUN=0` and start small (10 shares by default, with a $1 minimum). **Eyeball the FIRST real order on the
   Polymarket CLOB dashboard** — the live submit path is written from the API spec but has never
   executed; if a field name is off it's a one-line fix to report back (fail-safe: a wrong API-shape
   assumption degrades to no-order + Telegram alert, never a double-bet).

## Full details
- `../README.md` — the complete runbook (data flow, decision gates, order-management policy §3b,
  env contract §4, the `Decision → clob_executor` boundary §5, the JSONL log interface §6).
- `../DATA.md` — the data stores + the codified maintenance schedule.
