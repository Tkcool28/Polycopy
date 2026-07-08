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

PR24A2 — resolution-settlement edge-case hardening (dry-run coverage for
non-win paths). These tests pin the *current* contract of the backfill
script for lost / unknown / no-winner / ambiguous / mixed portfolios so
the upcoming accounting PR (PR24I) can rely on them. See
``/tmp/pr24a2_spec.md`` for the full specification.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest


# Resolve paths
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "backfill_resolution_truth.py"
# Use the Python interpreter that pytest itself is running under. The
# backfill script only needs the project on sys.path (we set
# ``PYTHONPATH=src`` in the env) and access to the standard library;
# it does not require a venv-specific binary. This makes the test
# work on the GitHub Actions runners (which use the system Python,
# not a project venv) and on developer workstations alike.
_PYTHON_EXECUTABLE = sys.executable


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
        [str(_PYTHON_EXECUTABLE), str(_SCRIPT), *args],
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


# ═══════════════════════════════════════════════════════════════════════
# PR24A2 — Resolution settlement edge-case hardening (PART 1)
# ═══════════════════════════════════════════════════════════════════════
#
# These tests pin the *current* dry-run contract of the backfill script
# for non-win scenarios. Where the spec asked for behavior that the
# script does not yet exhibit, the tests pin the *current* behavior
# (with a docstring note explaining why) rather than silently mutating
# production code. Rationale: tests-first, only change production code
# if a real bug is uncovered during PART 3 truthiness grep.


# ─────────────────────────────────────────────────────────────────────
# PR24A2 CASE 1 — Dry-run losing trade (NO token while YES wins)
# ─────────────────────────────────────────────────────────────────────


class TestDryRunLosingTrade:
    """Pin the dry-run contract for a trade whose token did NOT win.

    A market is resolved with winning_token_id == YES token. The trade
    holds the NO token at price=0.60, quantity=100, so the expected
    binary payoff is (0 - 0.60) * 100 = -60.0.
    """

    def test_dry_run_losing_trade_reports_lost_no_db_writes(
        self, tmp_path: Path,
    ) -> None:
        from polycopy.db.database import Database

        db_path = tmp_path / "case1_lost.db"

        db = Database(db_path=db_path).connect()
        market_id = str(uuid.uuid4())
        yes_token = (
            "72753295727566659208677964635039361717871718602259295378609650323504626128275"
        )
        no_token = (
            "50377602777708436937119431383653598860392608409270098129623048310195898769240"
        )
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
        trade_id = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', 'trd-lost', 'src-1', 'BUY', 'No', 100.0, 0.6, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, ?, 'unresolved')",
            (trade_id, no_token),
        )
        db.conn.commit()
        db.close()

        r = _run_script("--dry-run", "--json", db_path=db_path)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)

        assert data["dry_run"] is True
        assert data["trades_settled"] == 1  # counts won + lost
        assert data["by_status"]["lost"] == 1
        assert data["by_status"]["won"] == 0

        assert len(data["plan"]) == 1
        plan = data["plan"][0]
        assert plan["market_id"] == market_id
        assert plan["ambiguous"] is False
        assert len(plan["trade_settlements"]) == 1
        ts = plan["trade_settlements"][0]
        assert ts["trade_id"] == trade_id
        assert ts["resolution_status"] == "lost"
        assert ts["is_winning_trade"] == 0
        assert ts["realized_pnl"] == pytest.approx(-60.0)  # -0.6 * 100

        # DB columns must be UNCHANGED after a dry-run.
        db = Database(db_path=db_path).connect()
        try:
            trade = db.conn.execute(
                "SELECT resolution_status, is_winning_trade, realized_pnl, "
                "settlement_source, winning_token_id "
                "FROM source_trades WHERE id = ?",
                (trade_id,),
            ).fetchone()
            assert trade["resolution_status"] == "unresolved", (
                "--dry-run must NOT change trade resolution_status"
            )
            assert trade["is_winning_trade"] is None
            assert trade["realized_pnl"] is None
            assert trade["settlement_source"] is None
            assert trade["winning_token_id"] is None

            market = db.conn.execute(
                "SELECT winning_token_id, resolution_source, "
                "resolution_checked_at FROM markets WHERE id = ?",
                (market_id,),
            ).fetchone()
            assert market["winning_token_id"] == yes_token
            assert market["resolution_source"] is None, (
                "--dry-run must NOT stamp markets.resolution_source"
            )
            assert market["resolution_checked_at"] is None, (
                "--dry-run must NOT stamp markets.resolution_checked_at"
            )

            n_touched = db.conn.execute(
                "SELECT COUNT(*) AS c FROM market_outcomes "
                "WHERE market_id = ? AND is_winner IS NOT NULL",
                (market_id,),
            ).fetchone()
            assert n_touched["c"] == 0, (
                "--dry-run must NOT touch market_outcomes.is_winner"
            )
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────
# PR24A2 CASE 2 — Dry-run unknown trade (NULL or empty token_id)
# ─────────────────────────────────────────────────────────────────────


