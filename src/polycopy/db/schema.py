"""Versioned SQLite schema DDL.

Migrations are applied sequentially. The `_meta` table tracks the current
schema version. Each migration is a list of SQL statements.

Round-1 PR-1 additions (recovery sequence, v6 → v7):
  * ``market_outcomes.clob_token_id TEXT`` (nullable) — persisted identity for
    Gamma's per-outcome CLOB token, so multi-outcome markets can be
    unambiguously joined from ``source_trades``.
  * ``source_trades.token_id TEXT`` (nullable) — persisted upstream
    asset/identifier for each observed trade so the canonical mapping helper
    can resolve trades to market outcomes without re-parsing Gamma.
  * ``idx_market_outcomes_token`` — index on ``market_outcomes(clob_token_id)``
    to serve the canonical token-join directly.

Round-1 PR-2 additions (recovery sequence, v7 → v8):
  * New ``copy_candidates`` table — bounded, persisted artifact of
    evaluating one (wallet, source_trade) pair through the canonical
    resolver + basic eligibility checks. UNIQUE
    ``(wallet_id, source, source_trade_id)`` is the idempotency key.
    Indexes on status / wallet / source_trade_internal_id /
    market_outcome_id. No destructive change to any existing table.

Schema-bridge (v8 → v9) — see incident disclosure:
  * New ``candidate_price_snapshots`` table — append-only audit log of
    fresh CLOB-book fetches for PENDING_PRICE_CHECK candidates. UNIQUE
    ``(candidate_id, snapshot_run_id)`` is the per-run idempotency key.
    No destructive change. No pointer column on ``copy_candidates``; the
    "latest snapshot" is a query, not a foreign key. The PR-3 market-end
    metadata reuses the existing ``markets.end_date`` (populated from
    Gamma's ``endDate`` field); no new market column is added.

    This bridge PR contains the schema change ONLY. The CLOB adapter,
    snapshot engine, persistence helpers, config keys, and feature tests
    ship in a separate PR after this bridge merges.

All column / table additions are idempotent: the migration runner uses
``PRAGMA table_info(...)`` to gate ``ALTER TABLE ... ADD COLUMN`` and
``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` are
natively idempotent in SQLite.
"""

from __future__ import annotations

# ── Version 21: canonical specialist evidence accumulation ──────────────
SCHEMA_VERSION = 21

# Import v10 schema changes
from polycopy.db.schema_v10 import _V10_DDL  # noqa: E402

# Import v11 schema changes (additive ALTER TABLE for V2 shadow typed input)
from polycopy.db.schema_v11 import _V11_DDL  # noqa: E402

# Import v12 schema changes (additive audit storage for paper-signal input)
from polycopy.db.schema_v12 import _V12_DDL  # noqa: E402

# Import v13 schema changes (additive wallet_specialist_aggregations evidence
# table from PR #20). The PR20 runtime wiring (specialist_metrics.py,
# specialist_metrics_persistence.py, specialist_aggregation_step.py,
# POLYCOPY_SPECIALIST_AGGREGATIONS_ENABLED flag, and tests) is intentionally
# NOT carried by this hotfix. This import only registers the schema DDL so
# that main code can open a v13 DB. No scoring formula reads
# wallet_specialist_aggregations, so the table stays empty and inert until
# PR #20 is finalized and merged in a future PR.
from polycopy.db.schema_v13 import _V13_DDL  # noqa: E402

# Import v14 schema changes (PR24A — additive resolution-truth columns
# on markets / market_outcomes / source_trades). No scoring formula
# reads these columns in this PR; the backfill script (which defaults
# to --dry-run) is the only writer. No runtime job is added.
from polycopy.db.schema_v14 import _V14_DDL  # noqa: E402

# Import v15 schema changes (PR24I — additive settlement accounting
# ledger derived from already-known source_trade settlement truth). No
# scoring formula, PR20 runtime, timer, live fetch, or production apply
# is enabled by this migration.
from polycopy.db.schema_v15 import _V15_DDL  # noqa: E402

# Import v16 schema changes (PR24P — additive Trade Copyability v1 price-trace
# columns on trade_copyability_decisions). No scoring formula, timer,
# automation, or production DB write is enabled by this migration. The
# production DB is migrated only later, when a service is intentionally
# deployed/restarted (the runner applies this migration idempotently on
# next connect).
from polycopy.db.schema_v16 import _V16_DDL  # noqa: E402
from polycopy.db.schema_v17 import _V17_DDL  # noqa: E402
# v18 — Specialist paper execution spine (durable approvals, provenance-tracked
# paper orders/fills/positions, execution-risk + settlement evidence). See
# ``schema_v18.py`` for the full design contract. Purely additive; v17 schema is
# preserved for backward compatibility.
from polycopy.db.schema_v18 import _V18_DDL  # noqa: E402
# v19 — Specialist approved-trade enrichment + durable dispatch. Purely
# additive; v18 schema (approvals + execution spine) is preserved.
from polycopy.db.schema_v19 import _V19_DDL  # noqa: E402
# v20 — Retryable blocked execution: rebuild execution_risk_decisions without
# UNIQUE(paper_signal_decision_id) and add immutable attempt identity
# (execution_attempt_id, authorization_id, attempt_number). Preserves existing
# risk-decision IDs and the paper_orders FK into risk_decision_id.
from polycopy.db.schema_v20 import _V20_DDL  # noqa: E402
# v21 — Canonical Specialist Evidence Accumulation (research plane). Adds the
# research-only watchlist + market-refresh state, and inlines the PR #70
# (v18/v19) tables so a fresh v21 DB is self-sufficient (see
# schema_v21.py for the fresh-DB completeness rationale).
from polycopy.db.schema_v21 import _V21_DDL  # noqa: E402


