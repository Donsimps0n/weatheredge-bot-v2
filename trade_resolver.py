"""
trade_resolver.py — Resolve open paper/live trades against Polymarket outcomes.

Fetches all resolved weather events from the Gamma API, builds a
token_id → outcome lookup, then matches unresolved trades in the ledger
and records win/loss + P&L.

Public API
----------
    resolve_trades(force=False) -> dict
        Call from schedule_loop or manually. Checks at most once per hour
        unless force=True. Returns summary dict.

P&L rules by signal type
-------------------------
    BUY YES   : bought YES tokens at `price`. If token resolves YES → payout = size * $1.
                PnL = size - spend.  If NO → PnL = -spend.
    NO_HARVEST: bought NO tokens at `price`. If token resolves NO → payout = size * $1.
                PnL = size - spend.  If YES → PnL = -spend.
    EXIT_SELL_ALL: already sold position; PnL = spend (proceeds).
                  We mark these as resolved with pnl=0 since the entry leg is the one
                  that matters — the exit leg is just closing.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("trade_resolver")

# ── Gamma API ─────────────────────────────────────────────────────────────────
_GAMMA_BASE  = "https://gamma-api.polymarket.com"
_PAGE_LIMIT  = 100

# Guard: don't re-check more than once per hour
_last_check_ts: float = 0.0
_CHECK_INTERVAL_S = 3600  # 1 hour


def _fetch_gamma_page(offset: int) -> list:
    params = urllib.parse.urlencode({
        "tag_slug": "weather", "closed": "true",
        "limit": _PAGE_LIMIT, "offset": offset,
    })
    url = f"{_GAMMA_BASE}/events?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "weatheredge-bot/2.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _build_resolution_maps() -> Tuple[Dict[str, dict], Dict[str, dict], Dict[str, dict]]:
    """
    Fetch all resolved weather events and build THREE lookup structures:

    1. full_token_map:   { full_token_id (76 digits) → resolution_entry }
    2. prefix_map:       { first_12_digits → resolution_entry }
       (for trades where JS truncated to ~16 digits or stored with "...")
    3. question_map:     { normalized_question_text → resolution_entry }
       (fallback: match by city + date + bin label)

    resolution_entry = {
        "yes_won": bool,
        "bin_label": str,
        "event_title": str,
        "token_side": "YES" | "NO",
    }
    """
    full_map: Dict[str, dict] = {}
    prefix_map: Dict[str, dict] = {}
    question_map: Dict[str, dict] = {}
    page = 0

    log.info("Building resolution maps from Gamma API ...")

    while True:
        offset = page * _PAGE_LIMIT
        try:
            events = _fetch_gamma_page(offset)
        except Exception as exc:
            log.error("Gamma page error at offset=%d: %s", offset, exc)
            break

        if not events:
            break

        for event in events:
            title = event.get("title", "")
            if "highest temperature" not in title.lower():
                continue

            for mkt in event.get("markets", []):
                prices_raw = mkt.get("outcomePrices", "[]")
                try:
                    prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                except Exception:
                    continue

                if not prices or len(prices) < 2:
                    continue

                yes_price = str(prices[0])
                if yes_price not in ("0", "1"):
                    continue  # not yet resolved

                yes_won = (yes_price == "1")
                bin_label = mkt.get("groupItemTitle", "?")
                question = mkt.get("question", "")

                clob_tokens_raw = mkt.get("clobTokenIds", "[]")
                try:
                    clob_tokens = (json.loads(clob_tokens_raw)
                                   if isinstance(clob_tokens_raw, str)
                                   else clob_tokens_raw)
                except Exception:
                    clob_tokens = []

                # Build entries for YES (index 0) and NO (index 1) tokens
                for i, side_label in enumerate(["YES", "NO"]):
                    if i >= len(clob_tokens) or not clob_tokens[i]:
                        continue
                    tok = str(clob_tokens[i])
                    entry = {
                        "yes_won": yes_won,
                        "bin_label": bin_label,
                        "event_title": title[:120],
                        "token_side": side_label,
                    }
                    # Full token map (exact match)
                    full_map[tok] = entry
                    # Prefix map: store first 12 digits (covers JS-truncated tokens)
                    prefix = tok[:12]
                    prefix_map[prefix] = entry

                # Question map: normalize question text for fuzzy matching
                if question:
                    q_key = _normalize_question(question)
                    question_map[q_key] = {
                        "yes_won": yes_won,
                        "bin_label": bin_label,
                        "event_title": title[:120],
                    }

        if page % 5 == 0:
            log.debug("  Page %d: %d full tokens, %d prefixes, %d questions",
                      page, len(full_map), len(prefix_map), len(question_map))

        if len(events) < _PAGE_LIMIT:
            break
        page += 1
        time.sleep(0.15)

    log.info("Resolution maps built: %d full, %d prefix, %d question entries from %d pages",
             len(full_map), len(prefix_map), len(question_map), page + 1)
    return full_map, prefix_map, question_map


def _normalize_question(q: str) -> str:
    """Normalize a question string for fuzzy matching (lowercase, strip punctuation)."""
    return re.sub(r"[^a-z0-9 ]", "", q.lower()).strip()


def _lookup_resolution(
    token_id: str,
    question: str,
    signal: str,
    full_map: Dict[str, dict],
    prefix_map: Dict[str, dict],
    question_map: Dict[str, dict],
) -> Optional[dict]:
    """
    Try to find a resolution entry for a trade using cascading lookups:
    1. Exact full token_id match (for 75+ digit tokens)
    2. Prefix match (first 12 digits — handles JS precision loss + "..." truncation)
    3. Question text match (handles cases where token_id is completely garbled)

    Returns resolution entry dict or None.
    """
    # Strategy 1: Exact full match
    if token_id in full_map:
        entry = full_map[token_id]
        entry["match_method"] = "exact"
        return entry

    # Strategy 2: Prefix match (strip "..." suffix, take first 12 digits)
    clean_tok = token_id.rstrip(".")
    prefix = clean_tok[:12]
    if len(prefix) >= 10 and prefix in prefix_map:
        entry = prefix_map[prefix].copy()
        entry["match_method"] = "prefix"
        return entry

    # Strategy 3: Question text match
    if question and not question.startswith("EXIT"):
        q_key = _normalize_question(question)
        if q_key in question_map:
            entry = question_map[q_key].copy()
            # Infer token_side from signal type
            if signal == "BUY YES":
                entry["token_side"] = "YES"
            elif signal == "NO_HARVEST":
                entry["token_side"] = "NO"
            else:
                entry["token_side"] = "YES"
            entry["match_method"] = "question"
            return entry

    return None


# ── P&L computation ──────────────────────────────────────────────────────────

def _compute_pnl(
    signal: str,
    token_side: str,
    yes_won: bool,
    price: float,
    size: float,
    spend: float,
) -> Tuple[bool, float, float]:
    """
    Compute (won, resolution_price, pnl) for a trade.

    Args:
        signal: Trade signal type (BUY YES, NO_HARVEST, EXIT_SELL_ALL).
        token_side: Which side of the token this trade holds ("YES" or "NO").
        yes_won: Whether the YES outcome won for this market.
        price: Entry price per share.
        size: Number of shares.
        spend: Total amount spent (price * size).

    Returns:
        (won: bool, resolution_price: float, pnl: float)
    """
    if signal == "EXIT_SELL_ALL":
        # Exit trades are closing an existing position.
        # The proceeds are `spend`. Mark as resolved with pnl=0;
        # the P&L belongs to the entry leg.
        return True, price, 0.0

    if signal == "BUY YES":
        # Holding YES tokens
        if yes_won:
            # YES resolved at $1 — we get $1 per share
            payout = size * 1.0
            pnl = payout - spend
            return True, 1.0, round(pnl, 4)
        else:
            # YES resolved at $0 — total loss
            return False, 0.0, round(-spend, 4)

    if signal == "NO_HARVEST":
        # Holding NO tokens (bought NO at `price`)
        if not yes_won:
            # NO resolved at $1 — we get $1 per share
            payout = size * 1.0
            pnl = payout - spend
            return True, 1.0, round(pnl, 4)
        else:
            # YES won → NO resolved at $0 — total loss
            return False, 0.0, round(-spend, 4)

    # Unknown signal — treat as loss
    log.warning("Unknown signal type '%s', treating as unresolved", signal)
    return False, 0.0, round(-spend, 4)


# ── Public entry point ────────────────────────────────────────────────────────

def resolve_trades(force: bool = False) -> dict:
    """
    Check unresolved trades against Polymarket outcomes and record P&L.

    Should be called from schedule_loop each cycle. The once-per-hour guard
    prevents excessive API calls.

    Returns:
        Summary dict: { ran, resolved_count, wins, losses, total_pnl, errors }
    """
    global _last_check_ts

    now = time.time()
    if not force and (now - _last_check_ts) < _CHECK_INTERVAL_S:
        return {"ran": False, "reason": "throttled"}

    _last_check_ts = now
    summary = {
        "ran": True,
        "resolved_count": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "errors": [],
    }

    try:
        import trade_ledger
    except ImportError:
        summary["errors"].append("trade_ledger not importable")
        return summary

    # Step 1: Get unresolved trades
    unresolved = trade_ledger.get_unresolved_trades()
    if not unresolved:
        log.info("trade_resolver: no unresolved trades")
        return summary

    log.info("trade_resolver: %d unresolved trades to check", len(unresolved))

    # Step 2: Build resolution maps from Gamma API (full + prefix + question)
    try:
        full_map, prefix_map, question_map = _build_resolution_maps()
    except Exception as exc:
        log.error("trade_resolver: failed to build resolution maps: %s", exc)
        summary["errors"].append(str(exc))
        return summary

    total_entries = len(full_map) + len(prefix_map) + len(question_map)
    if total_entries == 0:
        log.warning("trade_resolver: all maps empty — no resolved weather events found")
        return summary

    # Step 3: Match trades against resolution maps (cascading lookup)
    match_methods = {"exact": 0, "prefix": 0, "question": 0, "miss": 0}

    for trade in unresolved:
        trade_id  = trade["id"]
        token_id  = str(trade.get("token_id", ""))
        signal    = trade.get("signal", "")
        question  = trade.get("question", "")
        price     = float(trade.get("price", 0))
        size      = float(trade.get("size", 0))
        spend     = float(trade.get("spend", 0))
        city      = trade.get("city", "")

        # EXIT trades are position closures — auto-resolve with pnl=0
        # Must check BEFORE token_id gate since EXIT trades often have empty token_ids
        if signal == "EXIT_SELL_ALL" or signal.startswith("EXIT"):
            try:
                trade_ledger.mark_resolved(
                    trade_id=trade_id, won=True,
                    resolution_price=price, pnl=0.0,
                )
                summary["resolved_count"] += 1
                summary["wins"] += 1
                log.info("trade_resolver: #%d EXIT auto-resolved pnl=$0.00", trade_id)
            except Exception as exc:
                log.error("trade_resolver: EXIT mark_resolved failed for #%d: %s", trade_id, exc)
                summary["errors"].append(f"#{trade_id}: {exc}")
            continue

        if not token_id:
            continue

        resolution = _lookup_resolution(
            token_id, question, signal, full_map, prefix_map, question_map
        )
        if resolution is None:
            match_methods["miss"] += 1
            continue

        match_methods[resolution.get("match_method", "?")] = (
            match_methods.get(resolution.get("match_method", "?"), 0) + 1
        )
        yes_won    = resolution["yes_won"]
        token_side = resolution.get("token_side", "YES")
        bin_label  = resolution["bin_label"]

        won, res_price, pnl = _compute_pnl(
            signal=signal,
            token_side=token_side,
            yes_won=yes_won,
            price=price,
            size=size,
            spend=spend,
        )

        try:
            trade_ledger.mark_resolved(
                trade_id=trade_id,
                won=won,
                resolution_price=res_price,
                pnl=pnl,
            )
            summary["resolved_count"] += 1
            if won:
                summary["wins"] += 1
            else:
                summary["losses"] += 1
            summary["total_pnl"] += pnl

            log.info(
                "trade_resolver: #%d %s %s %s → %s, pnl=$%.2f (bin=%s)",
                trade_id, city, signal, "YES_WON" if yes_won else "NO_WON",
                "WIN" if won else "LOSS", pnl, bin_label,
            )
        except Exception as exc:
            log.error("trade_resolver: mark_resolved failed for #%d: %s", trade_id, exc)
            summary["errors"].append(f"#{trade_id}: {exc}")

    summary["match_methods"] = match_methods

    log.info(
        "trade_resolver: done — resolved=%d, W=%d, L=%d, PnL=$%.2f, matches=%s",
        summary["resolved_count"], summary["wins"], summary["losses"],
        summary["total_pnl"], match_methods,
    )
    return summary
