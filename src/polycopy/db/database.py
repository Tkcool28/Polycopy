"""Database connection and migration runner.

The migration runner is intentionally tolerant of additive ``ALTER TABLE
ADD COLUMN`` statements. SQLite prior to 3.35 lacks portable ``ALTER TABLE
... ADD COLUMN IF NOT EXISTS`` syntax, so we apply each statement through a
small guard that uses ``pragma_table_info`` to detect existing columns and
skip the ``ALTER`` when the column is already present. ``CREATE INDEX IF
NOT EXISTS`` is natively idempotent in SQLite and passes through unchanged.

This guard applies only to v7 (added in the PR-1 recovery sequence); the
older v1–v6 migrations keep their original behavior. The guard is
implemented inside the migration runner rather than the schema registry so
that future additive-only migrations get the same protection automatically.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

from polycopy.db.schema import SCHEMA_VERSION, MIGRATIONS

logger = logging.getLogger(__name__)


# Match an additive ``ALTER TABLE <name> ADD COLUMN <col> <type>`` statement
# (case-insensitive, optional whitespace, semicolon tolerated). The regex is
# intentionally narrow — only additive ADD COLUMN statements are recognized.
# Anything else (DROP, RENAME, UPDATE, INSERT, ...) is passed through to
# ``conn.execute`` unchanged. group(1)=table, group(2)=column.
_ADD_COLUMN_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+(?P<table>[\w\"]+)\s+ADD\s+COLUMN\s+(?P<column>[\w\"]+)\b",
    re.IGNORECASE,
)


class Database:
    """Thin wrapper around sqlite3.Connection with versioned schema management."""

    def __init__(self, db_path: Path, echo: bool = False) -> None:
        self.db_path = db_path
        self.echo = echo
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    def connect(self) -> "Database":
        """Open (or create) the SQLite database and run pending migrations."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.echo:
            self._conn.set_trace_callback(lambda sql: logger.debug("SQL: %s", sql))
        self._run_migrations()
        return self

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── Migration logic ─────────────────────────────────────────────────────

    def _current_version(self) -> int:
        """Read the current schema version from _meta, or 0 if not initialized."""
        try:
            row = self.conn.execute("SELECT value FROM _meta WHERE key = 'schema_version'").fetchone()
            return int(row["value"]) if row else 0
        except sqlite3.OperationalError:
            # _meta table doesn't exist yet
            return 0

    def _set_version(self, version: int) -> None:
        """Write the schema version to _meta."""
        self.conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(version),),
        )
        self.conn.commit()

    # ── Idempotent-migration guard (PR-1 / v7) ─────────────────────────────

    @staticmethod
    def _strip_sql_quotes(identifier: str) -> str:
        """Strip surrounding double quotes / brackets / backticks from an identifier."""
        s = identifier.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "`"):
            return s[1:-1]
        if len(s) >= 2 and s[0] == "[" and s[-1] == "]":
            return s[1:-1]
        return s

    def _column_exists(self, table: str, column: str) -> bool:
        """Return True if ``table`` already has a column named ``column``.

        Uses ``pragma_table_info`` which returns one row per column. ``pragma_*
        `` calls accept table names without quoting. We defensively strip the
        outer quotes the schema may emit so the lookup works.
        """
        try:
            rows = self.conn.execute(
                f"PRAGMA table_info({self._strip_sql_quotes(table)})"
            ).fetchall()
        except sqlite3.OperationalError:
            # Table doesn't exist yet — treat as "column absent" so the
            # surrounding migration can run / create the table first.
            return False
        target = self._strip_sql_quotes(column)
        for row in rows:
            # pragma_table_info columns: cid, name, type, notnull, dflt_value, pk
            if row["name"] == target:
                return True
        return False

    def _execute_migration_statement(self, stmt: str) -> None:
        """Execute one migration statement, guarded by column-existence check.

        ``ALTER TABLE <t> ADD COLUMN <c> ...`` is skipped iff ``<t>.<c>`` already
        exists (verified via ``PRAGMA table_info``). Any other statement
        (``CREATE TABLE``, ``CREATE INDEX IF NOT EXISTS``, ``INSERT``,
        ``UPDATE``, ``DELETE``, ``DROP``, ``RENAME``, ...) is executed as-is.
        This is what makes the v7 migration idempotent without breaking any
        of the v1–v6 migrations or other statement types.
        """
        m = _ADD_COLUMN_RE.match(stmt)
        if m is not None:
            table = m.group("table")
            column = m.group("column")
            if self._column_exists(table, column):
                logger.debug(
                    "migration skip (column already exists): table=%s column=%s",
                    table, column,
                )
                return
        self.conn.execute(stmt)

    def _run_migrations(self) -> None:
        """Apply all pending migrations in order.

        Each migration statement is executed via
        :meth:`_execute_migration_statement` so ``ALTER TABLE ... ADD COLUMN``
        statements are de-duplicated against the live schema. This makes the
        additive v7 migration safe to re-run on a database that has already
        reached v7 (e.g. after a partial application), without breaking the
        semantics of the destructive v1–v6 migrations.
        """
        current = self._current_version()
        if current == SCHEMA_VERSION:
            logger.debug("Schema at version %d, no migrations needed.", current)
            return

        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version ({current}) is newer than code ({SCHEMA_VERSION}). "
                "Upgrade polycopy or use a newer database."
            )

        logger.info("Migrating schema from version %d to %d.", current, SCHEMA_VERSION)
        for target_version in range(current + 1, SCHEMA_VERSION + 1):
            statements = MIGRATIONS.get(target_version)
            if statements is None:
                raise RuntimeError(f"No migration defined for version {target_version}.")
            for stmt in statements:
                self._execute_migration_statement(stmt)
            self._set_version(target_version)
            logger.info("Applied migration to version %d.", target_version)

        self.conn.commit()

    # ── Convenience query helpers ───────────────────────────────────────────

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()


# ── Singleton accessor ──────────────────────────────────────────────────────────

_db: Optional[Database] = None


def get_database(reload: bool = False) -> Database:
    """Return a connected Database using app settings. Use reload=True to reconnect."""
    global _db
    if _db is not None and not reload:
        return _db
    from polycopy.config.settings import get_settings

    settings = get_settings()
    _db = Database(db_path=settings.db_path, echo=settings.db_echo)
    _db.connect()
    return _db
