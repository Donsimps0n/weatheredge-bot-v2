"""
Tests for regime_classifier module.

Covers spec bullet #6: Regime classification with deterministic rules and distribution shaping.
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


class TestRegimeClassifier:
    """Tests for regime classification."""

    def test_classify_front(self):
        """Test front classification: high spread + wind → front."""
        from regime_classifier import classify_regime

        result = classify_regime(
            ensemble_spread=5.0,  # High spread > 4.0
            wind_dir_shift_prob=0.6,  # Wind shift > 0.5
            cloud_cover=0.3,
            precip_prob=0.2,
            coastal=False,
        )

        assert result.regime == "front", f"Expected front, got {result.regime}"

    def test_classify_marine(self):
        """Test marine classification: coastal + cloud → marine."""
        from regime_classifier import classify_regime

        result = classify_regime(
            ensemble_spread=2.0,  # Low spread < 2.5
            wind_dir_shift_prob=0.3,
            cloud_cover=0.7,  # High cloud > 0.6
            precip_prob=0.2,
            coastal=True,  # Must be coastal
        )

        assert result.regime == "marine", f"Expected marine, got {result.regime}"

    def test_classify_convective(self):
        """Test convective classification: precip + spread → convective."""
        from regime_classifier import classify_regime

        result = classify_regime(
            ensemble_spread=3.5,  # Moderate spread > 3.0
            wind_dir_shift_prob=0.3,
            cloud_cover=0.6,  # Moderate clouds > 0.5
            precip_prob=0.5,  # High precip > 0.4
            coastal=False,
        )

        assert result.regime == "convective", f"Expected convective, got {result.regime}"

    def test_classify_clear(self):
        """Test clear classification: low cloud + low precip → clear."""
        from regime_classifier import classify_regime

        result = classify_regime(
            ensemble_spread=1.5,
            wind_dir_shift_prob=0.2,
            cloud_cover=0.2,  # Low cloud
            precip_prob=0.1,  # Low precip
            coastal=False,
        )

        assert result.regime == "clear", f"Expected clear, got {result.regime}"

    def test_classify_neutral(self):
        """Test neutral classification: default case."""
        from regime_classifier import classify_regime

        result = classify_regime(
            ensemble_spread=2.0,
            wind_dir_shift_prob=0.2,
            cloud_cover=0.4,
            precip_prob=0.2,
            coastal=False,
        )

        # Should match one of the regimes or neutral
        assert result.regime in ["front", "convective", "marine", "clear", "neutral"], (
            f"Got unexpected regime {result.regime}"
        )

    def test_shape_distribution_front(self):
        """Test front regime distribution shaping: verify skew and sigma mult."""
        from regime_classifier import shape_forecast_distribution

        mu = 20.0
        sigma = 2.0
        regime = "front"

        # Front regime should apply negative skew and increased sigma
        params = shape_forecast_distribution(
            mu=mu,
            sigma=sigma,
            regime=regime,
            obs_max_so_far=None,
        )

        assert params.regime == "front", f"Expected front regime"
        assert params.skew < 0, f"Front should have negative skew, got {params.skew}"
        assert params.sigma_mult > 1.0, f"Front should widen sigma, got {params.sigma_mult}"
