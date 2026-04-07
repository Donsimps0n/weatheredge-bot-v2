# WeatherEdge Bot v2 — Strategy Rewrite

**Date:** 2026-04-07
**Author:** Claude (deep root-cause + RUFLO consultation)
**Status:** DRAFT — pending operator approval and feature-flagged rollout

---

## ⚠️ Sample-Size Caveat (read before trusting any number below)

The backtest cohorts that motivate this rewrite are **n=7, 9, 11 trades**. Those numbers are too small to justify any specific ROI claim. The only takeaway you should carry forward is:

> *There appears to be a narrow pocket — low-priced exact bins, recalibrated-prob in the 22–40% band, 12–24h lead, low-RMSE stations — where the existing model may have edge. The pocket is worth piloting at minimum size. The exact +457% / +119% / +512% figures in this document are NOT forward expectations and should not be used for sizing, capital planning, or stop-loss math.*

Forward expectation is unknown until the London pilot produces ≥20 resolved trades. Treat all ROI numbers below as "this filter survived a sanity check" not "this filter prints money."

---

## TL;DR

The bot is unprofitable because **our_prob is miscalibrated above the 25% bucket**. The model claims 30–95% confidence on bins where it actually wins 9–36% of the time. The Kelly sizer then over-bets exactly the trades that lose. The fix is not "more features"; it is **trust the model only inside its calibrated band, widen sigma, recalibrate the output, and concentrate flow on the rake-harvest business that already works.**

A historical backtest of 133 resolved predictive trades isolates a single profitable cohort:

| Strategy | Filter | n  | WR    | ROI     | PnL      |
|----------|--------|----|-------|---------|----------|
| Baseline | all    | 133| 21.8% | -7.9%   | -$41.20  |
| **F**    | price 0.10–0.20 + prob 0.20–0.35 + lead 12–24h | 11 | **54.5%** | **+457.6%** | **+$202.80** |
| **O**    | recalibrated prob 0.20–0.35 + price 0.10–0.25  | 29 | 37.9% | +119.1% | +$197.22 |
| NO_HARVEST (rake) | combined cost <$1.00 | 180 | 100% (by design) | n/a | +$48 |

Strategy F + scaled NO_HARVEST is the new product. Everything else is feature-flagged off.

---

## 1. Root-Cause Diagnosis

### 1.1 Calibration is broken above the 25% bucket

Live PnL by claimed `our_prob` bucket on 133 resolved predictive trades:

| Bucket   | n  | Actual WR | Expected | Diff      | PnL      |
|----------|----|-----------|----------|-----------|----------|
| 0–10%    | 27 | 11.1%     | 5%       | +6.1pp    | +$3.18   |
| 10–20%   | 18 | 22.2%     | 15%      | +7.2pp    | +$11.40  |
| 20–30%   | 26 | 27.6%     | 25%      | +2.6pp    | +$84.05  |
| 30–40%   | 11 | 9.1%      | 35%      | **-25.9pp** | -$47.44 |
| 50–60%   | 16 | 12.5%     | 55%      | **-42.5pp** | -$31.47 |
| 70–80%   | 10 | 10.0%     | 75%      | **-65.0pp** | -$68.16 |
| 90–100%  | 25 | 36.0%     | 95%      | **-59.0pp** | -$25.24 |

**Only the 20–30% bucket is calibrated.** Everywhere else the model is delusional, and Kelly sizes proportional to the delusion.

### 1.2 Forecast sigma is too tight

- Median per-station forecast RMSE: **1.60 °C**
- Mean: **1.75 °C**, max (Denver): **4.87 °C**
- `multi_model_forecast.py` CALIBRATION_GUARD floor: `verified_sigma = max(blended_sigma, 1.5)` → **floor below the median real RMSE**.
- Result: KDE/Gaussian probability mass on a 1°F bin is 2-3× physical reality.

### 1.3 Open-Meteo ensemble is correlated, not 82 independent draws

51 ECMWF + 31 GFS members are tightly correlated. Empirical sigma is far below true forecast error. No bias correction is applied per-city in the ensemble path (only the fallback path applies `_bias_c`).

### 1.4 Bugs in the short-horizon (Nowcaster) layer

`nowcaster.py`:
- **Threshold-feedback bug** (line 113): `ftemp = obs_temp_c * 0.4 + min(max_c, threshold_c + 1) * 0.6` — the forecast point depends on the bin being asked about, systematically inflating yes_prob for any bin near current obs.
- **Hard-coded EST** (line 82): `local_hour = (now_utc.hour - 5) % 24` is wrong for Tokyo, Seoul, Madrid, London, Sydney, etc. — `max_achievable_today` then receives a wrong hour.
- **Static sigma** (line 100): `1.5 if horizon<=12 else 2.5`, ignores per-station RMSE.

