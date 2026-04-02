# WeatherEdge Bot V2 — Profitability Roadmap

**Date**: April 2, 2026
**Status**: Strategically unproven, operationally strong
**Current Win Rate (clean bucket)**: 24.4% (41 resolved: 10W / 31L)
**Goal**: Achieve >55% win rate on post-fix cohort, then evaluate for live capital

---

## PHASE 0: EMERGENCY — Deploy the Calibration Fix (Day 1)

### Problem Found
The calibration fix (commits `2d2dd0c` + `5924b8c`) is committed and pushed to GitHub
but **NOT running on Railway**. Evidence:
- All recent trades have `calibration_v=MISSING` in meta
- Exact-bin trades still showing 50-99% probabilities (Seattle 99.9%, Ankara 86.9%, Mexico City 74.8%)
- The 45% hard cap is NOT active in production
- The bot is still running the broken overconfident code

### Action Required
1. Log into Railway dashboard
2. Trigger a manual redeploy from the `main` branch (commit `5924b8c`)
3. Verify the new deployment by checking `/api/health` for a new `boot_id`
4. Place a test trade and confirm `calibration_v: 2` appears in the meta JSON
5. Monitor for 1 hour — exact-bin probabilities should stay below 45%

**This is the single most important action. Nothing else matters until the fix is live.**

---

## PHASE 1: OBSERVATION — Collect Post-Fix Data (Days 1-7)

### Goal
Accumulate 100+ resolved main_loop trades with `calibration_v >= 2` so we can measure
whether the fix actually produces profitable trades.

### What to Track (already wired but needs the deploy)
The `/api/stats/reliability` endpoint has a `postfix_cohort` section that filters trades
by `calibration_v >= 2` and reports:
- win_rate_pct, won, lost, pending
- avg_our_prob, avg_mkt_price, avg_edge

### Key Metrics to Watch

| Metric | Red Flag | Acceptable | Good |
|--------|----------|------------|------|
| Win rate (exact bins) | < 30% | 35-45% | > 50% |
| Win rate (above/below) | < 40% | 45-55% | > 60% |
| Avg our_prob | > 40% | 15-35% | 20-30% |
| Avg edge claimed | > 25pp | 8-15pp | 10-15pp |
| Trades per day | > 30 | 5-15 | 8-12 |

### What NOT to Do
- Do not add strategy complexity
- Do not change thresholds or parameters
- Do not switch to live trading
- Do not panic if first 20 trades look bad — need 100+ sample

---

## PHASE 2: ADJACENT-BIN ERROR ANALYSIS (Days 3-7)

### Purpose
ChatGPT's key recommendation: determine if losses are "one bin away" (sigma is too tight)
or "all over the place" (forecast signal is weak). This tells us whether to fix calibration
or rethink the whole approach.

### Implementation: New `/api/stats/bin_error` Endpoint

Build a new endpoint that for each resolved main_loop trade:

1. Parse the question to extract: city, date, threshold_c, direction
2. Look up what ACTUALLY happened (the winning bin from the Gamma resolved event)
3. Compute: `error_bins = actual_winning_threshold - predicted_threshold`
4. Classify:
   - **Hit**: error = 0 (we picked the right bin)
   - **Adjacent miss**: |error| = 1°C (one bin off)
   - **Near miss**: |error| = 2°C (two bins off)
   - **Wild miss**: |error| > 2°C (forecast was wrong)

### Expected Outcomes and What They Mean

**If most losses are adjacent (1 bin off):**
→ Forecast mean is good, sigma is too tight
→ Fix: widen sigma further, shift to above/below trades instead of exact bins
→ Prognosis: GOOD — bot has real forecast skill, just needs calibration

**If losses are scattered (2+ bins off):**
→ Forecast mean is unreliable for that city/horizon
→ Fix: reduce exact-bin trading, focus on wider above/below bins
→ Prognosis: MIXED — need to cherry-pick cities where forecast is strong

**If losses are systematically biased (always high or always low for a city):**
→ Per-city bias correction is wrong or missing
→ Fix: update _CITY_BIAS_C values from latest station_bias.db data
→ Prognosis: GOOD — easy fix once identified

### Data Source
The resolver already stores `bin_label` from the Gamma event. We need to match each
trade's predicted bin against the actual winning bin from the same event.

---

## PHASE 3: CALIBRATION LOOP (Days 7-14)

### The Real Fix (Beyond Heuristic Caps)

ChatGPT is right that the 45% cap is a safety rail, not a model. The long-term answer
is calibration against resolved outcomes. Here's how:

### 3A. Probability Calibration Table

After 100+ resolved trades, build a calibration table:

```
Claimed prob range | Trades | Won | Actual win % | Calibration factor
0-10%              |   25   |  3  |    12%       | 1.2x (underclaiming)
10-20%             |   30   |  4  |    13%       | 0.87x (slight overclaim)
20-30%             |   20   |  3  |    15%       | 0.6x (overclaiming!)
30-45%             |   15   |  4  |    27%       | 0.75x (overclaiming)
```

