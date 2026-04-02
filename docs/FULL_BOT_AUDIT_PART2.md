# WeatherEdge Bot V2 — Code Audit Part 2: Core Trading Pipeline

## FILE 1: api_server.py (4,270 lines) — Main Flask App

### 1A. Per-City Sigma Calibration (lines 1131-1210)

These constants control the Gaussian CDF fallback and set the uncertainty envelope for each city.

```python
_CITY_SIGMA: dict = {
    # city_name_lower: (sigma_tomorrow_c, sigma_today_c)
    # ── US cities (RMSE-calibrated) ──
    "miami":          (3.4, 2.1),   # RMSE 2.0F, std7 4.0F → keep 3.4
    "los angeles":    (3.4, 2.1),   # low n, keep conservative
    "seattle":        (2.3, 1.5),   # RMSE 1.9F — tight, keep
    "chicago":        (2.8, 1.8),   # RMSE 3.4F → sig_floor 2.2, spring volatile
    "atlanta":        (2.5, 1.6),   # RMSE 3.2F → sig_floor 2.1
    "dallas":         (2.8, 1.8),   # RMSE 3.3F, outlier_rate 9.6% — wide
    "new york":       (2.6, 1.7),   # RMSE 3.6F (n=425) → sig_floor 2.4
    # ── Canada ──
    "toronto":        (6.0, 3.7),   # extreme seasonal swings — must stay wide
    # ── LATAM ──
    "buenos aires":   (2.6, 1.7),   # RMSE 3.8F → sig_floor 2.5 (was 2.4)
    "mexico city":    (2.3, 1.5),
    # ── Europe ──
    "london":         (1.8, 1.2),   # RMSE 2.1F (n=429) — tightest reliable data
    "munich":         (1.8, 1.2),   # RMSE 1.9F (n=24)
    "paris":          (1.8, 1.2),   # RMSE 2.1F (n=43)
    "milan":          (2.4, 1.6),   # RMSE 3.3F (n=12, watch)
    "warsaw":         (2.0, 1.3),   # RMSE 2.8F (n=12)
    # ── Middle East ──
    "ankara":         (2.3, 1.5),   # RMSE 2.5F (n=66), p95=5.0F — keep
    "tel aviv":       (1.8, 1.2),   # RMSE 1.9F (n=19) — tight
    # ── Asia ──
    "singapore":      (1.5, 1.0),   # RMSE 1.1F (n=16) — most stable globally
    "tokyo":          (1.8, 1.2),   # RMSE 1.6F (n=19)
    "seoul":          (2.2, 1.5),   # RMSE 2.9F (n=113) — sig_floor 1.95
    "shanghai":       (1.8, 1.2),   # RMSE 1.9F (n=16)
    "taipei":         (4.0, 2.6),   # RMSE 5.4F, outlier_rate 25% — very wide
    "hong kong":      (4.0, 2.6),   # RMSE 5.4F, outlier_rate 33% — very wide
    # ── Oceania ──
    "wellington":     (2.0, 1.3),   # RMSE 2.7F (n=66), p95=4.6F
    # ── Global fallback ──
    "__default__":    (2.5, 1.8),
}

# Per-city bias correction (°C). Source: station_reliability_report_v3.xlsx
# Sign: positive = OM overestimates WU → subtract from ftemp
_CITY_BIAS_C: dict = {
    "los angeles":   +1.17,  # ERA5 +2.1F high vs KLAX (n=7)
    "taipei":        +1.15,  # ERA5 +2.1F high (n=12)
    "buenos aires":  -1.64,  # ERA5 −3.0F low vs WU (n=113)
    "new york":      -1.19,  # ERA5 −2.1F low vs WU (n=425)
    "wellington":    -1.18,  # ERA5 −2.1F low vs WU (n=66)
}

def _city_sigma(city_name: str, is_tomorrow: bool) -> float:
    key = city_name.lower().strip()
    pair = _CITY_SIGMA.get(key, _CITY_SIGMA["__default__"])
    return pair[0] if is_tomorrow else pair[1]
```

---

### 1B. Probability Computation — Two Paths (lines 1240-1330)

Every market gets `our_prob` computed via one of two paths: ensemble (primary) or static Gaussian (fallback).