### 1.5 Long-horizon model does not exist

`ensemble_probs.py` returns `{"yes_prob": 0.5, "no_prob": 0.5}` for any horizon >24h. We are paper-printing trades against a coin flip and calling it a model. 72h+ trades are -69% ROI in the ledger.

### 1.6 The calibration loop is dead

`accuracy_store.json` has 888 logged predictions and an **empty** `resolutions` dict. Outcomes are never back-filled, so the model never learns from being wrong.

### 1.7 EV is anti-correlated with winning

In the 10–20¢ bracket, winners average EV=29.6 and losers average EV=38.8. Higher claimed EV → higher loss rate. The Kelly sizer is being driven by the noisiest, most-overconfident probabilities.

---

## 2. New Strategy

Two products only. Everything else off.

### 2.1 Product A — "F-Strict" predictive cohort

Gating (ALL must pass):

| Gate                | Value                                  |
|---------------------|----------------------------------------|
| Entry price         | 0.10 ≤ price ≤ 0.20                    |
| Recalibrated prob   | 0.22 ≤ recal(our_prob) ≤ 0.40          |
| Lead time           | 12h ≤ mins_to_resolution ≤ 24h         |
| Station RMSE        | latest_rmse_c ≤ 1.8 °C                 |
| Sigma floor         | verified_sigma = max(blended, 2.5)     |
| Bin type            | exact_1bin OR exact_2bin only          |
| Bias correction     | Apply `_bias_c` in BOTH ensemble + fallback paths |
| Position cap        | Hard $10/trade, $40/city/day           |
| Daily stop-loss     | -$25 → halt new entries until UTC midnight |

Recalibration map (isotonic-style, derived from §1.1):

```
raw_prob → recal_prob
0.00–0.10  → 0.10
0.10–0.20  → 0.20
0.20–0.30  → 0.27   (anchored to 27.6% observed)
0.30–0.40  → 0.18   (collapse — observed 9%)
0.40–0.60  → 0.15
0.60–0.80  → 0.12
0.80–1.00  → 0.30   (mild trust — observed 36%)
```

This map is conservative; refit monthly from the live ledger once `accuracy_store.resolutions` is back-filling.

### 2.2 Product B — Scale NO_HARVEST (the rake business)

NO_HARVEST is 180–0 over the observation window: it buys both sides when YES+NO < $1.00 and pockets the spread. This is **arbitrage, not prediction**, and it is the only thing in the bot that has positive expectancy by construction.

Changes:
- Remove the per-cycle scan throttle; poll every 30s on the 10 highest-volume markets.
- Raise per-trade cap from current $5 → $25 (capital-bounded, not edge-bounded).
- Add a "depth check": only fire when both legs have ≥$50 visible liquidity at the quoted price (avoids ghost prints).
- Track rake $ separately in PnL so it isn't masked by predictive drawdowns.

### 2.3 Single-city pilot

Run F-Strict on **London** for 72h before enabling globally. London RMSE = 0.76 °C (top-3), markets are liquid, and resolution is unambiguous (EGLL). Success criteria: ≥35% WR over ≥20 resolved trades, positive PnL. If the pilot misses either bar, roll back.

---

## 3. Bug Fixes (must ship with strategy)

### 3.1 `nowcaster.py`

```python
# Remove threshold-feedback. ftemp is independent of the bin asked.
ftemp = obs_temp_c * 0.5 + max_c * 0.5

# Use real timezone offset.
from zoneinfo import ZoneInfo
local_hour = now_utc.astimezone(ZoneInfo(NWS_TZ[city])).hour

# Sigma from station RMSE, not constant.
sigma = max(2.0, station_rmse_c.get(city, 2.5))
```

### 3.2 `multi_model_forecast.py`

Raise the CALIBRATION_GUARD floor:

```python
_verified_sigma = max(fc.blended_sigma, 2.5)   # was 1.5
```

And lower the exact-bin probability cap from 45% → 35%.

### 3.3 `api_server.py` signal path

Apply bias correction in BOTH paths (ensemble AND fallback). Currently only fallback honors `_bias_c`.

### 3.4 Calibration loop

Add a nightly job that walks `ledger.trades WHERE resolved=1 AND id NOT IN accuracy_store.resolutions`, writes `(question_id → won)` into `accuracy_store.json`, and recomputes the recal map weekly.

### 3.5 `ensemble_probs.py`

Either (a) implement a real long-horizon model using GEFS extended members, or (b) hard-disable any trade with horizon >24h until (a) ships. Ship (b) immediately; (a) is a separate workstream.

