"""
Probability calculation module for Polymarket temperature trading bot.

Implements:
- Bullet #13: Uncertainty as probability standard deviation
- Bullet #14: Consensus blending with weighted total variance
- Bullet #16: NO handling and YES normalization
- Parts of #19: Non-regression test hooks and fallback handling
"""

import logging
import math
import numpy as np
from dataclasses import dataclass
from typing import Tuple, List

# scipy is optional — use pure-Python fallbacks if not available
try:
    from scipy.stats import gaussian_kde, beta as beta_dist
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
    gaussian_kde = None
    beta_dist = None

# Try to import config; fallback to defaults if not available
try:
    from config import KDE_BOOTSTRAP_RESAMPLES
except ImportError:
    KDE_BOOTSTRAP_RESAMPLES = 200

logger = logging.getLogger(__name__)


@dataclass
class ProbabilityEstimate:
    """Probability estimate with uncertainty."""
    p: float
    u_prob: float

    def __post_init__(self):
        """Validate bounds."""
        if not (0.0 <= self.p <= 1.0):
            raise ValueError(f"p must be in [0, 1], got {self.p}")
        if not (0.0 <= self.u_prob <= 0.5):
            raise ValueError(f"u_prob must be in [0, 0.5], got {self.u_prob}")


def kde_with_uncertainty(
    samples: np.ndarray,
    lo: float,
    hi: float,
    n_resamples: int = KDE_BOOTSTRAP_RESAMPLES,
) -> Tuple[float, float]:
    """
    Estimate probability in [lo, hi] using KDE with bootstrap uncertainty.

    Implements Bullet #13: returns (p, u_prob) where u_prob is std dev of bootstrap probs.

    Args:
        samples: 1D array of temperature/value samples
        lo: Lower bound of region
        hi: Upper bound of region
        n_resamples: Number of bootstrap resamples (default from config)

    Returns:
        (p, u_prob) where p in [0, 1] and u_prob in [0, 0.5]

    Non-regression #19: Must call integrate_box_1d, never crash on edge cases.
    """
    samples = np.asarray(samples).ravel()

    if len(samples) == 0:
        logger.warning("kde_with_uncertainty: empty samples, returning (0.5, 0.5)")
        return (0.5, 0.5)

    if len(samples) < 2:
        logger.warning(f"kde_with_uncertainty: {len(samples)} sample(s), using uniform fallback")
        return (0.5, 0.5)

    if HAS_SCIPY:
        return _kde_with_uncertainty_scipy(samples, lo, hi, n_resamples)
    else:
        logger.info("kde_with_uncertainty: scipy unavailable, using histogram fallback")
        return _kde_with_uncertainty_histogram(samples, lo, hi, n_resamples)


def _kde_with_uncertainty_scipy(
    samples: np.ndarray, lo: float, hi: float, n_resamples: int
) -> Tuple[float, float]:
    """KDE via scipy.stats.gaussian_kde with bootstrap uncertainty."""
    try:
        kde = gaussian_kde(samples)
        p = kde.integrate_box_1d(lo, hi)
        p = float(np.clip(p, 0.0, 1.0))

        bootstrap_probs = []
        for _ in range(n_resamples):
            resampled = np.random.choice(samples, size=len(samples), replace=True)
            try:
                kde_boot = gaussian_kde(resampled)
                p_boot = kde_boot.integrate_box_1d(lo, hi)
                bootstrap_probs.append(float(np.clip(p_boot, 0.0, 1.0)))
            except Exception as e:
                logger.debug(f"kde bootstrap resample failed: {e}")
                continue

        if bootstrap_probs:
            u_prob = float(np.std(bootstrap_probs))
            u_prob = float(np.clip(u_prob, 0.0, 0.5))
        else:
            logger.warning("kde_with_uncertainty: all bootstrap resamples failed")
            u_prob = 0.5

        return (p, u_prob)

    except Exception as e:
        logger.error(f"kde_with_uncertainty_scipy failed: {e}", exc_info=True)
        return (0.5, 0.5)


def _kde_with_uncertainty_histogram(
    samples: np.ndarray, lo: float, hi: float, n_resamples: int
) -> Tuple[float, float]:
    """Pure-Python/numpy histogram fallback when scipy is not available."""
    try:
        n = len(samples)
        p = float(np.sum((samples >= lo) & (samples < hi))) / n
        p = float(np.clip(p, 0.0, 1.0))

        bootstrap_probs = []
        rng = np.random.default_rng()
        for _ in range(n_resamples):
            resampled = rng.choice(samples, size=n, replace=True)
            p_boot = float(np.sum((resampled >= lo) & (resampled < hi))) / n
            bootstrap_probs.append(float(np.clip(p_boot, 0.0, 1.0)))

        u_prob = float(np.std(bootstrap_probs))
        u_prob = float(np.clip(u_prob, 0.0, 0.5))
        return (p, u_prob)

    except Exception as e:
        logger.error(f"kde_with_uncertainty_histogram failed: {e}", exc_info=True)
        return (0.5, 0.5)


