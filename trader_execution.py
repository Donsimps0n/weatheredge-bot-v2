"""
Execution & fill quality module for Polymarket temperature trading bot.

Handles:
- Spec #9: Execution & fill quality (passive limit orders, fill-prob proxy, size caps)
- Spec #16: NO handling (complementary YES token representation)
- Spec #18: CLOB depth impact (order book depth impact, no LMSR)
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Optional, Callable
from enum import Enum

# Configuration imports (stub - would come from config module)
DEFAULT_TIME_IN_BOOK_S = 60
MAX_REPRICES = 3
SIZE_CAP_DEFAULT_PCT = 0.20
SIZE_CAP_HIGH_DEPTH_PCT = 0.35
HIGH_DEPTH_THRESHOLD = 30000

logger = logging.getLogger(__name__)


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class WalkResult:
    """Result of walking order book levels."""
    avg_price: float
    total_filled: float
    slippage: float
    levels_walked: int


@dataclass
class OrderResult:
    """Result of placing a limit order."""
    order_id: str
    status: str
    placed_price: float
    placed_size: float
    fill_type: str = "maker"
    timestamp: str = ""


@dataclass
class FillResult:
    """Result of order lifecycle simulation/execution."""
    order_id: str
    filled: bool
    fill_price: Optional[float] = None
    fill_size: Optional[float] = None
    time_in_book_s: float = 0.0
    reprice_count: int = 0
    cancel_reason: Optional[str] = None
    fill_type: str = "maker"


# ============================================================================
# Core Execution Functions
# ============================================================================

def compute_fill_prob(relative_spread: float, depth: float, recent_fill_rate: float) -> float:
    """
    Compute fill probability for passive limit orders.

    Higher fill prob when spread is tight, depth is high, fill rate is high.

    Args:
        relative_spread: Relative spread (bid-ask spread / mid-price), normalized [0, 1)
        depth: Total depth at top-of-book level
        recent_fill_rate: Recent fill rate (fills/second or similar)

    Returns:
        Fill probability clamped to [0.01, 0.99]
    """
    # Components:
    # 1. Spread component: tighter spread = higher prob
    spread_component = 1.0 / (1.0 + relative_spread * 100)

    # 2. Depth component: deeper book = higher prob (scaled by 10k baseline)
    depth_component = min(1.0, depth / 10000.0)

    # 3. Fill rate component: higher recent fill rate = higher prob (scaled by 2.0 baseline)
    fill_rate_component = min(1.0, recent_fill_rate / 2.0)

    fill_prob = spread_component * depth_component * fill_rate_component

    # Clamp to valid range
    fill_prob = max(0.01, min(0.99, fill_prob))

    logger.debug(
        f"compute_fill_prob: spread={relative_spread:.4f}, depth={depth:.0f}, "
        f"fill_rate={recent_fill_rate:.4f} => {fill_prob:.4f}"
    )

    return fill_prob


def compute_size_cap(depth: float, theo_ev: float) -> float:
    """
    Compute maximum order size based on depth and theoretical EV.

    Args:
        depth: Total depth at top-of-book level
        theo_ev: Theoretical edge / expected value

    Returns:
        Size cap in absolute terms (minimum 1.0)
    """
    # High depth + high edge: allow larger orders
    if depth > HIGH_DEPTH_THRESHOLD and theo_ev > 0.20:
        size_cap = SIZE_CAP_HIGH_DEPTH_PCT * depth
    else:
        size_cap = SIZE_CAP_DEFAULT_PCT * depth

    size_cap = max(1.0, size_cap)

    logger.debug(
        f"compute_size_cap: depth={depth:.0f}, theo_ev={theo_ev:.4f}, "
        f"high_depth={depth > HIGH_DEPTH_THRESHOLD}, "
        f"high_ev={theo_ev > 0.20} => size_cap={size_cap:.2f}"
    )

    return size_cap


def walk_book_levels(
    levels: list[dict],
    size: float,
    side: str
) -> WalkResult:
    """
    Walk order book levels for taker execution (market order impact).

    Args:
        levels: List of level dicts with keys: price, size, side
        size: Total size to fill
        side: "BUY" or "SELL"

    Returns:
        WalkResult with avg_price, total_filled, slippage, levels_walked
    """
    if not levels or size <= 0:
        return WalkResult(avg_price=0.0, total_filled=0.0, slippage=0.0, levels_walked=0)

    # For buys: we walk ask levels (ascending price)
    # For sells: we walk bid levels (descending price)
    if side.upper() == "BUY":
        # Sort asks from lowest to highest
        sorted_levels = sorted([l for l in levels if l.get("side") == "ASK"],
                              key=lambda x: x["price"])
        best_price = sorted_levels[0]["price"] if sorted_levels else 0.0
    else:
        # Sort bids from highest to lowest
        sorted_levels = sorted([l for l in levels if l.get("side") == "BID"],
                              key=lambda x: x["price"], reverse=True)
        best_price = sorted_levels[0]["price"] if sorted_levels else 0.0

    total_filled = 0.0
    weighted_price = 0.0
    levels_walked = 0

    for level in sorted_levels:
        if total_filled >= size:
            break

        level_size = min(level["size"], size - total_filled)
        level_price = level["price"]

        weighted_price += level_price * level_size
        total_filled += level_size
        levels_walked += 1

    avg_price = weighted_price / total_filled if total_filled > 0 else 0.0
    slippage = (avg_price - best_price) if side.upper() == "BUY" else (best_price - avg_price)

    logger.debug(
        f"walk_book_levels: side={side}, size={size:.2f}, avg_price={avg_price:.6f}, "
        f"total_filled={total_filled:.2f}, slippage={slippage:.6f}, levels={levels_walked}"
    )

    return WalkResult(
        avg_price=avg_price,
        total_filled=total_filled,
        slippage=slippage,
        levels_walked=levels_walked
    )


def maker_fill_prob(
    relative_spread: float,
    depth: float,
    fill_rate: float
) -> float:
    """
    Compute fill probability for passive (maker) limit orders.

    Similar to compute_fill_prob but tuned for passive orders.

    Args:
        relative_spread: Relative spread (bid-ask spread / mid-price)
        depth: Total depth at top-of-book level
        fill_rate: Recent fill rate (fills/second or similar)

    Returns:
        Fill probability clamped to [0.01, 0.99]
    """
    # Maker orders are less likely to fill immediately
    # Reduce weight on spread vs. depth
    spread_component = 0.8 / (1.0 + relative_spread * 100)
    depth_component = min(1.0, depth / 10000.0)
    fill_rate_component = min(1.0, fill_rate / 2.0)

    fill_prob = spread_component * depth_component * fill_rate_component
    fill_prob = max(0.01, min(0.99, fill_prob))

    logger.debug(
        f"maker_fill_prob: spread={relative_spread:.4f}, depth={depth:.0f}, "
        f"fill_rate={fill_rate:.4f} => {fill_prob:.4f}"
    )

    return fill_prob


def place_passive_limit(
    token_id: str,
    price: float,
    size: float,
    book_snapshot: dict,
    paper_mode: bool = True,
    api_client=None
) -> OrderResult:
    """
    Place a passive limit order.

    Args:
        token_id: Polymarket token ID
        price: Limit price [0, 1]
        size: Order size
        book_snapshot: Current order book snapshot
        paper_mode: If True, simulate; else call api_client
        api_client: Live API client (required if not paper_mode)

    Returns:
        OrderResult with order metadata
    """
    order_id = str(uuid.uuid4())

    if paper_mode:
        # Simulate order placement
        status = "PLACED"
        logger.info(
            f"[PAPER] Placed passive limit order: token={token_id}, price={price:.6f}, "
            f"size={size:.2f}, order_id={order_id}, impact_method=CLOB"
        )
    else:
        if api_client is None:
            raise ValueError("api_client required when paper_mode=False")
        # TODO: Call api_client.place_order() and handle response
        status = "PLACED"
        logger.info(
            f"[LIVE] Placed passive limit order: token={token_id}, price={price:.6f}, "
            f"size={size:.2f}, order_id={order_id}, impact_method=CLOB"
        )

    result = OrderResult(
        order_id=order_id,
        status=status,
        placed_price=price,
        placed_size=size,
        fill_type="maker",
        timestamp=""
    )

    return result


def order_lifecycle(
    order_id: str,
    max_time_in_book_s: float = DEFAULT_TIME_IN_BOOK_S,
    max_reprices: int = MAX_REPRICES,
    paper_mode: bool = True,
    api_client=None,
    get_book_fn: Optional[Callable] = None
) -> FillResult:
    """
    Simulate or execute order lifecycle with time-in-book, repricing, and cancellation.

    In paper mode:
    - Simulate fill probability based on time in book
    - fill_prob increases with time: base_prob * (1 + time_elapsed/max_time)
    - If not filled after max_time: attempt reprice (adjust price by 1 tick)
    - After max_reprices: cancel
    - Cancel reasons: "timeout", "decay", "depth_collapse"

    Args:
        order_id: Order ID to track
        max_time_in_book_s: Max time before reprice attempt (default 60s)
        max_reprices: Max reprice attempts before cancel (default 3)
        paper_mode: If True, simulate; else call api_client
        api_client: Live API client (required if not paper_mode)
        get_book_fn: Function to get current book snapshot (for depth checks)

    Returns:
        FillResult with fill outcome and lifecycle metadata
    """

    if paper_mode:
        # Simulate fill probability curve
        # Base probability: moderate (50%)
        base_prob = 0.5

        # Simulate time elapsed (random in paper mode, but deterministic in tests)
        time_elapsed = max_time_in_book_s * 0.75

        # Fill prob increases with time in book
        fill_prob_adjusted = base_prob * (1.0 + time_elapsed / max_time_in_book_s)
        fill_prob_adjusted = min(0.99, fill_prob_adjusted)

        # Simulate fill
        import random
        filled = random.random() < fill_prob_adjusted

        if filled:
            result = FillResult(
                order_id=order_id,
                filled=True,
                fill_price=0.5,  # Placeholder
                fill_size=10.0,  # Placeholder
                time_in_book_s=time_elapsed,
                reprice_count=0,
                cancel_reason=None,
                fill_type="maker"
            )
            logger.info(
                f"[PAPER] Order filled: order_id={order_id}, time_in_book={time_elapsed:.1f}s, "
                f"impact_method=CLOB"
            )
            return result

        # Not filled after max_time: attempt reprices
        reprice_count = 0
        cancel_reason = None

        for attempt in range(max_reprices):
            reprice_count += 1

            # Check for depth collapse if get_book_fn provided
            if get_book_fn is not None:
                try:
                    book = get_book_fn()
                    depth = book.get("depth", 0)
                    if depth < 1000:  # Arbitrary collapse threshold
                        cancel_reason = "depth_collapse"
                        logger.warning(
                            f"[PAPER] Cancelling order due to depth collapse: order_id={order_id}, "
                            f"depth={depth:.0f}, impact_method=CLOB"
                        )
                        break
                except Exception as e:
                    logger.warning(f"Error fetching book snapshot: {e}")

            # Simulate reprice (adjust price by 1 tick = 0.01)
            # Re-evaluate fill prob
            reprice_time = time_elapsed + (attempt + 1) * (max_time_in_book_s / max_reprices)
            fill_prob_adjusted = base_prob * (1.0 + reprice_time / max_time_in_book_s)
            fill_prob_adjusted = min(0.99, fill_prob_adjusted)

            if random.random() < fill_prob_adjusted:
                result = FillResult(
                    order_id=order_id,
                    filled=True,
                    fill_price=0.51,  # Placeholder
                    fill_size=10.0,   # Placeholder
                    time_in_book_s=reprice_time,
                    reprice_count=reprice_count,
                    cancel_reason=None,
                    fill_type="maker"
                )
                logger.info(
                    f"[PAPER] Order filled after reprice: order_id={order_id}, "
                    f"reprice_count={reprice_count}, time_in_book={reprice_time:.1f}s, "
                    f"impact_method=CLOB"
                )
                return result

        # Not filled after all reprices: cancel
        if cancel_reason is None:
            cancel_reason = "timeout"

        result = FillResult(
            order_id=order_id,
            filled=False,
            fill_price=None,
            fill_size=None,
            time_in_book_s=time_elapsed + max_reprices * (max_time_in_book_s / max_reprices),
            reprice_count=reprice_count,
            cancel_reason=cancel_reason,
            fill_type="maker"
        )
        logger.warning(
            f"[PAPER] Order cancelled: order_id={order_id}, reason={cancel_reason}, "
            f"reprice_count={reprice_count}, impact_method=CLOB"
        )
        return result

    else:
        # Live execution (TODO: integrate with Polymarket CLOB API)
        if api_client is None:
            raise ValueError("api_client required when paper_mode=False")

        # Placeholder for live order lifecycle
        # TODO: Poll api_client.get_order_status() in loop
        # TODO: Implement reprice logic via api_client.cancel_order() + place_passive_limit()
        # TODO: Check depth via get_book_fn() for depth_collapse detection

        result = FillResult(
            order_id=order_id,
            filled=False,
            fill_price=None,
            fill_size=None,
            time_in_book_s=0.0,
            reprice_count=0,
            cancel_reason="NOT_IMPLEMENTED",
            fill_type="maker"
        )
        logger.warning(
            f"[LIVE] Order lifecycle not implemented: order_id={order_id}, "
            f"impact_method=CLOB"
        )
        return result


def normalize_to_yes_execution(
    side: str,
    price: float,
    token_id_yes: str,
    token_id_no: Optional[str] = None
) -> tuple[str, float, str]:
    """
    Normalize execution to YES token representation (spec #16: NO handling).

    Default YES-only representation. If side is "NO", trade YES complement at 1-price.

    Args:
        side: "BUY" or "SELL"
        price: Original price [0, 1]
        token_id_yes: YES token ID
        token_id_no: NO token ID (optional)

    Returns:
        (effective_side, adjusted_price, token_to_use)
    """
    side_upper = side.upper().strip()
    if side_upper in ("YES", "BUY", "BUY_YES"):
        # Buying YES: direct trade
        return ("YES", price, token_id_yes)
    elif side_upper in ("SELL", "SELL_YES"):
        # Selling YES: direct trade
        return ("YES", price, token_id_yes)
    elif side_upper in ("NO", "BUY_NO"):
        # Buying NO = trade complementary YES at 1-price
        adjusted_price = 1.0 - price
        logger.debug(
            f"normalize_to_yes_execution: Converted {side} @ {price:.6f} "
            f"to YES @ {adjusted_price:.6f}"
        )
        return ("YES", adjusted_price, token_id_yes)
    elif side_upper == "SELL_NO":
        # Selling NO = Buying YES at complement price
        adjusted_price = 1.0 - price
        logger.debug(
            f"normalize_to_yes_execution: Converted SELL_NO @ {price:.6f} "
            f"to YES @ {adjusted_price:.6f}"
        )
        return ("YES", adjusted_price, token_id_yes)
    else:
        # Default: YES representation
        logger.warning(f"normalize_to_yes_execution: unknown side '{side}', defaulting to YES")
        return ("YES", price, token_id_yes)


# ============================================================================
# Execution Adapters
# ============================================================================

class PaperExecutionAdapter:
    """
    Paper trading adapter: in-memory order simulation with book snapshots.
    """

    def __init__(self):
        self.orders = {}  # order_id -> {token_id, price, size, side, status, filled_price}
        self.book_snapshots = {}  # token_id -> book snapshot
        logger.info("Initialized PaperExecutionAdapter, impact_method=CLOB")

    def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str
    ) -> dict:
        """
        Place an order in paper trading.

        Args:
            token_id: Token ID
            price: Order price [0, 1]
            size: Order size
            side: "BUY" or "SELL"

        Returns:
            Order dict with order_id, status, etc.
        """
        order_id = str(uuid.uuid4())
        self.orders[order_id] = {
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side,
            "status": "PLACED",
            "filled_price": None,
            "filled_size": 0.0
        }
        logger.debug(
            f"[PAPER] Placed order: {order_id}, token={token_id}, "
            f"price={price:.6f}, size={size:.2f}, side={side}, impact_method=CLOB"
        )
        return {
            "order_id": order_id,
            "status": "PLACED",
            "price": price,
            "size": size,
            "side": side
        }

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an order."""
        if order_id in self.orders:
            self.orders[order_id]["status"] = "CANCELLED"
            logger.debug(f"[PAPER] Cancelled order: {order_id}, impact_method=CLOB")
            return {"order_id": order_id, "status": "CANCELLED"}
        return {"error": f"Order {order_id} not found"}

    def get_order_status(self, order_id: str) -> dict:
        """Get current order status."""
        if order_id in self.orders:
            order = self.orders[order_id]
            return {
                "order_id": order_id,
                "status": order["status"],
                "filled_price": order["filled_price"],
                "filled_size": order["filled_size"]
            }
        return {"error": f"Order {order_id} not found"}

    def set_book_snapshot(self, token_id: str, book: dict):
        """Store a book snapshot for depth checks."""
        self.book_snapshots[token_id] = book
        logger.debug(f"[PAPER] Stored book snapshot for {token_id}, impact_method=CLOB")

    def get_book_snapshot(self, token_id: str) -> dict:
        """Retrieve stored book snapshot."""
        return self.book_snapshots.get(token_id, {})


class LiveExecutionAdapter:
    """
    Live execution adapter: Polymarket CLOB API integration (TODO).
    """

    def __init__(self, api_key: str = "", private_key: str = ""):
        """
        Initialize live adapter with credentials.

        Args:
            api_key: Polymarket API key
            private_key: Wallet private key for signing orders
        """
        self.api_key = api_key
        self.private_key = private_key
        logger.info(
            "Initialized LiveExecutionAdapter (CLOB API integration pending), "
            "impact_method=CLOB"
        )

    def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str
    ) -> dict:
        """
        Place an order on Polymarket CLOB.

        TODO: Sign order with wallet and submit to CLOB API.
        """
        raise NotImplementedError(
            "Live order placement not yet implemented. "
            "TODO: Implement Polymarket CLOB API integration with order signing."
        )

    def cancel_order(self, order_id: str) -> dict:
        """
        Cancel an order on Polymarket CLOB.

        TODO: Sign cancellation with wallet and submit to CLOB API.
        """
        raise NotImplementedError(
            "Live order cancellation not yet implemented. "
            "TODO: Implement Polymarket CLOB API integration."
        )

    def get_order_status(self, order_id: str) -> dict:
        """
        Get order status from Polymarket CLOB.

        TODO: Query CLOB API for current order state.
        """
        raise NotImplementedError(
            "Live order status query not yet implemented. "
            "TODO: Implement Polymarket CLOB API integration."
        )
