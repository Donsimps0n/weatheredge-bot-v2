"""
Tests for station_parser module.

Covers spec bullets #1 and #12:
- Station integrity & no-trade confidence
- WU vs METAR sanity checks
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from station_parser import (
    parse_station,
    compute_confidence,
    wu_metar_sanity_check,
)


class TestStationParser:
    """Tests for station parsing."""

    def test_parse_station_with_icao(self):
        """Test parsing station with ICAO code.

        Rules text containing "KJFK" → confidence 3 if URL also present.
        """
        rules_text = """
        Market resolves based on high temperature at KJFK.
        See https://www.wunderground.com/history/daily/KJFK
        """
        result = parse_station(rules_text)

        assert result.icao == "KJFK", f"Expected KJFK, got {result.icao}"
        assert result.url is not None, "Expected URL to be found"
        assert result.confidence == 3.0, f"Expected confidence 3.0, got {result.confidence}"

    def test_parse_station_no_match(self):
        """Test parsing empty rules → confidence 0."""
        result = parse_station("")

        assert result.icao is None, "Expected no ICAO"
        assert result.url is None, "Expected no URL"
        assert result.confidence == 0.0, f"Expected confidence 0.0, got {result.confidence}"

    def test_confidence_tiers(self):
        """Test each confidence tier (3, 2, 1, 0.5, 0)."""

        # Tier 3: URL + ICAO
        rules_3 = "KJFK https://www.wunderground.com/history"
        conf_3 = compute_confidence("KJFK", "https://www.wunderground.com/history", ["temperature"], None)
        assert conf_3 == 3.0, f"Expected 3.0, got {conf_3}"

        # Tier 2: keyword + city map lookup
        conf_2 = compute_confidence(None, None, ["temperature"], "new york")
        assert conf_2 == 2.0, f"Expected 2.0, got {conf_2}"

        # Tier 1: keyword only
        conf_1 = compute_confidence(None, None, ["temperature"], None)
        assert conf_1 == 1.0, f"Expected 1.0, got {conf_1}"

        # Tier 0.5: city fallback (city found, no keywords/ICAO/URL)
        conf_half = compute_confidence(None, None, [], "new york")
        assert conf_half == 0.5, f"Expected 0.5, got {conf_half}"

        # Tier 0: nothing found
        conf_0 = compute_confidence(None, None, [], None)
        assert conf_0 == 0.0, f"Expected 0.0, got {conf_0}"

    def test_wu_metar_sanity_normal(self):
        """Test WU vs METAR sanity check: similar temps → normal risk."""
        wu_temps = [20.0, 21.0, 19.5, 20.5, 21.0, 20.0]
        metar_temps = [20.2, 21.1, 19.6, 20.4, 21.1, 20.1]

        result = wu_metar_sanity_check(
            station_icao="KJFK",
            wu_daily_maxes=wu_temps,
            metar_daily_maxes=metar_temps,
        )

        assert result.risk_level == "normal", f"Expected normal, got {result.risk_level}"
        assert result.avg_diff_c < 1.2, f"Expected avg_diff < 1.2, got {result.avg_diff_c}"
        assert result.min_theo_ev_boost == 0.0, f"Expected boost 0.0, got {result.min_theo_ev_boost}"

    def test_wu_metar_sanity_high(self):
        """Test WU vs METAR sanity check: diff > 1.2°C → high risk, boost 0.04."""
        wu_temps = [20.0, 21.0, 19.0, 20.5, 21.0, 20.0]
        metar_temps = [23.0, 24.0, 23.0, 23.5, 24.0, 23.0]

        result = wu_metar_sanity_check(
            station_icao="KJFK",
            wu_daily_maxes=wu_temps,
            metar_daily_maxes=metar_temps,
        )

        assert result.risk_level == "high", f"Expected high, got {result.risk_level}"
        assert result.avg_diff_c > 1.2, f"Expected avg_diff > 1.2, got {result.avg_diff_c}"
        assert result.min_theo_ev_boost == 0.04, f"Expected boost 0.04, got {result.min_theo_ev_boost}"
        assert result.skip_market is False, "Should not skip market"
