#!/usr/bin/env python3
"""
WeatherEdge Ruflo Monitor - runs as daemon alongside the bot.
4 agents: pre-trade validator, position monitor, post-trade analyst, market scanner.
"""
import time, requests, json, logging, os
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('ruflo_monitor')

RAILWAY = 'https://weatheredge-bot-v2-production.up.railway.app'
POLYMARKET_DATA = 'https://data-api.polymarket.com'
WALLET = '0xE2FB305bE360286808e5ffa2923B70d9014a37BE'

# ============================================================
# AGENT 1 - PRE-TRADE SIGNAL VALIDATOR
# ============================================================
class PreTradeValidator:
    """Validates every signal before the bot is allowed to place a trade."""

    def validate(self, signal: dict) -> tuple:
        checks = []

        # 1. Station confidence must be >= 2
        conf = signal.get('confidence', 0)
        if conf < 2:
            return False, f'REJECT: station confidence {conf} < 2'
        checks.append('confidence OK')

        # 2. theoretical_full_ev must be > 0.10
        ev = signal.get('theo_ev', signal.get('ev', 0))
        if ev < 0.10:
            return False, f'REJECT: theo_ev {ev} < 0.10 minimum'
        checks.append(f'theo_ev {ev:.3f} OK')

        # 3. Market must not be within 1h of resolution
        end_date = signal.get('end_date', '')
        if end_date:
            try:
                end = datetime.fromisoformat(end_date.replace('Z','+00:00'))
                mins_left = (end - datetime.now(timezone.utc)).total_seconds() / 60
                if mins_left < 60:
                    return False, f'REJECT: only {mins_left:.0f} min to resolution'
                checks.append(f'{mins_left:.0f} min to resolution OK')
            except: pass

        # 4. No existing position in same conditionId
        # (would need ledger check - stub for now)
        checks.append('no duplicate position check (TODO: wire to ledger)')

        # 5. Trade size must be <= $10
        size = signal.get('size', 999)
        if size > 10:
            return False, f'REJECT: size ${size} > $10 cap'
        checks.append(f'size ${size} OK')

        return True, ' | '.join(checks)

    def validate_safety(self, signal: dict) -> tuple:
        """Safety-only validation (skips EV check). Use for hedges and
        variance-reduction trades that don't seek edge but still need
        resolution-time, size-cap, and basic sanity checks."""
        checks = []

        # 1. Market must not be within 1h of resolution
        end_date = signal.get('end_date', '')
        if end_date:
            try:
                end = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                mins_left = (end - datetime.now(timezone.utc)).total_seconds() / 60
                if mins_left < 60:
                    return False, f'REJECT: only {mins_left:.0f} min to resolution'
                checks.append(f'{mins_left:.0f} min to resolution OK')
            except Exception:
                pass

        # 2. Trade size must be <= $10
        size = signal.get('size', 999)
        if size > 10:
            return False, f'REJECT: size ${size} > $10 cap'
        checks.append(f'size ${size} OK')

        return True, ' | '.join(checks)

# ============================================================
# AGENT 2 - POSITION MONITOR
# ============================================================
class PositionMonitor:
    """Monitors open positions every 5 min, applies smart exit rules.

    Spread-aware: low-priced tokens (avg entry < 10c) get a 25-min grace
    period before any stop-loss or time-exit can fire, because the bid-ask
    spread alone can make a position look 15-50% underwater immediately after
    entry even when the underlying edge is intact.  Profit-take fires at any
    time since we never want to miss locking in gains.
    """

    def __init__(self):
        # Tracks UTC datetime each position was first observed, keyed by
        # conditionId (or title as fallback).  Persists across check cycles.
        self._first_seen: dict = {}

    def check_positions(self) -> list:
        alerts = []
        now = datetime.now(timezone.utc)
        try:
            r = requests.get(f'{POLYMARKET_DATA}/positions?user={WALLET}', timeout=10)
            if not r.ok:
                return [{'alert': 'WARN', 'msg': f'position fetch failed: {r.status_code}'}]
            positions = r.json()
            if not isinstance(positions, list):
                positions = positions.get('positions', [])

            for p in positions:
                entry = float(p.get('initialValue', p.get('size', 0)))
                current = float(p.get('currentValue', p.get('value', 0)))
                title = p.get('title', p.get('question', '?'))[:50]
                end_str = p.get('endDate', p.get('end_date', ''))
                mins_left = 9999
                if end_str:
                    try:
                        end = datetime.fromisoformat(end_str.replace('Z','+00:00'))
                        mins_left = (end - now).total_seconds() / 60
                    except: pass

                if entry <= 0: continue
                ratio = current / entry

                # --- spread-aware grace period ---
                # Estimate avg price per token to detect cheap-token entries.
                shares = float(p.get('size', p.get('shares', 0)))
                avg_price = (entry / shares) if shares > 0 else 1.0
                is_cheap = avg_price < 0.10   # tokens priced under 10c

                pos_key = p.get('conditionId', title)
                if pos_key not in self._first_seen:
                    self._first_seen[pos_key] = now
                mins_held = (now - self._first_seen[pos_key]).total_seconds() / 60

                # Grace: 25 min for cheap tokens (spread noise), 10 min for others
                grace = 25 if is_cheap else 10
                in_grace = mins_held < grace

                # RULE A: time exit ÃÂ¢ÃÂÃÂ only after grace; tighter ratio so spread
                # noise (up to ~50%) doesn't trigger a premature exit
                if not in_grace and mins_left < 120 and ratio < 0.35:
                    alerts.append({'alert': 'EXIT_TIME', 'market': title,
                        'reason': f'<2h to resolution, value at {ratio*100:.0f}% of entry (held {mins_held:.0f}m)',
                        'entry': entry, 'current': current, 'mins_left': mins_left})

                # RULE B: EV decay stop-loss ÃÂ¢ÃÂÃÂ only after grace period
                elif not in_grace and ratio < 0.15:
                    alerts.append({'alert': 'EXIT_EV_DECAY', 'market': title,
                        'reason': f'value at only {ratio*100:.0f}% of entry after {mins_held:.0f}m - model wrong',
                        'entry': entry, 'current': current})

                # RULE C: profit take ÃÂ¢ÃÂÃÂ fires immediately, no grace needed
                elif ratio > 2.0:
                    alerts.append({'alert': 'PROFIT_TAKE', 'market': title,
                        'reason': f'value at {ratio*100:.0f}% of entry - take 50% profit',
                        'entry': entry, 'current': current})

        except Exception as e:
            alerts.append({'alert': 'ERROR', 'msg': str(e)})
        return alerts

# ============================================================
# AGENT 3 - POST-TRADE ANALYST
# ============================================================
class PostTradeAnalyst:
    """Records and analyzes each trade outcome."""

    def __init__(self):
        self.outcomes = []

    def record(self, signal: dict, result: dict):
        entry = signal.get('price', 0)
        size = signal.get('size', 0)
        won = result.get('won', False)
        pnl = result.get('pnl', 0)
        self.outcomes.append({'market': signal.get('question','?')[:40],
            'entry_price': entry, 'size': size, 'won': won, 'pnl': pnl,
            'theo_ev': signal.get('ev', 0), 'ts': datetime.now().isoformat()})

        # Rolling stats
        if len(self.outcomes) >= 5:
            last10 = self.outcomes[-10:]
            wins = sum(1 for o in last10 if o['won'])
            total_pnl = sum(o['pnl'] for o in last10)
            win_rate = wins / len(last10)
            log.info(f'POST_TRADE: last {len(last10)} trades WR={win_rate:.0%} PNL=${total_pnl:.2f}')
            if win_rate < 0.40:
                log.warning('ALERT: rolling win rate < 40% - consider pausing bot')

# ============================================================
# AGENT 4 - MARKET INTELLIGENCE SCANNER
# ============================================================
class MarketScanner:
    """Scans and ranks markets by true edge quality at 00Z/12Z."""

    def scan(self) -> list:
        ranked = []
        try:
            r = requests.get(f'{RAILWAY}/api/markets', timeout=15)
            if not r.ok: return []
            data = r.json()
            markets = data.get('weather_markets', [])

            for m in markets:
                edge = m.get('best_edge', 0)
                conf = m.get('confidence', 0)
                yes_price = m.get('yes_price', 0.5)

                # Only rank markets with genuine edge
                if edge < 0.10: continue
                if conf < 2: continue
                if yes_price < 0.02 or yes_price > 0.98: continue  # too illiquid

                score = edge * conf  # combined score
                ranked.append({'market': m.get('question','?')[:60],
                    'edge': edge, 'confidence': conf, 'yes_price': yes_price,
                    'score': score, 'city': m.get('city','?')})

            ranked.sort(key=lambda x: -x['score'])
            if ranked:
                log.info(f'SCANNER: top signal = {ranked[0]["market"]} edge={ranked[0]["edge"]:.3f}')
        except Exception as e:
            log.error(f'SCANNER error: {e}')
        return ranked[:10]