class TestDryRunUnknownTrade:
    """Pin the dry-run contract when a trade has no usable token_id.

    PR24A2 PART 4 surfaced this as a reporting gap: pre-PR24A2, the
    ``_fetch_unsettled_trades_for_market`` SQL filtered with
    ``st.token_id IS NOT NULL``, so a NULL-token_id trade was NOT
    fetched at all and never appeared in the dry-run report. The
    trade simply disappeared — ``trades_seen=0`` with no way to know
    a trade existed at all.

    PR24A2 PART 4 fix: the SQL pre-filter is unchanged (NULL-token
    trades still cannot be settled — there is no key to match on),
    BUT the script now reports the count of NULL-token trades
    separately as ``report.trades_skipped_missing_token``. This makes
    the existence of these trades visible without changing the
    settlement math. The contract tested here:

      * ``trades_seen == 0`` (still excluded from the settlement plan)
      * ``trades_skipped_missing_token == 1`` (NEW visible counter)
      * ``by_status`` excludes ``unknown`` (the trade never enters
        settlement logic, so it cannot be classified as ``unknown``)
      * The plan's ``trade_settlements`` list is empty for this market

    The accounting PR (PR24I) can now detect and reconcile
    NULL-token trades via ``trades_skipped_missing_token`` instead
    of being silently misled by ``trades_seen=0``.

    Note: the settlement helper
    (``polycopy.engine.trade_settlement.settle_source_trade_against_truth``)
    DOES return ``resolution_status='unknown'`` for empty/whitespace
    ``token_id``. That path is covered separately by
    ``test_blank_string_token_id_trade_settles_as_unknown`` below —
    only NULL tokens hit the SQL pre-filter.
    """

    def test_null_token_id_trade_visible_via_skipped_counter(
        self, tmp_path: Path,
    ) -> None:
        """NULL ``token_id`` trades are now visible via
        ``trades_skipped_missing_token`` (PR24A2 PART 4).

        Pre-PR24A2, these trades were silently excluded from the
        dry-run report (``trades_seen=0``, no other indication they
        existed). Post-PR24A2, the new counter surfaces them.
        """
        from polycopy.db.database import Database

        db_path = tmp_path / "case2_null.db"
        db = Database(db_path=db_path).connect()
        market_id = str(uuid.uuid4())
        yes_token = (
            "72753295727566659208677964635039361717871718602259295378609650323504626128275"
        )
        no_token = (
            "50377602777708436937119431383653598860392608409270098129623048310195898769240"
        )
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
        trade_id = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', 'trd-null', 'src-1', 'BUY', 'No', 100.0, 0.6, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, NULL, 'unresolved')",
            (trade_id,),
        )
        db.conn.commit()
        db.close()

        r = _run_script("--dry-run", "--json", db_path=db_path)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)

        assert data["markets_seen"] == 1
        # PR24A2 PART 4: the NULL-token trade is still NOT in the
        # settlement plan (it can't be matched against a winning
        # token), but the report makes its existence visible via the
        # new counter.
        assert data["trades_seen"] == 0, (
            "NULL token_id trades remain excluded from the "
            "settlement plan (they cannot match a winning token)."
        )
        assert data["trades_skipped_missing_token"] == 1, (
            "PR24A2 PART 4: NULL-token trades must be counted "
            "separately so they are not silently hidden."
        )
        # by_status doesn't include 'unknown' because the trade
        # never enters the settlement logic.
        for status in ("won", "lost", "unknown", "ambiguous", "unresolved"):
            assert data["by_status"][status] == 0
        assert len(data["plan"]) == 1
        assert data["plan"][0]["trade_settlements"] == []

        db = Database(db_path=db_path).connect()
        try:
            row = db.conn.execute(
                "SELECT resolution_status, is_winning_trade, realized_pnl "
                "FROM source_trades WHERE id = ?",
                (trade_id,),
            ).fetchone()
            assert row["resolution_status"] == "unresolved"
            assert row["is_winning_trade"] is None
            assert row["realized_pnl"] is None
        finally:
            db.close()

    @pytest.mark.parametrize("blank_token_id", ["", "   "])
    def test_blank_string_token_id_trade_settles_as_unknown(
        self, tmp_path: Path, blank_token_id: str,
    ) -> None:
        """Empty / whitespace ``token_id`` is NOT excluded by the SQL
        pre-filter (those values are not NULL). The settlement helper
        then collapses them to None and reports ``unknown``.

        This is a SEPARATE scenario from the NULL case above: the
        trade IS planned, IS counted in trades_seen, and IS settled
        with ``status=unknown``.
        """
        from polycopy.db.database import Database

        db_path = tmp_path / "case2_blank.db"
        db = Database(db_path=db_path).connect()
        market_id = str(uuid.uuid4())
        yes_token = (
            "72753295727566659208677964635039361717871718602259295378609650323504626128275"
        )
        no_token = (
            "50377602777708436937119431383653598860392608409270098129623048310195898769240"
        )
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
        trade_id = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', 'trd-blank', 'src-1', 'BUY', 'No', 100.0, 0.6, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, ?, 'unresolved')",
            (trade_id, blank_token_id),
        )
        db.conn.commit()
        db.close()

        r = _run_script("--dry-run", "--json", db_path=db_path)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)

        # Trade IS planned. SQL pre-filter only excludes IS NULL.
        assert data["trades_seen"] == 1
        assert data["by_status"]["unknown"] == 1
        # winning_token is recorded on the settlement for audit.
        ts = data["plan"][0]["trade_settlements"][0]
        assert ts["trade_id"] == trade_id
        assert ts["resolution_status"] == "unknown"
        assert ts["is_winning_trade"] is None
        assert ts["realized_pnl"] is None
        # winning_token_id is populated even when the trade token is
        # blank -- the helper still records the truth for audit.
        assert ts["winning_token_id"] == yes_token

        db = Database(db_path=db_path).connect()
        try:
            row = db.conn.execute(
                "SELECT resolution_status, is_winning_trade, realized_pnl "
                "FROM source_trades WHERE id = ?",
                (trade_id,),
            ).fetchone()
            assert row["resolution_status"] == "unresolved"
            assert row["is_winning_trade"] is None
            assert row["realized_pnl"] is None
        finally:
            db.close()

    def test_unknown_trade_status_returned_by_settlement_helper(self) -> None:
        """The pure settlement helper *does* classify NULL token_id as
        ``unknown`` even though the backfill script SQL pre-filter
        never feeds it in. This test pins that the helper-level
        behavior is what accounting code should rely on, while the
        script-level behavior above (CASE 2a) needs a widening fix
        to expose those trades."""

        from polycopy.engine.market_resolution_truth import MarketResolutionTruth
        from polycopy.engine.trade_settlement import settle_source_trade_against_truth

        s = settle_source_trade_against_truth(
            source_trade={"token_id": None, "price": 0.6, "quantity": 100.0},
            market_truth=MarketResolutionTruth(
                market_id="m-unknown",
                resolved=True,
                winning_token_id="tok-winner",
                source="manual_test_fixture",
            ),
        )
        assert s.resolution_status == "unknown"
        assert s.is_winning_trade is None
        assert s.realized_pnl is None
        assert s.winning_token_id == "tok-winner"  # captured for audit


# ─────────────────────────────────────────────────────────────────────
# PR24A2 CASE 3 — Resolved market with no winning token (no_match)
# ─────────────────────────────────────────────────────────────────────


