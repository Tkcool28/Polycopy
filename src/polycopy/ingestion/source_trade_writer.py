"""PR24Z — The ONE centralized source_trade writer for the manual path.

This module is the ONLY component permitted to INSERT into ``source_trades``
for the new manual real source-trade ingestion path. It:

  * uses ``polycopy.db.database.Database.connect()`` so WAL / busy_timeout /
    wal_autocheckpoint / foreign_keys PRAGMAs apply.
  * receives validated normalized rows only (it performs NO normalization,
    NO validation, NO network access, NO scoring).
  * writes one bounded transaction per batch and commits exactly once.
  * uses INSERT OR IGNORE (dedup-safe; never INSERT OR REPLACE, never
    UPDATE/DELETE).
  * returns a structured :class:`WriteResult`.
  * never writes to any downstream table.

It deliberately imports NO network client and NO adapter. The CLI is
responsible for the dry-run gate, the production-write gates
(``--allow-live --write --confirm-production-db``), and the pre/post
integrity checks.

See PR24X audit: exactly one writer role is allowed in the architecture.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

from polycopy.db.database import Database
from polycopy.ingestion.normalized_source_trade import NormalizedSourceTrade
from polycopy.ingestion.source_trade_metadata import serialize_source_trade_metadata

# Columns inserted by the writer (matches schema v1 source_trades DDL).
_INSERT_SQL = """
INSERT OR IGNORE INTO source_trades
   (id, source, source_trade_id, market_source_id, side, outcome,
    quantity, price, trader_address, timestamp, is_sample, token_id, metadata_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_SQL_LEGACY = """
INSERT OR IGNORE INTO source_trades
   (id, source, source_trade_id, market_source_id, side, outcome,
    quantity, price, trader_address, timestamp, is_sample, token_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# The dedupe guarantee lives on (source, source_trade_id) with an enabled
# UNIQUE constraint. We assert this before any production write.
_DEDUPE_COLUMNS = ("source", "source_trade_id")


@dataclass
class BackupResult:
    """Outcome of a verified SQLite online backup of the production DB."""

    success: bool = False
    path: Optional[str] = None
    method: Optional[str] = None
    sha256: Optional[str] = None
    size: Optional[int] = None
    integrity_check: Optional[str] = None
    foreign_key_violations: Optional[int] = None
    source_trades_count: Optional[int] = None
    schema_version: Optional[int] = None
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "path": self.path,
            "method": self.method,
            "sha256": self.sha256,
            "size": self.size,
            "integrity_check": self.integrity_check,
            "foreign_key_violations": self.foreign_key_violations,
            "source_trades_count": self.source_trades_count,
            "schema_version": self.schema_version,
            "error": self.error,
        }


@dataclass
class UniqueConstraintResult:
    """Preflight result proving the dedupe uniqueness guarantee exists."""

    present: bool = False
    index_name: Optional[str] = None
    columns: list[str] = field(default_factory=list)
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "present": self.present,
            "index_name": self.index_name,
            "columns": list(self.columns),
            "error": self.error,
        }


def _sha256_file(path: str) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def create_verified_backup(db_path: str, *, backup_path: Optional[str] = None) -> BackupResult:
    """Create a WAL-safe SQLite online backup and verify it independently.

    Uses ``source_conn.backup(dest_conn)`` (the SQLite online backup API) so
    committed WAL data is folded into the backup — NOT a raw file copy of just
    the main DB file. After creation the backup is opened independently and
    checked with ``PRAGMA integrity_check``, ``PRAGMA foreign_key_check``, and
    a ``source_trades`` row count. The SHA-256 of the completed backup file is
    computed.

    The backup is valid only when success=True, integrity_check="ok",
    foreign_key_violations=0, source_trades_count is populated, sha256 is
    populated, and size>0. Callers MUST treat a non-valid result as a hard
    gate failure (do not proceed to a production write).
    """
    import datetime
    from pathlib import Path

    if backup_path is None:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = f"{db_path}.pr24z_online_backup_{ts}"
    res = BackupResult(path=backup_path, method="sqlite_online_backup")
    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        res.error = f"source open failed: {exc}"
        return res
    try:
        dst = sqlite3.connect(backup_path)
        try:
            # Online backup: folds WAL into the destination.
            src.backup(dst)
            dst.commit()
        except sqlite3.Error as exc:
            res.error = f"backup failed: {exc}"
            try:
                dst.close()
            except sqlite3.Error:
                pass
            return res
        finally:
            try:
                dst.close()
            except sqlite3.Error:
                pass
    finally:
        try:
            src.close()
        except sqlite3.Error:
            pass

    # Independently verify the backup file.
    try:
        bk = sqlite3.connect(backup_path)
        try:
            ic = bk.execute("PRAGMA integrity_check").fetchone()
            res.integrity_check = ic[0] if ic else "??"
            res.foreign_key_violations = len(list(bk.execute("PRAGMA foreign_key_check")))
            try:
                res.source_trades_count = int(
                    bk.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
                )
            except sqlite3.Error:
                res.source_trades_count = None
            # Canonical schema version lives in _meta.schema_version (NOT
            # PRAGMA schema_version, which is a connection schema cookie). Read
            # it directly from the completed backup during independent verify.
            try:
                _sv = bk.execute(
                    "SELECT value FROM _meta WHERE key='schema_version'"
                ).fetchone()
                res.schema_version = int(_sv[0]) if _sv else None
            except sqlite3.Error:
                res.schema_version = None
        finally:
            bk.close()
    except sqlite3.Error as exc:
        res.error = (res.error or "") + f"; verify open failed: {exc}"
        res.integrity_check = res.integrity_check or "error"
        res.foreign_key_violations = res.foreign_key_violations if res.foreign_key_violations is not None else -1
        return res

    res.sha256 = _sha256_file(backup_path)
    try:
        res.size = Path(backup_path).stat().st_size
    except OSError:
        res.size = None

    res.success = bool(
        res.integrity_check == "ok"
        and res.foreign_key_violations == 0
        and res.source_trades_count is not None
        and res.sha256 is not None
        and (res.size or 0) > 0
    )
    return res


def assert_unique_dedupe_constraint(db: Database) -> UniqueConstraintResult:
    """Verify a UNIQUE constraint covers exactly (source, source_trade_id).

    A production write may only proceed when an ENABLED UNIQUE index or table
    constraint covers exactly the intended dedupe columns. Returns a structured
    result; callers abort when ``present`` is False.
    """
    res = UniqueConstraintResult()
    try:
        conn = db.conn
        indexes = conn.execute("PRAGMA index_list(source_trades)").fetchall()
        for idx in indexes:
            # idx = (seq, name, unique, origin, partial)
            name = idx[1]
            unique = bool(idx[2])
            if not unique:
                continue
            cols = [r[2] for r in conn.execute(f"PRAGMA index_info({name})").fetchall()]
            if set(cols) == set(_DEDUPE_COLUMNS):
                res.present = True
                res.index_name = name
                res.columns = list(cols)
                return res
        # Fall back to table-level UNIQUE constraints.
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='source_trades'"
        ).fetchone()
        if sql and "UNIQUE(source, source_trade_id)" in sql[0]:
            res.present = True
            res.index_name = "table_constraint"
            res.columns = list(_DEDUPE_COLUMNS)
            return res
        res.error = "no enabled UNIQUE constraint covers (source, source_trade_id)"
    except sqlite3.Error as exc:
        res.error = f"inspect failed: {exc}"
    return res




@dataclass
class WriteResult:
    """Structured outcome of a single writer batch."""

    attempted: int = 0
    inserted: int = 0
    deduplicated: int = 0
    rejected: int = 0
    errors: int = 0
    committed: bool = False
    rolled_back: bool = False
    # Canonical duplicate recognition: eligible rows already present under
    # the current canonical source_trade_id.
    existing_duplicates_recognized: int = 0
    # Whether the UNIQUE(source, source_trade_id) preflight passed.
    unique_constraint_present: bool = False
    error_message: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "attempted": self.attempted,
            "inserted": self.inserted,
            "deduplicated": self.deduplicated,
            "rejected": self.rejected,
            "errors": self.errors,
            "committed": self.committed,
            "rolled_back": self.rolled_back,
            "existing_duplicates_recognized": self.existing_duplicates_recognized,
            "unique_constraint_present": self.unique_constraint_present,
            "error_message": self.error_message,
        }


def _row_tuple(c: NormalizedSourceTrade, *, include_metadata: bool = True) -> tuple:
    values = (
        str(uuid4()),
        c.source,
        c.source_trade_id,
        c.market_source_id,
        c.side,
        c.outcome or "Unknown",
        c.quantity,
        c.price,
        c.trader_address,  # canonical lowercased wallet, or None
        c.timestamp.isoformat() if c.timestamp else None,
        int(c.is_sample),
        c.token_id,
    )
    if include_metadata:
        values += (serialize_source_trade_metadata(c.metadata),)
    return values


def _metadata_column_available(conn: sqlite3.Connection) -> bool:
    """Require metadata on latest schemas; tolerate legacy isolated fixtures."""
    has_column = any(
        row[1] == "metadata_json" for row in conn.execute("PRAGMA table_info(source_trades)")
    )
    if has_column:
        return True
    try:
        row = conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()
        if row is not None and int(row[0]) >= 17:
            raise RuntimeError(
                "latest source_trades schema is missing required metadata_json column"
            )
    except sqlite3.OperationalError:
        # Narrow support for old, hand-built test fixtures without _meta.
        pass
    return False


def write_valid_rows(
    db: Database,
    rows: list[NormalizedSourceTrade],
    *,
    dry_run: bool = True,
    pre_existing_ids: Optional[set[str]] = None,
    # PR #73 seam: when False the writer performs NO commit; the caller (the
    # bounded multi-watch cohort CLI) owns the transaction so the whole cohort
    # commits or rolls back together. Defaults to True (unchanged behavior).
    auto_commit: bool = True,
) -> WriteResult:
    """Insert validated normalized rows into source_trades.

    Args:
        db: an already-connected ``Database`` (caller opened it with the gates).
        rows: validated candidates (``validation_status == "valid"``).
        dry_run: when True, perform NO writes and return a result with
            ``attempted`` set but ``committed=False``. The CLI passes
            ``dry_run=True`` for every non-production path.
        pre_existing_ids: set of canonical source_trade_ids already present in
            the DB for this source (used to count existing-duplicate
            recognition; does NOT change INSERT OR IGNORE behavior).

    Returns:
        A :class:`WriteResult`. On a production write, exactly one transaction
        is opened and committed (or rolled back on error).

    Safety:
        * Rejects any row without a stable ``source_trade_id`` (cannot dedupe).
        * Before a non-dry-run write, asserts the UNIQUE(source, source_trade_id)
          constraint exists via :func:`assert_unique_dedupe_constraint`. If the
          constraint is missing, returns ``unique_constraint_present=False`` and
          performs NO write.
        * Raises nothing to the caller; errors are counted in ``errors`` and
          the batch is rolled back.
    """
    result = WriteResult()
    # Only rows that are valid AND carry a stable id are eligible.
    eligible = [r for r in rows if r.validation_status == "valid" and r.source_trade_id]
    result.attempted = len(eligible)
    result.rejected = len(rows) - len(eligible)

    # Existing-duplicate recognition is canonical-only: if the current
    # source_trade_id already exists for this source, INSERT OR IGNORE will
    # deduplicate it through UNIQUE(source, source_trade_id). No PR24Z legacy
    # alias/fallback matching is performed in the permanent writer path.
    pre = pre_existing_ids or set()
    for c in eligible:
        sid = c.source_trade_id
        if sid is not None and sid in pre:
            result.existing_duplicates_recognized += 1

    if dry_run:
        # Pure dry-run: never open a transaction, never write.
        return result

    # UNIQUE preflight — required before a real write.
    preflight = assert_unique_dedupe_constraint(db)
    result.unique_constraint_present = preflight.present
    if not preflight.present:
        result.rolled_back = True
        result.errors += 1
        result.error_message = f"UNIQUE dedupe constraint missing: {preflight.error}"
        return result

    if not eligible:
        # Nothing to write; not an error, not a transaction.
        result.committed = True
        return result

    conn = db.conn
    try:
        # One bounded transaction for the whole batch.
        # Legacy support is deliberately narrow: latest-schema databases must
        # expose metadata_json; only pre-v17/hand-built isolated fixtures use
        # the legacy insert shape.
        include_metadata = _metadata_column_available(conn)
        insert_sql = _INSERT_SQL if include_metadata else _INSERT_SQL_LEGACY
        inserted = 0
        for c in eligible:
            cur = conn.execute(insert_sql, _row_tuple(c, include_metadata=include_metadata))
            # INSERT OR IGNORE: rowcount == 1 fresh, 0 duplicate (UNIQUE hit).
            if getattr(cur, "rowcount", 0) == 1:
                inserted += 1
        if auto_commit:
            # Standalone single-watch path: commit immediately as before.
            conn.commit()
            result.committed = True
        # auto_commit=False: caller (cohort) owns the transaction; do NOT
        # commit here. result.committed stays False.
        result.inserted = inserted
        result.deduplicated = result.attempted - inserted
    except sqlite3.Error as exc:
        if auto_commit:
            # Standalone path: roll back the local transaction.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        # auto_commit=False: propagate the exception so the cohort owner
        # performs the one outer rollback. Do NOT roll back here.
        result.rolled_back = True
        result.errors += 1
        result.error_message = f"{type(exc).__name__}: {exc}"[:300]
        return result
    return result
