# WeatherEdge Recovery Build
**Date:** 2026-04-11  
**Status:** Implementation-ready  
**Ground truth:** repo code + exported CSVs + measured trade data only

---

## 1. FINAL VERDICT

**Lane:** BUY YES on directional above/below threshold markets only.  
Specifically: buy YES when question is "above X" and our recal_prob > market_price + 5pp; buy YES when question is "below X" and our recal_prob > market_price + 5pp. Treat both as the same directional lane with one fixed signal type.

**Cities (hard whitelist — 4 cities):**
- **Munich** (EDDM) — lowest forecast std in dataset (0.98°F, n=24), consistent warm bias 2.52°F is correctable
- **Singapore** (WSSS) — std 1.18°F, n=16, tropical stability, low daily variance
- **London** (EGLL) — std 1.37°F, n=100 (largest sample), bias 0.86°F (small)
- **Paris** (LFPG) — std 1.62°F, n=43, bias 0.85°F (small)

**What must be turned off immediately:**
- BinSniper agent (firing SNIPE_YES signals with no resolved outcome data)
- GFSRefreshAgent (firing GFS_DELTA_YES signals with no resolved outcome data)
- ObsConfirmAgent (firing OBS_CONFIRM_YES signals with no resolved outcome data)
- ProfitTaker / RiskCutter exit agents
- CrossCityCorrelationEngine
- DutchBookScanner
- HedgeManager
- METARIntel
- LastMileAgent
- NOHarvester / YESHarvester (economically trivial, +$0.31 across 89 hours)
- Kelly sizing (replaced by fixed $2.00/trade)
- Coordinator conviction multipliers (1.5x boost disabled)
- Station-edge probability override (api_server.py L1457-1482)
- PILOT_CITY_ONLY restriction (replaced by RECOVERY_CITIES whitelist)
- F-Strict gate for exact bins (exact bins disabled entirely)
- Long-horizon trades (already disabled, verify stays off)
- Calibration backfill manual-only (wire into scheduler)

---

## 2. EVIDENCE

### PROVEN (directly measured from repo data)

**P1. All 4 BUY YES wins came from a single Atlanta market on a single day.**  
Old ledger: 4 wins from "Atlanta 64-65°F on March 29". Same market, 4 separate bets at our_prob=28.9%, market_price=5-9.5c. $176.80 PnL. Remove this one cluster: BUY YES is -$16.25 from 19 resolved trades (0 wins). This is not edge — it is variance on a lucky low-probability hit.

**P2. BUY YES at our_prob 40%+ has 0% win rate (0 of 19 resolved trades).**  
Combined data: trades where our_prob > 40%, resolved count = 19, wins = 0, PnL = -$112.45. The model's "confident" signals lose every time.

**P3. clob_edge_at_fill uses raw our_prob (not recalibrated) and hardcodes 1% fee.**  
api_server.py L2663-2664: `edge_at_fill(our_prob, ...)`. clob_book.py L114: `fee_cost = 0.01`. This means the clob_edge_at_fill logged in the ledger is computed from the uncalibrated probability. On a trade where our_prob=70% but actual win rate is ~10%, clob_edge_at_fill shows large positive edge while real edge is deeply negative. **The ledger field clob_edge_at_fill is systematically misleading.**

**P4. Station-edge overrides ensemble probability completely (api_server.py L1467).**  
Code: `our_prob = round(_se.probability * 100, 1)` — full replacement, not blend. Station edge itself blends internally (obs+ensemble by time of day), but the result replaces whatever the ensemble computed. During morning hours (obs_weight=0.3), this is a METAR-anchored estimate with high noise. Cannot validate this adds edge vs. pure ensemble.

**P5. end_date parsing silently fails for non-ISO dates, bypassing F-Strict gate.**  
api_server.py L1499-1503: `datetime.fromisoformat(...)` — no fallback for "Thu, 09 Apr 2026" format. On parse failure, `_mins_left = None`. F-Strict gate check is `if _mins_left is not None` — so silent fail = gate skip, NOT gate block. Trades can be placed with unknown lead time.

**P6. Calibration backfill is a manual standalone script. Zero resolutions in accuracy_store.**  
accuracy_store.json: predictions=888, resolutions=0. AccuracyTracker in ruflo_monitor.py is defined but never instantiated in run_monitor(). calibration_backfill.py is not imported by scheduler.py or api_server.py. The calibration system is completely disconnected from production.

