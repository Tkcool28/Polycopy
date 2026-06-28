"""Regression tests for run_scan.py anonymous-trade handling.

These tests cover the fix for Codex P2 finding:

    In scripts/run_scan.py, anonymous trades are skipped during wallet
    discovery, but they remain in all_trades. Later, Step 4 sends every
    trade to TradeDetector.process_trade(...) with wallet_address=None,
    which crashes inside make_dedup_key / TrackedTrade where
    wallet_address.lower() is called.

The expected behavior after the fix:

- Anonymous trades remain in `all_trades` for provenance / market-level
  counts.
- Anonymous trades are excluded from `attributed_trades`, which is what
  Step 4 (trade detection) iterates over.
- No wallet is created for an anonymous trade.
- No wallet-linked signal can be generated for an anonymous trade.
- Real attributed trades continue through the detector normally.
- `trades_total == trades_processed + anonymous_trades_skipped`.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Ensure repo root is importable (matches how run_scan.py sets sys.path).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

# NOTE: Do not touch POLYCOPY_DB_PATH at import time. CI does not set it,
# and the test_config.py::test_defaults test depends on the default value.
# Each test fixture sets its own DB path via monkeypatch as needed.

from polycopy.db.database import Database  # noqa: E402
from polycopy.domain.order import OrderSide  # noqa: E402
from polycopy.domain.source_trade import SourceTrade, is_sentinel_trader_address  # noqa: E402

# Import AFTER env + sys.path are set so the script's relative-imports work.
import scripts.run_scan as run_scan_module  # noqa: E402


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_trade(
    market_source_id: str,
    trader_address: str | None,
    source_trade_id_suffix: str,
    price: float = 0.55,
    quantity: float = 10.0,
    timestamp: datetime | None = None,
) -> SourceTrade:
    return SourceTrade(
        source="polymarket_clob",
        source_trade_id=f"anon-test-{market_source_id}-{source_trade_id_suffix}",
        market_source_id=market_source_id,
        side=OrderSide.BUY,
        outcome="Yes",
        quantity=quantity,
        price=price,
        trader_address=trader_address,
        timestamp=timestamp or datetime.now(timezone.utc),
        is_sample=False,
    )


def _patched_fetch_trades(monkeypatch, trades_by_market: dict):
    """Replace _fetch_trades so we control exactly what the scan sees."""

    async def fake_fetch_trades(db, market_source_id, now, result, use_sample):
        return trades_by_market.get(market_source_id, [])

    monkeypatch.setattr(run_scan_module, "_fetch_trades", fake_fetch_trades)


def _patched_fetch_markets(monkeypatch, markets):
    async def fake_fetch_markets(db, settings, limit, result, use_sample):
        return markets

    monkeypatch.setattr(run_scan_module, "_fetch_markets", fake_fetch_markets)


def _patched_generate_signals(monkeypatch, signals):
    """Replace _generate_signals so we can count wallet-linked signals."""

    def fake_generate_signals(db, markets, now):
        return signals

    monkeypatch.setattr(run_scan_module, "_generate_signals", fake_generate_signals)


def _make_market(source_id: str = "mkt-1"):
    from polycopy.domain.market import Market, MarketOutcome

    return Market(
        source_id=source_id,
        question=f"Test market {source_id}",
        outcomes=[MarketOutcome(label="Yes", price=0.6, volume=12000.0)],
        source="polymarket_clob",
        active=True,
        closed=False,
        resolved=False,
        volume_24h=12000.0,
        fetched_at=datetime.now(timezone.utc),
        is_sample=False,
    )


def _empty_db(tmp_path: Path) -> Database:
    db_path = tmp_path / "run_scan_anon.sqlite"
    if db_path.exists():
        db_path.unlink()
    return Database(db_path=db_path).connect()


# ─── Unit tests for the helper used in the fix ─────────────────────────────────


class TestSentinelHelperStillExcludesNone:
    """is_sentinel_trader_address must treat None / '' as sentinel."""

    def test_none_is_sentinel(self):
        assert is_sentinel_trader_address(None) is True

    def test_empty_string_is_sentinel(self):
        assert is_sentinel_trader_address("") is True

    def test_whitespace_is_sentinel(self):
        assert is_sentinel_trader_address("   ") is True
        assert is_sentinel_trader_address("\t\n") is True

    def test_known_strings_are_sentinel(self):
        for s in ["unknown", "Unknown", "UNKNOWN", "anonymous", "missing", "0x", "0x0", "0X0"]:
            assert is_sentinel_trader_address(s) is True, f"expected sentinel for {s!r}"

    def test_real_wallet_is_not_sentinel(self):
        for s in [
            "0xATTRIBUTED_WALLET",
            "0x1234567890abcdef1234567890abcdef12345678",
            "0xabc",
        ]:
            assert is_sentinel_trader_address(s) is False, f"expected NOT sentinel for {s!r}"


# ─── End-to-end run_scan regression ───────────────────────────────────────────


class TestRunScanAnonymousExclusion:
    """End-to-end: drive run_scan() with mixed anonymous + attributed trades."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "run_scan_anon.sqlite"))
        return _empty_db(tmp_path)

    @pytest.mark.asyncio
    async def test_anonymous_trade_does_not_crash_run_scan(self, db, monkeypatch):
        """Codex finding: anonymous trade in all_trades crashes Step 4.

        After the fix, run_scan must complete cleanly with anonymous trades
        present in all_trades.
        """
        market = _make_market("mkt-anon-only")
        _patched_fetch_markets(monkeypatch, [market])
        _patched_fetch_trades(
            monkeypatch,
            {
                "mkt-anon-only": [
                    _make_trade("mkt-anon-only", None, "001"),
                    _make_trade("mkt-anon-only", None, "002"),
                ]
            },
        )
        _patched_generate_signals(monkeypatch, [])

        result = await run_scan_module.run_scan(db, market_limit=1, use_sample=False)

        assert result.trades_total == 2
        assert result.anonymous_trades_skipped == 2
        assert result.trades_processed == 0
        # No errors raised → the scan completed cleanly.
        assert result.errors == []
        db.close()

    @pytest.mark.asyncio
    async def test_mixed_input_processes_only_attributed_trades(self, db, monkeypatch):
        """Mixed attributed + anonymous: only attributed reach the detector."""
        market = _make_market("mkt-mixed")
        _patched_fetch_markets(monkeypatch, [market])
        _patched_fetch_trades(
            monkeypatch,
            {
                "mkt-mixed": [
                    _make_trade("mkt-mixed", "0xREAL_WALLET_A", "001"),
                    _make_trade("mkt-mixed", None, "002"),
                    _make_trade("mkt-mixed", "0xREAL_WALLET_B", "003"),
                    _make_trade("mkt-mixed", "unknown", "004"),  # legacy sentinel string
                    _make_trade("mkt-mixed", "", "005"),          # empty string
                ]
            },
        )
        _patched_generate_signals(monkeypatch, [])

        result = await run_scan_module.run_scan(db, market_limit=1, use_sample=False)

        # Total counts
        assert result.trades_total == 5
        # Anonymous (sentinel) count: None + "unknown" + "" = 3
        assert result.anonymous_trades_skipped == 3
        # Processed count = attributed only = 2
        assert result.trades_processed == 2
        # Trades accounted for
        assert result.trades_total == result.trades_processed + result.anonymous_trades_skipped

        # Two real wallets discovered (none for anonymous).
        assert result.wallets_discovered == 2

        # No wallet rows exist for sentinel addresses.
        wallet_rows = db.fetchall("SELECT address FROM wallets")
        wallet_addresses = {r["address"] for r in wallet_rows}
        assert "unknown" not in wallet_addresses
        assert "" not in wallet_addresses
        assert None not in wallet_addresses
        assert "0xREAL_WALLET_A" in wallet_addresses
        assert "0xREAL_WALLET_B" in wallet_addresses
        assert len(wallet_addresses) == 2

        # No errors → Step 4 didn't crash on None.
        assert result.errors == []
        db.close()

    @pytest.mark.asyncio
    async def test_attributed_trade_still_reaches_detector(self, db, monkeypatch):
        """Real attributed trades continue through Step 4 normally."""
        market = _make_market("mkt-attributed")
        _patched_fetch_markets(monkeypatch, [market])
        _patched_fetch_trades(
            monkeypatch,
            {
                "mkt-attributed": [
                    _make_trade("mkt-attributed", "0xATTRIBUTED_WALLET", "001"),
                ]
            },
        )
        _patched_generate_signals(monkeypatch, [])

        result = await run_scan_module.run_scan(db, market_limit=1, use_sample=False)

        assert result.trades_total == 1
        assert result.anonymous_trades_skipped == 0
        assert result.trades_processed == 1
        assert result.wallets_discovered == 1
        assert result.errors == []
        db.close()

    @pytest.mark.asyncio
    async def test_no_wallet_row_created_for_anonymous_trade(self, db, monkeypatch):
        """Anonymous trade must not produce any wallet row."""
        market = _make_market("mkt-no-wallet")
        _patched_fetch_markets(monkeypatch, [market])
        _patched_fetch_trades(
            monkeypatch,
            {
                "mkt-no-wallet": [
                    _make_trade("mkt-no-wallet", None, "001"),
                    _make_trade("mkt-no-wallet", "unknown", "002"),
                    _make_trade("mkt-no-wallet", "anonymous", "003"),
                    _make_trade("mkt-no-wallet", "missing", "004"),
                    _make_trade("mkt-no-wallet", "0x", "005"),
                    _make_trade("mkt-no-wallet", "0x0", "006"),
                ]
            },
        )
        _patched_generate_signals(monkeypatch, [])

        result = await run_scan_module.run_scan(db, market_limit=1, use_sample=False)

        assert result.trades_total == 6
        assert result.anonymous_trades_skipped == 6
        assert result.trades_processed == 0
        assert result.wallets_discovered == 0

        wallet_rows = db.fetchall("SELECT address FROM wallets")
        assert len(wallet_rows) == 0, f"expected zero wallets, got {wallet_rows}"
        db.close()

    @pytest.mark.asyncio
    async def test_summary_includes_trades_total_and_anonymous_count(self, db, monkeypatch):
        """The ScanResult.summary() output must surface trades_total and the
        anonymous-skip count so operators can tell attributed vs anonymous apart.
        """
        market = _make_market("mkt-summary")
        _patched_fetch_markets(monkeypatch, [market])
        _patched_fetch_trades(
            monkeypatch,
            {
                "mkt-summary": [
                    _make_trade("mkt-summary", "0xWALLET_OK", "001"),
                    _make_trade("mkt-summary", None, "002"),
                ]
            },
        )
        _patched_generate_signals(monkeypatch, [])

        result = await run_scan_module.run_scan(db, market_limit=1, use_sample=False)
        summary = result.summary()
        assert "trades total: 2" in summary
        assert "trades processed: 1" in summary
        assert "anonymous (sentinel) skipped: 1" in summary
        db.close()


