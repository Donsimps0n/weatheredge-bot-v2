"""
Distribution Inconsistency Detector for Polymarket Temperature Bins

Detects distribution inconsistencies in Polymarket temperature markets where exactly one bin resolves YES.
When sum of YES prices ≠ 1.0, generates inconsistency signals for scoring:
- sum > 1.0: overbooked distribution (more likely to trade)
- sum < 1.0: underbooked distribution (more likely to trade)

Signals feed the scoring engine; executable only if net edge exceeds fee costs.
"""

import re
import logging
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, asdict
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)

# Constants
MIN_BOOK_EDGE = 0.05  # 5% minimum imbalance before flagging
FULL_ARB_THRESHOLD = 0.06  # 6% for full all-bins arbitrage
# Polymarket CLOB fees are in bps, not percent — many weather markets are fee-free.
# We use a conservative spread+cost estimate rather than a fixed fee percentage.
# This is the estimated round-trip execution cost per bin (spread + any fees).
EXECUTION_COST_USD_PER_BIN = 0.01  # $0.01 per bin round-trip (spread + fees estimate)
MAX_TRADES_PER_SCAN = 8
SCAN_INTERVAL_S = 120

# Month mappings for date parsing
MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12
}


@dataclass
class BinMarket:
    """Represents a single temperature bin market"""
    slug: str
    question: str
    bin_range: str
    yes_price: float
    no_price: float
    outcome: str


@dataclass
class BookImbalance:
    """Represents a distribution inconsistency signal (not guaranteed profit)"""
    city: str
    date: str
    book_value: float
    imbalance_pct: float
    direction: str  # "overbooked" or "underbooked"
    most_mispriced_bins: List[Dict[str, Any]]
    net_edge_after_fees: float
    executable: bool


