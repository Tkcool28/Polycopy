"""Schema version 19 — Specialist approved-trade enrichment + dispatch.

This migration adds the operational path that delivers approved source trades
into the proven Pass 1 execution spine WITHOUT changing any v18 table:

  * ``source_trade_enrichments`` — durable, idempotent authoritative enrichment
    state for one exact source_trade internal UUID. It records the resolved
    normalized evidence (token/condition/market/event identity, normalized
    category, taxonomy status, market timing/state, tradability, evidence
    source + hash, completion status + reason codes). It does NOT duplicate
    canonical source_trade columns that already exist on ``source_trades``;
    those remain the system of record. The enrichment record is the durable
    proof that authoritative evidence was resolved and persisted.
  * ``approved_specialist_trade_dispatches`` — durable dispatch state tying an
    enabled/non-revoked approval to an enriched source trade and (after a
    successful bridge call) the resulting candidate + paper_signal_decision.
    Strict status machine: pending → enrichment_incomplete → ready_for_bridge →
    bridge_complete → execution_pending → complete, plus failed. The dispatcher
    NEVER executes orders or positions; it owns the approval→source→enrich→
    bridge→copy_candidate boundary only.

All DDL is idempotent (CREATE TABLE/INDEX IF NOT EXISTS). The migration runner
applies it in order; the reconciliation short-circuit in database.py is extended
to require these objects before claiming target shape.
"""

from __future__ import annotations

_V19_DDL: list[str] = [
    # ── Durable authoritative source-trade enrichment ─────────────────────
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
    # ── Durable approved-specialist trade dispatch ────────────────────────
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
    # ── Indexes ───────────────────────────────────────────────────────────
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
