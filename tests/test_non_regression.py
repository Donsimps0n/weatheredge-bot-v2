"""
Non-regression tests for critical spec bullets from #19.

These tests MUST fail if critical bugs regress:
- EV per dollar calculation
- KDE uncertainty quantification
- Bayesian smoothing shrinkage of extremes
- Consensus blending with weighted total variance
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

try:
    import numpy as np
    from scipy import stats
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


@pytest.mark.skipif(not HAS_SCIPY, reason="scipy/numpy not installed")
class TestNonRegression:
    """Non-regression tests for critical functionality."""

    def test_ev_per_dollar_yes(self):
        """Test EV per $ staked for YES must be (p - P) / P.

        Non-regression #19a: Verify formula works correctly.
        """
        from probability_calculator import compute_ev_per_dollar_yes

        # Test case 1: true_prob=0.6, price=0.4 → EV = (0.6-0.4)/0.4 = 0.5
        ev1 = compute_ev_per_dollar_yes(true_prob=0.6, entry_price=0.4)
        expected1 = (0.6 - 0.4) / 0.4
        assert abs(ev1 - expected1) < 1e-6, f"Expected {expected1}, got {ev1}"

        # Test case 2: true_prob=0.3, price=0.5 → EV = (0.3-0.5)/0.5 = -0.4
        ev2 = compute_ev_per_dollar_yes(true_prob=0.3, entry_price=0.5)
        expected2 = (0.3 - 0.5) / 0.5
        assert abs(ev2 - expected2) < 1e-6, f"Expected {expected2}, got {ev2}"

    def test_kde_integrate_box(self):
        """Test KDE must not crash and must call integrate_box_1d.

        Non-regression #19b: Generate 100 samples from N(70, 3).
        Call kde_with_uncertainty(samples, 68, 72).
        Assert result is tuple of (p, u) where 0 < p < 1 and 0 <= u <= 0.5.
        """
        from probability_calculator import kde_with_uncertainty

        # Generate 100 samples from N(70, 3)
        np.random.seed(42)
        samples = np.random.normal(loc=70, scale=3, size=100)

        # Call kde_with_uncertainty
        p, u = kde_with_uncertainty(samples, lo=68, hi=72)

        # Verify result is tuple with valid bounds
        assert isinstance(p, float), f"p should be float, got {type(p)}"
        assert isinstance(u, float), f"u should be float, got {type(u)}"
        assert 0 < p < 1, f"p must be in (0, 1), got {p}"
        assert 0 <= u <= 0.5, f"u must be in [0, 0.5], got {u}"

    def test_bayesian_no_extremes(self):
        """Test Bayesian smoothing must shrink extremes.

        Non-regression #19c:
        - k=0, n=10: p must be > 0 (not 0.0)
        - k=10, n=10: p must be < 1 (not 1.0)
        """
        from probability_calculator import bayesian_smoothing

        # Test k=0, n=10: p must be > 0
        p_min, u_min = bayesian_smoothing(k=0, n=10)
        assert p_min > 0.0, f"k=0, n=10 should shrink from 0, got {p_min}"
        assert 0.0 <= u_min <= 0.5, f"u should be in [0, 0.5], got {u_min}"

        # Test k=10, n=10: p must be < 1
        p_max, u_max = bayesian_smoothing(k=10, n=10)
        assert p_max < 1.0, f"k=10, n=10 should shrink from 1, got {p_max}"
        assert 0.0 <= u_max <= 0.5, f"u should be in [0, 0.5], got {u_max}"

    def test_consensus_total_variance(self):
        """Test consensus uncertainty uses weighted total variance (law of total variance).

        Non-regression #19d:
        - sources = [(0.3, 0.05, 1.0), (0.7, 0.05, 1.0)]  # very different probs
        - p_blend should be 0.5
        - u_blend must be > 0.05 (because disagreement adds variance)
        - u_blend^2 should ≈ sum(0.5 * (0.05^2 + (p_i - 0.5)^2))
        - = 0.5*(0.0025 + 0.04) + 0.5*(0.0025 + 0.04) = 0.0425
        - u_blend ≈ 0.206
        - Assert u_blend > 0.15 (much larger than individual uncertainties)
        """
        from probability_calculator import consensus_blend

        # Two sources with very different probabilities
        sources = [
            (0.3, 0.05, 1.0),  # prob=0.3, uncertainty=0.05, weight=1.0
            (0.7, 0.05, 1.0),  # prob=0.7, uncertainty=0.05, weight=1.0
        ]

        p_blend, u_blend = consensus_blend(sources)

        # p_blend should be 0.5 (average of 0.3 and 0.7)
        assert abs(p_blend - 0.5) < 1e-6, f"p_blend should be 0.5, got {p_blend}"

        # u_blend must be > 0.05 (larger than individual uncertainties)
        assert u_blend > 0.05, f"u_blend {u_blend} must be > 0.05 (disagreement adds variance)"

        # Verify variance calculation:
        # within_var = 0.5 * 0.05^2 + 0.5 * 0.05^2 = 0.5 * 0.0025 + 0.5 * 0.0025 = 0.0025
        # between_var = 0.5 * (0.3 - 0.5)^2 + 0.5 * (0.7 - 0.5)^2
        #            = 0.5 * 0.04 + 0.5 * 0.04 = 0.04
        # total_var = 0.0025 + 0.04 = 0.0425
        # u_blend = sqrt(0.0425) ≈ 0.2062

        expected_variance = 0.0425
        expected_u_blend = np.sqrt(expected_variance)

        assert abs(u_blend - expected_u_blend) < 1e-2, (
            f"u_blend {u_blend} should be ≈ {expected_u_blend}"
        )

        # Assert u_blend > 0.15 (much larger than individual uncertainties)
        assert u_blend > 0.15, (
            f"u_blend {u_blend} must be > 0.15 "
            f"(demonstrates disagreement increases uncertainty)"
        )
