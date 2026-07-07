"""PR24C: Tests that Database.connect() enforces SQLite safety PRAGMAs.

Verifies that every fresh Database(db_path).connect() sets:
  - PRAGMA foreign_keys      = 1
  - PRAGMA journal_mode      = wal
  - PRAGMA busy_timeout      = 30000
  - PRAGMA wal_autocheckpoint = 1000

And that closing + reconnecting to the same file preserves journal_mode=wal
(via the file header) and re-applies the per-connection PRAGMAs.

These tests are deliberately scoped to the Database wrapper. They do NOT
exercise the API, the timers, or any ingestion path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from polycopy.db.database import Database


def _pragma_int(db: Database, name: str) -> int:
    """Run a per-connection PRAGMA and return its integer value."""
    row = db.conn.execute(f"PRAGMA {name}").fetchone()
    assert row is not None, f"PRAGMA {name} returned no row"
    return int(row[0])


def _pragma_text(db: Database, name: str) -> str:
    """Run a per-connection PRAGMA and return its text value."""
    row = db.conn.execute(f"PRAGMA {name}").fetchone()
    assert row is not None, f"PRAGMA {name} returned no row"
    return str(row[0])


class TestFreshConnectEnforcesSafetyPragmas:
    """A brand-new Database(db_path).connect() must enable every safety PRAGMA."""

    def test_fresh_connect_enables_foreign_keys(self, tmp_path: Path):
        db_path = tmp_path / "fresh-fk.db"
        with Database(db_path=db_path) as db:
            assert _pragma_int(db, "foreign_keys") == 1

    def test_fresh_connect_enables_wal(self, tmp_path: Path):
        db_path = tmp_path / "fresh-wal.db"
        with Database(db_path=db_path) as db:
            assert _pragma_text(db, "journal_mode") == "wal"

    def test_fresh_connect_sets_busy_timeout(self, tmp_path: Path):
        db_path = tmp_path / "fresh-busy.db"
        with Database(db_path=db_path) as db:
            assert _pragma_int(db, "busy_timeout") == 30_000

    def test_fresh_connect_sets_wal_autocheckpoint(self, tmp_path: Path):
        db_path = tmp_path / "fresh-checkpoint.db"
        with Database(db_path=db_path) as db:
            assert _pragma_int(db, "wal_autocheckpoint") == 1_000

    def test_fresh_connect_all_safety_pragmas_together(self, tmp_path: Path):
        """Single-connection check that all four are simultaneously correct."""
        db_path = tmp_path / "fresh-all.db"
        with Database(db_path=db_path) as db:
            assert _pragma_int(db, "foreign_keys") == 1
            assert _pragma_text(db, "journal_mode") == "wal"
            assert _pragma_int(db, "busy_timeout") == 30_000
            assert _pragma_int(db, "wal_autocheckpoint") == 1_000


class TestReopenPreservesSafetyPragmas:
    """close() + reconnect() must re-apply per-connection PRAGMAs and
    preserve journal_mode=wal via the file header."""

    def test_reopen_journal_mode_still_wal(self, tmp_path: Path):
        db_path = tmp_path / "reopen-wal.db"
        db = Database(db_path=db_path)
        db.connect()
        assert _pragma_text(db, "journal_mode") == "wal"
        db.close()

        db2 = Database(db_path=db_path)
        db2.connect()
        try:
            assert _pragma_text(db2, "journal_mode") == "wal"
        finally:
            db2.close()

    def test_reopen_busy_timeout_reapplied(self, tmp_path: Path):
        db_path = tmp_path / "reopen-busy.db"
        db = Database(db_path=db_path)
        db.connect()
        assert _pragma_int(db, "busy_timeout") == 30_000
        db.close()

        db2 = Database(db_path=db_path)
        db2.connect()
        try:
            assert _pragma_int(db2, "busy_timeout") == 30_000
        finally:
            db2.close()

    def test_reopen_all_safety_pragmas(self, tmp_path: Path):
        db_path = tmp_path / "reopen-all.db"
        # First session: initialize.
        db = Database(db_path=db_path)
        db.connect()
        assert _pragma_text(db, "journal_mode") == "wal"
        db.close()

        # Second session: reopen and verify every pragma is correct.
        db2 = Database(db_path=db_path)
        db2.connect()
        try:
            assert _pragma_int(db2, "foreign_keys") == 1
            assert _pragma_text(db2, "journal_mode") == "wal"
            assert _pragma_int(db2, "busy_timeout") == 30_000
            assert _pragma_int(db2, "wal_autocheckpoint") == 1_000
        finally:
            db2.close()


class TestSafetyPragmasDoNotBreakMigrationFlow:
    """Migrations must still run to SCHEMA_VERSION with safety PRAGMAs on."""

    def test_migrations_complete_with_wal_on(self, tmp_path: Path):
        from polycopy.db.schema import SCHEMA_VERSION

        db_path = tmp_path / "migrate-wal.db"
        with Database(db_path=db_path) as db:
            row = db.fetchone(
                "SELECT value FROM _meta WHERE key = 'schema_version'"
            )
            assert row is not None
            assert int(row["value"]) == SCHEMA_VERSION

    def test_safety_pragmas_survive_after_migration(self, tmp_path: Path):
        """PRAGMAs are set before migrations; they must still hold after."""
        db_path = tmp_path / "post-migrate.db"
        with Database(db_path=db_path) as db:
            assert _pragma_int(db, "foreign_keys") == 1
            assert _pragma_text(db, "journal_mode") == "wal"
            assert _pragma_int(db, "busy_timeout") == 30_000
            assert _pragma_int(db, "wal_autocheckpoint") == 1_000

    def test_idempotent_open_after_migration(self, tmp_path: Path):
        """A second connect() on the same file must still enforce PRAGMAs."""
        db_path = tmp_path / "idempotent-open.db"
        # Open + close twice.
        for _ in range(2):
            db = Database(db_path=db_path)
            db.connect()
            assert _pragma_int(db, "foreign_keys") == 1
            assert _pragma_text(db, "journal_mode") == "wal"
            assert _pragma_int(db, "busy_timeout") == 30_000
            assert _pragma_int(db, "wal_autocheckpoint") == 1_000
            db.close()


class TestRawSqliteConnectDoesNotInheritPragmas:
    """Sanity check: a raw sqlite3.connect (no Database wrapper) does NOT
    automatically get these PRAGMAs. The PR24C contract is that callers
    must go through the Database wrapper to get the safety guarantees.

    This guards against future refactors that bypass Database.connect().
    """

    def test_raw_connect_default_journal_mode_is_delete(self, tmp_path: Path):
        db_path = tmp_path / "raw.db"
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            assert row is not None
            assert str(row[0]) == "delete", (
                "Raw sqlite3 default changed; the safety contract relies on "
                "Database.connect() to explicitly set WAL."
            )
        finally:
            conn.close()