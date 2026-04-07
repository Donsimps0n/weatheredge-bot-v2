#!/usr/bin/env python3
"""
calibration_backfill.py — STRATEGY_REWRITE §3.4

Walks the trade ledger for resolved trades whose outcome has not yet been
written into accuracy_store.json["resolutions"], and back-fills them so the
calibration loop is alive again. Intended to be run nightly.

Usage:
    python scripts/calibration_backfill.py
    python scripts/calibration_backfill.py --ledger ledger.db --store accuracy_store.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone


def backfill(ledger_path: str, store_path: str) -> dict:
    if not os.path.exists(ledger_path):
        return {"ok": False, "error": f"ledger not found: {ledger_path}"}
    if os.path.exists(store_path):
        with open(store_path) as fh:
            try:
                store = json.load(fh)
            except Exception:
                store = {}
    else:
        store = {}
    store.setdefault("predictions", [])
    store.setdefault("resolutions", {})

    con = sqlite3.connect(ledger_path)
    con.row_factory = sqlite3.Row
    cols = [r[1] for r in con.execute("PRAGMA table_info(trades)")]
    if "resolved" not in cols:
        return {"ok": False, "error": "trades.resolved column missing"}
    rows = list(con.execute("SELECT * FROM trades WHERE resolved=1"))
    n_added = 0
    for r in rows:
        rid = str(r["id"])
        if rid in store["resolutions"]:
            continue
        store["resolutions"][rid] = {
            "won": int(r["won"] or 0),
            "pnl": float(r["pnl"] or 0),
            "our_prob": float(r["our_prob"] or 0),
            "mkt_price": float(r["mkt_price"] or 0),
            "city": r["city"],
            "ts": r["ts"],
        }
        n_added += 1

    store["last_backfill_utc"] = datetime.now(timezone.utc).isoformat()

    # Bucketed empirical WR (the live recal map source)
    buckets = [(0,10),(10,20),(20,30),(30,40),(40,60),(60,80),(80,100)]
    bstats = {f"{lo}-{hi}": {"n":0,"wins":0,"pnl":0.0} for lo,hi in buckets}
    for rid, rec in store["resolutions"].items():
        p = rec["our_prob"]
        for lo,hi in buckets:
            if lo <= p < hi:
                k = f"{lo}-{hi}"
                bstats[k]["n"] += 1
                bstats[k]["wins"] += rec["won"]
                bstats[k]["pnl"] += rec["pnl"]
                break
    for k,v in bstats.items():
        v["wr"] = (v["wins"]/v["n"]) if v["n"] else None
    store["bucket_stats_latest"] = bstats

    with open(store_path, "w") as fh:
        json.dump(store, fh, indent=2)

    return {"ok": True, "added": n_added, "total_resolved": len(store["resolutions"]), "buckets": bstats}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="ledger.db")
    ap.add_argument("--store", default="accuracy_store.json")
    args = ap.parse_args(argv)
    result = backfill(args.ledger, args.store)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
