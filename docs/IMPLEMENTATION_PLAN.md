# WeatherEdge Bot V2 — Implementation Plan

**Date**: April 2, 2026
**Based on**: ChatGPT audit + scalp engine recommendation + infrastructure reality check

---

## HONEST ASSESSMENT OF CHATGPT'S RECOMMENDATIONS

ChatGPT's audit is excellent. The scalp engine idea is directionally right but
**not feasible on the current infrastructure**. Here's the reality:

| Scalp requirement | What bot has | Gap |
|-------------------|-------------|-----|
| Real-time price data | 30s cached polls, 15min cycles | FATAL |
| Sub-second execution | 60s passive limit orders + 3 reprices | FATAL |
| Volume acceleration | No real-time volume data | FATAL |
| Fast exit (seconds) | 15-minute cycle check | FATAL |

A scalp engine that "buys at 8% and sells at 12%" needs WebSocket streaming,
aggressive limit orders, and sub-second reaction time. That's a different bot
entirely — probably written in Rust or Go, not Python on Railway.

### What IS correct from ChatGPT:
1. The bot needs more than "predict and hold to resolution"
2. Cheap bins are often mispriced — there's edge there
3. Execution strategy matters as much as prediction
4. We need multiple trade types, not one-size-fits-all

### What we can actually build today:
An **enhanced hold-and-exit system** that exploits the same price-movement
insight but on a 15-minute cycle instead of seconds.

---

## THE PLAN: 4 WORKSTREAMS IN PARALLEL

### STREAM 1: Deploy the Calibration Fix (30 min — BLOCKING)

**Owner**: User (requires Railway dashboard access)
**Status**: Code is pushed, Railway needs manual redeploy

Steps:
1. Go to Railway dashboard → WeatherEdge service
2. Click "Deploy" or trigger redeploy from latest commit
3. Watch logs for new boot_id
4. Verify: `curl .../api/health` shows new boot_id
5. Verify: `curl .../api/trades/latest_db_main?n=1` — next trades should have calibration_v=2
6. Monitor for 1 hour — exact-bin probabilities should cap at 45%

### STREAM 2: Adjacent-Bin Error Analysis Endpoint (2-3 hours)

**Owner**: Claude (code change)
**File**: api_server.py — new endpoint `/api/stats/bin_errors`

Purpose: For every resolved main_loop trade, compare predicted bin vs actual
winning bin. This tells us whether losses are calibration errors (fixable) or
forecast errors (fundamental problem).

Logic:
```
For each resolved trade:
  1. Parse question → extract city, date, threshold_c, direction
  2. Query Gamma for that event's resolved outcome (winning bin)
  3. Compute: error = actual_winning_bin - predicted_bin (in °C)
  4. Classify: HIT (0), ADJACENT (±1°C), NEAR (±2°C), WILD (>2°C)
  5. Store per city, per direction type
```

Output:
```json
{
  "total_resolved": 41,
  "hit": 10, "adjacent": 15, "near": 8, "wild": 8,
  "by_city": {"tokyo": {"hit":2, "adjacent":3, ...}, ...},
  "avg_error_c": 1.2,
  "systematic_bias": {"tokyo": -0.5, "seattle": +1.1}
}
```

### STREAM 3: Smart Exit Upgrade — "Momentum Exit" (3-4 hours)

**Owner**: Claude (code change)
**Files**: api_server.py, active_trader.py

This is the REALISTIC version of ChatGPT's scalp idea. Instead of scalping
(which needs real-time data), we add a momentum-aware exit layer that checks
every 15-minute cycle:

**New exit signal: MOMENTUM_EXIT**
```
For each open position:
  1. Get current market price from CLOB
  2. Compare to entry price
  3. If current_price >= entry_price * 1.5:  → SELL_ALL (50% profit)
  4. If current_price >= entry_price * 1.3:  → SELL_HALF (lock in some profit)
  5. If time_remaining < 2 hours AND price < entry * 0.8: → SELL_ALL (cut loss)
  6. If time_remaining < 30 min: → SELL_ALL (don't hold through resolution on thin edge)
```

Why this works on 15-min cycles:
- Weather markets move slowly (hours, not seconds)
- A bin that goes from 8¢ to 15¢ over 2 hours is visible on 15-min polls
- We don't need to catch the exact top — just the trend

What this gives us:
- Locks in profits before resolution risk
- Cuts losers before they go to zero
- Generates "flow-like" returns without needing real-time data

### STREAM 4: Above/Below Strategy Shift (2 hours)

**Owner**: Claude (code change)
**Files**: api_server.py (probability + trade filtering)

ChatGPT correctly identified that exact-bin trading is inherently hard (best case
30-45% WR). Above/below bins are much easier to get right (60%+ possible).

Changes:
1. Lower min_ev for above/below trades from 5pp to 3pp
2. Keep min_ev for exact bins at 5pp (or raise to 8pp)
3. Track above/below vs exact separately in postfix_cohort
4. Add direction to the cohort tracking in /api/stats/reliability

This is a strategic pivot, not a complexity addition. We're saying:
"We're better at directional forecasting than pinpoint forecasting."

---

## IMPLEMENTATION ORDER

```
Day 1 (TODAY):
  ├── [USER] Stream 1: Deploy calibration fix to Railway
  └── [CLAUDE] Stream 2: Build /api/stats/bin_errors endpoint

Day 2-3:
  ├── [CLAUDE] Stream 3: Smart exit upgrade (momentum exit)
  └── [CLAUDE] Stream 4: Above/below strategy shift

Day 4-7:
  └── OBSERVE — collect 100+ post-fix trades

Day 7:
  └── REVIEW — analyze bin_errors, cohort data, momentum exits
```

---

## WHAT ABOUT THE SCALP ENGINE LONG-TERM?

If the bot proves profitable on prediction + smart exits, THEN we can consider
a true scalp engine. But that requires:

1. WebSocket connection to Polymarket CLOB (real-time price stream)
2. Aggressive order placement (not passive limit orders)
3. Sub-minute cycle time (dedicated event loop, not Flask timer)
4. Volume tracking infrastructure (tick-by-tick or 1-second aggregates)
5. Probably a separate service (Node.js or Go for speed)

That's a V3 project, not a V2 add-on. Current priority: make V2 profitable first.

---

## SUCCESS CRITERIA

### Week 1 (post-fix)
- [ ] Calibration fix deployed and verified
- [ ] 50+ post-fix trades accumulated
- [ ] Adjacent-bin error analysis available
- [ ] Momentum exit generating at least some early exits

### Week 2
- [ ] 100+ post-fix trades with resolution data
- [ ] Post-fix win rate measured (target: >40% exact, >55% above/below)
- [ ] Adjacent-bin data shows whether problem is sigma or forecast
- [ ] Above/below trades showing better results than exact

### Week 3
- [ ] Decision point: is the prediction engine viable?
- [ ] If yes: calibrate from empirical data, expand
- [ ] If no: pivot to above/below only + smart exits
- [ ] Live readiness assessment

---

## FUTURE: SCALP ENGINE V1 (ONLY AFTER V2 PROFITABLE)

If we build a scalp engine later, it would be a SEPARATE SERVICE:

```
weatheredge-scalper/
  ├── ws_client.py      # WebSocket to CLOB
  ├── price_tracker.py  # Real-time price + volume state
  ├── scalp_engine.py   # Entry/exit logic
  ├── order_manager.py  # Aggressive limit order placement
  └── risk.py           # Position limits, PnL tracking
```

Tech: asyncio Python or Node.js, deployed separately on Railway.
NOT integrated into the current api_server.py Flask app.