```python
        # Determine if this is a tomorrow or today market
        _dm = _re.search(r'(?:march|april|may|...)\s+(\d+)', _q_lower)
        if _dm:
            _qday = int(_dm.group(1))
            _is_tomorrow = _qday != _today.day

        # ── PRIMARY: Ensemble-based probability (82 members + 7 models) ──
        _used_ensemble = False
        _ens_fc = None
        _bias_c = _bias_agent.get_correction_c(p["city"]) if HAS_BIAS_AGENT else _CITY_BIAS_C.get(p["city"].lower(), 0.0)
        if _ENSEMBLE_AVAILABLE:
            try:
                _coords = _CITY_COORDS.get(p["city"].lower())
                if _coords:
                    _ens_prob, _ens_fc = _ensemble_prob(
                        city=p["city"],
                        lat=_coords[0], lon=_coords[1],
                        threshold_c=p["threshold_c"],
                        direction=p["direction"],
                        is_tomorrow=_is_tomorrow,
                        timezone="auto",
                        bias_correction_c=_bias_c,
                    )
                    our_prob = round(_ens_prob * 100, 1)
                    our_prob = max(0.1, min(99.9, our_prob))
                    # Hard cap: exact bins can't exceed 45% (calibration guard)
                    if p["direction"] == "exact":
                        our_prob = min(our_prob, 45.0)
                    ftemp = _ens_fc.ensemble_mean if _ens_fc.n_ensemble_members > 0 else _ens_fc.multimodel_mean
                    sigma = _ens_fc.blended_sigma
                    _used_ensemble = True
            except Exception as _ens_err:
                logger.warning("Ensemble failed for %s, falling back to static sigma: %s", p["city"], _ens_err)

        # ── FALLBACK: Static sigma Gaussian CDF (old method) ──
        if not _used_ensemble:
            if wx:
                if _is_tomorrow and wx.get("temp_max_tomorrow") is not None:
                    ftemp = wx["temp_max_tomorrow"]
                    sigma = _city_sigma(p["city"], True)
                elif wx.get("temp_max") is not None:
                    ftemp = wx["temp_max"]
                    sigma = _city_sigma(p["city"], False)
                else:
                    ftemp = wx.get("temp", 20)
                    sigma = _city_sigma(p["city"], _is_tomorrow)
            else:
                ftemp = 20.0
                sigma = _city_sigma(p["city"], _is_tomorrow)

            # Anchor same-day forecast with real NWS observation if available
            if not _is_tomorrow and ACTIVE_TRADER_AVAILABLE:
                try:
                    import active_trader as _at
                    _obs_f = _at.get_obs_temp_f(p["city"])
                    if _obs_f is not None:
                        _obs_c = (_obs_f - 32) * 5.0 / 9.0
                        _now_hour = (datetime.now(timezone.utc).hour - 5) % 24
                        _max_f = _at.max_achievable_today(_obs_f, _now_hour)
                        _max_c = (_max_f - 32) * 5.0 / 9.0
                        ftemp = _obs_c * 0.4 + min(_max_c, ftemp) * 0.6
                        sigma = max(_city_sigma(p["city"], False), sigma * 0.7)
                except Exception:
                    pass

            # Apply per-city bias correction
            if _bias_c != 0.0:
                ftemp -= _bias_c

            # Gaussian CDF probability
            if p["direction"] == "exact":
                z_hi = (p["threshold_c"] + 0.5 - ftemp) / sigma
                z_lo = (p["threshold_c"] - 0.5 - ftemp) / sigma
                our_prob = round((_ncdf(z_hi) - _ncdf(z_lo)) * 100, 1)
            elif p["direction"] == "above":
                z = (ftemp - p["threshold_c"]) / sigma
                our_prob = round(_ncdf(z) * 100, 1)
            else:
                z = (p["threshold_c"] - ftemp) / sigma
                our_prob = round(_ncdf(z) * 100, 1)
            our_prob = max(0.1, min(99.9, our_prob))
            # Hard cap: no single exact bin should exceed 45%
            if p["direction"] == "exact":
                our_prob = min(our_prob, 45.0)
```

---

### 1C. Trade Execution (lines 2540-2650)

After probability and edge computation, trades are sized and recorded.

