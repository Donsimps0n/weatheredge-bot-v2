"""
Cross-City Geographic Correlation Engine for Weather Trading

When one city's METAR observation surprises (e.g., Atlanta 4°F warmer than forecast),
nearby cities sharing the same air mass are likely also warmer. This module uses
confirmed observations to compute ranking boosts and gate adjustments for correlated
neighbors BEFORE they report their own observations.

Correlation strength: distance-based + country bonus + coastal match bonus.
Boost generation: Returns ranking boosts + gate tighteners, NOT probability rewrites.
Gate adjustment: When signal is inconsistent with cross-city evidence, raise EV gate to 8%+.
Correlation floor: Only propagate if computed correlation >= 0.40 (was 0.225).
Adjustment cap: Capped to ±2.0°F regardless of neighbor pile-up.
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
        - 500-1000km: 0.25-0.35 (weak, raised from 0.15-0.3)
        - >1000km: 0 (none)
        - Same country: +0.1
        - Both coastal or both inland: +0.05
        - GATE: Only propagate if final corr >= 0.40 (correlation floor)

        Args:
            city_a, city_b: City names.

        Returns:
            Correlation coefficient [0, 1]. Returns 0 if < 0.40 (floor).
        """
        if city_a not in self.city_map or city_b not in self.city_map:
            return 0.0

        ca = self.city_map[city_a]
        cb = self.city_map[city_b]

        dist_km = self.haversine_km(ca["lat"], ca["lon"], cb["lat"], cb["lon"])

        # Distance-based correlation (raised minimum from 0.225 to 0.30)
        if dist_km < 200:
            corr = 0.75
        elif dist_km < 500:
            corr = 0.50
        elif dist_km < 1000:
            corr = 0.30  # Raised from 0.225
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

        corr = min(corr, 1.0)

        # Correlation floor: only propagate if >= 0.40
        if corr < 0.40:
            return 0.0

        return corr

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

            # Cap adjustment to ±2.0°F regardless of neighbor pile-up
            max_adjustment = 2.0
            adjustment_f = max(-max_adjustment, min(max_adjustment, adjustment_f))

            self.accumulated_adjustments[neighbor] += adjustment_f

            if adjustment_f != 0:
                logger.debug(
                    f"  → {neighbor}: corr={corr:.2f}, decay={decay:.2f}, "
                    f"adjustment={adjustment_f:+.2f}°F (capped to ±{max_adjustment}°F)"
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
    # BOOST & GATE GENERATION
    # =========================================================================

    def get_boosts_and_gates(
        self, all_signals: List[Dict], live_obs_dict: Dict[str, float]
    ) -> List[Dict]:
        """
        Main entry point. Compute ranking boosts + gate adjustments from cross-city signals.

        This is a RANKING BOOST + GATE TIGHTENER, NOT a probability rewriter.
        - Boosts rank cities higher in priority for collection
        - Gates tighten EV thresholds when cross-city signal is inconsistent

        Returns:
            List of boost dicts:
            {
              city: str,
              adjustment_f: float (capped ±2.0°F),
              boost_reason: str,
              min_ev_gate: float (8.0 if inconsistent signal, else None)
            }

        Args:
            all_signals: List of signal dicts:
              {city, market_price, our_prob, fcst_temp_f, signal_direction, ...}
            live_obs_dict: {city: temp_f} live observations.
        """
        boosts = []

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

            boost_reason = f"cross_city_correlation: {adjustment_f:+.2f}°F adjustment"

            # Check for signal inconsistency: if cross-city is opposite to signal intent
            # e.g., signal says "cold" (SELL) but neighbors warming (positive adjustment)
            signal_direction = signal.get("signal_direction")  # "WARM" or "COLD"
            min_ev_gate = None

            if signal_direction:
                is_warming = adjustment_f > 0
                is_cold_signal = signal_direction == "COLD"
                is_warm_signal = signal_direction == "WARM"

                # Inconsistency: neighbors warm but we're betting on cold
                if is_warming and is_cold_signal:
                    min_ev_gate = 8.0
                    boost_reason += " [GATE: inconsistent signal, raising EV gate to 8%+]"
                # Inconsistency: neighbors cold but we're betting on warm
                elif not is_warming and is_warm_signal:
                    min_ev_gate = 8.0
                    boost_reason += " [GATE: inconsistent signal, raising EV gate to 8%+]"

            boost = {
                "city": city,
                "adjustment_f": adjustment_f,
                "boost_reason": boost_reason,
                "min_ev_gate": min_ev_gate,
            }

            boosts.append(boost)
            logger.info(f"Boost for {city}: {boost_reason}")

            if self.shared_state:
                self.shared_state.publish(
                    "cross_city",
                    f"boost_{city}",
                    boost,
                )
                self.shared_state.boost_city_priority(
                    "cross_city",
                    city,
                    abs(adjustment_f) / 2.0,  # Use adjustment magnitude for priority
                    f"Cross-city correlation: {adjustment_f:+.2f}°F",
                )

        return boosts

    def check_and_trade(
        self, all_signals: List[Dict], live_obs_dict: Dict[str, float]
    ) -> List[Dict]:
        """
        DEPRECATED: Backward-compatibility wrapper for get_boosts_and_gates.

        Returns empty list. New code should call get_boosts_and_gates directly.

        Args:
            all_signals: Ignored (for API compatibility).
            live_obs_dict: Ignored (for API compatibility).

        Returns:
            Empty list (deprecated).
        """
        logger.warning(
            "check_and_trade() is deprecated. Use get_boosts_and_gates() instead."
        )
        return []

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
        Return engine statistics for API (new format: ranking boosts + gates).

        Returns:
            Dict with stats: cities_with_adjustments, avg_adjustment, max_adjustment,
            correlation_floor_applied, adjustment_cap_applied.
        """
        if not self.accumulated_adjustments:
            return {
                "cities_with_adjustments": 0,
                "propagated_observations_count": 0,
                "avg_adjustment_f": 0.0,
                "max_adjustment_f": 0.0,
                "correlation_floor": 0.40,
                "adjustment_cap_f": 2.0,
            }

        adjustments = list(self.accumulated_adjustments.values())
        return {
            "cities_with_adjustments": len(self.accumulated_adjustments),
            "propagated_observations_count": len(self.propagated_observations),
            "avg_adjustment_f": sum(adjustments) / len(adjustments),
            "max_adjustment_f": max(abs(a) for a in adjustments),
            "adjustments_by_city": self.accumulated_adjustments,
            "correlation_floor": 0.40,
            "adjustment_cap_f": 2.0,
            "output_format": "ranking_boosts_and_gates",
        }
