"""Version 14 schema migration — PR24A resolution-truth pipeline foundation.

This is the schema leg of PR24A. It adds the durable storage needed to
record *which* outcome token won a market and *how* every observed
``source_trades`` row settled against that truth. PR24A does NOT consume
these fields in any scoring formula; downstream layers (PR20 specialist
aggregation, copy-candidate settlement) will read from them once they
ship.

Additive, idempotent, no destructive changes:

  * ``market_outcomes.is_winner INTEGER`` — 1=won, 0=lost, NULL=unknown.
    Nullable, defaulted to NULL so existing rows are unchanged.

  * ``markets.winning_token_id TEXT`` — token id of the winning outcome
    when known; NULL when unresolved / unknown / ambiguous. Nullable,
    defaulted to NULL.

  * ``markets.resolution_checked_at TEXT`` — ISO-8601 timestamp of the
    most recent resolution check (success or no-op). Nullable, defaulted
    to NULL.

  * ``markets.resolution_source TEXT`` — provenance tag for the last
    resolution check (e.g. ``"polymarket_gamma"``, ``"clob"``,
    ``"manual_test_fixture"``). Nullable, defaulted to NULL.

  * ``source_trades.resolution_status TEXT DEFAULT 'unresolved'`` —
    settlement status for the trade against the resolved market truth.
    Allowed values: ``unresolved``, ``won``, ``lost``, ``ambiguous``,
    ``unknown``. The default (``unresolved``) preserves prior semantics
    for every existing row.

  * ``source_trades.resolved_at TEXT`` — ISO-8601 timestamp of settlement
    (NULL until settlement runs).

  * ``source_trades.winning_token_id TEXT`` — winning token at the time
    of settlement (NULL until settlement runs).

  * ``source_trades.is_winning_trade INTEGER`` — 1=won, 0=lost, NULL=unknown.

  * ``source_trades.realized_pnl REAL`` — realized payoff when the
    trade has enough cost/size fields to compute it. NULL otherwise;
    we never invent a P/L.

  * ``source_trades.settlement_source TEXT`` — provenance tag for the
    settlement source (e.g. ``"backfill_resolution_truth"``,
    ``"manual_test_fixture"``).

Indexes (additive, idempotent):

  * ``idx_market_outcomes_winner`` — for the ``is_winner=1`` lookup
    pattern (one winner per market).
  * ``idx_source_trades_resolution_status`` — for the "what's still
    unresolved?" query pattern.
  * ``idx_source_trades_winning_token`` — for "settle against winner"
    join lookups.

Why this migration is safe
==========================

1. Every ALTER TABLE is gated by the migration runner's PRAGMA
   table_info check (``_execute_migration_statement``); re-running v14
   on a v14 DB is a no-op.
2. Every CREATE INDEX uses IF NOT EXISTS (native SQLite idempotency).
3. No existing column is tightened, dropped, or renamed.
4. No existing data is rewritten. The new columns default to NULL /
   'unresolved' so legacy rows remain semantically identical.
5. No CHECK constraints are added that would reject legacy values.
6. No FK changes; the winner columns are scoped to existing tables.

What PR24A does NOT do
======================

* Does NOT add a new table.
* Does NOT compute winning tokens from text or heuristics; this
  migration is purely the storage layer.
* Does NOT enable specialist aggregation, scoring, or trading.
* Does NOT add a runtime job. The helper modules in
  ``src/polycopy/engine/market_resolution_truth.py`` and the extended
  ``trade_resolution.settle_source_trade_against_truth`` are pure
  functions / persisted-write helpers, not scheduled work.
"""

from __future__ import annotations

_V14_DDL: list[str] = [
    # ── market_outcomes.is_winner ────────────────────────────────────────
    # 1 = this outcome won, 0 = lost, NULL = unknown / not checked.
    # Nullable so existing rows stay NULL (no fake winners for any
    # legacy market). The migration runner skips this ALTER if the
    # column already exists.
    "ALTER TABLE market_outcomes ADD COLUMN is_winner INTEGER;",
    # ── markets.winning_token_id ─────────────────────────────────────────
    # Token id of the winning outcome when the market has a known
    # winner. NULL otherwise (unresolved, ambiguous, not yet checked).
    "ALTER TABLE markets ADD COLUMN winning_token_id TEXT;",
    # ── markets.resolution_checked_at ───────────────────────────────────
    # ISO-8601 UTC timestamp of the most recent resolution check,
    # regardless of outcome. Lets us audit "when did we last look?"
    # without implying a winner.
    "ALTER TABLE markets ADD COLUMN resolution_checked_at TEXT;",
    # ── markets.resolution_source ───────────────────────────────────────
    # Free-text provenance tag for the last check
    # (e.g. "polymarket_gamma", "clob", "manual_test_fixture").
    "ALTER TABLE markets ADD COLUMN resolution_source TEXT;",
    # ── source_trades.resolution_status ─────────────────────────────────
    # Settlement status of this trade against the resolved truth.
    # Default 'unresolved' so existing rows preserve their prior
    # semantics. Allowed values:
    #   unresolved | won | lost | ambiguous | unknown
    "ALTER TABLE source_trades ADD COLUMN resolution_status TEXT DEFAULT 'unresolved';",
    # ── source_trades.resolved_at ────────────────────────────────────────
    # ISO-8601 UTC timestamp of when this trade was settled against
    # a winning_token_id. NULL until settlement runs.
    "ALTER TABLE source_trades ADD COLUMN resolved_at TEXT;",
    # ── source_trades.winning_token_id ──────────────────────────────────
    # The winning token recorded at the time this trade was settled.
    # Captured at settlement time so later winner churn on the
    # market doesn't rewrite the trade's history.
    "ALTER TABLE source_trades ADD COLUMN winning_token_id TEXT;",
    # ── source_trades.is_winning_trade ──────────────────────────────────
    # 1=won (trade token == winning token), 0=lost (winning token
    # known, trade token different), NULL=unresolved/unknown/ambiguous.
    "ALTER TABLE source_trades ADD COLUMN is_winning_trade INTEGER;",
    # ── source_trades.realized_pnl ───────────────────────────────────────
    # Binary-payoff realized P/L: (1 - price) * quantity when winning,
    # -price * quantity when losing. Nullable: if price or quantity is
    # missing / non-finite, we leave it NULL rather than fabricate a
    # number. Settlement writes only when both fields are usable.
    "ALTER TABLE source_trades ADD COLUMN realized_pnl REAL;",
    # ── source_trades.settlement_source ─────────────────────────────────
    # Provenance tag for who/what performed the settlement
    # (e.g. "backfill_resolution_truth", "manual_test_fixture").
    "ALTER TABLE source_trades ADD COLUMN settlement_source TEXT;",
    # ── Indexes ──────────────────────────────────────────────────────────
    # Lookup the single winning outcome for a market
    # (SELECT * FROM market_outcomes WHERE market_id=? AND is_winner=1).
    "CREATE INDEX IF NOT EXISTS idx_market_outcomes_winner "
    "ON market_outcomes(market_id, is_winner);",
    # Status filter ("show me every still-unresolved trade").
    "CREATE INDEX IF NOT EXISTS idx_source_trades_resolution_status "
    "ON source_trades(resolution_status);",
    # Settle-against-winner join lookup ("what did this winner settle?").
    "CREATE INDEX IF NOT EXISTS idx_source_trades_winning_token "
    "ON source_trades(winning_token_id);",
]