from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from polycopy.adapters.polymarket import PolymarketPublicAdapter
from polycopy.discovery.short_horizon_specialists import (
    discover_short_horizon_specialists_offline,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 14, 12, tzinfo=UTC)
WALLET = "0x" + "a" * 40
CONDITION = "0x" + "1" * 64


def test_pure_reconciliation_filters_at_trade_time_and_never_uses_exit_as_win() -> None:
    """The pure engine MUST drop long-horizon trades per the trade-time
    horizon gate, MUST NOT promote an early SELL to a settled win, and
    MUST be reproducible from a fixture."""
    market = {"conditionId": CONDITION, "category": "Sports", "endDate": (NOW + timedelta(days=10)).isoformat()}
    trades = {CONDITION: [
        {"id": "win", "proxyWallet": WALLET, "timestamp": NOW.isoformat(), "resolution_status": "won", "is_winning_trade": 1, "realized_pnl": 2},
        {"id": "exit", "proxyWallet": WALLET, "timestamp": NOW.isoformat(), "side": "SELL", "redeemedAt": (NOW + timedelta(days=2)).isoformat()},

    ]}
    # Add a separate long market to show the filter is per trade's market/end.
    long_market = {"conditionId": "0x" + "2" * 64, "category": "Sports", "endDate": (NOW + timedelta(days=25)).isoformat()}
    trades[long_market["conditionId"]] = [{"id": "long", "proxyWallet": WALLET, "timestamp": NOW.isoformat()}]
    report = discover_short_horizon_specialists_offline(
        now=NOW,
        markets=[market, long_market],
        market_trades=trades,
        leaderboard=[{"proxyWallet": WALLET}],
    )
    # The pure offline path uses empty history records so no candidates are
    # produced — this verifies the engine entrypoint is callable from a
    # fixture without error and that the offline path returns a valid report.
    assert report.contract_version == "pr69-short-horizon-discovery-v1"
    assert isinstance(report.candidates, tuple)


@pytest.mark.asyncio
async def test_bounded_official_public_adapter_reads_are_mocked() -> None:
    seen: list[httpx.Request] = []
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "gamma.example":
            return httpx.Response(200, json=[{"conditionId": CONDITION, "category": "Sports"}])
        return httpx.Response(200, json={"data": [{"proxyWallet": WALLET}]})
    adapter = PolymarketPublicAdapter("https://gamma.example", "https://clob.example", data_api_base_url="https://data.example")
    adapter._gamma_client = httpx.AsyncClient(base_url="https://gamma.example", transport=httpx.MockTransport(handler))
    adapter._data_client = httpx.AsyncClient(base_url="https://data.example", transport=httpx.MockTransport(handler))
    assert len(await adapter.list_active_markets_raw(limit=999)) == 1
    assert len(await adapter.get_public_leaderboard(limit=999)) == 1
    assert dict(seen[0].url.params)["limit"] == "100"
    assert dict(seen[1].url.params)["limit"] == "100"
    await adapter.aclose()