# ============================================================
# AGENT 5 - NO HARVESTER
# ============================================================
class NOHarvester:
    """Scans ALL weather signals for near-certain NO opportunities.

    Strategy proven by top Polymarket weather traders:
      - jangsunjuu  (#7,  $56K profit,  25 trades,  88% WR)
      - ColdMath    (#2,  $80K profit, 5971 trades, 82% WR)

    When a temperature bin is clearly wrong Ã¢ÂÂ both our model AND the market
    price YES at <= 10% Ã¢ÂÂ buying NO at 90-98c is near-guaranteed profit.
    The bin won't hit; NO resolves to $1.  Each trade earns 2-10% on ~$25,
    deployed across 10-20 bins per cycle: low-risk, consistent daily yield.
    """

    def __init__(self):
        self.min_no_price = 0.90   # Only trade when NO >= 90c (YES <= 10c)
        self.max_our_prob = 12.0   # Our model agrees: < 12% YES probability
        self.max_size     = 25.0   # $25 per NO trade (vs $10 YES cap)
        self.max_per_city = 3      # Cap NO trades per city per cycle
        self._seen: set   = set()  # condition_ids already traded this session

    def scan(self, all_signals: list) -> list:
        """Return ranked NO-buy opportunities from the FULL signal list.

        Pass the complete _build_signals output (including SKIP signals) so
        we can find bins where YES is near-worthless but still tradeable.
        """
        opps        = []
        city_counts: dict = {}

        for sig in all_signals:
            city     = sig.get('city', '').lower()
            if city_counts.get(city, 0) >= self.max_per_city:
                continue

            yes_pct  = sig.get('market_price', 50)   # YES market price as %
            our_prob = sig.get('our_prob', 50)         # our YES probability %
            no_price = round((100 - yes_pct) / 100, 4)

            # Both market AND model must agree the bin is very unlikely
            if no_price < self.min_no_price:
                continue   # NO not priced high enough
            if our_prob > self.max_our_prob:
                continue   # Our model still thinks YES is plausible Ã¢ÂÂ skip

            # Must have a NO token to trade
            no_token_id = None
            for tk in sig.get('tokens', []):
                if str(tk.get('outcome', '')).lower() == 'no':
                    no_token_id = tk.get('token_id', '')
                    break
            if not no_token_id:
                continue

            cond_key = no_token_id  # use token_id Ã¢ÂÂ always unique & non-empty
            if cond_key in self._seen:
                continue

            # Expected gain when NO resolves to $1
            expected_return_pct = round((1.0 - no_price) * 100, 1)

            opps.append({
                'city'               : sig.get('city', ''),
                'question'           : sig.get('question', ''),
                'signal'             : 'BUY NO',
                'no_price'           : no_price,
                'yes_price_pct'      : yes_pct,
                'our_prob'           : our_prob,
                'expected_return_pct': expected_return_pct,
                'no_token_id'        : no_token_id,
                'condition_id'       : sig.get('condition_id', ''),
                'end_date'           : sig.get('end_date', ''),
                'tokens'             : sig.get('tokens', []),
                'size'               : self.max_size,
                'confidence'         : 4,
            })
            city_counts[city] = city_counts.get(city, 0) + 1

        # Highest NO price = most certain = best risk/reward first
        opps.sort(key=lambda x: x['no_price'], reverse=True)
        if opps:
            log.info('NO_HARVESTER: %d opportunities | top=%s NO=%.3f exp=+%.1f%%',
                     len(opps), opps[0]['city'], opps[0]['no_price'],
                     opps[0]['expected_return_pct'])
        return opps[:15]


# ============================================================


class YESHarvester:
    """Scans ALL weather signals for near-certain YES opportunities where the
    correct bin is highly likely to resolve YES but still priced at 92Ã¢ÂÂ98c.
    Symmetric mirror of NOHarvester.

    Strategy: When market prices YES at Ã¢ÂÂ¥0.92 AND our model also agrees
    (our_prob Ã¢ÂÂ¥ 88%), the expected return is 2Ã¢ÂÂ9% with very high confidence.
    This is the 'right-bin certainty' edge used by Handsanitizer23 (#5 $68K)
    and Hans323 (#3 $80K) Ã¢ÂÂ we capture it at lower size but same edge logic.

    Return per trade:  (1.0 - yes_price) / yes_price * 100
      e.g., YES at 0.92 Ã¢ÂÂ ~8.7% | YES at 0.95 Ã¢ÂÂ ~5.3% | YES at 0.98 Ã¢ÂÂ ~2.0%
    """
    def __init__(self):
        self.min_yes_price = 0.92   # Only trade when YES >= 92c
        self.min_our_prob  = 88.0   # Our model agrees: >= 88% YES probability
        self.max_size      = 25.0   # $25 per YES trade (same as NO side)
        self.max_per_city  = 3      # Cap YES harvest trades per city per cycle
        self._seen: set    = set()  # condition_ids already traded this session

    def scan(self, all_signals: list) -> list:
        opps = []
        city_counts: dict = {}
        for sig in all_signals:
            city     = sig.get('city', '').lower()
            if city_counts.get(city, 0) >= self.max_per_city:
                continue
            yes_pct  = sig.get('market_price', 50)
            our_prob = sig.get('our_prob', 50)
            yes_price = round(yes_pct / 100, 4)
            if yes_price < self.min_yes_price:
                continue
            if our_prob < self.min_our_prob:
                continue
            yes_token_id = None
            for tk in sig.get('tokens', []):
                if str(tk.get('outcome', '')).lower() == 'yes':
                    yes_token_id = tk.get('token_id', '')
                    break
            if not yes_token_id:
                continue
            cond_key = yes_token_id  # use token_id Ã¢ÂÂ always unique & non-empty
            if cond_key in self._seen:
                continue
            expected_return_pct = round((1.0 - yes_price) / yes_price * 100, 1)
            opps.append({
                'city': sig.get('city', ''),
                'question': sig.get('question', ''),
                'signal': 'BUY YES',
                'yes_price': yes_price,
                'yes_price_pct': yes_pct,
                'our_prob': our_prob,
                'expected_return_pct': expected_return_pct,
                'yes_token_id': yes_token_id,
                'condition_id': sig.get('condition_id', ''),
                'end_date': sig.get('end_date', ''),
                'tokens': sig.get('tokens', []),
                'size': self.max_size,
                'confidence': 4,
            })
            city_counts[city] = city_counts.get(city, 0) + 1
            self._seen.add(cond_key)  # dedup Ã¢ÂÂ never re-enter same token
        opps.sort(key=lambda x: x['yes_price'], reverse=False)  # cheapest YES first = highest return
        if opps:
            log.info('YES_HARVESTER: %d opportunities | top=%s YES=%.3f exp=+%.1f%%',
                     len(opps), opps[0]['city'], opps[0]['yes_price'],
                     opps[0]['expected_return_pct'])
        return opps[:15]