def bayesian_smoothing(
    k: int,
    n: int,
    prior_a: float = 1.0,
    prior_b: float = 1.0,
) -> Tuple[float, float]:
    """
    Beta-binomial Bayesian smoothing with probability uncertainty.

    Implements Bullet #13: returns (p, u_prob) where u_prob is std dev of posterior.

    Args:
        k: Number of successes (observations in bin)
        n: Total number of trials
        prior_a: Beta prior alpha (default 1.0 = uniform)
        prior_b: Beta prior beta (default 1.0 = uniform)

    Returns:
        (p, u_prob) where p in [0, 1] and u_prob in [0, 0.5]

    Non-regression #19: Must shrink extremes (k=0 != p=0, k=n != p=1).
    """
    if n < 1:
        logger.warning(f"bayesian_smoothing: invalid n={n}, returning (0.5, 0.5)")
        return (0.5, 0.5)

    k = int(np.clip(k, 0, n))

    try:
        # Posterior parameters
        a = prior_a + k
        b = prior_b + (n - k)

        # Mean
        p = a / (a + b)
        p = float(np.clip(p, 0.0, 1.0))

        # Variance of Beta distribution
        var = (a * b) / ((a + b) ** 2 * (a + b + 1))
        u_prob = float(np.sqrt(var))
        u_prob = float(np.clip(u_prob, 0.0, 0.5))

        return (p, u_prob)

    except Exception as e:
        logger.error(f"bayesian_smoothing failed: k={k}, n={n}: {e}", exc_info=True)
        return (0.5, 0.5)


def consensus_blend(
    sources: List[Tuple[float, float, float]],
) -> Tuple[float, float]:
    """
    Blend multiple probability estimates using weighted total variance.

    Implements Bullet #14: weighted total variance law combining within and between variance.

    Args:
        sources: List of (p_i, u_i, w_i) tuples where:
            - p_i is the probability estimate
            - u_i is the uncertainty (std dev)
            - w_i is the weight

    Returns:
        (p_blend, u_blend) where both are in valid ranges

    Non-regression #19: Must use weighted total variance, NOT raw std across sources.
    """
    if not sources:
        logger.warning("consensus_blend: no sources, returning (0.5, 0.5)")
        return (0.5, 0.5)

    try:
        sources = [
            (float(np.clip(p, 0.0, 1.0)), float(np.clip(u, 0.0, 0.5)), float(w))
            for p, u, w in sources
        ]

        # Normalize weights
        total_weight = sum(w for _, _, w in sources)
        if total_weight <= 0:
            logger.warning("consensus_blend: total weight <= 0, returning (0.5, 0.5)")
            return (0.5, 0.5)

        normalized_sources = [
            (p, u, w / total_weight) for p, u, w in sources
        ]

        # Blended mean
        p_blend = sum(w * p for p, u, w in normalized_sources)
        p_blend = float(np.clip(p_blend, 0.0, 1.0))

        # Weighted total variance: E[Var] + Var[E]
        # = sum(w_i * u_i^2) + sum(w_i * (p_i - p_blend)^2)
        within_var = sum(w * (u ** 2) for p, u, w in normalized_sources)
        between_var = sum(w * ((p - p_blend) ** 2) for p, u, w in normalized_sources)
        total_var = within_var + between_var

        u_blend = float(np.sqrt(total_var))
        u_blend = float(np.clip(u_blend, 0.0, 0.5))

        return (p_blend, u_blend)

    except Exception as e:
        logger.error(f"consensus_blend failed: {e}", exc_info=True)
        return (0.5, 0.5)


def normalize_to_yes(
    side: str,
    price: float,
) -> Tuple[str, float]:
    """
    Normalize any side (YES/NO) to YES representation.

    Implements Bullet #16: NO handling.

    Args:
        side: "YES" or "NO"
        price: Market price in [0, 1]

    Returns:
        ("YES", normalized_price) where normalized_price is the equivalent YES price
    """
    price = float(np.clip(price, 0.0, 1.0))

    if side.upper() == "YES":
        return ("YES", price)
    elif side.upper() == "NO":
        # NO at price P is equivalent to YES at price (1 - P)
        return ("YES", 1.0 - price)
    else:
        logger.warning(f"normalize_to_yes: unknown side '{side}', defaulting to YES")
        return ("YES", price)