Apply as: `calibrated_prob = raw_prob * calibration_factor[bucket]`

### 3B. Per-City Sigma Recalibration

For each city with 20+ resolved trades:
1. Compute the empirical distribution of `(actual_temp - forecast_mean)`
2. The standard deviation of that distribution IS the true sigma
3. Replace `_CITY_SIGMA` values with empirically measured ones
4. Cities with < 20 trades keep the current conservative values

### 3C. Dynamic EV Threshold

Instead of a fixed `min_ev = 5pp`, adjust based on:
- Station reliability bucket (already wired via bias_agent)
- Historical accuracy for that city (from calibration table)
- Horizon (same-day vs tomorrow)

Formula: `min_ev = 5 + ev_addon + (1 - city_calibration_factor) * 10`
This automatically raises the bar for cities where we overclaim.

---

## PHASE 4: STRATEGY REFINEMENT (Days 14-21)

### 4A. Shift Toward Above/Below Trades

If the adjacent-bin analysis shows most losses are 1 bin off, exact-bin trading
is inherently hard. The math:

- Exact bin (1°C wide): even perfect forecasts hit ~30-40% max
- Above/below: a good forecast can reasonably hit 60-70%

The bot should weight above/below trades more heavily and use tighter EV gates
for exact bins. Consider:
- Exact bins: min_ev = 10pp (up from 5)
- Above/below: min_ev = 5pp (keep current)

### 4B. Time-of-Day Edge Decay

The ensemble updates every 6 hours (00Z, 06Z, 12Z, 18Z). Our probability
estimate decays in accuracy as we get further from the last model run.
Track edge_at_trade_time vs resolution outcome and see if trades placed
within 2 hours of model initialization win more than those placed 5+ hours after.

### 4C. Market Price as Signal

When market price < 10¢ for an exact bin, it means the crowd assigns < 10%
probability. Our model says 25%. Who's right?

Track: `our_prob / mkt_price` ratio vs actual win rate.
If ratio > 2x consistently wins, the market is underpricing.
If ratio > 2x consistently loses, we're overconfident.

---

## PHASE 5: LIVE READINESS CHECKLIST (Day 21+)

### Hard Prerequisites (ALL must be met)

- [ ] Post-fix cohort has 100+ resolved main_loop trades
- [ ] Post-fix win rate > 45% on exact bins OR > 55% on above/below
- [ ] Post-fix PnL is positive (net profit on paper)
- [ ] Adjacent-bin analysis shows majority of losses are ≤ 1 bin off
- [ ] Calibration table shows our probability claims match actual outcomes within ±10pp
- [ ] No city has > 20 trades with < 25% win rate (indicates broken forecast for that city)
- [ ] System has been continuously stable for 7+ days (no crashes, no missed cycles)

### Soft Prerequisites (Strongly Recommended)

- [ ] Per-city sigma recalibrated from empirical data
- [ ] Dynamic EV threshold active
- [ ] Above/below vs exact-bin strategy distinction deployed
- [ ] Time-of-day analysis shows when edge is strongest

### Live Deployment Plan

1. Start with $50 bankroll, max $2 per trade
2. Trade only the 5 cities with highest post-fix win rate
3. Run for 7 days, compare paper vs live execution (slippage analysis)
4. If live PnL tracks paper within 20%, expand to $200 bankroll
5. Weekly review: if any rolling 50-trade window drops below 40% win rate, pause and investigate

---

## IMPLEMENTATION PRIORITY

### This Week (Must Do)
1. **Deploy the calibration fix to Railway** (30 minutes)
2. **Verify deployment** — check exact-bin caps are active (10 minutes)
3. **Wait for post-fix trades to accumulate** (passive, 3-5 days)
4. **Build the adjacent-bin error endpoint** (2-3 hours of coding)

### Next Week (Should Do)
5. Run adjacent-bin analysis on post-fix data
6. Build probability calibration table from resolved trades
7. Per-city sigma recalibration from empirical data
8. Decide: exact-bin vs above/below strategy weighting

### Week 3 (Nice to Have)
9. Dynamic EV threshold
10. Time-of-day edge decay analysis
11. Live readiness assessment

---

## SUMMARY

The bot has strong engineering but an unproven strategy. The biggest problem right now
is that the calibration fix ISN'T EVEN RUNNING — deploy it first, then watch the data.

The path to profitability requires patience:
1. Deploy fix → collect data → measure → adjust → collect more data → assess

ChatGPT's recommendation to "not add complexity" is correct. The temptation is to add
more agents, more strategies, more signals. Resist it. The bot needs fewer, better-calibrated
trades, not more.

The honest timeline: 3 weeks minimum before you can know if this is viable for real money.
If post-fix data looks good after 100+ trades, the path to profitability is clear.
If it doesn't, the bot's forecast signal may not be strong enough for exact-bin trading,
and the strategy needs to pivot to above/below bins or wider spreads.
