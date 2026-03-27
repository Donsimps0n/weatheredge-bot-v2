"""
CLOB Order Book utilities for WeatherEdge.

Provides real bid/ask depth, spread, and fill-aware sizing.
Uses Polymarket CLOB API (public, no auth needed for reads).
"""
import logging
import time
from typing import Optional, Dict, Tuple

log = logging.getLogger(__name__)

_client = None
_book_cache: Dict[str, dict] = {}  # token_id -> {book, ts}
_CACHE_TTL = 30  # seconds


def _get_client():
    global _client
    if _client is None:
        from py_clob_client.client import ClobClient
        _client = ClobClient("https://clob.polymarket.com", chain_id=137)
    return _client


def get_book(token_id: str) -> Optional[dict]:
    """Fetch L2 order book for a token. Returns parsed dict with bids/asks."""
    now = time.time()
    cached = _book_cache.get(token_id)
    if cached and (now - cached["ts"]) < _CACHE_TTL:
        return cached["book"]
    try:
        client = _get_client()
        raw = client.get_order_book(token_id)
        book = {
            "bids": [{"price": float(o.price), "size": float(o.size)} for o in (raw.bids or [])],
            "asks": [{"price": float(o.price), "size": float(o.size)} for o in (raw.asks or [])],
        }
        # Sort: bids descending, asks ascending
        book["bids"].sort(key=lambda x: x["price"], reverse=True)
        book["asks"].sort(key=lambda x: x["price"])
        # Derived fields
        best_bid = book["bids"][0]["price"] if book["bids"] else 0
        best_ask = book["asks"][0]["price"] if book["asks"] else 1
        book["best_bid"] = best_bid
        book["best_ask"] = best_ask
        book["mid"] = round((best_bid + best_ask) / 2, 4)
        book["spread"] = round(best_ask - best_bid, 4)
        book["spread_pct"] = round(book["spread"] / max(0.001, book["mid"]) * 100, 1)
        # Depth within 2% of mid
        _depth_bids = sum(o["size"] * o["price"] for o in book["bids"]
                         if o["price"] >= best_bid * 0.98)
        _depth_asks = sum(o["size"] * o["price"] for o in book["asks"]
                         if o["price"] <= best_ask * 1.02)
        book["bid_depth_usd"] = round(_depth_bids, 2)
        book["ask_depth_usd"] = round(_depth_asks, 2)
        _book_cache[token_id] = {"book": book, "ts": now}
        return book
    except Exception as e:
        log.warning("CLOB book fetch failed for %s: %s", token_id[:16], e)
        return None


def expected_fill_price(book: dict, side: str, spend_usd: float) -> Tuple[float, float]:
    """Walk the book to compute volume-weighted average fill price.

    Args:
        book: parsed order book from get_book()
        side: "buy" or "sell"
        spend_usd: how much USD we want to deploy

    Returns:
        (avg_fill_price, filled_usd) — if filled_usd < spend_usd, book is too thin.
    """
    levels = book["asks"] if side == "buy" else book["bids"]
    remaining = spend_usd
    total_shares = 0
    total_cost = 0
    for level in levels:
        level_usd = level["price"] * level["size"]
        take = min(remaining, level_usd)
        shares = take / level["price"]
        total_shares += shares
        total_cost += take
        remaining -= take
        if remaining <= 0:
            break
    if total_shares == 0:
        return 0, 0
    avg_price = round(total_cost / total_shares, 4)
    return avg_price, round(total_cost, 2)


def edge_at_fill(our_prob: float, book: dict, side: str, spend_usd: float) -> dict:
    """Compute real EV accounting for fill price, spread, and depth.

    Args:
        our_prob: our probability estimate (0-1 scale)
        book: parsed order book
        side: "buy_yes" or "buy_no"
        spend_usd: target spend

    Returns:
        dict with edge_at_fill, spread_cost, fill_price, fillable_usd, tradeable (bool)
    """
    book_side = "buy"  # we're always buying (YES or NO tokens)
    fill_price, filled = expected_fill_price(book, book_side, spend_usd)

    if fill_price <= 0 or filled < 1.0:
        return {"tradeable": False, "reason": "insufficient_depth", "fillable_usd": filled}

    # Fees: Polymarket charges ~0% maker, ~2% taker on profit
    # For conservative estimate, assume taker and add 1% cost proxy
    fee_cost = 0.01

    # Dollar EV at fill: (p - fill_price) / fill_price - fee
    if side == "buy_yes":
        raw_ev = (our_prob - fill_price) / max(0.01, fill_price)
    else:  # buy_no
        raw_ev = ((1 - our_prob) - fill_price) / max(0.01, fill_price)

    net_ev = raw_ev - fee_cost

    mid = book.get("mid", fill_price)
    spread_cost = abs(fill_price - mid)

    return {
        "tradeable": net_ev > 0.03,  # at least 3% net EV after costs
        "edge_at_fill": round(net_ev * 100, 1),
        "fill_price": fill_price,
        "spread": book.get("spread", 0),
        "spread_pct": book.get("spread_pct", 0),
        "spread_cost": round(spread_cost, 4),
        "fillable_usd": filled,
        "bid_depth": book.get("bid_depth_usd", 0),
        "ask_depth": book.get("ask_depth_usd", 0),
        "reason": "ok" if net_ev > 0.03 else "edge_too_thin_after_costs",
    }


# Batch prefetch for efficiency
def prefetch_books(token_ids: list):
    """Prefetch order books for a batch of token IDs."""
    fetched = 0
    for tid in token_ids:
        if tid and get_book(tid):
            fetched += 1
    return fetched
