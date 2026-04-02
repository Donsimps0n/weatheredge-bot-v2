# WeatherEdge Bot V2 — Code Audit Part 3: Supporting Modules

## FILE 3: trade_resolver.py (414 lines) — COMPLETE

### 3A. Module Purpose + Constants

```python
"""
trade_resolver.py — Resolve open paper/live trades against Polymarket outcomes.

Fetches all resolved weather events from the Gamma API, builds a
token_id → outcome lookup, then matches unresolved trades in the ledger
and records win/loss + P&L.

P&L rules by signal type:
    BUY YES   : If token resolves YES → payout = size * $1. PnL = size - spend.
                If NO → PnL = -spend.
    NO_HARVEST: If token resolves NO → payout = size * $1. PnL = size - spend.
                If YES → PnL = -spend.
    EXIT_SELL_ALL: Already sold; mark resolved with pnl=0 (entry leg matters).
"""
import json, logging, re, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_PAGE_LIMIT = 100
_last_check_ts: float = 0.0
_CHECK_INTERVAL_S = 3600  # 1 hour
```

### 3B. Resolution Map Builder (lines 58-215)

Fetches ALL closed weather events (2,400+) with pagination. Builds 3 lookup structures.

```python
def _build_resolution_maps() -> Tuple[Dict, Dict, Dict]:
    """
    Three lookup structures:
    1. full_token_map:  { full_token_id (76 digits) → resolution_entry }
    2. prefix_map:      { first_12_digits → resolution_entry }
    3. question_map:    { normalized_question_text → resolution_entry }

    resolution_entry = {
        "yes_won": bool,
        "bin_label": str,
        "event_title": str,
        "token_side": "YES" | "NO",
    }
    """
    full_map, prefix_map, question_map = {}, {}, {}
    page = 0

    while True:
        offset = page * _PAGE_LIMIT
        events = _fetch_gamma_page(offset)
        if not events:
            break

        for event in events:
            title = event.get("title", "")
            if "highest temperature" not in title.lower():
                continue

            for mkt in event.get("markets", []):
                prices_raw = mkt.get("outcomePrices", "[]")
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                if not prices or len(prices) < 2:
                    continue

                yes_price = str(prices[0])
                if yes_price not in ("0", "1"):
                    continue  # not yet resolved

                yes_won = (yes_price == "1")
                bin_label = mkt.get("groupItemTitle", "?")
                question = mkt.get("question", "")

                clob_tokens = json.loads(mkt.get("clobTokenIds", "[]"))

                # Build entries for YES (index 0) and NO (index 1) tokens
                for i, side_label in enumerate(["YES", "NO"]):
                    if i >= len(clob_tokens) or not clob_tokens[i]:
                        continue
                    tok = str(clob_tokens[i])
                    entry = {
                        "yes_won": yes_won,
                        "bin_label": bin_label,
                        "event_title": title[:120],
                        "token_side": side_label,
                    }
                    full_map[tok] = entry
                    prefix_map[tok[:12]] = entry

                # Question map: normalized for fuzzy matching
                if question:
                    q_key = re.sub(r"[^a-z0-9 ]", "", question.lower()).strip()
                    question_map[q_key] = {
                        "yes_won": yes_won, "bin_label": bin_label,
                        "event_title": title[:120],
                    }

        if len(events) < _PAGE_LIMIT:
            break
        page += 1
        time.sleep(0.15)

    return full_map, prefix_map, question_map
```

### 3C. Cascading Lookup (lines 142-188)

```python
def _lookup_resolution(token_id, question, signal, full_map, prefix_map, question_map):
    """
    Cascading lookup:
    1. Exact full token_id match (76-digit tokens)
    2. Prefix match (first 12 digits — handles JS precision loss)
    3. Question text match (handles garbled token_ids)
    """
    # Strategy 1: Exact full match
    if token_id in full_map:
        entry = full_map[token_id]
        entry["match_method"] = "exact"
        return entry

    # Strategy 2: Prefix match
    clean_tok = token_id.rstrip(".")
    prefix = clean_tok[:12]
    if len(prefix) >= 10 and prefix in prefix_map:
        entry = prefix_map[prefix].copy()
        entry["match_method"] = "prefix"
        return entry

    # Strategy 3: Question text match
    if question and not question.startswith("EXIT"):
        q_key = re.sub(r"[^a-z0-9 ]", "", question.lower()).strip()
        if q_key in question_map:
            entry = question_map[q_key].copy()
            if signal == "BUY YES":
                entry["token_side"] = "YES"
            elif signal == "NO_HARVEST":
                entry["token_side"] = "NO"
            else:
                entry["token_side"] = "YES"
            entry["match_method"] = "question"
            return entry

    return None
```