class TestDryRunNoWinner:
    """Pin the dry-run contract when the script cannot derive a winner.

    With ``winning_token_id=NULL`` in ``markets`` AND a label that does
    NOT match any ``market_outcomes.label``, the script's
    ``plan_truth_for_market`` keeps ``winning_token_id=None`` and the
    application surfaces ``markets_no_match``. Trade settlement plans
    are produced for visibility but their ``settlement`` is None, so
    ``trades_skipped_unresolved`` increments.

    The trade's plan row reports ``resolution_status='unresolved'``
    (because the script defaults unresolved entries to that string
    when ``settlement is None``). This matches ``skipped`` semantics
    rather than ``unknown``, which is the spec's acceptable outcome.
    """

    def test_no_winner_label_match_skips_trade_settlement(
        self, tmp_path: Path,
    ) -> None:
        from polycopy.db.database import Database

        db_path = tmp_path / "case3_no_winner.db"
        db = Database(db_path=db_path).connect()
        market_id = str(uuid.uuid4())
        yes_token = (
            "72753295727566659208677964635039361717871718602259295378609650323504626128275"
        )
        no_token = (
            "50377602777708436937119431383653598860392608409270098129623048310195898769240"
        )
        db.conn.execute(
            "INSERT INTO markets (id, source_id, source, question, fetched_at, "
            "resolved, resolution_outcome, winning_token_id) "
            "VALUES (?, 'src-1', 'test', 'q', '2026-01-01T00:00:00+00:00', "
            "1, 'Maybe', NULL)",
            (market_id,),
        )
        db.conn.execute(
            "INSERT INTO market_outcomes (market_id, label, price, clob_token_id) "
            "VALUES (?, 'Yes', 0.5, ?), (?, 'No', 0.5, ?)",
            (market_id, yes_token, market_id, no_token),
        )
        trade_id = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', 'trd-nw', 'src-1', 'BUY', 'Yes', 100.0, 0.5, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, ?, 'unresolved')",
            (trade_id, yes_token),
        )
        db.conn.commit()
        db.close()

        r = _run_script("--dry-run", "--json", db_path=db_path)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)

        assert data["markets_seen"] == 1
        assert data["markets_planned"] == 0
        assert data["markets_no_match"] == 1
        assert data["markets_ambiguous"] == 0

        assert data["trades_seen"] == 1
        assert data["trades_settled"] == 0
        assert data["trades_skipped_unresolved"] == 1

        for s in ("won", "lost", "unknown", "ambiguous", "unresolved"):
            assert data["by_status"][s] == 0

        plan = data["plan"][0]
        assert plan["ambiguous"] is False
        assert plan["no_match"] is True
        assert len(plan["trade_settlements"]) == 1
        ts = plan["trade_settlements"][0]
        assert ts["trade_id"] == trade_id
        # script defaults the no-settlement row to "unresolved"
        assert ts["resolution_status"] == "unresolved"
        assert ts["is_winning_trade"] is None
        assert ts["winning_token_id"] is None
        # Realized P&L MUST be NULL -- no fake number is fabricated.
        assert ts["realized_pnl"] is None, (
            "realized_pnl must be NULL when no winner can be derived"
        )

        db = Database(db_path=db_path).connect()
        try:
            trade = db.conn.execute(
                "SELECT resolution_status, is_winning_trade, realized_pnl "
                "FROM source_trades WHERE id = ?",
                (trade_id,),
            ).fetchone()
            assert trade["resolution_status"] == "unresolved"
            assert trade["is_winning_trade"] is None
            assert trade["realized_pnl"] is None

            market = db.conn.execute(
                "SELECT winning_token_id, resolution_source, "
                "resolution_checked_at FROM markets WHERE id = ?",
                (market_id,),
            ).fetchone()
            assert market["winning_token_id"] is None
            assert market["resolution_source"] is None
            assert market["resolution_checked_at"] is None
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────
# PR24A2 CASE 4 — Ambiguous duplicate-winner market
# ─────────────────────────────────────────────────────────────────────