# ============================================================
# AGENT 7 - WEATHER SENTINEL (Live Station Monitor)
# ============================================================
class WeatherSentinel:
    """Continuously monitors METAR weather stations, builds observation
    history, computes temperature trends, and feeds real-time intelligence
    to all other agents."""

    METAR_URL = 'https://aviationweather.gov/api/data/metar'

    STATIONS = {
        'KATL': 'Atlanta',    'KLAX': 'Los Angeles', 'KSFO': 'San Francisco',
        'CYYZ': 'Toronto',    'EPWA': 'Warsaw',      'KMIA': 'Miami',
        'KORD': 'Chicago',    'KSEA': 'Seattle',     'LEMD': 'Madrid',
        'LFPG': 'Paris',      'LLBG': 'Tel Aviv',    'LTAC': 'Istanbul',
        'RJTT': 'Tokyo',      'RKSI': 'Seoul',       'SAEZ': 'Buenos Aires',
        'SBGR': 'Sao Paulo',  'WSSS': 'Singapore',   'ZBAA': 'Beijing',
        'ZSPD': 'Shanghai',
    }

    CITY_TO_STATION = {v: k for k, v in STATIONS.items()}

    def __init__(self):
        self._history = {}
        self._trends = {}
        self._confidence = {}
        self._last_poll = 0.0
        self._poll_count = 0
        self._errors = {}
        self._alerts = []
        self.max_history = 288
        self.poll_interval = 300
        log.info('SENTINEL: initialized for %d stations', len(self.STATIONS))

    def poll(self):
        results = {}
        now = time.time()
        station_ids = ','.join(self.STATIONS.keys())
        try:
            resp = requests.get(self.METAR_URL, params={'ids': station_ids, 'format': 'json', 'hours': 1}, timeout=15)
            resp.raise_for_status()
            metar_data = resp.json()
        except Exception as e:
            log.warning('SENTINEL: METAR batch fetch failed: %s', e)
            for sid in self.STATIONS:
                self._errors[sid] = self._errors.get(sid, 0) + 1
            self._last_poll = now
            self._poll_count += 1
            return results

        for obs in metar_data:
            sid = obs.get('icaoId', obs.get('stationId', ''))
            if sid not in self.STATIONS:
                continue
            temp_c = obs.get('temp')
            if temp_c is None:
                continue
            record = {
                'temp_c': float(temp_c),
                'dewp_c': obs.get('dewp'),
                'wdir': obs.get('wdir'),
                'wspd': obs.get('wspd'),
                'vis': obs.get('visib'),
                'cloud': obs.get('cover', ''),
                'raw': obs.get('rawOb', '')[:120],
                'ts': now,
                'obs_time': obs.get('obsTime', obs.get('reportTime', '')),
            }
            if sid not in self._history:
                self._history[sid] = []
            self._history[sid].append(record)
            if len(self._history[sid]) > self.max_history:
                self._history[sid] = self._history[sid][-self.max_history:]
            self._errors[sid] = 0
            results[sid] = record

        self._compute_all_trends()
        self._compute_all_confidence()
        self._last_poll = now
        self._poll_count += 1
        log.info('SENTINEL: polled %d/%d stations', len(results), len(self.STATIONS))
        return results

    def _compute_all_trends(self):
        for sid, history in self._history.items():
            if len(history) < 2:
                latest = history[-1] if history else None
                t = {'rate_c_hr': 0.0, 'direction': 'insufficient'}
                if latest and latest.get('temp_c') is not None:
                    t['current_c'] = latest['temp_c']
                    t['current_f'] = round(latest['temp_c'] * 9/5 + 32, 1)
                self._trends[sid] = t
                continue
            recent = [h for h in history if h['ts'] > time.time() - 7200]
            if len(recent) < 2:
                recent = history[-2:]
            oldest, newest = recent[0], recent[-1]
            dt_hours = (newest['ts'] - oldest['ts']) / 3600.0
            if dt_hours < 0.05:
                self._trends[sid] = {
                    'rate_c_hr': 0.0, 'direction': 'stable',
                    'current_c': newest['temp_c'],
                    'current_f': round(newest['temp_c'] * 9/5 + 32, 1),
                    'samples': len(recent), 'window_hrs': 0.0,
                }
                continue
            delta_c = newest['temp_c'] - oldest['temp_c']
            rate = round(delta_c / dt_hours, 2)
            direction = 'stable' if abs(rate) < 0.3 else ('rising' if rate > 0 else 'falling')
            self._trends[sid] = {
                'rate_c_hr': rate, 'rate_f_hr': round(rate * 9/5, 2),
                'direction': direction,
                'current_c': newest['temp_c'], 'current_f': round(newest['temp_c'] * 9/5 + 32, 1),
                'samples': len(recent), 'window_hrs': round(dt_hours, 1),
            }

    def _compute_all_confidence(self):
        for sid in self.STATIONS:
            history = self._history.get(sid, [])
            errors = self._errors.get(sid, 0)
            data_score = 0 if not history else (20 if len(history) < 3 else (40 if len(history) < 12 else 60))
            freshness = 0
            if history:
                age = time.time() - history[-1]['ts']
                freshness = 25 if age < 600 else (15 if age < 1800 else (5 if age < 3600 else 0))
            error_penalty = min(errors * 15, 50)
            trend_bonus = min(self._trends.get(sid, {}).get('samples', 0) * 3, 15)
            self._confidence[sid] = max(0, min(100, data_score + freshness - error_penalty + trend_bonus))

    def check_bin_boundaries(self, city, bins):
        sid = self.CITY_TO_STATION.get(city)
        if not sid:
            return []
        trend = self._trends.get(sid, {})
        current_f = trend.get('current_f')
        rate_f = trend.get('rate_f_hr', 0)
        if current_f is None:
            return []
        alerts = []
        for boundary in bins:
            distance = boundary - current_f
            abs_dist = abs(distance)
            if abs_dist > 5.0:
                continue
            eta_hours = round(abs_dist / abs(rate_f), 1) if rate_f != 0 and ((distance > 0 and rate_f > 0) or (distance < 0 and rate_f < 0)) else None
            urgency = 'critical' if abs_dist < 1.0 else ('high' if abs_dist < 2.0 else ('medium' if abs_dist < 3.0 else 'low'))
            alerts.append({'boundary_f': boundary, 'current_f': current_f, 'distance_f': round(distance, 1), 'rate_f_hr': rate_f, 'direction': trend.get('direction', 'unknown'), 'eta_hours': eta_hours, 'urgency': urgency, 'approaching': eta_hours is not None and eta_hours < 3.0})
        alerts.sort(key=lambda a: abs(a['distance_f']))
        return alerts

    def enrich_signals(self, sigs):
        for sig in sigs:
            city = sig.get('city', '')
            sid = self.CITY_TO_STATION.get(city)
            if not sid:
                continue
            trend = self._trends.get(sid, {})
            conf = self._confidence.get(sid, 50)
            sig['sentinel_confidence'] = conf
            sig['sentinel_trend'] = trend.get('direction', 'unknown')
            sig['sentinel_rate_f_hr'] = trend.get('rate_f_hr', 0)
            sig['sentinel_current_f'] = trend.get('current_f')
            sig['sentinel_current_c'] = trend.get('current_c')
            sig['sentinel_station'] = sid
        return sigs

    def get_station_state(self, station_id):
        history = self._history.get(station_id, [])
        return {'station': station_id, 'city': self.STATIONS.get(station_id, 'Unknown'), 'confidence': self._confidence.get(station_id, 0), 'trend': self._trends.get(station_id, {}), 'observations': len(history), 'latest': history[-1] if history else None, 'errors': self._errors.get(station_id, 0)}

    def get_all_states(self):
        stations = {sid: self.get_station_state(sid) for sid in self.STATIONS}
        return {'ok': True, 'poll_count': self._poll_count, 'last_poll': self._last_poll, 'station_count': len(self.STATIONS), 'stations_with_data': sum(1 for s in self._history if self._history[s]), 'stations': stations}

    def needs_poll(self):
        return (time.time() - self._last_poll) >= self.poll_interval

    def get_high_confidence_cities(self, min_confidence=60.0):
        return [self.STATIONS[sid] for sid, conf in self._confidence.items() if conf >= min_confidence]