### 3D. P&L Computation (lines 217-280)

```python
def _compute_pnl(signal, token_side, yes_won, price, size, spend):
    """Returns (won: bool, resolution_price: float, pnl: float)"""
    if signal == "EXIT_SELL_ALL":
        return True, price, 0.0

    if signal == "BUY YES":
        if yes_won:
            payout = size * 1.0
            return True, 1.0, round(payout - spend, 4)
        else:
            return False, 0.0, round(-spend, 4)

    if signal == "NO_HARVEST":
        if not yes_won:
            payout = size * 1.0
            return True, 1.0, round(payout - spend, 4)
        else:
            return False, 0.0, round(-spend, 4)

    return False, 0.0, round(-spend, 4)
```

### 3E. Main resolve_trades Function (lines 280-415)

```python
def resolve_trades(force=False) -> dict:
    """Check unresolved trades against Polymarket outcomes."""
    global _last_check_ts

    now = time.time()
    if not force and (now - _last_check_ts) < _CHECK_INTERVAL_S:
        return {"ran": False, "reason": "throttled"}

    _last_check_ts = now
    summary = {"ran": True, "resolved_count": 0, "wins": 0, "losses": 0,
               "total_pnl": 0.0, "errors": []}

    import trade_ledger
    unresolved = trade_ledger.get_unresolved_trades()
    if not unresolved:
        return summary

    full_map, prefix_map, question_map = _build_resolution_maps()

    match_methods = {"exact": 0, "prefix": 0, "question": 0, "miss": 0}

    for trade in unresolved:
        trade_id = trade["id"]
        token_id = str(trade.get("token_id", ""))
        signal = trade.get("signal", "")
        question = trade.get("question", "")
        price = float(trade.get("price", 0))
        size = float(trade.get("size", 0))
        spend = float(trade.get("spend", 0))

        # EXIT trades: auto-resolve with pnl=0 (BEFORE token_id gate)
        if signal == "EXIT_SELL_ALL" or signal.startswith("EXIT"):
            trade_ledger.mark_resolved(trade_id=trade_id, won=True,
                                       resolution_price=price, pnl=0.0)
            summary["resolved_count"] += 1
            summary["wins"] += 1
            continue

        if not token_id:
            continue

        resolution = _lookup_resolution(
            token_id, question, signal, full_map, prefix_map, question_map)
        if resolution is None:
            match_methods["miss"] += 1
            continue

        match_methods[resolution.get("match_method", "?")] += 1
        won, res_price, pnl = _compute_pnl(
            signal=signal, token_side=resolution.get("token_side", "YES"),
            yes_won=resolution["yes_won"],
            price=price, size=size, spend=spend)

        trade_ledger.mark_resolved(trade_id=trade_id, won=won,
                                    resolution_price=res_price, pnl=pnl)
        summary["resolved_count"] += 1
        if won: summary["wins"] += 1
        else: summary["losses"] += 1
        summary["total_pnl"] += pnl

    summary["match_methods"] = match_methods
    return summary
```

---

## FILE 4: trade_ledger.py (221 lines) — COMPLETE

SQLite persistence layer. Two tables: `trades` and `cycles`.

