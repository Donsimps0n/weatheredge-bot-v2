"""
WeatherEdge Bot v2 - API Server
Lightweight Flask API that exposes bot data to the Vercel dashboard.
"""

import os
import time
import logging
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("api_server")

# ---------- Flask setup ----------
try:
    from flask import Flask, jsonify, request
    from flask_cors import CORS
except ImportError as exc:
    logger.error("Flask not installed.  pip install flask flask-cors")
    raise

app = Flask(__name__)
CORS(app, origins=[
    "https://iamweather.vercel.app",
    "http://localhost:3000",
    "http://localhost:5500",
])

# ---------- Import bot modules (graceful fallback) ----------
try:
    from gamma_client import get_markets as _gamma_get_markets
    HAS_GAMMA = True
except Exception:
    HAS_GAMMA = False
    logger.warning("gamma_client unavailable - /api/markets returns empty")

try:
    from market_classifier import classify_market
    HAS_CLASSIFIER = True
except Exception:
    HAS_CLASSIFIER = False

    def classify_market(question):
        """Fallback keyword classifier."""
        q = question.lower()
        kw = [
            "temperature", "weather", "rain", "snow", "wind",
            "heat", "cold", "celsius", "fahrenheit", "degrees",
            "forecast", "precipitation", "humidity", "storm",
        ]
        return any(k in q for k in kw)

# ---------- In-memory state ----------
_state = {
    "bot_running": False,
    "paper_mode": os.environ.get("PAPER_MODE", "true").lower() == "true",
    "last_scan": None,
    "markets_cache": [],
    "weather_markets": [],
    "scan_count": 0,
    "start_time": datetime.now(timezone.utc).isoformat(),
    "activity_log": [],
}


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    entry = f"{ts}  {msg}"
    _state["activity_log"].append(entry)
    if len(_state["activity_log"]) > 200:
        _state["activity_log"] = _state["activity_log"][-200:]
    logger.info(msg)


# ---------- Routes ----------

@app.route("/")
def root():
    return jsonify({"service": "weatheredge-api", "version": "2.0", "status": "ok"})


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "uptime_s": int(time.time() - _boot)})


@app.route("/api/status")
def status():
    return jsonify({
        "bot_running": _state["bot_running"],
        "paper_mode": _state["paper_mode"],
        "last_scan": _state["last_scan"],
        "scan_count": _state["scan_count"],
        "weather_markets": len(_state["weather_markets"]),
        "total_markets": len(_state["markets_cache"]),
        "start_time": _state["start_time"],
    })


@app.route("/api/markets")
def markets():
    """Return cached weather markets from the last scan."""
    return jsonify({
        "weather_markets": _state["weather_markets"][:100],
        "count": len(_state["weather_markets"]),
        "total_gamma": len(_state["markets_cache"]),
        "last_scan": _state["last_scan"],
    })



