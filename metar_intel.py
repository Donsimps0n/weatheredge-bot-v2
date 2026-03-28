"""
METAR Field Intelligence Agent

Extracts trading signals from wind, clouds, dewpoint, and pressure data.
Enriches temperature probability signals with meteorological adjustments.
"""

import logging
import math
import time
from typing import Optional, Dict, List, Any
import requests

logger = logging.getLogger(__name__)


class METARIntel:
    """Analyzes METAR data to extract trading signals and probability adjustments."""

    BASE_URL = "https://aviationweather.gov/api/data/metar"
    CACHE_TTL = 900  # 15 minutes

    def __init__(self, shared_state: Optional[Any] = None):
        """Initialize METAR intelligence agent.

        Args:
            shared_state: Optional SharedState for publishing enrichments
        """
        self.shared_state = shared_state
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._enrichments: Dict[str, Dict[str, Any]] = {}
        self._stats = {"api_calls": 0, "cache_hits": 0, "errors": 0}

    def parse_metar_json(self, metar_data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract all useful fields from JSON METAR response.

        Args:
            metar_data: Raw JSON from aviationweather.gov API

        Returns:
            Dict with parsed fields: temp, dewp, wdir, wspd, wgst, altim, visib, clouds
        """
        try:
            parsed = {
                "temp": metar_data.get("temp"),
                "dewp": metar_data.get("dewp"),
                "wdir": metar_data.get("wdir"),
                "wspd": metar_data.get("wspd"),
                "wgst": metar_data.get("wgst"),
                "altim": metar_data.get("altim"),
                "visib": metar_data.get("visib"),
                "clouds": metar_data.get("clouds", []),
            }
            return parsed
        except Exception as e:
            logger.error(f"Error parsing METAR data: {e}")
            self._stats["errors"] += 1
            return {}

    def _analyze_clouds(self, clouds: List[Dict[str, Any]]) -> float:
        """Analyze cloud cover and return temperature adjustment.

        Args:
            clouds: List of cloud layers with 'cover' (FEW/SCT/BKN/OVC) and 'base'

        Returns:
            Temperature adjustment in °F (positive = boost, negative = reduce)
        """
        if not clouds:
            return 0.0

        # Weight by lowest cloud layer (most impact on surface)
        lowest = min(clouds, key=lambda c: c.get("base", 999999))
        cover = lowest.get("cover", "").upper()

        if cover in ("FEW", "SCT"):
            return 1.5  # Strong solar heating
        elif cover == "BKN":
            return 0.0  # Moderate, neutral
        elif cover == "OVC":
            return -2.5  # Overcast suppresses heating
        return 0.0

    def _analyze_wind(self, wdir: Optional[int], wspd: Optional[int]) -> float:
        """Analyze wind advection and return temperature adjustment.

        Args:
            wdir: Wind direction in degrees (0-360)
            wspd: Wind speed in knots

        Returns:
            Temperature adjustment in °F
        """
        if wdir is None or wspd is None or wspd == 0:
            return 0.0

        # Warm advection: South/SW (150-240°)
        if 150 <= wdir <= 240:
            base_adj = min(3.0, wspd / 15.0 * 3.0)  # Max 3°F, scales with speed
            return base_adj

        # Cold advection: North/NE (330-60°)
        if wdir >= 330 or wdir <= 60:
            base_adj = min(3.0, wspd / 15.0 * 3.0)
            return -base_adj

        return 0.0

    def _analyze_dewpoint(self, temp: Optional[float], dewp: Optional[float]) -> float:
        """Analyze dewpoint and return overnight low adjustment.

        Args:
            temp: Current temperature in °F
            dewp: Dewpoint in °F

        Returns:
            Temperature adjustment in °F (for overnight low)
        """
        if temp is None or dewp is None:
            return 0.0

        dewpoint_depression = temp - dewp

        # High dewpoint limits overnight cooling
        if dewp > 65:
            return 2.0  # Boost overnight low probability

        # Low dewpoint allows deep radiational cooling
        if dewp < 40:
            return -2.0  # Reduce overnight low probability

        # Fog/cloud risk when depression < 5°F
        if dewpoint_depression < 5:
            return -1.0  # Slight reduction

        return 0.0

    def _get_metar_for_station(self, icao: str) -> Optional[Dict[str, Any]]:
        """Fetch and cache METAR data for a station.

        Args:
            icao: ICAO code (e.g., 'KJFK')

        Returns:
            Parsed METAR data or None on error
        """
        # Check cache
        if icao in self._cache:
            cached = self._cache[icao]
            if time.time() - cached["timestamp"] < self.CACHE_TTL:
                self._stats["cache_hits"] += 1
                return cached["data"]

        # Fetch from API
        try:
            self._stats["api_calls"] += 1
            resp = requests.get(f"{self.BASE_URL}?ids={icao}&format=json", timeout=5)
            resp.raise_for_status()

            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                metar = data[0]
            else:
                metar = data

            # Cache result
            self._cache[icao] = {
                "timestamp": time.time(),
                "data": metar
            }
            return metar
        except Exception as e:
            logger.error(f"Error fetching METAR for {icao}: {e}")
            self._stats["errors"] += 1
            return None

    def enrich_signals(self, signals: List[Dict[str, Any]], sentinel=None) -> List[Dict[str, Any]]:
        """Enrich signals with METAR-based probability adjustments.

        Args:
            signals: List of signal dicts with 'city', 'signal_type', 'our_prob'
            sentinel: Optional sentinel value (unused, for API compatibility)

        Returns:
            Enriched signals with metar_*_adj_f and metar_total_adj_f fields
        """
        enriched = []

        for signal in signals:
            try:
                city = signal.get("city", "")
                # Map city to ICAO code (simple mapping; expand as needed)
                icao = self._city_to_icao(city)

                if not icao:
                    enriched.append(signal)
                    continue

                # Fetch METAR
                metar_raw = self._get_metar_for_station(icao)
                if not metar_raw:
                    enriched.append(signal)
                    continue

                # Parse and analyze
                parsed = self.parse_metar_json(metar_raw)

                wind_adj = self._analyze_wind(parsed.get("wdir"), parsed.get("wspd"))
                cloud_adj = self._analyze_clouds(parsed.get("clouds"))
                dewpoint_adj = self._analyze_dewpoint(parsed.get("temp"), parsed.get("dewp"))

                total_adj = wind_adj + cloud_adj + dewpoint_adj

                # Enrich signal
                enriched_signal = signal.copy()
                enriched_signal["metar_wind_adj_f"] = round(wind_adj, 2)
                enriched_signal["metar_cloud_adj_f"] = round(cloud_adj, 2)
                enriched_signal["metar_dewpoint_adj_f"] = round(dewpoint_adj, 2)
                enriched_signal["metar_total_adj_f"] = round(total_adj, 2)

                # Update summary
                if city not in self._enrichments:
                    self._enrichments[city] = {}
                self._enrichments[city] = {
                    "icao": icao,
                    "wind_adj": wind_adj,
                    "cloud_adj": cloud_adj,
                    "dewpoint_adj": dewpoint_adj,
                    "total_adj": total_adj,
                    "timestamp": time.time()
                }

                enriched.append(enriched_signal)
            except Exception as e:
                logger.error(f"Error enriching signal for {signal.get('city')}: {e}")
                self._stats["errors"] += 1
                enriched.append(signal)

        # Publish to shared state
        if self.shared_state:
            self.shared_state.publish("metar_intel", "enrichments", self.get_enrichment_summary())

        return enriched

    def get_enrichment_summary(self) -> Dict[str, Any]:
        """Get summary of all current enrichments by city.

        Returns:
            Dict mapping city to enrichment data
        """
        return self._enrichments.copy()

    def get_stats(self) -> Dict[str, int]:
        """Get API usage statistics.

        Returns:
            Dict with api_calls, cache_hits, errors
        """
        return self._stats.copy()

    @staticmethod
    def _city_to_icao(city: str) -> Optional[str]:
        """Simple mapping from city name to ICAO code.

        Args:
            city: City name

        Returns:
            ICAO code or None if not found
        """
        mapping = {
            "new york": "KJFK",
            "los angeles": "KLAX",
            "chicago": "KORD",
            "dallas": "KDFW",
            "denver": "KDEN",
            "phoenix": "KPHX",
            "seattle": "KSEA",
            "boston": "KBOS",
            "miami": "KMIA",
            "sf": "KSFO",
            "san francisco": "KSFO",
        }
        return mapping.get(city.lower())
