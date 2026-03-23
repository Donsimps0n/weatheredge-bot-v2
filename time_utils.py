"""
Time utility module for Polymarket temperature trading bot.

Handles:
- Bullet #4: Diurnal staging gates (peak window calculation, staging constraints)
- Bullet #15: Backtest time-causal (entry time computation, causality enforcement)
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import (
    COASTAL_PEAK_SHIFT,
    HIGH_LAT_PEAK,
    HIGH_LAT_THRESHOLD,
    KELLY_SIZE_CAP_NEAR_PEAK,
    LADDER_BINS_DEFAULT,
    LADDER_BINS_NEAR_PEAK,
    LOW_LAT_PEAK,
    MID_LAT_PEAK,
    MID_LAT_THRESHOLD,
    NEAR_PEAK_EV_BOOST,
    POST_PEAK_MIN_EV,
    POST_PEAK_OBS_STALE_HOURS,
    POST_PEAK_RAW_EDGE_MIN,
)


@dataclass
class DiurnalDecision:
    """Result of applying diurnal constraints to a trade."""

    allow_entry: bool
    size_cap: float | None
    min_ev_boost: float
    ladder_bins: tuple[int, int]
    block_reason: str | None = None


@dataclass
class CausalityResult:
    """Result of causality enforcement check."""

    forecast_ok: bool
    obs_ok: bool
    book_ok: bool
    signal_only: bool


def get_peak_window(lat: float, coastal: bool) -> tuple[int, int]:
    """
    Determine the peak temperature window based on latitude and coastal flag.

    Args:
        lat: Latitude in degrees (positive = north)
        coastal: Whether location is coastal (affects timing)

    Returns:
        (peak_start_hour, peak_end_hour) in local time (24h format)

    Rules:
        - lat > 50: peak 13:00-16:00
        - 30 <= lat <= 50: peak 14:00-17:00
        - lat < 30: peak 15:00-18:00
        - coastal flag: shift both hours by +1h
    """
    if lat > HIGH_LAT_THRESHOLD:
        peak_start, peak_end = HIGH_LAT_PEAK
    elif lat >= MID_LAT_THRESHOLD:
        peak_start, peak_end = MID_LAT_PEAK
    else:
        peak_start, peak_end = LOW_LAT_PEAK

    if coastal:
        peak_start += COASTAL_PEAK_SHIFT
        peak_end += COASTAL_PEAK_SHIFT

    return (peak_start, peak_end)


def get_diurnal_stage(
    now_local: datetime, peak_start: int, peak_end: int
) -> str:
    """
    Determine the diurnal stage relative to the peak window.

    Args:
        now_local: Current time in local timezone
        peak_start: Peak window start hour (24h format)
        peak_end: Peak window end hour (24h format)

    Returns:
        One of: "pre-peak", "near-peak", "post-peak"
    """
    hour = now_local.hour

    if hour < peak_start:
        return "pre-peak"
    elif peak_start <= hour <= peak_end:
        return "near-peak"
    else:
        return "post-peak"


def apply_diurnal_constraints(
    stage: str,
    theo_ev: float,
    kelly_size: float,
    obs_max: float | None = None,
    obs_percentile_75: float | None = None,
    obs_max_unchanged_hours: float | None = None,
    raw_edge: float | None = None,
) -> DiurnalDecision:
    """
    Apply diurnal constraints to a trade based on the current stage.

    Args:
        stage: Current diurnal stage ("pre-peak", "near-peak", "post-peak")
        theo_ev: Theoretical expected value of the trade
        kelly_size: Recommended Kelly sizing (before caps)
        obs_max: Maximum observed value (for post-peak checks)
        obs_percentile_75: 75th percentile of observed values
        obs_max_unchanged_hours: Hours since obs_max was last updated
        raw_edge: Raw edge percentage (e.g., 0.12 for +12%)

    Returns:
        DiurnalDecision with entry allowance, size caps, EV boosts, and ladder config
    """
    # Default decision: allow entry, no size cap, no EV boost, default ladder bins
    decision = DiurnalDecision(
        allow_entry=True,
        size_cap=None,
        min_ev_boost=0.0,
        ladder_bins=LADDER_BINS_DEFAULT,
        block_reason=None,
    )

    if stage == "near-peak":
        # Near-peak constraints:
        # - Require min_ev boost of +0.02
        # - Cap size to 15% of Kelly
        # - Use (4,5) ladder bins
        decision.min_ev_boost = NEAR_PEAK_EV_BOOST
        decision.size_cap = KELLY_SIZE_CAP_NEAR_PEAK * kelly_size
        decision.ladder_bins = LADDER_BINS_NEAR_PEAK

    elif stage == "post-peak":
        # Post-peak constraints:
        # 1. No new entries UNLESS obs_max < 75th percentile AND theo_ev >= 0.18
        # 2. Auto-flatten if raw_edge < +12% OR obs_max unchanged > 2h

        # Check condition for allowing new entries
        allow_entry = False
        block_reason = None

        if obs_max is not None and obs_percentile_75 is not None:
            if obs_max < obs_percentile_75 and theo_ev >= POST_PEAK_MIN_EV:
                allow_entry = True
        else:
            # If we don't have observation stats, block entry in post-peak
            block_reason = (
                "post-peak: missing observation data (obs_max, obs_percentile_75)"
            )

        decision.allow_entry = allow_entry
        decision.block_reason = block_reason

        # Check auto-flatten conditions
        if raw_edge is not None and raw_edge < POST_PEAK_RAW_EDGE_MIN:
            decision.block_reason = (
                f"post-peak: raw_edge {raw_edge:.2%} < {POST_PEAK_RAW_EDGE_MIN:.2%}"
            )
            decision.allow_entry = False

        if (
            obs_max_unchanged_hours is not None
            and obs_max_unchanged_hours > POST_PEAK_OBS_STALE_HOURS
        ):
            decision.block_reason = (
                f"post-peak: obs_max stale for {obs_max_unchanged_hours:.1f}h "
                f"> {POST_PEAK_OBS_STALE_HOURS:.1f}h"
            )
            decision.allow_entry = False

    return decision


def compute_t_entry(resolution_time: datetime, local_tz: str) -> datetime:
    """
    Compute the entry time for backtest causality.

    Entry time is the minimum of:
    1. Local 12:00 the day before resolution
    2. Exactly 24h before resolution

    Args:
        resolution_time: UTC datetime of market resolution
        local_tz: IANA timezone string (e.g., 'America/New_York')

    Returns:
        Entry time as UTC datetime
    """
    # Convert resolution time to local timezone
    tz = ZoneInfo(local_tz)
    resolution_local = resolution_time.astimezone(tz)

    # Option 1: Local midnight of the day before resolution, then 12:00
    day_before = resolution_local.replace(
        hour=12, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)

    # Option 2: Exactly 24 hours before resolution
    exactly_24h_before = resolution_local - timedelta(hours=24)

    # Take the minimum (earliest) of the two
    t_entry_local = min(day_before, exactly_24h_before)

    # Convert back to UTC
    t_entry_utc = t_entry_local.astimezone(ZoneInfo("UTC"))

    return t_entry_utc


def enforce_causality(
    forecast_ts: datetime,
    obs_ts: datetime | None,
    book_ts: datetime | None,
    t_entry: datetime,
) -> CausalityResult:
    """
    Enforce time-causal constraints for backtesting.

    All timestamps must be on or before t_entry (the entry cutoff).

    Args:
        forecast_ts: UTC datetime of forecast generation
        obs_ts: UTC datetime of observation data (can be None)
        book_ts: UTC datetime of order book snapshot (can be None)
        t_entry: UTC datetime of entry cutoff

    Returns:
        CausalityResult with causality status for each component
    """
    forecast_ok = forecast_ts <= t_entry
    obs_ok = obs_ts is None or obs_ts <= t_entry
    book_ok = book_ts is None or book_ts <= t_entry
    signal_only = book_ts is None

    return CausalityResult(
        forecast_ok=forecast_ok,
        obs_ok=obs_ok,
        book_ok=book_ok,
        signal_only=signal_only,
    )
