I have all the data I need. Producing the report now.

---

# City/Season Selection + "Solar Peak" Calibration Report

**Scope:** 20 candidate METAR stations, full-year (366-day) profiles. Traded quantity = integer daily MAX temperature (°C). Edge = forecasting it; Strike fired ~1 h before the Solar Peak (solar noon + thermal lag). All `model_sigma` values are the ERA5-vs-station daily-max std (a *predictability proxy*, not a true forecast-error estimate — see §5).

---

## 1. Predictability tiers

Scoring inputs: `annual_model_sigma` (ERA5↔station daily-max std), `annual_max_std` (climatological spread), and the **summer** (warmest-season) `model_sigma` + `precip_day_pct`, since summer is where convection wrecks the clean solar curve. A low climatological std with high model_sigma still means a hard *relative* bet (Singapore-type efficiency risk).

| Tier | City | annual_mod_σ | annual_max_std | summer mod_σ | summer precip% | Note |
|---|---|---|---|---|---|---|
| **MILD/STABLE** | **Paris** (LFPB) | 0.80 | 7.55 | 0.93 | 50 | Cleanest in the set. Oceanic Cfb, σ stays <1 K most of year. |
| | **Wellington** (NZWN) | 0.84 | 3.36 | 0.89 | 59 | Maritime, tiny climatic spread (oceanic-damped, per climate-variability: maritime ≈2–3 K day-to-day). But see lag warning §3. |
| | **London** (EGLC) | 0.93 | 6.64 | 1.04 | 46 | Our home/verified station; σ≈0.5–1.0 outside spring. |
| | **Singapore** (WSSS) | 0.94 | **1.37** | 0.87 | 87 | Tropical low-variance (Roth & Chow 2022: daily std ~1 K). Predictable *level* but **efficiency risk**: climatology alone is near-optimal, so edge over the market is thin and a 1 K miss ≈ 1 std. |
| | **Madrid** (LEMD) | 0.95 | 9.90 | 1.03 | 13 | Dry continental summer (6–19% wet days) → big DTR (16 °C), radiation-driven, σ collapses to 0.7 in JJA. Strong dry-season edge. |
| **MODERATE** | **Shanghai** (ZSPD) | 0.98 | 8.89 | 1.07 | 72 | Clean shoulder seasons; summer Meiyu/typhoon noise. |
| | **Warsaw** (EPWA) | 1.03 | 10.07 | 1.09 | 60 | Continental, large climatic swing; σ moderate. Winter cold-pool risk (mos-bias: stable regimes 2–5 K). |
| | **Tokyo** (RJTT) | 1.13 | 8.34 | 1.24 | 46 | Maritime-tempered; Baiu (Jun) + typhoons add noise. |
| | **Sao Paulo** (SBGR) | 1.21 | 4.75 | 0.96 (DJF=summer) | 33→ wet summer 90+ | Subtropical highland; summer (DJF) wet-season convective. |
| | **Ankara** (LTAC) | 1.22 | 9.74 | 1.02 | **10** | Semi-arid plateau; superb dry summer (σ 0.63–0.69 Jul/Aug). Winter cold-pool risk (Jan σ 1.75). |
| | **Lucknow** (VILK) | 1.23 | 6.26 | 1.19 | 87 | Sharp monsoon switch; excellent pre-monsoon (MAM σ 1.01, dry), poor JJA. |
| | **Shenzhen** (ZGSZ) | 1.28 | 4.59 | 0.89 | **95** | *Unverified* (§4). Low climatic spread but negative-lag, near-100% summer wet. |
| | **Seoul** (RKSI) | 1.33 | 10.48 | 1.33 | 58 | Big climatic swing; Changma (Jun–Jul) convective. |
| **VOLATILE (hard)** | **Munich** (EDDM) | 1.44 | 9.14 | **1.67** | 58 | Alpine-foreland convection; Jun σ **2.78**. Hardest European site. |
| | **Wuhan** (ZHHH) | 1.46 | 9.37 | 1.34 | 62 | Yangtze basin; Sep σ 1.87. |
| | **Beijing** (ZBAA) | 1.45 | 11.04 | **1.62** | 58 | Largest climatic spread in set; Jun σ **2.33** (pre-monsoon dry convection + dust/heat). |
| | **Guangzhou** (ZGGG) | 1.55 | 5.82 | 1.62 | **93** | South-China monsoon; summer σ 1.4–1.9, ~90–100% wet. |
| | **Chongqing** (ZUCK) | 1.61 | 9.05 | 1.54 | 57 | Sichuan basin, fog/cloud-trapping; broadly high σ all year. |
| | **Chengdu** (ZUUU) | 1.61 | 8.12 | **1.98** | 73 | Sichuan basin; Jun σ **2.46**. Persistently hard. |
| | **Taipei** (RCSS) | **2.75** | 7.13 | 1.41 | 79 | **Worst in the set.** σ explodes in cool season (Oct 3.61, Jan 3.60, DJF mean 2.96) — NE-monsoon drizzle/advection regime, not solar-driven. Avoid except mid-summer. |

