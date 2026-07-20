"""PR #73 — Cohort-wide Gamma request budget and dedupe wrapper.

A single awaitable wrapper that:

  * counts EVERY Gamma resolution (both the ingestion pipeline's per-candidate
    resolve and ``enrich_source_trade``'s provenance resolve) against ONE shared
    cohort budget;
  * resolves each unique ``condition_id`` exactly once (caches the result) so
    the same trade/condition is never hit twice;
  * raises :class:`GammaBudgetExhausted` the moment the budget would be
    exceeded, so the cohort can stop cleanly and roll back;
  * preserves the underlying resolver's identity (found / not-found / provider
    error) — it never turns a provider error into not-found.

This is the authoritative Gamma owner for the bounded multi-watch cohort. The
collector removes its own second (redundant) metadata resolve and delegates all
Gamma work here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional


class GammaBudgetExhausted(Exception):
    """Raised when the shared cohort Gamma-request budget is exhausted.

    Propagated as a cohort-level failure (stop reason ``gamma_budget_exhausted``)
    so the whole cohort rolls back rather than silently degrading or exceeding
    the configured maximum.
    """


class GammaResolutionError(Exception):
    """Hard Gamma provider failure raised by the cohort path.

    Distinguishes a real provider/network/Gemini error from an ordinary
    not-found. The cohort converts this into a failed cohort + full rollback
    rather than silently degrading the evidence.
    """


# The real resolver yields ``Optional[Mapping]``; the wrapper is async to match
# both the ingestion pipeline (``await gamma_resolver(...)``) and
# ``resolve_gamma_state`` (which awaits an awaitable resolver).
GammaResolver = Callable[[str], Awaitable[Optional[Mapping[str, Any]]]]


class SharedGammaBudget:
    """Cohort-wide Gamma budget + per-condition dedupe.

    Args:
        base: the underlying resolver (may be a real async gamma fetch or a
            fake). ``None`` means no Gamma is requested and the wrapper is never
            used.
        budget: total Gamma requests allowed for the ENTIRE cohort.
    """

    def __init__(self, base: Optional[GammaResolver], *, budget: int) -> None:
        self._base = base
        self._budget = max(0, int(budget))
        self._used = 0
        self._cache: dict[str, Optional[Mapping[str, Any]]] = {}
        self.exhausted = False

    @property
    def used(self) -> int:
        return self._used

    @property
    def budget(self) -> int:
        return self._budget

    @property
    def remaining(self) -> int:
        return max(0, self._budget - self._used)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gamma_budget": self._budget,
            "gamma_used": self._used,
            "gamma_remaining": self.remaining,
        }

    async def __call__(self, condition_id: str) -> Optional[Mapping[str, Any]]:
        if self._base is None:
            # No gamma configured; treat as not-found without charging budget.
            return None

        # Resolve each unique condition at most once.
        if condition_id in self._cache:
            return self._cache[condition_id]

        # Enforce the hard cohort-wide cap BEFORE charging.
        if self._used >= self._budget:
            self.exhausted = True
            raise GammaBudgetExhausted(
                f"cohort Gamma request budget exhausted "
                f"(used={self._used}, budget={self._budget})"
            )

        self._used += 1
        market = await self._base(condition_id)
        self._cache[condition_id] = market
        return market


@dataclass
class CohortBudget:
    """One shared resource budget for the ENTIRE cohort run.

    * ``remaining_records`` — shared cohort-wide new-trade record budget.
      Decremented once per watch as it accepts rows; never resets per watch.
    * ``gamma`` — the shared :class:`SharedGammaBudget` (one cap, one dedupe
      cache for the whole cohort).
    * ``deadline_ts`` — absolute ``time.monotonic()`` deadline; ``None`` means
      "no deadline" (still bounded by per-watch config timeout).
    * ``rss_mb_limit`` — fail-closed resident-set ceiling.
    * ``stop_reason`` — exact reason the cohort stopped (set by the collector
      or orchestrator when a bound is hit).
    """

    remaining_records: int
    gamma: SharedGammaBudget
    deadline_ts: Optional[float] = None
    rss_mb_limit: float = 512.0
    stop_reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        d = {
            "remaining_records": self.remaining_records,
            "rss_mb_limit": self.rss_mb_limit,
            "stop_reason": self.stop_reason,
        }
        d.update(self.gamma.as_dict())
        return d
