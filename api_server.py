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


# ---------- Boot ----------
_boot = time.time()
PORT = int(os.environ.get("PORT", 8080))

if __name__ == "__main__":
    _log(f"WeatherEdge API v2 starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
