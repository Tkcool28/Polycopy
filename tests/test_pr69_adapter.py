"""Section A — adapter tests."""
from __future__ import annotations

import json

import httpx
import pytest

from polycopy.discovery._safe_get import (
    ERR_BUDGET_EXHAUSTED,
    ERR_HTTP_4XX,
    ERR_HTTP_429,
    ERR_HTTP_5XX,
    ERR_INVALID_JSON,
    ERR_TIMEOUT,
    _RequestBudget,
    safe_get_json,
)
from polycopy.discovery.adapter import (
    DiscoveryAdapter,
)


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _async_mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


class _StubUnderlying:
    def __init__(self):
        self._gamma_client = None
        self._clob_client = None
        self._data_client = None

    async def _get_gamma_client(self):
        return self._gamma_client

    async def _get_clob_client(self):
        return self._clob_client

    async def _get_data_client(self):
        return self._data_client

    async def aclose(self):
        pass


def _bind_adapter(adapter: DiscoveryAdapter, *, gamma=None, clob=None, data=None) -> DiscoveryAdapter:
    under = _StubUnderlying()
    adapter._underlying = under  # type: ignore[attr-defined]
    adapter._owns_underlying = True  # avoid re-constructing in aclose
    if gamma:
        under._gamma_client = httpx.AsyncClient(base_url="https://gamma.example", transport=_async_mock(gamma))
    if clob:
        under._clob_client = httpx.AsyncClient(base_url="https://clob.example", transport=_async_mock(clob))
    if data:
        under._data_client = httpx.AsyncClient(base_url="https://data.example", transport=_async_mock(data))
    return adapter


def _error_handler(payload, status=200, headers=None):
    body = json.dumps(payload).encode()
    if status == 200:
        return httpx.Response(200, content=body, headers=headers or {})
    return httpx.Response(status, content=body, headers=headers or {})


# -----------------------------------------------------------------------------
# A.1 market pagination
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_active_markets_pages_and_truncates_at_limit() -> None:
    pages = [
        [{"conditionId": f"0x{i:064x}", "endDate": "2026-08-01T00:00:00Z", "tags": [], "clobTokenIds": ["1"]} for i in range(3)],
        [{"conditionId": f"0x{i:064x}", "endDate": "2026-08-01T00:00:00Z", "tags": [], "clobTokenIds": ["1"]} for i in range(3, 6)],
        [],
    ]
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        offset = int(req.url.params.get("offset", "0"))
        page_index = offset // 3
        if page_index >= len(pages):
            return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})
        return httpx.Response(200, content=json.dumps(pages[page_index]).encode(), headers={"content-type": "application/json"})

    adapter = _bind_adapter(DiscoveryAdapter(), gamma=handler)
    rows, errors = await adapter.list_active_markets(  # type: ignore[attr-defined]
        limit=10, offset=0, max_pages=5, page_size=3, budget=_RequestBudget(50)
    )
    assert errors == []
    assert len(rows) == 6
    assert call_count["n"] >= 2
    await adapter.aclose()


@pytest.mark.asyncio
async def test_list_active_markets_end_date_passes_through() -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(dict(req.url.params))
        return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})

    adapter = _bind_adapter(DiscoveryAdapter(), gamma=handler)
    await adapter.list_active_markets(  # type: ignore[attr-defined]
        end_date_min="2026-07-14", end_date_max="2026-08-07",
        tag_slug="sports",
        limit=10, offset=0, max_pages=1, page_size=10, budget=_RequestBudget(20),
    )
    assert captured.get("end_date_min") == "2026-07-14"
    assert captured.get("end_date_max") == "2026-08-07"
    assert captured.get("active") == "true"
    assert captured.get("closed") == "false"
    assert captured.get("tag_slug") == "sports"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_market_tags_returns_only_official_fields() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "/markets" in req.url.path:
            return httpx.Response(200, content=json.dumps([{"conditionId": "0xabc", "tags": [{"id": "1", "label": "Sports", "slug": "sports"}]}]).encode(), headers={"content-type": "application/json"})
        return httpx.Response(404)

    adapter = _bind_adapter(DiscoveryAdapter(), gamma=handler)
    tags = await adapter.get_market_tags("0xabc")  # type: ignore[attr-defined]
    assert tags == [{"id": "1", "label": "Sports", "slug": "sports"}]
    await adapter.aclose()


@pytest.mark.asyncio
async def test_event_lookup_and_event_tags() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "/events" in req.url.path:
            return httpx.Response(200, content=json.dumps([{"id": "123", "tags": [{"slug": "crypto", "label": "Crypto"}]}]).encode(), headers={"content-type": "application/json"})
        return httpx.Response(404)

    adapter = _bind_adapter(DiscoveryAdapter(), gamma=handler)
    event = await adapter.get_event_raw("123")  # type: ignore[attr-defined]
    assert event is not None and event["id"] == "123"
    tags = await adapter.get_event_tags("123")  # type: ignore[attr-defined]
    assert tags == [{"slug": "crypto", "label": "Crypto"}]
    await adapter.aclose()


