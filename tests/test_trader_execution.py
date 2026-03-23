"""
Tests for trader_execution module.

Covers spec bullets #9 (Execution & fill quality) and #16 (NO handling).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from trader_execution import (
    compute_fill_prob,
    walk_order_book,
    normalize_to_yes,
    WalkResult,
)


class TestTraderExecution:
    """Tests for order execution and fill quality."""

    def test_fill_prob_calculation(self):
        """Test fill probability computation."""
        fill_prob = compute_fill_prob(
            relative_spread=0.001,
            depth=5000,
            recent_fill_rate=1.0,
        )

        assert 0.01 <= fill_prob <= 0.99, f"Fill prob should be in [0.01, 0.99], got {fill_prob}"

    def test_size_cap_default(self):
        """Test size cap for normal market depth."""
        from trader_execution import compute_size_cap

        depth = 5000
        size_cap = compute_size_cap(depth=depth, high_depth_threshold=30000)

        # Default cap: 20% of depth
        expected = depth * 0.20
        assert size_cap == expected, f"Expected {expected}, got {size_cap}"

    def test_size_cap_high_depth(self):
        """Test size cap for deep market."""
        from trader_execution import compute_size_cap

        depth = 50000
        size_cap = compute_size_cap(depth=depth, high_depth_threshold=30000)

        # High depth cap: 35% of depth
        expected = depth * 0.35
        assert size_cap == expected, f"Expected {expected}, got {size_cap}"

    def test_walk_book_levels(self):
        """Test walking order book levels."""
        book_levels = [
            {"price": 0.50, "size": 1000},
            {"price": 0.49, "size": 500},
            {"price": 0.48, "size": 300},
        ]

        result = walk_order_book(
            book_levels=book_levels,
            target_size=1500,
        )

        assert isinstance(result, WalkResult), "Should return WalkResult"
        assert result.total_filled >= 1000, "Should fill at least 1000"
        assert result.levels_walked >= 1, "Should have walked at least 1 level"

    def test_normalize_to_yes(self):
        """Test normalization of NO to YES representation."""
        # NO at price 0.3 = YES at price 0.7
        side_yes, price_yes = normalize_to_yes("NO", 0.3)

        assert side_yes == "YES", f"Expected YES, got {side_yes}"
        assert price_yes == 0.7, f"Expected 0.7, got {price_yes}"

        # YES at price 0.4 = YES at price 0.4
        side_yes, price_yes = normalize_to_yes("YES", 0.4)

        assert side_yes == "YES", f"Expected YES, got {side_yes}"
        assert price_yes == 0.4, f"Expected 0.4, got {price_yes}"

    def test_paper_adapter(self):
        """Test paper trading adapter."""
        from trader_execution import PaperAdapter

        adapter = PaperAdapter(paper_mode=True)

        # In paper mode, should not execute real orders
        assert adapter.paper_mode is True, "Should be in paper mode"
