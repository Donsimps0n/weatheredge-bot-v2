"""
Risk Manager for Polymarket Temperature Trading Bot

Handles theoretical expected value (EV) calculations, cost proxies, and dynamic risk gates.
Implements bullets #3 (theoretical_full_ev) and #10 (min_theo_ev gate & dynamic ratchet).
"""

import logging
from dataclasses import dataclass, field
from typing import List, Dict

logger = logging.getLogger(__name__)

# ============================================================================
# Configuration (imported from config module in production)
# ============================================================================
MIN_THEO_EV_BASE = 0.10
THEO_EV_FLATTEN_THRESHOLD = 0.10
GATE_12H_MIN_EV = 0.14
GATE_6H_MIN_EV = 0.20
LEAKAGE_RATCHET_PER_HALF_BPS = 0.01


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class CostProxy:
    """Cost proxy breakdown for fill operations."""
    effective_roundtrip_bps: float
    slippage_proxy: float
    fee_cost: float
    total: float
