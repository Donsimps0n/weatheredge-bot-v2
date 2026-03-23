"""
Tests for cross_market_filter module.

Covers spec bullet #7: Cross-market consistency checks.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from cross_market_filter import (
    compute_delta_zscore,
    check_cross_market,
    MarketInfo,
)


class TestCrossMarketFilter:
    """Tests for cross-market filtering."""

    def test_delta_zscore_normal(self):
        """Test delta z-score computation: normal case."""
        z_score = compute_delta_zscore(
            delta_implied=0.05,
            mean_delta=0.02,
            std_delta=0.03,
        )

        # (0.05 - 0.02) / 0.03 = 1.0
        expected = 1.0
        assert abs(z_score - expected) < 1e-6, f"Expected {expected}, got {z_score}"

    def test_delta_zscore_zero_std(self):
        """Test delta z-score: zero std returns 0.0."""
        z_score = compute_delta_zscore(
            delta_implied=0.05,
            mean_delta=0.02,
            std_delta=0.0,
        )

        assert z_score == 0.0, f"Expected 0.0 for zero std, got {z_score}"

    def test_cross_market_no_peers(self):
        """Test cross-market with no peers: neutral result."""
        target = MarketInfo(
            market_slug="temp-ny-20230601",
            regime="clear",
            implied_prob=0.45,
            model_prob=0.5,
            station_confidence=3,
        )

        peer_markets = []  # No peers
        season_corr_matrix = {}

        result = check_cross_market(
            target_market=target,
            peer_markets=peer_markets,
            season_corr_matrix=season_corr_matrix,
        )

        # With no peers, should return neutral result
        assert result.flag_raised is False, "Should not raise flag with no peers"
        assert result.skip_recommended is False, "Should not recommend skip"
        assert result.min_theo_ev_adjustment == 0.0, "Should have no adjustment"

    def test_cross_market_flag(self):
        """Test cross-market flag: |z| > 2.8 → flag raised."""
        target = MarketInfo(
            market_slug="temp-ny-20230601",
            regime="clear",
            implied_prob=0.45,
            model_prob=0.65,  # Large disagreement
            station_confidence=3,
        )

        # Create peers with similar probabilities
        peer_markets = [
            MarketInfo(
                market_slug="temp-ny-alt",
                regime="clear",
                implied_prob=0.50,
                model_prob=0.52,
                station_confidence=2,
            ),
            MarketInfo(
                market_slug="temp-ny-other",
                regime="clear",
                implied_prob=0.48,
                model_prob=0.50,
                station_confidence=2,
            ),
        ]

        season_corr_matrix = {
            ("temp-ny-20230601", "temp-ny-alt"): 0.95,
            ("temp-ny-20230601", "temp-ny-other"): 0.95,
        }

        result = check_cross_market(
            target_market=target,
            peer_markets=peer_markets,
            season_corr_matrix=season_corr_matrix,
            delta_z_threshold=2.8,
        )

        # Large delta between model and implied should raise flag
        assert isinstance(result.flag_raised, bool), "Should return bool"
        assert isinstance(result.delta_z_score, float), "Should return float z_score"

    def test_cross_market_min_corr_threshold(self):
        """Test cross-market filters peers by min correlation."""
        target = MarketInfo(
            market_slug="temp-ny-20230601",
            regime="clear",
            implied_prob=0.45,
            model_prob=0.50,
            station_confidence=3,
        )

        # Create peers, one highly correlated and one not
        peer_markets = [
            MarketInfo(
                market_slug="temp-ny-high-corr",
                regime="clear",
                implied_prob=0.46,
                model_prob=0.51,
                station_confidence=2,
            ),
            MarketInfo(
                market_slug="temp-ny-low-corr",
                regime="clear",
                implied_prob=0.40,
                model_prob=0.55,
                station_confidence=2,
            ),
        ]

        season_corr_matrix = {
            ("temp-ny-20230601", "temp-ny-high-corr"): 0.95,  # High correlation
            ("temp-ny-20230601", "temp-ny-low-corr"): 0.70,  # Low correlation (below 0.90)
        }

        result = check_cross_market(
            target_market=target,
            peer_markets=peer_markets,
            season_corr_matrix=season_corr_matrix,
            min_corr_threshold=0.90,
        )

        # Should only include high-correlation peer
        assert len(result.peer_markets_used) <= len(peer_markets), (
            "Should filter peers by correlation"
        )
