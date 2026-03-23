"""
Tests for ladder_builder module.

Tests order ladder building based on probabilities, edges, and Kelly sizing.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

pytestmark = pytest.mark.skipif(not HAS_NUMPY, reason="numpy not installed")


class TestLadderBuilder:
    """Tests for order ladder building."""

    def test_kelly_size_positive_edge(self):
        """Test Kelly sizing with positive edge."""
        from ladder_builder import kelly_size

        # True prob 0.6, price 0.4 → edge of 0.5
        size = kelly_size(
            true_prob=0.6,
            market_price=0.4,
            bankroll=100.0,
            kelly_fraction=0.25,
        )

        # Kelly: f* = (bp - q) / b where b = 1 (binary), p = 0.6, q = 0.4
        # f* = (0.6 - 0.4) / 1 = 0.2 = 20% of bankroll
        # With kelly_fraction = 0.25, actual = 0.25 * 0.2 = 0.05 = 5%
        expected = 100.0 * 0.05
        assert size > 0, "Should size positive for positive edge"
        assert abs(size - expected) < 1.0, f"Expected ~{expected}, got {size}"

    def test_kelly_size_no_edge(self):
        """Test Kelly sizing with no edge."""
        from ladder_builder import kelly_size

        # True prob equals market price → no edge
        size = kelly_size(
            true_prob=0.5,
            market_price=0.5,
            bankroll=100.0,
            kelly_fraction=0.25,
        )

        assert size == 0, "Should not size when no edge"

    def test_kelly_size_negative_edge(self):
        """Test Kelly sizing with negative edge."""
        from ladder_builder import kelly_size

        # True prob 0.4, price 0.5 → negative edge
        size = kelly_size(
            true_prob=0.4,
            market_price=0.5,
            bankroll=100.0,
            kelly_fraction=0.25,
        )

        assert size <= 0, "Should not size for negative edge"

    def test_build_ladder_filters_no_edge(self):
        """Test ladder building filters out bins with no edge."""
        from ladder_builder import build_ladder, BookSnapshot

        bin_probs = [
            ("[15, 18)", 0.1, 0.05),
            ("[18, 21)", 0.5, 0.08),  # No edge here
            ("[21, 24)", 0.3, 0.06),
            ("[24, 27)", 0.1, 0.05),
        ]

        market_prices = {
            "token-1": 0.1,
            "token-2": 0.5,  # Market price = true prob
            "token-3": 0.3,
            "token-4": 0.1,
        }

        book_snapshots = {
            f"token-{i+1}": BookSnapshot(
                token_id=f"token-{i+1}",
                best_bid=price * 0.99,
                best_ask=price * 1.01,
                mid_price=price,
                spread=price * 0.02,
                relative_spread=0.02,
                bid_depth_top3=1000,
                ask_depth_top3=1000,
                total_bid_depth=5000,
                total_ask_depth=5000,
            )
            for i, (_, _, price) in enumerate([(0, 0, 0.1), (0, 0, 0.5), (0, 0, 0.3), (0, 0, 0.1)])
        }

        # Add correct token ids
        book_snapshots = {
            "token-1": BookSnapshot(
                token_id="token-1", best_bid=0.099, best_ask=0.101, mid_price=0.1,
                spread=0.002, relative_spread=0.02, bid_depth_top3=1000, ask_depth_top3=1000,
                total_bid_depth=5000, total_ask_depth=5000,
            ),
            "token-2": BookSnapshot(
                token_id="token-2", best_bid=0.495, best_ask=0.505, mid_price=0.5,
                spread=0.01, relative_spread=0.02, bid_depth_top3=1000, ask_depth_top3=1000,
                total_bid_depth=5000, total_ask_depth=5000,
            ),
            "token-3": BookSnapshot(
                token_id="token-3", best_bid=0.297, best_ask=0.303, mid_price=0.3,
                spread=0.006, relative_spread=0.02, bid_depth_top3=1000, ask_depth_top3=1000,
                total_bid_depth=5000, total_ask_depth=5000,
            ),
            "token-4": BookSnapshot(
                token_id="token-4", best_bid=0.099, best_ask=0.101, mid_price=0.1,
                spread=0.002, relative_spread=0.02, bid_depth_top3=1000, ask_depth_top3=1000,
                total_bid_depth=5000, total_ask_depth=5000,
            ),
        }

        result = build_ladder(
            bin_probs=bin_probs,
            market_prices=market_prices,
            book_snapshots=book_snapshots,
            min_theo_ev=0.05,
            diurnal_stage="pre-peak",
            hours_to_resolution=12,
            kelly_fraction=0.25,
            bankroll=100.0,
        )

        # Should skip token-2 (no edge)
        assert result.bins_traded <= 3, "Should skip bin with no edge"

    def test_ladder_size_scaling(self):
        """Test ladder size scaling with depth."""
        from ladder_builder import build_ladder, BookSnapshot

        bin_probs = [
            ("[15, 18)", 0.3, 0.05),
            ("[18, 21)", 0.5, 0.08),
            ("[21, 24)", 0.2, 0.06),
        ]

        market_prices = {
            "token-1": 0.2,
            "token-2": 0.5,
            "token-3": 0.2,
        }

        # Deep market
        book_snapshots_deep = {
            "token-1": BookSnapshot(
                token_id="token-1", best_bid=0.198, best_ask=0.202, mid_price=0.2,
                spread=0.004, relative_spread=0.02, bid_depth_top3=50000, ask_depth_top3=50000,
                total_bid_depth=100000, total_ask_depth=100000,
            ),
            "token-2": BookSnapshot(
                token_id="token-2", best_bid=0.495, best_ask=0.505, mid_price=0.5,
                spread=0.01, relative_spread=0.02, bid_depth_top3=50000, ask_depth_top3=50000,
                total_bid_depth=100000, total_ask_depth=100000,
            ),
            "token-3": BookSnapshot(
                token_id="token-3", best_bid=0.198, best_ask=0.202, mid_price=0.2,
                spread=0.004, relative_spread=0.02, bid_depth_top3=50000, ask_depth_top3=50000,
                total_bid_depth=100000, total_ask_depth=100000,
            ),
        }

        result = build_ladder(
            bin_probs=bin_probs,
            market_prices=market_prices,
            book_snapshots=book_snapshots_deep,
            min_theo_ev=0.05,
            diurnal_stage="pre-peak",
            hours_to_resolution=12,
            kelly_fraction=0.25,
            bankroll=100.0,
        )

        assert result.total_size > 0, "Should have positive sizing"
