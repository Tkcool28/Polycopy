"""Version 16 schema migration — PR24P Trade Copyability v1 trace fields.

PR24P hardens Trade Copyability Score v1 (BUY-only) and adds additive
price-trace columns to ``trade_copyability_decisions`` so the new
traceability fields on :class:`TradeCopyabilityInputV1` round-trip
through persistence without loss:

  * ``source_entry_price``        — price the source wallet got on the trade
  * ``current_copy_price``        — visible current price when evaluating
  * ``estimated_fill_price``      — actual estimated fill/VWAP from depth walk
  * ``source_trade_timestamp``    — source trade's timestamp
  * ``price_snapshot_fetched_at`` — time of the price/depth snapshot
  * ``evaluation_timestamp``      — time the copyability score was computed

The columns are additive, nullable, and bounded (the price columns
carry the same ``[0, 1]`` CHECK as the other probability-price columns).
No scoring formula, timer, automation, or production DB write is enabled
by this migration. The production DB is migrated only later, when a
service is intentionally deployed/restarted (the runner applies this
migration idempotently on next connect).

Why this migration is safe
==========================

1. Every statement is ``ALTER TABLE ... ADD COLUMN`` and is gated by the
   migration runner's ``PRAGMA table_info`` check (``_execute_migration_statement``);
   re-running v16 on an already-v16 DB is a no-op.
2. No existing column is tightened, dropped, or renamed.
3. No existing data is rewritten. New columns default to NULL, so legacy
   rows remain semantically identical.
4. No CHECK constraints are added that would reject legacy values.
5. No FK changes.
6. No scoring formula, timer, automation, or runtime job is added.
"""

from __future__ import annotations

# Plain ALTER TABLE ADD COLUMN statements. The production runner guards
# each one with a column-existence check (PRAGMA table_info), so this is
# safe to re-run on an already-v16 database. Mirrors the v14 pattern.
_V16_DDL: list[str] = [
    # ── trade_copyability_decisions price-trace columns ─────────────────
    "ALTER TABLE trade_copyability_decisions ADD COLUMN "
    "source_entry_price REAL "
    "CHECK (source_entry_price IS NULL OR "
    "(source_entry_price >= 0 AND source_entry_price <= 1));",
    "ALTER TABLE trade_copyability_decisions ADD COLUMN "
    "current_copy_price REAL "
    "CHECK (current_copy_price IS NULL OR "
    "(current_copy_price >= 0 AND current_copy_price <= 1));",
    "ALTER TABLE trade_copyability_decisions ADD COLUMN "
    "estimated_fill_price REAL "
    "CHECK (estimated_fill_price IS NULL OR "
    "(estimated_fill_price >= 0 AND estimated_fill_price <= 1));",
    "ALTER TABLE trade_copyability_decisions ADD COLUMN "
    "source_trade_timestamp TEXT;",
    "ALTER TABLE trade_copyability_decisions ADD COLUMN "
    "price_snapshot_fetched_at TEXT;",
    "ALTER TABLE trade_copyability_decisions ADD COLUMN "
    "evaluation_timestamp TEXT;",
]

__all__ = ["_V16_DDL"]
