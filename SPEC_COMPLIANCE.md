# Spec Compliance Table & Data Contract
## Frozen Max-Edge March 2026 Blueprint

---

## A. Spec Compliance Table

| # | Requirement | Module File | Key Functions | Key Data Fields Logged |
|---|-------------|-------------|---------------|----------------------|
| 1 | Station integrity & no-trade confidence | `station_parser.py` | `parse_station(rules_text) -> StationResult`, `compute_confidence(icao, url, keywords, city) -> int`, `validate_on_hash_change(rules_hash, cached_hash)` | `station_icao`, `station_url`, `confidence_tier` (0/0.5/1/2/3), `no_trade_reason`, `rules_hash` |
| 2 | Frozen model snapshot per trade_group_id | `ledger_telemetry.py`, `probability_calculator.py` | `freeze_snapshot(trade_group_id, true_prob_vec, model_run_ts, forecast_inputs_hash) -> FrozenSnapshot`, `load_frozen_snapshot(trade_group_id) -> FrozenSnapshot` | `trade_group_id`, `true_prob_vector` (JSON array), `model_run_timestamp`, `forecast_inputs_hash`, `frozen_at` |
| 3 | theoretical_full_ev as primary exit & gate | `risk_manager.py` | `compute_theoretical_full_ev(legs, cost_proxy) -> float`, `compute_cost_proxy(fill_prob, aggressiveness, depth, rel_spread) -> float`, `check_ev_gates(theo_ev, hours_to_res) -> bool`, `should_auto_flatten(theo_ev) -> bool` | `theoretical_full_ev`, `cost_proxy`, `effective_roundtrip_bps`, `expected_slippage_proxy`, `gate_result`, `flatten_trigger` |
| 4 | Diurnal staging gates | `time_utils.py` | `get_peak_window(lat, coastal) -> (start_hour, end_hour)`, `get_diurnal_stage(now_local, peak_window) -> str`, `apply_diurnal_constraints(stage, theo_ev, kelly_size) -> DiurnalDecision` | `diurnal_stage` (pre-peak/near-peak/post-peak), `peak_window_start`, `peak_window_end`, `obs_max`, `size_cap_applied`, `entry_blocked_reason` |
| 5 | Nowcasting in last 24h | `nowcasting.py` | `nowcast_distribution(obs_now, mu_now, mu_forecast_hourly, sigma_hourly, obs_max_so_far, hours_remaining, regime, n_samples=5000) -> BinProbabilities`, `compute_mu_adj(mu_h, offset, h, half_life) -> float`, `ar1_residuals(rho, sigma_h, n_hours) -> np.array`, `observation_sanity(obs_now, mu_now, obs_ts) -> ObsSanityResult` | `obs_temp_now`, `mu_now`, `offset`, `half_life_used`, `rho_used`, `n_monte_carlo`, `obs_anomaly_flag`, `sigma_widened`, `obs_weight_reduced` |
| 6 | Regime classifier & distribution shaping | `regime_classifier.py` | `classify_regime(ensemble_spread, wind_dir_shift_prob, cloud_cover, precip_prob, coastal) -> RegimeResult`, `shape_distribution(regime, mu, sigma) -> ShapedDistParams` | `regime_detected` (front/marine/convective/clear/neutral), `regime_features` (JSON), `skew_applied`, `sigma_multiplier`, `storm_probability`, `warm_bias` |
| 7 | Cross-market consistency filter | `cross_market_filter.py` | `check_cross_market(target_market, peer_markets, season_corr_matrix) -> CrossMarketResult`, `compute_delta_zscore(delta_implied, mean_delta, std_delta) -> float` | `delta_z_score`, `peer_markets_used`, `season_corr`, `min_theo_ev_adjustment`, `flag_raised` |
| 8 | Edge decay decomposition | `ledger_telemetry.py` | `record_edge_decay(leg_id, mid_at_decision, mid_at_fill, fill_price, best_bid, best_ask) -> None`, `compute_decay_metrics(trade_group_id) -> DecayMetrics`, `bucket_decay(burst_override, signal_age, fill_type) -> str` | `mid_price_at_decision`, `mid_price_at_fill_time`, `fill_price`, `best_bid_at_fill`, `best_ask_at_fill`, `adverse_move_pct`, `spread_paid_pct`, `decay_bucket` |
| 9 | Execution & fill quality | `trader_execution.py` | `place_passive_limit(token_id, price, size, book_snapshot) -> OrderResult`, `compute_fill_prob(rel_spread, depth, recent_fill_rate) -> float`, `compute_size_cap(depth, theo_ev) -> float`, `order_lifecycle(order_id, max_time_in_book, max_reprices) -> FillResult` | `fill_prob_proxy`, `size_cap_used`, `depth_at_entry`, `time_in_book_s`, `reprice_count`, `cancel_reason`, `fill_type` (maker/taker), `fill_completion_ratio_60s` |
| 10 | min_theo_ev gate & dynamic ratchet | `risk_manager.py` | `compute_min_theo_ev(base, liquidity_adj, time_to_res_adj, burst_adj, leakage_ratchet) -> float`, `update_leakage_ratchet(rolling_leakage_bps, baseline_bps) -> float` | `min_theo_ev_applied`, `base_min_ev`, `liquidity_adj`, `time_to_res_adj`, `burst_adj`, `leakage_ratchet_increment` |
| 11 | Alerts & logging | `ledger_telemetry.py` | `check_alerts(metrics) -> list[Alert]`, `log_no_trade_histogram(reason, is_burst) -> None`, `log_burst_context(snapshot) -> None` | `alert_type`, `alert_value`, `no_trade_reason_histogram` (JSON), `burst_override_context` (JSON), `signal_age_minutes` |
| 12 | Sanity checks & fallbacks | `station_parser.py`, `risk_manager.py` | `wu_metar_sanity_check(station, last_30d_wu, last_30d_metar) -> SanityResult` | `wu_metar_avg_diff_c`, `sanity_risk_level`, `min_theo_ev_boost`, `skip_market` |
| 13 | Uncertainty = prob std dev everywhere | `probability_calculator.py` | `kde_with_uncertainty(samples, lo, hi, n_resamples=200) -> (p, u_prob)`, `bayesian_smoothing(k, n, prior_a, prior_b) -> (p, u_prob)` | `p_estimate`, `u_prob` (probability std dev, [0, 0.5]), `estimation_method`, `n_resamples` |
| 14 | Consensus blending (weighted total variance) | `probability_calculator.py` | `consensus_blend(sources: list[tuple[p,u,w]]) -> (p_blend, u_blend)` | `p_blend`, `u_blend`, `source_weights`, `source_probs`, `source_uncertainties` |
| 15 | Backtest time-causal (no lookahead) | `scheduler.py` (backtest mode) | `compute_t_entry(market) -> datetime`, `enforce_causality(data, t_entry) -> CausalData`, `mark_signal_only(has_book) -> bool` | `t_entry`, `forecast_run_ts_used`, `obs_ts_used`, `book_snapshot_ts`, `signal_only_flag` |
| 16 | NO handling correct & consistent | `trader_execution.py`, `probability_calculator.py` | `normalize_to_yes_representation(side, price) -> (token_id, adj_price)`, `compute_payout_yes_only(fill_price, outcome) -> float` | `representation` (always YES unless impossible), `original_side`, `token_id_used`, `payout_logic` |
| 17 | No silent fallbacks | `ledger_telemetry.py` | `log_fallback(source, target, market_id, reason) -> None`, `increment_fallback_counter(fallback_type) -> None` | `fallback_type`, `fallback_count`, `market_id`, `reason`, `timestamp` |
| 18 | CLOB depth impact (no LMSR) | `trader_execution.py` | `walk_book_levels(book_snapshot, size, side) -> (avg_price, slippage)`, `maker_fill_prob(rel_spread, depth, fill_rate) -> float` | `slippage_estimate`, `levels_walked`, `depth_available`, `impact_method` (always "CLOB") |
| 19 | Non-regression tests | `tests/test_non_regression.py` | `test_ev_per_dollar_yes()`, `test_kde_integrate_box()`, `test_bayesian_no_extremes()`, `test_consensus_total_variance()` | (test pass/fail) |
| 20 | Reporting sanity | `ledger_telemetry.py` | `compute_sharpe(daily_returns) -> float`, `compute_win_rate(trade_groups) -> float` | `sharpe_annualized` (daily × sqrt(252)), `win_rate_trade_group_level`, `total_trade_groups`, `winning_groups` |
| 21 | Fee and rebate awareness | `fee_client.py` | `get_fees_enabled(market) -> bool`, `fetch_fee_rate_bps(token_id) -> int`, `check_maker_rebates(address) -> float`, `log_fee_info(leg_id, fees_enabled, fee_rate, realized_fees)` | `fees_enabled`, `fee_rate_bps_used`, `realized_fees_paid`, `maker_rebate_amount` |