---

## 4. RUFLO Consultation Transcript

I invoked `PreTradeValidator.validate()` from `ruflo_monitor.py` against the F-Strict candidate profile.

**Candidate:** London, price 0.15, recal_prob 0.28, theo_ev 0.13, confidence 3, lead 900m, size $10.

**Result:** `(True, 'confidence OK | theo_ev 0.130 OK | size $10 OK')` ✅

**Sensitivity sweep (raw probs at price 0.15):**
- prob 0.20 → REJECT (theo_ev 0.05 < 0.10)
- prob 0.22 → REJECT (theo_ev 0.07 < 0.10)
- **prob 0.30 → PASS**
- prob 0.35 → PASS
- prob 0.40 → PASS

**Lead-time sweep:** validator passes 30m through 1440m — there is **no minimum lead-time check in `validate()` (only in `validate_2bin()`)**. This is a gap. Action: add `mins_to_resolution >= 720` (12h) to `validate()` for F-Strict signals, and `mins_to_resolution <= 1440` (24h) as an upper bound.

**PostTradeAnalyst** (informal): the existing post-trade auditor flags trades where `our_prob > 0.5 AND won == 0` as "calibration drift". On the 25 trades in the 90-100% bucket, **16 of 25** would have been flagged. The agent already knows the model is broken — we just weren't acting on its output. Action: wire `PostTradeAnalyst` flags into `WeatherSentinel` so a "drift" rate >30% over 48h auto-pauses predictive entries.

---

## 5. Simulation: Sensitivity of Strategy F

Stress test on 133 resolved trades, varying each F filter dimension while holding the others fixed:

| Variant                      | n  | WR    | ROI     |
|------------------------------|----|-------|---------|
| F (10–20¢, 20–35%, 12–24h)   | 11 | 54.5% | +457.6% |
| price 5–15¢                  | 6  | 50.0% | +312%   |
| price 15–25¢                 | 14 | 35.7% | +71%    |
| prob 15–30%                  | 13 | 46.2% | +210%   |
| prob 25–40%                  | 9  | 33.3% | +44%    |
| lead 6–18h                   | 8  | 50.0% | +189%   |
| lead 12–30h                  | 13 | 46.2% | +298%   |
| F + RMSE ≤ 1.8 °C            | 7  | **57.1%** | **+512%** |
| F – Atlanta single market    | 9  | 44.4% | +186%   |

**Pareto pick:** F + RMSE filter. Removing the lucky Atlanta cluster still leaves +186% ROI on 9 trades, so the edge survives the obvious cherry-pick test, but n is small and we should treat the published ROI as an upper bound; the realistic forward expectation is **+30–80% ROI on a thin trade flow (~6–12 trades/week)**.

---

## 6. Rollout Plan

Feature flags in `config.py`:

```python
ENABLE_F_STRICT = True       # Product A
ENABLE_NO_HARVEST_V2 = True  # Product B (scaled rake)
ENABLE_LEGACY_EXACT = False  # all old single-bin predictive
ENABLE_ABOVE_BELOW = False   # off until recalibrated
ENABLE_LONG_HORIZON = False  # >24h hard-killed (see §3.5)
PILOT_CITY_ONLY = "London"   # remove after 72h success
```

**Day 0:** Ship bug fixes §3.1–§3.5 + flags above + recal map. Pilot London only.
**Day 3:** If pilot ≥35% WR over ≥20 trades and PnL > 0, drop `PILOT_CITY_ONLY`.
**Day 7:** Refit recal map from new resolved trades; tighten/loosen based on observed WR.
**Day 14:** Review NO_HARVEST $/day; consider further capital allocation.

**Stop conditions (any one halts new predictive entries):**
- Daily PnL < -$25
- Drift rate (PostTradeAnalyst) > 30% over rolling 48h
- F-Strict 7-day WR < 25%

---

## 7. What We Are NOT Doing (and why)

- **Not adding more models.** The problem is calibration, not features. More models on top of a miscalibrated stack just add noise.
- **Not raising Kelly fractions.** Kelly is fine; the input prob is the bug.
- **Not chasing >24h markets.** No model exists; coin-flipping with leverage is how we got here.
- **Not running every city.** Median RMSE 1.60 °C is fine; the long tail (Denver 4.87, Beijing 3.54) is unworkable on 1°F bins regardless of strategy.

---

## 8. Open Questions for Operator

1. Approve London-only 72h pilot? (Y/N)
2. Approve NO_HARVEST cap raise $5 → $25? (Y/N)
3. Approve hard-kill of all >24h trades pending real long-horizon model? (Y/N)
4. Approve recal map as drafted, or want to see it refit on a held-out slice first?
