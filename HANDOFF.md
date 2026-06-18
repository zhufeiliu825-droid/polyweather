# HANDOFF — Polyweather / Reward Farmer

_Written 2026-06-18; synchronized 2026-06-19 by Codex after the hourly Farmer LLM rollout._

> **LIVE now:** the Farmer makes one batched `gpt-5.4-mini` calibration call per hour, caches an event-level
> `actual_max - Open-Meteo_peak` adjustment plus participate/confidence/reason, and reuses it in every 5-minute
> cycle. The LLM changes adjusted peak, lean, fair and ranking but never calls the executor. Advice expires after
> 3h and is rejected on regime change or >1.5°C forecast drift; deterministic station/regime calibration is the
> fallback. New BUYs still require `fair_yes - bid >= 3¢`; force-flat remains **peak−6h**.

---

## 0. TL;DR — what you're inheriting and what to do

`polyweather` runs **two** LIVE real-money Polymarket bots on a 24/7 Mac mini:
1. **Directional daemon** — bets the daily-high-temperature buckets near each city's forecast peak (the main strategy; mostly out of scope for you).
2. **Reward farmer** — `src/polyweather/rewards/` — rests passive maker bids on temperature reward buckets to earn Polymarket **liquidity rewards** (paid for RESTING in-band, not for filling). **This is your focus.**

The Farmer is LIVE under `com.polyweather.rewards`; the hourly adviser was loaded and verified on 2026-06-18
22:52Z. The first batch returned 35 valid records for 36 events (33 participate, 2 skip); subsequent 5-minute
cycles reused the cache. Existing resting/filled state is refreshed with current advice so a fill cannot snapshot
an obsolete fair value.

**The audit's verdict (your main job): the design has ONE architectural root cause — a shared funder wallet — and patching it locally does not converge.** The user chose to **accept + monitor** for now (option 3). Your 2 days, roughly in priority order:
1. **The root fix** — research + implement a separate funder wallet for the farmer ("option 1", §4.1), and/or harden the shared-funder accounting ("option 2", §4.2). This is the main thing.
2. **Genuinely re-evaluate the FORCE-taker** (§4.3) — it caused most of the audit's critical findings; a simpler all-maker design may be strictly better. Quantify before deciding.
3. **Monitor the hourly LLM adviser** (§4.5) — implemented and live. Preserve the boundary: cached selection
   calibration only, never direct order-state-machine control.

**Hard constraints (do NOT violate — §6):** never expose/edit secrets, never start a second Farmer, and keep
LLM output outside executor/state-machine control. Validate before reloading; record every deployed behavior
change in this handoff.

---

## 1. How the reward farmer works

Reward mechanics (verified against Polymarket docs): **reward = time an order RESTS in-band, sampled per minute over a 7-day epoch. Fills are never referenced — a filled position earns 0.** Band = `mid ± rewardsMaxSpread` (temp = 4.5¢). Score is quadratic: ~60% at 1¢ from mid, ~31% at 2¢, ~0 at the 4.5¢ edge. Pool per bucket ~$60–200/day; your share ≈ `size/(competition+size)`. So the ideal is to **rest unfilled, in-band, on low-competition buckets** — "sit dry."

Every 5 min (`farmer.run_cycle`):
1. **scan** (`rewards/scan.py`) — TOMORROW+ reward candidates plus exact FILLED tokens from today's event for
   valuation/maker sells. Adds current forecast peak/regime, deterministic calibration, `fair_yes` and lean.
2. **hourly advice** (`rewards/advice.py`) — at most once per 60 minutes, one structured OpenAI batch covers
   every station/date event. It sees current forecast/regime, 7/14/30d residuals, matched-regime statistics,
   variance and reward ladder. Cache: `data/reward_llm_advice.json`; audit: `.jsonl`. Usable advice recalculates
   adjusted peak, lean, fair and score. Missing/stale/mismatched advice preserves deterministic output.
3. **select** (`farmer.select`) — keep healthy (de-vig ~1, sane spread), in-band (mid 0.10–0.84), LLM-participation
   allowed when advice applies, forecast-aligned (`our_yes_lean`≥0.3), and require **positive calibrated edge**:
   `fair_yes - passive_bid >= 3¢`. Per-event TOP-2 still applies; final ranking is
   `(reward×lean÷competition) × model_edge` under the capital cap.
