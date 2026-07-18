"""Schema version 21 — Canonical Specialist Evidence Accumulation (research plane).

This migration adds the research-only evidence plane that turns unapproved
real wallets into *evaluatable* wallets WITHOUT ever authorizing execution:

  * ``specialist_evidence_watchlist`` — durable research-only membership. One
    ACTIVE watch per wallet (partial unique index). Watchlist membership grants
    RESEARCH permission only; it can NOT satisfy ``specialist_approvals``,
    dispatch, or execution. No FK here references any approval/execution table.
  * ``specialist_market_refresh_state`` — scheduling/bookkeeping only for the
    market-centric resolution refresher. It is NEVER a resolution-authority
    source; canonical truth stays on ``source_trades.resolution_status`` /
    ``source_trades.winning_token_id`` and the source-trade resolution
    provenance columns.

CRITICAL fresh-database correction (PR #70 completeness)
----------------------------------------------------------------
The repository's migration-runner reconciliation short-circuit
(``Database._physical_schema_at_target``) only requires the v13 base objects
(see ``_REQUIRED_V13_OBJECTS``). The approved-specialist + paper-execution
tables introduced by ``schema_v18`` / ``schema_v19`` (PR #70) are NOT in
that required set. A *fresh* DB opened at v21 reaches the short-circuit and
would therefore SKIP every PR #70 table, leaving the code unable to open a
complete v21 database.

To keep a fresh v21 DB self-sufficient, this module MUST carry the full
idempotent DDL (``CREATE TABLE/INDEX IF NOT EXISTS``) for every PR #70
object that the code depends on, in addition to the two new research-plane
tables. The forward-migrate path (v20 -> v21) already applies them because
v18/v19 are in the migration chain, but the fresh-build path needs them
inline. Nothing here alters v18/v19 semantics; the statements are
idempotent and only ever add objects that are absent.

All DDL is idempotent (CREATE TABLE/INDEX IF NOT EXISTS). The migration
runner applies it in order; ``Database._physical_schema_at_target`` is
extended (``_REQUIRED_V21_OBJECTS``) to require the new + PR #70 objects
before claiming target shape.
"""

from __future__ import annotations

