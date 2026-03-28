#!/usr/bin/env python3
"""
WeatherEdge Exit Agents - ProfitTaker (Agent 11) and RiskCutter (Agent 12).
Handles position exit strategies: profit-taking, trailing stops, loss limits,
time decay, and weather divergence exits.
"""
import time
import math
import re
import logging

log = logging.getLogger(__name__)

# ============================================================
# AGENT 11 - PROFIT TAKER (Exit Strategy: Maximize Realized Gains)
# ============================================================
class ProfitTaker:
    """Monitors open positions and generates SELL signals to lock in profits.

    Math:
      - Profit targets: Kelly-derived multipliers (2.5x high conf, 2x med, 1.5x low)
      - Trailing stops: Fixed-dollar stops (better than % for cheap 3-10c bins)
        1.5c for low vol, 2.5c medium, 4c high volatility
      - Partial exits: 50% at 1.5x, 25% at 2.5x, hold 25% with tight trail
      - EV comparison: SELL if EV(sell_now) > EV(hold_to_resolution)
        EV(sell) = current_price
        EV(hold) = P(win) * 100 - entry_price  (payout $1 minus cost)
      - Time-urgency exit: if <2 hrs to resolution and in profit, sell

    Position tracking:
      Each position = {
        'token_id', 'city', 'bin', 'entry_price', 'entry_time',
        'size', 'current_price', 'peak_price', 'peak_time',
        'partial_sold': 0/1/2, 'signal_type': 'BUY YES'/'BUY NO',
        'question', 'condition_id',
      }
    """

    # Profit target multipliers by confidence level
    TARGETS = {
        'HIGH':   {'t1': 1.5, 't2': 2.5, 't3': 4.0},   # aggressive - ride winners
        'MED':    {'t1': 1.5, 't2': 2.0, 't3': 3.0},
        'LOW':    {'t1': 1.3, 't2': 1.8, 't3': 2.5},
    }

    # Fixed trailing stop amounts (cents) by volatility regime
    TRAIL_STOPS = {
        'low':    1.5,    # stable price, tight stop
        'medium': 2.5,
        'high':   4.0,    # volatile, wider stop
    }

    # Partial exit schedule: (fraction_to_sell, target_multiplier)
    PARTIAL_SCHEDULE = [
        (0.50, 1.5),   # sell 50% at 1.5x
        (0.50, 2.5),   # sell 50% of remainder (25% total) at 2.5x
        # remaining 25% rides with trailing stop
    ]

    def __init__(self):
        self._positions = {}       # token_id -> position dict
        self._sell_signals = []    # pending sell signals
        self._stats = {
            'total_exits': 0, 'profit_target_exits': 0,
            'trailing_stop_exits': 0, 'partial_exits': 0,
            'time_urgency_exits': 0, 'total_profit_taken': 0.0,
        }
        log.info('PROFIT_TAKER: initialized')

    def register_position(self, token_id, entry_price, size, city='',
                          bin_label='', confidence='MED', signal_type='BUY YES',
                          question='', condition_id=''):
        """Register a new position when a BUY order fills."""
        now = time.time()
        self._positions[token_id] = {
            'token_id': token_id,
            'city': city,
            'bin': bin_label,
            'entry_price': entry_price,   # in cents (3-99)
            'entry_time': now,
            'size': size,                 # dollar amount
            'shares': round(size / (entry_price / 100), 2) if entry_price > 0 else 0,
            'current_price': entry_price,
            'peak_price': entry_price,
            'peak_time': now,
            'partial_sold': 0,            # 0=none, 1=first partial, 2=second partial
            'confidence': confidence,
            'signal_type': signal_type,
            'question': question,
            'condition_id': condition_id,
            'unrealized_pnl': 0.0,
        }
        log.info('PROFIT_TAKER: registered %s @ %.1f¢ ($%.2f) [%s %s]',
                 city, entry_price, size, bin_label, confidence)

    def update_prices(self, price_map):
        """Update current market prices. price_map = {token_id: current_price_cents}"""
        for tid, price in price_map.items():
            pos = self._positions.get(tid)
            if not pos:
                continue
            pos['current_price'] = price
            # Track peak
            if price > pos['peak_price']:
                pos['peak_price'] = price
                pos['peak_time'] = time.time()
            # Unrealized P&L
            shares = pos['shares']
            pos['unrealized_pnl'] = round((price - pos['entry_price']) / 100 * shares, 4)

    def _get_volatility_regime(self, pos):
        """Estimate volatility based on price movement from entry."""
        spread = abs(pos['current_price'] - pos['entry_price'])
        if spread < 3:
            return 'low'
        elif spread < 8:
            return 'medium'
        return 'high'

    def _ev_sell_vs_hold(self, pos, p_win):
        """Compare EV of selling now vs holding to resolution.
        EV(sell) = current_price (guaranteed)
        EV(hold) = P(win) * 100 - (1-P(win)) * entry_price
                 = P(win)*100 cents payout if win, else lose entry
        Actually for shares already owned:
        EV(hold) = P(win) * 100  (payout per share in cents)
        EV(sell) = current_price (what we get now per share)
        """
        ev_hold = p_win * 100.0    # expected cents per share if we hold
        ev_sell = pos['current_price']  # guaranteed cents per share now
        return ev_sell, ev_hold

    def evaluate(self, hours_to_resolution=24.0, p_win_map=None):
        """Run profit-taking evaluation on all positions.

        Args:
            hours_to_resolution: hours until market resolves
            p_win_map: {token_id: probability_of_winning} from weather models

        Returns list of sell signals.
        """
        self._sell_signals = []
        p_win_map = p_win_map or {}

        for tid, pos in list(self._positions.items()):
            entry = pos['entry_price']
            current = pos['current_price']
            peak = pos['peak_price']
            conf = pos.get('confidence', 'MED')
            targets = self.TARGETS.get(conf, self.TARGETS['MED'])
            vol = self._get_volatility_regime(pos)
            trail_amt = self.TRAIL_STOPS[vol]
            p_win = p_win_map.get(tid, current / 100.0)  # fallback: use market price as prob

            # ─── Check 1: Profit target exits ───
            multiplier = current / max(entry, 0.5)

            # Partial exit schedule
            if pos['partial_sold'] == 0 and multiplier >= targets['t1']:
                # First partial: sell 50%
                sell_frac = self.PARTIAL_SCHEDULE[0][0]
                sell_size = round(pos['size'] * sell_frac, 2)
                self._sell_signals.append({
                    'token_id': tid, 'action': 'SELL', 'reason': 'partial_profit_t1',
                    'sell_fraction': sell_frac, 'sell_size': sell_size,
                    'entry_price': entry, 'current_price': current,
                    'multiplier': round(multiplier, 2),
                    'city': pos['city'], 'bin': pos['bin'],
                    'urgency': 'medium',
                })
                pos['partial_sold'] = 1
                pos['size'] = round(pos['size'] - sell_size, 2)
                pos['shares'] = round(pos['shares'] * (1 - sell_frac), 2)
                self._stats['partial_exits'] += 1
                continue

            if pos['partial_sold'] == 1 and multiplier >= targets['t2']:
                # Second partial: sell 50% of remaining (25% of original)
                sell_frac = self.PARTIAL_SCHEDULE[1][0]
                sell_size = round(pos['size'] * sell_frac, 2)
                self._sell_signals.append({
                    'token_id': tid, 'action': 'SELL', 'reason': 'partial_profit_t2',
                    'sell_fraction': sell_frac, 'sell_size': sell_size,
                    'entry_price': entry, 'current_price': current,
                    'multiplier': round(multiplier, 2),
                    'city': pos['city'], 'bin': pos['bin'],
                    'urgency': 'medium',
                })
                pos['partial_sold'] = 2
                pos['size'] = round(pos['size'] - sell_size, 2)
                pos['shares'] = round(pos['shares'] * (1 - sell_frac), 2)
                self._stats['partial_exits'] += 1
                continue

            # Full exit at t3 multiplier
            if multiplier >= targets['t3']:
                self._sell_signals.append({
                    'token_id': tid, 'action': 'SELL', 'reason': 'profit_target_full',
                    'sell_fraction': 1.0, 'sell_size': pos['size'],
                    'entry_price': entry, 'current_price': current,
                    'multiplier': round(multiplier, 2),
                    'city': pos['city'], 'bin': pos['bin'],
                    'urgency': 'high',
                })
                self._stats['profit_target_exits'] += 1
                continue

            # ─── Check 2: Trailing stop ───
            # Only active after position has appreciated (peak > entry + 2c)
            if peak > entry + 2.0 and current <= peak - trail_amt:
                self._sell_signals.append({
                    'token_id': tid, 'action': 'SELL', 'reason': 'trailing_stop',
                    'sell_fraction': 1.0, 'sell_size': pos['size'],
                    'entry_price': entry, 'current_price': current,
                    'peak_price': peak, 'trail_amount': trail_amt,
                    'city': pos['city'], 'bin': pos['bin'],
                    'urgency': 'high',
                })
                self._stats['trailing_stop_exits'] += 1
                continue

            # ─── Check 3: EV comparison ───
            ev_sell, ev_hold = self._ev_sell_vs_hold(pos, p_win)
            # Sell if EV(sell) is better AND we're in profit AND not too early
            if (ev_sell > ev_hold * 1.1 and current > entry
                    and hours_to_resolution < 12):
                self._sell_signals.append({
                    'token_id': tid, 'action': 'SELL', 'reason': 'ev_comparison',
                    'sell_fraction': 1.0, 'sell_size': pos['size'],
                    'entry_price': entry, 'current_price': current,
                    'ev_sell': round(ev_sell, 1), 'ev_hold': round(ev_hold, 1),
                    'city': pos['city'], 'bin': pos['bin'],
                    'urgency': 'medium',
                })
                self._stats['profit_target_exits'] += 1
                continue

            # ─── Check 4: Time urgency exit ───
            # <2 hours to resolution and in profit — take what you can get
            if hours_to_resolution < 2.0 and current > entry + 1.0:
                self._sell_signals.append({
                    'token_id': tid, 'action': 'SELL', 'reason': 'time_urgency_profit',
                    'sell_fraction': 1.0, 'sell_size': pos['size'],
                    'entry_price': entry, 'current_price': current,
                    'hours_left': round(hours_to_resolution, 1),
                    'city': pos['city'], 'bin': pos['bin'],
                    'urgency': 'critical',
                })
                self._stats['time_urgency_exits'] += 1

        return self._sell_signals

    def remove_position(self, token_id, realized_pnl=0.0):
        """Remove position after sell executes."""
        pos = self._positions.pop(token_id, None)
        if pos:
            self._stats['total_exits'] += 1
            self._stats['total_profit_taken'] += realized_pnl
            log.info('PROFIT_TAKER: closed %s %s | PnL: $%.4f',
                     pos['city'], pos['bin'], realized_pnl)

    def get_positions(self):
        """Return all tracked positions."""
        return dict(self._positions)

    def get_sell_signals(self):
        """Return pending sell signals."""
        return list(self._sell_signals)

    def get_report(self):
        """Status report for API/coordinator."""
        positions = list(self._positions.values())
        total_unrealized = sum(p['unrealized_pnl'] for p in positions)
        return {
            'ok': True,
            'agent': 'profit_taker',
            'active_positions': len(positions),
            'pending_sells': len(self._sell_signals),
            'total_unrealized_pnl': round(total_unrealized, 4),
            'stats': dict(self._stats),
            'positions': [{
                'city': p['city'], 'bin': p['bin'],
                'entry': p['entry_price'], 'current': p['current_price'],
                'peak': p['peak_price'], 'multiplier': round(p['current_price'] / max(p['entry_price'], 0.5), 2),
                'pnl': p['unrealized_pnl'], 'partial_sold': p['partial_sold'],
            } for p in positions],
        }