@app.route("/api/debug/gamma")
def debug_gamma():
    """Debug: raw Gamma API test."""
    import requests as req
    results = {}
    headers = {"User-Agent": "weatheredge-bot/2.0"}
    
    # Test 1: events endpoint with tag_slug
    try:
        r = req.get("https://gamma-api.polymarket.com/events",
                     params={"active": "true", "closed": "false", "limit": 5, "tag_slug": "weather"},
                     timeout=15, headers=headers)
        d = r.json()
        results["events_tag_slug"] = {"status": r.status_code, "count": len(d) if isinstance(d, list) else "not_list"}
        if isinstance(d, list) and len(d) > 0:
            results["events_tag_slug"]["first"] = d[0].get("title", "no title")[:60]
            results["events_tag_slug"]["has_markets"] = "markets" in d[0]
            if "markets" in d[0]:
                results["events_tag_slug"]["sub_market_count"] = len(d[0]["markets"])
                if d[0]["markets"]:
                    results["events_tag_slug"]["sub_q"] = d[0]["markets"][0].get("question", "")[:60]
    except Exception as e:
        results["events_tag_slug"] = {"error": str(e)}
    
    # Test 2: events with slug containing "weather" 
    try:
        r = req.get("https://gamma-api.polymarket.com/events",
                     params={"active": "true", "closed": "false", "limit": 5, "slug_contains": "weather"},
                     timeout=15, headers=headers)
        d = r.json()
        results["events_slug_contains"] = {"status": r.status_code, "count": len(d) if isinstance(d, list) else "not_list"}
        if isinstance(d, list) and len(d) > 0:
            results["events_slug_contains"]["first"] = d[0].get("title", "no title")[:60]
    except Exception as e:
        results["events_slug_contains"] = {"error": str(e)}
    
    # Test 3: events with tag (not tag_slug)
    try:
        r = req.get("https://gamma-api.polymarket.com/events",
                     params={"active": "true", "closed": "false", "limit": 5, "tag": "Weather"},
                     timeout=15, headers=headers)
        d = r.json()
        results["events_tag_Weather"] = {"status": r.status_code, "count": len(d) if isinstance(d, list) else "not_list"}
        if isinstance(d, list) and len(d) > 0:
            results["events_tag_Weather"]["first"] = d[0].get("title", "no title")[:60]
    except Exception as e:
        results["events_tag_Weather"] = {"error": str(e)}
    
    # Test 4: search for temperature in markets
    try:
        r = req.get("https://gamma-api.polymarket.com/markets",
                     params={"active": "true", "closed": "false", "limit": 10, "order": "liquidity", "ascending": "false"},
                     timeout=15, headers=headers)
        d = r.json()
        if isinstance(d, list):
            temp = [m.get("question","")[:60] for m in d if "temperature" in m.get("question","").lower()]
            results["top_liquidity"] = {"total": len(d), "temp_count": len(temp), "temp_qs": temp[:3]}
            results["top_liquidity"]["sample_qs"] = [m.get("question","")[:60] for m in d[:3]]
    except Exception as e:
        results["top_liquidity"] = {"error": str(e)}
    
    # Test 5: CLOB API for weather
    try:
        r = req.get("https://clob.polymarket.com/markets",
                     params={"limit": 5},
                     timeout=15, headers=headers)
        d = r.json()
        results["clob_api"] = {"status": r.status_code, "type": str(type(d).__name__), "keys": list(d.keys())[:10] if isinstance(d, dict) else "is_list"}
    except Exception as e:
        results["clob_api"] = {"error": str(e)}
    
    # Test 6: Gamma events with category
    try:
        r = req.get("https://gamma-api.polymarket.com/events",
                     params={"active": "true", "closed": "false", "limit": 200},
                     timeout=30, headers=headers)
        d = r.json()
        if isinstance(d, list):
            weather_events = []
            for ev in d:
                title = ev.get("title","").lower()
                tags = [t.get("slug","") if isinstance(t, dict) else str(t) for t in ev.get("tags", [])]
                if "weather" in title or "temperature" in title or "weather" in str(tags).lower():
                    weather_events.append({"title": ev.get("title","")[:60], "tags": tags[:5], "n_markets": len(ev.get("markets", []))})
            results["all_events_weather_scan"] = {"total_events": len(d), "weather_found": len(weather_events), "matches": weather_events[:5]}
    except Exception as e:
        results["all_events_weather_scan"] = {"error": str(e)}
    
    return jsonify(results)

@app.route("/api/scan", methods=["GET", "POST"])
def scan():
    """Fetch markets from Gamma, classify weather ones, cache results."""
    if not HAS_GAMMA:
        return jsonify({"error": "gamma_client not available"}), 503
    try:
        _log("Scan triggered via API")
        raw = _gamma_get_markets()
        _state["markets_cache"] = raw
        # get_markets() already returns only temperature markets
        # so no need to re-filter with classify_market()
        weather = []
        for m in raw:
            # Extract yes/no prices from tokens array
            _tokens = m.get("tokens", [])
            _yes_p = 0.0
            _no_p = 0.0
            for _t in _tokens:
                _out = str(_t.get("outcome", "")).lower()
                _pr = float(_t.get("price", 0) or 0)
                if _out == "yes":
                    _yes_p = _pr
                elif _out == "no":
                    _no_p = _pr
            weather.append({
                "slug": m.get("slug", ""),
                "question": m.get("question", m.get("category", "")),
                "city": m.get("city", ""),
                "station": m.get("station", ""),
                "category": m.get("category", ""),
                "outcomes": m.get("outcomes", []),
                "active": m.get("active", True),
                "end_date": m.get("end_date_iso", m.get("resolution_time", "")),
                "prices": m.get("prices", {}),
                "tokens": _tokens,
                "confidence": m.get("confidence", 0),
                # Frontend-compatible edge fields (from tokens array)
                "yes_price": _yes_p,
                "no_price": _no_p,
                "best_side": "YES" if _yes_p < _no_p else "NO",
                "best_edge": abs(_yes_p - _no_p) if (_yes_p + _no_p) > 0 else 0,
                "theoretical_full_ev": 0,
                "regime": m.get("category", ""),
            })
        _state["weather_markets"] = weather
        _state["last_scan"] = datetime.now(timezone.utc).isoformat()
        _state["scan_count"] += 1
        _log(f"Scan complete: {len(raw)} total, {len(weather)} weather markets")
        
        # Diversify: round-robin across cities for better coverage
        _by_city = {}
        for _w in weather:
            _c = _w.get("city", "?")
            _by_city.setdefault(_c, []).append(_w)
        _diverse = []
        _idx = 0
        _cities = sorted(_by_city.keys())
        while len(_diverse) < min(50, len(weather)):
            _added = False
            for _c in _cities:
                if _idx < len(_by_city[_c]):
                    _diverse.append(_by_city[_c][_idx])
                    _added = True
                if len(_diverse) >= 50:
                    break
            _idx += 1
            if not _added:
                break

        return jsonify({
            "weather_markets": len(weather),
            "total_markets": len(raw),
            "markets": _diverse,
            "scan_time": _state["last_scan"],
            "count": len(weather),
            "edges": _diverse,
            "cache_size": len(raw),
        })
    except Exception as exc:
        _log(f"Scan error: {exc}")
        logger.exception("Scan error")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/bot/toggle", methods=["POST"])
