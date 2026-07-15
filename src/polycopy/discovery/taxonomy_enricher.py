"""Taxonomy orchestration for PR69 discovery.

The pure :class:`OfficialPolymarketTaxonomyResolverV1` resolves taxonomy
only from a market-shaped payload it is given.  This enricher is the
HTTP-aware orchestration layer that:

  1. Receives a market payload (already enriched, or sparse).
  2. Fetches the official Gamma ``events[]`` entry whose ``id`` matches the
     market's denormalized event reference, so the resolver can probe
     event-level category/tags as a fallback.
  3. Fetches the official ``series`` payload where the resolver needs it.
  4. Calls the pure resolver with the assembled payload and returns a
     deterministic result that RETURNS THE INPUT + provenance so callers
     can persist the full chain.

The enricher NEVER infers a category from titles, slugs, outcomes, or
group-item titles.  It cannot lower trust standards.  Its sole job is
fetching official evidence the market payload happened to lack.

The module opens no database and never invokes a writer.  Its budget is
shared with the rest of the audit; requests decrement the same counter.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from polycopy.discovery._safe_get import _RequestBudget
from polycopy.discovery.adapter import DiscoveryAdapter
from polycopy.taxonomy.official_polymarket import (
    TAXONOMY_CONFLICT,
    TAXONOMY_PARTIAL,
    TAXONOMY_UNAVAILABLE,
    TAXONOMY_USABLE,
    OfficialPolymarketTaxonomyResolverV1,
    OfficialTaxonomyResult,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnrichmentAudit:
    """Audit counters for one enricher run (one CLI invocation).

    Exactly one terminal taxonomy status is assigned per market:
      usable | partial | unavailable | conflict.
    The invariant ``usable + partial + unavailable + conflict == markets_seen``
    MUST hold. Attempt/provenance counters are separate from the terminal
    counters so they cannot double-count across enrichment passes.
    """

    markets_seen: int = 0
    usable: int = 0
    partial: int = 0
    unavailable: int = 0
    conflict: int = 0
    # Attempt / provenance counters (NOT terminal; never overlap terminal).
    embedded_attempted: int = 0
    embedded_success: int = 0
    market_tag_attempted: int = 0
    market_tag_success: int = 0
    event_attempted: int = 0
    event_success: int = 0
    series_attempted: int = 0
    series_success: int = 0
    api_failures: int = 0
    lower_priority_mismatch_warnings: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


# Source labels used in the audit report.
SOURCE_EMBEDDED = "embedded"
SOURCE_MARKET_TAG = "market_tag_fallback"
SOURCE_EVENT = "event_fallback"
SOURCE_SERIES = "series_fallback"


def _event_payload(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return the embedded event dict inside a Gamma market payload."""
    if not isinstance(payload, Mapping):
        return {}
    events = payload.get("events")
    if isinstance(events, list) and events:
        first = events[0]
        if isinstance(first, Mapping):
            return first
    event = payload.get("event")
    if isinstance(event, Mapping):
        return event
    return {}