4. **plan_cycle** (PURE function — the brain) — from fresh targets + holdings + live books + prior state, decide place/cancel/sell. Rest a PASSIVE maker bid at `min(best_bid, mid−2¢)` (NEVER at the mid → instant fill).
5. On the first **FILL**, cancel+confirm the remaining BUY and freeze the order-level matched size before any SELL (prevents late partials racing the sell). Then Stage-2 maker `TAKE_PROFIT`: target 50% of entry model edge, bounded to 2–8¢ and capped at fresh `fair_yes−2¢`. Dropping out of top-N affects new BUYs only and **never invalidates a holding**. A defensive maker `FLATTEN` starts only when fresh `fair_yes < cost−3¢`; it is sticky. At `peak_utc−6h`, force-flat with the one allowed taker FAK at the bid. CLOSE when flat.
6. **_execute** — apply the plan via the executor (the I/O glue). Safe-cancel discipline: cancel→confirm→replace.

**Why TOMORROW+ only for BUYs:** keeps new Farmer exposure separate from the directional bot. Today's exact
FILLED tokens remain in scan context so fair/maker SELL management continues after local midnight.

---

## 2. Key files & how to run

| File | Role |
|------|------|
| `src/polyweather/rewards/farmer.py` | The farmer: `select`, `plan_cycle` (pure brain), `_execute`, `_place_sells`, `run_cycle`, `_snapshot_filled`, `_sell_decision`. |
| `src/polyweather/rewards/scan.py` | Scanner: `scan()` → ranked target dicts (+ `peak_utc`, `fair_yes`). |
| `src/polyweather/rewards/advice.py` | Hourly structured LLM batch, validation, atomic cache and application to fair/lean/score. |
| `src/polyweather/realized/residuals.py` | Fair-value model: `fair_calibration`, `recent_bias`, `bucket_fair_yes`. Reads `data/forecast_residuals.jsonl`. |
| `src/polyweather/clob_executor.py` | Executor: `place_from_decision` (maker GTC/GTD + taker FAK), `cancel_order` (returns `gone`), `book_bid_ask`, `filled_size` (order-level `size_matched`), `positions_held`, `_live_client` (cached), `_tick_size` (cached). |
| `run/run_rewards.py` | CLI: `--scan-once`, `--farm-dry`, `--farm-loop --cap N --max M --interval 5`, `--cancel-all`. |
| `run/deploy/com.polyweather.rewards.plist` + `run_rewards.sh` | 24/7 launchd agent (the USER loads/unloads it). Logs → `logs/rewards.out.log`. |
| `data/reward_state.json` | LIVE farmer state (per-token entries). |
| `data/reward_llm_advice.json` / `.jsonl` | Current atomic advice cache / append-only successful-batch audit. |
| `tests/test_rewards_advice.py`, `test_rewards_farmer.py`, `test_clob_executor.py`, `test_residuals.py`, `test_rewards_scan.py` | The tests. |

**Commands you may run (all read-only / DRY):**
```
uv run pytest -q                          # full suite (must stay green)
uvx ruff check src/ tests/ run/           # lint (must stay clean)
POLY_DRY_RUN=1 uv run python run/run_rewards.py --scan-once --top 10
POLY_DRY_RUN=1 uv run python run/run_rewards.py --farm-dry --cap 100 --max 10
```
`plan_cycle` is a PURE function — unit-test new logic against it directly (see the existing tests for the `_filled(...)`, `_order(...)`, `_held(...)`, `_t(...)` helpers). Prefer pure-function tests over live calls.

**Latest verification (2026-06-19):** 357 tests green; ruff, compileall, shell syntax and dependency checks
clean. Launchd Farmer is running; first real hourly advice batch succeeded; no new stderr was written after
reload. Current resting entries carry their applied `llm_advice` and adjustment.

**Gotchas:** code edits need a process restart (no hot reload). Use `launchctl kickstart -k` on the existing
single job only after tests pass; never `load` a duplicate. All cities resolve integer °C.

