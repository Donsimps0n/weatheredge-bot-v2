"""
Shim: provides the ``NowcasterEnsemble`` class expected by ``scheduler.py``.

Full implementation delegates to ``nowcasting.nowcast_distribution()`` once
live weather-observation fetching is wired.  Until then the stub returns a
neutral 50/50 dict so the pipeline runs end-to-end in paper mode.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from nowcasting import nowcast_distribution  # noqa: F401
    _NOWCASTING_AVAILABLE = True
except ImportError:
    _NOWCASTING_AVAILABLE = False


class NowcasterEnsemble:
    """
    High-level ensemble wrapper used by the scheduler for short-horizon
    forecasts (≤ 24 h to resolution).

    Scheduler call-site::

        nowcaster = NowcasterEnsemble(config=self.config)
        probs = nowcaster.forecast(station=station,
                                   time_horizon=hours,
                                   category=category)
        # returns {"yes_prob": float, "no_prob": float, "bin_probs": list}
    """

    def __init__(self, config=None):
        self.config = config

    def forecast(
        self,
        station: str,
        time_horizon: float,
        category: str,
        market_data: Optional[dict] = None,
    ) -> dict:
        """
        Return probability-estimate dict for *station* / *category*.

        TODO: fetch obs_temp_now, mu_now, sigma from weather sources and call
        ``nowcast_distribution()`` with real parameters.
        """
        if not _NOWCASTING_AVAILABLE:
            logger.debug("nowcasting unavailable — returning neutral stub for %s", station)
        else:
            logger.debug(
                "NowcasterEnsemble.forecast: weather-obs fetch not yet wired "
                "for %s — returning neutral stub",
                station,
            )
        return {"yes_prob": 0.5, "no_prob": 0.5, "bin_probs": [], "source": "stub"}
