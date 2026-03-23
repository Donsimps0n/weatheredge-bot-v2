"""
Risk Manager for Polymarket Temperature Trading Bot

Handles theoretical expected value (EV) calculations, cost proxies, and dynamic risk gates.
Implements bullets #3 (theoretical_full_ev) and #10 (min_theo_ev gate & dynamic ratchet).
"""

import logging
from dataclasses import dataclass, field
from typing import List, Dict

logger = logging.getLogger(__name__)

# ============================================================================
# Configuration (imported from config module in production)
# ============================================================================
MIN_THEO_EV_BASE = 0.10
THEO_EV_FLATTEN_THRESHOLD = 0.10
GATE_12H_MIN_EV = 0.14
GATE_6H_MIN_EV = 0.20
LEAKAGE_RATCHET_PER_HALF_BPS = 0.01


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class CostProxy:
    """Cost proxy breakdown for fill operations."""
    effective_roundtrip_bps: float
    slippage_proxy: float
    fee_cost: float
    total: float


@dataclass
class LegInput:
    """Single leg of a multi-leg position."""
    size: float
    true_prob: float
    entry_price: float


@dataclass
class GateResult:
    """Result of EV gate check."""
    passes: bool
    gate_name: str
    required_ev: float
    actual_ev: float


@dataclass
class MinTheoEvResult:
    """Result of min_theo_ev computation with adjustment details."""
    min_ev: float
    base_ev: float
    adjustments: Dict[str, float] = field(default_factory=dict)


# ============================================================================
# Core Functions
# ============================================================================

def compute_cost_proxy(
    fill_prob: float,
    aggressiveness: float,
    depth: float,
    relative_spread: float,
    fee_rate_bps: int = 0,
    fees_enabled: bool = False,
) -> CostProxy:
    """
    Compute cost proxy including roundtrip spread, slippage, and fees.

    Args:
        fill_prob: Probability of fill (0.0 to 1.0).
        aggressiveness: Aggressiveness multiplier (0.0+).
        depth: Market depth in number of contracts.
        relative_spread: Relative spread as a fraction (e.g., 0.002 for 20 bps).
        fee_rate_bps: Fee rate in basis points.
        fees_enabled: Whether to include fees in cost.

    Returns:
        CostProxy with effective_roundtrip_bps, slippage_proxy, fee_cost, and total.
    """
    # Effective roundtrip: spread * (1 + aggressiveness) / fill_prob
    effective_roundtrip_bps = (
        relative_spread * (1 + aggressiveness) / max(fill_prob, 0.01)
    )

    # Slippage proxy: depth impact modeled as spread * 0.5 * (1 + 1000/depth)
    slippage_proxy = relative_spread * 0.5 * (1 + 1000 / max(depth, 100))

    # Fee cost: roundtrip fees if enabled
    fee_cost = (fee_rate_bps / 10000) * 2 if fees_enabled else 0.0

    # Total cost
    total = effective_roundtrip_bps + slippage_proxy + fee_cost

    logger.debug(
        f"Cost proxy computed: roundtrip={effective_roundtrip_bps:.6f}, "
        f"slippage={slippage_proxy:.6f}, fees={fee_cost:.6f}, total={total:.6f}"
    )

    return CostProxy(
        effective_roundtrip_bps=effective_roundtrip_bps,
        slippage_proxy=slippage_proxy,
        fee_cost=fee_cost,
        total=total,
    )


def compute_theoretical_full_ev(legs: List[LegInput], cost_proxy: float) -> float:
    """
    Compute theoretical full expected value (EV) across all legs.

    Bullet #3 specification:
    theo_ev = SUM_i [ size_i * ( true_prob_i * (1/entry_price_i - 1) - (1 - true_prob_i) ) ] - cost_proxy

    Args:
        legs: List of LegInput dataclasses with size, true_prob, entry_price.
        cost_proxy: Cost proxy in decimal form (e.g., 0.001 for 10 bps).

    Returns:
        Theoretical full EV as a float.
    """
    ev_sum = 0.0

    for leg in legs:
        if leg.entry_price <= 0:
            logger.warning(f"Invalid entry price {leg.entry_price} for leg; skipping")
            continue

        # Contribution: size * (true_prob * (1/entry_price - 1) - (1 - true_prob))
        leg_ev = leg.size * (
            leg.true_prob * (1 / leg.entry_price - 1) - (1 - leg.true_prob)
        )
        ev_sum += leg_ev

    theo_ev = ev_sum - cost_proxy

    logger.debug(
        f"Theoretical full EV computed: ev_sum={ev_sum:.6f}, "
        f"cost_proxy={cost_proxy:.6f}, theo_ev={theo_ev:.6f}"
    )

    return theo_ev


def should_auto_flatten(theo_ev: float) -> bool:
    """
    Check if position should be auto-flattened based on theo EV threshold.

    Bullet #3: Auto-flatten if theoretical_full_ev < 0.10

    Args:
        theo_ev: Theoretical full EV value.

    Returns:
        True if theo_ev < THEO_EV_FLATTEN_THRESHOLD, False otherwise.
    """
    should_flatten = theo_ev < THEO_EV_FLATTEN_THRESHOLD

    if should_flatten:
        logger.info(
            f"Auto-flatten triggered: theo_ev={theo_ev:.6f} < "
            f"threshold={THEO_EV_FLATTEN_THRESHOLD}"
        )

    return should_flatten


