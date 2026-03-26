#!/usr/bin/env python3
"""
WeatherEdge Ruflo Monitor Ñ runs as daemon alongside the bot.
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
# AGENT 1 Ñ PRE-TRADE SIGNAL VALIDATOR
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
# AGENT 2 Ñ POSITION MONITOR
# ============================================================
class PositionMonitor:
    """Monitors open positions every 5 min, applies smart exit rules."""

    def check_positions(self) -> list:
        alerts = []
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
                        mins_left = (end - datetime.now(timezone.utc)).total_seconds() / 60
                    except: pass

                if entry <= 0: continue
                ratio = current / entry

                # RULE A: time exit
                if mins_left < 120 and ratio < 0.5:
                    alerts.append({'alert': 'EXIT_TIME', 'market': title,
                        'reason': f'<2h to resolution, value at {ratio*100:.0f}% of entry',
                        'entry': entry, 'current': current, 'mins_left': mins_left})

                # RULE B: EV decay (would need model check Ñ flag for now)
                elif ratio < 0.15:
                    alerts.append({'alert': 'EXIT_EV_DECAY', 'market': title,
                        'reason': f'value at only {ratio*100:.0f}% of entry Ñ likely model was wrong',
                        'entry': entry, 'current': current})

                # RULE C: profit take
                elif ratio > 2.0:
                    alerts.append({'alert': 'PROFIT_TAKE', 'market': title,
                        'reason': f'value at {ratio*100:.0f}% of entry Ñ take 50% profit',
                        'entry': entry, 'current': current})

        except Exception as e:
            alerts.append({'alert': 'ERROR', 'msg': str(e)})
        return alerts

# ============================================================
# AGENT 3 Ñ POST-TRADE ANALYST
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
                log.warning('ALERT: rolling win rate < 40% Ñ consider pausing bot')

# ============================================================
# AGENT 4 Ñ MARKET INTELLIGENCE SCANNER
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
