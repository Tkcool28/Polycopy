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
from typing import Any, Optional
from uuid import uuid4

from polycopy.db.database import Database
from polycopy.ingestion.normalized_source_trade import NormalizedSourceTrade

# Columns inserted by the writer (matches schema v1 source_trades DDL).
_INSERT_SQL = """
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


def derive_legacy_fallback_id(candidate: "NormalizedSourceTrade") -> Optional[str]:
    """Recompute the legacy deterministic-composite (fallback) identity.

    Used ONLY for compatibility in :func:`write_valid_rows` so a row whose
    canonical strong id is new but whose recomputed legacy fallback id matches a
    pre-correction row is still recognized as a duplicate (no re-insert).
    """
    from polycopy.ingestion.normalized_source_trade import _fallback_identity

    if candidate is None:
        return None
    raw = {
        "proxyWallet": candidate.trader_address,
        "asset": candidate.token_id,
        "conditionId": candidate.market_source_id,
        "side": candidate.side,
        "outcome": candidate.outcome,
        "price": candidate.price,
        "size": candidate.quantity,
        "timestamp": candidate.timestamp.isoformat() if candidate.timestamp else None,
    }
    return _fallback_identity(raw)


def legacy_fallback_id_from_db_row(row: Any) -> Optional[str]:
    """Recompute legacy fallback id from a DB ``source_trades`` row object.

    ``row`` is a sqlite3.Row. Used by the CLI to build the legacy-id set for
    the 14 previously-persisted rows so a rerun dedupes against them.
    """
    from polycopy.ingestion.normalized_source_trade import _fallback_identity

    raw = {
        "proxyWallet": row["trader_address"] if "trader_address" in row.keys() else None,
        "asset": row["token_id"] if "token_id" in row.keys() else None,
        "conditionId": row["market_source_id"] if "market_source_id" in row.keys() else None,
        "side": row["side"] if "side" in row.keys() else None,
        "outcome": row["outcome"] if "outcome" in row.keys() else None,
        "price": row["price"] if "price" in row.keys() else None,
        "size": row["quantity"] if "quantity" in row.keys() else None,
        "timestamp": row["timestamp"] if "timestamp" in row.keys() else None,
    }
    return _fallback_identity(raw)


@dataclass
class IdentityCompatibilityGate:
    """HARD GATE — legacy identity compatibility for the historical 14 rows.

    No future production ``source_trade`` write is permitted until the 14
    historical PR24Z rows are proven compatible with the corrected identity
    logic (which prefers ``sourceProvidedTradeId`` as the canonical strong
    identity). The gate runs read-only and proves each historical row is
    recognized as already-present under EITHER its canonical strong id OR its
    recomputed legacy fallback id.

    Fail-closed: if any historical row is unrecognized, or the counts don't
    reconcile, ``safe_for_future_production_write`` is False and the CLI must
    abort before backup/writer/transaction.
    """

    checked: bool = False
    historical_rows_expected: int = 14
    historical_rows_examined: int = 0
    canonical_matches: int = 0
    legacy_alias_matches: int = 0
    unmatched: int = 0
    rerun_would_insert: int = 0
    safe_for_future_production_write: bool = False
    rows: list[dict] = field(default_factory=list)
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "historical_rows_expected": self.historical_rows_expected,
            "historical_rows_examined": self.historical_rows_examined,
            "canonical_matches": self.canonical_matches,
            "legacy_alias_matches": self.legacy_alias_matches,
            "unmatched": self.unmatched,
            "rerun_would_insert": self.rerun_would_insert,
            "safe_for_future_production_write": self.safe_for_future_production_write,
            "row_detail": self.rows,
            "error": self.error,
        }


def run_identity_compatibility_gate(
    source: str,
    historical_records: list[dict[str, Any]],
    *,
    db: Optional[Database] = None,
    db_path: Optional[str] = None,
    expected: int = 14,
) -> IdentityCompatibilityGate:
    """Run the HARD legacy-identity compatibility gate (read-only).

    Args:
        source: the ingestion source name (``polymarket_data_api_trades_user``).
        historical_records: the 14 historical production records (raw dicts with
            the same fields used by :func:`normalize_source_trade`). They carry the
            upstream ``sourceProvidedTradeId`` (the corrected canonical strong id).
        db / db_path: an open read-only ``Database`` OR a DB path to open read-only.
            Exactly one must be supplied.
        expected: expected number of historical rows (default 14).

    Returns:
        An :class:`IdentityCompatibilityGate`. ``safe_for_future_production_write``
        is True only when every historical row is recognized (canonical or legacy
        alias), ``rerun_would_insert == 0``, and ``unmatched == 0``.

    This function performs NO write. It reads existing ``source_trade_id`` values
    and recomputes legacy fallback ids; it never UPDATEs/DELETEs the 14 rows nor
    inserts replacement rows.
    """
    from polycopy.ingestion.normalized_source_trade import normalize_source_trade

    gate = IdentityCompatibilityGate(historical_rows_expected=expected)
    gate.checked = True

    own_db = None
    if db is not None:
        conn = db.conn
    else:
        if db_path is None:
            gate.error = "no db or db_path provided"
            return gate
        try:
            import sqlite3 as _sqlite3

            own_db = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            own_db.row_factory = _sqlite3.Row
            conn = own_db
        except Exception as exc:  # pragma: no cover - defensive
            gate.error = f"open failed: {exc}"
            return gate

    try:
        existing_ids = {
            r[0]
            for r in conn.execute(
                "SELECT source_trade_id FROM source_trades WHERE source = ?", (source,)
            ).fetchall()
        }
        db_rows = conn.execute(
            "SELECT id, source, source_trade_id, market_source_id, token_id, "
            "trader_address, side, outcome, quantity, price, timestamp, is_sample "
            "FROM source_trades WHERE source = ?",
            (source,),
        ).fetchall()
        legacy_set = {legacy_fallback_id_from_db_row(r) for r in db_rows}
        legacy_set.discard(None)
    except Exception as exc:
        gate.error = f"read failed: {exc}"
        return gate
    finally:
        if own_db is not None:
            try:
                own_db.close()
            except Exception:
                pass

    canonical = 0
    legacy_alias = 0
    unmatched = 0
    rerun_insert = 0
    detail: list[dict] = []
    for rec in historical_records:
        # Corrected canonical strong id (as the current pipeline would compute it).
        cand = normalize_source_trade(dict(rec), record_index=0)
        corrected_id = cand.source_trade_id
        upstream_id = rec.get("sourceProvidedTradeId") or rec.get("source_trade_id")
        hist_fallback = rec.get("source_trade_id")
        existing_db_id = rec.get("source_trade_id") in existing_ids
        canonical_match = corrected_id in existing_ids
        # Recompute THIS record's legacy fallback id and check it against the set
        # of legacy fallback ids already persisted. This is the dual-ID safety
        # net: a row whose canonical strong id differs but whose legacy fallback
        # matches an existing row is still recognized (no re-insert).
        legacy_id = derive_legacy_fallback_id(cand)
        legacy_match = (not canonical_match) and (legacy_id in legacy_set)
        recognized = existing_db_id or canonical_match or legacy_match
        would_insert = 0 if recognized else 1

        canonical += int(canonical_match)
        legacy_alias += int(legacy_match)
        unmatched += int(not recognized)
        rerun_insert += would_insert
        detail.append({
            "existing_db_source_trade_id": rec.get("source_trade_id"),
            "historical_fallback_source_trade_id": hist_fallback,
            "upstream_source_provided_trade_id": upstream_id,
            "corrected_canonical_strong_id": corrected_id,
            "existing_id_equals_corrected_id": bool(existing_db_id),
            "legacy_fallback_alias_matches_existing": bool(legacy_match),
            "recognized_as_existing": bool(recognized),
            "would_insert_on_rerun": would_insert,
        })

    gate.historical_rows_examined = len(historical_records)
    gate.canonical_matches = canonical
    gate.legacy_alias_matches = legacy_alias
    gate.unmatched = unmatched
    gate.rerun_would_insert = rerun_insert
    gate.rows = detail
    gate.safe_for_future_production_write = bool(
        len(historical_records) == expected
        and unmatched == 0
        and rerun_insert == 0
    )
    return gate



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
    # Compatibility: how many eligible rows were recognized as already present
    # in the DB (either by canonical strong id OR by legacy fallback id).
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


def _row_tuple(c: NormalizedSourceTrade) -> tuple:
    return (
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


def write_valid_rows(
    db: Database,
    rows: list[NormalizedSourceTrade],
    *,
    dry_run: bool = True,
    pre_existing_ids: Optional[set[str]] = None,
    legacy_fallback_ids: Optional[set[str]] = None,
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
        legacy_fallback_ids: set of legacy fallback ids persisted by the
            pre-correction path. A row whose canonical strong id is NOT already
            present but whose recomputed legacy fallback id IS present is still
            recognized as a duplicate (prevents re-inserting the 14 existing
            rows under new ids on a rerun).

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

    # Existing-duplicate recognition. A row is recognized as an existing
    # duplicate when EITHER its canonical strong id OR its recomputed legacy
    # fallback id matches a pre-existing row. Recognized rows are skipped on the
    # real write (dual-ID dedupe), not merely counted.
    pre = pre_existing_ids or set()
    leg = legacy_fallback_ids or set()
    skip_ids: set[str] = set()
    for c in eligible:
        sid = c.source_trade_id
        if sid is None:
            continue
        if sid in pre:
            result.existing_duplicates_recognized += 1
            skip_ids.add(sid)
            continue
        if leg:
            lid = derive_legacy_fallback_id(c)
            if lid is not None and lid in leg:
                result.existing_duplicates_recognized += 1
                skip_ids.add(sid)

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
        inserted = 0
        for c in eligible:
            if c.source_trade_id in skip_ids:
                # Already recognized as an existing duplicate via dual-ID dedupe.
                continue
            cur = conn.execute(_INSERT_SQL, _row_tuple(c))
            # INSERT OR IGNORE: rowcount == 1 fresh, 0 duplicate (UNIQUE hit).
            if getattr(cur, "rowcount", 0) == 1:
                inserted += 1
        conn.commit()
        result.inserted = inserted
        result.deduplicated = result.attempted - inserted
        result.committed = True
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        result.rolled_back = True
        result.errors += 1
        result.error_message = f"{type(exc).__name__}: {exc}"[:300]
    return result
