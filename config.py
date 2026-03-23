"""
Polymarket Temperature Trading Bot Configuration Module

Central configuration with no global mutable state.
All settings use dataclasses for immutability and dependency injection.
"""

import os
from dataclasses import dataclass, field
from typing import List, Dict, Tuple


# ============================================================================
# CITIES: 58 Major World Cities with ICAO Codes and Coordinates
# ============================================================================

CITIES: List[Dict[str, any]] = [
    # North America - US
    {"city": "New York", "icao": "KJFK", "lat": 40.6413, "lon": -74.0060, "coastal": True, "country": "USA", "timezone": "America/New_York"},
    {"city": "Los Angeles", "icao": "KLAX", "lat": 33.9425, "lon": -118.4081, "coastal": True, "country": "USA", "timezone": "America/Los_Angeles"},
    {"city": "Chicago", "icao": "KORD", "lat": 41.8781, "lon": -87.6298, "coastal": False, "country": "USA", "timezone": "America/Chicago"},
    {"city": "Miami", "icao": "KMIA", "lat": 25.7617, "lon": -80.1918, "coastal": True, "country": "USA", "timezone": "America/New_York"},
    {"city": "Dallas", "icao": "KDFW", "lat": 32.8753, "lon": -97.0208, "coastal": False, "country": "USA", "timezone": "America/Chicago"},
    {"city": "Denver", "icao": "KDEN", "lat": 39.7392, "lon": -104.9903, "coastal": False, "country": "USA", "timezone": "America/Denver"},
    {"city": "Seattle", "icao": "KSEA", "lat": 47.6062, "lon": -122.3321, "coastal": True, "country": "USA", "timezone": "America/Los_Angeles"},
    {"city": "Boston", "icao": "KBOS", "lat": 42.3601, "lon": -71.0589, "coastal": True, "country": "USA", "timezone": "America/New_York"},
    {"city": "Phoenix", "icao": "KPHX", "lat": 33.4484, "lon": -112.0742, "coastal": False, "country": "USA", "timezone": "America/Phoenix"},
    {"city": "Minneapolis", "icao": "KMSP", "lat": 44.9537, "lon": -93.0900, "coastal": False, "country": "USA", "timezone": "America/Chicago"},
    {"city": "Las Vegas", "icao": "KLAS", "lat": 36.1699, "lon": -115.1398, "coastal": False, "country": "USA", "timezone": "America/Los_Angeles"},
    {"city": "San Francisco", "icao": "KSFO", "lat": 37.7749, "lon": -122.4194, "coastal": True, "country": "USA", "timezone": "America/Los_Angeles"},
    {"city": "Atlanta", "icao": "KATL", "lat": 33.7490, "lon": -84.3880, "coastal": False, "country": "USA", "timezone": "America/New_York"},
    {"city": "Houston", "icao": "KIAH", "lat": 29.7604, "lon": -95.3698, "coastal": True, "country": "USA", "timezone": "America/Chicago"},

    # North America - Canada
    {"city": "Toronto", "icao": "CYYZ", "lat": 43.6629, "lon": -79.3957, "coastal": False, "country": "Canada", "timezone": "America/Toronto"},
    {"city": "Vancouver", "icao": "CYVR", "lat": 49.2827, "lon": -123.1207, "coastal": True, "country": "Canada", "timezone": "America/Vancouver"},
    {"city": "Montreal", "icao": "CYUL", "lat": 45.5017, "lon": -73.5673, "coastal": False, "country": "Canada", "timezone": "America/Toronto"},

    # North America - Mexico
    {"city": "Mexico City", "icao": "MMMX", "lat": 19.4326, "lon": -99.1332, "coastal": False, "country": "Mexico", "timezone": "America/Mexico_City"},

    # Europe - UK & Ireland
    {"city": "London", "icao": "EGLL", "lat": 51.5074, "lon": -0.1278, "coastal": True, "country": "UK", "timezone": "Europe/London"},
    {"city": "Dublin", "icao": "EIDW", "lat": 53.3498, "lon": -6.2603, "coastal": True, "country": "Ireland", "timezone": "Europe/Dublin"},

    # Europe - Western
    {"city": "Paris", "icao": "LFPG", "lat": 48.8566, "lon": 2.3522, "coastal": False, "country": "France", "timezone": "Europe/Paris"},
    {"city": "Amsterdam", "icao": "EHAM", "lat": 52.3676, "lon": 4.9041, "coastal": True, "country": "Netherlands", "timezone": "Europe/Amsterdam"},
    {"city": "Berlin", "icao": "EDDF", "lat": 52.5200, "lon": 13.4050, "coastal": False, "country": "Germany", "timezone": "Europe/Berlin"},
    {"city": "Frankfurt", "icao": "EDDF", "lat": 50.1109, "lon": 8.6821, "coastal": False, "country": "Germany", "timezone": "Europe/Berlin"},

    # Europe - Southern
    {"city": "Madrid", "icao": "LEMD", "lat": 40.4168, "lon": -3.7038, "coastal": False, "country": "Spain", "timezone": "Europe/Madrid"},
    {"city": "Barcelona", "icao": "LEIB", "lat": 41.3851, "lon": 2.1734, "coastal": True, "country": "Spain", "timezone": "Europe/Madrid"},
    {"city": "Rome", "icao": "LIRF", "lat": 41.9028, "lon": 12.4964, "coastal": True, "country": "Italy", "timezone": "Europe/Rome"},
    {"city": "Milan", "icao": "LIML", "lat": 45.4642, "lon": 9.1900, "coastal": False, "country": "Italy", "timezone": "Europe/Rome"},

    # Europe - Southern/Mediterranean
    {"city": "Athens", "icao": "LGAV", "lat": 37.9838, "lon": 23.7275, "coastal": True, "country": "Greece", "timezone": "Europe/Athens"},
    {"city": "Lisbon", "icao": "LPPT", "lat": 38.7223, "lon": -9.1393, "coastal": True, "country": "Portugal", "timezone": "Europe/Lisbon"},

    # Europe - Northern/Eastern
    {"city": "Stockholm", "icao": "ESSA", "lat": 59.3293, "lon": 18.0686, "coastal": True, "country": "Sweden", "timezone": "Europe/Stockholm"},
    {"city": "Copenhagen", "icao": "EKCH", "lat": 55.6761, "lon": 12.5683, "coastal": True, "country": "Denmark", "timezone": "Europe/Copenhagen"},
    {"city": "Moscow", "icao": "UUWW", "lat": 55.7558, "lon": 37.6173, "coastal": False, "country": "Russia", "timezone": "Europe/Moscow"},
    {"city": "Warsaw", "icao": "EPWA", "lat": 52.2297, "lon": 21.0122, "coastal": False, "country": "Poland", "timezone": "Europe/Warsaw"},

    # Middle East
    {"city": "Dubai", "icao": "OMDB", "lat": 25.2048, "lon": 55.2708, "coastal": True, "country": "UAE", "timezone": "Asia/Dubai"},
    {"city": "Istanbul", "icao": "LTAC", "lat": 41.0082, "lon": 28.9784, "coastal": True, "country": "Turkey", "timezone": "Europe/Istanbul"},
    {"city": "Tel Aviv", "icao": "LLBG", "lat": 32.0853, "lon": 34.7818, "coastal": True, "country": "Israel", "timezone": "Asia/Jerusalem"},

    # Asia - South
    {"city": "Mumbai", "icao": "VABB", "lat": 19.0760, "lon": 72.8777, "coastal": True, "country": "India", "timezone": "Asia/Kolkata"},
    {"city": "Delhi", "icao": "VIDP", "lat": 28.5921, "lon": 77.2829, "coastal": False, "country": "India", "timezone": "Asia/Kolkata"},
    {"city": "Bangalore", "icao": "VOBL", "lat": 12.9716, "lon": 77.5946, "coastal": False, "country": "India", "timezone": "Asia/Kolkata"},

    # Asia - Southeast
    {"city": "Singapore", "icao": "WSSS", "lat": 1.3521, "lon": 103.8198, "coastal": True, "country": "Singapore", "timezone": "Asia/Singapore"},
    {"city": "Bangkok", "icao": "VTBS", "lat": 13.7563, "lon": 100.5018, "coastal": True, "country": "Thailand", "timezone": "Asia/Bangkok"},
    {"city": "Hong Kong", "icao": "VHHH", "lat": 22.3193, "lon": 114.1694, "coastal": True, "country": "Hong Kong", "timezone": "Asia/Hong_Kong"},

    # Asia - East
    {"city": "Tokyo", "icao": "RJTT", "lat": 35.6762, "lon": 139.6503, "coastal": True, "country": "Japan", "timezone": "Asia/Tokyo"},
    {"city": "Seoul", "icao": "RKSI", "lat": 37.5665, "lon": 126.9780, "coastal": True, "country": "South Korea", "timezone": "Asia/Seoul"},
    {"city": "Shanghai", "icao": "ZSPD", "lat": 31.2304, "lon": 121.4737, "coastal": True, "country": "China", "timezone": "Asia/Shanghai"},
    {"city": "Beijing", "icao": "ZBAA", "lat": 39.9042, "lon": 116.4074, "coastal": False, "country": "China", "timezone": "Asia/Shanghai"},

    # Asia - Central
    {"city": "Istanbul", "icao": "LTAC", "lat": 41.0082, "lon": 28.9784, "coastal": True, "country": "Turkey", "timezone": "Europe/Istanbul"},

    # Oceania
    {"city": "Sydney", "icao": "YSSY", "lat": -33.8688, "lon": 151.2093, "coastal": True, "country": "Australia", "timezone": "Australia/Sydney"},
    {"city": "Melbourne", "icao": "YMML", "lat": -37.8136, "lon": 144.9631, "coastal": True, "country": "Australia", "timezone": "Australia/Melbourne"},
    {"city": "Auckland", "icao": "NZAA", "lat": -37.0082, "lon": 174.7850, "coastal": True, "country": "New Zealand", "timezone": "Pacific/Auckland"},

    # South America
    {"city": "São Paulo", "icao": "SBGR", "lat": -23.5505, "lon": -46.6333, "coastal": False, "country": "Brazil", "timezone": "America/Sao_Paulo"},
    {"city": "Rio de Janeiro", "icao": "SBGL", "lat": -22.9068, "lon": -43.1729, "coastal": True, "country": "Brazil", "timezone": "America/Sao_Paulo"},
    {"city": "Buenos Aires", "icao": "SAEZ", "lat": -34.6037, "lon": -58.3816, "coastal": True, "country": "Argentina", "timezone": "America/Argentina/Buenos_Aires"},
    {"city": "Santiago", "icao": "SCEL", "lat": -33.4489, "lon": -70.6693, "coastal": False, "country": "Chile", "timezone": "America/Santiago"},

    # Africa
    {"city": "Cairo", "icao": "HECA", "lat": 30.0444, "lon": 31.2357, "coastal": True, "country": "Egypt", "timezone": "Africa/Cairo"},
    {"city": "Johannesburg", "icao": "FAOR", "lat": -26.2023, "lon": 28.0436, "coastal": False, "country": "South Africa", "timezone": "Africa/Johannesburg"},
    {"city": "Lagos", "icao": "DNAA", "lat": 6.5244, "lon": 3.3792, "coastal": True, "country": "Nigeria", "timezone": "Africa/Lagos"},
]


