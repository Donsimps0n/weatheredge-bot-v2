"""
gfs_refresh.py — GFS Model Refresh Agent

Monitors GFS (Global Forecast System) model update schedule and triggers
repricing of all open markets when new forecast data becomes available.

GFS runs 4 times daily: 00Z, 06Z, 12Z, 18Z
Data typically becomes available ~3.5 hours after each run:
  - 00Z run → available ~03:30 UTC
  - 06Z run → available ~09:30 UTC
  - 12Z run → available ~15:30 UTC
  - 18Z run → available ~21:30 UTC

When new data drops, this agent:
1. Invalidates the market cache (forces fresh Gamma API fetch)
2. Re-runs probability calculations with the new forecast
3. Applies station bias corrections
4. Compares new prices against current market prices
5. Trades any new edge that appeared from the forecast shift

Communicates via RufloSharedState:
  - Reads: station_bias/corrections, sentinel/all_states, bin_sniper/stats
  - Publishes: gfs_refresh/status, gfs_refresh/deltas, gfs_refresh/trades
  - Emits: 'gfs_update_detected', 'gfs_reprice_complete', 'gfs_trade_executed'
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# GFS release schedule (UTC hours when data typically becomes available)
# Each tuple: (run_name, expected_availability_utc_hour, expected_availability_utc_minute)
GFS_SCHEDULE = [
    ('00Z', 3, 30),   # 00Z run available ~03:30 UTC
    ('06Z', 9, 30),   # 06Z run available ~09:30 UTC
    ('12Z', 15, 30),  # 12Z run available ~15:30 UTC
    ('18Z', 21, 30),  # 18Z run available ~21:30 UTC
]

# Window around expected time to check (±30 min early, +90 min late)
CHECK_WINDOW_EARLY_MIN = 30
CHECK_WINDOW_LATE_MIN = 90


class GFSRefreshAgent:
    """Watches for GFS model updates and reprices markets when new data drops.

    Args:
        shared_state: RufloSharedState instance for inter-agent communication
    """

    def __init__(self, shared_state=None):
        self._shared = shared_state
        self._last_processed_runs: Dict[str, float] = {}  # run_name → timestamp when processed
        self._price_snapshots: Dict[str, Dict] = {}  # market_id → {our_prob, market_price} before refresh
        self._delta_history: list = []
        self._trade_history: list = []
        self._stats = {
            'total_checks': 0,
            'updates_detected': 0,
            'reprices_done': 0,
            'delta_trades': 0,
            'last_check_ts': 0,
            'last_update_run': '',
            'last_update_ts': 0,
            'next_expected_run': '',
            'next_expected_ts': 0,
        }
        # Thresholds
        self.min_delta_edge_pct = 6.0     # Min edge change to trigger a trade
        self.min_prob_after_refresh = 12.0 # Min corrected prob after refresh
        self.max_delta_size = 15.0         # Max $ per delta trade
        self.max_trades_per_refresh = 8    # Cap trades per GFS update
        self._seen_delta_tokens: set = set()

        if shared_state:
            shared_state.register_agent('gfs_refresh',
                'Watches GFS model updates, reprices markets, trades forecast deltas')

        self._update_next_expected()
        log.info("GFS_REFRESH: initialized | next expected: %s at ~%s UTC",
                 self._stats['next_expected_run'],
                 datetime.fromtimestamp(self._stats['next_expected_ts'], tz=timezone.utc).strftime('%H:%M')
                 if self._stats['next_expected_ts'] else '?')

    def _update_next_expected(self):
        """Calculate the next expected GFS data availability time."""
        now = datetime.now(timezone.utc)
        best_name = ''
        best_dt = None

        for run_name, hour, minute in GFS_SCHEDULE:
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # If this time already passed today, try tomorrow
            if candidate <= now:
                candidate += timedelta(days=1)
            if best_dt is None or candidate < best_dt:
                best_dt = candidate
                best_name = run_name

        self._stats['next_expected_run'] = best_name
        self._stats['next_expected_ts'] = best_dt.timestamp() if best_dt else 0

    def is_gfs_update_window(self) -> Tuple[bool, str]:
        """Check if we're currently in a GFS update window.

        Returns:
            (is_in_window, run_name) — True if a GFS run should be available now
        """
        now = datetime.now(timezone.utc)
        today = now.date()

        for run_name, hour, minute in GFS_SCHEDULE:
            expected = datetime(today.year, today.month, today.day,
                                hour, minute, tzinfo=timezone.utc)

            window_start = expected - timedelta(minutes=CHECK_WINDOW_EARLY_MIN)
            window_end = expected + timedelta(minutes=CHECK_WINDOW_LATE_MIN)

            if window_start <= now <= window_end:
                # Check if we already processed this run today
                run_key = f"{run_name}_{today.isoformat()}"
                if run_key not in self._last_processed_runs:
                    return True, run_name

        return False, ''

    def check_and_refresh(self, all_markets: list, all_signals: list,
                          gamma_client_module=None, bias_module=None,
                          sentinel=None) -> List[Dict]:
        """Main entry point. Called from the auto-trade loop.

        1. Check if we're in a GFS update window
        2. If yes, snapshot current prices, invalidate cache, get fresh data
        3. Compare new probabilities vs old ones
        4. Return delta trades where edge shifted meaningfully

        Args:
            all_markets: Current market list
            all_signals: Current signal list with probabilities
            gamma_client_module: gamma_client module (to invalidate cache)
            bias_module: station_bias module for corrections
            sentinel: WeatherSentinel for live temp data

        Returns:
            List of delta trade dicts ready for execution
        """
        self._stats['total_checks'] += 1
        self._stats['last_check_ts'] = time.time()

        in_window, run_name = self.is_gfs_update_window()
        if not in_window:
            self._update_next_expected()
            self._publish_stats()
            return []

        log.info("GFS_REFRESH: %s update window detected! Starting reprice cycle...", run_name)
        self._stats['updates_detected'] += 1
        self._stats['last_update_run'] = run_name
        self._stats['last_update_ts'] = time.time()

        if self._shared:
            self._shared.emit('gfs_refresh', 'gfs_update_detected', {
                'run': run_name,
                'ts': time.time(),
            })

        # Step 1: Snapshot current prices BEFORE refresh
        self._snapshot_prices(all_signals)

        # Step 2: Invalidate gamma cache to force fresh market data
        if gamma_client_module:
            try:
                gamma_client_module.invalidate_cache()
                log.info("GFS_REFRESH: gamma cache invalidated")
            except Exception as e:
                log.warning("GFS_REFRESH: failed to invalidate gamma cache: %s", e)

        # Step 3: Mark this run as processed
        today = datetime.now(timezone.utc).date()
        run_key = f"{run_name}_{today.isoformat()}"
        self._last_processed_runs[run_key] = time.time()

        # Clean old processed runs (keep last 7 days)
        cutoff = time.time() - 7 * 86400
        self._last_processed_runs = {
            k: v for k, v in self._last_processed_runs.items() if v > cutoff
        }

        # Step 4: The caller will re-run signal generation after this returns.
        # We return a special marker that tells the main loop to do a full refresh.
        # The actual delta calculation happens in process_post_refresh().
        self._stats['reprices_done'] += 1
        self._update_next_expected()
        self._publish_stats()

        # Return empty for now — the main loop should call process_post_refresh()
        # after re-building signals with fresh data
        return []

    def _snapshot_prices(self, signals: list):
        """Snapshot current probability estimates before a refresh."""
        self._price_snapshots.clear()
        for sig in signals:
            cid = sig.get('condition_id', '')
            if cid:
                self._price_snapshots[cid] = {
                    'city': sig.get('city', ''),
                    'our_prob': sig.get('our_prob', 0),
                    'market_price': sig.get('market_price', 0),
                    'threshold': sig.get('threshold', 0),
                    'question': sig.get('question', '')[:80],
                    'station': sig.get('sentinel_station', ''),
                    'forecast': sig.get('forecast'),
                    'tokens': sig.get('tokens', []),
                    'snapshot_ts': time.time(),
                }
        log.info("GFS_REFRESH: snapshotted %d market prices", len(self._price_snapshots))

    def process_post_refresh(self, new_signals: list,
                              bias_module=None, sentinel=None) -> List[Dict]:
        """Called AFTER the main loop has re-generated signals with fresh GFS data.

        Compares new probabilities against pre-refresh snapshots to find
        markets where the forecast shifted enough to create new edge.

        Args:
            new_signals: Fresh signal list after GFS update
            bias_module: station_bias module for corrections
            sentinel: WeatherSentinel for live temp data

        Returns:
            List of delta trade dicts
        """
        if not self._price_snapshots:
            log.warning("GFS_REFRESH: no snapshots to compare against")
            return []

        deltas = []
        for sig in new_signals:
            cid = sig.get('condition_id', '')
            if cid not in self._price_snapshots:
                continue

            old = self._price_snapshots[cid]
            new_prob = sig.get('our_prob', 0)
            old_prob = old.get('our_prob', 0)
            market_price = sig.get('market_price', old.get('market_price', 50))
            station = sig.get('sentinel_station', old.get('station', ''))

            # Apply bias correction to new probability
            corrected_prob = new_prob
            correction_f = 0.0
            if bias_module and station and sig.get('forecast') is not None and sig.get('threshold'):
                corrected_prob, explanation = bias_module.apply_bias_to_probability(
                    station,
                    sig['threshold'] - 1, sig['threshold'] + 1,
                    new_prob, sig['forecast']
                )
                correction_f = bias_module.get_bias_correction(station)
            else:
                explanation = "no_bias"

            # Calculate the delta: how much did our probability shift?
            prob_shift = corrected_prob - old_prob
            new_edge = corrected_prob - market_price

            delta_record = {
                'city': sig.get('city', old.get('city', '')),
                'station': station,
                'question': sig.get('question', old.get('question', '')),
                'condition_id': cid,
                'old_prob': round(old_prob, 1),
                'new_prob': round(new_prob, 1),
                'corrected_prob': round(corrected_prob, 1),
                'market_price': round(market_price, 1),
                'prob_shift': round(prob_shift, 1),
                'new_edge': round(new_edge, 1),
                'bias_correction_f': correction_f,
            }
            self._delta_history.append({**delta_record, 'ts': time.time()})

            # Only trade if:
            # 1. The forecast shifted meaningfully (>3pp change)
            # 2. The new edge is above our threshold
            # 3. The shift INCREASED our edge (not decreased it)
            if abs(prob_shift) < 3.0:
                continue
            if new_edge < self.min_delta_edge_pct:
                continue
            if corrected_prob < self.min_prob_after_refresh:
                continue

            # Determine trade direction
            if new_edge > 0:
                # YES side: our model says this bin is more likely than market thinks
                token_id = None
                for tk in sig.get('tokens', old.get('tokens', [])):
                    if str(tk.get('outcome', '')).lower() == 'yes':
                        token_id = tk.get('token_id', '')
                        break
                if not token_id or token_id in self._seen_delta_tokens:
                    continue
                self._seen_delta_tokens.add(token_id)

                trade_price = market_price / 100.0
                size = min(self.max_delta_size, max(1.0, new_edge / 5.0 * 5.0))

                trade = {
                    'signal': 'GFS_DELTA_YES',
                    'city': sig.get('city', ''),
                    'station': station,
                    'question': sig.get('question', ''),
                    'condition_id': cid,
                    'token_id': token_id,
                    'tokens': sig.get('tokens', []),
                    'price': trade_price,
                    'our_prob': new_prob,
                    'corrected_prob': corrected_prob,
                    'old_prob': old_prob,
                    'market_price': market_price,
                    'prob_shift': round(prob_shift, 1),
                    'edge_pct': round(new_edge, 1),
                    'bias_correction_f': correction_f,
                    'size': size,
                    'confidence': 3 if abs(prob_shift) > 8 else 2,
                    'source': 'gfs_refresh',
                    'end_date': sig.get('end_date', ''),
                    'threshold': sig.get('threshold', 0),
                    'ev': round(new_edge * size / 100, 2),
                }
                deltas.append(trade)
                log.info("GFS_DELTA: %s %s | shift=%+.1fpp | old=%.1f%% new=%.1f%% (corrected=%.1f%%) mkt=%.1f%% | edge=%.1f%%",
                         sig.get('city', ''), sig.get('question', '')[:40],
                         prob_shift, old_prob, new_prob, corrected_prob,
                         market_price, new_edge)

        if len(self._delta_history) > 500:
            self._delta_history = self._delta_history[-500:]

        # Rank by edge, cap
        deltas.sort(key=lambda d: d.get('edge_pct', 0), reverse=True)
        deltas = deltas[:self.max_trades_per_refresh]

        if deltas:
            self._stats['delta_trades'] += len(deltas)
            log.info("GFS_REFRESH: %d delta trades from forecast shift | top edge=%.1f%%",
                     len(deltas), deltas[0].get('edge_pct', 0))
            if self._shared:
                self._shared.emit('gfs_refresh', 'gfs_reprice_complete', {
                    'delta_count': len(deltas),
                    'top_city': deltas[0].get('city', ''),
                    'top_edge': deltas[0].get('edge_pct', 0),
                })

        # Also check for EXIT signals: positions where forecast shifted AGAINST us
        # This is published to shared state for the exit engine to consume
        exit_warnings = []
        for sig in new_signals:
            cid = sig.get('condition_id', '')
            if cid not in self._price_snapshots:
                continue
            old = self._price_snapshots[cid]
            new_prob = sig.get('our_prob', 0)
            old_prob = old.get('our_prob', 0)
            prob_shift = new_prob - old_prob
            # If our probability dropped significantly, warn the exit engine
            if prob_shift < -10:
                exit_warnings.append({
                    'city': sig.get('city', ''),
                    'condition_id': cid,
                    'old_prob': old_prob,
                    'new_prob': new_prob,
                    'shift': round(prob_shift, 1),
                    'warning': f'GFS shift {prob_shift:+.1f}pp — model confidence dropped',
                })

        if exit_warnings and self._shared:
            self._shared.publish('gfs_refresh', 'exit_warnings', exit_warnings)
            self._shared.emit('gfs_refresh', 'forecast_deteriorated', {
                'count': len(exit_warnings),
                'cities': [w['city'] for w in exit_warnings],
            })
            log.warning("GFS_REFRESH: %d positions with deteriorated forecast!", len(exit_warnings))

        self._publish_stats()
        return deltas

    def _publish_stats(self):
        """Publish stats to shared state."""
        if not self._shared:
            return
        self._shared.publish('gfs_refresh', 'status', self._stats)
        self._shared.publish('gfs_refresh', 'recent_deltas', self._delta_history[-20:])

    def get_stats(self) -> dict:
        """Return stats for API/dashboard."""
        return {
            **self._stats,
            'snapshots_held': len(self._price_snapshots),
            'delta_history_count': len(self._delta_history),
            'recent_deltas': self._delta_history[-10:],
            'processed_runs': list(self._last_processed_runs.keys())[-10:],
        }

    def needs_check(self) -> bool:
        """Quick check if we should run the GFS check this cycle."""
        in_window, _ = self.is_gfs_update_window()
        return in_window
