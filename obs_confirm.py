"""
obs_confirm.py — Observation Confirmation & Kill Agent

The highest-conviction agent in the entire system. Uses LIVE METAR
observations from ALL 58 cities (not just 12 US NWS stations) to make
two types of trades:

1. CONFIRMATION TRADES — When the current observation is INSIDE a bin
   that's still priced below 80%, the market is wrong. The bin WILL
   resolve YES (or is overwhelmingly likely to). We buy aggressively.

2. KILL TRADES — When the observation proves a bin is IMPOSSIBLE (temp
   already exceeded bin_hi after peak, or max_achievable < bin_lo), the
   YES token is worthless. We sell any position and/or buy NO.

Why this is the best edge:
  - Observations are FACTS, not forecasts. When the thermometer says
    74°F and the bin is 74-75°F, the probability should be 85-95%.
  - The market often lags 10-30 minutes behind observations because
    most bots run on fixed cycles (5-15 min), not real-time METAR.
  - International markets are especially mispriced because fewer bots
    have METAR data for Tokyo, Singapore, Beijing, etc.

Communicates via RufloSharedState:
  Reads:  sentinel/all_states, station_bias/corrections,
          accuracy_tracker/excellent_stations
  Publishes: obs_confirm/confirmed_bins, obs_confirm/killed_bins,
             obs_confirm/live_obs, obs_confirm/trades, obs_confirm/stats
  Emits: 'obs_confirmed', 'obs_killed', 'obs_approaching'

Architecture:
  - Uses WeatherSentinel's METAR data for ALL cities (no NWS dependency)
  - Falls back to direct METAR API if sentinel data is stale
  - Applies station bias correction before comparing to bins
  - Runs every cycle (1 min) — speed is the edge here
"""

import logging
import math
import time
import requests
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# METAR API for direct fallback when sentinel is stale
METAR_API = 'https://aviationweather.gov/api/data/metar'

# Full 58-city ICAO mapping built from config.py
# This is the critical piece: we cover EVERY city, not just 12 US ones
CITY_ICAO = {
    # US (14)
    'New York': 'KJFK', 'Los Angeles': 'KLAX', 'Chicago': 'KORD',
    'Miami': 'KMIA', 'Dallas': 'KDFW', 'Denver': 'KDEN',
    'Seattle': 'KSEA', 'Boston': 'KBOS', 'Phoenix': 'KPHX',
    'Minneapolis': 'KMSP', 'Las Vegas': 'KLAS', 'San Francisco': 'KSFO',
    'Atlanta': 'KATL', 'Houston': 'KIAH',
    # Canada (3)
    'Toronto': 'CYYZ', 'Vancouver': 'CYVR', 'Montreal': 'CYUL',
    # Mexico (1)
    'Mexico City': 'MMMX',
    # Europe (14)
    'London': 'EGLL', 'Dublin': 'EIDW', 'Paris': 'LFPG',
    'Amsterdam': 'EHAM', 'Berlin': 'EDDF', 'Frankfurt': 'EDDF',
    'Madrid': 'LEMD', 'Barcelona': 'LEIB', 'Rome': 'LIRF',
    'Milan': 'LIML', 'Athens': 'LGAV', 'Lisbon': 'LPPT',
    'Stockholm': 'ESSA', 'Copenhagen': 'EKCH', 'Moscow': 'UUWW',
    'Warsaw': 'EPWA',
    # Middle East (3)
    'Dubai': 'OMDB', 'Istanbul': 'LTAC', 'Tel Aviv': 'LLBG',
    # Asia (9)
    'Mumbai': 'VABB', 'Delhi': 'VIDP', 'Bangalore': 'VOBL',
    'Singapore': 'WSSS', 'Bangkok': 'VTBS', 'Hong Kong': 'VHHH',
    'Tokyo': 'RJTT', 'Seoul': 'RKSI', 'Shanghai': 'ZSPD',
    'Beijing': 'ZBAA',
    # Oceania (3)
    'Sydney': 'YSSY', 'Melbourne': 'YMML', 'Auckland': 'NZAA',
    # South America (4)
    'São Paulo': 'SBGR', 'Sao Paulo': 'SBGR', 'Rio de Janeiro': 'SBGL',
    'Buenos Aires': 'SAEZ', 'Santiago': 'SCEL',
    # Africa (3)
    'Cairo': 'HECA', 'Johannesburg': 'FAOR', 'Lagos': 'DNAA',
}
ICAO_CITY = {v: k for k, v in CITY_ICAO.items()}