@pytest.mark.asyncio
async def test_series_lookup_does_not_raise_on_404() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    adapter = _bind_adapter(DiscoveryAdapter(), gamma=handler)
    res = await adapter.get_series_raw("999")  # type: ignore[attr-defined]
    assert res is None
    await adapter.aclose()


# -----------------------------------------------------------------------------
# A.2 leaderboard combos
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaderboard_validates_enums() -> None:
    adapter = DiscoveryAdapter()
    with pytest.raises(ValueError):
        await adapter.get_public_leaderboard(category="bogus", time_period="WEEK", order_by="PNL")
    with pytest.raises(ValueError):
        await adapter.get_public_leaderboard(category="SPORTS", time_period="bogus", order_by="PNL")
    with pytest.raises(ValueError):
        await adapter.get_public_leaderboard(category="SPORTS", time_period="WEEK", order_by="bogus")


@pytest.mark.asyncio
async def test_leaderboard_sends_all_params() -> None:
    captured = {}
    body = [{"rank": 1, "proxyWallet": _body_wallet(), "pnl": 1.0}]

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(dict(req.url.params))
        return httpx.Response(200, content=json.dumps(body).encode(), headers={"content-type": "application/json"})

    adapter = _bind_adapter(DiscoveryAdapter(), data=handler)
    rows = await adapter.get_public_leaderboard(category="SPORTS", time_period="WEEK", order_by="PNL", limit=10)
    assert len(rows) == 1
    assert captured.get("category") == "SPORTS"
    assert captured.get("timePeriod") == "WEEK"
    assert captured.get("orderBy") == "PNL"
    assert captured.get("limit") == "10"
    await adapter.aclose()


# -----------------------------------------------------------------------------
# A.3 wallet trades / closed-positions / activity
# -----------------------------------------------------------------------------


def _body_wallet() -> str:
    return "0x" + "a" * 40


def _body_condition() -> str:
    return "0x" + "1" * 64


@pytest.mark.asyncio
async def test_wallet_trades_includes_maker_fills() -> None:
    captured = {}

    body = [{"proxyWallet": _body_wallet(), "side": "BUY", "conditionId": _body_condition(), "timestamp": "2026-07-14T00:00:00Z"}]

    def handler(req: httpx.Request) -> httpx.Response:
        if "/trades" in req.url.path and req.url.params.get("user"):
            captured.update(dict(req.url.params))
            return httpx.Response(200, content=json.dumps(body).encode(), headers={"content-type": "application/json"})
        return httpx.Response(404)

    adapter = _bind_adapter(DiscoveryAdapter(), data=handler)
    rows, errors = await adapter.wallet_trades(wallet_address=_body_wallet())
    assert len(rows) == 1
    assert captured.get("takerOnly") == "false"
    assert errors == []
    await adapter.aclose()


@pytest.mark.asyncio
async def test_wallet_trades_rejects_invalid_address() -> None:
    adapter = DiscoveryAdapter()
    with pytest.raises(ValueError):
        await adapter.wallet_trades(wallet_address="not-a-wallet")


@pytest.mark.asyncio
async def test_closed_positions_and_redeem_activity_filters() -> None:
    body1 = [{"user": _body_wallet(), "realizedPnl": 1.5, "conditionId": _body_condition()}]
    body2 = [{"type": "REDEEM"}]

    def handler(req: httpx.Request) -> httpx.Response:
        if "/closed-positions" in req.url.path:
            return httpx.Response(200, content=json.dumps(body1).encode(), headers={"content-type": "application/json"})
        if "/activity" in req.url.path:
            captured = req.url.params
            assert captured.get("type") == "REDEEM"
            return httpx.Response(200, content=json.dumps(body2).encode(), headers={"content-type": "application/json"})
        return httpx.Response(404)

    adapter = _bind_adapter(DiscoveryAdapter(), data=handler)
    pos, _ = await adapter.wallet_closed_positions(wallet_address=_body_wallet())
    assert len(pos) == 1 and pos[0]["realizedPnl"] == 1.5
    rows, _ = await adapter.wallet_redeem_activity(wallet_address=_body_wallet())
    assert len(rows) == 1
    await adapter.aclose()


