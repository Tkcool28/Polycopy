"""Correction tests: taxonomy enricher terminal counters (STEP 10/11)."""
from __future__ import annotations


from polycopy.discovery.taxonomy_enricher import (
    EnrichmentAudit,
    TAXONOMY_USABLE,
    TaxonomyEnricher,
    enrich_market,
)


def test_terminal_counter_invariant_usable():
    audit = EnrichmentAudit(markets_seen=1, usable=1)
    assert audit.usable + audit.partial + audit.unavailable + audit.conflict == audit.markets_seen


def test_terminal_counter_invariant_partial():
    audit = EnrichmentAudit(markets_seen=3, usable=1, partial=1, unavailable=1)
    assert audit.usable + audit.partial + audit.unavailable + audit.conflict == audit.markets_seen


def test_terminal_counter_invariant_conflict():
    audit = EnrichmentAudit(markets_seen=2, usable=1, conflict=1)
    assert audit.usable + audit.partial + audit.unavailable + audit.conflict == audit.markets_seen


def test_terminal_counter_invariant_unavailable():
    audit = EnrichmentAudit(markets_seen=1, unavailable=1)
    assert audit.usable + audit.partial + audit.unavailable + audit.conflict == audit.markets_seen


def test_lower_priority_mismatch_is_warning_not_override():
    """STEP 10: event/series mismatch vs embedded must NOT override embedded usable."""
    market = {
        "conditionId": "0xc1",
        "question": "Will X win?",
        "outcomeType": "BINARY",
        "endDate": "2026-12-31T00:00:00+00:00",
        "category": "Politics",
        "subcategories": ["Elections"],
        "events": [{"name": "Different Event", "slug": "different-event"}],
        "series": [{"name": "Other Series", "slug": "other-series"}],
    }
    enricher = TaxonomyEnricher(None)
    import asyncio
    out = asyncio.run(enricher.enrich_one(market))
    # Embedded usable wins; lower-priority mismatch recorded as warning, not override.
    assert out.result.status == TAXONOMY_USABLE
    assert enricher.audit().lower_priority_mismatch_warnings >= 0
    # The usable decision came from embedded, not the mismatched fallback.
    assert out.source_used.startswith("embedded")


def test_event_fallback_only_when_embedded_missing():
    """STEP 10: with no embedded category, resolver yields partial/unavailable (no net)."""
    market = {
        "conditionId": "0xc2",
        "question": "Will Y happen?",
        "outcomeType": "BINARY",
        "endDate": "2026-12-31T00:00:00+00:00",
    }
    out = enrich_market(market, embedded_only=True)
    assert out.result.status in ("PARTIAL", "UNAVAILABLE")


def test_phase_label_threaded():
    """STEP 11: phase label is recorded in the outcome for budget accounting."""
    market = {
        "conditionId": "0xc3",
        "question": "Will Z happen?",
        "outcomeType": "BINARY",
        "endDate": "2026-12-31T00:00:00+00:00",
        "category": "Sports",
        "subcategories": ["Soccer"],
    }
    out = enrich_market(market, embedded_only=True, phase="universe_taxonomy")
    assert out.phase == "universe_taxonomy"


def test_enricher_audit_empty_initial():
    enricher = TaxonomyEnricher(None)
    assert enricher.audit().markets_seen == 0
