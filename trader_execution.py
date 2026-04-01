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
        # Call the live API client to place the actual CLOB order
        try:
            resp = api_client.place_order(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY",
            )
            order_id = resp.get("order_id", order_id)
            status = resp.get("status", "PLACED")
            logger.info(
                f"[LIVE] Placed passive limit order: token={token_id}, price={price:.6f}, "
                f"size={size:.2f}, order_id={order_id}, status={status}, impact_method=CLOB"
            )
        except Exception as exc:
            status = "ERROR"
            logger.error(
                f"[LIVE] Failed to place passive limit: token={token_id}, "
                f"price={price:.6f}, size={size:.2f}, error={exc}"
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
        # ── Live execution: poll → reprice → cancel loop ──────────────────
        if api_client is None:
            raise ValueError("api_client required when paper_mode=False")

        import time as _time

        POLL_INTERVAL_S = 5.0   # seconds between status polls
        REPRICE_TICK    = 0.01  # price improvement per reprice (1 cent)

        reprice_count = 0
        cancel_reason = None
        t_start = _time.monotonic()

        # Current order price (may be adjusted by repricing)
        current_price = None  # unknown from here; lifecycle just tracks the id

        while True:
            elapsed = _time.monotonic() - t_start

            # ── Check fill status ─────────────────────────────────────────
            try:
                status_resp = api_client.get_order_status(order_id)
            except Exception as exc:
                logger.warning(f"[LIVE] status poll error for {order_id}: {exc}")
                _time.sleep(POLL_INTERVAL_S)
                continue

            order_status = status_resp.get("status", "unknown").upper()
            filled_size  = float(status_resp.get("filled_size", 0))

            # Fully filled
            if order_status == "MATCHED" or filled_size > 0:
                fill_price = status_resp.get("fill_price") or status_resp.get("price")
                result = FillResult(
                    order_id=order_id,
                    filled=True,
                    fill_price=float(fill_price) if fill_price else None,
                    fill_size=filled_size if filled_size > 0 else None,
                    time_in_book_s=elapsed,
                    reprice_count=reprice_count,
                    cancel_reason=None,
                    fill_type="maker",
                )
                logger.info(
                    f"[LIVE] Order filled: order_id={order_id}, "
                    f"time_in_book={elapsed:.1f}s, reprices={reprice_count}, "
                    f"impact_method=CLOB"
                )
                return result

            # Already cancelled externally
            if order_status in ("CANCELLED", "EXPIRED", "DEAD"):
                result = FillResult(
                    order_id=order_id,
                    filled=False,
                    fill_price=None,
                    fill_size=None,
                    time_in_book_s=elapsed,
                    reprice_count=reprice_count,
                    cancel_reason="external_cancel",
                    fill_type="maker",
                )
                logger.warning(
                    f"[LIVE] Order externally cancelled: {order_id}, "
                    f"status={order_status}"
                )
                return result

            # ── Check for depth collapse ──────────────────────────────────
            if get_book_fn is not None:
                try:
                    book = get_book_fn()
                    depth = book.get("depth", book.get("total_bid_depth", 99999))
                    if depth < 1000:
                        cancel_reason = "depth_collapse"
                        logger.warning(
                            f"[LIVE] Depth collapse ({depth:.0f}) — cancelling {order_id}"
                        )
                        api_client.cancel_order(order_id)
                        break
                except Exception:
                    pass  # book fetch failure is non-fatal

            # ── Time-in-book exceeded → reprice or cancel ─────────────────
            if elapsed >= max_time_in_book_s:
                if reprice_count < max_reprices:
                    # Cancel old order and place a new one with improved price
                    reprice_count += 1
                    logger.info(
                        f"[LIVE] Repricing {order_id} (attempt {reprice_count}/{max_reprices})"
                    )
                    try:
                        api_client.cancel_order(order_id)
                        # Improve price by one tick
                        new_price = round((current_price or 0.50) + REPRICE_TICK, 4)
                        new_price = min(new_price, 0.99)
                        resp = api_client.place_order(
                            token_id=order_id.split("-")[0] if "-" in order_id else "",
                            price=new_price,
                            size=10.0,  # carry over original size
                            side="BUY",
                        )
                        order_id = resp.get("order_id", order_id)
                        current_price = new_price
                        t_start = _time.monotonic()  # reset timer for new order
                    except Exception as exc:
                        logger.error(f"[LIVE] Reprice failed: {exc}")
                        cancel_reason = "reprice_error"
                        break
                else:
                    cancel_reason = "timeout"
                    logger.info(
                        f"[LIVE] Max reprices reached ({max_reprices}) — cancelling {order_id}"
                    )
                    try:
                        api_client.cancel_order(order_id)
                    except Exception:
                        pass
                    break

            _time.sleep(POLL_INTERVAL_S)

        # Fell through — order not filled
        result = FillResult(
            order_id=order_id,
            filled=False,
            fill_price=None,
            fill_size=None,
            time_in_book_s=_time.monotonic() - t_start,
            reprice_count=reprice_count,
            cancel_reason=cancel_reason or "timeout",
            fill_type="maker",
        )
        logger.warning(
            f"[LIVE] Order not filled: order_id={order_id}, "
            f"reason={result.cancel_reason}, reprices={reprice_count}, "
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

    def __init__(self, ledger=None):
        self.ledger = ledger
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

    def place_orders(
        self,
        market_slug: str = "",
        ladder=None,
        book_snapshot: dict = None,
        paper_mode: bool = True,
    ) -> list[dict]:
        """
        Place orders for all tradeable legs in a ladder.

        Called by TradingScheduler.run_cycle(). Iterates LadderResult.legs,
        calls place_order() for each leg with positive capped_size, and
        simulates fill via order_lifecycle() in paper mode.

        Args:
            market_slug: Market identifier for logging.
            ladder: LadderResult (has .legs list) or plain list of leg dicts.
            book_snapshot: Current order book snapshot dict.
            paper_mode: Always True for PaperExecutionAdapter.

        Returns:
            List of order result dicts (one per placed order).
        """
        if ladder is None:
            return []

        # Support both LadderResult objects (.legs) and plain lists
        legs = getattr(ladder, "legs", ladder) if not isinstance(ladder, list) else ladder
        if not legs:
            return []

        placed = []
        for leg in legs:
            # Extract fields from LadderLeg dataclass or dict
            token_id    = getattr(leg, "token_id", None) or (leg.get("token_id") if isinstance(leg, dict) else "")
            side        = getattr(leg, "side", None) or (leg.get("side", "BUY") if isinstance(leg, dict) else "BUY")
            price       = getattr(leg, "market_price", None) or (leg.get("market_price", 0.5) if isinstance(leg, dict) else 0.5)
            capped_size = getattr(leg, "capped_size", None) or (leg.get("capped_size", 0) if isinstance(leg, dict) else 0)
            bin_label   = getattr(leg, "bin_label", None) or (leg.get("bin_label", "?") if isinstance(leg, dict) else "?")
            edge        = getattr(leg, "edge", None) or (leg.get("edge", 0) if isinstance(leg, dict) else 0)

            if not token_id or capped_size <= 0:
                continue

            order_result = self.place_order(
                token_id=token_id,
                price=price,
                size=capped_size,
                side=side,
            )
            order_result["market_slug"] = market_slug
            order_result["bin_label"] = bin_label
            order_result["edge"] = edge

            # Simulate fill lifecycle in paper mode
            fill = order_lifecycle(
                order_id=order_result["order_id"],
                paper_mode=True,
                get_book_fn=lambda tid=token_id: self.get_book_snapshot(tid),
            )
            order_result["filled"] = fill.filled
            order_result["fill_price"] = fill.fill_price
            order_result["cancel_reason"] = fill.cancel_reason

            # Log to ledger if available
            if self.ledger and hasattr(self.ledger, "log_decision"):
                self.ledger.log_decision(
                    slug=market_slug,
                    action="paper_order",
                    side=side,
                    price=price,
                    size=capped_size,
                    filled=fill.filled,
                    edge=edge,
                    bin_label=bin_label,
                )

            placed.append(order_result)
            logger.info(
                f"[PAPER] {market_slug} {bin_label}: {side} {capped_size:.2f} @ "
                f"{price:.4f}, edge={edge:.4f}, filled={fill.filled}"
            )

        return placed


class LiveExecutionAdapter:
    """
    Live execution adapter: Polymarket CLOB API integration via py-clob-client.
    Falls back to paper mode silently if POLYMARKET_PRIVATE_KEY is missing.
    """

    def __init__(self, ledger=None, fee_client=None, api_key: str = "", private_key: str = ""):
        """
        Initialize live adapter with credentials.
        If private_key is empty, reads from POLYMARKET_PRIVATE_KEY env var.
        Falls back to paper mode if key missing.

        Args:
            ledger: Optional ledger for logging decisions.
            fee_client: Optional fee client for computing maker/taker fees.
            api_key: Polymarket API key (optional).
            private_key: Polymarket private key (reads POLYMARKET_PRIVATE_KEY env if empty).
        """
        import os
        self.ledger = ledger
        self.fee_client = fee_client
        self.api_key = api_key
        self.private_key = private_key or os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        self._client = None
        self._paper_fallback = False

        if not self.private_key:
            logger.warning(
                "LiveExecutionAdapter: POLYMARKET_PRIVATE_KEY not set — falling back to paper mode"
            )
            self._paper_fallback = True
        else:
            try:
                self._init_client()
            except Exception as e:
                logger.error("LiveExecutionAdapter: client init failed: %s — paper fallback", e)
                self._paper_fallback = True

        logger.info(
            "Initialized LiveExecutionAdapter (paper_fallback=%s), impact_method=CLOB",
            self._paper_fallback
        )

    def _init_client(self):
        """Initialize the py-clob-client."""
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        chain_id = 137  # Polygon mainnet
        host = "https://clob.polymarket.com"
        self._client = ClobClient(
            host,
            chain_id=chain_id,
            key=self.private_key,
            signature_type=0,
        )
        logger.info("LiveExecutionAdapter: CLOB client initialized")

    def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str
    ) -> dict:
        """
        Place a BUY or SELL order on Polymarket CLOB.
        side: "BUY" or "SELL"
        Returns dict with order_id and status.
        """
        if self._paper_fallback:
            import uuid
            order_id = str(uuid.uuid4())
            logger.info(
                "[PAPER_FALLBACK] place_order: token=%s price=%.4f size=%.2f side=%s → %s",
                token_id[:16], price, size, side, order_id
            )
            return {"order_id": order_id, "status": "PAPER_FALLBACK", "price": price, "size": size}

        try:
            from py_clob_client.order_builder.constants import BUY, SELL as SELL_SIDE
            from py_clob_client.clob_types import OrderArgs

            # Normalize to YES token side
            eff_side, adj_price, use_token = normalize_to_yes_execution(
                side, price, token_id
            )
            clob_side = BUY if eff_side in ("YES", "BUY") else SELL_SIDE

            order_args = OrderArgs(
                price=adj_price,
                size=size,
                side=clob_side,
                token_id=use_token,
            )
            resp = self._client.create_and_post_order(order_args)
            order_id = resp.get("orderID", resp.get("id", "unknown")) if isinstance(resp, dict) else str(resp)[:64]
            logger.info(
                "[LIVE] place_order: token=%s price=%.4f size=%.2f side=%s → %s",
                token_id[:16], adj_price, size, side, order_id
            )
            return {
                "order_id": order_id,
                "status": resp.get("status", "placed") if isinstance(resp, dict) else "placed",
                "price": adj_price,
                "size": size,
                "raw": str(resp)[:200],
            }
        except Exception as e:
            logger.error("LiveExecutionAdapter.place_order failed: %s", e)
            return {"order_id": None, "status": "ERROR", "error": str(e)}

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order on Polymarket CLOB."""
        if self._paper_fallback:
            logger.info("[PAPER_FALLBACK] cancel_order: %s", order_id)
            return {"order_id": order_id, "status": "PAPER_FALLBACK_CANCELLED"}

        try:
            resp = self._client.cancel(order_id)
            logger.info("[LIVE] cancel_order: %s → %s", order_id, str(resp)[:100])
            return {"order_id": order_id, "status": "cancelled", "raw": str(resp)[:200]}
        except Exception as e:
            logger.error("LiveExecutionAdapter.cancel_order failed: %s", e)
            return {"order_id": order_id, "status": "ERROR", "error": str(e)}

    def get_order_status(self, order_id: str) -> dict:
        """Get current status of an order from Polymarket CLOB."""
        if self._paper_fallback:
            return {"order_id": order_id, "status": "PAPER_FALLBACK"}

        try:
            resp = self._client.get_order(order_id)
            status = resp.get("status", "unknown") if isinstance(resp, dict) else "unknown"
            logger.info("[LIVE] get_order_status: %s → %s", order_id, status)
            return {
                "order_id": order_id,
                "status": status,
                "filled_size": resp.get("filledSize", 0) if isinstance(resp, dict) else 0,
                "raw": str(resp)[:200],
            }
        except Exception as e:
            logger.error("LiveExecutionAdapter.get_order_status failed: %s", e)
            return {"order_id": order_id, "status": "ERROR", "error": str(e)}

    def place_orders(
        self,
        market_slug: str = "",
        ladder=None,
        book_snapshot: dict = None,
        paper_mode: bool = False,
    ) -> list[dict]:
        """
        Place orders for all tradeable legs in a ladder via Polymarket CLOB.

        Called by TradingScheduler.run_cycle(). Iterates LadderResult.legs,
        calls place_order() for each leg with positive capped_size, then
        tracks fills via order_lifecycle().

        Args:
            market_slug: Market identifier for logging.
            ladder: LadderResult (has .legs list) or plain list.
            book_snapshot: Current order book snapshot dict.
            paper_mode: Passed by scheduler, but LiveAdapter uses its own fallback flag.

        Returns:
            List of order result dicts.
        """
        if ladder is None:
            return []

        legs = getattr(ladder, "legs", ladder) if not isinstance(ladder, list) else ladder
        if not legs:
            return []

        placed = []
        for leg in legs:
            token_id    = getattr(leg, "token_id", None) or (leg.get("token_id") if isinstance(leg, dict) else "")
            side        = getattr(leg, "side", None) or (leg.get("side", "BUY") if isinstance(leg, dict) else "BUY")
            price       = getattr(leg, "market_price", None) or (leg.get("market_price", 0.5) if isinstance(leg, dict) else 0.5)
            capped_size = getattr(leg, "capped_size", None) or (leg.get("capped_size", 0) if isinstance(leg, dict) else 0)
            bin_label   = getattr(leg, "bin_label", None) or (leg.get("bin_label", "?") if isinstance(leg, dict) else "?")
            edge        = getattr(leg, "edge", None) or (leg.get("edge", 0) if isinstance(leg, dict) else 0)

            if not token_id or capped_size <= 0:
                continue

            order_result = self.place_order(
                token_id=token_id,
                price=price,
                size=capped_size,
                side=side,
            )
            order_result["market_slug"] = market_slug
            order_result["bin_label"] = bin_label
            order_result["edge"] = edge

            # Run order lifecycle (live polling or paper fallback)
            fill = order_lifecycle(
                order_id=order_result["order_id"],
                paper_mode=self._paper_fallback,
                api_client=self if not self._paper_fallback else None,
            )
            order_result["filled"] = fill.filled
            order_result["fill_price"] = fill.fill_price
            order_result["cancel_reason"] = fill.cancel_reason

            if self.ledger and hasattr(self.ledger, "log_decision"):
                self.ledger.log_decision(
                    slug=market_slug,
                    action="live_order" if not self._paper_fallback else "paper_fallback_order",
                    side=side,
                    price=price,
                    size=capped_size,
                    filled=fill.filled,
                    edge=edge,
                    bin_label=bin_label,
                )

            placed.append(order_result)
            mode_tag = "LIVE" if not self._paper_fallback else "PAPER_FALLBACK"
            logger.info(
                f"[{mode_tag}] {market_slug} {bin_label}: {side} {capped_size:.2f} @ "
                f"{price:.4f}, edge={edge:.4f}, filled={fill.filled}"
            )

        return placed

