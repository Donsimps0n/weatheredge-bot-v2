"""
Time-of-day liquidity optimizer for Polymarket.
Adjusts trade aggression based on activity patterns (ET timezone, UTC-4).
"""

import logging
from datetime import datetime
from collections import deque
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Hourly aggression multipliers (ET)
AGGRESSION_MULTIPLIERS = {
    0: 1.15,   # 0-2am: Late night, thinning
    1: 1.15,
    2: 1.35,   # 2-5am: Thinnest books, best fills
    3: 1.35,
    4: 1.35,
    5: 1.20,   # 5-7am: Early morning, still thin
    6: 1.20,
    7: 1.05,   # 7-9am: Waking up
    8: 1.05,
    9: 0.90,   # 9am-12pm: Peak activity
    10: 0.90,
    11: 0.90,
    12: 0.95,  # 12-2pm: Lunch dip
    13: 0.95,
    14: 0.90,  # 2-4pm: Afternoon peak
    15: 0.90,
    16: 1.00,  # 4-6pm: Winding down
    17: 1.00,
    18: 1.05,  # 6-9pm: Evening, moderate
    19: 1.05,
    20: 1.05,
    21: 1.10,  # 9pm-12am: Night, thinning
    22: 1.10,
    23: 1.10,
}

# Estimated bid-ask spread (cents) by hour
SPREAD_ESTIMATES = {
    0: 8.0,    # Dead hours: 6-10 cents
    1: 8.5,
    2: 9.0,    # Thinnest
    3: 9.5,
    4: 9.0,
    5: 7.0,    # Early morning
    6: 6.5,
    7: 4.0,    # Waking up: 2-3 cents
    8: 3.5,
    9: 2.5,    # Peak hours: 2-3 cents
    10: 2.3,
    11: 2.4,
    12: 2.8,   # Lunch
    13: 2.9,
    14: 2.4,   # Afternoon peak
    15: 2.5,
    16: 3.2,   # Winding down
    17: 3.5,
    18: 3.8,   # Evening
    19: 4.2,
    20: 4.5,
    21: 5.0,   # Night
    22: 6.0,
    23: 7.0,
}

THIN_MARKET_THRESHOLD = 0.75  # Aggression < 0.75 = thin


@dataclass
class ExecutionAdvice:
    """Execution guidance for current market conditions."""
    current_multiplier: float
    spread_estimate: float
    recommendation: str
    hour_et: int
    is_thin_market: bool


class LiquidityTimer:
    """Optimizer for trading based on Polymarket hourly activity patterns."""

    def __init__(self, shared_state=None):
        self.shared_state = shared_state
        self.fill_history = deque(maxlen=500)
        self.stats = {
            h: {"count": 0, "avg_slippage": 0.0} for h in range(24)
        }
        logger.info("LiquidityTimer initialized")

    def _get_et_hour(self, hour_et: Optional[int] = None) -> int:
        """Get current ET hour or use provided value."""
        if hour_et is None:
            utc_now = datetime.utcnow()
            hour_et = (utc_now.hour - 4) % 24
        return hour_et

    def get_current_multiplier(self) -> float:
        """Get aggression multiplier for right now."""
        hour = self._get_et_hour()
        mult = AGGRESSION_MULTIPLIERS[hour]
        logger.debug(f"Multiplier for ET hour {hour}: {mult}")
        return mult

    def get_spread_estimate(self, hour_et: Optional[int] = None) -> float:
        """Estimated bid-ask spread in cents."""
        hour = self._get_et_hour(hour_et)
        spread = SPREAD_ESTIMATES[hour]
        logger.debug(f"Spread estimate for ET hour {hour}: {spread} cents")
        return spread

    def should_widen_limit(self, hour_et: Optional[int] = None) -> bool:
        """Place limit orders further from mid in thin markets."""
        mult = self.get_current_multiplier()
        return mult > THIN_MARKET_THRESHOLD

    def get_optimal_size(self, base_size: int, hour_et: Optional[int] = None) -> int:
        """Adjusted size accounting for liquidity."""
        mult = self.get_current_multiplier()
        adjusted = int(base_size * mult)
        logger.info(f"Size adjustment: {base_size} * {mult:.2f} = {adjusted}")
        return adjusted

    def get_execution_advice(self) -> ExecutionAdvice:
        """Complete execution guidance for current conditions."""
        hour = self._get_et_hour()
        mult = self.get_current_multiplier()
        spread = self.get_spread_estimate(hour)
        is_thin = mult > THIN_MARKET_THRESHOLD

        if mult > 1.20:
            rec = "AGGRESSIVE: Thin books, use market orders"
        elif mult > 1.00:
            rec = "NORMAL: Balanced aggression"
        else:
            rec = "DEFENSIVE: Peak hours, use limit orders"

        advice = ExecutionAdvice(
            current_multiplier=mult,
            spread_estimate=spread,
            recommendation=rec,
            hour_et=hour,
            is_thin_market=is_thin,
        )

        if self.shared_state:
            self.shared_state.publish("execution_advice", advice)

        logger.info(f"Advice: {advice.recommendation} (mult={mult:.2f})")
        return advice

    def record_fill(self, hour_et: int, expected_price: float, fill_price: float):
        """Record actual fill to learn real liquidity patterns."""
        slippage = abs(fill_price - expected_price)
        self.fill_history.append({
            "hour": hour_et,
            "expected": expected_price,
            "filled": fill_price,
            "slippage": slippage,
        })

        # Update running stats
        stat = self.stats[hour_et]
        stat["count"] += 1
        stat["avg_slippage"] = (
            (stat["avg_slippage"] * (stat["count"] - 1) + slippage)
            / stat["count"]
        )
        logger.debug(f"Recorded fill: hour {hour_et}, slippage {slippage:.4f}")

    def get_stats(self) -> dict:
        """API stats: trade counts and fill quality by hour."""
        return {
            "total_fills": len(self.fill_history),
            "by_hour": self.stats.copy(),
            "recent_fills": list(self.fill_history)[-10:],
        }
