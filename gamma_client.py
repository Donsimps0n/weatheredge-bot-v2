"""
gamma_client.py

Polymarket Gamma API client for temperature market discovery.

Hits https://gamma-api.polymarket.com/events, filters to open temperature
markets from event sub-markets, parses each market's rules text to extract station ICAO +
resolution time, matches against the 58-city CITIES list, and returns
dicts in the exact shape that TradingScheduler.run_cycle() expects.

Cache: results are held in memory for CACHE_TTL_SECONDS (default 1800 s)
so the scheduler loop can call get_markets() every 15 minutes cheaply.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

from config import CITIES, create_default_config
from station_parser import parse_station

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GAMMA_BASE_URL  = "https://gamma-api.polymarket.com"
EVENTS_PATH     = "/events"
PAGE_LIMIT      = 100
CACHE_TTL_SECONDS = 1800   # 30 min

# Keywords that identify a temperature market in the question text
TEMP_KEYWORDS = [
    "temperature", "high temp", "low temp", "high of", "low of",
    "degrees", "fahrenheit", "celsius", "°f", "°c",
    "highest temperature", "lowest temperature",
    "forecast high", "forecast low",
]

# Outcome label normalisation → category token
CATEGORY_MAP = {
    # High-temp bins look like "Above 90°F", "80–89°F", etc.
    "high": "high_temp",
    "low":  "low_temp",
    "max":  "high_temp",
    "min":  "low_temp",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RawMarket:
    """Parsed fields from a single Gamma API market response."""
    market_id:       str
    slug:            str
    question:        str
    rules:           str
    end_date_iso:    str
    tokens:          List[Dict]       # [{token_id, outcome, price}, ...]
    active:          bool
    closed:          bool
    volume:          float


@dataclass
class DiscoveredMarket:
    """Enriched market dict ready for TradingScheduler.run_cycle()."""
    slug:            str
    market_id:       str
    station:         str              # ICAO
    city:            str
    country:         str
    category:        str              # "high_temp" | "low_temp"
    confidence:      int              # 0–3
    timezone:        str
    lat:             float
    lon:             float
    coastal:         bool
    resolution_time: datetime
    prices:          Dict[str, float] # token_id → current price (0–1)
    tokens:          List[Dict]
    wu_data:         Optional[Dict]   = None
    metar_data:      Optional[Dict]   = None
    regime_data:     Dict             = field(default_factory=dict)
    forecast_probs:  Dict             = field(default_factory=dict)
    book_snapshot:   Optional[Dict]   = None


# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_cache_ts:      float             = 0.0
_cache_markets: List[Dict]        = []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_temp_market(question: str) -> bool:
    """Return True if the question text mentions temperature."""
    q = question.lower()
    return any(kw in q for kw in TEMP_KEYWORDS)


def _infer_category(question: str, rules: str) -> str:
    """
    Return 'high_temp' or 'low_temp' based on question/rules text.
    Defaults to 'high_temp' when ambiguous.
    """
    text = (question + " " + rules).lower()
    if "low temp" in text or "low of" in text or "minimum" in text or "overnight low" in text:
        return "low_temp"
    return "high_temp"


def _match_city(icao: str) -> Optional[Dict]:
    """Return the CITIES entry whose icao matches, or None."""
    for city in CITIES:
        if city["icao"].upper() == icao.upper():
            return city
    return None


def _parse_resolution_time(end_date_iso: str) -> Optional[datetime]:
    """Parse ISO-8601 string → UTC datetime. Returns None on failure."""
    try:
        # Strip trailing Z and reattach +00:00 for fromisoformat compat
        s = end_date_iso.replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception as exc:
        log.warning("Could not parse end_date_iso=%r: %s", end_date_iso, exc)
        return None


def _extract_prices(tokens: List[Dict]) -> Dict[str, float]:
    """
    Build {token_id: price} from the tokens list.

    Gamma returns prices as strings in outcomePrices or inside each token.
    We normalise to float in [0, 1].
    """
    prices: Dict[str, float] = {}
    for tok in tokens:
        tid = tok.get("token_id") or tok.get("tokenId", "")
        # price field may be 'price', 'outcome_price', or numeric string
        raw = tok.get("price") or tok.get("outcomesPrice") or "0.5"
        try:
            prices[tid] = float(raw)
        except (TypeError, ValueError):
            prices[tid] = 0.5
    return prices


def _fetch_events_page(session: requests.Session, offset: int, tag: Optional[str]) -> List[Dict]:
    """Fetch one page of events from the Gamma API and extract sub-markets."""
    params: Dict = {
        "active": "true",
        "closed": "false",
        "limit":  PAGE_LIMIT,
        "offset": offset,
    }
    if tag:
        params["tag_slug"] = tag

    resp = session.get(
        GAMMA_BASE_URL + EVENTS_PATH,
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    # Events API returns a list of event objects, each with a "markets" array
    if not isinstance(data, list):
        data = data.get("events", data.get("data", []))

    # Flatten: extract all sub-markets from all events
    all_markets = []
    for event in data:
        sub_markets = event.get("markets", [])
        for m in sub_markets:
            # Carry event-level info into each sub-market for context
            if "description" not in m or not m["description"]:
                m["description"] = event.get("description", "")
            all_markets.append(m)

    return all_markets


def _raw_to_discovered(raw: RawMarket) -> Optional[DiscoveredMarket]:
    """
    Try to enrich a RawMarket into a DiscoveredMarket.

    Returns None if:
    - station_parser fails or confidence < 1
    - city not in our 58-city list
    - resolution_time cannot be parsed or is in the past
    """
    rules_text = raw.rules or raw.question

    # --- parse station ---
    station_result = parse_station(rules_text)
    if not station_result.icao or station_result.confidence < 1:
        log.debug("skip %s: no ICAO or low confidence (%s)",
                  raw.slug, station_result.confidence)
        return None

    # --- match city ---
    city_entry = _match_city(station_result.icao)
    if city_entry is None:
        log.debug("skip %s: ICAO %s not in 58-city list",
                  raw.slug, station_result.icao)
        return None

    # --- resolution time ---
    res_time = _parse_resolution_time(raw.end_date_iso)
    if res_time is None:
        return None
    now_utc = datetime.now(timezone.utc)
    if res_time <= now_utc:
        log.debug("skip %s: resolution time %s is in the past", raw.slug, res_time)
        return None

    # --- category ---
    category = _infer_category(raw.question, raw.rules)

    # --- prices ---
    prices = _extract_prices(raw.tokens)

    return DiscoveredMarket(
        slug=raw.slug,
        market_id=raw.market_id,
        station=station_result.icao,
        city=city_entry["city"],
        country=city_entry["country"],
        category=category,
        confidence=int(station_result.confidence),
        timezone=city_entry["timezone"],
        lat=city_entry["lat"],
        lon=city_entry["lon"],
        coastal=city_entry["coastal"],
        resolution_time=res_time,
        prices=prices,
        tokens=raw.tokens,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_open_temp_markets(
    tag: Optional[str] = "weather",
    max_pages: int = 10,
) -> List[DiscoveredMarket]:
    """
    Hit the Gamma Events API and return all open temperature markets that:
    - Match a temperature keyword in the question or event title
    - Have an ICAO station in our 58-city list
    - Have a future resolution time

    Markets on Polymarket are grouped under events. The /events endpoint
    with tag_slug=weather returns weather events, each containing sub-markets
    with individual temperature questions.

    Args:
        tag:       Gamma tag_slug filter (default 'weather'). Pass None to skip.
        max_pages: Hard cap on API pages to fetch.

    Returns:
        List of DiscoveredMarket objects ready for run_cycle().
    """
    session = requests.Session()
    session.headers["User-Agent"] = "weatheredge-bot/2.0"

    discovered: List[DiscoveredMarket] = []
    seen_slugs: set = set()

    for page in range(max_pages):
        offset = page * PAGE_LIMIT
        try:
            raw_list = _fetch_events_page(session, offset, tag)
        except requests.RequestException as exc:
            log.error("Gamma Events API error at offset=%d: %s", offset, exc)
            break

        if not raw_list:
            log.debug("Gamma Events API: empty page at offset=%d, stopping", offset)
            break

        for item in raw_list:
            slug = item.get("slug") or item.get("conditionId", "")
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)

            question = item.get("question", "")
            if not _is_temp_market(question):
                continue

            # Parse tokens — Gamma nests them differently across versions
            tokens = item.get("tokens", [])
            if not tokens:
                # Some Gamma responses put token IDs in clobTokenIds
                clob_ids = item.get("clobTokenIds", [])
                outcomes  = item.get("outcomes", [])
                prices_raw = item.get("outcomePrices", [])
                # clobTokenIds and outcomes may be JSON strings
                if isinstance(clob_ids, str):
                    try:
                        import json as _json
                        clob_ids = _json.loads(clob_ids)
                    except Exception:
                        clob_ids = []
                if isinstance(outcomes, str):
                    try:
                        import json as _json
                        outcomes = _json.loads(outcomes)
                    except Exception:
                        outcomes = []
                if isinstance(prices_raw, str):
                    try:
                        import json as _json
                        prices_raw = _json.loads(prices_raw)
                    except Exception:
                        prices_raw = []
                tokens = [
                    {
                        "token_id": tid,
                        "outcome":  outcomes[i] if i < len(outcomes) else "?",
                        "price":    prices_raw[i] if i < len(prices_raw) else "0.5",
                    }
                    for i, tid in enumerate(clob_ids)
                ]

            raw = RawMarket(
                market_id=item.get("conditionId", item.get("id", slug)),
                slug=slug,
                question=question,
                rules=item.get("description", ""),
                end_date_iso=item.get("endDateIso", item.get("endDate", "")),
                tokens=tokens,
                active=bool(item.get("active", True)),
                closed=bool(item.get("closed", False)),
                volume=float(item.get("volumeNum", item.get("volume", 0)) or 0),
            )

            dm = _raw_to_discovered(raw)
            if dm is not None:
                discovered.append(dm)

        log.info("Gamma Events page %d: %d sub-markets, %d discovered so far",
                 page, len(raw_list), len(discovered))

    log.info("fetch_open_temp_markets: %d markets discovered from events API", len(discovered))
    return discovered


def as_cycle_dict(dm: DiscoveredMarket) -> Dict:
    """
    Convert a DiscoveredMarket to the flat dict shape
    that TradingScheduler.run_cycle() iterates over.
    """
    return {
        "slug":            dm.slug,
        "market_id":       dm.market_id,
        "station":         dm.station,
        "city":            dm.city,
        "country":         dm.country,
        "category":        dm.category,
        "confidence":      dm.confidence,
        "timezone":        dm.timezone,
        "lat":             dm.lat,
        "lon":             dm.lon,
        "coastal":         dm.coastal,
        "resolution_time": dm.resolution_time,
        "prices":          dm.prices,
        "tokens":          dm.tokens,
        "wu_data":         dm.wu_data,
        "metar_data":      dm.metar_data,
        "regime_data":     dm.regime_data,
        "forecast_probs":  dm.forecast_probs,
        "book_snapshot":   dm.book_snapshot,
    }


def get_markets(tag: Optional[str] = "weather") -> List[Dict]:
    """
    Cached market discovery. Returns cached result if younger than
    CACHE_TTL_SECONDS, otherwise re-fetches from Gamma API.

    This is the function called by TradingScheduler.schedule_loop().

    Returns:
        List of cycle-ready market dicts.
    """
    global _cache_ts, _cache_markets

    age = time.monotonic() - _cache_ts
    if age < CACHE_TTL_SECONDS and _cache_markets:
        log.debug("get_markets: cache hit (age=%.0fs, n=%d)", age, len(_cache_markets))
        return _cache_markets

    log.info("get_markets: refreshing from Gamma API (cache age=%.0fs)", age)
    discovered = fetch_open_temp_markets(tag=tag)
    _cache_markets = [as_cycle_dict(dm) for dm in discovered]
    _cache_ts      = time.monotonic()

    log.info("get_markets: cached %d markets", len(_cache_markets))
    return _cache_markets


def invalidate_cache() -> None:
    """Force the next get_markets() call to re-fetch from Gamma API."""
    global _cache_ts
    _cache_ts = 0.0


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    markets = get_markets()
    print(f"\n=== {len(markets)} temperature markets discovered ===\n")
    for m in markets:
        res = m["resolution_time"].strftime("%Y-%m-%d %H:%M UTC")
        print(f"  {m['slug'][:55]:<55}  {m['station']}  {m['city']:<18}  res={res}")
