"""Version 12 schema migration — additive paper-signal audit storage
plus V2 shadow offset audit columns.

Two additive concerns live in this migration:

1. **Paper-signal audit storage** (Repair 1): the typed
   :class:`PaperSignalDecisionInput` must round-trip through
   persistence without lossy coercion. The canonical JSON
   serialization of the typed input is persisted in
   ``paper_signal_decisions.decision_input_json`` so a future
   reload can rebuild the exact decision-engine input byte-
   for-byte.

   The three upstream-decision-id columns
   (``wallet_score_decision_id``,
   ``category_score_decision_id``,
   ``trade_score_decision_id``) are added as **plain INTEGER**
   columns — NOT foreign-key references — to avoid the
   cross-version FK hazard of SQLite ``ALTER TABLE ... ADD
   COLUMN REFERENCES``. The canonical-JSON contract in
   ``decision_input_json`` is the authoritative replay record.

2. **Shadow V2 offset audit** (Repair 2): the V2 shadow typed
   input now exposes three additional offset fields:

     * ``target_delay_seconds`` (REAL, nullable)
     * ``actual_observed_delay_seconds`` (REAL, nullable)
     * ``delay_error_seconds`` (REAL, nullable)

   These are the audit fields that let a reviewer see whether the
   persisted snapshot actually landed inside the scenario's
   delay-window tolerance, or whether the snapshot was late
   and produced a SHADOW_INCOMPLETE honestly.

All statements are additive ``ALTER TABLE ... ADD COLUMN`` and are
idempotent via the canonical :func:`apply_v12_idempotent` helper
(matching the v11 pattern in ``schema_v11.py``).
"""

from __future__ import annotations

import sqlite3
from typing import Iterable


# ── v12 column-additions (structured for the idempotency guard) ──────────────

_V12_COLUMN_ADDS: list[tuple[str, str, str]] = [
    # ── Paper-signal audit storage (Repair 1) ─────────────────────────────────
    # Canonical-JSON serialization of PaperSignalDecisionInput.
    # Nullable for historical rows that pre-date v12 (no
    # destructive rebuild). The runtime always writes a non-null
    # value for new rows.
    ("paper_signal_decisions", "decision_input_json", "TEXT"),
    # Three plain INTEGER FK-shaped columns (no REFERENCES clause
    # to keep ALTER TABLE safe across SQLite versions). Nullable
    # for historical rows.
    ("paper_signal_decisions", "wallet_score_decision_id", "INTEGER"),
    ("paper_signal_decisions", "category_score_decision_id", "INTEGER"),
    ("paper_signal_decisions", "trade_score_decision_id", "INTEGER"),
    # ── Shadow V2 offset audit (Repair 2) ─────────────────────────────────────
    # ``target_delay_seconds`` is the scenario's requested delay
    # (NULL when the scenario computes its own offset, e.g.
    # ACTUAL_MEASURED_DELAY).
    # ``actual_observed_delay_seconds`` is the measured offset
    # between the source trade timestamp and the persisted
    # snapshot's ``fetched_at``.
    # ``delay_error_seconds`` = actual_observed_delay_seconds -
    # target_delay_seconds when both are available.
    ("shadow_decisions", "target_delay_seconds", "REAL"),
    ("shadow_decisions", "actual_observed_delay_seconds", "REAL"),
    ("shadow_decisions", "delay_error_seconds", "REAL"),
]


def _build_v12_ddl() -> list[str]:
    """Materialize ``_V12_DDL`` from the structured column-add table."""
    return [
        f"ALTER TABLE {table} ADD COLUMN {column} {type_sql};"
        for table, column, type_sql in _V12_COLUMN_ADDS
    ]


_V12_DDL: list[str] = _build_v12_ddl()


# ── Idempotency helpers ──────────────────────────────────────────────────────


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True iff ``table`` already has a column named ``column``.

    Mirrors the v11 helper. Kept as a separate function (rather than
    imported from schema_v11) so each migration module is self-contained
    and the dependency graph stays one-directional.
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
        name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
        if name == target_column:
            return True
    return False


def apply_v12_idempotent(conn: sqlite3.Connection) -> None:
    """Apply every v12 column-add to ``conn`` iff the column is absent.

    Mirrors the v11 applier contract: safe to call against a fresh
    database (every column present → no-op), a historical v11
    database (each absent column is added), and an already-v12
    database (no-op for every column).
    """
    for table, column, type_sql in _V12_COLUMN_ADDS:
        if column_exists(conn, table, column):
            continue
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {type_sql}"
        )


def iter_v12_idempotent_statements(conn: sqlite3.Connection) -> Iterable[str]:
    """Yield one SQL statement per v12 column-add, guarding each one."""
    for table, column, type_sql in _V12_COLUMN_ADDS:
        if column_exists(conn, table, column):
            yield "SELECT 1"
        else:
            yield f"ALTER TABLE {table} ADD COLUMN {column} {type_sql}"