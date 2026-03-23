"""
Fee and rebate awareness client for Polymarket temperature trading bot.

Implements spec bullet #21:
- Detect whether market has fees enabled using feesEnabled field
- Before placing any order, fetch current fee rate in bps for relevant tokenId
- Execution simulator and backtester include feeRateBps in cost proxy ONLY when feesEnabled=true
- Log per trade leg: feesEnabled, feeRateBps_used, realized fees paid
- Daily optional check for maker rebates via public rebates endpoint
- If exact endpoint paths unknown, create stub methods with clear TODOs
"""

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RebateResult:
    """Result of rebate check."""
    amount: float
    eligible: bool
    checked_at: str
    error: Optional[str] = None


def get_fees_enabled(market: dict) -> bool:
    """
    Check market dict for feesEnabled field.

    Args:
        market: Market dictionary from Polymarket API

    Returns:
        Boolean indicating whether fees are enabled for this market
    """
    fees_enabled = market.get("feesEnabled", False)
    logger.debug(f"Market fees enabled: {fees_enabled}")
    return fees_enabled


def fetch_fee_rate_bps(
    token_id: str,
    api_base_url: str = "https://clob.polymarket.com",
    paper_mode: bool = True,
    default_fee_bps: int = 0
) -> int:
    """
    Fetch current fee rate in basis points for a given token.

    Args:
        token_id: Token identifier (e.g., contract address or token symbol)
        api_base_url: Base URL for Polymarket API
        paper_mode: If True, return default_fee_bps instead of calling API
        default_fee_bps: Default fee rate to use in paper mode (0-10000 bps)

    Returns:
        Fee rate in basis points (0-10000)

    Note:
        TODO: Replace with actual Polymarket fee-rate endpoint when documented
        Stub: returns default value for paper mode
    """
    if paper_mode:
        logger.info(
            f"Paper mode: returning default fee rate {default_fee_bps} bps for token {token_id}"
        )
        return default_fee_bps

    # TODO: Replace with actual Polymarket fee-rate endpoint when documented
    # Expected behavior:
    # 1. Make GET request to: {api_base_url}/fees/{token_id} or similar
    # 2. Parse response for current fee rate in basis points
    # 3. Cache result with TTL < 60 seconds to avoid stale values
    # 4. On error: log warning, increment fallback counter, return default_fee_bps

    logger.warning(
        f"Fee rate endpoint not configured. Using default {default_fee_bps} bps for token {token_id}"
    )
    return default_fee_bps


def compute_fee_cost(
    size: float,
    price: float,
    fee_rate_bps: int,
    fees_enabled: bool
) -> float:
    """
    Compute fee cost for a trade leg.

    Args:
        size: Order size (quantity)
        price: Price per unit
        fee_rate_bps: Fee rate in basis points
        fees_enabled: Whether fees are enabled for this market

    Returns:
        Fee cost in quote currency (0.0 if fees not enabled)
    """
    if not fees_enabled:
        return 0.0

    notional_value = size * price
    fee_cost = notional_value * (fee_rate_bps / 10000.0)
    return fee_cost


