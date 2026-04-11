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
    # ══════════════════════════════════════════════════════════════════════
    # ALL coordinates are AIRPORT coordinates matching the Polymarket
    # settlement station. NO city-centre coords. Verified 2026-04-05.
    # ══════════════════════════════════════════════════════════════════════

    # North America - US
    {"city": "New York", "icao": "KLGA", "lat": 40.7772, "lon": -73.8726, "coastal": True, "country": "USA", "timezone": "America/New_York"},  # LaGuardia — Polymarket settlement (NOT KJFK)
    {"city": "Los Angeles", "icao": "KLAX", "lat": 33.9425, "lon": -118.4081, "coastal": True, "country": "USA", "timezone": "America/Los_Angeles"},
    {"city": "Chicago", "icao": "KORD", "lat": 41.9742, "lon": -87.9073, "coastal": False, "country": "USA", "timezone": "America/Chicago"},
    {"city": "Miami", "icao": "KMIA", "lat": 25.7932, "lon": -80.2906, "coastal": True, "country": "USA", "timezone": "America/New_York"},
    {"city": "Dallas", "icao": "KDAL", "lat": 32.8471, "lon": -96.8518, "coastal": False, "country": "USA", "timezone": "America/Chicago"},  # Love Field — Polymarket settlement (NOT KDFW)
    {"city": "Denver", "icao": "KBKF", "lat": 39.7170, "lon": -104.7519, "coastal": False, "country": "USA", "timezone": "America/Denver"},  # Buckley SFB — Polymarket settlement (NOT KDEN)
    {"city": "Seattle", "icao": "KSEA", "lat": 47.4502, "lon": -122.3088, "coastal": True, "country": "USA", "timezone": "America/Los_Angeles"},
    {"city": "Boston", "icao": "KBOS", "lat": 42.3656, "lon": -71.0096, "coastal": True, "country": "USA", "timezone": "America/New_York"},
    {"city": "Phoenix", "icao": "KPHX", "lat": 33.4373, "lon": -112.0078, "coastal": False, "country": "USA", "timezone": "America/Phoenix"},
    {"city": "Minneapolis", "icao": "KMSP", "lat": 44.8848, "lon": -93.2223, "coastal": False, "country": "USA", "timezone": "America/Chicago"},
    {"city": "Las Vegas", "icao": "KLAS", "lat": 36.0840, "lon": -115.1537, "coastal": False, "country": "USA", "timezone": "America/Los_Angeles"},
    {"city": "San Francisco", "icao": "KSFO", "lat": 37.6213, "lon": -122.3790, "coastal": True, "country": "USA", "timezone": "America/Los_Angeles"},
    {"city": "Atlanta", "icao": "KATL", "lat": 33.6407, "lon": -84.4277, "coastal": False, "country": "USA", "timezone": "America/New_York"},
    {"city": "Houston", "icao": "KHOU", "lat": 29.6454, "lon": -95.2789, "coastal": True, "country": "USA", "timezone": "America/Chicago"},  # Hobby — Polymarket settlement (NOT KIAH)

    # North America - Canada
    {"city": "Toronto", "icao": "CYYZ", "lat": 43.6772, "lon": -79.6306, "coastal": False, "country": "Canada", "timezone": "America/Toronto"},
    {"city": "Vancouver", "icao": "CYVR", "lat": 49.1967, "lon": -123.1815, "coastal": True, "country": "Canada", "timezone": "America/Vancouver"},
    {"city": "Montreal", "icao": "CYUL", "lat": 45.4706, "lon": -73.7408, "coastal": False, "country": "Canada", "timezone": "America/Toronto"},

    # North America - Mexico
    {"city": "Mexico City", "icao": "MMMX", "lat": 19.4363, "lon": -99.0721, "coastal": False, "country": "Mexico", "timezone": "America/Mexico_City"},

    # Europe - UK & Ireland
    {"city": "London", "icao": "EGLC", "lat": 51.5048, "lon": 0.0495, "coastal": True, "country": "UK", "timezone": "Europe/London"},  # London City Airport — Polymarket settlement (NOT EGLL)
    {"city": "Dublin", "icao": "EIDW", "lat": 53.4264, "lon": -6.2499, "coastal": True, "country": "Ireland", "timezone": "Europe/Dublin"},

    # Europe - Western
    {"city": "Paris", "icao": "LFPG", "lat": 49.0097, "lon": 2.5478, "coastal": False, "country": "France", "timezone": "Europe/Paris"},
    {"city": "Amsterdam", "icao": "EHAM", "lat": 52.3105, "lon": 4.7683, "coastal": True, "country": "Netherlands", "timezone": "Europe/Amsterdam"},
    {"city": "Berlin", "icao": "EDDB", "lat": 52.3667, "lon": 13.5033, "coastal": False, "country": "Germany", "timezone": "Europe/Berlin"},
    {"city": "Frankfurt", "icao": "EDDF", "lat": 50.0379, "lon": 8.5622, "coastal": False, "country": "Germany", "timezone": "Europe/Berlin"},
    {"city": "Munich", "icao": "EDDM", "lat": 48.3537, "lon": 11.7750, "coastal": False, "country": "Germany", "timezone": "Europe/Berlin"},

    # Europe - Southern
    {"city": "Madrid", "icao": "LEMD", "lat": 40.4983, "lon": -3.5676, "coastal": False, "country": "Spain", "timezone": "Europe/Madrid"},
    {"city": "Barcelona", "icao": "LEBL", "lat": 41.2971, "lon": 2.0785, "coastal": True, "country": "Spain", "timezone": "Europe/Madrid"},  # El Prat (was LEIB which is Ibiza!)
    {"city": "Rome", "icao": "LIRF", "lat": 41.8003, "lon": 12.2389, "coastal": True, "country": "Italy", "timezone": "Europe/Rome"},
    {"city": "Milan", "icao": "LIMC", "lat": 45.6306, "lon": 8.7231, "coastal": False, "country": "Italy", "timezone": "Europe/Rome"},  # Malpensa — Polymarket settlement (NOT LIML)

    # Europe - Southern/Mediterranean
    {"city": "Athens", "icao": "LGAV", "lat": 37.9364, "lon": 23.9445, "coastal": True, "country": "Greece", "timezone": "Europe/Athens"},
    {"city": "Lisbon", "icao": "LPPT", "lat": 38.7756, "lon": -9.1354, "coastal": True, "country": "Portugal", "timezone": "Europe/Lisbon"},

    # Europe - Northern/Eastern
    {"city": "Helsinki", "icao": "EFHK", "lat": 60.3172, "lon": 24.9633, "coastal": True, "country": "Finland", "timezone": "Europe/Helsinki"},
    {"city": "Stockholm", "icao": "ESSA", "lat": 59.6519, "lon": 17.9186, "coastal": True, "country": "Sweden", "timezone": "Europe/Stockholm"},
    {"city": "Copenhagen", "icao": "EKCH", "lat": 55.6180, "lon": 12.6560, "coastal": True, "country": "Denmark", "timezone": "Europe/Copenhagen"},
    {"city": "Moscow", "icao": "UUWW", "lat": 55.5915, "lon": 37.2615, "coastal": False, "country": "Russia", "timezone": "Europe/Moscow"},
    {"city": "Warsaw", "icao": "EPWA", "lat": 52.1657, "lon": 20.9671, "coastal": False, "country": "Poland", "timezone": "Europe/Warsaw"},

    # Middle East
    {"city": "Dubai", "icao": "OMDB", "lat": 25.2532, "lon": 55.3657, "coastal": True, "country": "UAE", "timezone": "Asia/Dubai"},
    {"city": "Istanbul", "icao": "LTAC", "lat": 39.9497, "lon": 32.6883, "coastal": True, "country": "Turkey", "timezone": "Europe/Istanbul"},  # Esenboga
    {"city": "Tel Aviv", "icao": "LLBG", "lat": 32.0114, "lon": 34.8867, "coastal": True, "country": "Israel", "timezone": "Asia/Jerusalem"},

    # Asia - South
    {"city": "Mumbai", "icao": "VABB", "lat": 19.0896, "lon": 72.8656, "coastal": True, "country": "India", "timezone": "Asia/Kolkata"},
    {"city": "Delhi", "icao": "VIDP", "lat": 28.5665, "lon": 77.1031, "coastal": False, "country": "India", "timezone": "Asia/Kolkata"},
    {"city": "Bangalore", "icao": "VOBL", "lat": 13.1979, "lon": 77.7063, "coastal": False, "country": "India", "timezone": "Asia/Kolkata"},
    {"city": "Lucknow", "icao": "VILK", "lat": 26.7606, "lon": 80.8893, "coastal": False, "country": "India", "timezone": "Asia/Kolkata"},

    # Asia - Southeast
    {"city": "Singapore", "icao": "WSSS", "lat": 1.3592, "lon": 103.9894, "coastal": True, "country": "Singapore", "timezone": "Asia/Singapore"},
    {"city": "Bangkok", "icao": "VTBS", "lat": 13.6811, "lon": 100.7475, "coastal": True, "country": "Thailand", "timezone": "Asia/Bangkok"},
    {"city": "Hong Kong", "icao": "VHHH", "lat": 22.3080, "lon": 113.9185, "coastal": True, "country": "Hong Kong", "timezone": "Asia/Hong_Kong"},
    {"city": "Jakarta", "icao": "WIHH", "lat": -6.2666, "lon": 106.8905, "coastal": True, "country": "Indonesia", "timezone": "Asia/Jakarta"},  # Halim Perdanakusuma
    {"city": "Kuala Lumpur", "icao": "WMKK", "lat": 2.7456, "lon": 101.7099, "coastal": True, "country": "Malaysia", "timezone": "Asia/Kuala_Lumpur"},

    # Asia - East
    {"city": "Tokyo", "icao": "RJTT", "lat": 35.5494, "lon": 139.7798, "coastal": True, "country": "Japan", "timezone": "Asia/Tokyo"},
    {"city": "Seoul", "icao": "RKSI", "lat": 37.4602, "lon": 126.4407, "coastal": True, "country": "South Korea", "timezone": "Asia/Seoul"},
    {"city": "Busan", "icao": "RKPK", "lat": 35.1795, "lon": 128.9382, "coastal": True, "country": "South Korea", "timezone": "Asia/Seoul"},
    {"city": "Shanghai", "icao": "ZSPD", "lat": 31.1443, "lon": 121.8083, "coastal": True, "country": "China", "timezone": "Asia/Shanghai"},
    {"city": "Beijing", "icao": "ZBAA", "lat": 40.0799, "lon": 116.6031, "coastal": False, "country": "China", "timezone": "Asia/Shanghai"},
    {"city": "Chengdu", "icao": "ZUUU", "lat": 30.5785, "lon": 103.9468, "coastal": False, "country": "China", "timezone": "Asia/Shanghai"},
    {"city": "Chongqing", "icao": "ZUCK", "lat": 29.7192, "lon": 106.6413, "coastal": False, "country": "China", "timezone": "Asia/Shanghai"},
    {"city": "Shenzhen", "icao": "ZGSZ", "lat": 22.6393, "lon": 113.8107, "coastal": True, "country": "China", "timezone": "Asia/Shanghai"},
    {"city": "Wuhan", "icao": "ZHHH", "lat": 30.7838, "lon": 114.2081, "coastal": False, "country": "China", "timezone": "Asia/Shanghai"},
    {"city": "Taipei", "icao": "RCSS", "lat": 25.0699, "lon": 121.5523, "coastal": True, "country": "Taiwan", "timezone": "Asia/Taipei"},  # Songshan — Polymarket settlement (NOT RCTP)

    # Oceania
    {"city": "Sydney", "icao": "YSSY", "lat": -33.9461, "lon": 151.1772, "coastal": True, "country": "Australia", "timezone": "Australia/Sydney"},
    {"city": "Melbourne", "icao": "YMML", "lat": -37.6690, "lon": 144.8410, "coastal": True, "country": "Australia", "timezone": "Australia/Melbourne"},
    {"city": "Auckland", "icao": "NZAA", "lat": -37.0082, "lon": 174.7850, "coastal": True, "country": "New Zealand", "timezone": "Pacific/Auckland"},
    {"city": "Wellington", "icao": "NZWN", "lat": -41.3272, "lon": 174.8050, "coastal": True, "country": "New Zealand", "timezone": "Pacific/Auckland"},

    # South America
    {"city": "Sao Paulo", "icao": "SBGR", "lat": -23.4356, "lon": -46.4731, "coastal": False, "country": "Brazil", "timezone": "America/Sao_Paulo"},  # Guarulhos
    {"city": "Rio de Janeiro", "icao": "SBGL", "lat": -22.8099, "lon": -43.2506, "coastal": True, "country": "Brazil", "timezone": "America/Sao_Paulo"},
    {"city": "Buenos Aires", "icao": "SAEZ", "lat": -34.8222, "lon": -58.5358, "coastal": True, "country": "Argentina", "timezone": "America/Argentina/Buenos_Aires"},  # Ezeiza
    {"city": "Santiago", "icao": "SCEL", "lat": -33.3930, "lon": -70.7858, "coastal": False, "country": "Chile", "timezone": "America/Santiago"},

    # Africa
    {"city": "Cairo", "icao": "HECA", "lat": 30.1219, "lon": 31.4056, "coastal": True, "country": "Egypt", "timezone": "Africa/Cairo"},
    {"city": "Johannesburg", "icao": "FAOR", "lat": -26.1392, "lon": 28.2460, "coastal": False, "country": "South Africa", "timezone": "Africa/Johannesburg"},
    {"city": "Lagos", "icao": "DNMM", "lat": 6.5774, "lon": 3.3215, "coastal": True, "country": "Nigeria", "timezone": "Africa/Lagos"},  # Murtala Muhammed (was DNAA Abuja!)

    # Central America
    {"city": "Panama City", "icao": "MPMG", "lat": 9.0714, "lon": -79.3835, "coastal": True, "country": "Panama", "timezone": "America/Panama"},
]