---

## B. Data Contract

### B.1 trade_group_id Schema

A `trade_group_id` uniquely identifies one decision cycle for one market at one point in time.

```
trade_group_id: str = "{market_slug}_{timestamp_utc_iso}_{short_hash}"
Example: "will-nyc-temp-hit-75f-2026-03-24_2026-03-23T14:00:00Z_a3f0"
```

### B.2 Leg Schema

Each trade_group contains 1+ legs (one per bin/token traded).

```python
@dataclass
class Leg:
    leg_id: str                    # "{trade_group_id}_leg{N}"
    trade_group_id: str
    token_id: str                  # Polymarket CLOB token ID
    market_id: str                 # Polymarket condition_id
    bin_label: str                 # e.g. "72-73°F"
    side: str                      # "YES" (always, per #16)
    entry_price: float             # fill price in [0, 1]
    size: float                    # number of contracts
    true_prob: float               # from frozen snapshot
    u_prob: float                  # probability std dev
    fill_prob_proxy: float
    depth_at_entry: float
    mid_price_at_decision: float
    mid_price_at_fill_time: float
    fill_price: float
    best_bid_at_fill: float
    best_ask_at_fill: float
    time_in_book_s: float
    reprice_count: int
    fill_type: str                 # "maker" or "taker"
    fees_enabled: bool
    fee_rate_bps_used: int
    realized_fees_paid: float
    adverse_move_pct: float        # computed post-fill
    spread_paid_pct: float         # computed post-fill
    timestamp_utc: str
```

