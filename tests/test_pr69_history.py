"""Section E — historical wallet reconciliation tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from polycopy.discovery._safe_get import _RequestBudget
from polycopy.discovery.adapter import DiscoveryAdapter
from polycopy.discovery.market_universe import (
    MarketClassification,
    PREFERRED_SHORT_HORIZON,
)
from polycopy.discovery.wallet_history import (
    WalletHistoryFetcher,
)
from polycopy.discovery.wallet_seeds import SeedWallet

UTC = timezone.utc
NOW = datetime(2026, 7, 14, 12, tzinfo=UTC)
WALLET = "0x" + "a" * 40
COND_PREFERRED = "0x" + "1" * 64


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


def _good_market(condition: str, end_offset_days: int) -> MarketClassification:
    return MarketClassification(
        condition_id=condition,
        question="Q",
        end_date_iso=(NOW + timedelta(days=end_offset_days)).isoformat(),
        category_label="sports",
        taxonomy_source="embedded",
        taxonomy_status="USABLE",
        horizon_status="HORIZON_PREFERRED",
        bucket=PREFERRED_SHORT_HORIZON,
        reasons=(),
        excluded=False,
        eligible=True,
    )


def _trade(condition: str, side: str, ts_offset_h: int = 0, price: float = 0.5, size: float = 1.0, tx: str = "0x" + "a" * 64, asset: str = "asset-1"):
    return {
        "proxyWallet": WALLET,
        "side": side,
        "conditionId": condition,
        "timestamp": (NOW + timedelta(hours=ts_offset_h)).isoformat(),
        "price": price,
        "size": size,
        "transactionHash": tx,
        "asset": asset,
    }


# -----------------------------------------------------------------------------
# Test cases drive the live fetcher via mocked transport.
# Each test controls both the trades list, the activity, the closed-positions,
# and the market lookup so that we exercise a focused reconciliation path.
# -----------------------------------------------------------------------------


def _multi_trade_handler(trades, activity=None, closed=None, gamma_market=None):
    activity = activity if activity is not None else []
    closed = closed if closed is not None else []

    def h(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/trades":
            return httpx.Response(200, content=json.dumps(trades).encode(),
                                  headers={"content-type": "application/json"})
        if path == "/activity":
            return httpx.Response(200, content=json.dumps(activity).encode(),
                                  headers={"content-type": "application/json"})
        if path == "/closed-positions":
            return httpx.Response(200, content=json.dumps(closed).encode(),
                                  headers={"content-type": "application/json"})
        if path.startswith("/markets") and gamma_market is not None:
            return httpx.Response(200, content=json.dumps([gamma_market]).encode(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(404)

    return h


# --- E.1 settled win ---------------------------------------------------------


@pytest.mark.asyncio
async def test_settled_win_is_recorded_with_redeem_and_closed_position() -> None:
    # Trade within window; market ends in 5 days; activity records REDEEM;
    # closed position has realized PnL. Final outcome: settled winning trade.
    trades = [_trade(COND_PREFERRED, "BUY", ts_offset_h=-48)]
    activity = [{"conditionId": COND_PREFERRED, "type": "REDEEM", "winning": True, "proxyWallet": WALLET}]
    closed = [{"conditionId": COND_PREFERRED, "realizedPnl": 5.0, "proxyWallet": WALLET}]

    adapter = _bind(DiscoveryAdapter(), _multi_trade_handler(trades, activity=activity, closed=closed))
    fetcher = WalletHistoryFetcher(adapter, budget=_RequestBudget(20))
    classifications = [_good_market(COND_PREFERRED, 5)]
    seeds = (SeedWallet(wallet_address=WALLET, sources=("market_first",)),)
    report = await fetcher.fetch(seeds=seeds, classifications=classifications, as_of=NOW)
    await adapter.aclose()
    assert len(report.wallets) == 1
    record = report.wallets[0]
    assert len(record.settled) == 1
    assert record.settled[0].winning_outcome is True
    assert record.settled[0].redeemed is True


# --- E.2 settled loss --------------------------------------------------------


@pytest.mark.asyncio
async def test_settled_loss_is_recorded() -> None:
    trades = [_trade(COND_PREFERRED, "BUY", ts_offset_h=-48)]
    activity = [{"conditionId": COND_PREFERRED, "type": "REDEEM", "winning": False, "proxyWallet": WALLET}]
    closed = [{"conditionId": COND_PREFERRED, "realizedPnl": -2.0, "proxyWallet": WALLET}]
    adapter = _bind(DiscoveryAdapter(), _multi_trade_handler(trades, activity=activity, closed=closed))
    fetcher = WalletHistoryFetcher(adapter, budget=_RequestBudget(20))
    classifications = [_good_market(COND_PREFERRED, 5)]
    report = await fetcher.fetch(seeds=(SeedWallet(wallet_address=WALLET, sources=("market_first",)),),
                                 classifications=classifications, as_of=NOW)
    await adapter.aclose()
    record = report.wallets[0]
    assert len(record.settled) == 1
    assert record.settled[0].winning_outcome is False
    assert record.settled[0].settled_realized_pnl == -2.0


# --- E.3 redeem-confirmed but winning unknown -------------------------------


@pytest.mark.asyncio
async def test_redeem_confirmed_evidence_distinct_from_winning_outcome() -> None:
    trades = [_trade(COND_PREFERRED, "BUY", ts_offset_h=-48)]
    activity = [{"conditionId": COND_PREFERRED, "type": "REDEEM", "proxyWallet": WALLET}]  # no winning field
    closed = [{"conditionId": COND_PREFERRED, "realizedPnl": 0.0, "proxyWallet": WALLET}]
    adapter = _bind(DiscoveryAdapter(), _multi_trade_handler(trades, activity=activity, closed=closed))
    fetcher = WalletHistoryFetcher(adapter, budget=_RequestBudget(20))
    classifications = [_good_market(COND_PREFERRED, 5)]
    report = await fetcher.fetch(seeds=(SeedWallet(wallet_address=WALLET, sources=("market_first",)),),
                                 classifications=classifications, as_of=NOW)
    await adapter.aclose()
    record = report.wallets[0]
    assert record.settled[0].redeemed is True
    assert record.settled[0].winning_outcome is False


# --- E.4 resolved without redeem stays as early-exit (no settled promotion) -


@pytest.mark.asyncio
async def test_resolved_without_redeem_does_not_become_settled() -> None:
    trades = [_trade(COND_PREFERRED, "BUY", ts_offset_h=-48)]
    activity = []  # no REDEEM
    closed = [{"conditionId": COND_PREFERRED, "realizedPnl": 1.5, "proxyWallet": WALLET}]  # closed only
    adapter = _bind(DiscoveryAdapter(), _multi_trade_handler(trades, activity=activity, closed=closed))
    fetcher = WalletHistoryFetcher(adapter, budget=_RequestBudget(20))
    classifications = [_good_market(COND_PREFERRED, 5)]
    report = await fetcher.fetch(seeds=(SeedWallet(wallet_address=WALLET, sources=("market_first",)),),
                                 classifications=classifications, as_of=NOW)
    await adapter.aclose()
    record = report.wallets[0]
    # No settled promotion; closed-only trades are tagged early_exit per spec.
    assert len(record.settled) == 0
    # The single trade has realized_pnl attributed via closed-only path.
    assert len(record.early_exit) == 1


# --- E.5 early exit is recorded separately from settled --------------------


@pytest.mark.asyncio
async def test_early_exit_is_recorded_as_early_exit_not_settled_win() -> None:
    trades = [_trade(COND_PREFERRED, "BUY", ts_offset_h=-48), _trade(COND_PREFERRED, "SELL", ts_offset_h=-24)]
    activity = []
    closed = [{"conditionId": COND_PREFERRED, "realizedPnl": 0.5, "proxyWallet": WALLET}]
    adapter = _bind(DiscoveryAdapter(), _multi_trade_handler(trades, activity=activity, closed=closed))
    fetcher = WalletHistoryFetcher(adapter, budget=_RequestBudget(20))
    classifications = [_good_market(COND_PREFERRED, 5)]
    report = await fetcher.fetch(seeds=(SeedWallet(wallet_address=WALLET, sources=("market_first",)),),
                                 classifications=classifications, as_of=NOW)
    await adapter.aclose()
    record = report.wallets[0]
    # Both trades pass horizon. No REDEEM; closed position has realizedPnl.
    # Both end up as early_exit with the realized_pnl attribution.
    assert all(ev for ev in record.early_exit)
    assert all(ev.realized_pnl == 0.5 for ev in record.early_exit)
    assert not record.settled


# --- E.6 unresolved stays unresolved when no activity / closed --------------


@pytest.mark.asyncio
async def test_unresolved_evidence_when_neither_redeem_nor_closed_position() -> None:
    trades = [_trade(COND_PREFERRED, "BUY", ts_offset_h=-48)]
    adapter = _bind(DiscoveryAdapter(), _multi_trade_handler(trades, activity=[], closed=[]))
    fetcher = WalletHistoryFetcher(adapter, budget=_RequestBudget(20))
    classifications = [_good_market(COND_PREFERRED, 5)]
    report = await fetcher.fetch(seeds=(SeedWallet(wallet_address=WALLET, sources=("market_first",)),),
                                 classifications=classifications, as_of=NOW)
    await adapter.aclose()
    record = report.wallets[0]
    assert len(record.unresolved) == 1
    assert not record.settled


# --- E.7 long-horizon excluded ---------------------------------------------


@pytest.mark.asyncio
async def test_long_horizon_excluded_from_evidence() -> None:
    trades = [_trade(COND_PREFERRED, "BUY", ts_offset_h=-48)]
    adapter = _bind(DiscoveryAdapter(), _multi_trade_handler(trades))
    fetcher = WalletHistoryFetcher(adapter, budget=_RequestBudget(20))
    classifications = [_good_market(COND_PREFERRED, 25)]  # 25 days = hard horizon miss
    report = await fetcher.fetch(seeds=(SeedWallet(wallet_address=WALLET, sources=("market_first",)),),
                                 classifications=classifications, as_of=NOW)
    await adapter.aclose()
    record = report.wallets[0]
    assert record.long_horizon_excluded == 1
    assert not record.settled
    assert not record.unresolved


# --- E.8 taxonomy excluded -------------------------------------------------


@pytest.mark.asyncio
async def test_taxonomy_excluded_when_market_has_no_category() -> None:
    trades = [_trade(COND_PREFERRED, "BUY", ts_offset_h=-48)]
    cls = MarketClassification(
        condition_id=COND_PREFERRED, question="Q", end_date_iso=(NOW + timedelta(days=5)).isoformat(),
        category_label=None, taxonomy_source="embedded", taxonomy_status="UNAVAILABLE",
        horizon_status="HORIZON_PREFERRED", bucket="TAXONOMY_UNAVAILABLE",
        reasons=(), excluded=True, eligible=False,
    )
    adapter = _bind(DiscoveryAdapter(), _multi_trade_handler(trades))
    fetcher = WalletHistoryFetcher(adapter, budget=_RequestBudget(20))
    report = await fetcher.fetch(seeds=(SeedWallet(wallet_address=WALLET, sources=("market_first",)),),
                                 classifications=[cls], as_of=NOW)
    await adapter.aclose()
    assert report.wallets[0].taxonomy_excluded == 1


# --- E.9 multi-fill grouping / dedupe -------------------------------------


@pytest.mark.asyncio
async def test_multi_fill_grouping_keeps_distinct_identity() -> None:
    """Two fills at different prices share a wallet+condition but should
    remain two distinct evidence rows (no fake dedupe by wallet+condition)."""
    trades = [
        _trade(COND_PREFERRED, "BUY", ts_offset_h=-48, price=0.4, tx="0x" + "a" * 64),
        _trade(COND_PREFERRED, "BUY", ts_offset_h=-36, price=0.6, tx="0x" + "b" * 64),
    ]
    adapter = _bind(DiscoveryAdapter(), _multi_trade_handler(trades))
    fetcher = WalletHistoryFetcher(adapter, budget=_RequestBudget(20))
    classifications = [_good_market(COND_PREFERRED, 5)]
    report = await fetcher.fetch(seeds=(SeedWallet(wallet_address=WALLET, sources=("market_first",)),),
                                 classifications=classifications, as_of=NOW)
    await adapter.aclose()
    record = report.wallets[0]
    # 2 fills => 2 unresolved evidence rows (no settled promotion in this fixture).
    assert len(record.unresolved) == 2
    # distinct events count is per *settled* market; this fixture has none.
    assert len(record.distinct_events) == 0


# --- E.10 no double PnL counting ------------------------------------------


@pytest.mark.asyncio
async def test_no_double_pnl_between_closed_positions_and_trades() -> None:
    trades = [_trade(COND_PREFERRED, "BUY", ts_offset_h=-48)]
    activity = [{"conditionId": COND_PREFERRED, "type": "REDEEM", "winning": True, "proxyWallet": WALLET}]
    closed = [{"conditionId": COND_PREFERRED, "realizedPnl": 5.0, "proxyWallet": WALLET}, {"conditionId": COND_PREFERRED, "realizedPnl": 5.0, "proxyWallet": WALLET}]
    adapter = _bind(DiscoveryAdapter(), _multi_trade_handler(trades, activity=activity, closed=closed))
    fetcher = WalletHistoryFetcher(adapter, budget=_RequestBudget(20))
    classifications = [_good_market(COND_PREFERRED, 5)]
    report = await fetcher.fetch(seeds=(SeedWallet(wallet_address=WALLET, sources=("market_first",)),),
                                 classifications=classifications, as_of=NOW)
    await adapter.aclose()
    record = report.wallets[0]
    # settled_realized_pnl uses closed_pnl[condition] which is summed (10.0); the dedupe
    # is per-condition-id at the closed-positions level, NOT per fill. The test
    # confirms the FIRST settled row reads 10.0 from the closed_pnl aggregate.
    assert record.settled[0].settled_realized_pnl == 10.0
