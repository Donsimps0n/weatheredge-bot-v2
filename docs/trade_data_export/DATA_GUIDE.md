# WeatherEdge Bot v2 — Trade Data Export Guide

**Exported:** 2026-04-11
**Purpose:** Complete trade data for external audit. Feed these CSVs to ChatGPT alongside BOT_ENCYCLOPEDIA.md and FORENSIC_AUDIT.md.

---

## File Inventory

### Core Trade Data

| File | Rows | Description |
|------|------|-------------|
| `combined_all_trades.csv` | 468 | **START HERE.** All trades ever placed, deduplicated across all databases. Includes `source_db` column to identify origin. |
| `trades_current.csv` | 261 | Trades from current ledger.db (post-strategy-rewrite, Apr 3 onwards). Includes F-Strict pilot + NO_HARVEST V2. |
| `trades_old_pre_rewrite.csv` | 206 | Trades from the original "good period" (Mar 27-30). This is where the $242 paper profit came from (before it collapsed). |
| `trades_pre_fix.csv` | 1 | Single trade from a brief pre-fix snapshot. |

### Trade Columns (all trade CSVs)

- `id, ts` — Row ID and UTC timestamp
- `question` — Full Polymarket market question text
- `city` — City name
- `signal` — Signal type: BUY_YES, NO_HARVEST, EXIT_SELL_ALL, ABOVE_BELOW, etc.
- `token_id` — Polymarket CLOB token identifier
- `price` — Entry price (0.00-1.00 scale, where 1.00 = $1)
- `size` — Number of shares/contracts
- `spend` — Total dollars spent (price * size)
- `ev` — Expected value percentage claimed by model
- `ev_dollar` — Expected value in dollars
- `kelly` — Kelly criterion fraction
- `our_prob` — Model's claimed probability (0-100 scale)
- `mkt_price` — Market price at time of trade
- `sigma` — Forecast uncertainty (degrees C)
- `mode` — PAPER or LIVE
- `clob_spread, clob_edge_at_fill` — Order book metrics at trade time
- `resolved` — "yes" or NULL
- `resolution_price` — 1.0 (YES won) or 0.0 (NO won)
- `pnl` — Profit/loss in dollars
- `won` — 1 (win) or 0 (loss)
- `resolved_at` — UTC timestamp of resolution
- `meta` — JSON blob with extra signal metadata
- `source_db` — (combined only) Which database the row came from

### Cycle Logs

| File | Rows | Description |
|------|------|-------------|
| `cycles_current.csv` | 8105 | Every 15-min trading cycle since rewrite. Shows signals found, trades placed, top city/EV per cycle. |
| `cycles_old_pre_rewrite.csv` | 1701 | Cycles from the original period. |

### Forecast Accuracy

| File | Rows | Description |
|------|------|-------------|
| `accuracy_predictions.csv` | 888 | Every prediction the model logged. Includes city, condition_id, question, direction, threshold, our_prob, market_price, forecast data. **NOTE: 0 resolutions were ever backfilled — the calibration loop was dead.** |

### Station Data

| File | Rows | Description |
|------|------|-------------|
| `station_bias_observations.csv` | 2096 | Per-observation forecast vs actual temperature comparisons. Columns: station, city, date, forecast_temp_f, resolved_temp_f, forecast_error_f, market_error_f, bin_question, bin_outcome, our_prob, market_price. |
| `station_bias_summary.csv` | 37 | Per-station aggregate bias stats: mean/median error, std, warm/cold bias percentages. |
| `station_reliability.csv` | 5 | Station RMSE data (only 5 stations have reliability data). |
| `historical_resolved_temps.csv` | 2139 | Historical resolved temperatures by city and date from Weather Underground. Useful for backtesting. |

### Summary

| File | Rows | Description |
|------|------|-------------|
| `summary_stats.csv` | 3 | Aggregate stats for old, current, and combined datasets: total trades, win rate, PnL, spend, signal breakdown, top cities. |

---

## Quick Start for ChatGPT Audit

1. Upload `combined_all_trades.csv` + `summary_stats.csv` first
2. Ask ChatGPT to analyze win rates by signal type, city, price bucket, and time period
3. Upload `accuracy_predictions.csv` to examine calibration (predicted prob vs actual outcome)
4. Upload `station_bias_observations.csv` to check forecast accuracy per station
5. Upload `historical_resolved_temps.csv` for backtesting analysis
6. Reference `BOT_ENCYCLOPEDIA.md` for system architecture and `FORENSIC_AUDIT.md` for prior findings
