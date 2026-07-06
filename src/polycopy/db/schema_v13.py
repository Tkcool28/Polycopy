"""Version 13 schema migration — additive specialist-metric evidence table.

Introduces ONE new table, ``wallet_specialist_aggregations``, that
persists conservative aggregation evidence for the already-planned
specialist wallet formula (PR #20).

Design notes
============

* **Evidence, not formula input.** This table is a transparent log of
  metrics that *can* be computed honestly from existing data. Nothing
  in this migration (or PR #20) consumes these rows inside a scoring
  formula; they are observable audit data so a future specialist
  formula PR has a defensible foundation.

* **Idempotent.** The new table and indexes use
  ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` so
  the migration is safe to run multiple times against the same DB.

* **Non-destructive.** No existing table is altered. No CHECK
  constraints are tightened. No data is moved or rebuilt.

* **Conservative column set.** Every column is nullable (or has an
  explicit ``CHECK (... >= 0)``) so a partial evidence bundle can be
  persisted without coercion. ``quality`` is a free string tag; the
  runtime values are restricted to ``'observed' | 'partial' |
  'unknown' | 'incomplete'`` via the application layer
  (:mod:`polycopy.scoring.specialist_metrics_persistence`).

* **Blocked metrics deliberately absent.** Realized P/L, win rate,
  profit factor, max drawdown, and ``resolved_markets`` are **not**
  columns on this table — they cannot be computed honestly while
  ``markets.resolved = 0`` everywhere and ``market_outcomes.clob_token_id``
  is NULL on every row. Adding columns for them would invite fake
  zeros; we persist the gap in ``missing_essentials_json`` instead.

PR scope
========
PR #20 only writes rows through
:mod:`polycopy.scoring.specialist_metrics_persistence`. No scoring
formula consumes this table until a follow-on PR designs and reviews
the consumer.
"""

from __future__ import annotations

_V13_DDL: list[str] = [
    # Wallet specialist aggregations — additive evidence table.
    # All numeric columns are NULLABLE so a partial evidence bundle
    # can be persisted without coercion. ``quality`` and
    # ``missing_essentials_json`` document which fields are missing
    # and why. UNIQUE on
    # (wallet_id, category_label, formula_name, formula_version,
    # idempotency_key) is the re-run idempotency key.
    """CREATE TABLE IF NOT EXISTS wallet_specialist_aggregations (
        aggregation_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet_id               TEXT    NOT NULL REFERENCES wallets(id),
        category_label          TEXT    NOT NULL,
        formula_name            TEXT    NOT NULL,
        formula_version         TEXT    NOT NULL,
        idempotency_key         TEXT    NOT NULL,
        source_data_timestamp   TEXT    NOT NULL,

        -- READY-NOW metrics (computed honestly from existing data)
        trade_count               INTEGER CHECK (trade_count IS NULL OR trade_count >= 0),
        distinct_markets          INTEGER CHECK (distinct_markets IS NULL OR distinct_markets >= 0),
        distinct_events           INTEGER CHECK (distinct_events IS NULL OR distinct_events >= 0),
        active_trading_days       INTEGER CHECK (active_trading_days IS NULL OR active_trading_days >= 0),
        category_trade_count      INTEGER CHECK (category_trade_count IS NULL OR category_trade_count >= 0),
        category_distinct_markets INTEGER CHECK (category_distinct_markets IS NULL OR category_distinct_markets >= 0),
        category_active_days      INTEGER CHECK (category_active_days IS NULL OR category_active_days >= 0),
        category_concentration    REAL,
        sample_reliability_score  REAL,

        -- PARTIAL metrics (persisted with quality tag; do NOT feed formulas yet)
        holding_period_days       INTEGER CHECK (holding_period_days IS NULL OR holding_period_days >= 0),

        -- SHADOW metrics (state-only; no numeric content in this PR)
        behavior_classification   TEXT,
        copyability_evidence_state TEXT,
        price_improvement_state   TEXT,

        -- Quality + missing-evidence bookkeeping
        component_scores_json     TEXT NOT NULL,
        quality                   TEXT NOT NULL,
        missing_essentials_json   TEXT NOT NULL,
        created_at                TEXT NOT NULL,

        UNIQUE(wallet_id, category_label, formula_name, formula_version, idempotency_key)
    );""",

    "CREATE INDEX IF NOT EXISTS idx_wsa_wallet "
    "ON wallet_specialist_aggregations(wallet_id);",

    "CREATE INDEX IF NOT EXISTS idx_wsa_category "
    "ON wallet_specialist_aggregations(category_label);",

    "CREATE INDEX IF NOT EXISTS idx_wsa_quality "
    "ON wallet_specialist_aggregations(quality);",

    "CREATE INDEX IF NOT EXISTS idx_wsa_wallet_category "
    "ON wallet_specialist_aggregations(wallet_id, category_label);",
]