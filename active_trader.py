"""
WeatherEdge Active Trader - Ruflo-integrated position management engine.
Implements the 4 rules discovered in simulation:
  Rule 1: Entry kill switch - skip if max_achievable < bin_lo
  Rule 2: Fact-based exit - close when obs proves bin impossible  
  Rule 3: Hold winners to full $1.00 payout
  Rule 4: Momentum exit for international cities (no NWS obs)
"""
import logging, time, requests
from datetime import datetime, timezone

log = logging.getLogger('active_trader')

NWS_STATIONS = {
    'Atlanta': 'KATL', 'Miami': 'KMIA', 'Chicago': 'KORD',
    'New York': 'KLGA', 'Los Angeles': 'KLAX', 'Dallas': 'KDFW',
    'Seattle': 'KSEA', 'Denver': 'KDEN', 'Boston': 'KBOS',
    'Phoenix': 'KPHX', 'Houston': 'KIAH', 'San Francisco': 'KSFO',
}

_obs_cache = {}
_obs_ts = {}

def get_obs_temp_f(city):
    """Fetch latest NWS observation in Fahrenheit. Cached 15min."""
    station = NWS_STATIONS.get(city)
    if not station:
        return None
    now = time.time()
    if city in _obs_cache and now - _obs_ts.get(city, 0) < 900:
        return _obs_cache[city]
    try:
        r = requests.get(f'https://api.weather.gov/stations/{station}/observations/latest', timeout=8)
        if r.ok:
            tc = r.json()['properties']['temperature']['value']
            if tc is not None:
                tf = round(tc * 9/5 + 32, 1)
                _obs_cache[city] = tf
                _obs_ts[city] = now
                return tf
    except Exception as e:
        log.warning('OBS fetch failed for %s: %s', city, e)
    return _obs_cache.get(city)

def max_achievable_today(obs_temp_f, hour_local):
    """
    Estimate max temperature achievable from now until end of day.
    Based on typical diurnal heating rates.
    Peak is typically 3-4pm local time.
    """
    if obs_temp_f is None:
        return 999.0
    if hour_local < 10:
        heat = (15 - hour_local) * 2.5
    elif hour_local < 13:
        heat = (15 - hour_local) * 2.0
    elif hour_local < 15:
        heat = (15 - hour_local) * 1.5
    elif hour_local < 16:
        heat = 1.0
    else:
        heat = 0.0
    return obs_temp_f + max(0, heat)

def should_enter(city, bin_lo, bin_hi, local_hour):
    """
    Entry kill switch: returns (ok, reason)
    NEVER enter if the bin is physically unreachable given current obs.
    """
    obs = get_obs_temp_f(city)
    if obs is None:
        return True, 'no obs available - allowing entry'
    achievable = max_achievable_today(obs, local_hour)
    if achievable < bin_lo:
        return False, f'KILL: obs={obs}F max_achievable={achievable:.1f}F < bin_lo={bin_lo}F'
    if obs > bin_hi + 2:
        return False, f'KILL: obs={obs}F already above bin_hi={bin_hi}F'
    return True, f'OK: obs={obs}F achievable={achievable:.1f}F bin={bin_lo}-{bin_hi}F'

def should_exit_position(city, bin_lo, bin_hi, entry_price, current_price,
                          entry_cost, current_value, mins_to_resolution, local_hour):
    """
    Smart exit engine: returns (exit, reason, action)
    action: 'SELL_ALL', 'SELL_HALF', 'HOLD'
    """
    obs = get_obs_temp_f(city)

    # Rule 1: Fact-based kill - obs proves bin impossible
    if obs is not None:
        achievable = max_achievable_today(obs, local_hour)
        if achievable < bin_lo:
            return True, f'OBS_KILL: max_achievable={achievable:.1f}F < bin_lo={bin_lo}F', 'SELL_ALL'
        # Bin already passed (temp went above hi and cooling)
        if local_hour >= 16 and obs > bin_hi + 1:
            return True, f'OBS_PASSED: obs={obs}F > bin_hi={bin_hi}F and cooling', 'SELL_ALL'
        # Confirmed winner - hold to full payout
        if obs >= bin_lo and obs <= bin_hi:
            return False, f'WIN_CONFIRMED: obs={obs}F inside bin {bin_lo}-{bin_hi}F - HOLD TO $1', 'HOLD'

    # Rule 2: Time-based exit for deep losers
    if mins_to_resolution < 120 and entry_cost > 0:
        ratio = current_value / entry_cost
        if ratio < 0.3:
            return True, f'TIME_EXIT: {mins_to_resolution:.0f}min left, value at {ratio*100:.0f}% of entry', 'SELL_ALL'

    # Rule 3: Profit take for momentum positions
    if entry_cost > 0:
        ratio = current_value / entry_cost
        if ratio > 3.0:
            return True, f'PROFIT_TAKE_3X: value at {ratio:.1f}x entry', 'SELL_HALF'
        if ratio > 2.0 and mins_to_resolution < 240:
            return True, f'PROFIT_TAKE_2X: value at {ratio:.1f}x entry, <4h left', 'SELL_HALF'

    return False, 'HOLD', 'HOLD'

def run_position_monitor(positions, get_market_price_fn=None):
    """
    Main monitoring loop called by Ruflo position monitor agent.
    positions: list of dicts with keys:
      city, bin_lo, bin_hi, entry_price, entry_cost, 
      current_price, current_value, mins_to_resolution, token_id
    Returns list of exit actions to take.
    """
    actions = []
    now_utc = datetime.now(timezone.utc)
    for pos in positions:
        city = pos.get('city', '')
        local_hour = now_utc.hour - 5  # rough EST offset
        exit_flag, reason, action = should_exit_position(
            city=city,
            bin_lo=pos.get('bin_lo', 0),
            bin_hi=pos.get('bin_hi', 999),
            entry_price=pos.get('entry_price', 0),
            current_price=pos.get('current_price', 0),
            entry_cost=pos.get('entry_cost', 0),
            current_value=pos.get('current_value', 0),
            mins_to_resolution=pos.get('mins_to_resolution', 9999),
            local_hour=local_hour
        )
        if exit_flag:
            log.warning('EXIT SIGNAL: %s | %s | %s', city, reason, action)
            actions.append({
                'token_id': pos.get('token_id'),
                'market': pos.get('market', city),
                'action': action,
                'reason': reason,
                'city': city,
                'current_price': pos.get('current_price', 0),
                '_trade_ref': pos.get('_trade_ref'),
            })
        else:
            log.info('HOLD: %s | %s', city, reason)
    return actions

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    print('Active trader module loaded. Testing entry filters:')
    print()
    tests = [
        ('Atlanta', 74, 75, 12),
        ('Atlanta', 68, 69, 12),
        ('Miami', 74, 75, 12),
        ('Chicago', 62, 63, 14),
        ('Los Angeles', 70, 71, 11),
    ]
    for city, lo, hi, hour in tests:
        ok, reason = should_enter(city, lo, hi, hour)
        status = 'ENTER' if ok else 'SKIP'
        print(f'{status}: {city} {lo}-{hi}F at {hour}:00 local | {reason}')