```python
            # Position sizing with multiple multipliers
            _lm_mult = float(sig.get("last_mile_multiplier", 1.0))
            _station_mult = float(sig.get("size_mult", 1.0))  # Knob D: station reliability
            _total_mult = _coord_mult * _lm_mult * float(_liquidity_mult) * _station_mult
            _base_size = cfg["max_size"]
            spend = max(1.0, _base_size * _total_mult)

            # Depth cap: don't spend more than 25% of displayed side depth
            _depth_cap_applied = False
            _spend_pre_cap = spend
            _depth_cap_value = None
            if _side_depth and _side_depth > 0:
                _depth_cap_value = round(_side_depth * 0.25, 2)
                if spend > _depth_cap_value:
                    spend = max(1.0, _depth_cap_value)
                    _depth_cap_applied = True

            _remaining_budget = max(0.0, _MAX_CITY_DAY_EXPOSURE - _current_exposure)
            spend = min(spend, _remaining_budget) if _remaining_budget < spend else spend
            size = max(1, math.ceil(spend / price))

            trade_info = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "question": sig.get("question", "")[:80],
                "city": sig.get("city", ""),
                "signal": sig_type,
                "trade_source": "main_loop",
                "token_id": token_id,
                "price": price, "size": size,
                "ev": sig.get("theo_ev", 0),
                "kelly": sig.get("kelly", 0),
                "our_prob": sig.get("our_prob", 0),
                "mkt_price": sig.get("market_price", 0),
                "ev_dollar": sig.get("ev_dollar", 0),
                "data_quality": sig.get("data_quality", "good"),
                # Rich meta for post-mortem
                "edge_pp": sig.get("theo_ev", 0),
                "depth_usd_side": _side_depth,
                "station_confidence": sig.get("confidence", None),
                "bias_correction_f": sig.get("bias_correction_f", 0),
                "bias_confidence": sig.get("bias_confidence", "none"),
                "sigma_floor_used": sig.get("sigma_floor_c", 0),
                "ev_addon_used": sig.get("ev_addon", 0),
                "min_ev_base_pp": cfg["min_ev"],
                "min_ev_final_pp": sig.get("_min_ev_adj", cfg["min_ev"]),
                "size_mult_used": sig.get("size_mult", 1.0),
                "station_rmse": sig.get("sigma", 0),
                "station_n": sig.get("bias_n_obs", 0),
                "ev_addon_reasons": sig.get("ev_addon_reasons", []),
                "depth_cap_applied": _depth_cap_applied,
                "depth_cap_value_usd": _depth_cap_value,
                "spend_pre_cap": round(_spend_pre_cap, 2),
                "meta_proof_bypass": _this_sig_is_proof_bypass,
                "boot_id": BOOT_ID,
                "calibration_v": 2,  # v2=sigma floor 1.5 + 45% cap
            }

            if cfg["paper_mode"]:
                trade_info["mode"] = "PAPER"
                _traded_tokens.add(token_id)
                _spend = price * size
                _city_day_exposure[_city_key] = _city_day_exposure.get(_city_key, 0) + _spend
                _paper_trades.append(trade_info)
                _trade_log.append(trade_info)
                # Persist to SQLite ledger
                if HAS_LEDGER:
                    try:
                        trade_ledger.record_trade(trade_info)
                    except Exception as _led_err:
                        logger.debug("Ledger write error: %s", _led_err)
```

---

### 1D. Trading Loop Entry (lines 1791-1860)

```python
def _run_auto_trade_cycle():
    """Execute one auto-trade cycle: get signals, filter, place orders."""
    global _paper_trades, _meta_proof_consumed
    cfg = _auto_trade_config
    if not _auto_trade_active:
        return

    try:
        # Auto-scan: refresh markets if empty or stale (>5 min)
        wm = _state.get("weather_markets", [])
        _last = _state.get("last_scan")
        _stale = True
        if _last:
            try:
                _age = (datetime.now(timezone.utc) - datetime.fromisoformat(_last)).total_seconds()
                _stale = _age > 300
            except Exception:
                _stale = True
        if (not wm or _stale) and HAS_GAMMA:
            try:
                raw = _gamma_get_markets()
                # ... parse markets into weather list ...
                _state["weather_markets"] = weather
                _state["last_scan"] = datetime.now(timezone.utc).isoformat()
            except Exception as _scan_err:
                logger.warning("Auto-scan failed: %s", _scan_err)

        # Auto-refresh weather cache if stale (>10 min)
        if not _weather_cache["data"] or (time.time() - _weather_cache["ts"]) > 600:
            try:
                _warm_weather_cache()
            except Exception:
                pass
```