### B.3 Frozen Snapshot Schema

Persisted at entry time. All subsequent calculations for this trade_group use ONLY this snapshot.

```python
@dataclass
class FrozenSnapshot:
    trade_group_id: str
    true_prob_vector: list[float]    # one per bin, JSON serialized
    u_prob_vector: list[float]       # uncertainty per bin
    bin_labels: list[str]
    model_run_timestamp: str         # ISO UTC
    forecast_inputs_hash: str        # SHA256 of (forecast data + obs data + regime)
    regime_detected: str
    regime_features: dict            # JSON: ensemble_spread, wind_dir, cloud, precip
    obs_temp_at_freeze: float | None
    obs_max_at_freeze: float | None
    diurnal_stage: str
    peak_window_start: int           # local hour
    peak_window_end: int             # local hour
    station_icao: str
    station_confidence: int
    theoretical_full_ev: float
    cost_proxy: float
    min_theo_ev_applied: float
    frozen_at: str                   # ISO UTC timestamp
```

### B.4 Observation Record Schema

```python
@dataclass
class ObservationRecord:
    station_icao: str
    obs_timestamp: str             # ISO UTC
    temp_c: float
    temp_f: float
    source: str                    # "METAR" or "WU"
    obs_anomaly_flag: bool         # |T_obs - mu| > 6°C or stale > 45min
    sigma_widened: bool
    obs_weight_reduced: bool
```

### B.5 Order Book Snapshot Schema

