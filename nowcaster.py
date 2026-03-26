"""
Nowcaster: NowcasterEnsemble for scheduler.py.
Anchors short-horizon forecasts to real NWS observations.
Falls back to neutral 50/50 stub if obs fetch fails.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from nowcasting import nowcast_distribution  # noqa: F401
    _NOWCASTING_AVAILABLE = True
except ImportError:
    _NOWCASTING_AVAILABLE = False

try:
    from active_trader import get_obs_temp_f, max_achievable_today, NWS_STATIONS
    _ACTIVE_TRADER_AVAILABLE = True
except ImportError:
    _ACTIVE_TRADER_AVAILABLE = False
    NWS_STATIONS = {}
    def get_obs_temp_f(city): return None
    def max_achievable_today(obs, hour): return 999.0

# Reverse map: ICAO station code -> city name
_STATION_TO_CITY = {v: k for k, v in NWS_STATIONS.items()} if NWS_STATIONS else {}


def _ncdf(z):
    """Standard normal CDF approximation."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


class NowcasterEnsemble:
    """
    Ensemble wrapper for short-horizon forecasts (<=24h to resolution).
    Now anchored with real NWS observations.

    Scheduler call-site::

        nowcaster = NowcasterEnsemble(config=self.config)
        probs = nowcaster.forecast(station=station, time_horizon=hours, category=category)
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
        Return probability-estimate dict for station/category.
        Uses real NWS observations to anchor forecast when available.
        Falls back to neutral 50/50 stub if no obs available.
        """
        # Resolve city from ICAO station code
        city = _STATION_TO_CITY.get(station, station)

        # Fetch real NWS observation
        obs_temp_f = None
        if _ACTIVE_TRADER_AVAILABLE and city:
            obs_temp_f = get_obs_temp_f(city)

        if obs_temp_f is None:
            logger.debug(
                "NowcasterEnsemble: no NWS obs for station=%s city=%s -- returning neutral stub",
                station, city
            )
            return {"yes_prob": 0.5, "no_prob": 0.5, "bin_probs": [], "source": "stub"}

        obs_temp_c = (obs_temp_f - 32) * 5.0 / 9.0
        now_utc = datetime.now(timezone.utc)
        local_hour = (now_utc.hour - 5) % 24  # rough EST

        # Compute max achievable temperature today
        max_f = max_achievable_today(obs_temp_f, local_hour)
        max_c = (max_f - 32) * 5.0 / 9.0

        logger.info(
            "NWS obs: station=%s city=%s obs=%.1fF (%.1fC) max_achievable=%.1fF hour=%d",
            station, city, obs_temp_f, obs_temp_c, max_f, local_hour
        )

        # If market_data provides bin threshold, compute obs-anchored probability
        if market_data:
            threshold_c = market_data.get("threshold_c")
            direction = market_data.get("direction", "exact")
            sigma = 1.5 if time_horizon <= 12 else 2.5

            if threshold_c is not None:
                # Bin physically unreachable -> near-zero probability
                if direction in ("above", "exact") and max_c < threshold_c - 0.5:
                    logger.info(
                        "NWS OBS_KILL via nowcaster: max_achievable=%.1fC < threshold=%.1fC",
                        max_c, threshold_c
                    )
                    return {
                        "yes_prob": 0.01, "no_prob": 0.99, "bin_probs": [],
                        "source": "nws_obs_kill",
                        "obs_temp_f": obs_temp_f, "max_achievable_f": max_f,
                    }

                # Anchor forecast to obs + projected max
                ftemp = obs_temp_c * 0.4 + min(max_c, threshold_c + 1) * 0.6

                if direction == "exact":
                    z_hi = (threshold_c + 0.5 - ftemp) / sigma
                    z_lo = (threshold_c - 0.5 - ftemp) / sigma
                    yes_prob = max(0.01, min(0.99, _ncdf(z_hi) - _ncdf(z_lo)))
                elif direction == "above":
                    z = (ftemp - threshold_c) / sigma
                    yes_prob = max(0.01, min(0.99, _ncdf(z)))
                else:
                    z = (threshold_c - ftemp) / sigma
                    yes_prob = max(0.01, min(0.99, _ncdf(z)))

                return {
                    "yes_prob": round(yes_prob, 4),
                    "no_prob": round(1.0 - yes_prob, 4),
                    "bin_probs": [],
                    "source": "nws_obs_anchored",
                    "obs_temp_f": obs_temp_f,
                    "max_achievable_f": max_f,
                }

        # No bin info - return neutral with obs metadata
        return {
            "yes_prob": 0.5,
            "no_prob": 0.5,
            "bin_probs": [],
            "source": "nws_obs_no_bin",
            "obs_temp_f": obs_temp_f,
        }