---

### 1E. Cycle End: Resolver + Exit Engine (lines 3089-3170)

```python
        # Shadow trading: test relaxed execution gates
        _shadow_count = 0
        for _sh_sig in tradeable:
            # ... relaxed gates: $1 depth, 80% spread (vs strict $5 / 50%) ...
            # Logs SHADOW_TRADE entries for analysis
            pass

        # Persist cycle stats
        if HAS_LEDGER:
            trade_ledger.record_cycle(len(sigs), len(tradeable), traded, _top, _topev)

        # ── Trade resolver: settle open trades against Polymarket outcomes ────
        # Called every cycle but self-throttles to once per hour internally.
        try:
            from trade_resolver import resolve_trades as _resolve_trades
            _resolver_result = _resolve_trades()
            if _resolver_result.get("ran"):
                logger.info("RESOLVER_RUN: resolved=%d W=%d L=%d PnL=$%.2f",
                            _resolver_result.get("resolved_count", 0),
                            _resolver_result.get("wins", 0),
                            _resolver_result.get("losses", 0),
                            _resolver_result.get("total_pnl", 0))
                global _last_resolver_result
                _last_resolver_result = _resolver_result
        except Exception as _res_err:
            logger.warning("trade_resolver error: %s", _res_err)
```

### 1F. Bot Timer + Start Endpoint (lines 3169-3215)

```python
def _start_auto_trade_timer():
    """Run auto-trade cycle every 60 seconds in background thread."""
    import threading
    def _loop():
        while True:
            try:
                if _auto_trade_active:
                    _run_auto_trade_cycle()
            except Exception as _thread_err:
                logger.error("Auto-trade THREAD error: %s", _thread_err)
            time.sleep(60)
    t = threading.Thread(target=_loop, daemon=True)
    t.start()

@app.route("/api/bot/start", methods=["POST"])
def start_bot():
    """Start the auto-trade bot. Body: {mode: 'paper'|'live', min_ev, max_size}"""
    global _auto_trade_active
    data = request.get_json(force=True) if request.data else {}
    mode = data.get("mode", "paper").lower()
    _auto_trade_config["paper_mode"] = mode != "live"
    if "min_ev" in data:
        _auto_trade_config["min_ev"] = float(data["min_ev"])
    if "max_size" in data:
        _auto_trade_config["max_size"] = float(data["max_size"])
    if "min_kelly" in data:
        _auto_trade_config["min_kelly"] = float(data["min_kelly"])
    _auto_trade_active = True
    # Seed Bin Sniper with existing markets so it only snipes NEW ones
    # ... run first cycle immediately in thread ...
```

---

### 1G. Reliability Stats Endpoint (lines 3440-3600)

```python
@app.route("/api/stats/reliability")
def stats_reliability():
    """Per reliability bucket (clean / LOW_N / NOISY / DRIFT):
    - win_rate_pct, won, lost, pending
    - avg_ev_gate_delta_pp: how much reliability raised the EV gate
    - avg_spend_pre_cap_usd, avg_side_depth_usd
    - avg_spend_vs_depth_ratio, avg_cap_pressure_ratio
    Also returns trade_source_counts and overall totals."""

    # Read last 500 trades from SQLite
    rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 500").fetchall()

    # Classify each trade by source (main_loop / agent / exit_engine)
    # Only bucket main_loop trades by reliability flags

    # Determine bucket from ev_addon_reasons in meta JSON:
    #   [] → "clean"
    #   ["DRIFT"] → "DRIFT"
    #   ["NOISY"] → "NOISY"
    #   ["LOW_N"] → "LOW_N"

    # For each bucket: compute win_rate, depth metrics, EV gate delta

    # ── Post-fix cohort: trades with calibration_v >= 2 ──
    _postfix = {"trades": 0, "won": 0, "lost": 0, "pending": 0,
                "avg_our_prob": None, "avg_mkt_price": None, "avg_edge": None}
    # Filter trades where meta JSON contains "calibration_v" >= 2
    # Compute separate stats for post-fix cohort
```

---

