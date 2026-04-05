"""
WeatherEdge Active Trader - Ruflo-integrated position management engine.
Implements the 4 rules discovered in simulation:
  Rule 1: Entry kill switch - skip if max_achievable < bin_lo
  Rule 2: Fact-based exit - close when obs proves bin impossible
  Rule 3: Hold winners to full $1.00 payout
  Rule 4: Momentum exit for international cities (no NWS obs)

Now enhanced: uses METAR data for ALL 58 cities globally, not just 12
US NWS stations. International cities now get the same fact-based exit
logic as US cities.
"""
import logging, time, requests
from datetime import datetime, timezone

log = logging.getLogger('active_trader')

# Legacy NWS-only stations (still used as primary for US cities)
NWS_STATIONS = {
    'Atlanta': 'KATL', 'Miami': 'KMIA', 'Chicago': 'KORD',
    'New York': 'KLGA', 'Los Angeles': 'KLAX', 'Dallas': 'KDAL',
    'Seattle': 'KSEA', 'Denver': 'KBKF', 'Boston': 'KBOS',
    'Phoenix': 'KPHX', 'Houston': 'KHOU', 'San Francisco': 'KSFO',
}

# Full ICAO mapping for ALL 58 cities — enables global fact-based exits
ALL_STATIONS = {
    # US (14) — all matched to Polymarket settlement stations
    'Atlanta': 'KATL', 'Miami': 'KMIA', 'Chicago': 'KORD',
    'New York': 'KLGA', 'Los Angeles': 'KLAX', 'Dallas': 'KDAL',
    'Seattle': 'KSEA', 'Denver': 'KBKF', 'Boston': 'KBOS',
    'Phoenix': 'KPHX', 'Houston': 'KHOU', 'San Francisco': 'KSFO',
    'Minneapolis': 'KMSP', 'Las Vegas': 'KLAS',
    # Canada
    'Toronto': 'CYYZ', 'Vancouver': 'CYVR', 'Montreal': 'CYUL',
    # Mexico
    'Mexico City': 'MMMX',
    # Europe
    'London': 'EGLC', 'Dublin': 'EIDW', 'Paris': 'LFPG',
    'Amsterdam': 'EHAM', 'Berlin': 'EDDB', 'Frankfurt': 'EDDF',
    'Madrid': 'LEMD', 'Barcelona': 'LEBL', 'Rome': 'LIRF',
    'Milan': 'LIMC', 'Athens': 'LGAV', 'Lisbon': 'LPPT',
    'Stockholm': 'ESSA', 'Copenhagen': 'EKCH', 'Moscow': 'UUWW',
    'Warsaw': 'EPWA',
    # Middle East
    'Dubai': 'OMDB', 'Istanbul': 'LTAC', 'Tel Aviv': 'LLBG',
    # Asia
    'Mumbai': 'VABB', 'Delhi': 'VIDP', 'Bangalore': 'VOBL',
    'Singapore': 'WSSS', 'Bangkok': 'VTBS', 'Hong Kong': 'VHHH',
    'Tokyo': 'RJTT', 'Seoul': 'RKSI', 'Shanghai': 'ZSPD',
    'Beijing': 'ZBAA',
    # Oceania
    'Sydney': 'YSSY', 'Melbourne': 'YMML', 'Auckland': 'NZAA',
    # South America
    'São Paulo': 'SBGR', 'Sao Paulo': 'SBGR', 'Rio de Janeiro': 'SBGL',
    'Buenos Aires': 'SAEZ', 'Santiago': 'SCEL',
    # Africa
    'Cairo': 'HECA', 'Johannesburg': 'FAOR', 'Lagos': 'DNMM',
    # New cities
    'Chengdu': 'ZUUU', 'Chongqing': 'ZUCK', 'Shenzhen': 'ZGSZ',
    'Wuhan': 'ZHHH', 'Taipei': 'RCSS', 'Busan': 'RKPK',
    'Jakarta': 'WIHH', 'Kuala Lumpur': 'WMKK',
    'Helsinki': 'EFHK', 'Munich': 'EDDM', 'Lucknow': 'VILK',
    'Wellington': 'NZWN', 'Panama City': 'MPMG',
}

METAR_API = 'https://aviationweather.gov/api/data/metar'

_obs_cache = {}
_obs_ts = {}

# ObsConfirm agent can inject its observations here for cross-agent sharing
_shared_obs: dict = {}  # city -> temp_f (set by obs_confirm agent)


def set_shared_obs(obs_data: dict):
    """Called by ObsConfirmAgent to share its observations with the exit engine.
    obs_data may be {city: temp_f} or {city: {temp_f: ..., ...}} from get_live_obs().
    We normalize to {city: float} for get_obs_temp_f() compatibility.
    """
    global _shared_obs
    normalized = {}
    for city, val in obs_data.items():
        if isinstance(val, dict):
            normalized[city] = val.get('temp_f', val.get('temp_c', 0))
        else:
            normalized[city] = float(val)
    _shared_obs = normalized