def check_ev_gates(theo_ev: float, hours_to_resolution: float) -> GateResult:
    """
    Check if theo EV passes time-based gates.

    Bullet #3 gates:
    - <12h to resolution: theo_ev >= 0.14
    - <6h to resolution: theo_ev >= 0.20

    Args:
        theo_ev: Theoretical full EV value.
        hours_to_resolution: Hours until market resolution.

    Returns:
        GateResult with pass/fail status and required EV threshold.
    """
    if hours_to_resolution < 6:
        required_ev = GATE_6H_MIN_EV
        gate_name = "gate_6h"
    elif hours_to_resolution < 12:
        required_ev = GATE_12H_MIN_EV
        gate_name = "gate_12h"
    else:
        required_ev = MIN_THEO_EV_BASE
        gate_name = "gate_base"

    passes = theo_ev >= required_ev

    logger.debug(
        f"EV gate check: {gate_name}, required={required_ev:.4f}, "
        f"actual={theo_ev:.6f}, passes={passes}"
    )

    return GateResult(
        passes=passes,
        gate_name=gate_name,
        required_ev=required_ev,
        actual_ev=theo_ev,
    )


def compute_min_theo_ev(
    base: float,
    liquidity_score: float,
    hours_to_resolution: float,
    is_burst: bool,
    rolling_leakage_bps: float,
    baseline_leakage_bps: float = 2.0,
) -> MinTheoEvResult:
    """
    Compute dynamic min_theo_ev with adjustments for market conditions.

    Bullet #10 specification:
    - Base: 0.10
    - Adjustments ±0.02–0.04 based on liquidity, time-to-res, burst windows
    - Ratchet up if rolling leakage rises: +0.01 min_theo_ev per 0.5 bps above baseline

    Args:
        base: Base min_theo_ev (typically MIN_THEO_EV_BASE = 0.10).
        liquidity_score: Liquidity score (0.0 to 1.0).
        hours_to_resolution: Hours until market resolution.
        is_burst: True if in a model burst window.
        rolling_leakage_bps: Rolling average leakage in basis points.
        baseline_leakage_bps: Baseline leakage threshold (default 2.0 bps).

    Returns:
        MinTheoEvResult with min_ev, base_ev, and breakdown of adjustments.
    """
    adjustments = {}

    # Liquidity adjustment
    if liquidity_score < 0.3:
        adjustments["liquidity"] = 0.03
    elif liquidity_score < 0.5:
        adjustments["liquidity"] = 0.02
    else:
        adjustments["liquidity"] = 0.0

    # Time-to-resolution adjustment
    if hours_to_resolution < 6:
        adjustments["time_to_res"] = 0.04
    elif hours_to_resolution < 12:
        adjustments["time_to_res"] = 0.02
    else:
        adjustments["time_to_res"] = 0.0

    # Burst window adjustment (allow tighter edge during model runs)
    adjustments["burst"] = -0.02 if is_burst else 0.0

    # Leakage ratchet: +0.01 per 0.5 bps above baseline
    leakage_ratchet = update_leakage_ratchet(rolling_leakage_bps, baseline_leakage_bps)
    adjustments["leakage_ratchet"] = leakage_ratchet

    # Sum all adjustments
    total_adjustment = sum(adjustments.values())
    min_ev = base + total_adjustment

    # Clamp to never go below 0.04 (never negative gate)
    min_ev = max(min_ev, 0.04)

    logger.debug(
        f"Min theo_ev computed: base={base:.4f}, adjustments={adjustments}, "
        f"min_ev={min_ev:.4f}"
    )

    return MinTheoEvResult(
        min_ev=min_ev,
        base_ev=base,
        adjustments=adjustments,
    )


def update_leakage_ratchet(
    rolling_leakage_bps: float, baseline_bps: float = 2.0
) -> float:
    """
    Compute leakage-based ratchet adjustment to min_theo_ev.

    Bullet #10: Ratchet up +0.01 min_theo_ev per 0.5 bps above baseline.

    Args:
        rolling_leakage_bps: Rolling average leakage in basis points.
        baseline_bps: Baseline leakage threshold (default 2.0 bps).

    Returns:
        Ratchet adjustment amount (non-negative float).
    """
    if rolling_leakage_bps <= baseline_bps:
        return 0.0

    excess_bps = rolling_leakage_bps - baseline_bps
    num_half_bps_increments = int(excess_bps / 0.5)
    ratchet = num_half_bps_increments * LEAKAGE_RATCHET_PER_HALF_BPS

    logger.debug(
        f"Leakage ratchet: rolling={rolling_leakage_bps:.2f} bps, "
        f"baseline={baseline_bps:.2f} bps, excess={excess_bps:.2f} bps, "
        f"ratchet={ratchet:.4f}"
    )

    return max(0, ratchet)