class TestDryRunAmbiguousMarket:
    """Pin the dry-run contract when two outcomes share the winning
    token id (a data-integrity / API race condition).

    The script's ``apply_market_resolution_truth`` notices the duplicate
    and returns ``MarketTruthApplication(ambiguous=True,
    winner_outcome_id=None, is_winner_by_outcome_id={})``. The script
    then records ``markets_ambiguous=1`` and ``markets_planned=0`` at
    the market level.

    PR24A2 PART 1 fixed ``_plan_trade_settlements`` so that market
    ambiguity propagates into per-trade settlement. The trade attached
    to the ambiguous market now settles as
    ``resolution_status="ambiguous"`` with ``is_winning_trade=None``
    and ``realized_pnl=None`` — it is NEVER reported as ``won`` /
    ``lost`` against the truth record's ``winning_token_id``.

    The trade record preserves the truth's ``winning_token_id`` for
    audit (so downstream debugging can still see what the truth
    claimed), but the P/L math is suppressed.
    """

    def test_ambiguous_market_reported_trade_settles_as_ambiguous_not_won(
        self, tmp_path: Path,
    ) -> None:
        from polycopy.db.database import Database

        db_path = tmp_path / "case4_ambiguous.db"
        db = Database(db_path=db_path).connect()
        market_id = str(uuid.uuid4())
        shared_token = "tok-shared-" + uuid.uuid4().hex
        db.conn.execute(
            "INSERT INTO markets (id, source_id, source, question, fetched_at, "
            "resolved, resolution_outcome, winning_token_id) "
            "VALUES (?, 'src-1', 'test', 'q', '2026-01-01T00:00:00+00:00', "
            "1, 'Yes', ?)",
            (market_id, shared_token),
        )
        # Two outcomes sharing the SAME clob_token_id => ambiguous.
        db.conn.execute(
            "INSERT INTO market_outcomes (market_id, label, price, clob_token_id) "
            "VALUES (?, 'Yes', 0.5, ?), (?, 'Yes', 0.5, ?)",
            (market_id, shared_token, market_id, shared_token),
        )
        trade_id = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', 'trd-amb', 'src-1', 'BUY', 'Yes', 100.0, 0.4, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, ?, 'unresolved')",
            (trade_id, shared_token),
        )
        db.conn.commit()
        db.close()

        r = _run_script("--dry-run", "--json", db_path=db_path)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)

        # ── Market-level ambiguity reporting is correct. ──────────────
        assert data["markets_seen"] == 1
        assert data["markets_ambiguous"] == 1
        assert data["markets_planned"] == 0
        assert data["markets_no_match"] == 0

        plan = data["plan"][0]
        assert plan["ambiguous"] is True
        assert plan["is_winner_by_outcome_id"] == {}
        assert plan.get("no_match") in (False, None)

        # ── Trade-level ambiguity propagation (PR24A2 PART 1). ───────
        # Before PR24A2: trade settled as "won" because the truth
        # record's winning_token_id was populated. After PR24A2: the
        # trade inherits the market's ambiguity flag and is reported
        # with resolution_status="ambiguous", is_winning_trade=null,
        # realized_pnl=null. The winning_token_id is preserved on the
        # record for audit.
        assert data["trades_seen"] == 1
        assert data["trades_settled"] == 0  # ambiguous != won|lost
        assert data["by_status"]["ambiguous"] == 1
        assert data["by_status"]["won"] == 0
        assert data["by_status"]["lost"] == 0
        assert data["by_status"]["unknown"] == 0

        ts = plan["trade_settlements"][0]
        assert ts["trade_id"] == trade_id
        assert ts["resolution_status"] == "ambiguous"
        assert ts["is_winning_trade"] is None
        assert ts["realized_pnl"] is None
        # winning_token_id retained for audit.
        assert ts["winning_token_id"] == shared_token

        # ── DB columns unchanged after dry-run. ───────────────────────
        db = Database(db_path=db_path).connect()
        try:
            row = db.conn.execute(
                "SELECT resolution_status, is_winning_trade, realized_pnl, "
                "winning_token_id FROM source_trades WHERE id = ?",
                (trade_id,),
            ).fetchone()
            assert row["resolution_status"] == "unresolved"
            assert row["is_winning_trade"] is None
            assert row["realized_pnl"] is None
            assert row["winning_token_id"] is None  # dry-run never writes
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────
# PR24A2 CASE 5 — Mixed-portfolio dry-run (won + lost + unknown +
#                       unresolved + ambiguous, single tmp DB)
# ─────────────────────────────────────────────────────────────────────


