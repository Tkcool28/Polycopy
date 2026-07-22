"""Focused Phase C/D/E lifecycle regressions for PR #73 cohort collection.

All cases use disposable file-backed SQLite databases.  They exercise the real
cohort orchestration and collector; only timing/resource observations and the
SQLite connection's final commit seam are controlled.
"""
from __future__ import annotations

import asyncio
import math
import tempfile
from pathlib import Path

import pytest

from polycopy.db.database import Database
from polycopy.ingestion.specialist_evidence_cohort import CohortRunConfig, run_cohort
from polycopy.ingestion import specialist_evidence_watchlist as watchlist


ADDRESS = [
    "0xphasec000000000000000000000000000000000001",
    "0xphasec000000000000000000000000000000000002",
]
WALLET = ["phase-cde-wallet-1", "phase-cde-wallet-2"]
CONDITION = "0x" + "a" * 64
TOKEN = "0x" + "b" * 64


def _open() -> Database:
    return Database(Path(tempfile.mktemp(suffix=".db"))).connect()


def _seed(db: Database) -> list[str]:
    watch_ids = []
    for index, address in enumerate(ADDRESS):
        db.conn.execute(
            "INSERT INTO wallets(id,address,label,is_sample,created_at) VALUES (?,?,?,?,?)",
            (WALLET[index], address, "phase-cde", 0, "2026-01-01T00:00:00Z"),
        )
        watch_ids.append(watchlist.add_watch(db, wallet_id=WALLET[index]))
    return watch_ids


def _trade(index: int) -> dict[str, str]:
    return {
        "sourceProvidedTradeId": f"phase-cde-{index}",
        "proxyWallet": ADDRESS[index],
        "asset": TOKEN,
        "conditionId": CONDITION,
        "side": "BUY",
        "outcome": "Yes",
        "price": "0.40",
        "size": "10",
        "timestamp": "2026-02-01T00:00:00Z",
    }


class _Adapter:
    def __init__(self, rows: dict[str, list[dict[str, str]]]) -> None:
        self.rows = rows
        self.aclose_calls = 0
        self.fetches: list[str] = []

    async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
        self.fetches.append(wallet.lower())
        return self.rows.get(wallet.lower(), [])[:limit]

    async def aclose(self) -> None:
        self.aclose_calls += 1


class _SleepingAdapter:
    """Provider-shaped adapter that proves wait_for cancels real I/O work."""

    def __init__(self) -> None:
        self.fetch_started = asyncio.Event()
        self.fetches = 0
        self.fetch_cancelled = 0
        self.aclose_calls = 0

    async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
        self.fetches += 1
        self.fetch_started.set()
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            self.fetch_cancelled += 1
            raise

    async def aclose(self) -> None:
        self.aclose_calls += 1


class _SyncCloseOnlySleepingAdapter:
    """Legacy close-only adapter used to prove cancellation still cleans up."""

    def __init__(self) -> None:
        self.fetch_started = asyncio.Event()
        self.fetch_cancelled = 0
        self.close_calls = 0

    async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
        self.fetch_started.set()
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            self.fetch_cancelled += 1
            raise

    def close(self) -> None:
        self.close_calls += 1


class _PrimaryFailureWithBrokenCloseAdapter:
    """A close error must not replace the original provider failure."""

    def __init__(self) -> None:
        self.aclose_calls = 0

    async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
        raise RuntimeError("primary provider failure")

    async def aclose(self) -> None:
        self.aclose_calls += 1
        raise RuntimeError("cleanup close failure")


class _CommitFailConnection:
    """Delegate all SQLite behavior except the final commit, which fails once."""

    def __init__(self, conn) -> None:
        self._conn = conn
        self.commit_calls = 0
        self.rollback_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1
        raise RuntimeError("injected final cohort commit failure")

    def rollback(self) -> None:
        self.rollback_calls += 1
        self._conn.rollback()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _row_count(db: Database, table: str) -> int:
    return db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_phase_c_deadline_mid_cohort_rolls_back_staged_first_watch(monkeypatch):
    """A deadline after watch one aborts before watch two and rolls back watch one."""
    import polycopy.ingestion.specialist_evidence_collector as collector

    db = _open()
    watch_ids = _seed(db)
    adapter = _Adapter({ADDRESS[0].lower(): [_trade(0)], ADDRESS[1].lower(): [_trade(1)]})
    original_collect = collector.collect_evidence
    completed_once = False

    async def collect_then_expire_deadline(*args, **kwargs):
        nonlocal completed_once
        if completed_once:
            raise collector.CohortDeadlineExceeded("injected phase-C deadline expiry")
        result = await original_collect(*args, **kwargs)
        completed_once = True
        return result

    monkeypatch.setattr(collector, "collect_evidence", collect_then_expire_deadline)
    try:
        result = asyncio.run(run_cohort(
            db, watch_ids=watch_ids, adapter=adapter, dry_run=False,
            config=CohortRunConfig(timeout_seconds=1.0, rss_mb_limit=10_000.0),
        ))
        assert result.status == "failed", result.as_dict()
        assert result.stop_reason == "deadline_exceeded"
        assert result.cohort_committed is False and result.rolled_back is True
        # The deadline is observed when the second watch enters collection, so
        # it is the failed watch; it never reaches provider fetch.
        assert [watch.status for watch in result.watches] == ["ok", "error"]
        assert _row_count(db, "source_trades") == 0
        assert len(adapter.fetches) == 1  # failing watch never reaches provider fetch
        assert adapter.aclose_calls == 1
    finally:
        db.close()


