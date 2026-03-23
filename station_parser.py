"""
Station parser module for Polymarket temperature trading bot.
Handles spec bullets #1 and #12:
- Station integrity & no-trade confidence (bullet #1)
- WU vs METAR sanity checks (bullet #12)
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Optional


# Known ICAO prefixes for validation
KNOWN_ICAO_PREFIXES = [
    'K',      # US contiguous
    'P',      # US Alaska/Hawaii
    'C',      # Canada
    'U',      # Russia/CIS
    'E',      # Northern Europe
    'L',      # Southern Europe
    'W',      # East Asia/Pacific
    'R',      # Russia
    'Z',      # China
    'V',      # Vietnam/SE Asia
    'T',      # Thailand
    'M',      # Mexico/Caribbean
    'S',      # South America
]

# Weather-related keywords to search for in rules text
WEATHER_KEYWORDS = [
    'temperature',
    'weather',
    'forecast',
    'high temp',
    'maximum temperature',
    'degrees',
    'fahrenheit',
    'celsius',
    'temp',
    'daily high',
]

# URL patterns for weather station URLs
URL_PATTERNS = {
    'wunderground': r'wunderground\.com',
    'weather_gov': r'weather\.gov',
    'noaa': r'noaa\.gov',
    'nws': r'nws\.noaa\.gov',
    'airport': r'airport',
}

# Fallback city to ICAO mapping for secondary verification
CITY_ICAO_MAP = {
    'new york': 'KJFK',
    'los angeles': 'KLAX',
    'chicago': 'KORD',
    'dallas': 'KDFW',
    'denver': 'KDEN',
    'san francisco': 'KSFO',
    'seattle': 'KSEA',
    'miami': 'KMIA',
    'phoenix': 'KPHX',
    'boston': 'KBOS',
    'atlanta': 'KATL',
    'houston': 'KIAH',
    'las vegas': 'KLAX',
    'washington': 'KDCA',
    'philadelphia': 'KPHL',
}


@dataclass
class StationResult:
    """Result of parsing station information from rules text."""
    icao: Optional[str]
    url: Optional[str]
    keywords_found: list[str]
    city_name: Optional[str]
    confidence: float


@dataclass
class SanityResult:
    """Result of WU vs METAR sanity check."""
    avg_diff_c: float
    risk_level: str  # "normal" or "high"
    min_theo_ev_boost: float
    skip_market: bool


def parse_station(rules_text: str) -> StationResult:
    """
    Parse ICAO code and URL from market rules text.

    Args:
        rules_text: Raw rules text from market description

    Returns:
        StationResult with parsed station info and confidence score
    """
    if not rules_text:
        return StationResult(
            icao=None,
            url=None,
            keywords_found=[],
            city_name=None,
            confidence=0.0,
        )

    text_lower = rules_text.lower()

    # Extract ICAO code: 4-letter, starts with known prefix
    icao = _extract_icao(rules_text)

    # Extract URL
    url = _extract_url(rules_text)

    # Find weather keywords
    keywords_found = _extract_keywords(text_lower)

    # Extract city name (simple heuristic)
    city_name = _extract_city(text_lower)

    # Compute confidence
    confidence = compute_confidence(icao, url, keywords_found, city_name)

    return StationResult(
        icao=icao,
        url=url,
        keywords_found=keywords_found,
        city_name=city_name,
        confidence=confidence,
    )


def _extract_icao(text: str) -> Optional[str]:
    """Extract ICAO code from text using regex."""
    # Look for 4-letter uppercase code starting with known prefix
    # Pattern: Known prefix + 3 more letters (usually uppercase)
    pattern = r'\b([K|P|C|U|E|L|W|R|Z|V|T|M|S][A-Z]{3})\b'

    matches = re.findall(pattern, text)
    if matches:
        # Return first match (typically the most prominent)
        return matches[0]

    return None


def _extract_url(text: str) -> Optional[str]:
    """Extract weather station URL from text."""
    # Look for HTTP(S) URLs containing weather-related domains
    url_pattern = r'https?://[^\s\)\"\'<>]*(wunderground|weather\.gov|noaa|nws)[^\s\)\"\'<>]*'
    matches = re.findall(url_pattern, text, re.IGNORECASE)

    if matches:
        return matches[0]

    return None


def _extract_keywords(text_lower: str) -> list[str]:
    """Extract weather keywords from text."""
    found = []
    for keyword in WEATHER_KEYWORDS:
        if keyword in text_lower:
            found.append(keyword)
    return found


def _extract_city(text_lower: str) -> Optional[str]:
    """Extract city name from text using known city keywords."""
    for city in CITY_ICAO_MAP.keys():
        if city in text_lower:
            return city

    return None


def compute_confidence(
    icao: Optional[str],
    url: Optional[str],
    keywords: list[str],
    city: Optional[str],
) -> float:
    """
    Compute confidence score for station identification.

    Scoring:
    - 3.0: URL + ICAO both found
    - 2.0: keyword match + map lookup (keyword found AND city maps to known ICAO)
    - 1.0: keyword only (weather keyword found but no ICAO/URL)
    - 0.5: city fallback (city name found, mapped to ICAO from config)
    - 0.0: no station identified

    Args:
        icao: Extracted ICAO code
        url: Extracted weather URL
        keywords: List of found weather keywords
        city: Extracted city name

    Returns:
        Confidence score (0.0-3.0)
    """
    # Both URL and ICAO found
    if url and icao:
        return 3.0

    # Keyword match + city map lookup
    if keywords and city and city in CITY_ICAO_MAP:
        return 2.0

    # Keyword only (no ICAO/URL)
    if keywords and not icao and not url:
        return 1.0

    # City fallback (city found but no keywords/ICAO/URL)
    if city and city in CITY_ICAO_MAP and not icao and not url:
        return 0.5

    # No station identified
    return 0.0


def validate_on_hash_change(
    rules_hash: str,
    cached_hash: Optional[str] = None,
) -> bool:
    """
    Check if rules have changed based on hash comparison.

    Used to determine if re-validation of station is needed.

    Args:
        rules_hash: Hash of current rules text
        cached_hash: Previously cached hash (if any)

    Returns:
        True if hash changed or no cached hash (needs re-validation)
        False if hash unchanged (cached result still valid)
    """
    if cached_hash is None:
        return True

    return rules_hash != cached_hash


def compute_rules_hash(rules_text: str) -> str:
    """
    Compute SHA256 hash of rules text for change detection.

    Args:
        rules_text: Raw rules text

    Returns:
        Hex digest of SHA256 hash
    """
    return hashlib.sha256(rules_text.encode()).hexdigest()


def wu_metar_sanity_check(
    station_icao: str,
    wu_daily_maxes: list[float],
    metar_daily_maxes: list[float],
) -> SanityResult:
    """
    Compare WU and METAR data for sanity check.

    Compares last 30 days of daily max temperatures.
    If average difference > 1.2°C, returns high-risk with EV boost.

    Args:
        station_icao: ICAO code of station
        wu_daily_maxes: List of WU daily max temperatures (°C)
        metar_daily_maxes: List of METAR daily max temperatures (°C)

    Returns:
        SanityResult with risk assessment and EV adjustment
    """
    # Ensure both lists are not empty
    if not wu_daily_maxes or not metar_daily_maxes:
        return SanityResult(
            avg_diff_c=0.0,
            risk_level='normal',
            min_theo_ev_boost=0.0,
            skip_market=False,
        )

    # Take last 30 days (or all if fewer)
    wu_sample = wu_daily_maxes[-30:]
    metar_sample = metar_daily_maxes[-30:]

    # Calculate pairwise differences
    # If lengths differ, only compare overlapping period
    min_len = min(len(wu_sample), len(metar_sample))
    diffs = []

    for i in range(min_len):
        diff = abs(wu_sample[i] - metar_sample[i])
        diffs.append(diff)

    if not diffs:
        return SanityResult(
            avg_diff_c=0.0,
            risk_level='normal',
            min_theo_ev_boost=0.0,
            skip_market=False,
        )

    avg_diff = sum(diffs) / len(diffs)

    # High risk if avg difference > 1.2°C
    if avg_diff > 1.2:
        return SanityResult(
            avg_diff_c=avg_diff,
            risk_level='high',
            min_theo_ev_boost=0.04,
            skip_market=False,
        )

    # Normal risk
    return SanityResult(
        avg_diff_c=avg_diff,
        risk_level='normal',
        min_theo_ev_boost=0.0,
        skip_market=False,
    )


def should_trade(station_result: StationResult, min_confidence: float = 2.0) -> bool:
    """
    Determine if market should be traded based on station confidence.

    No-trade if confidence < min_confidence threshold.

    Args:
        station_result: Result from parse_station()
        min_confidence: Minimum acceptable confidence (default 2.0)

    Returns:
        True if should trade, False if should skip
    """
    return station_result.confidence >= min_confidence
