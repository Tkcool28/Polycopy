"""Tests for SQLite schema init and migrations."""

from pathlib import Path

import pytest

from polycopy.db.database import Database, MigrationBlocked
from polycopy.db.schema import SCHEMA_VERSION


class TestDatabase:
    def test_init_creates_schema(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            # Verify _meta table exists and has correct version
            row = db.fetchone("SELECT value FROM _meta WHERE key = 'schema_version'")
            assert row is not None
            assert int(row["value"]) == SCHEMA_VERSION

    def test_all_tables_created(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            tables = {row["name"] for row in db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )}
        expected = {
            "_meta", "wallets", "wallet_balances", "markets", "market_outcomes",
            "signals", "orders", "positions", "source_trades", "decision_log",
            "experiment_runs", "raw_snapshots", "performance_summaries",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    def test_idempotent_connect(self, tmp_path: Path):
        """Connecting twice should not error and should keep the same version."""
        db_path = tmp_path / "test.db"
        db = Database(db_path=db_path)
        db.connect()
        db.close()
        db.connect()
        row = db.fetchone("SELECT value FROM _meta WHERE key = 'schema_version'")
        assert row is not None
        assert int(row["value"]) == SCHEMA_VERSION
        db.close()

    def test_newer_db_raises(self, tmp_path: Path):
        """If DB schema is newer than code, raise RuntimeError."""
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            # Manually bump version ahead of code
            db.execute("INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', '999')")
            db.conn.commit()

        with pytest.raises(RuntimeError, match="newer than code"):
            db2 = Database(db_path=db_path)
            db2.connect()

    def test_can_insert_wallet(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, ?, ?)",
                ("test-id", "0xABC", "test-wallet", 0, "2026-01-01T00:00:00Z"),
            )
            db.conn.commit()
            row = db.fetchone("SELECT address FROM wallets WHERE id = ?", ("test-id",))
            assert row is not None
            assert row["address"] == "0xABC"

    def test_can_insert_market_and_outcomes(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            db.execute(
                "INSERT INTO markets (id, source_id, source, question, fetched_at) VALUES (?, ?, ?, ?, ?)",
                ("m1", "cond-1", "polymarket", "Will X?", "2026-01-01T00:00:00Z"),
            )
            db.execute(
                "INSERT INTO market_outcomes (market_id, label, price, volume) VALUES (?, ?, ?, ?)",
                ("m1", "Yes", 0.7, 1000.0),
            )
            db.conn.commit()
            outcomes = db.fetchall("SELECT * FROM market_outcomes WHERE market_id = ?", ("m1",))
            assert len(outcomes) == 1
            assert float(outcomes[0]["price"]) == pytest.approx(0.7)

    def test_can_insert_order(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, ?, ?)",
                ("w1", "0xW1", "wallet1", 0, "2026-01-01T00:00:00Z"),
            )
            db.execute(
                "INSERT INTO markets (id, source_id, source, question, fetched_at) VALUES (?, ?, ?, ?, ?)",
                ("m1", "cond-1", "polymarket", "Will X?", "2026-01-01T00:00:00Z"),
            )
            db.execute(
                """INSERT INTO orders (id, market_id, wallet_id, side, order_type, outcome,
                   quantity, price, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("o1", "m1", "w1", "buy", "limit", "Yes", 10.0, 0.65, "pending", "2026-01-01T00:00:00Z"),
            )
            db.conn.commit()
            row = db.fetchone("SELECT * FROM orders WHERE id = ?", ("o1",))
            assert row is not None
            assert row["side"] == "buy"
            assert float(row["quantity"]) == pytest.approx(10.0)

    def test_raw_snapshot_table(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            db.execute(
                """INSERT INTO raw_snapshots (id, source, endpoint, file_path, content_hash,
                   size_bytes, fetched_at, ingested_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("s1", "polymarket_gamma", "/markets", "snap.json", "abc123", 100, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
            db.conn.commit()
            row = db.fetchone("SELECT * FROM raw_snapshots WHERE id = ?", ("s1",))
            assert row is not None
            assert row["content_hash"] == "abc123"


class TestMigrationRunnerPhysicalSchemaGuard:
    """PR23: the migration runner must not blindly replay destructive
    migrations when ``_meta.schema_version`` is behind the code's
    ``SCHEMA_VERSION`` but the physical schema is already at the target.

    These tests construct the "metadata lag" state by initializing a
    fresh DB to v13, then overwriting ``_meta.schema_version`` to '4'
    (the production state at the time PR23 was written). They verify:

    1. Reconnect reconciles ``_meta`` to v13 without running v5.
    2. The v5 FK guard raises ``MigrationBlocked`` when a child FK to
       ``source_trades`` exists.
    3. Fresh init still creates the 3 PR23 v13 indexes.
    4. Missing PR23 v13 indexes are created during reconciliation.
    5. Repeated reconnects are idempotent (no journal bloat).
    """

    def _build_v13_db_with_meta_4(self, db_path: Path) -> None:
        """Initialize a fresh v13 DB then overwrite ``_meta`` to '4'.

        This reproduces the production state where ``_meta.schema_version``
        is stuck at '4' even though the physical schema is v13. After
        this helper returns, the DB is ready for a reconnect test.
        """
        # Step 1: create a fresh v13 DB.
        with Database(db_path=db_path) as db:
            row = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
            assert row is not None
            assert int(row["value"]) == SCHEMA_VERSION
        # Step 2: overwrite _meta.schema_version to '4' (production state).
        import sqlite3
        con = sqlite3.connect(str(db_path))
        con.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', '4')"
        )
        con.commit()
        con.close()

    def test_meta_lag_physical_match_reconciles_without_replay(self, tmp_path: Path):
        """When ``_meta='4'`` but physical schema is v13, the runner
        must reconcile ``_meta`` to v13 without running v5 (no
        ``source_trades_new`` table, no v5 sentinel-wallet deletes).
        """
        db_path = tmp_path / "test.db"
        self._build_v13_db_with_meta_4(db_path)

        # Sanity check: the production state is set up.
        import sqlite3
        con = sqlite3.connect(str(db_path))
        meta = con.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
        assert meta[0] == "4"
        con.close()

        # Reconnect via the runner. This is the path PR23 fixes.
        with Database(db_path=db_path) as db:
            # _meta should now be reconciled to the target.
            row = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
            assert row is not None
            assert int(row["value"]) == SCHEMA_VERSION

            # v5 must NOT have run: no source_trades_new table should exist.
            tbls = {r["name"] for r in db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            assert "source_trades_new" not in tbls, (
                "v5 must not replay when physical schema is already at target"
            )

            # All v13 tables must still be present.
            for required in ("wallets", "source_trades", "copy_candidates",
                             "wallet_specialist_aggregations", "orders", "positions"):
                assert required in tbls, f"required table {required} missing"

    def test_v5_fk_guard_blocks_drop_when_child_refs_exist(self, tmp_path: Path):
        """When ``_meta='4'`` AND a child FK into ``source_trades`` exists
        in the physical schema, the v5 migration must raise
        :class:`MigrationBlocked` rather than failing partway through
        with a raw ``FOREIGN KEY constraint failed``.

        This is the worked example from production: PR #17+ added
        ``copy_candidates.source_trade_internal_id`` referencing
        ``source_trades.id``, but the v5 migration still tries to
        ``DROP TABLE source_trades`` and would fail mid-statement.
        """
        # Build a v4-shaped DB where a child FK into source_trades
        # already exists. We use a minimal v4 shape rather than the
        # full v13 schema because the runner's reconciliation branch
        # (PR23) would short-circuit before reaching v5 on a v13 DB.
        db_path = tmp_path / "test_v5_fk.db"
        import sqlite3
        con = sqlite3.connect(str(db_path))
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("""
            CREATE TABLE source_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trader_address TEXT NOT NULL,
                payload TEXT
            )
        """)
        # Add a child table that references source_trades.
        con.execute("""
            CREATE TABLE child_of_st (
                id INTEGER PRIMARY KEY,
                source_trade_internal_id INTEGER NOT NULL,
                FOREIGN KEY (source_trade_internal_id) REFERENCES source_trades(id)
            )
        """)
        # _meta with schema_version = 4 (production state).
        con.execute("CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO _meta (key, value) VALUES ('schema_version', '4')")
        con.commit()
        con.close()

        # Connect: the runner should detect the child FK to source_trades
        # and raise MigrationBlocked when it gets to v5.
        db = Database(db_path=db_path)
        with pytest.raises(MigrationBlocked, match="cannot drop source_trades"):
            db.connect()
        db.close()

        # _meta must still be '4' — partial application is not allowed.
        con = sqlite3.connect(str(db_path))
        meta = con.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
        assert meta is not None and meta[0] == "4", (
            "MigrationBlocked must prevent any partial _meta advance"
        )
        con.close()

    def test_fresh_init_creates_pr23_indexes(self, tmp_path: Path):
        """Fresh DB init at v13 must include the 3 PR23 indexes."""
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            idxs = {r["name"] for r in db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )}
        for required in ("idx_wsa_category_score", "idx_wsa_sample",
                         "idx_wsa_computed_at"):
            assert required in idxs, f"PR23 v13 index {required} missing on fresh init"

    def test_meta_lag_with_pr23_indexes_missing_applies_them(self, tmp_path: Path):
        """When physical schema is v13 but the 3 PR23 indexes are
        missing, the reconciliation path must create them as a
        post-reconciliation step.
        """
        db_path = tmp_path / "test.db"
        self._build_v13_db_with_meta_4(db_path)

        # Drop the 3 PR23 v13 indexes to simulate a DB that was
        # reconciled by an older code revision that didn't have them.
        import sqlite3
        con = sqlite3.connect(str(db_path))
        for idx in ("idx_wsa_category_score", "idx_wsa_sample", "idx_wsa_computed_at"):
            con.execute(f"DROP INDEX IF EXISTS {idx}")
        con.commit()
        con.close()

        # Reconnect: reconciliation should fire AND create the missing
        # indexes.
        with Database(db_path=db_path) as db:
            row = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
            assert row is not None
            assert int(row["value"]) == SCHEMA_VERSION

            idxs = {r["name"] for r in db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )}
            for required in ("idx_wsa_category_score", "idx_wsa_sample",
                             "idx_wsa_computed_at"):
                assert required in idxs, (
                    f"PR23 v13 index {required} not created by reconciliation"
                )

    def test_repeat_reconnect_is_idempotent(self, tmp_path: Path):
        """Connecting repeatedly to a reconciled DB must not bloat the
        file size (no journal accumulation, no migration storm).
        """
        db_path = tmp_path / "test.db"
        self._build_v13_db_with_meta_4(db_path)

        # First connect: reconciliation fires.
        with Database(db_path=db_path):
            pass
        size_after_first = db_path.stat().st_size

        # Second connect: no reconciliation needed (already at target).
        with Database(db_path=db_path):
            pass
        size_after_second = db_path.stat().st_size

        # Third connect: still no work.
        with Database(db_path=db_path):
            pass
        size_after_third = db_path.stat().st_size

        # File size must not grow more than a few KB (SQLite overhead).
        # A 4-minute migration storm would add hundreds of MB.
        drift_second = size_after_second - size_after_first
        drift_third = size_after_third - size_after_second
        assert drift_second < 50_000, (
            f"second connect added {drift_second} bytes — possible journal bloat"
        )
        assert drift_third < 50_000, (
            f"third connect added {drift_third} bytes — possible journal bloat"
        )

    def test_safety_defaults_unchanged_on_fresh_init(self, tmp_path: Path):
        """PR23 must not change any non-schema defaults (trading mode,
        PR20 activation flag, etc.). A fresh-init DB is the most
        strict check: nothing in the PR23 code path can mutate env,
        change broker_mode, or enable specialist aggregation.
        """
        import os
        # Confirm PR20 env var is not set in this test process.
        assert "POLYCOPY_SPECIALIST_AGGREGATIONS_ENABLED" not in os.environ
        # Confirm wallet_specialist_aggregations table exists but is empty
        # (PR20 runtime is not active by default).
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            row = db.fetchone(
                "SELECT COUNT(*) AS n FROM wallet_specialist_aggregations"
            )
            assert row is not None
            n = row["n"]
            assert n == 0, "wallet_specialist_aggregations must be empty on fresh init"