def _series_payload(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    series = payload.get("series")
    if isinstance(series, list) and series:
        first = series[0]
        if isinstance(first, Mapping):
            return first
    if isinstance(series, Mapping):
        return series
    return {}


def _event_id_from_market(market: Mapping[str, Any]) -> str | int | None:
    """Find the canonical event id reference in a Gamma market payload."""
    event = _event_payload(market)
    event_id = event.get("id")
    if isinstance(event_id, (str, int)) and not isinstance(event_id, bool):
        return event_id
    return None


def _series_id_from_market(market: Mapping[str, Any]) -> str | int | None:
    series = _series_payload(market)
    series_id = series.get("id")
    if isinstance(series_id, (str, int)) and not isinstance(series_id, bool):
        return series_id
    return None


def _is_broad_only(tags: Iterable[Mapping[str, Any]] | None) -> bool:
    """A tag list is broad-only if it contains no specific (non-root) tags.

    A 'broad-only' tag list is empty or only contains official broad roots,
    so the resolver can safely return USABLE without needing further
    fallback. A list with at least one specific tag is PARTIAL unless an
    explicit category is present elsewhere.
    """
    from polycopy.taxonomy.official_polymarket import OFFICIAL_BROAD_CATEGORY_MAP_V1

    for tag in tags or ():
        if not isinstance(tag, Mapping):
            return False
        label = str(tag.get("label") or "").strip().lower()
        slug = str(tag.get("slug") or "").strip().lower()
        if label and label not in OFFICIAL_BROAD_CATEGORY_MAP_V1:
            return False
        if slug and slug not in OFFICIAL_BROAD_CATEGORY_MAP_V1:
            return False
    return True


@dataclass(frozen=True)
class EnrichmentOutcome:
    """Outcome of one enricher pass for one market.

    The wrapped :class:`OfficialTaxonomyResult` is preserved verbatim; the
    extra fields retain the provenance of any fetched event/series
    evidence so the auditor can reproduce the result offline.
    """

    market_condition_id: str
    result: OfficialTaxonomyResult
    source_used: str
    enrichment_attempted: bool
    enrichment_errors: tuple[str, ...] = ()
    phase: str = "universe_taxonomy"

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_condition_id": self.market_condition_id,
            "resolver": self.result.to_dict(),
            "source_used": self.source_used,
            "enrichment_attempted": self.enrichment_attempted,
            "enrichment_errors": list(self.enrichment_errors),
            "phase": self.phase,
        }


