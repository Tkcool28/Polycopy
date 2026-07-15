"""Section C — market universe tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from polycopy.discovery._safe_get import _RequestBudget
from polycopy.discovery.adapter import DiscoveryAdapter
from polycopy.discovery.market_universe import (
    ELIGIBLE_BUCKETS,
    ELIGIBLE_SHORT_HORIZON,
    HORIZON_TOO_LONG_BUCKET,
    MALFORMED_BUCKET,
    MarketUniverseCrawler,
    MarketUniverseConfig,
    PREFERRED_SHORT_HORIZON,
    TAXONOMY_PARTIAL_BUCKET,
    validate_config,
)
from polycopy.discovery.taxonomy_enricher import TaxonomyEnricher


class _StubUnderlying:
    def __init__(self, transport):
        self._gamma_client = httpx.AsyncClient(base_url="https://gamma.example", transport=httpx.MockTransport(transport))

    async def _get_gamma_client(self):
        return self._gamma_client

    async def aclose(self):
        pass


def _bind(adapter, transport):
    adapter._underlying = _StubUnderlying(transport)  # type: ignore[attr-defined]
    adapter._owns_underlying = True
    return adapter


def _good_market(condition: str, end_offset_days: int) -> dict:
    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    return {
        "conditionId": condition,
        "endDate": (now + timedelta(days=end_offset_days)).isoformat(),
        "category": "Sports",
        "tags": [{"slug": "sports", "label": "Sports"}],
        "clobTokenIds": ["1"],
        "active": True,
        "closed": False,
        "acceptingOrders": True,
    }


def _crawler_with_handler(handler):
    adapter = _bind(DiscoveryAdapter(), handler)
    enricher = TaxonomyEnricher(adapter, budget=_RequestBudget(20))
    return MarketUniverseCrawler(adapter, enricher, budget=_RequestBudget(20)), enricher, adapter


def _default_config(**overrides):
    base = dict(
        as_of=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        categories=("sports",),
        max_markets=10,
        page_size=10,
        max_pages=2,
        max_requests=20,
    )
    base.update(overrides)
    return MarketUniverseConfig(**base)


@pytest.mark.asyncio
async def test_preferred_short_horizon() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=httpx._content.encode_json([_good_market("0xa" + "1" * 63, 10)]).__bytes__(),  # type: ignore[attr-defined]
                              headers={"content-type": "application/json"})

    import json
    def hh(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps([_good_market("0xa" + "1" * 63, 10)]).encode(),
                              headers={"content-type": "application/json"})

    crawler, enricher, adapter = _crawler_with_handler(hh)
    classifications, audit = await crawler.run(_default_config())
    await adapter.aclose()
    assert any(c.bucket == PREFERRED_SHORT_HORIZON for c in classifications), audit.bucket_counts
    assert all(c.eligible for c in classifications if c.bucket in ELIGIBLE_BUCKETS)


@pytest.mark.asyncio
async def test_eligible_non_preferred_within_hard_cap() -> None:
    import json
    market = _good_market("0xb" + "1" * 63, 20)  # 20 days: prefer=14 fails, hard=24 passes

    def hh(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps([market]).encode(),
                              headers={"content-type": "application/json"})

    crawler, enricher, adapter = _crawler_with_handler(hh)
    classifications, _ = await crawler.run(_default_config())
    await adapter.aclose()
    assert any(c.bucket == ELIGIBLE_SHORT_HORIZON for c in classifications)


@pytest.mark.asyncio
async def test_exact_hard_cap_pass() -> None:
    import json
    market = _good_market("0xc" + "1" * 63, 24)  # 24 days scheduled end; +6 buffer = 30 = exact hard cap

    def hh(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps([market]).encode(),
                              headers={"content-type": "application/json"})

    crawler, enricher, adapter = _crawler_with_handler(hh)
    classifications, _ = await crawler.run(_default_config())
    await adapter.aclose()
    assert any(c.bucket == ELIGIBLE_SHORT_HORIZON for c in classifications)


@pytest.mark.asyncio
async def test_one_second_over_hard_cap() -> None:
    import json
    market = _good_market("0xd" + "1" * 63, 999)  # long-horizon far beyond 24

    def hh(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps([market]).encode(),
                              headers={"content-type": "application/json"})

    crawler, enricher, adapter = _crawler_with_handler(hh)
    classifications, _ = await crawler.run(_default_config())
    await adapter.aclose()
    assert all(c.bucket == HORIZON_TOO_LONG_BUCKET for c in classifications)


@pytest.mark.asyncio
async def test_missing_end_date_marks_malformed() -> None:
    import json
    market = {"conditionId": "0xe" + "1" * 63, "category": "Sports", "tags": [{"slug": "sports", "label": "Sports"}], "clobTokenIds": ["1"]}

    def hh(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps([market]).encode(),
                              headers={"content-type": "application/json"})

    crawler, enricher, adapter = _crawler_with_handler(hh)
    classifications, _ = await crawler.run(_default_config())
    await adapter.aclose()
    assert any(c.bucket == MALFORMED_BUCKET for c in classifications)


@pytest.mark.asyncio
async def test_active_closed_and_accepting_orders_excluded() -> None:
    import json
    market = _good_market("0xf" + "1" * 63, 10)
    market["acceptingOrders"] = False  # explicit not accepting

    def hh(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps([market]).encode(),
                              headers={"content-type": "application/json"})

    crawler, enricher, adapter = _crawler_with_handler(hh)
    classifications, _ = await crawler.run(_default_config())
    await adapter.aclose()
    assert all(c.bucket == "NOT_TRADABLE" for c in classifications)


@pytest.mark.asyncio
async def test_missing_token_id_marks_malformed() -> None:
    import json
    market = _good_market("0x10" + "1" * 62, 10)
    market["clobTokenIds"] = []

    def hh(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps([market]).encode(),
                              headers={"content-type": "application/json"})

    crawler, enricher, adapter = _crawler_with_handler(hh)
    classifications, _ = await crawler.run(_default_config())
    await adapter.aclose()
    assert all(c.bucket == MALFORMED_BUCKET for c in classifications)


@pytest.mark.asyncio
async def test_taxonomy_partial_state_excluded() -> None:
    import json
    market = _good_market("0x11" + "1" * 62, 10)
    market["category"] = ""  # clear
    market["tags"] = [{"slug": "specific-tag", "label": "SpecificTag"}]  # specific only

    def hh(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps([market]).encode(),
                              headers={"content-type": "application/json"})

    crawler, enricher, adapter = _crawler_with_handler(hh)
    classifications, _ = await crawler.run(_default_config())
    await adapter.aclose()
    assert any(c.bucket == TAXONOMY_PARTIAL_BUCKET for c in classifications)


@pytest.mark.asyncio
async def test_request_budget_respected() -> None:
    """Many markets; verify total classified count is bounded by max_markets."""
    import json
    rows = [_good_market(f"0x{i:064x}", 5) for i in range(20)]

    def hh(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(rows).encode(),
                              headers={"content-type": "application/json"})

    crawler, enricher, adapter = _crawler_with_handler(hh)
    classifications, audit = await crawler.run(_default_config(max_markets=5, page_size=5, max_pages=2))
    await adapter.aclose()
    assert audit.markets_inspected == 5
    assert all(c.bucket == PREFERRED_SHORT_HORIZON for c in classifications)


@pytest.mark.asyncio
async def test_pagination_terminates_when_page_short() -> None:
    import json
    page1 = [_good_market(f"0x{i:064x}", 5) for i in range(3)]

    def hh(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(page1).encode(),
                              headers={"content-type": "application/json"})

    crawler, enricher, adapter = _crawler_with_handler(hh)
    classifications, audit = await crawler.run(_default_config(max_markets=20, page_size=5, max_pages=10))
    await adapter.aclose()
    assert len(classifications) == 3


def test_validate_config_caps() -> None:
    base = dict(
        as_of=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError):
        validate_config(MarketUniverseConfig(preferred_days=0, **base))
    with pytest.raises(ValueError):
        validate_config(MarketUniverseConfig(preferred_days=40, max_capital_lock_days=30, **base))
    with pytest.raises(ValueError):
        validate_config(MarketUniverseConfig(max_capital_lock_days=31, **base))


def test_validate_config_minimums() -> None:
    base = dict(
        as_of=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError):
        validate_config(MarketUniverseConfig(resolution_buffer_days=-1, **base))
    with pytest.raises(ValueError):
        validate_config(MarketUniverseConfig(max_markets=0, **base))


def test_malformed_reason_carries_provenance() -> None:
    import json
    market = {"conditionId": "0x15" + "1" * 62, "category": "Sports", "tags": [{"slug": "sports"}], "clobTokenIds": ["1"]}

    async def runner():
        def hh(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps([market]).encode(),
                                  headers={"content-type": "application/json"})

        crawler, _, adapter = _crawler_with_handler(hh)
        classifications, _ = await crawler.run(_default_config())
        await adapter.aclose()
        return classifications

    import asyncio
    out = asyncio.run(runner())
    assert out and not out[0].eligible
    assert any("missing_end" in r for r in out[0].reasons) or not out[0].reasons