```python
@dataclass
class BookSnapshot:
    token_id: str
    timestamp_utc: str
    best_bid: float
    best_ask: float
    mid_price: float
    spread: float
    relative_spread: float         # spread / mid_price
    bid_depth_top3: float          # total size in top 3 bid levels
    ask_depth_top3: float          # total size in top 3 ask levels
    total_bid_depth: float
    total_ask_depth: float
    recent_fill_rate: float        # fills per minute, trailing 5 min
    levels: list[dict]             # [{price, size, side}, ...]
```

### B.6 SQLite Ledger Schema

```sql
-- Core trade group table
CREATE TABLE IF NOT EXISTS trade_groups (
    trade_group_id TEXT PRIMARY KEY,
    market_slug TEXT NOT NULL,
    market_id TEXT NOT NULL,
    station_icao TEXT,
    station_confidence INTEGER,
    regime_detected TEXT,
    regime_features TEXT,            -- JSON
    diurnal_stage TEXT,
    peak_window_start INTEGER,
    peak_window_end INTEGER,
    theoretical_full_ev REAL,
    cost_proxy REAL,
    min_theo_ev_applied REAL,
    obs_temp_at_entry REAL,
    obs_max_at_entry REAL,
    time_to_resolution_h REAL,
    signal_age_minutes REAL,
    is_burst_override INTEGER DEFAULT 0,
    burst_context TEXT,              -- JSON
    flatten_trigger TEXT,
    outcome TEXT,                    -- "win", "loss", "pending", "flattened"
    pnl REAL,
    fees_total REAL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

-- Individual legs
CREATE TABLE IF NOT EXISTS legs (
    leg_id TEXT PRIMARY KEY,
    trade_group_id TEXT NOT NULL REFERENCES trade_groups(trade_group_id),
    token_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    bin_label TEXT NOT NULL,
    side TEXT NOT NULL DEFAULT 'YES',
    entry_price REAL NOT NULL,
    size REAL NOT NULL,
    true_prob REAL NOT NULL,
    u_prob REAL NOT NULL,
    fill_prob_proxy REAL,
    depth_at_entry REAL,
    mid_price_at_decision REAL,
    mid_price_at_fill_time REAL,
    fill_price REAL,
    best_bid_at_fill REAL,
    best_ask_at_fill REAL,
    time_in_book_s REAL,
    reprice_count INTEGER DEFAULT 0,
    fill_type TEXT,
    fees_enabled INTEGER,
    fee_rate_bps_used INTEGER,
    realized_fees_paid REAL DEFAULT 0,
    adverse_move_pct REAL,
    spread_paid_pct REAL,
    decay_bucket TEXT,
    timestamp_utc TEXT NOT NULL
);

-- Frozen snapshots (one per trade group)
CREATE TABLE IF NOT EXISTS frozen_snapshots (
    trade_group_id TEXT PRIMARY KEY REFERENCES trade_groups(trade_group_id),
    true_prob_vector TEXT NOT NULL,    -- JSON array
    u_prob_vector TEXT NOT NULL,       -- JSON array
    bin_labels TEXT NOT NULL,          -- JSON array
    model_run_timestamp TEXT NOT NULL,
    forecast_inputs_hash TEXT NOT NULL,
    regime_detected TEXT,
    regime_features TEXT,              -- JSON
    obs_temp_at_freeze REAL,
    obs_max_at_freeze REAL,
    diurnal_stage TEXT,
    peak_window_start INTEGER,
    peak_window_end INTEGER,
    station_icao TEXT,
    station_confidence INTEGER,
    theoretical_full_ev REAL,
    cost_proxy REAL,
    min_theo_ev_applied REAL,
    frozen_at TEXT NOT NULL
);

-- Observation log
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_icao TEXT NOT NULL,
    obs_timestamp TEXT NOT NULL,
    temp_c REAL NOT NULL,
    temp_f REAL NOT NULL,
    source TEXT NOT NULL,
    obs_anomaly_flag INTEGER DEFAULT 0,
    sigma_widened INTEGER DEFAULT 0,
    obs_weight_reduced INTEGER DEFAULT 0,
    recorded_at TEXT NOT NULL
);

-- Order book snapshots
CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    best_bid REAL,
    best_ask REAL,
    mid_price REAL,
    spread REAL,
    relative_spread REAL,
    bid_depth_top3 REAL,
    ask_depth_top3 REAL,
    total_bid_depth REAL,
    total_ask_depth REAL,
    recent_fill_rate REAL,
    levels TEXT                        -- JSON
);

-- No-trade histogram
CREATE TABLE IF NOT EXISTS no_trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug TEXT NOT NULL,
    reason TEXT NOT NULL,
    is_burst INTEGER DEFAULT 0,
    station_confidence INTEGER,
    theo_ev REAL,
    min_theo_ev REAL,
    details TEXT,                      -- JSON
    timestamp_utc TEXT NOT NULL
);

-- Fallback log (#17)
CREATE TABLE IF NOT EXISTS fallback_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fallback_type TEXT NOT NULL,
    source_method TEXT NOT NULL,
    target_method TEXT NOT NULL,
    market_id TEXT,
    reason TEXT,
    timestamp_utc TEXT NOT NULL
);

-- Alert log
CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,
    alert_value REAL,
    threshold REAL,
    trade_group_id TEXT,
    market_slug TEXT,
    details TEXT,                      -- JSON
    timestamp_utc TEXT NOT NULL
);

-- Fee & rebate log (#21)
CREATE TABLE IF NOT EXISTS fee_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    leg_id TEXT REFERENCES legs(leg_id),
    fees_enabled INTEGER,
    fee_rate_bps_used INTEGER,
    realized_fees_paid REAL,
    timestamp_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rebate_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    rebate_amount REAL,
    check_timestamp TEXT NOT NULL
);

-- Sanity check log (#12)
CREATE TABLE IF NOT EXISTS sanity_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_icao TEXT NOT NULL,
    wu_metar_avg_diff_c REAL,
    risk_level TEXT,
    min_theo_ev_boost REAL,
    skip_market INTEGER DEFAULT 0,
    check_date TEXT NOT NULL
);

-- Daily reporting (#20)
CREATE TABLE IF NOT EXISTS daily_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL,
    sharpe_annualized REAL,
    win_rate_group_level REAL,
    total_trade_groups INTEGER,
    winning_groups INTEGER,
    total_pnl REAL,
    total_fees REAL,
    details TEXT,                      -- JSON
    created_at TEXT NOT NULL
);

-- Cross-market filter log (#7)
CREATE TABLE IF NOT EXISTS cross_market_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_group_id TEXT,
    target_market TEXT,
    peer_markets TEXT,                 -- JSON
    delta_z_score REAL,
    season_corr REAL,
    min_theo_ev_adjustment REAL,
    flag_raised INTEGER DEFAULT 0,
    timestamp_utc TEXT NOT NULL
);
```

### B.7 Key Invariants

1. **Frozen snapshot immutability**: Once `frozen_snapshots` row is inserted for a `trade_group_id`, it is NEVER updated. All edge/decay calculations for that group read from this row.

2. **YES-only representation**: `legs.side` is always "YES". NO positions are expressed by trading the complementary YES token.

3. **Uncertainty unit**: `u_prob` in `legs` and `u_prob_vector` in `frozen_snapshots` are always probability standard deviations in [0, 0.5]. Never bandwidth ratios.

4. **Causality**: In backtest mode, `frozen_snapshots.model_run_timestamp <= t_entry` and `observations.obs_timestamp <= t_entry` are enforced constraints.

5. **Fee awareness**: `legs.fee_rate_bps_used` reflects the rate fetched at order-sign time. `cost_proxy` in `trade_groups` includes fees only when `fees_enabled = 1`.

6. **No silent fallbacks**: Every fallback writes to `fallback_log` before proceeding.

7. **theoretical_full_ev formula**:
   ```
   theo_ev = SUM_i [ size_i * ( true_prob_i * (1/entry_price_i - 1) - (1 - true_prob_i) ) ] - cost_proxy
   ```
   Where cost_proxy = effective_roundtrip_bps_cost + slippage_proxy.
