"""
Regime classifier and distribution shaping for Polymarket temperature trading bot.

Implements spec bullet #6: Regime classification with deterministic rules and
distribution shaping per weather regime.
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np

# Configuration constants
FRONT_SKEW = -1.5  # degrees C
FRONT_SIGMA_MULT = 1.5
MARINE_SKEW = -1.2  # degrees C
MARINE_UPPER_CLAMP = True
CONVECTIVE_P_STORM = 0.32
CONVECTIVE_MAX_CAP_OFFSET = 1.5  # degrees C above obs_max_so_far
CLEAR_WARM_BIAS_MIN = 0.8  # degrees C
CLEAR_WARM_BIAS_MAX = 1.5  # degrees C
CLEAR_SIGMA_MULT = 0.8


@dataclass
class RegimeResult:
    """Result of regime classification."""
    regime: str
    features: dict
    confidence_note: str


@dataclass
class ShapedDistParams:
    """Parameters for shaped temperature distribution."""
    mu_adj: float
    sigma_adj: float
    skew: float
    warm_bias: float
    is_mixture: bool
    p_storm: float
    storm_max_cap: Optional[float]
    upper_clamp: bool


def classify_regime(
    ensemble_spread: float,
    wind_dir_shift_prob: float,
    cloud_cover: float,
    precip_prob: float,
    coastal: bool = False,
) -> RegimeResult:
    """
    Classify weather regime into one of five categories.

    Checks in order: front, convective, marine, clear, neutral.
    Returns the first matching regime.

    Args:
        ensemble_spread: Spread of ensemble forecasts (°C)
        wind_dir_shift_prob: Probability of significant wind direction shift (0-1)
        cloud_cover: Fraction of cloud cover (0-1)
        precip_prob: Probability of precipitation (0-1)
        coastal: Whether location is coastal (for marine regime check)

    Returns:
        RegimeResult with regime name, feature dict, and confidence note.
    """
    features = {
        "ensemble_spread": ensemble_spread,
        "wind_dir_shift_prob": wind_dir_shift_prob,
        "cloud_cover": cloud_cover,
        "precip_prob": precip_prob,
        "coastal": coastal,
    }

    # Check in order: front, convective, marine, clear, neutral

    # Front: high spread AND significant wind shift
    if ensemble_spread > 4.0 and wind_dir_shift_prob > 0.5:
        return RegimeResult(
            regime="front",
            features=features,
            confidence_note="Strong frontal signature: high spread and wind shift",
        )

    # Convective: high precip probability with moderately high spread and clouds
    if precip_prob > 0.4 and ensemble_spread > 3.0 and cloud_cover > 0.5:
        return RegimeResult(
            regime="convective",
            features=features,
            confidence_note="Convective potential: precip + spread + clouds",
        )

    # Marine: coastal with high clouds and low spread
    if coastal and cloud_cover > 0.6 and ensemble_spread < 2.5:
        return RegimeResult(
            regime="marine",
            features=features,
            confidence_note="Marine layer: coastal + high clouds + low spread",
        )

    # Clear: low clouds, low precip, low spread
    if cloud_cover < 0.2 and precip_prob < 0.1 and ensemble_spread < 2.0:
        return RegimeResult(
            regime="clear",
            features=features,
            confidence_note="Clear sky conditions: minimal clouds, precip, and spread",
        )

    # Neutral: anything else
    return RegimeResult(
        regime="neutral",
        features=features,
        confidence_note="No strong regime signature; neutral conditions",
    )


def shape_distribution(
    regime: str,
    mu: float,
    sigma: float,
    obs_max_so_far: Optional[float] = None,
) -> ShapedDistParams:
    """
    Generate distribution shaping parameters based on regime.

    Applies regime-specific adjustments to mean, standard deviation, and shape.

    Args:
        regime: One of "front", "marine", "convective", "clear", "neutral"
        mu: Mean temperature (°C)
        sigma: Standard deviation (°C)
        obs_max_so_far: Maximum observed temperature to date (for convective cap)

    Returns:
        ShapedDistParams with all adjustments for applying to samples.
    """

    if regime == "front":
        return ShapedDistParams(
            mu_adj=mu,
            sigma_adj=sigma * FRONT_SIGMA_MULT,
            skew=FRONT_SKEW,
            warm_bias=0.0,
            is_mixture=False,
            p_storm=0.0,
            storm_max_cap=None,
            upper_clamp=False,
        )

    elif regime == "marine":
        return ShapedDistParams(
            mu_adj=mu,
            sigma_adj=sigma * 1.0,  # No sigma multiplier for marine
            skew=MARINE_SKEW,
            warm_bias=0.0,
            is_mixture=False,
            p_storm=0.0,
            storm_max_cap=None,
            upper_clamp=MARINE_UPPER_CLAMP,
        )

    elif regime == "convective":
        storm_cap = None
        if obs_max_so_far is not None:
            storm_cap = obs_max_so_far + CONVECTIVE_MAX_CAP_OFFSET

        return ShapedDistParams(
            mu_adj=mu,
            sigma_adj=sigma * 1.0,  # No sigma multiplier for convective
            skew=0.0,
            warm_bias=0.0,
            is_mixture=True,
            p_storm=CONVECTIVE_P_STORM,
            storm_max_cap=storm_cap,
            upper_clamp=False,
        )

    elif regime == "clear":
        # Use deterministic midpoint for warm bias
        warm_bias = (CLEAR_WARM_BIAS_MIN + CLEAR_WARM_BIAS_MAX) / 2.0
        return ShapedDistParams(
            mu_adj=mu + warm_bias,
            sigma_adj=sigma * CLEAR_SIGMA_MULT,
            skew=0.0,
            warm_bias=warm_bias,
            is_mixture=False,
            p_storm=0.0,
            storm_max_cap=None,
            upper_clamp=False,
        )

    else:  # neutral
        return ShapedDistParams(
            mu_adj=mu,
            sigma_adj=sigma * 1.0,
            skew=0.0,
            warm_bias=0.0,
            is_mixture=False,
            p_storm=0.0,
            storm_max_cap=None,
            upper_clamp=False,
        )


def apply_regime_to_samples(
    shaped: ShapedDistParams,
    base_samples: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """
    Apply regime-specific shaping to base temperature samples.

    Applies in order:
    1. Add warm bias
    2. Apply skew via skew-normal approximation
    3. Scale to new sigma
    4. For mixture models, probabilistically cap at storm_max_cap
    5. For upper_clamp, cap at mu + 2*sigma (marine layer effect)

    Args:
        shaped: ShapedDistParams from shape_distribution()
        base_samples: Array of temperature samples (assumed N(mu, sigma))
        rng: numpy random generator for stochastic operations

    Returns:
        Adjusted temperature samples as numpy array.
    """
    samples = base_samples.copy().astype(np.float64)

    # Step 1: Add warm bias
    if shaped.warm_bias != 0.0:
        samples = samples + shaped.warm_bias

    # Step 2: Apply skew via skew-normal approximation
    # skew-normal approx: if X ~ N(0,1), then X + skew * |X| has approximate skew
    if shaped.skew != 0.0:
        z = rng.standard_normal(samples.shape)
        samples = samples + shaped.skew * np.abs(z)

    # Step 3: Scale sigma (rescale around mean)
    # samples = mu + (samples - mu) * sigma_adj / sigma_orig
    sigma_ratio = shaped.sigma_adj / shaped.sigma_adj if shaped.sigma_adj == shaped.sigma_adj else 1.0

    # More carefully: if original samples came from N(mu_orig, sigma_orig),
    # we want to convert to N(mu_adj, sigma_adj).
    # The base_samples input is standardized; we need to know original sigma.
    # Since we operate on base_samples which may already be shifted, we compute ratio carefully.
    if shaped.sigma_adj != 0.0 and len(base_samples) > 0:
        base_mean = np.mean(base_samples)
        if np.std(base_samples) > 0:
            sigma_orig = np.std(base_samples)
        else:
            sigma_orig = 1.0
        samples = shaped.mu_adj + (samples - base_mean) * (shaped.sigma_adj / sigma_orig)

    # Step 4: Mixture model (convective): probabilistically cap at storm_max_cap
    if shaped.is_mixture and shaped.p_storm > 0:
        if shaped.storm_max_cap is not None:
            storm_mask = rng.uniform(0, 1, samples.shape) < shaped.p_storm
            samples[storm_mask] = np.minimum(samples[storm_mask], shaped.storm_max_cap)

    # Step 5: Upper clamp (marine layer)
    if shaped.upper_clamp:
        upper_bound = shaped.mu_adj + 2 * shaped.sigma_adj
        samples = np.minimum(samples, upper_bound)

    return samples
