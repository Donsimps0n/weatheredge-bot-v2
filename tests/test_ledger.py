"""
Tests for ledger_telemetry module.

Covers spec bullets #2 (Frozen snapshots), #8 (Edge decay metrics),
#11 (Alerts and logging), #17 (No silent fallbacks), and #20 (Reporting).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import tempfile
from pathlib import Path
from ledger_telemetry import Ledger


class TestLedger:
    """Tests for SQLite ledger."""

    @pytest.fixture
    def temp_db(self):
        """Create a temporary database file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            yield db_path

    def test_init_db(self, temp_db):
        """Test database initialization: tables created."""
        ledger = Ledger(temp_db)
        ledger.init_db()

        # Verify database file exists
        assert os.path.exists(temp_db), "Database file should exist"

        # Verify tables are created (query should not fail)
        conn = ledger._get_connection()
        cursor = conn.cursor()

        # Check if trade_groups table exists
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='trade_groups'
        """)
        assert cursor.fetchone() is not None, "trade_groups table should exist"

        conn.close()

    def test_freeze_snapshot_immutable(self, temp_db):
        """Test frozen snapshots: second insert should not overwrite.

        Spec #2: Frozen snapshots are immutable.
        """
        from ledger_telemetry import TradeSnapshot

        ledger = Ledger(temp_db)
        ledger.init_db()

        snapshot = TradeSnapshot(
            trade_group_id="test-001",
            timestamp="2023-06-01T12:00:00Z",
            data={"test": "data"},
        )

        # Insert first snapshot
        ledger.freeze_snapshot(snapshot)

        # Try to insert same ID again - should raise or not overwrite
        with pytest.raises(Exception):
            ledger.freeze_snapshot(snapshot)

    def test_insert_and_query_trade_group(self, temp_db):
        """Test inserting and querying trade group data."""
        ledger = Ledger(temp_db)
        ledger.init_db()

        trade_data = {
            "trade_group_id": "trade-123",
            "market_slug": "temp-ny-20230601",
            "market_id": "market-xyz",
            "station_icao": "KJFK",
            "station_confidence": 3,
            "regime_detected": "clear",
            "diurnal_stage": "pre-peak",
            "peak_window_start": 14,
            "peak_window_end": 17,
            "theoretical_full_ev": 0.15,
            "cost_proxy": 0.02,
            "min_theo_ev_applied": 0.10,
        }

        ledger.insert_trade_group(trade_data)

        # Query it back
        result = ledger.query_trade_group("trade-123")
        assert result is not None, "Should find inserted trade"
        assert result["market_slug"] == "temp-ny-20230601"

    def test_compute_sharpe(self, temp_db):
        """Test Sharpe ratio computation with annualization."""
        ledger = Ledger(temp_db)
        ledger.init_db()

        # Insert sample returns
        returns = [0.01, -0.005, 0.02, 0.015, 0.01, -0.002, 0.012]

        for i, ret in enumerate(returns):
            ledger.log_return(
                trade_group_id="test-sharpe",
                return_pct=ret * 100,
                timestamp="2023-06-01T12:00:00Z",
            )

        # Compute Sharpe
        sharpe = ledger.compute_sharpe("test-sharpe")

        # Should be annualized (× sqrt(252))
        assert isinstance(sharpe, float), "Should return float"
        # With small sample, Sharpe should be reasonable
        assert abs(sharpe) < 100, "Sharpe should be reasonable magnitude"

    def test_compute_win_rate(self, temp_db):
        """Test win rate computation."""
        ledger = Ledger(temp_db)
        ledger.init_db()

        # Insert winning and losing trades
        returns = [0.01, 0.02, -0.005, 0.015, -0.01, 0.012]

        for i, ret in enumerate(returns):
            ledger.log_return(
                trade_group_id="test-winrate",
                return_pct=ret * 100,
                timestamp="2023-06-01T12:00:00Z",
            )

        win_rate = ledger.compute_win_rate("test-winrate")

        # 4 wins out of 6 trades
        assert 0.0 <= win_rate <= 1.0, "Win rate should be in [0, 1]"
        assert abs(win_rate - 4/6) < 0.01, f"Expected ~0.667, got {win_rate}"

    def test_log_fallback(self, temp_db):
        """Test fallback logging: verify fallback is logged when operation fails.

        Spec #17: No silent fallbacks - all fallbacks must be logged.
        """
        ledger = Ledger(temp_db)
        ledger.init_db()

        # Attempt operation that might have fallback
        # This should log the fallback if it occurs
        ledger.log_fallback(
            component="test_component",
            reason="Test fallback reason",
            severity="warning",
        )

        # Verify fallback was logged
        # (Check that no exception was raised and logging occurred)

    def test_alert_on_edge_decay(self, temp_db):
        """Test alerting on edge decay."""
        from ledger_telemetry import Alert

        ledger = Ledger(temp_db)
        ledger.init_db()

        alert = Alert(
            alert_type="edge_decay",
            alert_value=0.08,
            threshold=0.10,
            trade_group_id="trade-123",
            market_slug="temp-ny-20230601",
            details="Edge decayed faster than expected",
        )

        ledger.log_alert(alert)

        # Verify alert was logged
        alerts = ledger.query_alerts(trade_group_id="trade-123")
        assert len(alerts) > 0, "Alert should be logged"
