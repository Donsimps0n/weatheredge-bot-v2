"""
Builds the order ladder for Polymarket temperature trading.

Decides which bins to trade, at what sizes, across a market based on:
- True probabilities vs market prices
- Edge and expected value thresholds
- Kelly criterion sizing
- Order book depth and liquidity constraints
- Diurnal trading stage (pre-peak, near-peak, post-peak)
"""

import logging
from dataclasses import dataclass
from typing import Optional

# Local imports would go here in actual deployment
# from config import (
#     SIZE_CAP_DEFAULT_PCT,
#     SIZE_CAP_HIGH_DEPTH_PCT,
#     HIGH_DEPTH_THRESHOLD,
#     LADDER_BINS_NEAR_PEAK,
#     LADDER_BINS_DEFAULT,
#     KELLY_SIZE_CAP_NEAR_PEAK,
#     MIN_THEO_EV_BASE,
# )

logger = logging.getLogger(__name__)


# ============================================================================
# Configuration defaults (would typically import from config.py)
# ============================================================================
SIZE_CAP_DEFAULT_PCT = 0.20  # 20% of top-of-book depth
SIZE_CAP_HIGH_DEPTH_PCT = 0.35  # 35% of top-of-book depth for deep markets
HIGH_DEPTH_THRESHOLD = 30000.0  # Depth threshold for "high depth" markets
LADDER_BINS_NEAR_PEAK = 5  # Number of bins to trade near resolution peak
LADDER_BINS_DEFAULT = 4  # Number of bins to trade in default stage
KELLY_SIZE_CAP_NEAR_PEAK = 0.15  # Cap at 15% of Kelly size near peak
MIN_THEO_EV_BASE = 0.05  # Minimum theoretical EV per dollar


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class BookSnapshot:
    """Snapshot of order book state for a single token."""
    token_id: str
    best_bid: float
    best_ask: float
    mid_price: float
    spread: float
    relative_spread: float
    bid_depth_top3: float
    ask_depth_top3: float
    total_bid_depth: float
    total_ask_depth: float


@dataclass
class LadderLeg:
    """A single leg of the trading ladder."""
    bin_label: str
    token_id: str
    side: str  # "YES" or "NO"
    true_prob: float
    u_prob: float  # Unofficial/market probability
    market_price: float
    edge: float
    kelly_size: float
    capped_size: float
    fill_prob: float
    depth_available: float


@dataclass
class LadderResult:
    """Result of ladder building."""
    legs: list[LadderLeg]
    total_size: float
    total_theoretical_ev: float
    bins_considered: int
    bins_traded: int


# ============================================================================
# Main ladder building function
# ============================================================================

def build_ladder(
    bin_probs: list[tuple[str, float, float]],
    market_prices: dict[str, float],
    book_snapshots: dict[str, BookSnapshot],
    min_theo_ev: float,
    diurnal_stage: str,
    hours_to_resolution: float,
    kelly_fraction: float = 0.25,
    bankroll: float = 100.0,
) -> LadderResult:
    """
    Build the order ladder across temperature bins%

    Args:
        bin_probs: List of (bin_label, u_prob, true_prob) tuples.
        market_prices: Dict mapping bin_label to market price (0-1).
        book_snapshots: Dict mapping bin_label to BookSnapshot.
        min_theo_ev: Minimum theoretical EV per dollar to qualify.
        diurnal_stage: One of "pre-peak", "near-peak", "post-peak".
        hours_to_resolution: Hours remaining until market resolution.
        kelly_fraction: Fractional Kelly multiplier (default 0.25).
        bankroll: Available capital for sizing (default 100.0).

    Returns:
        LadderResult with all legs, totals, and counts.
    """
    logger.info(
        f"Building ladder: {len(bin_probs)} bins, stage={diurnal_stage}, "
        f"kelly_fraction={kelly_fraction}, bankroll={bankroll}"
    )

    legs = []
    bins_considered = 0
    bins_traded = 0

    # Step 1: Score all bins and filter by edge + EV threshold
    candidate_legs = []

    for bin_label, u_prob, true_prob in bin_probs:
        bins_considered += 1
        market_price = market_prices.get(bin_label)
        book = book_snapshots.get(bin_label)

        if market_price is None or book is None:
            logger.debug(f"Skipping {bin_label}: missing market price or book snapshot")
            continue

        # Compute edge
        edge = true_prob - market_price

        # Filter by edge > 0
        if edge <= 0:
            logger.debug(f"Skipping {bin_label}: edge={edge:.4f} <= 0")
            continue

        # Compute EV per dollar and filter
        theo_ev = compute_ev_per_dollar(true_prob, market_price)
        if theo_ev < min_theo_ev:
            logger.debug(
                f"Skipping {bin_label}: theo_ev={theo_ev:.6f} < {min_theo_ev:.6f}"
            )
            continue

        # Compute Kelly size
        kelly_size = compute_kelly_size(true_prob, market_price, kelly_fraction, bankroll)

        # Get depth for caps
        depth_available = book.ask_depth_top3  # Depth on the ask side (where we buy YES)

        # Apply size caps based on depth and diurnal stage
        capped_size = apply_size_caps(kelly_size, depth_available, diurnal_stage, theo_ev)

        # Estimate fill probability (simpler heuristic: inversely proportional to relative spread)
        fill_prob = estimate_fill_probability(book)

        candidate_legs.append(
            LadderLeg(
                bin_label=bin_label,
                token_id=bin_label,  # Simplified; in real system, token_id may differ
                side="YES",
                true_prob=true_prob,
                u_prob=u_prob,
                market_price=market_price,
                edge=edge,
                kelly_size=kelly_size,
                capped_size=capped_size,
                fill_prob=fill_prob,
                depth_available=depth_available,
            )
        )

    # Step 2: Sort by edge descending and select top N based on diurnal stage
    candidate_legs.sort(key=lambda leg: leg.edge, reverse=True)

    if diurnal_stage == "near-peak":
        max_bins = LADDER_BINS_NEAR_PEAK
    else:
        max_bins = LADDER_BINS_DEFAULT

    legs = candidate_legs[:max_bins]
    bins_traded = len(legs)

    # Step 3: Compute totals
    total_size = sum(leg.capped_size for leg in legs)
    total_theoretical_ev = sum(
        leg.capped_size * (leg.true_prob - leg.market_price) for leg in legs
    )

    logger.info(
        f"Ladder built: {bins_traded}/{bins_considered} bins traded, "
        f"total_size={total_size:.2f}, total_theo_ev={total_theoretical_ev:.4f}"
    )

    return LadderResult(
        legs=legs,
        total_size=total_size,
        total_theoretical_ev=total_theoretical_ev,
        bins_considered=bins_considered,
        bins_traded=bins_traded,
    )


