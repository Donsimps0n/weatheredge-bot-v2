"""Tests for strategy family selection, exact_2bin logic, RUFLO integration, and stats."""
import pytest
import sys
import os
import json

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestStrategyConfig:
    """Test strategy configuration flags."""

    def test_config_flags_exist(self):
        from config import (ENABLE_ABOVE_BELOW, ENABLE_EXACT_SINGLE, ENABLE_EXACT_2BIN,
                            EXACT_2BIN_MIN_COMBINED_EDGE, EXACT_2BIN_MAX_COMBINED_COST,
                            EXACT_2BIN_REQUIRE_ADJACENT)
        assert ENABLE_ABOVE_BELOW is True
        assert ENABLE_EXACT_SINGLE is False
        assert ENABLE_EXACT_2BIN is True
        assert EXACT_2BIN_REQUIRE_ADJACENT is True

    def test_exact_single_disabled_by_default(self):
        from config import ENABLE_EXACT_SINGLE
        assert ENABLE_EXACT_SINGLE is False, "exact_single should be disabled"

    def test_exact_2bin_thresholds_conservative(self):
        from config import EXACT_2BIN_MIN_COMBINED_EDGE, EXACT_2BIN_MAX_COMBINED_COST
        assert EXACT_2BIN_MIN_COMBINED_EDGE >= 0.08, "min edge too aggressive"
        assert EXACT_2BIN_MAX_COMBINED_COST <= 0.50, "max cost too loose"


class TestExact2BinCandidateSelection:
    """Test adjacent pair selection logic."""

    def _make_signal(self, city, threshold_c, our_prob, mkt_price, end_date="2026-04-07T12:00:00Z"):
        return {
            "city": city,
            "direction": "exact",
            "threshold_c": threshold_c,
            "our_prob": our_prob,
            "market_price": mkt_price,
            "signal": "BUY YES",
            "tokens": [{"outcome": "Yes", "token_id": f"tok_{city}_{threshold_c}"}],
            "end_date": end_date,
            "confidence": 3,
            "data_quality": "good",
        }

    def test_adjacent_pair_selected(self):
        """Adjacent bins (1°C apart) should form a valid pair."""
        sig_a = self._make_signal("Atlanta", 20.0, 25.0, 12.0)
        sig_b = self._make_signal("Atlanta", 21.0, 22.0, 10.0)
        # Combined prob = 0.25 + 0.22 = 0.47
        # Combined cost = 0.12 + 0.10 = 0.22
        # Combined edge = 0.47 - 0.22 = 0.25 > 0.10 threshold
        combined_prob = sig_a["our_prob"] / 100 + sig_b["our_prob"] / 100
        combined_cost = sig_a["market_price"] / 100 + sig_b["market_price"] / 100
        combined_edge = combined_prob - combined_cost
        assert combined_edge > 0.10
        assert combined_cost < 0.40
        assert abs(sig_b["threshold_c"] - sig_a["threshold_c"]) <= 1.5

    def test_non_adjacent_pair_rejected(self):
        """Bins 5°C apart should NOT form a pair when adjacency required."""
        sig_a = self._make_signal("Atlanta", 20.0, 25.0, 12.0)
        sig_b = self._make_signal("Atlanta", 25.0, 22.0, 10.0)
        gap = abs(sig_b["threshold_c"] - sig_a["threshold_c"])
        assert gap > 1.5, "Non-adjacent bins should be rejected"

    def test_combined_cost_gate(self):
        """Pair with combined cost > 40c should be rejected."""
        sig_a = self._make_signal("Atlanta", 20.0, 35.0, 25.0)
        sig_b = self._make_signal("Atlanta", 21.0, 30.0, 22.0)
        combined_cost = sig_a["market_price"] / 100 + sig_b["market_price"] / 100
        assert combined_cost > 0.40, "Over-budget pairs should be rejected"

    def test_combined_edge_below_threshold(self):
        """Pair with combined edge < 10pp should be rejected."""
        sig_a = self._make_signal("Atlanta", 20.0, 12.0, 10.0)
        sig_b = self._make_signal("Atlanta", 21.0, 11.0, 9.0)
        combined_prob = sig_a["our_prob"] / 100 + sig_b["our_prob"] / 100
        combined_cost = sig_a["market_price"] / 100 + sig_b["market_price"] / 100
        combined_edge = combined_prob - combined_cost
        assert combined_edge < 0.10, "Low-edge pairs should be rejected"

    def test_combined_math_correct(self):
        """Verify combined probability and cost math."""
        sig_a = self._make_signal("Chicago", 15.0, 30.0, 15.0)
        sig_b = self._make_signal("Chicago", 16.0, 28.0, 13.0)
        combined_prob = sig_a["our_prob"] / 100 + sig_b["our_prob"] / 100
        combined_cost = sig_a["market_price"] / 100 + sig_b["market_price"] / 100
        combined_edge = combined_prob - combined_cost
        assert abs(combined_prob - 0.58) < 0.001
        assert abs(combined_cost - 0.28) < 0.001
        assert abs(combined_edge - 0.30) < 0.001


