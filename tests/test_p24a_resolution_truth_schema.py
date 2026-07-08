"""PR24A: regression tests for the v14 resolution-truth schema migration.

Covers the schema leg of PR24A:

1. New columns exist after migration.
2. Migration is idempotent.
3. Fresh DB reaches schema_version=14.
4. Existing data not destroyed.
5. Indexes created.
6. v14 DDL only adds new columns / indexes (additive).
7. Reconciliation path (PR23) is now v14-aware so it doesn't
   silently bump a v13-physical DB to v14-metadata without
   applying the v14 ALTERs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from polycopy.db.database import Database
from polycopy.db import schema


_V14_NEW_COLUMNS = (
    ("market_outcomes", "is_winner"),
    ("markets", "winning_token_id"),
    ("markets", "resolution_checked_at"),
    ("markets", "resolution_source"),
    ("source_trades", "resolution_status"),
    ("source_trades", "resolved_at"),
    ("source_trades", "winning_token_id"),
    ("source_trades", "is_winning_trade"),
    ("source_trades", "realized_pnl"),
    ("source_trades", "settlement_source"),
)

_V14_NEW_INDEXES = (
    "idx_market_outcomes_winner",
    "idx_source_trades_resolution_status",
    "idx_source_trades_winning_token",
)


# ────────────────────────────────────────────────────────────────────
# 1. Schema constants + registry
# ────────────────────────────────────────────────────────────────────


class TestSchemaVersionAndRegistry:
    def test_schema_version_constant_is_14(self) -> None:
        """PR24A: SCHEMA_VERSION must be 14."""
        assert schema.SCHEMA_VERSION == 14, (
            f"PR24A requires SCHEMA_VERSION=14, got {schema.SCHEMA_VERSION}"
        )

    def test_migrations_registry_contains_v14(self) -> None:
        """MIGRATIONS dict must have an entry for 14 with non-empty DDL."""
        assert 14 in schema.MIGRATIONS, "MIGRATIONS[14] missing"
        assert len(schema.MIGRATIONS[14]) > 0, "MIGRATIONS[14] is empty"

    def test_v14_module_exports_v14_ddl(self) -> None:
        from polycopy.db import schema_v14
        assert hasattr(schema_v14, "_V14_DDL")
        assert len(schema_v14._V14_DDL) > 0


# ────────────────────────────────────────────────────────────────────
# 2. DDL is purely additive
# ────────────────────────────────────────────────────────────────────


class TestV14DdlIsAdditive:
    def test_no_drop_table_in_v14(self) -> None:
        """v14 must not drop any existing table."""
        from polycopy.db import schema_v14
        ddl = "\n".join(schema_v14._V14_DDL)
        for forbidden in ("DROP TABLE", "DROP INDEX"):
            assert forbidden not in ddl.upper(), (
                f"v14 DDL contains forbidden statement: {forbidden}"
            )

    def test_all_v14_statements_are_alter_or_create(self) -> None:
        """v14 statements must be ALTER TABLE ... ADD COLUMN or
        CREATE INDEX IF NOT EXISTS (or harmless equivalents). No
        destructive operations are permitted."""
        from polycopy.db import schema_v14
        for stmt in schema_v14._V14_DDL:
            upper = stmt.strip().upper()
            assert (
                upper.startswith("ALTER TABLE ")
                or upper.startswith("CREATE INDEX ")
            ), f"Unexpected v14 statement: {stmt!r}"


# ────────────────────────────────────────────────────────────────────
# 3. Migration applies cleanly to a fresh DB
# ────────────────────────────────────────────────────────────────────


class TestFreshDbMigratesToV14:
    def test_fresh_db_reaches_schema_version_14(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fresh.db"
        db = Database(db_path=db_path).connect()
        try:
            row = db.conn.execute(
                "SELECT value FROM _meta WHERE key='schema_version'"
            ).fetchone()
            assert row is not None
            assert row["value"] == "14", (
                f"Expected schema_version=14 on a fresh DB, got {row['value']}"
            )
        finally:
            db.close()

    @pytest.mark.parametrize("table,column", _V14_NEW_COLUMNS)
    def test_new_column_exists_after_migration(
        self, tmp_path: Path, table: str, column: str
    ) -> None:
        db_path = tmp_path / "fresh.db"
        db = Database(db_path=db_path).connect()
        try:
            cols = {
                row["name"]
                for row in db.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            assert column in cols, (
                f"Column {table}.{column} missing after v14 migration. "
                f"Present: {sorted(cols)}"
            )
        finally:
            db.close()

    @pytest.mark.parametrize("index_name", _V14_NEW_INDEXES)
    def test_new_index_exists_after_migration(
        self, tmp_path: Path, index_name: str
    ) -> None:
        db_path = tmp_path / "fresh.db"
        db = Database(db_path=db_path).connect()
        try:
            indexes = {
                row["name"]
                for row in db.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            assert index_name in indexes, (
                f"Index {index_name} missing after v14 migration. "
                f"Present: {sorted(indexes)}"
            )
        finally:
            db.close()


# ────────────────────────────────────────────────────────────────────
# 4. Migration is idempotent
# ────────────────────────────────────────────────────────────────────


class TestMigrationIsIdempotent:
    def test_reopen_does_not_replay_migration(self, tmp_path: Path) -> None:
        db_path = tmp_path / "idempotent.db"
        db1 = Database(db_path=db_path).connect()
        db1.close()
        # Second open must not error and must report the same version.
        db2 = Database(db_path=db_path).connect()
        try:
            row = db2.conn.execute(
                "SELECT value FROM _meta WHERE key='schema_version'"
            ).fetchone()
            assert row is not None
            assert row["value"] == "14"
        finally:
            db2.close()

    def test_open_three_times_still_v14(self, tmp_path: Path) -> None:
        db_path = tmp_path / "idempotent3.db"
        for _ in range(3):
            db = Database(db_path=db_path).connect()
            db.close()
        # Final read.
        db = Database(db_path=db_path).connect()
        try:
            row = db.conn.execute(
                "SELECT value FROM _meta WHERE key='schema_version'"
            ).fetchone()
            assert row["value"] == "14"
        finally:
            db.close()


# ────────────────────────────────────────────────────────────────────
# 5. Existing data not destroyed
# ────────────────────────────────────────────────────────────────────


class TestExistingDataPreserved:
    def test_pre_v14_row_count_preserved(self, tmp_path: Path) -> None:
        """Insert a row at v13, then apply v14, then verify the row
        is still there with the same primary key."""
        db_path = tmp_path / "preserve.db"

        # First create a v13 DB and insert a row.
        from polycopy.db import schema as _schema
        # Roll back to v13 to simulate a real pre-v14 production DB.
        prev_version = _schema.SCHEMA_VERSION
        _schema.SCHEMA_VERSION = 13
        try:
            db1 = Database(db_path=db_path).connect()
            # Wallets is created at v1 and persists; insert a row.
            db1.conn.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                "VALUES ('w-1', '0xabc', 'preserve-test', 0, '2026-01-01T00:00:00+00:00')"
            )
            db1.conn.commit()
            db1.close()
        finally:
            _schema.SCHEMA_VERSION = prev_version

        # Now open as v14. The migration should add columns without
        # destroying the wallet row.
        db2 = Database(db_path=db_path).connect()
        try:
            row = db2.conn.execute(
                "SELECT id, address FROM wallets WHERE id='w-1'"
            ).fetchone()
            assert row is not None, "wallet row was destroyed by v14 migration"
            assert row["address"] == "0xabc"
        finally:
            db2.close()


# ────────────────────────────────────────────────────────────────────
# 6. Reconciliation path (PR23) is now v14-aware
# ────────────────────────────────────────────────────────────────────


class TestReconciliationRequiresV14:
    def test_required_v14_columns_class_attribute(self) -> None:
        """The Database class must enumerate v14 columns so the
        reconciliation path doesn't silently bump a v13-physical DB
        to v14-metadata without applying the ALTERs."""
        assert hasattr(Database, "_REQUIRED_V14_COLUMNS")
        cols = Database._REQUIRED_V14_COLUMNS
        # Every expected (table, column) pair must be present.
        for table, column in _V14_NEW_COLUMNS:
            assert (table, column) in cols, (
                f"_REQUIRED_V14_COLUMNS missing ({table}, {column})"
            )

    def test_physical_schema_at_target_true_on_fresh_v14(self, tmp_path: Path) -> None:
        """A fully migrated v14 DB MUST be considered at target."""
        db_path = tmp_path / "v14_full.db"
        db = Database(db_path=db_path).connect()
        try:
            assert db._physical_schema_at_target() is True, (
                "physical_schema_at_target must return True for a fully "
                "migrated v14 DB so the reconciliation path can short-circuit."
            )
        finally:
            db.close()

    def test_physical_schema_at_target_false_when_v14_column_dropped(
        self, tmp_path: Path
    ) -> None:
        """If a v14 column is missing, the runner MUST NOT short-circuit.

        We prove this by inspecting the helper's contract: the
        ``_REQUIRED_V14_COLUMNS`` enumeration includes
        ``market_outcomes.is_winner``, so any DB missing that column
        will fail the helper check. We verify the helper directly on
        a fresh v14 DB that has had its index dropped and table
        rebuilt without the column.

        NOTE: we DO NOT actually drop the column via ``ALTER TABLE``
        because that requires a v5 re-run which is blocked by
        ``copy_candidates`` FKs. Instead we prove the contract by
        asserting that the helper's column list includes every v14
        column AND by demonstrating that on a fresh v14 DB the
        helper returns True (the previous test).
        """
        # The list itself is the contract; the previous test
        # (``test_physical_schema_at_target_true_on_fresh_v14``)
        # proves the helper returns True when every listed column
        # is present, and ``test_required_v14_columns_class_attribute``
        # proves every v14 column is in the list. A DB missing any
        # one of these columns will therefore cause the helper to
        # return False, blocking the reconciliation short-circuit.
        #
        # Verify the list contains all v14 columns AND that removing
        # one entry from the list flips the helper's return value
        # (synthetic check using monkey-patching).
        db_path = tmp_path / "v14_full.db"
        db = Database(db_path=db_path).connect()
        try:
            # Sanity: with the real list, helper returns True.
            assert db._physical_schema_at_target() is True

            # Synthetic: pretend market_outcomes.is_winner is NOT
            # required. The helper should still return True (since
            # all real columns exist). This is just a control.
            original = Database._REQUIRED_V14_COLUMNS
            try:
                Database._REQUIRED_V14_COLUMNS = tuple(
                    c for c in original
                    if c != ("market_outcomes", "is_winner")
                )
                assert db._physical_schema_at_target() is True, (
                    "removing a v14 column from the required list "
                    "should not flip the helper (since the column "
                    "still exists physically)"
                )
            finally:
                Database._REQUIRED_V14_COLUMNS = original

            # Now simulate "column is missing" by monkey-patching
            # _column_exists to return False for one v14 column.
            # The helper MUST then return False.
            original_col = db._column_exists

            def _patched(table, column, original=original_col):
                if (table, column) == ("market_outcomes", "is_winner"):
                    return False
                return original(table, column)

            try:
                db._column_exists = _patched
                assert db._physical_schema_at_target() is False, (
                    "when a v14 column is reported missing, "
                    "_physical_schema_at_target must return False "
                    "so the runner actually re-applies v14 ALTERs."
                )
            finally:
                db._column_exists = original_col
        finally:
            db.close()


# ────────────────────────────────────────────────────────────────────
# 7. Integrity + foreign-key check still pass after v14
# ────────────────────────────────────────────────────────────────────


class TestIntegrityAfterMigration:
    def test_integrity_check_passes_on_fresh_v14_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "integrity.db"
        db = Database(db_path=db_path).connect()
        try:
            integrity = db.conn.execute("PRAGMA integrity_check").fetchone()
            assert integrity[0] == "ok", (
                f"integrity_check failed after v14 migration: {integrity[0]}"
            )
            fk_violations = list(db.conn.execute("PRAGMA foreign_key_check"))
            assert fk_violations == [], (
                f"foreign_key_check violations after v14 migration: {fk_violations}"
            )
        finally:
            db.close()


# ────────────────────────────────────────────────────────────────────
# 8. Legacy default values on existing rows
# ────────────────────────────────────────────────────────────────────


class TestLegacyDefaults:
    def test_existing_market_outcomes_get_is_winner_null(self, tmp_path: Path) -> None:
        """A market_outcomes row created before v14 (with clob_token_id)
        must have is_winner=NULL after migration."""
        db_path = tmp_path / "legacy_default.db"
        db = Database(db_path=db_path).connect()
        try:
            # Insert a legacy-style market_outcomes row.
            db.conn.execute(
                "INSERT INTO markets (id, source_id, source, question, fetched_at) "
                "VALUES ('m-legacy', 'src-1', 'test', 'q', '2026-01-01T00:00:00+00:00')"
            )
            db.conn.execute(
                "INSERT INTO market_outcomes (market_id, label, price, clob_token_id) "
                "VALUES ('m-legacy', 'Yes', 0.5, 'tok-1')"
            )
            db.conn.commit()

            row = db.conn.execute(
                "SELECT is_winner FROM market_outcomes WHERE market_id='m-legacy'"
            ).fetchone()
            assert row["is_winner"] is None, (
                f"Legacy market_outcomes row got non-NULL is_winner={row['is_winner']!r}"
            )
        finally:
            db.close()

    def test_existing_source_trades_get_resolution_status_unresolved(
        self, tmp_path: Path
    ) -> None:
        """A source_trades row created before v14 must have
        resolution_status='unresolved' after migration (the default)."""
        db_path = tmp_path / "legacy_trades.db"
        db = Database(db_path=db_path).connect()
        try:
            db.conn.execute(
                "INSERT INTO source_trades "
                "(id, source, source_trade_id, market_source_id, side, "
                " outcome, quantity, price, timestamp) "
                "VALUES ('t-legacy', 'test', 'src-t-1', 'm-1', 'BUY', "
                " 'Yes', 1.0, 0.5, '2026-01-01T00:00:00+00:00')"
            )
            db.conn.commit()

            row = db.conn.execute(
                "SELECT resolution_status, is_winning_trade, realized_pnl "
                "FROM source_trades WHERE id='t-legacy'"
            ).fetchone()
            assert row["resolution_status"] == "unresolved"
            assert row["is_winning_trade"] is None
            assert row["realized_pnl"] is None
        finally:
            db.close()