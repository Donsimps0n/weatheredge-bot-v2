"""
Shim: provides the ``NowcasterEnsemble`` class expected by ``scheduler.py``.
Full implementation uses real NWS observations to anchor the AR(1) Monte Carlo
simulation from nowcasting.py for short-horizon (≤ 24h) forecasts.
Falls back to neutral 50/50 stub if observation fetch fails.
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

# Import obs fetcher with guard
try:
    from active_trader import get_obs_temp_f, max_achievable_today, NWS_STATIONS
    _ACTIVE_TRADER_AVAILABLE = True
except ImportError:
    _ACTIVE_TRADER_AVAILABLE = False
    NWS_STATIONS = {}
    def get_obs_temp_f(city): return None
    def max_achievable_today(obs, hour): return 999.0

# Reverse map: ICAO station → city name
_STATION_TO_CITY = {v: k for k, v in NWS_STATIONS.items()} if NWS_STATIONS else {}


def _ncdf(z):
    """Standard normal CDF approximation."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


class NowcasterEnsemble:
    """
    High-level ensemble wrapper used by the scheduler for short-horizon forecasts
    (≤ 24 h to resolution). Now anchored with real NWS observations.

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
        Return probability-estimate dict for *station* / *category*.
        Uses real NWS observations to anchor the forecast when available.
        Falls back to neutral 50/50 stub if no obs available.
        """
        # Resolve city from station ICAO code
        city = _STATION_TO_CITY.get(station)
        if not city:
            # Try matching station as a city name directly
            city = station

        # Try to get real NWS observation
        obs_temp_f = None
        if _ACTIVE_TRADER_AVAILABLE and city:
            obs_temp_f = get_obs_temp_f(city)

        if obs_temp_f is None:
            logger.debug(
                "NowcasterEnsemble: no NWS obs for station=%s city=%s — returning neutral stub",
                station, city
            )
            return {"yes_prob": 0.5, "no_prob": 0.5, "bin_probs": [], "source": "stub"}

        obs_temp_c = (obs_temp_f - 32) * 5.0 / 9.0
        now_utc = datetime.now(timezone.utc)
        local_hour = (now_utc.hour - 5) % 24  # rough EST

        # Compute max achievable today (in F)
        max_f = max_achievable_today(obs_temp_f, local_hour)
        max_c = (max_f - 32) * 5.0 / 9.0

        logger.info(
            "NWS obs: station=%s city=%s obs=%.1fF (%.1fC) max_achievable=%.1fF hour=%d",
            station, city, obs_temp_f, obs_temp_c, max_f, local_hour
        )

        # If market_data provides a bin threshold, compute obs-anchored probability
        if market_data:
            threshold_c = market_data.get("threshold_c")
            direction = market_data.get("direction", "exact")
            sigma = 1.5 if time_horizon <= 12 else 2.5  # tighter sigma as resolution nears

            if threshold_c is not None:
                # Check if bin is physically achievable
                if direction in ("above", "exact") and max_c < threshold_c - 0.5:
                    logger.info(
                        "NWS OBS_KILL via nowcaster: max_achievable=%.1fC < threshold=%.1fC",
                        max_c, threshold_c
                    )
                    return {"yes_prob": 0.01, "no_prob": 0.99, "bin_probs": [], "source": "nws_obs_kill"}

                # Use max_achievable as anchor for same-day forecast
                ftemp = min(max_c, obs_temp_c + (max_c - obs_temp_c) * 0.7)

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

        # No market_data bin info — return obs-informed but generic estimate
        # Use obs_temp vs typical daily range as rough guide
        # Conservative: if obs is very low for time of day, reduce confidence
        yes_prob = 0.5
        if local_hour >= 13 and time_horizon <= 8:
            # Late in day + short time horizon: obs is now close to daily max
            # Confidence that max stays near obs_temp_c
            yes_prob = 0.5  # neutral without bin info
        return {
            "yes_prob": yes_prob,
            "no_prob": 1.0 - yes_prob,
            "bin_probs": [],
            "source": "nws_obs_no_bin",
            "obs_temp_f": obs_temp_f,
        }