# ============================================================================
# STRATEGY FAMILY CONFIGURATION
# ============================================================================

# Strategy enable flags — controls which strategy families accept new entries
# ── RECOVERY BUILD (2026-04-11) ──
# Lane: above/below only, 4 city whitelist, fixed $2 sizing
# Evidence basis: docs/RECOVERY_BUILD.md
ENABLE_F_STRICT       = False   # WAS True — produced 0 trades in 89h, disabled
ENABLE_NO_HARVEST_V2  = False   # WAS True — +$0.31 in 89h, trivial, disabled
ENABLE_ABOVE_BELOW    = True    # PRIMARY LANE (no longer shadow)
ENABLE_EXACT_SINGLE   = False   # Disabled: 0/11 WR, systematic overestimation
ENABLE_EXACT_2BIN     = False   # WAS True — exact bins physically unprofitable
ENABLE_LONG_HORIZON   = False   # Hard-killed: no model exists for >24h
PILOT_CITY_ONLY       = ""      # Replaced by RECOVERY_CITIES whitelist below
ABOVE_BELOW_SHADOW    = False   # No longer shadow — it IS the lane

# ── RECOVERY MODE BLOCK (2026-04-11) ────────────────────────────────────────
# Narrow comeback lane: above/below only, 4-city whitelist, fixed tiny sizing.
# Set RECOVERY_MODE=False to return to full-feature operation.
RECOVERY_MODE: bool = True