```python
"""Persistent trade ledger using SQLite."""
import sqlite3, json, logging, os
from datetime import datetime, timezone

# On Railway, use /data volume (persistent). Locally, use ledger.db.
_default_db = "/data/ledger.db" if os.path.isdir("/data") else "ledger.db"
DB_PATH = os.environ.get("LEDGER_DB", _default_db)

# Schema:
# trades: id, ts, question, city, signal, token_id, price, size, spend,
#         ev, ev_dollar, kelly, our_prob, mkt_price, sigma, mode,
#         clob_spread, clob_edge_at_fill, resolved, resolution_price,
#         pnl, won, resolved_at, meta (JSON blob)
#
# cycles: id, ts, total_signals, tradeable, trades_placed,
#         top_city, top_ev, recalibrated, meta

def record_trade(trade: dict):
    """Insert a trade. Overflow fields go into meta JSON blob."""
    conn.execute("""
        INSERT INTO trades (ts, question, city, signal, token_id, price, size,
            spend, ev, ev_dollar, kelly, our_prob, mkt_price, sigma, mode,
            clob_spread, clob_edge_at_fill, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (trade fields... meta = json.dumps(overflow_keys)))
    conn.commit()

def record_cycle(total_signals, tradeable, trades_placed, top_city, top_ev, recalibrated=0):
    """Log a trade cycle for analytics."""

def mark_resolved(trade_id, won, resolution_price, pnl):
    """Mark trade as resolved with outcome."""
    conn.execute("""
        UPDATE trades SET resolved='yes', won=?, resolution_price=?,
            pnl=?, resolved_at=? WHERE id=?
    """, (1 if won else 0, resolution_price, pnl, now_iso, trade_id))

def get_unresolved_trades() -> List[Dict]:
    """SELECT * FROM trades WHERE resolved IS NULL ORDER BY id DESC"""

def get_performance_summary() -> Dict:
    """Overall stats: total, resolved, wins, losses, win_rate, total_pnl,
    per-city breakdown, cycle stats."""
```

---

## FILE 5: gamma_client.py (469 lines) — Market Discovery

### 5A. Configuration + Dataclasses

```python
"""Polymarket Gamma API client for temperature market discovery.
Hits /events with tag_slug=weather, filters to temperature markets,
parses station ICAO + resolution time, matches against 58-city list."""

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
PAGE_LIMIT = 100
CACHE_TTL_SECONDS = 1800  # 30 min

TEMP_KEYWORDS = ["temperature", "high temp", "low temp", "degrees",
                 "fahrenheit", "celsius", "highest temperature", ...]

@dataclass
class RawMarket:
    market_id: str; slug: str; question: str; rules: str
    end_date_iso: str; tokens: List[Dict]; active: bool; closed: bool; volume: float

@dataclass
class DiscoveredMarket:
    slug: str; market_id: str; question: str; station: str; city: str
    country: str; category: str; confidence: int; timezone: str
    lat: float; lon: float; coastal: bool; resolution_time: datetime
    prices: Dict[str, float]; tokens: List[Dict]
```

### 5B. Market Fetch + Cache (lines 269-410)

```python
def fetch_open_temp_markets(tag="weather", max_pages=10) -> List[DiscoveredMarket]:
    """
    Hit Gamma Events API, return all open temperature markets that:
    - Match a temperature keyword
    - Have an ICAO station in our city list
    - Have a future resolution time
    """
    for page in range(max_pages):
        raw_list = _fetch_events_page(session, offset, tag)
        for item in raw_list:
            if not _is_temp_market(question):
                continue
            # Parse tokens from clobTokenIds / outcomes / outcomePrices
            raw = RawMarket(...)
            dm = _raw_to_discovered(raw)
            if dm is not None:
                discovered.append(dm)
    return discovered

def get_markets(tag="weather") -> List[Dict]:
    """Cached market discovery. Re-fetches if older than 30 min."""
    if age < CACHE_TTL_SECONDS and _cache_markets:
        return _cache_markets
    # ... refresh ...
```

---

## FILE 6: src/bias_agent.py (597 lines) — Station Bias Correction

### 6A. Four-Knob System