**P7. Current ledger has 261 trades, 0 resolved.**  
All PAPER mode. Trade resolver `resolve_trades()` is called in scheduler.py L481, but the current ledger snapshot shows zero resolution. Either the resolver is broken in the deployed version, or this snapshot was taken before markets closed.

**P8. No above/below trades have ever been resolved in this bot's history.**  
Old ledger: 1 above trade (Ankara, unresolved), 3 below trades (all unresolved). 40 exact-bin trades resolved. There is zero empirical evidence for or against above/below edge from this bot's own data.

### LIKELY (strong inference from data)

**L1. The Munich warm bias (+2.52°F, 95.8% warm) is stable and correctable.**  
n=24 observations, all pointing same direction. Bias agent should apply ~-1.4°C correction to ensemble forecast for Munich. Need to verify bias agent is actually running and applying this.

**L2. The recalibration map may not transfer cleanly to above/below markets.**  
_RECAL_BUCKETS was derived from exact-bin trade win rates. Above/below markets are structurally different (wider target, different market maker pricing). The map may overstate or understate probabilities for directional trades.

**L3. Strategy agents (BinSniper, GFSRefresh) are producing signals with no validation.**  
Current ledger shows 31 GFS_DELTA_YES and 20 SNIPE_YES signals. 0 are resolved. These agents have no measured win rate and add complexity and noise.

### UNKNOWN

**U1. Whether above/below markets in the 4 target cities offer 15-45c YES prices with real liquidity.**  
No direct evidence from this bot's history. Must be measured in paper phase.

**U2. Whether ensemble directional accuracy on European cities (Munich, London, Paris) beats market.**  
No resolved above/below trades to measure against. This is the core hypothesis being tested.

**U3. Whether the bias correction for Munich actually fires in production.**  
Bias agent polls station_bias.db every 3600s. Station_bias.db has 2096 rows. Need to confirm Munich's EDDM station is in the DB with correct correction.

**U4. Whether live execution would differ materially from paper.**  
All 261 current trades are PAPER. No live fills observed. Slippage, fee, and order management behavior in live mode are unvalidated.

---

## 3. ROOT CAUSES THAT STILL MATTER

**Rank 1 — Model probability is not calibrated for the trade type being placed.**  
The recal map is derived from 23 resolved exact-bin trades (an extremely small sample dominated by a single Atlanta cluster). The raw model claims 40-95% probability on trades that never win. Even the recal map's conservative remapping may not apply to above/below markets. Until 50+ resolved above/below trades exist from these 4 cities, we cannot trust any EV estimate.

**Rank 2 — clob_edge_at_fill is computed from uncalibrated probability and logged as a real metric.**  
Every signal shows positive CLOB edge because the raw our_prob is high and uncalibrated. The ledger currently has no reliable edge metric. The recovery build must log both raw and recal versions of every probability, and must NOT use clob_edge_at_fill as a decision input.

**Rank 3 — The calibration feedback loop is dead.**  
888 predictions logged, 0 resolutions backfilled. The bot cannot learn from its mistakes because AccuracyTracker is dead code. The recal map is frozen at its initial state. Without live calibration, the bot will stay miscalibrated forever. This must be wired before the recovery build can compound on data.

**Rank 4 — Multiple strategy agents are firing signals with no measured win rate.**  
GFS_DELTA_YES, SNIPE_YES, OBS_CONFIRM_YES are live in the current build with no evidence they add value. They consume position slots, generate ledger noise, and distort any aggregate win rate analysis. Must be disabled to isolate the signal being tested.

**Rank 5 — Station-edge override introduces unvalidated METAR dependency.**  
The obs-based blending is theoretically sound but adds METAR data quality risk, timing risk, and a probability override that cannot be validated from current data. For 4 European cities + Singapore, morning METAR coverage may be incomplete. Disable for recovery build, use ensemble-only probability path.

---

## 4. RECOVERY BUILD

### Files to edit

