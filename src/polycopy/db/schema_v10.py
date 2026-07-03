"""Version 10 schema migration for scoring formulas, paper signals, and depth levels.

Additive schema migration for PR 4:
- wallet_score_decisions
- category_wallet_score_decisions
- trade_copyability_decisions
- v2_shadow_decisions
- decision_verdicts
- paper_signal_decisions
- exit_experiment_registrations
- score_component_inputs (raw and normalized)
- candidate_price_snapshot_levels — normalized order-book depth
"""

from __future__ import annotations

# v9 → v10 schema changes
_V10_DDL = [
    # Wallet score decisions (versioned, immutable)
    """CREATE TABLE IF NOT EXISTS wallet_score_decisions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet_id       TEXT    NOT NULL REFERENCES wallets(id),
        formula_name    TEXT    NOT NULL,
        formula_version TEXT    NOT NULL,
        idempotency_key TEXT    NOT NULL,
        
        -- Raw inputs
        info_score              REAL,
        win_rate                REAL,
        profit_factor           REAL,
        trade_intervals_std     REAL,
        trade_count             INTEGER,
        max_drawdown            REAL,
        sharpe_ratio            REAL,
        sample_fraction         REAL,
        category_trade_count      INTEGER,
        category_distinct_markets  INTEGER,
        overall_trade_count       INTEGER,
        largest_winner_share      REAL,
        top_3_concentration     REAL,
        
        -- Eligibility gates
        resolved_markets        INTEGER,
        active_trading_days       INTEGER,
        distinct_events           INTEGER,
        category_resolved_markets INTEGER,
        category_distinct_events  INTEGER,
        category_active_days      INTEGER,
        
        -- Component scores
        component_scores_json   TEXT,
        
        -- Final score and verdict
        final_score             REAL NOT NULL,
        verdict                 TEXT NOT NULL,
        missing_essentials_json TEXT,
        eligibility_failures_json TEXT,
        
        -- Timestamps
        source_data_timestamp  TEXT,
        computed_at            TEXT NOT NULL,
        created_at             TEXT NOT NULL,
        
        -- Source references
        candidate_id           INTEGER,
        
        UNIQUE(wallet_id, formula_name, formula_version, idempotency_key)
    );""",
    
    # Category wallet score decisions
    """CREATE TABLE IF NOT EXISTS category_wallet_score_decisions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet_id       TEXT    NOT NULL REFERENCES wallets(id),
        category_label  TEXT    NOT NULL,
        formula_name    TEXT    NOT NULL,
        formula_version TEXT    NOT NULL,
        idempotency_key TEXT    NOT NULL,

        -- Raw inputs
        info_score              REAL,
        win_rate                REAL,
        profit_factor           REAL,
        trade_intervals_std     REAL,
        trade_count             INTEGER,
        max_drawdown            REAL,
        sharpe_ratio            REAL,
        sample_fraction         REAL,
        category_trade_count      INTEGER,
        category_distinct_markets  INTEGER,
        overall_trade_count       INTEGER,
        largest_winner_share      REAL,
        top_3_concentration     REAL,

        -- Category gate values (Phase 2 / Chunk 3)
        category_resolved_markets INTEGER,
        category_distinct_events  INTEGER,
        category_active_days      INTEGER,

        -- Component scores
        component_scores_json   TEXT,

        -- Final score and verdict
        final_score             REAL NOT NULL,
        verdict                 TEXT NOT NULL,
        missing_essentials_json TEXT,
        category_gate_failures_json TEXT,

        -- Timestamps
        source_data_timestamp  TEXT,
        computed_at            TEXT NOT NULL,
        created_at             TEXT NOT NULL,

        UNIQUE(wallet_id, category_label, formula_name, formula_version, idempotency_key)
    );""",
    
    # Trade copyability decisions (versioned, immutable)
    """CREATE TABLE IF NOT EXISTS trade_copyability_decisions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet_id       TEXT    NOT NULL REFERENCES wallets(id),
        source_trade_id TEXT    NOT NULL,
        formula_name    TEXT    NOT NULL,
        formula_version TEXT    NOT NULL,
        idempotency_key TEXT    NOT NULL,
        
        -- Raw inputs
        price_deterioration_pct REAL,
        side                TEXT,
        intended_stake      REAL,
        executable_depth    REAL,
        fill_percentage     REAL,
        spread              REAL,
        best_bid_size       REAL,
        best_ask_size       REAL,
        trade_age_seconds   INTEGER,
        seconds_to_market_end INTEGER,
        market_active       INTEGER,
        market_closed       INTEGER,
        market_resolved     INTEGER,
        
        -- Depth-walk details
        depth_walk_json     TEXT,
        insufficient_depth_reason TEXT,
        
        -- Component scores
        component_scores_json   TEXT,
        
        -- Final score and verdict
        final_score             REAL NOT NULL,
        verdict                 TEXT NOT NULL,
        missing_essentials_json TEXT,
        rejection_reasons_json TEXT,
        
        -- Timestamps
        source_data_timestamp  TEXT,
        computed_at            TEXT NOT NULL,
        created_at             TEXT NOT NULL,
        
        -- Source references
        candidate_id           INTEGER REFERENCES copy_candidates(id),
        price_snapshot_id      TEXT REFERENCES candidate_price_snapshots(id),
        
        UNIQUE(source_trade_id, formula_name, formula_version, idempotency_key)
    );""",
    
    # V2 shadow decisions (parallel to v1)
    """CREATE TABLE IF NOT EXISTS shadow_decisions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet_id       TEXT    NOT NULL REFERENCES wallets(id),
        source_trade_id TEXT    NOT NULL,
        formula_name    TEXT    NOT NULL,
        formula_version TEXT    NOT NULL,
        idempotency_key TEXT    NOT NULL,
        
        -- Raw inputs
        delay_seconds           REAL,
        alpha_signal            REAL,
        price_retention_ratio   REAL,
        slippage_pct            REAL,
        fill_percentage         REAL,
        wallet_score            REAL,
        days_since_last_trade   INTEGER,
        copied_trade_pnl        REAL,
        copied_trade_count      INTEGER,
        position_concentration  REAL,
        correlation_score       REAL,
        
        -- Component scores
        component_scores_json   TEXT,
        
        -- Final score and verdict
        final_score             REAL NOT NULL,
        verdict                 TEXT NOT NULL,
        missing_components_json TEXT,
        delay_scenario        TEXT,
        
        -- Timestamps
        source_data_timestamp  TEXT,
        computed_at            TEXT NOT NULL,
        created_at             TEXT NOT NULL,
        
        -- Source references
        candidate_id           INTEGER REFERENCES copy_candidates(id),
        v1_decision_id         INTEGER REFERENCES trade_copyability_decisions(id),
        
        UNIQUE(wallet_id, source_trade_id, formula_name, formula_version, idempotency_key)
    );""",
    
    # Decision verdicts (consolidated view for signal generation)
    """CREATE TABLE IF NOT EXISTS decision_verdicts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet_id       TEXT    NOT NULL REFERENCES wallets(id),
        formula_name    TEXT    NOT NULL,
        formula_version TEXT    NOT NULL,
        verdict         TEXT    NOT NULL,
        verdict_family  TEXT    NOT NULL,  -- copy_candidate, watchlist, skip, incomplete
        score           REAL    NOT NULL,
        computed_at     TEXT    NOT NULL,
        source_ref_type TEXT,  -- candidate_id / source_trade_id
        source_ref_id   TEXT,
        exclusion_reasons_json TEXT,
        
        UNIQUE(wallet_id, formula_name, formula_version, source_ref_id)
    );""",
    
    # Paper signal decisions (unapproved, paper-only)
    """CREATE TABLE IF NOT EXISTS paper_signal_decisions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id    INTEGER NOT NULL REFERENCES copy_candidates(id),
        wallet_id       TEXT    NOT NULL REFERENCES wallets(id),
        signal_family   TEXT    NOT NULL,  -- COPY_CANDIDATE, WATCHLIST, SKIP, INCOMPLETE
        signal_reason   TEXT,
        wallet_score    REAL,
        trade_score     REAL,
        shadow_score    REAL,
        shadow_verdict  TEXT,
        final_verdict   TEXT NOT NULL,
        is_approved     INTEGER NOT NULL DEFAULT 0,  -- Always 0 for PR4
        
        -- Source references
        source_data_timestamp TEXT,
        source_trade_id     TEXT,
        price_snapshot_id   TEXT REFERENCES candidate_price_snapshots(id),
        
        -- Idempotency
        idempotency_key TEXT NOT NULL,
        
        computed_at     TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        
        UNIQUE(candidate_id, idempotency_key)
    );""",
    
    # Exit experiment registrations
    """CREATE TABLE IF NOT EXISTS exit_experiment_registrations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        paper_signal_id INTEGER NOT NULL REFERENCES paper_signal_decisions(id),
        experiment_type TEXT    NOT NULL,  -- hold_to_resolution, exit_24h, exit_72h, move_5pct, move_10pct, move_15pct, thesis_failure
        status          TEXT    NOT NULL DEFAULT 'registered',
        registered_at   TEXT    NOT NULL,
        scheduled_at    TEXT,  -- For timed exits
        
        UNIQUE(paper_signal_id, experiment_type)
    );""",
    
    # Score component inputs log
    """CREATE TABLE IF NOT EXISTS score_component_inputs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_ref_type TEXT NOT NULL,  -- wallet_score, trade_score, shadow_score
        decision_ref_id   INTEGER NOT NULL,
        component_name    TEXT NOT NULL,
        raw_value         REAL,
        normalized_value  REAL,
        weight            REAL,
        quality           TEXT,
        formula           TEXT,
        note              TEXT,
        logged_at         TEXT NOT NULL
    );""",
    
    # ── Candidate price snapshot levels (bounded depth) ────────────────────────
    """CREATE TABLE IF NOT EXISTS candidate_price_snapshot_levels (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_id         TEXT    NOT NULL
                                    REFERENCES candidate_price_snapshots(id),
        side                TEXT    NOT NULL
                                    CHECK (side IN ('BID', 'ASK')),
        level_index         INTEGER NOT NULL,
        price               REAL    NOT NULL
                                    CHECK (price >= 0 AND price <= 1),
        size                REAL    NOT NULL
                                    CHECK (size >= 0),
        cumulative_size     REAL    NOT NULL,
        cumulative_notional REAL    NOT NULL,
        created_at          TEXT    NOT NULL,
        
        UNIQUE(snapshot_id, side, level_index),
        UNIQUE(snapshot_id, side, price)
    );""",
    
    # Indexes for the scoring tables
    "CREATE INDEX IF NOT EXISTS idx_wallet_score_wallet ON wallet_score_decisions(wallet_id);",
    "CREATE INDEX IF NOT EXISTS idx_wallet_score_verdict ON wallet_score_decisions(verdict);",
    "CREATE INDEX IF NOT EXISTS idx_category_score_wallet ON category_wallet_score_decisions(wallet_id);",
    "CREATE INDEX IF NOT EXISTS idx_trade_score_wallet ON trade_copyability_decisions(wallet_id);",
    "CREATE INDEX IF NOT EXISTS idx_trade_score_verdict ON trade_copyability_decisions(verdict);",
    "CREATE INDEX IF NOT EXISTS idx_shadow_decision_wallet ON shadow_decisions(wallet_id);",
    "CREATE INDEX IF NOT EXISTS idx_shadow_decision_verdict ON shadow_decisions(verdict);",
    "CREATE INDEX IF NOT EXISTS idx_paper_signal_candidate ON paper_signal_decisions(candidate_id);",
    "CREATE INDEX IF NOT EXISTS idx_paper_signal_approved ON paper_signal_decisions(is_approved);",
    "CREATE INDEX IF NOT EXISTS idx_paper_signal_wallet ON paper_signal_decisions(wallet_id);",
    "CREATE INDEX IF NOT EXISTS idx_exit_experiment_signal ON exit_experiment_registrations(paper_signal_id);",
    "CREATE INDEX IF NOT EXISTS idx_score_inputs_decision ON score_component_inputs(decision_ref_type, decision_ref_id);",
    # Levels table indexes
    "CREATE INDEX IF NOT EXISTS idx_cpsl_snapshot ON candidate_price_snapshot_levels(snapshot_id);",
    "CREATE INDEX IF NOT EXISTS idx_cpsl_snapshot_side ON candidate_price_snapshot_levels(snapshot_id, side);",
    "CREATE INDEX IF NOT EXISTS idx_cpsl_snapshot_side_level ON candidate_price_snapshot_levels(snapshot_id, side, level_index);",
]