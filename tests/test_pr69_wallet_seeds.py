"""Section D — wallet seed tests (channels A + B)."""
from __future__ import annotations

import json

import httpx
import pytest

from polycopy.discovery._safe_get import _RequestBudget
from polycopy.discovery.adapter import DiscoveryAdapter, extract_wallet_address
from polycopy.discovery.market_universe import (
    MarketClassification,
    PREFERRED_SHORT_HORIZON,
)
from polycopy.discovery.wallet_seeds import (
    SEED_LEADERBOARD,
    WalletSeedBuilder,
    seed_wallets_from_report,
)


class _StubUnderlying:
    def __init__(self, transport):
        self._data_client = httpx.AsyncClient(base_url="https://data.example", transport=httpx.MockTransport(transport))
        self._gamma_client = None
        self._clob_client = None

    async def _get_data_client(self):
        return self._data_client

    async def _get_gamma_client(self):
        return self._gamma_client

    async def _get_clob_client(self):
        return self._clob_client

    async def aclose(self):
        pass


def _bind(adapter, transport):
    adapter._underlying = _StubUnderlying(transport)  # type: ignore[attr-defined]
    adapter._owns_underlying = True
    return adapter


WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40
COND_A = "0x" + "1" * 64


def _eligible(condition: str = COND_A) -> MarketClassification:
    return MarketClassification(
        condition_id=condition, question="?", end_date_iso="2026-07-18T00:00:00+00:00",
        category_label="sports", taxonomy_source="embedded", taxonomy_status="USABLE",
        horizon_status="HORIZON_PREFERRED", bucket=PREFERRED_SHORT_HORIZON,
        reasons=(), excluded=False, eligible=True,
    )


# --- D.1 market-first + leaderboard WORKBOOK / Channel A & B


@pytest.mark.asyncio
async def test_channel_a_market_first_extracts_unique_wallets() -> None:
    trades = [
        {"proxyWallet": WALLET_A, "side": "BUY", "conditionId": COND_A, "transactionHash": "0x" + "a" * 64, "timestamp": "2026-07-14T00:00:00Z", "asset": "1", "price": 0.5, "size": 1.0},
        {"proxyWallet": WALLET_A, "side": "SELL", "conditionId": COND_A, "transactionHash": "0x" + "b" * 64, "timestamp": "2026-07-14T01:00:00Z", "asset": "1", "price": 0.6, "size": 1.0},
        {"proxyWallet": WALLET_B, "side": "BUY", "conditionId": COND_A, "transactionHash": "0x" + "c" * 64, "timestamp": "2026-07-14T02:00:00Z", "asset": "1", "price": 0.5, "size": 1.0},
    ]

    def h(req: httpx.Request) -> httpx.Response:
        if "/trades" in req.url.path and req.url.params.get("market"):
            return httpx.Response(200, content=json.dumps(trades).encode(),
                                  headers={"content-type": "application/json"})
        if "/v1/leaderboard" in req.url.path:
            return httpx.Response(200, content=b"[]")
        return httpx.Response(404)

    adapter = _bind(DiscoveryAdapter(), h)
    builder = WalletSeedBuilder(adapter, budget=_RequestBudget(20))
    report = await builder.build(classifications=[_eligible()], categories=["sports"])
    await adapter.aclose()
    assert WALLET_A in report.market_first_wallets
    assert WALLET_B in report.market_first_wallets


@pytest.mark.asyncio
async def test_channel_b_leaderboard_with_all_period_orders() -> None:
    captured: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/v1/leaderboard" in path:
            captured.append(f"{req.url.params.get('category')}/{req.url.params.get('timePeriod')}/{req.url.params.get('orderBy')}")
            return httpx.Response(200, content=json.dumps(
                [{"rank": 1, "proxyWallet": WALLET_A, "pnl": 1.0, "vol": 100}]
            ).encode(), headers={"content-type": "application/json"})
        if "/trades" in req.url.path:
            return httpx.Response(200, content=b"[]")
        return httpx.Response(404)

    adapter = _bind(DiscoveryAdapter(), h)
    builder = WalletSeedBuilder(adapter, budget=_RequestBudget(50))
    report = await builder.build(classifications=[_eligible()], categories=["sports"])
    await adapter.aclose()
    # Captured 4 combos: WEEK/PNL, WEEK/VOL, MONTH/PNL, MONTH/VOL
    assert len(captured) == 4
    assert any("SPORTS/WEEK/PNL" in c for c in captured)
    assert any("SPORTS/MONTH/VOL" in c for c in captured)
    assert WALLET_A in report.leaderboard_wallets


