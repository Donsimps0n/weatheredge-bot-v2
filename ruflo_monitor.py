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

                # RULE A: time exit â only after grace; tighter ratio so spread
                # noise (up to ~50%) doesn't trigger a premature exit
                if not in_grace and mins_left < 120 and ratio < 0.35:
                    alerts.append({'alert': 'EXIT_TIME', 'market': title,
                        'reason': f'<2h to resolution, value at {ratio*100:.0f}% of entry (held {mins_held:.0f}m)',
                        'entry': entry, 'current': current, 'mins_left': mins_left})

                # RULE B: EV decay stop-loss â only after grace period
                elif not in_grace and ratio < 0.15:
                    alerts.append({'alert': 'EXIT_EV_DECAY', 'market': title,
                        'reason': f'value at only {ratio*100:.0f}% of entry after {mins_held:.0f}m - model wrong',
                        'entry': entry, 'current': current})

                # RULE C: profit take â fires immediately, no grace needed
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

    When a temperature bin is clearly wrong — both our model AND the market
    price YES at <= 10% — buying NO at 90-98c is near-guaranteed profit.
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
                continue   # Our model still thinks YES is plausible — skip

            # Must have a NO token to trade
            no_token_id = None
            for tk in sig.get('tokens', []):
                if str(tk.get('outcome', '')).lower() == 'no':
                    no_token_id = tk.get('token_id', '')
                    break
            if not no_token_id:
                continue

            cond_key = sig.get('condition_id', '') + '_NO'
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
    correct bin is highly likely to resolve YES but still priced at 92–98c.
    Symmetric mirror of NOHarvester.

    Strategy: When market prices YES at ≥0.92 AND our model also agrees
    (our_prob ≥ 88%), the expected return is 2–9% with very high confidence.
    This is the 'right-bin certainty' edge used by Handsanitizer23 (#5 $68K)
    and Hans323 (#3 $80K) — we capture it at lower size but same edge logic.

    Return per trade:  (1.0 - yes_price) / yes_price * 100
      e.g., YES at 0.92 → ~8.7% | YES at 0.95 → ~5.3% | YES at 0.98 → ~2.0%
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
            cond_key = sig.get('condition_id', '') + '_YES_HARVEST'
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
        opps.sort(key=lambda x: x['yes_price'], reverse=False)  # cheapest YES first = highest return
        if opps:
            log.info('YES_HARVESTER: %d opportunities | top=%s YES=%.3f exp=+%.1f%%',
                     len(opps), opps[0]['city'], opps[0]['yes_price'],
                     opps[0]['expected_return_pct'])
        return opps[:15]


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
