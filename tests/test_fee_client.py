"""
Tests for fee_client module.

Covers spec bullet #21: Fee and rebate awareness.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fee_client import (
    get_fees_enabled,
    fetch_fee_rate_bps,
)


class TestFeeClient:
    """Tests for fee awareness client."""

    def test_fees_enabled_true(self):
        """Test detection of fees enabled in market."""
        market = {
            "feesEnabled": True,
            "feeRateBps": 10,
        }

        fees_enabled = get_fees_enabled(market)
        assert fees_enabled is True, "Should detect fees enabled"

    def test_fees_enabled_false(self):
        """Test detection of fees disabled in market."""
        market = {
            "feesEnabled": False,
            "feeRateBps": 0,
        }

        fees_enabled = get_fees_enabled(market)
        assert fees_enabled is False, "Should detect fees disabled"

    def test_fees_enabled_missing_field(self):
        """Test default behavior when feesEnabled field missing."""
        market = {
            "id": "market-xyz",
        }

        fees_enabled = get_fees_enabled(market)
        assert fees_enabled is False, "Should default to False when field missing"

    def test_fetch_fee_rate_bps(self):
        """Test fetching fee rate in basis points."""
        # In paper mode, should return default
        fee_bps = fetch_fee_rate_bps(
            token_id="token-123",
            paper_mode=True,
            default_fee_bps=10,
        )

        assert fee_bps == 10, "Should return default in paper mode"

    def test_fetch_fee_rate_bps_paper_mode_custom(self):
        """Test custom default in paper mode."""
        fee_bps = fetch_fee_rate_bps(
            token_id="token-456",
            paper_mode=True,
            default_fee_bps=25,
        )

        assert fee_bps == 25, "Should return custom default"

    def test_fee_cost_disabled(self):
        """Test that fees return 0 when disabled."""
        from risk_manager import compute_cost_proxy

        proxy = compute_cost_proxy(
            fill_prob=0.5,
            aggressiveness=0.5,
            depth=10000,
            relative_spread=0.001,
            fee_rate_bps=10,
            fees_enabled=False,
        )

        assert proxy.fee_cost == 0.0, "Should have no fee cost when disabled"

    def test_fee_cost_enabled(self):
        """Test fee cost included when enabled."""
        from risk_manager import compute_cost_proxy

        proxy_disabled = compute_cost_proxy(
            fill_prob=0.5,
            aggressiveness=0.5,
            depth=10000,
            relative_spread=0.001,
            fee_rate_bps=10,
            fees_enabled=False,
        )

        proxy_enabled = compute_cost_proxy(
            fill_prob=0.5,
            aggressiveness=0.5,
            depth=10000,
            relative_spread=0.001,
            fee_rate_bps=10,
            fees_enabled=True,
        )

        assert proxy_enabled.fee_cost > 0, "Should have fee cost when enabled"
        assert proxy_enabled.total > proxy_disabled.total, "Total cost should be higher with fees"