def _build_idempotent_add_column_sql(table: str, column: str, type_sql: str) -> str:
    """Return a SQL fragment that adds ``table.column`` iff it does not exist.

    SQLite does not support ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` in
    versions prior to 3.35 (and even after, the syntax is non-standard for
    our purposes). The portable trick is a ``CASE`` expression over
    ``pragma_table_info`` returning the ``ALTER TABLE`` DDL when the column
    is absent and a benign ``SELECT 1`` when it is already present, then
    executing the result. The fragment below is one valid statement:

        SELECT CASE WHEN (
            SELECT COUNT(*) FROM pragma_table_info('<table>')
            WHERE name = '<column>'
        ) = 0 THEN 'ALTER TABLE <table> ADD COLUMN <column> <type_sql>;'
        ELSE 'SELECT 1;' END;

    The runner executes each returned string sequentially. The string
    returned here is itself a single statement that emits a second
    statement via the CASE expression; the migration runner needs to
    recognize this and execute the inner statement. See
    :func:`polycopy.db.database.Database._run_migrations` for the wrapper.
    """
    # NOTE: actual execution wrapper lives in the migration runner because
    # SQLite's Python driver executes one statement per `execute()` call —
    # we cannot nest a dynamic statement without an explicit two-step call.
    # We instead emit the ALTER TABLE directly; the runner applies the
    # idempotency guard by inspecting ``PRAGMA table_info`` BEFORE running
    # each v7 ADD COLUMN statement.
    raise NotImplementedError(
        "Use _v7_idempotent_add_column() in database.py instead of building a "
        "string here. See _V7_DDL below for the canonical application."
    )


def _idempotent_add_column_ddl(table: str, column: str, type_sql: str) -> str:
    """Return the ``ALTER TABLE ... ADD COLUMN`` SQL for ``(table, column)``.

    The migration runner applies this only when ``PRAGMA table_info`` shows
    the column is absent; the function itself returns plain DDL so it
    composes with the existing ``list[str]`` migration registry. The
    actual idempotency guard lives in
    :meth:`polycopy.db.database.Database._apply_v7_migration`.
    """
    return f"ALTER TABLE {table} ADD COLUMN {column} {type_sql};"