def get_obs_temp_f(city):
    """Fetch latest observation in Fahrenheit for ANY city globally.

    Priority:
    1. Shared observations from ObsConfirmAgent (freshest, already polled)
    2. NWS API (US cities only — most reliable)
    3. METAR API via aviationweather.gov (all cities globally)
    4. Cache (stale data better than nothing)
    """
    now = time.time()

    # Source 1: Shared observations from ObsConfirm agent
    if city in _shared_obs:
        return _shared_obs[city]

    # Check cache first
    if city in _obs_cache and now - _obs_ts.get(city, 0) < 900:
        return _obs_cache[city]

    # Source 2: NWS API for US cities (most reliable)
    station = NWS_STATIONS.get(city)
    if station:
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
            log.debug('NWS fetch failed for %s: %s', city, e)

    # Source 3: METAR API for ALL cities (including international)
    icao = ALL_STATIONS.get(city)
    if icao and (city not in _obs_ts or now - _obs_ts.get(city, 0) > 600):
        try:
            r = requests.get(METAR_API, params={
                'ids': icao, 'format': 'json', 'hours': 1
            }, timeout=10)
            if r.ok:
                data = r.json()
                if data and isinstance(data, list):
                    for obs in data:
                        tc = obs.get('temp')
                        if tc is not None:
                            tf = round(float(tc) * 9/5 + 32, 1)
                            _obs_cache[city] = tf
                            _obs_ts[city] = now
                            log.info('METAR obs for %s (%s): %.1fF', city, icao, tf)
                            return tf
        except Exception as e:
            log.debug('METAR fetch failed for %s (%s): %s', city, icao, e)

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
                          entry_cost, current_value, mins_to_resolution, local_hour,
                          our_prob=0, signal='', ev=0, direction=''):
    """
    Smart exit engine: returns (exit, reason, action)
    action: 'SELL_ALL', 'SELL_HALF', 'HOLD'

    Uses 4-layer decision hierarchy:
      1. Fact-based (NWS obs) — overrides everything
      2. Forecast confidence — if our model still says strong edge, hold through spread noise
      3. Spread-aware pricing — don't cut on illiquid bid alone
      4. Time + price — last resort for deep losers near resolution
    """
    obs = get_obs_temp_f(city)

    # ── Layer 1: FACT-BASED (NWS observations) ──
    if obs is not None:
        achievable = max_achievable_today(obs, local_hour)
        if achievable < bin_lo:
            return True, f'OBS_KILL: max_achievable={achievable:.1f}F < bin_lo={bin_lo}F', 'SELL_ALL'
        if local_hour >= 16 and obs > bin_hi + 1:
            return True, f'OBS_PASSED: obs={obs}F > bin_hi={bin_hi}F and cooling', 'SELL_ALL'
        if obs >= bin_lo and obs <= bin_hi:
            return False, f'WIN_CONFIRMED: obs={obs}F inside bin {bin_lo}-{bin_hi}F - HOLD TO $1', 'HOLD'

    # ── Layer 2: FORECAST CONFIDENCE ──
    # If our model still has high conviction, the low market bid is
    # likely spread noise / illiquidity, not new information.
    # Don't cut a position our model says is still a good bet.
    is_no_harvest = 'NO_HARVEST' in signal or 'YES_HARVEST' in signal
    if our_prob > 0 and not is_no_harvest:
        # For directional YES/NO bets: hold if our model confidence is strong
        mkt_implied = current_price * 100 if current_price < 1 else current_price
        model_edge = our_prob - mkt_implied  # positive = we think it's underpriced

        if model_edge > 10:
            # Our model says >10pp edge over market — this is spread/mispricing, not a loss
            return False, f'MODEL_HOLD: our_prob={our_prob:.1f}% mkt={mkt_implied:.1f}% edge={model_edge:+.1f}pp — holding through spread', 'HOLD'

        if our_prob > 25 and model_edge > 0:
            # Moderate confidence and still positive edge — don't cut yet
            if mins_to_resolution > 120:
                return False, f'MODEL_HOLD: our_prob={our_prob:.1f}% > 25% edge={model_edge:+.1f}pp >2h left — patience', 'HOLD'

    # ── Layer 3: SPREAD-AWARE PRICING ──
    # In thin weather markets, the bid can be far below fair value.
    # If the bid dropped but there's no volume (just wide spread), hold.
    if entry_cost > 0:
        ratio = current_value / entry_cost

        # Check if the price drop is just spread, not a real move:
        # If entry was a low-probability bet (< $0.20), the bid naturally
        # sits far below the ask. A 50% "drop" from $0.10 to $0.05 is just
        # normal spread in a thin market, not a real loss signal.
        if entry_price < 0.20 and ratio > 0.2 and mins_to_resolution > 240:
            return False, f'SPREAD_HOLD: thin mkt entry=${entry_price:.2f} ratio={ratio:.0%} >4h left — spread noise', 'HOLD'

    # ── Layer 4: TIME + PRICE EXIT (last resort) ──
    if mins_to_resolution < 120 and entry_cost > 0:
        ratio = current_value / entry_cost
        if ratio < 0.15:
            # Very deep loss near resolution — even our model can't save this
            return True, f'TIME_EXIT: {mins_to_resolution:.0f}min left, value at {ratio*100:.0f}% of entry', 'SELL_ALL'
        if ratio < 0.3 and our_prob < 15:
            # Low model confidence AND low market price — real loser
            return True, f'TIME_EXIT: {mins_to_resolution:.0f}min left, value at {ratio*100:.0f}%, model={our_prob:.0f}%', 'SELL_ALL'
        if ratio < 0.3 and our_prob >= 15:
            # Low market price BUT model still has some confidence — log but hold
            log.info('HOLD_OVERRIDE: %s | ratio=%.0f%% but our_prob=%.1f%% — model says hold', city, ratio*100, our_prob)
            return False, f'HOLD_OVERRIDE: market says {ratio*100:.0f}% but model={our_prob:.1f}% — trusting forecast', 'HOLD'

    # ── Profit taking ──
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
            local_hour=local_hour,
            our_prob=pos.get('our_prob', 0),
            signal=pos.get('signal', ''),
            ev=pos.get('ev', 0),
            direction=pos.get('direction', ''),
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