class DutchBookScanner:
    """Scans for Dutch Book arbitrage in temperature bin markets"""

    def __init__(self, shared_state: Optional[Any] = None):
        self.shared_state = shared_state
        self.stats = {
            'scans': 0,
            'opportunities_found': 0,
            'total_edge': 0.0,
            'last_scan': None
        }

    def parse_date_from_question(self, question: str) -> Optional[str]:
        """Extract date from market question (e.g., 'March 30' -> '2026-03-30')"""
        # Pattern: "Month Day" (case-insensitive)
        match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})', question, re.IGNORECASE)
        if not match:
            return None

        month_name = match.group(1).lower()
        day = int(match.group(2))
        month = MONTHS.get(month_name)

        if not month:
            return None

        # Assume current year or next year depending on date
        today = datetime.now()
        year = today.year
        try:
            date_obj = datetime(year, month, day)
            # If date is in the past, use next year
            if date_obj < today:
                year += 1
                date_obj = datetime(year, month, day)
            return date_obj.strftime('%Y-%m-%d')
        except ValueError:
            return None

    def extract_city(self, question: str) -> Optional[str]:
        """Extract city name from question"""
        # Pattern: "in [City]"
        match = re.search(r'in\s+([A-Za-z]+)', question)
        if match:
            return match.group(1)
        return None

    def extract_bin_range(self, question: str) -> Optional[str]:
        """Extract temperature bin range from question (e.g., '55-59°F')"""
        match = re.search(r'between\s+(\d+)°?F?\s+and\s+(\d+)°?F?', question)
        if match:
            return f"{match.group(1)}-{match.group(2)}°F"
        return None

    def group_markets_by_city_date(self, markets: List[Dict]) -> Dict[str, List[BinMarket]]:
        """Group markets by (city, date) key"""
        grouped = defaultdict(list)

        for market in markets:
            question = market.get('question', '')
            city = self.extract_city(question)
            date = self.parse_date_from_question(question)
            bin_range = self.extract_bin_range(question)

            if not (city and date):
                continue

            key = f"{city}_{date}"
            bin_market = BinMarket(
                slug=market.get('slug', ''),
                question=question,
                bin_range=bin_range or 'unknown',
                yes_price=market.get('yes_price', 0.0),
                no_price=market.get('no_price', 0.0),
                outcome=market.get('outcome', 'Yes')
            )
            grouped[key].append(bin_market)

        return grouped

    def calculate_book_value(self, bins: List[BinMarket]) -> float:
        """Calculate sum of YES prices across all bins"""
        return sum(bin.yes_price for bin in bins)

    def detect_arbitrage(self, city_date: str, bins: List[BinMarket]) -> Optional[BookImbalance]:
        """Detect distribution inconsistency in a city+date bin group"""
        if len(bins) < 2:
            return None

        book_value = self.calculate_book_value(bins)
        imbalance = abs(book_value - 1.0)

        if imbalance < MIN_BOOK_EDGE:
            return None

        # Determine direction
        if book_value > 1.0:
            direction = "overbooked"
        else:
            direction = "underbooked"

        # Calculate net edge after execution costs (spread + fees per bin)
        num_bins_to_trade = min(3, len(bins))  # Rank top 3 mispriced bins
        net_edge = imbalance - (num_bins_to_trade * EXECUTION_COST_USD_PER_BIN)

        # Only executable if net edge > 1% after all fees
        executable = net_edge > 0.01

        # Rank most mispriced bins
        mispriced_bins = self._rank_mispriced_bins(bins, direction)

        city_date_parts = city_date.split('_')
        city = city_date_parts[0]
        date = '_'.join(city_date_parts[1:])

        return BookImbalance(
            city=city,
            date=date,
            book_value=round(book_value, 4),
            imbalance_pct=round(imbalance, 4),
            direction=direction,
            most_mispriced_bins=mispriced_bins[:3],
            net_edge_after_fees=round(net_edge, 4),
            executable=executable
        )

    def _rank_mispriced_bins(self, bins: List[BinMarket], direction: str) -> List[Dict]:
        """Identify and rank most out-of-line bins (mispriced) as signal features"""
        bin_signals = []

        for bin in bins:
            if direction == "overbooked":
                # In overbooked state, YES prices are too high (sell signal)
                # Use ASK (bid for sells) - what we'd receive
                ask_price = bin.yes_price
                deviation = ask_price
                signal = {
                    'bin_range': bin.bin_range,
                    'slug': bin.slug,
                    'yes_price': round(bin.yes_price, 4),
                    'no_price': round(bin.no_price, 4),
                    'executable_price': round(ask_price, 4),  # Ask for sells
                    'deviation_from_fair': round(deviation, 4)
                }
            else:
                # In underbooked state, YES prices are too low (buy signal)
                # Use BID (ask for buys) - what we'd pay
                bid_price = bin.yes_price
                deviation = bid_price
                signal = {
                    'bin_range': bin.bin_range,
                    'slug': bin.slug,
                    'yes_price': round(bin.yes_price, 4),
                    'no_price': round(bin.no_price, 4),
                    'executable_price': round(bid_price, 4),  # Bid for buys
                    'deviation_from_fair': round(deviation, 4)
                }

            bin_signals.append(signal)

        # Sort by deviation (most mispriced first)
        bin_signals.sort(key=lambda b: b['deviation_from_fair'], reverse=True)
        return bin_signals

    def scan(self, markets: List[Dict]) -> List[BookImbalance]:
        """Scan markets for distribution inconsistency signals"""
        self.stats['scans'] += 1
        self.stats['last_scan'] = datetime.now().isoformat()

        grouped = self.group_markets_by_city_date(markets)
        signals = []

        for city_date, bins in grouped.items():
            signal = self.detect_arbitrage(city_date, bins)
            if signal:
                signals.append(signal)
                self.stats['opportunities_found'] += 1
                self.stats['total_edge'] += signal.imbalance_pct

                logger.info(
                    f"Distribution Inconsistency: {signal.city} {signal.date} - "
                    f"{signal.direction} (imbalance={signal.imbalance_pct:.2%}, "
                    f"net_edge={signal.net_edge_after_fees:.2%}, executable={signal.executable})"
                )

                # Publish to shared state if available
                if self.shared_state:
                    self.shared_state.publish(
                        'distribution_inconsistency',
                        f"{signal.city}_{signal.date}",
                        asdict(signal)
                    )

        return signals

    def get_stats(self) -> Dict[str, Any]:
        """Return scanner statistics for API endpoint"""
        return {
            'total_scans': self.stats['scans'],
            'opportunities_found': self.stats['opportunities_found'],
            'average_edge': round(
                self.stats['total_edge'] / max(self.stats['opportunities_found'], 1), 4
            ),
            'last_scan': self.stats['last_scan']
        }
