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

    def __init__(self, shared_state=None):
        """
        Initialize the agent.

        Args:
            shared_state: Optional shared state object for integration with api_server.
        """
        self.shared_state = shared_state

        # Import CITIES from config
        try:
            from config import CITIES
            self.cities = {c['city']: c for c in CITIES}
        except ImportError:
            self.cities = {}

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
        obs_age_minutes: Optional[int] = None,
    ) -> Tuple[str, float, str]:
        """
        Get confidence level and sizing multiplier based on time-of-day and
        observation confirmation with staleness and uncertainty gates.

        Args:
            local_hour: Current local hour (0-24, float).
            is_high_market: True if this is a HIGH temp market, False for LOW.
            obs_temp_f: Current observed temperature (F), or None if not available.
            bin_lo_f: Lower bound of the target bin (F).
            bin_hi_f: Upper bound of the target bin (F).
            obs_age_minutes: Age of observation in minutes, or None if unknown.

        Returns:
            (confidence: str, multiplier: float, reason: str)
            confidence: 'HIGH', 'MODERATE', 'LOW', 'VERY_LOW', 'MINIMAL'
            multiplier: Position size multiplier (1.0 = no adjustment)
            reason: Human-readable explanation
        """

        if is_high_market:
            # HIGH market: uncertainty decreases as we approach/pass 2-4pm peak
            # Capped multipliers based on time window
            if local_hour < 10:
                confidence = 'HIGH'
                base_multiplier = 1.0
                reason = "Morning: uncertainty still HIGH before peak window"
            elif local_hour < 13:
                confidence = 'MODERATE'
                base_multiplier = 1.1
                reason = "Late morning: approaching peak, uncertainty MODERATE"
            elif local_hour < 15:
                confidence = 'LOW'
                base_multiplier = 1.2
                reason = "Early afternoon: near peak window, uncertainty LOW"
            elif local_hour < 17:
                confidence = 'VERY_LOW'
                base_multiplier = 1.5
                reason = "Late afternoon: peak likely passed, uncertainty VERY_LOW"
            else:
                confidence = 'MINIMAL'
                base_multiplier = 1.8
                reason = "Evening: high locked in, uncertainty MINIMAL"

            multiplier = base_multiplier
        else:
            # LOW market: uncertainty decreases as we approach/pass 5-7am low
            # Capped multipliers based on time window
            if local_hour < 2:
                confidence = 'HIGH'
                base_multiplier = 1.0
                reason = "Early night: uncertainty HIGH, low still hours away"
            elif local_hour < 5:
                confidence = 'LOW'
                base_multiplier = 1.2
                reason = "Late night: low approaching, uncertainty LOW"
            elif local_hour < 8:
                confidence = 'VERY_LOW'
                base_multiplier = 1.5
                reason = "Early morning: near low window, uncertainty VERY_LOW"
            else:
                confidence = 'MINIMAL'
                base_multiplier = 1.8
                reason = "Morning+: low locked in, uncertainty MINIMAL"

            multiplier = base_multiplier

        # Staleness kill: if observation is stale (>60 min), cap multiplier at 1.0
        if obs_age_minutes is not None and obs_age_minutes > 60:
            multiplier = 1.0
            reason += " [OBS STALE >60min: multiplier capped at 1.0]"
            return confidence, multiplier, reason

        # Observation confirmation boost: if obs is inside bin post-peak
        if obs_temp_f is not None and bin_lo_f <= obs_temp_f <= bin_hi_f:
            # We're in the target bin NOW
            if (is_high_market and local_hour >= 15) or (not is_high_market and local_hour >= 5):
                # Post-peak and obs confirms the bin
                # Only allow boost to 2.5x if BOTH: obs is fresh (<30 min) AND our_prob >= 0.95
                # For now, conservatively apply +0.3 boost, capped at 2.0x
                obs_is_fresh = obs_age_minutes is None or obs_age_minutes < 30
                if obs_is_fresh:
                    multiplier = min(multiplier + 0.3, 2.0)
                    reason += " + OBSERVATION IN BIN POST-PEAK (fresh, +0.3, max 2.0x)"
                else:
                    reason += " + OBSERVATION IN BIN POST-PEAK (stale, no boost)"
        elif obs_temp_f is not None and (
            (is_high_market and obs_temp_f > bin_hi_f) or
            (not is_high_market and obs_temp_f < bin_lo_f)
        ):
            # Observation already ruled out the bin (post-peak)
            if (is_high_market and local_hour >= 15) or (not is_high_market and local_hour >= 5):
                reason = "EXIT: observation rules out bin post-peak"
                return confidence, 0.0, reason

        # Uncertainty gate: multiplier cannot exceed 1.0 + our_prob
        # For now, we estimate our_prob from confidence level (conservative)
        confidence_to_prob = {
            'MINIMAL': 0.95,
            'VERY_LOW': 0.80,
            'LOW': 0.65,
            'MODERATE': 0.50,
            'HIGH': 0.35,
        }
        our_prob = confidence_to_prob.get(confidence, 0.5)
        max_multiplier_by_uncertainty = 1.0 + our_prob
        if multiplier > max_multiplier_by_uncertainty:
            old_mult = multiplier
            multiplier = max_multiplier_by_uncertainty
            reason += f" [UNCERTAINTY GATE: {old_mult:.2f}x → {multiplier:.2f}x (prob={our_prob:.2f})]"

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

            # Compute confidence and multiplier (obs_age_minutes unknown for now, use conservative default)
            confidence, multiplier, reason = self.get_confidence_level(
                local_hour, is_high, obs_temp, bin_lo, bin_hi, obs_age_minutes=None
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
