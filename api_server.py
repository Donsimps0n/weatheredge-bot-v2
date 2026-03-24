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
    from gamma_client import GammaClient
    _gamma = GammaClient()
    HAS_GAMMA = True
except Exception:
    HAS_GAMMA = False
    _gamma = None
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


@app.route("/api/scan", methods=["POST"])
def scan():
    """Fetch markets from Gamma, classify weather ones, cache results."""
    if not HAS_GAMMA:
        return jsonify({"error": "gamma_client not available"}), 503
    try:
        _log("Scan triggered via API")
        raw = _gamma.get_markets()
        _state["markets_cache"] = raw
        weather = []
        for m in raw:
            q = m.get("question", "")
            if classify_market(q):
                weather.append({
                    "slug": m.get("slug", ""),
                    "question": q,
                    "outcomes": m.get("outcomes", []),
                    "active": m.get("active", False),
                    "end_date": m.get("end_date_iso", ""),
                })
        _state["weather_markets"] = weather
        _state["last_scan"] = datetime.now(timezone.utc).isoformat()
        _state["scan_count"] += 1
        _log(f"Scan complete: {len(raw)} total, {len(weather)} weather markets")
        return jsonify({
            "weather_markets": len(weather),
            "total_markets": len(raw),
            "markets": weather[:50],
            "scan_time": _state["last_scan"],
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
        return jsonify({"ok": False, "error": "Invalid proxy key — must start with pk-"}), 400
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
        logger.warning("RAILWAY_API_TOKEN / SERVICE_ID / ENV_ID not set — saved in memory only")

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
            f"&current=temperature_2m,relative_humidity_2m,precipitation,cloud_cover,weather_code"
            f"&timezone=auto"
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
            cities.append({
                "name": city["name"],
                "country": city["country"],
                "lat": city["lat"],
                "lon": city["lon"],
                "temp": cur.get("temperature_2m", 0),
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


# ---------- Boot ----------
_boot = time.time()
PORT = int(os.environ.get("PORT", 8080))

if __name__ == "__main__":
    _log(f"WeatherEdge API v2 starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