# City timezone offsets from UTC (approximate, used for diurnal modeling)
CITY_TZ_OFFSET = {
    # US
    'New York': -4, 'Los Angeles': -7, 'Chicago': -5, 'Miami': -4,
    'Dallas': -5, 'Denver': -6, 'Seattle': -7, 'Boston': -4,
    'Phoenix': -7, 'Minneapolis': -5, 'Las Vegas': -7, 'San Francisco': -7,
    'Atlanta': -4, 'Houston': -5,
    # Canada
    'Toronto': -4, 'Vancouver': -7, 'Montreal': -4,
    # Mexico
    'Mexico City': -6,
    # Europe
    'London': 1, 'Dublin': 1, 'Paris': 2, 'Amsterdam': 2,
    'Berlin': 2, 'Frankfurt': 2, 'Madrid': 2, 'Barcelona': 2,
    'Rome': 2, 'Milan': 2, 'Athens': 3, 'Lisbon': 1,
    'Stockholm': 2, 'Copenhagen': 2, 'Moscow': 3, 'Warsaw': 2,
    # Middle East
    'Dubai': 4, 'Istanbul': 3, 'Tel Aviv': 3,
    # Asia
    'Mumbai': 5, 'Delhi': 5, 'Bangalore': 5, 'Singapore': 8,
    'Bangkok': 7, 'Hong Kong': 8, 'Tokyo': 9, 'Seoul': 9,
    'Shanghai': 8, 'Beijing': 8,
    # Oceania
    'Sydney': 11, 'Melbourne': 11, 'Auckland': 13,
    # South America
    'São Paulo': -3, 'Sao Paulo': -3, 'Rio de Janeiro': -3,
    'Buenos Aires': -3, 'Santiago': -3,
    # Africa
    'Cairo': 2, 'Johannesburg': 2, 'Lagos': 1,
}


def _local_hour(city: str) -> int:
    """Get approximate local hour for a city."""
    utc_hour = datetime.now(timezone.utc).hour
    offset = CITY_TZ_OFFSET.get(city, 0)
    return (utc_hour + offset) % 24


def _max_achievable(obs_f: float, local_hour: int, coastal: bool = False) -> float:
    """Estimate max temperature achievable today from current observation.

    Refined model: coastal cities peak later and heat slower.
    Inland cities peak 2-3pm, coastal peak 3-4pm.
    """
    if obs_f is None:
        return 999.0

    peak_hour = 16 if coastal else 15
    if local_hour >= peak_hour:
        # Past peak — temperature is falling. Max is already achieved.
        return obs_f

    hours_to_peak = peak_hour - local_hour
    if hours_to_peak <= 0:
        return obs_f

    # Heating rate depends on time of day
    if local_hour < 8:
        rate = 1.5  # slow early morning heating
    elif local_hour < 11:
        rate = 2.5 if not coastal else 2.0  # ramp up
    elif local_hour < 13:
        rate = 2.0 if not coastal else 1.5  # approaching peak
    elif local_hour < peak_hour:
        rate = 1.0 if not coastal else 0.8  # near peak, slowing
    else:
        rate = 0.0

    remaining_heat = min(hours_to_peak * rate, 12.0)  # cap at 12°F remaining
    return obs_f + remaining_heat