def compute_ev_per_dollar_yes(
    true_prob: float,
    entry_price: float,
) -> float:
    """
    Compute EV per dollar staked for YES token.

    Implements Bullet #16: EV calculation for YES tokens.

    Formula: EV = (true_prob - entry_price) / entry_price

    Args:
        true_prob: True probability estimate in [0, 1]
        entry_price: Market price paid in (0, 1)

    Returns:
        EV per dollar staked (can be negative)

    Non-regression #19: Verifies formula (p - P) / P
    """
    true_prob = float(np.clip(true_prob, 0.0, 1.0))
    entry_price = float(np.clip(entry_price, 1e-6, 1.0 - 1e-6))

    try:
        ev = (true_prob - entry_price) / entry_price
        return float(ev)
    except Exception as e:
        logger.error(f"compute_ev_per_dollar_yes failed: {e}", exc_info=True)
        return 0.0


def estimate_bin_probs_ensemble(
    forecast_temps: np.ndarray,
    bin_edges: List[float],
    n_resamples: int = KDE_BOOTSTRAP_RESAMPLES,
) -> List[Tuple[str, float, float]]:
    """
    Estimate probabilities for each temperature bin using ensemble (KDE only).

    Implements part of Bullet #13: returns list of (bin_label, p, u_prob).

    Args:
        forecast_temps: Array of forecasted temperatures
        bin_edges: List of bin boundaries (len N creates N-1 bins)
        n_resamples: Bootstrap resamples for uncertainty

    Returns:
        List of (label, p, u_prob) tuples, one per bin
    """
    forecast_temps = np.asarray(forecast_temps).ravel()
    bin_edges = sorted(bin_edges)

    if len(bin_edges) < 2:
        logger.warning("estimate_bin_probs_ensemble: need at least 2 bin edges")
        return []

    results = []
    for i in range(len(bin_edges) - 1):
        lo = bin_edges[i]
        hi = bin_edges[i + 1]
        label = f"[{lo}, {hi})"

        p, u_prob = kde_with_uncertainty(
            forecast_temps,
            lo,
            hi,
            n_resamples=n_resamples,
        )
        results.append((label, p, u_prob))

    return results


def compute_bin_probabilities(
    nowcast_result,
    bin_edges: List[float],
) -> List[Tuple[str, float, float]]:
    """
    Compute probabilities for each bin from nowcast results.

    Implements Bullet #14: combines KDE ensemble with Bayesian estimates.

    Args:
        nowcast_result: Nowcast result object with 'forecast_temps' and 'observations'
        bin_edges: List of bin boundaries

    Returns:
        List of (bin_label, p_blend, u_blend) tuples
    """
    if not hasattr(nowcast_result, 'forecast_temps'):
        logger.warning("compute_bin_probabilities: nowcast_result missing forecast_temps")
        return []

    forecast_temps = np.asarray(nowcast_result.forecast_temps).ravel()

    # KDE-based estimates
    kde_results = estimate_bin_probs_ensemble(forecast_temps, bin_edges)

    if not kde_results:
        logger.warning("compute_bin_probabilities: KDE estimation failed")
        return []

    results = []
    for label, p_kde, u_kde in kde_results:
        # Parse bin edges from label
        parts = label.strip("[]()").split(",")
        if len(parts) == 2:
            try:
                lo = float(parts[0].strip())
                hi = float(parts[1].strip())

                # Count observations in bin (for Bayesian estimate)
                if hasattr(nowcast_result, 'observations'):
                    obs = np.asarray(nowcast_result.observations).ravel()
                    in_bin = np.sum((obs >= lo) & (obs < hi))
                    total_obs = len(obs)
                else:
                    in_bin = 0
                    total_obs = 0

                # Bayesian estimate if we have observations
                if total_obs > 0:
                    p_bayes, u_bayes = bayesian_smoothing(in_bin, total_obs)
                    # Blend KDE and Bayesian with equal weights
                    sources = [
                        (p_kde, u_kde, 1.0),
                        (p_bayes, u_bayes, 1.0),
                    ]
                    p_blend, u_blend = consensus_blend(sources)
                else:
                    # No observations, use KDE only
                    p_blend, u_blend = p_kde, u_kde

                results.append((label, p_blend, u_blend))
            except Exception as e:
                logger.error(f"compute_bin_probabilities: parse error for '{label}': {e}")
                results.append((label, p_kde, u_kde))
        else:
            results.append((label, p_kde, u_kde))

    return results
