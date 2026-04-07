"""
strategy_gate.py — F-Strict gating + recalibration map for WeatherEdge Bot v2.

Implements the STRATEGY_REWRITE.md plan:
- recal_prob():  isotonic-style remap of raw model probability into the
                 calibrated band, derived from §1.1 of STRATEGY_REWRITE.md.
- f_strict_pass(): hard gating filter for predictive entries.
- station_rmse_ok(): per-station RMSE filter.
- shadow_lane_ok(): tiny-risk ABOVE_BELOW shadow lane (operator tweak).

This module is import-safe (no side effects) and designed to be called from
api_server signal generation and from PreTradeValidator.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ── Recalibration map ────────────────────────────────────────────────
# Derived from observed live PnL by claimed_prob bucket on 133 resolved
# predictive trades. See docs/STRATEGY_REWRITE.md §1.1 + §2.1.
# This is a TEMPORARY operational map; refit weekly from live ledger
# once accuracy_store.resolutions backfill is running.
_RECAL_BUCKETS = [
    # (raw_lo, raw_hi, recal_value)
    (0.00, 0.10, 0.10),
    (0.10, 0.20, 0.20),
    (0.20, 0.30, 0.27),  # only calibrated bucket — anchored to observed 27.6%
    (0.30, 0.40, 0.18),  # collapse — observed 9.1% WR
    (0.40, 0.60, 0.15),
    (0.60, 0.80, 0.12),
    (0.80, 1.00, 0.30),  # mild trust — observed 36% WR
]


def recal_prob(raw_prob: float) -> float:
    """Map a raw model probability (0..1) into the recalibrated band."""
    try:
        p = float(raw_prob)
    except (TypeError, ValueError):
        return 0.0
    if p > 1.0:
        # caller passed a percentage, accept it gracefully
        p = p / 100.0
    p = max(0.0, min(1.0, p))
    for lo, hi, val in _RECAL_BUCKETS:
        if lo <= p < hi:
            return val
    return _RECAL_BUCKETS[-1][2]


# ── Per-station RMSE filter ──────────────────────────────────────────
_RMSE_CACHE: dict[str, float] = {}
_RMSE_LOADED = False


def _load_station_rmse() -> dict[str, float]:
    global _RMSE_LOADED
    if _RMSE_LOADED:
        return _RMSE_CACHE
    for path in (
        "station_reliability_latest.json",
        os.path.join(os.path.dirname(__file__), "..", "station_reliability_latest.json"),
    ):
        try:
            with open(path) as fh:
                data = json.load(fh)
            stations = data.get("stations", data) if isinstance(data, dict) else {}
            for k, v in stations.items():
                rmse_c = None
                city = None
                if isinstance(v, dict):
                    if v.get("rmse_c") is not None:
                        rmse_c = float(v["rmse_c"])
                    elif v.get("rmse_f") is not None:
                        rmse_c = float(v["rmse_f"]) / 1.8
                    elif v.get("rmse") is not None:
                        rmse_c = float(v["rmse"])
                    city = v.get("city")
                elif isinstance(v, (int, float)):
                    rmse_c = float(v)
                if rmse_c is not None:
                    _RMSE_CACHE[str(k).lower()] = rmse_c       # ICAO key
                    if city:
                        _RMSE_CACHE[str(city).lower()] = rmse_c  # city-name key
            break
        except Exception:
            continue
    _RMSE_LOADED = True
    return _RMSE_CACHE


def station_rmse_c(city: str) -> Optional[float]:
    rmse_map = _load_station_rmse()
    return rmse_map.get((city or "").lower())


def station_rmse_ok(city: str, max_rmse_c: float = 1.8) -> bool:
    """True if station has known RMSE ≤ max_rmse_c. Unknown stations FAIL closed."""
    rmse = station_rmse_c(city)
    if rmse is None:
        return False
    return rmse <= max_rmse_c


# ── F-Strict gate ────────────────────────────────────────────────────
F_STRICT_PRICE_MIN = 0.10
F_STRICT_PRICE_MAX = 0.20
F_STRICT_RECAL_PROB_MIN = 0.22
F_STRICT_RECAL_PROB_MAX = 0.40
F_STRICT_LEAD_MIN_MIN = 12 * 60   # 12h
F_STRICT_LEAD_MAX_MIN = 24 * 60   # 24h
F_STRICT_MAX_RMSE_C = 1.8
F_STRICT_PER_TRADE_CAP_USD = 10
F_STRICT_PER_CITY_DAY_CAP_USD = 40
F_STRICT_DAILY_STOP_USD = -25.0


def f_strict_pass(
    *,
    price: float,
    raw_prob: float,
    mins_to_resolution: float,
    city: str,
    bin_type: str = "exact_1bin",
) -> tuple[bool, str, float]:
    """
    Returns (ok, reason, recalibrated_prob).
    Hard gate for the F-Strict predictive cohort.
    """
    if bin_type not in ("exact_1bin", "exact_2bin", "exact"):
        return False, f"bin_type {bin_type} not in F-Strict cohort", 0.0
    if not (F_STRICT_PRICE_MIN <= price <= F_STRICT_PRICE_MAX):
        return False, f"price {price:.3f} outside [{F_STRICT_PRICE_MIN},{F_STRICT_PRICE_MAX}]", 0.0
    if not (F_STRICT_LEAD_MIN_MIN <= mins_to_resolution <= F_STRICT_LEAD_MAX_MIN):
        return False, f"lead {mins_to_resolution:.0f}m outside [{F_STRICT_LEAD_MIN_MIN},{F_STRICT_LEAD_MAX_MIN}]", 0.0
    if not station_rmse_ok(city, F_STRICT_MAX_RMSE_C):
        rmse = station_rmse_c(city)
        return False, f"station {city} rmse={rmse} > {F_STRICT_MAX_RMSE_C}°C (or unknown)", 0.0
    rp = recal_prob(raw_prob)
    if not (F_STRICT_RECAL_PROB_MIN <= rp <= F_STRICT_RECAL_PROB_MAX):
        return False, f"recal_prob {rp:.3f} outside [{F_STRICT_RECAL_PROB_MIN},{F_STRICT_RECAL_PROB_MAX}]", rp
    return True, "F-STRICT PASS", rp


# ── ABOVE_BELOW shadow lane (operator tweak: keep alive, tiny risk) ──
SHADOW_AB_PER_TRADE_CAP_USD = 2.0
SHADOW_AB_DAILY_BUDGET_USD = 10.0


def shadow_lane_ok(
    *,
    raw_prob: float,
    price: float,
    mins_to_resolution: float,
) -> tuple[bool, str]:
    """ABOVE_BELOW shadow lane: only the calibrated 20–35% raw band, lead ≥6h.
    Sized externally at ≤$2/trade. Tracked separately so it cannot mask
    F-Strict PnL. Operator tweak from STRATEGY_REWRITE review."""
    if not (0.20 <= raw_prob <= 0.35):
        return False, f"shadow: raw_prob {raw_prob:.3f} outside [0.20,0.35]"
    if not (0.05 <= price <= 0.40):
        return False, f"shadow: price {price:.3f} outside [0.05,0.40]"
    if mins_to_resolution < 6 * 60:
        return False, f"shadow: lead {mins_to_resolution:.0f}m < 360m"
    return True, "SHADOW PASS"
