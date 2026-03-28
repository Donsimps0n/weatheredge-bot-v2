"""
last_mile.py — Resolution Last-Mile Agent

Aggressively sizes up positions when the temperature outcome is nearly locked in.
Detects the "last mile" window when market prices lag the physical reality of an
already-resolved temperature. Uses time-of-day confidence modeling to boost
position multipliers as peak approaches and passes.

Communicates via SharedState:
  Reads: live_signals, live_obs, active_positions
  Publishes: last_mile/adjustments, last_mile/confidence_levels, last_mile/stats
  Emits: 'last_mile_sizing_boost', 'last_mile_exit_recommendation'
"""

import logging
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ============================================================================
# TIMEZONE OFFSET LOOKUP (for local hour calculation without pytz)
# ============================================================================

TZ_OFFSETS = {
    # North America - US (EDT/CDT/MDT/PDT in summer, EST/CST/MST/PST in winter)
    'America/New_York': -4, 'America/Chicago': -5, 'America/Denver': -6,
    'America/Los_Angeles': -7, 'America/Phoenix': -7, 'America/Toronto': -4,
    'America/Vancouver': -7, 'America/Mexico_City': -5,
    # Europe (CEST/CET/EEST/EET)
    'Europe/London': 1, 'Europe/Dublin': 1, 'Europe/Paris': 2,
    'Europe/Amsterdam': 2, 'Europe/Berlin': 2, 'Europe/Madrid': 2,
    'Europe/Rome': 2, 'Europe/Athens': 3, 'Europe/Lisbon': 1,
    'Europe/Stockholm': 2, 'Europe/Copenhagen': 2, 'Europe/Moscow': 3,
    'Europe/Warsaw': 2, 'Europe/Istanbul': 3,
    # Middle East & Africa
    'Asia/Dubai': 4, 'Asia/Jerusalem': 3, 'Africa/Cairo': 3,
    'Africa/Johannesburg': 2, 'Africa/Lagos': 1,
    # Asia
    'Asia/Kolkata': 5.5, 'Asia/Singapore': 8, 'Asia/Bangkok': 7,
    'Asia/Hong_Kong': 8, 'Asia/Tokyo': 9, 'Asia/Seoul': 9, 'Asia/Shanghai': 8,
    # Oceania
    'Australia/Sydney': 10, 'Australia/Melbourne': 10, 'Pacific/Auckland': 12,
    # South America
    'America/Sao_Paulo': -3, 'America/Argentina/Buenos_Aires': -3,
    'America/Santiago': -3,
}


# ============================================================================
# LAST MILE AGENT CLASS
# ============================================================================