# ============================================================
# AGENT 8 - ACCURACY TRACKER (Prediction vs Outcome Logger)
# ============================================================
class AccuracyTracker:
    """Logs predictions each cycle, checks market resolutions, and builds
    per-station accuracy scores over time.

    Storage: JSON file persisted to disk. Survives restarts.
    Tracks: prediction logs, resolution outcomes, per-station stats.
    """

    STORE_PATH = os.environ.get('ACCURACY_STORE', '/tmp/accuracy_store.json')
    POLYMARKET_CLOB = 'https://clob.polymarket.com'

    def __init__(self):
        self._predictions = []
        self._resolutions = {}
        self._station_stats = {}
        self._last_resolution_check = 0
        self.resolution_check_interval = 1800  # 30 min
        self._load_store()
        log.info('ACCURACY_TRACKER: initialized | %d predictions | %d resolutions | %d stations tracked',
                 len(self._predictions), len(self._resolutions), len(self._station_stats))

    # -- Persistence --
    def _load_store(self):
        try:
            if os.path.exists(self.STORE_PATH):
                with open(self.STORE_PATH, 'r') as f:
                    data = json.load(f)
                self._predictions = data.get('predictions', [])
                self._resolutions = data.get('resolutions', {})
                self._station_stats = data.get('station_stats', {})
                log.info('ACCURACY_TRACKER: loaded store from %s', self.STORE_PATH)
        except Exception as e:
            log.warning('ACCURACY_TRACKER: failed to load store: %s', e)

    def _save_store(self):
        try:
            data = {
                'predictions': self._predictions[-5000:],
                'resolutions': self._resolutions,
                'station_stats': self._station_stats,
                'saved_at': datetime.now(timezone.utc).isoformat(),
            }
            with open(self.STORE_PATH, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            log.warning('ACCURACY_TRACKER: failed to save store: %s', e)

    # -- Prediction logging --
    def log_predictions(self, sigs, sentinel=None):
        """Log predictions from current cycle signals."""
        now = datetime.now(timezone.utc)
        today_str = now.strftime('%Y-%m-%d')
        cycle_ts = now.isoformat()
        logged = 0
        for sig in sigs:
            city = sig.get('city', '')
            condition_id = sig.get('condition_id', '')
            if not city or not condition_id:
                continue
            # Deduplicate: skip if we already logged this condition_id today
            already = any(
                p['condition_id'] == condition_id and p['date'] == today_str
                for p in self._predictions[-200:]
            )
            if already:
                continue
            pred = {
                'ts': cycle_ts,
                'date': today_str,
                'city': city,
                'condition_id': condition_id,
                'question': sig.get('question', '')[:100],
                'direction': sig.get('direction', ''),
                'threshold': sig.get('threshold'),
                'our_prob': sig.get('our_prob', 0),
                'market_price': sig.get('market_price', 0),
                'forecast': sig.get('forecast'),
                'signal': sig.get('signal', ''),
                'ev': sig.get('theo_ev', 0),
            }
            # Add sentinel data if available
            station = sig.get('sentinel_station', '')
            if station:
                pred['station'] = station
                pred['sentinel_confidence'] = sig.get('sentinel_confidence', 0)
                pred['sentinel_trend'] = sig.get('sentinel_trend', '')
                pred['sentinel_temp_f'] = sig.get('sentinel_current_f')
                pred['sentinel_temp_c'] = sig.get('sentinel_current_c')
                pred['sentinel_rate_f_hr'] = sig.get('sentinel_rate_f_hr', 0)
            self._predictions.append(pred)
            logged += 1
        if len(self._predictions) > 5000:
            self._predictions = self._predictions[-5000:]
        if logged > 0:
            log.info('ACCURACY_TRACKER: logged %d predictions for %s', logged, today_str)
            self._save_store()
        return logged

    # -- Resolution checking --
    def check_resolutions(self):
        """Check Polymarket for resolved markets and score predictions."""
        now = time.time()
        if now - self._last_resolution_check < self.resolution_check_interval:
            return 0
        self._last_resolution_check = now
        unresolved_ids = set()
        for pred in self._predictions:
            cid = pred.get('condition_id', '')
            if cid and cid not in self._resolutions:
                unresolved_ids.add(cid)
        if not unresolved_ids:
            return 0
        newly_resolved = 0
        for cid in list(unresolved_ids)[:20]:
            try:
                resp = requests.get(f'{self.POLYMARKET_CLOB}/markets/{cid}', timeout=10)
                if resp.status_code != 200:
                    continue
                mkt_data = resp.json()
                resolved = mkt_data.get('resolved', False)
                if not resolved:
                    continue
                outcome = mkt_data.get('outcome', '')
                outcome_bool = outcome.lower() == 'yes' if isinstance(outcome, str) else bool(outcome)
                self._resolutions[cid] = {
                    'outcome': 'YES' if outcome_bool else 'NO',
                    'outcome_prob': 100.0 if outcome_bool else 0.0,
                    'resolved_at': datetime.now(timezone.utc).isoformat(),
                    'question': mkt_data.get('question', '')[:100],
                }
                newly_resolved += 1
            except Exception as e:
                log.debug('ACCURACY_TRACKER: resolution check failed for %s: %s', cid[:12], e)
        if newly_resolved > 0:
            log.info('ACCURACY_TRACKER: %d markets newly resolved', newly_resolved)
            self._score_predictions()
            self._save_store()
        return newly_resolved

    # -- Scoring --
    def _score_predictions(self):
        """Recalculate per-station accuracy stats from resolved predictions."""
        stats = {}
        for pred in self._predictions:
            cid = pred.get('condition_id', '')
            if cid not in self._resolutions:
                continue
            res = self._resolutions[cid]
            station = pred.get('station', 'UNKNOWN')
            city = pred.get('city', '')
            if station not in stats:
                stats[station] = {
                    'city': city, 'total': 0, 'correct_signal': 0,
                    'brier_sum': 0.0, 'abs_error_sum': 0.0,
                    'predictions_by_date': {},
                }
            s = stats[station]
            s['total'] += 1
            actual = 1.0 if res['outcome'] == 'YES' else 0.0
            predicted = pred['our_prob'] / 100.0
            s['brier_sum'] += (predicted - actual) ** 2
            s['abs_error_sum'] += abs(pred['our_prob'] - res['outcome_prob'])
            sig = pred.get('signal', '')
            if sig == 'BUY YES' and res['outcome'] == 'YES':
                s['correct_signal'] += 1
            elif sig == 'BUY NO' and res['outcome'] == 'NO':
                s['correct_signal'] += 1
            d = pred.get('date', '')
            if d not in s['predictions_by_date']:
                s['predictions_by_date'][d] = 0
            s['predictions_by_date'][d] += 1
        for sid, s in stats.items():
            if s['total'] > 0:
                s['brier_score'] = round(s['brier_sum'] / s['total'], 4)
                s['mean_abs_error'] = round(s['abs_error_sum'] / s['total'], 1)
                s['signal_accuracy_pct'] = round(s['correct_signal'] / s['total'] * 100, 1)
                s['days_tracked'] = len(s['predictions_by_date'])
        self._station_stats = stats

    # -- Public accessors --
    def get_accuracy_report(self):
        """Full accuracy report for /api/weather/accuracy."""
        self._score_predictions()
        ranked = sorted(
            self._station_stats.items(),
            key=lambda x: x[1].get('signal_accuracy_pct', 0) or 0,
            reverse=True
        )
        return {
            'ok': True,
            'total_predictions': len(self._predictions),
            'total_resolutions': len(self._resolutions),
            'stations_tracked': len(self._station_stats),
            'station_rankings': [
                {
                    'station': sid, 'city': s['city'],
                    'total_predictions': s['total'],
                    'correct_signals': s['correct_signal'],
                    'signal_accuracy_pct': s.get('signal_accuracy_pct'),
                    'brier_score': s.get('brier_score'),
                    'mean_abs_error': s.get('mean_abs_error'),
                    'days_tracked': s.get('days_tracked', 0),
                }
                for sid, s in ranked
            ],
            'recent_resolutions': [
                {
                    'condition_id': cid[:16] + '...',
                    'outcome': r['outcome'],
                    'question': r['question'],
                    'resolved_at': r['resolved_at'],
                }
                for cid, r in sorted(self._resolutions.items(),
                    key=lambda x: x[1].get('resolved_at', ''), reverse=True)[:10]
            ],
            'unresolved_markets': len(set(
                p.get('condition_id', '') for p in self._predictions
                if p.get('condition_id', '') not in self._resolutions
            )),
        }

    def get_station_accuracy(self, station_id):
        """Get accuracy stats for a single station."""
        s = self._station_stats.get(station_id, {})
        preds = [p for p in self._predictions if p.get('station') == station_id]
        return {'station': station_id, 'stats': s, 'recent_predictions': preds[-20:]}

    def needs_resolution_check(self):
        return (time.time() - self._last_resolution_check) >= self.resolution_check_interval



class IntelligenceFeed:
    """Phase 3: Full intelligence feed.

    1. Dynamic confidence — adjusts sigma based on AccuracyTracker's per-station
       Brier scores. Proven-accurate stations get tighter sigma (→ sharper predictions),
       unreliable stations get wider sigma (→ more conservative).
    2. Bin-boundary alerts — flags signals where current temp is within 5°F of a
       market bin boundary, with urgency levels and ETA to crossing.
    3. Multi-source consensus — pulls Open-Meteo forecasts (free, no key) as a
       second source, compares with primary forecast, flags disagreements.
    """

    OPEN_METEO_URL = 'https://api.open-meteo.com/v1/forecast'

    # lat/lon for each city (matching WeatherSentinel's 19 stations)
    CITY_COORDS = {
        'Atlanta':       (33.75, -84.39),
        'Los Angeles':   (33.94, -118.41),
        'San Francisco': (37.77, -122.42),
        'Toronto':       (43.68, -79.63),
        'Warsaw':        (52.17, 20.97),
        'Miami':         (25.79, -80.29),
        'Chicago':       (41.98, -87.90),
        'Seattle':       (47.45, -122.31),
        'Madrid':        (40.47, -3.56),
        'Paris':         (49.01, 2.55),
        'Tel Aviv':      (32.01, 34.88),
        'Istanbul':      (40.98, 28.82),
        'Tokyo':         (35.55, 139.78),
        'Seoul':         (37.46, 126.44),
        'Buenos Aires':  (-34.82, -58.54),
        'Sao Paulo':     (-23.43, -46.47),
        'Singapore':     (1.36, 103.99),
        'Beijing':       (40.08, 116.58),
        'Shanghai':      (31.14, 121.81),
    }

    def __init__(self):
        self._consensus_cache = {}
        self._consensus_ts = 0
        self._consensus_interval = 1800  # refresh every 30 min
        self._sigma_adjustments = {}
        self._alerts_cache = {}
        log.info('INTEL_FEED: initialized for %d cities', len(self.CITY_COORDS))

    # ---- 1. DYNAMIC CONFIDENCE / SIGMA ADJUSTMENT ----

    def compute_sigma_adjustments(self, accuracy_tracker):
        """Use AccuracyTracker station stats to compute per-station sigma multipliers.

        Stations with low Brier scores (accurate) → multiplier < 1.0 (tighter sigma)
        Stations with high Brier scores (inaccurate) → multiplier > 1.0 (wider sigma)
        New stations with no data → multiplier = 1.0 (no change)
        """
        adjustments = {}
        try:
            report = accuracy_tracker.get_accuracy_report()
            rankings = report.get('station_rankings', [])
            if not rankings:
                self._sigma_adjustments = {}
                return {}

            # Collect Brier scores
            brier_scores = {}
            for station in rankings:
                sid = station.get('station_id', '')
                brier = station.get('brier_score')
                days = station.get('days_tracked', 0)
                if brier is not None and days >= 2:
                    brier_scores[sid] = brier

            if not brier_scores:
                self._sigma_adjustments = {}
                return {}

            # Median Brier as baseline
            sorted_brier = sorted(brier_scores.values())
            mid = len(sorted_brier) // 2
            median_brier = sorted_brier[mid] if len(sorted_brier) % 2 else (sorted_brier[mid-1] + sorted_brier[mid]) / 2

            for sid, brier in brier_scores.items():
                if median_brier == 0:
                    multiplier = 1.0
                else:
                    # ratio: brier/median — below median = good, above = bad
                    ratio = brier / median_brier
                    # Clamp to [0.6, 1.5] range
                    # ratio < 1 = better than median → tighter sigma
                    # ratio > 1 = worse than median → wider sigma
                    multiplier = max(0.6, min(1.5, 0.5 + ratio * 0.5))
                adjustments[sid] = {
                    'multiplier': round(multiplier, 3),
                    'brier': round(brier, 4),
                    'median_brier': round(median_brier, 4),
                    'rating': 'excellent' if multiplier < 0.75 else ('good' if multiplier < 0.95 else ('average' if multiplier < 1.1 else ('poor' if multiplier < 1.3 else 'unreliable')))
                }
        except Exception as e:
            log.warning('INTEL_FEED: sigma adjustment computation failed: %s', e)

        self._sigma_adjustments = adjustments
        return adjustments

    def get_sigma_multiplier(self, station_id):
        """Get the sigma multiplier for a station. Returns 1.0 if no data."""
        adj = self._sigma_adjustments.get(station_id, {})
        return adj.get('multiplier', 1.0)

    def adjust_signal_sigma(self, sig):
        """Adjust a signal's sigma using the station's dynamic multiplier.
        Mutates the signal dict in-place and returns it."""
        sid = sig.get('sentinel_station', '')
        if not sid:
            return sig
        mult = self.get_sigma_multiplier(sid)
        if 'sigma' in sig:
            old_sigma = sig['sigma']
            sig['sigma'] = round(old_sigma * mult, 3)
            sig['intel_sigma_mult'] = mult
            sig['intel_sigma_old'] = old_sigma
        adj = self._sigma_adjustments.get(sid, {})
        sig['intel_station_rating'] = adj.get('rating', 'unrated')
        return sig

    # ---- 2. BIN-BOUNDARY ALERTS ----

    def generate_alerts(self, sentinel, sigs):
        """Generate bin-boundary alerts for all active signals.
        Uses the sentinel's check_bin_boundaries() and enriches with market context."""
        alerts = []
        seen_cities = set()
        for sig in sigs:
            city = sig.get('city', '')
            if city in seen_cities:
                continue
            seen_cities.add(city)

            # Extract bin boundaries from the signal's threshold
            threshold = sig.get('threshold')
            if threshold is None:
                continue
            # Build approximate bin edges (Polymarket typically uses 5°F bins)
            threshold_f = threshold if sig.get('unit') == 'F' else (threshold * 9/5 + 32)
            # Standard Polymarket temp bins: ..., 55-60, 60-65, 65-70, 70-75, 75-80, ...
            base = int(threshold_f // 5) * 5
            bins = [base - 5, base, base + 5, base + 10]

            city_alerts = sentinel.check_bin_boundaries(city, bins)
            for alert in city_alerts:
                alert['city'] = city
                alert['market_question'] = sig.get('question', '')
                alert['condition_id'] = sig.get('condition_id', '')
                alerts.append(alert)

        # Sort by urgency (critical first)
        urgency_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        alerts.sort(key=lambda a: urgency_order.get(a.get('urgency', 'low'), 4))
        self._alerts_cache = {'alerts': alerts, 'ts': time.time(), 'count': len(alerts)}
        return alerts

    # ---- 3. MULTI-SOURCE CONSENSUS ----

    def fetch_open_meteo_forecasts(self):
        """Fetch today's high temp forecast from Open-Meteo for all 19 cities.
        Returns dict: {city: {'high_c': float, 'high_f': float, 'source': 'open-meteo'}}"""
        forecasts = {}
        # Batch by building a multi-location request (Open-Meteo supports this)
        lats = []
        lons = []
        cities_ordered = sorted(self.CITY_COORDS.keys())
        for city in cities_ordered:
            lat, lon = self.CITY_COORDS[city]
            lats.append(str(lat))
            lons.append(str(lon))

        try:
            params = {
                'latitude': ','.join(lats),
                'longitude': ','.join(lons),
                'daily': 'temperature_2m_max,temperature_2m_min',
                'timezone': 'auto',
                'forecast_days': 2,
            }
            resp = requests.get(self.OPEN_METEO_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            # Open-Meteo returns array when multiple locations
            results = data if isinstance(data, list) else [data]
            for i, city in enumerate(cities_ordered):
                if i >= len(results):
                    break
                daily = results[i].get('daily', {})
                highs = daily.get('temperature_2m_max', [])
                lows = daily.get('temperature_2m_min', [])
                if highs:
                    today_high_c = highs[0]
                    tomorrow_high_c = highs[1] if len(highs) > 1 else None
                    today_low_c = lows[0] if lows else None
                    forecasts[city] = {
                        'high_c': round(today_high_c, 1),
                        'high_f': round(today_high_c * 9/5 + 32, 1),
                        'low_c': round(today_low_c, 1) if today_low_c is not None else None,
                        'tomorrow_high_c': round(tomorrow_high_c, 1) if tomorrow_high_c is not None else None,
                        'tomorrow_high_f': round(tomorrow_high_c * 9/5 + 32, 1) if tomorrow_high_c is not None else None,
                        'source': 'open-meteo',
                    }
        except Exception as e:
            log.warning('INTEL_FEED: Open-Meteo fetch failed: %s', e)

        return forecasts

    def build_consensus(self, sigs, force=False, sentinel=None):
        """Compare primary forecast (from signals) with Open-Meteo.
        Returns consensus data per city: agreement level, spread, recommendation.
        If sentinel is provided, directly queries it for any city missing data."""
        now = time.time()
        # Only use cache when called without signals (e.g. from API endpoint).
        # If real sigs are provided (trade cycle), always rebuild so primary
        # forecasts are fresh and the cache is never poisoned by an empty-sigs call.
        has_signals = bool(sigs)
        cache_valid = (
            not force
            and not has_signals
            and (now - self._consensus_ts) < self._consensus_interval
            and self._consensus_cache
            and any('primary_c' in v for v in self._consensus_cache.values())
        )
        if cache_valid:
            return self._consensus_cache

        om_forecasts = self.fetch_open_meteo_forecasts()
        consensus = {}

        # Extract primary forecasts from signals
        # RUFLO FIX: Prefer sentinel_current_c (real METAR data) over
        # 'forecast' field which defaults to 20C/68F when weather cache empty.
        primary_by_city = {}
        for sig in sigs:
            city = sig.get('city', '')
            if city and city not in primary_by_city:
                # Priority 1: WeatherSentinel's live METAR observation (always °C)
                sentinel_c = sig.get('sentinel_current_c')
                if sentinel_c is not None:
                    primary_by_city[city] = {
                        'temp_c': round(sentinel_c, 1),
                        'temp_f': round(sentinel_c * 9/5 + 32, 1),
                        'source': 'sentinel_metar',
                    }
                    continue
                # Priority 2: forecast field with unit conversion
                fcast = sig.get('forecast')
                if fcast is not None:
                    sig_unit = sig.get('unit', 'C')
                    if sig_unit == 'F':
                        fcast_c = round((fcast - 32) * 5/9, 1)
                        fcast_f = round(fcast, 1)
                    else:
                        fcast_c = round(fcast, 1)
                        fcast_f = round(fcast * 9/5 + 32, 1)
                    primary_by_city[city] = {
                        'temp_c': fcast_c,
                        'temp_f': fcast_f,
                        'source': 'forecast_converted',
                    }

        # RUFLO: Direct sentinel lookup for any city still missing primary data.
        # Covers cities where: (a) no market signal exists, (b) signal wasn't
        # enriched, or (c) METAR had no data during the last poll but history exists.
        if sentinel is not None:
            for city_name, station_id in getattr(sentinel, 'CITY_TO_STATION', {}).items():
                if city_name not in primary_by_city:
                    trend = sentinel._trends.get(station_id, {})
                    tc = trend.get('current_c')
                    if tc is not None:
                        primary_by_city[city_name] = {
                            'temp_c': round(tc, 1),
                            'temp_f': round(tc * 9/5 + 32, 1),
                            'source': 'sentinel_direct',
                        }
                    else:
                        # Try latest observation from history
                        hist = sentinel._history.get(station_id, [])
                        if hist and hist[-1].get('temp_c') is not None:
                            tc2 = hist[-1]['temp_c']
                            primary_by_city[city_name] = {
                                'temp_c': round(tc2, 1),
                                'temp_f': round(tc2 * 9/5 + 32, 1),
                                'source': 'sentinel_history',
                            }

        for city in set(list(primary_by_city.keys()) + list(om_forecasts.keys())):
            primary = primary_by_city.get(city)
            om = om_forecasts.get(city)

            if primary and om:
                p_c = primary['temp_c']
                o_c = om['high_c']
                spread_c = round(abs(p_c - o_c), 1)
                spread_f = round(spread_c * 9/5, 1)
                avg_c = round((p_c + o_c) / 2, 1)
                avg_f = round(avg_c * 9/5 + 32, 1)

                if spread_c < 1.0:
                    agreement = 'strong'
                    recommendation = 'high_confidence'
                elif spread_c < 2.0:
                    agreement = 'moderate'
                    recommendation = 'normal'
                elif spread_c < 3.5:
                    agreement = 'weak'
                    recommendation = 'widen_sigma'
                else:
                    agreement = 'divergent'
                    recommendation = 'reduce_size'

                consensus[city] = {
                    'primary_c': p_c,
                    'primary_f': primary['temp_f'],
                    'primary_source': primary.get('source', 'unknown'),
                    'open_meteo_c': o_c,
                    'open_meteo_f': om['high_f'],
                    'spread_c': spread_c,
                    'spread_f': spread_f,
                    'consensus_c': avg_c,
                    'consensus_f': avg_f,
                    'agreement': agreement,
                    'recommendation': recommendation,
                    'sources': 2,
                }
            elif primary:
                consensus[city] = {
                    'primary_c': primary['temp_c'],
                    'primary_f': primary['temp_f'],
                    'agreement': 'single_source',
                    'recommendation': 'normal',
                    'sources': 1,
                }
            elif om:
                consensus[city] = {
                    'open_meteo_c': om['high_c'],
                    'open_meteo_f': om['high_f'],
                    'agreement': 'single_source',
                    'recommendation': 'normal',
                    'sources': 1,
                }

        self._consensus_cache = consensus
        self._consensus_ts = now
        return consensus

    def enrich_signals_phase3(self, sigs, sentinel, accuracy_tracker):
        """Full Phase 3 enrichment: dynamic sigma, alerts, consensus.
        Call AFTER sentinel.enrich_signals(sigs)."""
        # 1. Refresh sigma adjustments
        self.compute_sigma_adjustments(accuracy_tracker)

        # 2. Build consensus
        consensus = self.build_consensus(sigs, sentinel=sentinel)

        # 3. Generate alerts
        alerts = self.generate_alerts(sentinel, sigs)

        # 4. Enrich each signal
        alert_by_city = {}
        for a in alerts:
            city = a.get('city', '')
            if city not in alert_by_city:
                alert_by_city[city] = []
            alert_by_city[city].append(a)

        for sig in sigs:
            # Dynamic sigma
            self.adjust_signal_sigma(sig)

            city = sig.get('city', '')

            # Consensus data
            city_consensus = consensus.get(city, {})
            sig['intel_consensus'] = city_consensus.get('agreement', 'unknown')
            sig['intel_spread_c'] = city_consensus.get('spread_c', 0)
            sig['intel_recommendation'] = city_consensus.get('recommendation', 'normal')
            if city_consensus.get('consensus_c') is not None:
                sig['intel_consensus_temp_c'] = city_consensus['consensus_c']

            # Apply consensus-based sigma widening
            rec = city_consensus.get('recommendation', 'normal')
            if rec == 'widen_sigma' and 'sigma' in sig:
                sig['sigma'] = round(sig['sigma'] * 1.15, 3)
                sig['intel_sigma_consensus_adj'] = 1.15
            elif rec == 'reduce_size':
                sig['intel_reduce_size'] = True

            # Bin-boundary alerts
            city_alerts = alert_by_city.get(city, [])
            if city_alerts:
                sig['intel_bin_alerts'] = len(city_alerts)
                top = city_alerts[0]
                sig['intel_nearest_boundary'] = top.get('boundary_f')
                sig['intel_boundary_distance'] = top.get('distance_f')
                sig['intel_boundary_urgency'] = top.get('urgency')
                sig['intel_approaching'] = top.get('approaching', False)

        return sigs

    def get_intelligence_report(self, sigs, sentinel, accuracy_tracker):
        """Full intelligence report combining all Phase 3 data."""
        consensus = self.build_consensus(sigs, sentinel=sentinel)
        alerts = self._alerts_cache.get('alerts', [])
        sigma_adj = self._sigma_adjustments

        # Summary stats
        n_strong = sum(1 for c in consensus.values() if c.get('agreement') == 'strong')
        n_divergent = sum(1 for c in consensus.values() if c.get('agreement') == 'divergent')
        n_alerts_critical = sum(1 for a in alerts if a.get('urgency') == 'critical')
        n_alerts_high = sum(1 for a in alerts if a.get('urgency') == 'high')
        n_rated = len(sigma_adj)
        n_excellent = sum(1 for v in sigma_adj.values() if v.get('rating') == 'excellent')

        return {
            'ok': True,
            'ts': time.time(),
            'summary': {
                'consensus_cities': len(consensus),
                'strong_agreement': n_strong,
                'divergent': n_divergent,
                'critical_alerts': n_alerts_critical,
                'high_alerts': n_alerts_high,
                'total_alerts': len(alerts),
                'stations_rated': n_rated,
                'excellent_stations': n_excellent,
            },
            'consensus': consensus,
            'alerts': alerts[:20],  # Top 20 most urgent
            'sigma_adjustments': sigma_adj,
        }




class RufloSharedState:
    """Shared memory bus for all Ruflo agents.

    Every agent gets a reference to this object. They publish data to named
    channels and subscribe to channels they care about. This enables:

    1. Real-time cross-agent communication (not just signal-dict stamps)
    2. Cross-cycle memory (persists across trade cycles)
    3. Event system (agents can broadcast events others react to)
    4. Strategy insights (winning patterns propagate to all agents)
    5. Priority system (agents can request attention on specific cities)
    """

    def __init__(self):
        # --- Named data channels (agent -> channel -> data) ---
        self._channels = {}
        # --- Event bus (list of events any agent can publish/read) ---
        self._events = []
        self._max_events = 500
        # --- Cross-cycle memory ---
        self._memory = {
            'cycle_count': 0,
            'last_cycle_ts': 0,
            'city_stats': {},          # per-city running stats
            'strategy_insights': [],   # winning patterns
            'station_reputation': {},  # long-term station quality
        }
        # --- Priority queue (cities that need attention) ---
        self._priority_cities = {}  # city -> {'score': float, 'reasons': [], 'ts': float}
        # --- Agent registry ---
        self._agents = {}  # name -> {'status': str, 'last_write': float, 'channels': []}
        log.info('SHARED_STATE: initialized — memory bus online')

    # =========== CHANNEL OPERATIONS ===========

    def publish(self, agent_name, channel, data):
        """Publish data to a named channel. Any agent can read it."""
        key = f'{agent_name}/{channel}'
        self._channels[key] = {
            'data': data,
            'ts': time.time(),
            'agent': agent_name,
            'channel': channel,
        }
        # Track agent activity
        if agent_name not in self._agents:
            self._agents[agent_name] = {'status': 'active', 'last_write': 0, 'channels': []}
        self._agents[agent_name]['last_write'] = time.time()
        if channel not in self._agents[agent_name]['channels']:
            self._agents[agent_name]['channels'].append(channel)

    def read(self, agent_name, channel):
        """Read the latest data from a specific agent's channel.
        Returns None if channel doesn't exist."""
        key = f'{agent_name}/{channel}'
        entry = self._channels.get(key)
        if entry:
            return entry['data']
        return None

    def read_any(self, channel):
        """Read all agents' data for a given channel name.
        Returns dict: {agent_name: data}"""
        result = {}
        for key, entry in self._channels.items():
            if entry['channel'] == channel:
                result[entry['agent']] = entry['data']
        return result

    def read_freshest(self, channel, max_age_s=300):
        """Read the freshest data for a channel, ignoring stale entries."""
        cutoff = time.time() - max_age_s
        best = None
        best_ts = 0
        for key, entry in self._channels.items():
            if entry['channel'] == channel and entry['ts'] > cutoff and entry['ts'] > best_ts:
                best = entry['data']
                best_ts = entry['ts']
        return best

    # =========== EVENT BUS ===========

    def emit(self, agent_name, event_type, payload=None):
        """Broadcast an event that any agent can react to."""
        event = {
            'ts': time.time(),
            'agent': agent_name,
            'type': event_type,
            'payload': payload or {},
        }
        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]

    def get_events(self, event_type=None, since_ts=0, limit=50):
        """Get recent events, optionally filtered by type and time."""
        filtered = self._events
        if event_type:
            filtered = [e for e in filtered if e['type'] == event_type]
        if since_ts:
            filtered = [e for e in filtered if e['ts'] > since_ts]
        return filtered[-limit:]

    # =========== CITY PRIORITY SYSTEM ===========

    def boost_city_priority(self, agent_name, city, score_delta, reason):
        """Increase a city's priority score. Multiple agents can boost."""
        if city not in self._priority_cities:
            self._priority_cities[city] = {'score': 0, 'reasons': [], 'ts': time.time(), 'boosters': []}
        p = self._priority_cities[city]
        p['score'] += score_delta
        p['reasons'].append(f'{agent_name}: {reason}')
        if len(p['reasons']) > 10:
            p['reasons'] = p['reasons'][-10:]
        if agent_name not in p['boosters']:
            p['boosters'].append(agent_name)
        p['ts'] = time.time()

    def get_priority_cities(self, min_score=5, limit=10):
        """Get cities sorted by priority score, above a minimum threshold."""
        now = time.time()
        # Decay scores older than 30 min
        for city, p in self._priority_cities.items():
            age = now - p['ts']
            if age > 1800:
                p['score'] *= 0.5
        ranked = sorted(
            [(c, p) for c, p in self._priority_cities.items() if p['score'] >= min_score],
            key=lambda x: x[1]['score'], reverse=True
        )
        return [{'city': c, 'score': round(p['score'], 1), 'reasons': p['reasons'],
                 'boosters': p['boosters']} for c, p in ranked[:limit]]

    # =========== CROSS-CYCLE MEMORY ===========

    def record_cycle(self):
        """Called at end of each trade cycle. Increments counter."""
        self._memory['cycle_count'] += 1
        self._memory['last_cycle_ts'] = time.time()

    def update_city_stats(self, city, key, value):
        """Update a running stat for a city across cycles."""
        if city not in self._memory['city_stats']:
            self._memory['city_stats'][city] = {}
        self._memory['city_stats'][city][key] = value
        self._memory['city_stats'][city]['_updated'] = time.time()

    def get_city_stats(self, city):
        """Get all accumulated stats for a city."""
        return self._memory['city_stats'].get(city, {})

    def add_strategy_insight(self, agent_name, insight):
        """Record a strategic observation any agent can learn from.
        E.g., 'YES trades on above-bins outperforming by 12% this week'"""
        self._memory['strategy_insights'].append({
            'ts': time.time(),
            'agent': agent_name,
            'insight': insight,
        })
        if len(self._memory['strategy_insights']) > 100:
            self._memory['strategy_insights'] = self._memory['strategy_insights'][-100:]

    def get_strategy_insights(self, limit=20):
        """Get recent strategy insights from all agents."""
        return self._memory['strategy_insights'][-limit:]

    def update_station_reputation(self, station_id, brier=None, freshness=None, trend_accuracy=None):
        """Update long-term reputation metrics for a weather station."""
        if station_id not in self._memory['station_reputation']:
            self._memory['station_reputation'][station_id] = {
                'brier_history': [], 'freshness_avg': 0,
                'trend_accuracy': 0, 'overall_grade': 'unknown',
            }
        rep = self._memory['station_reputation'][station_id]
        if brier is not None:
            rep['brier_history'].append(round(brier, 4))
            if len(rep['brier_history']) > 50:
                rep['brier_history'] = rep['brier_history'][-50:]
        if freshness is not None:
            rep['freshness_avg'] = round(freshness, 1)
        if trend_accuracy is not None:
            rep['trend_accuracy'] = round(trend_accuracy, 2)
        # Compute overall grade
        avg_brier = sum(rep['brier_history']) / len(rep['brier_history']) if rep['brier_history'] else 1.0
        if avg_brier < 0.1:
            rep['overall_grade'] = 'A'
        elif avg_brier < 0.2:
            rep['overall_grade'] = 'B'
        elif avg_brier < 0.35:
            rep['overall_grade'] = 'C'
        elif avg_brier < 0.5:
            rep['overall_grade'] = 'D'
        else:
            rep['overall_grade'] = 'F'

    def get_station_reputation(self, station_id):
        """Get the long-term reputation for a station."""
        return self._memory['station_reputation'].get(station_id, {})

    def get_all_station_reputations(self):
        """Get all station reputations."""
        return dict(self._memory['station_reputation'])

    # =========== AGENT REGISTRY ===========

    def register_agent(self, name, role):
        """Register an agent so others know it exists."""
        self._agents[name] = {
            'role': role,
            'status': 'active',
            'last_write': 0,
            'channels': [],
            'registered_at': time.time(),
        }

    def get_agent_directory(self):
        """List all registered agents and their status."""
        return dict(self._agents)

    # =========== FULL STATE REPORT ===========

    def get_state_report(self):
        """Complete state dump for the API endpoint."""
        now = time.time()
        # Count active channels
        active_channels = sum(1 for v in self._channels.values() if now - v['ts'] < 600)
        return {
            'ok': True,
            'ts': now,
            'memory': {
                'cycle_count': self._memory['cycle_count'],
                'last_cycle_ts': self._memory['last_cycle_ts'],
                'cities_tracked': len(self._memory['city_stats']),
                'strategy_insights': len(self._memory['strategy_insights']),
                'stations_rated': len(self._memory['station_reputation']),
            },
            'channels': {
                'total': len(self._channels),
                'active': active_channels,
                'list': [{'key': k, 'agent': v['agent'], 'channel': v['channel'],
                          'age_s': round(now - v['ts'])}
                         for k, v in sorted(self._channels.items(), key=lambda x: x[1]['ts'], reverse=True)[:30]],
            },
            'events': {
                'total': len(self._events),
                'recent': self._events[-15:],
            },
            'priority_cities': self.get_priority_cities(),
            'agent_directory': self.get_agent_directory(),
            'station_reputations': self.get_all_station_reputations(),
        }


class RufloCoordinator:
    """Agent 10: Meta-agent that supervises all other agents.

    Sits after all enrichment (Agents 7-9) and before the tradeable filter.
    Reads every field stamped by every agent and makes unified decisions:

    1. Cross-agent signal scoring — synthesizes sentinel, intel, accuracy data
    2. Veto / boost logic — can kill bad signals or amplify high-conviction ones
    3. Dynamic threshold adjustment — raises/lowers EV requirements per city
    4. Position sizing override — adjusts size based on multi-agent consensus
    5. Feedback loop — tracks which agent combos lead to wins/losses
    6. Agent health monitoring — detects if an agent is failing/stale
    """

    def __init__(self):
        self._city_cooldowns = {}       # city -> {'until': timestamp, 'reason': str}
        self._agent_health = {}         # agent_name -> {'last_ok': ts, 'fail_count': int}
        self._decision_log = []         # last N coordinator decisions
        self._max_log = 200
        self._cycle_count = 0
        self._conviction_stats = {'high': 0, 'medium': 0, 'low': 0, 'vetoed': 0}
        log.info('COORDINATOR: Agent 10 initialized — supervising all agents')

    # ---- AGENT HEALTH TRACKING ----

    def report_agent_status(self, agent_name, success, error_msg=None):
        """Called after each agent runs. Tracks health over time."""
        now = time.time()
        if agent_name not in self._agent_health:
            self._agent_health[agent_name] = {
                'last_ok': 0, 'last_fail': 0,
                'ok_count': 0, 'fail_count': 0,
                'consecutive_fails': 0, 'status': 'unknown'
            }
        h = self._agent_health[agent_name]
        if success:
            h['last_ok'] = now
            h['ok_count'] += 1
            h['consecutive_fails'] = 0
            h['status'] = 'healthy'
        else:
            h['last_fail'] = now
            h['fail_count'] += 1
            h['consecutive_fails'] += 1
            h['last_error'] = str(error_msg)[:200] if error_msg else 'unknown'
            if h['consecutive_fails'] >= 3:
                h['status'] = 'degraded'
            if h['consecutive_fails'] >= 10:
                h['status'] = 'failing'

    def get_agent_health(self):
        """Return health status of all tracked agents."""
        return dict(self._agent_health)

    # ---- CITY COOLDOWN (feedback from PostTradeAnalyst) ----

    def apply_cooldown(self, city, duration_s, reason):
        """Put a city on cooldown — reduce or block trading for a period."""
        self._city_cooldowns[city] = {
            'until': time.time() + duration_s,
            'reason': reason,
            'applied': time.time(),
        }
        log.info('COORDINATOR: cooldown %s for %ds — %s', city, duration_s, reason)

    def is_on_cooldown(self, city):
        """Check if a city is currently on cooldown."""
        cd = self._city_cooldowns.get(city)
        if not cd:
            return False, None
        if time.time() > cd['until']:
            del self._city_cooldowns[city]
            return False, None
        return True, cd['reason']

    # ---- CROSS-AGENT SIGNAL SCORING ----

    def _score_signal(self, sig):
        """Compute a unified conviction score (0-100) from all agent data on a signal.

        Weighs inputs from:
        - sentinel_confidence (Agent 7)
        - intel_consensus / intel_station_rating (Agent 9)
        - accuracy track record (via intel_sigma_mult)
        - EV and Kelly (base signal)
        - bin-boundary proximity (intel alerts)
        """
        score = 50.0  # neutral baseline
        reasons = []

        # --- Sentinel data (Agent 7) ---
        sent_conf = sig.get('sentinel_confidence', 0)
        if sent_conf >= 80:
            score += 15
            reasons.append(f'sentinel_high({sent_conf})')
        elif sent_conf >= 60:
            score += 5
            reasons.append(f'sentinel_ok({sent_conf})')
        elif sent_conf > 0:
            score -= 10
            reasons.append(f'sentinel_low({sent_conf})')

        # Trend alignment — if temp is trending toward our predicted direction
        trend_dir = sig.get('sentinel_trend', '')
        if trend_dir in ('rising', 'falling'):
            # Check if trend supports our position
            direction = sig.get('direction', '')
            if (direction == 'above' and trend_dir == 'rising') or \
               (direction == 'below' and trend_dir == 'falling'):
                score += 8
                reasons.append('trend_aligned')
            elif (direction == 'above' and trend_dir == 'falling') or \
                 (direction == 'below' and trend_dir == 'rising'):
                score -= 12
                reasons.append('trend_opposed')

        # --- Intelligence Feed data (Agent 9) ---
        consensus = sig.get('intel_consensus', 'unknown')
        if consensus == 'strong':
            score += 15
            reasons.append('consensus_strong')
        elif consensus == 'moderate':
            score += 5
            reasons.append('consensus_moderate')
        elif consensus == 'weak':
            score -= 8
            reasons.append('consensus_weak')
        elif consensus == 'divergent':
            score -= 20
            reasons.append('consensus_divergent')

        # Station accuracy rating
        rating = sig.get('intel_station_rating', 'unrated')
        if rating == 'excellent':
            score += 10
            reasons.append('station_excellent')
        elif rating == 'good':
            score += 5
            reasons.append('station_good')
        elif rating == 'poor':
            score -= 10
            reasons.append('station_poor')
        elif rating == 'unreliable':
            score -= 20
            reasons.append('station_unreliable')

        # Bin-boundary proximity (from intel alerts)
        boundary_urgency = sig.get('intel_boundary_urgency', '')
        approaching = sig.get('intel_approaching', False)
        if boundary_urgency == 'critical' and approaching:
            score += 12  # High opportunity if temp is about to cross a bin edge
            reasons.append('boundary_critical_approaching')
        elif boundary_urgency == 'critical' and not approaching:
            score -= 8  # Risky — near boundary but moving away
            reasons.append('boundary_critical_retreating')

        # --- Base signal quality ---
        theo_ev = sig.get('theo_ev', 0)
        if theo_ev >= 15:
            score += 10
            reasons.append(f'high_ev({theo_ev:.1f})')
        elif theo_ev >= 10:
            score += 5
        elif theo_ev < 5:
            score -= 10
            reasons.append(f'low_ev({theo_ev:.1f})')

        kelly = sig.get('kelly', 0)
        if kelly >= 8:
            score += 5
            reasons.append(f'strong_kelly({kelly:.1f})')

        # Clamp to 0-100
        score = max(0, min(100, score))
        return round(score, 1), reasons

    # ---- MAIN COORDINATION: EVALUATE ALL SIGNALS ----

    def evaluate(self, sigs, analyst=None):
        """Main coordination pass. Runs after all enrichment agents.

        For each signal:
        1. Compute cross-agent conviction score
        2. Check city cooldowns
        3. Apply veto/boost decisions
        4. Adjust recommended size
        5. Log the decision

        Mutates signals in-place and returns them.
        Also sets coordinator_verdict on each signal:
          'high_conviction', 'trade', 'reduce', 'veto'
        """
        self._cycle_count += 1
        now = time.time()

        # Check PostTradeAnalyst for losing cities (feedback loop)
        if analyst:
            try:
                recent = analyst.analyze()
                if isinstance(recent, list):
                    for trade in recent:
                        city = trade.get('city', '')
                        pnl = trade.get('pnl', 0)
                        if pnl < -5 and city:  # Lost more than $5 on a city
                            on_cd, _ = self.is_on_cooldown(city)
                            if not on_cd:
                                self.apply_cooldown(city, 1800, f'recent_loss_pnl={pnl:.2f}')
            except Exception as e:
                log.debug('COORDINATOR: analyst feedback failed: %s', e)

        decisions = []
        for sig in sigs:
            city = sig.get('city', '')
            conviction, reasons = self._score_signal(sig)
            sig['coordinator_conviction'] = conviction
            sig['coordinator_reasons'] = reasons

            # Check cooldown
            on_cd, cd_reason = self.is_on_cooldown(city)
            if on_cd:
                sig['coordinator_verdict'] = 'cooldown'
                sig['coordinator_note'] = f'city on cooldown: {cd_reason}'
                sig['coordinator_size_mult'] = 0.0
                decisions.append({'city': city, 'verdict': 'cooldown', 'conviction': conviction})
                self._conviction_stats['vetoed'] += 1
                continue

            # Decision logic based on conviction
            if conviction >= 75:
                sig['coordinator_verdict'] = 'high_conviction'
                sig['coordinator_size_mult'] = 1.5  # Boost size by 50%
                sig['coordinator_note'] = 'all agents agree — boosted'
                self._conviction_stats['high'] += 1
            elif conviction >= 50:
                sig['coordinator_verdict'] = 'trade'
                sig['coordinator_size_mult'] = 1.0  # Normal size
                sig['coordinator_note'] = 'acceptable signal'
                self._conviction_stats['medium'] += 1
            elif conviction >= 30:
                sig['coordinator_verdict'] = 'reduce'
                sig['coordinator_size_mult'] = 0.5  # Half size
                sig['coordinator_note'] = 'mixed signals — reduced size'
                self._conviction_stats['low'] += 1
            else:
                sig['coordinator_verdict'] = 'veto'
                sig['coordinator_size_mult'] = 0.0
                sig['coordinator_note'] = 'too many red flags — vetoed'
                self._conviction_stats['vetoed'] += 1

            # Intel reduce_size flag (from consensus divergence)
            if sig.get('intel_reduce_size') and sig['coordinator_size_mult'] > 0.5:
                sig['coordinator_size_mult'] = 0.5
                sig['coordinator_note'] += ' | intel_reduce_override'

            decisions.append({
                'city': city,
                'verdict': sig['coordinator_verdict'],
                'conviction': conviction,
                'reasons': reasons[:5],
                'size_mult': sig['coordinator_size_mult'],
            })

        # Trim decision log
        self._decision_log.extend(decisions)
        if len(self._decision_log) > self._max_log:
            self._decision_log = self._decision_log[-self._max_log:]

        log.info('COORDINATOR: evaluated %d signals — %d high, %d trade, %d reduce, %d veto',
                 len(sigs),
                 sum(1 for d in decisions if d['verdict'] == 'high_conviction'),
                 sum(1 for d in decisions if d['verdict'] == 'trade'),
                 sum(1 for d in decisions if d['verdict'] == 'reduce'),
                 sum(1 for d in decisions if d['verdict'] in ('veto', 'cooldown')))

        return sigs

    def get_coordinator_report(self):
        """Full report of coordinator state for the API endpoint."""
        return {
            'ok': True,
            'ts': time.time(),
            'cycle_count': self._cycle_count,
            'conviction_stats': dict(self._conviction_stats),
            'agent_health': self.get_agent_health(),
            'active_cooldowns': {
                city: {
                    'reason': cd['reason'],
                    'remaining_s': round(cd['until'] - time.time()),
                }
                for city, cd in self._city_cooldowns.items()
                if time.time() < cd['until']
            },
            'recent_decisions': self._decision_log[-30:],
        }


# MAIN MONITORING LOOP
# ============================================================
def run_monitor():
    log.info('Ruflo WeatherEdge Monitor starting...')
    validator = PreTradeValidator()
    monitor = PositionMonitor()
    analyst = PostTradeAnalyst()
    scanner = MarketScanner()

    last_scan = 0
    cycle = 0

    while True:
        cycle += 1
        now = time.time()
        log.info(f'=== Monitor cycle {cycle} ===')

        # Position check every 5 min
        alerts = monitor.check_positions()
        for a in alerts:
            log.warning(f'POSITION ALERT: {a}')

        # Full scan every 30 min
        if now - last_scan > 1800:
            signals = scanner.scan()
            log.info(f'SCANNER: {len(signals)} ranked signals')
            last_scan = now

        time.sleep(300)  # 5 min

if __name__ == '__main__':
    run_monitor()