# ── Version 1: initial schema ───────────────────────────────────────────────────
_V1_DDL: list[str] = [
    # Meta table for schema versioning
    """CREATE TABLE IF NOT EXISTS _meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );""",
    # Wallets
    """CREATE TABLE IF NOT EXISTS wallets (
        id         TEXT PRIMARY KEY,  -- UUID
        address    TEXT NOT NULL,
        label      TEXT NOT NULL DEFAULT 'default',
        is_sample  INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL       -- ISO-8601 UTC
    );""",
    # Wallet balances
    """CREATE TABLE IF NOT EXISTS wallet_balances (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet_id  TEXT NOT NULL REFERENCES wallets(id),
        currency   TEXT NOT NULL,
        amount     REAL NOT NULL CHECK(amount >= 0),
        as_of      TEXT NOT NULL,       -- ISO-8601 UTC
        is_sample  INTEGER NOT NULL DEFAULT 0
    );""",
    # Markets
    """CREATE TABLE IF NOT EXISTS markets (
        id              TEXT PRIMARY KEY,  -- UUID
        source_id       TEXT NOT NULL,     -- Source-specific ID
        source          TEXT NOT NULL,
        question        TEXT NOT NULL,
        active          INTEGER NOT NULL DEFAULT 1,
        closed          INTEGER NOT NULL DEFAULT 0,
        resolved        INTEGER NOT NULL DEFAULT 0,
        resolution_outcome TEXT,
        volume_24h      REAL NOT NULL DEFAULT 0,
        end_date        TEXT,              -- ISO-8601 UTC
        fetched_at      TEXT NOT NULL,     -- ISO-8601 UTC
        is_sample       INTEGER NOT NULL DEFAULT 0,
        UNIQUE(source, source_id)
    );""",
    # Market outcomes — created without ``clob_token_id``; the v7 migration
    # adds that column additively. Keeping the V1 schema byte-identical to
    # the original (no clob_token_id at V1) is essential so that legacy
    # v6-production-style databases round-trip through the v7 migration
    # without orphaned columns and so that the v7 idempotency guard sees
    # the column as "absent" before the migration runs.
    """CREATE TABLE IF NOT EXISTS market_outcomes (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id      TEXT NOT NULL REFERENCES markets(id),
        label          TEXT NOT NULL,
        price          REAL NOT NULL CHECK(price >= 0 AND price <= 1),
        volume         REAL NOT NULL DEFAULT 0 CHECK(volume >= 0)
    );""",
    # Signals
    """CREATE TABLE IF NOT EXISTS signals (
        id             TEXT PRIMARY KEY,  -- UUID
        market_id      TEXT NOT NULL REFERENCES markets(id),
        source         TEXT NOT NULL,
        strength       TEXT NOT NULL,
        confidence     REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
        edge_estimate  REAL NOT NULL,
        predicted_prob REAL NOT NULL CHECK(predicted_prob >= 0 AND predicted_prob <= 1),
        market_prob    REAL NOT NULL CHECK(market_prob >= 0 AND market_prob <= 1),
        reasoning      TEXT NOT NULL DEFAULT '',
        produced_at    TEXT NOT NULL,     -- ISO-8601 UTC
        is_sample      INTEGER NOT NULL DEFAULT 0
    );""",
    # Orders
    """CREATE TABLE IF NOT EXISTS orders (
        id              TEXT PRIMARY KEY,  -- UUID
        market_id       TEXT NOT NULL REFERENCES markets(id),
        wallet_id       TEXT NOT NULL REFERENCES wallets(id),
        side            TEXT NOT NULL,     -- buy/sell
        order_type      TEXT NOT NULL,     -- limit/market
        outcome         TEXT NOT NULL,
        quantity        REAL NOT NULL CHECK(quantity > 0),
        price           REAL NOT NULL CHECK(price >= 0 AND price <= 1),
        status          TEXT NOT NULL DEFAULT 'pending',
        filled_quantity REAL NOT NULL DEFAULT 0 CHECK(filled_quantity >= 0),
        source_order_id TEXT,
        signal_id       TEXT REFERENCES signals(id),
        created_at      TEXT NOT NULL,     -- ISO-8601 UTC
        updated_at      TEXT,              -- ISO-8601 UTC
        is_sample       INTEGER NOT NULL DEFAULT 0
    );""",
    # Positions
    """CREATE TABLE IF NOT EXISTS positions (
        id              TEXT PRIMARY KEY,  -- UUID
        market_id       TEXT NOT NULL REFERENCES markets(id),
        wallet_id       TEXT NOT NULL REFERENCES wallets(id),
        outcome         TEXT NOT NULL,
        quantity        REAL NOT NULL CHECK(quantity >= 0),
        avg_entry_price REAL NOT NULL CHECK(avg_entry_price >= 0 AND avg_entry_price <= 1),
        current_price   REAL NOT NULL CHECK(current_price >= 0 AND current_price <= 1),
        realized_pnl    REAL NOT NULL DEFAULT 0,
        opened_at       TEXT NOT NULL,     -- ISO-8601 UTC
        updated_at      TEXT,              -- ISO-8601 UTC
        is_sample       INTEGER NOT NULL DEFAULT 0
    );""",
    # Source trades (observed, not our own) — created without ``token_id``;
    # the v7 migration adds that column additively. ``trader_address``
    # stays ``NOT NULL`` in V1 here on purpose: the v5 migration is the
    # authoritative step that relaxes it to nullable and normalizes
    # sentinels. Pre-relaxing V1 would make v5 a no-op and break the v4
    # → v5 regression that proves the migration actually rewrites the
    # column. New DBs run V1 → V5 → V6 → V7 in order; production v6
    # databases run just V7. Both end up with ``trader_address TEXT``
    # (nullable) and ``token_id TEXT`` (nullable).
    """CREATE TABLE IF NOT EXISTS source_trades (
        id               TEXT PRIMARY KEY,  -- UUID
        source           TEXT NOT NULL,
        source_trade_id  TEXT NOT NULL,
        market_source_id TEXT NOT NULL,
        side             TEXT NOT NULL,
        outcome          TEXT NOT NULL,
        quantity         REAL NOT NULL CHECK(quantity > 0),
        price            REAL NOT NULL CHECK(price >= 0 AND price <= 1),
        trader_address   TEXT NOT NULL,
        timestamp        TEXT NOT NULL,     -- ISO-8601 UTC
        is_sample        INTEGER NOT NULL DEFAULT 0,
        UNIQUE(source, source_trade_id)
    );""",
    # Decision log
    """CREATE TABLE IF NOT EXISTS decision_log (
        id            TEXT PRIMARY KEY,  -- UUID
        wallet_id     TEXT NOT NULL REFERENCES wallets(id),
        market_id     TEXT NOT NULL REFERENCES markets(id),
        decision_type TEXT NOT NULL,
        signal_ids    TEXT NOT NULL DEFAULT '[]',  -- JSON array of UUIDs
        order_id      TEXT REFERENCES orders(id),
        rationale     TEXT NOT NULL DEFAULT '',
        metrics       TEXT NOT NULL DEFAULT '{}',  -- JSON object
        created_at    TEXT NOT NULL,              -- ISO-8601 UTC
        is_sample     INTEGER NOT NULL DEFAULT 0
    );""",
    # Experiment runs
    """CREATE TABLE IF NOT EXISTS experiment_runs (
        id              TEXT PRIMARY KEY,  -- UUID
        label           TEXT NOT NULL,
        strategy_config TEXT NOT NULL DEFAULT '{}',  -- JSON object
        status          TEXT NOT NULL DEFAULT 'pending',
        started_at      TEXT,              -- ISO-8601 UTC
        ended_at        TEXT,              -- ISO-8601 UTC
        result_summary  TEXT NOT NULL DEFAULT '{}',  -- JSON object
        error_message   TEXT,
        is_sample       INTEGER NOT NULL DEFAULT 0
    );""",
    # Raw snapshots (provenance)
    """CREATE TABLE IF NOT EXISTS raw_snapshots (
        id            TEXT PRIMARY KEY,  -- UUID
        source        TEXT NOT NULL,
        endpoint      TEXT NOT NULL,
        query_params  TEXT NOT NULL DEFAULT '{}',  -- JSON object
        file_path     TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        hash_algo     TEXT NOT NULL DEFAULT 'sha256',
        content_type  TEXT NOT NULL DEFAULT 'application/json',
        size_bytes    INTEGER NOT NULL CHECK(size_bytes >= 0),
        fetched_at    TEXT NOT NULL,     -- ISO-8601 UTC
        ingested_at   TEXT NOT NULL,     -- ISO-8601 UTC
        is_sample     INTEGER NOT NULL DEFAULT 0
    );""",
    # Performance summaries
    """CREATE TABLE IF NOT EXISTS performance_summaries (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet_id     TEXT NOT NULL REFERENCES wallets(id),
        strategy_label TEXT NOT NULL DEFAULT 'default',
        start_date    TEXT NOT NULL,     -- ISO-8601 UTC
        end_date      TEXT NOT NULL,     -- ISO-8601 UTC
        total_pnl     REAL NOT NULL,
        realized_pnl  REAL NOT NULL,
        unrealized_pnl REAL NOT NULL,
        win_rate      REAL NOT NULL CHECK(win_rate >= 0 AND win_rate <= 1),
        sharpe_ratio  REAL,
        max_drawdown  REAL NOT NULL CHECK(max_drawdown >= 0),
        trade_count   INTEGER NOT NULL CHECK(trade_count >= 0),
        is_sample     INTEGER NOT NULL DEFAULT 0
    );""",
    # Indexes for common queries
    "CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_id);",
    "CREATE INDEX IF NOT EXISTS idx_orders_wallet ON orders(wallet_id);",
    "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);",
    "CREATE INDEX IF NOT EXISTS idx_positions_wallet ON positions(wallet_id);",
    "CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);",
    "CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market_id);",
    "CREATE INDEX IF NOT EXISTS idx_source_trades_market ON source_trades(market_source_id);",
    "CREATE INDEX IF NOT EXISTS idx_source_trades_timestamp ON source_trades(timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_decision_log_wallet ON decision_log(wallet_id);",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_source ON raw_snapshots(source);",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_fetched ON raw_snapshots(fetched_at);",
]

