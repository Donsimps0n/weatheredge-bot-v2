"""
ledger_telemetry.py

SQLite ledger and telemetry module for Polymarket temperature trading bot.

Handles:
  - Bullet #2: Frozen snapshots
  - Bullet #8: Edge decay metrics
  - Bullet #11: Alerts and logging
  - Bullet #17: No silent fallbacks
  - Bullet #20: Reporting and sanity checks

Provides CRUD operations and query utilities with no global state.
All database connections created per-call or via context manager.
"""

import sqlite3
import json
import math
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple, Any


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class Alert:
    """Alert notification."""
    alert_type: str
    alert_value: float
    threshold: float
    trade_group_id: Optional[str] = None
    market_slug: Optional[str] = None
    details: str = ""


@dataclass
class DecayMetrics:
    """Edge decay metrics for a trade group."""
    notional_weighted_adverse_move_pct: float
    notional_weighted_spread_paid_pct: float
    time_to_first_fill_s: Optional[float]
    fill_completion_ratio_60s: Optional[float]
    rolling_leakage_bps: Optional[float]


# ============================================================================
# Ledger Class
# ============================================================================

class Ledger:
    """SQLite ledger and telemetry manager for temperature trading bot."""

    def __init__(self, db_path: str):
        """
        Initialize ledger with database path.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = db_path

    def _get_connection(self) -> sqlite3.Connection:
        """Create and return a database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        """Create all tables in the database."""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            # trade_groups table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_groups (
                    trade_group_id TEXT PRIMARY KEY,
                    market_slug TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    station_icao TEXT,
                    station_confidence INTEGER,
                    regime_detected TEXT,
                    regime_features TEXT,
                    diurnal_stage TEXT,
                    peak_window_start INTEGER,
                    peak_window_end INTEGER,
                    theoretical_full_ev REAL,
                    cost_proxy REAL,
                    min_theo_ev_applied REAL,
                    obs_temp_at_entry REAL,
                    obs_max_at_entry REAL,
                    time_to_resolution_h REAL,
                    signal_age_minutes REAL,
                    is_burst_override INTEGER DEFAULT 0,
                    burst_context TEXT,
                    flatten_trigger TEXT,
                    outcome TEXT,
                    pnl REAL,
                    fees_total REAL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                )
            """)

            # legs table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS legs (
                    leg_id TEXT PRIMARY KEY,
                    trade_group_id TEXT NOT NULL REFERENCES trade_groups(trade_group_id),
                    token_id TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    bin_label TEXT NOT NULL,
                    side TEXT NOT NULL DEFAULT 'YES',
                    entry_price REAL NOT NULL,
                    size REAL NOT NULL,
                    true_prob REAL NOT NULL,
                    u_prob REAL NOT NULL,
                    fill_prob_proxy REAL,
                    depth_at_entry REAL,
                    mid_price_at_decision REAL,
                    mid_price_at_fill_time REAL,
                    fill_price REAL,
                    best_bid_at_fill REAL,
                    best_ask_at_fill REAL,
                    time_in_book_s REAL,
                    reprice_count INTEGER DEFAULT 0,
                    fill_type TEXT,
                    fees_enabled INTEGER,
                    fee_rate_bps_used INTEGER,
                    realized_fees_paid REAL DEFAULT 0,
                    adverse_move_pct REAL,
                    spread_paid_pct REAL,
                    decay_bucket TEXT,
                    timestamp_utc TEXT NOT NULL
                )
            """)

            # frozen_snapshots table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS frozen_snapshots (
                    trade_group_id TEXT PRIMARY KEY REFERENCES trade_groups(trade_group_id),
                    true_prob_vector TEXT NOT NULL,
                    u_prob_vector TEXT NOT NULL,
                    bin_labels TEXT NOT NULL,
                    model_run_timestamp TEXT NOT NULL,
                    forecast_inputs_hash TEXT NOT NULL,
                    regime_detected TEXT,
                    regime_features TEXT,
                    obs_temp_at_freeze REAL,
                    obs_max_at_freeze REAL,
                    diurnal_stage TEXT,
                    peak_window_start INTEGER,
                    peak_window_end INTEGER,
                    station_icao TEXT,
                    station_confidence INTEGER,
                    theoretical_full_ev REAL,
                    cost_proxy REAL,
                    min_theo_ev_applied REAL,
                    frozen_at TEXT NOT NULL
                )
            """)

            # observations table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station_icao TEXT NOT NULL,
                    obs_timestamp TEXT NOT NULL,
                    temp_c REAL NOT NULL,
                    temp_f REAL NOT NULL,
                    source TEXT NOT NULL,
                    obs_anomaly_flag INTEGER DEFAULT 0,
                    sigma_widened INTEGER DEFAULT 0,
                    obs_weight_reduced INTEGER DEFAULT 0,
                    recorded_at TEXT NOT NULL
                )
            """)

            # book_snapshots table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS book_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id TEXT NOT NULL,
                    timestamp_utc TEXT NOT NULL,
                    best_bid REAL,
                    best_ask REAL,
                    mid_price REAL,
                    spread REAL,
                    relative_spread REAL,
                    bid_depth_top3 REAL,
                    ask_depth_top3 REAL,
                    total_bid_depth REAL,
                    total_ask_depth REAL,
                    recent_fill_rate REAL,
                    levels TEXT
                )
            """)

            # no_trade_log table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS no_trade_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_slug TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    is_burst INTEGER DEFAULT 0,
                    station_confidence INTEGER,
                    theo_ev REAL,
                    min_theo_ev REAL,
                    details TEXT,
                    timestamp_utc TEXT NOT NULL
                )
            """)

            # fallback_log table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS fallback_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fallback_type TEXT NOT NULL,
                    source_method TEXT NOT NULL,
                    target_method TEXT NOT NULL,
                    market_id TEXT,
                    reason TEXT,
                    timestamp_utc TEXT NOT NULL
                )
            """)

            # alert_log table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS alert_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_type TEXT NOT NULL,
                    alert_value REAL,
                    threshold REAL,
                    trade_group_id TEXT,
                    market_slug TEXT,
                    details TEXT,
                    timestamp_utc TEXT NOT NULL
                )
            """)

            # fee_log table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS fee_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    leg_id TEXT REFERENCES legs(leg_id),
                    fees_enabled INTEGER,
                    fee_rate_bps_used INTEGER,
                    realized_fees_paid REAL,
                    timestamp_utc TEXT NOT NULL
                )
            """)

            # rebate_log table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rebate_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    address TEXT NOT NULL,
                    rebate_amount REAL,
                    check_timestamp TEXT NOT NULL
                )
            """)

            # sanity_checks table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sanity_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station_icao TEXT NOT NULL,
                    wu_metar_avg_diff_c REAL,
                    risk_level TEXT,
                    min_theo_ev_boost REAL,
                    skip_market INTEGER DEFAULT 0,
                    check_date TEXT NOT NULL
                )
            """)

            # daily_reports table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_date TEXT NOT NULL,
                    sharpe_annualized REAL,
                    win_rate_group_level REAL,
                    total_trade_groups INTEGER,
                    winning_groups INTEGER,
                    total_pnl REAL,
                    total_fees REAL,
                    details TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            # cross_market_log table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cross_market_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_group_id TEXT,
                    target_market TEXT,
                    peer_markets TEXT,
                    delta_z_score REAL,
                    season_corr REAL,
                    min_theo_ev_adjustment REAL,
                    flag_raised INTEGER DEFAULT 0,
                    timestamp_utc TEXT NOT NULL
                )
            """)

            conn.commit()
        finally:
            conn.close()

    # ========================================================================
    # Bullet #2: Frozen Snapshots
    # ========================================================================

    def freeze_snapshot(
        self,
        trade_group_id: str,
        true_prob_vec: List[float],
        u_prob_vec: List[float],
        bin_labels: List[str],
        model_run_ts: str,
        forecast_hash: str,
        regime: Optional[str],
        regime_features: Optional[str],
        obs_temp: Optional[float],
        obs_max: Optional[float],
        diurnal_stage: Optional[str],
        peak_start: Optional[int],
        peak_end: Optional[int],
        station_icao: Optional[str],
        station_confidence: Optional[int],
        theo_ev: Optional[float],
        cost_proxy: Optional[float],
        min_ev: Optional[float],
    ) -> None:
        """
        Insert frozen snapshot. NEVER UPDATE existing rows.

        Args:
            trade_group_id: Unique trade group identifier.
            true_prob_vec: List of true probabilities.
            u_prob_vec: List of u-probabilities.
            bin_labels: List of bin labels.
            model_run_ts: Model run timestamp.
            forecast_hash: Hash of forecast inputs.
            regime: Detected regime.
            regime_features: Regime features JSON string.
            obs_temp: Observed temperature at freeze.
            obs_max: Observed max temperature at freeze.
            diurnal_stage: Diurnal stage label.
            peak_start: Peak window start (minutes from midnight).
            peak_end: Peak window end (minutes from midnight).
            station_icao: Station ICAO code.
            station_confidence: Station confidence score.
            theo_ev: Theoretical full EV.
            cost_proxy: Cost proxy value.
            min_ev: Minimum theoretical EV applied.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            frozen_at = datetime.now(timezone.utc).isoformat()

            cursor.execute("""
                INSERT INTO frozen_snapshots (
                    trade_group_id,
                    true_prob_vector,
                    u_prob_vector,
                    bin_labels,
                    model_run_timestamp,
                    forecast_inputs_hash,
                    regime_detected,
                    regime_features,
                    obs_temp_at_freeze,
                    obs_max_at_freeze,
                    diurnal_stage,
                    peak_window_start,
                    peak_window_end,
                    station_icao,
                    station_confidence,
                    theoretical_full_ev,
                    cost_proxy,
                    min_theo_ev_applied,
                    frozen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_group_id,
                json.dumps(true_prob_vec),
                json.dumps(u_prob_vec),
                json.dumps(bin_labels),
                model_run_ts,
                forecast_hash,
                regime,
                regime_features,
                obs_temp,
                obs_max,
                diurnal_stage,
                peak_start,
                peak_end,
                station_icao,
                station_confidence,
                theo_ev,
                cost_proxy,
                min_ev,
                frozen_at,
            ))

            conn.commit()
        finally:
            conn.close()

    def load_frozen_snapshot(self, trade_group_id: str) -> Optional[Dict[str, Any]]:
        """
        Load frozen snapshot for a tradeade group.

        Args:
            trade_group_id: Unique trade group identifier.

        Returns:
            Dictionary with parsed JSON fields, or None if not found.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM frozen_snapshots WHERE trade_group_id = ?",
                (trade_group_id,),
            )
            row = cursor.fetchone()

            if row is None:
                return None

            result = dict(row)
            result["true_prob_vector"] = json.loads(result["true_prob_vector"])
            result["u_prob_vector"] = json.loads(result["u_prob_vector"])
            result["bin_labels"] = json.loads(result["bin_labels"])

            return result
        finally:
            conn.close()

    # ========================================================================
    # Bullet #8: Edge Decay
    # ========================================================================

    def record_edge_decay(
        self,
        leg_id: str,
        mid_at_decision: float,
        mid_at_fill: float,
        fill_price: float,
        best_bid: float,
        best_ask: float,
    ) -> None:
        """
        Record edge decay metrics for a leg.

        Computes:
          - adverse_move_pct = (mid_at_fill - mid_at_decision) / mid_at_decision
          - spread_paid_pct = (fill_price - mid_at_fill) / mid_at_fill

        Args:
            leg_id: Unique leg identifier.
            mid_at_decision: Mid price at decision time.
            mid_at_fill: Mid price at fill time.
            fill_price: Actual fill price.
            best_bid: Best bid at fill time.
            best_ask: Best ask at fill time.
        """
        # Guard against division by zero
        if mid_at_decision == 0 or mid_at_fill == 0:
            adverse_move_pct = None
            spread_paid_pct = None
        else:
            adverse_move_pct = (mid_at_fill - mid_at_decision) / mid_at_decision
            spread_paid_pct = (fill_price - mid_at_fill) / mid_at_fill

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE legs SET
                    mid_price_at_decision = ?,
                    mid_price_at_fill_time = ?,
                    fill_price = ?,
                    best_bid_at_fill = ?,
                    best_ask_at_fill = ?,
                    adverse_move_pct = ?,
                    spread_paid_pct = ?
                WHERE leg_id = ?
            """, (
                mid_at_decision,
                mid_at_fill,
                fill_price,
                best_bid,
                best_ask,
                adverse_move_pct,
                spread_paid_pct,
                leg_id,
            ))

            conn.commit()
        finally:
            conn.close()

    def compute_decay_metrics(self, trade_group_id: str) -> Dict[str, Any]:
        """
        Compute notional-weighted decay metrics for a trade group.

        Returns:
            Dictionary with weighted adverse_move_pct and spread_paid_pct.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT size, adverse_move_pct, spread_paid_pct, time_in_book_s, fill_type
                FROM legs
                WHERE trade_group_id = ?
                ORDER BY timestamp_utc
            """, (trade_group_id,))

            rows = cursor.fetchall()

            if not rows:
                return {
                    "notional_weighted_adverse_move_pct": None,
                    "notional_weighted_spread_paid_pct": None,
                    "time_to_first_fill_s": None,
                    "fill_completion_ratio_60s": None,
                    "rolling_leakage_bps": None,
                }

            total_notional = sum(row["size"] for row in rows)

            if total_notional == 0:
                weighted_adverse = None
                weighted_spread = None
            else:
                # Compute weighted averages, excluding None values
                adverse_values = [
                    (row["size"], row["adverse_move_pct"])
                    for row in rows
                    if row["adverse_move_pct"] is not None
                ]
                spread_values = [
                    (row["size"], row["spread_paid_pct"])
                    for row in rows
                    if row["spread_paid_pct"] is not None
                ]

                if adverse_values:
                    weighted_adverse = sum(
                        s * a for s, a in adverse_values
                    ) / sum(s for s, _ in adverse_values)
                else:
                    weighted_adverse = None

                if spread_values:
                    weighted_spread = sum(
                        s * sp for s, sp in spread_values
                    ) / sum(s for s, _ in spread_values)
                else:
                    weighted_spread = None

            # Time to first fill (first leg's time_in_book_s)
            time_to_first = None
            for row in rows:
                if row["time_in_book_s"] is not None:
                    time_to_first = row["time_in_book_s"]
                    break

            # Fill completion ratio in 60s
            filled_in_60s = sum(
                1 for row in rows
                if row["time_in_book_s"] is not None and row["time_in_book_s"] <= 60
            )
            completion_ratio_60s = filled_in_60s / len(rows) if rows else None

            return {
                "notional_weighted_adverse_move_pct": weighted_adverse,
                "notional_weighted_spread_paid_pct": weighted_spread,
                "time_to_first_fill_s": time_to_first,
                "fill_completion_ratio_60s": completion_ratio_60s,
                "rolling_leakage_bps": None,  # Computed elsewhere if needed
            }
        finally:
            conn.close()

    @staticmethod
    def bucket_decay(
        burst_override: bool,
        signal_age_minutes: Optional[float],
        fill_type: Optional[str],
    ) -> str:
        """
        Bucket leg into decay category based on conditions.

        Args:
            burst_override: Whether burst override is active.
            signal_age_minutes: Age of signal in minutes.
            fill_type: Type of fill (e.g., 'market', 'limit', 'partial').

        Returns:
            Decay bucket label.
        """
        if burst_override:
            return "burst_override"

        if signal_age_minutes is None:
            return "unknown_age"

        if signal_age_minutes > 120:
            return "stale_signal_120m"
        elif signal_age_minutes > 30:
            return "aging_signal_30m"
        elif signal_age_minutes <= 5:
            return "fresh_signal_5m"
        else:
            return "medium_age_signal"

    # ========================================================================
    # Bullet #11: Alerts and Logging
    # ========================================================================

    @staticmethod
    def check_alerts(metrics: Dict[str, Any]) -> List[Alert]:
        """
        Check metrics against alert thresholds.

        Thresholds:
          - decay > 25%
          - spread_paid > 10%
          - time_to_first_fill > 90s
          - fill_completion_ratio_60s < 40%
          - rolling_leakage > 8 bps

        Args:
            metrics: Metrics dictionary (from compute_decay_metrics or similar).

        Returns:
            List of Alert objects.
        """
        alerts = []

        # Decay alert
        if (
            metrics.get("notional_weighted_adverse_move_pct") is not None
            and metrics["notional_weighted_adverse_move_pct"] > 0.25
        ):
            alerts.append(
                Alert(
                    alert_type="decay_threshold",
                    alert_value=metrics["notional_weighted_adverse_move_pct"],
                    threshold=0.25,
                    details="Adverse move exceeds 25%",
                )
            )

        # Spread alert
        if (
            metrics.get("notional_weighted_spread_paid_pct") is not None
            and metrics["notional_weighted_spread_paid_pct"] > 0.10
        ):
            alerts.append(
                Alert(
                    alert_type="spread_paid_threshold",
                    alert_value=metrics["notional_weighted_spread_paid_pct"],
                    threshold=0.10,
                    details="Spread paid exceeds 10%",
                )
            )

        # Time to first fill alert
        if (
            metrics.get("time_to_first_fill_s") is not None
            and metrics["time_to_first_fill_s"] > 90
        ):
            alerts.append(
                Alert(
                    alert_type="time_to_first_fill_threshold",
                    alert_value=metrics["time_to_first_fill_s"],
                    threshold=90,
                    details="Time to first fill exceeds 90s",
                )
            )

        # Fill completion ratio alert
        if (
            metrics.get("fill_completion_ratio_60s") is not None
            and metrics["fill_completion_ratio_60s"] < 0.40
        ):
            alerts.append(
                Alert(
                    alert_type="fill_completion_ratio_threshold",
                    alert_value=metrics["fill_completion_ratio_60s"],
                    threshold=0.40,
                    details="Fill completion ratio in 60s below 40%",
                )
            )

        # Rolling leakage alert
        if (
            metrics.get("rolling_leakage_bps") is not None
            and metrics["rolling_leakage_bps"] > 0.0008
        ):  # 8 bps = 0.0008
            alerts.append(
                Alert(
                    alert_type="rolling_leakage_threshold",
                    alert_value=metrics["rolling_leakage_bps"] * 10000,  # Convert to bps
                    threshold=8,
                    details="Rolling leakage exceeds 8 bps",
                )
            )

        return alerts

    def log_no_trade_histogram(
        self,
        market_slug: str,
        reason: str,
        is_burst: int,
        confidence: Optional[int],
        theo_ev: Optional[float],
        min_ev: Optional[float],
        details: Optional[str],
    ) -> None:
        """
        Log no-trade event.

        Args:
            market_slug: Market identifier.
            reason: Reason for not trading.
            is_burst: 1 if burst context, 0 otherwise.
            confidence: Station confidence score.
            theo_ev: Theoretical EV.
            min_ev: Minimum EV threshold.
            details: Additional details JSON string.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            timestamp_utc = datetime.now(timezone.utc).isoformat()

            cursor.execute("""
                INSERT INTO no_trade_log (
                    market_slug,
                    reason,
                    is_burst,
                    station_confidence,
                    theo_ev,
                    min_theo_ev,
                    details,
                    timestamp_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                market_slug,
                reason,
                is_burst,
                confidence,
                theo_ev,
                min_ev,
                details,
                timestamp_utc,
            ))

            conn.commit()
        finally:
            conn.close()

    def log_burst_context(self, trade_group_id: str, context_dict: Dict[str, Any]) -> None:
        """
        Update burst context for a trade group.

        Args:
            trade_group_id: Unique trade group identifier.
            context_dict: Context information dictionary.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            context_json = json.dumps(context_dict)

            cursor.execute("""
                UPDATE trade_groups
                SET burst_context = ?, is_burst_override = 1
                WHERE trade_group_id = ?
            """, (context_json, trade_group_id))

            conn.commit()
        finally:
            conn.close()

    # ========================================================================
    # Bullet #17: No Silent Fallbacks
    # ========================================================================

    def log_fallback(
        self,
        fallback_type: str,
        source_method: str,
        target_method: str,
        market_id: Optional[str],
        reason: str,
    ) -> None:
        """
        Log fallback from one method to another.

        Args:
            fallback_type: Type of fallback (e.g., 'forecast', 'fill_estimation').
            source_method: Original method name.
            target_method: Fallback method name.
            market_id: Market identifier (optional).
            reason: Reason for fallback.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            timestamp_utc = datetime.now(timezone.utc).isoformat()

            cursor.execute("""
                INSERT INTO fallback_log (
                    fallback_type,
                    source_method,
                    target_method,
                    market_id,
                    reason,
                    timestamp_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                fallback_type,
                source_method,
                target_method,
                market_id,
                reason,
                timestamp_utc,
            ))

            conn.commit()
        finally:
            conn.close()

    def increment_fallback_counter(self, fallback_type: str) -> int:
        """
        Count fallbacks of a specific type.

        Args:
            fallback_type: Type of fallback to count.

        Returns:
            Count of fallbacks of this type.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) as cnt FROM fallback_log WHERE fallback_type = ?",
                (fallback_type,),
            )
            row = cursor.fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()

    # ========================================================================
    # Bullet #20: Reporting and Sanity
    # ========================================================================

    @staticmethod
    def compute_sharpe(daily_returns: List[float]) -> float:
        """
        Compute annualized Sharpe ratio from daily returns.

        Sharpe = mean / std * sqrt(252)

        Args:
            daily_returns: List of daily returns (as decimal fractions).

        Returns:
            Annualized Sharpe ratio. Returns 0.0 if std dev is zero or insufficient data.
        """
        if not daily_returns or len(daily_returns) < 2:
            return 0.0

        mean_return = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_return) ** 2 for r in daily_returns) / len(daily_returns)
        std_dev = math.sqrt(variance)

        if std_dev == 0:
            return 0.0

        return (mean_return / std_dev) * math.sqrt(252)

    def compute_win_rate(self) -> Tuple[float, int, int]:
        """
        Compute win rate at trade group level.

        Returns:
            Tuple of (win_rate, winning_groups, total_groups).
            win_rate is in [0, 1]. Returns (0.0, 0, 0) if no groups found.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            # Count total groups
            cursor.execute("SELECT COUNT(*) as cnt FROM trade_groups")
            total = cursor.fetchone()["cnt"]

            if total == 0:
                return 0.0, 0, 0

            # Count winning groups (pnl > 0)
            cursor.execute("SELECT COUNT(*) as cnt FROM trade_groups WHERE pnl > 0")
            winning = cursor.fetchone()["cnt"]

            win_rate = winning / total if total > 0 else 0.0

            return win_rate, winning, total
        finally:
            conn.close()

    def generate_daily_report(self) -> Dict[str, Any]:
        """
        Generate daily report and insert into daily_reports table.

        Returns:
            Dictionary with report metrics.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()

            report_date = datetime.now(timezone.utc).date().isoformat()

            # Win rate
            win_rate, winning_groups, total_groups = self.compute_win_rate()

            # PnL metrics
            cursor.execute("""
                SELECT SUM(pnl) as total_pnl, SUM(fees_total) as total_fees
                FROM trade_groups
            """)
            pnl_row = cursor.fetchone()
            total_pnl = pnl_row["total_pnl"] or 0.0
            total_fees = pnl_row["total_fees"] or 0.0

            # Daily returns (simplified: return per group)
            cursor.execute(
                "SELECT pnl FROM trade_groups WHERE outcome IS NOT NULL"
            )
            pnl_rows = cursor.fetchall()
            daily_returns = [
                row["pnl"] for row in pnl_rows
                if row["pnl"] is not None
            ]

            # Normalize returns if we have total capital (assuming 1000 units as base)
            if daily_returns:
                daily_returns = [r / 1000.0 for r in daily_returns]
            else:
                daily_returns = []

            sharpe = self.compute_sharpe(daily_returns)

            created_at = datetime.now(timezone.utc).isoformat()
            details = json.dumps({
                "daily_return_mean": sum(daily_returns) / len(daily_returns) if daily_returns else 0.0,
                "daily_return_count": len(daily_returns),
            })

            cursor.execute("""
                INSERT INTO daily_reports (
                    report_date,
                    sharpe_annualized,
                    win_rate_group_level,
                    total_trade_groups,
                    winning_groups,
                    total_pnl,
                    total_fees,
                    details,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                report_date,
                sharpe,
                win_rate,
                total_groups,
                winning_groups,
                total_pnl,
                total_fees,
                details,
                created_at,
            ))

            conn.commit()

            return {
                "report_date": report_date,
                "sharpe_annualized": sharpe,
                "win_rate_group_level": win_rate,
                "total_trade_groups": total_groups,
                "winning_groups": winning_groups,
                "total_pnl": total_pnl,
                "total_fees": total_fees,
            }
        finally:
            conn.close()

    # ========================================================================
    # General CRUD Operations
    # ========================================================================

    def insert_trade_group(self, **fields) -> None:
        """
        Insert trade group.

        Args:
            **fields: Keyword arguments matching trade_groups columns.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            columns = ", ".join(fields.keys())
            placeholders = ", ".join("?" * len(fields))

            cursor.execute(
                f"INSERT INTO trade_groups ({columns}) VALUES ({placeholders})",
                tuple(fields.values()),
            )

            conn.commit()
        finally:
            conn.close()

    def insert_leg(self, **fields) -> None:
        """
        Insert leg.

        Args:
            **fields: Keyword arguments matching legs columns.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            columns = ", ".join(fields.keys())
            placeholders = ", ".join("?" * len(fields))

            cursor.execute(
                f"INSERT INTO legs ({columns}) VALUES ({placeholders})",
                tuple(fields.values()),
            )

            conn.commit()
        finally:
            conn.close()

    def insert_observation(self, **fields) -> None:
        """
        Insert observation.

        Args:
            **fields: Keyword arguments matching observations columns.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            columns = ", ".join(fields.keys())
            placeholders = ", ".join("?" * len(fields))

            cursor.execute(
                f"INSERT INTO observations ({columns}) VALUES ({placeholders})",
                tuple(fields.values()),
            )

            conn.commit()
        finally:
            conn.close()

    def insert_book_snapshot(self, **fields) -> None:
        """
        Insert book snapshot.

        Args:
            **fields: Keyword arguments matching book_snapshots columns.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            columns = ", ".join(fields.keys())
            placeholders = ", ".join("?" * len(fields))

            cursor.execute(
                f"INSERT INTO book_snapshots ({columns}) VALUES ({placeholders})",
                tuple(fields.values()),
            )

            conn.commit()
        finally:
            conn.close()

    def log_alert(
        self,
        alert_type: str,
        value: float,
        threshold: float,
        tg_id: Optional[str],
        slug: Optional[str],
        details: str,
    ) -> None:
        """
        Log alert.

        Args:
            alert_type: Type of alert.
            value: Alert value.
            threshold: Threshold value.
            tg_id: Trade group ID.
            slug: Market slug.
            details: Alert details.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            timestamp_utc = datetime.now(timezone.utc).isoformat()

            cursor.execute("""
                INSERT INTO alert_log (
                    alert_type,
                    alert_value,
                    threshold,
                    trade_group_id,
                    market_slug,
                    details,
                    timestamp_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                alert_type,
                value,
                threshold,
                tg_id,
                slug,
                details,
                timestamp_utc,
            ))

            conn.commit()
        finally:
            conn.close()

    def log_fee(
        self,
        leg_id: str,
        fees_enabled: int,
        fee_rate: int,
        realized: float,
    ) -> None:
        """
        Log fee information.

        Args:
            leg_id: Leg identifier.
            fees_enabled: 1 if fees enabled, 0 otherwise.
            fee_rate: Fee rate in basis points.
            realized: Realized fees paid.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            timestamp_utc = datetime.now(timezone.utc).isoformat()

            cursor.execute("""
                INSERT INTO fee_log (
                    leg_id,
                    fees_enabled,
                    fee_rate_bps_used,
                    realized_fees_paid,
                    timestamp_utc
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                leg_id,
                fees_enabled,
                fee_rate,
                realized,
                timestamp_utc,
            ))

            conn.commit()
        finally:
            conn.close()

    def log_rebate(self, address: str, amount: float) -> None:
        """
        Log rebate.

        Args:
            address: Wallet address.
            amount: Rebate amount.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            check_timestamp = datetime.now(timezone.utc).isoformat()

            cursor.execute("""
                INSERT INTO rebate_log (
                    address,
                    rebate_amount,
                    check_timestamp
                ) VALUES (?, ?, ?)
            """, (
                address,
                amount,
                check_timestamp,
            ))

            conn.commit()
        finally:
            conn.close()

    def log_sanity_check(
        self,
        station: str,
        diff: float,
        risk: str,
        boost: float,
        skip: int,
    ) -> None:
        """
        Log sanity check result.

        Args:
            station: Station ICAO code.
            diff: WU METAR average difference (Celsius).
            risk: Risk level.
            boost: Min theo EV boost.
            skip: 1 if market should be skipped, 0 otherwise.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            check_date = datetime.now(timezone.utc).date().isoformat()

            cursor.execute("""
                INSERT INTO sanity_checks (
                    station_icao,
                    wu_metar_avg_diff_c,
                    risk_level,
                    min_theo_ev_boost,
                    skip_market,
                    check_date
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                station,
                diff,
                risk,
                boost,
                skip,
                check_date,
            ))

            conn.commit()
        finally:
            conn.close()

    def log_cross_market(
        self,
        tg_id: Optional[str],
        target: str,
        peers: str,
        delta_z: float,
        corr: float,
        adj: float,
        flag: int,
    ) -> None:
        """
        Log cross-market analysis.

        Args:
            tg_id: Trade group ID.
            target: Target market.
            peers: Peer markets JSON string.
            delta_z: Delta z-score.
            corr: Seasonal correlation.
            adj: Min theo EV adjustment.
            flag: 1 if flag raised, 0 otherwise.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            timestamp_utc = datetime.now(timezone.utc).isoformat()

            cursor.execute("""
                INSERT INTO cross_market_log (
                    trade_group_id,
                    target_market,
                    peer_markets,
                    delta_z_score,
                    season_corr,
                    min_theo_ev_adjustment,
                    flag_raised,
                    timestamp_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tg_id,
                target,
                peers,
                delta_z,
                corr,
                adj,
                flag,
                timestamp_utc,
            ))

            conn.commit()
        finally:
            conn.close()