class LastMileAgent:
    """
    Resolution last-mile sizing agent.

    Detects when temperature outcomes are nearly locked in and boosts position
    sizes aggressively. Uses time-of-day confidence modeling to determine
    multipliers based on:
    - Distance from market peak (2-4pm local for HIGH markets)
    - Observation confirmation (is current temp in the target bin?)
    - Historical path reachability (can the low actually be reached?)
    """

    def __init__(self, cities_config: List[Dict]):
        """
        Initialize the agent.

        Args:
            cities_config: List of city dicts from config.CITIES,
                          each with 'city', 'timezone' fields.
        """
        self.cities = {c['city']: c for c in cities_config}
        self.stats = {
            'total_adjustments': 0,
            'avg_multiplier': 0.0,
            'exit_recommendations': 0,
        }

    def get_local_hour(self, city_name: str) -> float:
        """
        Get local hour (0-24) for a city, accounting for UTC offset.

        Returns float to allow for fractional hours (e.g., 14.5 = 2:30pm).
        """
        if city_name not in self.cities:
            log.warning(f"City {city_name} not found, using UTC")
            return datetime.now(tz=timezone.utc).hour

        tz_name = self.cities[city_name].get('timezone', 'UTC')
        offset = TZ_OFFSETS.get(tz_name, 0)

        # Current UTC time
        now_utc = datetime.now(tz=timezone.utc)
        utc_hour = now_utc.hour + now_utc.minute / 60.0

        # Local hour = UTC + offset (wrapping around 24)
        local_hour = (utc_hour + offset) % 24
        return local_hour

    def get_confidence_level(
        self,
        local_hour: float,
        is_high_market: bool,
        obs_temp_f: Optional[float],
        bin_lo_f: float,
        bin_hi_f: float,
    ) -> Tuple[str, float, str]:
        """
        Get confidence level and sizing multiplier based on time-of-day and
        observation confirmation.

        Args:
            local_hour: Current local hour (0-24, float).
            is_high_market: True if this is a HIGH temp market, False for LOW.
            obs_temp_f: Current observed temperature (F), or None if not available.
            bin_lo_f: Lower bound of the target bin (F).
            bin_hi_f: Upper bound of the target bin (F).

        Returns:
            (confidence: str, multiplier: float, reason: str)
            confidence: 'HIGH', 'MODERATE', 'LOW', 'VERY_LOW', 'MINIMAL'
            multiplier: Position size multiplier (1.0 = no adjustment)
            reason: Human-readable explanation
        """

        if is_high_market:
            # HIGH market: uncertainty decreases as we approach/pass 2-4pm peak
            if local_hour < 10:
                confidence = 'HIGH'
                multiplier = 1.0
                reason = "Morning: uncertainty still HIGH before peak window"
            elif local_hour < 13:
                confidence = 'MODERATE'
                multiplier = 1.2
                reason = "Late morning: approaching peak, uncertainty MODERATE"
            elif local_hour < 15:
                confidence = 'LOW'
                multiplier = 1.5
                reason = "Early afternoon: near peak window, uncertainty LOW"
            elif local_hour < 17:
                confidence = 'VERY_LOW'
                multiplier = 2.0
                reason = "Late afternoon: peak likely passed, uncertainty VERY_LOW"
            else:
                confidence = 'MINIMAL'
                multiplier = 2.5
                reason = "Evening: high locked in, uncertainty MINIMAL"
        else:
            # LOW market: uncertainty decreases as we approach/pass 5-7am low
            if local_hour < 2:
                confidence = 'HIGH'
                multiplier = 1.0
                reason = "Early night: uncertainty HIGH, low still hours away"
            elif local_hour < 5:
                confidence = 'LOW'
                multiplier = 1.5
                reason = "Late night: low approaching, uncertainty LOW"
            elif local_hour < 8:
                confidence = 'VERY_LOW'
                multiplier = 2.0
                reason = "Early morning: near low window, uncertainty VERY_LOW"
            else:
                confidence = 'MINIMAL'
                multiplier = 2.5
                reason = "Morning+: low locked in, uncertainty MINIMAL"

        # Observation confirmation boost: if obs is inside bin post-peak
        if obs_temp_f is not None and bin_lo_f <= obs_temp_f <= bin_hi_f:
            # We're in the target bin NOW
            if (is_high_market and local_hour >= 15) or (not is_high_market and local_hour >= 5):
                # Post-peak and obs confirms the bin
                multiplier = min(multiplier + 1.0, 4.0)
                reason += " + OBSERVATION IN BIN POST-PEAK (max 4.0x)"
        elif obs_temp_f is not None and (
            (is_high_market and obs_temp_f > bin_hi_f) or
            (not is_high_market and obs_temp_f < bin_lo_f)
        ):
            # Observation already ruled out the bin (post-peak)
            if (is_high_market and local_hour >= 15) or (not is_high_market and local_hour >= 5):
                reason = "EXIT: observation rules out bin post-peak"
                return confidence, 0.0, reason

        return confidence, multiplier, reason

    def check_last_mile(
        self,
        all_signals: List[Dict],
        live_obs: Dict[str, Optional[float]],
        positions: List[Dict],
    ) -> List[Dict]:
        """
        Check all signals for last-mile opportunities and return sizing adjustments.

        Args:
            all_signals: List of market signals, each with:
              {
                'city': str,
                'token_id': str,
                'is_high_market': bool,
                'bin_lo_f': float,
                'bin_hi_f': float,
                ... (other signal fields)
              }
            live_obs: Dict mapping city -> current observed temp (F),
                     e.g. {'New York': 72.5, 'Chicago': 68.2}
            positions: List of active positions, each with:
              {
                'city': str,
                'token_id': str,
                'size': int,
                ... (other position fields)
              }

        Returns:
            List of adjustment dicts:
            [
              {
                'city': str,
                'token_id': str,
                'original_size': int,
                'adjusted_size': int,
                'multiplier': float,
                'confidence': str,
                'reason': str,
              },
              ...
            ]
        """
        adjustments = []
        multipliers = []

        # Map positions by token_id for easy lookup
        pos_by_token = {p.get('token_id'): p for p in positions}

        for signal in all_signals:
            city = signal.get('city')
            token_id = signal.get('token_id')
            is_high = signal.get('is_high_market', True)
            bin_lo = signal.get('bin_lo_f', 0.0)
            bin_hi = signal.get('bin_hi_f', 100.0)

            if not city or not token_id:
                continue

            # Get local hour for this city
            local_hour = self.get_local_hour(city)

            # Get current observation (if available)
            obs_temp = live_obs.get(city)

            # Compute confidence and multiplier
            confidence, multiplier, reason = self.get_confidence_level(
                local_hour, is_high, obs_temp, bin_lo, bin_hi
            )

            # If multiplier is 0.0, it's an exit recommendation (skip sizing boost)
            if multiplier == 0.0:
                log.info(f"Last-Mile EXIT for {city}/{token_id}: {reason}")
                self.stats['exit_recommendations'] += 1
                continue

            # Look up current position
            position = pos_by_token.get(token_id)
            if not position:
                # No active position for this signal, skip
                continue

            original_size = position.get('size', 0)
            if original_size <= 0:
                continue

            # Apply multiplier to original size
            adjusted_size = int(original_size * multiplier)

            adjustment = {
                'city': city,
                'token_id': token_id,
                'original_size': original_size,
                'adjusted_size': adjusted_size,
                'multiplier': multiplier,
                'confidence': confidence,
                'reason': reason,
            }
            adjustments.append(adjustment)
            multipliers.append(multiplier)

            log.info(
                f"Last-Mile Sizing: {city}/{token_id} "
                f"{original_size} → {adjusted_size} "
                f"({multiplier:.2f}x, {confidence}, local_hour={local_hour:.1f})"
            )

        # Update stats
        self.stats['total_adjustments'] += len(adjustments)
        if multipliers:
            self.stats['avg_multiplier'] = sum(multipliers) / len(multipliers)

        return adjustments

    def get_stats(self) -> Dict:
        """
        Return summary statistics for API exposure.

        Returns:
            Dict with keys: total_adjustments, avg_multiplier, exit_recommendations
        """
        return {
            'total_adjustments': self.stats['total_adjustments'],
            'avg_multiplier': round(self.stats['avg_multiplier'], 3),
            'exit_recommendations': self.stats['exit_recommendations'],
        }


# ============================================================================
# MODULE-LEVEL FACTORY (for shared state integration)
# ============================================================================

_agent_instance: Optional[LastMileAgent] = None


def initialize_agent(cities_config: List[Dict]) -> LastMileAgent:
    """Factory to initialize or return the singleton agent."""
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = LastMileAgent(cities_config)
    return _agent_instance


def get_agent() -> LastMileAgent:
    """Get the current agent instance."""
    if _agent_instance is None:
        raise RuntimeError("LastMileAgent not initialized. Call initialize_agent() first.")
    return _agent_instance
