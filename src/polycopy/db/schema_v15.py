"""Version 15 schema migration — PR24I settlement accounting ledger.

PR24I adds an accounting-ready ledger derived from already-known
``source_trades`` settlement truth. The table is additive and inert:
no scoring formula, specialist aggregation, live fetch, timer, or PR20
runtime consumes it in this migration.

The ledger stores one deterministic accounting row per source trade via
``UNIQUE(source_trade_id)``. ``source_trade_id`` is the internal
``source_trades.id`` UUID, not the provider-scoped external trade id.
"""

from __future__ import annotations

_V15_DDL: list[str] = [
    """CREATE TABLE IF NOT EXISTS settlement_accounting_ledger (
        id TEXT PRIMARY KEY,

        source_trade_id TEXT NOT NULL REFERENCES source_trades(id),
        wallet_id TEXT,
        trader_address TEXT,

        market_id TEXT,
        market_source_id TEXT,
        token_id TEXT,
        winning_token_id TEXT,

        side TEXT,
        outcome TEXT,

        quantity REAL,
        price REAL,
        cost_basis REAL,
        payout REAL,
        realized_pnl REAL,
        roi REAL,

        resolution_status TEXT NOT NULL,
        is_winning_trade INTEGER,

        accounting_status TEXT NOT NULL,
        accounting_reason TEXT,

        settlement_source TEXT,
        resolved_at TEXT,

        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,

        UNIQUE(source_trade_id)
    );""",
    """CREATE INDEX IF NOT EXISTS idx_settlement_ledger_wallet
       ON settlement_accounting_ledger(wallet_id);""",
    """CREATE INDEX IF NOT EXISTS idx_settlement_ledger_trader
       ON settlement_accounting_ledger(trader_address);""",
    """CREATE INDEX IF NOT EXISTS idx_settlement_ledger_market
       ON settlement_accounting_ledger(market_id);""",
    """CREATE INDEX IF NOT EXISTS idx_settlement_ledger_status
       ON settlement_accounting_ledger(resolution_status, accounting_status);""",
    """CREATE INDEX IF NOT EXISTS idx_settlement_ledger_source_trade
       ON settlement_accounting_ledger(source_trade_id);""",
]

__all__ = ["_V15_DDL"]
