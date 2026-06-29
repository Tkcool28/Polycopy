"""Regression tests for PR #3 per-market Data API trade ingestion.

These tests pin the round-7 ingestion contract: live trade collection must use
``GET /trades?market=<conditionId>`` with bounded pagination instead of relying
on a broad all-market window/cache.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

import sys

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from polycopy.adapters.polymarket import PolymarketPublicAdapter  # noqa: E402


def _raw_trade(market: str, suffix: str, ts: int = 1_782_636_254) -> dict:
    return {
        "proxyWallet": "0x1111111111111111111111111111111111111111",
        "side": "BUY",
        "asset": f"asset-{suffix}",
        "conditionId": market,
        "size": 1.0,
        "price": 0.42,
        "timestamp": ts,
        "outcome": "Yes",
        "transactionHash": f"0x{suffix:0>8}",
    }


def _adapter(handler) -> PolymarketPublicAdapter:
    adapter = PolymarketPublicAdapter(
        gamma_base_url="https://gamma.example.test",
        clob_base_url="https://clob.example.test",
        data_api_base_url="https://data-api.example.test",
        timeout=5.0,
        data_api_request_interval_seconds=0.0,
    )
    adapter._data_client = httpx.AsyncClient(  # noqa: SLF001 - intentional test wiring
        base_url=adapter.data_api_base_url,
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )
    return adapter


@pytest.mark.asyncio
async def test_fetch_trades_for_market_sends_market_param_and_paginates_offsets():
    """A full page must advance to the next offset and keep the same market filter."""
    requests: list[dict[str, str]] = []
    market = "0xMARKET_A"
    pages = {
        0: [_raw_trade(market, "1"), _raw_trade(market, "2")],
        2: [_raw_trade(market, "3")],  # short page terminates pagination
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(str(request.url.query, "utf-8"))
        requests.append({k: v[0] for k, v in qs.items()})
        assert qs["market"] == [market]
        assert qs["limit"] == ["2"]
        offset = int(qs["offset"][0])
        return httpx.Response(200, json=pages[offset])

    adapter = _adapter(handler)
    try:
        trades = await adapter.fetch_trades_for_market(market, limit=2, max_pages=5)
    finally:
        await adapter.aclose()

    assert [r["offset"] for r in requests] == ["0", "2"]
    assert len(trades) == 3
    assert {t.market_source_id for t in trades} == {market}


@pytest.mark.asyncio
async def test_fetch_trades_for_market_does_not_use_global_window_cache_between_markets():
    """Each market fetch must issue its own request with its own market parameter."""
    requested_markets: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(str(request.url.query, "utf-8"))
        requested_market = qs["market"][0]
        requested_markets.append(requested_market)
        return httpx.Response(200, json=[_raw_trade(requested_market, requested_market[-1])])

    adapter = _adapter(handler)
    try:
        trades_a = await adapter.fetch_trades_for_market("0xMARKET_A", limit=10)
        trades_b = await adapter.fetch_trades_for_market("0xMARKET_B", limit=10)
    finally:
        await adapter.aclose()

    assert requested_markets == ["0xMARKET_A", "0xMARKET_B"]
    assert [t.market_source_id for t in trades_a] == ["0xMARKET_A"]
    assert [t.market_source_id for t in trades_b] == ["0xMARKET_B"]


@pytest.mark.asyncio
async def test_fetch_trades_for_market_filters_stray_rows_and_stops_at_max_pages():
    """Client-side filtering remains a guardrail; max_pages bounds full-page loops."""
    calls = 0
    market = "0xMARKET_A"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        qs = parse_qs(str(request.url.query, "utf-8"))
        assert qs["market"] == [market]
        # Always return a full page, including a stray row from another market.
        return httpx.Response(
            200,
            json=[
                _raw_trade(market, f"{calls}a"),
                _raw_trade("0xOTHER_MARKET", f"{calls}b"),
            ],
        )

    adapter = _adapter(handler)
    try:
        trades = await adapter.fetch_trades_for_market(
            market,
            since=datetime.fromtimestamp(0, tz=timezone.utc),
            limit=2,
            max_pages=3,
        )
    finally:
        await adapter.aclose()

    assert calls == 3
    assert len(trades) == 3
    assert all(t.market_source_id == market for t in trades)


@pytest.mark.asyncio
async def test_fetch_trades_for_market_honors_max_rows_on_short_page():
    """max_rows is a hard cap even when the first response is a short page."""
    market = "0xMARKET_A"

    async def handler(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(str(request.url.query, "utf-8"))
        assert qs["market"] == [market]
        # Three rows is a short page for limit=10, but max_rows=2 must still cap it.
        return httpx.Response(
            200,
            json=[
                _raw_trade(market, "1"),
                _raw_trade(market, "2"),
                _raw_trade(market, "3"),
            ],
        )

    adapter = _adapter(handler)
    try:
        trades = await adapter.fetch_trades_for_market(market, limit=10, max_rows=2)
    finally:
        await adapter.aclose()

    assert len(trades) == 2
    assert all(trade.market_source_id == market for trade in trades)
    assert len({trade.source_trade_id for trade in trades}) == 2