# ============================================================================
# Helper functions
# ============================================================================

def compute_kelly_size(
    true_prob: float,
    market_price: float,
    kelly_fraction: float,
    bankroll: float,
) -> float:
    """
    Compute Kelly criterion sizing for a single bin.

    Args:
        true_prob: True probability (0-1).
        market_price: Market price / odds (0-1).
        kelly_fraction: Fractional Kelly multiplier.
        bankroll: Available capital.

    Returns:
        Recommended position size in dollars.
    """
    # Guard: invalid inputs
    if market_price <= 0 or market_price >= 1:
        return 0.0

    edge = true_prob - market_price
    if edge <= 0:
        return 0.0

    # Odds = net payout per dollar for YES token
    # If we buy YES at market_price, we risk market_price to win (1 - market_price)
    # net payout = (1 - market_price) / market_price
    odds = 1.0 / market_price - 1.0

    if odds <= 0:
        return 0.0

    # Kelly criterion: f* = (b*p - q) / b, where:
    # b = net odds (payout per dollar)
    # p = true probability
    # q = 1 - p
    # Simplified: f* = edge / odds
    kelly_full = edge / odds

    # Apply fractional Kelly and bankroll
    kelly_size = kelly_full * kelly_fraction * bankroll

    logger.debug(
        f"Kelly sizing: true_prob={true_prob:.4f}, market_price={market_price:.4f}, "
        f"edge={edge:.4f}, odds={odds?:.4f}, kelly_full={kelly_full:.4f}, "
        f"kelly_size={kelly_size:.2f}"
    )

    return max(0.0, kelly_size)


def apply_size_caps( kelly_size: float,
    depth: float,
    diurnal_stage: str,
    theo_ev: float,
) -> float:
    """
    Apply size caps based on depth, diurnal stage, and EV.

    Args:
        kelly_size: Uncapped Kelly-based size.
        depth: Available depth on top of book.
        diurnal_stage: One of "pre-peak", "near-peak", "post-peak".
        theo_ev: Theoretical EV per dollar.

    Returns:
        Capped position size.
    """
    # Determine base cap percentage
    if depth > HIGH_DEPTH_THRESHOLD and theo_ev > 0.20:
        cap_pct = SIZE_CAP_HIGH_DEPTH_PCT
    else:
        cap_pct = SIZE_CAP_DEFAULT_PCT

    depth_cap = cap_pct * depth

    # Apply near-peak additional cap
    capped_size = depth_cap
    if diurnal_stage == "near-peak":
        kelly_cap = KELLY_SIZE_CAP_NEAR_PEAK * kelly_size
        capped_size = min(capped_size, kelly_cap)

    # Final cap: never exceed Kelly size
    capped_size = min(capped_size, kelly_size)

    logger.debug(
        f"Size caps: kelly_size={kelly_size:.2f}, depth={depth:.0f}, "
        f"cap_pct={cap_pct:.0%}, depth_cap={depth_cap:.2f}, "
        f"diurnal_stage={diurnal_stage}, capped_size={capped_size:.2f}"
    )

    return max(0.0, capped_size)


def compute_ev_per_dollar(true_prob: float, market_price: float) -> float:
    """
    Compute theoretical expected value per dollar risked.

    Args:
        true_prob: True probability of the event.
        market_price: Market price (0-1).

    Returns:
        Expected value per dollar.
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0

    # Expected value if we buy YES at market_price
    # We risk market_price to win (1 - market_price)
    # EV = true_prob * (1 - market_price) - (1 - true_prob) * market_price
    # EV_per_dollar = EV / market_price
    ev = true_prob * (1 - market_price) - (1 - true_prob) * market_price
    ev_per_dollar = ev / market_price

    return max(0.0, ev_per_dollar)


def estimate_fill_probability(book: BookSnapshot) -> float:
    """
    Estimate probability of fill based on spread and depth.

    Simple heuristic: tighter spread = higher fill probability.

    Args:
        book: BookSnapshot with spread and depth info.

    Returns:
        Estimated fill probability (0-1).
    """
    if book.relative_spread <= 0.001:  # < 0.1% relative spread
        return 0.95
    elif book.relative_spread <= 0.005:  # < 0.5% relative spread
        return 0.85
    elif book.relative_spread <= 0.01:  # < 1% relative spread
        return 0.70
    else:
        return 0.50


# ============================================================================
# Export
# ============================================================================

__all__ = [
    "BookSnapshot",
    "LadderLeg",
    "LadderResult",
    "build_ladder",
    "compute_kelly_size",
    "apply_size_caps",
    "compute_ev_per_dollar",
    "estimate_fill_probability",
]