**Tropical low-variance note (Singapore-type):** Singapore is the textbook case — `annual_max_std` 1.37 K, daily-max essentially 30–32 °C year-round (climate-variability: tropics, diurnal range > seasonal range; predictability intrinsically high). The catch is the *flip side*: with such a narrow distribution, climatology is already a strong forecast, so there is little exploitable spread and the market is likely efficient. Shenzhen (4.59) and Sao Paulo (4.75) are milder versions of the same profile.

---

## 2. Seasonal betting pool (most-tradeable vs PAUSE)

Hemisphere note: Wellington and Sao Paulo are Southern-Hemisphere — their "summer" is DJF, "winter" JJA. Every other city is NH. Driving principle (monsoon-convection research): the forecastable edge lives in the **dry / pre-monsoon** months (radiation-driven, smooth solar curve, even seasonal skill up to ~2 months out — Saha et al. 2024); it collapses in the **active wet/monsoon** months (Tmax becomes convection-gated and stochastic, AUC≈0.5 — arXiv 2401.03746).

**Northern Hemisphere, by season:**

- **DJF (winter):**
  - TRADE: Madrid, Ankara, Lucknow (all dry-season, σ ≤1.3), Guangzhou & Shenzhen (S-China dry season — Guangzhou DJF precip 30%, σ 1.12), London/Paris/Shanghai (oceanic, σ 0.6–0.7).
  - PAUSE/caution: continental cold-pool sites where stable anticyclonic inversions blow up error — Warsaw, Munich, Beijing winter highs are sensitive to snow/inversion (mos-bias: stable/snow regimes 2–5 K warm bias, surface-inversion errors ~5 K). **Taipei: hard PAUSE** (DJF σ 2.96, NE-monsoon).