# ============================================================
# AGENT 12 - RISK CUTTER (Exit Strategy: Minimize Losses)
# ============================================================
class RiskCutter:
    """Monitors open positions and generates SELL signals to cut losses.

    Math:
      Time-decay probability for temperature bins:
        P(bin_hit) = Phi((bin_mid - current_temp) / (sigma * sqrt(hours_left)))
        where sigma = historical hourly temp volatility (~0.5-1.5 C/hr)
        Phi = standard normal CDF

      Loss limit matrix (hours_to_resolution x loss%):
        >12 hrs: cut if loss >50% AND P(win) <25%
        6-12 hrs: cut if loss >40% AND P(win) <15%
        2-6 hrs:  cut if loss >25% AND P(win) <10%
        <2 hrs:   cut if loss >15% (regardless of P)

      Rapid decay indicator:
        price_cents * hours_remaining < 5 → immediate sell
        (e.g., 2c bin with 2 hours left = 4 < 5 → sell)

      Weather divergence:
        trend_rate (C/hr) tells us if temp is moving toward or away from bin.
        If moving away at >0.5 C/hr, multiply P(win) by 0.7 (pessimistic adj)
        If moving toward at >0.5 C/hr, multiply P(win) by 1.3 (optimistic adj)

      End-of-day cleanup:
        2 hours before resolution, sell everything except the 1-2 bins closest
        to the current temperature (highest P(win)).
    """

    # Loss limit thresholds: (max_hours, max_loss_pct, max_p_win)
    LOSS_MATRIX = [
        (2.0,  0.15, 1.00),   # <2 hrs: cut if lost >15% (no P threshold)
        (6.0,  0.25, 0.10),   # 2-6 hrs: cut if lost >25% AND P(win)<10%
        (12.0, 0.40, 0.15),   # 6-12 hrs: cut if lost >40% AND P(win)<15%
        (99.0, 0.50, 0.25),   # >12 hrs: cut if lost >50% AND P(win)<25%
    ]

    # HARD KILL: unconditional exit at -60% regardless of P(win) or time
    # No position should ever sit at -95%. This is the ultimate safety net.
    HARD_DRAWDOWN_KILL = 0.60  # 60% loss = immediate sell, no questions asked

    # Rapid decay threshold
    RAPID_DECAY_THRESHOLD = 5.0  # price_cents * hours_remaining

    # End-of-day cleanup: hours before resolution
    EOD_CLEANUP_HOURS = 2.0
    # Keep only bins within this distance (°F) of current temp
    EOD_KEEP_DISTANCE_F = 3.0

    def __init__(self):
        self._cut_signals = []
        self._stats = {
            'total_cuts': 0, 'loss_limit_cuts': 0,
            'rapid_decay_cuts': 0, 'weather_divergence_cuts': 0,
            'eod_cleanup_cuts': 0, 'total_loss_cut': 0.0,
        }
        log.info('RISK_CUTTER: initialized')

    @staticmethod
    def _phi(x):
        """Standard normal CDF approximation."""
        import math
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def calc_bin_probability(self, current_temp_f, bin_lo_f, bin_hi_f,
                             hours_left, trend_rate_f_hr=0.0,
                             sigma_f_hr=1.5):
        """Calculate probability that daily high falls in [bin_lo, bin_hi].

        Uses projected temperature + normal distribution:
          projected = current_temp + trend_rate * min(hours_left, 6)
          P = Phi((bin_hi - projected) / (sigma * sqrt(h))) -
              Phi((bin_lo - projected) / (sigma * sqrt(h)))

        Args:
            current_temp_f: current temperature in °F
            bin_lo_f, bin_hi_f: bin boundaries in °F
            hours_left: hours until market resolution
            trend_rate_f_hr: temperature trend in °F/hr (positive=warming)
            sigma_f_hr: hourly temp volatility in °F (default 1.5)
        """
        import math
        if hours_left <= 0:
            return 0.0

        # Project temp forward (cap at 6 hours of trend extrapolation)
        trend_hours = min(hours_left, 6.0)
        projected = current_temp_f + trend_rate_f_hr * trend_hours

        # Standard deviation grows with sqrt of time
        total_sigma = sigma_f_hr * math.sqrt(hours_left)
        if total_sigma < 0.1:
            total_sigma = 0.1

        # For daily HIGH market: we care about the maximum temperature
        # The daily high tends to occur in early-mid afternoon
        # If it's past peak hours, current temp IS close to the high
        bin_mid = (bin_lo_f + bin_hi_f) / 2.0

        p = self._phi((bin_hi_f - projected) / total_sigma) - \
            self._phi((bin_lo_f - projected) / total_sigma)

        return max(0.001, min(0.999, round(p, 4)))

    def evaluate(self, positions, hours_to_resolution=24.0,
                 sentinel_data=None, profit_taker_signals=None):
        """Run risk-cutting evaluation on all positions.

        Args:
            positions: dict from ProfitTaker.get_positions()
            hours_to_resolution: hours until midnight resolution
            sentinel_data: {city: {'current_f', 'trend_rate_f_hr'}} from WeatherSentinel
            profit_taker_signals: list of sell signals already generated by ProfitTaker
                                  (skip positions that ProfitTaker is already selling)

        Returns list of cut signals.
        """
        self._cut_signals = []
        sentinel_data = sentinel_data or {}
        # Skip positions that ProfitTaker already flagged
        already_selling = set()
        if profit_taker_signals:
            for s in profit_taker_signals:
                already_selling.add(s.get('token_id', ''))

        for tid, pos in positions.items():
            if tid in already_selling:
                continue

            entry = pos['entry_price']
            current = pos['current_price']
            city = pos.get('city', '')

            # Loss percentage
            if entry > 0:
                loss_pct = (entry - current) / entry
            else:
                loss_pct = 0.0

            # Get weather data for this city
            wx = sentinel_data.get(city, {})
            current_temp_f = wx.get('current_f')
            trend_rate_f = wx.get('trend_rate_f_hr', 0.0)

            # Parse bin boundaries from question/bin label
            bin_lo, bin_hi = self._parse_bin(pos.get('bin', ''), pos.get('question', ''))

            # Calculate P(win) if we have weather data and bin info
            p_win = None
            if current_temp_f is not None and bin_lo is not None:
                p_win = self.calc_bin_probability(
                    current_temp_f, bin_lo, bin_hi,
                    hours_to_resolution, trend_rate_f
                )
                # Weather divergence adjustment
                if trend_rate_f != 0:
                    bin_mid = (bin_lo + bin_hi) / 2.0
                    temp_moving_toward = (
                        (trend_rate_f > 0 and current_temp_f < bin_mid) or
                        (trend_rate_f < 0 and current_temp_f > bin_mid)
                    )
                    if not temp_moving_toward and abs(trend_rate_f) > 0.5:
                        p_win *= 0.7   # pessimistic: temp moving away
                    elif temp_moving_toward and abs(trend_rate_f) > 0.5:
                        p_win *= 1.3   # optimistic: temp moving toward
                    p_win = max(0.001, min(0.999, p_win))

            # Fallback P(win) from market price
            if p_win is None:
                p_win = current / 100.0

            # ─── Check 0: HARD DRAWDOWN KILL — unconditional ───
            # No position should ever sit at -95%. Kill at -60% regardless of P(win) or time.
            if loss_pct >= self.HARD_DRAWDOWN_KILL:
                self._cut_signals.append({
                    'token_id': tid, 'action': 'SELL', 'reason': 'hard_drawdown_kill',
                    'sell_fraction': 1.0, 'sell_size': pos['size'],
                    'entry_price': entry, 'current_price': current,
                    'loss_pct': round(loss_pct * 100, 1),
                    'p_win': round(p_win, 3) if p_win else 0,
                    'city': city, 'bin': pos.get('bin', ''),
                    'urgency': 'critical',
                })
                self._stats['loss_limit_cuts'] += 1
                log.warning("HARD_KILL: %s | -%d%% loss | entry=%.1fc curr=%.1fc | P(win)=%.1f%%",
                            city, loss_pct * 100, entry, current, (p_win or 0) * 100)
                continue

            # ─── Check 1: Rapid decay ───
            rapid_score = current * hours_to_resolution
            if rapid_score < self.RAPID_DECAY_THRESHOLD and current < entry:
                self._cut_signals.append({
                    'token_id': tid, 'action': 'SELL', 'reason': 'rapid_decay',
                    'sell_fraction': 1.0, 'sell_size': pos['size'],
                    'entry_price': entry, 'current_price': current,
                    'rapid_score': round(rapid_score, 1),
                    'city': city, 'bin': pos.get('bin', ''),
                    'urgency': 'critical',
                })
                self._stats['rapid_decay_cuts'] += 1
                continue

            # ─── Check 2: Loss limit matrix ───
            if loss_pct > 0:  # only if position is in loss
                for max_hrs, max_loss, max_p in self.LOSS_MATRIX:
                    if hours_to_resolution <= max_hrs:
                        if loss_pct >= max_loss and p_win <= max_p:
                            self._cut_signals.append({
                                'token_id': tid, 'action': 'SELL',
                                'reason': 'loss_limit',
                                'sell_fraction': 1.0, 'sell_size': pos['size'],
                                'entry_price': entry, 'current_price': current,
                                'loss_pct': round(loss_pct * 100, 1),
                                'p_win': round(p_win, 3),
                                'hours_left': round(hours_to_resolution, 1),
                                'city': city, 'bin': pos.get('bin', ''),
                                'urgency': 'high',
                            })
                            self._stats['loss_limit_cuts'] += 1
                        break

            # ─── Check 3: Weather divergence ───
            # If temp is moving strongly away from our bin and P(win) is low
            if (current_temp_f is not None and bin_lo is not None
                    and not self._is_temp_near_bin(current_temp_f, bin_lo, bin_hi, 10.0)
                    and p_win < 0.05 and current < entry):
                self._cut_signals.append({
                    'token_id': tid, 'action': 'SELL',
                    'reason': 'weather_divergence',
                    'sell_fraction': 1.0, 'sell_size': pos['size'],
                    'entry_price': entry, 'current_price': current,
                    'current_temp_f': current_temp_f,
                    'bin': pos.get('bin', ''),
                    'p_win': round(p_win, 3),
                    'city': city,
                    'urgency': 'high',
                })
                self._stats['weather_divergence_cuts'] += 1
                continue

            # ─── Check 4: End-of-day cleanup ───
            if hours_to_resolution <= self.EOD_CLEANUP_HOURS:
                if (current_temp_f is not None and bin_lo is not None
                        and not self._is_temp_near_bin(
                            current_temp_f, bin_lo, bin_hi,
                            self.EOD_KEEP_DISTANCE_F)):
                    self._cut_signals.append({
                        'token_id': tid, 'action': 'SELL',
                        'reason': 'eod_cleanup',
                        'sell_fraction': 1.0, 'sell_size': pos['size'],
                        'entry_price': entry, 'current_price': current,
                        'hours_left': round(hours_to_resolution, 1),
                        'distance_f': round(abs(current_temp_f - (bin_lo + bin_hi) / 2), 1),
                        'city': city, 'bin': pos.get('bin', ''),
                        'urgency': 'critical',
                    })
                    self._stats['eod_cleanup_cuts'] += 1

        return self._cut_signals

    @staticmethod
    def _parse_bin(bin_label, question=''):
        """Extract bin lo/hi in °F from label like '70-71°F' or question text."""
        import re
        text = bin_label or question or ''
        m = re.search(r'(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)\s*°?\s*[Ff]', text)
        if m:
            return float(m.group(1)), float(m.group(2))
        # Try single threshold: "above 75°F"
        m2 = re.search(r'(?:above|over|exceed)\s*(\d+\.?\d*)', text.lower())
        if m2:
            t = float(m2.group(1))
            return t, t + 5.0  # assume ~5°F bin width
        return None, None

    @staticmethod
    def _is_temp_near_bin(current_f, bin_lo, bin_hi, distance_f):
        """Check if current temp is within distance of the bin."""
        bin_mid = (bin_lo + bin_hi) / 2.0
        return abs(current_f - bin_mid) <= distance_f

    def get_cut_signals(self):
        return list(self._cut_signals)

    def get_report(self):
        return {
            'ok': True,
            'agent': 'risk_cutter',
            'pending_cuts': len(self._cut_signals),
            'stats': dict(self._stats),
            'cut_signals': self._cut_signals[:10],  # top 10
        }