# -----------------------------------------------------------------------------
# A.4 429 + retry-exhaustion + budget
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_get_429_honors_retry_after() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.05"}, content=b"")
        return httpx.Response(200, content=b'{"ok":1}', headers={"content-type": "application/json"})

    transport = _async_mock(handler)
    client = httpx.AsyncClient(base_url="https://x.test", transport=transport)
    budget = _RequestBudget(20)
    res = await safe_get_json(client, "/anything", budget=budget)
    assert res.error_code is None and res.data == {"ok": 1} and res.retries == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_safe_get_429_without_retry_after_returns_fail_closed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"")

    transport = _async_mock(handler)
    client = httpx.AsyncClient(base_url="https://x.test", transport=transport)
    budget = _RequestBudget(20)
    res = await safe_get_json(client, "/anything", budget=budget)
    assert res.status == 429 and res.error_code == ERR_HTTP_429
    await client.aclose()


@pytest.mark.asyncio
async def test_safe_get_5xx_then_success_retries() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, content=b"")
        return httpx.Response(200, content=b'{"ok":1}', headers={"content-type": "application/json"})

    transport = _async_mock(handler)
    client = httpx.AsyncClient(base_url="https://x.test", transport=transport)
    res = await safe_get_json(client, "/anything", budget=_RequestBudget(20), max_retries=2)
    assert res.error_code is None and res.retries == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_safe_get_5xx_retries_exhausted() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"")

    transport = _async_mock(handler)
    client = httpx.AsyncClient(base_url="https://x.test", transport=transport)
    res = await safe_get_json(client, "/anything", budget=_RequestBudget(20), max_retries=1)
    assert res.error_code == ERR_HTTP_5XX and res.status == 503
    await client.aclose()


@pytest.mark.asyncio
async def test_safe_get_budget_exhaustion() -> None:
    transport = _async_mock(lambda req: httpx.Response(200, content=b"{}"))
    client = httpx.AsyncClient(base_url="https://x.test", transport=transport)
    budget = _RequestBudget(1)
    a = await safe_get_json(client, "/anything", budget=budget)
    b = await safe_get_json(client, "/anything", budget=budget)
    assert a.error_code is None
    assert b.error_code == ERR_BUDGET_EXHAUSTED
    await client.aclose()


@pytest.mark.asyncio
async def test_safe_get_invalid_json_returns_fail_closed() -> None:
    transport = _async_mock(lambda req: httpx.Response(200, content=b"not-json"))
    client = httpx.AsyncClient(base_url="https://x.test", transport=transport)
    res = await safe_get_json(client, "/anything", budget=_RequestBudget(20))
    assert res.error_code == ERR_INVALID_JSON
    await client.aclose()


@pytest.mark.asyncio
async def test_safe_get_4xx_returns_fail_closed() -> None:
    transport = _async_mock(lambda req: httpx.Response(404, content=b'{"err":1}'))
    client = httpx.AsyncClient(base_url="https://x.test", transport=transport)
    res = await safe_get_json(client, "/anything", budget=_RequestBudget(20))
    assert res.error_code == ERR_HTTP_4XX
    await client.aclose()


@pytest.mark.asyncio
async def test_safe_get_timeout_returns_fail_closed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("boom")

    transport = _async_mock(handler)
    client = httpx.AsyncClient(base_url="https://x.test", transport=transport)
    res = await safe_get_json(client, "/anything", budget=_RequestBudget(20), max_retries=1)
    assert res.error_code == ERR_TIMEOUT
    await client.aclose()


# -----------------------------------------------------------------------------
# A.5 deterministic pagination
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pagination_terminates_when_page_short() -> None:
    pages = [
        [{"id": str(i)} for i in range(0, 5)],
        [],
    ]
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if "offset=0" in str(req.url):
            return httpx.Response(200, content=json.dumps(pages[0]).encode())
        return httpx.Response(200, content=b"[]")

    adapter = _bind_adapter(DiscoveryAdapter(), data=handler)
    rows, _ = await adapter.market_trades(condition_id="0x" + "1" * 64, limit=20, max_pages=5)
    assert calls["n"] == 1  # short page terminates
    assert len(rows) == 5
    await adapter.aclose()


# -----------------------------------------------------------------------------
# A.6 extract_wallet_address
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"proxyWallet": "0x" + "a" * 40}, "0x" + "a" * 40),
        ({"user": "0x" + "b" * 40}, "0x" + "b" * 40),
        ({"wallet": "0x" + "c" * 40}, "0x" + "c" * 40),
        ({"address": "0x" + "d" * 40}, "0x" + "d" * 40),
        ({"rank": 1}, None),
        ({"name": "John"}, None),
        ({"pseudonym": "anon"}, None),
        ({"proxyWallet": ""}, None),
        ({"proxyWallet": "not-a-wallet"}, None),
    ],
)
def test_extract_wallet_address_rejects_rank_and_name(row, expected):
    from polycopy.discovery.adapter import extract_wallet_address
    assert extract_wallet_address(row) == expected