# ============================================================================
# TRADING THRESHOLDS (Named Constants)
# ============================================================================

MIN_THEO_EV_BASE = 0.10
THEO_EV_FLATTEN_THRESHOLD = 0.10
GATE_12H_MIN_EV = 0.14
GATE_6H_MIN_EV = 0.20
NEAR_PEAK_EV_BOOST = 0.02
POST_PEAK_MIN_EV = 0.18
POST_PEAK_RAW_EDGE_MIN = 0.12
POST_PEAK_OBS_STALE_HOURS = 2
KELLY_SIZE_CAP_NEAR_PEAK = 0.15
CROSS_MARKET_DELTA_Z_THRESHOLD = 2.8
CROSS_MARKET_EV_BOOST = 0.03
CROSS_MARKET_MIN_CORR = 0.90
LEAKAGE_RATCHET_PER_HALF_BPS = 0.01


# ============================================================================
# NOWCASTING PARAMETERS
# ============================================================================

HALF_LIFE_NEAR_PEAK = 2.0
HALF_LIFE_DEFAULT = 4.0
HALF_LIFE_COASTAL = 3.0
AR1_RHO_DEFAULT = 0.78
AR1_RHO_COASTAL = 0.70
MONTE_CARLO_SAMPLES = 5000
OBS_ANOMALY_TEMP_THRESHOLD = 6.0  # °C
OBS_ANOMALY_TIME_THRESHOLD = 45  # minutes
OBS_SIGMA_WIDEN_FACTOR = 1.4


