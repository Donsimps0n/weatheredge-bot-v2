"""
Shim: provides the ``EnsembleProbability`` class expected by ``scheduler.py``.

Full implementation delegates to ``probability_calculator.estimate_bin_probs_ensemble``
once ensemble forecast-data ingestion is wired.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from probability_calculator import estimate_bin_probs_ensemble  # noqa: F401
    _PROB_CALC_AVAILABLE = True
except ImportError:
    _PROB_CALC_AVAILABLE = False


class EnsembleProbability:
    """
    High-level wrapper for longer-horizon markets (> 24 h to resolution).

    Scheduler call-site::

        ensemble = EnsembleProbability(config=self.config)
        probs = ensemble.estimate_probability(station=station,
                                              category=category,
                                              forecast_data=market.get("forecast_data"))
    """

    def __init__(self, config=None):
        self.config = config

    def estimate_probability(
        self,
        station: str,
        category: str,
        forecast_data: Optional[dict] = None,
    ) -> dict:
        """
        Return probability-estimate dict.

        TODO: extract forecast_temps + bin_edges from *forecast_data* and call
        ``estimate_bin_probs_ensemble()``.
        """
        if not _PROB_CALC_AVAILABLE:
            logger.debug("probability_calculator unavailable — returning neutral stub")
        elif forecast_data is None:
            logger.debug(
                "EnsembleProbability: no forecast_data for %s — returning neutral stub",
                station,
            )
        else:
            logger.debug(
                "EnsembleProbability: bin_edges mapping not yet wired for %s "
                "— returning neutral stub",
                station,
            )
        return {"yes_prob": 0.5, "no_prob": 0.5, "bin_probs": [], "source": "stub"}
