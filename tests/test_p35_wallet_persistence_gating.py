"""Round 11 / P3 PRRT_kwDOTG4Cf86M7Xbp — wallet persistence gating tests.

The run_scan Step 3 loop must NOT add a wallet to the in-memory
discovery registry, MUST NOT increment the new-wallet counter, MUST
NOT score, and MUST NOT create wallet history/snapshot rows if the
underlying ``wallets`` INSERT fails. Trade persistence (the raw
market-level observation in ``source_trades``) is independent and
may succeed while wallet promotion fails — but the wallet itself
must stay out of the run.

These tests monkeypatch ``_persist_wallet`` in
``scripts.run_scan`` to return ``None`` for a specific address and
verify every invariant.
"""

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
from polycopy.discovery.wallet_discovery import WalletDiscovery  # noqa: E402

import scripts.run_scan as run_scan_module  # noqa: E402


# ─── Helpers ──────────────────────────────────────────────────────────────


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


def _attributed_trade(
    market_source_id: str,
    source_trade_id: str,
    wallet: str,
) -> SourceTrade:
    return SourceTrade(
        source="polymarket_data_api",
        source_trade_id=source_trade_id,
        market_source_id=market_source_id,
        side=OrderSide.BUY,
        outcome="Yes",
        quantity=10.0,
        price=0.45,
        trader_address=wallet,
        timestamp=datetime.now(timezone.utc),
        is_sample=False,
    )


def _anonymous_trade(
    market_source_id: str,
    source_trade_id: str,
) -> SourceTrade:
    return SourceTrade(
        source="polymarket_data_api",
        source_trade_id=source_trade_id,
        market_source_id=market_source_id,
        side=OrderSide.SELL,
        outcome="No",
        quantity=5.0,
        price=0.6,
        trader_address=None,
        timestamp=datetime.now(timezone.utc),
        is_sample=False,
    )


def _empty_db(tmp_path: Path, name: str = "p35.sqlite") -> Database:
    db_path = tmp_path / name
    if db_path.exists():
        db_path.unlink()
    return Database(db_path=db_path).connect()


# ─── Tests ────────────────────────────────────────────────────────────────