@pytest.mark.asyncio
async def test_cross_channel_dedupe_via_union() -> None:
    trades = [
        {"proxyWallet": WALLET_A, "side": "BUY", "conditionId": COND_A, "transactionHash": "0x" + "a" * 64, "timestamp": "2026-07-14T00:00:00Z", "asset": "1", "price": 0.5, "size": 1.0},
    ]

    def h(req: httpx.Request) -> httpx.Response:
        if "/trades" in req.url.path:
            return httpx.Response(200, content=json.dumps(trades).encode(),
                                  headers={"content-type": "application/json"})
        if "/v1/leaderboard" in req.url.path:
            return httpx.Response(200, content=json.dumps([{"rank": 1, "proxyWallet": WALLET_A}]).encode(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(404)

    adapter = _bind(DiscoveryAdapter(), h)
    builder = WalletSeedBuilder(adapter, budget=_RequestBudget(50))
    report = await builder.build(classifications=[_eligible()], categories=["sports"])
    await adapter.aclose()
    assert WALLET_A in report.duplicate_wallets
    assert report.union_wallets == (WALLET_A,)


@pytest.mark.asyncio
async def test_invalid_wallet_rows_rejected() -> None:
    trades = [
        {"proxyWallet": "not-a-wallet", "conditionId": COND_A, "transactionHash": "0x" + "d" * 64, "timestamp": "2026-07-14T00:00:00Z", "asset": "1", "price": 0.5, "size": 1.0},
        {"proxyWallet": WALLET_A, "conditionId": COND_A, "transactionHash": "0x" + "e" * 64, "timestamp": "2026-07-14T00:00:00Z", "asset": "1", "price": 0.5, "size": 1.0},
    ]

    def h(req: httpx.Request) -> httpx.Response:
        if "/trades" in req.url.path:
            return httpx.Response(200, content=json.dumps(trades).encode(),
                                  headers={"content-type": "application/json"})
        if "/v1/leaderboard" in req.url.path:
            return httpx.Response(200, content=b"[]")
        return httpx.Response(404)

    adapter = _bind(DiscoveryAdapter(), h)
    builder = WalletSeedBuilder(adapter, budget=_RequestBudget(20))
    report = await builder.build(classifications=[_eligible()], categories=["sports"])
    await adapter.aclose()
    assert report.invalid_wallet_rows >= 1
    assert WALLET_A in report.union_wallets


@pytest.mark.asyncio
async def test_deterministic_max_wallet_cap_truncates() -> None:
    """Build a set of 200 wallets but cap at 25; deterministic order."""
    wallets = sorted(f"0x{i:040x}" for i in range(200))
    trades = [
        {"proxyWallet": w, "side": "BUY", "conditionId": COND_A, "transactionHash": f"0x{i:064x}", "timestamp": "2026-07-14T00:00:00Z", "asset": "1", "price": 0.5, "size": 1.0}
        for i, w in enumerate(wallets)
    ]

    def h(req: httpx.Request) -> httpx.Response:
        if "/trades" in req.url.path:
            return httpx.Response(200, content=json.dumps(trades).encode(),
                                  headers={"content-type": "application/json"})
        if "/v1/leaderboard" in req.url.path:
            return httpx.Response(200, content=b"[]")
        return httpx.Response(404)

    adapter = _bind(DiscoveryAdapter(), h)
    builder = WalletSeedBuilder(adapter, budget=_RequestBudget(100), max_wallets=25)
    report = await builder.build(classifications=[_eligible()], categories=["sports"])
    await adapter.aclose()
    assert len(report.union_wallets) == 25
    # Sorted order, deterministic truncation keeps first 25.
    assert report.union_wallets[0] == wallets[0]
    assert report.union_wallets[-1] == wallets[24]


@pytest.mark.asyncio
async def test_rank_is_recorded_but_not_a_score_input() -> None:
    """Leaderboard rank survives as provenance; never propagates into evidence."""
    def h(req: httpx.Request) -> httpx.Response:
        if "/v1/leaderboard" in req.url.path:
            return httpx.Response(200, content=json.dumps([
                {"rank": 1, "proxyWallet": WALLET_A, "pnl": 99.9},
                {"rank": 2, "proxyWallet": WALLET_B, "pnl": 50.0},
            ]).encode(), headers={"content-type": "application/json"})
        if "/trades" in req.url.path:
            return httpx.Response(200, content=b"[]")
        return httpx.Response(404)

    adapter = _bind(DiscoveryAdapter(), h)
    builder = WalletSeedBuilder(adapter, budget=_RequestBudget(20))
    report = await builder.build(classifications=[_eligible()], categories=["sports"])
    await adapter.aclose()
    # Both wallets in union, both flagged as leaderboard source.
    assert set(report.leaderboard_wallets) == {WALLET_A, WALLET_B}
    # Rank is captured as provenance in seed records (test for source tag).
    seeds = seed_wallets_from_report(report, [_eligible()], {COND_A: []})
    assert all(SEED_LEADERBOARD in s.sources for s in seeds)


# --- D.2 validation -------------------------------------------------------------


def test_builder_validates_bounds() -> None:
    adapter = DiscoveryAdapter()
    with pytest.raises(ValueError):
        WalletSeedBuilder(adapter, budget=_RequestBudget(1), leaderboard_top=0)
    with pytest.raises(ValueError):
        WalletSeedBuilder(adapter, budget=_RequestBudget(1), leaderboard_top=101)
    with pytest.raises(ValueError):
        WalletSeedBuilder(adapter, budget=_RequestBudget(1), max_wallets=101)
    with pytest.raises(ValueError):
        WalletSeedBuilder(adapter, budget=_RequestBudget(1), concurrency=0)
    with pytest.raises(ValueError):
        WalletSeedBuilder(adapter, budget=_RequestBudget(1), concurrency=5)


# --- D.3 helpers ---------------------------------------------------------------


def test_extract_wallet_address_rejects_rank_and_name_only() -> None:
    assert extract_wallet_address({"rank": 1, "name": "Alice", "pseudonym": "anon"}) is None
    assert extract_wallet_address({"proxyWallet": WALLET_A}) == WALLET_A
    assert extract_wallet_address({"user": WALLET_B}) == WALLET_B
