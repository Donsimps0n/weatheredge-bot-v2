"""
hedge_manager.py — Boundary Position Hedging Agent

When we own YES on a temperature bin and the current observation is near the
bin's boundary (e.g., 79.5°F for a 75-80°F bin), variance is high. This agent:

1. Detects positions near bin boundaries (within 3°F)
2. Finds adjacent bin markets (70-75 or 80-85)
3. Calculates optimal hedge size (15%-50% of primary position)
4. Executes small hedge trades to reduce variance

Communicates via RufloSharedState:
  - Reads: all positions, live observations, signal definitions
  - Publishes: hedge_manager/trades, hedge_manager/boundaries, hedge_manager/stats
  - Emits: 'boundary_detected', 'hedge_placed', 'hedge_skipped'
"""

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


class HedgeManager:
    """Manages boundary hedges to reduce variance on temperature bin positions."""

    def __init__(self, shared_state=None):
        """
        Args:
            shared_state: RufloSharedState instance for inter-agent communication
        """
        self._shared = shared_state
        self._hedged_token_ids: set = set()  # Positions we've already hedged
        self._boundary_cache: dict = {}      # (city, date, token_id) -> boundary info
        self._stats = {
            'hedges_placed': 0,
            'positions_monitored': 0,
            'boundaries_detected': 0,
            'avg_boundary_distance_f': 0.0,
            'last_run_ts': 0,
            'hedge_trades': [],
        }
        log.info("HEDGE_MANAGER: initialized")

    def generate_hedge_trades(
        self,
        positions: list,
        live_obs: dict,
        all_signals: list,
    ) -> List[Dict]:
        """Main entry point. Generates hedge trades for boundary positions.

        Args:
            positions: List of active positions (each with city, date, token_id, etc.)
            live_obs: Live observation dict {city: temp_f, ...}
            all_signals: Full signal definitions (with question text)

        Returns:
            List of hedge trade dicts, each with signal='HEDGE'
        """
        self._stats['last_run_ts'] = datetime.utcnow().timestamp()
        self._stats['positions_monitored'] = len(positions)
        hedge_trades = []

        # Find positions near boundaries
        boundary_positions = self.find_boundary_positions(
            positions, live_obs, all_signals
        )

        if not boundary_positions:
            log.debug("HEDGE_MANAGER: no boundary positions detected")
            return []

        self._stats['boundaries_detected'] = len(boundary_positions)
        log.info("HEDGE_MANAGER: %d boundary positions detected", len(boundary_positions))

        # For each boundary position, find adjacent bin and generate hedge
        for bp in boundary_positions:
            city = bp['city']
            token_id = bp['token_id']
            signal_id = bp['signal_id']
            obs_temp = live_obs.get(city, 999.0)

            # Avoid double-hedging
            if token_id in self._hedged_token_ids:
                log.debug("HEDGE_MANAGER: %s already hedged, skipping", token_id)
                continue

            # Don't hedge near-certain positions (>= 85% probability)
            primary_prob = bp.get('model_probability', 0.5)
            if primary_prob >= 0.85:
                log.debug(
                    "HEDGE_MANAGER: primary position too likely (%.1f%%), skipping hedge for %s",
                    primary_prob * 100, token_id
                )
                continue

            # Find adjacent bin market
            adjacent = self.find_adjacent_bin(
                all_signals,
                city,
                bp['date'],
                bp['boundary_temp_f'],
                bp['direction'],
            )
            if not adjacent:
                log.debug("HEDGE_MANAGER: no adjacent bin found for %s", token_id)
                continue

            # Check if hedge bin is also mispriced (YES < model probability)
            hedge_price = adjacent.get('current_price', 0.5)
            hedge_model_prob = adjacent.get('model_probability', 0.5)
            if hedge_price >= hedge_model_prob:
                log.debug(
                    "HEDGE_MANAGER: hedge bin fairly priced (%.2f >= %.2f), skipping for %s",
                    hedge_price, hedge_model_prob, token_id
                )
                continue

            # Calculate hedge size
            hedge_size = self.calculate_hedge_size(
                primary_position_size=bp['size'],
                boundary_distance_f=bp['boundary_distance_f'],
                primary_price=bp.get('price', 0.5),
                hedge_price=hedge_price,
            )

            if hedge_size <= 0:
                log.debug(
                    "HEDGE_MANAGER: hedge too small or expensive for %s",
                    token_id
                )
                continue

            # Build trade dict
            trade = {
                'signal': 'HEDGE',
                'city': city,
                'token_id': adjacent['token_id'],
                'price': hedge_price,
                'size': hedge_size,
                'source': 'hedge_manager',
                'primary_token_id': token_id,
                'boundary_temp_f': bp['boundary_temp_f'],
                'boundary_distance_f': bp['boundary_distance_f'],
                'hedge_ratio': hedge_size / bp['size'] if bp['size'] > 0 else 0.0,
                'question': adjacent.get('question', ''),
            }

            hedge_trades.append(trade)
            self._hedged_token_ids.add(token_id)
            self._stats['hedges_placed'] += 1

            if self._shared:
                self._shared.emit('hedge_manager', 'hedge_placed', {
                    'city': city,
                    'primary_bin': bp.get('question', '')[:60],
                    'hedge_bin': adjacent.get('question', '')[:60],
                    'ratio': trade['hedge_ratio'],
                    'distance_f': bp['boundary_distance_f'],
                })

            log.info(
                "HEDGE_MANAGER: hedge placed | city=%s primary=%s adjacent=%s ratio=%.1f%%",
                city, token_id[:8], adjacent['token_id'][:8], trade['hedge_ratio'] * 100
            )

        # Update average boundary distance
        if boundary_positions:
            avg_dist = sum(bp['boundary_distance_f'] for bp in boundary_positions) / len(
                boundary_positions
            )
            self._stats['avg_boundary_distance_f'] = avg_dist

        self._publish_stats()
        return hedge_trades

    def find_boundary_positions(
        self,
        positions: list,
        live_obs: dict,
        all_signals: list,
    ) -> List[Dict]:
        """Find positions where current temp is near a bin boundary.

        A position is "near boundary" if |obs_temp - bin_edge| < 3°F.

        Args:
            positions: Active positions
            live_obs: Live temperature observations {city: temp_f}
            all_signals: Signal definitions

        Returns:
            List of boundary position dicts with boundary_temp_f and direction
        """
        boundary_positions = []

        for pos in positions:
            city = pos.get('city', '')
            token_id = pos.get('token_id', '')
            signal_id = pos.get('signal_id', '')

            obs_temp = live_obs.get(city)
            if obs_temp is None:
                continue

            # Find corresponding signal to extract bin range
            signal = self._find_signal(all_signals, signal_id)
            if not signal:
                continue

            bin_range = self._parse_bin_range(signal.get('question', ''))
            if not bin_range:
                continue

            lower_f, upper_f = bin_range

            # Check if near lower boundary
            distance_to_lower = obs_temp - lower_f
            if 0 < distance_to_lower < 3.0:
                boundary_positions.append({
                    'city': city,
                    'token_id': token_id,
                    'signal_id': signal_id,
                    'date': pos.get('date', ''),
                    'size': pos.get('size', 0),
                    'price': pos.get('price', 0.5),
                    'question': signal.get('question', ''),
                    'boundary_temp_f': lower_f,
                    'boundary_distance_f': distance_to_lower,
                    'direction': 'lower',
                })

            # Check if near upper boundary
            distance_to_upper = upper_f - obs_temp
            if 0 < distance_to_upper < 3.0:
                boundary_positions.append({
                    'city': city,
                    'token_id': token_id,
                    'signal_id': signal_id,
                    'date': pos.get('date', ''),
                    'size': pos.get('size', 0),
                    'price': pos.get('price', 0.5),
                    'question': signal.get('question', ''),
                    'boundary_temp_f': upper_f,
                    'boundary_distance_f': distance_to_upper,
                    'direction': 'upper',
                })

        return boundary_positions

    def find_adjacent_bin(
        self,
        signals: list,
        city: str,
        date: str,
        boundary_temp_f: float,
        direction: str,
    ) -> Optional[Dict]:
        """Find the adjacent bin market.

        If direction='upper' and boundary_temp=80, we're near the top of 75-80,
        so find the 80-85 bin. If direction='lower', find the bin below.

        Args:
            signals: All signal definitions
            city: City name
            date: Trade date
            boundary_temp_f: Boundary temperature (80 in above example)
            direction: 'upper' or 'lower'

        Returns:
            Signal dict for adjacent bin, or None
        """
        if direction == 'upper':
            target_lower = boundary_temp_f
            target_upper = boundary_temp_f + 5.0  # Assume 5°F bin width
        else:  # direction == 'lower'
            target_upper = boundary_temp_f
            target_lower = boundary_temp_f - 5.0

        # Search signals for matching city, date, and temperature range
        for sig in signals:
            if sig.get('city') != city or sig.get('date') != date:
                continue

            question = sig.get('question', '')
            sig_range = self._parse_bin_range(question)
            if not sig_range:
                continue

            lower, upper = sig_range
            if abs(lower - target_lower) < 0.5 and abs(upper - target_upper) < 0.5:
                return {
                    'token_id': sig.get('token_id', ''),
                    'signal_id': sig.get('signal_id', ''),
                    'question': question,
                    'current_price': sig.get('current_price', 0.5),
                }

        return None

    def calculate_hedge_size(
        self,
        primary_position_size: int,
        boundary_distance_f: float,
        primary_price: float,
        hedge_price: float,
    ) -> int:
        """Calculate optimal hedge size based on distance from boundary.

        Scale increases as we get closer:
        - >2.5°F away: 10%
        - 1.5-2.5°F: 15%
        - 0.5-1.5°F: 25%
        - <0.5°F: 30%

        Also reject hedges if hedge_price + round-trip fees > 0.25 (too expensive).
        Estimated round-trip fees: 0.04 (4 cents)

        Args:
            primary_position_size: Size of primary YES position
            boundary_distance_f: Distance from boundary in °F
            primary_price: Price paid for primary (for context)
            hedge_price: Current price of hedge

        Returns:
            Hedge size in contracts (rounded down)
        """
        # Don't hedge expensive hedges (price + estimated round-trip fees)
        round_trip_fees = 0.04
        total_cost = hedge_price + round_trip_fees
        max_hedge_cost = 0.25

        if total_cost > max_hedge_cost:
            log.debug(
                "HEDGE_MANAGER: rejecting hedge at %.2f (total cost %.2f > max %.2f)",
                hedge_price, total_cost, max_hedge_cost
            )
            return 0

        # Scale by distance
        if boundary_distance_f > 2.5:
            ratio = 0.10
        elif boundary_distance_f > 1.5:
            ratio = 0.15
        elif boundary_distance_f > 0.5:
            ratio = 0.25
        else:
            ratio = 0.30

        hedge_size = int(primary_position_size * ratio)
        return max(0, hedge_size)

    def get_stats(self) -> Dict:
        """Return performance stats for API."""
        return {
            'hedges_placed': self._stats['hedges_placed'],
            'positions_monitored': self._stats['positions_monitored'],
            'boundaries_detected': self._stats['boundaries_detected'],
            'avg_boundary_distance_f': round(self._stats['avg_boundary_distance_f'], 2),
            'last_run_ts': self._stats['last_run_ts'],
        }

    # ── Private helpers ────────────────────────────────────────────────────

    def _parse_bin_range(self, question: str) -> Optional[Tuple[float, float]]:
        """Extract temperature bin range from question text.

        Matches patterns like:
        - "between 75°F and 80°F"
        - "between 75 and 80°F"
        - "75-80°F"

        Returns:
            (lower_temp, upper_temp) or None
        """
        # Pattern 1: "between X°?F? and Y°?F?"
        match = re.search(
            r'between\s+(\d+)°?F?\s+and\s+(\d+)°?F?',
            question,
            re.IGNORECASE
        )
        if match:
            return (float(match.group(1)), float(match.group(2)))

        # Pattern 2: "X-Y°F"
        match = re.search(r'(\d+)\s*-\s*(\d+)°?F?', question)
        if match:
            return (float(match.group(1)), float(match.group(2)))

        return None

    def _find_signal(self, signals: list, signal_id: str) -> Optional[Dict]:
        """Find signal by ID in the signals list."""
        for sig in signals:
            if sig.get('signal_id') == signal_id or sig.get('token_id') == signal_id:
                return sig
        return None

    def _publish_stats(self):
        """Publish stats to shared state."""
        if not self._shared:
            return
        self._shared.publish('hedge_manager', 'stats', self._stats)

