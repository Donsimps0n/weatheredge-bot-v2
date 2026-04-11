"""
Recovery Gate — above/below only lane for 4 whitelisted cities.
Replaces F-Strict gate for the 2026-04-11 recovery build.

All probability values passed in and out are in 0..1 (not percent).
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


def _cfg() -> dict:
    """Load config at call time so runtime changes are picked up."""
    from config import (
        RECOVERY_CITIES,
        RECOVERY_AB_MIN_LEAD_MIN,
        RECOVERY_AB_MAX_LEAD_MIN,
        RECOVERY_AB_MIN_MARKET_PRICE,
        RECOVERY_AB_MAX_MARKET_PRICE,
        RECOVERY_AB_MIN_RECAL_PROB,
        RECOVERY_AB_MIN_EDGE_PP,
        FIXED_TRADE_SIZE_USD,
    )
    return {
        "cities": RECOVERY_CITIES,
        "min_lead": RECOVERY_AB_MIN_LEAD_MIN,
        "max_lead": RECOVERY_AB_MAX_LEAD_MIN,
        "min_mkt": RECOVERY_AB_MIN_MARKET_PRICE,
        "max_mkt": RECOVERY_AB_MAX_MARKET_PRICE,
        "min_recal": RECOVERY_AB_MIN_RECAL_PROB,
        "min_edge": RECOVERY_AB_MIN_EDGE_PP,
        "size": FIXED_TRADE_SIZE_USD,
    }


def recovery_ab_pass(
    *,
    city: str,
    direction: str,
    market_price: float,        # 0..1
    recal_prob: float,          # 0..1 — from recal_prob(raw_prob/100)
    mins_to_resolution: Optional[float],
    raw_prob: float,            # 0..1 — for logging only
    ensemble_prob: float,       # 0..1 — ensemble-only value, for logging
    bias_correction_c: float = 0.0,
    sigma_c: float = 2.5,
) -> tuple[bool, str, dict]:
    """
    Gate for recovery above/below lane.

    Returns:
        ok (bool): True if trade should be placed
        reason (str): human-readable pass/fail reason
        log_dict (dict): full probability audit trail — always populated.
            Attach this to the trade meta field regardless of ok/fail.

    Gate logic:
        1. City must be in RECOVERY_CITIES whitelist
        2. Direction must be "above" or "below"
        3. mins_to_resolution must be known and in [6h, 24h]
        4. market_price must be in [0.10, 0.45]
        5. recal_prob >= 0.25
        6. recal_prob - market_price >= 0.05 (5pp edge)
    """
    c = _cfg()

    log_dict: dict = {
        "raw_prob": round(raw_prob * 100, 2),
        "ensemble_prob": round(ensemble_prob * 100, 2),
        "recal_prob": round(recal_prob * 100, 2),
        "final_prob_used": round(recal_prob * 100, 2),
        "market_price_c": round(market_price * 100, 2),
        "edge_at_decision_pp": round((recal_prob - market_price) * 100, 2),
        "mins_to_resolution": mins_to_resolution,
        "date_parse_ok": mins_to_resolution is not None,
        "city": city,
        "direction": direction,
        "strategy_lane": "recovery_above_below",
        "bias_correction_c": round(bias_correction_c, 3),
        "sigma_used_c": round(sigma_c, 3),
        # Always warn that clob_edge_at_fill is computed from raw_prob, not recal
        "clob_note": "clob_edge_at_fill uses raw_prob — misleading, do not use as edge signal",
        "station_edge_active": False,  # disabled in recovery mode
        "gate_reject_reason": None,
    }

    def _fail(reason: str) -> tuple[bool, str, dict]:
        log_dict["gate_reject_reason"] = reason
        log.debug("RECOVERY_AB_REJECT city=%s reason=%s", city, reason)
        return False, reason, log_dict

    # ── 1. City whitelist ────────────────────────────────────────────────────
    if city not in c["cities"]:
        return _fail(f"city '{city}' not in recovery whitelist {sorted(c['cities'])}")

    # ── 2. Direction ─────────────────────────────────────────────────────────
    if direction not in ("above", "below"):
        return _fail(f"direction '{direction}' — recovery lane accepts above/below only")

    # ── 3. Lead time — hard block if date parse failed ───────────────────────
    if mins_to_resolution is None:
        return _fail("mins_to_resolution unknown — end_date parse failed, gate blocked")
    if mins_to_resolution < c["min_lead"]:
        return _fail(
            f"lead {mins_to_resolution:.0f}m < {c['min_lead']:.0f}m minimum "
            f"(too close to resolution)"
        )
    if mins_to_resolution > c["max_lead"]:
        return _fail(
            f"lead {mins_to_resolution:.0f}m > {c['max_lead']:.0f}m maximum "
            f"(too early, forecast too uncertain)"
        )

    # ── 4. Market price band ─────────────────────────────────────────────────
    if not (c["min_mkt"] <= market_price <= c["max_mkt"]):
        return _fail(
            f"market_price {market_price:.3f} ({market_price*100:.1f}c) outside "
            f"[{c['min_mkt']:.2f}, {c['max_mkt']:.2f}] band"
        )

    # ── 5. Recalibrated probability floor ────────────────────────────────────
    if recal_prob < c["min_recal"]:
        return _fail(
            f"recal_prob {recal_prob:.3f} ({recal_prob*100:.1f}%) < "
            f"{c['min_recal']:.3f} ({c['min_recal']*100:.0f}%) minimum"
        )

    # ── 6. Edge gate ─────────────────────────────────────────────────────────
    edge = recal_prob - market_price
    if edge < c["min_edge"]:
        return _fail(
            f"edge {edge*100:.1f}pp < {c['min_edge']*100:.0f}pp minimum "
            f"(recal={recal_prob*100:.1f}% mkt={market_price*100:.1f}c)"
        )

    # ── PASS ─────────────────────────────────────────────────────────────────
    log.info(
        "RECOVERY_AB_PASS city=%-12s dir=%-5s mkt=%.2f recal=%.2f "
        "edge=%+.1fpp lead=%.0fm size=$%.2f",
        city, direction, market_price, recal_prob,
        edge * 100, mins_to_resolution, c["size"],
    )
    return True, "RECOVERY_PASS", log_dict


def parse_end_date_safe(s: str):
    """
    Parse Polymarket end_date string to UTC-aware datetime.

    Handles:
      - ISO 8601:          "2026-04-09T15:00:00Z"  or  "2026-04-09T15:00:00+00:00"
      - Polymarket live:   "Sun, 12 Apr 2026 00:00:00 GMT"  (actual API format)
      - RFC-ish display:   "Thu, 09 Apr 2026"
      - Short display:     "09 Apr 2026"  /  "Apr 9, 2026"
      - Date only:         "2026-04-09"

    Returns None on any failure — never raises.
    """
    from datetime import datetime, timezone

    if not s:
        return None

    # ── ISO 8601 (most common from Polymarket API) ──────────────────────────
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass

    # ── Polymarket live API format with time component ───────────────────────
    # Actual observed format: "Sun, 12 Apr 2026 00:00:00 GMT"
    # Try with and without the %Z suffix; always attach UTC.
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %Z",  # "Sun, 12 Apr 2026 00:00:00 GMT"
        "%a, %d %b %Y %H:%M:%S",     # "Sun, 12 Apr 2026 00:00:00" (no TZ)
    ):
        try:
            d = datetime.strptime(s.strip(), fmt)
            # Parsed time is present — treat as UTC, do NOT substitute end-of-day.
            return d.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            # %Z raises TypeError on some Python builds when TZ token is unrecognised
            continue

    # ── Fallback: strip trailing TZ token and retry with no-%Z format ───────────
    # Handles platforms where %Z raises TypeError for "GMT" / "UTC" / "EST" etc.
    import re as _re
    _s_no_tz = _re.sub(r'\s+[A-Z]{2,5}$', '', s.strip())
    if _s_no_tz != s.strip():
        try:
            d = datetime.strptime(_s_no_tz, "%a, %d %b %Y %H:%M:%S")
            return d.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass

    # ── Display-only formats (no time component — assume end of day UTC) ──────
    for fmt in (
        "%a, %d %b %Y",   # "Thu, 09 Apr 2026"
        "%d %b %Y",        # "09 Apr 2026"
        "%B %d, %Y",       # "April 9, 2026"
        "%b %d, %Y",       # "Apr 9, 2026"
        "%Y-%m-%d",        # "2026-04-09"
    ):
        try:
            d = datetime.strptime(s.strip(), fmt)
            # Assume end of day UTC for display-only dates (no time component)
            return d.replace(hour=23, minute=59, second=0, tzinfo=timezone.utc)
        except ValueError:
            continue

    log.warning("parse_end_date_safe: unrecognised format %r", s)
    return None
