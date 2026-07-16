"""Schema version 18 — Specialist paper execution spine.

This migration adds the missing operational path so one eligible trade from one
manually approved specialist wallet can become a safe, persistent, fully
traceable paper position and later be marked and settled.

All new tables carry full provenance back to the originating approval, source
trade, candidate, snapshot, decisions, order, and fill. The canonical writer
for each table is the new specialist-execution spine (see
``polycopy.Execution.specialist_execution``). No existing table is altered
destructively; the v17 schema is preserved for backward compatibility (existing
``orders``/``positions`` rows keep NULL provenance columns).

Design notes:
  * ``specialist_approvals`` — durable manual approval record replacing the
    single-address ``.env`` implicit model. A partial unique index enforces
    "at most one ACTIVE approval per (wallet_address, specialist_category,
    formula_version)" — active meaning enabled=1 AND revoked_at IS NULL.
  * ``paper_signal_execution_authorizations`` — the explicit manual
    execution-approval gate for an eligible paper signal. This is the durable,
    tested replacement for silently flipping ``paper_signal_decisions.is_approved``
    (which the PR4 serializer forces to 0 for safety).
  * ``execution_risk_decisions`` — immutable risk-decision evidence; a BLOCKED
    decision creates no order.
  * ``paper_orders`` / ``paper_fills`` / ``paper_positions`` — provenance-tracked
    execution lifecycle. ``paper_orders`` carries UNIQUE(paper_signal_decision_id)
    so exactly one order is ever produced per eligible signal (the exactly-once
    durable idempotency key).
  * ``paper_position_marks`` / ``paper_position_settlements`` — marking and
    settlement evidence, linked to the paper position/order/fill.

All DDL is idempotent (CREATE TABLE/INDEX IF NOT EXISTS). The migration runner
applies it in order; the reconciliation short-circuit in database.py is extended
to require these objects before claiming target shape.
"""

from __future__ import annotations

_V18_DDL: list[str] = [
    # ── Durable manual specialist-wallet approval ─────────────────────────
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
    # Canonical lookup by wallet (collector + monitor read the same source).
    "CREATE INDEX IF NOT EXISTS idx_specialist_approvals_wallet "
    "ON specialist_approvals(wallet_address);",
    # Canonical lookup by (wallet, category, version).
    "CREATE INDEX IF NOT EXISTS idx_specialist_approvals_wallet_category "
    "ON specialist_approvals(wallet_address, specialist_category, formula_version);",
    # Durable "at most one ACTIVE approval per (wallet, category, version)".
    # Active = enabled=1 AND revoked_at IS NULL. Revoked/disabled historical
    # rows are preserved (auditable) and do NOT collide with this index.
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_specialist_approvals_active "
    "ON specialist_approvals(wallet_address, specialist_category, formula_version) "
    "WHERE enabled = 1 AND revoked_at IS NULL;",

    # ── Manual execution-authorization gate for an eligible signal ─────────
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

    # ── Immutable execution-risk decision ─────────────────────────────────
    """CREATE TABLE IF NOT EXISTS execution_risk_decisions (
        risk_decision_id         TEXT PRIMARY KEY,
        paper_signal_decision_id INTEGER NOT NULL
                                    REFERENCES paper_signal_decisions(id),
        specialist_approval_id   TEXT
                                    REFERENCES specialist_approvals(approval_id),
        source_trade_id          TEXT NOT NULL,
        candidate_id             INTEGER NOT NULL REFERENCES copy_candidates(id),
        snapshot_id              TEXT REFERENCES candidate_price_snapshots(id),
        decision                 TEXT NOT NULL CHECK (decision IN ('allow', 'block', 'no_op_already_executed', 'dry_run')),
        reason_codes             TEXT,
        requested_quantity       REAL,
        requested_price          REAL,
        estimated_fill_price     REAL,
        estimated_slippage       REAL,
        market_exposure_before   REAL NOT NULL DEFAULT 0,
        wallet_exposure_before   REAL NOT NULL DEFAULT 0,
        portfolio_exposure_before REAL NOT NULL DEFAULT 0,
        configured_limits_json   TEXT,
        kill_switch_state        INTEGER NOT NULL,
        paper_mode               TEXT NOT NULL,
        evidence_timestamp       TEXT,
        evaluated_at             TEXT NOT NULL,
        policy_version           TEXT NOT NULL,
        UNIQUE(paper_signal_decision_id)
    );""",
    "CREATE INDEX IF NOT EXISTS idx_execution_risk_signal "
    "ON execution_risk_decisions(paper_signal_decision_id);",
    "CREATE INDEX IF NOT EXISTS idx_execution_risk_approval "
    "ON execution_risk_decisions(specialist_approval_id);",

    # ── Provenance-tracked paper order (exactly one per eligible signal) ───
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

    # ── Durable simulated fill (distinct from the in-memory broker) ────────
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

    # ── Provenance-tracked paper position (one per order, first milestone) ─
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

    # ── Position lots (one entry per fill, provenance to the fill) ──────────
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

    # ── Marking evidence (linked to the paper position) ───────────────────
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

    # ── Settlement evidence (exactly once per position+evidence) ───────────
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


# Objects the migration-runner reconciliation short-circuit must require before
# it claims the physical schema is at v18 (otherwise v18 would be skipped and
# _meta bumped without creating these tables).
V18_REQUIRED_TABLES: tuple[str, ...] = (
    "specialist_approvals",
    "paper_signal_execution_authorizations",
    "execution_risk_decisions",
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "paper_position_marks",
    "paper_position_settlements",
)

V18_REQUIRED_INDEXES: tuple[str, ...] = (
    "ux_specialist_approvals_active",
    "idx_paper_signal_exec_authz_signal",
    "idx_execution_risk_signal",
    "idx_paper_orders_signal",
    "idx_paper_fills_order",
    "idx_paper_positions_order",
    "idx_paper_position_marks_position",
    "idx_paper_position_settlements_position",
)
