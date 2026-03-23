# ledger.py
"""
Alias shim: re-exports ``Ledger`` from ``ledger_telemetry`` under the shorter
name that ``scheduler.py`` imports.
"""
from ledger_telemetry import Ledger  # noqa: F401

__all__ = ["Ledger"]
