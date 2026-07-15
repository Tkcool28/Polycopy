"""Section B — taxonomy enricher tests."""
from __future__ import annotations

import json

import httpx
import pytest

from polycopy.discovery._safe_get import _RequestBudget
from polycopy.discovery.adapter import DiscoveryAdapter
from polycopy.discovery.taxonomy_enricher import (
    TaxonomyEnricher,
)
from polycopy.taxonomy.official_polymarket import (
    TAXONOMY_CONFLICT,
    TAXONOMY_PARTIAL,
    TAXONOMY_UNAVAILABLE,
    TAXONOMY_USABLE,
    OfficialPolymarketTaxonomyResolverV1,
)


class _StubUnderlying:
    def __init__(self, transport):
        self._gamma_client = httpx.AsyncClient(base_url="https://gamma.example", transport=httpx.MockTransport(transport))

    async def _get_gamma_client(self):
        return self._gamma_client

    async def aclose(self):
        pass


def _bind_adapter(adapter: DiscoveryAdapter, transport) -> DiscoveryAdapter:
    adapter._underlying = _StubUnderlying(transport)  # type: ignore[attr-defined]
    adapter._owns_underlying = True
    return adapter


# --- B.1 embedded market category -----------------------------------------------


def test_embedded_market_category_is_usable_without_fetch() -> None:
    captured = []

    def h(req: httpx.Request) -> httpx.Response:
        captured.append(req.url.path)
        return httpx.Response(404)

    adapter = _bind_adapter(DiscoveryAdapter(), h)
    enricher = TaxonomyEnricher(adapter, budget=_RequestBudget(20))
    import asyncio
    out = asyncio.run(enricher.enrich_one({"conditionId": "0xabc", "category": "Sports"}))
    assert out.result.status == TAXONOMY_USABLE
    assert out.source_used in ("embedded_market_category",)
    assert captured == []  # no fetches


# --- B.2 market root tag fallback ---------------------------------------------


@pytest.mark.asyncio
async def test_market_root_tag_is_usable() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
    adapter = _bind_adapter(DiscoveryAdapter(), h)
    enricher = TaxonomyEnricher(adapter, budget=_RequestBudget(20))
    out = await enricher.enrich_one({"conditionId": "0xabc", "tags": [{"slug": "crypto", "label": "Crypto"}]})
    assert out.result.status == TAXONOMY_USABLE
    assert out.source_used == "embedded_market_root_tag"


# --- B.3 event fallback ---------------------------------------------------------


@pytest.mark.asyncio
async def test_event_fallback_when_market_lacks_category() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        if "/events" in req.url.path:
            return httpx.Response(200, content=json.dumps([{"id": "42", "category": "Weather"}]).encode(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(404)

    adapter = _bind_adapter(DiscoveryAdapter(), h)
    enricher = TaxonomyEnricher(adapter, budget=_RequestBudget(20))
    out = await enricher.enrich_one({
        "conditionId": "0xabc",
        "events": [{"id": "42"}],
    })
    assert out.result.status == TAXONOMY_USABLE
    assert "event" in out.source_used.lower()


# --- B.4 series fallback -------------------------------------------------------


@pytest.mark.asyncio
async def test_series_fallback_when_event_lacks_category() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        if "/series/" in req.url.path:
            return httpx.Response(200, content=json.dumps({"category": "Finance"}).encode(),
                                  headers={"content-type": "application/json"})
        if "/events" in req.url.path:
            return httpx.Response(200, content=json.dumps([{"id": "42", "category": "Politics"}]).encode(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(404)

    adapter = _bind_adapter(DiscoveryAdapter(), h)
    enricher = TaxonomyEnricher(adapter, budget=_RequestBudget(20))
    out = await enricher.enrich_one({
        "conditionId": "0xabc",
        "events": [{"id": "42", "category": "Sports"}],
        "series": [{"id": "7"}],
    })
    assert out.result.status == TAXONOMY_USABLE


# --- B.5 specific tag remains partial -----------------------------------------


@pytest.mark.asyncio
async def test_specific_tag_remains_partial_and_never_mapped() -> None:
    adapter = _bind_adapter(DiscoveryAdapter(), lambda r: httpx.Response(404))
    enricher = TaxonomyEnricher(adapter, budget=_RequestBudget(20))
    out = await enricher.enrich_one({"conditionId": "0xabc", "tags": [{"slug": "donald-trump", "label": "Donald Trump"}]})
    assert out.result.status == TAXONOMY_PARTIAL
    assert out.result.category_label is None


# --- B.6 conflicts fail closed ------------------------------------------------


@pytest.mark.asyncio
async def test_taxonomy_conflict_does_not_silently_pick() -> None:
    adapter = _bind_adapter(DiscoveryAdapter(), lambda r: httpx.Response(404))
    enricher = TaxonomyEnricher(adapter, budget=_RequestBudget(20))
    out = await enricher.enrich_one({"conditionId": "0xabc", "category": "Sports", "tags": [{"slug": "crypto"}]})
    assert out.result.status == TAXONOMY_CONFLICT
    assert out.result.category_label is None


# --- B.7 display text never used ----------------------------------------------


@pytest.mark.asyncio
async def test_display_text_never_becomes_category() -> None:
    adapter = _bind_adapter(DiscoveryAdapter(), lambda r: httpx.Response(404))
    enricher = TaxonomyEnricher(adapter, budget=_RequestBudget(20))
    out = await enricher.enrich_one({
        "conditionId": "0xabc",
        "title": "Sports", "question": "Sports?", "slug": "sports", "groupItemTitle": "Politics"
    })
    assert out.result.status == TAXONOMY_UNAVAILABLE


# --- B.8 duplicate tag dedupe -------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_tags_dedupe_deterministically() -> None:
    resolver = OfficialPolymarketTaxonomyResolverV1()
    payload = {"conditionId": "0xabc", "tags": [
        {"id": "2", "label": "Sports", "slug": "sports"},
        {"id": "2", "label": "Sports", "slug": "sports"},
    ]}
    a = resolver.resolve(payload).to_dict()
    b = resolver.resolve(dict(payload)).to_dict()
    assert a == b


# --- B.9 audit counter increments ---------------------------------------------


@pytest.mark.asyncio
async def test_audit_counters_increment() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
    adapter = _bind_adapter(DiscoveryAdapter(), h)
    enricher = TaxonomyEnricher(adapter, budget=_RequestBudget(20))

    # Market with embedded category → embedded_usable + successful
    await enricher.enrich_one({"conditionId": "0x1", "category": "Sports"})
    # Specific tag only → partial
    await enricher.enrich_one({"conditionId": "0x2", "tags": [{"slug": "specific"}]})
    # Conflict
    await enricher.enrich_one({"conditionId": "0x3", "category": "Sports", "tags": [{"slug": "crypto"}]})
    audit = enricher.audit()
    assert audit.embedded_usable >= 1
    assert audit.partial >= 1
    assert audit.conflict >= 1
    assert audit.markets_seen >= 3


# --- B.10 missing event/series → unavailable ----------------------------------


@pytest.mark.asyncio
async def test_missing_event_does_not_infer_category() -> None:
    adapter = _bind_adapter(DiscoveryAdapter(), lambda r: httpx.Response(404))
    enricher = TaxonomyEnricher(adapter, budget=_RequestBudget(20))
    out = await enricher.enrich_one({"conditionId": "0xabc", "tags": [{"slug": "specific"}]})
    assert out.result.status in (TAXONOMY_PARTIAL, TAXONOMY_UNAVAILABLE)
    assert out.result.category_label is None
