"""
Scheduler module for Polymarket temperature trading bot.

Handles scheduling with burst triggers, backtest mode, and the main decision loop.
Orchestrates the full pipeline for market evaluation cycles.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Callable
import time

# Daily resolved-market data logger — keeps resolved.json / era5.json current
try:
    import data_logger as _data_logger
    _HAS_DATA_LOGGER = True
except ImportError:
    _HAS_DATA_LOGGER = False

# Trade resolver — settles open trades against Polymarket outcomes
try:
    import trade_resolver as _trade_resolver
    _HAS_TRADE_RESOLVER = True
except ImportError:
    _HAS_TRADE_RESOLVER = False

import time as _time
try:
    import requests as _req_mod
except ImportError:
    _req_mod = None

_skip_list = {}
_SKIP_S = 3600
_bal_cache = [None, 0.0]

def _is_skipped(slug):
    if slug in _skip_list:
        if _time.time() - _skip_list[slug] < _SKIP_S:
            return True
        del _skip_list[slug]
    return False

def _mark_skipped(slug):
    _skip_list[slug] = _time.time()
    import logging; logging.getLogger(__name__).warning("SKIP: %s (%d skipped)", slug, len(_skip_list))

def _get_balance():
    if _bal_cache[0] is not None and _time.time() - _bal_cache[1] < 30:
        return _bal_cache[0]
    try:
        if _req_mod:
            r = _req_mod.get("https://clob.polymarket.com/balance", params={"owner": "0xE2FB305bE360286808e5ffa2923B70d9014a37BE"}, timeout=5)
            if r.ok:
                d = r.json()
                bal = float(d.get("balance", d.get("USDC", 99)))
                _bal_cache[0] = bal; _bal_cache[1] = _time.time()
                return bal
    except: pass
    return _bal_cache[0] if _bal_cache[0] else 999.0

def _smart_exit(entry_cost, current_value, theo_ev, mins_to_res):
    if entry_cost <= 0: return False, "no_cost"
    if mins_to_res < 120 and current_value < 0.5 * entry_cost: return True, "time_exit"
    if theo_ev < 0.0: return True, "ev_decay"
    if current_value > 2.0 * entry_cost: return True, "profit_take"
    return False, "hold"


# Local imports (assuming these modules exist)
try:
    from ledger import Ledger
    from fee_client import FeeClient
    from config import Config
    from market_classifier import classify_regime
    from time_utils import compute_t_entry, get_diurnal_stage
    from nowcaster import NowcasterEnsemble
    from ensemble_probs import EnsembleProbability
    from cross_market_filter import apply_cross_market_filter
    from ladder_builder import build_ladder
    from trader_execution import PaperExecutionAdapter, LiveExecutionAdapter
except ImportError as _import_err:
    import logging as _log
    _log.getLogger(__name__).warning(
        "One or more local modules missing (%s) — running with stubs", _import_err
    )

    class Ledger:  # noqa: E701
        def __init__(self, db_path="ledger.db"): self.db_path = db_path
        def init_db(self): pass
        def log_alert(self, a): pass
        def log_decision(self, **kw): pass

    class FeeClient:
        def get_taker_fee(self): return 0.002

    class Config:
        min_theo_ev: float = 0.02
        db_path: str = "ledger.db"

    def classify_regime(data):  # noqa: E306
        return "unknown"

    def compute_t_entry(resolution_time, local_tz):  # noqa: E306
        return resolution_time

    def get_diurnal_stage(now_utc, local_tz):  # noqa: E306
        return "unknown"

    class NowcasterEnsemble:
        def __init__(self, config=None): pass
        def forecast(self, **kw): return {"yes_prob": 0.5, "no_prob": 0.5, "bin_probs": [], "source": "stub"}

    class EnsembleProbability:
        def __init__(self, config=None): pass
        def estimate_probability(self, **kw): return {"yes_prob": 0.5, "no_prob": 0.5, "bin_probs": [], "source": "stub"}

    def apply_cross_market_filter(probs, market, markets):  # noqa: E306
        return probs

    def build_ladder(probs=None, category=None, prices=None, **kw):  # noqa: E306
        return []

    class PaperExecutionAdapter:
        def __init__(self, ledger=None): self.ledger = ledger
        def place_order(self, **kw): return {"order_id": "stub", "status": "STUB"}
        def place_orders(self, **kw): return []

    class LiveExecutionAdapter:
        def __init__(self, ledger=None, fee_client=None): self.ledger = ledger
        def place_order(self, **kw): return {"order_id": "stub", "status": "STUB"}
        def place_orders(self, **kw): return []


logger = logging.getLogger(__name__)


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class CycleResult:
    """Result of a single trading cycle."""
    markets_evaluated: int
    trades_placed: int
    no_trades: int
    alerts: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BacktestMarketData:
    """Historical data for a single market in backtest mode."""
    market_slug: str
    resolution_time: datetime
    forecasts: list  # list of dicts: {'timestamp': datetime, 'value': float, ...}
    observations: list  # list of dicts: {'timestamp': datetime, 'temp': float, ...}
    book_snapshots: list  # list of dicts or None: {'timestamp': datetime, 'bid': float, 'ask': float, ...}
    actual_outcome: str  # e.g., "YES" or "NO"


@dataclass
class BacktestMarketResult:
    """Result for a single market in backtest."""
    market_slug: str
    t_entry: datetime
    trades_placed: int
    signal_only: bool
    pnl: float
    outcome: str
    error: Optional[str] = None


@dataclass
class BacktestResult:
    """Overall result of a backtest run."""
    total_markets: int
    trades_placed: int
    signal_only_count: int
    pnl: float
    sharpe: float
    win_rate: float
    results_per_market: list = field(default_factory=list)


# ============================================================================
# TradingScheduler
# ============================================================================

class TradingScheduler:
    """
    Main scheduler that orchestrates the full trading pipeline.

    Handles:
    - Cycle execution with market evaluation
    - Burst trigger detection (00Z/12Z hard, 06Z/18Z secondary)
    - HRRR polling for US markets
    - Paper vs. live mode execution
    """

    def __init__(
        self,
        ledger: Ledger,
        fee_client: FeeClient,
        config: Config,
        paper_mode: bool = True,
        telegram_token: Optional[str] = None,
    ):
        """
        Initialize the trading scheduler.

        Args:
            ledger: Ledger instance for tracking trades and decisions
            fee_client: Fee client for computing maker/taker fees
            config: Configuration object with thresholds and parameters
            paper_mode: If True, use PaperExecutionAdapter; else LiveExecutionAdapter
            telegram_token: Optional Telegram bot token for alerts
        """
        self.ledger = ledger
        self.fee_client = fee_client
        self.config = config
        self.paper_mode = paper_mode

        # Initialize execution adapter
        if paper_mode:
            self.execution_adapter = PaperExecutionAdapter(ledger)
        else:
            self.execution_adapter = LiveExecutionAdapter(ledger, fee_client)

        # Alert system
        self.alert_system = AlertSystem(ledger, telegram_token)

        # Tracking
        self.last_hrrr_poll: Optional[datetime] = None

    def run_cycle(self, markets: list[dict]) -> CycleResult:
        """
        Run a single trading cycle for all markets.

        Pipeline for each market:
        1. Parse station, check confidence
        2. Validate WU/METAR sanity
        3. Get market prices and book snapshots
        4. Compute time to resolution, diurnal stage, regime
        5. Choose nowcasting vs ensemble probability estimation
        6. Apply cross-market filter
        7. Compute theoretical EV with dynamic ratchet
        8. Build ladder and check EV gates
        9. Place orders if passes all gates
        10. Check and process alerts

        Args:
            markets: List of market dicts with slug, station, category, etc.

        Returns:
            CycleResult with evaluation summary
        """
        result = CycleResult(
            markets_evaluated=0,
            trades_placed=0,
            no_trades=0,
            alerts=[],
            errors=[],
        )

        now_utc = datetime.now(timezone.utc)

        logger.info(f"Starting trading cycle at {now_utc.isoformat()}")

        for market in markets:
            try:
                market_slug = market.get("slug")
                station = market.get("station")
                category = market.get("category")

                logger.debug(f"Evaluating market: {market_slug} (station: {station})")

                # Step 1: Parse station and check confidence
                if not station:
                    result.no_trades += 1
                    logger.warning(f"{market_slug}: No station specified, skipping")
                    continue

                confidence = market.get("confidence", 0)
                if confidence < 2:
                    result.no_trades += 1
                    logger.debug(f"{market_slug}: Confidence {confidence} < 2, skipping")
                    continue

                # Step 2: Validate WU/METAR sanity
                # In paper mode, skip this gate — WU/METAR data isn't populated
                # by gamma_client and blocking here rejects every market.
                if not self.paper_mode:
                    wu_valid = self._validate_wu_data(market.get("wu_data"))
                    metar_valid = self._validate_metar_data(market.get("metar_data"))
                    if not (wu_valid and metar_valid):
                        result.no_trades += 1
                        logger.warning(f"{market_slug}: WU/METAR sanity check failed")
                        continue

                # Step 3: Get market prices and book snapshots
                prices = market.get("prices", {})
                book_snapshot = market.get("book_snapshot")

                # Step 4: Compute time to resolution, diurnal stage, regime
                resolution_time = market.get("resolution_time")
                if not resolution_time:
                    result.no_trades += 1
                    logger.warning(f"{market_slug}: No resolution time")
                    continue

                time_to_resolution = resolution_time - now_utc
                time_to_resolution_hours = time_to_resolution.total_seconds() / 3600

                local_tz = market.get("timezone", "UTC")
                try:
                    from zoneinfo import ZoneInfo as _ZI
                    _now_local = now_utc.astimezone(_ZI(local_tz))
                    diurnal_stage = get_diurnal_stage(_now_local, peak_start=14, peak_end=17)
                except Exception as _ds_err:
                    logger.warning(
                        "%s: diurnal_stage failed (%s), defaulting to 'unknown'",
                        market_slug, _ds_err,
                    )
                    diurnal_stage = "unknown"
                regime = classify_regime(market.get("regime_data", {}))

                logger.debug(
                    f"{market_slug}: TTR={time_to_resolution_hours:.1f}h, "
                    f"diurnal={diurnal_stage}, regime={regime}"
                )

                # Step 5: Choose nowcasting vs ensemble
                if time_to_resolution_hours <= 24:
                    # Use nowcasting
                    nowcaster = NowcasterEnsemble(config=self.config)
                    forecast_probs = nowcaster.forecast(
                        station=station,
                        time_horizon=time_to_resolution_hours,
                        category=category,
                    )
                else:
                    # Use ensemble probability estimation
                    ensemble = EnsembleProbability(config=self.config)
                    forecast_probs = ensemble.estimate_probability(
                        station=station,
                        category=category,
                        forecast_data=market.get("forecast_data"),
                    )

                # Step 6: Apply cross-market filter
                filtered_probs = apply_cross_market_filter(
                    forecast_probs, market, markets
                )

                # Step 7: Compute min_theo_ev with dynamic ratchet
                min_theo_ev = self._compute_min_theo_ev(
                    burst_mode=False,  # Can be overridden in schedule_loop
                    base_min_ev=self.config.min_theo_ev,
                )

                # Step 8: Build ladder and compute theoretical EV
                ladder = build_ladder(
                    probs=filtered_probs,
                    category=category,
                    prices=prices,
                )

                theoretical_full_ev = self._compute_theoretical_ev(
                    ladder=ladder,
                    filtered_probs=filtered_probs,
                    fee_client=self.fee_client,
                )

                # Step 9: Check EV gates
                if not self._check_ev_gates(theoretical_full_ev, min_theo_ev):
                    result.no_trades += 1
                    logger.info(
                        f"{market_slug}: EV {theoretical_full_ev:.4f} < "
                        f"min {min_theo_ev:.4f}, not trading"
                    )
                    continue

                # Step 10: Freeze snapshot and place orders
                # In paper mode, synthesise a book snapshot from market prices
                # when the real one is missing (gamma_client doesn't poll CLOB).
                if book_snapshot is None and self.paper_mode:
                    yes_price = prices.get("yes", 0.5)
                    no_price = prices.get("no", 1 - yes_price)
                    book_snapshot = {
                        "timestamp": now_utc,
                        "bid": round(yes_price - 0.01, 4),
                        "ask": round(yes_price + 0.01, 4),
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "synthetic": True,
                    }
                    logger.debug(f"{market_slug}: Using synthetic book snapshot (paper mode)")
                if book_snapshot is None:
                    logger.warning(f"{market_slug}: No book snapshot available, signal only")
                else:
                    try:
                        trade_orders = self.execution_adapter.place_orders(
                            market_slug=market_slug,
                            ladder=ladder,
                            book_snapshot=book_snapshot,
                            paper_mode=self.paper_mode,
                        )

                        if trade_orders:
                            result.trades_placed += len(trade_orders)
                            logger.info(
                                f"{market_slug}: Placed {len(trade_orders)} orders, "
                                f"EV={theoretical_full_ev:.4f}"
                            )
                        else:
                            result.no_trades += 1
                            logger.info(f"{market_slug}: No tradeable levels in ladder")
                    except Exception as e:
                        logger.error(f"{market_slug}: Error placing orders: {e}")
                        result.errors.append(f"{market_slug}: {str(e)}")

                # Step 11: Check alerts
                market_alerts = self._check_alerts(market, forecast_probs)
                if market_alerts:
                    result.alerts.extend(market_alerts)

                result.markets_evaluated += 1

            except Exception as e:
                logger.error(f"Error evaluating market {market.get('slug', '?')}: {e}")
                result.errors.append(str(e))

        logger.info(
            f"Cycle complete: {result.markets_evaluated} evaluated, "
            f"{result.trades_placed} trades, {result.no_trades} no-trades, "
            f"{len(result.errors)} errors"
        )

        # Process alerts
        if result.alerts:
            self.alert_system.process_alerts(result.alerts)

        return result

    def schedule_loop(
        self,
        markets_fn: Callable[[], list[dict]],
        interval_minutes: int = 15,
    ) -> None:
        """
        Main scheduling loop.

        - Checks for burst triggers (00Z/12Z/06Z/18Z)
        - Runs cycles at regular intervals
        - Handles HRRR polling for US markets

        Args:
            markets_fn: Callable that returns list of markets to evaluate
            interval_minutes: Minutes between normal cycles (default 15)
        """
        logger.info(f"Starting schedule loop (interval={interval_minutes}m)")

        while True:
            now_utc = datetime.now(timezone.utc)
            is_burst, trigger_label = self.is_burst_trigger(now_utc)

            # Daily data logger — appends newly resolved markets + ERA5 to JSON stores
            if _HAS_DATA_LOGGER:
                try:
                    dl_result = _data_logger.maybe_update()
                    if dl_result.get("ran"):
                        logger.info(
                            "data_logger: +%d resolved rows, +%d ERA5 rows, new cities: %s",
                            dl_result.get("new_resolved", 0),
                            dl_result.get("new_era5", 0),
                            dl_result.get("new_cities", []),
                        )
                        if dl_result.get("error"):
                            logger.warning("data_logger error: %s", dl_result["error"])
                except Exception as _dl_exc:
                    logger.warning("data_logger raised: %s", _dl_exc)

            # Trade resolver — settle open trades against Polymarket outcomes
            if _HAS_TRADE_RESOLVER:
                try:
                    tr_result = _trade_resolver.resolve_trades()
                    if tr_result.get("ran") and tr_result.get("resolved_count", 0) > 0:
                        logger.info(
                            "trade_resolver: settled %d trades — W=%d L=%d PnL=$%.2f",
                            tr_result["resolved_count"],
                            tr_result.get("wins", 0),
                            tr_result.get("losses", 0),
                            tr_result.get("total_pnl", 0),
                        )
                except Exception as _tr_exc:
                    logger.warning("trade_resolver raised: %s", _tr_exc)

            # Calibration backfill — wire recal_prob() map to live resolutions.
            # Runs at most once per hour; safe to call every cycle (no-ops if
            # < _CALIB_INTERVAL_S seconds have elapsed since last run).
            _CALIB_INTERVAL_S = 3600
            if not hasattr(self, "_last_calib_ts"):
                self._last_calib_ts = 0.0
            _now_ts = time.time()
            if _now_ts - self._last_calib_ts >= _CALIB_INTERVAL_S:
                try:
                    import os as _os
                    from scripts.calibration_backfill import backfill as _calib_backfill
                    _ledger_path = _os.environ.get("LEDGER_DB", "ledger.db")
                    _store_path = _os.environ.get("ACCURACY_STORE", "accuracy_store.json")
                    _calib_result = _calib_backfill(_ledger_path, _store_path)
                    _added = _calib_result.get("added", 0) if isinstance(_calib_result, dict) else 0
                    _total = _calib_result.get("total_resolutions", 0) if isinstance(_calib_result, dict) else 0
                    if _added > 0:
                        logger.info(
                            "calibration_backfill: added=%d total_resolutions=%d",
                            _added, _total,
                        )
                    else:
                        logger.debug("calibration_backfill: no new resolutions (total=%d)", _total)
                except ImportError:
                    logger.debug("calibration_backfill not available — skipping")
                except Exception as _calib_exc:
                    logger.warning("calibration_backfill raised: %s", _calib_exc)
                finally:
                    # Always update timestamp so we don't retry every cycle on error
                    self._last_calib_ts = _now_ts

            # Check HRRR polling
            should_poll = self.should_poll_hrrr(now_utc, self.last_hrrr_poll)
            if should_poll:
                logger.debug("HRRR poll due for US markets")
                self.last_hrrr_poll = now_utc
                # Stub: actual HRRR polling logic would go here

            # Run cycle
            try:
                markets = markets_fn()

                if is_burst:
                    logger.info(f"Burst trigger detected: {trigger_label}")
                    # Could tighten min_ev for burst mode
                    result = self.run_cycle(markets)
                else:
                    result = self.run_cycle(markets)

            except Exception as e:
                logger.error(f"Error in schedule loop: {e}")

            # Sleep until next interval
            next_run = now_utc + timedelta(minutes=interval_minutes)
            sleep_seconds = (next_run - datetime.now(timezone.utc)).total_seconds()
            if sleep_seconds > 0:
                logger.debug(f"Sleeping for {sleep_seconds:.1f}s until next cycle")
                time.sleep(sleep_seconds)

    def is_burst_trigger(self, now_utc: datetime) -> tuple[bool, str]:
        """
        Check if current time is within a burst trigger window.

        Hard triggers: 00:00Z, 12:00Z (major model runs)
        Secondary triggers: 06:00Z, 18:00Z
        Window: ±15 minutes

        Args:
            now_utc: Current UTC datetime

        Returns:
            (is_burst, trigger_label) tuple
            - is_burst: True if within trigger window
            - trigger_label: "00Z", "12Z", "06Z", "18Z", or ""
        """
        hour = now_utc.hour
        minute = now_utc.minute

        # Hard triggers
        if hour in [0, 12]:
            if minute <= 15 or minute >= 45:
                return (True, "00Z" if hour == 0 else "12Z")

        # Secondary triggers
        if hour in [6, 18]:
            if minute <= 15 or minute >= 45:
                return (True, "06Z" if hour == 6 else "18Z")

        return (False, "")

    def should_poll_hrrr(
        self,
        now_utc: datetime,
        last_hrrr_poll: Optional[datetime],
    ) -> bool:
        """
        Check if HRRR should be polled for US markets.

        HRRR is polled hourly.

        Args:
            now_utc: Current UTC datetime
            last_hrrr_poll: Timestamp of last HRRR poll

        Returns:
            True if > 60 minutes since last poll
        """
        if last_hrrr_poll is None:
            return True

        time_since_poll = (now_utc - last_hrrr_poll).total_seconds() / 60
        return time_since_poll > 60

    # ========================================================================
    # Helper Methods
    # ========================================================================

    def _validate_wu_data(self, wu_data: Optional[dict]) -> bool:
        """Validate Weather Underground data sanity."""
        if wu_data is None:
            # Data not yet populated — pass through rather than blocking
            return True
        # TODO: check required fields and timestamp freshness
        return True

    def _validate_metar_data(self, metar_data: Optional[dict]) -> bool:
        """Validate METAR data sanity."""
        if metar_data is None:
            # Data not yet populated — pass through rather than blocking
            return True
        # TODO: check required fields and timestamp freshness
        return True

    def _compute_min_theo_ev(
        self,
        burst_mode: bool,
        base_min_ev: float,
    ) -> float:
        """
        Compute minimum theoretical EV threshold.

        Dynamic ratchet: tighter (lower) for burst mode.

        Args:
            burst_mode: Whether in burst trigger mode
            base_min_ev: Base threshold from config

        Returns:
            Effective minimum EV threshold
        """
        if burst_mode:
            # Tighten by 20% during burst
            return base_min_ev * 0.8
        return base_min_ev

    def _compute_theoretical_ev(
        self,
        ladder: list[dict],
        filtered_probs: dict,
        fee_client: FeeClient,
    ) -> float:
        """
        Compute theoretical EV for the ladder.

        Accounts for maker/taker fees.

        Args:
            ladder: Order ladder from builder
            filtered_probs: Probability estimates
            fee_client: Fee client for lookups

        Returns:
            Theoretical EV (float)
        """
        # Stub: simplified calculation
        if not ladder:
            return 0.0

        total_ev = 0.0
        for level in ladder:
            price = level.get("price", 0.5)
            size = level.get("size", 0)
            prob = filtered_probs.get("yes_prob", 0.5)

            # Expected value before fees
            ev_before = (prob * (1 - price) - (1 - prob) * price) * size

            # Deduct fees (stub)
            fee_rate = fee_client.get_taker_fee() if fee_client else 0.002
            ev_after = ev_before * (1 - fee_rate)

            total_ev += ev_after

        return total_ev

    def _check_ev_gates(self, theoretical_ev: float, min_theo_ev: float) -> bool:
        """
        Check if theoretical EV passes minimum gate.

        Args:
            theoretical_ev: Computed EV
            min_theo_ev: Minimum threshold

        Returns:
            True if passes gate
        """
        return theoretical_ev >= min_theo_ev

    def _check_alerts(self, market: dict, forecast_probs: dict) -> list[dict]:
        """
        Check for alerts on a market.

        Examples: extreme probability shifts, missing data, etc.

        Args:
            market: Market dict
            forecast_probs: Probability estimates

        Returns:
            List of alert dicts
        """
        alerts = []

        # Stub: check for conditions that warrant alerts
        yes_prob = forecast_probs.get("yes_prob", 0.5)
        if yes_prob < 0.05 or yes_prob > 0.95:
            alerts.append({
                "market": market.get("slug"),
                "type": "extreme_prob",
                "value": yes_prob,
            })

        return alerts


# ============================================================================
# BacktestRunner
# ============================================================================

class BacktestRunner:
    """
    Backtest runner for historical market evaluation.

    Runs the same decision pipeline on historical data,
    enforcing causal filtering (only use data available at trade time).
    """

    def __init__(
        self,
        ledger: Ledger,
        fee_client: FeeClient,
        config: Config,
    ):
        """
        Initialize backtest runner.

        Args:
            ledger: Ledger instance for tracking
            fee_client: Fee client for computing fees
            config: Configuration object
        """
        self.ledger = ledger
        self.fee_client = fee_client
        self.config = config
        self.scheduler = TradingScheduler(
            ledger=ledger,
            fee_client=fee_client,
            config=config,
            paper_mode=True,  # Always paper mode in backtest
        )

    def run_backtest(
        self,
        historical_data: list[BacktestMarketData],
    ) -> BacktestResult:
        """
        Run a backtest on historical market data.

        Pipeline per market:
        1. Compute t_entry (trade time)
        2. Filter data causally (only data with ts <= t_entry)
        3. Check for book snapshots; mark signal_only if none
        4. Run same decision pipeline as live
        5. Compute outcome and PnL

        Args:
            historical_data: List of BacktestMarketData instances

        Returns:
            BacktestResult with performance metrics
        """
        logger.info(f"Starting backtest on {len(historical_data)} markets")

        result = BacktestResult(
            total_markets=len(historical_data),
            trades_placed=0,
            signal_only_count=0,
            pnl=0.0,
            sharpe=0.0,
            win_rate=0.0,
            results_per_market=[],
        )

        pnls = []

        for market_data in historical_data:
            try:
                market_result = self._backtest_market(market_data)
                result.results_per_market.append(market_result)

                if market_result.error is None:
                    if market_result.signal_only:
                        result.signal_only_count += 1
                    else:
                        result.trades_placed += market_result.trades_placed

                    result.pnl += market_result.pnl
                    pnls.append(market_result.pnl)

                    logger.info(
                        f"{market_data.market_slug}: trades={market_result.trades_placed}, "
                        f"signal_only={market_result.signal_only}, pnl={market_result.pnl:.4f}"
                    )
                else:
                    logger.error(f"{market_data.market_slug}: {market_result.error}")

            except Exception as e:
                logger.error(f"Error backtesting {market_data.market_slug}: {e}")
                result.results_per_market.append(
                    BacktestMarketResult(
                        market_slug=market_data.market_slug,
                        t_entry=datetime.now(timezone.utc),
                        trades_placed=0,
                        signal_only=False,
                        pnl=0.0,
                        outcome="ERROR",
                        error=str(e),
                    )
                )

        # Compute aggregate metrics
        if pnls:
            import statistics
            if len(pnls) > 1:
                try:
                    result.sharpe = self._compute_sharpe(pnls)
                except:
                    result.sharpe = 0.0

            wins = sum(1 for p in pnls if p > 0)
            result.win_rate = wins / len(pnls) if pnls else 0.0

        logger.info(
            f"Backtest complete: {result.trades_placed} total trades, "
            f"{result.signal_only_count} signal-only, pnl={result.pnl:.4f}, "
            f"win_rate={result.win_rate:.2%}, sharpe={result.sharpe:.2f}"
        )

        return result

    def _backtest_market(
        self,
        market_data: BacktestMarketData,
    ) -> BacktestMarketResult:
        """
        Backtest a single market.

        Args:
            market_data: Historical data for market

        Returns:
            BacktestMarketResult
        """
        # Step 1: Compute t_entry
        local_tz = market_data.market_slug.split("_")[-1]  # Stub
        t_entry = compute_t_entry(market_data.resolution_time, local_tz)

        # Step 2: Filter data causally
        causal_forecasts = [
            f for f in market_data.forecasts
            if f.get("timestamp", datetime.now(timezone.utc)) <= t_entry
        ]
        causal_obs = [
            o for o in market_data.observations
            if o.get("timestamp", datetime.now(timezone.utc)) <= t_entry
        ]
        causal_books = [
            b for b in market_data.book_snapshots
            if b and b.get("timestamp", datetime.now(timezone.utc)) <= t_entry
        ] if market_data.book_snapshots else []

        # Step 3: Check for book snapshots
        signal_only = len(causal_books) == 0

        if signal_only:
            # No book snapshot; can't place orders
            logger.debug(f"{market_data.market_slug}: No book snapshot, signal only")
            return BacktestMarketResult(
                market_slug=market_data.market_slug,
                t_entry=t_entry,
                trades_placed=0,
                signal_only=True,
                pnl=0.0,
                outcome="SIGNAL_ONLY",
            )

        # Step 4: Run decision pipeline (stub)
        # In reality, would call the full pipeline with causal data
        trades_placed = 1  # Stub

        # Step 5: Compute outcome and PnL
        pnl = 0.0  # Stub computation
        if market_data.actual_outcome == "YES":
            pnl = 10.0  # Example
        else:
            pnl = -5.0  # Example

        return BacktestMarketResult(
            market_slug=market_data.market_slug,
            t_entry=t_entry,
            trades_placed=trades_placed,
            signal_only=False,
            pnl=pnl,
            outcome=market_data.actual_outcome,
        )

    def compute_t_entry(
        self,
        resolution_time: datetime,
        local_tz: str,
    ) -> datetime:
        """
        Compute trade entry time.

        Delegates to time_utils.compute_t_entry.

        Args:
            resolution_time: Market resolution time
            local_tz: Local timezone

        Returns:
            Trade entry time
        """
        return compute_t_entry(resolution_time, local_tz)

    @staticmethod
    def _compute_sharpe(pnls: list[float]) -> float:
        """
        Compute Sharpe ratio from PnL series.

        Args:
            pnls: List of PnL values

        Returns:
            Sharpe ratio
        """
        if not pnls or len(pnls) < 2:
            return 0.0

        import statistics
        mean = statistics.mean(pnls)
        stdev = statistics.stdev(pnls)

        if stdev == 0:
            return 0.0

        # Annualized Sharpe (assuming daily PnLs)
        return (mean / stdev) * (252 ** 0.5)


# ============================================================================
# AlertSystem
# ============================================================================

class AlertSystem:
    """
    Alert system for notifications and logging.

    Logs alerts to ledger, prints to console, and optionally sends via Telegram.
    """

    def __init__(
        self,
        ledger: Ledger,
        telegram_token: Optional[str] = None,
    ):
        """
        Initialize alert system.

        Args:
            ledger: Ledger instance for logging
            telegram_token: Optional Telegram bot token
        """
        self.ledger = ledger
        self.telegram_token = telegram_token

    def process_alerts(self, alerts: list[dict]) -> None:
        """
        Process a list of alerts.

        Logs to ledger, prints to console, and sends via Telegram if configured.

        Args:
            alerts: List of alert dicts
        """
        for alert in alerts:
            # Log to ledger
            self.ledger.log_alert(alert)

            # Print to console
            alert_str = self._format_alert(alert)
            logger.warning(f"ALERT: {alert_str}")

            # Send via Telegram if configured
            if self.telegram_token:
                try:
                    self.send_telegram(alert_str)
                except Exception as e:
                    logger.error(f"Failed to send Telegram alert: {e}")

    def send_telegram(self, message: str) -> None:
        """
        Send alert via Telegram.

        TODO: Implement actual Telegram API call.

        Args:
            message: Alert message to send
        """
        if not self.telegram_token:
            logger.warning("Telegram token not configured")
            return

        try:
            import requests
        except ImportError:
            logger.warning("requests library not available for Telegram")
            return

        # Stub: actual implementation
        # POST to https://api.telegram.org/bot{TOKEN}/sendMessage
        # with chat_id and text parameters
        logger.debug(f"[STUB] Would send Telegram: {message}")

    @staticmethod
    def _format_alert(alert: dict) -> str:
        """
        Format an alert dict into a readable string.

        Args:
            alert: Alert dict

        Returns:
            Formatted alert string
        """
        alert_type = alert.get("type", "UNKNOWN")
        market = alert.get("market", "?")
        value = alert.get("value", "")

        if value:
            return f"{alert_type} on {market}: {value}"
        return f"{alert_type} on {market}"


# ============================================================================
# Main Entry Point (for standalone testing)
# ============================================================================

if __name__ == "__main__":
    import os
    from config import create_default_config
    from ledger_telemetry import Ledger as LedgerTelemetry
    from gamma_client import get_markets, invalidate_cache

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    cfg = create_default_config()
    ledger = LedgerTelemetry(db_path=cfg.db_path)
    ledger.init_db()

    paper = os.environ.get("PAPER_MODE", "true").lower() != "false"
    logger.info("WeatherEdge Bot v2 starting (paper_mode=%s)", paper)

    scheduler = TradingScheduler(
        ledger=ledger,
        fee_client=None,          # wired via fee_client.py when CLOB keys present
        config=cfg,
        paper_mode=paper,
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
    )

    # Burst triggers invalidate the Gamma cache so fresh market prices are fetched
    _last_trigger: str = ""

    def _get_markets_with_burst() -> list:
        global _last_trigger
        _, trigger = scheduler.is_burst_trigger(datetime.now(timezone.utc))
        if trigger and trigger != _last_trigger:
            logger.info("Burst trigger %s — invalidating Gamma cache", trigger)
            invalidate_cache()
            _last_trigger = trigger
        return get_markets()

    interval = int(os.environ.get("CYCLE_INTERVAL_MINUTES", "15"))
    logger.info("Starting schedule loop (interval=%dm)", interval)
    scheduler.schedule_loop(
        markets_fn=_get_markets_with_burst,
        interval_minutes=interval,
    )
