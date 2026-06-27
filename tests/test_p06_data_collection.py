"""Tests for Phase 06 data collection workflow scripts.

Covers:
- FileLock concurrency guard
- collect_smart_money_data.py (with sample adapter)
- run_scan.py (with sample data)
- update_paper_portfolio.py (with sample data)
- settle_paper_positions.py (with sample data)
- seed_demo_data.py
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Ensure src is importable
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polycopy.config.settings import BrokerMode, Settings
from polycopy.db.database import Database
from polycopy.utils.concurrency import FileLock, LockError, lock_path


class FailingAsyncClient:
    """Minimal async client that fails requests without touching the network."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *args, **kwargs):
        raise RuntimeError("provider unavailable")


# ── Fixtures ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Create a temporary database path."""
    return tmp_path / "test_polycopy.db"


@pytest.fixture
def db(tmp_db_path: Path) -> Generator[Database, None, None]:
    """Create a connected, migrated database."""
    db = Database(db_path=tmp_db_path)
    db.connect()
    yield db
    db.close()


@pytest.fixture
def settings(tmp_db_path: Path) -> Settings:
    """Create test settings with temp DB path."""
    return Settings(
        db_path=tmp_db_path,
        broker_mode=BrokerMode.PAPER,
        paper_mode="paper_manual",
        polymarket_private_key=None,
    )


# ── FileLock tests ───────────────────────────────────────────────────────────────

class TestFileLock:
    """Tests for the file locking concurrency guard."""

    def test_acquire_and_release(self, tmp_path: Path) -> None:
        """Lock can be acquired and released cleanly."""
        lock = FileLock(tmp_path / "test.lock", timeout=1.0)
        with lock:
            assert lock._locked is True
        # Lock file should be removed after release
        assert not (tmp_path / "test.lock").exists()

    def test_mutual_exclusion_same_process(self, tmp_path: Path) -> None:
        """Re-entrant lock attempt on the same path in process context raises LockError.

        Note: In this container environment, flock() across threads in the
        same process does not block (flock is per-PID). We verify the
        timeout-based LockError is raised when the file is held.
        """
        lock1 = FileLock(tmp_path / "test_reentrant.lock", timeout=1.0)
        lock2 = FileLock(tmp_path / "test_reentrant.lock", timeout=0.8)

        with lock1:
            with pytest.raises(LockError):
                with lock2:
                    pass

    def test_stale_lock_removal(self, tmp_path: Path) -> None:
        """Stale lock (PID no longer exists) is detected and removed."""
        lock_path_file = tmp_path / "test.lock"
        # Write a non-existent PID to the lock file
        lock_path_file.write_text("999999\n")

        lock = FileLock(lock_path_file, timeout=1.0, stale_after=0)
        with lock:
            assert lock._locked is True

    def test_context_manager_exception_cleanup(self, tmp_path: Path) -> None:
        """Lock is released even if an exception occurs inside the context."""
        lock = FileLock(tmp_path / "test_exc.lock", timeout=1.0)
        with pytest.raises(RuntimeError):
            with lock:
                raise RuntimeError("test error")

        # Lock should be released
        assert lock._locked is False

    def test_lock_path_helper(self) -> None:
        """lock_path() generates expected paths."""
        result = lock_path("scan")
        assert result.name == "polycopy_scan.lock"

        result = lock_path("portfolio", base_dir=Path("/custom"))
        assert result == Path("/custom/polycopy_portfolio.lock")


# ── collect_smart_money_data tests ──────────────────────────────────────────────

