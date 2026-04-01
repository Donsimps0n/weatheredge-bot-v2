"""
Cross-market consistency filter for Polymarket temperature trading bot.

Implements spec bullet #7: Apply cross-market consistency checks to filter
and rank markets based on delta z-scores and season correlation alignment.
"""

import logging
from dataclasses import dataclass
from typing import Optional

# Configure logging
logger = logging.getLogger(__name__)


# Dataclass definitions
@dataclass
class MarketInfo:
    """Information about a single market."""
    market_slug: str
    regime: str
    implied_prob: float  # Market-implied probability for the target bin
    model_prob: float    # Our model probability
    station_confidence: int


@dataclass
class CrossMarketResult:
    """Result of cross-market consistency filter."""
    delta_z_score: float
    peer_markets_used: list[str]
    season_corr: float  # Max correlation among qualifying peers
    min_theo_ev_adjustment: float  # 0.0 or 0.03
    flag_raised: bool
    skip_recommended: bool  # True only if flag_raised AND station_confidence < 2
    details: str


def compute_delta_zscore(
    delta_implied: float,
    mean_delta: float,
    std_delta: float
) -> float:
    """
    Compute delta z-score for a market.

    Args:
        delta_implied: The delta value for the market (model_prob - implied_prob)
        mean_delta: Mean delta across peer markets
        std_delta: Standard deviation of delta across peer markets

    Returns:
        Z-score: (delta_implied - mean_delta) / std_delta
        Returns 0.0 if std_delta < 1e-8 to avoid division by zero
    """
    if std_delta < 1e-8:
        logger.debug(f"std_delta too small ({std_delta}), returning 0.0")
        return 0.0

    z_score = (delta_implied - mean_delta) / std_delta
    logger.debug(f"Computed delta_zscore: {z_score:.4f} (delta={delta_implied:.6f}, mean={mean_delta:.6f}, std={std_delta:.6f})")
    return z_score


def check_cross_market(
    target_market: MarketInfo,
    peer_markets: list[MarketInfo],
    season_corr_matrix: dict[tuple[str, str], float],
    min_corr_threshold: float = 0.90,
    delta_z_threshold: float = 2.8
) -> CrossMarketResult:
    """
    Check cross-market consistency for a target market.

    Args:
        target_market: The market to evaluate
        peer_markets: List of peer markets to compare against
        season_corr_matrix: Dict mapping (market1_slug, market2_slug) -> correlation
        min_corr_threshold: Minimum season correlation to include peer (default 0.90)
        delta_z_threshold: Z-score threshold for flagging (default 2.8)

    Returns:
        CrossMarketResult with detailed analysis and recommendations
    """
    logger.info(f"Checking cross-market consistency for {target_market.market_slug}")

    # Step a: Filter peers by regime and season correlation
    qualifying_peers = []
    max_season_corr = 0.0

    for peer in peer_markets:
        # Same regime check
        if peer.regime != target_market.regime:
            logger.debug(f"Peer {peer.market_slug} skipped: different regime ({peer.regime} vs {target_market.regime})")
            continue

        # Season correlation check
        corr_key = (target_market.market_slug, peer.market_slug)
        corr_key_reverse = (peer.market_slug, target_market.market_slug)

        correlation = season_corr_matrix.get(corr_key) or season_corr_matrix.get(corr_key_reverse)

        if correlation is None:
            logger.debug(f"Peer {peer.market_slug} skipped: no correlation data")
            continue

        if correlation <= min_corr_threshold:
            logger.debug(f"Peer {peer.market_slug} skipped: correlation {correlation:.4f} < {min_corr_threshold}")
            continue

        qualifying_peers.append(peer)
        max_season_corr = max(max_season_corr, correlation)
        logger.debug(f"Peer {peer.market_slug} qualified: correlation {correlation:.4f}")

    # Step b: If no qualifying peers, return neutral result
    if not qualifying_peers:
        logger.info(f"No qualifying peers for {target_market.market_slug}")
        return CrossMarketResult(
            delta_z_score=0.0,
            peer_markets_used=[],
            season_corr=0.0,
            min_theo_ev_adjustment=0.0,
            flag_raised=False,
            skip_recommended=False,
            details="No qualifying peers found (same regime + correlation > 0.90)"
        )

    # Step c: Compute delta for each qualifying peer
    deltas = []
    peer_slugs = []

    for peer in qualifying_peers:
        delta = peer.model_prob - peer.implied_prob
        deltas.append(delta)
        peer_slugs.append(peer.market_slug)
        logger.debug(f"Peer {peer.market_slug} delta: {delta:.6f} (model={peer.model_prob:.6f}, implied={peer.implied_prob:.6f})")

    # Step d: Compute mean and std of deltas
    mean_delta = sum(deltas) / len(deltas)

    if len(deltas) > 1:
        variance = sum((d - mean_delta) ** 2 for d in deltas) / len(deltas)
        std_delta = variance ** 0.5
    else:
        std_delta = 0.0

    logger.debug(f"Delta statistics: mean={mean_delta:.6f}, std={std_delta:.6f}, n_peers={len(deltas)}")

    # Step e: Compute delta_z for target market
    target_delta = target_market.model_prob - target_market.implied_prob
    delta_z = compute_delta_zscore(target_delta, mean_delta, std_delta)

    # Step f: Check if |delta_z| > threshold
    flag_raised = abs(delta_z) > delta_z_threshold
    min_theo_ev_adjustment = 0.03 if flag_raised else 0.0

    if flag_raised:
        logger.warning(f"Flag raised for {target_market.market_slug}: |delta_z|={abs(delta_z):.4f} > {delta_z_threshold}")

    # Step g: Determine if skip is recommended
    skip_recommended = flag_raised and target_market.station_confidence < 2

    if skip_recommended:
        logger.warning(f"Skip recommended for {target_market.market_slug}: flag_raised=True, confidence={target_market.station_confidence}")

    # Step h: Build details string
    details = (
        f"Peers: {len(qualifying_peers)}, "
        f"max_corr={max_season_corr:.4f}, "
        f"delta_z={delta_z:.4f}, "
        f"target_delta={target_delta:.6f}, "
        f"mean_delta={mean_delta:.6f}, "
        f"std_delta={std_delta:.6f}"
    )

    result = CrossMarketResult(
        delta_z_score=delta_z,
        peer_markets_used=peer_slugs,
        season_corr=max_season_corr,
        min_theo_ev_adjustment=min_theo_ev_adjustment,
        flag_raised=flag_raised,
        skip_recommended=skip_recommended,
        details=details
    )

    logger.info(f"Cross-market check result for {target_market.market_slug}: flag={flag_raised}, skip={skip_recommended}")
    return result


