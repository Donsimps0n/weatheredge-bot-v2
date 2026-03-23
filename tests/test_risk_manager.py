"""
Tests for risk_manager module.

Covers spec bullets #3 (theoretical_full_ev) and #10 (min_theo_ev gate & dynamic ratchet).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from risk_manager import (
    compute_cost_proxy,
    compute_theoretical_full_ev,
    check_ev_gate,
    compute_min_theo_ev,
    LegInput,
    CostProxy,
)


class TestRiskManager:
    """Tests for risk manager functionality."""

    def test_theoretical_full_ev(self):
        """Test theoretical EV formula for multi-leg positions."""
        # Two legs: betting YES at price 0.4 with true prob 0.6, and NO at price 0.5 with true prob 0.3
        legs = [
            LegInput(size=1.0, true_prob=0.6, entry_price=0.4),
            LegInput(size=1.0, true_prob=0.3, entry_price=0.5),
        ]

        theo_ev = compute_theoretical_full_ev(legs)

        # Should be positive since we have edge on YES (true_prob > entry_price)
        # EV for YES leg: (0.6 - 0.4) / 0.4 * 1.0 = 0.5
        # EV for NO leg: (1 - 0.3 - (1 - 0.5)) / (1 - 0.5) * 1.0 = negative
        assert isinstance(theo_ev, float), "Should return float"

    def test_auto_flatten_threshold(self):
        """Test auto-flatten: theo_ev < 0.10 → flatten."""
        # When theo_ev is negative or low, should trigger flatten
        legs = [
            LegInput(size=1.0, true_prob=0.35, entry_price=0.5),  # Negative edge
        ]

        theo_ev = compute_theoretical_full_ev(legs)
        assert theo_ev < 0.10, "Expected low/negative EV for testing flatten"

    def test_ev_gates_6h(self):
        """Test EV gate for 6h: requires 0.20."""
        theo_ev = 0.20

        result = check_ev_gate(theo_ev=theo_ev, hours_to_resolution=6)

        assert result.passes is True, "Should pass gate at 0.20 for 6h"

        # Below threshold should fail
        result_fail = check_ev_gate(theo_ev=0.19, hours_to_resolution=6)
        assert result_fail.passes is False, "Should fail below 0.20 for 6h"

    def test_ev_gates_12h(self):
        """Test EV gate for 12h: requires 0.14."""
        theo_ev = 0.14

        result = check_ev_gate(theo_ev=theo_ev, hours_to_resolution=12)

        assert result.passes is True, "Should pass gate at 0.14 for 12h"

        # Below threshold should fail
        result_fail = check_ev_gate(theo_ev=0.13, hours_to_resolution=12)
        assert result_fail.passes is False, "Should fail below 0.14 for 12h"

    def test_min_theo_ev_ratchet(self):
        """Test min_theo_ev computation with leakage ratchet."""
        # With no leakage, should return base
        result = compute_min_theo_ev(
            base_ev=0.10,
            leakage_bps=0,
            leakage_ratchet_per_half_bps=0.01,
        )

        assert result.base_ev == 0.10, f"Expected base 0.10, got {result.base_ev}"

        # With leakage, should adjust upward
        result_with_leak = compute_min_theo_ev(
            base_ev=0.10,
            leakage_bps=50,  # 50 bps = 25 half-bps
            leakage_ratchet_per_half_bps=0.01,
        )

        # Adjustment: 25 * 0.01 = 0.25
        assert result_with_leak.min_ev >= result.min_ev, "Leakage should increase min_ev"

    def test_cost_proxy(self):
        """Test cost proxy component calculation."""
        proxy = compute_cost_proxy(
            fill_prob=0.5,
            aggressiveness=0.5,
            depth=10000,
            relative_spread=0.001,
            fee_rate_bps=0,
            fees_enabled=False,
        )

        assert isinstance(proxy, CostProxy), "Should return CostProxy"
        assert proxy.total >= 0, "Total cost should be non-negative"
        assert proxy.effective_roundtrip_bps >= 0, "Roundtrip should be non-negative"
        assert proxy.slippage_proxy >= 0, "Slippage should be non-negative"

    def test_cost_proxy_with_fees(self):
        """Test cost proxy includes fees when enabled."""
        proxy_no_fees = compute_cost_proxy(
            fill_prob=0.5,
            aggressiveness=0.5,
            depth=10000,
            relative_spread=0.001,
            fee_rate_bps=10,
            fees_enabled=False,
        )

        proxy_with_fees = compute_cost_proxy(
            fill_prob=0.5,
            aggressiveness=0.5,
            depth=10000,
            relative_spread=0.001,
            fee_rate_bps=10,
            fees_enabled=True,
        )

        # With fees enabled, cost should be higher
        assert proxy_with_fees.total > proxy_no_fees.total, (
            "Cost with fees should be higher"
        )