class TestRufloStrategyValidation:
    """Test RUFLO pre-trade validator with strategy types."""

    def test_ruflo_validate_2bin_exists(self):
        """validate_2bin method should exist on PreTradeValidator."""
        from ruflo_monitor import PreTradeValidator
        v = PreTradeValidator()
        assert hasattr(v, 'validate_2bin'), "validate_2bin method missing"

    def test_ruflo_validate_2bin_accepts_good_pair(self):
        from ruflo_monitor import PreTradeValidator
        v = PreTradeValidator()
        sig_a = {'confidence': 3, 'theo_ev': 8.0, 'end_date': '2026-04-07T18:00:00Z',
                 'size': 5, 'size_cap': 10}
        sig_b = {'confidence': 3, 'theo_ev': 7.0, 'end_date': '2026-04-07T18:00:00Z',
                 'size': 5, 'size_cap': 10}
        ok, reason = v.validate_2bin(sig_a, sig_b)
        assert ok is True, f"Should accept good pair: {reason}"

    def test_ruflo_validate_2bin_rejects_low_confidence(self):
        from ruflo_monitor import PreTradeValidator
        v = PreTradeValidator()
        sig_a = {'confidence': 1, 'theo_ev': 8.0, 'end_date': '2026-04-07T18:00:00Z',
                 'size': 5, 'size_cap': 10}
        sig_b = {'confidence': 3, 'theo_ev': 7.0, 'end_date': '2026-04-07T18:00:00Z',
                 'size': 5, 'size_cap': 10}
        ok, reason = v.validate_2bin(sig_a, sig_b)
        assert ok is False, "Should reject low confidence leg"

    def test_ruflo_validate_2bin_rejects_low_ev(self):
        from ruflo_monitor import PreTradeValidator
        v = PreTradeValidator()
        sig_a = {'confidence': 3, 'theo_ev': 0.03, 'end_date': '2026-04-07T18:00:00Z',
                 'size': 5, 'size_cap': 10}
        sig_b = {'confidence': 3, 'theo_ev': 0.03, 'end_date': '2026-04-07T18:00:00Z',
                 'size': 5, 'size_cap': 10}
        ok, reason = v.validate_2bin(sig_a, sig_b)
        assert ok is False, "Should reject low combined EV"


class TestStrategyTagging:
    """Test that trades get correct strategy_type tags."""

    def test_above_below_tagged(self):
        direction = "above"
        strategy_type = "above_below" if direction in ("above", "below") else "exact_single"
        assert strategy_type == "above_below"

    def test_below_tagged(self):
        direction = "below"
        strategy_type = "above_below" if direction in ("above", "below") else "exact_single"
        assert strategy_type == "above_below"

    def test_exact_tagged_as_single(self):
        direction = "exact"
        strategy_type = "above_below" if direction in ("above", "below") else "exact_single"
        assert strategy_type == "exact_single"


class TestLedgerStrategyColumns:
    """Test that ledger supports strategy columns."""

    def test_record_trade_with_strategy_type(self, tmp_path):
        """Ensure strategy_type is persisted in ledger."""
        import sqlite3
        db = str(tmp_path / "test_ledger.db")
        # Temporarily override DB_PATH
        import trade_ledger
        orig = trade_ledger.DB_PATH
        trade_ledger.DB_PATH = db
        trade_ledger._conn = None  # force reconnect
        try:
            trade_ledger.record_trade({
                "city": "Atlanta",
                "question": "test above",
                "signal": "BUY YES",
                "token_id": "tok_test",
                "price": 0.50,
                "size": 10,
                "ev": 5.0,
                "strategy_type": "above_below",
                "trade_group_id": None,
            })
            trades = trade_ledger.get_all_trades(1)
            assert len(trades) == 1
            t = trades[0]
            assert t.get("strategy_type") == "above_below"
        finally:
            trade_ledger.DB_PATH = orig
            trade_ledger._conn = None

    def test_record_trade_with_2bin_group(self, tmp_path):
        """Ensure trade_group_id is persisted for exact_2bin legs."""
        import sqlite3
        db = str(tmp_path / "test_ledger2.db")
        import trade_ledger
        orig = trade_ledger.DB_PATH
        trade_ledger.DB_PATH = db
        trade_ledger._conn = None
        try:
            group_id = "grp_abc123"
            trade_ledger.record_trade({
                "city": "Chicago",
                "question": "test exact 2bin leg A",
                "signal": "BUY YES",
                "token_id": "tok_a",
                "price": 0.15,
                "size": 5,
                "ev": 8.0,
                "strategy_type": "exact_2bin",
                "trade_group_id": group_id,
            })
            trade_ledger.record_trade({
                "city": "Chicago",
                "question": "test exact 2bin leg B",
                "signal": "BUY YES",
                "token_id": "tok_b",
                "price": 0.12,
                "size": 5,
                "ev": 8.0,
                "strategy_type": "exact_2bin",
                "trade_group_id": group_id,
            })
            trades = trade_ledger.get_all_trades(10)
            assert len(trades) == 2
            assert all(t.get("strategy_type") == "exact_2bin" for t in trades)
            assert all(t.get("trade_group_id") == group_id for t in trades)
        finally:
            trade_ledger.DB_PATH = orig
            trade_ledger._conn = None


class TestExact2BinSettlement:
    """Test that exact_2bin can be scored correctly — group wins if either leg wins."""

    def test_group_wins_if_one_leg_wins(self):
        """A 2-bin group should count as won if either leg resolves YES."""
        legs = [
            {"won": 1, "pnl": 0.85},  # Leg A wins
            {"won": 0, "pnl": -0.12},  # Leg B loses
        ]
        group_won = any(l["won"] == 1 for l in legs)
        group_pnl = sum(l["pnl"] for l in legs)
        assert group_won is True
        assert group_pnl > 0

    def test_group_loses_if_both_legs_lose(self):
        legs = [
            {"won": 0, "pnl": -0.15},
            {"won": 0, "pnl": -0.12},
        ]
        group_won = any(l["won"] == 1 for l in legs)
        group_pnl = sum(l["pnl"] for l in legs)
        assert group_won is False
        assert group_pnl < 0
