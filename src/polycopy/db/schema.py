"""Versioned SQLite schema DDL.

Migrations are applied sequentially. The `_meta` table tracks the current
schema version. Each migration is a list of SQL statements.
"""

from __future__ import annotations

# ── Schema version ──────────────────────────────────────────────────────────────
SCHEMA_VERSION = 4

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


# ── Migration registry ──────────────────────────────────────────────────────────
# Key = target version, Value = list of DDL statements to reach that version from (version - 1).
MIGRATIONS: dict[int, list[str]] = {
    1: _V1_DDL,
    2: _V2_DDL,
    3: _V3_DDL,
    4: _V4_DDL,
}

# Current DDL is the latest migration
CURRENT_DDL: list[str] = MIGRATIONS[SCHEMA_VERSION] 