| File | Action |
|------|--------|
| `config.py` | Add RECOVERY_MODE, RECOVERY_CITIES, FIXED_TRADE_SIZE_USD, disable all agent flags |
| `src/recovery_gate.py` | NEW — above/below gate with full logging |
| `api_server.py` | Enforce city whitelist; disable station-edge override; add _parse_end_date(); add comprehensive log dict; fix signal path |
| `scheduler.py` | Wire calibration_backfill into periodic run (every 3600s) |
| `src/strategy_gate.py` | Fix None check in f_strict_pass(); add above_below_recovery_pass() call |

### What to disable

- `ENABLE_EXACT_SINGLE = False` (already False)
- `ENABLE_EXACT_2BIN = False` (set to False)
- `ENABLE_F_STRICT = False` (was True, producing 0 trades anyway)
- `ENABLE_NO_HARVEST_V2 = False` (trivial PnL)
- `ENABLE_LONG_HORIZON = False` (already False)
- `ENABLE_ABOVE_BELOW = True` (keep — this IS the lane)
- `ABOVE_BELOW_SHADOW = False` (it's no longer shadow — it's primary)
- `PILOT_CITY_ONLY = None` (remove — replaced by RECOVERY_CITIES whitelist)
- Station-edge override block (api_server.py L1457-1482): bypass in recovery mode
- All strategy agents: BinSniper, GFSRefreshAgent, ObsConfirmAgent, DutchBookScanner, CrossCityCorrelationEngine, METARIntel, HedgeManager, LastMileAgent, ProfitTaker, RiskCutter

### What to replace

- `PILOT_CITY_ONLY = "London"` → `RECOVERY_CITIES = {"Munich", "Singapore", "London", "Paris"}`
- Kelly sizing → `FIXED_TRADE_SIZE_USD = 2.00`
- Coordinator size_mult (1.5x/1.0x/0.5x/0.0x) → fixed multiplier 1.0, no vetoes
- Shadow lane $2/trade cap → primary lane $2/trade cap (same size, different status)
- F-Strict gate → recovery_above_below_pass() from src/recovery_gate.py

### What to log (every trade, in meta JSON field)

```json
{
  "raw_prob":          <float, model output before any override, 0-100>,
  "ensemble_prob":     <float, ensemble-only output, 0-100>,
  "station_edge_prob": <float, station_edge output if available, else null>,
  "blended_prob":      <float, what our_prob was set to after any blending, 0-100>,
  "recal_prob":        <float, recal_prob(raw/100)*100, 0-100>,
  "final_prob_used":   <float, probability used for EV decision, 0-100>,
  "market_price_c":    <float, market price in cents>,
  "edge_at_decision":  <float, recal_prob - market_price, percentage points>,
  "edge_at_fill":      <float, from clob if available, else null>,
  "clob_note":         <str, "raw_prob_not_recal — do not trust" if edge_at_fill present>,
  "signal_age_s":      <float, seconds since signal generated>,
  "fill_price_simulated": <float, simulated fill in paper mode>,
  "mins_to_resolution": <float, null if parse failed>,
  "date_parse_ok":     <bool>,
  "city":              <str>,
  "station_icao":      <str>,
  "direction":         <str, "above" or "below">,
  "strategy_lane":     "recovery_above_below",
  "gate_reject_reason": <str, null if passed>,
  "station_edge_active": <bool>,
  "bias_correction_c": <float>,
  "sigma_used":        <float>
}
```

### What to keep