def toggle_bot():
    _state["bot_running"] = not _state["bot_running"]
    action = "started" if _state["bot_running"] else "stopped"
    mode = "paper" if _state["paper_mode"] else "LIVE"
    _log(f"Bot {action} ({mode} mode)")
    return jsonify({
        "bot_running": _state["bot_running"],
        "action": action,
        "paper_mode": _state["paper_mode"],
    })


@app.route("/api/log")
def activity_log():
    return jsonify({"log": _state["activity_log"][-50:]})



# ---------- Config (proxy key + mode) ----------
RAILWAY_API_TOKEN = os.environ.get("RAILWAY_API_TOKEN", "")
RAILWAY_SERVICE_ID = os.environ.get("RAILWAY_SERVICE_ID", "")
RAILWAY_ENV_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")


@app.route("/api/config", methods=["POST"])
def save_config():
    """Accept proxy_key and trading_mode. Store as Railway env vars."""
    data = request.get_json(force=True)
    proxy_key = (data.get("proxy_key") or "").strip()
    trading_mode = data.get("trading_mode", "paper").strip().lower()

    if not proxy_key or not proxy_key.startswith("pk-"):
        return jsonify({"ok": False, "error": "Invalid proxy key ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ must start with pk-"}), 400
    if trading_mode not in ("paper", "live"):
        return jsonify({"ok": False, "error": "trading_mode must be paper or live"}), 400

    # Update in-memory state immediately
    _state["paper_mode"] = trading_mode == "paper"
    os.environ["POLYMARKET_PROXY_KEY"] = proxy_key
    os.environ["PAPER_MODE"] = str(_state["paper_mode"]).lower()
    _log(f"Config updated: mode={trading_mode}, proxy key set (len={len(proxy_key)})")

    # Persist to Railway env vars via Railway GraphQL API
    if RAILWAY_API_TOKEN and RAILWAY_SERVICE_ID and RAILWAY_ENV_ID:
        try:
            _set_railway_env_vars({
                "POLYMARKET_PROXY_KEY": proxy_key,
                "PAPER_MODE": str(_state["paper_mode"]).lower(),
            })
            logger.info("Proxy key + mode persisted to Railway env vars")
        except Exception as exc:
            logger.error("Failed to persist to Railway: %s", exc)
            return jsonify({"ok": True, "warning": "Saved in memory but Railway persist failed"})
    else:
        logger.warning("RAILWAY_API_TOKEN / SERVICE_ID / ENV_ID not set ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ saved in memory only")

    return jsonify({"ok": True})


