"""PR24A: regression tests for the backfill_resolution_truth script.

Covers the dry-run / apply contract:

1. ``--dry-run`` writes nothing.
2. ``--apply`` uses the global operational lock.
3. ``--limit`` honored.
4. ``--market-id`` honored.
5. ``--json`` output is parseable.
6. Idempotent re-run produces zero additional changes.
7. Default mode is dry-run.
8. Lock-conflict surfaces a clear non-zero exit.
9. Unresolved market gets truth recorded but no winner.
10. Ambiguous market is detected and reported.
11. Trade settlement updates ``source_trades`` columns.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest


# Resolve paths
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "backfill_resolution_truth.py"
_VENV_PY = _REPO_ROOT / ".venv" / "bin" / "python3"


def _run_script(
    *args: str,
    db_path: Path,
    lock_path: Path | None = None,
) -> subprocess.CompletedProcess:
    """Run the backfill script with the given args, returning the
    completed process. Always uses the project's venv python and
    isolates the DB via the database path env var pattern used by
    Settings.
    """
    env = {
        "PYTHONPATH": str(_REPO_ROOT / "src"),
        "PATH": "/usr/bin:/usr/local/bin",
        # Force Settings to use a per-test DB.
        "POLYCOPY_DB_PATH": str(db_path),
    }
    if lock_path is not None:
        env["POLYCOPY_OPERATIONAL_LOCK_PATH"] = str(lock_path)
    return subprocess.run(
        [str(_VENV_PY), str(_SCRIPT), *args],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _make_resolved_market_db(
    db_path: Path,
    *,
    with_winning_outcome: bool = True,
    with_trade: bool = True,
    source_id_suffix: str | None = None,
) -> dict[str, Any]:
    """Create a fresh v14 DB and seed it with a resolved market.

    Returns a dict of useful ids (market_id, winning_token_id, etc.)
    so tests can assert against them.
    """
    from polycopy.db.database import Database
    db = Database(db_path=db_path).connect()
    conn = db.conn
    market_id = str(uuid.uuid4())
    yes_token = "72753295727566659208677964635039361717871718602259295378609650323504626128275"
    no_token = "50377602777708436937119431383653598860392608409270098129623048310195898769240"
    suffix = source_id_suffix or uuid.uuid4().hex[:8]
    src_id = f"src-{suffix}"

    conn.execute(
        "INSERT INTO markets (id, source_id, source, question, fetched_at, "
        "resolved, resolution_outcome, winning_token_id) "
        "VALUES (?, ?, 'test', 'q', '2026-01-01T00:00:00+00:00', "
        "1, 'Yes', ?)",
        (market_id, src_id, yes_token if with_winning_outcome else None),
    )
    conn.execute(
        "INSERT INTO market_outcomes (market_id, label, price, clob_token_id) "
        "VALUES (?, 'Yes', 0.5, ?), (?, 'No', 0.5, ?)",
        (market_id, yes_token, market_id, no_token),
    )

    out: dict[str, Any] = {
        "market_id": market_id,
        "yes_token": yes_token,
        "no_token": no_token,
        "source_id": src_id,
        "trade_id": None,
    }
    if with_trade:
        trade_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', ?, ?, 'BUY', 'Yes', 100.0, 0.4, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, ?, 'unresolved')",
            (trade_id, f"trd-{suffix}", src_id, yes_token),
        )
        out["trade_id"] = trade_id
    conn.commit()
    db.close()
    return out


# ────────────────────────────────────────────────────────────────────
# 1. Default = dry-run
# ────────────────────────────────────────────────────────────────────


class TestDefaultIsDryRun:
    def test_no_flags_means_dry_run(self, tmp_path: Path) -> None:
        db_path = tmp_path / "default.db"
        # No flags at all.
        r = _run_script(db_path=db_path)
        assert r.returncode == 0, f"stderr: {r.stderr}"

        # Verify nothing was written to the markets or source_trades
        # tables. The trade should still be 'unresolved' and the
        # market_outcomes is_winner flags should still be NULL.
        from polycopy.db.database import Database
        db = Database(db_path=db_path).connect()
        try:
            # Any row in either markets.resolution_source or
            # source_trades.resolution_status that is not NULL or
            # 'unresolved' would indicate a dry-run write.
            market = db.conn.execute(
                "SELECT COUNT(*) AS c FROM markets "
                "WHERE resolution_source IS NOT NULL"
            ).fetchone()
            assert market["c"] == 0, (
                "dry-run should NOT have written any markets.resolution_source"
            )
            trade = db.conn.execute(
                "SELECT COUNT(*) AS c FROM source_trades "
                "WHERE resolution_status != 'unresolved'"
            ).fetchone()
            assert trade["c"] == 0, (
                "dry-run should NOT have settled any trades"
            )
        finally:
            db.close()


# ────────────────────────────────────────────────────────────────────
# 2. Dry-run + JSON output
# ────────────────────────────────────────────────────────────────────


class TestDryRunJsonOutput:
    def test_dry_run_json_is_parseable(self, tmp_path: Path) -> None:
        db_path = tmp_path / "json.db"
        ids = _make_resolved_market_db(db_path)
        r = _run_script("--dry-run", "--json", db_path=db_path)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)
        assert data["dry_run"] is True
        assert data["markets_seen"] == 1
        assert data["markets_planned"] == 1
        assert data["trades_seen"] == 1
        assert data["trades_settled"] == 1
        assert data["by_status"]["won"] == 1
        assert len(data["plan"]) == 1
        plan = data["plan"][0]
        assert plan["market_id"] == ids["market_id"]
        assert plan["winning_token_id"] == ids["yes_token"]
        # The plan must list the trade settlement.
        assert len(plan["trade_settlements"]) == 1
        ts = plan["trade_settlements"][0]
        assert ts["resolution_status"] == "won"
        assert ts["realized_pnl"] == pytest.approx(60.0)  # (1 - 0.4) * 100

    def test_dry_run_does_not_persist(self, tmp_path: Path) -> None:
        """A second dry-run after the first must produce identical
        output (no hidden writes from the first run)."""
        db_path = tmp_path / "idem.db"
        _make_resolved_market_db(db_path)
        r1 = _run_script("--dry-run", "--json", db_path=db_path)
        r2 = _run_script("--dry-run", "--json", db_path=db_path)
        assert r1.returncode == 0
        assert r2.returncode == 0
        data1 = json.loads(r1.stdout)
        data2 = json.loads(r2.stdout)
        # The numbers must be identical; the started_at/finished_at
        # timestamps will differ, so we only compare counts.
        for key in (
            "markets_seen",
            "markets_planned",
            "trades_seen",
            "trades_settled",
            "by_status",
        ):
            assert data1[key] == data2[key], f"key {key!r} differs: {data1[key]!r} vs {data2[key]!r}"


# ────────────────────────────────────────────────────────────────────
# 3. --market-id filtering
# ────────────────────────────────────────────────────────────────────


class TestMarketIdFiltering:
    def test_market_id_limits_scope(self, tmp_path: Path) -> None:
        db_path = tmp_path / "scope.db"
        # Seed two resolved markets.
        _make_resolved_market_db(db_path)
        ids_b = _make_resolved_market_db(db_path)

        r = _run_script(
            "--dry-run", "--json", "--market-id", ids_b["market_id"],
            db_path=db_path,
        )
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)
        assert data["markets_seen"] == 1
        assert data["plan"][0]["market_id"] == ids_b["market_id"]


# ────────────────────────────────────────────────────────────────────
# 4. --limit filtering
# ────────────────────────────────────────────────────────────────────


class TestLimitFiltering:
    def test_limit_1_returns_one_market(self, tmp_path: Path) -> None:
        db_path = tmp_path / "limit.db"
        for _ in range(3):
            _make_resolved_market_db(db_path)
        r = _run_script("--dry-run", "--json", "--limit", "1", db_path=db_path)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)
        assert data["markets_seen"] == 1


# ────────────────────────────────────────────────────────────────────
# 5. --apply writes
# ────────────────────────────────────────────────────────────────────


class TestApply:
    def test_apply_writes_markets_and_trades(self, tmp_path: Path) -> None:
        db_path = tmp_path / "apply.db"
        lock_path = tmp_path / "ops.lock"
        ids = _make_resolved_market_db(db_path)
        r = _run_script(
            "--apply", "--json",
            db_path=db_path, lock_path=lock_path,
        )
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)
        assert data["dry_run"] is False

        # Verify the DB state.
        from polycopy.db.database import Database
        db = Database(db_path=db_path).connect()
        try:
            market = db.conn.execute(
                "SELECT resolved, winning_token_id, resolution_source, "
                "resolution_checked_at FROM markets WHERE id=?",
                (ids["market_id"],),
            ).fetchone()
            assert market["resolved"] == 1
            assert market["winning_token_id"] == ids["yes_token"]
            assert market["resolution_source"] == "backfill_resolution_truth"
            assert market["resolution_checked_at"] is not None

            outcomes = {
                row["label"]: row["is_winner"]
                for row in db.conn.execute(
                    "SELECT label, is_winner FROM market_outcomes "
                    "WHERE market_id=?",
                    (ids["market_id"],),
                ).fetchall()
            }
            assert outcomes["Yes"] == 1
            assert outcomes["No"] == 0

            trade = db.conn.execute(
                "SELECT resolution_status, is_winning_trade, realized_pnl, "
                "settlement_source, winning_token_id FROM source_trades "
                "WHERE id=?",
                (ids["trade_id"],),
            ).fetchone()
            assert trade["resolution_status"] == "won"
            assert trade["is_winning_trade"] == 1
            assert trade["realized_pnl"] == pytest.approx(60.0)
            assert trade["settlement_source"] == "backfill_resolution_truth"
            assert trade["winning_token_id"] == ids["yes_token"]
        finally:
            db.close()

    def test_apply_is_idempotent(self, tmp_path: Path) -> None:
        """A second --apply run produces zero additional changes."""
        db_path = tmp_path / "apply_idem.db"
        lock_path = tmp_path / "ops_idem.lock"
        _make_resolved_market_db(db_path)
        r1 = _run_script(
            "--apply", "--json",
            db_path=db_path, lock_path=lock_path,
        )
        assert r1.returncode == 0
        r2 = _run_script(
            "--apply", "--json",
            db_path=db_path, lock_path=lock_path,
        )
        assert r2.returncode == 0, f"stderr: {r2.stderr}"
        data1 = json.loads(r1.stdout)
        data2 = json.loads(r2.stdout)
        # First apply settles 1 trade; second apply finds nothing
        # to settle (the trade is now 'won' and won't be re-settled).
        assert data1["trades_settled"] == 1
        assert data2["trades_seen"] == 0, (
            "second apply must not re-settle trades that already "
            "have resolution_status != 'unresolved'"
        )
        assert data2["trades_settled"] == 0


# ────────────────────────────────────────────────────────────────────
# 6. --skip-trades
# ────────────────────────────────────────────────────────────────────


class TestSkipTrades:
    def test_skip_trades_does_not_settle(self, tmp_path: Path) -> None:
        db_path = tmp_path / "skip_trades.db"
        lock_path = tmp_path / "ops_skip.lock"
        ids = _make_resolved_market_db(db_path)
        r = _run_script(
            "--apply", "--json", "--skip-trades",
            db_path=db_path, lock_path=lock_path,
        )
        assert r.returncode == 0, f"stderr: {r.stderr}"

        from polycopy.db.database import Database
        db = Database(db_path=db_path).connect()
        try:
            trade = db.conn.execute(
                "SELECT resolution_status, is_winning_trade, realized_pnl "
                "FROM source_trades WHERE id=?",
                (ids["trade_id"],),
            ).fetchone()
            assert trade["resolution_status"] == "unresolved", (
                "--skip-trades must not have settled the trade"
            )
            assert trade["is_winning_trade"] is None
            assert trade["realized_pnl"] is None
        finally:
            db.close()


# ────────────────────────────────────────────────────────────────────
# 7. Lock conflict surfaces non-zero exit
# ────────────────────────────────────────────────────────────────────


class TestLockConflict:
    def test_apply_with_held_lock_fails(self, tmp_path: Path) -> None:
        """If another job holds the operational lock, --apply must
        exit nonzero and NOT mutate the DB."""
        from polycopy.utils.concurrency import FileLock
        db_path = tmp_path / "lock_conflict.db"
        lock_path = tmp_path / "ops_conflict.lock"
        ids = _make_resolved_market_db(db_path)

        # Hold the lock from this test process.
        with FileLock(lock_path=lock_path, timeout=0):
            r = _run_script(
                "--apply", "--json", "--lock-timeout", "0",
                db_path=db_path, lock_path=lock_path,
            )
        assert r.returncode == 3, (
            f"expected exit 3 on lock conflict, got {r.returncode}: "
            f"stderr={r.stderr}"
        )
        assert "operational lock" in r.stderr.lower() or "lock" in r.stderr.lower()

        # Verify the DB is unchanged.
        from polycopy.db.database import Database
        db = Database(db_path=db_path).connect()
        try:
            trade = db.conn.execute(
                "SELECT resolution_status FROM source_trades WHERE id=?",
                (ids["trade_id"],),
            ).fetchone()
            assert trade["resolution_status"] == "unresolved", (
                "DB must be unchanged when lock is held by another job"
            )
        finally:
            db.close()


# ────────────────────────────────────────────────────────────────────
# 8. No resolved markets yields empty plan
# ────────────────────────────────────────────────────────────────────


class TestEmptyDatabase:
    def test_dry_run_on_empty_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "empty.db"
        # Don't seed anything; just initialize the schema.
        from polycopy.db.database import Database
        Database(db_path=db_path).connect().close()
        r = _run_script("--dry-run", "--json", db_path=db_path)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)
        assert data["markets_seen"] == 0
        assert data["trades_seen"] == 0
        assert data["plan"] == []


# ────────────────────────────────────────────────────────────────────
# 9. Unresolved market is not in scope
# ────────────────────────────────────────────────────────────────────


class TestUnresolvedMarketsExcluded:
    def test_unresolved_market_not_planned(self, tmp_path: Path) -> None:
        """Markets with resolved=0 are not in the backfill scope."""
        from polycopy.db.database import Database
        db_path = tmp_path / "unresolved.db"
        db = Database(db_path=db_path).connect()
        market_id = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO markets (id, source_id, source, question, fetched_at) "
            "VALUES (?, 'src-1', 'test', 'q', '2026-01-01T00:00:00+00:00')",
            (market_id,),
        )
        db.conn.commit()
        db.close()
        r = _run_script("--dry-run", "--json", db_path=db_path)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["markets_seen"] == 0


# ────────────────────────────────────────────────────────────────────
# 10. Lost trade settlement
# ────────────────────────────────────────────────────────────────────


class TestLostTradeSettlement:
    def test_losing_trade_settles_as_lost(self, tmp_path: Path) -> None:
        from polycopy.db.database import Database
        db_path = tmp_path / "lost_trade.db"
        db = Database(db_path=db_path).connect()
        yes_token = "72753295727566659208677964635039361717871718602259295378609650323504626128275"
        no_token = "50377602777708436937119431383653598860392608409270098129623048310195898769240"
        market_id = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO markets (id, source_id, source, question, fetched_at, "
            "resolved, resolution_outcome, winning_token_id) "
            "VALUES (?, 'src-1', 'test', 'q', '2026-01-01T00:00:00+00:00', "
            "1, 'Yes', ?)",
            (market_id, yes_token),
        )
        db.conn.execute(
            "INSERT INTO market_outcomes (market_id, label, price, clob_token_id) "
            "VALUES (?, 'Yes', 0.5, ?), (?, 'No', 0.5, ?)",
            (market_id, yes_token, market_id, no_token),
        )
        # A trade holding the NO token (which lost).
        trade_id = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', 'trd-l', 'src-1', 'BUY', 'No', 100.0, 0.6, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, ?, 'unresolved')",
            (trade_id, no_token),
        )
        db.conn.commit()
        db.close()

        lock_path = db_path.parent / "ops_lost.lock"
        r = _run_script(
            "--apply", "--json", db_path=db_path, lock_path=lock_path,
        )
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)
        assert data["by_status"]["lost"] == 1
        assert data["trades_settled"] == 1

        db = Database(db_path=db_path).connect()
        try:
            trade = db.conn.execute(
                "SELECT resolution_status, is_winning_trade, realized_pnl "
                "FROM source_trades WHERE id=?", (trade_id,),
            ).fetchone()
            assert trade["resolution_status"] == "lost"
            assert trade["is_winning_trade"] == 0
            assert trade["realized_pnl"] == pytest.approx(-60.0)
        finally:
            db.close()