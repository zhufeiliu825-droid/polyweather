# Polyweather — autonomous trading runbook

Automated trading on Polymarket **"Highest temperature in `<city>` on `<date>`"** daily
markets. The system forecasts each city's daily-max bucket, and when a city is near its
predicted peak it places a **resting maker bet** on the bucket or skips it. The runtime is self-contained:
it collects facts, asks the optional OpenAI agent for judgment, hard-validates the result, executes under
local credentials, and records every decision. Model improvement remains an offline process.

---

## 1. Data flow (one city)

```
30-MIN scan (all 32 cities, light: 1 Open-Meteo 15-minute curve each)
  scan_city → peak_utc (argmax) + predicted_max          → render digest → Telegram
        │
        └─ ACTIVATION:  0 < (peak_utc − now) < 50 min
                 │
                 ▼  pass 1, then one stronger retry ~20 min later if no order
   Open-Meteo curve → RAW remaining peak + soft 30-day station/regime residual reference
   AWC METAR → running_max over the FULL local day  + freshness guard (<90 min)
   Gamma event → 11 buckets + CLOB token ids + market prices → EventState
   decision.decide(...) → round(forecast peak) deterministic FALLBACK  (no agent)
   IF OPENAI_API_KEY: agent.propose(context) → JSON   (the agent CHOOSES the bucket; 2-pass mini→gpt-5.4)
                          → decision.validate_proposal (HARD SAFETY gates only) (authority)
        → Decision(action = BET | ESCALATE | SKIP) | None
        │
        ├─ BET      → exact-token momentum check → GTD maker BUY (default 10 shares, auto-expires in 5 min)
        └─ no bet   → retry once, then silent SKIP; account/order anomalies still alert
```

**Realized METAR (AWC) is the final determinant.** The daily max is monotonic; a bucket
below the running max is dead. An EQ/LE bucket exactly at the running max is still possible only if the
temperature does not rise again; a GE bucket already reached has no remaining upside.

---

## 2. Entry points

| Command | Purpose |
|---|---|
| `uv run python run/run_daemon.py` | **24/7 orchestrator** (rescan peaks every 30 min · hourly digest · activate around peak−50 min · two-pass decision · GTD order management · logging). |
| `uv run python run/run_daemon.py --once` | one full pass then exit (smoke test). |
| `uv run python run/run_daemon.py --once --only EGLC,RCSS --activate-all` | force full analysis on a few cities (DRY smoke of the BET/ESCALATE path). |
| `uv run python run/run_once.py --station EGLC` | end-to-end analysis → (dry) order for ONE city; prints the full reasoning. |
| `uv run pytest -q` | the test suite. |

---

## 3. Decision rules — the AGENT chooses the bucket; the deterministic layer only VETOES

The bucket the day's HIGH lands in is a **judgment call** (a 25.9 forecast → ==26, adjusted by the
realized climb + the live atmosphere). The agent (`agent.propose`) makes that call from the RAW forecast
peak + the intraday snapshot fan + the realized running_max + the meteorologist met context + the market
ladder. Recent 30-day Open-Meteo−IEM residuals are a **soft station/regime reference**, not an authority:
the prompt receives count, mean/median, sample variance/SD, MAE/RMSE, direction rates, and conditional
clear/cloudy/rainy/mixed summaries for the forecast peak weather. When `OPENAI_API_KEY` is unset,
`decision.decide()` is a simple fallback: round the
forecast peak to the nearest integer bucket (floored to running_max); an X.5 boundary → ESCALATE.

Either way `decision.validate_proposal` applies the **HARD SAFETY gates only** (it VETOES an unsafe bet →
ESCALATE, it never picks the bucket):

