"""Focused regressions for PR #73 rollback truthfulness and rejected metrics."""
from __future__ import annotations

import asyncio

import pytest

from polycopy.db.database import Database
from polycopy.ingestion import specialist_evidence_watchlist as watchlist
from polycopy.ingestion.specialist_evidence_cohort import CohortRunConfig, run_cohort


ADDRESS = [
    "0xrollback000000000000000000000000000000000001",
    "0xrollback000000000000000000000000000000000002",
]
WALLET = ["rollback-wallet-1", "rollback-wallet-2"]
CONDITION = "0x" + "a" * 64
TOKEN = "0x" + "b" * 64


def _open() -> Database:
    raise RuntimeError("_open is provided by the module-owned SQLite fixture")


@pytest.fixture(autouse=True)
def _owned_sqlite_paths(monkeypatch, owned_sqlite):
    """Route this module's disposable SQLite files through pytest ownership."""
    monkeypatch.setitem(
        globals(), "_open", lambda: Database(owned_sqlite.new_path()).connect()
    )


def _seed(db: Database) -> list[str]:
    ids = []
    for index, address in enumerate(ADDRESS):
        db.conn.execute(
            "INSERT INTO wallets(id,address,label,is_sample,created_at) VALUES (?,?,?,?,?)",
            (WALLET[index], address, "rollback", 0, "2026-01-01T00:00:00Z"),
        )
        ids.append(watchlist.add_watch(db, wallet_id=WALLET[index]))
    return ids


def _trade(index: int) -> dict[str, str]:
    return {
        "sourceProvidedTradeId": f"rollback-{index}",
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
    def __init__(self) -> None:
        self.aclose_calls = 0

    async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
        index = ADDRESS.index(wallet)
        return [_trade(index)][:limit]

    async def aclose(self) -> None:
        self.aclose_calls += 1


class _CommitAndRollbackFailConnection:
    def __init__(self, conn) -> None:
        self._conn = conn
        self.commit_calls = 0
        self.rollback_calls = 0

    def commit(self) -> None:
        self.commit_calls += 1
        raise RuntimeError("injected final cohort commit failure")

    def rollback(self) -> None:
        self.rollback_calls += 1
        raise RuntimeError("injected cohort rollback failure")

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_record_budget_commit_failure_preserves_primary_error_when_rollback_also_fails():
    db = _open()
    watch_ids = _seed(db)
    adapter = _Adapter()
    failing_conn = _CommitAndRollbackFailConnection(db.conn)
    db._conn = failing_conn  # type: ignore[assignment]
    try:
        result = asyncio.run(run_cohort(
            db,
            watch_ids=watch_ids,
            adapter=adapter,
            dry_run=False,
            config=CohortRunConfig(max_total_new_trades=1, rss_mb_limit=10_000.0),
        ))
        assert result.status == "failed", result.as_dict()
        assert result.stop_reason == "commit_failure"
        assert result.cohort_committed is False
        assert result.rolled_back is False
        assert "injected final cohort commit failure" in (result.error or "")
        assert "injected cohort rollback failure" in (result.error or "")
        assert "rollback_failure" in result.reason_codes
        assert failing_conn.commit_calls == 1
        assert failing_conn.rollback_calls == 1
        assert adapter.aclose_calls == 1
    finally:
        db.close()


def test_validation_rejections_are_failed_metrics_not_unprocessed():
    db = _open()
    (watch_id,) = _seed(db)[:1]
    try:
        result = asyncio.run(run_cohort(
            db,
            watch_ids=[watch_id, "not-a-watch-id"],
            adapter=_Adapter(),
            dry_run=True,
            config=CohortRunConfig(),
        ))
        assert [watch.status for watch in result.watches] == ["rejected"]
        assert result.watch_count_processed == 0
        assert result.watch_count_failed == 1
        assert result.watch_count_unprocessed == 1
        assert result.as_dict()["watch_count_rejected"] == 1
    finally:
        db.close()