# ─── Detector-level guard ─────────────────────────────────────────────────────


class TestDetectorGuard:
    """Direct check: passing wallet_address=None to TradeDetector.process_trade
    would crash inside make_dedup_key. The orchestrator (run_scan) is the
    single chokepoint that guarantees the detector only sees attributed
    trades. This test makes that contract explicit.
    """

    def test_detector_crashes_on_none_wallet_address(self):
        """If we (incorrectly) handed None to the detector, it would raise.
        This test pins the failure mode that run_scan must continue to avoid.
        """
        from polycopy.discovery.wallet_discovery import TradeDetector

        detector = TradeDetector()
        with pytest.raises((AttributeError, TypeError)):
            detector.process_trade(
                source="polymarket_clob",
                source_trade_id="guard-001",
                wallet_address=None,  # type: ignore[arg-type]
                market_source_id="mkt",
                side="buy",
                outcome="Yes",
                quantity=10.0,
                price=0.5,
                timestamp=datetime.now(timezone.utc),
                now=datetime.now(timezone.utc),
                is_sample=False,
            )

    def test_detector_accepts_attributed_wallet(self):
        """Sanity check: detector works fine when given a real wallet."""
        from polycopy.discovery.wallet_discovery import TradeDetector

        detector = TradeDetector()
        tracked = detector.process_trade(
            source="polymarket_clob",
            source_trade_id="guard-002",
            wallet_address="0xATTRIBUTED",
            market_source_id="mkt",
            side="buy",
            outcome="Yes",
            quantity=10.0,
            price=0.5,
            timestamp=datetime.now(timezone.utc),
            now=datetime.now(timezone.utc),
            is_sample=False,
        )
        assert tracked.wallet_address == "0xattributed"  # detector lowercases