1. **alive** — EQ/LE below `running_max` are dead; GE at/below it has already won and offers no upside.
2. **price** — fav market price `< 0.85` (`PRICE_CEILING`); `≥ 0.85` → no upside → escalate.
3. **health** — de-vigged bucket-price sum ≈ 1, spread not absurd, AND the target not priced ~0 (a bucket
   the market deems impossible is rejected — the natural sanity bound on the agent's freedom).
4. **size** — `≥ 5` shares AND the notional within `[MIN_ORDER_USD=$1, MAX_SIZE_USD=$50]`.

Bet only **pre-peak** (`now < the strike-extended cutoff`); after it the daemon opens nothing new. No daily
cap — every city whose chosen bucket clears the safety gates is bet.

## 3b. Directional order management

An accepted decision posts a resting **GTD maker** at market minus the configured undercut, snapped to
the exchange tick. The exchange auto-expires it after `POLY_MAKER_TTL_MIN` (default 5 minutes); only after
confirmed expiry plus a buffer may the daemon re-evaluate and post another. A fill is recorded and held to
settlement. Intent and result state are persisted so restarts cannot create a second live order.

## 3c. Decision agent — LLM judgment, deterministic authority (OPTIONAL; set an LLM key to enable)

At each activation the daemon adds an LLM judgment step (replacing the dropped "Codex" arm). It is
OPTIONAL: with **no LLM key set, the system runs purely on `decide()`** (the deterministic baseline) —
free, and the deterministic gates are the authority anyway, so this is a complete working configuration.

**OpenAI agent** (`src/polyweather/agent.py`): set **`OPENAI_API_KEY`** (OpenAI Chat Completions,
function-calling). **Two-pass escalating models**, daemon-driven: PASS 1 at activation uses
`POLY_AGENT_MODEL` (default `gpt-5.4-mini`); if no order is placed, PASS 2 ~`POLY_AGENT_RETRY_MIN`(20)min
later uses the stronger `POLY_AGENT_MODEL_RETRY` (default `gpt-5.4`); if PASS 2 also declines → real SKIP.
Momentum is a hard gate in both passes: the final selected token is checked, then refreshed immediately
before submit. A ≥30% drawdown over 5 minutes blocks the order; unavailable final history fails closed by default.
FULL-AUTO — no human prompt (a skip is silent; the hourly digest shows 未下注). A stale `claude-…` model
name is ignored (falls back to the default), so a leftover can't break the call. Low-frequency (a few
calls/city/day, ~1.5–2k tokens each → within OpenAI's free daily tier; gpt-5.4 runs only on the retry).
NOTE: a "free shared" OpenAI tier may share prompts with OpenAI — the context is weather + market numbers
only (no creds/PII), so that's benign.

Flow: `analyze_city` builds a fact context (forecast peak, realized `running_max`, the local-day forecast
curve, peak-weather regime, regime-conditioned residual statistics, market ladder, recent settled outcomes,
the deterministic baseline) →
**`agent.propose()`** calls OpenAI, FORCED
to emit the `emit_decision` tool → structured JSON `{action, comparator, threshold, confidence, reason,
flags}` → **`decision.validate_proposal()` is the AUTHORITY**: it hard-checks the proposal against the
facts and accepts a BET ONLY if the bucket is real, **alive (≥ running_max — dead-bucket backstop)**,
priced `< 0.85`, in a healthy market, ≥ the 5-share exchange minimum and within the $50 notional cap — **never on the
agent's word**. Anything else → ESCALATE (SKIP / post-peak → no order). So the agent adds judgment
(convective day? forecast vs market disagreement? boundary?) and may override the soft margin gate, but
it **cannot breach the hard safety floor**, and it **never places an order** (the deterministic executor
does, under your creds, DRY by default). API error / no-credits / malformed reply → falls back to the
deterministic `decide()` (never a silent bet). The agent's `reason` is a 2–4 sentence prose explanation
written to the per-city journal (`logs/journal/<city>.txt`).

## 3d. Reward Farmer hourly adviser

The Farmer still executes every five minutes with deterministic safety, liquidity, edge, capital, and date
gates. Once per hour, one batched `gpt-5.4-mini` call reviews every active station/date: current Open-Meteo
peak and peak-weather regime, 7/14/30-day forecast-minus-actual residuals, matched-regime samples and
variance, deterministic calibration, and the rewarded market ladder. It returns an expected temperature
adjustment (`actual max − Open-Meteo peak`), participation recommendation, confidence, and reason.

The cached adjustment recomputes adjusted peak, bucket lean, `fair_yes`, and ranking for each five-minute
cycle. Advice is rejected after three hours, after a regime change, or after a forecast revision greater than
1.5°C. Missing/malformed API output falls back to the deterministic shrunk regime calibration. The LLM never
chooses an order, size, or price and cannot bypass the Farmer's hard gates.

---

## 4. Environment / credentials contract

All secrets + tunable knobs live in a gitignored **`.env`** at the repo root (`run/deploy/run.sh`
loads it; for manual runs `set -a; . ./.env; set +a` first). The code reads only `os.environ` —
no secret is ever in code, logs, or committed. The full var list + defaults are below + in `params.py`.

| Var | Used by | Notes |
|---|---|---|
| `OPENAI_API_KEY` | `agent` | unset → deterministic `decide()` (no LLM). Set → per-activation OpenAI judgment agent (validator still hard-checks). |
| `POLY_AGENT_MODEL` | `agent` | PASS-1 model, default `gpt-5.4-mini`. |
| `POLY_AGENT_MODEL_RETRY` | `agent` | PASS-2 (the +20min retry) model, default `gpt-5.4` (stronger). |
| `POLY_FARM_AGENT_MODEL` | Farmer adviser | hourly batched calibration model; default `gpt-5.4-mini`, then falls back to `POLY_AGENT_MODEL`. |
| `POLY_AGENT_RETRY_MIN` | `agent` | minutes before the PASS-2 retry, default `20`. |
| `POLY_MOMENTUM_DROP_PCT` / `POLY_MOMENTUM_WINDOW_MIN` | `analyze` | hard order gate; defaults to a 30% drawdown over 5 minutes. |
| `POLY_MOMENTUM_FAIL_CLOSED` | `analyze` | `1` (default) blocks submit when the final momentum history is unavailable/thin; `0` restores fail-open. |
| `POLY_DRY_RUN` | `clob_executor` | **defaults `1` (DRY)**. `0` = live submit. The single live switch. |
| `POLY_PRIVATE_KEY` | `clob_executor` | wallet key for EIP-712 signing (live only). |
| `CLOB_API_KEY` / `CLOB_API_SECRET` / `CLOB_API_PASSPHRASE` | `clob_executor` | L2 API creds. **Rotate the set pasted in chat 2026-06-14.** |
| `POLY_SIGNATURE_TYPE` / `POLY_FUNDER` | `clob_executor` | `0`=EOA (no funder) · `1`=Magic/email proxy · `2`=browser proxy. **1/2 require `POLY_FUNDER`** (the executor refuses to submit without it). |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | `notify.telegram` | unset → DRY (logs, no send). |
| `CHECKWX_API_KEY` | build scripts only | optional; `build_stations.py` / `add_stations.py` / `checkwx_live.py`. No runtime/daemon path uses it. |

**`py-clob-client` is imported lazily**, only on the live submit path — DRY needs nothing
installed. Before going live: `uv add py-clob-client`.

**Strategy / runtime knobs are env-tunable** — price ceiling (`POLY_PRICE_CEILING`), clear-leader
margin (`POLY_CONF_MARGIN`), bet size in shares (`POLY_SIZE_SHARES`), $ notional cap (`POLY_MAX_SIZE_USD`), maker
undercut, health bands, activation window, recheck cadence, AWC freshness, etc. — all in
`src/polyweather/params.py` with defaults = current values, overridable from `.env` (see the knobs
block there). Edit `.env` then RESTART the daemon to apply.

---

## 5. The `Decision → clob_executor` boundary (the order contract)

`decide()` emits a `Decision` (`src/polyweather/models.py`). For `action == "BET"`,
`clob_executor.place_from_decision(decision, now_iso=...)`:
- validates `side==BUY_YES`, `order_type==maker`, `token_yes` present, `0.01 ≤ maker_price < 0.85`, `size_shares > 0`, and `shares × price ≤ $50` notional cap;
- places a **FIXED `size_shares`** (default 10) — Polymarket sizes orders in shares; the $ cost = `shares × maker_price` (varies with price), snapped down to the exchange tick grid;
- **DRY:** logs the intended order to `data/orders.jsonl` (status `DRY_RUN`), submits nothing;
- **LIVE (`POLY_DRY_RUN=0`):** builds a `ClobClient` from env creds and posts a **GTD** resting maker for the directional bot (the farmer uses GTC); logs the ack.

`clob_executor.cancel_order(order_id, ...)` cancels a resting order (DRY logs; live calls `client.cancel`).
These two functions are the ONLY place orders touch the network — audit them before live.

---

## 6. Observable interface — logs (`data/`, JSONL, append-only)

| File | Written by | Contents |
|---|---|---|
| `data/decisions.jsonl` | daemon, every tick | full per-city context: peak, running max/freshness, action, chosen bucket, market/maker price, momentum, agent proposal/reason, and ladder. |
| `data/orders.jsonl` | `clob_executor` | every place/cancel attempt: DRY_RUN / REJECTED / LIVE + the order detail. |
| `data/fills.jsonl` | `run_daemon` | confirmed main-bot FILLED/PARTIAL transitions keyed by order id, with matched shares and confirmation source. This, not `orders.jsonl`, is settlement fill truth. |
| `data/telegram.jsonl` | `notify.telegram` | every digest / escalation / reply (DRY or SENT). |
| `data/odds_log.jsonl` | `maintain/odds_logger.py` | independent read-only Gamma snapshots — per-bucket bid/ask/mid/last/vol + de-vig sum for every live temp event. The irreplaceable forward odds history (Gamma keeps none). |
| `logs/journal/<city>.txt` | `run_daemon._journal` | **human-readable**, ONE file per city, appended every activation: bet-or-not + the agent's prose WHY. For you to read; prune manually. |
| `data/reward_llm_advice.json` | Farmer adviser | atomically replaced current hourly station/date advice cache. |
| `data/reward_llm_advice.jsonl` | Farmer adviser | append-only history of every successful hourly advice batch. |

`decisions.jsonl` rows carry a **`dist`** field = per-bucket `{b, mkt, alive}` at decision time.
There is no formula-generated `our_p`; the agent chooses the bucket and the validator supplies safety gates.
These logs are the source of truth and the
way to monitor a running daemon (`tail -f`). A queryable SQLite index is a TODO (§7).

---

## 7. Going live — checklist (config + run only, no code to write)

The live-execution hardening is **BUILT** (not a TODO): fill detection (`clob_executor.order_status`
→ a filled order becomes `FILLED`/held, no longer managed), state persistence + restart resume
(`data/city_state.json`, rehydrated on startup; resting orders re-verified each tick), order-success
validation (`_place_and_track` escalates a failed/untrackable order instead of silently RESTING), and
settlement capture (`settle_event.py`). So going live is config + run:

1. `uv add py-clob-client`
2. Fill creds in **`.env`** (already at the repo root, gitignored); **rotate the chat-pasted CLOB creds** — generate a fresh key set in the Polymarket CLOB dashboard, revoke the 2026-06-14 set, confirm it's revoked, put only the new set in `.env`. Set `POLY_SIGNATURE_TYPE` (+ `POLY_FUNDER` for a proxy wallet).
3. (Optional) set `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` for the digest + escalations + order-fail alerts.
   (Optional) set `OPENAI_API_KEY` so the agent CHOOSES the bucket; without it the daemon runs the
   deterministic round(forecast-peak) fallback.
4. Enable the launchd jobs (daemon + odds-logger + maintenance) — see `DATA.md` §2.
5. **First live run = small.** Set `POLY_DRY_RUN=0`. Directional buys are resting makers (default 10 shares;
   cheap buckets size up only as needed to clear the $1 minimum). Watch `orders.jsonl` + `daemon.out.log`.
   **Verify the first real order on the CLOB dashboard** (the live `py-clob-client` submit path is written
   from the API spec but has never executed against the real API — confirm the first place/fill round-trips;
   esp. the FAK taker `status` shape and that `OrderArgs.size` is SHARES; if a field is off, one-line fix).
6. Let it run; `settle_event.py` records outcomes daily for later offline analysis.

> Still genuinely open: the directional bot has no calibrated EV gate. Its agent makes a bucket judgment,
> while the deterministic layer enforces safety rather than proving positive expected value.

---

## 8. Run 24/7 on the Mac mini (launchd)

The daemon already loops internally (hourly scan + 60-s ticks) and survives transient
errors (each tick is wrapped: a network/API blip logs and continues). **launchd** keeps the
*process* alive — restart on crash, start at login:

```sh
# edit the two absolute paths in the plist if the repo isn't at /Users/wekoiuk/polyweather
cp run/deploy/com.polyweather.daemon.plist ~/Library/LaunchAgents/
launchctl load  ~/Library/LaunchAgents/com.polyweather.daemon.plist     # start now + at login
launchctl list | grep polyweather                                       # status (PID, last exit)
tail -f logs/daemon.out.log                                             # live output
launchctl unload ~/Library/LaunchAgents/com.polyweather.daemon.plist    # stop
```

- `KeepAlive=true` + `ThrottleInterval=30` → auto-restart (min 30 s between restarts).
- `RunAtLoad=true` → starts at login.
**Start the odds logger FIRST (now, before the trading daemon — read-only, no creds):**

```sh
cp run/deploy/com.polyweather.oddslogger.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.polyweather.oddslogger.plist   # → data/odds_log.jsonl every 30 min
```

It accumulates the forward odds history that validating the edge depends on — the gap only
fills going forward, so the sooner it runs the better. Same launchctl commands as the daemon.

**Recurring maintenance is codified** (never manual) as one-shot `StartCalendarInterval` launchd
jobs in `run/deploy/` (`maintenance.sh` = backup daily / log-rotation daily / calib re-fit monthly).
See **`DATA.md`** for the full data-store inventory, what's irreplaceable vs regenerable, the
complete maintenance schedule + the enable-all loop, and the remaining TODO jobs (settlement
capture, SQLite rebuild) with their codify plan.

- Set **System Settings → Energy → Prevent sleep** (a LaunchAgent only runs while logged in/awake). For run-without-login, install as a root LaunchDaemon in `/Library/LaunchDaemons` instead.
- Logs rotate nowhere by default — add a `newsyslog`/logrotate entry or truncate `logs/*.log` periodically.
- Back up `data/` off-site daily (odds/outcome logs are irreplaceable — Gamma keeps no history).

---

## 9. Layout — 3 zones

```
src/polyweather/          # SHARED LIBRARY (Claude maintains; run/ + maintain/ import it)
  clob_executor.py        # Decision → maker order (place/cancel/order_status); DRY_RUN default
  decision.py             # decide() [deterministic baseline] + validate_proposal() [agent hard floor] + devig
  agent.py                # OpenAI decision agent → structured-JSON proposal (opt-in via OPENAI_API_KEY)
  models.py               # EventState, Bucket, Decision, Comparator   (unit is always °C)
  discovery.py            # Gamma event → buckets/tokens (_bucket_from_market, parse °C-only)
  runtime/analyze.py      # scan_city (light) + analyze_city (full per-city tick) + strike timing + snapshots
  forecast/{open_meteo,upside,snapshots}.py · realized/{metar_source,metar_parse,metar_obs,feature_store,solar}.py
  notify/telegram.py · stations.py · util/{pmjson,timewin}.py

run/                    # THE LIVE RUN-SET (the autonomous daemon — you launch it)
  run_daemon.py · run_once.py · telegram_digest.py · deploy/ (run.sh, run_odds.sh, maintenance/, *.plist)
  README.md               # operating quickstart

maintain/                 # MAINTAINER tooling (Claude + you, OFFLINE) — scheduled, not the live loop
  settle_event.py · odds_logger.py · build_stations.py · add_stations.py
  verify_new.py · profile_cities.py

archived/                 # DEAD — nobody touches (legacy monitor modules, old dev scripts, their tests)
config/stations.json (32 °C cities) · data/ (logs + artifacts) · tests/ (live-path tests)
```

**Registry note:** only `verified=true` cities are bet (`run_daemon` skips the rest — they
still appear in the digest). `verify_new.py` is the only writer of `verified`. Two
never-verified cities (Shenzhen/ZGSZ, Karachi/OPMR) were removed 2026-06-15 → archived in
`data/removed_stations.json` with re-add steps.
