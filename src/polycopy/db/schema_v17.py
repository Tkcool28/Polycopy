"""PR66 additive wallet-evidence metadata storage."""
from __future__ import annotations

_V17_DDL: list[str] = [
    "ALTER TABLE source_trades ADD COLUMN metadata_json TEXT;",
    "CREATE INDEX IF NOT EXISTS idx_source_trades_wallet_timestamp "
    "ON source_trades(trader_address, timestamp);",
]