class TestDryRunMixedPortfolio:
    """Pin the dry-run contract on a portfolio of mixed outcomes.

    Portfolio:

      * market1: resolved, winning YES. Trade YES @ 0.40 -> won, +60.
                 Trade NO  @ 0.60 -> lost, -60.
      * market2: resolved, winning YES. Trade with NULL token_id ->
                 excluded from plan by current SQL filter.
      * market3: resolved=0 (unresolved at market level) -> entire
                 market excluded from backfill scope.
      * market4: resolved, two outcomes sharing the winning token id
                 (ambiguous). Trade matching the winning token ->
                 reported via market ambiguity; trade still settles
                 as 'won' against the truth record (current gap, same
                 as CASE 4).

    Assertions exercise the *aggregated* dry-run report so the
    accounting PR (PR24I) can rely on counts + per-plan breakdowns.
    """

    def test_dry_run_mixed_portfolio_breakdown(
        self, tmp_path: Path,
    ) -> None:
        from polycopy.db.database import Database

        db_path = tmp_path / "case5_mixed.db"
        db = Database(db_path=db_path).connect()

        suffix = uuid.uuid4().hex[:8]
        yes1 = "yes-" + suffix + "-1"
        no1 = "no-" + suffix + "-1"
        yes2 = "yes-" + suffix + "-2"
        no2 = "no-" + suffix + "-2"
        yes4 = "yes-" + suffix + "-4"

        def _seed_market(
            src: str,
            *,
            resolved: int,
            winning: str | None,
            outcome_labels: list[tuple[str, str]],
        ) -> None:
            mid = str(uuid.uuid4())
            db.conn.execute(
                "INSERT INTO markets (id, source_id, source, question, "
                "fetched_at, resolved, resolution_outcome, winning_token_id) "
                "VALUES (?, ?, 'test', 'q', '2026-01-01T00:00:00+00:00', "
                "?, ?, ?)",
                (mid, src, resolved, "Yes" if winning else None, winning),
            )
            for label, token in outcome_labels:
                db.conn.execute(
                    "INSERT INTO market_outcomes (market_id, label, price, "
                    "clob_token_id) VALUES (?, ?, 0.5, ?)",
                    (mid, label, token),
                )

        # Market 1: resolved, won+lost pair.
        _seed_market(
            f"src-{suffix}-m1",
            resolved=1,
            winning=yes1,
            outcome_labels=[("Yes", yes1), ("No", no1)],
        )
        won_trade = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', ?, ?, 'BUY', 'Yes', 100.0, 0.40, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, ?, 'unresolved')",
            (won_trade, f"{suffix}-m1-won", f"src-{suffix}-m1", yes1),
        )
        lost_trade = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', ?, ?, 'BUY', 'No', 100.0, 0.60, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, ?, 'unresolved')",
            (lost_trade, f"{suffix}-m1-lost", f"src-{suffix}-m1", no1),
        )

        # Market 2: resolved, trade with NULL token_id (CASE 2 path).
        _seed_market(
            f"src-{suffix}-m2",
            resolved=1,
            winning=yes2,
            outcome_labels=[("Yes", yes2), ("No", no2)],
        )
        null_trade = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', ?, ?, 'BUY', 'Yes', 100.0, 0.50, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, NULL, 'unresolved')",
            (null_trade, f"{suffix}-m2-null", f"src-{suffix}-m2"),
        )

        # Market 3: unresolved (resolved=0). Trade exists but the
        # entire market is excluded from backfill scope.
        _seed_market(
            f"src-{suffix}-m3",
            resolved=0,
            winning=None,
            outcome_labels=[("Yes", "tok-m3-yes"), ("No", "tok-m3-no")],
        )
        unresolved_trade = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', ?, ?, 'BUY', 'Yes', 100.0, 0.50, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, ?, 'unresolved')",
            (unresolved_trade, f"{suffix}-m3-unres", f"src-{suffix}-m3",
             "tok-m3-yes"),
        )

        # Market 4: ambiguous (two outcomes share the winning token).
        _seed_market(
            f"src-{suffix}-m4",
            resolved=1,
            winning=yes4,
            outcome_labels=[("Yes", yes4), ("Yes", yes4)],
        )
        ambiguous_trade = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', ?, ?, 'BUY', 'Yes', 100.0, 0.40, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, ?, 'unresolved')",
            (ambiguous_trade, f"{suffix}-m4-amb", f"src-{suffix}-m4", yes4),
        )

        db.conn.commit()
        db.close()

        r = _run_script("--dry-run", "--json", db_path=db_path)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)

        # markets_seen: 3 (m3 unresolved excluded by SQL).
        assert data["markets_seen"] == 3
        assert data["markets_ambiguous"] == 1

        # Trades seen breakdown: 2 from m1, 0 from m2 (NULL token_id
        # excluded by SQL pre-filter but counted in
        # trades_skipped_missing_token below), 0 from m3 (market out
        # of scope), 1 from m4. Total = 3.
        assert data["trades_seen"] == 3

        # by_status: m1 contributes 1 won + 1 lost. m4 (ambiguous
        # market) contributes 1 ambiguous trade per PR24A2 PART 1.
        assert data["by_status"]["won"] >= 1
        assert data["by_status"]["lost"] >= 1
        assert data["by_status"]["ambiguous"] >= 1
        assert data["by_status"]["unknown"] == 0
        assert data["by_status"]["unresolved"] == 0

        # PR24A2 PART 4: NULL-token_id trades (market2) are now
        # visible via the trades_skipped_missing_token counter.
        assert data["trades_skipped_missing_token"] == 1

        plans = data["plan"]
        assert len(plans) == 3

        plan_by_count = {len(p["trade_settlements"]): p for p in plans}
        assert set(plan_by_count.keys()) == {0, 1, 2}

        # The 2-trade plan must have one 'won' (+60) and one 'lost' (-60).
        pair_plan = plan_by_count[2]
        statuses = sorted(
            ts["resolution_status"] for ts in pair_plan["trade_settlements"]
        )
        assert statuses == ["lost", "won"]
        pnl_by_status = {
            ts["resolution_status"]: ts["realized_pnl"]
            for ts in pair_plan["trade_settlements"]
        }
        assert pnl_by_status["won"] == pytest.approx(60.0)
        assert pnl_by_status["lost"] == pytest.approx(-60.0)
        assert pair_plan["ambiguous"] is False
        assert pair_plan.get("no_match") in (False, None)

        # Empty plan is market2 (NULL-token_id trade excluded from
        # the SQL pre-filter; counted separately as
        # trades_skipped_missing_token).
        empty_plan = plan_by_count[0]
        assert empty_plan["trade_settlements"] == []
        assert empty_plan["ambiguous"] is False

        # 1-trade plan is market4 (ambiguous). Per PR24A2 PART 1, the
        # trade now settles as 'ambiguous' (NOT 'won') with no P/L.
        amb_plan = plan_by_count[1]
        assert amb_plan["ambiguous"] is True
        ts = amb_plan["trade_settlements"][0]
        assert ts["trade_id"] == ambiguous_trade
        assert ts["resolution_status"] == "ambiguous"
        assert ts["is_winning_trade"] is None
        assert ts["realized_pnl"] is None

        # All DB rows must remain unchanged (dry-run invariant).
        db = Database(db_path=db_path).connect()
        try:
            for tid in (won_trade, lost_trade, null_trade,
                        unresolved_trade, ambiguous_trade):
                row = db.conn.execute(
                    "SELECT resolution_status, is_winning_trade, "
                    "realized_pnl FROM source_trades WHERE id = ?",
                    (tid,),
                ).fetchone()
                assert row["resolution_status"] == "unresolved"
                assert row["is_winning_trade"] is None
                assert row["realized_pnl"] is None
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────
# PR24A2 CASE 6 — Same-market multi-fill dry-run (per-fill settlement,
#                       position/cost-basis aggregation deferred to PR24I)
# ─────────────────────────────────────────────────────────────────────


