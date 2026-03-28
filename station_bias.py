"""
station_bias.py — Station Bias Tracker

Tracks systematic temperature bias per weather station by comparing
our forecasts and market-implied temperatures against actual resolution
outcomes. This gives us a correction factor: if KATL consistently resolves
2°F above forecast, we shift our probability distribution accordingly.

Publishes bias data to RufloSharedState so all agents (especially
BinSniper and GFSRefresh) can apply bias-corrected forecasts.

Storage: SQLite table in the shared ledger DB for persistence across deploys.
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Use same DB as trade_ledger for persistence
_default_db = "/data/station_bias.db" if os.path.isdir("/data") else "station_bias.db"
DB_PATH = os.environ.get("BIAS_DB", _default_db)

_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        log.info("Station bias DB: %s", DB_PATH)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_tables(_conn)
    return _conn


def _init_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bias_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            station TEXT NOT NULL,
            city TEXT NOT NULL,
            date TEXT NOT NULL,
            forecast_temp_f REAL,
            market_implied_f REAL,
            resolved_temp_f REAL,
            forecast_error_f REAL,
            market_error_f REAL,
            bin_question TEXT,
            bin_outcome TEXT,
            our_prob REAL,
            market_price REAL,
            meta TEXT
        );

        CREATE TABLE IF NOT EXISTS station_bias_summary (
            station TEXT PRIMARY KEY,
            city TEXT,
            n_observations INTEGER DEFAULT 0,
            mean_forecast_error_f REAL DEFAULT 0,
            mean_market_error_f REAL DEFAULT 0,
            median_forecast_error_f REAL DEFAULT 0,
            std_forecast_error_f REAL DEFAULT 0,
            warm_bias_pct REAL DEFAULT 0,
            cold_bias_pct REAL DEFAULT 0,
            last_updated TEXT,
            recent_errors TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_bias_station ON bias_observations(station);
        CREATE INDEX IF NOT EXISTS idx_bias_date ON bias_observations(date);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Recording observations
# ---------------------------------------------------------------------------

def record_observation(
    station: str,
    city: str,
    date: str,
    forecast_temp_f: float,
    resolved_temp_f: float,
    market_implied_f: Optional[float] = None,
    bin_question: str = "",
    bin_outcome: str = "",
    our_prob: float = 0,
    market_price: float = 0,
    meta: Optional[dict] = None,
):
    """Record a single forecast-vs-resolution observation for bias tracking.

    Args:
        station: ICAO station ID (e.g., 'KATL')
        city: City name
        date: Date string (YYYY-MM-DD)
        forecast_temp_f: Our forecasted temperature in °F
        resolved_temp_f: Actual resolved temperature in °F
        market_implied_f: Market's implied most-likely temperature in °F
        bin_question: The Polymarket question text
        bin_outcome: YES or NO
        our_prob: Our probability estimate (0-100)
        market_price: Market price at time of prediction (0-100)
        meta: Additional metadata dict
    """
    conn = _get_conn()
    forecast_error = resolved_temp_f - forecast_temp_f
    market_error = (resolved_temp_f - market_implied_f) if market_implied_f is not None else None

    conn.execute("""
        INSERT INTO bias_observations
            (ts, station, city, date, forecast_temp_f, market_implied_f,
             resolved_temp_f, forecast_error_f, market_error_f,
             bin_question, bin_outcome, our_prob, market_price, meta)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        station, city, date,
        forecast_temp_f, market_implied_f, resolved_temp_f,
        forecast_error, market_error,
        bin_question, bin_outcome, our_prob, market_price,
        json.dumps(meta or {}),
    ))
    conn.commit()
    log.info("BIAS: recorded %s/%s forecast=%.1f°F actual=%.1f°F error=%+.1f°F",
             station, city, forecast_temp_f, resolved_temp_f, forecast_error)
    _update_summary(station, city)


def _update_summary(station: str, city: str):
    """Recompute bias summary stats for a station from all observations."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT forecast_error_f FROM bias_observations WHERE station = ? ORDER BY date DESC LIMIT 100",
        (station,)
    ).fetchall()

    if not rows:
        return

    errors = [r['forecast_error_f'] for r in rows if r['forecast_error_f'] is not None]
    if not errors:
        return

    n = len(errors)
    mean_err = sum(errors) / n
    sorted_errors = sorted(errors)
    median_err = sorted_errors[n // 2] if n % 2 == 1 else (sorted_errors[n // 2 - 1] + sorted_errors[n // 2]) / 2
    variance = sum((e - mean_err) ** 2 for e in errors) / max(n - 1, 1)
    std_err = variance ** 0.5
    warm_bias_pct = round(sum(1 for e in errors if e > 0.5) / n * 100, 1)
    cold_bias_pct = round(sum(1 for e in errors if e < -0.5) / n * 100, 1)

    # Also compute market error stats
    mkt_rows = conn.execute(
        "SELECT market_error_f FROM bias_observations WHERE station = ? AND market_error_f IS NOT NULL ORDER BY date DESC LIMIT 100",
        (station,)
    ).fetchall()
    mean_mkt_err = 0.0
    if mkt_rows:
        mkt_errors = [r['market_error_f'] for r in mkt_rows]
        mean_mkt_err = sum(mkt_errors) / len(mkt_errors)

    # Store recent errors for quick access (last 20)
    recent = errors[:20]

    conn.execute("""
        INSERT OR REPLACE INTO station_bias_summary
            (station, city, n_observations, mean_forecast_error_f, mean_market_error_f,
             median_forecast_error_f, std_forecast_error_f, warm_bias_pct, cold_bias_pct,
             last_updated, recent_errors)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        station, city, n,
        round(mean_err, 2), round(mean_mkt_err, 2),
        round(median_err, 2), round(std_err, 2),
        warm_bias_pct, cold_bias_pct,
        datetime.now(timezone.utc).isoformat(),
        json.dumps(recent),
    ))
    conn.commit()


