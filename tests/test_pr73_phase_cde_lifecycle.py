"""Focused Phase C/D/E lifecycle regressions for PR #73 cohort collection.

All cases use disposable file-backed SQLite databases.  They exercise the real
cohort orchestration and collector; only timing/resource observations and the
SQLite connection's final commit seam are controlled.
"""
from __future__ import annotations

import asyncio
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
