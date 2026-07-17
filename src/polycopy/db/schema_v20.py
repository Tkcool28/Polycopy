"""Schema version 20 — retryable blocked execution (multiple immutable risk attempts).

This migration removes the ``UNIQUE(paper_signal_decision_id)`` constraint from
``execution_risk_decisions`` so that a *blocked* evaluation can be retried
without colliding with the earlier immutable attempt. Retryability is what makes
the fail-closed spine safe to re-run: a kill-switch / stale-snapshot block
writes a durable ``block`` risk decision, and a later re-evaluation (after the
operator clears the condition) writes a *new* immutable attempt rather than
being rejected by a uniqueness violation.

SQLite cannot ``DROP`` a unique constraint inline, so we use the standard
rebuild pattern: create ``execution_risk_decisions_new`` with the new shape,
copy rows preserving every existing column (and the original
``risk_decision_id`` identity), drop the old table, and rename. Existing
``paper_orders`` rows keep their FK into ``execution_risk_decisions(risk_decision_id)``;
that column is preserved verbatim so the FK still resolves after the rebuild.

New shape (post-v20):

  * ``execution_attempt_id TEXT PRIMARY KEY`` — immutable identity for one
    evaluation attempt (always a fresh UUID; for migrated rows it reuses the
    original ``risk_decision_id`` so history is preserved).
  * ``authorization_id TEXT REFERENCES
    paper_signal_execution_authorizations(authorization_id)`` — nullable; the
    authorization this attempt was evaluated under (NULL when no active auth
    existed, e.g. a blocked evaluation).
  * ``attempt_number INTEGER NOT NULL DEFAULT 1`` — 1-based sequence of
    attempts for a given ``paper_signal_decision_id``.

``paper_signal_decision_id`` becomes a plain (non-unique) column. ``paper_orders``
KEEPS its ``UNIQUE(paper_signal_decision_id)`` — exactly-once order creation is
unchanged and is what actually prevents a second order for the same signal.

All DDL is idempotent in effect: ``CREATE TABLE IF NOT EXISTS`` for the temp
table, ``CREATE INDEX IF NOT EXISTS`` for the new indexes, and the
INSERT...SELECT / DROP / RENAME are guarded by the migration runner (a DB
already at v20 has no ``execution_risk_decisions`` table to rebuild, because the
reconciliation short-circuit returns before replaying).
"""

from __future__ import annotations

_V20_DDL: list[str] = [
    # ── Rebuild execution_risk_decisions without UNIQUE(paper_signal_decision_id) ──
    """CREATE TABLE IF NOT EXISTS execution_risk_decisions_new (
        risk_decision_id          TEXT PRIMARY KEY,
        execution_attempt_id      TEXT UNIQUE NOT NULL,
        paper_signal_decision_id   INTEGER NOT NULL
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
        reason_codes               TEXT,
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
    # Copy existing rows; preserve the original risk_decision_id as both the
    # new PK (execution_attempt_id) and the legacy risk_decision_id column so
    # paper_orders' FK keeps resolving. authorization_id is unknown for legacy
    # rows → NULL. attempt_number defaults to 1 for all migrated history.
    """INSERT INTO execution_risk_decisions_new (
           execution_attempt_id, risk_decision_id, paper_signal_decision_id,
           specialist_approval_id, authorization_id, source_trade_id,
           candidate_id, snapshot_id, decision, reason_codes,
           requested_quantity, requested_price, estimated_fill_price,
           estimated_slippage, market_exposure_before, wallet_exposure_before,
           portfolio_exposure_before, configured_limits_json, kill_switch_state,
           paper_mode, evidence_timestamp, evaluated_at, policy_version,
           attempt_number)
       SELECT risk_decision_id, risk_decision_id, paper_signal_decision_id,
           specialist_approval_id, NULL, source_trade_id, candidate_id,
           snapshot_id, decision, reason_codes, requested_quantity,
           requested_price, estimated_fill_price, estimated_slippage,
           market_exposure_before, wallet_exposure_before,
           portfolio_exposure_before, configured_limits_json, kill_switch_state,
           paper_mode, evidence_timestamp, evaluated_at, policy_version, 1
       FROM execution_risk_decisions;""",
    "DROP TABLE execution_risk_decisions;",
    "ALTER TABLE execution_risk_decisions_new RENAME TO execution_risk_decisions;",
    # ── Indexes ───────────────────────────────────────────────────────────────
    "CREATE INDEX IF NOT EXISTS idx_execution_risk_signal "
    "ON execution_risk_decisions(paper_signal_decision_id);",
    "CREATE INDEX IF NOT EXISTS idx_execution_risk_authz "
    "ON execution_risk_decisions(authorization_id);",
    "CREATE INDEX IF NOT EXISTS idx_execution_risk_attempt "
    "ON execution_risk_decisions(paper_signal_decision_id, attempt_number);",
]

# Objects the migration-runner reconciliation short-circuit must require before
# it claims the physical schema is at v20. The rebuilt table shares its name
# with v18/v19, so the discriminating objects are the new indexes.
V20_REQUIRED_TABLES: tuple[str, ...] = ()

V20_REQUIRED_INDEXES: tuple[str, ...] = (
    "idx_execution_risk_authz",
    "idx_execution_risk_attempt",
)
