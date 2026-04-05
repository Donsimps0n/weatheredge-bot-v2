"""
bias_agent.py — Live Station Bias Agent

Runs every cycle as part of the Ruflo agent swarm. Reads station_bias.db,
computes per-station corrections in °C, publishes them to SharedState,
and enriches signals with bias-corrected probabilities.

This REPLACES the hardcoded _CITY_BIAS_C dict in api_server.py.

Architecture:
  1. poll()         — refresh corrections from station_bias.db (every 5 min)
  2. get_corrections() — return current {city: correction_c} dict
  3. enrich_signals()  — apply corrections to signal probabilities
  4. report()       — publish stats to SharedState for other agents

The agent communicates with:
  - IntelligenceFeed (sigma calibration uses bias confidence)
  - BinSniper (bias-corrected probabilities for snipe evaluation)
  - GFSRefresh (corrected baseline for delta detection)
  - ObsConfirm (corrected kill thresholds)
  - AccuracyTracker (reports correction effectiveness)
  - SharedState (publishes corrections for all consumers)
"""

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Minimum requirements to apply a correction
MIN_OBS_FOR_CORRECTION = 5
MIN_OBS_FOR_FULL_WEIGHT = 30
CORRECTION_THRESHOLD_F = 0.5  # Only correct if |bias| > 0.5°F
POLL_INTERVAL_S = 3600  # Re-read DB every hour (was 86400; need fresher data during observation phase)
F_TO_C = 1.0 / 1.8

# ── Knob B: Sigma floor ──
# If station RMSE (std) is high, the model is uncertain → widen sigma
# so we don't overtrade on bad data. Floor in °C.
SIGMA_FLOOR_BASE_C = 1.0      # Minimum sigma for any city
SIGMA_FLOOR_NOISY_C = 2.0     # Floor for stations with std > 4°F
NOISY_STD_THRESHOLD_F = 4.0   # Stations above this get the noisy floor

# ── Knob C: Min EV gate addon ──
# Unreliable stations need a higher EV bar to trade.
# Adds to the base min_theo_ev gate. ALL VALUES IN PERCENTAGE POINTS (pp).
# The gate uses min_ev=5.0pp, so addons must be in the same unit.
EV_ADDON_NOISY = 4.0          # +4pp EV for noisy stations (std > 4°F)
EV_ADDON_LOW_N = 2.0          # +2pp EV for stations with < 15 observations
EV_ADDON_OUTLIER = 3.0        # +3pp EV if outlier rate > 75%
OUTLIER_THRESHOLD_PCT = 15.0

# ── Knob D: Size multiplier ──
# Scale position size based on station reliability.
# Reliable = size up. Unreliable = size down.
SIZE_MULT_EXCELLENT = 1.2     # n >= 30, std < 2.5°F, |bias| < 1°F
SIZE_MULT_GOOD = 1.0          # n >= 15, std < 4°F
SIZE_MULT_MEDIOCRE = 0.7      # n >= 5, std >= 4°F or high outlier rate
SIZE_MULT_UNKNOWN = 0.5       # No data or n < 5

# ── Combined-penalty cap ──
# Prevent triple-stacking from effectively killing a station.
# ALL VALUES IN PERCENTAGE POINTS to match the gate unit system.
MAX_EV_ADDON = 5.0            # Cap at +5pp (gate raises from 5pp to max 10pp)
MIN_SIZE_MULT = 0.4           # Floor at 0.4x
MAX_SIGMA_FLOOR_C = 2.5       # Cap at 2.5°C — enough to penalize without blacklisting


