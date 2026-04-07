"""
multi_model_forecast.py — Ensemble-based temperature probability engine.

Replaces the single-point forecast + static sigma Gaussian CDF approach
with REAL probability distributions derived from:

1. Open-Meteo Ensemble API:  51 ECMWF IFS + 31 GFS GEFS = 82 ensemble members
2. Open-Meteo Multi-Model:   7 deterministic models (GFS, ECMWF, ICON, JMA, GEM,
                              Météo-France, UKMO) for cross-validation

Bin probabilities are computed by counting ensemble members that fall in each
temperature bin, then blending with a kernel-smoothed distribution from the
deterministic models. This produces calibrated probabilities with real
uncertainty — not a Gaussian guess.

Public API
----------
    get_ensemble_forecast(lat, lon, city, timezone, forecast_day) -> EnsembleForecast
        Returns full ensemble data + bin probabilities for a city/day.

    ensemble_bin_probability(forecast, threshold_c, direction, is_exact) -> float
        Compute probability for a specific Polymarket bin from ensemble data.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("multi_model_forecast")

# ── API endpoints (free, no key) ────────────────────────────────────────────
_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
_MULTIMODEL_URL = "https://api.open-meteo.com/v1/forecast"

# Cache: city_key → (timestamp, EnsembleForecast)
_cache: Dict[str, Tuple[float, "EnsembleForecast"]] = {}
_CACHE_TTL_S = 1800  # 30 minutes — ensembles update every 6h, no need to hammer


@dataclass
class EnsembleForecast:
    """Container for ensemble forecast data and derived probabilities."""
    city: str
    lat: float
    lon: float
    forecast_day: int  # 0=today, 1=tomorrow, 2=day-after

    # Raw ensemble members (°C, daily max temperatures)
    ecmwf_members: List[float] = field(default_factory=list)   # up to 51
    gfs_members: List[float] = field(default_factory=list)      # up to 31
    all_members: List[float] = field(default_factory=list)      # combined 82

    # Deterministic multi-model forecasts (°C)
    model_forecasts: Dict[str, float] = field(default_factory=dict)  # model_name → temp

    # Derived statistics
    ensemble_mean: float = 0.0
    ensemble_std: float = 0.0
    ensemble_min: float = 0.0
    ensemble_max: float = 0.0
    ensemble_p10: float = 0.0
    ensemble_p25: float = 0.0
    ensemble_p50: float = 0.0
    ensemble_p75: float = 0.0
    ensemble_p90: float = 0.0

    multimodel_mean: float = 0.0
    multimodel_std: float = 0.0

    # Blended sigma (real uncertainty from ensemble spread)
    blended_sigma: float = 2.5  # fallback

    # Quality flags
    n_ensemble_members: int = 0
    n_models: int = 0
    data_quality: str = "unknown"  # "good", "partial", "fallback"
    fetch_ts: float = 0.0


_openmeteo_429_count = 0
_openmeteo_429_backoff_until = 0.0
_OPENMETEO_429_BACKOFF_S = 120  # Back off for 120s after a 429 — gives cache TTL time to clear

def _fetch_json(url: str, timeout: int = 20) -> dict:
    """Fetch JSON from URL with 429 backoff and error handling."""
    global _openmeteo_429_count, _openmeteo_429_backoff_until
    # Honour active backoff window — raise immediately so caller falls back
    if time.time() < _openmeteo_429_backoff_until:
        remaining = round(_openmeteo_429_backoff_until - time.time(), 0)
        raise urllib.error.HTTPError(url, 429, f"Rate-limit backoff active ({remaining}s remaining)", {}, None)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "weatheredge-bot/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _openmeteo_429_count += 1
            _openmeteo_429_backoff_until = time.time() + _OPENMETEO_429_BACKOFF_S
            log.warning("OPENMETEO_429: rate limited (count=%d) — backing off %ds",
                        _openmeteo_429_count, _OPENMETEO_429_BACKOFF_S)
        raise


def _percentile(sorted_vals: List[float], p: float) -> float:
    """Compute percentile from sorted list. p in [0, 100]."""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def _fetch_ensemble(lat: float, lon: float, tz: str, forecast_days: int = 3) -> dict:
    """
    Fetch ensemble forecasts from Open-Meteo.
    Returns raw JSON with ecmwf_ifs025 (51 members) + gfs_seamless (31 members).
    """
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "models": "ecmwf_ifs025,gfs_seamless",
        "forecast_days": forecast_days,
        "temperature_unit": "celsius",
        "timezone": tz or "auto",
    })
    url = f"{_ENSEMBLE_URL}?{params}"
    log.debug("Fetching ensemble: %s", url)
    return _fetch_json(url)


def _fetch_multimodel(lat: float, lon: float, tz: str, forecast_days: int = 3) -> dict:
    """
    Fetch 7 deterministic model forecasts from Open-Meteo.
    Models: GFS, ECMWF, ICON, JMA, GEM, Météo-France, UKMO.
    """
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "models": "gfs_seamless,ecmwf_ifs025,icon_seamless,jma_seamless,gem_seamless,meteofrance_seamless,ukmo_seamless",
        "forecast_days": forecast_days,
        "temperature_unit": "celsius",
        "timezone": tz or "auto",
    })
    url = f"{_MULTIMODEL_URL}?{params}"
    log.debug("Fetching multi-model: %s", url)
    return _fetch_json(url)


def _parse_ensemble_members(data: dict, forecast_day: int) -> Tuple[List[float], List[float]]:
    """
    Parse ensemble API response into per-model member lists.

    Open-Meteo ensemble returns daily data indexed by model.
    Each model key has a 'daily' → 'temperature_2m_max' array with one value per day.
    The ensemble members are returned as separate model entries.
    """
    ecmwf = []
    gfs = []

    # The ensemble API returns a list of model results or a nested structure
    # Handle both single-location and multi-location responses
    if isinstance(data, list):
        data = data[0] if data else {}

    # Try the standard ensemble format: each model returns member arrays
    # Format: data[model_key]["daily"]["temperature_2m_max"] = [day0, day1, day2] per member
    for key in data:
        if key in ("latitude", "longitude", "generationtime_ms", "utc_offset_seconds",
                    "timezone", "timezone_abbreviation", "elevation"):
            continue

        model_data = data[key]
        if not isinstance(model_data, dict):
            continue

        daily = model_data.get("daily", {})
        tmax = daily.get("temperature_2m_max", [])

        if not tmax:
            continue

        # tmax is the array for this member across forecast days
        if forecast_day < len(tmax) and tmax[forecast_day] is not None:
            val = float(tmax[forecast_day])
            if "ecmwf" in key.lower():
                ecmwf.append(val)
            elif "gfs" in key.lower():
                gfs.append(val)

    # Alternative format: flat daily structure with member indices
    # e.g., data["daily"]["temperature_2m_max_member01"] = [day0, day1, ...]
    if not ecmwf and not gfs:
        daily = data.get("daily", {})
        for key, values in daily.items():
            if not key.startswith("temperature_2m_max"):
                continue
            if key == "temperature_2m_max_time" or key == "time":
                continue
            if not isinstance(values, list) or forecast_day >= len(values):
                continue
            val = values[forecast_day]
            if val is None:
                continue
            val = float(val)
            # Real API keys:
            #   temperature_2m_max_member01_ecmwf_ifs025_ensemble  (ECMWF)
            #   temperature_2m_max_ecmwf_ifs025_ensemble           (ECMWF control)
            #   temperature_2m_max_member01_ncep_gefs_seamless     (GFS GEFS)
            #   temperature_2m_max_ncep_gefs_seamless              (GFS control)
            kl = key.lower()
            if "ecmwf" in kl:
                ecmwf.append(val)
            elif "gfs" in kl or "gefs" in kl or "ncep" in kl:
                gfs.append(val)
            elif key == "temperature_2m_max":
                ecmwf.append(val)

    return ecmwf, gfs


def _parse_multimodel(data: dict, forecast_day: int) -> Dict[str, float]:
    """
    Parse multi-model API response into model_name → temperature dict.

    Open-Meteo multi-model returns one entry per model with daily arrays.
    """
    models = {}

    if isinstance(data, list):
        data = data[0] if data else {}

    # Multi-model API returns nested by model name
    model_names = [
        "gfs_seamless", "ecmwf_ifs025", "icon_seamless",
        "jma_seamless", "gem_seamless", "meteofrance_seamless", "ukmo_seamless"
    ]

    for model_name in model_names:
        model_data = data.get(model_name, {})
        if not isinstance(model_data, dict):
            continue
        daily = model_data.get("daily", {})
        tmax = daily.get("temperature_2m_max", [])
        if forecast_day < len(tmax) and tmax[forecast_day] is not None:
            models[model_name] = float(tmax[forecast_day])

    # Alternative flat format: data["daily"]["temperature_2m_max_gfs_seamless"] etc.
    if not models:
        daily = data.get("daily", {})
        for key, values in daily.items():
            if not key.startswith("temperature_2m_max"):
                continue
            if key == "temperature_2m_max" or key == "time":
                continue
            if not isinstance(values, list) or forecast_day >= len(values):
                continue
            val = values[forecast_day]
            if val is None:
                continue
            # Extract model name from key: temperature_2m_max_gfs_seamless → gfs_seamless
            suffix = key.replace("temperature_2m_max_", "")
            if suffix:
                models[suffix] = float(val)

    return models


def get_ensemble_forecast(
    lat: float,
    lon: float,
    city: str = "",
    timezone: str = "auto",
    forecast_day: int = 1,
) -> EnsembleForecast:
    """
    Fetch and process ensemble + multi-model forecasts for a location.

    Args:
        lat: Latitude
        lon: Longitude
        city: City name (for caching/logging)
        timezone: IANA timezone string
        forecast_day: 0=today, 1=tomorrow, 2=day-after

    Returns:
        EnsembleForecast with all member data and derived statistics.
    """
    cache_key = f"{city or f'{lat},{lon}'}_{forecast_day}"
    now = time.time()

    # Check cache
    if cache_key in _cache:
        cached_ts, cached_fc = _cache[cache_key]
        if now - cached_ts < _CACHE_TTL_S:
            log.debug("Cache hit: %s (age=%.0fs)", cache_key, now - cached_ts)
            return cached_fc

    fc = EnsembleForecast(
        city=city, lat=lat, lon=lon,
        forecast_day=forecast_day, fetch_ts=now,
    )

    # ── Fetch ensemble data ──────────────────────────────────────────────
    try:
        ens_data = _fetch_ensemble(lat, lon, timezone, forecast_days=max(3, forecast_day + 1))
        ecmwf, gfs = _parse_ensemble_members(ens_data, forecast_day)
        fc.ecmwf_members = ecmwf
        fc.gfs_members = gfs
        fc.all_members = ecmwf + gfs
        log.info("Ensemble fetched for %s day=%d: %d ECMWF + %d GFS = %d members",
                 city, forecast_day, len(ecmwf), len(gfs), len(fc.all_members))
    except Exception as exc:
        log.error("Ensemble fetch failed for %s: %s", city, exc)

    # ── Fetch multi-model data ───────────────────────────────────────────
    try:
        mm_data = _fetch_multimodel(lat, lon, timezone, forecast_days=max(3, forecast_day + 1))
        fc.model_forecasts = _parse_multimodel(mm_data, forecast_day)
        log.info("Multi-model fetched for %s day=%d: %d models — %s",
                 city, forecast_day, len(fc.model_forecasts),
                 {k: f"{v:.1f}" for k, v in fc.model_forecasts.items()})
    except Exception as exc:
        log.error("Multi-model fetch failed for %s: %s", city, exc)

    # ── Compute statistics ───────────────────────────────────────────────
    _compute_stats(fc)

    # ── Cache result ─────────────────────────────────────────────────────
    # Good/partial results get the full 30-min TTL.
    # Fallback results (e.g. 429 on startup) get a shorter 2-min TTL so
    # they retry soon without hammering the API every single cycle.
    _cache[cache_key] = (now if fc.data_quality != "fallback" else now - _CACHE_TTL_S + 120, fc)
    if fc.data_quality == "fallback":
        log.debug("Caching fallback result for %s day=%d with 2-min TTL — will retry in ~2 min",
                  city or f"{lat},{lon}", forecast_day)
    return fc


def _compute_stats(fc: EnsembleForecast):
    """Compute derived statistics from raw ensemble + model data."""

    members = fc.all_members
    models = list(fc.model_forecasts.values())

    # Ensemble stats
    if members:
        fc.n_ensemble_members = len(members)
        fc.ensemble_mean = sum(members) / len(members)

        if len(members) > 1:
            variance = sum((m - fc.ensemble_mean) ** 2 for m in members) / (len(members) - 1)
            fc.ensemble_std = math.sqrt(variance)

        sorted_m = sorted(members)
        fc.ensemble_min = sorted_m[0]
        fc.ensemble_max = sorted_m[-1]
        fc.ensemble_p10 = _percentile(sorted_m, 10)
        fc.ensemble_p25 = _percentile(sorted_m, 25)
        fc.ensemble_p50 = _percentile(sorted_m, 50)
        fc.ensemble_p75 = _percentile(sorted_m, 75)
        fc.ensemble_p90 = _percentile(sorted_m, 90)

    # Multi-model stats
    if models:
        fc.n_models = len(models)
        fc.multimodel_mean = sum(models) / len(models)
        if len(models) > 1:
            variance = sum((m - fc.multimodel_mean) ** 2 for m in models) / (len(models) - 1)
            fc.multimodel_std = math.sqrt(variance)

    # Blended sigma: use ensemble spread as primary, model spread as floor
    if fc.n_ensemble_members >= 10:
        # IQR-based sigma is more robust than std for non-Gaussian tails
        iqr = fc.ensemble_p75 - fc.ensemble_p25
        iqr_sigma = iqr / 1.35  # IQR → sigma for normal distribution
        # Use the larger of std and IQR-sigma (captures fat tails)
        # Floor at 1.5°C: ensemble spread systematically underestimates real
        # forecast uncertainty due to shared model physics. Historical day-ahead
        # high temp RMSE is 1.5-3.0°C even when ensemble spread is < 0.5°C.
        fc.blended_sigma = max(fc.ensemble_std, iqr_sigma, 1.5)
        fc.data_quality = "good"
    elif fc.n_models >= 3:
        # Fallback to multi-model spread (wider because models are independent)
        fc.blended_sigma = max(fc.multimodel_std * 1.3, 1.0)
        fc.data_quality = "partial"
    else:
        fc.blended_sigma = 2.5  # conservative fallback
        fc.data_quality = "fallback"

    log.info(
        "Stats for %s day=%d: ens_mean=%.1f°C ens_std=%.2f°C "
        "mm_mean=%.1f°C mm_std=%.2f°C blended_sigma=%.2f°C quality=%s",
        fc.city, fc.forecast_day,
        fc.ensemble_mean, fc.ensemble_std,
        fc.multimodel_mean, fc.multimodel_std,
        fc.blended_sigma, fc.data_quality,
    )


# ── Bin probability computation ──────────────────────────────────────────────

def ensemble_bin_probability(
    fc: EnsembleForecast,
    threshold_c: float,
    direction: str = "exact",
    bin_width_c: float = 0.5,
    bias_correction_c: float = 0.0,
) -> float:
    """
    Compute the probability for a Polymarket temperature bin using ensemble data.

    This is the core improvement: instead of a single forecast + guessed sigma
    through a Gaussian CDF, we COUNT how many ensemble members actually fall
    in the bin. For bins where the count is 0 or very low, we blend with a
    kernel-smoothed estimate to avoid zero-probability predictions.

    Args:
        fc: EnsembleForecast with member data.
        threshold_c: Bin center temperature in °C.
        direction: "exact" (bin centered at threshold), "above", or "below".
        bin_width_c: Half-width of the bin in °C (default 0.5 = ±0.5°C).
        bias_correction_c: Per-city bias to subtract from members (positive = model runs hot).

    Returns:
        Probability as a fraction (0.0 to 1.0).
    """
    members = fc.all_members

    # Apply bias correction to members
    if bias_correction_c != 0.0:
        members = [m - bias_correction_c for m in members]

    if not members:
        # Fall back to Gaussian with blended sigma
        return _gaussian_bin_prob(
            fc.ensemble_mean or fc.multimodel_mean or 20.0,
            fc.blended_sigma,
            threshold_c, direction, bin_width_c,
        )

    n = len(members)

    # ── Direct count from ensemble members ───────────────────────────────
    if direction == "exact":
        lo = threshold_c - bin_width_c
        hi = threshold_c + bin_width_c
        count = sum(1 for m in members if lo <= m < hi)
    elif direction == "above":
        count = sum(1 for m in members if m >= threshold_c)
    else:  # below
        count = sum(1 for m in members if m < threshold_c)

    raw_prob = count / n

    # ── Kernel-smoothed estimate (handles sparse bins) ───────────────────
    # Use a Gaussian kernel around each member to smooth the distribution
    # Bandwidth = ensemble_std / n^(1/5) (Silverman's rule)
    bandwidth = fc.blended_sigma * (n ** -0.2) if fc.blended_sigma > 0 else 0.5

    kernel_prob = 0.0
    for m in members:
        if direction == "exact":
            # Probability this member contributes to the bin [lo, hi)
            z_hi = (threshold_c + bin_width_c - m) / bandwidth
            z_lo = (threshold_c - bin_width_c - m) / bandwidth
            kernel_prob += (_ncdf(z_hi) - _ncdf(z_lo))
        elif direction == "above":
            z = (m - threshold_c) / bandwidth
            kernel_prob += _ncdf(z)
        else:
            z = (threshold_c - m) / bandwidth
            kernel_prob += _ncdf(z)

    kernel_prob /= n

    # ── Blend: weight direct count more when we have lots of members ─────
    # With 82 members: ~75% direct count, ~25% kernel smooth
    # With 20 members: ~55% direct count, ~45% kernel smooth
    direct_weight = min(0.85, 0.5 + n / 200.0)
    blended = direct_weight * raw_prob + (1 - direct_weight) * kernel_prob

    # ── Multi-model cross-validation ─────────────────────────────────────
    # If we have deterministic models, check agreement and widen uncertainty
    # if models disagree with the ensemble
    if fc.n_models >= 3 and fc.multimodel_std > 0:
        model_vals = [v - bias_correction_c for v in fc.model_forecasts.values()]
        mm_prob = _gaussian_bin_prob(
            fc.multimodel_mean - bias_correction_c,
            fc.multimodel_std,
            threshold_c, direction, bin_width_c,
        )

        # If multi-model and ensemble disagree significantly, hedge toward
        # the wider (more uncertain) estimate
        disagreement = abs(fc.ensemble_mean - fc.multimodel_mean) / max(fc.blended_sigma, 0.5)
        if disagreement > 1.0:
            # Models disagree by >1 sigma — increase uncertainty
            hedge_weight = min(0.3, disagreement * 0.1)
            blended = blended * (1 - hedge_weight) + mm_prob * hedge_weight
            log.debug(
                "Model disagreement for %s: %.1f sigma, hedging %.0f%% toward multi-model",
                fc.city, disagreement, hedge_weight * 100,
            )

    # ── KDE tail clamp: don't assign probability outside member range + buffer ──
    # Prevents KDE from leaking mass into physically impossible territory
    if members and direction == "exact":
        _member_min = min(members)
        _member_max = max(members)
        _buffer = max(1.0, fc.blended_sigma * 0.5)  # buffer = half sigma, min 1°C
        if threshold_c > _member_max + _buffer or threshold_c < _member_min - _buffer:
            # Bin is outside all members + buffer → clamp to near-zero
            blended = min(blended, 0.005)
            log.debug("KDE_CLAMP: %s threshold=%.1f outside members [%.1f, %.1f]+/-%.1f → clamped to %.3f",
                       fc.city, threshold_c, _member_min, _member_max, _buffer, blended)

    # ── CALIBRATION GUARD: ensemble clustering ≠ forecast certainty ──────
    # NWP ensembles share model physics → correlated errors → ensemble spread
    # systematically underestimates real uncertainty. Historical RMSE for
    # day-ahead high temps is 1.5-3.0°C, but ensemble spread can be < 0.5°C.
    #
    # Fix: blend ensemble probability with a Gaussian estimate using the
    # verified RMSE-based sigma (minimum 1.5°C for exact bins). This prevents
    # the model from claiming >50% on any single 1°C bin.
    if direction == "exact" and blended > 0.30:
        # STRATEGY_REWRITE §1.2 + §3.2: median per-station RMSE is 1.60°C, so a
        # 1.5°C floor was structurally over-confident. Raise to 2.5°C.
        _verified_sigma = max(fc.blended_sigma, 2.5)
        _mean = sum(members) / len(members) if members else (fc.ensemble_mean or 20.0)
        _conservative_prob = _gaussian_bin_prob(
            _mean, _verified_sigma, threshold_c, direction, bin_width_c,
        )
        # Blend: cap at 60/40 ensemble/conservative when ensemble is very confident
        # This still trusts the ensemble but prevents extreme overconfidence
        _ensemble_overconfidence = max(0.0, (blended - 0.30) / 0.70)  # 0→1 as blended goes 30%→100%
        _conservative_weight = min(0.50, _ensemble_overconfidence * 0.50)
        _old_blended = blended
        blended = blended * (1 - _conservative_weight) + _conservative_prob * _conservative_weight
        log.info(
            "CALIBRATION_GUARD: %s bin=%.1f%s ens_prob=%.1f%% → blended=%.1f%% "
            "(conservative=%.1f%%, weight=%.0f%%, verified_sigma=%.1f)",
            fc.city, threshold_c, "°C", _old_blended * 100, blended * 100,
            _conservative_prob * 100, _conservative_weight * 100, _verified_sigma,
        )

    # Hard cap: no single exact bin should exceed 45% probability.
    # Even the best day-ahead forecasts can't be >45% certain about a 1°C bin
    # when the temperature distribution has 7+ possible outcomes.
    if direction == "exact":
        # STRATEGY_REWRITE §3.2: lowered hard cap from 0.45 → 0.35.
        blended = min(blended, 0.35)

    # Clamp to reasonable range
    return max(0.001, min(0.999, blended))


def _ncdf(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _gaussian_bin_prob(
    mean: float,
    sigma: float,
    threshold_c: float,
    direction: str,
    bin_width_c: float,
) -> float:
    """Gaussian fallback for when ensemble data is unavailable."""
    if sigma <= 0:
        sigma = 2.5

    if direction == "exact":
        z_hi = (threshold_c + bin_width_c - mean) / sigma
        z_lo = (threshold_c - bin_width_c - mean) / sigma
        return _ncdf(z_hi) - _ncdf(z_lo)
    elif direction == "above":
        z = (mean - threshold_c) / sigma
        return _ncdf(z)
    else:
        z = (threshold_c - mean) / sigma
        return _ncdf(z)


# ── Convenience: compute all bin probabilities for a market ──────────────────

def compute_bin_probabilities(
    fc: EnsembleForecast,
    bins_c: List[Tuple[str, float]],
    direction: str = "exact",
    bin_width_c: float = 0.5,
    bias_correction_c: float = 0.0,
) -> Dict[str, float]:
    """
    Compute probabilities for all bins in a market.

    Args:
        fc: EnsembleForecast
        bins_c: List of (bin_label, threshold_celsius) tuples
        direction: "exact", "above", or "below"
        bin_width_c: Half-width of bins
        bias_correction_c: Per-city bias correction

    Returns:
        Dict of bin_label → probability (0-1)
    """
    probs = {}
    for label, threshold in bins_c:
        p = ensemble_bin_probability(
            fc, threshold, direction, bin_width_c, bias_correction_c,
        )
        probs[label] = p

    # Normalize exact bins to sum to ~1.0 (they represent a partition)
    if direction == "exact" and probs:
        total = sum(probs.values())
        if total > 0 and abs(total - 1.0) > 0.01:
            for label in probs:
                probs[label] /= total

    return probs


# ── Integration helper for api_server.py ─────────────────────────────────────

def get_ensemble_probability(
    city: str,
    lat: float,
    lon: float,
    threshold_c: float,
    direction: str,
    is_tomorrow: bool,
    timezone: str = "auto",
    bias_correction_c: float = 0.0,
) -> Tuple[float, EnsembleForecast]:
    """
    Drop-in replacement for the static sigma Gaussian computation in api_server.py.

    Returns (probability_0_to_1, forecast_object).

    Args:
        city: City name
        lat, lon: Coordinates
        threshold_c: Bin threshold in Celsius
        direction: "exact", "above", "below"
        is_tomorrow: True for tomorrow's market, False for today
        timezone: IANA timezone
        bias_correction_c: Bias correction (positive = model hot, subtract)

    Returns:
        (probability, EnsembleForecast) — probability is 0.0-1.0
    """
    forecast_day = 1 if is_tomorrow else 0

    fc = get_ensemble_forecast(
        lat=lat, lon=lon, city=city,
        timezone=timezone, forecast_day=forecast_day,
    )

    prob = ensemble_bin_probability(
        fc=fc,
        threshold_c=threshold_c,
        direction=direction,
        bin_width_c=0.5,
        bias_correction_c=bias_correction_c,
    )

    return prob, fc


__all__ = [
    "EnsembleForecast",
    "get_ensemble_forecast",
    "ensemble_bin_probability",
    "compute_bin_probabilities",
    "get_ensemble_probability",
]
