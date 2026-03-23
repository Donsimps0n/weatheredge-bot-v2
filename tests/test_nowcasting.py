"""
Tests for nowcasting module.

Covers spec bullet #5: Nowcasting in last 24h with observation anchoring and AR(1) residuals.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import datetime, timedelta

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

pytestmark = pytest.mark.skipif(not HAS_NUMPY, reason="numpy not installed")


class TestNowcasting:
    """Tests for nowcasting module."""

    def test_observation_sanity_normal(self):
        """Test observation sanity check: small diff → no anomaly."""
        from nowcasting import observation_sanity

        obs_temp = 20.0
        mu_now = 20.5
        obs_timestamp = datetime.now() - timedelta(minutes=10)
        now = datetime.now()

        result = observation_sanity(obs_temp, mu_now, obs_timestamp, now)

        # Small difference (0.5°C) and recent observation should not flag
        assert result.anomaly_flag is False, "Should not flag as anomaly"
        assert result.sigma_widen_factor == 1.0, "Should not widen sigma"
        assert result.obs_weight == 1.0, "Should not reduce obs weight"

    def test_observation_sanity_anomaly(self):
        """Test observation sanity check: large diff → anomaly flag."""
        from nowcasting import observation_sanity

        obs_temp = 10.0
        mu_now = 20.0  # 10°C difference > OBS_ANOMALY_TEMP_THRESHOLD (6.0)
        obs_timestamp = datetime.now() - timedelta(minutes=10)
        now = datetime.now()

        result = observation_sanity(obs_temp, mu_now, obs_timestamp, now)

        # Large difference should flag
        assert result.anomaly_flag is True, "Should flag as anomaly"
        assert len(result.reasons) > 0, "Should have reasons"

    def test_ar1_residuals_shape(self):
        """Test AR(1) residuals output shape matches n_hours."""
        from nowcasting import ar1_residuals

        n_hours = 24
        rho = 0.78
        sigma = 2.0

        residuals = ar1_residuals(n_hours, rho, sigma)

        assert len(residuals) == n_hours, f"Expected {n_hours} residuals, got {len(residuals)}"

    def test_ar1_residuals_initial_zero(self):
        """Test AR(1) residuals: e_0 must be 0."""
        from nowcasting import ar1_residuals

        n_hours = 24
        rho = 0.78
        sigma = 2.0

        residuals = ar1_residuals(n_hours, rho, sigma)

        assert residuals[0] == 0.0, f"First residual e_0 must be 0, got {residuals[0]}"

    def test_compute_mu_adj(self):
        """Test compute_mu_adj: verify exponential decay."""
        from nowcasting import compute_mu_adj

        # At t=0, mu_adj should be close to obs_temp
        # As t increases, decay toward mu_forecast
        mu_forecast = 20.0
        obs_temp = 25.0
        half_life = 4.0  # hours

        # At t=0
        adj_0 = compute_mu_adj(t_hours=0, obs_temp=obs_temp, mu_forecast=mu_forecast, half_life=half_life)
        assert abs(adj_0 - obs_temp) < 0.1, f"At t=0, should be ≈ {obs_temp}, got {adj_0}"

        # At t=half_life, decay by 50%
        adj_hl = compute_mu_adj(t_hours=half_life, obs_temp=obs_temp, mu_forecast=mu_forecast, half_life=half_life)
        expected_hl = mu_forecast + (obs_temp - mu_forecast) * 0.5
        assert abs(adj_hl - expected_hl) < 0.5, f"At t={half_life}, expected ≈{expected_hl}, got {adj_hl}"

    def test_nowcast_produces_probabilities(self):
        """Test nowcasting produces valid probability distribution."""
        from nowcasting import nowcast_ensemble

        # Create mock inputs
        forecast_temps = np.random.normal(20, 2, 100)
        obs_temp = 20.5
        obs_timestamp = datetime.now() - timedelta(hours=2)
        now = datetime.now()
        bin_edges = [15, 18, 21, 24, 27]

        result = nowcast_ensemble(
            forecast_temps=forecast_temps,
            obs_temp=obs_temp,
            obs_timestamp=obs_timestamp,
            now=now,
            bin_edges=bin_edges,
        )

        # Should have probabilities for each bin
        assert hasattr(result, 'bin_probs'), "Result should have bin_probs"
        assert len(result.bin_probs) == len(bin_edges) - 1, "Should have one prob per bin"

        # Probabilities should sum to ~1.0
        prob_sum = sum(result.bin_probs)
        assert abs(prob_sum - 1.0) < 0.1, f"Probabilities should sum to ~1.0, got {prob_sum}"

        # Each probability should be in [0, 1]
        for p in result.bin_probs:
            assert 0 <= p <= 1, f"Probability {p} should be in [0, 1]"