def _set_railway_env_vars(variables: dict):
    """Upsert environment variables on Railway via the GraphQL API."""
    import urllib.request
    import json as _json
    mutation = """
    mutation($input: VariableCollectionUpsertInput!) {
      variableCollectionUpsert(input: $input)
    }
    """
    payload = _json.dumps({
        "query": mutation,
        "variables": {
            "input": {
                "serviceId": RAILWAY_SERVICE_ID,
                "environmentId": RAILWAY_ENV_ID,
                "variables": variables,
            }
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://backboard.railway.app/graphql/v2",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {RAILWAY_API_TOKEN}",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = _json.loads(resp.read())
        if "errors" in body:
            raise RuntimeError(f"Railway API error: {body['errors']}")



# ---------- Live Traders from Polymarket Analytics ----------
_traders_cache = {"data": None, "ts": 0}

@app.route("/api/traders")
def get_traders():
    """Proxy top weather traders from Polymarket Analytics, cached 5 min."""
    import urllib.request
    import json as _json
    now = time.time()
    if _traders_cache["data"] and now - _traders_cache["ts"] < 300:
        return jsonify(_traders_cache["data"])
    try:
        url = (
            "https://polymarketanalytics.com/api/traders-tag-performance"
            "?tag=Weather&sortDirection=ASC&limit=20&offset=0&sortColumn=rank"
            "&minPnL=0&maxPnL=500000"
            "&minActivePositions=0&maxActivePositions=15000"
            "&minWinAmount=0&maxWinAmount=500000"
            "&minLossAmount=-300000&maxLossAmount=0"
            "&minWinRate=0&maxWinRate=100"
            "&minCurrentValue=0&maxCurrentValue=200000"
            "&minTotalPositions=1&maxTotalPositions=30000"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "WeatherEdge/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = _json.loads(resp.read())
        rows = body.get("data", body) if isinstance(body, dict) else body
        traders = []
        for r in rows[:20]:
            traders.append({
                "rank": r.get("rank", 0),
                "name": r.get("trader_name") or r.get("trader", "")[:10],
                "address": r.get("trader", ""),
                "pnl": round(r.get("overall_gain", 0), 2),
                "wins": round(r.get("win_amount", 0), 2),
                "losses": round(abs(r.get("loss_amount", 0)), 2),
                "winRate": round(r.get("win_rate", 0) * 100, 1),
                "positions": r.get("total_positions", 0),
                "active": r.get("active_positions", 0),
            })
        result = {"ok": True, "traders": traders, "ts": int(now)}
        _traders_cache["data"] = result
        _traders_cache["ts"] = now
        return jsonify(result)
    except Exception as exc:
        logger.error("Failed to fetch traders: %s", exc)
        if _traders_cache["data"]:
            return jsonify(_traders_cache["data"])
        return jsonify({"ok": False, "error": str(exc)}), 502


# ---------- Live Weather from Open-Meteo ----------
_weather_cache = {"data": None, "ts": 0}

WEATHER_CITIES = [
    {"name":"London","country":"UK","lat":51.51,"lon":-0.13},
    {"name":"Paris","country":"FR","lat":48.86,"lon":2.35},
    {"name":"Tokyo","country":"JP","lat":35.68,"lon":139.69},
    {"name":"New York","country":"US","lat":40.71,"lon":-74.01},
    {"name":"Seoul","country":"KR","lat":37.57,"lon":126.98},
    {"name":"Sydney","country":"AU","lat":-33.87,"lon":151.21},
    {"name":"Mumbai","country":"IN","lat":19.08,"lon":72.88},
    {"name":"Dubai","country":"AE","lat":25.20,"lon":55.27},
    {"name":"Berlin","country":"DE","lat":52.52,"lon":13.41},
    {"name":"Moscow","country":"RU","lat":55.76,"lon":37.62},
    {"name":"Toronto","country":"CA","lat":43.65,"lon":-79.38},
    {"name":"Chicago","country":"US","lat":41.88,"lon":-87.63},
    {"name":"Miami","country":"US","lat":25.76,"lon":-80.19},
    {"name":"Dallas","country":"US","lat":32.78,"lon":-96.80},
    {"name":"Atlanta","country":"US","lat":33.75,"lon":-84.39},
    {"name":"Seattle","country":"US","lat":47.61,"lon":-122.33},
    {"name":"Mexico City","country":"MX","lat":19.43,"lon":-99.13},
    {"name":"Rome","country":"IT","lat":41.90,"lon":12.50},
    {"name":"Madrid","country":"ES","lat":40.42,"lon":-3.70},
    {"name":"Cairo","country":"EG","lat":30.04,"lon":31.24},
    {"name":"Lagos","country":"NG","lat":6.52,"lon":3.38},
    {"name":"Nairobi","country":"KE","lat":-1.29,"lon":36.82},
    {"name":"Johannesburg","country":"ZA","lat":-26.20,"lon":28.05},
    {"name":"Bangkok","country":"TH","lat":13.76,"lon":100.50},
    {"name":"Singapore","country":"SG","lat":1.35,"lon":103.82},
    {"name":"Manila","country":"PH","lat":14.60,"lon":120.98},
    {"name":"Jakarta","country":"ID","lat":-6.21,"lon":106.85},
    {"name":"Hong Kong","country":"HK","lat":22.32,"lon":114.17},
    {"name":"Taipei","country":"TW","lat":25.03,"lon":121.57},
    {"name":"Denver","country":"US","lat":39.74,"lon":-104.99},
    {"name":"Phoenix","country":"US","lat":33.45,"lon":-112.07},
    {"name":"San Francisco","country":"US","lat":37.77,"lon":-122.42},
    {"name":"Los Angeles","country":"US","lat":34.05,"lon":-118.24},
    {"name":"Lima","country":"PE","lat":-12.05,"lon":-77.04},
    {"name":"Buenos Aires","country":"AR","lat":-34.60,"lon":-58.38},
    {"name":"Oslo","country":"NO","lat":59.91,"lon":10.75},
]

@app.route("/api/weather")
def get_weather():
    """Current weather for tracked cities via Open-Meteo (free, no key)."""
    import urllib.request
    import json as _json
    now = time.time()
    if _weather_cache["data"] and now - _weather_cache["ts"] < 300:
        return jsonify(_weather_cache["data"])
    try:
        lats = ",".join(str(c["lat"]) for c in WEATHER_CITIES)
        lons = ",".join(str(c["lon"]) for c in WEATHER_CITIES)
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lats}&longitude={lons}"
            f"&current=temperature_2m,relative_humidity_2m,precipitation,cloud_cover,weather_code&daily=temperature_2m_max,temperature_2m_min&forecast_days=3&timezone=auto"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "WeatherEdge/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = _json.loads(resp.read())
        # Open-Meteo returns a list when multiple coords
        results = body if isinstance(body, list) else [body]
        cities = []
        for i, city in enumerate(WEATHER_CITIES):
            if i >= len(results):
                break
            cur = results[i].get("current", {})
            daily = results[i].get("daily", {})
            tmax_list = daily.get("temperature_2m_max", [])
            tmin_list = daily.get("temperature_2m_min", [])
            # Index 0 = today, 1 = tomorrow
            temp_max_today = tmax_list[0] if tmax_list else None
            temp_min_today = tmin_list[0] if tmin_list else None
            temp_max_tomorrow = tmax_list[1] if len(tmax_list) > 1 else temp_max_today
            cities.append({
                "name": city["name"],
                "country": city["country"],
                "lat": city["lat"],
                "lon": city["lon"],
                "temp": cur.get("temperature_2m", 0),
                "temp_max": temp_max_today,
                "temp_min": temp_min_today,
                "temp_max_tomorrow": temp_max_tomorrow,
                "humidity": cur.get("relative_humidity_2m", 0),
                "precip": cur.get("precipitation", 0),
                "cloud": cur.get("cloud_cover", 0),
                "code": cur.get("weather_code", 0),
            })
        result = {"ok": True, "cities": cities, "ts": int(now)}
        _weather_cache["data"] = result
        _weather_cache["ts"] = now
        return jsonify(result)
    except Exception as exc:
        logger.error("Failed to fetch weather: %s", exc)
        if _weather_cache["data"]:
            return jsonify(_weather_cache["data"])
        return jsonify({"ok": False, "error": str(exc)}), 502




# ---------- Signals scanner ----------
import math as _math
import re as _re
import random as _random

_signals_cache = {"data": None, "ts": 0}

_CITY_COORDS = {
    "new york": (40.71, -74.01), "los angeles": (34.05, -118.24),
    "chicago": (41.88, -87.63), "houston": (29.76, -95.37),
    "phoenix": (33.45, -112.07), "philadelphia": (39.95, -75.17),
    "san diego": (32.72, -117.16), "dallas": (32.78, -96.80),
    "miami": (25.76, -80.19), "atlanta": (33.75, -84.39),
    "boston": (42.36, -71.06), "seattle": (47.61, -122.33),
    "denver": (39.74, -104.99), "nashville": (36.16, -86.78),
    "detroit": (42.33, -83.05), "portland": (45.52, -122.68),
    "las vegas": (36.17, -115.14), "memphis": (35.15, -90.05),
    "baltimore": (39.29, -76.61), "milwaukee": (43.04, -87.91),
    "london": (51.51, -0.13), "paris": (48.86, 2.35),
    "tokyo": (35.68, 139.69), "sydney": (-33.87, 151.21),
    "toronto": (43.65, -79.38), "berlin": (52.52, 13.41),
    "rome": (41.90, 12.50), "madrid": (40.42, -3.70),
    "dubai": (25.20, 55.27), "singapore": (1.35, 103.82),
    "mumbai": (19.08, 72.88), "moscow": (55.76, 37.62),
    "dc": (38.91, -77.04), "washington": (38.91, -77.04),
    "seoul": (37.57, 126.98), "beijing": (39.90, 116.41),
    "ankara": (39.93, 32.86), "buenos aires": (-34.60, -58.38),
    "sao paulo": (-23.55, -46.63), "sÃ£o paulo": (-23.55, -46.63),
    "mexico city": (19.43, -99.13), "cairo": (30.04, 31.24),
    "johannesburg": (-26.20, 28.05), "hong kong": (22.32, 114.17),
    "bangkok": (13.76, 100.50), "jakarta": (-6.21, 106.85),
    "istanbul": (41.01, 28.98), "lima": (-12.05, -77.04),
    "bogota": (4.71, -74.07), "santiago": (-33.45, -70.67),
    "lagos": (6.52, 3.38), "nairobi": (-1.29, 36.82),
    "riyadh": (24.71, 46.67), "tel aviv": (32.09, 34.78),
}


def _parse_market_q(q):
    ql = q.lower()
    city = lat = lon = None
    for name, coords in _CITY_COORDS.items():
        if name in ql:
            city = name.title()
            lat, lon = coords
            break
    temp_match = _re.search(r'(\d+\.?\d*)\s*[\xb0]?\s*(?:degrees?\s*)?(?:fahrenheit|f\b|celsius|c\b)', ql)
    if not temp_match:
        temp_match = _re.search(r'(\d+\.?\d*)\s*[\xb0]?\s*[fFcC]?\s*(?:or\s+)?(?:above|below|exceed|over|under|higher|lower)', ql)
        if not temp_match:
            temp_match = _re.search(r'(?:above|below|exceed|over|under|at least|reach)\s*(\d+\.?\d*)', ql)
    threshold = float(temp_match.group(1)) if temp_match else None
    is_f = 'fahrenheit' in ql or '\xb0f' in ql or (temp_match is not None and ql[temp_match.end()-1:temp_match.end()] == 'f')
    threshold_c = ((threshold - 32) * 5.0 / 9.0) if (is_f and threshold) else threshold
    is_above = any(w in ql for w in ['above', 'exceed', 'over', 'at least', 'reach', 'higher', 'warmer'])
    is_below = any(w in ql for w in ['below', 'under', 'lower', 'cooler', 'colder'])
    # Detect exact temperature questions: "be XÂ°C on" with no above/below
    is_exact = (not is_above and not is_below and threshold is not None
                and _re.search(r'be\s+\d+', ql) is not None)
    if is_exact:
        direction = "exact"
    elif is_above:
        direction = "above"
    else:
        direction = "below"
    metric = "temperature"
    if any(w in ql for w in ['rain', 'precipitation']): metric = 'precipitation'
    elif any(w in ql for w in ['snow']): metric = 'snow'
    elif any(w in ql for w in ['wind']): metric = 'wind'
    return {"city": city, "lat": lat, "lon": lon, "threshold": threshold,
            "threshold_c": threshold_c, "direction": direction, "metric": metric,
            "is_f": is_f, "is_exact": is_exact}

def _ncdf(x):
    return 0.5 * (1 + _math.erf(x / _math.sqrt(2)))


def _build_signals(weather_markets, weather_cities):
    city_wx = {}
    if weather_cities:
        for c in weather_cities:
            city_wx[c["name"].lower()] = c
    # Diversify: round-robin across cities for better coverage
    by_city = {}
    for mkt in weather_markets:
        q = mkt.get("question", "")
        p = _parse_market_q(q)
        if not p["city"] or p["threshold_c"] is None:
            continue
        by_city.setdefault(p["city"].lower(), []).append((mkt, p))
    diverse = []
    idx = 0
    cities = sorted(by_city.keys())
    while len(diverse) < min(60, sum(len(v) for v in by_city.values())):
        added = False
        for c in cities:
            if idx < len(by_city[c]):
                diverse.append(by_city[c][idx])
                added = True
            if len(diverse) >= 60:
                break
        if not added:
            break
        idx += 1
    signals = []
    for mkt, p in diverse:
        wx = city_wx.get(p["city"].lower())
        # Determine if market is for today or tomorrow
        _q_lower = mkt.get("question", "").lower()
        _today = datetime.now(timezone.utc)
        _is_tomorrow = False
        _dm = _re.search(r'(?:march|april|may|june|july|aug|sep|oct|nov|dec|jan|feb)\s+(\d+)', _q_lower)
        if _dm:
            _qday = int(_dm.group(1))
            _is_tomorrow = _qday != _today.day
        if wx:
            # Use forecasted daily HIGH, not current temp
            if _is_tomorrow and wx.get("temp_max_tomorrow") is not None:
                ftemp = wx["temp_max_tomorrow"]
                sigma = 2.0  # tomorrow forecast: ~2Â°C uncertainty
            elif wx.get("temp_max") is not None:
                ftemp = wx["temp_max"]
                sigma = 1.5  # today forecast: ~1.5Â°C uncertainty
            else:
                ftemp = wx.get("temp", 20)
                sigma = 3.0
        else:
            ftemp = 20.0
            sigma = 3.5
        if p["direction"] == "exact":
            # Exact temp: probability of landing within +/- 0.5C of threshold
            z_hi = (p["threshold_c"] + 0.5 - ftemp) / sigma
            z_lo = (p["threshold_c"] - 0.5 - ftemp) / sigma
            our_prob = round((_ncdf(z_hi) - _ncdf(z_lo)) * 100, 1)
        elif p["direction"] == "above":
            z = (ftemp - p["threshold_c"]) / sigma
            our_prob = round(_ncdf(z) * 100, 1)
        else:
            z = (p["threshold_c"] - ftemp) / sigma
            our_prob = round(_ncdf(z) * 100, 1)
        our_prob = max(0.1, min(99.9, our_prob))
        # Use real market price from tokens
        _tkns = mkt.get('tokens', [])
        _yes_mp = 0
        for _tk in _tkns:
            if str(_tk.get('outcome', '')).lower() == 'yes':
                _yes_mp = float(_tk.get('price', 0) or 0)
                break
        mp = round(_yes_mp * 100, 1) if _yes_mp > 0 else max(5, min(95, our_prob))
        ev = round(our_prob - mp, 1)
        is_f = p.get("is_f", False)
        df = round(ftemp * 9 / 5 + 32, 1) if p.get("is_f") else round(ftemp, 1)
        unit = "F" if is_f else "C"
        kelly = round(max(0, (our_prob / 100 * (100 / max(0.1, mp)) - 1) / ((100 / max(0.1, mp)) - 1) * 100), 1) if mp > 0 else 0
        conf = min(5, max(1, int(abs(ev) / 10) + 1))
        agreement = "STRONG" if abs(ev) > 20 else "MODERATE" if abs(ev) > 10 else "WEAK"
        sig_type = "SKIP" if abs(ev) < 5 else ("BUY YES" if ev > 0 else "BUY NO")
        signals.append({
            "question": mkt.get("question", ""),
            "city": p["city"], "metric": p["metric"],
            "signal": sig_type, "confidence": conf,
            "our_prob": our_prob, "market_price": round(mp, 1),
            "theo_ev": ev, "forecast": df, "unit": unit,
            "threshold": p["threshold"], "sigma": round(sigma, 2),
            "agreement": agreement, "models": ["GFS", "ECMWF", "UKMO", "MF"],
            "kelly": kelly, "active": mkt.get("active", True),
            "end_date": mkt.get("end_date", ""),
            "direction": p["direction"],
        })
    signals.sort(key=lambda s: abs(s["theo_ev"]), reverse=True)
    return signals

@app.route("/api/signals")
def get_signals():
    """Generate trading signals from weather markets + forecasts, cached 5 min."""
    now = time.time()
    if _signals_cache["data"] and now - _signals_cache["ts"] < 300:
        return jsonify(_signals_cache["data"])
    try:
        wx_cities = []
        if _weather_cache["data"] and _weather_cache["data"].get("cities"):
            wx_cities = _weather_cache["data"]["cities"]
        wm = _state.get("weather_markets", [])
        sigs = _build_signals(wm, wx_cities)
        result = {"ok": True, "signals": sigs, "count": len(sigs),
                  "ts": int(now), "scan_time": datetime.now(timezone.utc).isoformat()}
        _signals_cache["data"] = result
        _signals_cache["ts"] = now
        return jsonify(result)
    except Exception as exc:
        logger.error("Signals error: %s", exc)
        if _signals_cache["data"]:
            return jsonify(_signals_cache["data"])
        return jsonify({"ok": False, "error": str(exc), "signals": []}), 502




# ---------- Polymarket CLOB Trading ----------
_CLOB_HOST = "https://clob.polymarket.com"
_POLYGON_CHAIN_ID = 137
_clob_client = None
_clob_creds = None
_trade_log = []
_MAX_TRADE_LOG = 200

def _init_clob():
    """Initialize Polymarket CLOB client from env vars."""
    global _clob_client, _clob_creds
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
    if not pk:
        logger.warning("POLYMARKET_PRIVATE_KEY not set - trading disabled")
        return False
    try:
        from py_clob_client.client import ClobClient
        client = ClobClient(
            host=_CLOB_HOST,
            chain_id=_POLYGON_CHAIN_ID,
            key=pk,
            signature_type=2,  # GNOSIS_SAFE for fresh MetaMask wallet
            funder=funder if funder else None
        )
        creds = client.create_or_derive_api_creds()
        _clob_creds = creds
        client.set_api_creds(creds)
        _clob_client = client
        logger.info("CLOB client initialized OK - trading enabled")
        return True
    except Exception as exc:
        logger.error("CLOB init failed: %s", exc)
        return False


@app.route("/api/trading/status")
def trading_status():
    """Check if CLOB trading is enabled and return wallet info."""
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
    connected = _clob_client is not None
    return jsonify({
        "ok": True,
        "trading_enabled": connected,
        "has_private_key": bool(pk),
        "funder_address": funder if funder else None,
        "credentials_active": _clob_creds is not None,
        "recent_trades": len(_trade_log),
    })


@app.route("/api/trading/balance")
def trading_balance():
    """Get CLOB collateral balance."""
    if not _clob_client:
        return jsonify({"ok": False, "error": "Trading not connected"}), 503
    try:
        bal = _clob_client.get_balance_allowance()
        return jsonify({"ok": True, "balance": bal})
    except Exception as exc:
        logger.error("Balance check failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/trading/place", methods=["POST"])
def place_trade():
    """
    Place a trade on Polymarket CLOB.
    Body JSON: {token_id, side (BUY/SELL), price, size, tick_size, neg_risk}
    """
    if not _clob_client:
        return jsonify({"ok": False, "error": "Trading not connected"}), 503
    data = request.get_json(force=True)
    token_id = data.get("token_id", "")
    side = data.get("side", "BUY")
    price = float(data.get("price", 0))
    size = float(data.get("size", 0))
    tick_size = data.get("tick_size", "0.01")
    neg_risk = data.get("neg_risk", False)
    if not token_id or price <= 0 or size <= 0:
        return jsonify({"ok": False, "error": "Missing token_id, price, or size"}), 400
    if price < 0.01 or price > 0.99:
        return jsonify({"ok": False, "error": "Price must be between 0.01 and 0.99"}), 400
    if size > 500:
        return jsonify({"ok": False, "error": "Max size 500 per order (safety limit)"}), 400
    try:
        order_args = {
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side,
        }
        order_opts = {"tick_size": tick_size, "neg_risk": neg_risk}
        resp = _clob_client.create_and_post_order(order_args, order_opts)
        trade_entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "token_id": token_id, "side": side,
            "price": price, "size": size,
            "result": str(resp),
        }
        _trade_log.append(trade_entry)
        if len(_trade_log) > _MAX_TRADE_LOG:
            _trade_log.pop(0)
        logger.info("Trade placed: %s %s @ %.2f x %.1f", side, token_id[:16], price, size)
        return jsonify({"ok": True, "order": str(resp), "trade": trade_entry})
    except Exception as exc:
        logger.error("Trade failed: %s", exc)
        err_entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "token_id": token_id, "side": side,
            "price": price, "size": size,
            "error": str(exc),
        }
        _trade_log.append(err_entry)
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/trading/log")
def trade_history():
    """Return recent trade log."""
    return jsonify({"ok": True, "trades": list(reversed(_trade_log)), "count": len(_trade_log)})


@app.route("/api/trading/auto", methods=["POST"])
def auto_trade_signal():
    """
    Auto-trade: takes a signal object and places a trade if EV threshold met.
    Body JSON: {signal (from /api/signals), max_size, min_ev, min_kelly}
    """
    if not _clob_client:
        return jsonify({"ok": False, "error": "Trading not connected"}), 503
    data = request.get_json(force=True)
    sig = data.get("signal", {})
    max_size = float(data.get("max_size", 10))
    min_ev = float(data.get("min_ev", 5))
    min_kelly = float(data.get("min_kelly", 2))
    if not sig.get("slug"):
        return jsonify({"ok": False, "error": "No signal slug provided"}), 400
    ev = abs(sig.get("theo_ev", 0))
    kelly = sig.get("kelly", 0)
    if ev < min_ev:
        return jsonify({"ok": True, "action": "SKIP", "reason": f"EV {ev} below threshold {min_ev}"})
    if kelly < min_kelly:
        return jsonify({"ok": True, "action": "SKIP", "reason": f"Kelly {kelly} below threshold {min_kelly}"})
    sig_type = sig.get("signal", "SKIP")
    if sig_type == "SKIP":
        return jsonify({"ok": True, "action": "SKIP", "reason": "Signal is SKIP"})
    # Determine trade side and price
    our_prob = sig.get("our_prob", 50) / 100.0
    mkt_price = sig.get("market_price", 50) / 100.0
    if sig_type == "BUY YES":
        side = "BUY"
        price = round(min(mkt_price + 0.01, our_prob - 0.02), 2)
    else:  # BUY NO
        side = "SELL"
        price = round(max(mkt_price - 0.01, our_prob + 0.02), 2)
    # Kelly-based sizing, capped at max_size
    size = round(min(max_size, max_size * (kelly / 100.0) * 2), 1)
    size = max(1, size)  # minimum 1 unit
    price = max(0.01, min(0.99, price))
    logger.info("Auto-trade: %s %s @ %.2f x %.1f (EV=%.1f, Kelly=%.1f)",
               side, sig.get("slug", "?")[:30], price, size, ev, kelly)
    return jsonify({
        "ok": True, "action": "TRADE_READY",
        "side": side, "price": price, "size": size,
        "signal": sig_type, "ev": ev, "kelly": kelly,
        "note": "Call /api/trading/place with token_id to execute"
    })


# ---------- Boot ----------
_boot = time.time()
_init_clob()  # attempt CLOB connection on startup
PORT = int(os.environ.get("PORT", 8080))


def _start_scheduler_thread():
    """Start the scheduler in a background thread so API server stays responsive."""
    import threading
    def _run_scheduler():
        try:
            import subprocess
            subprocess.run(["python", "scheduler.py"])
        except Exception as exc:
            logger.error("Scheduler thread failed: %s", exc)
    t = threading.Thread(target=_run_scheduler, daemon=True)
    t.start()
    logger.info("Scheduler started in background thread")


if __name__ == "__main__":
    _log(f"WeatherEdge API v2 starting on port {PORT}")
    _start_scheduler_thread()
    app.run(host="0.0.0.0", port=PORT, debug=False)