# ── PR #70 (v18) objects ─────────────────────────────────────────────────────
# Inlined verbatim from schema_v18 so a fresh v21 DB is complete. Idempotent.
_V18_DDL: list[str] = [
    """CREATE TABLE IF NOT EXISTS specialist_approvals (
        approval_id              TEXT PRIMARY KEY,
        wallet_address           TEXT NOT NULL,
        specialist_category      TEXT NOT NULL,
        wallet_score_decision_id TEXT,
        category_score_decision_id TEXT,
        formula_name             TEXT NOT NULL,
        formula_version          TEXT NOT NULL,
        evidence_fingerprint     TEXT,
        evidence_report_path     TEXT,
        reviewer                 TEXT NOT NULL,
        approval_reason          TEXT,
        approved_at              TEXT NOT NULL,
        enabled                  INTEGER NOT NULL DEFAULT 1
                                    CHECK (enabled IN (0, 1)),
        monitoring_enabled       INTEGER NOT NULL DEFAULT 1
                                    CHECK (monitoring_enabled IN (0, 1)),
        revoked_at               TEXT,
        revoked_by               TEXT,
        revocation_reason        TEXT,
        created_at               TEXT NOT NULL,
        updated_at               TEXT NOT NULL
    );""",
    "CREATE INDEX IF NOT EXISTS idx_specialist_approvals_wallet "
    "ON specialist_approvals(wallet_address);",
    "CREATE INDEX IF NOT EXISTS idx_specialist_approvals_wallet_category "
    "ON specialist_approvals(wallet_address, specialist_category, formula_version);",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_specialist_approvals_active "
    "ON specialist_approvals(wallet_address, specialist_category, formula_version) "
    "WHERE enabled = 1 AND revoked_at IS NULL;",

    """CREATE TABLE IF NOT EXISTS paper_signal_execution_authorizations (
        authorization_id         TEXT PRIMARY KEY,
        paper_signal_decision_id INTEGER NOT NULL
                                    REFERENCES paper_signal_decisions(id),
        specialist_approval_id   TEXT NOT NULL
                                    REFERENCES specialist_approvals(approval_id),
        source_trade_id          TEXT NOT NULL,
        candidate_id             INTEGER NOT NULL REFERENCES copy_candidates(id),
        authorized_by            TEXT NOT NULL,
        authorization_reason     TEXT,
        review_notes             TEXT,
        status                   TEXT NOT NULL DEFAULT 'active'
                                    CHECK (status IN ('active', 'used', 'revoked')),
        policy_version           TEXT NOT NULL,
        approved_at              TEXT NOT NULL,
        used_at                  TEXT,
        created_at               TEXT NOT NULL,
        updated_at               TEXT NOT NULL,
        UNIQUE(paper_signal_decision_id)
    );""",
    "CREATE INDEX IF NOT EXISTS idx_paper_signal_exec_authz_signal "
    "ON paper_signal_execution_authorizations(paper_signal_decision_id);",

    """CREATE TABLE IF NOT EXISTS execution_risk_decisions (
        risk_decision_id         TEXT PRIMARY KEY,
        execution_attempt_id      TEXT UNIQUE NOT NULL,
        paper_signal_decision_id INTEGER NOT NULL
                                    REFERENCES paper_signal_decisions(id),
        specialist_approval_id     TEXT
                                    REFERENCES specialist_approvals(approval_id),
        authorization_id           TEXT
                                    REFERENCES paper_signal_execution_authorizations(authorization_id),
        source_trade_id            TEXT NOT NULL,
        candidate_id               INTEGER NOT NULL REFERENCES copy_candidates(id),
        snapshot_id                TEXT REFERENCES candidate_price_snapshots(id),
        decision                   TEXT NOT NULL CHECK (decision IN
            ('allow', 'block', 'no_op_already_executed', 'dry_run')),
        reason_codes             TEXT,
        requested_quantity         REAL,
        requested_price            REAL,
        estimated_fill_price       REAL,
        estimated_slippage         REAL,
        market_exposure_before     REAL NOT NULL DEFAULT 0,
        wallet_exposure_before     REAL NOT NULL DEFAULT 0,
        portfolio_exposure_before  REAL NOT NULL DEFAULT 0,
        configured_limits_json     TEXT,
        kill_switch_state          INTEGER NOT NULL,
        paper_mode                 TEXT NOT NULL,
        evidence_timestamp         TEXT,
        evaluated_at               TEXT NOT NULL,
        policy_version             TEXT NOT NULL,
        attempt_number             INTEGER NOT NULL DEFAULT 1
    );""",
    "CREATE INDEX IF NOT EXISTS idx_execution_risk_signal "
    "ON execution_risk_decisions(paper_signal_decision_id);",
    "CREATE INDEX IF NOT EXISTS idx_execution_risk_approval "
    "ON execution_risk_decisions(specialist_approval_id);",
    "CREATE INDEX IF NOT EXISTS idx_execution_risk_attempt "
    "ON execution_risk_decisions(paper_signal_decision_id, attempt_number);",

    """CREATE TABLE IF NOT EXISTS paper_orders (
        id                        TEXT PRIMARY KEY,
        specialist_approval_id    TEXT NOT NULL
                                    REFERENCES specialist_approvals(approval_id),
        source_trade_internal_id  TEXT NOT NULL REFERENCES source_trades(id),
        copy_candidate_id         INTEGER NOT NULL REFERENCES copy_candidates(id),
        candidate_price_snapshot_id TEXT REFERENCES candidate_price_snapshots(id),
        trade_copyability_decision_id INTEGER REFERENCES trade_copyability_decisions(id),
        paper_signal_decision_id  INTEGER NOT NULL
                                    REFERENCES paper_signal_decisions(id),
        execution_risk_decision_id TEXT NOT NULL
                                    REFERENCES execution_risk_decisions(risk_decision_id),
        source_wallet_id          TEXT NOT NULL REFERENCES wallets(id),
        market_id                 TEXT NOT NULL REFERENCES markets(id),
        wallet_id                 TEXT NOT NULL REFERENCES wallets(id),
        side                      TEXT NOT NULL,
        outcome                   TEXT NOT NULL,
        quantity                  REAL NOT NULL CHECK (quantity > 0),
        price                     REAL NOT NULL CHECK (price >= 0 AND price <= 1),
        status                    TEXT NOT NULL DEFAULT 'filled',
        requested_quantity        REAL,
        requested_price           REAL,
        fill_model_version        TEXT,
        created_at                TEXT NOT NULL,
        policy_version            TEXT NOT NULL,
        UNIQUE(paper_signal_decision_id)
    );""",
    "CREATE INDEX IF NOT EXISTS idx_paper_orders_signal "
    "ON paper_orders(paper_signal_decision_id);",
    "CREATE INDEX IF NOT EXISTS idx_paper_orders_approval "
    "ON paper_orders(specialist_approval_id);",
    "CREATE INDEX IF NOT EXISTS idx_paper_orders_source_trade "
    "ON paper_orders(source_trade_internal_id);",
    "CREATE INDEX IF NOT EXISTS idx_paper_orders_risk "
    "ON paper_orders(execution_risk_decision_id);",

    """CREATE TABLE IF NOT EXISTS paper_fills (
        fill_id                   TEXT PRIMARY KEY,
        order_id                  TEXT NOT NULL REFERENCES paper_orders(id),
        quantity                  REAL NOT NULL CHECK (quantity > 0),
        price                     REAL NOT NULL CHECK (price >= 0 AND price <= 1),
        fee                       REAL NOT NULL DEFAULT 0,
        slippage                  REAL,
        fill_model_version        TEXT NOT NULL,
        filled_at                 TEXT NOT NULL,
        snapshot_id               TEXT REFERENCES candidate_price_snapshots(id)
    );""",
    "CREATE INDEX IF NOT EXISTS idx_paper_fills_order "
    "ON paper_fills(order_id);",

    """CREATE TABLE IF NOT EXISTS paper_positions (
        id                        TEXT PRIMARY KEY,
        market_id                 TEXT NOT NULL REFERENCES markets(id),
        wallet_id                 TEXT NOT NULL REFERENCES wallets(id),
        outcome                   TEXT NOT NULL,
        quantity                  REAL NOT NULL CHECK (quantity >= 0),
        avg_entry_price           REAL NOT NULL CHECK (avg_entry_price >= 0 AND avg_entry_price <= 1),
        current_price             REAL NOT NULL CHECK (current_price >= 0 AND current_price <= 1),
        realized_pnl              REAL NOT NULL DEFAULT 0,
        source_wallet_id          TEXT NOT NULL REFERENCES wallets(id),
        source_trade_internal_id  TEXT NOT NULL REFERENCES source_trades(id),
        copy_candidate_id         INTEGER NOT NULL REFERENCES copy_candidates(id),
        paper_order_id            TEXT NOT NULL REFERENCES paper_orders(id),
        paper_fill_id             TEXT NOT NULL REFERENCES paper_fills(fill_id),
        paper_signal_decision_id  INTEGER NOT NULL
                                    REFERENCES paper_signal_decisions(id),
        execution_risk_decision_id TEXT NOT NULL
                                    REFERENCES execution_risk_decisions(risk_decision_id),
        opened_at                 TEXT NOT NULL,
        settled_at                TEXT,
        status                    TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'settled')),
        updated_at                TEXT,
        UNIQUE(paper_order_id)
    );""",
    "CREATE INDEX IF NOT EXISTS idx_paper_positions_order "
    "ON paper_positions(paper_order_id);",
    "CREATE INDEX IF NOT EXISTS idx_paper_positions_wallet "
    "ON paper_positions(wallet_id);",
    "CREATE INDEX IF NOT EXISTS idx_paper_positions_market "
    "ON paper_positions(market_id);",
    "CREATE INDEX IF NOT EXISTS idx_paper_positions_source_trade "
    "ON paper_positions(source_trade_internal_id);",

    """CREATE TABLE IF NOT EXISTS paper_position_lots (
        id              TEXT PRIMARY KEY,
        position_id     TEXT NOT NULL REFERENCES paper_positions(id),
        paper_fill_id   TEXT NOT NULL REFERENCES paper_fills(fill_id),
        quantity        REAL NOT NULL CHECK (quantity > 0),
        entry_price     REAL NOT NULL CHECK (entry_price >= 0 AND entry_price <= 1),
        opened_at       TEXT NOT NULL
    );""",
    "CREATE INDEX IF NOT EXISTS idx_paper_position_lots_position "
    "ON paper_position_lots(position_id);",

    """CREATE TABLE IF NOT EXISTS paper_position_marks (
        id              TEXT PRIMARY KEY,
        position_id     TEXT NOT NULL REFERENCES paper_positions(id),
        mark_price      REAL NOT NULL,
        bid_price       REAL,
        ask_price       REAL,
        source          TEXT NOT NULL,
        observed_at     TEXT NOT NULL,
        unrealized_pnl  REAL NOT NULL,
        is_sample       INTEGER NOT NULL DEFAULT 0
    );""",
    "CREATE INDEX IF NOT EXISTS idx_paper_position_marks_position "
    "ON paper_position_marks(position_id);",

    """CREATE TABLE IF NOT EXISTS paper_position_settlements (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id         TEXT NOT NULL REFERENCES paper_positions(id),
        paper_order_id      TEXT NOT NULL REFERENCES paper_orders(id),
        paper_fill_id       TEXT NOT NULL REFERENCES paper_fills(fill_id),
        market_source_id    TEXT NOT NULL,
        outcome             TEXT NOT NULL,
        resolution_outcome  TEXT NOT NULL,
        is_winner           INTEGER NOT NULL CHECK (is_winner IN (0, 1)),
        payout              REAL NOT NULL,
        realized_pnl        REAL NOT NULL,
        fee                 REAL NOT NULL DEFAULT 0,
        evidence_source     TEXT NOT NULL,
        evidence_hash       TEXT NOT NULL,
        settled_at          TEXT NOT NULL,
        UNIQUE(position_id, evidence_hash)
    );""",
    "CREATE INDEX IF NOT EXISTS idx_paper_position_settlements_position "
    "ON paper_position_settlements(position_id);",
    "CREATE INDEX IF NOT EXISTS idx_paper_position_settlements_status "
    "ON paper_position_settlements(is_winner);",
]

