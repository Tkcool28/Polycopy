"""Freshness helpers for data health monitoring."""

from __future__ import annotations

from datetime import datetime, timezone


def seconds_since(dt: datetime | None) -> float | None:
    """Return the number of seconds elapsed since `dt` (UTC).

    Returns None if dt is None or not timezone-aware.
    """
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        # Treat naive UTC datetimes as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    return delta.total_seconds()