---

## 3. ★ THE ROOT ISSUE — shared funder (read this carefully) ★

The farmer and the directional daemon **share one wallet** (`POLY_FUNDER` in `.env`). `clob_executor.positions_held()` is keyed by that one funder and returns **combined** open positions. So `cur` (= our holding of a token, per the positions API) **cannot distinguish the farmer's shares from the daemon's co-held shares of the same token.**

Every sell-side sizing/close decision keys off `cur`, and **almost every bug the audit found traces to this single ambiguity:**
- a directional **co-hold inflates `cur`** → the farmer oversells / fires a crossing taker for shares it doesn't own (naked short of the daemon's inventory);
- a directional **co-hold shrink** drops `cur` → the farmer **false-CLOSEs** a position it still holds → orphan;
- a co-hold within `[0, size]` can even **fabricate a "fill"** of a bid that never filled (`_snapshot_filled` keys off `cur`).

All the bookkeeping you'll see — `held_at_place` (baseline), `held_at_fill` (private ceiling), `own_max`, the re-grow guard, the `filled_size` decrement, the FORCE-mode exclusions — is a **workaround for this one ambiguity.** That's why 4 rounds of patching each found ~5 new variants: **we were treating symptoms.**

**The co-hold window IS narrow by design** (the farmer trades tomorrow+, the daemon trades same-day, and the user-confirmed 6h force-flat is well before the daemon's `[peak−1h, peak]` window). It's a **date-rollover edge**, not an every-cycle hazard. That's why the user judged it OK to run as-is for now. But it is real, and it is the thing to fix properly.

---

## 4. ★ YOUR WORK — the deferred root fix + research ★

The user picked **option 3 (accept + monitor)** this round. Your 2 days are to do the work that was deferred. Investigate all three; bring a recommendation.

### 4.1 Option 1 — separate funder wallet (the clean root fix, RECOMMENDED by the audit)
Give the farmer its **own** wallet (a distinct `POLY_FUNDER`), so `positions_held()` returns ONLY farmer shares. Then `cur` is exact, `held_at_place ≡ 0`, and you can **delete** the entire fragile co-hold layer (`held_at_fill`/`own_max`/re-grow guard/`filled_size` decrement/FORCE-mode exclusions) — the sell logic collapses to "sell up to `cur`."
- **Code:** the executor already reads `POLY_FUNDER` from env. Add an optional `POLY_REWARDS_FUNDER` that the farmer's path (and only the farmer's `positions_held`) uses, falling back to `POLY_FUNDER`. Then simplify `plan_cycle`/`_place_sells`/`_snapshot_filled`.
- **Research first (the user must answer the wallet question — you can't):** can the user run a second Polymarket proxy/funder wallet? How is the private key / funder configured in `.env` (you can't read it, but document the *shape* of what's needed from the code in `clob_executor._live_client`)? Does it need its own USDC? Write up the exact operational steps for the user.
- **Deliverable:** a branch/PR that wires `POLY_REWARDS_FUNDER` + deletes the co-hold logic **behind a flag** (so it's a no-op until the user creates the wallet), with tests showing the simplified path. Plus a short doc of the operational steps.

### 4.2 Option 2 — harden the shared-funder accounting (if a 2nd wallet is infeasible)
Make **order-level `filled_size`** (co-hold-independent — reads `get_order().size_matched`) the AUTHORITATIVE signal instead of `cur`:
- the **BUY→FILLED flip** (`farmer.py` RESTING management, the `cur > held_at_place` check) should require `filled_size(order_id) > 0` and seed `held_at_fill` from the order's matched size, not `cur`;
- the **CLOSE** detection should confirm via `filled_size(sell_order_id)`, not infer from `cur` dropping;
- the **`filled_size()==None` fallback** (a live `get_order` failure) currently degrades to the co-hold-inflated `cur` clamp → conservatively assume the maker sold its full posted `sell_size` instead (mirror the FAK assume-full decrement already in `_place_sells`).
- **Caveat the audit flagged:** `get_order` returns 404/GONE for a filled-then-off-book maker, so `filled_size` can't gate the fill-confirm in that case (which is exactly why the design used `positions_held` as fill-truth). Think this through — it may be unsolvable cleanly without the separate wallet, which is why option 1 is preferred.

### 4.3 Research — is the FORCE taker even worth it?
The 关门时间 taker force-flatten is the source of most state-machine complexity. The user has now explicitly
reconfirmed that force-flat starts **6h before the peak**; do not silently restore 120 minutes. Research may still
compare alternatives, but changing this requires another explicit user decision:
- **Drop the taker entirely** (pure all-maker, user's original instinct): a stuck flatten just rides to settlement. How bad is that, really? Quantify from `data/reward_orders.jsonl` + `logs/` how often a flatten actually gets stuck.
- **Flatten earlier / more aggressively as a maker** (start the maker flatten further before peak, ratchet harder) so the taker is rarely needed.
- **Quantify the actual co-hold frequency:** grep the live logs / positions history — how often does the directional bot ACTUALLY co-hold a tomorrow+ token at 关门时间? If ~never, the whole co-hold risk is moot and option 3 is permanently fine.

### 4.4 Other research the user would value
- **Positive-edge validation:** the fair-value model (`residuals.bucket_fair_yes`) uses per-station residual σ/bias. Validate it against held-out residuals; is the new 3¢ `MIN_MODEL_EDGE` enough after model error? (Calibration fact: for `==N` buckets, P(round(actual)==N) is only ~24% even when the forecast peak rounds to N.)
- **Reward yield:** are we farming the best buckets? Competition vs pool analysis from the scan logs. Is `--cap 100 --max 10` optimal?

### 4.5 Hourly LLM calibration adviser — COMPLETED / LIVE

Implemented in `src/polyweather/rewards/advice.py`. One batched call per hour returns one record per
station/date: `adjustment_C = expected_actual_max - Open-Meteo_peak`, `participate`, `confidence`, and
`reason`. The model is `POLY_FARM_AGENT_MODEL`, falling back to `POLY_AGENT_MODEL`, then
`gpt-5.4-mini`.

The model sees current peak/regime/met features, deterministic adjustment/sigma, 7/14/30-day residual
statistics, matched and all-regime samples, and the reward bucket ladder. `apply_advice()` recomputes adjusted
peak, lean, fair and reward score; `participate=false` gates new BUYs only. Existing holdings still receive
the updated fair for maker SELL/thesis management.

Safety boundaries: no key or failed/malformed response uses deterministic calibration; a failed attempt is
throttled for an hour; successful advice is rejected after 3h, regime mismatch, or >1.5°C forecast drift.
Adjustment is schema- and runtime-bounded to ±5°C and participation requires confidence ≥0.5. The LLM never
chooses bid price, size, order type, cancel, sell, or FORCE behavior.

The first LIVE batch at 2026-06-18 22:52Z produced 35/36 valid records. Two events were marked no-participation.
Seoul 2026-06-20 received +4.8°C at 0.97 confidence, consistent with its large recent underforecast residual.
Tests are in `tests/test_rewards_advice.py`; resting-fill synchronization is covered in
`tests/test_rewards_farmer.py`.

---

## 5. Accepted residuals (round-4 findings, NOT fixed — know they're here)

These are real but narrow (need the co-hold race). The user accepted them under option 3. If you do option 1, they vanish; if option 2, fix them explicitly. Locations are approximate (functions, not line numbers — code drifts).

| Invariant | Where | What |
|-----------|-------|------|
| no-naked-short | `_execute` SELL-cancel `filled_size` fallback + `_place_sells` clamp | Partial maker fill + `filled_size()==None` on a LIVE `get_order` failure → the stale `cur`-based clamp oversells under a co-hold. |
| no-orphan | `plan_cycle` CLOSE cond. 3 (`cur <= held_at_fill - posted`) | A co-hold **shrink** with a partial-sized resting maker false-CLOSEs a still-held position → orphan. |
| no-naked-short | `plan_cycle` BUY→FILLED flip (`cur > held_at_place`) + `_snapshot_filled` | A co-hold appearing after the bid rests **fabricates a fill** → Stage-2 sells shares we don't own. |
| no-naked-short | `_execute` gone-cancel of a fully-filled maker + `filled_size None` | Re-grows `held_at_fill` off co-hold-inflated `cur` → re-opens a short. |

What was FIXED across the sessions (so you don't redo it): cancel-`gone` classification + resell/replace only on a clean cancel; first partial BUY fill now cancels/confirms its remainder before any SELL and freezes order-level matched size; FORCE taker decrements its ceiling (fire-once → CLOSE); FORCE never fires on an id-less ambiguous maker (`sell_pending` blocks it); ambiguous BUY placeholder is held while a target but reaped when its bucket drops; one-sided/empty book handled; `last_mid` refresh; `filled_size` decrement on maker cancel; positive-edge gate; held valuation separated from top-N; fair-aware take-profit; cap counts FILLED; tick cache.

**Reward-minimum caveat:** new state records `reward_min_size`. Normal `TAKE_PROFIT` maker asks below that size are
held rather than falsely treated as reward-eligible. Defensive `FLATTEN` and the explicit FORCE safety path may
still exit below reward minimum; there is no safe way to make a partial holding reward-eligible without buying
more or overselling.

---

## 6. ★ HARD CONSTRAINTS (security — do not violate) ★

1. **NEVER edit or expose `.env`.** It holds private keys and API credentials. A diagnostic subprocess may
   print only non-secret presence/derived status, never values.
2. **NEVER invoke ad-hoc real order-placement commands.** The existing launchd Farmer is LIVE; tests and
   one-shot diagnostics stay DRY. Deployment means restarting the single existing job after verification.
3. **NEVER `launchctl load` a duplicate Farmer.** Use `kickstart -k` only when deployment/reload is intended;
   two processes would fight over `reward_state.json`.
4. **NEVER paste/commit secrets.** Anything that touches the chat is considered exposed.
5. Keep the farmer a **separate process/state/plist** from the directional daemon — a farmer bug must never touch the main bot.
6. All temperature buckets resolve **integer °C** (zero Fahrenheit) — don't reintroduce °F assumptions.
7. Before changing live behavior: full pytest, ruff, compile/syntax checks, then restart the one existing job
   and verify PID, first cycle, cache/state, and stderr. Synchronize this handoff in the same turn.

---

## 7. Working discipline (this is a real-money system)

The audit found that **local patches to this state machine keep introducing new bugs** — two of my own fixes regressed (a critical double-sell, and the cap leak). So:
- Treat `plan_cycle` as a pure state machine and **trace concrete adversarial scenarios** (co-hold inflate/shrink, partial fill, ambiguous order, fill-during-cancel race) for any change — write them as tests.
- The once-per-cycle `positions_held` snapshot is **stale within the cycle** (a fill can land between the snapshot and a cancel) — many bugs live in that race.
- If you have multi-agent / parallel review tooling, **adversarially verify every change** before trusting it. If not, be paranoid manually.
- Prefer **deleting** the fragile co-hold logic (option 1) over adding more guards. Less code on a shared funder ≠ safer; a separate funder + simple code IS safer.

## 8. Pointers
- Reward mechanics + calibration evidence: see the docstrings in `rewards/scan.py`, `realized/residuals.py`, and `clob_executor.py` (they're written to explain the "why").
- Polymarket liquidity-rewards docs: https://docs.polymarket.com/developers/market-makers/liquidity-rewards
- The directional bot's fill test that the co-hold could mis-attribute: `run/run_daemon.py` (search `sz`). Worth understanding for §4.1.
- Calibration: `data/forecast_residuals.jsonl` (per-station forecast−actual residuals; drives both the EV gate and the directional bot).

## 9. Git backup status

The workspace was initialized as a Git repository on `main` on 2026-06-19. The baseline snapshot includes
source, tests, deployment scripts, configuration, docs and archived code. `.env`, `data/`, `logs/`, virtual
environments, local tool settings/caches, `.DS_Store` and `*.bak` are ignored. It is backed up to the PRIVATE
repository `https://github.com/zhufeiliu825-droid/polyweather`; local `main` tracks `origin/main`.

**Keep this `HANDOFF.md` synchronized after every behavior, deployment, configuration-contract, or unresolved-risk change.**