def test_phase_d_rss_mid_cohort_rolls_back_and_closes_once(monkeypatch):
    """An RSS breach after watch one is fail-closed, atomic, and closes once."""
    import polycopy.ingestion.specialist_evidence_collector as collector

    db = _open()
    watch_ids = _seed(db)
    adapter = _Adapter({ADDRESS[0].lower(): [_trade(0)], ADDRESS[1].lower(): [_trade(1)]})
    original_collect = collector.collect_evidence
    completed_once = False

    async def collect_then_raise_rss(*args, **kwargs):
        nonlocal completed_once
        if completed_once:
            raise collector.CohortRssExceeded("injected phase-D RSS breach")
        result = await original_collect(*args, **kwargs)
        completed_once = True
        return result

    monkeypatch.setattr(collector, "collect_evidence", collect_then_raise_rss)
    try:
        result = asyncio.run(run_cohort(
            db, watch_ids=watch_ids, adapter=adapter, dry_run=False,
            config=CohortRunConfig(rss_mb_limit=10_000.0),
        ))
        assert result.status == "failed", result.as_dict()
        assert result.stop_reason == "rss_limit_exceeded"
        assert result.cohort_committed is False and result.rolled_back is True
        assert [watch.status for watch in result.watches] == ["ok", "error"]
        assert _row_count(db, "source_trades") == 0
        assert adapter.aclose_calls == 1
    finally:
        db.close()


def test_phase_e_final_commit_failure_rolls_back_staged_rows_and_closes_once():
    """A final commit error is reported as commit_failure and rolls back all DML."""
    db = _open()
    watch_ids = _seed(db)
    adapter = _Adapter({ADDRESS[0].lower(): [_trade(0)], ADDRESS[1].lower(): [_trade(1)]})
    failing_conn = _CommitFailConnection(db.conn)
    db._conn = failing_conn
    try:
        result = asyncio.run(run_cohort(
            db, watch_ids=watch_ids, adapter=adapter, dry_run=False,
            config=CohortRunConfig(rss_mb_limit=10_000.0),
        ))
        assert result.status == "failed", result.as_dict()
        assert result.stop_reason == "commit_failure"
        assert result.cohort_committed is False and result.rolled_back is True
        assert "injected final cohort commit failure" in (result.error or "")
        assert failing_conn.commit_calls == 1
        assert failing_conn.rollback_calls == 1
        assert _row_count(db, "source_trades") == 0
        assert adapter.aclose_calls == 1
    finally:
        db.close()


def test_phase_e_cancellation_rolls_back_staged_rows_and_closes_once(monkeypatch):
    """Cancellation is a transaction failure: stage one must not survive it."""
    import polycopy.ingestion.specialist_evidence_collector as collector

    db = _open()
    watch_ids = _seed(db)
    adapter = _Adapter({ADDRESS[0].lower(): [_trade(0)], ADDRESS[1].lower(): [_trade(1)]})
    original_collect = collector.collect_evidence
    completed_once = False

    async def collect_then_cancel(*args, **kwargs):
        nonlocal completed_once
        if completed_once:
            raise asyncio.CancelledError("injected phase-E cancellation")
        result = await original_collect(*args, **kwargs)
        completed_once = True
        return result

    monkeypatch.setattr(collector, "collect_evidence", collect_then_cancel)
    try:
        with pytest.raises(asyncio.CancelledError, match="injected phase-E cancellation"):
            asyncio.run(run_cohort(
                db, watch_ids=watch_ids, adapter=adapter, dry_run=False,
                config=CohortRunConfig(rss_mb_limit=10_000.0),
            ))
        assert _row_count(db, "source_trades") == 0
        assert adapter.aclose_calls == 1
    finally:
        db.close()