# ============================================================================
# REGIME CLASSIFIER THRESHOLDS
# ============================================================================

FRONT_SKEW = -1.5
FRONT_SIGMA_MULT = 1.5
MARINE_SKEW = -1.2
CONVECTIVE_P_STORM = 0.32
CONVECTIVE_MAX_CAP_OFFSET = 1.5
CLEAR_WARM_BIAS_MIN = 0.8
CLEAR_WARM_BIAS_MAX = 1.5
CLEAR_SIGMA_MULT = 0.8


# ============================================================================
# EXECUTION PARAMETERS
# ============================================================================

DEFAULT_TIME_IN_BOOK_S = 60
MAX_REPRICES = 3
SIZE_CAP_DEFAULT_PCT = 0.20  # of top-of-book depth
SIZE_CAP_HIGH_DEPTH_PCT = 0.35
HIGH_DEPTH_THRESHOLD = 30000
LADDER_BINS_NEAR_PEAK = (4, 5)
LADDER_BINS_DEFAULT = (3, 4)


# ============================================================================
# ALERT THRESHOLDS
# ============================================================================

ALERT_DECAY_PCT = 0.25
ALERT_SPREAD_PAID_PCT = 0.10
ALERT_TIME_TO_FIRST_FILL_S = 90
ALERT_FILL_COMPLETION_60S = 0.40
ALERT_ROLLING_LEAKAGE_BPS = 8


