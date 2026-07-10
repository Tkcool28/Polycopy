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

import sqlite3
from dataclasses import dataclass
from typing import Optional
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
) -> WriteResult:
    """Insert validated normalized rows into source_trades.

    Args:
        db: an already-connected ``Database`` (caller opened it with the gates).
        rows: validated candidates (``validation_status == "valid"``).
        dry_run: when True, perform NO writes and return a result with
            ``attempted`` set but ``committed=False``. The CLI passes
            ``dry_run=True`` for every non-production path.

    Returns:
        A :class:`WriteResult`. On a production write, exactly one transaction
        is opened and committed (or rolled back on error).

    Safety:
        * Rejects any row without a stable ``source_trade_id`` (cannot dedupe).
        * Raises nothing to the caller; errors are counted in ``errors`` and
          the batch is rolled back.
    """
    result = WriteResult()
    # Only rows that are valid AND carry a stable id are eligible.
    eligible = [r for r in rows if r.validation_status == "valid" and r.source_trade_id]
    result.attempted = len(eligible)
    result.rejected = len(rows) - len(eligible)

    if dry_run:
        # Pure dry-run: never open a transaction, never write.
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