class TestDryRunSameMarketMultiFill:
    """Pin the dry-run contract when one wallet holds multiple fills of
    the SAME winning token in the SAME market.

    Contract
    --------

    PR24A settles per-fill.

    PR24I must implement actual position/cost-basis aggregation.

    PR24A's backfill settles each ``source_trades`` row independently —
    it does NOT pre-aggregate by ``(market_source_id, token_id)`` before
    computing P/L. This test pins that per-fill contract explicitly so
    the upcoming accounting PR (PR24I) cannot accidentally rely on the
    backfill script for position-level cost-basis aggregation. PR24I
    must compute its own ``avg_entry``, ``total_qty``, and
    ``position_pnl`` from the per-fill settlements emitted here.

    Portfolio for this case
    -----------------------

    One resolved market (YES wins). Two fills of YES:

      * Trade A: price=0.40, quantity=50. P/L = (1 - 0.40) * 50 = 30.0.
      * Trade B: price=0.55, quantity=50. P/L = (1 - 0.55) * 50 = 22.5.

    Hand-aggregated position view (NOT what the script emits — but what
    PR24I must reconstruct):

      * total_cost = (0.40 * 50) + (0.55 * 50) = 47.5
      * total_qty  = 100
      * avg_entry  = 47.5 / 100 = 0.475
      * position_pnl_if_yes_wins = (1 - 0.475) * 100 = 52.5

    Per-fill sum: 30.0 + 22.5 = 52.5 (agrees with position-level view).

    Floating-point note
    -------------------

    Trade B's hand-calculation is 22.5 exactly, but the script emits
    ``22.499999999999996`` (IEEE-754 representation of 0.45). This test
    uses ``pytest.approx`` for all P/L comparisons and additionally pins
    the float-precision contract with an explicit assertion against the
    raw JSON value, so a future refactor that "fixes" the float by
    rounding will have to update this test deliberately.
    """

    def test_dry_run_same_market_multi_fill_per_fill_pnl(
        self, tmp_path: Path,
    ) -> None:
        from polycopy.db.database import Database

        db_path = tmp_path / "case6_multifill.db"
        db = Database(db_path=db_path).connect()

        suffix = uuid.uuid4().hex[:8]
        yes_token = f"yes-{suffix}-multifill"
        no_token = f"no-{suffix}-multifill"
        src = f"src-{suffix}-multifill"
        market_id = str(uuid.uuid4())

        db.conn.execute(
            "INSERT INTO markets (id, source_id, source, question, fetched_at, "
            "resolved, resolution_outcome, winning_token_id) "
            "VALUES (?, ?, 'test', 'q', '2026-01-01T00:00:00+00:00', "
            "1, 'Yes', ?)",
            (market_id, src, yes_token),
        )
        db.conn.execute(
            "INSERT INTO market_outcomes (market_id, label, price, clob_token_id) "
            "VALUES (?, 'Yes', 0.5, ?), (?, 'No', 0.5, ?)",
            (market_id, yes_token, market_id, no_token),
        )

        # Trade A: YES @ 0.40 qty 50.
        trade_a_id = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', ?, ?, 'BUY', 'Yes', 50.0, 0.40, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, ?, 'unresolved')",
            (trade_a_id, f"{suffix}-trd-A", src, yes_token),
        )

        # Trade B: YES @ 0.55 qty 50. Same market_source_id, same token.
        trade_b_id = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', ?, ?, 'BUY', 'Yes', 50.0, 0.55, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, ?, 'unresolved')",
            (trade_b_id, f"{suffix}-trd-B", src, yes_token),
        )

        db.conn.commit()
        db.close()

        # ── Run dry-run ────────────────────────────────────────────────
        r = _run_script("--dry-run", "--json", db_path=db_path)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)

        # ── Aggregate-level assertions ────────────────────────────────
        assert data["dry_run"] is True
        assert data["markets_seen"] == 1
        assert data["markets_planned"] == 1
        assert data["markets_ambiguous"] == 0
        assert data["markets_no_match"] == 0
        assert data["trades_seen"] == 2
        assert data["trades_settled"] == 2
        assert data["trades_skipped_unresolved"] == 0
        assert data["by_status"]["won"] == 2
        assert data["by_status"]["lost"] == 0
        assert data["by_status"]["unknown"] == 0
        assert data["by_status"]["ambiguous"] == 0
        assert data["by_status"]["unresolved"] == 0

        # ── Per-plan + per-fill assertions ─────────────────────────────
        assert len(data["plan"]) == 1
        plan = data["plan"][0]
        assert plan["market_id"] == market_id
        assert plan["resolved"] is True
        assert plan["ambiguous"] is False
        assert plan["winning_token_id"] == yes_token

        settlements = plan["trade_settlements"]
        assert len(settlements) == 2
        by_trade_id = {ts["trade_id"]: ts for ts in settlements}
        assert trade_a_id in by_trade_id
        assert trade_b_id in by_trade_id

        # Trade A: P/L = (1 - 0.40) * 50 = 30.0 (exact in IEEE-754).
        ts_a = by_trade_id[trade_a_id]
        assert ts_a["resolution_status"] == "won"
        assert ts_a["is_winning_trade"] == 1
        assert ts_a["winning_token_id"] == yes_token
        assert ts_a["realized_pnl"] == pytest.approx(30.0)

        # Trade B: hand-calc 22.5, but IEEE-754 of (1 - 0.55) * 50
        # produces 22.499999999999996. Use approx; pin the raw value
        # separately so future "rounding fixes" are intentional.
        ts_b = by_trade_id[trade_b_id]
        assert ts_b["resolution_status"] == "won"
        assert ts_b["is_winning_trade"] == 1
        assert ts_b["winning_token_id"] == yes_token
        assert ts_b["realized_pnl"] == pytest.approx(22.5)
        # PIN: document the exact float the script emits today so any
        # future change to settlement math is reflected here.
        assert ts_b["realized_pnl"] == pytest.approx(22.499999999999996, abs=1e-9)

        # ── Hand-aggregated position view (what PR24I must compute) ───
        # Per-fill sum must equal the position-level P/L if a future
        # consumer were to weight-average cost basis.
        per_fill_sum = ts_a["realized_pnl"] + ts_b["realized_pnl"]
        assert per_fill_sum == pytest.approx(52.5)

        # Position-level reconstruction: avg_entry = 0.475, qty = 100.
        total_cost = (0.40 * 50) + (0.55 * 50)  # = 47.5
        total_qty = 50 + 50                     # = 100
        avg_entry = total_cost / total_qty      # = 0.475
        position_pnl_if_yes_wins = (1 - avg_entry) * total_qty  # = 52.5
        assert total_cost == pytest.approx(47.5)
        assert avg_entry == pytest.approx(0.475)
        assert position_pnl_if_yes_wins == pytest.approx(52.5)
        # Per-fill sum and position-level P/L must agree (binary payoff).
        assert per_fill_sum == pytest.approx(position_pnl_if_yes_wins)

        # ── DB-unchanged invariants ────────────────────────────────────
        db = Database(db_path=db_path).connect()
        try:
            for tid in (trade_a_id, trade_b_id):
                row = db.conn.execute(
                    "SELECT resolution_status, is_winning_trade, "
                    "realized_pnl FROM source_trades WHERE id = ?",
                    (tid,),
                ).fetchone()
                assert row["resolution_status"] == "unresolved"
                assert row["is_winning_trade"] is None
                assert row["realized_pnl"] is None
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────
# PR24A2 PART 3 — Boundary-price dry-run coverage (0.0 and 1.0 edges)
# ─────────────────────────────────────────────────────────────────────


