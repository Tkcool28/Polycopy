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


class MigrationBlocked(RuntimeError):
    """Raised when a migration cannot run safely against the current database.

    The classic case is the v5 rewrite of ``source_trades`` (DROP TABLE +
    ALTER TABLE RENAME) hitting a foreign-key constraint from a child
    table that was added by a later PR. The runner must not silently
    swallow the error — the operator needs to know the migration was
    intentionally blocked, and why.
    """


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

    # PR24C: Safety PRAGMAs enforced on every connect(). These are idempotent
    # at the connection level (``journal_mode`` is also persisted in the
    # database file header, but we still set it explicitly here so any caller
    # opening the file — including fresh DB creation — gets WAL immediately).
    _SAFETY_JOURNAL_MODE = "WAL"
    _SAFETY_BUSY_TIMEOUT_MS = 30_000
    _SAFETY_WAL_AUTOCHECKPOINT = 1_000

    def connect(self) -> "Database":
        """Open (or create) the SQLite database and run pending migrations.

        Enforces safety PRAGMAs on every connection:

        - ``PRAGMA foreign_keys = ON``  (per-connection, required for FKs)
        - ``PRAGMA journal_mode = WAL`` (set explicitly + persisted in file header)
        - ``PRAGMA busy_timeout = 30000`` (per-connection, ms)
        - ``PRAGMA wal_autocheckpoint = 1000`` (per-connection, frames)
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # FKs must be enabled before migrations run so the v5 rewrite of
        # source_trades sees the right referential state.
        self._conn.execute("PRAGMA foreign_keys = ON")
        # journal_mode=WAL returns a row with the new mode (typically "wal");
        # the value is also persisted in the file header so future reopens
        # inherit it. Setting it explicitly here makes fresh-DB creation
        # safe even before any read of the header.
        self._conn.execute(f"PRAGMA journal_mode = {self._SAFETY_JOURNAL_MODE}")
        # busy_timeout makes the writer wait instead of raising
        # SQLITE_BUSY when another connection is mid-transaction.
        self._conn.execute(
            f"PRAGMA busy_timeout = {self._SAFETY_BUSY_TIMEOUT_MS}"
        )
        # wal_autocheckpoint bounds how many frames the WAL can hold
        # before SQLite auto-checkpoints. 1000 frames is the
        # well-trodden Polycopy operational value.
        self._conn.execute(
            f"PRAGMA wal_autocheckpoint = {self._SAFETY_WAL_AUTOCHECKPOINT}"
        )
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

    def _table_exists(self, name: str) -> bool:
        """Return True if a base table named ``name`` exists in the schema."""
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    def _index_exists(self, name: str) -> bool:
        """Return True if an index named ``name`` exists in the schema."""
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    # Tables that must physically exist before the runner will consider
    # the database to be at the target schema version. These are the
    # 15 base tables the live production DB has today, plus the 4 v13
    # indexes that ``_V13_DDL`` defines. Indexes added in this PR
    # (``idx_wsa_category_score``, ``idx_wsa_sample``,
    # ``idx_wsa_computed_at``) are NOT in this set — they are
    # post-reconciliation additions handled by
    # :meth:`_apply_missing_v13_indexes`.
    _REQUIRED_V13_OBJECTS: tuple[str, ...] = (
        # base tables
        "wallets", "markets", "source_trades", "wallet_score_decisions",
        "decision_verdicts", "score_component_inputs", "copy_candidates",
        "paper_signal_decisions", "category_wallet_score_decisions",
        "trade_copyability_decisions", "shadow_decisions",
        "exit_experiment_registrations", "wallet_specialist_aggregations",
        "orders", "positions",
        # v13 indexes
        "idx_wsa_wallet", "idx_wsa_category", "idx_wsa_quality",
        "idx_wsa_wallet_category",
    )

    # PR24A: extend the physical-schema check with the v14 resolution-truth
    # columns. The reconciliation path that protects against the v5 DROP
    # replay must NOT fire if the v14 ADD COLUMNs haven't been applied —
    # otherwise a DB at physical v13 / _meta v13 will be silently bumped to
    # _meta v14 without the new columns, leaving the schema one step behind
    # the code. We include the most important v14 columns here so the
    # reconciliation short-circuit only fires when v14 is *physically*
    # present.
    _REQUIRED_V14_COLUMNS: tuple[tuple[str, str], ...] = (
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

    # PR24I: v15 ledger objects required before a schema-metadata-lag
    # reconciliation can claim the physical schema is at target. Without
    # this, a v14 database whose v13/v14 objects already exist could be
    # bumped to _meta=15 without creating the new ledger table.
    _REQUIRED_V15_OBJECTS: tuple[str, ...] = (
        "settlement_accounting_ledger",
        "idx_settlement_ledger_wallet",
        "idx_settlement_ledger_trader",
        "idx_settlement_ledger_market",
        "idx_settlement_ledger_status",
        "idx_settlement_ledger_source_trade",
    )

    # PR24P: v16 price-trace columns required on trade_copyability_decisions
    # before a schema-metadata-lag reconciliation can claim the physical
    # schema is at target. Without this, a v15 DB whose v13/v14/v15 objects
    # already exist could be bumped to _meta=16 without adding the new
    # trace columns.
    _REQUIRED_V16_COLUMNS: tuple[tuple[str, str], ...] = (
        ("trade_copyability_decisions", "source_entry_price"),
        ("trade_copyability_decisions", "current_copy_price"),
        ("trade_copyability_decisions", "estimated_fill_price"),
        ("trade_copyability_decisions", "source_trade_timestamp"),
        ("trade_copyability_decisions", "price_snapshot_fetched_at"),
        ("trade_copyability_decisions", "evaluation_timestamp"),
    )

    # PR66: v17 source-trade metadata evidence column and wallet-history index
    # must exist before schema-metadata reconciliation can claim target shape.
    _REQUIRED_V17_COLUMNS: tuple[tuple[str, str], ...] = (
        ("source_trades", "metadata_json"),
    )
    _REQUIRED_V17_OBJECTS: tuple[str, ...] = (
        "idx_source_trades_wallet_timestamp",
    )
    # v18 — specialist paper execution spine. The reconciliation short-circuit
    # must require every newly created table + unique index to be physically
    # present before it claims the target shape; otherwise a DB at v17 metadata
    # would be bumped to v18 without applying the new tables.
    _REQUIRED_V18_OBJECTS: tuple[str, ...] = (
        "specialist_approvals",
        "paper_signal_execution_authorizations",
        "execution_risk_decisions",
        "paper_orders",
        "paper_fills",
        "paper_positions",
        "paper_position_marks",
        "paper_position_settlements",
        "ux_specialist_approvals_active",
        "idx_paper_signal_exec_authz_signal",
        "idx_execution_risk_signal",
        "idx_paper_orders_signal",
        "idx_paper_fills_order",
        "idx_paper_positions_order",
        "idx_paper_position_marks_position",
        "idx_paper_position_settlements_position",
    )

    # Indexes this PR adds to ``_V13_DDL``. They are created as a
    # post-reconciliation step when the rest of the v13 schema is
    # already present but these specific indexes are missing.
    _PR23_V13_INDEXES: tuple[str, ...] = (
        "idx_wsa_category_score", "idx_wsa_sample", "idx_wsa_computed_at",
    )

    def _physical_schema_at_target(self) -> bool:
        """Return True only when the physical schema has every target object.

        Used by the migration runner to detect "schema metadata lag":
        a database whose ``_meta.schema_version`` is behind the code's
        ``SCHEMA_VERSION`` but whose physical schema is already at the
        target. In that case the destructive migrations must NOT replay
        (they would either fail, bloat the DB, or both — see PR23 for
        the worked example with v5 rewriting ``source_trades``).

        PR24A extended this check to include the v14 resolution-truth
        columns so the reconciliation path only short-circuits when v14
        is *physically* present, not merely when v13 is.
        """
        for obj in self._REQUIRED_V13_OBJECTS:
            if not (self._table_exists(obj) or self._index_exists(obj)):
                return False
        for table, column in self._REQUIRED_V14_COLUMNS:
            if not self._column_exists(table, column):
                return False
        for obj in self._REQUIRED_V15_OBJECTS:
            if not (self._table_exists(obj) or self._index_exists(obj)):
                return False
        for table, column in self._REQUIRED_V16_COLUMNS:
            if not self._column_exists(table, column):
                return False
        for table, column in self._REQUIRED_V17_COLUMNS:
            if not self._column_exists(table, column):
                return False
        for obj in self._REQUIRED_V17_OBJECTS:
            if not self._index_exists(obj):
                return False
        for obj in self._REQUIRED_V18_OBJECTS:
            if not (self._table_exists(obj) or self._index_exists(obj)):
                return False
        return True

    def _missing_pr23_v13_indexes(self) -> list[str]:
        """Return the subset of PR23 v13 indexes that are NOT yet on the DB."""
        return [name for name in self._PR23_V13_INDEXES if not self._index_exists(name)]

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

        PR23 adds a pre-flight physical-schema guard: if
        ``_meta.schema_version`` is behind ``SCHEMA_VERSION`` but every
        required v13 object already exists in the live database, the
        runner reconciles ``_meta`` to the target without replaying any
        migration. This protects the runner from the v5 case where the
        destructive ``source_trades`` rewrite now collides with FKs
        added by later PRs (e.g. ``copy_candidates.source_trade_internal_id``).
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

        # PR23: physical-schema pre-flight. If every required v13 object
        # is already present, the destructive migrations would either
        # fail (e.g. v5 DROP TABLE source_trades blocked by a later FK)
        # or do pointless work (CREATE TABLE IF NOT EXISTS on existing
        # tables). Reconcile _meta in a single transaction and return.
        if self._physical_schema_at_target():
            integrity = self.conn.execute("PRAGMA integrity_check").fetchone()
            fk_violations = list(self.conn.execute("PRAGMA foreign_key_check"))
            missing_indexes = self._missing_pr23_v13_indexes()
            logger.warning(
                "schema metadata lag detected; physical schema matches target "
                "(SCHEMA_VERSION=%d); reconciling _meta.schema_version from %d -> %d "
                "without replaying migrations. integrity_check=%s foreign_key_check=%d rows",
                SCHEMA_VERSION, current, SCHEMA_VERSION,
                integrity[0] if integrity else "?",
                len(fk_violations),
            )
            self._set_version(SCHEMA_VERSION)
            if missing_indexes:
                logger.warning(
                    "physical schema at target but %d PR23 v13 indexes missing "
                    "(%s); applying as a post-reconciliation step",
                    len(missing_indexes), ", ".join(missing_indexes),
                )
                self._apply_missing_v13_indexes(missing_indexes)
            return

        logger.info("Migrating schema from version %d to %d.", current, SCHEMA_VERSION)
        for target_version in range(current + 1, SCHEMA_VERSION + 1):
            statements = MIGRATIONS.get(target_version)
            if statements is None:
                raise RuntimeError(f"No migration defined for version {target_version}.")
            # v11 carries its own idempotency contract via
            # ``polycopy.db.schema_v11.apply_v11_idempotent``: schema_v10's
            # CREATE TABLE already declares every column v11 would otherwise
            # add, so re-running v11 against a fresh DB (or a DB that has
            # already reached v11) must be a no-op. We use the explicit
            # applier for v11 so the guard lives in one place, and fall
            # back to the generic statement-by-statement path for all
            # other versions.
            if target_version == 11:
                from polycopy.db.schema_v11 import apply_v11_idempotent

                apply_v11_idempotent(self.conn)
            elif target_version == 12:
                # v12 introduces decision_input_json on
                # paper_signal_decisions. Schema_v10 does NOT declare this
                # column, so the v11-style "fresh DB already has it"
                # pattern does not apply — every fresh DB needs the ALTER.
                # The applier remains idempotent for upgraded v12 DBs.
                from polycopy.db.schema_v12 import apply_v12_idempotent

                apply_v12_idempotent(self.conn)
            elif target_version == 5:
                # PR23: route v5 through the FK guard so we never
                # attempt DROP TABLE source_trades while a child FK
                # exists. The migration is incompatible with any
                # physical schema that already has v17+ tables.
                self._apply_v5_with_fk_guard()
            else:
                for stmt in statements:
                    self._execute_migration_statement(stmt)
            self._set_version(target_version)
            logger.info("Applied migration to version %d.", target_version)

        self.conn.commit()

    # ── PR23: migration guards ──────────────────────────────────────────────

    def _apply_v5_with_fk_guard(self) -> None:
        """Apply the v5 migration, but only if it is safe to drop ``source_trades``.

        The v5 migration rebuilds ``source_trades`` with a nullable
        ``trader_address`` via the standard SQLite rewrite pattern
        (CREATE new, INSERT SELECT, DROP old, ALTER RENAME). That DROP
        is unsafe if any child table has a foreign key into
        ``source_trades.id`` — a condition introduced by later PRs
        (e.g. ``copy_candidates.source_trade_internal_id``).

        If a child FK is detected we raise :class:`MigrationBlocked`.
        The runner surfaces this as a clear error to the operator; we
        intentionally do NOT swallow it, because silently skipping v5
        would leave ``_meta`` claiming v5 is applied when it is not.
        """
        # pragma_foreign_key_list returns 0 rows when the table doesn't
        # exist. We use sqlite_master to enumerate tables that HAVE an
        # FK into source_trades, which is more reliable than per-table
        # pragmas (some pragma forms return no rows for missing tables
        # without raising).
        child_refs = self.conn.execute(
            """
            SELECT m.name AS child_table
            FROM sqlite_master m, pragma_foreign_key_list(m.name) fk
            WHERE fk."table" = 'source_trades'
            """
        ).fetchall()
        if child_refs:
            child_tables = sorted({row["child_table"] for row in child_refs})
            raise MigrationBlocked(
                f"v5 migration cannot drop source_trades: child table(s) "
                f"{child_tables} reference it via foreign key. Manual "
                f"reconciliation is required. See PR23 for the worked "
                f"example and the physical-schema reconciliation path."
            )
        # No child FKs — the v5 DROP is safe. Run the original DDL.
        for stmt in MIGRATIONS[5]:
            self._execute_migration_statement(stmt)
        self._set_version(5)
        logger.info("Applied migration to version 5 (FK guard passed).")

    def _apply_missing_v13_indexes(self, missing: list[str]) -> None:
        """Create the named v13 indexes that the post-reconciliation step requires.

        Called from the reconciliation branch of :meth:`_run_migrations`
        when the rest of the physical v13 schema is already present but
        some of the indexes this PR adds to ``_V13_DDL`` are not yet on
        the live DB. All statements are additive (``CREATE INDEX IF NOT
        EXISTS``) and idempotent.

        Hardening: after executing the matching ``CREATE INDEX``
        statements, this method verifies that every requested index now
        exists. If any are still missing (e.g. the SQL was filtered out
        of ``_V13_DDL``, or a name no longer matches the substring
        pattern, or the execute call silently failed), it raises
        :class:`MigrationBlocked` listing the missing names. The caller
        will not log a false-positive success.
        """
        from polycopy.db.schema_v13 import _V13_DDL  # local import to avoid cycle
        # _V13_DDL is a flat list of statements. We only need the ones
        # that create the missing indexes; map index name -> SQL by
        # scanning the list. This is intentionally simple — the v13
        # DDL is short and stable.
        for stmt in _V13_DDL:
            for index_name in missing:
                # The DDL is a Python list of strings; some are
                # concatenated across multiple literal fragments. The
                # index name appears in a single fragment like
                # ``"CREATE INDEX IF NOT EXISTS <name> "``.
                if f"CREATE INDEX IF NOT EXISTS {index_name} " in stmt:
                    self.conn.execute(stmt)
                    break
        self.conn.commit()

        # Verify every requested index is now present. We do this in
        # the same connection so the read-after-write sees the just-
        # committed indexes (DELETE-mode journals make newly committed
        # schema objects visible to subsequent reads on the same
        # connection without requiring a reconnect).
        still_missing = [name for name in missing if not self._index_exists(name)]
        if still_missing:
            raise MigrationBlocked(
                f"post-reconciliation step failed to create v13 index(es): "
                f"{still_missing}. The CREATE INDEX statement was found "
                f"in _V13_DDL but did not result in a visible index — "
                f"possible causes: DDL filter mismatch, permission "
                f"issue, or the statement was silently ignored. Manual "
                f"investigation required before the next startup."
            )
        logger.info("Applied %d post-reconciliation v13 indexes.", len(missing))

    # ── Convenience query helpers ───────────────────────────────────────────

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ── Bounded query iteration (PR24B) ─────────────────────────────────────
    #
    # Operational scripts previously loaded unbounded result sets with
    # ``fetchall``, which on the old 1.6 GB DB caused per-wallet RSS to
    # grow without bound. These helpers stream rows in fixed-size batches
    # via a server-side cursor so peak memory is bounded by ``batch_size``.
    #
    # All three are thin wrappers around the connection; the implementation
    # lives in :mod:`polycopy.runtime.query_batches` so unit tests can
    # exercise it without a full ``Database`` instance.

    def iter_rows(
        self,
        sql: str,
        params: tuple = (),
        *,
        batch_size: int = 200,
    ):
        from polycopy.runtime.query_batches import iter_rows as _iter_rows

        return _iter_rows(self.conn, sql, params, batch_size=batch_size)

    def iter_batches(
        self,
        sql: str,
        params: tuple = (),
        *,
        batch_size: int = 200,
    ):
        from polycopy.runtime.query_batches import iter_batches as _iter_batches

        return _iter_batches(self.conn, sql, params, batch_size=batch_size)

    def iter_keyset_batches(
        self,
        *,
        base_sql: str,
        keyset_col: str,
        last_value,
        extra_where: str = "",
        base_params: tuple = (),
        batch_size: int = 200,
        descending: bool = True,
    ):
        from polycopy.runtime.query_batches import (
            iter_keyset_batches as _iter_keyset_batches,
        )

        return _iter_keyset_batches(
            self.conn,
            base_sql=base_sql,
            keyset_col=keyset_col,
            last_value=last_value,
            extra_where=extra_where,
            base_params=base_params,
            batch_size=batch_size,
            descending=descending,
        )


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