class TestWalletPersistenceGating:
    """A failed wallet INSERT must not leak into discovery, scoring, or
    counter increments. The trade row may still persist (raw market
    observation) — but the wallet itself must not enter the run."""

    @pytest.mark.asyncio
    async def test_wallet_insert_failure_creates_no_discovery_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """If ``_persist_wallet`` returns ``None``, the canonical
        wallet address must NOT be added to the in-memory
        ``WalletDiscovery`` registry."""
        market = _market()
        wallet = "0xbeef0000000000000000000000000000000001"
        trade = _attributed_trade(market.source_id, "p35-fail-1", wallet)
        captured_discovery: list[WalletDiscovery] = []

        async def fake_fetch_markets(db, settings, limit, result, use_sample):
            return [market], {}

        async def fake_fetch_trades(
            db, market_source_id, now, result, use_sample,
            *, asset_to_outcome=None,
        ):
            return MarketTradeFetchResult(
                trades=[trade],
                status="complete",
                pages_fetched=1,
                rows_fetched=1,
                market_source_id=market.source_id,
            )

        def fake_generate_signals(db, markets, now):
            return []

        def fail_persist_wallet(db, wallet_obj):
            # Failure path: return None — caller MUST treat the wallet
            # as "not persisted" and skip discovery/scoring.
            return None

        original_discovery = run_scan_module.WalletDiscovery  # noqa: SLF001

        def tracking_discovery(*args, **kwargs):
            d = original_discovery(*args, **kwargs)
            captured_discovery.append(d)
            return d

        monkeypatch.setattr(run_scan_module, "_fetch_markets", fake_fetch_markets)
        monkeypatch.setattr(run_scan_module, "_fetch_trades", fake_fetch_trades)
        monkeypatch.setattr(run_scan_module, "_generate_signals", fake_generate_signals)
        monkeypatch.setattr(run_scan_module, "_persist_wallet", fail_persist_wallet)
        monkeypatch.setattr(run_scan_module, "WalletDiscovery", tracking_discovery)
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p35.sqlite"))

        db = _empty_db(tmp_path)
        try:
            result = await run_scan_module.run_scan(  # noqa: SLF001
                db, market_limit=1, use_sample=False
            )
            # The trade persisted (raw observation), but the wallet
            # never made it into the discovery registry.
            assert result.trades_persisted == 1
            assert result.trades_attributed == 1
            assert result.wallets_discovered_new == 0
            assert result.wallets_total_known == 0
            # The captured discovery object must be empty.
            assert captured_discovery, "WalletDiscovery was never instantiated"
            assert captured_discovery[0].list_wallets() == []
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_wallet_insert_failure_does_not_increment_new_wallet_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``wallets_discovered_new`` MUST stay at 0 when the wallet
        INSERT fails. The pre-fix bug counted the wallet as new
        BEFORE the insert attempt, so a failed insert still
        inflated the counter."""
        market = _market()
        wallet = "0xbeef0000000000000000000000000000000002"
        trade = _attributed_trade(market.source_id, "p35-fail-2", wallet)

        async def fake_fetch_markets(db, settings, limit, result, use_sample):
            return [market], {}

        async def fake_fetch_trades(
            db, market_source_id, now, result, use_sample,
            *, asset_to_outcome=None,
        ):
            return MarketTradeFetchResult(
                trades=[trade],
                status="complete",
                pages_fetched=1,
                rows_fetched=1,
                market_source_id=market.source_id,
            )

        def fake_generate_signals(db, markets, now):
            return []

        def fail_persist_wallet(db, wallet_obj):
            return None

        monkeypatch.setattr(run_scan_module, "_fetch_markets", fake_fetch_markets)
        monkeypatch.setattr(run_scan_module, "_fetch_trades", fake_fetch_trades)
        monkeypatch.setattr(run_scan_module, "_generate_signals", fake_generate_signals)
        monkeypatch.setattr(run_scan_module, "_persist_wallet", fail_persist_wallet)
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p35.sqlite"))

        db = _empty_db(tmp_path)
        try:
            result = await run_scan_module.run_scan(  # noqa: SLF001
                db, market_limit=1, use_sample=False
            )
            assert result.wallets_discovered_new == 0
            assert result.wallets_discovered == 0  # back-compat alias
            assert result.wallets_total_known == 0
            assert result.errors == [
                f"Wallet persist failed for {wallet[:12]}; skipped discovery/scoring"
            ]
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_wallet_insert_failure_produces_no_scoring_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A failed wallet insert MUST NOT cause ``_compute_wallet_metrics``
        (the scoring path) to run. Pre-fix the loop reached the
        ``is_known_wallet`` branch regardless of insert outcome."""
        market = _market()
        wallet = "0xbeef0000000000000000000000000000000003"
        trade = _attributed_trade(market.source_id, "p35-fail-3", wallet)
        metrics_called = False

        async def fake_fetch_markets(db, settings, limit, result, use_sample):
            return [market], {}

        async def fake_fetch_trades(
            db, market_source_id, now, result, use_sample,
            *, asset_to_outcome=None,
        ):
            return MarketTradeFetchResult(
                trades=[trade],
                status="complete",
                pages_fetched=1,
                rows_fetched=1,
                market_source_id=market.source_id,
            )

        def fake_generate_signals(db, markets, now):
            return []

        def fail_persist_wallet(db, wallet_obj):
            return None

        def fail_if_metrics_called(db, address, now):
            nonlocal metrics_called
            metrics_called = True
            raise AssertionError(
                "scoring must not run for a wallet whose insert failed"
            )

        monkeypatch.setattr(run_scan_module, "_fetch_markets", fake_fetch_markets)
        monkeypatch.setattr(run_scan_module, "_fetch_trades", fake_fetch_trades)
        monkeypatch.setattr(run_scan_module, "_generate_signals", fake_generate_signals)
        monkeypatch.setattr(run_scan_module, "_persist_wallet", fail_persist_wallet)
        monkeypatch.setattr(
            run_scan_module,
            "_compute_wallet_metrics",
            fail_if_metrics_called,
        )
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p35.sqlite"))

        db = _empty_db(tmp_path)
        try:
            result = await run_scan_module.run_scan(  # noqa: SLF001
                db, market_limit=1, use_sample=False
            )
            assert metrics_called is False
            assert result.wallets_scored == 0
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_wallet_insert_failure_creates_no_wallet_history_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A failed wallet insert must not leave any side effects in
        the ``wallets`` table for the address. The trade row may
        still exist (raw market observation), but the wallet row
        MUST be absent."""
        market = _market()
        wallet = "0xbeef0000000000000000000000000000000004"
        trade = _attributed_trade(market.source_id, "p35-fail-4", wallet)

        async def fake_fetch_markets(db, settings, limit, result, use_sample):
            return [market], {}

        async def fake_fetch_trades(
            db, market_source_id, now, result, use_sample,
            *, asset_to_outcome=None,
        ):
            return MarketTradeFetchResult(
                trades=[trade],
                status="complete",
                pages_fetched=1,
                rows_fetched=1,
                market_source_id=market.source_id,
            )

        def fake_generate_signals(db, markets, now):
            return []

        def fail_persist_wallet(db, wallet_obj):
            return None

        monkeypatch.setattr(run_scan_module, "_fetch_markets", fake_fetch_markets)
        monkeypatch.setattr(run_scan_module, "_fetch_trades", fake_fetch_trades)
        monkeypatch.setattr(run_scan_module, "_generate_signals", fake_generate_signals)
        monkeypatch.setattr(run_scan_module, "_persist_wallet", fail_persist_wallet)
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p35.sqlite"))

        db = _empty_db(tmp_path)
        try:
            await run_scan_module.run_scan(  # noqa: SLF001
                db, market_limit=1, use_sample=False
            )
            # Trade persists (raw observation).
            source_rows = db.fetchall("SELECT source_trade_id FROM source_trades")
            assert [r["source_trade_id"] for r in source_rows] == ["p35-fail-4"]
            # Wallet row is ABSENT — the failed insert must not leave
            # a partial row behind. Filter the result through the
            # shared sentinel helper so a legacy sentinel that
            # somehow survived cleanup is also accounted for.
            wallet_rows = db.fetchall("SELECT address FROM wallets")
            assert all(is_sentinel_trader_address(r["address"]) for r in wallet_rows)
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_later_successful_retry_discovers_and_scores(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """After a failed insert, a subsequent successful run must
        discover the wallet, score it, and persist the row. The
        gating must not strand the wallet forever."""
        market = _market()
        wallet = "0xbeef0000000000000000000000000000000005"
        trade = _attributed_trade(market.source_id, "p35-retry", wallet)
        persist_attempts: list[int] = []

        async def fake_fetch_markets(db, settings, limit, result, use_sample):
            return [market], {}

        async def fake_fetch_trades(
            db, market_source_id, now, result, use_sample,
            *, asset_to_outcome=None,
        ):
            return MarketTradeFetchResult(
                trades=[trade],
                status="complete",
                pages_fetched=1,
                rows_fetched=1,
                market_source_id=market.source_id,
            )

        def fake_generate_signals(db, markets, now):
            return []

        def flaky_persist_wallet(db, wallet_obj):
            persist_attempts.append(1)
            # First call fails, subsequent calls succeed via the
            # real find-or-create helper.
            if len(persist_attempts) == 1:
                return None
            return run_scan_module.__dict__["_real_persist_wallet"](db, wallet_obj)

        # Use the real persist_wallet on the second attempt.
        real_persist = run_scan_module._persist_wallet  # noqa: SLF001
        monkeypatch.setattr(run_scan_module, "_fetch_markets", fake_fetch_markets)
        monkeypatch.setattr(run_scan_module, "_fetch_trades", fake_fetch_trades)
        monkeypatch.setattr(run_scan_module, "_generate_signals", fake_generate_signals)
        monkeypatch.setattr(run_scan_module, "_persist_wallet", flaky_persist_wallet)
        # Stash the real helper so the spy can delegate to it.
        run_scan_module.__dict__["_real_persist_wallet"] = real_persist
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p35.sqlite"))

        # Run 1: insert fails, wallet stays out.
        db = _empty_db(tmp_path, "p35-retry.sqlite")
        try:
            result1 = await run_scan_module.run_scan(  # noqa: SLF001
                db, market_limit=1, use_sample=False
            )
            assert result1.wallets_discovered_new == 0
            assert result1.wallets_total_known == 0
            assert result1.errors
        finally:
            db.close()

        # Run 2: insert succeeds, wallet discovered, total_known==1.
        # Note: the first run FAILED to insert the wallet row, so the
        # second run starts with no wallet row in the DB
        # (loaded_existing == 0). The wallet is now freshly
        # discovered and persisted successfully
        # (discovered_new == 1, total_known == 1).
        db2 = _empty_db(tmp_path, "p35-retry.sqlite")
        try:
            result2 = await run_scan_module.run_scan(  # noqa: SLF001
                db2, market_limit=1, use_sample=False
            )
            assert result2.wallets_loaded_existing == 0
            assert result2.wallets_discovered_new == 1
            assert result2.wallets_total_known == 1
            assert result2.errors == []
            # And the wallet row is now in the DB.
            wallet_rows = db2.fetchall("SELECT address FROM wallets")
            assert all(
                not is_sentinel_trader_address(r["address"])
                for r in wallet_rows
            )
            assert len(wallet_rows) == 1
            assert wallet_rows[0]["address"] == wallet
        finally:
            db2.close()

    @pytest.mark.asyncio
    async def test_trade_persists_while_wallet_promotion_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The trade row can persist as a raw market observation even
        when the wallet promotion fails. Both invariants are
        independent — the trade is a market-level fact, the wallet
        promotion is a wallet-level fact."""
        market = _market()
        wallet = "0xbeef0000000000000000000000000000000006"
        trade = _attributed_trade(market.source_id, "p35-trade-only", wallet)
        anon_trade = _anonymous_trade(market.source_id, "p35-anon")

        async def fake_fetch_markets(db, settings, limit, result, use_sample):
            return [market], {}

        async def fake_fetch_trades(
            db, market_source_id, now, result, use_sample,
            *, asset_to_outcome=None,
        ):
            return MarketTradeFetchResult(
                trades=[trade, anon_trade],
                status="complete",
                pages_fetched=1,
                rows_fetched=2,
                market_source_id=market.source_id,
            )

        def fake_generate_signals(db, markets, now):
            return []

        def fail_persist_wallet(db, wallet_obj):
            return None

        monkeypatch.setattr(run_scan_module, "_fetch_markets", fake_fetch_markets)
        monkeypatch.setattr(run_scan_module, "_fetch_trades", fake_fetch_trades)
        monkeypatch.setattr(run_scan_module, "_generate_signals", fake_generate_signals)
        monkeypatch.setattr(run_scan_module, "_persist_wallet", fail_persist_wallet)
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p35.sqlite"))

        db = _empty_db(tmp_path)
        try:
            result = await run_scan_module.run_scan(  # noqa: SLF001
                db, market_limit=1, use_sample=False
            )
            # BOTH trade rows persist (raw observations).
            assert result.trades_fetched == 2
            assert result.trades_persisted == 2
            assert result.trades_attributed == 1
            assert result.anonymous_trades == 1
            # But the wallet promotion failed → no wallets in DB,
            # no discovery, no scoring.
            assert result.wallets_discovered_new == 0
            assert result.wallets_total_known == 0
            persisted = db.fetchall(
                "SELECT source_trade_id, trader_address FROM source_trades "
                "ORDER BY source_trade_id"
            )
            assert [(r["source_trade_id"], r["trader_address"]) for r in persisted] == [
                ("p35-anon", None),
                ("p35-trade-only", wallet),
            ]
            assert db.fetchall("SELECT address FROM wallets") == [] or all(
                is_sentinel_trader_address(r["address"])
                for r in db.fetchall("SELECT address FROM wallets")
            )
        finally:
            db.close()