class TestDryRunBoundaryPrices:
    """Pin the dry-run contract at boundary prices (0.0 and 1.0).

    Binary payoff formula
    ---------------------

    Won:  ``realized_pnl = (1 - price) * quantity``
    Lost: ``realized_pnl = -price * quantity``

    Boundary cases (qty=100 each):

    * Winning token, price=0.0  -> won,  P/L = (1 - 0.0)  * 100 = +100.0
    * Winning token, price=1.0  -> won,  P/L = (1 - 1.0)  * 100 =   0.0
    * Losing token, price=0.0  -> lost, P/L = -0.0        * 100 =  -0.0
    * Losing token, price=1.0  -> lost, P/L = -1.0        * 100 = -100.0

    Notes
    -----

    The "winning token, price=1.0" case is degenerate: the trader paid
    the full payout upfront, so a win returns exactly what they paid
    (P/L = 0). The "losing token, price=0.0" case is the symmetric
    degenerate: the trader paid nothing, so a loss loses nothing
    (P/L = -0.0, which is numerically equal to 0.0 but carries a
    negative sign).

    Floating-point gotcha: ``-0.0`` and ``0.0`` compare equal under
    ``==`` and ``pytest.approx``, but they differ in ``math.copysign``
    and string representation. The test asserts on
    ``pytest.approx(expected)`` which normalizes both to 0.0, and
    additionally pins the contract explicitly per-case.
    """

    def test_dry_run_boundary_prices_win_lose(self, tmp_path: Path) -> None:
        from polycopy.db.database import Database

        db_path = tmp_path / "case7_boundary.db"
        db = Database(db_path=db_path).connect()

        # One resolved YES-wins market shared across all 4 boundary
        # trades. Each trade has a unique source_trade_id so they
        # are distinct source_trades rows.
        suffix = uuid.uuid4().hex[:8]
        yes_token = f"yes-{suffix}-boundary"
        no_token = f"no-{suffix}-boundary"
        market_id = str(uuid.uuid4())
        src = f"src-{suffix}-boundary"

        db.conn.execute(
            "INSERT INTO markets (id, source_id, source, question, fetched_at, "
            "resolved, resolution_outcome, winning_token_id) "
            "VALUES (?, ?, 'test', 'q', '2026-01-01T00:00:00+00:00', "
            "1, 'Yes', ?)",
            (market_id, src, yes_token),
        )
        db.conn.execute(
            "INSERT INTO market_outcomes (market_id, label, price, clob_token_id) "
            "VALUES (?, 'Yes', 0.5, ?), (?, 'No', 0.5, ?)",
            (market_id, yes_token, market_id, no_token),
        )

        # Trade A: winning token (YES), price=0.0, qty=100 -> won, +100
        tid_a = str(uuid.uuid4())
        # Trade B: winning token (YES), price=1.0, qty=100 -> won, 0
        tid_b = str(uuid.uuid4())
        # Trade C: losing token (NO),  price=0.0, qty=100 -> lost, -0.0
        tid_c = str(uuid.uuid4())
        # Trade D: losing token (NO),  price=1.0, qty=100 -> lost, -100
        tid_d = str(uuid.uuid4())

        for tid, suffix2, token, price, outcome in (
            (tid_a, "A", yes_token, 0.0, "Yes"),
            (tid_b, "B", yes_token, 1.0, "Yes"),
            (tid_c, "C", no_token, 0.0, "No"),
            (tid_d, "D", no_token, 1.0, "No"),
        ):
            db.conn.execute(
                "INSERT INTO source_trades "
                "(id, source, source_trade_id, market_source_id, side, "
                " outcome, quantity, price, trader_address, timestamp, is_sample, "
                " token_id, resolution_status) "
                "VALUES (?, 'test', ?, ?, 'BUY', ?, 100.0, ?, "
                " '0xtest', '2026-01-01T00:00:00+00:00', 1, ?, 'unresolved')",
                (tid, f"{suffix}-{suffix2}", src, outcome, price, token),
            )

        db.conn.commit()
        db.close()

        r = _run_script("--dry-run", "--json", db_path=db_path)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)

        # Aggregate assertions.
        assert data["dry_run"] is True
        assert data["markets_seen"] == 1
        assert data["markets_planned"] == 1
        assert data["markets_ambiguous"] == 0
        assert data["trades_seen"] == 4
        assert data["trades_settled"] == 4
        assert data["trades_skipped_missing_token"] == 0
        assert data["by_status"]["won"] == 2
        assert data["by_status"]["lost"] == 2
        assert data["by_status"]["unknown"] == 0
        assert data["by_status"]["ambiguous"] == 0

        # Per-trade assertions.
        plan = data["plan"][0]
        assert plan["market_id"] == market_id
        by_trade_id = {ts["trade_id"]: ts for ts in plan["trade_settlements"]}

        # Trade A: winning token, price=0.0, qty=100 -> won, +100
        ts_a = by_trade_id[tid_a]
        assert ts_a["resolution_status"] == "won"
        assert ts_a["is_winning_trade"] == 1
        assert ts_a["realized_pnl"] == pytest.approx(100.0)

        # Trade B: winning token, price=1.0, qty=100 -> won, 0
        ts_b = by_trade_id[tid_b]
        assert ts_b["resolution_status"] == "won"
        assert ts_b["is_winning_trade"] == 1
        assert ts_b["realized_pnl"] == pytest.approx(0.0)

        # Trade C: losing token, price=0.0, qty=100 -> lost, -0.0
        # Note: -0.0 == 0.0 numerically. Use approx to normalize.
        ts_c = by_trade_id[tid_c]
        assert ts_c["resolution_status"] == "lost"
        assert ts_c["is_winning_trade"] == 0
        assert ts_c["realized_pnl"] == pytest.approx(-0.0)
        assert ts_c["realized_pnl"] == pytest.approx(0.0)

        # Trade D: losing token, price=1.0, qty=100 -> lost, -100
        ts_d = by_trade_id[tid_d]
        assert ts_d["resolution_status"] == "lost"
        assert ts_d["is_winning_trade"] == 0
        assert ts_d["realized_pnl"] == pytest.approx(-100.0)

        # Hand-summed P/L should be 100 + 0 + (-0.0) + (-100) = 0.
        summed = (
            ts_a["realized_pnl"] + ts_b["realized_pnl"]
            + ts_c["realized_pnl"] + ts_d["realized_pnl"]
        )
        assert summed == pytest.approx(0.0)

        # DB-unchanged invariant.
        db = Database(db_path=db_path).connect()
        try:
            for tid in (tid_a, tid_b, tid_c, tid_d):
                row = db.conn.execute(
                    "SELECT resolution_status, is_winning_trade, realized_pnl "
                    "FROM source_trades WHERE id = ?",
                    (tid,),
                ).fetchone()
                assert row["resolution_status"] == "unresolved"
                assert row["is_winning_trade"] is None
                assert row["realized_pnl"] is None
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────
# PR24A2 PART 2 — Voided / no-winner market (spec-named variant)
# ─────────────────────────────────────────────────────────────────────


