"""
bin_sniper.py — Bin Sniper Agent

Watches for newly posted temperature markets on Polymarket and snipes
bins where OUR bias-corrected forecast disagrees with the market's
initial pricing. Runs on a fast poll loop (every 2-3 minutes).

Key edge: when a market first posts, the initial prices are set by
Polymarket's market maker based on THEIR forecast model. If our model
(corrected for station bias) says a different bin is most likely, we
buy that bin before other bots correct the price.

Communicates via RufloSharedState:
  - Reads: station_bias/corrections, sentinel/all_states, accuracy_tracker/report
  - Publishes: bin_sniper/new_markets, bin_sniper/snipe_trades, bin_sniper/stats
  - Emits: 'new_market_detected', 'snipe_executed', 'snipe_skipped'

Also integrates with:
  - gamma_client: for market discovery
  - station_bias: for bias-corrected probability
  - active_trader: for entry kill switch
  - trade pipeline: records trades to _trade_log + trade_ledger
"""

import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


class BinSniper:
    """Detects newly posted markets and snipes mispriced bins using
    bias-corrected forecasts.

    Args:
        shared_state: RufloSharedState instance for inter-agent communication
        poll_interval_s: How often to check for new markets (default 150s = 2.5 min)
    """

    def __init__(self, shared_state=None, poll_interval_s: int = 150):
        self._shared = shared_state
        self.poll_interval = poll_interval_s
        self._last_poll = 0.0
        self._known_market_ids: set = set()  # Markets we've already seen
        self._snipe_history: list = []        # Recent snipe decisions
        self._stats = {
            'total_polls': 0,
            'new_markets_found': 0,
            'snipes_attempted': 0,
            'snipes_skipped': 0,
            'last_poll_ts': 0,
            'last_new_market_ts': 0,
        }
        # Thresholds
        self.min_edge_pct = 8.0       # Minimum edge (our_prob - market) to snipe
        self.min_prob_pct = 15.0      # Minimum our_prob to consider a bin
        self.max_market_price = 0.85  # Don't snipe bins already priced > 85c
        self.min_market_price = 0.01  # Don't snipe bins priced < 1c (no liquidity)
        self.max_snipe_size = 15.0    # Max $ per snipe trade
        self.max_snipes_per_cycle = 5 # Cap snipes per poll cycle
        self.max_age_minutes = 60     # Only snipe markets posted within last 60 min
        self._seen_snipes: set = set()  # token_ids we've already sniped

        if shared_state:
            shared_state.register_agent('bin_sniper',
                'Detects new markets, snipes mispriced bins using bias-corrected forecasts')
        log.info("BIN_SNIPER: initialized (poll_interval=%ds, min_edge=%.1f%%)",
                 poll_interval_s, self.min_edge_pct)

    def needs_poll(self) -> bool:
        """Check if it's time to poll for new markets."""
        return (time.time() - self._last_poll) >= self.poll_interval

    def poll_and_snipe(self, all_markets: list, all_signals: list,
                       bias_module=None, sentinel=None) -> List[Dict]:
        """Main entry point. Called from the auto-trade loop.

        1. Detect markets we haven't seen before
        2. For new markets, get bias-corrected probabilities
        3. Compare our corrected prob vs market price
        4. Return snipe opportunities (caller handles execution)

        Args:
            all_markets: Full list of discovered markets from gamma_client
            all_signals: Full signal list from _build_signals (with probabilities)
            bias_module: station_bias module for corrections
            sentinel: WeatherSentinel for live temperature data

        Returns:
            List of snipe trade dicts ready for execution
        """
        self._last_poll = time.time()
        self._stats['total_polls'] += 1
        self._stats['last_poll_ts'] = self._last_poll

        # Step 1: Detect new markets
        new_markets = self._detect_new_markets(all_markets)
        if not new_markets:
            self._publish_stats()
            return []

        self._stats['new_markets_found'] += len(new_markets)
        self._stats['last_new_market_ts'] = time.time()
        log.info("BIN_SNIPER: %d NEW markets detected!", len(new_markets))

        if self._shared:
            self._shared.emit('bin_sniper', 'new_market_detected', {
                'count': len(new_markets),
                'cities': list(set(m.get('city', '') for m in new_markets)),
            })

        # Step 2: Build snipe opportunities
        snipes = []
        for market in new_markets:
            market_snipes = self._evaluate_market(market, all_signals, bias_module, sentinel)
            snipes.extend(market_snipes)

        # Step 3: Rank by edge and cap
        snipes.sort(key=lambda s: s.get('edge_pct', 0), reverse=True)
        snipes = snipes[:self.max_snipes_per_cycle]

        # Record
        for s in snipes:
            self._snipe_history.append({**s, 'ts': time.time()})
            self._stats['snipes_attempted'] += 1
            if self._shared:
                self._shared.emit('bin_sniper', 'snipe_executed', {
                    'city': s.get('city'), 'bin': s.get('question', '')[:60],
                    'edge': s.get('edge_pct', 0), 'side': s.get('signal'),
                })

        if len(self._snipe_history) > 200:
            self._snipe_history = self._snipe_history[-200:]

        self._publish_stats()
        if snipes:
            log.info("BIN_SNIPER: returning %d snipe trades | top edge=%.1f%%",
                     len(snipes), snipes[0].get('edge_pct', 0))
        return snipes

    def _detect_new_markets(self, all_markets: list) -> list:
        """Find markets we haven't seen before."""
        new = []
        for m in all_markets:
            mid = m.get('market_id', m.get('slug', ''))
            if mid and mid not in self._known_market_ids:
                self._known_market_ids.add(mid)
                new.append(m)

        # Also check freshness — only snipe very recent markets
        # (Markets we missed earlier are already being priced by other bots)
        now = datetime.now(timezone.utc)
        fresh_new = []
        for m in new:
            res_time = m.get('resolution_time')
            if res_time:
                if hasattr(res_time, 'timestamp'):
                    # resolution_time is a datetime object
                    hours_to_resolution = (res_time - now).total_seconds() / 3600
                else:
                    hours_to_resolution = 24  # assume ~24h if unknown
                # Markets for tomorrow (>12h away) are fresh enough to snipe
                if hours_to_resolution > 2:
                    fresh_new.append(m)

        return fresh_new

    def _evaluate_market(self, market: dict, all_signals: list,
                         bias_module=None, sentinel=None) -> list:
        """Evaluate a single market for snipe opportunities.

        Finds matching signals, applies bias correction, and returns
        trade opportunities where our corrected probability disagrees
        with the market price.
        """
        snipes = []
        city = market.get('city', '')
        station = market.get('station', '')
        market_id = market.get('market_id', '')

        # Find signals matching this market
        matching_signals = [
            s for s in all_signals
            if s.get('city', '').lower() == city.lower()
            or s.get('condition_id', '') == market_id
        ]

        if not matching_signals:
            log.debug("BIN_SNIPER: no signals for %s/%s", city, market_id[:12])
            return []

        # Get bias correction for this station
        correction_f = 0.0
        bias_info = {}
        if bias_module and station:
            bias_info = bias_module.get_station_bias(station)
            correction_f = bias_info.get('correction_f', 0.0)

        # Also check shared state for corrections from other agents
        if self._shared and correction_f == 0.0:
            corrections = self._shared.read('station_bias', 'corrections')
            if corrections and station in corrections:
                correction_f = corrections[station]

        # Get sentinel data for live temperature context
        sentinel_temp_f = None
        if sentinel:
            state = sentinel.get_station_state(station) if hasattr(sentinel, 'get_station_state') else {}
            trend = state.get('trend', {})
            sentinel_temp_f = trend.get('current_f')

        for sig in matching_signals:
            our_prob = sig.get('our_prob', 0)
            market_price_pct = sig.get('market_price', 50)
            threshold = sig.get('threshold', 0)
            forecast_temp = sig.get('forecast')

            # Apply bias correction to our probability
            if bias_module and station and forecast_temp is not None and threshold:
                bin_lo = threshold - 1
                bin_hi = threshold + 1
                corrected_prob, explanation = bias_module.apply_bias_to_probability(
                    station, bin_lo, bin_hi, our_prob, forecast_temp
                )
            else:
                corrected_prob = our_prob
                explanation = "no_bias_applied"

            # Now compare OUR corrected prob vs market price
            edge_pct = corrected_prob - market_price_pct
            market_price_decimal = market_price_pct / 100.0

            # Determine if this is a snipe opportunity
            if edge_pct < self.min_edge_pct:
                continue
            if corrected_prob < self.min_prob_pct:
                continue
            if market_price_decimal > self.max_market_price:
                continue
            if market_price_decimal < self.min_market_price:
                continue

            # Get token info
            yes_token_id = None
            for tk in sig.get('tokens', []):
                if str(tk.get('outcome', '')).lower() == 'yes':
                    yes_token_id = tk.get('token_id', '')
                    break
            if not yes_token_id:
                # Try from market prices
                prices = market.get('prices', {})
                for tid, price in prices.items():
                    if abs(price - market_price_decimal) < 0.05:
                        yes_token_id = tid
                        break

            if not yes_token_id or yes_token_id in self._seen_snipes:
                continue

            self._seen_snipes.add(yes_token_id)

            # Calculate size based on edge confidence
            size = min(self.max_snipe_size, max(1.0, edge_pct / 5.0 * 5.0))

            snipe = {
                'signal': 'SNIPE_YES',
                'city': city,
                'station': station,
                'question': sig.get('question', market.get('question', '')),
                'condition_id': sig.get('condition_id', market_id),
                'token_id': yes_token_id,
                'tokens': sig.get('tokens', market.get('tokens', [])),
                'price': market_price_decimal,
                'our_prob': our_prob,
                'corrected_prob': corrected_prob,
                'market_price': market_price_pct,
                'edge_pct': round(edge_pct, 1),
                'bias_correction_f': correction_f,
                'bias_explanation': explanation,
                'sentinel_temp_f': sentinel_temp_f,
                'size': size,
                'confidence': 4 if edge_pct > 20 else (3 if edge_pct > 12 else 2),
                'source': 'bin_sniper',
                'end_date': sig.get('end_date', ''),
                'threshold': threshold,
                'ev': round(edge_pct * size / 100, 2),
            }
            snipes.append(snipe)
            log.info("BIN_SNIPER: SNIPE %s %s | our=%.1f%% (corrected=%.1f%%) vs mkt=%.1f%% | edge=%.1f%% | bias=%+.1f°F",
                     city, sig.get('question', '')[:40], our_prob, corrected_prob,
                     market_price_pct, edge_pct, correction_f)

        # Also check for NO-side snipes (market overpricing a bin)
        for sig in matching_signals:
            our_prob = sig.get('our_prob', 0)
            market_price_pct = sig.get('market_price', 50)

            # Apply bias correction
            if bias_module and station and sig.get('forecast') is not None and sig.get('threshold'):
                corrected_prob, _ = bias_module.apply_bias_to_probability(
                    station, sig['threshold'] - 1, sig['threshold'] + 1,
                    our_prob, sig['forecast']
                )
            else:
                corrected_prob = our_prob

            # NO-side: market thinks YES is likely, we think NO
            no_edge = market_price_pct - corrected_prob
            if no_edge < self.min_edge_pct:
                continue
            if corrected_prob > 15:  # We still think there's a decent chance — skip
                continue

            no_token_id = None
            for tk in sig.get('tokens', []):
                if str(tk.get('outcome', '')).lower() == 'no':
                    no_token_id = tk.get('token_id', '')
                    break
            if not no_token_id or no_token_id in self._seen_snipes:
                continue

            self._seen_snipes.add(no_token_id)
            no_price = round((100 - market_price_pct) / 100, 4)
            size = min(self.max_snipe_size, max(1.0, no_edge / 5.0 * 5.0))

            snipe = {
                'signal': 'SNIPE_NO',
                'city': city,
                'station': station,
                'question': sig.get('question', market.get('question', '')),
                'condition_id': sig.get('condition_id', market.get('market_id', '')),
                'token_id': no_token_id,
                'tokens': sig.get('tokens', market.get('tokens', [])),
                'price': no_price,
                'our_prob': our_prob,
                'corrected_prob': corrected_prob,
                'market_price': market_price_pct,
                'edge_pct': round(no_edge, 1),
                'bias_correction_f': correction_f,
                'sentinel_temp_f': sentinel_temp_f,
                'size': size,
                'confidence': 4 if no_edge > 20 else (3 if no_edge > 12 else 2),
                'source': 'bin_sniper',
                'end_date': sig.get('end_date', ''),
                'threshold': sig.get('threshold', 0),
                'ev': round(no_edge * size / 100, 2),
            }
            snipes.append(snipe)
            log.info("BIN_SNIPER: SNIPE_NO %s | mkt_yes=%.1f%% our_yes=%.1f%% | NO edge=%.1f%%",
                     city, market_price_pct, corrected_prob, no_edge)

        return snipes

    def _publish_stats(self):
        """Publish stats and trade history to shared state."""
        if not self._shared:
            return
        self._shared.publish('bin_sniper', 'stats', self._stats)
        self._shared.publish('bin_sniper', 'recent_snipes', self._snipe_history[-20:])
        self._shared.publish('bin_sniper', 'known_markets', len(self._known_market_ids))

    def get_stats(self) -> dict:
        """Return current stats for API/dashboard."""
        return {
            **self._stats,
            'known_market_count': len(self._known_market_ids),
            'snipe_history_count': len(self._snipe_history),
            'recent_snipes': self._snipe_history[-10:],
        }

    def seed_known_markets(self, markets: list):
        """Seed the known markets set on startup so we don't snipe old markets.

        Call this with the initial get_markets() result before starting the poll loop.
        """
        for m in markets:
            mid = m.get('market_id', m.get('slug', ''))
            if mid:
                self._known_market_ids.add(mid)
        log.info("BIN_SNIPER: seeded %d known markets (will only snipe NEW ones)",
                 len(self._known_market_ids))