- Multi-model ensemble forecast (core signal, keep as-is)
- Nowcasting for ≤24h lead (keep, it's the only validated short-horizon path)
- recal_prob() mapping (keep — it's the best calibration we have, imperfect)
- Trade ledger + telemetry (keep all logging, critical for validation)
- Trade resolver (keep, wire into periodic execution)
- WeatherSentinel (keep for METAR data, but only as observation input — no probability override)
- AccuracyTracker (keep class, but wire it into run_monitor properly)
- PreTradeValidator (keep, it enforces the final gate)

### Hard stop conditions

- Daily PnL < -$10 on recovery lane → halt new entries, alert
- Signal win rate after 20 resolved trades < 30% → halt and audit
- If 0 trades fire in any 48h window → verify gate parameters are not too tight
- If clob_edge_at_fill field shows >20% on any trade → log warning (known to be misleading)

---

## 5. PATCHES

### PATCH 1: `config.py` — Add recovery mode block

Add after existing `ENABLE_*` flags:

```python
# ─── RECOVERY MODE (2026-04-11) ────────────────────────────────────────────
# Narrow comeback lane: above/below only, 4 city whitelist, fixed tiny sizing.
# Set RECOVERY_MODE=False to return to full-feature operation.
RECOVERY_MODE: bool = True

# Hard city whitelist. Only these 4 cities generate signals in recovery mode.
# Chosen by lowest forecast std (station_bias_summary.csv): 0.98/1.18/1.37/1.62°F
RECOVERY_CITIES: set = {"Munich", "Singapore", "London", "Paris"}

# Fixed trade size. Kelly disabled in recovery mode.
FIXED_TRADE_SIZE_USD: float = 2.00
DISABLE_KELLY: bool = True

# Recovery above/below gate parameters
RECOVERY_AB_MIN_LEAD_MIN: float = 360.0    # 6h minimum lead
RECOVERY_AB_MAX_LEAD_MIN: float = 1440.0   # 24h maximum lead
RECOVERY_AB_MIN_MARKET_PRICE: float = 0.10  # don't buy sub-10c certainties
RECOVERY_AB_MAX_MARKET_PRICE: float = 0.45  # don't buy above 45c (market already pricing high)
RECOVERY_AB_MIN_RECAL_PROB: float = 0.25   # minimum recalibrated probability
RECOVERY_AB_MIN_EDGE_PP: float = 0.05      # recal must exceed market by 5pp
RECOVERY_AB_DAILY_STOP_USD: float = -10.00 # halt new entries if daily PnL < -$10

# Strategy agent disables for recovery mode
RECOVERY_DISABLE_BINSNIPER: bool = True
RECOVERY_DISABLE_GFS_REFRESH: bool = True
RECOVERY_DISABLE_OBS_CONFIRM: bool = True
RECOVERY_DISABLE_EXIT_AGENTS: bool = True
RECOVERY_DISABLE_CROSS_CITY: bool = True
RECOVERY_DISABLE_DUTCH_BOOK: bool = True
RECOVERY_DISABLE_HEDGE_MANAGER: bool = True
RECOVERY_DISABLE_METAR_INTEL: bool = True
RECOVERY_DISABLE_LAST_MILE: bool = True
RECOVERY_DISABLE_NO_HARVEST: bool = True
RECOVERY_DISABLE_YES_HARVEST: bool = True
RECOVERY_DISABLE_STATION_EDGE_OVERRIDE: bool = True  # use ensemble-only prob path
```

Also change these existing flags:
```python
ENABLE_F_STRICT: bool = False          # was True — produces 0 trades, disabled
ENABLE_EXACT_2BIN: bool = False        # was True — exact bins disabled in recovery
ENABLE_NO_HARVEST_V2: bool = False     # was True — trivial PnL, disabled
ABOVE_BELOW_SHADOW: bool = False       # was True — above/below is now primary lane
PILOT_CITY_ONLY: str = ""              # was "London" — replaced by RECOVERY_CITIES
```

---

### PATCH 2: `src/recovery_gate.py` (NEW FILE)

```python
"""
Recovery Gate — above/below only lane for 4 whitelisted cities.
Replaces F-Strict gate for the 2026-04-11 recovery build.

All probability values are in 0..1 (not percent).
"""
from __future__ import annotations
import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# Import config at call time to pick up runtime changes
def _cfg():
    from config import (
        RECOVERY_CITIES,
        RECOVERY_AB_MIN_LEAD_MIN, RECOVERY_AB_MAX_LEAD_MIN,
        RECOVERY_AB_MIN_MARKET_PRICE, RECOVERY_AB_MAX_MARKET_PRICE,
        RECOVERY_AB_MIN_RECAL_PROB, RECOVERY_AB_MIN_EDGE_PP,
        FIXED_TRADE_SIZE_USD,
    )
    return {
        "cities": RECOVERY_CITIES,
        "min_lead": RECOVERY_AB_MIN_LEAD_MIN,
        "max_lead": RECOVERY_AB_MAX_LEAD_MIN,
        "min_mkt": RECOVERY_AB_MIN_MARKET_PRICE,
        "max_mkt": RECOVERY_AB_MAX_MARKET_PRICE,
        "min_recal": RECOVERY_AB_MIN_RECAL_PROB,
        "min_edge": RECOVERY_AB_MIN_EDGE_PP,
        "size": FIXED_TRADE_SIZE_USD,
    }


def recovery_ab_pass(
    *,
    city: str,
    direction: str,
    market_price: float,       # 0..1
    recal_prob: float,         # 0..1, from recal_prob(raw_prob)
    mins_to_resolution: Optional[float],
    raw_prob: float,           # 0..1, for logging
    ensemble_prob: float,      # 0..1, for logging
    bias_correction_c: float,  # applied upstream, for logging
    sigma_c: float,            # forecast sigma, for logging
) -> tuple[bool, str, dict]:
    """
    Gate for recovery above/below lane.

    Returns:
        (ok: bool, reason: str, log_dict: dict)

    log_dict is always populated regardless of ok/fail — attach to meta.
    """
    c = _cfg()
    t0 = time.monotonic()

    log_dict: dict = {
        "raw_prob": round(raw_prob * 100, 2),
        "ensemble_prob": round(ensemble_prob * 100, 2),
        "recal_prob": round(recal_prob * 100, 2),
        "final_prob_used": round(recal_prob * 100, 2),
        "market_price_c": round(market_price * 100, 2),
        "edge_at_decision": round((recal_prob - market_price) * 100, 2),
        "mins_to_resolution": mins_to_resolution,
        "date_parse_ok": mins_to_resolution is not None,
        "city": city,
        "direction": direction,
        "strategy_lane": "recovery_above_below",
        "bias_correction_c": round(bias_correction_c, 3),
        "sigma_used": round(sigma_c, 3),
        "station_edge_active": False,  # disabled in recovery mode
        "clob_note": "clob_edge_at_fill uses raw_prob not recal — do not trust as edge signal",
        "gate_reject_reason": None,
    }

    def fail(reason: str) -> tuple[bool, str, dict]:
        log_dict["gate_reject_reason"] = reason
        log.debug("RECOVERY_AB REJECT city=%s reason=%s", city, reason)
        return False, reason, log_dict

    # 1. City whitelist
    if city not in c["cities"]:
        return fail(f"city {city!r} not in recovery whitelist")

    # 2. Direction must be above or below
    if direction not in ("above", "below"):
        return fail(f"direction {direction!r} — recovery lane is above/below only")

    # 3. Lead time — fail hard if unknown
    if mins_to_resolution is None:
        return fail("mins_to_resolution unknown — end_date parse failed, gate blocked")
    if mins_to_resolution < c["min_lead"]:
        return fail(f"lead {mins_to_resolution:.0f}m < {c['min_lead']:.0f}m minimum")
    if mins_to_resolution > c["max_lead"]:
        return fail(f"lead {mins_to_resolution:.0f}m > {c['max_lead']:.0f}m maximum")

    # 4. Market price band
    if not (c["min_mkt"] <= market_price <= c["max_mkt"]):
        return fail(
            f"market_price {market_price:.3f} outside "
            f"[{c['min_mkt']:.2f},{c['max_mkt']:.2f}]"
        )

    # 5. Recalibrated probability floor
    if recal_prob < c["min_recal"]:
        return fail(f"recal_prob {recal_prob:.3f} < {c['min_recal']:.3f} minimum")

    # 6. Edge gate: recal must materially exceed market
    edge = recal_prob - market_price
    if edge < c["min_edge"]:
        return fail(
            f"edge {edge*100:.1f}pp < {c['min_edge']*100:.1f}pp minimum "
            f"(recal={recal_prob:.3f} mkt={market_price:.3f})"
        )

    log_dict["gate_reject_reason"] = None
    log.info(
        "RECOVERY_AB PASS city=%s dir=%s mkt=%.3f recal=%.3f edge=%.1fpp lead=%.0fm size=$%.2f",
        city, direction, market_price, recal_prob, edge * 100,
        mins_to_resolution, c["size"],
    )
    return True, "RECOVERY_PASS", log_dict


def parse_end_date_safe(s: str):
    """
    Parse Polymarket end_date string to UTC datetime.
    Handles ISO 8601 AND display formats like 'Thu, 09 Apr 2026'.
    Returns None on any failure — never raises.
    """
    from datetime import datetime, timezone
    if not s:
        return None
    # ISO 8601 (most common)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass
    # RFC-2822-ish display formats from Polymarket UI
    for fmt in ("%a, %d %b %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s.strip(), fmt)
            # Assume end of day UTC for display-only dates
            return d.replace(hour=23, minute=59, second=0, tzinfo=timezone.utc)
        except ValueError:
            continue
    log.warning("parse_end_date_safe: could not parse %r", s)
    return None
```

---

### PATCH 3: `api_server.py` — 6 targeted edits

**Edit A: Import recovery gate (near top, after existing strategy_gate imports)**
```python
# RECOVERY BUILD: import recovery gate
try:
    from src.recovery_gate import recovery_ab_pass, parse_end_date_safe
    _RECOVERY_GATE_AVAILABLE = True
except ImportError:
    _RECOVERY_GATE_AVAILABLE = False
    log.warning("recovery_gate not available")
```

**Edit B: Load recovery config flags (after existing config imports, around L56)**
```python
from config import (
    # ... existing imports ...
    RECOVERY_MODE, RECOVERY_CITIES, FIXED_TRADE_SIZE_USD, DISABLE_KELLY,
    RECOVERY_DISABLE_STATION_EDGE_OVERRIDE,
    RECOVERY_DISABLE_BINSNIPER, RECOVERY_DISABLE_GFS_REFRESH,
    RECOVERY_DISABLE_OBS_CONFIRM, RECOVERY_DISABLE_NO_HARVEST,
)
```

**Edit C: Fix end_date parsing (replace L1499-1503)**

Replace:
```python
_mins_left = None
try:
    _end = datetime.fromisoformat(mkt.get("end_date","").replace("Z","+00:00"))
    _mins_left = (_end - datetime.now(timezone.utc)).total_seconds() / 60.0
except Exception:
    pass
```

With:
```python
_mins_left = None
_date_parse_ok = False
_raw_end_str = mkt.get("end_date", "") or ""
if _RECOVERY_GATE_AVAILABLE:
    _end_dt = parse_end_date_safe(_raw_end_str)
else:
    try:
        _end_dt = datetime.fromisoformat(_raw_end_str.replace("Z", "+00:00"))
    except Exception:
        _end_dt = None
if _end_dt is not None:
    _mins_left = (_end_dt - datetime.now(timezone.utc)).total_seconds() / 60.0
    _date_parse_ok = True
else:
    logger.warning("end_date parse failed: %r — gate will block this market", _raw_end_str)
```

**Edit D: Disable station-edge override in recovery mode (around L1457)**

Replace:
```python
if _STATION_EDGE_AVAILABLE:
    _se = _station_prob(...)
    our_prob = round(_se.probability * 100, 1)   # OVERRIDE
    ...
```

With:
```python
_station_edge_active = False
if _STATION_EDGE_AVAILABLE and not RECOVERY_DISABLE_STATION_EDGE_OVERRIDE:
    _se = _station_prob(...)
    our_prob = round(_se.probability * 100, 1)
    _station_edge_active = True
    # (rest of station edge block unchanged)
```

**Edit E: Replace F-Strict gate with recovery gate for above/below (around L1507)**

Add AFTER existing F-Strict block:
```python
# RECOVERY BUILD: above/below gate
_ensemble_prob_pct = our_prob  # capture before any further mutation
if (
    p["direction"] in ("above", "below")
    and RECOVERY_MODE
    and _RECOVERY_GATE_AVAILABLE
):
    _raw_prob_01 = (our_prob / 100.0)
    _recal_01 = recal_prob(_raw_prob_01)
    _market_price_01 = mp / 100.0
    _bias_c = _bias_agent.get_correction_c(p["city"]) if HAS_BIAS_AGENT else 0.0

    _ok, _why, _recovery_log = recovery_ab_pass(
        city=p["city"],
        direction=p["direction"],
        market_price=_market_price_01,
        recal_prob=_recal_01,
        mins_to_resolution=_mins_left,
        raw_prob=_raw_prob_01,
        ensemble_prob=_ensemble_prob_pct / 100.0,
        bias_correction_c=_bias_c,
        sigma_c=float(p.get("sigma", 2.5) or 2.5),
    )
    if _ok:
        _gate_ok = True
        _lane = "recovery_above_below"
        _recovery_log["date_parse_ok"] = _date_parse_ok
        _recovery_log["station_edge_active"] = _station_edge_active
    else:
        sig_type = "SKIP"
        _gate_ok = False
        _recovery_log["gate_reject_reason"] = _why
```

**Edit F: Write recovery_log into meta field on trade record (around L2775)**

Replace:
```python
"meta": json.dumps({...existing meta...}),
```

With:
```python
"meta": json.dumps({
    **(existing_meta_dict),
    **(sig.get("_recovery_log") or {}),
}),
```

And in the signal dict append (L1556), add:
```python
"_recovery_log": _recovery_log if '_recovery_log' in dir() else {},
```

**Edit G: Disable strategy agents in recovery mode (near agent initialization, ~L1072)**

```python
if RECOVERY_MODE:
    # Disable all non-core strategy agents for recovery build
    _bin_sniper_available = False
    _gfs_refresh_available = False
    _obs_confirm_available = False
    _dutch_book_available = False
    _cross_city_available = False
    _metar_intel_available = False
    _last_mile_available = False
    logger.info("RECOVERY_MODE: all strategy agent modules disabled")
```

**Edit H: Fixed sizing in recovery mode (wherever Kelly/size is computed)**

```python
if RECOVERY_MODE and DISABLE_KELLY:
    size = FIXED_TRADE_SIZE_USD
    kelly = 0.0  # disabled
else:
    # existing Kelly logic
    size = ...
    kelly = ...
```

---

### PATCH 4: `scheduler.py` — Wire calibration backfill

Add to `schedule_loop()` after trade_resolver call (around L481):

```python
# RECOVERY BUILD: periodic calibration backfill (every 3600s)
_CALIB_INTERVAL_S = 3600
if not hasattr(self, '_last_calib_ts'):
    self._last_calib_ts = 0.0
_now_ts = time.time()
if _now_ts - self._last_calib_ts > _CALIB_INTERVAL_S:
    try:
        from scripts.calibration_backfill import backfill
        import os
        _ledger_path = os.environ.get("LEDGER_DB", "ledger.db")
        _store_path = os.environ.get("ACCURACY_STORE", "accuracy_store.json")
        _result = backfill(_ledger_path, _store_path)
        logger.info("calibration_backfill: added=%d total_resolutions=%d",
                    _result.get("n_added", 0), _result.get("total_resolutions", 0))
    except Exception as _e:
        logger.warning("calibration_backfill failed: %s", _e)
    self._last_calib_ts = _now_ts
```

---

### PATCH 5: `src/strategy_gate.py` — Fix None handling in f_strict_pass

The current code will TypeError on `None <= mins_to_resolution`. Add explicit check:

```python
def f_strict_pass(
    *,
    price: float,
    raw_prob: float,
    mins_to_resolution: float,
    city: str,
    bin_type: str = "exact_1bin",
) -> tuple[bool, str, float]:
    if bin_type not in ("exact_1bin", "exact_2bin", "exact"):
        return False, f"bin_type {bin_type} not in F-Strict cohort", 0.0
    # FIX: explicit None check — was silently crashing then being caught upstream
    if mins_to_resolution is None:
        return False, "mins_to_resolution is None — end_date parse failed", 0.0
    if not (F_STRICT_PRICE_MIN <= price <= F_STRICT_PRICE_MAX):
        return False, f"price {price:.3f} outside [{F_STRICT_PRICE_MIN},{F_STRICT_PRICE_MAX}]", 0.0
    # ... rest unchanged
```

---

## 6. VALIDATION PLAN

### Phase 1: Paper Validation (target: 2 weeks, 50+ resolved above/below trades)

**Entry criteria:**
- Recovery build deployed to Railway with all patches applied
- All strategy agent modules confirmed disabled (check /api/health response)
- RECOVERY_MODE=True confirmed in health output
- calibration_backfill running (confirm in logs hourly)

**Measure per resolved trade:**
- raw_prob, recal_prob, market_price (from meta JSON)
- won/loss, PnL

**Success criteria (after 50 resolved trades):**
- Above/below win rate > 30% (beats recal_prob floor of 25%)
- PnL > -$20 (expected loss from calibration uncertainty is within $20)
- At least 1 trade per city resolved
- Win rate in 20-30% recal_prob bucket: between 20-35%
- No systematic correlation between gate_reject_reason="mins_to_resolution unknown" and bad trades

**Failure criteria:**
- Win rate < 20% after 30 resolved trades → halt
- win rate anticorrelates with recal_prob (higher recal → lower win rate) → halt and audit
- 0 trades in any 48h window → check gate parameters, not halt

**Auto stop:**
- Daily PnL < -$10 → set RECOVERY_MODE flags to halt new entries (do not redeploy — just log)

### Phase 2: Shadow/Live-Sim (target: 1 week after Phase 1 success)

**Entry criteria:**
- Phase 1: ≥50 resolved trades, win rate ≥30%, PnL > -$5
- Recal map accuracy confirmed: bucket win rates match expected ranges
- calibration_backfill has run ≥14 times and bucket_stats_latest shows valid data

**What changes:**
- Run paper bot and live canary in parallel (same signals, paper takes action, live tracks what it WOULD cost)
- Compare paper fill price vs CLOB best_ask at signal time
- Measure actual slippage: (paper_fill - clob_ask) as % of price

**Success criteria:**
- Slippage < 5% of entry price on average
- Paper vs shadow PnL diverge by < 15% over 20 trades
- No "insufficient_depth" rejections from CLOB on >10% of signals

**Failure criteria:**
- Slippage consistently > 10% → liquidity is inadequate for these 4 cities
- Shadow PnL diverges > 30% from paper → execution realism is broken

### Phase 3: Live Canary ($2/trade, same size as paper)

**Entry criteria:**
- Phase 2: slippage measured and acceptable
- POLYMARKET_PRIVATE_KEY set in Railway environment
- PAPER_MODE=false configured
- Max 5 live trades per day hard cap set

**Success criteria (30 live trades):**
- Live PnL within 20% of paper PnL on same signals
- No order stuck in book > 120s (execution working)
- Fee tracking operational (fee_client not returning 0 bps every time)

**Failure criteria:**
- Any live trade size > $2 (sizing bug)
- Any live fill at price > market_price + 5c at signal time (slippage bug)
- Live PnL < -$20 in first 30 trades

**Auto stop conditions (live):**
- Single day PnL < -$10 → pause live, remain in paper shadow
- Cumulative live PnL < -$30 → stop live entirely, return to Phase 1 analysis

---

## 7. RED FLAGS

These specific outcomes prove the bot is still broken:

1. **Win rate on recal_prob 25-35% trades is below 20% after 30 resolved trades.** The recal map floor gives 25% as the minimum credible probability. If even this bucket can't hit 20%, the model has no directional edge at all.

2. **clob_edge_at_fill is positive on every trade and bears no correlation to won/loss.** This is already expected from the code audit (it uses raw not recal probability). If it stays uncorrelated, the ledger metric is worthless and must be removed or recomputed.

3. **Munich trades have lower win rate than London/Paris/Singapore despite better RMSE.** Would indicate the +2.52°F warm bias is not being corrected (bias agent not running or correction not applied to above/below logic).

4. **0 trades in any 7-day period after launch.** Means gate parameters are too tight for the available market supply. Check RECOVERY_AB_MAX_MARKET_PRICE (may need to widen to 0.50) and RECOVERY_AB_MIN_EDGE_PP (may need to drop to 0.03).

5. **gate_reject_reason="mins_to_resolution unknown" appears on >20% of markets.** Means the date parsing fix (Patch 3 Edit C) either wasn't deployed or Polymarket switched to a third date format. Check raw end_date field in logs.

6. **Win rate for direction="above" is >50% higher than direction="below" or vice versa.** Would indicate the model has systematic directional bias unrelated to calibration. Would require checking whether the ensemble mean is warm-biased for above or cold-biased for below.

7. **PnL is positive but win_rate < 25%.** Would indicate the bot is accidentally running a harvest-style play (buying 3c YES and winning at $1 occasionally) rather than genuine edge. Check avg market_price of resolved winning trades — should be 15-45c, not <10c.

8. **calibration_backfill runs but bucket_stats_latest shows 0 in all buckets after 2 weeks.** Means resolved trades are not being written to the ledger (trade_resolver broken) or the backfill is reading the wrong DB path.
