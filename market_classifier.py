# market_classifier.py
"""
Alias shim: re-exports ``classify_regime`` from ``regime_classifier`` under the
name that ``scheduler.py`` imports.
"""
from regime_classifier import classify_regime  # noqa: F401

__all__ = ["classify_regime"]