def apply_cross_market_filter(
    forecast_probs: dict,
    market: dict,
    markets: list[dict],
) -> dict:
    """
    Lightweight wrapper called by TradingScheduler.run_cycle().

    Takes the forecast probability dict for a single market and a list of
    all markets in the current cycle, returning (possibly adjusted) probs.

    Currently passes through unchanged — cross-market consistency checks
    are performed separately via check_cross_market() and used for ranking
    in rank_markets_with_cross_filter(). This wrapper exists so the scheduler
    import doesn't break and so cross-market adjustments can be layered in
    later without touching the scheduler code.

    Args:
        forecast_probs: Dict with keys like yes_prob, no_prob, bin_probs, source.
        market: The current market dict being evaluated.
        markets: All markets in the cycle (for peer comparison).

    Returns:
        The (possibly adjusted) forecast_probs dict.
    """
    # Future: could apply a z-score penalty to yes_prob / no_prob based on
    # cross-market delta analysis. For now, passthrough.
    return forecast_probs


def rank_markets_with_cross_filter(
    markets: list[tuple[str, float, CrossMarketResult]]
) -> list[tuple[str, float]]:
    """
    Rank markets using theoretical EV and cross-market filter results.

    Demotes flagged markets (moves them to the end of equivalent-EV groups).

    Args:
        markets: List of (market_slug, theo_ev, cross_result) tuples

    Returns:
        List of (market_slug, adjusted_ranking_score) tuples, sorted by score descending
    """
    logger.info(f"Ranking {len(markets)} markets with cross-market filter")

    # Separate flagged and non-flagged markets
    non_flagged = []
    flagged = []

    for market_slug, theo_ev, cross_result in markets:
        if cross_result.flag_raised:
            flagged.append((market_slug, theo_ev, cross_result))
            logger.debug(f"Market {market_slug} flagged during ranking")
        else:
            non_flagged.append((market_slug, theo_ev, cross_result))

    # Sort non-flagged by theo_ev descending
    non_flagged_sorted = sorted(non_flagged, key=lambda x: x[1], reverse=True)

    # Sort flagged by theo_ev descending
    flagged_sorted = sorted(flagged, key=lambda x: x[1], reverse=True)

    # Combine: non-flagged first, then flagged
    ranked = non_flagged_sorted + flagged_sorted

    # Build output with ranking scores
    # Ranking score: theo_ev for non-flagged, theo_ev - penalty for flagged
    results = []
    for i, (market_slug, theo_ev, cross_result) in enumerate(ranked):
        if cross_result.flag_raised:
            # Penalize flagged markets by small amount to demote them
            adjusted_score = theo_ev - 0.001
        else:
            adjusted_score = theo_ev

        results.append((market_slug, adjusted_score))
        logger.debug(f"Rank {i+1}: {market_slug}, score={adjusted_score:.6f}, flagged={cross_result.flag_raised}")

    logger.info(f"Ranking complete: {len(non_flagged_sorted)} non-flagged, {len(flagged_sorted)} flagged")
    return results