- **MAM (spring):** Broadly the best NH window. TRADE: **Lucknow (pre-monsoon, σ 1.01, dry, the model's sweet spot)**, Madrid (σ 0.85), Paris/Warsaw/London. Watch Beijing (σ 1.33, dust/dryline) and Chengdu/Chongqing (basin, σ 1.2–1.6).
- **JJA (summer):**
  - TRADE: **Madrid (σ 1.03, only 13% wet — radiation-dominated, lag well-behaved)**, **Ankara (σ ~0.65, 10% wet)** — these two are the standout summer bets. London/Paris acceptable (σ ~1.0).
  - **PAUSE (monsoon/convective): Guangzhou & Shenzhen — pause JJA** (S-China monsoon, ~93–100% wet days, max shifts before solar noon, see §3); trade their DJF/MAM instead. Also pause **Chengdu (σ 1.98), Munich (σ 1.67/Jun 2.78), Beijing (Jun σ 2.33), Chongqing, Wuhan, Lucknow (JJA 87% wet)**, and Seoul/Shanghai during Jun–Jul Changma/Meiyu.
- **SON (autumn):** Good for the European/oceanic group (London 0.65, Paris 0.72, Madrid 0.70). **PAUSE Taipei (SON σ 2.74 — NE-monsoon onset)** and Wuhan (Sep σ 1.87).

**Southern Hemisphere:**
- **Wellington:** most tradeable in its **summer/shoulder (DJF–MAM local, σ 0.67–0.79)**; the climatic spread is tiny so absolute error is low, but the persistent negative lag (§3) means the *timing* model, not the level, is the live risk. Cool season (JJA local) σ 0.89.
- **Sao Paulo:** TRADE the **dry winter (JJA local, σ 0.96–1.12, though paradoxically 90%+ wet-day flag in Jun–Aug here — treat the wet flag with care)** and SON; the wet summer (DJF) is convective (σ 1.0–1.36). Best clean window is the cooler, drier April–Aug shoulder.

---

## 3. Per-city Solar Peak (thermal-lag) calibration

Baseline expectation (thermal-lag research, Seidel et al. 2005; Soneye et al. 2019): clear-sky 2-m max lags solar noon by **~+1 to +3 h** (peak ~15:00 LST), larger/later in summer, smaller/earlier in winter. Surface energy balance — not solar timing — sets the peak; cloud/convection/advection can reverse the net flux *before* noon and produce a **negative lag** (late-morning max). Strike = Solar-Peak − 1 h, so a wrong lag directly mis-times the bet.

| City | Annual lag pattern | Summer lag | Seasonal shift | Flag |
|---|---|---|---|---|
| **Madrid** | **+1.7 to +2.4** (textbook) | Jul **+3.04**, Aug +2.26 | Strong summer-late shift | Cleanest +lag; matches dry-continental theory. |
| **Ankara** | **+0.6 to +2.6** | Jul **+2.58**, Aug +2.46 | Large summer shift | Clean +lag, dry plateau. |
| **Beijing** | **+0.4 to +1.9**, all-positive | Jul +1.89 | Later in summer | Reliable +lag (continental). |
| **Munich** | +1.3 to +2.0 summer | Aug +2.04 | Winter dips negative (Dec −2.16) | Mostly +; winter cold-pool flips it. |
| **Lucknow** | **+1.0 to +2.1**, all-positive | JJA +1.0 (suppressed by monsoon cloud) | Pre-monsoon highest (Apr +2.14) | +lag but monsoon *compresses* it. |
| **Sao Paulo** | +0.6 to +1.7, all-positive | DJF +1.1–1.4 | Stable | Clean +lag year-round. |
| **Paris** | +0.7 to +1.6 summer | Jul +1.15 | Autumn/winter → ~0/neg | Standard oceanic. |
| **London** | Summer **+1.2 to +1.7**; winter NEG | JJA +1.46 | Flips negative Oct–Feb (Jan −1.23, Feb −1.27) | Use +1.5 h summer, ~0 to −1 h winter. |
| **Warsaw** | Summer +1.1 to +2.0; **winter strongly NEG** (Jan −2.87, Dec −2.67) | JJA +1.59 | Huge seasonal swing | Winter low-sun + frontal → late-morning max. |
| **Tokyo** | Near-zero to +0.9; **autumn NEG** (Oct −1.23) | JJA ~+0.2 | Maritime damped | Small lag; Strike tighter. |
| **Beijing/Wuhan/Chengdu/Chongqing** | Mixed; **summer +, cool-season NEG** (Chongqing Jan −2.46, Dec −2.08) | JJA +0.4–1.0 | Basin sites flip neg in winter fog | Negative-lag winter caution. |
| **Shanghai** | Mostly NEG except mid-summer (Oct −1.52, Jan −1.48) | JJA ~+0.1 | Cool-season negative | Coastal/advective. |
| **Seoul** | Mostly **NEG** (Dec −2.19, Oct/Nov ~−1.1); summer ~0 | JJA −0.05 | Negative-dominant | Coastal + monsoon; max often pre-noon. |

### NEGATIVE-lag cities — the "solar noon + lag" model BREAKS here

These run their daily max *before solar noon*, where the Strike-minus-1h-of-Solar-Peak logic is invalid and must be re-derived (the max is set by convective cloud-cap / NE-monsoon advection / sea-breeze, per monsoon-convection + thermal-lag research). Listed by severity:

- **Taipei (RCSS):** annual lag pattern dominated by large negatives — **Dec −3.64, Nov −2.34, Jan −2.37, Oct −2.99**, DJF mean −2.48, SON −1.76. The NE monsoon (drizzle/cloud, advective cool-season regime) pushes the max into the morning and σ to ~3 K. The solar model is essentially non-applicable Oct–Feb. **Re-think Strike timing or pause.**
- **Wellington (NZWN):** persistently negative **all year** (Jun −3.02, JJA −1.90, even DJF −2.44). Maritime/sea-breeze + frontal — the "hottest" reading often comes mid-morning before the marine layer/breeze caps it. Low σ makes the *level* easy but the *timing* is anti-solar; do not anchor Strike to solar noon + positive lag.
- **Shenzhen (ZGSZ):** negative across the warm season — **Apr −1.45, May −1.07, Jun −1.10, Aug −1.25** (MAM −1.05, JJA −1.09). Classic monsoon convective cap: morning heating, afternoon storm truncates the rise → late-morning max (monsoon-convection: rain-cooled, max earlier & lower). See §4.
- **Guangzhou (ZGGG):** winter dips negative (Jan −0.44) though summer stays + (cloud builds late enough that the radiative peak survives most days); transitional.
- **Seoul / Shanghai / cool-season basin & continental sites:** negative in autumn/winter (low sun + frontal advection). For these, treat the lag as **regime-dependent**: positive on clear summer days, ~0 to strongly negative on cloudy/frontal/monsoon days — gate the Strike on the morning-diagnosed regime (mos-bias/nwp-skill: the morning ob's main value is regime ID, not direct persistence).

---

## 4. Shenzhen note (verification failure: 22% match)

Shenzhen (ZGSZ) is the **only `verified: false`** station; earlier resolution-mapping matched our computed daily max on only ~22% of resolved days. Given its profile, **the failure is plausibly data/regime-driven, not purely a code bug — but the regime makes it a poor candidate regardless:**

- **Negative lag across the warm season** (MAM −1.05, JJA −1.09; specific months −1.10 to −1.45). When the true max occurs in the late morning before convection, a pipeline that searches an afternoon "solar-peak" window or mis-handles the METAR timestamp/day-boundary will systematically pick the wrong hour — exactly the kind of error that produces a low match rate.
- **~95% summer wet days** (Jun 100%, Jul 94%, Aug 90%) means almost every summer day is a convective-cap day: rain-cooled, suppressed, early max (monsoon-convection: convective rain cooling ~5–8 °C, max pulled to late morning). The "true" resolved max and any solar-window computation will frequently disagree, and even small timestamp/UTC-offset bugs get amplified.
- **Low climatic spread (4.59 K)** compresses everything into a narrow band, so a systematic 1–2 °C timing/rounding offset flips the *integer* resolution bucket on a large fraction of days — turning a small error into a large match-rate failure.
- The other South-China hourly-METAR sites (Guangzhou) verified fine, so the issue is likely Shenzhen-specific data (station reporting cadence / resolution feed / day-boundary handling) compounded by the negative-lag, near-saturated-precip regime. **Recommendation:** do not trade Shenzhen until both (a) the resolution-mapping is re-debugged with a negative-lag-aware max window and (b) it re-verifies; even then, restrict to DJF (dry, σ 1.1, lag ≈0).

---

## 5. Methodology caveats

1. **`model_sigma` is ERA5-vs-station — a LOWER BOUND on real forecast error, not the error itself.** ERA5 *assimilates the very surface obs* it is compared against, so reanalysis-vs-station RMSE underestimates genuine forward-forecast error, especially at short lead (mos-bias; arXiv 2305.00048; WeatherReal 2024 — ERA5 fits best exactly where station density is highest = assimilation fingerprint). ERA5 also under-represents the diurnal range at ~80% of stations, so it *smooths the daily max* — biasing our predictability proxy optimistic precisely on the quantity we trade. **Treat every `model_sigma` here as a floor; add genuine predictability error + the irreducible point-vs-grid representativeness penalty.**

2. **Real same-day NWP max-T error is larger than these σ suggest — calibrate expectations to ~1–2 K.** Operational day-1 2-m T: MAE ≈ 1.0–1.5 K post-processed, RMSE ≈ 1.3–2.0 K at a good lowland site; raw global models 1.7–2.4 K (nwp-skill; meteoblue; Bremnes et al. 2023). So a city showing `model_sigma ≈ 0.7` (e.g. summer Madrid/Ankara) will in practice carry ~1.3–1.5 K real same-day uncertainty — still our best bets, but not the sub-1 K the proxy implies.

3. **Tmax is the hard half of the diurnal cycle.** Error peaks at the afternoon max (18 UTC) vs the morning min, and models systematically **under-amplify the cycle — Tmax ~1–2 K too low in summer** (nwp-skill; ECMWF super-site). We trade the worse-forecast half; a small warm correction to model Tmax in summer is justified.

4. **Regime dominates, and it doubles the error.** In stable/anticyclonic clear-sky or snow conditions errors reach **2–5 K** (cloud-cover misses) (nwp-skill; mos-bias). This is the live risk for winter Warsaw/Munich/Beijing/Ankara cold pools, and for the cool-season negative-lag advective regimes (Taipei, Shanghai). Continental/basin/mountain > maritime; the basin trio (Chengdu/Chongqing) and Munich's Alpine foreland carry the largest summer σ for this reason.

5. **Monsoon months are near-zero-skill — do not bet, do not trust any point lag.** During active monsoon/convection, Tmax is gated by whether/when an afternoon storm fires (stochastic, AUC≈0.5; max often comes earlier and 5–8 °C lower than the clear-day curve) — monsoon-convection research. This is why §2 pauses Guangzhou/Shenzhen/Lucknow/Chengdu/Munich in their wet months and why the §3 negative-lag flags concentrate there.

6. **The morning ob's real value is regime diagnosis, not persistence.** RAFT-style nowcasting buys ~10–20% RMSE only for ~5 h and washes out before the afternoon max (Hess et al. 2019); but a clear vs cloudy/convective morning read tells us whether the positive-lag solar model or the negative-lag convective model applies — which is exactly where the 2–5 K regime errors live. **Gate the Strike timing on the morning-diagnosed regime, especially for all §3 negative-lag and mixed-sign cities.**

---

### One-line selection summary
- **Core tradeable pool (clean solar model):** Madrid, Ankara (dry summer), Paris, London, Lucknow (MAM pre-monsoon), Shanghai/Tokyo (shoulder seasons), Sao Paulo/Wellington (low-spread, but Wellington is negative-lag → timing-rethink).
- **Trade only in dry season, pause in wet:** Guangzhou, Shenzhen, Lucknow, Sao Paulo, Seoul, Shanghai.
- **Hard / avoid mostly:** Taipei (σ 2.75, negative-lag cool season), Chengdu/Chongqing (basins), Munich/Beijing (convective summers, cold-pool winters), Wuhan.
- **Don't trade until re-verified:** Shenzhen (22% match; failure is plausibly data + negative-lag + ~100%-wet-summer driven).

Source data: `/Users/wekoiuk/polyweather/data/city_profiles.json` (20 stations, 366-day profiles).