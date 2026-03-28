"""
Persistent trade ledger using SQLite.

Stores every paper/live trade so history survives restarts and deploys.
Provides query methods for accuracy analysis and performance tracking.
"""
import sqlite3
import json
import logging
import os
from datetime import datetime, timezone
from typing import List, Dict, Optional

log = logging.getLogger(__name__)

# On Railway, use /data volume (persistent). Locally, use ledger.db in project dir.
# Set LEDGER_DB env var to override.
_default_db = "/data/ledger.db" if os.path.isdir("/data") else "ledger.db"
DB_PATH = os.environ.get("LEDGER_DB", _default_db)

_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        log.info("Trade ledger DB: %s", DB_PATH)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_tables(_conn)
    return _conn


def _init_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            question TEXT,
            city TEXT,
            signal TEXT,
            token_id TEXT,
            price REAL,
            size REAL,
            spend REAL,
            ev REAL,
            ev_dollar REAL,
            kelly REAL,
            our_prob REAL,
            mkt_price REAL,
            sigma REAL,
            mode TEXT DEFAULT 'PAPER',
            clob_spread REAL,
            clob_edge_at_fill REAL,
            resolved TEXT,
            resolution_price REAL,
            pnl REAL,
            won INTEGER,
            resolved_at TEXT,
            meta TEXT
        );

        CREATE TABLE IF NOT EXISTS cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            total_signals INTEGER,
            tradeable INTEGER,
            trades_placed INTEGER,
            top_city TEXT,
            top_ev REAL,
            recalibrated INTEGER DEFAULT 0,
            meta TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_trades_city ON trades(city);
        CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
        CREATE INDEX IF NOT EXISTS idx_trades_resolved ON trades(resolved);
    """)
    conn.commit()


def record_trade(trade: dict):
    """Insert a trade record."""
    conn = _get_conn()
    conn.execute("""
        INSERT INTO trades (ts, question, city, signal, token_id, price, size,
            spend, ev, ev_dollar, kelly, our_prob, mkt_price, sigma, mode,
            clob_spread, clob_edge_at_fill, meta)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade.get("ts", datetime.now(timezone.utc).isoformat()),
        trade.get("question", ""),
        trade.get("city", ""),
        trade.get("signal", ""),
        trade.get("token_id", ""),
        trade.get("price", 0),
        trade.get("size", 0),
        trade.get("price", 0) * trade.get("size", 0),
        trade.get("ev", 0),
        trade.get("ev_dollar", 0),
        trade.get("kelly", 0),
        trade.get("our_prob", 0),
        trade.get("mkt_price", 0),
        trade.get("sigma"),
        trade.get("mode", "PAPER"),
        trade.get("clob_spread"),
        trade.get("clob_edge_at_fill"),
        json.dumps({k: v for k, v in trade.items()
                    if k not in ("ts","question","city","signal","token_id",
                                 "price","size","ev","ev_dollar","kelly",
                                 "our_prob","mkt_price","sigma","mode",
                                 "clob_spread","clob_edge_at_fill")}),
    ))
    conn.commit()


def record_cycle(total_signals: int, tradeable: int, trades_placed: int,
                 top_city: str = "", top_ev: float = 0, recalibrated: int = 0):
    """Log a trade cycle for analytics."""
    conn = _get_conn()
    conn.execute("""
        INSERT INTO cycles (ts, total_signals, tradeable, trades_placed,
            top_city, top_ev, recalibrated)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        total_signals, tradeable, trades_placed,
        top_city, top_ev, recalibrated,
    ))
    conn.commit()


def mark_resolved(trade_id: int, won: bool, resolution_price: float, pnl: float):
    """Mark a trade as resolved with outcome."""
    conn = _get_conn()
    conn.execute("""
        UPDATE trades SET resolved = 'yes', won = ?, resolution_price = ?,
            pnl = ?, resolved_at = ? WHERE id = ?
    """, (1 if won else 0, resolution_price, pnl,
          datetime.now(timezone.utc).isoformat(), trade_id))
    conn.commit()


def get_all_trades(limit: int = 500) -> List[Dict]:
    """Get recent trades."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_trades_by_city(city: str) -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM trades WHERE city = ? ORDER BY id DESC", (city,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_unresolved_trades() -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM trades WHERE resolved IS NULL ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_performance_summary() -> Dict:
    """Overall performance stats."""
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    resolved = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE resolved = 'yes'").fetchone()[0]
    wins = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE won = 1").fetchone()[0]
    losses = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE won = 0 AND resolved = 'yes'"
    ).fetchone()[0]
    total_pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE resolved = 'yes'"
    ).fetchone()[0]
    avg_ev = conn.execute(
        "SELECT COALESCE(AVG(ev), 0) FROM trades").fetchone()[0]
    avg_ev_dollar = conn.execute(
        "SELECT COALESCE(AVG(ev_dollar), 0) FROM trades").fetchone()[0]
    total_spent = conn.execute(
        "SELECT COALESCE(SUM(spend), 0) FROM trades").fetchone()[0]

    # Per-city breakdown
    city_rows = conn.execute("""
        SELECT city, COUNT(*) as cnt,
               SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN won=0 AND resolved='yes' THEN 1 ELSE 0 END) as losses,
               COALESCE(SUM(pnl), 0) as pnl,
               COALESCE(AVG(ev), 0) as avg_ev
        FROM trades GROUP BY city ORDER BY cnt DESC
    """).fetchall()
    cities = [dict(r) for r in city_rows]

    # Cycles
    cycle_count = conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
    avg_tradeable = conn.execute(
        "SELECT COALESCE(AVG(tradeable), 0) FROM cycles").fetchone()[0]

    return {
        "total_trades": total,
        "resolved": resolved,
        "pending": total - resolved,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / max(1, wins + losses) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "total_spent": round(total_spent, 2),
        "avg_ev": round(avg_ev, 1),
        "avg_ev_dollar": round(avg_ev_dollar, 1),
        "total_cycles": cycle_count,
        "avg_tradeable_per_cycle": round(avg_tradeable, 1),
        "cities": cities,
    }
