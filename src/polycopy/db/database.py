"""Database connection and migration runner."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

from polycopy.db.schema import SCHEMA_VERSION, MIGRATIONS

logger = logging.getLogger(__name__)


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

    def _run_migrations(self) -> None:
        """Apply all pending migrations in order."""
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
                self.conn.execute(stmt)
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