@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "write"])
def test_real_sleeping_provider_deadline_cancels_inflight_fetch(dry_run):
    """The real wait_for boundary cancels sleeping provider I/O in both modes."""
    db = _open()
    watch_ids = _seed(db)
    adapter = _SleepingAdapter()
    try:
        result = asyncio.run(run_cohort(
            db,
            watch_ids=watch_ids,
            adapter=adapter,
            dry_run=dry_run,
            config=CohortRunConfig(timeout_seconds=0.02, rss_mb_limit=10_000.0),
        ))
        assert result.status == "failed", result.as_dict()
        assert result.stop_reason == "deadline_exceeded"
        assert result.cohort_committed is False
        assert result.rolled_back is (not dry_run)
        assert adapter.fetches == 1
        assert adapter.fetch_started.is_set()
        assert adapter.fetch_cancelled == 1
        assert adapter.aclose_calls == 1
        assert _row_count(db, "source_trades") == 0
    finally:
        db.close()


def test_rss_measurement_is_finite_and_preflight_limit_blocks_provider(monkeypatch):
    """Exercise the Linux RSS meter and prove an over-limit preflight is zero-I/O."""
    import polycopy.ingestion.specialist_evidence_collector as collector

    measured = collector._rss_mb()
    assert math.isfinite(measured) and measured > 0.0

    db = _open()
    watch_ids = _seed(db)
    adapter = _Adapter({})
    monkeypatch.setattr(collector, "_rss_mb", lambda: 128.0)
    try:
        result = asyncio.run(run_cohort(
            db,
            watch_ids=watch_ids,
            adapter=adapter,
            dry_run=False,
            config=CohortRunConfig(rss_mb_limit=127.0),
        ))
        assert result.status == "failed", result.as_dict()
        assert result.stop_reason == "rss_limit_exceeded"
        assert adapter.fetches == []
        assert adapter.aclose_calls == 1
        assert _row_count(db, "source_trades") == 0
    finally:
        db.close()


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "write"])
def test_rss_crossing_during_real_cohort_work_fails_closed(dry_run, monkeypatch):
    """A below-limit precheck followed by a crossing rolls back/no-ops safely."""
    import polycopy.ingestion.specialist_evidence_collector as collector

    db = _open()
    watch_ids = _seed(db)
    adapter = _Adapter({ADDRESS[0].lower(): [_trade(0)]})
    samples_before_crossing = 2 if dry_run else 3
    samples = [10.0] * samples_before_crossing + [20.0]
    observed: list[float] = []

    def sampled_rss() -> float:
        value = samples[min(len(observed), len(samples) - 1)]
        observed.append(value)
        return value

    monkeypatch.setattr(collector, "_rss_mb", sampled_rss)
    try:
        result = asyncio.run(run_cohort(
            db,
            watch_ids=watch_ids,
            adapter=adapter,
            dry_run=dry_run,
            config=CohortRunConfig(rss_mb_limit=15.0),
        ))
        assert result.status == "failed", result.as_dict()
        assert result.stop_reason == "rss_limit_exceeded"
        assert observed[:samples_before_crossing] == [10.0] * samples_before_crossing
        assert observed[samples_before_crossing] == 20.0
        # Watch IDs determine order, so only require that the one started
        # watch fetched before the post-work RSS crossing stopped the cohort.
        assert len(adapter.fetches) == 1
        assert adapter.aclose_calls == 1
        assert _row_count(db, "source_trades") == 0
        assert db.conn.execute(
            "SELECT last_collection_at FROM specialist_evidence_watchlist WHERE id=?",
            (watch_ids[0],),
        ).fetchone()[0] is None
    finally:
        db.close()


def test_close_error_does_not_replace_primary_provider_failure():
    db = _open()
    watch_ids = _seed(db)
    adapter = _PrimaryFailureWithBrokenCloseAdapter()
    try:
        result = asyncio.run(run_cohort(
            db,
            watch_ids=watch_ids,
            adapter=adapter,
            dry_run=False,
            config=CohortRunConfig(rss_mb_limit=10_000.0),
        ))
        assert result.status == "failed", result.as_dict()
        assert result.stop_reason == "watch_error"
        assert "primary provider failure" in (result.error or "")
        assert "cleanup close failure" not in (result.error or "")
        assert adapter.aclose_calls == 1
    finally:
        db.close()


def test_cancellation_uses_sync_close_when_aclose_is_missing():
    """Cancellation propagates while a close-only adapter is still closed once."""
    db = _open()
    watch_ids = _seed(db)
    adapter = _SyncCloseOnlySleepingAdapter()

    async def cancel_run() -> None:
        task = asyncio.create_task(run_cohort(
            db,
            watch_ids=watch_ids,
            adapter=adapter,
            dry_run=False,
            config=CohortRunConfig(timeout_seconds=30.0, rss_mb_limit=10_000.0),
        ))
        await adapter.fetch_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(cancel_run())
        assert adapter.fetch_cancelled == 1
        assert adapter.close_calls == 1
        assert _row_count(db, "source_trades") == 0
    finally:
        db.close()
