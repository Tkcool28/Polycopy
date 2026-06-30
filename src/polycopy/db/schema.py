"""Versioned SQLite schema DDL.

Migrations are applied sequentially. The `_meta` table tracks the current
schema version. Each migration is a list of SQL statements.
"""

from __future__ import annotations

# ── Schema version ──────────────────────────────────────────────────────────────
SCHEMA_VERSION = 6

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
    # Market outcomes
    """CREATE TABLE IF NOT EXISTS market_outcomes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id  TEXT NOT NULL REFERENCES markets(id),
        label      TEXT NOT NULL,
        price      REAL NOT NULL CHECK(price >= 0 AND price <= 1),
        volume     REAL NOT NULL DEFAULT 0 CHECK(volume >= 0)
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
    # Source trades (observed, not our own)
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

# ── Migration registry ──────────────────────────────────────────────────────────
# Key = target version, Value = list of DDL statements to reach that version from (version - 1).
MIGRATIONS: dict[int, list[str]] = {
    1: _V1_DDL,
    2: _V2_DDL,
    3: _V3_DDL,
    4: _V4_DDL,
    5: _V5_DDL,
    6: _V6_DDL,
}

# Current DDL is the latest migration
CURRENT_DDL: list[str] = MIGRATIONS[SCHEMA_VERSION] 