## FILE 2: src/multi_model_forecast.py (687 lines) — Ensemble Probability Engine

### 2A. EnsembleForecast Dataclass

```python
@dataclass
class EnsembleForecast:
    """Container for ensemble forecast data and derived probabilities."""
    city: str
    lat: float
    lon: float
    forecast_day: int  # 0=today, 1=tomorrow

    # Raw ensemble members (°C, daily max temperatures)
    ecmwf_members: List[float] = field(default_factory=list)   # up to 51
    gfs_members: List[float] = field(default_factory=list)      # up to 31
    all_members: List[float] = field(default_factory=list)      # combined 82

    # Deterministic multi-model forecasts (°C)
    model_forecasts: Dict[str, float] = field(default_factory=dict)

    # Derived statistics
    ensemble_mean: float = 0.0
    ensemble_std: float = 0.0
    ensemble_min: float = 0.0
    ensemble_max: float = 0.0
    ensemble_p10: float = 0.0
    ensemble_p25: float = 0.0
    ensemble_p50: float = 0.0
    ensemble_p75: float = 0.0
    ensemble_p90: float = 0.0
    multimodel_mean: float = 0.0
    multimodel_std: float = 0.0
    blended_sigma: float = 2.5  # fallback
    n_ensemble_members: int = 0
    n_models: int = 0
    data_quality: str = "unknown"
```

### 2B. _compute_stats — Blended Sigma Calculation (lines 360-410)

```python
    # Blended sigma: use ensemble spread as primary, model spread as floor
    if fc.n_ensemble_members >= 10:
        iqr = fc.ensemble_p75 - fc.ensemble_p25
        iqr_sigma = iqr / 1.35  # IQR → sigma for normal distribution

        # Floor at 1.5°C: ensemble spread systematically underestimates real
        # forecast uncertainty due to shared model physics. Historical day-ahead
        # high temp RMSE is 1.5-3.0°C even when ensemble spread is < 0.5°C.
        fc.blended_sigma = max(fc.ensemble_std, iqr_sigma, 1.5)
        fc.data_quality = "good"
    elif fc.n_models >= 3:
        fc.blended_sigma = max(fc.multimodel_std * 1.3, 1.0)
        fc.data_quality = "partial"
    else:
        fc.blended_sigma = 2.5  # conservative fallback
        fc.data_quality = "fallback"
```

### 2C. ensemble_bin_probability — FULL FUNCTION (lines 410-560)

This is the core probability computation. The calibration fix is at the end.