def check_maker_rebates(
    address: str,
    api_base_url: str = "https://clob.polymarket.com"
) -> RebateResult:
    """
    Check for maker rebates eligibility and amount for an address.

    Args:
        address: User's wallet address
        api_base_url: Base URL for Polymarket API

    Returns:
        RebateResult with amount, eligibility, and timestamp

    Note:
        TODO: Replace with actual Polymarket rebates endpoint when documented
        Stub: returns error status indicating endpoint not configured
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    # TODO: Replace with actual Polymarket public rebates endpoint when documented
    # Expected behavior:
    # 1. Make GET request to: {api_base_url}/rebates/{address} or /user/{address}/rebates
    # 2. Parse response for rebate amount and eligibility status
    # 3. Return RebateResult with amount, eligible=True/False, checked_at timestamp
    # 4. On error: log warning, return RebateResult with error field set

    result = RebateResult(
        amount=0.0,
        eligible=False,
        checked_at=now_iso,
        error="Rebates endpoint not configured"
    )
    logger.info(f"Rebate check for {address}: {result.error}")
    return result


def log_fee_info(
    leg_id: str,
    fees_enabled: bool,
    fee_rate_bps: int,
    realized_fees: float
) -> dict:
    """
    Create ledger entry for fee information on a trade leg.

    Args:
        leg_id: Unique identifier for this trade leg
        fees_enabled: Whether fees were enabled for this market
        fee_rate_bps: Fee rate in basis points used
        realized_fees: Actual fees paid in quote currency

    Returns:
        Dictionary with all fee fields for ledger insertion
    """
    timestamp_utc = datetime.now(timezone.utc).isoformat()

    fee_info = {
        "leg_id": leg_id,
        "fees_enabled": fees_enabled,
        "fee_rate_bps_used": fee_rate_bps,
        "realized_fees_paid": realized_fees,
        "timestamp_utc": timestamp_utc
    }

    logger.debug(
        f"Leg {leg_id}: fees_enabled={fees_enabled}, "
        f"fee_rate_bps={fee_rate_bps}, realized_fees=${realized_fees:.6f}"
    )

    return fee_info


class FeeClient:
    """
    Client for managing fees and rebates in Polymarket temperature trading bot.

    Handles:
    - Detection of feesEnabled flag in market data
    - Fetching current fee rates per token
    - Computing fee costs for trade legs
    - Checking maker rebate eligibility
    - Logging fee information for ledger/audit trail
    """

    def __init__(
        self,
        api_base_url: str = "https://clob.polymarket.com",
        paper_mode: bool = True,
        default_fee_bps: int = 0
    ):
        """
        Initialize FeeClient.

        Args:
            api_base_url: Base URL for Polymarket CLOB API
            paper_mode: If True, use default fee rates instead of fetching from API
            default_fee_bps: Default fee rate (bps) to use in paper mode
        """
        self.api_base_url = api_base_url
        self.paper_mode = paper_mode
        self.default_fee_bps = default_fee_bps
        self._fallback_counter = 0

        logger.info(
            f"FeeClient initialized: paper_mode={paper_mode}, "
            f"default_fee_bps={default_fee_bps}"
        )

    def get_fees_enabled(self, market: dict) -> bool:
        """
        Check if fees are enabled for a market.

        Args:
            market: Market dictionary from Polymarket API

        Returns:
            Boolean indicating whether fees are enabled
        """
        return get_fees_enabled(market)

    def fetch_fee_rate(self, token_id: str) -> int:
        """
        Fetch current fee rate for a token.

        Args:
            token_id: Token identifier

        Returns:
            Fee rate in basis points
        """
        return fetch_fee_rate_bps(
            token_id=token_id,
            api_base_url=self.api_base_url,
            paper_mode=self.paper_mode,
            default_fee_bps=self.default_fee_bps
        )

    def compute_cost(
        self,
        size: float,
        price: float,
        fee_rate_bps: int,
        fees_enabled: bool
    ) -> float:
        """
        Compute total fee cost for a trade leg.

        Args:
            size: Order size
            price: Price per unit
            fee_rate_bps: Fee rate in basis points
            fees_enabled: Whether fees are enabled

        Returns:
            Fee cost in quote currency
        """
        return compute_fee_cost(
            size=size,
            price=price,
            fee_rate_bps=fee_rate_bps,
            fees_enabled=fees_enabled
        )

    def check_rebates(self, address: str) -> RebateResult:
        """
        Check maker rebate eligibility for an address.

        Args:
            address: User's wallet address

        Returns:
            RebateResult with amount, eligibility, and timestamp
        """
        return check_maker_rebates(address=address, api_base_url=self.api_base_url)

    def log_fee_info(
        self,
        leg_id: str,
        fees_enabled: bool,
        fee_rate_bps: int,
        realized_fees: float
    ) -> dict:
        """
        Create ledger entry for fee information.

        Args:
            leg_id: Unique identifier for trade leg
            fees_enabled: Whether fees were enabled
            fee_rate_bps: Fee rate used
            realized_fees: Actual fees paid

        Returns:
            Dictionary with fee information for ledger
        """
        return log_fee_info(
            leg_id=leg_id,
            fees_enabled=fees_enabled,
            fee_rate_bps=fee_rate_bps,
            realized_fees=realized_fees
        )

    def register_fallback(self, reason: str) -> None:
        """
        Register a fallback event (per spec #17).

        Args:
            reason: Description of why fallback was used
        """
        self._fallback_counter += 1
        logger.warning(
            f"Fallback #{self._fallback_counter}: {reason} "
            f"(total fallbacks: {self._fallback_counter})"
        )

    def get_fallback_count(self) -> int:
        """Get total number of fallback events registered."""
        return self._fallback_counter