# Hard city whitelist — only these 4 cities generate signals.
# Selection basis: station_bias_summary.csv std dev (lowest = most predictable):
#   Munich 0.98°F (n=24), Singapore 1.18°F (n=16),
#   London 1.37°F (n=100), Paris 1.62°F (n=43)
RECOVERY_CITIES: set = {"Munich", "Singapore", "London", "Paris"}

# Fixed trade size — Kelly disabled in recovery mode.
FIXED_TRADE_SIZE_USD: float = 2.00
DISABLE_KELLY: bool = True

# Recovery above/below gate parameters
RECOVERY_AB_MIN_LEAD_MIN: float     = 360.0   # 6h minimum lead time
RECOVERY_AB_MAX_LEAD_MIN: float     = 1440.0  # 24h maximum lead time
RECOVERY_AB_MIN_MARKET_PRICE: float = 0.10    # don't buy sub-10c (near-certainty NO)
RECOVERY_AB_MAX_MARKET_PRICE: float = 0.45    # don't buy above 45c (market already rich)
RECOVERY_AB_MIN_RECAL_PROB: float   = 0.25    # recalibrated probability minimum
RECOVERY_AB_MIN_EDGE_PP: float      = 0.05    # recal must exceed market by ≥5pp
RECOVERY_AB_DAILY_STOP_USD: float   = -10.00  # halt new entries if daily PnL below this

