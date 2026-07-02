"""Deterministic scoring helpers for all versioned formulas.

These are the only shared utilities for score normalization.
All formulas are pure, deterministic, and produce consistent results
for the same inputs.

Functions:
- linear_score: Linear interpolation between bounds
- inverse_score: Higher input = lower output (for "bad" metrics)
- clamp: Force value into [min, max] range
"""

from __future__ import annotations


def linear_score(x: float, low: float, high: float) -> float:
    """Linear interpolation: low → 0, high → 100.

    For values below low: extrapolates to 0 (clamped).
    For values above high: extrapolates to 100 (clamped).
    """
    if x <= low:
        return 0.0
    if x >= high:
        return 100.0
    return ((x - low) / (high - low)) * 100.0


def inverse_score(x: float, good: float, bad: float) -> float:
    """Inverse linear: good → 100, bad → 0 (for penalty metrics).

    For metrics where higher is worse.
    """
    if x <= good:
        return 100.0
    if x >= bad:
        return 0.0
    return ((bad - x) / (bad - good)) * 100.0


def clamp(x: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    """Clamp value to [minimum, maximum]."""
    return max(minimum, min(maximum, x))