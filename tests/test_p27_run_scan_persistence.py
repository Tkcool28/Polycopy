"""Regression tests for run_scan persistence-before-scoring behavior."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.adapters.polymarket import MarketTradeFetchResult  # noqa: E402
from polycopy.db.database import Database  # noqa: E402
from polycopy.domain.market import Market, MarketOutcome  # noqa: E402
from polycopy.domain.order import OrderSide  # noqa: E402
from polycopy.domain.source_trade import (  # noqa: E402
    SourceTrade,
    is_sentinel_trader_address,
)

import scripts.run_scan as run_scan_module  # noqa: E402


def _market(source_id: str = "0xMARKET_A") -> Market:
    return Market(
        source_id=source_id,
        question="Test market",
        outcomes=[MarketOutcome(label="Yes", price=0.7, volume=20_000.0)],
        source="polymarket",
        active=True,
        closed=False,
        resolved=False,
        volume_24h=20_000.0,
        fetched_at=datetime.now(timezone.utc),
        is_sample=False,
    )


def _trade(
    market_source_id: str,
    source_trade_id: str,
    trader_address: str | None,
) -> SourceTrade:
    return SourceTrade(
        source="polymarket_data_api",
        source_trade_id=source_trade_id,
        market_source_id=market_source_id,
        side=OrderSide.BUY,
        outcome="Yes",
        quantity=10.0,
        price=0.45,
        trader_address=trader_address,
        timestamp=datetime.now(timezone.utc),
        is_sample=False,
    )


def _db(tmp_path: Path) -> Database:
    return Database(db_path=tmp_path / "p27.sqlite").connect()


@pytest.mark.asyncio
async def test_run_scan_persists_raw_trades_before_wallet_metrics_and_scoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Fetched trades, including anonymous rows, must be in source_trades before scoring.

    This catches the regression where run_scan discovered wallets from fetched
    trades but scored those wallets before persisting the raw trade history.
    Anonymous/sentinel rows must persist as NULL trader_address and must not
    crash or disappear from raw provenance.
    """
    market = _market()
    real_wallet = "0xabcdef0000000000000000000000000000000001"
    fetched = [
        _trade(market.source_id, "p27-attributed", real_wallet),
        _trade(market.source_id, "p27-anonymous", None),
    ]
    metrics_checked_after_persistence = False

    async def fake_fetch_markets(db, settings, limit, result, use_sample):
        return [market]

    async def fake_fetch_trades(db, market_source_id, now, result, use_sample):
        assert market_source_id == market.source_id
        return MarketTradeFetchResult(
            trades=fetched,
            status="complete",
            pages_fetched=1,
            rows_fetched=len(fetched),
            market_source_id=market.source_id,
        )

    def fake_generate_signals(db, markets, now):
        return []

    original_compute_metrics = run_scan_module._compute_wallet_metrics  # noqa: SLF001

    def assert_persisted_before_metrics(db, address, now):
        nonlocal metrics_checked_after_persistence
        assert address == real_wallet
        rows = db.fetchall(
            "SELECT source_trade_id, trader_address FROM source_trades "
            "ORDER BY source_trade_id"
        )
        assert [(r["source_trade_id"], r["trader_address"]) for r in rows] == [
            ("p27-anonymous", None),
            ("p27-attributed", real_wallet),
        ]
        metrics_checked_after_persistence = True
        return original_compute_metrics(db, address, now)

    monkeypatch.setattr(run_scan_module, "_fetch_markets", fake_fetch_markets)
    monkeypatch.setattr(run_scan_module, "_fetch_trades", fake_fetch_trades)
    monkeypatch.setattr(run_scan_module, "_generate_signals", fake_generate_signals)
    monkeypatch.setattr(
        run_scan_module,
        "_compute_wallet_metrics",
        assert_persisted_before_metrics,
    )
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p27.sqlite"))

    db = _db(tmp_path)
    try:
        result = await run_scan_module.run_scan(db, market_limit=1, use_sample=False)

        assert metrics_checked_after_persistence is True
        assert result.trades_fetched == 2
        assert result.trades_persisted == 2
        assert result.trades_attributed == 1
        assert result.anonymous_trades == 1
        assert result.trades_processed == 1
        assert result.anonymous_trades_skipped == 1
        assert result.errors == []

        persisted = db.fetchall(
            "SELECT source_trade_id, trader_address FROM source_trades "
            "ORDER BY source_trade_id"
        )
        assert [(r["source_trade_id"], r["trader_address"]) for r in persisted] == [
            ("p27-anonymous", None),
            ("p27-attributed", real_wallet),
        ]
        wallet_rows = db.fetchall("SELECT address FROM wallets")
        wallet_addresses = [
            r["address"]
            for r in wallet_rows
            if not is_sentinel_trader_address(r["address"])
        ]
        assert wallet_addresses == [real_wallet]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_run_scan_skips_wallet_scoring_when_trade_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A failed raw-trade insert must not proceed to false wallet scoring.

    The scanner may continue processing other persisted/anonymous rows, but a
    wallet from a trade that failed to persist must not be discovered, scored,
    or included in attributed-trade processing.
    """
    market = _market()
    real_wallet = "0xabcdef0000000000000000000000000000000001"
    fetched = [
        _trade(market.source_id, "p27-persist-fails", real_wallet),
        _trade(market.source_id, "p27-anonymous-persists", None),
    ]
    metrics_called = False

    async def fake_fetch_markets(db, settings, limit, result, use_sample):
        return [market]

    async def fake_fetch_trades(db, market_source_id, now, result, use_sample):
        assert market_source_id == market.source_id
        return MarketTradeFetchResult(
            trades=fetched,
            status="complete",
            pages_fetched=1,
            rows_fetched=len(fetched),
            market_source_id=market.source_id,
        )

    def fake_generate_signals(db, markets, now):
        return []

    original_persist_trade = run_scan_module._persist_trade  # noqa: SLF001

    def fail_attributed_trade(db, trade):
        if trade.source_trade_id == "p27-persist-fails":
            return None
        return original_persist_trade(db, trade)

    def fail_if_scoring_runs(db, address, now):
        nonlocal metrics_called
        metrics_called = True
        raise AssertionError("wallet scoring must not run for an unpersisted trade")

    monkeypatch.setattr(run_scan_module, "_fetch_markets", fake_fetch_markets)
    monkeypatch.setattr(run_scan_module, "_fetch_trades", fake_fetch_trades)
    monkeypatch.setattr(run_scan_module, "_generate_signals", fake_generate_signals)
    monkeypatch.setattr(run_scan_module, "_persist_trade", fail_attributed_trade)
    monkeypatch.setattr(run_scan_module, "_compute_wallet_metrics", fail_if_scoring_runs)
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p27.sqlite"))

    db = _db(tmp_path)
    try:
        result = await run_scan_module.run_scan(db, market_limit=1, use_sample=False)

        assert metrics_called is False
        assert result.trades_fetched == 2
        assert result.trades_persisted == 1
        assert result.trades_attributed == 0
        assert result.anonymous_trades == 1
        assert result.trades_processed == 0
        assert result.wallets_discovered == 0
        assert result.wallets_scored == 0
        assert result.errors == [
            "Failed to persist trade p27-persist-fails; skipped wallet scoring"
        ]

        persisted = db.fetchall(
            "SELECT source_trade_id, trader_address FROM source_trades "
            "ORDER BY source_trade_id"
        )
        assert [(r["source_trade_id"], r["trader_address"]) for r in persisted] == [
            ("p27-anonymous-persists", None),
        ]
        assert db.fetchall("SELECT address FROM wallets") == []
    finally:
        db.close()