# ============================================================================
# SANITY CHECKS
# ============================================================================

WU_METAR_MAX_DIFF_C = 1.2
WU_METAR_HIGH_RISK_EV_BOOST = 0.04
KDE_BOOTSTRAP_RESAMPLES = 200


# ============================================================================
# DIURNAL PEAK WINDOWS (Function of Latitude)
# ============================================================================

HIGH_LAT_THRESHOLD = 50
MID_LAT_THRESHOLD = 30
HIGH_LAT_PEAK = (13, 16)
MID_LAT_PEAK = (14, 17)
LOW_LAT_PEAK = (15, 18)
COASTAL_PEAK_SHIFT = 1


# ============================================================================
# SCHEDULER CONFIGURATION
# ============================================================================

BURST_TRIGGERS_HARD = ["00Z", "12Z"]
BURST_TRIGGERS_SECONDARY = ["06Z", "18Z"]
HRRR_POLL_INTERVAL_MIN = 60


# ============================================================================
# API CONFIGURATION DATACLASS
# ============================================================================

@dataclass
class APIConfig:
    """API configuration with dependency injection pattern."""

    polymarket_api_url: str = "https://api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    wu_api_key: str = field(default_factory=lambda: os.environ.get("WU_API_KEY", ""))
    telegram_bot_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    wallet_address: str = "0xA3F0466e37837dEF4588564B9c04100de9Df9136"
    paper_mode: bool = True
    db_path: str = "ledger.db"


# ============================================================================
# FACTORY FUNCTION
# ============================================================================

def create_default_config() -> APIConfig:
    """
    Factory function to create a default API configuration.

    Returns:
        APIConfig: Configuration object with defaults and environment variables.
    """
    return APIConfig()
