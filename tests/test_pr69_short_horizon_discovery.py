from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from polycopy.adapters.polymarket import PolymarketPublicAdapter
from polycopy.discovery.short_horizon_specialists import discover_short_horizon_specialists

UTC = timezone.utc
NOW = datetime(2026, 7, 14, 12, tzinfo=UTC)
WALLET = "0x" + "a" * 40
CONDITION = "0x" + "1" * 64


def test_pure_reconciliation_filters_at_trade_time_and_never_uses_exit_as_win() -> None:
    market = {"conditionId": CONDITION, "category": "Sports", "endDate": (NOW + timedelta(days=10)).isoformat()}
    trades = {CONDITION: [
        {"id": "win", "proxyWallet": WALLET, "timestamp": NOW.isoformat(), "resolution_status": "won", "is_winning_trade": 1, "realized_pnl": 2},
        {"id": "exit", "proxyWallet": WALLET, "timestamp": NOW.isoformat(), "side": "SELL", "redeemedAt": (NOW + timedelta(days=2)).isoformat()},

    ]}
    # Add a separate long market to show the filter is per trade's market/end.
    long_market = {"conditionId": "0x" + "2" * 64, "category": "Sports", "endDate": (NOW + timedelta(days=25)).isoformat()}
    trades[long_market["conditionId"]] = [{"id": "long", "proxyWallet": WALLET, "timestamp": NOW.isoformat()}]
    report = discover_short_horizon_specialists([market, long_market], trades, [{"proxyWallet": WALLET}], now=NOW)
    wallet = report.wallets[0]
    assert report.reconciled_trade_count == 2
    assert report.rejected["horizon:HORIZON_TOO_LONG"] == 1
    assert wallet["resolved_trades"] == 1  # early SELL was not promoted to a win
    assert wallet["wallet_verdict"] == "incomplete"


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