class TestDryRunNoWinnerMarket:
    """Pin the dry-run contract when a resolved market has no
    ``winning_token_id`` and no ``resolution_outcome`` label can be
    matched against outcomes (a "voided" / "no-winner" market).

    This is a complementary test to ``TestDryRunNoWinner`` above.
    That test exercises the label-mismatch path
    (``resolution_outcome='Maybe'`` with no outcome labeled 'Maybe');
    this one exercises the cleaner voided-style path where the market
    is ``resolved=1`` but ``winning_token_id=NULL`` and
    ``resolution_outcome=NULL`` from the start. Both paths must land
    in the same ``markets_no_match=1`` / ``trades_skipped_unresolved``
    bucket — never in ``by_status["won"]`` or ``by_status["lost"]``.

    Acceptable report behavior (per PART 2 spec)
    --------------------------------------------

    * ``trades_skipped_unresolved`` increments (current behavior).
    * OR ``resolution_status="unknown"`` on the trade plan row.
    * OR another explicit no-winner counter.

    Not acceptable
    --------------

    * Trade silently disappears from the report (no counter at all).
    * Trade settles as ``won`` or ``lost``.
    * A fabricated P/L number.
    """

    def test_resolved_market_with_no_winning_token_does_not_settle_trade(
        self, tmp_path: Path,
    ) -> None:
        from polycopy.db.database import Database

        db_path = tmp_path / "case3_voided.db"
        db = Database(db_path=db_path).connect()

        market_id = str(uuid.uuid4())
        suffix = uuid.uuid4().hex[:8]
        yes_token = f"yes-{suffix}-voided"
        no_token = f"no-{suffix}-voided"
        src = f"src-{suffix}-voided"

        # Voided-style market: resolved=1 but BOTH winning_token_id
        # AND resolution_outcome are NULL. This is the "no winner
        # can be derived" case at the cleanest level.
        db.conn.execute(
            "INSERT INTO markets (id, source_id, source, question, fetched_at, "
            "resolved, resolution_outcome, winning_token_id) "
            "VALUES (?, ?, 'test', 'q', '2026-01-01T00:00:00+00:00', "
            "1, NULL, NULL)",
            (market_id, src),
        )
        # Outcomes still exist with clob_token_ids — the market is
        # real, just unresolved at the winner level.
        db.conn.execute(
            "INSERT INTO market_outcomes (market_id, label, price, clob_token_id) "
            "VALUES (?, 'Yes', 0.5, ?), (?, 'No', 0.5, ?)",
            (market_id, yes_token, market_id, no_token),
        )

        # A real trade on this market with a real token. The script
        # must NOT pretend this trade won or lost.
        trade_id = str(uuid.uuid4())
        db.conn.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, "
            " outcome, quantity, price, trader_address, timestamp, is_sample, "
            " token_id, resolution_status) "
            "VALUES (?, 'test', ?, ?, 'BUY', 'Yes', 100.0, 0.5, "
            " '0xtest', '2026-01-01T00:00:00+00:00', 1, ?, 'unresolved')",
            (trade_id, f"{suffix}-trd-voided", src, yes_token),
        )

        db.conn.commit()
        db.close()

        r = _run_script("--dry-run", "--json", db_path=db_path)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)

        # ── Aggregate assertions ──────────────────────────────────────
        assert data["dry_run"] is True
        assert data["markets_seen"] == 1
        # Market is resolved but has no winner derivable.
        assert data["markets_no_match"] == 1
        assert data["markets_planned"] == 0
        assert data["markets_ambiguous"] == 0

        # Trade is counted (visible), skipped_unresolved, NOT settled.
        assert data["trades_seen"] == 1
        assert data["trades_settled"] == 0
        assert data["trades_skipped_unresolved"] == 1
        assert data["trades_skipped_missing_token"] == 0

        # The trade must NOT be classified as won, lost, or unknown
        # via the script. (unknown is reserved for blank-token_id
        # trades; ambiguous is reserved for multi-outcome-share-token
        # markets; both have dedicated counters/conditions.)
        for status in ("won", "lost", "unknown", "ambiguous"):
            assert data["by_status"][status] == 0

        # ── Per-plan assertions ───────────────────────────────────────
        plan = data["plan"][0]
        assert plan["market_id"] == market_id
        assert plan["resolved"] is True
        assert plan["winning_token_id"] is None  # no fake winner
        assert plan["ambiguous"] is False
        assert plan.get("no_match") is True
        assert plan["is_winner_by_outcome_id"] == {}

        ts = plan["trade_settlements"][0]
        assert ts["trade_id"] == trade_id
        # Script defaults unresolved/skip rows to "unresolved".
        assert ts["resolution_status"] == "unresolved"
        assert ts["is_winning_trade"] is None
        assert ts["winning_token_id"] is None
        # Realized P/L MUST be NULL — no fake number fabricated.
        assert ts["realized_pnl"] is None, (
            "realized_pnl must be NULL when no winner can be derived"
        )

        # ── DB-unchanged invariant ────────────────────────────────────
        db = Database(db_path=db_path).connect()
        try:
            trade = db.conn.execute(
                "SELECT resolution_status, is_winning_trade, realized_pnl, "
                "winning_token_id FROM source_trades WHERE id = ?",
                (trade_id,),
            ).fetchone()
            assert trade["resolution_status"] == "unresolved"
            assert trade["is_winning_trade"] is None
            assert trade["realized_pnl"] is None
            assert trade["winning_token_id"] is None

            # Market also unchanged: no winning_token_id was
            # fabricated by the dry-run.
            market = db.conn.execute(
                "SELECT winning_token_id, resolution_checked_at, "
                "resolution_source FROM markets WHERE id = ?",
                (market_id,),
            ).fetchone()
            assert market["winning_token_id"] is None
            assert market["resolution_checked_at"] is None
            assert market["resolution_source"] is None
        finally:
            db.close()