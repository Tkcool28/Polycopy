"""Lightweight RSS memory guard for long-running operational scripts (PR24B).

Long-running operational scripts (``collect_smart_money_data``,
``run_scan``, ``settle_paper_positions``, ``update_paper_portfolio``)
can balloon RSS through unintended accumulation (unbounded ``fetchall``
loops, in-memory trade-history lists, etc.). PR24B removes several of
the worst offenders (see :mod:`polycopy.runtime.query_batches`) and
adds a *belt-and-suspenders* watchdog so any future regression is
caught loudly and early.

Design choices
--------------
- **Disabled by default.** Setting ``POLYCOPY_MAX_RSS_MB`` to a positive
  integer enables the guard. Without it, every helper is a no-op so
  development / CI / unit-test runs do not accidentally trip a limit.
  Rationale: the current VPS budget has gigabytes of headroom, and a
  silent hard cap during a fresh-DB debugging session is worse than no
  cap. Operators who want the safety net turn it on explicitly.
- **Standard library only.** ``resource.getrusage`` on POSIX; falls back
  to ``/proc/self/status`` parsing on Linux for more accurate RSS (since
  macOS ``getrusage`` reports ru_maxrss which is peak-lifetime, not
  current). On non-POSIX platforms (Windows), the helper is best-effort
  and returns 0.0.
- **Cheap polling.** A ``check_rss_limit`` call is one ``open``+``read``
  on ``/proc/self/status`` (~3 KB) — microseconds. Safe to call from
  inside tight loops.

Exception
---------
:class:`MemoryLimitExceeded` is raised when RSS exceeds the configured
ceiling. Catch it at the script's main, log + stderr-print, and exit
nonzero so timers / operators see the abort. The shared operational
lock from PR24D releases automatically via its context manager.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

logger = logging.getLogger(__name__)


# Environment variable that turns on the guard.
RSS_LIMIT_ENV_VAR = "POLYCOPY_MAX_RSS_MB"


class MemoryLimitExceeded(RuntimeError):
    """Raised when the current process RSS exceeds the configured ceiling.

    Attributes:
        context: short label of where the check happened (e.g. ``"scan"``).
        rss_mb: current RSS in MiB at the time of the check.
        limit_mb: configured ceiling in MiB.
    """

    def __init__(self, context: str, rss_mb: float, limit_mb: float) -> None:
        self.context = context
        self.rss_mb = rss_mb
        self.limit_mb = limit_mb
        super().__init__(
            f"RSS limit exceeded in {context!r}: "
            f"{rss_mb:.1f} MiB > {limit_mb:.1f} MiB "
            f"(set {RSS_LIMIT_ENV_VAR}={limit_mb:.0f})"
        )


def get_current_rss_mb() -> float:
    """Return the current process RSS in MiB, or 0.0 if unavailable.

    Linux: parses ``/proc/self/status`` VmRSS line.
    Other POSIX: ``resource.getrusage(RUSAGE_SELF).ru_maxrss`` — note
    that on Linux this is in KiB while on macOS it is in bytes, so we
    normalise to MiB. The value is *peak lifetime*, not current, on
    macOS / BSD — sufficient for a watchdog because peak only ever
    grows.
    Windows: returns 0.0; callers should treat that as "cannot
    enforce", not "OK".
    """
    # Linux first: /proc/self/status VmRSS is the most accurate current
    # RSS. We try this before falling back to getrusage.
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    # VmRSS:     12345 kB
                    kb = float(parts[1])
                    return kb / 1024.0
    except (OSError, ValueError, IndexError):
        pass

    # POSIX fallback. resource module is available on Linux/macOS/BSD
    # but not on vanilla Windows.
    if sys.platform != "win32":
        try:
            import resource  # type: ignore[import-not-found]

            usage = resource.getrusage(resource.RUSAGE_SELF)
            # On Linux: ru_maxrss is in KiB. On macOS: bytes.
            rss_raw = float(usage.ru_maxrss)
            if sys.platform == "darwin":
                return rss_raw / (1024.0 * 1024.0)
            return rss_raw / 1024.0
        except (ImportError, AttributeError, ValueError):
            pass

    return 0.0


def get_max_rss_mb_from_env(default: Optional[float] = None) -> Optional[float]:
    """Parse ``POLYCOPY_MAX_RSS_MB`` into a float ceiling in MiB.

    Returns ``None`` (or the ``default``) when:
      - the variable is unset,
      - the variable is set to the empty string,
      - the value is not a positive number,
      - the value is non-positive (negative or zero).

    Returns the parsed positive float otherwise.

    Tests rely on this contract: any of {unset, empty, "abc", "-5",
    "0"} yields ``None``; only a parseable positive number yields a
    real limit.
    """
    raw = os.environ.get(RSS_LIMIT_ENV_VAR)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Ignoring invalid %s=%r (expected positive number of MiB)",
            RSS_LIMIT_ENV_VAR,
            raw,
        )
        return default
    if value <= 0:
        logger.warning(
            "Ignoring non-positive %s=%r (expected positive number of MiB)",
            RSS_LIMIT_ENV_VAR,
            raw,
        )
        return default
    return value


def check_rss_limit(
    context: str,
    max_rss_mb: Optional[float] = None,
) -> None:
    """Check current RSS against the configured ceiling.

    Args:
        context: short label for the calling site (e.g. ``"scan:wallet-3"``)
            so log lines identify *where* the guard tripped.
        max_rss_mb: ceiling in MiB. If ``None``, the function reads the
            current RSS and silently returns without raising — i.e. the
            guard is disabled.

    Raises:
        MemoryLimitExceeded: if ``max_rss_mb`` is set and the current
            RSS exceeds it.

    Notes:
        A ``max_rss_mb`` of 0 or negative is treated as "disabled" for
        safety (returns without raising). Tests rely on this so the
        guard never fires spuriously.
    """
    if max_rss_mb is None:
        return
    if max_rss_mb <= 0:
        return
    rss_mb = get_current_rss_mb()
    if rss_mb <= 0.0:
        # Cannot read RSS — do not block on a broken measurement.
        return
    if rss_mb > max_rss_mb:
        logger.error(
            "RSS guard tripped in %s: %.1f MiB > %.1f MiB",
            context,
            rss_mb,
            max_rss_mb,
        )
        raise MemoryLimitExceeded(context=context, rss_mb=rss_mb, limit_mb=max_rss_mb)


__all__ = [
    "MemoryLimitExceeded",
    "RSS_LIMIT_ENV_VAR",
    "check_rss_limit",
    "get_current_rss_mb",
    "get_max_rss_mb_from_env",
]