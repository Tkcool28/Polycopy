"""Shared operational DB helper for the research-evidence CLIs.

This is the ONE place every new CLI opens a database. It consolidates
read-only and write-path production safety so the six CLIs no longer carry
divergent, partly-broken local guard implementations.

Read-only / dry-run paths
--------------------------
* Open an EXISTING database with raw SQLite:
  ``file:<resolved-path>?mode=ro``.
* Never use ``immutable=1``.
* Never call ``Database().connect()``.
* Never create a missing database.
* Never run a migration.
* Verify ``_meta.schema_version == 21`` and fail clearly otherwise.

Write paths
-----------
* Resolve all paths and symlinks with ``Path.resolve()``.
* Recognize BOTH production locations:
    /root/Polycopy/data/polycopy.db
    <current-repository>/data/polycopy.db
* Require ALL THREE production gates:
    --write --allow-live --confirm-production-db
* Preflight schema v21 through a raw ``mode=ro`` connection BEFORE opening
  writable.
* Refuse (rather than auto-migrate) if the schema is not exactly 21.
* Never depend on ``Database().connect()`` to perform an operational migration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional, Sequence

# The research-evidence schema version every CLI requires.
REQUIRED_SCHEMA_VERSION = 21

# Recognized production DB locations (resolved at module load).
_REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_DB_REPO_RELATIVE = (_REPO_ROOT / "data" / "polycopy.db").resolve()
PRODUCTION_DB_ABSOLUTE = Path("/root/Polycopy/data/polycopy.db").resolve()


class DbConn:
    """Minimal connection wrapper matching the ``Database`` method surface.

    Exposes ``.fetchone(sql, params)``, ``.fetchall(sql, params)``,
    ``.execute(sql, params)``, ``.conn`` (the raw connection), ``.commit()``
    and ``.close()`` so legacy call sites keep working unchanged.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None):
        return self._conn.execute(sql, params or [])

    def fetchone(self, sql: str, params: Optional[Sequence[Any]] = None):
        cur = self._conn.execute(sql, params or [])
        return cur.fetchone()

    def fetchall(self, sql: str, params: Optional[Sequence[Any]] = None):
        cur = self._conn.execute(sql, params or [])
        return cur.fetchall()

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def resolve_db_path(db_path: str) -> Path:
    """Resolve a possibly-relative/symlinked db path to its canonical form."""
    try:
        return Path(db_path).resolve()
    except OSError:
        return Path(db_path)


def is_production_db(db_path: str) -> bool:
    """True iff the resolved path matches one of the recognized production DBs."""
    try:
        return resolve_db_path(db_path) in (
            PRODUCTION_DB_REPO_RELATIVE,
            PRODUCTION_DB_ABSOLUTE,
        )
    except OSError:
        return False


def _read_schema_version(conn: sqlite3.Connection) -> int:
    """Read ``_meta.schema_version`` via raw SQLite (no migration machinery)."""
    try:
        row = conn.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        # No _meta table -> pre-v1 / uninitialized -> never v21.
        return 0
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _preflight_schema_v21(uri: str) -> int:
    """Open a raw read-only connection and return the schema version.

    Uses ``mode=ro`` (NOT ``immutable=1``). The caller guarantees the file
    exists (read paths) so this never silently creates a DB.
    """
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        return _read_schema_version(conn)
    finally:
        conn.close()


def open_readonly(db_path: str) -> DbConn:
    """Open an existing DB read-only with a raw ``mode=ro`` SQLite connection.

    Never creates the file, never migrates, never uses ``immutable=1``. Verifies
    the schema is exactly v21 and raises ``RuntimeError`` if it is not.
    """
    resolved = resolve_db_path(db_path)
    if not resolved.exists():
        raise FileNotFoundError(
            f"database not found (read-only paths never create a DB): {resolved}"
        )
    uri = f"file:{resolved}?mode=ro"
    version = _preflight_schema_v21(uri)
    if version != REQUIRED_SCHEMA_VERSION:
        raise RuntimeError(
            f"schema version mismatch: required exactly "
            f"{REQUIRED_SCHEMA_VERSION}, found {version} at {resolved}"
        )
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return DbConn(conn)


def require_write_gates(args: Any, *, db_path: str) -> bool:
    """Return True iff a write is permitted under the production-safety gates.

    Fail-closed: a write requires ``--write`` AND, when the target is a
    recognized production DB, BOTH ``--allow-live`` AND ``--confirm-production-db``.
    Dry-run (``--dry-run``) is never a write.
    """
    if getattr(args, "dry_run", False):
        return False
    if not getattr(args, "write", False):
        return False
    if is_production_db(db_path):
        if not (
            getattr(args, "allow_live", False)
            and getattr(args, "confirm_production_db", False)
        ):
            return False
    return True


def open_writable(db_path: str, args: Any) -> DbConn:
    """Open a writable connection ONLY after all production gates pass.

    Refuses (raises ``RuntimeError``) when the caller has not satisfied
    :func:`require_write_gates` (which itself requires the full three-gate set on
    recognized production paths and ``--write`` everywhere else). Preflights
    schema v21 through a raw read-only connection first so we never auto-migrate.
    Opens writable with raw SQLite (foreign keys ON) — this path does NOT call
    ``Database().connect()`` and therefore never runs an operational migration.
    """
    if not require_write_gates(args, db_path=db_path):
        raise RuntimeError(
            "refused: write requires --write"
            + (" --allow-live --confirm-production-db" if is_production_db(db_path) else "")
        )
    resolved = resolve_db_path(db_path)
    if not resolved.exists():
        raise FileNotFoundError(
            f"database not found (writable paths never create a DB): {resolved}"
        )
    # Preflight: schema must already be exactly v21. Refuse to auto-migrate.
    version = _preflight_schema_v21(f"file:{resolved}?mode=ro")
    if version != REQUIRED_SCHEMA_VERSION:
        raise RuntimeError(
            f"schema version mismatch: required exactly "
            f"{REQUIRED_SCHEMA_VERSION}, found {version} at {resolved}; "
            f"refusing to auto-migrate"
        )
    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return DbConn(conn)