```python
def ensemble_bin_probability(
    fc: EnsembleForecast,
    threshold_c: float,
    direction: str = "exact",
    bin_width_c: float = 0.5,
    bias_correction_c: float = 0.0,
) -> float:
    """Compute probability for a Polymarket bin using ensemble data."""
    members = fc.all_members

    # Apply bias correction
    if bias_correction_c != 0.0:
        members = [m - bias_correction_c for m in members]

    if not members:
        return _gaussian_bin_prob(
            fc.ensemble_mean or fc.multimodel_mean or 20.0,
            fc.blended_sigma, threshold_c, direction, bin_width_c)

    n = len(members)

    # ── Direct count from ensemble members ──
    if direction == "exact":
        lo = threshold_c - bin_width_c
        hi = threshold_c + bin_width_c
        count = sum(1 for m in members if lo <= m < hi)
    elif direction == "above":
        count = sum(1 for m in members if m >= threshold_c)
    else:
        count = sum(1 for m in members if m < threshold_c)

    raw_prob = count / n

    # ── Kernel-smoothed estimate (handles sparse bins) ──
    bandwidth = fc.blended_sigma * (n ** -0.2) if fc.blended_sigma > 0 else 0.5

    kernel_prob = 0.0
    for m in members:
        if direction == "exact":
            z_hi = (threshold_c + bin_width_c - m) / bandwidth
            z_lo = (threshold_c - bin_width_c - m) / bandwidth
            kernel_prob += (_ncdf(z_hi) - _ncdf(z_lo))
        elif direction == "above":
            z = (m - threshold_c) / bandwidth
            kernel_prob += _ncdf(z)
        else:
            z = (threshold_c - m) / bandwidth
            kernel_prob += _ncdf(z)
    kernel_prob /= n

    # ── Blend: 75% direct count + 25% kernel smooth (with 82 members) ──
    direct_weight = min(0.85, 0.5 + n / 200.0)
    blended = direct_weight * raw_prob + (1 - direct_weight) * kernel_prob

    # ── Multi-model cross-validation ──
    if fc.n_models >= 3 and fc.multimodel_std > 0:
        model_vals = [v - bias_correction_c for v in fc.model_forecasts.values()]
        mm_prob = _gaussian_bin_prob(
            fc.multimodel_mean - bias_correction_c,
            fc.multimodel_std, threshold_c, direction, bin_width_c)
        disagreement = abs(fc.ensemble_mean - fc.multimodel_mean) / max(fc.blended_sigma, 0.5)
        if disagreement > 1.0:
            hedge_weight = min(0.3, disagreement * 0.1)
            blended = blended * (1 - hedge_weight) + mm_prob * hedge_weight

    # ── KDE tail clamp: no probability outside member range + buffer ──
    if members and direction == "exact":
        _member_min = min(members)
        _member_max = max(members)
        _buffer = max(1.0, fc.blended_sigma * 0.5)
        if threshold_c > _member_max + _buffer or threshold_c < _member_min - _buffer:
            blended = min(blended, 0.005)

    # ── CALIBRATION GUARD (NEW): ensemble clustering ≠ forecast certainty ──
    # NWP ensembles share model physics → correlated errors → ensemble spread
    # systematically underestimates real uncertainty.
    if direction == "exact" and blended > 0.30:
        _verified_sigma = max(fc.blended_sigma, 1.5)
        _mean = sum(members) / len(members) if members else (fc.ensemble_mean or 20.0)
        _conservative_prob = _gaussian_bin_prob(
            _mean, _verified_sigma, threshold_c, direction, bin_width_c)
        _ensemble_overconfidence = max(0.0, (blended - 0.30) / 0.70)
        _conservative_weight = min(0.50, _ensemble_overconfidence * 0.50)
        _old_blended = blended
        blended = blended * (1 - _conservative_weight) + _conservative_prob * _conservative_weight
        log.info(
            "CALIBRATION_GUARD: %s bin=%.1f°C ens_prob=%.1f%% → blended=%.1f%% "
            "(conservative=%.1f%%, weight=%.0f%%, verified_sigma=%.1f)",
            fc.city, threshold_c, _old_blended * 100, blended * 100,
            _conservative_prob * 100, _conservative_weight * 100, _verified_sigma)

    # Hard cap: no single exact bin should exceed 45%
    if direction == "exact":
        blended = min(blended, 0.45)

    return max(0.001, min(0.999, blended))
```

---

## REVIEW QUESTIONS FOR PART 2

1. **Sigma values**: Are the `_CITY_SIGMA` values well-calibrated? Some have very low sample sizes (n=7 for LA, n=12 for Milan/Warsaw). Should low-n cities use the `__default__` sigma instead?

2. **Bias correction sign convention**: `_CITY_BIAS_C` positive means "OM overestimates, subtract". But `bias_agent.get_correction_c()` — does it follow the same convention? Is there a double-correction risk when both are active?

3. **Ensemble bandwidth**: Silverman's rule uses `blended_sigma * n^(-1/5)`. With n=82 and blended_sigma forced to 1.5°C, bandwidth ≈ 0.62°C. Is this appropriate, or does the 1.5°C floor make the kernel too wide?

4. **Calibration guard blending**: The progressive blending caps at 50/50 ensemble/conservative. At the extreme (blended=100%), the guard produces `0.5 * 1.0 + 0.5 * conservative`. If conservative is ~25%, result ≈ 62.5% before the 45% hard cap. The hard cap is doing the real work. Is the guard redundant?

5. **45% cap on `our_prob` in api_server.py**: The cap is applied AFTER converting to percentage (`min(our_prob, 45.0)`). But `ensemble_bin_probability` already applies `min(blended, 0.45)`. Is the double-cap intentional? They should be equivalent but it's worth confirming no rounding edge cases exist.

6. **NWS observation anchor**: For same-day trades, `ftemp = _obs_c * 0.4 + min(_max_c, ftemp) * 0.6`. This only applies to the fallback Gaussian path, NOT the ensemble path. Is the ensemble already incorporating current observations, or is this a gap?