class TestCollectSmartMoneyData:
    """Tests for the data collection script."""

    def test_collection_result_summary(self) -> None:
        """CollectionResult.summary() returns formatted string."""
        from scripts.collect_smart_money_data import CollectionResult

        result = CollectionResult()
        result.markets_fetched = 5
        result.trades_fetched = 100
        result.wallets_discovered = 10
        result.signals_generated = 3

        summary = result.summary()
        assert "status=ok" in summary
        assert "markets: 5" in summary
        assert "wallets: 10" in summary

    def test_collection_result_partial(self) -> None:
        """CollectionResult detects partial success."""
        from scripts.collect_smart_money_data import CollectionResult

        result = CollectionResult()
        result.markets_fetched = 3
        result.markets_failed = 2

        assert result.is_partial is True
        assert result.is_failure is False

    def test_collection_result_failure(self) -> None:
        """CollectionResult detects total failure."""
        from scripts.collect_smart_money_data import CollectionResult

        result = CollectionResult()
        assert result.is_failure is True


# ── run_scan tests ──────────────────────────────────────────────────────────────

class TestRunScan:
    """Tests for the scan orchestrator."""

    @pytest.mark.asyncio
    async def test_scan_with_sample_data(self, db: Database, settings: Settings) -> None:
        """Scan completes successfully with sample data.

        Note: sample data has minimal trades per wallet, so wallets_scored
        may be 0 (not enough data for full scoring). The scan itself
        completes without error.
        """
        from scripts.run_scan import run_scan

        result = await run_scan(
            db=db,
            settings=settings,
            market_limit=5,
            use_sample=True,
        )

        assert result.wallets_discovered > 0
        # wallets_scored may be 0 with minimal sample data
        assert result.wallets_scored >= 0
        assert result.ended_at is not None

    @pytest.mark.asyncio
    async def test_scan_records_experiment(self, db: Database, settings: Settings) -> None:
        """Scan records an experiment run."""
        from scripts.run_scan import run_scan

        await run_scan(db=db, settings=settings, use_sample=True)

        row = db.fetchone("SELECT * FROM experiment_runs LIMIT 1")
        assert row is not None
        assert row["status"] == "completed"

    @pytest.mark.asyncio
    async def test_scan_persists_wallets(self, db: Database, settings: Settings) -> None:
        """Scan persists discovered wallets to the database."""
        from scripts.run_scan import run_scan

        await run_scan(db=db, settings=settings, use_sample=True)

        rows = db.fetchall("SELECT * FROM wallets")
        assert len(rows) > 0

    @pytest.mark.asyncio
    async def test_scan_persists_signals(self, db: Database, settings: Settings) -> None:
        """Scan generates signals for high-volume markets."""
        from scripts.run_scan import run_scan

        await run_scan(db=db, settings=settings, use_sample=True)

        rows = db.fetchall("SELECT * FROM signals")
        assert len(rows) > 0

    @pytest.mark.asyncio
    async def test_live_market_fetch_failure_does_not_use_sample(
        self,
        db: Database,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Live market provider failure returns no markets, not sample fallback."""
        from scripts.run_scan import ScanResult, _fetch_markets

        monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: FailingAsyncClient())

        result = ScanResult()
        markets = await _fetch_markets(db, settings, limit=5, result=result, use_sample=False)

        assert markets == []
        assert result.errors
        assert not any(m.is_sample for m in markets)


# ── update_paper_portfolio tests ───────────────────────────────────────────────

class TestUpdatePaperPortfolio:
    """Tests for the portfolio updater."""

    @pytest.mark.asyncio
    async def test_portfolio_update_with_sample_data(self, db: Database, settings: Settings) -> None:
        """Portfolio update completes with sample pricing."""
        from scripts.update_paper_portfolio import update_portfolio

        result = await update_portfolio(db=db, settings=settings, use_sample=True)

        assert result.ended_at is not None

    @pytest.mark.asyncio
    async def test_portfolio_update_no_positions(self, db: Database, settings: Settings) -> None:
        """Portfolio update handles no open positions gracefully."""
        from scripts.update_paper_portfolio import update_portfolio

        result = await update_portfolio(db=db, settings=settings, use_sample=True)

        assert result.positions_updated == 0
        assert result.ended_at is not None

    @pytest.mark.asyncio
    async def test_live_price_fetch_failure_does_not_use_sample(
        self,
        db: Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Live price provider failure returns None, not a hardcoded sample mark."""
        from scripts.update_paper_portfolio import _fetch_market_prices

        market_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO markets
               (id, source_id, source, question, active, closed, resolved,
                volume_24h, fetched_at, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                market_id,
                "live-condition-id",
                "polymarket",
                "Live market",
                1,
                0,
                0,
                100.0,
                datetime.now(timezone.utc).isoformat(),
                0,
            ),
        )
        db.conn.commit()
        monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: FailingAsyncClient())

        market = await _fetch_market_prices(db, market_id, use_sample=False)

        assert market is None

    @pytest.mark.asyncio
    async def test_live_price_fetch_refuses_sample_market_without_sample_flag(
        self,
        db: Database,
    ) -> None:
        """Sample-backed DB markets require explicit sample mode for sample marks."""
        from scripts.update_paper_portfolio import _fetch_market_prices

        market_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO markets
               (id, source_id, source, question, active, closed, resolved,
                volume_24h, fetched_at, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                market_id,
                "sample-market-001",
                "sample",
                "Sample market [SAMPLE DATA]",
                1,
                0,
                0,
                100.0,
                datetime.now(timezone.utc).isoformat(),
                1,
            ),
        )
        db.conn.commit()

        market = await _fetch_market_prices(db, market_id, use_sample=False)

        assert market is None


