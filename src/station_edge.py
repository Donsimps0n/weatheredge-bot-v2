"""
station_edge.py — Station-matched probability engine for maximum trading edge.

Combines three data sources into a single probability estimate that matches
what Polymarket's resolution station (Wunderground / METAR) will actually read:

1. METAR live observations — real-time temp from the EXACT station Polymarket uses
2. Ensemble forecast (82 members) — calibrated uncertainty from multi_model_forecast
3. Diurnal physics model — predicts max temp from current observation + time of day

Same-day strategy:
    Morning (before noon local): ensemble + obs trajectory → wide but informed
    Afternoon (noon-3pm): obs + remaining heating potential → narrow, high confidence
    Late afternoon (3pm+): obs IS the answer → near-certainty on many bins

Tomorrow strategy:
    Pure ensemble + station bias correction (METAR historical vs model)

Public API
----------
    get_station_probability(city, threshold_c, direction, is_tomorrow, ...) -> StationEdge
    StationEdge.probability — the final probability (0.0 to 1.0)
    StationEdge.confidence — how sure we are (0.0 to 1.0)
    StationEdge.recommended_size — Kelly-fraction position size
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple

log = logging.getLogger("station_edge")

# Try imports — graceful degradation
try:
    from multi_model_forecast import (
        get_ensemble_forecast, ensemble_bin_probability, EnsembleForecast
    )
    _HAS_ENSEMBLE = True
except ImportError:
    _HAS_ENSEMBLE = False
    log.warning("multi_model_forecast not available")

try:
    import active_trader
    _HAS_METAR = True
except ImportError:
    _HAS_METAR = False
    log.warning("active_trader not available — no live observations")


# ── City timezone offsets (UTC) for local hour computation ────────────────────
_CITY_UTC_OFFSET: Dict[str, float] = {
    # US
    "new york": -4, "los angeles": -7, "chicago": -5, "houston": -5,
    "phoenix": -7, "philadelphia": -4, "san diego": -7, "dallas": -5,
    "miami": -4, "atlanta": -4, "boston": -4, "seattle": -7,
    "denver": -6, "nashville": -5, "detroit": -4, "portland": -7,
    "las vegas": -7, "memphis": -5, "baltimore": -4, "milwaukee": -5,
    "san francisco": -7, "minneapolis": -5,
    # Canada
    "toronto": -4, "vancouver": -7, "montreal": -4,
    # Mexico / LATAM
    "mexico city": -6, "buenos aires": -3, "sao paulo": -3,
    "santiago": -4, "lima": -5, "bogota": -5,
    # Europe
    "london": 1, "paris": 2, "berlin": 2, "madrid": 2, "rome": 2,
    "amsterdam": 2, "dublin": 1, "moscow": 3, "warsaw": 2,
    "milan": 2, "barcelona": 2, "stockholm": 2, "copenhagen": 2,
    "lisbon": 1, "athens": 3, "istanbul": 3,
    # Middle East
    "dubai": 4, "tel aviv": 3, "ankara": 3, "riyadh": 3,
    # Asia
    "mumbai": 5.5, "delhi": 5.5, "bangalore": 5.5,
    "singapore": 8, "bangkok": 7, "hong kong": 8,
    "tokyo": 9, "seoul": 9, "shanghai": 8, "beijing": 8,
    "taipei": 8, "jakarta": 7,
    # Oceania
    "sydney": 11, "melbourne": 11, "auckland": 13,
    "wellington": 13,
    # Africa
    "cairo": 2, "johannesburg": 2, "lagos": 1, "nairobi": 3,
}


def _local_hour(city: str) -> float:
    """Get current local hour (0-24) for a city."""
    offset = _CITY_UTC_OFFSET.get(city.lower(), 0)
    utc_now = datetime.now(timezone.utc)
    local = utc_now + timedelta(hours=offset)
    return local.hour + local.minute / 60.0


# ── Diurnal heating model ────────────────────────────────────────────────────

def _max_remaining_heating_f(hour_local: float) -> float:
    """
    Estimate maximum additional heating (°F) from current time to daily peak.

    Based on typical diurnal cycle:
      - Peak occurs ~3pm local (hour 15)
      - Heating rate ~2-3°F/hr in morning, ~1°F/hr early afternoon
      - After 4pm, no more heating (cooling begins)

    Returns conservative (lower) estimate to avoid false confidence.
    """
    if hour_local < 8:
        return (15 - hour_local) * 2.0   # morning: lots of heating left
    elif hour_local < 11:
        return (15 - hour_local) * 1.8
    elif hour_local < 13:
        return (15 - hour_local) * 1.5
    elif hour_local < 15:
        return (15 - hour_local) * 1.0
    elif hour_local < 16:
        return 0.5  # nearly at peak
    else:
        return 0.0  # past peak, cooling


def _same_day_max_estimate_f(obs_f: float, hour_local: float) -> Tuple[float, float]:
    """
    Estimate today's max temperature from current observation.

    Returns (estimated_max_f, uncertainty_f).

    Uncertainty shrinks as the day progresses:
      Early morning: ±8°F (wide — day could go many ways)
      Late morning:  ±4°F
      Early afternoon: ±2°F
      After 3pm: ±1°F (the max is essentially known)
    """
    remaining_heat = _max_remaining_heating_f(hour_local)
    est_max = obs_f + remaining_heat

    # Uncertainty decreases with time
    if hour_local < 9:
        uncertainty = 8.0
    elif hour_local < 11:
        uncertainty = 5.0
    elif hour_local < 13:
        uncertainty = 3.0
    elif hour_local < 15:
        uncertainty = 2.0
    elif hour_local < 16:
        uncertainty = 1.5
    else:
        # After peak: current obs is near the max
        # The max might already have occurred — use obs as max estimate
        est_max = max(obs_f, est_max)
        uncertainty = 1.0

    return est_max, uncertainty


# ── Core probability computation ─────────────────────────────────────────────

@dataclass
class StationEdge:
    """Result of station-matched probability computation."""
    city: str
    threshold_c: float
    direction: str
    is_tomorrow: bool

    # The probability (0.0 to 1.0)
    probability: float = 0.0

    # Confidence in our probability (0.0 to 1.0)
    # High confidence = tight ensemble + live obs agreement
    confidence: float = 0.0

    # Recommended Kelly-fraction position size (0.0 to 0.25)
    recommended_size: float = 0.0

    # Data sources used
    has_obs: bool = False
    obs_temp_f: Optional[float] = None
    obs_max_estimate_f: Optional[float] = None
    has_ensemble: bool = False
    ensemble_members: int = 0
    ensemble_sigma: float = 0.0

    # Probability breakdown
    obs_probability: Optional[float] = None   # from observation alone
    ens_probability: Optional[float] = None   # from ensemble alone
    blend_weight_obs: float = 0.0             # how much obs contributed

    # Quality
    source: str = "unknown"  # "obs+ensemble", "obs_only", "ensemble_only", "fallback"
    local_hour: float = 0.0


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _gaussian_prob(mean_c: float, sigma_c: float, threshold_c: float,
                   direction: str) -> float:
    """Gaussian bin probability."""
    if sigma_c <= 0:
        sigma_c = 2.5
    if direction == "exact":
        z_hi = (threshold_c + 0.5 - mean_c) / sigma_c
        z_lo = (threshold_c - 0.5 - mean_c) / sigma_c
        return max(0.001, min(0.999, _ncdf(z_hi) - _ncdf(z_lo)))
    elif direction == "above":
        z = (mean_c - threshold_c) / sigma_c
        return max(0.001, min(0.999, _ncdf(z)))
    else:
        z = (threshold_c - mean_c) / sigma_c
        return max(0.001, min(0.999, _ncdf(z)))


def get_station_probability(
    city: str,
    lat: float,
    lon: float,
    threshold_c: float,
    direction: str = "exact",
    is_tomorrow: bool = True,
    bias_correction_c: float = 0.0,
) -> StationEdge:
    """
    Compute station-matched probability for a Polymarket temperature bin.

    This is the top-level function that combines all data sources.
    """
    edge = StationEdge(
        city=city, threshold_c=threshold_c,
        direction=direction, is_tomorrow=is_tomorrow,
    )
    edge.local_hour = _local_hour(city)

    # ── Get live observation (same-day only) ─────────────────────────────
    obs_f = None
    obs_raw_f = None
    if not is_tomorrow and _HAS_METAR:
        try:
            obs_raw_f = active_trader.get_obs_temp_f(city)
            if obs_raw_f is not None:
                _record_obs(city, obs_raw_f)
                # Use median-filtered value to reject glitches
                obs_f = _get_filtered_obs(city)
                if obs_f is not None:
                    edge.has_obs = True
                    edge.obs_temp_f = obs_f
                    _age = _get_obs_age_seconds(city)
                    _confirmed = _is_obs_confirmed(city)
                    log.debug(
                        "Live obs for %s: raw=%.1fF filtered=%.1fF age=%.0fs confirmed=%s (hour=%.1f)",
                        city, obs_raw_f, obs_f, _age, _confirmed, edge.local_hour)
        except Exception as e:
            log.debug("Obs fetch failed for %s: %s", city, e)

    # ── Get ensemble forecast ────────────────────────────────────────────
    ens_fc = None
    ens_prob = None
    if _HAS_ENSEMBLE:
        try:
            from multi_model_forecast import get_ensemble_probability
            ens_prob_raw, ens_fc = get_ensemble_probability(
                city=city, lat=lat, lon=lon,
                threshold_c=threshold_c,
                direction=direction,
                is_tomorrow=is_tomorrow,
                bias_correction_c=bias_correction_c,
            )
            ens_prob = ens_prob_raw
            edge.has_ensemble = True
            edge.ensemble_members = ens_fc.n_ensemble_members
            edge.ensemble_sigma = ens_fc.blended_sigma
            edge.ens_probability = ens_prob
        except Exception as e:
            log.debug("Ensemble failed for %s: %s", city, e)

    # ── Compute observation-based probability (same-day) ─────────────────
    obs_prob = None
    if obs_f is not None and not is_tomorrow:
        est_max_f, uncertainty_f = _same_day_max_estimate_f(obs_f, edge.local_hour)
        edge.obs_max_estimate_f = est_max_f

        # Convert to Celsius for probability calculation
        est_max_c = (est_max_f - 32) * 5.0 / 9.0
        uncertainty_c = uncertainty_f * 5.0 / 9.0

        obs_prob = _gaussian_prob(est_max_c, uncertainty_c, threshold_c, direction)
        edge.obs_probability = obs_prob

    # ── Blend probabilities based on available data and time ─────────────
    if is_tomorrow:
        # Tomorrow: pure ensemble (obs don't help predict tomorrow)
        if ens_prob is not None:
            edge.probability = ens_prob
            edge.blend_weight_obs = 0.0
            edge.source = "ensemble_only"
        else:
            # No ensemble — conservative fallback
            edge.probability = 0.5 if direction != "exact" else 0.05
            edge.source = "fallback"

    elif obs_prob is not None and ens_prob is not None:
        # Same-day with BOTH obs and ensemble: blend based on time of day
        # Later in day → trust obs more (they're measuring reality)
        hour = edge.local_hour
        if hour < 10:
            obs_weight = 0.3   # morning: ensemble still matters
        elif hour < 13:
            obs_weight = 0.5   # midday: equal weight
        elif hour < 15:
            obs_weight = 0.7   # afternoon: obs dominate
        elif hour < 16:
            obs_weight = 0.85  # late afternoon: obs nearly definitive
        else:
            obs_weight = 0.95  # past peak: obs ARE the answer

        # Apply freshness + confirmation cap
        _cap = _obs_weight_cap(city)
        if obs_weight > _cap:
            log.info("OBS_CAP: %s weight %.2f -> %.2f (age=%.0fs confirmed=%s)",
                     city, obs_weight, _cap, _get_obs_age_seconds(city),
                     _is_obs_confirmed(city))
            obs_weight = _cap

        edge.blend_weight_obs = obs_weight
        edge.probability = obs_weight * obs_prob + (1 - obs_weight) * ens_prob
        edge.source = "obs+ensemble"

    elif obs_prob is not None:
        edge.probability = obs_prob
        edge.blend_weight_obs = 1.0
        edge.source = "obs_only"

    elif ens_prob is not None:
        edge.probability = ens_prob
        edge.blend_weight_obs = 0.0
        edge.source = "ensemble_only"

    else:
        edge.probability = 0.5 if direction != "exact" else 0.05
        edge.source = "fallback"

    # Clamp
    edge.probability = max(0.001, min(0.999, edge.probability))

    # ── Compute confidence ───────────────────────────────────────────────
    edge.confidence = _compute_confidence(edge, ens_fc)

    # ── Compute recommended Kelly size ───────────────────────────────────
    edge.recommended_size = _compute_kelly_size(edge)

    return edge


def _compute_confidence(edge: StationEdge, ens_fc) -> float:
    """
    Confidence score (0-1) based on data quality and agreement.

    High confidence when:
    - Many ensemble members (>50)
    - Low ensemble spread (tight sigma)
    - Live obs available and agrees with ensemble
    - Later in the day (same-day markets)
    """
    conf = 0.0

    # Base confidence from ensemble quality
    if edge.has_ensemble:
        if edge.ensemble_members >= 70:
            conf += 0.3
        elif edge.ensemble_members >= 40:
            conf += 0.2
        else:
            conf += 0.1

        # Tight ensemble = more confidence
        if edge.ensemble_sigma < 1.0:
            conf += 0.2
        elif edge.ensemble_sigma < 2.0:
            conf += 0.1

    # Obs boost (same-day)
    if edge.has_obs and not edge.is_tomorrow:
        hour = edge.local_hour
        if hour >= 15:
            conf += 0.4    # past peak: near-certain
        elif hour >= 13:
            conf += 0.3
        elif hour >= 11:
            conf += 0.2
        else:
            conf += 0.1

        # Obs + ensemble agreement boost
        if edge.obs_probability is not None and edge.ens_probability is not None:
            agreement = 1.0 - abs(edge.obs_probability - edge.ens_probability)
            conf += agreement * 0.1

    return min(1.0, conf)


def _compute_kelly_size(edge: StationEdge) -> float:
    """
    Compute Kelly-criterion recommended position size.

    Kelly fraction = (p * b - q) / b
    where p = our probability, q = 1-p, b = payout odds

    We use fractional Kelly (25%) to be conservative.

    Returns a fraction (0.0 to 0.25 = max 25% of bankroll).
    """
    p = edge.probability
    if p <= 0 or p >= 1:
        return 0.0

    # For exact bins, the payout is $1 per share
    # We need market price to compute Kelly properly,
    # but we can compute a confidence-scaled base size
    # Full Kelly would be too aggressive — use quarter-Kelly
    kelly_raw = max(0, p - 0.5) * 2  # simplified: 0 at 50%, 1 at 100%
    kelly_scaled = kelly_raw * edge.confidence * 0.25  # quarter-Kelly with confidence

    return min(0.25, kelly_scaled)


# ── Signal selection: should we trade this market? ───────────────────────────

@dataclass
class TradeDecision:
    """Whether and how to trade a specific market."""
    should_trade: bool = False
    signal: str = "SKIP"          # BUY YES, BUY NO, SKIP
    size_dollars: float = 0.0     # recommended position size
    edge_pct: float = 0.0         # our prob - market prob (percentage points)
    our_prob: float = 0.0         # our probability (0-100)
    market_prob: float = 0.0      # market implied probability (0-100)
    confidence: float = 0.0       # confidence (0-1)
    reason: str = ""              # human-readable explanation


def evaluate_trade(
    city: str,
    lat: float,
    lon: float,
    threshold_c: float,
    direction: str,
    is_tomorrow: bool,
    market_yes_price: float,
    bias_correction_c: float = 0.0,
    bankroll: float = 1000.0,
    min_edge_pct: float = 8.0,
    min_confidence: float = 0.3,
) -> TradeDecision:
    """
    Full trade evaluation: should we trade, which direction, how much.

    This replaces the old signal scanner logic with station-matched edge.

    Args:
        market_yes_price: Current YES token price (0.0 to 1.0)
        bankroll: Current bankroll in dollars
        min_edge_pct: Minimum edge (percentage points) to trade
        min_confidence: Minimum confidence score to trade
    """
    decision = TradeDecision()
    decision.market_prob = round(market_yes_price * 100, 1)

    # Get station-matched probability
    se = get_station_probability(
        city=city, lat=lat, lon=lon,
        threshold_c=threshold_c, direction=direction,
        is_tomorrow=is_tomorrow,
        bias_correction_c=bias_correction_c,
    )

    decision.our_prob = round(se.probability * 100, 1)
    decision.confidence = se.confidence
    decision.edge_pct = round(decision.our_prob - decision.market_prob, 1)

    # ── Decision logic ───────────────────────────────────────────────────

    # Skip if confidence too low
    if se.confidence < min_confidence:
        decision.reason = f"Low confidence ({se.confidence:.2f} < {min_confidence})"
        return decision

    # Skip if edge too small
    if abs(decision.edge_pct) < min_edge_pct:
        decision.reason = f"Edge too small ({decision.edge_pct:+.1f}pp < {min_edge_pct}pp)"
        return decision

    # BUY YES: our probability >> market price
    if decision.edge_pct > min_edge_pct:
        decision.should_trade = True
        decision.signal = "BUY YES"

        # Size based on Kelly + confidence
        kelly_frac = se.recommended_size
        decision.size_dollars = round(bankroll * kelly_frac, 2)

        # Floor: minimum $2, maximum 10% of bankroll
        decision.size_dollars = max(2.0, min(bankroll * 0.10, decision.size_dollars))

        decision.reason = (
            f"BUY YES: {decision.our_prob}% vs mkt {decision.market_prob}% "
            f"(edge={decision.edge_pct:+.1f}pp, conf={se.confidence:.2f}, "
            f"src={se.source}, members={se.ensemble_members})"
        )

    # BUY NO: market price >> our probability (market overestimates YES)
    elif decision.edge_pct < -min_edge_pct:
        # Only take NO if we're very confident the market is wrong
        if se.confidence >= 0.5 and abs(decision.edge_pct) > min_edge_pct * 1.5:
            decision.should_trade = True
            decision.signal = "BUY NO"

            kelly_frac = se.recommended_size * 0.5  # half-size NO trades
            decision.size_dollars = round(bankroll * kelly_frac, 2)
            decision.size_dollars = max(2.0, min(bankroll * 0.05, decision.size_dollars))

            decision.reason = (
                f"BUY NO: mkt {decision.market_prob}% but we say {decision.our_prob}% "
                f"(edge={decision.edge_pct:+.1f}pp, conf={se.confidence:.2f})"
            )
        else:
            decision.reason = (
                f"NO edge exists ({decision.edge_pct:+.1f}pp) but confidence "
                f"too low ({se.confidence:.2f}) for NO trade"
            )

    return decision


__all__ = [
    "StationEdge",
    "TradeDecision",
    "get_station_probability",
    "evaluate_trade",
]