```python
"""Live Station Bias Agent — reads station_bias.db, computes per-station
corrections, enriches signals with bias-corrected probabilities."""

# Knob A: Temperature correction (°C) — shift forecast mean
MIN_OBS_FOR_CORRECTION = 5
CORRECTION_THRESHOLD_F = 0.5

# Knob B: Sigma floor — widen uncertainty for noisy stations
SIGMA_FLOOR_BASE_C = 1.0
SIGMA_FLOOR_NOISY_C = 2.0
NOISY_STD_THRESHOLD_F = 4.0

# Knob C: Min EV gate addon — require higher edge for unreliable stations
EV_ADDON_NOISY = 4.0    # +4pp for std > 4°F
EV_ADDON_LOW_N = 2.0    # +2pp for n < 15
EV_ADDON_OUTLIER = 3.0  # +3pp if outlier_rate > 15%

# Knob D: Size multiplier — scale position size by reliability
SIZE_MULT_EXCELLENT = 1.2  # n>=30, std<2.5°F, |bias|<1°F
SIZE_MULT_GOOD = 1.0       # n>=15, std<4°F
SIZE_MULT_MEDIOCRE = 0.7   # n>=5, std>=4°F or high outliers
SIZE_MULT_UNKNOWN = 0.5    # no data or n<5

# Combined-penalty caps (prevent triple-stacking from killing a station)
MAX_EV_ADDON = 5.0       # Cap at +5pp (gate: 5pp → max 10pp)
MIN_SIZE_MULT = 0.4
MAX_SIGMA_FLOOR_C = 2.5
```

### 6B. poll() Method — Knob Logic

```python
class StationBiasAgent:
    def __init__(self, db_path=None, config_cities=None):
        # Build city→ICAO mapping, initial load from DB

    def poll(self, force=False):
        """Read station_bias.db, compute all 4 knobs per station."""
        # For each station in DB:
        #   1. Read: n_obs, bias_f, std_f, outlier_pct, drift metrics
        #   2. Knob A: if |bias_f| > 0.5°F and n >= 5 → correction_c = bias_f * F_TO_C
        #   3. Knob B: if std_f > 4°F → sigma_floor = 2.0°C, else 1.0°C
        #   4. Knob C: sum EV addons (capped at 5pp)
        #   5. Knob D: classify reliability → size multiplier
        #   6. Apply combined-penalty caps

    def get_correction_c(self, city: str) -> float:
        """Return per-city temperature correction in °C."""

    def get_station_adjustments(self, city: str) -> dict:
        """Return all 4 knobs: {correction_c, sigma_floor_c, ev_addon, size_mult,
        ev_addon_reasons, confidence, ...}"""
```

---

## REVIEW QUESTIONS FOR PART 3

1. **Resolver prefix collision risk**: The prefix map uses first 12 digits of token_id. With ~5,000+ tokens in the full map, what's the collision probability? If two tokens share 12-digit prefix, the second overwrites the first in `prefix_map[prefix] = entry`. This could silently resolve a trade against the WRONG market.

2. **Resolver: question_map overwrite**: Similarly, if two markets have the same normalized question text (e.g., identical bin across different events), the later one wins. Is there date filtering to prevent cross-day collisions?

3. **EXIT auto-resolution before token_id check**: EXIT trades are auto-resolved with `won=True, pnl=0`. This happens before the `if not token_id: continue` gate. This means ALL 61 exit trades are marked as wins with $0 PnL. Confirm this is intentionally inflating the win count — should these be excluded from win rate calculations?

4. **_CHECK_INTERVAL_S = 3600**: The resolver only runs once per hour. Markets close daily around midnight UTC. If a market closes at 00:01 and the resolver ran at 23:59, the trade waits almost 2 hours to resolve. Is this frequent enough?

5. **Bias agent + hardcoded _CITY_BIAS_C**: Both exist. The code checks `HAS_BIAS_AGENT` first, falls back to `_CITY_BIAS_C`. But in the ensemble path, it uses `_bias_agent.get_correction_c()` directly. Are the two paths consistent? Could a city get corrected by the agent AND the hardcoded dict?

6. **Knob stacking**: A station could get: sigma_floor=2.0°C (Knob B) + ev_addon=5pp (Knob C, capped) + size_mult=0.4 (Knob D, floored). Combined effect: wider uncertainty, higher bar, smaller position. Is this too aggressive? The penalty caps exist but the cumulative effect might still be prohibitive.

7. **trade_ledger spend calculation**: `spend = trade.get("price", 0) * trade.get("size", 0)`. But `price` and `size` in the trade dict are already the final values after depth cap and budget clip. Is `spend` being computed correctly, or should the trade dict include `spend` explicitly?

8. **NO_HARVEST 100% win rate**: 190 agent trades, all wins. These buy NO on extreme bins (e.g., "Will Miami be 40°F?"). The strategy is sound — extreme bins almost never resolve YES. But 100% on 190 trades suggests no filtering is needed. Should these be verified against actual resolved Gamma events to confirm none were incorrectly marked?