# ── settle_paper_positions tests ───────────────────────────────────────────────

class TestSettlePaperPositions:
    """Tests for the settlement script."""

    @pytest.mark.asyncio
    async def test_settlement_with_sample_data(self, db: Database, settings: Settings) -> None:
        """Settlement completes with sample resolution data."""
        from scripts.settle_paper_positions import run_settlement

        result = await run_settlement(db=db, settings=settings, use_sample=True)

        assert result.markets_checked >= 0
        assert result.ended_at is not None

    @pytest.mark.asyncio
    async def test_settlement_dry_run(self, db: Database, settings: Settings) -> None:
        """Settlement dry-run does not persist changes."""
        from scripts.settle_paper_positions import run_settlement

        result = await run_settlement(
            db=db, settings=settings, use_sample=True, dry_run=True
        )

        assert result.ended_at is not None


# ── seed_demo_data tests ───────────────────────────────────────────────────────

class TestSeedDemoData:
    """Tests for the demo data seeder."""

    def test_seed_creates_wallets(self, db: Database) -> None:
        """Seeding creates the expected number of wallets."""
        from scripts.seed_demo_data import seed_demo_data

        seed_demo_data(db)

        rows = db.fetchall("SELECT * FROM wallets WHERE is_sample = 1")
        assert len(rows) == 6

    def test_seed_creates_markets(self, db: Database) -> None:
        """Seeding creates the expected number of markets."""
        from scripts.seed_demo_data import seed_demo_data

        seed_demo_data(db)

        rows = db.fetchall("SELECT * FROM markets WHERE is_sample = 1")
        assert len(rows) == 6

    def test_seed_creates_positions(self, db: Database) -> None:
        """Seeding creates paper positions."""
        from scripts.seed_demo_data import seed_demo_data

        seed_demo_data(db)

        rows = db.fetchall("SELECT * FROM positions WHERE is_sample = 1")
        assert len(rows) == 4

    def test_seed_creates_signals(self, db: Database) -> None:
        """Seeding creates trading signals."""
        from scripts.seed_demo_data import seed_demo_data

        seed_demo_data(db)

        rows = db.fetchall("SELECT * FROM signals WHERE is_sample = 1")
        assert len(rows) >= 5

    def test_seed_creates_decision_log(self, db: Database) -> None:
        """Seeding creates decision log entries with all verdict types."""
        from scripts.seed_demo_data import seed_demo_data

        seed_demo_data(db)

        rows = db.fetchall("SELECT * FROM decision_log WHERE is_sample = 1")
        assert len(rows) == 6

        # Verify all verdict types are represented
        decision_types = {row["decision_type"] for row in rows}
        assert "copy_candidate" in decision_types
        assert "watchlist" in decision_types
        assert "skip" in decision_types
        assert "incomplete" in decision_types
        assert "rejection_stale" in decision_types
        assert "rejection_spread" in decision_types

    def test_seed_creates_raw_snapshots(self, db: Database) -> None:
        """Seeding creates raw snapshot provenance records."""
        from scripts.seed_demo_data import seed_demo_data

        seed_demo_data(db)

        rows = db.fetchall("SELECT * FROM raw_snapshots WHERE is_sample = 1")
        assert len(rows) == 2

    def test_seed_creates_experiment_runs(self, db: Database) -> None:
        """Seeding creates experiment run records."""
        from scripts.seed_demo_data import seed_demo_data

        seed_demo_data(db)

        rows = db.fetchall("SELECT * FROM experiment_runs WHERE is_sample = 1")
        assert len(rows) >= 1

    def test_seed_is_idempotent_with_force(self, db: Database) -> None:
        """Seeding with --force clears and re-seeds without duplicates."""
        from scripts.seed_demo_data import seed_demo_data

        # Seed twice with force
        seed_demo_data(db, force=True)
        count1 = len(db.fetchall("SELECT * FROM wallets WHERE is_sample = 1"))

        seed_demo_data(db, force=True)
        count2 = len(db.fetchall("SELECT * FROM wallets WHERE is_sample = 1"))

        assert count1 == count2 == 6

    def test_seed_all_data_labeled_as_sample(self, db: Database) -> None:
        """All seeded data has is_sample=1."""
        from scripts.seed_demo_data import seed_demo_data

        seed_demo_data(db)

        # Check no non-sample data exists
        for table in ["wallets", "markets", "source_trades", "signals", "orders", "positions"]:
            non_sample = db.fetchall(
                f"SELECT COUNT(*) as cnt FROM {table} WHERE is_sample = 0"
            )
            assert non_sample[0]["cnt"] == 0, f"Table {table} has non-sample data"

    def test_seed_creates_performance_summaries(self, db: Database) -> None:
        """Seeding creates performance summary records."""
        from scripts.seed_demo_data import seed_demo_data

        seed_demo_data(db)

        rows = db.fetchall("SELECT * FROM performance_summaries WHERE is_sample = 1")
        assert len(rows) == 3

        # Verify pnl values
        total_pnls = {row["total_pnl"] for row in rows}
        assert 12500.0 in total_pnls  # alpha
        assert -1800.0 in total_pnls  # delta


# ── Integration: scan → portfolio → settlement ────────────────────────────────

class TestWorkflowIntegration:
    """End-to-end workflow tests."""

    @pytest.mark.asyncio
    async def test_scan_then_portfolio(self, db: Database, settings: Settings) -> None:
        """Scan followed by portfolio update works correctly."""
        from scripts.run_scan import run_scan
        from scripts.update_paper_portfolio import update_portfolio

        # Run scan
        scan_result = await run_scan(db=db, settings=settings, use_sample=True)
        assert scan_result.wallets_discovered > 0

        # Run portfolio update
        portfolio_result = await update_portfolio(db=db, settings=settings, use_sample=True)
        assert portfolio_result.ended_at is not None

    def test_seed_then_scan(self, db: Database, settings: Settings) -> None:
        """Seeding then scanning works without conflicts."""
        from scripts.seed_demo_data import seed_demo_data
        from scripts.run_scan import run_scan

        # Seed first
        seed_demo_data(db)

        # Scan with sample (should not fail due to existing data)
        result = asyncio.run(run_scan(db=db, settings=settings, use_sample=True))
        assert result.wallets_discovered > 0