# ── Version 2: provider health tracking ─────────────────────────────────────────
_V2_DDL: list[str] = [
    """CREATE TABLE IF NOT EXISTS provider_health (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        provider      TEXT NOT NULL,
        capability    TEXT NOT NULL,
        status        TEXT NOT NULL,
        last_success  TEXT,
        last_attempt  TEXT,
        http_status   INTEGER,
        error_message TEXT,
        sample_count  INTEGER NOT NULL DEFAULT 0,
        live_count    INTEGER NOT NULL DEFAULT 0,
        is_sample     INTEGER NOT NULL DEFAULT 0,
        UNIQUE(provider, capability)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_provider_health_provider ON provider_health(provider);",
]

# ── Version 3: allow closed paper positions to retain realized P&L ──────────────
_V3_DDL: list[str] = [
    """CREATE TABLE IF NOT EXISTS positions_new (
        id              TEXT PRIMARY KEY,
        market_id       TEXT NOT NULL REFERENCES markets(id),
        wallet_id       TEXT NOT NULL REFERENCES wallets(id),
        outcome         TEXT NOT NULL,
        quantity        REAL NOT NULL CHECK(quantity >= 0),
        avg_entry_price REAL NOT NULL CHECK(avg_entry_price >= 0 AND avg_entry_price <= 1),
        current_price   REAL NOT NULL CHECK(current_price >= 0 AND current_price <= 1),
        realized_pnl    REAL NOT NULL DEFAULT 0,
        opened_at       TEXT NOT NULL,
        updated_at      TEXT,
        is_sample       INTEGER NOT NULL DEFAULT 0
    );""",
    """INSERT INTO positions_new (
        id, market_id, wallet_id, outcome, quantity, avg_entry_price,
        current_price, realized_pnl, opened_at, updated_at, is_sample
    ) SELECT id, market_id, wallet_id, outcome, quantity, avg_entry_price,
             current_price, realized_pnl, opened_at, updated_at, is_sample
        FROM positions;""",
    "DROP TABLE positions;",
    "ALTER TABLE positions_new RENAME TO positions;",
    "CREATE INDEX IF NOT EXISTS idx_positions_wallet ON positions(wallet_id);",
    "CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);",
]

# ── Version 4: capability flags (data availability + wallet attribution) ───────
_V4_DDL: list[str] = [
    """CREATE TABLE IF NOT EXISTS capability_flags (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        capability        TEXT NOT NULL,
        status            TEXT NOT NULL,         -- 'ok' | 'unavailable' | 'partial' | 'unknown'
        wallet_attribution_available INTEGER NOT NULL DEFAULT 0,  -- 0/1
        details           TEXT NOT NULL DEFAULT '{}',  -- JSON object
        first_verified_at TEXT NOT NULL,         -- ISO-8601 UTC
        last_verified_at  TEXT NOT NULL,         -- ISO-8601 UTC
        is_sample         INTEGER NOT NULL DEFAULT 0,
        UNIQUE(capability)
    );""",
    "CREATE INDEX IF NOT EXISTS idx_capability_flags_capability ON capability_flags(capability);",
]

# ── Version 5: source_trades.trader_address becomes NULLable, sentinels → NULL ─
# Rationale: anonymous data-api rows (no proxyWallet) used to be persisted with
# the literal sentinel string "unknown" (or "anonymous", "missing", "0x0",
# "0x"). Those strings then collapsed into a single pseudo-wallet and got
# scored by evaluate_wallet. The fix:
#   1. Allow trader_address to be NULL (the absence of attribution).
#   2. Normalize ALL legacy sentinel values to NULL during the COPY step
#      below. This is a one-shot, self-correcting rewrite — historical rows
#      that were "unknown" / "anonymous" / "missing" / "0x" / "0x0" /
#      empty / whitespace are converted to NULL on upgrade. Real 0x
#      addresses and any other non-sentinel non-empty value are preserved
#      verbatim (case-sensitive).
#   3. Preserve is_sample and all other columns.
#   4. DELETE every row in ``wallets`` whose address lowercases to one of
#      the legacy sentinels or is empty / whitespace-only. Pre-v5 wallets
#      rows were created for the literal sentinel strings ("unknown",
#      "anonymous", "missing", "0x", "0x0") because a legacy collector
#      would promote a sentinel trader_address into a fake wallet row;
#      those rows then got scored by evaluate_wallet. The cleanup below
#      removes those fake rows on upgrade. Real wallet rows — including
#      any with surrounding whitespace or unusual casing — are preserved
#      byte-for-byte. This step is idempotent (DELETEs of already-deleted
#      rows are no-ops) and uses the same LOWER(TRIM(...)) predicate as
#      the source_trades rewrite above for consistency. PRAGMA
#      foreign_keys is disabled during the DELETE so any dependent rows
#      (wallet_balances, positions, orders, decision_log,
#      performance_summaries) referencing a now-deleted wallet are removed
#      by the explicit DELETE below; we then re-enable FK enforcement and
#      run PRAGMA foreign_key_check to verify no orphans remain.
_V5_DDL: list[str] = [
    # ── Step A: rebuild source_trades with a nullable trader_address ────────
    """CREATE TABLE IF NOT EXISTS source_trades_new (
        id               TEXT PRIMARY KEY,  -- UUID
        source           TEXT NOT NULL,
        source_trade_id  TEXT NOT NULL,
        market_source_id TEXT NOT NULL,
        side             TEXT NOT NULL,
        outcome          TEXT NOT NULL,
        quantity         REAL NOT NULL CHECK(quantity > 0),
        price            REAL NOT NULL CHECK(price >= 0 AND price <= 1),
        trader_address   TEXT,                -- nullable (was NOT NULL pre-v5)
        timestamp        TEXT NOT NULL,     -- ISO-8601 UTC
        is_sample        INTEGER NOT NULL DEFAULT 0,
        UNIQUE(source, source_trade_id)
    );""",
    """INSERT INTO source_trades_new (
        id, source, source_trade_id, market_source_id, side, outcome,
        quantity, price, trader_address, timestamp, is_sample
    ) SELECT
        id, source, source_trade_id, market_source_id, side, outcome,
        quantity, price,
        CASE
            WHEN trader_address IS NULL THEN NULL
            WHEN LENGTH(TRIM(trader_address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) = 0 THEN NULL
            WHEN LOWER(TRIM(trader_address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) IN (
                'unknown', 'anonymous', 'missing', '0x', '0x0'
            ) THEN NULL
            ELSE trader_address
        END,
        timestamp, is_sample
      FROM source_trades;""",
    "DROP TABLE source_trades;",
    "ALTER TABLE source_trades_new RENAME TO source_trades;",
    "CREATE INDEX IF NOT EXISTS idx_source_trades_market ON source_trades(market_source_id);",
    "CREATE INDEX IF NOT EXISTS idx_source_trades_timestamp ON source_trades(timestamp);",
    # ── Step B: delete sentinel rows from wallets (and their dependents) ──
    # The deletion ORDER below is the correctness mechanism. FK
    # enforcement stays ON throughout — the migration must satisfy
    # child-before-parent ordering so SQLite never raises
    # FOREIGN KEY constraint failed.
    #
    # Child-before-parent FK graph this satisfies:
    #   decision_log.order_id  → orders.id
    #   decision_log.wallet_id → wallets.id   (NOT NULL)
    #   orders.wallet_id       → wallets.id
    #   positions.wallet_id    → wallets.id
    #   wallet_balances.wallet_id → wallets.id
    #   performance_summaries.wallet_id → wallets.id
    #
    # A naive delete (orders before decision_log) fails with FK
    # violation when a decision_log row references an order belonging
    # to a different (real) wallet. We delete the cross-references
    # FIRST, then by-wallet dependents, then orders, then wallet rows.
    # 1. Child rows that reference sentinel-wallet orders. These MUST
    #    be removed before the orders themselves, otherwise the next
    #    DELETE fails with FOREIGN KEY constraint failed. Note we do
    #    NOT limit this to sentinel-wallet decision_logs: any
    #    decision_log row whose ``order_id`` points to a sentinel-wallet
    #    order must go, even if its own ``wallet_id`` belongs to a
    #    real wallet.
    "DELETE FROM decision_log WHERE order_id IN ("
    "SELECT o.id FROM orders o "
    "JOIN wallets w ON w.id = o.wallet_id "
    "WHERE LENGTH(TRIM(w.address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) = 0 "
    "OR LOWER(TRIM(w.address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) IN "
    "('unknown', 'anonymous', 'missing', '0x', '0x0'));",
    # 2. Child rows that reference sentinel-wallet wallets. Delete ALL
    #    dependents for sentinel wallets in child-before-parent order
    #    so the orders delete (step 3) is not blocked by remaining
    #    decision_log rows. We delete decision_log first since it can
    #    also reference orders (which we haven't deleted yet).
    "DELETE FROM decision_log WHERE wallet_id IN ("
    "SELECT id FROM wallets WHERE "
    "LENGTH(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) = 0 "
    "OR LOWER(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) IN "
    "('unknown', 'anonymous', 'missing', '0x', '0x0'));",
    # wallet_balances has no further dependents.
    "DELETE FROM wallet_balances WHERE wallet_id IN ("
    "SELECT id FROM wallets WHERE "
    "LENGTH(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) = 0 "
    "OR LOWER(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) IN "
    "('unknown', 'anonymous', 'missing', '0x', '0x0'));",
    # performance_summaries has no further dependents.
    "DELETE FROM performance_summaries WHERE wallet_id IN ("
    "SELECT id FROM wallets WHERE "
    "LENGTH(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) = 0 "
    "OR LOWER(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) IN "
    "('unknown', 'anonymous', 'missing', '0x', '0x0'));",
    # positions has no further dependents.
    "DELETE FROM positions WHERE wallet_id IN ("
    "SELECT id FROM wallets WHERE "
    "LENGTH(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) = 0 "
    "OR LOWER(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) IN "
    "('unknown', 'anonymous', 'missing', '0x', '0x0'));",
    # 3. orders — safe now because ALL decision_log rows that could
    #    reference sentinel-wallet orders are gone (deleted in step 1
    #    and step 2 above).
    "DELETE FROM orders WHERE wallet_id IN ("
    "SELECT id FROM wallets WHERE "
    "LENGTH(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) = 0 "
    "OR LOWER(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) IN "
    "('unknown', 'anonymous', 'missing', '0x', '0x0'));",
    # 4. The sentinel wallet rows themselves. By this point no dependent
    #    rows reference them.
    "DELETE FROM wallets WHERE "
    "LENGTH(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) = 0 "
    "OR LOWER(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) IN "
    "('unknown', 'anonymous', 'missing', '0x', '0x0');",
    # Verify no orphan dependent rows remain. PRAGMA foreign_key_check
    # returns rows for any FK violation; on a clean DB it returns empty.
    # The result is intentionally not asserted — the migration runner
    # surfaces it via tests; a non-empty result here would be a bug in
    # the migration itself and would fail the regression suite.
    "PRAGMA foreign_key_check;",
]


# ── Version 6: canonical wallet identity ───────────────────────────────────────
# Adds a persisted database key for wallet identity. Duplicate canonical groups
# are collapsed by deterministic survivor policy: oldest created_at, then lowest
# wallet id. All wallet_id FK dependents are re-homed before duplicate wallet
# rows are deleted, so the migration is valid with FK enforcement enabled.
_V6_DDL: list[str] = [
    "ALTER TABLE wallets ADD COLUMN canonical_address TEXT;",
    "UPDATE wallets SET canonical_address = CASE "
    "WHEN LENGTH(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) = 0 THEN NULL "
    "WHEN LOWER(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) IN "
    "('unknown', 'anonymous', 'missing', '0x', '0x0') THEN NULL "
    "ELSE LOWER(TRIM(address, X'09' || X'0A' || X'0D' || X'0B' || X'0C' || ' ')) END;",
    # Remove any invalid/sentinel wallet rows inserted after v5, child-first.
    "DELETE FROM decision_log WHERE order_id IN ("
    "SELECT o.id FROM orders o JOIN wallets w ON w.id = o.wallet_id "
    "WHERE w.canonical_address IS NULL);",
    "DELETE FROM decision_log WHERE wallet_id IN (SELECT id FROM wallets WHERE canonical_address IS NULL);",
    "DELETE FROM wallet_balances WHERE wallet_id IN (SELECT id FROM wallets WHERE canonical_address IS NULL);",
    "DELETE FROM performance_summaries WHERE wallet_id IN (SELECT id FROM wallets WHERE canonical_address IS NULL);",
    "DELETE FROM positions WHERE wallet_id IN (SELECT id FROM wallets WHERE canonical_address IS NULL);",
    "DELETE FROM orders WHERE wallet_id IN (SELECT id FROM wallets WHERE canonical_address IS NULL);",
    "DELETE FROM wallets WHERE canonical_address IS NULL;",
    # Build a duplicate->survivor map. Survivor = oldest created_at, then lowest id.
    "DROP TABLE IF EXISTS temp.wallet_merge_map;",
    "CREATE TEMP TABLE wallet_merge_map AS "
    "WITH ranked AS ("
    "  SELECT id, canonical_address, "
    "         FIRST_VALUE(id) OVER ("
    "           PARTITION BY canonical_address ORDER BY created_at ASC, id ASC"
    "         ) AS survivor_id, "
    "         ROW_NUMBER() OVER ("
    "           PARTITION BY canonical_address ORDER BY created_at ASC, id ASC"
    "         ) AS rn "
    "  FROM wallets WHERE canonical_address IS NOT NULL"
    ") "
    "SELECT id AS duplicate_id, survivor_id FROM ranked WHERE rn > 1;",
    # Preserve useful metadata on the survivor.
    "UPDATE wallets SET label = COALESCE(("
    "  SELECT w2.label FROM wallets w2 "
    "  WHERE w2.canonical_address = wallets.canonical_address "
    "    AND TRIM(w2.label) <> '' AND LOWER(TRIM(w2.label)) <> 'default' "
    "  ORDER BY w2.created_at ASC, w2.id ASC LIMIT 1"
    "), label) WHERE id IN (SELECT survivor_id FROM wallet_merge_map);",
    "UPDATE wallets SET is_sample = CASE WHEN EXISTS ("
    "  SELECT 1 FROM wallets w2 "
    "  WHERE w2.canonical_address = wallets.canonical_address AND w2.is_sample = 0"
    ") THEN 0 ELSE is_sample END "
    "WHERE id IN (SELECT survivor_id FROM wallet_merge_map);",
    # Re-home wallet-linked dependent rows to survivors.
    "UPDATE wallet_balances SET wallet_id = (SELECT survivor_id FROM wallet_merge_map WHERE duplicate_id = wallet_balances.wallet_id) "
    "WHERE wallet_id IN (SELECT duplicate_id FROM wallet_merge_map);",
    "UPDATE positions SET wallet_id = (SELECT survivor_id FROM wallet_merge_map WHERE duplicate_id = positions.wallet_id) "
    "WHERE wallet_id IN (SELECT duplicate_id FROM wallet_merge_map);",
    "UPDATE orders SET wallet_id = (SELECT survivor_id FROM wallet_merge_map WHERE duplicate_id = orders.wallet_id) "
    "WHERE wallet_id IN (SELECT duplicate_id FROM wallet_merge_map);",
    "UPDATE decision_log SET wallet_id = (SELECT survivor_id FROM wallet_merge_map WHERE duplicate_id = decision_log.wallet_id) "
    "WHERE wallet_id IN (SELECT duplicate_id FROM wallet_merge_map);",
    "UPDATE performance_summaries SET wallet_id = (SELECT survivor_id FROM wallet_merge_map WHERE duplicate_id = performance_summaries.wallet_id) "
    "WHERE wallet_id IN (SELECT duplicate_id FROM wallet_merge_map);",
    "DELETE FROM wallets WHERE id IN (SELECT duplicate_id FROM wallet_merge_map);",
    "DROP TABLE IF EXISTS temp.wallet_merge_map;",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_wallets_canonical_address ON wallets(canonical_address);",
    "PRAGMA foreign_key_check;",
]

# ── Version 7: persist Polymarket trade and outcome token identity ─────────────
# Round-1 PR-1 of the recovery sequence. Purely additive, idempotent, no data
# loss. Two nullable columns + one nullable index. Both columns default to
# NULL on existing rows; no production backfill in this PR.
#
# Idempotency contract: each ``ALTER TABLE ... ADD COLUMN`` statement below
# is GUARDED by the migration runner via ``PRAGMA table_info(...)``. The
# runner applies the ALTER only when the column is absent; re-running v7 on
# a v7 DB is a no-op. The index uses ``CREATE INDEX IF NOT EXISTS`` which is
# natively idempotent in SQLite.
_V7_DDL: list[str] = [
    # market_outcomes.clob_token_id — persisted CLOB token for each outcome.
    # Allows the canonical mapping helper to join ``source_trades.token_id``
    # to ``market_outcomes.clob_token_id`` for both binary and multi-outcome
    # markets. Nullable; existing rows remain NULL until the next ingest
    # round repopulates them (no production backfill in this PR).
    _idempotent_add_column_ddl("market_outcomes", "clob_token_id", "TEXT"),
    # source_trades.token_id — persisted upstream asset/identifier per
    # trade, taken from the data-api ``asset`` field. Nullable; existing
    # rows remain NULL. Together with the market-outcome column above this
    # is the bridge that PR 2 (copy-candidate persistence) consumes.
    _idempotent_add_column_ddl("source_trades", "token_id", "TEXT"),
    # Index on the new join column to serve the canonical token-join
    # directly. NULLs are indexed; this is the standard SQLite behavior for
    # B-tree indexes on a nullable column.
    "CREATE INDEX IF NOT EXISTS idx_market_outcomes_token "
    "ON market_outcomes(clob_token_id);",
]


# ── Version 8: persist evaluated copy candidates (PR-2 of recovery) ───────────
# Round-1 PR-2 of the recovery sequence. Purely additive, idempotent, no
# data loss. One new table + four indexes. No existing column is altered.
#
# Identity contract: the candidate layer's idempotency key is the triple
# ``(wallet_id, source, source_trade_id)`` — ``source_trade_id`` is NOT
# globally unique (two providers can legitimately emit the same string),
# and ``wallet_id`` alone is insufficient because a wallet may have trades
# from multiple sources (e.g. polymarket_data_api vs. a future ingest
# path). The bounded source-qualified key is enforced via UNIQUE on the
# table itself.
#
# All FK columns are nullable on purpose: a candidate row may be
# REJECTED at upstream stages (resolver INCOMPLETE, market closed,
# invalid trade fields) where the market_id / market_outcome_id / etc.
# have no value. The constraints still apply — SQLite enforces them
# only when ``PRAGMA foreign_keys=ON`` is set, which the Database class
# does on connect.
#
# Idempotency contract: ``CREATE TABLE IF NOT EXISTS`` and
# ``CREATE INDEX IF NOT EXISTS`` are natively idempotent in SQLite; re-
# running v8 on a v8 DB is a no-op.
_V8_DDL: list[str] = [
    """CREATE TABLE IF NOT EXISTS copy_candidates (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet_id                   TEXT    NOT NULL REFERENCES wallets(id),
        source                      TEXT    NOT NULL,
        source_trade_id             TEXT    NOT NULL,
        source_trade_internal_id    TEXT    REFERENCES source_trades(id),
        market_id                   TEXT    REFERENCES markets(id),
        market_outcome_id           INTEGER REFERENCES market_outcomes(id),
        market_source_id            TEXT,
        token_id                    TEXT,
        outcome_label               TEXT,
        side                        TEXT    NOT NULL,
        source_trade_price          REAL    NOT NULL,
        source_trade_quantity       REAL    NOT NULL,
        source_trade_notional       REAL,
        source_trade_timestamp      TEXT    NOT NULL,
        observed_at                 TEXT    NOT NULL,
        wallet_score_version        TEXT    NOT NULL,
        wallet_score                REAL    NOT NULL,
        wallet_verdict              TEXT    NOT NULL,
        status                      TEXT    NOT NULL,
        status_reason               TEXT,
        metrics_json                TEXT,
        created_at                  TEXT    NOT NULL,
        updated_at                  TEXT    NOT NULL,
        UNIQUE(wallet_id, source, source_trade_id)
    );""",
    # Status-index for the bounded workflow queries
    # ("what is pending?" / "what was rejected and why?"). Indexed even
    # though the table is empty in production until PR-3+ wires up scan
    # persistence; the cost is negligible and PR-3 will rely on it.
    "CREATE INDEX IF NOT EXISTS idx_copy_candidates_status "
    "ON copy_candidates(status);",
    # Wallet-side lookup ("all candidates for wallet X").
    "CREATE INDEX IF NOT EXISTS idx_copy_candidates_wallet "
    "ON copy_candidates(wallet_id);",
    # Reverse lookup ("what candidates reference source_trade Y?").
    "CREATE INDEX IF NOT EXISTS idx_copy_candidates_source_trade_internal "
    "ON copy_candidates(source_trade_internal_id);",
    # Outcome-side lookup ("all candidates for outcome X").
    "CREATE INDEX IF NOT EXISTS idx_copy_candidates_market_outcome "
    "ON copy_candidates(market_outcome_id);",
]


# ── Version 9: candidate price snapshots (schema-bridge) ─────────────────────
# This is the v8 → v9 schema-bridge migration. Purely additive, idempotent,
# no data loss. One new table + three indexes. No existing column is altered;
# no new column is added to ``copy_candidates`` (the "latest snapshot" is a
# query, not a foreign key).
#
# Identity contract: the per-run idempotency key is the pair
# ``(candidate_id, snapshot_run_id)``. ``snapshot_run_id`` is a UUID chosen
# by the caller of the snapshot engine; rerunning with the same id is a
# no-op (INSERT OR IGNORE). A NEW run-id creates a NEW observation. The
# append-only history is preserved.
#
# All book/snapshot fields are nullable: an OK snapshot populates them, but
# a non-OK snapshot (EMPTY_BOOK, ONE_SIDED_BOOK, MISSING_TOKEN,
# NOT_PENDING, MARKET_NOT_OPEN, RATE_LIMITED, HTTP_ERROR, TIMEOUT,
# PARSE_ERROR) leaves them NULL and records the bounded reason in
# ``fetch_status`` + ``fetch_error_code`` + ``fetch_error_message``. The
# source-trade fields (``source_trade_price``, ``source_trade_quantity``,
# ``source_trade_timestamp``) and the ``side`` are always populated (they
# come from the underlying ``copy_candidates`` row, which has them as
# NOT NULL); this guarantees the table never holds a row that cannot be
# joined back to its candidate.
#
# The market-end metadata is COPIED from ``markets.end_date`` (already
# populated by the existing Gamma ingestion path) into
# ``market_end_at`` + ``market_metadata_fetched_at`` at snapshot time.
# This makes the snapshot a self-contained historical observation. The
# bridge does NOT add a new column to ``markets`` — the existing
# ``markets.end_date`` is the authoritative declared-end field.
#
# ``seconds_to_market_end`` is an INTEGER (epoch-second deltas are
# always integers). Negative values are valid audit evidence and are
# preserved; the table does not cap or rewrite them.
_V9_DDL: list[str] = [
    """CREATE TABLE IF NOT EXISTS candidate_price_snapshots (
        id                          TEXT PRIMARY KEY,
        candidate_id                INTEGER NOT NULL
                                        REFERENCES copy_candidates(id),

        snapshot_run_id             TEXT NOT NULL,
        fetch_status                TEXT NOT NULL,
        fetch_endpoint              TEXT,
        fetch_http_status           INTEGER,
        fetch_latency_ms            INTEGER,
        request_attempts            INTEGER NOT NULL DEFAULT 1,
        fetch_error_code            TEXT,
        fetch_error_message         TEXT,

        token_id                    TEXT,
        side                        TEXT NOT NULL,
        source_trade_price          REAL NOT NULL,
        source_trade_quantity       REAL NOT NULL,
        source_trade_timestamp      TEXT NOT NULL,

        best_bid                    REAL,
        best_bid_size               REAL,
        best_ask                    REAL,
        best_ask_size               REAL,
        mid_price                   REAL,
        spread                      REAL,

        executable_price            REAL,
        executable_side_depth       REAL,
        expected_fill_price         REAL,

        price_deterioration         REAL,
        price_deterioration_pct     REAL,
        mid_change                  REAL,
        mid_change_pct              REAL,

        trade_age_seconds           INTEGER,
        market_end_at               TEXT,
        seconds_to_market_end       INTEGER,
        market_metadata_fetched_at  TEXT,

        market_active_at_fetch      INTEGER,
        market_closed_at_fetch      INTEGER,
        market_resolved_at_fetch    INTEGER,

        bid_level_count             INTEGER,
        ask_level_count             INTEGER,
        book_summary_json           TEXT,
        book_hash                   TEXT,

        fetched_at                  TEXT NOT NULL,
        created_at                  TEXT NOT NULL,

        UNIQUE(candidate_id, snapshot_run_id)
    );""",
    # Latest-snapshot lookup ("what is the most recent snapshot for this
    # candidate?"). Uses DESC ordering on (fetched_at, id) so the query
    # plan can serve the answer directly from the index without a sort.
    "CREATE INDEX IF NOT EXISTS idx_cps_candidate_fetched "
    "ON candidate_price_snapshots(candidate_id, fetched_at DESC, id DESC);",
    # Status-side filter ("show me every EMPTY_BOOK / RATE_LIMITED
    # snapshot in the run").
    "CREATE INDEX IF NOT EXISTS idx_cps_status "
    "ON candidate_price_snapshots(fetch_status);",
    # Run-side filter ("all rows for snapshot run X").
    "CREATE INDEX IF NOT EXISTS idx_cps_run "
    "ON candidate_price_snapshots(snapshot_run_id);",
]


# ── Migration registry ──────────────────────────────────────────────────────────
# Key = target version, Value = list of DDL statements to reach that version
# from (version - 1). Statements are run in order by the migration runner;
# each statement is committed individually via the runner's _set_version
# checkpoint.
MIGRATIONS: dict[int, list[str]] = {
    1: _V1_DDL,
    2: _V2_DDL,
    3: _V3_DDL,
    4: _V4_DDL,
    5: _V5_DDL,
    6: _V6_DDL,
    7: _V7_DDL,
    8: _V8_DDL,
    9: _V9_DDL,
    10: _V10_DDL,
    11: _V11_DDL,
    12: _V12_DDL,
    13: _V13_DDL,
    14: _V14_DDL,
    15: _V15_DDL,
    16: _V16_DDL,
    17: _V17_DDL,
    18: _V18_DDL,
    19: _V19_DDL,
    20: _V20_DDL,
    21: _V21_DDL,
}

# Current DDL is the latest migration
CURRENT_DDL: list[str] = MIGRATIONS[SCHEMA_VERSION] 