class StationBiasAgent:
    """Live agent that reads station_bias.db and provides per-station
    temperature corrections to the trading pipeline."""

    def __init__(self, db_path: str = None, config_cities: list = None):
        self._db_path = db_path or self._find_db()
        self._corrections_f: Dict[str, float] = {}   # icao -> correction °F
        self._corrections_c: Dict[str, float] = {}   # city_lower -> correction °C
        self._city_to_icao: Dict[str, str] = {}       # city_lower -> ICAO
        self._station_meta: Dict[str, dict] = {}      # icao -> full bias info
        self._drift_cache: Dict[str, dict] = {}       # icao -> drift info
        self._last_poll = 0.0
        self._last_db_mtime = 0.0                     # Track DB file modification
        self._poll_count = 0
        self._n_corrections_active = 0
        self._last_poll_ok = False                     # Did last poll succeed?
        self._last_error: Optional[str] = None         # Last error (truncated)

        # Build city→ICAO mapping from config
        if config_cities:
            for c in config_cities:
                self._city_to_icao[c["city"].lower()] = c["icao"]

        # Initial load
        self.poll(force=True)
        log.info("BIAS_AGENT: initialized | db=%s | %d stations | %d active corrections",
                 self._db_path, len(self._station_meta), self._n_corrections_active)

    def _find_db(self) -> str:
        candidates = [
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "station_bias.db"),
            "station_bias.db",
            "/data/station_bias.db",
        ]
        for p in candidates:
            if os.path.exists(p):
                return p
        return candidates[0]

    # ------------------------------------------------------------------
    # Polling — refresh from DB
    # ------------------------------------------------------------------

    def needs_poll(self) -> bool:
        """Check if we need to re-read the DB.
        Triggers on: (a) timer expired, or (b) DB file was modified since last read."""
        if (time.time() - self._last_poll) >= POLL_INTERVAL_S:
            return True
        # Also reload if DB was externally updated (daily job, bootstrap, etc.)
        try:
            mtime = os.path.getmtime(self._db_path)
            if mtime > self._last_db_mtime:
                log.info("BIAS_AGENT: DB modified (mtime %.0f > %.0f), triggering reload",
                         mtime, self._last_db_mtime)
                return True
        except OSError:
            pass
        return False

    def poll(self, force: bool = False):
        if not force and not self.needs_poll():
            return
        if not os.path.exists(self._db_path):
            log.debug("BIAS_AGENT: no DB at %s", self._db_path)
            return

        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT station, city, n_observations, mean_forecast_error_f,
                       median_forecast_error_f, std_forecast_error_f,
                       warm_bias_pct, cold_bias_pct, last_updated
                FROM station_bias_summary
                ORDER BY n_observations DESC
            """).fetchall()
            conn.close()
        except Exception as e:
            self._last_poll_ok = False
            self._last_error = str(e)[:200]
            log.warning("BIAS_AGENT: DB read failed: %s", e)
            return

        corrections_f = {}
        corrections_c = {}
        station_meta = {}
        n_active = 0

        for row in rows:
            icao = row["station"]
            city = row["city"]
            n = row["n_observations"]
            mean_err = row["mean_forecast_error_f"]
            std = row["std_forecast_error_f"]
            city_lower = city.lower()

            # Compute confidence tier
            if n >= 30:
                confidence = "high"
            elif n >= 15:
                confidence = "medium"
            elif n >= 5:
                confidence = "low"
            else:
                confidence = "none"

            # Compute correction
            correction_f = 0.0
            weight = 0.0
            if n >= MIN_OBS_FOR_CORRECTION and abs(mean_err) > CORRECTION_THRESHOLD_F:
                correction_f = round(mean_err, 1)
                # Scale weight by confidence
                if n >= MIN_OBS_FOR_FULL_WEIGHT:
                    weight = 1.0
                elif n >= 15:
                    weight = 0.7
                else:
                    weight = 0.3
                # Apply weight — partial correction for low confidence
                correction_f = round(correction_f * weight, 2)
                n_active += 1

            # Sign convention for main signal builder:
            # station_bias stores error = resolved - forecast
            # positive error = resolves warmer = our model underestimates
            # To correct: we ADD the error to our forecast (shift warmer)
            # In °C for the ensemble: correction_c = correction_f / 1.8
            # But _CITY_BIAS_C convention is OPPOSITE: positive = OM overestimates = SUBTRACT
            # So we NEGATE: correction_c = -correction_f / 1.8
            correction_c = round(-correction_f * F_TO_C, 3)

            corrections_f[icao] = correction_f
            corrections_c[city_lower] = correction_c

            # ── Knob B: Sigma floor (°C) ──
            # Noisy stations get a wider sigma floor so the model
            # doesn't pretend it knows more than it does.
            if std > NOISY_STD_THRESHOLD_F:
                sigma_floor_c = SIGMA_FLOOR_NOISY_C
            else:
                sigma_floor_c = SIGMA_FLOOR_BASE_C

            # ── Knob C: Min EV gate addon ──
            # Stacks additively: noisy + low n + high outlier = up to +9pp (capped at MAX_EV_ADDON)
            ev_addon = 0.0
            ev_addon_reasons = []
            if std > NOISY_STD_THRESHOLD_F:
                ev_addon += EV_ADDON_NOISY
                ev_addon_reasons.append("NOISY")
            if n < 15:
                ev_addon += EV_ADDON_LOW_N
                ev_addon_reasons.append("LOW_N")
            outlier_rate = max(row["warm_bias_pct"], row["cold_bias_pct"])
            if std > 3.0 and outlier_rate > 75:
                ev_addon += EV_ADDON_OUTLIER
                ev_addon_reasons.append("OUTLIER")
            # Apply caps and track if any cap bound
            _ev_raw = ev_addon
            ev_addon = round(min(ev_addon, MAX_EV_ADDON), 3)

            # ── Knob D: Size multiplier ──
            if confidence == "high" and std < 2.5 and abs(mean_err) < 1.0:
                size_mult = SIZE_MULT_EXCELLENT
            elif confidence in ("high", "medium") and std < NOISY_STD_THRESHOLD_F:
                size_mult = SIZE_MULT_GOOD
            elif n >= MIN_OBS_FOR_CORRECTION:
                size_mult = SIZE_MULT_MEDIOCRE
            else:
                size_mult = max(SIZE_MULT_UNKNOWN, MIN_SIZE_MULT)

            # Cap sigma floor
            _sf_raw = sigma_floor_c
            sigma_floor_c = min(sigma_floor_c, MAX_SIGMA_FLOOR_C)

            # Track if any penalty cap bound
            penalty_cap_hit = (
                _ev_raw > MAX_EV_ADDON or
                _sf_raw > MAX_SIGMA_FLOOR_C or
                SIZE_MULT_UNKNOWN < MIN_SIZE_MULT  # Would have been clamped
            )

            station_meta[icao] = {
                "station": icao,
                "city": city,
                "n": n,
                "mean_err_f": mean_err,
                "median_err_f": row["median_forecast_error_f"],
                "std_f": std,
                "warm_pct": row["warm_bias_pct"],
                "cold_pct": row["cold_bias_pct"],
                "confidence": confidence,
                "weight": weight,
                "correction_f": correction_f,
                "correction_c": correction_c,
                # Knobs
                "sigma_floor_c": sigma_floor_c,
                "ev_addon": ev_addon,
                "ev_addon_reasons": ev_addon_reasons,
                "size_mult": size_mult,
                "penalty_cap_hit": penalty_cap_hit,
                "last_updated": row["last_updated"],
            }

        self._corrections_f = corrections_f
        self._corrections_c = corrections_c
        self._station_meta = station_meta
        self._n_corrections_active = n_active
        self._last_poll = time.time()
        self._last_poll_ok = True
        self._last_error = None
        self._poll_count += 1

        # Track DB mtime for change-detection polling
        try:
            self._last_db_mtime = os.path.getmtime(self._db_path)
        except OSError:
            pass

        # Compute drift and apply drift adjustments
        self._drift_cache = self.compute_drift()
        self._apply_drift_adjustments()

        _n_capped = sum(1 for m in station_meta.values() if m.get("penalty_cap_hit"))
        if self._poll_count <= 1 or self._poll_count % 12 == 0:
            _drift_count = sum(1 for d in self._drift_cache.values() if d.get("drifting"))
            log.info("BIAS_AGENT: refreshed %d stations | %d active corrections | %d capped | %d drifting | top: %s",
                     len(station_meta), n_active, _n_capped, _drift_count,
                     ", ".join(f"{m['city']}={m['correction_f']:+.1f}F"
                               for m in sorted(station_meta.values(),
                                               key=lambda x: abs(x['correction_f']),
                                               reverse=True)[:5]))

    # ------------------------------------------------------------------
    # Drift-aware adjustments — make drift actionable, not just observable
    # ------------------------------------------------------------------

    DRIFT_SIZE_PENALTY = 0.8      # Additional 0.8x multiplier for drifting stations
    DRIFT_EV_ADDON = 1.0          # +1pp EV gate for drifting stations (in pp, same as gate unit)
    DRIFT_BIAS_BLEND = 0.5        # 50% recent + 50% historical for drifting bias

    def _apply_drift_adjustments(self):
        """For drifting stations, adjust knobs to prevent stale-bias poisoning.
        Modifies station_meta in-place after drift is computed.
        Fail-open: errors on individual stations are caught and logged; other
        stations and the agent itself are never affected."""
        for icao, drift in self._drift_cache.items():
            if not drift.get("drifting") or icao not in self._station_meta:
                continue
            try:
                meta = self._station_meta[icao]

                # Store original values for logging
                meta["drift_info"] = drift

                # Blend bias: 50% recent (30d) + 50% all-time
                bias_30d_f = drift["bias_30d_f"]
                bias_all_f = drift["bias_180d_f"]
                blended_f = round(self.DRIFT_BIAS_BLEND * bias_30d_f +
                                  (1 - self.DRIFT_BIAS_BLEND) * bias_all_f, 2)

                # Only apply blended correction if it passes threshold
                if abs(blended_f) > CORRECTION_THRESHOLD_F:
                    weight = meta["weight"] if meta["weight"] > 0 else 0.5
                    new_correction_f = round(blended_f * weight, 2)
                    new_correction_c = round(-new_correction_f * F_TO_C, 3)

                    meta["correction_f_pre_drift"] = meta["correction_f"]
                    meta["correction_f"] = new_correction_f
                    meta["correction_c"] = new_correction_c
                    # Update city-level correction
                    city_lower = meta["city"].lower()
                    self._corrections_c[city_lower] = new_correction_c
                    self._corrections_f[icao] = new_correction_f

                # Shrink size multiplier by drift penalty
                meta["size_mult_pre_drift"] = meta["size_mult"]
                meta["size_mult"] = round(max(meta["size_mult"] * self.DRIFT_SIZE_PENALTY,
                                              MIN_SIZE_MULT), 2)

                # Bump EV gate
                meta["ev_addon_pre_drift"] = meta["ev_addon"]
                meta["ev_addon"] = round(min(meta["ev_addon"] + self.DRIFT_EV_ADDON,
                                             MAX_EV_ADDON), 3)
                if "DRIFT" not in meta.get("ev_addon_reasons", []):
                    meta["ev_addon_reasons"] = meta.get("ev_addon_reasons", []) + ["DRIFT"]

                log.info("BIAS_DRIFT_ADJ: %s/%s | bias: %.1f→%.1fF (blend 30d=%.1f + all=%.1f) | "
                         "size: %.2f→%.2f | ev_addon: %.3f→%.3f",
                         icao, meta["city"],
                         meta.get("correction_f_pre_drift", meta["correction_f"]), meta["correction_f"],
                         bias_30d_f, bias_all_f,
                         meta.get("size_mult_pre_drift", meta["size_mult"]), meta["size_mult"],
                         meta.get("ev_addon_pre_drift", meta["ev_addon"]), meta["ev_addon"])

            except Exception as _drift_err:
                # Fail-open: skip this station, never kill the agent
                log.warning("BIAS_DRIFT_ADJ: %s skipped due to error: %s", icao, _drift_err)

    # ------------------------------------------------------------------
    # Corrections API
    # ------------------------------------------------------------------

    def get_city_bias_c(self) -> Dict[str, float]:
        """Return {city_lower: correction_c} dict.
        Drop-in replacement for _CITY_BIAS_C."""
        return dict(self._corrections_c)

    def get_correction_f(self, station: str) -> float:
        return self._corrections_f.get(station, 0.0)

    def get_correction_c(self, city: str) -> float:
        return self._corrections_c.get(city.lower(), 0.0)

    def get_station_info(self, station: str) -> dict:
        return self._station_meta.get(station, {})

    def get_all_corrections(self) -> Dict[str, dict]:
        return dict(self._station_meta)

    def get_station_adjustments(self, city: str) -> dict:
        """Return all four knobs for a city. Used by the decision engine.
        Returns conservative defaults if station is unknown."""
        city_lower = city.lower()
        icao = self._city_to_icao.get(city_lower, "")
        meta = self._station_meta.get(icao)

        if not meta:
            return {
                "bias_correction_c": 0.0,
                "bias_correction_f": 0.0,
                "sigma_floor_c": SIGMA_FLOOR_BASE_C,
                "ev_addon": EV_ADDON_LOW_N,  # Conservative: treat unknown as low-n
                "ev_addon_reasons": ["LOW_N"],
                "size_mult": SIZE_MULT_UNKNOWN,
                "confidence": "none",
                "n": 0,
                "penalty_cap_hit": False,
                "source": "default_unknown",
            }

        return {
            "bias_correction_c": meta["correction_c"],
            "bias_correction_f": meta["correction_f"],
            "sigma_floor_c": meta["sigma_floor_c"],
            "ev_addon": meta["ev_addon"],
            "ev_addon_reasons": meta.get("ev_addon_reasons", []),
            "size_mult": meta["size_mult"],
            "confidence": meta["confidence"],
            "n": meta["n"],
            "std_f": meta["std_f"],
            "penalty_cap_hit": meta.get("penalty_cap_hit", False),
            "source": "station_bias_agent",
        }

    # ------------------------------------------------------------------
    # Signal enrichment — inject all four knobs into signals
    # ------------------------------------------------------------------

    def enrich_signals(self, sigs: list) -> list:
        """Enrich signals with all four station reliability knobs.
        Called after _build_signals, before trading decisions.

        Injects into each signal:
          - bias_correction_c/f  (Knob A: shift distribution)
          - sigma_floor_c        (Knob B: minimum sigma)
          - ev_addon             (Knob C: raise EV gate)
          - size_mult            (Knob D: scale position size)
        """
        enriched = 0
        for sig in sigs:
            city = sig.get("city", "")
            adj = self.get_station_adjustments(city)

            sig["bias_correction_c"] = adj["bias_correction_c"]
            sig["bias_correction_f"] = adj["bias_correction_f"]
            sig["bias_confidence"] = adj["confidence"]
            sig["bias_n_obs"] = adj["n"]
            sig["bias_source"] = adj["source"]
            sig["sigma_floor_c"] = adj["sigma_floor_c"]
            sig["ev_addon"] = adj["ev_addon"]
            sig["ev_addon_reasons"] = adj.get("ev_addon_reasons", [])
            sig["size_mult"] = adj["size_mult"]
            sig["penalty_cap_hit"] = adj.get("penalty_cap_hit", False)

            # Inject drift flags if available
            icao = self._city_to_icao.get(city.lower(), "")
            drift = self._drift_cache.get(icao)
            if drift:
                sig["station_drifting"] = drift.get("drifting", False)
                sig["drift_shift_f"] = drift.get("drift_f", 0.0)
            else:
                sig["station_drifting"] = False
                sig["drift_shift_f"] = 0.0

            if adj["source"] != "default_unknown":
                enriched += 1

        if enriched > 0:
            # Always log summary; log sample knob values for first 3 polls to confirm wiring
            _nonzero_bias = sum(1 for s in sigs if s.get("bias_correction_f", 0) != 0)
            _nonzero_sf = sum(1 for s in sigs if s.get("sigma_floor_c", 0) > 1.0)
            _nonzero_ev = sum(1 for s in sigs if s.get("ev_addon", 0) > 0)
            _sized_down = sum(1 for s in sigs if s.get("size_mult", 1.0) < 1.0)
            _drifting = sum(1 for s in sigs if s.get("station_drifting"))
            _capped = sum(1 for s in sigs if s.get("penalty_cap_hit"))
            log.info("BIAS_AGENT: enriched %d/%d | bias_active=%d sigma_floor=%d ev_addon=%d sized_down=%d drifting=%d capped=%d",
                     enriched, len(sigs), _nonzero_bias, _nonzero_sf, _nonzero_ev, _sized_down, _drifting, _capped)
            if self._poll_count <= 3:
                # Sample 3 signals with active knobs for verification
                _samples = [s for s in sigs if s.get("bias_correction_f", 0) != 0 or s.get("ev_addon", 0) > 0][:3]
                for s in _samples:
                    _reasons = s.get("ev_addon_reasons", [])
                    log.info("  KNOB_SAMPLE: %s | bias_f=%s sf=%s ev_addon=%spp reasons=%s size_mult=%s drift=%s cap=%s",
                             s.get("city"), s.get("bias_correction_f"), s.get("sigma_floor_c"),
                             s.get("ev_addon"), _reasons or "none",
                             s.get("size_mult"), s.get("station_drifting"), s.get("penalty_cap_hit"))
        return sigs

    # ------------------------------------------------------------------
    # SharedState publishing
    # ------------------------------------------------------------------

    def publish_to_shared_state(self, shared_state):
        """Publish corrections to SharedState for all agents."""
        try:
            shared_state.publish('bias_agent', 'corrections_c', self._corrections_c)
            shared_state.publish('bias_agent', 'corrections_f', self._corrections_f)
            shared_state.publish('bias_agent', 'station_meta', {
                k: {kk: vv for kk, vv in v.items() if kk != 'last_updated'}
                for k, v in self._station_meta.items()
            })
            shared_state.publish('bias_agent', 'stats', {
                'n_stations': len(self._station_meta),
                'n_active_corrections': self._n_corrections_active,
                'poll_count': self._poll_count,
                'last_poll': self._last_poll,
            })

            # Publish significant biases as strategy insights
            significant = [
                m for m in self._station_meta.values()
                if abs(m['correction_f']) >= 1.0 and m['confidence'] in ('medium', 'high')
            ]
            for m in significant:
                shared_state.add_strategy_insight(
                    'bias_agent',
                    f"{m['station']}/{m['city']}: correction={m['correction_f']:+.1f}°F "
                    f"(n={m['n']}, conf={m['confidence']}, weight={m['weight']:.0%})"
                )
        except Exception as e:
            log.debug("BIAS_AGENT: publish to shared state failed: %s", e)

    # ------------------------------------------------------------------
    # Drift detection — 30d vs 180d bias comparison
    # ------------------------------------------------------------------

    def compute_drift(self) -> Dict[str, dict]:
        """Compare recent (30d) vs historical (180d) bias per station.
        Returns {icao: {city, bias_30d, bias_180d, drift_f, drifting}} for
        stations with enough data in both windows."""
        if not os.path.exists(self._db_path):
            return {}
        try:
            conn = sqlite3.connect(self._db_path)
            rows_30 = conn.execute("""
                SELECT station, city, AVG(forecast_error_f) as mean_err, COUNT(*) as n
                FROM bias_observations
                WHERE date >= date('now', '-30 days')
                GROUP BY station
                HAVING n >= 3
            """).fetchall()
            rows_180 = conn.execute("""
                SELECT station, city, AVG(forecast_error_f) as mean_err, COUNT(*) as n
                FROM bias_observations
                GROUP BY station
                HAVING n >= 10
            """).fetchall()
            conn.close()
        except Exception as e:
            log.warning("BIAS_AGENT: drift query failed: %s", e)
            return {}

        hist = {r[0]: {"city": r[1], "mean": r[2], "n": r[3]} for r in rows_180}
        drift_report = {}
        for r in rows_30:
            icao, city, mean_30, n_30 = r[0], r[1], r[2], r[3]
            if icao not in hist:
                continue
            mean_180 = hist[icao]["mean"]
            drift_f = round(mean_30 - mean_180, 2)
            # Flag if recent bias shifted by more than 2°F from historical
            drifting = abs(drift_f) > 2.0
            drift_report[icao] = {
                "city": city,
                "bias_30d_f": round(mean_30, 2),
                "bias_180d_f": round(mean_180, 2),
                "drift_f": drift_f,
                "n_30d": n_30,
                "n_180d": hist[icao]["n"],
                "drifting": drifting,
            }
            if drifting:
                log.warning("BIAS_DRIFT: %s/%s 30d=%.1f°F vs 180d=%.1f°F (drift=%+.1f°F)",
                            icao, city, mean_30, mean_180, drift_f)
        return drift_report

    def report(self) -> dict:
        """Return agent status report."""
        return {
            "agent": "StationBiasAgent",
            "db_path": self._db_path,
            "n_stations": len(self._station_meta),
            "n_active_corrections": self._n_corrections_active,
            "poll_count": self._poll_count,
            "last_poll_ago_s": round(time.time() - self._last_poll, 0),
            "top_corrections": sorted(
                [{"city": m["city"], "icao": m["station"],
                  "correction_f": m["correction_f"], "correction_c": m["correction_c"],
                  "confidence": m["confidence"], "n": m["n"]}
                 for m in self._station_meta.values()
                 if abs(m["correction_f"]) > 0],
                key=lambda x: abs(x["correction_f"]),
                reverse=True
            )[:10],
        }