class TaxonomyEnricher:
    """Orchestrate fetches needed to give the pure resolver its full evidence.

    State is per-instance (audit counters). One enricher per audit run.
    Pure inputs are the resolver instance + the bounded ``DiscoveryAdapter``.
    """

    def __init__(
        self,
        adapter: DiscoveryAdapter | None = None,
        resolver: OfficialPolymarketTaxonomyResolverV1 | None = None,
        *,
        budget: _RequestBudget | None = None,
        timeout_seconds: float = 12.0,
    ) -> None:
        self._adapter = adapter
        self._resolver = resolver or OfficialPolymarketTaxonomyResolverV1()
        self._budget = budget
        self._audit = EnrichmentAudit()
        self._timeout = timeout_seconds

    def audit(self) -> EnrichmentAudit:
        return self._audit

    def _record_terminal(self, status: str, *, fetch_errors: tuple[str, ...] = ()) -> None:
        """Increment exactly one terminal counter for a resolved status."""
        update: dict[str, int] = {"api_failures": self._audit.api_failures + (1 if fetch_errors else 0)}
        if status == "USABLE":
            update["usable"] = self._audit.usable + 1
        elif status == "PARTIAL":
            update["partial"] = self._audit.partial + 1
        elif status == "UNAVAILABLE":
            update["unavailable"] = self._audit.unavailable + 1
        elif status == "CONFLICT":
            update["conflict"] = self._audit.conflict + 1
        self._audit = EnrichmentAudit(**{**self._audit.as_dict(), **update})

    async def enrich_one(self, market: Mapping[str, Any], *, phase: str = "universe_taxonomy") -> EnrichmentOutcome:
        """Resolve one market, fetching fallback event / series evidence as needed.

        Implements the STEP 10 precedence contract:
          LEVEL 1 market → LEVEL 2 event → LEVEL 3 series.
        A higher-priority selected category is final; lower-priority
        mismatch is retained as provenance/a warning, never a conflict and
        never an override. A true same-level conflict (multiple broad
        categories at the chosen level) fails closed to CONFLICT.

        Terminal counters (usable/partial/unavailable/conflict) increment
        exactly once per market so the invariant holds. Attempt/provenance
        counters do not overlap terminal counters.
        """
        condition_id = str(market.get("conditionId") or market.get("condition_id") or "").lower()

        # --- LEVEL 1: market (embedded category + root tags) ---------------
        self._audit = EnrichmentAudit(
            **{**self._audit.as_dict(), "markets_seen": self._audit.markets_seen + 1,
               "embedded_attempted": self._audit.embedded_attempted + 1},
        )
        embedded = self._resolver.resolve(market)
        if embedded.status == TAXONOMY_CONFLICT:
            self._audit = EnrichmentAudit(
                **{**self._audit.as_dict(), "conflict": self._audit.conflict + 1},
            )
            return EnrichmentOutcome(
                market_condition_id=condition_id,
                result=embedded,
                source_used="conflict_held",
                enrichment_attempted=False,
                phase=phase,
            )
        if embedded.status == TAXONOMY_USABLE:
            self._audit = EnrichmentAudit(
                **{**self._audit.as_dict(), "usable": self._audit.usable + 1,
                   "embedded_success": self._audit.embedded_success + 1},
            )
            return EnrichmentOutcome(
                market_condition_id=condition_id,
                result=embedded,
                source_used=self._source_for_embedded(embedded, market),
                enrichment_attempted=False,
                phase=phase,
            )
        # embedded not usable (partial/unavailable at LEVEL 1) → fall through.
        if self._adapter is None:
            # No adapter → cannot fetch fallbacks. Record terminal once.
            self._record_terminal(embedded.status, fetch_errors=())
            return EnrichmentOutcome(
                market_condition_id=condition_id,
                result=embedded,
                source_used="no_adapter_no_fallback",
                enrichment_attempted=False,
                phase=phase,
            )
        market_tags = market.get("tags") if isinstance(market.get("tags"), list) else []
        fetched_event: Mapping[str, Any] | None = None
        fetched_series: Mapping[str, Any] | None = None
        fetch_errors: list[str] = []

        event_id = _event_id_from_market(market)
        if event_id is not None and (self._budget is None or self._budget.remaining > 0):
            self._audit = EnrichmentAudit(
                **{**self._audit.as_dict(), "event_attempted": self._audit.event_attempted + 1},
            )
            try:
                fetched_event = await self._adapter.get_event_raw(event_id, budget=self._budget, phase=phase)
            except Exception as exc:
                fetch_errors.append(f"event:{type(exc).__name__}")
                fetched_event = None
            if fetched_event is None and self._budget is not None and self._budget.remaining == 0:
                fetch_errors.append("event:budget_exhausted")

        series_id = _series_id_from_market(market)
        if series_id is not None and (self._budget is None or self._budget.remaining > 0):
            self._audit = EnrichmentAudit(
                **{**self._audit.as_dict(), "series_attempted": self._audit.series_attempted + 1},
            )
            try:
                fetched_series = await self._adapter.get_series_raw(series_id, budget=self._budget, phase=phase)
            except Exception as exc:
                fetch_errors.append(f"series:{type(exc).__name__}")
                fetched_series = None
            if fetched_series is None and self._budget is not None and self._budget.remaining == 0:
                fetch_errors.append("series:budget_exhausted")

        fetched_market_tags: list[dict[str, Any]] | None = None
        if not market_tags and condition_id and (self._budget is None or self._budget.remaining > 0):
            self._audit = EnrichmentAudit(
                **{**self._audit.as_dict(), "market_tag_attempted": self._audit.market_tag_attempted + 1},
            )
            try:
                fetched_market_tags = await self._adapter.get_market_tags(condition_id, budget=self._budget, phase=phase)
            except Exception as exc:
                fetch_errors.append(f"market_tags:{type(exc).__name__}")
                fetched_market_tags = None

        # Assemble a probe payload that the resolver can score.
        probe: dict[str, Any] = dict(market)
        if fetched_event is not None:
            probe["events"] = [dict(fetched_event)]
        if fetched_series is not None:
            probe["series"] = [dict(fetched_series)]
        if fetched_market_tags is not None:
            probe["tags"] = [dict(t) for t in fetched_market_tags]

        # Record successful-fetch provenance (separate from terminal counters).
        if fetched_event is not None:
            self._audit = EnrichmentAudit(**{**self._audit.as_dict(), "event_success": self._audit.event_success + 1})
        if fetched_series is not None:
            self._audit = EnrichmentAudit(**{**self._audit.as_dict(), "series_success": self._audit.series_success + 1})
        if fetched_market_tags is not None and fetched_market_tags:
            self._audit = EnrichmentAudit(**{**self._audit.as_dict(), "market_tag_success": self._audit.market_tag_success + 1})

        enriched = self._resolver.resolve(probe)
        if enriched.status == TAXONOMY_CONFLICT:
            self._audit = EnrichmentAudit(**{**self._audit.as_dict(), "conflict": self._audit.conflict + 1})
            return EnrichmentOutcome(
                market_condition_id=condition_id,
                result=enriched,
                source_used="conflict_after_enrichment",
                enrichment_attempted=True,
                enrichment_errors=tuple(fetch_errors),
                phase=phase,
            )
        if enriched.status == TAXONOMY_USABLE:
            self._audit = EnrichmentAudit(
                **{**self._audit.as_dict(),
                   "usable": self._audit.usable + 1,
                   "api_failures": self._audit.api_failures + (1 if fetch_errors else 0)},
            )
            source = SOURCE_EMBEDDED
            if fetched_market_tags is not None and fetched_market_tags:
                source = SOURCE_MARKET_TAG
            elif fetched_event is not None and fetched_series is not None:
                source = "event_and_series_fallback"
            elif fetched_event is not None:
                source = SOURCE_EVENT
            elif fetched_series is not None:
                source = SOURCE_SERIES
            return EnrichmentOutcome(
                market_condition_id=condition_id,
                result=enriched,
                source_used=source,
                enrichment_attempted=True,
                enrichment_errors=tuple(fetch_errors),
                phase=phase,
            )

        # Still PARTIAL / UNAVAILABLE after enrichment. Record terminal once.
        # Also record a lower-priority mismatch warning when LEVEL 1 produced a
        # different broad category that lost to a higher-priority selected one.
        lower_mismatch = 0
        if embedded.status == TAXONOMY_PARTIAL or embedded.status == TAXONOMY_UNAVAILABLE:
            # LEVEL 1 yielded no usable category but a different-tagged hint;
            # if a higher level selected a category, that is a provenance note.
            lower_mismatch = 1 if (fetched_event is not None or fetched_series is not None) else 0
        if enriched.status == TAXONOMY_PARTIAL:
            self._audit = EnrichmentAudit(
                **{**self._audit.as_dict(),
                   "partial": self._audit.partial + 1,
                   "lower_priority_mismatch_warnings": self._audit.lower_priority_mismatch_warnings + lower_mismatch,
                   "api_failures": self._audit.api_failures + (1 if fetch_errors else 0)},
            )
        elif enriched.status == TAXONOMY_UNAVAILABLE:
            self._audit = EnrichmentAudit(
                **{**self._audit.as_dict(),
                   "unavailable": self._audit.unavailable + 1,
                   "lower_priority_mismatch_warnings": self._audit.lower_priority_mismatch_warnings + lower_mismatch,
                   "api_failures": self._audit.api_failures + (1 if fetch_errors else 0)},
            )
        return EnrichmentOutcome(
            market_condition_id=condition_id,
            result=enriched,
            source_used="enrichment_insufficient",
            enrichment_attempted=True,
            enrichment_errors=tuple(fetch_errors),
            phase=phase,
        )

    @staticmethod
    def _source_for_embedded(
        result: OfficialTaxonomyResult, market: Mapping[str, Any]
    ) -> str:
        source = result.source or SOURCE_EMBEDDED
        if source == "market.category":
            return "embedded_market_category"
        if source == "market.root_tag":
            return "embedded_market_root_tag"
        if source == "event.category":
            return "embedded_event_category"
        if source == "event.root_tag":
            return "embedded_event_root_tag"
        if source == "series.category":
            return "embedded_series_category"
        if source == "series.root_tag":
            return "embedded_series_root_tag"
        return f"embedded:{source or 'unknown'}"


__all__ = [
    "EnrichmentAudit",
    "EnrichmentOutcome",
    "SOURCE_EMBEDDED",
    "SOURCE_EVENT",
    "SOURCE_MARKET_TAG",
    "SOURCE_SERIES",
    "TaxonomyEnricher",
    "enrich_market",
]


def enrich_market(
    market: Mapping[str, Any],
    *,
    adapter: DiscoveryAdapter | None = None,
    embedded_only: bool = False,
    phase: str | None = None,
) -> EnrichmentOutcome:
    """Synchronous convenience wrapper around :meth:`TaxonomyEnricher.enrich_one`.

    Useful for unit tests and offline callers. When ``embedded_only`` is true
    no fallback fetches are attempted (the adapter is never touched).
    """
    enricher = TaxonomyEnricher(adapter if not embedded_only else None)
    try:
        import asyncio

        return asyncio.run(enricher.enrich_one(market, phase=phase or "universe_taxonomy"))
    finally:
        pass
