"""
Tests for time_utils module.

Covers spec bullet #4 (Diurnal staging gates) and #15 (Backtest time-causal).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from time_utils import (
    get_peak_window,
    get_diurnal_stage,
    apply_diurnal_constraints,
)


class TestPeakWindow:
    """Tests for peak window calculation."""

    def test_peak_window_high_lat(self):
        """Test lat=55 → (13, 16)."""
        peak_start, peak_end = get_peak_window(lat=55, coastal=False)
        assert peak_start == 13, f"Expected 13, got {peak_start}"
        assert peak_end == 16, f"Expected 16, got {peak_end}"

    def test_peak_window_mid_lat(self):
        """Test lat=40 → (14, 17)."""
        peak_start, peak_end = get_peak_window(lat=40, coastal=False)
        assert peak_start == 14, f"Expected 14, got {peak_start}"
        assert peak_end == 17, f"Expected 17, got {peak_end}"

    def test_peak_window_low_lat(self):
        """Test lat=20 → (15, 18)."""
        peak_start, peak_end = get_peak_window(lat=20, coastal=False)
        assert peak_start == 15, f"Expected 15, got {peak_start}"
        assert peak_end == 18, f"Expected 18, got {peak_end}"

    def test_peak_window_coastal(self):
        """Test lat=40, coastal=True → (15, 18)."""
        peak_start, peak_end = get_peak_window(lat=40, coastal=True)
        # Base is (14, 17), add 1h for coastal
        assert peak_start == 15, f"Expected 15 (14+1), got {peak_start}"
        assert peak_end == 18, f"Expected 18 (17+1), got {peak_end}"


class TestDiurnalStage:
    """Tests for diurnal stage determination."""

    def test_diurnal_stage_pre_peak(self):
        """Test time before peak → pre-peak."""
        # Create a datetime at 10:00 (before peak_start=14)
        dt = datetime(2023, 6, 1, 10, 0, 0)
        stage = get_diurnal_stage(dt, peak_start=14, peak_end=17)
        assert stage == "pre-peak", f"Expected pre-peak, got {stage}"

    def test_diurnal_stage_near_peak(self):
        """Test time during peak window → near-peak."""
        # Create a datetime at 15:00 (within 14-17)
        dt = datetime(2023, 6, 1, 15, 0, 0)
        stage = get_diurnal_stage(dt, peak_start=14, peak_end=17)
        assert stage == "near-peak", f"Expected near-peak, got {stage}"

    def test_diurnal_stage_post_peak(self):
        """Test time after peak → post-peak."""
        # Create a datetime at 18:00 (after peak_end=17)
        dt = datetime(2023, 6, 1, 18, 0, 0)
        stage = get_diurnal_stage(dt, peak_start=14, peak_end=17)
        assert stage == "post-peak", f"Expected post-peak, got {stage}"


class TestDiurnalConstraints:
    """Tests for diurnal constraints application."""

    def test_diurnal_near_peak_constraints(self):
        """Test near-peak: verify size cap and EV boost."""
        decision = apply_diurnal_constraints(
            stage="near-peak",
            theo_ev=0.15,
            kelly_size=10.0,
        )

        # Near-peak should have:
        # - min_ev_boost of 0.02
        # - size_cap = 15% of kelly (0.15 * 10.0 = 1.5)
        # - ladder_bins = (4, 5) (LADDER_BINS_NEAR_PEAK)
        assert decision.allow_entry is True, "Should allow entry in near-peak"
        assert decision.min_ev_boost == 0.02, f"Expected boost 0.02, got {decision.min_ev_boost}"
        assert decision.size_cap == 1.5, f"Expected cap 1.5, got {decision.size_cap}"
        assert decision.ladder_bins == (4, 5), f"Expected (4, 5), got {decision.ladder_bins}"

    def test_diurnal_post_peak_blocked(self):
        """Test post-peak: verify entry blocked when conditions not met."""
        decision = apply_diurnal_constraints(
            stage="post-peak",
            theo_ev=0.10,  # Below POST_PEAK_MIN_EV (0.18)
            kelly_size=10.0,
            obs_max=25.0,
            obs_percentile_75=24.0,  # obs_max > obs_percentile_75 fails condition
        )

        # Should block entry because obs_max >= obs_percentile_75
        assert decision.allow_entry is False, "Should block entry in post-peak"
        assert decision.block_reason is not None, "Should have block reason"

    def test_diurnal_pre_peak_default(self):
        """Test pre-peak: default constraints (allow entry, no caps)."""
        decision = apply_diurnal_constraints(
            stage="pre-peak",
            theo_ev=0.05,
            kelly_size=10.0,
        )

        # Pre-peak should allow entry with no size cap
        assert decision.allow_entry is True, "Should allow entry in pre-peak"
        assert decision.size_cap is None, "Should have no size cap in pre-peak"
        assert decision.min_ev_boost == 0.0, "Should have no EV boost in pre-peak"
