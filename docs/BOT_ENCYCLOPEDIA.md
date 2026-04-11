# WeatherEdge Bot v2 — Complete System Encyclopedia

**Date:** 2026-04-11
**Purpose:** Exhaustive reference document for external audit. Covers every module, agent, data source, threshold, and decision path in the bot.
**Audience:** ChatGPT audit session (or any reviewer with no prior context).

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Market Discovery & Parsing](#2-market-discovery--parsing)
3. [Forecast Engine](#3-forecast-engine)
4. [Nowcasting Layer](#4-nowcasting-layer)
5. [Weather Regime Classification](#5-weather-regime-classification)
6. [Station Edge & Observation Blending](#6-station-edge--observation-blending)
7. [Station Bias Tracking](#7-station-bias-tracking)
8. [Probability Recalibration & F-Strict Gate](#8-probability-recalibration--f-strict-gate)
9. [RUFLO Agent System (10 Agents)](#9-ruflo-agent-system-10-agents)
10. [Strategy Agent Modules](#10-strategy-agent-modules)
11. [Execution & Order Management](#11-execution--order-management)
12. [Trade Ledger & Telemetry](#12-trade-ledger--telemetry)
13. [Trade Resolution](#13-trade-resolution)
14. [Scheduling & Main Loop](#14-scheduling--main-loop)
15. [Configuration Reference](#15-configuration-reference)
16. [API Server & Dashboard](#16-api-server--dashboard)
17. [Data Sources & External APIs](#17-data-sources--external-apis)
18. [File Map](#18-file-map)
19. [Known Bugs & Failure Modes](#19-known-bugs--failure-modes)

---

## 1. System Overview

WeatherEdge Bot v2 is a Polymarket weather temperature prediction trading bot. It trades binary outcome markets (YES resolves to $1.00 or NO resolves to $1.00) on questions like "Will the high temperature in Atlanta exceed 75°F on April 5?" Resolution is determined by Weather Underground observations at specific ICAO airport weather stations.

The bot operates in paper trading mode by default (simulated fills, no real money). It can switch to live mode using Polymarket's CLOB (Central Limit Order Book) on Polygon (chain_id=137).

### High-Level Architecture

```
Polymarket Gamma API → Market Discovery (gamma_client.py)
                            ↓
                    Station Parsing (station_parser.py)
                            ↓
    ┌───────────────────────┼───────────────────────┐
    ↓                       ↓                       ↓
Open-Meteo Ensemble   Deterministic Models     METAR Observations
(82 members)          (7 models)               (aviationweather.gov)
    ↓                       ↓                       ↓
    └───────────────────────┼───────────────────────┘
                            ↓
                Multi-Model Forecast (multi_model_forecast.py)
                            ↓
            ┌───────────────┼───────────────────┐
            ↓               ↓                   ↓
        Nowcaster       Regime Classifier   Station Edge
        (≤24h)          (front/marine/      (obs+ensemble
                         convective/clear)   time-blend)
            ↓               ↓                   ↓
            └───────────────┼───────────────────┘
                            ↓
                    Bias Agent (4 knobs)
                            ↓
                    Strategy Gate (F-Strict / Shadow)
                            ↓
                    RUFLO 10-Agent System
                    (validate → score → size → route)
                            ↓
                    Execution (paper or live CLOB)
                            ↓
                    Trade Ledger + Telemetry
                            ↓
                    Trade Resolver (PnL computation)
```

### Wallet & Identity

- **Wallet:** `0xE2FB305bE360286808e5ffa2923B70d9014a37BE`
- **Chain:** Polygon (chain_id=137)
- **CLOB Host:** `https://clob.polymarket.com`
- **Gamma API:** `https://gamma-api.polymarket.com`
- **Data API:** `https://data-api.polymarket.com`

---

## 2. Market Discovery & Parsing

### 2.1 Gamma Client (`gamma_client.py`)

Discovers open Polymarket weather temperature markets.

**API:** `https://gamma-api.polymarket.com/events` with params `active=true, closed=false, limit=100, tag_slug=weather`.

**Cache:** 1800s (30 min) TTL. Stale-while-revalidate: if a fresh fetch returns 0 markets, keeps old cache and retries in 2 min.

**Filtering pipeline:**
1. Fetch paginated events (up to 10 pages of 100)
2. Flatten sub-markets from events
3. Filter to temperature markets via keyword matching: "temperature", "high temp", "low temp", "degrees", "fahrenheit", "celsius"
4. Require ICAO station in 58-city list
5. Require future resolution time
6. Deduplicate by slug

**Output:** `DiscoveredMarket` objects with slug, market_id, question, station (ICAO), city, country, category (high_temp/low_temp), confidence (0-3), timezone, lat, lon, coastal flag, resolution_time, prices, tokens.

### 2.2 Station Parser (`station_parser.py`)

Extracts ICAO codes and weather station info from Polymarket rules text.

**Confidence scoring:**
- 3.0: URL + ICAO both found in rules
- 2.0: Keyword match + city map lookup
- 1.0: Keyword only
- 0.5: City fallback
- 0.0: No station identified

**Minimum confidence to trade:** 2.0

**ICAO extraction regex:** Matches 4-letter codes starting with K, P, C, U, E, L, W, R, Z, V, T, M, S, O, D, H, F, N, Y.

**WU-METAR sanity check:** Compares last 30 days of Weather Underground vs METAR observations. If avg difference >1.2C, flags as high-risk with +0.04 EV gate boost.

### 2.3 Market Classifier (`market_classifier.py`)

Classifies markets into categories: exact_1bin, exact_2bin, above_below, between.

**Temperature regex patterns:**
- Exact: "be X°F" with no directional keywords
- Above: keywords "above", "exceed", "at least", "or higher"
- Below: keywords "below", "under", "cooler", "or lower"

### 2.4 Coverage: 58 Cities

**North America (18):** New York (KLGA), Los Angeles (KLAX), Chicago (KORD), Miami (KMIA), Dallas (KDAL), Denver (KBKF), Seattle (KSEA), Boston (KBOS), Phoenix (KPHX), Minneapolis (KMSP), Las Vegas (KLAS), San Francisco (KSFO), Atlanta (KATL), Houston (KHOU), Toronto (CYYZ), Vancouver (CYVR), Montreal (CYUL), Mexico City (MMMX)

**Europe (18):** London (EGLC), Dublin (EIDW), Paris (LFPG), Amsterdam (EHAM), Berlin (EDDB), Frankfurt (EDDF), Munich (EDDM), Madrid (LEMD), Barcelona (LEBL), Rome (LIRF), Milan (LIMC), Athens (LGAV), Lisbon (LPPT), Helsinki (EFHK), Stockholm (ESSA), Copenhagen (EKCH), Moscow (UUWW), Warsaw (EPWA)

**Middle East (3):** Dubai (OMDB), Istanbul (LTAC), Tel Aviv (LLBG)

**South Asia (4):** Mumbai (VABB), Delhi (VIDP), Bangalore (VOBL), Lucknow (VILK)

**Southeast Asia (5):** Singapore (WSSS), Bangkok (VTBS), Hong Kong (VHHH), Jakarta (WIHH), Kuala Lumpur (WMKK)

**East Asia (10):** Tokyo (RJTT), Seoul (RKSI), Busan (RKPK), Shanghai (ZSPD), Beijing (ZBAA), Chengdu (ZUUU), Chongqing (ZUCK), Shenzhen (ZGSZ), Wuhan (ZHHH), Taipei (RCSS)

**Oceania (4):** Sydney (YSSY), Melbourne (YMML), Auckland (NZAA), Wellington (NZWN)

**South America (4):** Sao Paulo (SBGR), Rio de Janeiro (SBGL), Buenos Aires (SAEZ), Santiago (SCEL)

**Africa (3):** Cairo (HECA), Johannesburg (FAOR), Lagos (DNMM)

---

## 3. Forecast Engine

### 3.1 Multi-Model Forecast (`src/multi_model_forecast.py`)

The core probability engine. Fetches weather forecasts from two sources and blends them.

**Source 1 — Ensemble API:**
`https://ensemble-api.open-meteo.com/v1/ensemble`
- 51 ECMWF IFS members + 31 GFS GEFS members = 82 total ensemble members
- These members are correlated (shared model physics), not independent draws

**Source 2 — Deterministic Multi-Model API:**
`https://api.open-meteo.com/v1/forecast`
- 7 models: GFS, ECMWF, ICON, JMA, GEM, Meteo-France, UKMO

**Cache:** 30 min TTL per city/day. Rate-limit backoff: 120s after HTTP 429.

**Blended Sigma Computation:**
- IQR-based sigma: `(p75 - p25) / 1.35` (robust to fat tails)
- Standard sigma: `std(all_members)`
- Final: `max(std, iqr_sigma, 2.5)` — the 2.5C floor was raised from 1.5C per STRATEGY_REWRITE

**Data quality tiers:**
- "good": n_ensemble >= 10 members
- "partial": 3 <= n_models < 10
- "fallback": else, blended_sigma forced to 2.5C

**Bin Probability Computation:**
1. Direct member counting: What fraction of 82 members fall in the bin?
2. KDE (Kernel Density Estimation): Gaussian kernel with Silverman bandwidth
3. Blending: `weight_direct = min(0.85, 0.5 + n/200)` — with 82 members, direct count gets ~75% weight
4. CALIBRATION_GUARD: If blended exact-bin probability > 30%, blend with conservative Gaussian using verified_sigma=2.5C
   - Conservative weight: `min(0.50, overconfidence * 0.50)` where overconfidence = `max(0, (blended - 0.30) / 0.70)`
5. **Hard cap: 35%** on any exact-bin probability (lowered from 45%)

**Model disagreement hedge:** When disagreement > 1.0 sigma, a hedge weight of `min(0.3, disagreement * 0.1)` is applied.

### 3.2 Ensemble Probabilities Shim (`ensemble_probs.py`)

Wrapper for markets >24h to resolution. When `ENABLE_LONG_HORIZON=False` (current setting), returns `{tradeable: False, source: "long_horizon_disabled"}` instead of the old 50/50 coin-flip stub.

---

## 4. Nowcasting Layer

### 4.1 Nowcaster Wrapper (`nowcaster.py`)

Short-horizon (<=24h) probability estimation anchored to live observations.

**Key fixes applied (STRATEGY_REWRITE):**
- Threshold-feedback bug removed: `ftemp = obs_temp_c * 0.5 + max_c * 0.5` (was threshold-dependent)
- Hard-coded EST replaced with per-city timezone via `zoneinfo`
- Static sigma replaced with per-station RMSE: `sigma = max(2.0, station_rmse_c)` (was fixed 1.5/2.5)

**OBS_KILL trigger:** If `max_achievable < threshold - 0.5C` for above/exact bins, returns 0.01/0.99 split (near-certain NO).

### 4.2 Full Nowcasting Engine (`src/nowcasting.py`)

Monte Carlo nowcasting with AR(1) residuals.

**Parameters:**
- Monte Carlo samples: 5000
- AR(1) coefficient (rho): 0.78 default, 0.70 coastal
- Half-life (observation influence decay): 2.0h near-peak, 4.0h default, 3.0h coastal
- Anomaly detection: flag if temp deviation > 6.0C or obs > 45 min stale
- Anomaly handling: reduce obs_weight to 0.5, widen sigma by 1.4x

**Process:**
1. Compute adjusted mean: `mu_adj = mu_h + offset * exp(-h / half_life)`
2. Generate AR(1) residuals: `e_h = rho * e_{h-1} + sqrt(1 - rho^2) * z_h`
3. Run 5000 Monte Carlo paths
4. Convert empirical distribution to bin probabilities
5. Apply observation sanity checks

---

## 5. Weather Regime Classification

### `src/regime_classifier.py`

Detects weather regimes and adjusts probability distributions accordingly.

**Five regimes:**

| Regime | Detection Criteria | Effect |
|--------|-------------------|--------|
| FRONT | ensemble_spread >4.0C AND wind_shift_prob >0.5 | sigma x1.5, skew -1.5C |
| CONVECTIVE | precip_prob >0.4 AND spread >3.0C AND cloud >0.5 | Mixture model: 32% chance of storm cap at obs_max+1.5C |
| MARINE | Coastal AND cloud >0.6 AND spread <2.5C | skew -1.2C, upper clamp at mu+2*sigma |
| CLEAR | cloud <0.2 AND precip <0.1 AND spread <2.0C | warm bias +0.8-1.5C, sigma x0.8 |
| NEUTRAL | Default fallback | No adjustment |

**Application order:** warm_bias → skew (via skew-normal) → sigma scaling → storm cap (convective) → upper clamp (marine).

---

## 6. Station Edge & Observation Blending

### `src/station_edge.py`

Combines METAR observations with ensemble forecasts using time-of-day blending.

**Observation weight by local hour (same-day high markets):**
- Before 10am: 30% obs / 70% ensemble
- 10am-1pm: 50% / 50%
- 1pm-3pm: 70% / 30%
- 3pm-4pm: 85% / 15%
- After 4pm: 95% / 5%

**Max achievable temperature estimate (heating rates per hour):**
- Before 8am: +2.0F/hr
- 8-11am: +1.8F/hr
- 11am-1pm: +1.5F/hr
- 1pm-3pm: +1.0F/hr
- 3pm-4pm: +0.5F/hr
- After 4pm: +0.0F/hr (peak assumed passed)

**Uncertainty bands by hour:** <9am: +/-8F, 9-11am: +/-5F, 11am-1pm: +/-3F, 1pm-3pm: +/-2F, 3pm-4pm: +/-1.5F, >4pm: +/-1F

**Confidence scoring (0 to 1.0):**
- Ensemble members >=70: +0.3; 40-70: +0.2; <40: +0.1
- Sigma <1.0: +0.2; 1.0-2.0: +0.1
- Same-day obs bonus: hour >=15 → +0.4, >=13 → +0.3, >=11 → +0.2
- Obs/ensemble agreement: +agreement*0.1

**Kelly sizing:** Quarter-Kelly with confidence: `kelly_raw * confidence * 0.25`, capped at 25% of bankroll.

**Trade decision thresholds:**
- Min edge: 8.0 percentage points
- Min confidence: 0.3
- NO trade requires: confidence >=0.5 AND edge >12pp
- Floor: $2; ceiling: 10% bankroll (YES), 5% (NO)

---

## 7. Station Bias Tracking

### 7.1 Bias Database (`station_bias.py`)

Tracks systematic temperature bias per station by comparing forecasts against actual resolutions.

**SQLite tables:**
- `bias_observations`: ts, station, city, date, forecast_temp_f, market_implied_f, resolved_temp_f, forecast_error_f, market_error_f, bin_question, bin_outcome, our_prob, market_price, meta
- `station_bias_summary`: station (PK), city, n_observations, mean/median forecast_error_f, std_forecast_error_f, warm_bias_pct, cold_bias_pct, recent_errors (last 20, JSON)

**Confidence levels:** n >= 30 (high, full correction), n >= 15 (medium, 70%), n >= 5 (low, 30%).

**Learning:** Auto-learns from resolved trades via `learn_from_resolution()`.

### 7.2 Bias Agent (`src/bias_agent.py`)

Reads station_bias.db and applies 4 knobs to each signal.

**Knob A — Temperature Correction:**
- Only corrects if |bias| > 0.5F
- Weight: 1.0 (n>=30), 0.7 (n 15-30), 0.3 (n 5-15)
- Converts F→C: `correction_c = -correction_f / 1.8`

**Knob B — Sigma Floor:**
- Normal: 1.0C base
- Noisy (std >4.0F): 2.0C
- Hard cap: 2.5C

**Knob C — EV Gate Addon (extra percentage points required):**
- Noisy station (std >4F): +4.0pp
- Low sample count (n <15): +2.0pp
- High outlier rate (>75% AND std >3F): +3.0pp
- Cap: +5.0pp total

**Knob D — Size Multiplier:**
- Excellent (n>=30, std <2.5F, |bias| <1F): 1.2x
- Good (n>=15, std <4F): 1.0x
- Mediocre (n>=5, high std/outliers): 0.7x
- Unknown (n<5): 0.5x
- Floor: 0.4x

**Drift detection:** If |30d_bias - 180d_bias| > 2.0F, applies 0.8x size penalty and +1.0pp EV addon.

**Poll interval:** 3600s (re-reads DB hourly).

---

## 8. Probability Recalibration & F-Strict Gate

### `src/strategy_gate.py`

Implements the STRATEGY_REWRITE.md plan. The core insight: the model's claimed probability is wildly miscalibrated above the 25% bucket.

### 8.1 Recalibration Map

Derived from 133 resolved predictive trades:

| Raw Probability | Recalibrated | Observed Win Rate | Notes |
|----------------|-------------|-------------------|-------|
| 0.00 - 0.10 | 0.10 | 11.1% | Slight edge |
| 0.10 - 0.20 | 0.20 | 22.2% | Reasonable |
| 0.20 - 0.30 | 0.27 | 27.6% | **Only calibrated bucket** |
| 0.30 - 0.40 | 0.18 | 9.1% | Model delusional |
| 0.40 - 0.60 | 0.15 | 12.5% | Model delusional |
| 0.60 - 0.80 | 0.12 | 10.0% | Model delusional |
| 0.80 - 1.00 | 0.30 | 36.0% | Mild trust |

### 8.2 F-Strict Gate

ALL conditions must pass:

| Gate | Requirement |
|------|------------|
| Entry price | 0.10 <= price <= 0.20 |
| Recalibrated prob | 0.22 <= recal_prob <= 0.40 |
| Lead time | 720 <= mins_to_resolution <= 1440 (12-24h) |
| Station RMSE | <= 1.8C (fails closed if unknown) |
| Bin type | exact_1bin, exact_2bin, or exact only |
| Per-trade cap | $10 |
| Per-city/day cap | $40 |
| Daily stop-loss | -$25 halts new entries |

### 8.3 Shadow Lane (ABOVE_BELOW)

Tiny-risk shadow trades on above/below markets:
- Raw prob: 0.20-0.35
- Price: 0.05-0.40
- Lead: >= 6h
- Size cap: $2/trade, $10/day budget
- Tracked separately from F-Strict PnL

---

## 9. RUFLO Agent System (10 Agents)

File: `ruflo_monitor.py`

A multi-agent system where each agent has a specialized role. All agents communicate via `RufloSharedState` (shared memory bus with pub/sub channels, event bus, city priorities, and cross-cycle memory).

### Agent 1: PreTradeValidator

Validates signals before trade placement.

**Checks:**
- Confidence >= 2
- theo_ev > 0.10 (10%)
- F-Strict gates (price band, recal_prob band, station RMSE, lead time 12-24h) for exact-bin signals
- Market resolution > 60 min away (or 12-24h for F-Strict)
- Trade size <= size_cap (default $10)

**Also validates 2-bin grouped trades:** Both legs confidence >=2, combined theo_ev >=0.10, combined size <= 2x cap.

### Agent 2: PositionMonitor

Monitors open positions every 5 min with spread-aware exit rules.

**Grace period:** 25 min for cheap tokens (avg entry <10c), 10 min otherwise.

**Exit rules:**
- RULE A (EXIT_TIME): After grace, if <120 min to resolution and value <35% of entry
- RULE B (EXIT_EV_DECAY): After grace, if value <15% of entry
- RULE C (PROFIT_TAKE): Anytime if value >200% of entry (no grace)

### Agent 3: PostTradeAnalyst

Records trade outcomes. Tracks rolling stats on last 10 trades. Alerts if rolling win rate <40%.

### Agent 4: MarketScanner

Scans and ranks markets by edge quality at 00Z/12Z burst triggers.

**Filters:** edge >=0.10, confidence >=2, YES price 0.02-0.98. Returns top 10 ranked by `edge * confidence`.

### Agent 5: NOHarvester

Scans for near-certain NO opportunities (rake/arbitrage).

**Thresholds:**
- min NO price: 0.90 (YES <=10c)
- max our_prob: 12% YES
- max size: $25/trade
- max per city: 3/cycle
- Returns top 15 sorted by certainty

### Agent 6: YESHarvester

Mirror of NOHarvester for near-certain YES.

**Thresholds:**
- min YES price: 0.92
- min our_prob: 88%
- max size: $25/trade
- max per city: 3/cycle

### Agent 7: WeatherSentinel

Continuously monitors METAR stations, builds observation history, computes trends.

**Coverage:** 19 primary stations polled every 300s (5 min). Max history: 288 observations per station.

**METAR source:** `https://aviationweather.gov/api/data/metar`

**Trend computation:** 2-hour window, rate in C/hr, direction (rising/falling/stable at <0.3C/hr threshold).

**Confidence scoring (0-100):**
- Data score: 0 (no history) to 60 (>=12 obs)
- Freshness: 25 (<600s old) to 0 (>3600s)
- Error penalty: -15 per error (max -50)
- Trend bonus: +3 per sample (max +15)

**Bin boundary alerts:** Flags when current temp is within 5F of market bin boundary. Urgency: critical (<1F), high (<2F), medium (<3F), low (<5F).

### Agent 8: AccuracyTracker

Logs predictions and checks resolutions to build per-station accuracy scores.

**Storage:** `accuracy_store.json` (max 5000 predictions).

**Resolution check:** Every 1800s, checks up to 20 unresolved condition_ids against Polymarket API.

**Metrics:** Brier score, mean absolute error, signal accuracy (% correct BUY YES/NO).

### Agent 9: IntelligenceFeed

Dynamic confidence, multi-source consensus, bin-boundary alerts.

**Open-Meteo forecasts:** Fetches high/low temp for all 19 cities, cached 30 min, 90s backoff after 429.

**Sigma adjustments by Brier score:** multiplier range [0.6, 1.5]. Accurate stations get tighter sigma.

**Consensus levels (primary vs Open-Meteo):**
- Strong (spread <1C): high_confidence
- Moderate (<2C): normal
- Weak (<3.5C): widen_sigma (x1.15)
- Divergent (>=3.5C): reduce_size

### Agent 10: RufloCoordinator

Meta-agent supervising all others. Cross-agent signal scoring with conviction-based routing.

**Conviction scoring (0-100):**
- Sentinel confidence >=80: +15; >=60: +5; >0: -10
- Trend alignment (supports direction): +8; opposes: -12
- Intel consensus strong: +15; moderate: +5; weak: -8; divergent: -20
- Station rating excellent: +10; good: +5; poor: -10; unreliable: -20
- Bin boundary critical+approaching: +12; critical+retreating: -8
- theo_ev >=15: +10; >=10: +5; <5: -10

**Decision routing:**
- Conviction >=75: HIGH_CONVICTION, size x1.5
- Conviction >=50: TRADE, size x1.0
- Conviction >=30: REDUCE, size x0.5
- Conviction <30: VETO, size x0.0

**Feedback:** Losses >$5 trigger 30-min city cooldown.

### SharedState Bus

**Channels:** Agents publish/read named channels. Freshness filter: max_age_s=300.

**Event bus:** Max 500 events, queryable by type and timestamp.

**City priorities:** Score-based with 30-min half-life decay.

**Station reputation:** Long-term grades: A (brier <0.1), B (<0.2), C (<0.35), D (<0.5), F (>=0.5).

---

## 10. Strategy Agent Modules

### 10.1 BinSniper (`bin_sniper.py`)

Snipes mispriced bins on fresh markets.

**Poll interval:** 150s. **Signals:** SNIPE_YES, SNIPE_NO.

**Thresholds:** min edge 8pp, min prob 15%, max market price 85c, min 1c, max $15/trade, max 5 snipes/cycle, markets must be >2h from resolution and <60 min old.

**Confidence:** 2-4 based on edge (>20pp=4, >12pp=3).

### 10.2 GFSRefresh (`gfs_refresh.py`)

Trades on GFS model update deltas.

**GFS schedule (UTC):** 00Z available ~03:30, 06Z ~09:30, 12Z ~15:30, 18Z ~21:30. Check window: -30min to +90min.

**Signals:** GFS_DELTA_YES, GFS_DELTA_NO.

**Trigger:** Probability shift >3pp AND new edge >=6pp. Max $15/trade, 8 trades/refresh.

### 10.3 ObsConfirm (`obs_confirm.py`)

Real-time METAR observation confirmation/kill agent.

**Signals:** OBS_CONFIRM_YES (confidence 4-5), OBS_KILL_NO (confidence 5), OBS_EXIT_SELL.

**Confirmation:** Buy YES if obs in bin, price 0.15-0.80, $20 size. Fair values: 0.95 (post-peak), 0.85 (near-peak), 0.70 (pre-peak).

**Kill:** Buy NO if achievable < bin_lo (post-peak) or temp > bin_hi+1.5F. NO price >=80c, $25 size.

**Max achievable model:** Coastal peak at 4pm (+0.8F/hr), inland at 3pm (+1.0F/hr), capped at +12F remaining.

### 10.4 Exit Agents (`exit_agents.py`)

**ProfitTaker:**
- Targets by confidence: HIGH (1.5x/2.5x/4.0x), MED (1.5x/2.0x/3.0x), LOW (1.3x/1.8x/2.5x)
- Trailing stops: 1.5c (low), 2.5c (medium), 4.0c (high)
- Partial exits: 50% at t1, 25% at t2, 25% rides with trail
- Time urgency: <2h + profit >=1c → sell

**RiskCutter:**
- Loss matrix: <2h cut at 15% loss, 2-6h at 25%+P(win)<10%, 6-12h at 40%+P<15%, >12h at 50%+P<25%
- Hard drawdown kill: 60% loss = unconditional exit
- Rapid decay: `price_cents * hours_remaining < 5.0` → immediate sell
- Weather divergence: temp moving away >0.5F/hr → multiply P(win) by 0.7

### 10.5 CrossCity (`cross_city.py`)

Propagates temperature surprises to correlated neighbors.

**Correlation by distance:** <200km: 0.75, 200-500km: 0.50, 500-1000km: 0.30, >1000km: 0.0. Same-country bonus: +0.10, coastal match: +0.05. Floor: 0.40 to propagate.

**Adjustment cap:** +/-2.0F max. Contradictory evidence raises EV gate to 8%+.

### 10.6 DutchBook (`dutch_book.py`)

Detects distribution inconsistencies where sum(YES_prices) != 1.0.

**Thresholds:** min imbalance 5%, full arb at 6%, execution cost $0.01/bin, max 8 trades/scan, scan every 120s.

**Overbooked (sum >1.0):** YES prices too high → sell signal. **Underbooked (sum <1.0):** buy signal.

### 10.7 HedgeManager (`hedge_manager.py`)

Hedges positions near bin boundaries.

**Boundary detection:** |obs_temp - bin_edge| < 3.0F.

**Hedge sizing:** >2.5F: 10%, 1.5-2.5F: 15%, 0.5-1.5F: 25%, <0.5F: 30% of primary position.

**Cost rejection:** If hedge_price + round_trip_fees (0.04) > 0.25 → skip.

### 10.8 METARIntel (`metar_intel.py`)

Extracts temperature adjustments from METAR weather fields.

**Adjustments (capped at +/-1.5F total):**
- Cloud: FEW/SCT +1.0F, BKN 0.0F, OVC -1.5F
- Wind: Warm advection (S/SW) up to +1.5F, cold (N/NE) up to -1.5F
- Dewpoint: >65F +1.5F, <40F -1.5F, depression <5F -0.5F

**Sigma inflation:** `0.1 * |total_adj|`

### 10.9 LastMile (`last_mile.py`)

Size-up when outcome is nearly locked in.

**Multipliers (HIGH markets, local time):**
- <10am: 1.0x, 10-1pm: 1.1x, 1-3pm: 1.2x, 3-5pm: 1.5x, >5pm: 1.8x

**Observation confirmation boost:** +0.3x if obs in bin post-peak AND fresh (<30 min). Cap: 2.0x.

**EXIT condition:** Observation rules out bin post-peak → 0.0x (kill position).

---

## 11. Execution & Order Management

### 11.1 CLOB Book (`clob_book.py`)

Order book utilities for Polymarket CLOB.

**Cache:** 30s TTL per token_id. Depth window: +/-2% of mid price.

**`edge_at_fill()` — the tradability check:**
- Walks order book to compute real fill price
- Fee cost proxy: 1% (taker)
- raw_ev = (our_prob - fill_price) / fill_price
- net_ev = raw_ev - 0.01
- **Tradeable threshold: net_ev > 0.03 (3% net EV after costs)**

### 11.2 Trader Execution (`trader_execution.py`)

Manages order placement, fill simulation, repricing, and cancellation.

**Constants:**
- Default time-in-book: 60s
- Max reprices: 3
- Reprice tick: 1c improvement
- Default size cap: 20% of depth (35% if depth >$30k AND EV >20%)
- Depth collapse: <$1000 → cancel

**Paper mode:** Simulates fill probability curve `base_prob * (1 + time/max_time)`, capped 0.99. Reprices with 1c improvement. Cancels after max reprices.

**Live mode:** Uses py-clob-client with chain_id=137 (Polygon). Polls status every 5s.

**PaperExecutionAdapter:** Generates UUID order IDs, simulates fills, logs to ledger.

**LiveExecutionAdapter:** Calls `create_and_post_order()` on Polymarket CLOB. Reads `POLYMARKET_PRIVATE_KEY` env var.

### 11.3 Ladder Builder (`src/ladder_builder.py`)

Builds multi-leg order ladders for temperature bins.

**Process:** Filter bins by edge >0 and theo_ev >= min → sort by edge descending → select top N bins → compute Kelly size → apply depth caps.

**Kelly formula:** `f* = edge / odds`, then `kelly_fraction * bankroll`.

**Bins selected:** 4-5 near peak, 3-4 default.

### 11.4 Fee Client (`fee_client.py`)

Fee and rebate awareness. In paper mode: returns 0 bps (stub). Live endpoint not yet implemented.

---

## 12. Trade Ledger & Telemetry

### 12.1 Simple Ledger (`trade_ledger.py`)

SQLite persistent storage. DB path: `/data/ledger.db` (Railway) or `ledger.db` (local), overrideable via `LEDGER_DB` env var.

**Tables:**
- `trades`: id, ts, question, city, signal, token_id, price, size, spend, ev, ev_dollar, kelly, our_prob, mkt_price, sigma, mode, clob_spread, clob_edge_at_fill, resolved, resolution_price, pnl, won, resolved_at, meta, strategy_type, trade_group_id
- `cycles`: id, ts, total_signals, tradeable, trades_placed, top_city, top_ev, recalibrated, meta

**Key queries:** `get_performance_summary()` computes total/resolved/pending/wins/losses/win_rate/total_pnl/per-city breakdown.

### 12.2 Advanced Telemetry (`src/ledger_telemetry.py`)

Extended SQLite with frozen snapshots, decay metrics, and 14 tables.

**Key tables:**
- `trade_groups`: Full trade group metadata (market, station, regime, diurnal stage, obs at entry, EV, outcome, PnL)
- `legs`: Per-leg execution details (entry/fill prices, adverse move, spread paid, time-in-book, reprice count)
- `frozen_snapshots`: Immutable probability vectors at trade decision time (NEVER updated post-trade)
- `observations`: Station observation history
- `book_snapshots`: Order book state at trade time
- `no_trade_log`: Why a market was skipped
- `fallback_log`: When computation fell back to simpler method
- `alert_log`: Decay/spread/fill alerts
- `fee_log`, `rebate_log`, `sanity_checks`, `daily_reports`, `cross_market_log`

**Decay metrics (alerts):**
- Adverse move >25% → alert
- Spread paid >10% → alert
- Time to first fill >90s → alert
- Fill completion in 60s <40% → alert
- Rolling leakage >8 bps → alert

---

## 13. Trade Resolution

### `trade_resolver.py`

Resolves open trades against Polymarket outcomes.

**Source:** Gamma API `https://gamma-api.polymarket.com/events?tag_slug=weather&closed=true`, paginated at 100/page.

**3-tier resolution lookup:**
1. Exact match on full 76-digit token_id
2. Prefix match on first 12 digits (handles JS precision loss)
3. Normalized question text match (fallback)

**PnL computation:**
- BUY YES wins: PnL = (size * $1) - spend
- NO_HARVEST wins: PnL = (size * $1) - spend
- EXIT_SELL_ALL: auto-resolved with pnl=0

**Throttle:** Once per hour (3600s) unless forced.

---

## 14. Scheduling & Main Loop

### `scheduler.py`

Orchestrates the full trading cycle.

**Default interval:** 15 min between cycles.

**Burst triggers (model update windows):**
- Hard: 00Z, 12Z (UTC hours 0, 12, +/-15 min)
- Secondary: 06Z, 18Z
- Effect: EV threshold tightened by 20% (multiplied by 0.8)

**10-step cycle pipeline per market:**
1. Parse station; require confidence >= 2
2. Validate WU/METAR (skipped in paper mode)
3. Get prices & book snapshot
4. Compute TTR, diurnal stage, regime
5. Choose nowcasting (TTR <=24h) vs ensemble (TTR >24h)
6. Apply cross-market filter
7. Compute min_theo_ev with dynamic ratchet
8. Build ladder; compute theoretical EV
9. Check EV gates: theoretical_ev >= min_theo_ev
10. Freeze snapshot; place orders if passes gates

**HRRR poll:** Every 60 min.

**Backtest runner included:** Runs historical markets through the pipeline with causality enforcement.

---

## 15. Configuration Reference

### `config.py`

All configuration in immutable dataclasses.

**Strategy enables:**
| Flag | Value | Purpose |
|------|-------|---------|
| ENABLE_F_STRICT | True | F-Strict predictive cohort |
| ENABLE_NO_HARVEST_V2 | True | Scaled rake business |
| ENABLE_ABOVE_BELOW | True | Shadow lane only ($2/trade) |
| ENABLE_EXACT_SINGLE | False | Disabled (0/11 WR historically) |
| ENABLE_EXACT_2BIN | True | Inside F-Strict |
| ENABLE_LONG_HORIZON | False | Hard-killed (no model >24h) |
| PILOT_CITY_ONLY | "London" | Drop after 72h success |

**Trading thresholds:**
| Parameter | Value |
|-----------|-------|
| MIN_THEO_EV_BASE | 0.10 (10%) |
| GATE_12H_MIN_EV | 0.14 |
| GATE_6H_MIN_EV | 0.20 |
| POST_PEAK_MIN_EV | 0.18 |
| NEAR_PEAK_EV_BOOST | +0.02 |
| KELLY_SIZE_CAP_NEAR_PEAK | 0.15 |
| CROSS_MARKET_DELTA_Z_THRESHOLD | 2.8 |
| CROSS_MARKET_EV_BOOST | 0.03 |
| SIGMA_FLOOR_C | 2.5 |
| EXACT_BIN_HARD_CAP | 0.35 |

**Nowcasting parameters:**
| Parameter | Value |
|-----------|-------|
| HALF_LIFE_NEAR_PEAK | 2.0h |
| HALF_LIFE_DEFAULT | 4.0h |
| HALF_LIFE_COASTAL | 3.0h |
| AR1_RHO_DEFAULT | 0.78 |
| AR1_RHO_COASTAL | 0.70 |
| MONTE_CARLO_SAMPLES | 5000 |
| OBS_ANOMALY_TEMP_THRESHOLD | 6.0C |
| OBS_ANOMALY_TIME_THRESHOLD | 45 min |
| OBS_SIGMA_WIDEN_FACTOR | 1.4 |

**Execution parameters:**
| Parameter | Value |
|-----------|-------|
| DEFAULT_TIME_IN_BOOK | 60s |
| MAX_REPRICES | 3 |
| SIZE_CAP_DEFAULT_PCT | 20% of depth |
| SIZE_CAP_HIGH_DEPTH_PCT | 35% of depth |
| HIGH_DEPTH_THRESHOLD | $30,000 |

**Diurnal peak windows (by latitude):**
- High lat (>50N): 13-16 local
- Mid lat (30-50N): 14-17 local
- Low lat (<30N): 15-18 local
- Coastal shift: +1 hour

**API endpoints:**
- Polymarket API: `https://api.polymarket.com`
- Polymarket CLOB: `https://clob.polymarket.com`
- Paper mode: True (default)
- DB path: `ledger.db`
- Required env vars: `WU_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`

---

## 16. API Server & Dashboard

### `api_server.py`

Flask REST API exposing bot data to a Vercel dashboard.

**CORS origins:** `https://iamweather.vercel.app`, `http://localhost:3000`, `http://localhost:5500`

**Boot identity:** Random 4-byte hex BOOT_ID, UTC boot timestamp, git commit hash.

**Key routes:**
- `GET /` — Live HTML dashboard (auto-refresh 30s)
- `GET /api/health` — Health status with all strategy flags, pilot mode info
- `GET /api/weather` — Open-Meteo forecast for all 58 cities (cached 300s)
- `GET /api/ledger` — Trade history (last 200)
- `GET /api/ledger/open-positions` — Unresolved trades

**Signal generation pipeline (inside api_server):**
1. Parse city, threshold, direction from question text
2. Fetch ensemble forecast
3. Apply station edge if available
4. Apply bias correction via bias_agent
5. Apply strategy gates: `f_strict_pass()`, `shadow_lane_ok()`
6. Signal dict includes: our_prob, our_prob_recal, lane (F-STRICT/SHADOW/LEGACY), gate_reason, theo_ev_raw, ev_dollar_raw, kelly_raw

**Loaded agents at boot:** PreTradeValidator, PositionMonitor, PostTradeAnalyst, MarketScanner, NOHarvester, YESHarvester, WeatherSentinel, AccuracyTracker, IntelligenceFeed, RufloCoordinator, SharedState, StationBiasAgent, BinSniper, GFSRefreshAgent, ObsConfirmAgent, DutchBookScanner, CrossCityCorrelationEngine, METARIntel, LastMileAgent, LiquidityTimer, HedgeManager.

---

## 17. Data Sources & External APIs

| Source | URL | Data | Cache |
|--------|-----|------|-------|
| Open-Meteo Ensemble | `https://ensemble-api.open-meteo.com/v1/ensemble` | 82 ensemble members (51 ECMWF + 31 GFS) | 30 min |
| Open-Meteo Deterministic | `https://api.open-meteo.com/v1/forecast` | 7 deterministic models | 30 min |
| Open-Meteo (IntelligenceFeed) | `https://api.open-meteo.com/v1/forecast` | Consensus high/low temps | 30 min |
| METAR (Aviation Weather) | `https://aviationweather.gov/api/data/metar` | Live airport observations | 5-15 min |
| Polymarket Gamma | `https://gamma-api.polymarket.com/events` | Market discovery | 30 min |
| Polymarket CLOB | `https://clob.polymarket.com` | Order books, trade execution | 30s |
| Polymarket Data | `https://data-api.polymarket.com` | Resolution data | On-demand |
| Weather Underground | Via station rules URLs | Resolution source (truth) | N/A |

**Rate limiting:** Open-Meteo: 120s backoff on 429. IntelligenceFeed: 90s backoff. Gamma: stale-while-revalidate on empty fetch.

---

## 18. File Map

### Root Directory
| File | Lines | Purpose |
|------|-------|---------|
| api_server.py | ~5079 | Flask API, signal generation, dashboard |
| config.py | ~600 | Central configuration |
| ruflo_monitor.py | ~1800 | 10 RUFLO agents + SharedState |
| scheduler.py | ~500 | Main trading loop, burst triggers |
| active_trader.py | ~450 | Position management, 4-layer exit |
| trader_execution.py | ~400 | Order placement, fill simulation |
| trade_resolver.py | ~300 | Resolution lookup, PnL computation |
| trade_ledger.py | ~250 | Simple SQLite ledger |
| gamma_client.py | ~400 | Market discovery, caching |
| clob_book.py | ~200 | Order book utilities |
| fee_client.py | ~150 | Fee/rebate stubs |
| station_bias.py | ~300 | Bias database and learning |
| nowcaster.py | ~200 | Short-horizon wrapper |
| ensemble_probs.py | ~100 | Long-horizon shim |
| bin_sniper.py | ~200 | New market sniping |
| gfs_refresh.py | ~250 | GFS update trading |
| obs_confirm.py | ~350 | METAR confirmation/kill |
| exit_agents.py | ~400 | ProfitTaker + RiskCutter |
| cross_city.py | ~250 | Geographic correlations |
| dutch_book.py | ~200 | Distribution arbitrage |
| hedge_manager.py | ~200 | Boundary hedging |
| metar_intel.py | ~200 | METAR field adjustments |
| last_mile.py | ~200 | Resolution last-mile sizing |

### src/ Directory
| File | Lines | Purpose |
|------|-------|---------|
| multi_model_forecast.py | ~500 | Ensemble probability engine |
| nowcasting.py | ~300 | Monte Carlo nowcasting |
| probability_calculator.py | ~250 | KDE + Bayesian smoothing |
| regime_classifier.py | ~250 | Weather regime detection |
| station_edge.py | ~350 | Obs/ensemble blending |
| bias_agent.py | ~350 | 4-knob bias adjustments |
| strategy_gate.py | ~170 | F-Strict gate + recal map |
| risk_manager.py | ~200 | EV computation, cost proxy |
| station_parser.py | ~250 | ICAO extraction, confidence |
| market_classifier.py | ~150 | Market type classification |
| cross_market_filter.py | ~200 | Delta z-score filtering |
| ladder_builder.py | ~250 | Order ladder construction |
| time_utils.py | ~200 | Diurnal staging, peak windows |
| ledger_telemetry.py | ~500 | Advanced telemetry (14 tables) |

### Other
| File/Dir | Purpose |
|----------|---------|
| docs/STRATEGY_REWRITE.md | Strategy rewrite plan |
| docs/FORENSIC_AUDIT.md | 12-section forensic audit |
| scripts/calibration_backfill.py | Backfills accuracy_store resolutions |
| station_reliability_latest.json | Per-station RMSE data |
| accuracy_store.json | Prediction log + resolutions |
| ledger.db | Active trade database |
| station_bias.db | Bias observation database |

---

## 19. Known Bugs & Failure Modes

### 19.1 Confirmed Bugs (as of 2026-04-11)

1. **Calibration is broken above 25% bucket.** Model claims 30-95% confidence where actual win rate is 9-36%. This is the #1 cause of losses. Recalibration map applied but derived from only 133 trades.

2. **Ensemble members are correlated, not independent.** 51 ECMWF + 31 GFS share model physics. Empirical sigma underestimates real forecast uncertainty. No bias correction in ensemble path (only fallback).

3. **1F bins are physically too narrow.** Median station RMSE is 1.60C (~2.9F). A 1F bin is ~0.56C wide. Max hit rate ceiling is ~15-20% regardless of model quality.

4. **Calibration loop is dead.** accuracy_store.json has 888 predictions and 0 resolutions. The model never learns from being wrong. (calibration_backfill.py was written but needs to be run regularly.)

5. **end_date parsing bug.** Polymarket sends both ISO 8601 and display-formatted dates ("Thu, 09 Apr 2026"). When parsing fails, mins_to_resolution = None and gating rules are silently skipped.

6. **Paper trading overestimates profit.** Assumes instant fill at CLOB mid estimate, zero fees, zero slippage. Real fills would be ~1-2% worse. (Immaterial vs. fundamental signal problem.)

### 19.2 Structural Weaknesses

1. **Atlanta concentration risk.** In the "good period" (Mar 27-30), $177 of $242 profit came from 4 trades on a single Atlanta market. Removing Atlanta, BUY YES was -$112 from day one.

2. **EV is anti-correlated with winning.** In the 10-20c bracket, winners average EV=29.6 and losers average EV=38.8. Higher claimed EV → higher loss rate.

3. **Kelly sizer amplifies overconfidence.** Kelly sizes proportional to claimed probability. When probability is delusional (claims 80%, reality 10%), Kelly bets heavily on guaranteed losers.

4. **F-Strict pilot produced 0 trades in 89 hours.** London-only + tight gates = too few candidates. The strategy may be correct but needs multi-city deployment to generate meaningful trade flow.

5. **NO_HARVEST is economically trivial.** 88 opened, 26 resolved, +$0.31 total. The rake exists but penny-level at current volume.

---

*End of encyclopedia. This document covers every module, agent, threshold, data source, and decision path in the WeatherEdge Bot v2 codebase as of 2026-04-11.*
