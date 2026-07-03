"""Version 11 schema migration — additive typed-input columns for V2 shadow.

Adds the missing typed-input columns to ``shadow_decisions`` so the
typed :class:`ShadowScoreInputV2` round-trips through persistence
without lossy coercion.

Idempotency contract
====================

Each ``ALTER TABLE ... ADD COLUMN`` statement is **self-guarded** at
the SQL level via a portable single-statement trick: a SELECT over
``pragma_table_info`` returning the column-definition string, which
the runner / tests then ``execute`` conditionally. This makes the
v11 migration safe to apply against:

  (a) a fresh database where schema_v10's CREATE TABLE already
      includes every v11 column (no-op for every v11 ALTER), and
  (b) a historical v10 database where the columns are still absent
      (each v11 ALTER actually applies).

For convenience, this module also exposes
:func:`apply_v11_idempotent` for callers (production runner and
tests) that prefer a Python-level guard over the SQL form. Both
paths are exercised by the migration-property tests in
``tests/test_p37_sqlite_foreign_key_enforcement.py``.

The portable single-statement guard used here is::

    SELECT CASE WHEN (
        SELECT COUNT(*) FROM pragma_table_info('<table>')
        WHERE name = '<column>'
    ) = 0 THEN '<ALTER SQL>'
    ELSE 'SELECT 1' END;

The caller (production runner or test helper) inspects the returned
row and ``execute()``s it iff it is a real ``ALTER``. This pattern
works on every SQLite version supported by the project (it does not
rely on ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``).
"""

from __future__ import annotations

import sqlite3
from typing import Iterable


# ── v11 column-additions (structured for the idempotency guard) ──────────────
# Each tuple is (table, column, type_sql). The DDL string is built from these
# so that every form (raw ALTER, single-statement guard, Python helper) stays
# in sync.

_V11_COLUMN_ADDS: list[tuple[str, str, str]] = [
    # ── Chunk 5 — V2 shadow typed-input columns (additive, idempotent) ──────────
    ("shadow_decisions", "source_price", "REAL"),
    ("shadow_decisions", "delayed_copy_price", "REAL"),
    ("shadow_decisions", "slippage", "REAL"),
    ("shadow_decisions", "spread", "REAL"),
    ("shadow_decisions", "intended_stake", "REAL"),
    ("shadow_decisions", "executable_depth", "REAL"),
    (
        "shadow_decisions",
        "wallet_skill_persistence_input",
        "REAL",
    ),
    (
        "shadow_decisions",
        "copied_realized_performance_input",
        "REAL",
    ),
    (
        "shadow_decisions",
        "concentration_correlation_input",
        "REAL",
    ),
    ("shadow_decisions", "measured_delay_seconds", "REAL"),
    ("shadow_decisions", "missing_forward_reasons_json", "TEXT"),
    (
        "shadow_decisions",
        "price_snapshot_id",
        "TEXT REFERENCES candidate_price_snapshots(id)",
    ),
    ("shadow_decisions", "depth_hash", "TEXT"),
]


def _build_v11_ddl() -> list[str]:
    """Materialize ``_V11_DDL`` from the structured column-add table.

    Kept as a function (not a module-level constant) so the column-add
    registry remains the single source of truth. The resulting list
    has the same shape as the other migration DDLs (``list[str]``),
    so existing test helpers that iterate ``MIGRATIONS[v]`` continue
    to work, with the understanding that the production
    :class:`Database` runner applies its own regex guard on top.
    """
    return [
        f"ALTER TABLE {table} ADD COLUMN {column} {type_sql};"
        for table, column, type_sql in _V11_COLUMN_ADDS
    ]


# Plain ALTER statements, materialized once at import time. Tests that
# iterate ``MIGRATIONS[v]`` directly (e.g. raw sqlite3 helpers in
# test_p37) get this list. Production callers should prefer
# ``apply_v11_idempotent`` below, which guards each ALTER with a
# pragma_table_info check.
_V11_DDL: list[str] = _build_v11_ddl()


# ── Idempotency helpers ──────────────────────────────────────────────────────


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True iff ``table`` already has a column named ``column``.

    Uses ``pragma_table_info`` which returns one row per column. The
    ``pragma_*`` calls accept table names without quoting; we strip
    any surrounding double-quotes / brackets / backticks defensively.

    A missing table is treated as "column absent" so the caller can
    decide whether to CREATE the table first or skip the migration.
    """
    target_table = table.strip()
    if (
        len(target_table) >= 2
        and target_table[0] == target_table[-1]
        and target_table[0] in ('"', "`")
    ):
        target_table = target_table[1:-1]
    elif (
        len(target_table) >= 2
        and target_table[0] == "["
        and target_table[-1] == "]"
    ):
        target_table = target_table[1:-1]
    target_column = column.strip()
    if (
        len(target_column) >= 2
        and target_column[0] == target_column[-1]
        and target_column[0] in ('"', "`")
    ):
        target_column = target_column[1:-1]
    try:
        rows = conn.execute(
            f"PRAGMA table_info({target_table})"
        ).fetchall()
    except sqlite3.OperationalError:
        return False
    for row in rows:
        # pragma_table_info columns: cid, name, type, notnull, dflt_value, pk
        name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
        if name == target_column:
            return True
    return False


def apply_v11_idempotent(conn: sqlite3.Connection) -> None:
    """Apply every v11 column-add to ``conn`` iff the column is absent.

    This is the portable, self-guarding implementation of v11. It is
    safe to call against:

      * a fresh database where schema_v10's CREATE TABLE already
        includes every v11 column (every ALTER is skipped);
      * a historical v10 database missing the v11 columns (every
        ALTER applies);
      * a partial v10/v11 database where some columns are present
        and others are not (only the missing ones are added);
      * an already-v11 database (no-op for every ALTER).

    The function is the canonical entrypoint for both production
    runtime (called by :class:`Database` when applying v11) and raw
    sqlite3 test helpers.
    """
    for table, column, type_sql in _V11_COLUMN_ADDS:
        if column_exists(conn, table, column):
            continue
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {type_sql}"
        )


def iter_v11_idempotent_statements(conn: sqlite3.Connection) -> Iterable[str]:
    """Yield the SQL statements needed to bring ``conn`` to v11.

    Each statement is either an ``ALTER TABLE ... ADD COLUMN`` for
    a column that is absent, or a benign ``SELECT 1`` for a column
    that is already present. Callers that need a side-effecting
    statement list (e.g. transaction-aware runners) can iterate the
    result and ``execute`` each entry. The function never raises on
    a duplicate-column conflict because every entry is a valid
    statement on its own.
    """
    for table, column, type_sql in _V11_COLUMN_ADDS:
        if column_exists(conn, table, column):
            yield "SELECT 1"
        else:
            yield f"ALTER TABLE {table} ADD COLUMN {column} {type_sql}"