# ── PR #70 (v19) objects ─────────────────────────────────────────────────────
# Inlined verbatim from schema_v19 so a fresh v21 DB is complete. Idempotent.
_V19_DDL: list[str] = [
    """CREATE TABLE IF NOT EXISTS source_trade_enrichments (
        enrichment_id              TEXT PRIMARY KEY,
        source_trade_internal_id   TEXT NOT NULL
                                    REFERENCES source_trades(id),
        status                     TEXT NOT NULL
                                    CHECK (status IN
                                        ('pending','complete','incomplete',
                                         'unavailable','conflict','error')),
        token_id                   TEXT,
        condition_id               TEXT,
        market_id                  TEXT,
        market_slug                TEXT,
        market_title               TEXT,
        outcome_identity           TEXT,
        event_identity             TEXT,
        normalized_category        TEXT,
        taxonomy_status            TEXT,
        market_start_at            TEXT,
        market_end_at              TEXT,
        horizon_status             TEXT,
        market_state               TEXT,
        tradability                TEXT,
        evidence_source            TEXT,
        gamma_source               TEXT,
        clob_source                TEXT,
        evidence_hash              TEXT,
        reason_codes_json          TEXT,
        fetched_at                 TEXT,
        created_at                 TEXT NOT NULL,
        updated_at                 TEXT NOT NULL,
        UNIQUE (source_trade_internal_id)
    )""",
    """CREATE TABLE IF NOT EXISTS approved_specialist_trade_dispatches (
        dispatch_id                TEXT PRIMARY KEY,
        specialist_approval_id     TEXT NOT NULL
                                    REFERENCES specialist_approvals(approval_id),
        source_trade_internal_id   TEXT NOT NULL
                                    REFERENCES source_trades(id),
        wallet                     TEXT NOT NULL,
        category                   TEXT NOT NULL,
        enrichment_id              TEXT REFERENCES source_trade_enrichments(enrichment_id),
        status                     TEXT NOT NULL
                                    CHECK (status IN
                                        ('pending','enrichment_incomplete',
                                         'ready_for_bridge','bridge_complete',
                                         'execution_pending','complete','failed')),
        attempt_count              INTEGER NOT NULL DEFAULT 0,
        last_attempt_at            TEXT,
        candidate_id               INTEGER REFERENCES copy_candidates(id),
        paper_signal_decision_id   INTEGER REFERENCES paper_signal_decisions(id),
        reason_codes_json          TEXT,
        error_message              TEXT,
        created_at                 TEXT NOT NULL,
        updated_at                 TEXT NOT NULL,
        completed_at               TEXT,
        UNIQUE (specialist_approval_id, source_trade_internal_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_source_trade_enrichments_internal "
    "ON source_trade_enrichments(source_trade_internal_id)",
    "CREATE INDEX IF NOT EXISTS idx_source_trade_enrichments_status "
    "ON source_trade_enrichments(status)",
    "CREATE INDEX IF NOT EXISTS idx_astd_approval "
    "ON approved_specialist_trade_dispatches(specialist_approval_id)",
    "CREATE INDEX IF NOT EXISTS idx_astd_source_trade "
    "ON approved_specialist_trade_dispatches(source_trade_internal_id)",
    "CREATE INDEX IF NOT EXISTS idx_astd_status "
    "ON approved_specialist_trade_dispatches(status)",
]

# ── v21 research-plane objects (new) ──────────────────────────────────────────
_V21_NEW_DDL: list[str] = [
    # ── Research-only evidence watchlist ───────────────────────────────────
    # Durable membership granting RESEARCH permission only. No FK references
    # any approval/execution table, so it can never satisfy an execution
    # authorization. One ACTIVE watch per wallet enforced by a partial
    # unique index (paused/retired rows are excluded from the unique slot).
    """CREATE TABLE IF NOT EXISTS specialist_evidence_watchlist (
        id                        TEXT PRIMARY KEY,
        wallet_id                 TEXT NOT NULL REFERENCES wallets(id),
        status                    TEXT NOT NULL
                                      CHECK (status IN ('active','paused','retired')),
        source                    TEXT NOT NULL
                                      CHECK (source IN ('manual','discovery')),
        reason                    TEXT,
        created_by                TEXT,
        created_at                TEXT NOT NULL,
        paused_at                 TEXT,
        retired_at                TEXT,
        max_new_trades_per_run   INTEGER NOT NULL DEFAULT 25,
        last_collection_at        TEXT
    );""",
    "CREATE INDEX IF NOT EXISTS idx_evidence_watchlist_wallet "
    "ON specialist_evidence_watchlist(wallet_id);",
    "CREATE INDEX IF NOT EXISTS idx_evidence_watchlist_status "
    "ON specialist_evidence_watchlist(status);",
    # One ACTIVE watch per wallet (paused/retired excluded from the unique slot).
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_watchlist_active "
    "ON specialist_evidence_watchlist(wallet_id) WHERE status = 'active';",

    # ── Market refresh-state bookkeeping (NOT a resolution authority) ───────
    # Pure scheduling/backoff state for the market-centric resolution
    # refresher. Canonical resolution truth stays on source_trades.
    """CREATE TABLE IF NOT EXISTS specialist_market_refresh_state (
        market_source_id          TEXT PRIMARY KEY,
        last_checked_at          TEXT,
        last_status               TEXT,
        next_check_after          TEXT,
        attempt_count             INTEGER NOT NULL DEFAULT 0,
        last_error                TEXT,
        resolved_at               TEXT
    );""",
    "CREATE INDEX IF NOT EXISTS idx_market_refresh_next "
    "ON specialist_market_refresh_state(next_check_after);",
    "CREATE INDEX IF NOT EXISTS idx_market_refresh_status "
    "ON specialist_market_refresh_state(last_status);",
]

# Ordered DDL for the v20 -> v21 migration (and the self-sufficient fresh build).
_V21_DDL: list[str] = [
    *_V18_DDL,
    *_V19_DDL,
    *_V21_NEW_DDL,
]

# Objects the migration-runner reconciliation short-circuit must require before
# it claims the physical schema is at v21. Without this, a fresh v21 DB
# (whose v13 base already exists) would be declared "current" without
# applying any v18/v19/v21 objects. Discriminating objects: the new research
# tables plus the v18/v19 indexes that only those migrations add.
V21_REQUIRED_TABLES: tuple[str, ...] = (
    "specialist_approvals",
    "paper_signal_execution_authorizations",
    "execution_risk_decisions",
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "paper_position_lots",
    "paper_position_marks",
    "paper_position_settlements",
    "source_trade_enrichments",
    "approved_specialist_trade_dispatches",
    "specialist_evidence_watchlist",
    "specialist_market_refresh_state",
)

V21_REQUIRED_INDEXES: tuple[str, ...] = (
    "ux_specialist_approvals_active",
    "idx_paper_signal_exec_authz_signal",
    "idx_execution_risk_signal",
    "idx_execution_risk_attempt",
    "idx_paper_orders_signal",
    "idx_paper_fills_order",
    "idx_paper_positions_order",
    "idx_paper_position_marks_position",
    "idx_paper_position_settlements_position",
    "idx_source_trade_enrichments_internal",
    "idx_astd_approval",
    "ux_evidence_watchlist_active",
    "idx_market_refresh_next",
)
