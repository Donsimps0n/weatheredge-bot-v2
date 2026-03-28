"""
Dutch Book Arbitrage Scanner for Polymarket Temperature Bins

Detects mispricing in Polymarket temperature markets where exactly one bin resolves YES.
When sum of YES prices ≠ 1.0, generates arbitrage trades:
- sum > 1.0: buy NO on overpriced bins
- sum < 1.0: buy YES on underpriced bins
"""

import re
import logging
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, asdict
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)

# Constants
MIN_BOOK_EDGE = 0.03  # 3% minimum imbalance to trade
FULL_ARB_THRESHOLD = 0.06  # 6% for full all-bins arbitrage
POLYMARKET_FEE = 0.02  # 2% taker fee estimate
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
class DutchBookArb:
    """Represents a detected arbitrage opportunity"""
    city: str
    date: str
    book_value: float
    edge: float
    arbitrage_type: str  # "overbooked", "underbooked", "full"
    trades: List[Dict[str, Any]]
    profit_percent: float


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

    def detect_arbitrage(self, city_date: str, bins: List[BinMarket]) -> Optional[DutchBookArb]:
        """Detect arbitrage in a city+date bin group"""
        if len(bins) < 2:
            return None

        book_value = self.calculate_book_value(bins)
        edge = abs(book_value - 1.0)

        if edge < MIN_BOOK_EDGE:
            return None

        profit_percent = edge * 100

        # Determine arbitrage type
        if book_value > 1.0:
            arb_type = "overbooked"
            trades = self._generate_overbooked_trades(bins, book_value)
        else:
            arb_type = "underbooked"
            trades = self._generate_underbooked_trades(bins, book_value)

        # Check for full arbitrage if edge is large
        if edge >= FULL_ARB_THRESHOLD:
            arb_type = "full"
            trades = self._generate_full_arb_trades(bins, book_value)

        city_date_parts = city_date.split('_')
        city = city_date_parts[0]
        date = '_'.join(city_date_parts[1:])

        return DutchBookArb(
            city=city,
            date=date,
            book_value=round(book_value, 4),
            edge=round(edge, 4),
            arbitrage_type=arb_type,
            trades=trades[:MAX_TRADES_PER_SCAN],
            profit_percent=round(profit_percent, 2)
        )

    def _generate_overbooked_trades(self, bins: List[BinMarket], book_value: float) -> List[Dict]:
        """Buy NO on most overpriced bins when book sum > 1.0"""
        # Sort by YES price (most overpriced first)
        sorted_bins = sorted(bins, key=lambda b: b.yes_price, reverse=True)
        trades = []

        for bin in sorted_bins[:3]:  # Limit to 3 most overpriced
            if bin.no_price > 0:
                trade = {
                    'action': 'buy_no',
                    'slug': bin.slug,
                    'bin': bin.bin_range,
                    'price': round(bin.no_price, 4),
                    'reason': f'overpriced YES at {bin.yes_price}'
                }
                trades.append(trade)

        return trades

    def _generate_underbooked_trades(self, bins: List[BinMarket], book_value: float) -> List[Dict]:
        """Buy YES on most underpriced bins when book sum < 1.0"""
        # Sort by YES price (most underpriced first)
        sorted_bins = sorted(bins, key=lambda b: b.yes_price)
        trades = []

        for bin in sorted_bins[:3]:  # Limit to 3 most underpriced
            if bin.yes_price > 0:
                trade = {
                    'action': 'buy_yes',
                    'slug': bin.slug,
                    'bin': bin.bin_range,
                    'price': round(bin.yes_price, 4),
                    'reason': f'underpriced YES at {bin.yes_price}'
                }
                trades.append(trade)

        return trades

    def _generate_full_arb_trades(self, bins: List[BinMarket], book_value: float) -> List[Dict]:
        """Generate trades for full arbitrage across all bins"""
        trades = []

        if book_value > 1.0:
            # Buy all NOs
            for bin in bins:
                if bin.no_price > 0:
                    trade = {
                        'action': 'buy_no',
                        'slug': bin.slug,
                        'bin': bin.bin_range,
                        'price': round(bin.no_price, 4),
                        'reason': 'full arbitrage - buy all NOs'
                    }
                    trades.append(trade)
        else:
            # Buy all YESes
            for bin in bins:
                if bin.yes_price > 0:
                    trade = {
                        'action': 'buy_yes',
                        'slug': bin.slug,
                        'bin': bin.bin_range,
                        'price': round(bin.yes_price, 4),
                        'reason': 'full arbitrage - buy all YESes'
                    }
                    trades.append(trade)

        return trades

    def scan(self, markets: List[Dict]) -> List[DutchBookArb]:
        """Scan markets for arbitrage opportunities"""
        self.stats['scans'] += 1
        self.stats['last_scan'] = datetime.now().isoformat()

        grouped = self.group_markets_by_city_date(markets)
        opportunities = []

        for city_date, bins in grouped.items():
            arb = self.detect_arbitrage(city_date, bins)
            if arb:
                opportunities.append(arb)
                self.stats['opportunities_found'] += 1
                self.stats['total_edge'] += arb.edge

                logger.info(
                    f"Dutch Book: {arb.city} {arb.date} - "
                    f"{arb.arbitrage_type} (edge={arb.edge:.2%}, profit={arb.profit_percent}%)"
                )

                # Publish to shared state if available
                if self.shared_state:
                    self.shared_state.publish(
                        'dutch_book',
                        f"{arb.city}_{arb.date}",
                        asdict(arb)
                    )

        return opportunities

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