# ---------------------------------------------------------------------------
# Querying bias data
# ---------------------------------------------------------------------------

def get_station_bias(station: str) -> Dict:
    """Get bias summary for a single station.

    Returns dict with:
        - mean_forecast_error_f: average error (positive = station resolves warmer than forecast)
        - median_forecast_error_f: median error
        - std_forecast_error_f: standard deviation of errors
        - warm_bias_pct: % of times station resolved warmer than forecast
        - cold_bias_pct: % of times station resolved colder than forecast
        - n_observations: number of tracked resolutions
        - correction_f: recommended temperature correction to apply
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM station_bias_summary WHERE station = ?", (station,)
    ).fetchone()

    if not row:
        return {
            'station': station, 'known': False, 'n_observations': 0,
            'correction_f': 0.0, 'confidence': 'none',
        }

    n = row['n_observations']
    mean_err = row['mean_forecast_error_f']

    # Correction = the bias we should add to our forecast
    # If station resolves 2°F warmer on average, correction = +2.0
    # Only apply correction if we have enough data and it's statistically significant
    correction = 0.0
    confidence = 'none'
    if n >= 5:
        confidence = 'low'
        if abs(mean_err) > row['std_forecast_error_f'] * 0.5:
            # Bias is at least half a std dev — meaningful
            correction = round(mean_err, 1)
    if n >= 15:
        confidence = 'medium'
        correction = round(mean_err, 1)
    if n >= 30:
        confidence = 'high'
        correction = round(mean_err, 1)

    return {
        'station': station,
        'city': row['city'],
        'known': True,
        'n_observations': n,
        'mean_forecast_error_f': mean_err,
        'median_forecast_error_f': row['median_forecast_error_f'],
        'std_forecast_error_f': row['std_forecast_error_f'],
        'mean_market_error_f': row['mean_market_error_f'],
        'warm_bias_pct': row['warm_bias_pct'],
        'cold_bias_pct': row['cold_bias_pct'],
        'correction_f': correction,
        'confidence': confidence,
        'last_updated': row['last_updated'],
    }


def get_all_biases() -> Dict[str, Dict]:
    """Get bias summaries for all tracked stations."""
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM station_bias_summary ORDER BY n_observations DESC").fetchall()
    result = {}
    for row in rows:
        station = row['station']
        bias = get_station_bias(station)
        result[station] = bias
    return result


def get_bias_correction(station: str) -> float:
    """Quick helper: get the temperature correction in °F for a station.

    Returns 0.0 if we don't have enough data.
    Positive means station tends to resolve warmer than forecasts.
    Negative means station tends to resolve colder than forecasts.
    """
    bias = get_station_bias(station)
    return bias.get('correction_f', 0.0)


def apply_bias_to_probability(
    station: str,
    bin_lo_f: float,
    bin_hi_f: float,
    base_probability: float,
    forecast_temp_f: float,
) -> Tuple[float, str]:
    """Apply station bias correction to a bin probability estimate.

    If a station consistently resolves 2°F warmer, a bin at 72-73°F
    with a forecast of 71°F is actually more likely than the raw forecast
    suggests, because the corrected forecast is 73°F.

    Args:
        station: ICAO station ID
        bin_lo_f: Lower bound of temperature bin (°F)
        bin_hi_f: Upper bound of temperature bin (°F)
        base_probability: Our raw model probability (0-100)
        forecast_temp_f: Our raw forecast temperature (°F)

    Returns:
        (adjusted_probability, explanation_string)
    """
    bias = get_station_bias(station)
    correction = bias.get('correction_f', 0.0)
    confidence = bias.get('confidence', 'none')

    if correction == 0.0 or confidence == 'none':
        return base_probability, f"no_bias_data({station})"

    # Corrected forecast
    corrected_temp = forecast_temp_f + correction
    bin_center = (bin_lo_f + bin_hi_f) / 2.0
    bin_width = bin_hi_f - bin_lo_f

    # How much closer/further is the corrected forecast to this bin?
    raw_distance = abs(forecast_temp_f - bin_center)
    corrected_distance = abs(corrected_temp - bin_center)

    # Probability adjustment based on how much the bias moves us toward/away from the bin
    # If corrected forecast is closer to bin center, increase probability
    # Scale: each °F closer ≈ +5-15% relative probability change depending on bin width
    distance_change = raw_distance - corrected_distance  # positive = got closer
    if bin_width > 0:
        # Normalize by bin width: narrower bins are more sensitive
        relative_shift = distance_change / max(bin_width, 1.0)
        # Cap adjustment at ±30% of base probability
        adjustment_pct = min(30, max(-30, relative_shift * 25))
        adjusted = base_probability * (1 + adjustment_pct / 100)
    else:
        adjusted = base_probability

    # Clamp to valid range
    adjusted = max(0.1, min(99.9, adjusted))

    # Scale confidence: reduce adjustment if we have few observations
    if confidence == 'low':
        adjusted = base_probability + (adjusted - base_probability) * 0.3
    elif confidence == 'medium':
        adjusted = base_probability + (adjusted - base_probability) * 0.7
    # 'high' = full adjustment

    adjusted = round(adjusted, 2)
    explanation = (
        f"bias_correction({station}): correction={correction:+.1f}°F "
        f"n={bias['n_observations']} conf={confidence} "
        f"raw_prob={base_probability:.1f}% adj_prob={adjusted:.1f}%"
    )
    return adjusted, explanation


# ---------------------------------------------------------------------------
# Auto-learning from resolved markets
# ---------------------------------------------------------------------------

def learn_from_resolution(
    station: str,
    city: str,
    date: str,
    bin_question: str,
    bin_lo_f: float,
    bin_hi_f: float,
    outcome: str,
    our_prob: float,
    market_price: float,
    forecast_temp_f: Optional[float] = None,
    sentinel_temp_f: Optional[float] = None,
):
    """Called when a market resolves. Records the observation for bias learning.

    For YES resolutions: the actual temp was inside the bin.
    For NO resolutions: the actual temp was outside the bin.

    We use the bin boundaries + outcome to estimate what the actual temp was,
    then compare against our forecast and sentinel reading.
    """
    if outcome.upper() == 'YES':
        # Actual temp was inside the bin — estimate at center
        resolved_temp_f = (bin_lo_f + bin_hi_f) / 2.0
    else:
        # Actual temp was outside the bin — less useful for exact bias,
        # but we still know our forecast was wrong if we predicted YES
        # Skip recording exact bias for NO outcomes (ambiguous direction)
        if our_prob > 50:
            # We predicted YES but it resolved NO — our forecast was off
            # Use sentinel last reading as a rough proxy if available
            if sentinel_temp_f is not None:
                resolved_temp_f = sentinel_temp_f
            else:
                return  # Can't determine actual temp from NO resolution alone
        else:
            return  # We correctly predicted NO, no bias to learn from here

    # Use our forecast temp, or estimate from bin if not available
    if forecast_temp_f is None:
        # If we don't have a direct forecast temp, we can't compute meaningful bias
        return

    # Market implied temp: the bin with highest market price was the market's best guess
    # For individual bins, we approximate the market's implied temp from the price
    market_implied_f = None
    if market_price > 0:
        # If market priced this bin at 60%, it roughly implies the temp is near this bin
        # Weight the bin center by market price as a rough proxy
        market_implied_f = (bin_lo_f + bin_hi_f) / 2.0 if market_price > 30 else None

    record_observation(
        station=station,
        city=city,
        date=date,
        forecast_temp_f=forecast_temp_f,
        resolved_temp_f=resolved_temp_f,
        market_implied_f=market_implied_f,
        bin_question=bin_question,
        bin_outcome=outcome,
        our_prob=our_prob,
        market_price=market_price,
        meta={
            'sentinel_temp_f': sentinel_temp_f,
            'bin_lo_f': bin_lo_f,
            'bin_hi_f': bin_hi_f,
        }
    )


# ---------------------------------------------------------------------------
# SharedState integration
# ---------------------------------------------------------------------------

def publish_to_shared_state(shared_state):
    """Publish all bias data to the RufloSharedState bus so all agents can use it."""
    biases = get_all_biases()
    shared_state.publish('station_bias', 'all_biases', biases)

    # Highlight stations with significant bias
    significant = {
        sid: b for sid, b in biases.items()
        if abs(b.get('correction_f', 0)) >= 1.0 and b.get('confidence') in ('medium', 'high')
    }
    shared_state.publish('station_bias', 'significant_biases', significant)

    # Publish simple correction lookup
    corrections = {sid: b.get('correction_f', 0.0) for sid, b in biases.items()}
    shared_state.publish('station_bias', 'corrections', corrections)

    if significant:
        for sid, b in significant.items():
            shared_state.add_strategy_insight(
                'station_bias',
                f"{sid}/{b.get('city','?')}: systematic {b['correction_f']:+.1f}°F bias "
                f"(n={b['n_observations']}, conf={b['confidence']})"
            )

    log.info("BIAS: published %d station biases (%d significant) to shared state",
             len(biases), len(significant))
