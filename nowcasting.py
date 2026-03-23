"""
Nowcasting module for Polymarket temperature trading bot.

Handles spec bullet #5 (Nowcasting in last 24h):
- Observation anchoring with half-life decay
- AR(1) residual modeling
- Monte Carlo sampling (5000 samples)
- Bin probability distribution conversion
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List
import math

import numpy as np

# Import config constants
from config import (
    HALF_LIFE_NEAR_PEAK,
    HALF_LIFE_DEFAULT,
    HALF_LIFE_COASTAL,
    AR1_RHO_DEFAULT,
    AR1_RHO_COASTAL,
    MONTE_CARLO_SAMPLES,
    OBS_ANOMALY_TEMP_THRESHOLD,
    OBS_ANOMALY_TIME_THRESHOLD,
    OBS_SIGMA_WIDEN_FACTOR,
)


@dataclass
class ObsSanityResult:
    """Result of observation sanity check."""
    anomaly_flag: bool
    sigma_widen_factor: float
    obs_weight: float
    reasons: List[str]


@dataclass
class NowcastResult:
    """Result of nowcasting distribution computation."""
    bin_probs: List[float]
    bin_labels: List[str]
    obs_sanity: ObsSanityResult
    offset_used: float
    half_life_used: float
    rho_used: float


def observation_sanity(
    obs_temp: float,
    mu_now: float,
    obs_timestamp: datetime,
    now: datetime,
) -> ObsSanityResult:
    """
    Check observation sanity and determine adjustments.

    Flags anomalies if:
    - |T_obs_now - mu_now| > OBS_ANOMALY_TEMP_THRESHOLD
    - obs_timestamp is stale (> OBS_ANOMALY_TIME_THRESHOLD minutes)

    If anomalous:
    - Reduce obs_weight
    - Widen sigma by OBS_SIGMA_WIDEN_FACTOR
    - Log anomaly reasons

    Args:
        obs_temp: Current observed temperature
        mu_now: Current forecast mean
        obs_timestamp: Timestamp of observation
        now: Current datetime

    Returns:
        ObsSanityResult with anomaly_flag, sigma_widen_factor, obs_weight, reasons
    """
    reasons = []
    anomaly_flag = False
    sigma_widen_factor = 1.0
    obs_weight = 1.0

    # Check temperature deviation
    temp_deviation = abs(obs_temp - mu_now)
    if temp_deviation > OBS_ANOMALY_TEMP_THRESHOLD:
        anomaly_flag = True
        reasons.append(
            f"Temperature deviation {temp_deviation:.2f}°C exceeds threshold "
            f"{OBS_ANOMALY_TEMP_THRESHOLD}°C"
        )

    # Check observation staleness
    time_delta_minutes = (now - obs_timestamp).total_seconds() / 60.0
    if time_delta_minutes > OBS_ANOMALY_TIME_THRESHOLD:
        anomaly_flag = True
        reasons.append(
            f"Observation staleness {time_delta_minutes:.1f} min exceeds threshold "
            f"{OBS_ANOMALY_TIME_THRESHOLD} min"
        )

    # Apply anomaly adjustments
    if anomaly_flag:
        sigma_widen_factor = OBS_SIGMA_WIDEN_FACTOR
        obs_weight = 0.5  # Reduce weight for anomalous observations

    return ObsSanityResult(
        anomaly_flag=anomaly_flag,
        sigma_widen_factor=sigma_widen_factor,
        obs_weight=obs_weight,
        reasons=reasons,
    )


def compute_mu_adj(
    mu_h: float,
    offset: float,
    h: float,
    half_life: float,
) -> float:
    """
    Compute adjusted forecast mean with observation anchoring.

    Formula: mu_h_adj = mu_h + offset * exp(-h / half_life)

    Args:
        mu_h: Forecast mean at hour h
        offset: Temperature observation offset (T_obs_now - mu_now)
        h: Hours ahead from now
        half_life: Half-life decay constant (hours)

    Returns:
        Adjusted forecast mean
    """
    return mu_h + offset * math.exp(-h / half_life)


def ar1_residuals(
    rho: float,
    sigma_hourly: List[float],
    n_hours: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """
    Generate AR(1) residuals for hourly temperature deviations.

    Formula:
    - e_0 = 0
    - e_h = rho * e_{h-1} + sqrt(1 - rho^2) * z_h, where z_h ~ N(0, sigma_h)

    Args:
        rho: AR(1) coefficient (typically 0.70-0.78)
        sigma_hourly: List of hourly standard deviations (length n_hours)
        n_hours: Number of hours to generate
        rng: numpy random generator

    Returns:
        Array of residuals e_0..e_{n_hours-1}
    """
    residuals = np.zeros(n_hours)
    residuals[0] = 0.0

    # AR(1) evolution
    sqrt_factor = math.sqrt(1.0 - rho * rho)

    for h in range(1, n_hours):
        z_h = rng.standard_normal()
        residuals[h] = rho * residuals[h - 1] + sqrt_factor * sigma_hourly[h] * z_h

    return residuals


def samples_to_bin_probs(
    day_max_samples: np.ndarray,
    bin_edges: List[float],
) -> List[float]:
    """
    Convert empirical distribution of day_max samples to bin probabilities.

    Bins are defined as: [edge0, edge1), [edge1, edge2), ..., [edgeN-1, edgeN]
    Also includes <edge0 and >=edgeN as tail bins.

    Args:
        day_max_samples: Array of day_max values from Monte Carlo samples
        bin_edges: List of bin edge boundaries

    Returns:
        List of probabilities for each bin (length = len(bin_edges) + 1)
    """
    n_samples = len(day_max_samples)
    n_bins = len(bin_edges) + 1
    bin_counts = np.zeros(n_bins, dtype=int)

    # Count samples in <edge0
    bin_counts[0] = np.sum(day_max_samples < bin_edges[0])

    # Count samples in [edge_i, edge_{i+1})
    for i in range(len(bin_edges) - 1):
        mask = (day_max_samples >= bin_edges[i]) & (day_max_samples < bin_edges[i + 1])
        bin_counts[i + 1] = np.sum(mask)

    # Count samples in >=edge_N
    bin_counts[-1] = np.sum(day_max_samples >= bin_edges[-1])

    # Convert counts to probabilities
    bin_probs = (bin_counts / n_samples).tolist()

    return bin_probs


def nowcast_distribution(
    obs_temp_now: float,
    mu_now: float,
    mu_forecast_hourly: List[float],
    sigma_hourly: List[float],
    obs_max_so_far: float,
    hours_remaining: float,
    is_coastal: bool,
    is_near_peak: bool,
    obs_timestamp: datetime,
    now: datetime,
    bin_edges: List[float],
    n_samples: int = MONTE_CARLO_SAMPLES,
) -> NowcastResult:
    """
    Compute nowcasting distribution of daily maximum temperature.

    Performs Monte Carlo sampling with observation anchoring and AR(1) residuals.

    Algorithm:
    a) Run observation_sanity check
    b) Compute offset, reduce if anomaly
    c) Determine half_life and rho based on conditions
    d) For n_samples iterations:
       - Generate AR1 residuals (widen sigma if anomaly)
       - Compute T_h = mu_h_adj + e_h for each remaining hour
       - Compute future_max and day_max = max(obs_max_so_far, future_max)
    e) Convert day_max distribution to bin probabilities
    f) Return NowcastResult

    Args:
        obs_temp_now: Current observed temperature
        mu_now: Current forecast mean
        mu_forecast_hourly: List of forecast means for remaining hours
        sigma_hourly: List of forecast std devs for remaining hours
        obs_max_so_far: Maximum observed temperature so far today
        hours_remaining: Number of hours remaining in the day
        is_coastal: Whether location is coastal
        is_near_peak: Whether currently near peak temperature hour
        obs_timestamp: Timestamp of observation
        now: Current datetime
        bin_edges: Temperature bin edges for probability distribution
        n_samples: Number of Monte Carlo samples (default 5000)

    Returns:
        NowcastResult with bin probabilities, labels, and metadata
    """
    # Step a: Run observation sanity check
    obs_sanity = observation_sanity(obs_temp_now, mu_now, obs_timestamp, now)

    # Step b: Compute offset, reduce if anomaly
    offset = obs_temp_now - mu_now
    if obs_sanity.anomaly_flag:
        offset *= 0.5  # Reduce offset weight for anomalies

    # Step c: Determine half_life and rho
    if is_near_peak:
        half_life = HALF_LIFE_NEAR_PEAK
    elif is_coastal:
        half_life = HALF_LIFE_COASTAL
    else:
        half_life = HALF_LIFE_DEFAULT

    rho = AR1_RHO_COASTAL if is_coastal else AR1_RHO_DEFAULT

    # Step d: Monte Carlo sampling
    n_hours = int(hours_remaining)
    if n_hours <= 0:
        n_hours = 1

    rng = np.random.default_rng()
    day_max_samples = np.zeros(n_samples)

    for sample_idx in range(n_samples):
        # Adjust sigma if anomaly
        sigma_adj = np.array(sigma_hourly[:n_hours]) * obs_sanity.sigma_widen_factor

        # Generate AR1 residuals
        residuals = ar1_residuals(rho, sigma_adj.tolist(), n_hours, rng)

        # Compute hourly temperatures and future_max
        temps = []
        for h in range(n_hours):
            mu_h_adj = compute_mu_adj(
                mu_forecast_hourly[h], offset, float(h), half_life
            )
            temp_h = mu_h_adj + residuals[h]
            temps.append(temp_h)

        future_max = np.max(temps) if temps else obs_temp_now

        # Compute day_max
        day_max = max(obs_max_so_far, future_max)
        day_max_samples[sample_idx] = day_max

    # Step e: Convert to bin probabilities
    bin_probs = samples_to_bin_probs(day_max_samples, bin_edges)

    # Generate bin labels
    bin_labels = []
    bin_labels.append(f"< {bin_edges[0]:.1f}")
    for i in range(len(bin_edges) - 1):
        bin_labels.append(f"[{bin_edges[i]:.1f}, {bin_edges[i + 1]:.1f})")
    bin_labels.append(f">= {bin_edges[-1]:.1f}")

    # Step f: Return NowcastResult
    return NowcastResult(
        bin_probs=bin_probs,
        bin_labels=bin_labels,
        obs_sanity=obs_sanity,
        offset_used=offset,
        half_life_used=half_life,
        rho_used=rho,
    )