class ObsConfirmAgent:
    """Real-time observation confirmation agent.

    Checks live METAR observations against all open markets and positions.
    When an observation confirms or kills a bin, it trades immediately.

    Args:
        shared_state: RufloSharedState instance
    """

    def __init__(self, shared_state=None):
        self._shared = shared_state

        # Observation cache: city -> {temp_f, temp_c, ts, station, source}
        self._obs_cache: Dict[str, Dict] = {}
        self._obs_max_age = 900  # 15 min cache
        self._direct_poll_interval = 300  # Direct METAR poll every 5 min
        self._last_direct_poll = 0.0

        # Trade tracking
        self._confirmed_bins: list = []
        self._killed_bins: list = []
        self._trades: list = []
        self._approaching: list = []
        self._seen_confirms: set = set()  # token_ids already confirm-traded
        self._seen_kills: set = set()     # token_ids already kill-traded

        # Thresholds
        self.confirm_max_price = 0.80     # Only buy confirm if YES < 80c
        self.confirm_min_price = 0.15     # Don't confirm if already near-worthless
        self.confirm_size = 20.0          # $20 per confirmation trade (high confidence)
        self.kill_min_no_price = 0.80     # Only buy NO when priced >= 80c
        self.kill_size = 25.0             # $25 per kill trade
        self.approach_distance_f = 3.0    # Alert when within 3°F of bin boundary
        self.max_trades_per_cycle = 10

        # Stats
        self._stats = {
            'total_checks': 0,
            'confirms_found': 0,
            'kills_found': 0,
            'approaching_found': 0,
            'trades_executed': 0,
            'cities_with_obs': 0,
            'last_check_ts': 0,
        }

        if shared_state:
            shared_state.register_agent('obs_confirm',
                'Real-time METAR observation confirmation/kill agent for all 58 cities')
        log.info("OBS_CONFIRM: initialized for %d cities", len(CITY_ICAO))

    # ─── Observation gathering ─────────────────────────────────────

    def _get_obs(self, city: str, sentinel=None) -> Optional[Dict]:
        """Get current observation for a city. Priority:
        1. WeatherSentinel (already polled, most recent)
        2. SharedState (other agents may have published)
        3. Direct METAR API fallback
        4. Cache (stale data better than nothing)
        """
        now = time.time()
        icao = CITY_ICAO.get(city)
        if not icao:
            return None

        # Source 1: WeatherSentinel (best — already polled this cycle)
        if sentinel:
            try:
                state = sentinel.get_station_state(icao)
                latest = state.get('latest')
                if latest and latest.get('temp_c') is not None:
                    temp_c = latest['temp_c']
                    temp_f = round(temp_c * 9/5 + 32, 1)
                    obs = {
                        'temp_f': temp_f, 'temp_c': temp_c,
                        'ts': latest.get('ts', now), 'station': icao,
                        'source': 'sentinel',
                    }
                    self._obs_cache[city] = obs
                    return obs
            except Exception:
                pass

        # Source 2: SharedState sentinel data
        if self._shared:
            try:
                states = self._shared.read('sentinel', 'all_states')
                if states and isinstance(states, dict):
                    # states might be the full get_all_states() return
                    stations = states.get('stations', states)
                    for sid, sdata in stations.items():
                        if sid == icao:
                            trend = sdata.get('trend', {})
                            temp_f = trend.get('current_f')
                            if temp_f is not None:
                                obs = {
                                    'temp_f': temp_f, 'temp_c': trend.get('current_c'),
                                    'ts': now, 'station': icao,
                                    'source': 'shared_state',
                                }
                                self._obs_cache[city] = obs
                                return obs
            except Exception:
                pass

        # Source 3: Cache (if fresh enough)
        cached = self._obs_cache.get(city)
        if cached and (now - cached.get('ts', 0)) < self._obs_max_age:
            return cached

        # Source 4: Direct METAR poll (batch — don't do per-city)
        if cached:
            return cached  # Return stale cache rather than hammering API

        return None

    def _poll_direct_metar(self, cities: list) -> Dict[str, Dict]:
        """Batch poll METAR for cities missing sentinel data."""
        now = time.time()
        if now - self._last_direct_poll < self._direct_poll_interval:
            return {}
        self._last_direct_poll = now

        # Find cities that need direct polling
        need_poll = []
        for city in cities:
            icao = CITY_ICAO.get(city)
            if not icao:
                continue
            cached = self._obs_cache.get(city)
            if not cached or (now - cached.get('ts', 0)) > self._obs_max_age:
                need_poll.append(icao)

        if not need_poll:
            return {}

        results = {}
        # Batch request (max ~20 at a time to avoid timeout)
        for batch_start in range(0, len(need_poll), 20):
            batch = need_poll[batch_start:batch_start + 20]
            station_ids = ','.join(batch)
            try:
                resp = requests.get(
                    METAR_API,
                    params={'ids': station_ids, 'format': 'json', 'hours': 1},
                    timeout=12,
                )
                resp.raise_for_status()
                metar_data = resp.json()
                for obs in metar_data:
                    sid = obs.get('icaoId', obs.get('stationId', ''))
                    temp_c = obs.get('temp')
                    if sid and temp_c is not None:
                        city_name = ICAO_CITY.get(sid, sid)
                        temp_f = round(float(temp_c) * 9/5 + 32, 1)
                        entry = {
                            'temp_f': temp_f, 'temp_c': float(temp_c),
                            'ts': now, 'station': sid,
                            'source': 'direct_metar',
                        }
                        self._obs_cache[city_name] = entry
                        results[city_name] = entry
            except Exception as e:
                log.debug("OBS_CONFIRM: direct METAR batch failed: %s", e)

        if results:
            log.info("OBS_CONFIRM: direct METAR polled %d cities", len(results))
        return results

    # ─── Core logic ────────────────────────────────────────────────

    def check_and_trade(self, all_signals: list, open_trades: list,
                        sentinel=None, bias_module=None) -> List[Dict]:
        """Main entry point. Called every trade cycle (1 minute).

        1. Gather observations for all cities with active markets/positions
        2. Check each observation against bins for confirm/kill
        3. Return trade actions

        Args:
            all_signals: Full signal list from _build_signals
            open_trades: List of open trades from _trade_log
            sentinel: WeatherSentinel instance
            bias_module: station_bias module

        Returns:
            List of trade action dicts ready for execution
        """
        self._stats['total_checks'] += 1
        self._stats['last_check_ts'] = time.time()

        # Collect all cities that have active markets or positions
        active_cities = set()
        for sig in all_signals:
            city = sig.get('city', '')
            if city:
                active_cities.add(city)
        for t in open_trades:
            if isinstance(t, dict):
                city = t.get('city', '')
                if city:
                    active_cities.add(city)

        if not active_cities:
            return []

        # Gather observations
        obs_count = 0
        for city in active_cities:
            obs = self._get_obs(city, sentinel=sentinel)
            if obs:
                obs_count += 1

        # Direct poll for cities missing data
        missing_cities = [c for c in active_cities if c not in self._obs_cache
                          or (time.time() - self._obs_cache[c].get('ts', 0)) > self._obs_max_age]
        if missing_cities:
            self._poll_direct_metar(missing_cities)
            for city in missing_cities:
                if city in self._obs_cache:
                    obs_count += 1

        self._stats['cities_with_obs'] = obs_count
        trades = []
        self._confirmed_bins.clear()
        self._killed_bins.clear()
        self._approaching.clear()

        # Check signals (all market bins)
        for sig in all_signals:
            city = sig.get('city', '')
            obs = self._obs_cache.get(city)
            if not obs:
                continue

            temp_f = obs['temp_f']
            station = obs.get('station', '')
            threshold = sig.get('threshold', 0)
            if not threshold:
                continue

            # Apply bias correction to observation interpretation
            correction = 0.0
            if bias_module and station:
                try:
                    correction = bias_module.get_bias_correction(station)
                except Exception:
                    pass

            # Determine bin boundaries (Polymarket uses 2°F bins typically)
            direction = sig.get('direction', 'exact')
            if direction == 'above':
                bin_lo, bin_hi = threshold, 999.0
            elif direction == 'below':
                bin_lo, bin_hi = 0.0, threshold
            else:
                bin_lo = threshold - 1
                bin_hi = threshold + 1

            local_hr = _local_hour(city)
            market_price_pct = sig.get('market_price', 50)
            market_price = market_price_pct / 100.0
            our_prob = sig.get('our_prob', 0)

            # ── CONFIRMATION CHECK ──
            # Is the observation already inside this bin?
            if bin_lo <= temp_f <= bin_hi:
                # CONFIRMED! Temperature is inside the bin right now.
                # If it's past peak hour, this bin almost certainly resolves YES.
                # If it's before peak, there's still time for temp to leave the bin,
                # but the probability should still be very high.
                is_past_peak = local_hr >= (16 if sig.get('coastal', False) else 15)
                is_near_peak = local_hr >= 12

                if is_past_peak:
                    # Temperature is in the bin AND cooling has started.
                    # This is near-certain: 95%+ probability.
                    fair_value = 0.95
                elif is_near_peak:
                    # In the bin, approaching peak. Very likely but not locked in.
                    fair_value = 0.85
                else:
                    # Morning — temp is in bin but could leave. Still strongly positive.
                    fair_value = 0.70

                # Adjust with bias: if station runs warm, the "confirmed" might
                # actually overshoot the bin
                if correction > 1.0 and temp_f > (bin_hi - 0.5):
                    fair_value *= 0.9  # Slight discount — might overshoot

                confirm_entry = {
                    'city': city, 'station': station, 'obs_temp_f': temp_f,
                    'bin_lo': bin_lo, 'bin_hi': bin_hi,
                    'fair_value': round(fair_value, 2),
                    'market_price': market_price,
                    'edge': round(fair_value - market_price, 3),
                    'local_hour': local_hr, 'past_peak': is_past_peak,
                    'question': sig.get('question', '')[:80],
                    'bias_correction': correction,
                }
                self._confirmed_bins.append(confirm_entry)
                self._stats['confirms_found'] += 1

                # Generate trade if market is underpricing
                if market_price < self.confirm_max_price and market_price > self.confirm_min_price:
                    edge = fair_value - market_price
                    if edge > 0.05:
                        # Find YES token
                        yes_token = None
                        for tk in sig.get('tokens', []):
                            if str(tk.get('outcome', '')).lower() == 'yes':
                                yes_token = tk.get('token_id', '')
                                break
                        if yes_token and yes_token not in self._seen_confirms:
                            self._seen_confirms.add(yes_token)
                            # Size scales with conviction: post-peak gets full size
                            size = self.confirm_size if is_past_peak else (self.confirm_size * 0.6)
                            trade = {
                                'signal': 'OBS_CONFIRM_YES',
                                'city': city,
                                'station': station,
                                'question': sig.get('question', '')[:80],
                                'condition_id': sig.get('condition_id', ''),
                                'token_id': yes_token,
                                'tokens': sig.get('tokens', []),
                                'price': market_price,
                                'our_prob': round(fair_value * 100, 1),
                                'corrected_prob': round(fair_value * 100, 1),
                                'market_price': market_price_pct,
                                'edge_pct': round(edge * 100, 1),
                                'obs_temp_f': temp_f,
                                'bias_correction_f': correction,
                                'size': round(size, 1),
                                'confidence': 5 if is_past_peak else 4,
                                'source': 'obs_confirm',
                                'end_date': sig.get('end_date', ''),
                                'threshold': threshold,
                                'ev': round(edge * size, 2),
                                'reason': f"OBS_CONFIRM: {temp_f:.1f}F inside {bin_lo}-{bin_hi}F ({'post-peak' if is_past_peak else 'pre-peak'})",
                            }
                            trades.append(trade)
                            log.info("OBS_CONFIRM: %s %.1fF inside %d-%dF | mkt=%.0f%% fair=%.0f%% | %s",
                                     city, temp_f, bin_lo, bin_hi, market_price*100, fair_value*100,
                                     'POST-PEAK' if is_past_peak else 'PRE-PEAK')

                continue  # Skip kill check — obs is inside this bin

            # ── KILL CHECK ──
            # Is this bin physically impossible given the observation?
            achievable = _max_achievable(temp_f, local_hr, sig.get('coastal', False))

            # Apply bias: if station consistently resolves warmer, adjust achievable up
            achievable_corrected = achievable + max(0, correction)

            kill_reason = None
            if achievable_corrected < bin_lo and local_hr >= 10:
                # Can't reach the bin even with max remaining heating
                kill_reason = f"OBS_KILL: achievable={achievable_corrected:.1f}F < bin_lo={bin_lo}F (obs={temp_f:.1f}F hr={local_hr})"
            elif local_hr >= 16 and temp_f > bin_hi + 1.5:
                # Past peak and already above the bin — cooling means it'll never come back down into a lower bin
                # Actually this means the high exceeded this bin, so it depends on direction
                if direction != 'above':
                    kill_reason = f"OBS_KILL: post-peak obs={temp_f:.1f}F > bin_hi={bin_hi}F+1.5, cooling"
            elif local_hr >= 16 and temp_f < bin_lo - 1.5:
                # Past peak and below the bin — won't heat up anymore
                kill_reason = f"OBS_KILL: post-peak obs={temp_f:.1f}F < bin_lo={bin_lo}F-1.5, cooling"

            if kill_reason:
                kill_entry = {
                    'city': city, 'station': station, 'obs_temp_f': temp_f,
                    'bin_lo': bin_lo, 'bin_hi': bin_hi,
                    'achievable_f': achievable_corrected,
                    'reason': kill_reason, 'local_hour': local_hr,
                    'question': sig.get('question', '')[:80],
                }
                self._killed_bins.append(kill_entry)
                self._stats['kills_found'] += 1

                # Generate NO trade if YES is still priced high enough
                no_price = round(1.0 - market_price, 4)
                if no_price >= self.kill_min_no_price:
                    no_token = None
                    for tk in sig.get('tokens', []):
                        if str(tk.get('outcome', '')).lower() == 'no':
                            no_token = tk.get('token_id', '')
                            break
                    if no_token and no_token not in self._seen_kills:
                        self._seen_kills.add(no_token)
                        expected_return = round((1.0 - no_price) * 100, 1)
                        trade = {
                            'signal': 'OBS_KILL_NO',
                            'city': city,
                            'station': station,
                            'question': sig.get('question', '')[:80],
                            'condition_id': sig.get('condition_id', ''),
                            'token_id': no_token,
                            'tokens': sig.get('tokens', []),
                            'price': no_price,
                            'our_prob': round((1 - market_price) * 100, 1),
                            'corrected_prob': round((1 - market_price) * 100, 1),
                            'market_price': market_price_pct,
                            'edge_pct': round((1 - no_price) * 100, 1),
                            'obs_temp_f': temp_f,
                            'bias_correction_f': correction,
                            'size': self.kill_size,
                            'confidence': 5,  # Highest — this is a fact
                            'source': 'obs_confirm',
                            'end_date': sig.get('end_date', ''),
                            'threshold': threshold,
                            'ev': round(expected_return * self.kill_size / 100, 2),
                            'reason': kill_reason,
                        }
                        trades.append(trade)
                        log.info("OBS_KILL: %s %.1fF | %s | NO@%.3f exp=+%.1f%%",
                                 city, temp_f, kill_reason, no_price, expected_return)

            # ── APPROACHING CHECK ──
            # Temperature is close to a bin boundary — alert other agents
            distance = min(abs(temp_f - bin_lo), abs(temp_f - bin_hi))
            if distance <= self.approach_distance_f and distance > 0:
                approaching_from = 'below' if temp_f < bin_lo else ('above' if temp_f > bin_hi else 'inside')
                self._approaching.append({
                    'city': city, 'station': station, 'obs_temp_f': temp_f,
                    'bin_lo': bin_lo, 'bin_hi': bin_hi,
                    'distance_f': round(distance, 1),
                    'approaching_from': approaching_from,
                    'local_hour': local_hr,
                })
                self._stats['approaching_found'] += 1

        # Also check open positions for exit signals
        exit_trades = self._check_positions_for_exits(open_trades, sentinel, bias_module)
        trades.extend(exit_trades)

        # Cap total trades
        trades = trades[:self.max_trades_per_cycle]
        self._stats['trades_executed'] += len(trades)

        # Publish to shared state
        self._publish(trades)

        if trades:
            log.info("OBS_CONFIRM: %d trades | %d confirms | %d kills | %d approaching | %d cities with obs",
                     len(trades), len(self._confirmed_bins), len(self._killed_bins),
                     len(self._approaching), obs_count)
        return trades

    def _check_positions_for_exits(self, open_trades: list,
                                    sentinel=None, bias_module=None) -> list:
        """Check open positions against observations for exit signals.

        If we hold a YES position and the obs proves the bin is impossible,
        we should sell immediately (even at a loss — better than $0).
        """
        exits = []
        for t in open_trades:
            if not isinstance(t, dict) or t.get('exited'):
                continue
            city = t.get('city', '')
            obs = self._obs_cache.get(city)
            if not obs:
                continue

            temp_f = obs['temp_f']
            threshold = t.get('threshold', 0)
            if not threshold:
                continue

            signal = t.get('signal', '')
            local_hr = _local_hour(city)

            # For YES positions: check if bin is now impossible
            if signal in ('BUY YES', 'SNIPE_YES', 'OBS_CONFIRM_YES', 'YES_HARVEST', 'GFS_DELTA_YES'):
                bin_lo = threshold - 1
                bin_hi = threshold + 1
                achievable = _max_achievable(temp_f, local_hr)

                if achievable < bin_lo and local_hr >= 12:
                    # Our YES position is dead — bin can't be reached
                    exits.append({
                        'signal': 'OBS_EXIT_SELL',
                        'city': city,
                        'token_id': t.get('token_id', ''),
                        'reason': f"OBS_EXIT: obs={temp_f:.1f}F achievable={achievable:.1f}F < bin_lo={bin_lo}F — sell YES",
                        'obs_temp_f': temp_f,
                        'source': 'obs_confirm',
                        '_trade_ref': t,
                    })

            # For NO positions: check if bin is now confirmed (our NO loses)
            elif signal in ('NO_HARVEST', 'SNIPE_NO', 'OBS_KILL_NO'):
                bin_lo = threshold - 1
                bin_hi = threshold + 1
                if bin_lo <= temp_f <= bin_hi and local_hr >= 15:
                    exits.append({
                        'signal': 'OBS_EXIT_SELL',
                        'city': city,
                        'token_id': t.get('token_id', ''),
                        'reason': f"OBS_EXIT: obs={temp_f:.1f}F inside {bin_lo}-{bin_hi}F — our NO is losing",
                        'obs_temp_f': temp_f,
                        'source': 'obs_confirm',
                        '_trade_ref': t,
                    })

        return exits

    # ─── SharedState communication ─────────────────────────────────

    def _publish(self, trades: list):
        """Publish all data to SharedState bus."""
        if not self._shared:
            return

        self._shared.publish('obs_confirm', 'confirmed_bins', self._confirmed_bins)
        self._shared.publish('obs_confirm', 'killed_bins', self._killed_bins)
        self._shared.publish('obs_confirm', 'approaching', self._approaching)
        self._shared.publish('obs_confirm', 'trades', [
            {k: v for k, v in t.items() if k != '_trade_ref'}
            for t in (self._trades[-20:] if self._trades else trades[:20])
        ])
        self._shared.publish('obs_confirm', 'stats', self._stats)

        # Publish live observations for other agents
        self._shared.publish('obs_confirm', 'live_obs', {
            city: {
                'temp_f': obs['temp_f'], 'temp_c': obs.get('temp_c'),
                'station': obs['station'], 'source': obs['source'],
                'age_s': round(time.time() - obs['ts']),
            }
            for city, obs in self._obs_cache.items()
            if (time.time() - obs['ts']) < 1800  # Only publish fresh obs
        })

        # Emit events
        for c in self._confirmed_bins:
            self._shared.emit('obs_confirm', 'obs_confirmed', {
                'city': c['city'], 'temp_f': c['obs_temp_f'],
                'bin': f"{c['bin_lo']}-{c['bin_hi']}F",
                'fair_value': c['fair_value'],
            })
            self._shared.boost_city_priority('obs_confirm', c['city'], 20,
                f"obs_confirmed: {c['obs_temp_f']:.1f}F in {c['bin_lo']}-{c['bin_hi']}F")

        for k in self._killed_bins:
            self._shared.emit('obs_confirm', 'obs_killed', {
                'city': k['city'], 'temp_f': k['obs_temp_f'],
                'bin': f"{k['bin_lo']}-{k['bin_hi']}F",
                'reason': k['reason'],
            })

        for a in self._approaching:
            if a['distance_f'] <= 1.0:
                self._shared.boost_city_priority('obs_confirm', a['city'], 15,
                    f"obs_{a['approaching_from']}: {a['obs_temp_f']:.1f}F within {a['distance_f']:.1f}F of {a['bin_lo']}-{a['bin_hi']}F")

    # ─── API / diagnostics ─────────────────────────────────────────

    def get_stats(self) -> dict:
        return {
            **self._stats,
            'obs_cache_size': len(self._obs_cache),
            'confirmed_bins': len(self._confirmed_bins),
            'killed_bins': len(self._killed_bins),
            'approaching_bins': len(self._approaching),
            'seen_confirms': len(self._seen_confirms),
            'seen_kills': len(self._seen_kills),
            'recent_confirms': self._confirmed_bins[-5:],
            'recent_kills': self._killed_bins[-5:],
            'recent_approaching': self._approaching[-5:],
        }

    def get_live_obs(self) -> dict:
        """All current observations for the API."""
        now = time.time()
        return {
            city: {
                'temp_f': obs['temp_f'], 'temp_c': obs.get('temp_c'),
                'station': obs['station'], 'source': obs['source'],
                'age_s': round(now - obs['ts']),
                'local_hour': _local_hour(city),
            }
            for city, obs in sorted(self._obs_cache.items())
            if (now - obs['ts']) < 3600
        }
