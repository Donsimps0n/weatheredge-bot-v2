"""
Cross-City Geographic Correlation Engine for Weather Trading

When one city's METAR observation surprises (e.g., Atlanta 4°F warmer than forecast),
nearby cities sharing the same air mass are likely also warmer. This module uses
confirmed observations to adjust probabilities for correlated neighbors BEFORE they
report their own observations.

Correlation strength: distance-based + country bonus + coastal match bonus.
Trade generation: If correlation adjustment shifts probability by >5%, generate trade.
"""

import math
import logging
from typing import Dict, List, Optional, Tuple
from config import CITIES

logger = logging.getLogger(__name__)


class CrossCityCorrelationEngine:
    """Geographic correlation engine for propagating temp surprises across cities."""

    def __init__(self, shared_state=None):
        """
        Initialize the correlation engine.

        Args:
            shared_state: Optional shared state object for publishing events.
        """
        self.shared_state = shared_state
        self.city_map: Dict[str, Dict] = {c["city"]: c for c in CITIES}

        # Track accumulated adjustments this cycle: {city: adjustment_f}
        self.accumulated_adjustments: Dict[str, float] = {}

        # Track observations propagated this cycle for stats
        self.propagated_observations: Dict[str, Tuple[float, float]] = {}

        logger.info(
            f"CrossCityCorrelationEngine initialized with {len(CITIES)} cities"
        )

    # =========================================================================
    # HAVERSINE DISTANCE
    # =========================================================================

    @staticmethod
    def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """
        Calculate great-circle distance between two coordinates.

        Args:
            lat1, lon1: First coordinate (degrees).
            lat2, lon2: Second coordinate (degrees).

        Returns:
            Distance in kilometers.
        """
        R_KM = 6371.0
        lat1_rad = math.radians(lat1)
        lon1_rad = math.radians(lon1)
        lat2_rad = math.radians(lat2)
        lon2_rad = math.radians(lon2)

        dlat = lat2_rad - lat1_rad
        dlon = lon2_rad - lon1_rad

        a = math.sin(dlat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
        c = 2 * math.asin(math.sqrt(a))
        return R_KM * c

    # =========================================================================
    # CORRELATION STRENGTH
    # =========================================================================

    def compute_correlation(self, city_a: str, city_b: str) -> float:
        """
        Compute correlation strength between two cities.

        Rules:
        - <200km: 0.7-0.8 (strong)
        - 200-500km: 0.4-0.6 (moderate)
        - 500-1000km: 0.15-0.3 (weak)
        - >1000km: 0 (none)
        - Same country: +0.1
        - Both coastal or both inland: +0.05

        Args:
            city_a, city_b: City names.

        Returns:
            Correlation coefficient [0, 1].
        """
        if city_a not in self.city_map or city_b not in self.city_map:
            return 0.0

        ca = self.city_map[city_a]
        cb = self.city_map[city_b]

        dist_km = self.haversine_km(ca["lat"], ca["lon"], cb["lat"], cb["lon"])

        # Distance-based correlation
        if dist_km < 200:
            corr = 0.75
        elif dist_km < 500:
            corr = 0.50
        elif dist_km < 1000:
            corr = 0.225
        else:
            corr = 0.0

        if corr == 0.0:
            return 0.0

        # Same country bonus
        if ca["country"] == cb["country"]:
            corr += 0.10

        # Coastal/inland match bonus
        if ca["coastal"] == cb["coastal"]:
            corr += 0.05

        return min(corr, 1.0)

    # =========================================================================
    # OBSERVATION PROPAGATION
    # =========================================================================

    def propagate_observation(
        self, city: str, obs_temp_f: float, forecast_f: float
    ) -> None:
        """
        When a city's observation surprises, propagate adjustments to neighbors.

        For each neighbor:
          surprise_f = obs_temp_f - forecast_f
          adjustment_f = surprise_f * correlation * distance_decay

        Args:
            city: City name with new observation.
            obs_temp_f: Observed temperature (°F).
            forecast_f: Forecasted temperature (°F).
        """
        if city not in self.city_map:
            logger.warning(f"City {city} not found in city map")
            return

        surprise_f = obs_temp_f - forecast_f
        if abs(surprise_f) < 0.1:
            return  # Negligible surprise

        self.propagated_observations[city] = (obs_temp_f, forecast_f)
        logger.info(
            f"Propagating observation for {city}: "
            f"obs={obs_temp_f:.1f}°F, fcst={forecast_f:.1f}°F, "
            f"surprise={surprise_f:.1f}°F"
        )

        ca = self.city_map[city]

        # Compute adjustments for all neighbors
        for neighbor in self.city_map:
            if neighbor == city:
                continue

            corr = self.compute_correlation(city, neighbor)
            if corr <= 0:
                continue

            # Simple distance decay: full weight up to 500km, linear decay to 1000km
            cb = self.city_map[neighbor]
            dist_km = self.haversine_km(ca["lat"], ca["lon"], cb["lat"], cb["lon"])

            if dist_km < 500:
                decay = 1.0
            elif dist_km < 1000:
                decay = (1000.0 - dist_km) / 500.0
            else:
                decay = 0.0

            adjustment_f = surprise_f * corr * decay

            if neighbor not in self.accumulated_adjustments:
                self.accumulated_adjustments[neighbor] = 0.0

            self.accumulated_adjustments[neighbor] += adjustment_f

            if adjustment_f != 0:
                logger.debug(
                    f"  → {neighbor}: corr={corr:.2f}, decay={decay:.2f}, "
                    f"adjustment={adjustment_f:+.2f}°F"
                )

        if self.shared_state:
            self.shared_state.publish(
                "cross_city",
                f"propagated_{city}",
                {"surprise_f": surprise_f, "neighbors_affected": len(self.city_map) - 1},
            )

    # =========================================================================
    # GET ADJUSTMENTS FOR TARGET CITY
    # =========================================================================

    def get_correlated_adjustments(self, target_city: str) -> float:
        """
        Return accumulated adjustments from all neighbor observations this cycle.

        Args:
            target_city: City name.

        Returns:
            Accumulated temperature adjustment (°F).
        """
        return self.accumulated_adjustments.get(target_city, 0.0)

    # =========================================================================
    # TRADE GENERATION
    # =========================================================================

    def check_and_trade(
        self, all_signals: List[Dict], live_obs_dict: Dict[str, float]
    ) -> List[Dict]:
        """
        Main entry point. For each signal, check if correlation creates tradeable edge.

        Edge threshold: >5% shift in probability vs market_price.

        Args:
            all_signals: List of signal dicts:
              {city, market_price, our_prob, fcst_temp_f, ...}
            live_obs_dict: {city: temp_f} live observations.

        Returns:
            List of trade dicts with >5% edge, or empty list.
        """
        trades = []

        for signal in all_signals:
            city = signal.get("city")
            if not city or city not in self.city_map:
                continue

            # Skip if we already have live obs for this city
            if city in live_obs_dict:
                continue

            adjustment_f = self.get_correlated_adjustments(city)
            if abs(adjustment_f) < 0.1:
                continue

            # Estimate probability shift from adjustment
            # Simple heuristic: ~0.5°F shifts probability by 1-2% depending on regime
            # Use 2% per °F as baseline
            prob_shift = abs(adjustment_f) * 0.02

            market_price = signal.get("market_price", 0.5)
            our_prob = signal.get("our_prob", 0.5)

            # Check if adjustment creates >5% edge
            if prob_shift > 0.05:
                direction = "BUY" if adjustment_f > 0 else "SELL"
                implied_prob = our_prob + (prob_shift if adjustment_f > 0 else -prob_shift)
                edge_pct = abs(implied_prob - market_price) * 100

                trade = {
                    "city": city,
                    "direction": direction,
                    "reason": f"cross_city_correlation",
                    "correlated_adjustment_f": adjustment_f,
                    "prob_shift_pct": prob_shift * 100,
                    "implied_prob": implied_prob,
                    "market_price": market_price,
                    "edge_pct": edge_pct,
                }

                trades.append(trade)
                logger.info(
                    f"Trade signal for {city} ({direction}): "
                    f"adjustment={adjustment_f:+.2f}°F, "
                    f"edge={edge_pct:.1f}%"
                )

                if self.shared_state:
                    self.shared_state.publish(
                        "cross_city",
                        f"trade_{city}",
                        trade,
                    )
                    self.shared_state.boost_city_priority(
                        "cross_city",
                        city,
                        prob_shift,
                        f"Cross-city correlation from {city} neighbors",
                    )

        return trades

    # =========================================================================
    # CYCLE MANAGEMENT
    # =========================================================================

    def reset_cycle(self) -> None:
        """Clear accumulated adjustments for next cycle."""
        logger.debug(
            f"Resetting cycle: {len(self.accumulated_adjustments)} cities had "
            f"accumulated adjustments"
        )
        self.accumulated_adjustments.clear()
        self.propagated_observations.clear()

    # =========================================================================
    # STATS & API
    # =========================================================================

    def get_stats(self) -> Dict:
        """
        Return engine statistics for API.

        Returns:
            Dict with stats: cities_with_adjustments, avg_adjustment, etc.
        """
        if not self.accumulated_adjustments:
            return {
                "cities_with_adjustments": 0,
                "propagated_observations_count": 0,
                "avg_adjustment_f": 0.0,
                "max_adjustment_f": 0.0,
            }

        adjustments = list(self.accumulated_adjustments.values())
        return {
            "cities_with_adjustments": len(self.accumulated_adjustments),
            "propagated_observations_count": len(self.propagated_observations),
            "avg_adjustment_f": sum(adjustments) / len(adjustments),
            "max_adjustment_f": max(abs(a) for a in adjustments),
            "adjustments_by_city": self.accumulated_adjustments,
        }