# Recovery mode: disable all non-core strategy agent modules
RECOVERY_DISABLE_STATION_EDGE_OVERRIDE: bool = True  # use ensemble-only prob path
RECOVERY_DISABLE_BINSNIPER:      bool = True
RECOVERY_DISABLE_GFS_REFRESH:    bool = True
RECOVERY_DISABLE_OBS_CONFIRM:    bool = True
RECOVERY_DISABLE_EXIT_AGENTS:    bool = True
RECOVERY_DISABLE_CROSS_CITY:     bool = True
RECOVERY_DISABLE_DUTCH_BOOK:     bool = True
RECOVERY_DISABLE_HEDGE_MANAGER:  bool = True
RECOVERY_DISABLE_METAR_INTEL:    bool = True
RECOVERY_DISABLE_LAST_MILE:      bool = True
RECOVERY_DISABLE_NO_HARVEST:     bool = True
RECOVERY_DISABLE_YES_HARVEST:    bool = True
# ────────────────────────────────────────────────────────────────────────────

# F-Strict gate parameters (mirror of src/strategy_gate.py constants)
F_STRICT_PRICE_BAND        = (0.10, 0.20)
F_STRICT_RECAL_PROB_BAND   = (0.22, 0.40)
F_STRICT_LEAD_HOURS        = (12, 24)
F_STRICT_MAX_RMSE_C        = 1.8
F_STRICT_PER_TRADE_CAP_USD = 10
F_STRICT_DAILY_STOP_USD    = -25.0
SIGMA_FLOOR_C              = 2.5  # raised from 1.5 (median station RMSE = 1.6)
EXACT_BIN_HARD_CAP         = 0.35  # lowered from 0.45

# NO_HARVEST staged scaling (operator: $5 → $10 → $25)
NO_HARVEST_CAP_USD         = 10    # STAGE 1 (will bump to 25 after fill-quality validation)
NO_HARVEST_MIN_DEPTH_USD   = 50    # both legs must show ≥$50 visible liquidity
NO_HARVEST_POLL_SECONDS    = 30

# Exact 2-bin parameters
EXACT_2BIN_SAME_DAY_ONLY = False        # Allow next-day markets too
EXACT_2BIN_ALLOW_NEXT_DAY = True        # Explicitly allow next-day
EXACT_2BIN_REQUIRE_ADJACENT = True      # Bins must be adjacent
EXACT_2BIN_MIN_COMBINED_EDGE = 0.10     # Min combined_model_prob - combined_cost
EXACT_2BIN_MAX_COMBINED_COST = 0.40     # Max sum of both leg prices (40c)


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
OBS_ANOMALY_TEMP_THRESHOLD = 6.0  # Â°C
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
    wallet_address: str = "0xE2FB305bE360286808e5ffa2923B70d9014a37BE"
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


# Alias: scheduler.py imports "Config" — point it at APIConfig
Config = APIConfig
