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
#
# Verdict contracts (canonical persisted strings, frozen for PR 4):
#
#   V1 (wallet/category/trade/paper_signal): lowercase
#     - copy_candidate
#     - watchlist
#     - skip
#     - incomplete
#
#   V2 shadow: uppercase
#     - SHADOW_COPY_CANDIDATE
#     - SHADOW_WATCHLIST
#     - SHADOW_SKIP
#     - SHADOW_INCOMPLETE
#
#   Exit tracks (research, paper-only): seven uppercase canonical
#     - HOLD_TO_RESOLUTION
#     - EXIT_24H
#     - EXIT_72H
#     - FAVORABLE_MOVE_005
#     - FAVORABLE_MOVE_010
#     - FAVORABLE_MOVE_015
#     - THESIS_OR_LIQUIDITY_FAILURE
#
#   Delay scenarios (shadow V2): lowercase enum values
#     - theoretical_immediate
#     - delay_30_seconds
#     - delay_2_minutes
#     - delay_5_minutes
#     - delay_15_minutes
#     - actual_measured_delay
#
# CHECK constraints are added to the CREATE TABLE definitions for
# fresh databases. Existing v10/v11 databases cannot receive the
# new CHECKs additively (SQLite ALTER TABLE cannot attach CHECK
# constraints). Application-level validators in
# ``polycopy.scoring.persistence_validation`` mirror every CHECK
# for upgraded DBs. No destructive rebuilds are performed.
_V10_DDL = [
    # Wallet score decisions (versioned, immutable). V1 verdict enum:
    # 'copy_candidate' | 'watchlist' | 'skip' | 'incomplete'.
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
        trade_count             INTEGER  CHECK (trade_count IS NULL OR trade_count >= 0),
        max_drawdown            REAL,
        sharpe_ratio            REAL,
        sample_fraction         REAL,
        category_trade_count      INTEGER  CHECK (category_trade_count IS NULL OR category_trade_count >= 0),
        category_distinct_markets  INTEGER  CHECK (category_distinct_markets IS NULL OR category_distinct_markets >= 0),
        overall_trade_count       INTEGER  CHECK (overall_trade_count IS NULL OR overall_trade_count >= 0),
        largest_winner_share      REAL,
        top_3_concentration     REAL,

        -- Eligibility gates
        resolved_markets        INTEGER  CHECK (resolved_markets IS NULL OR resolved_markets >= 0),
        active_trading_days       INTEGER  CHECK (active_trading_days IS NULL OR active_trading_days >= 0),
        distinct_events           INTEGER  CHECK (distinct_events IS NULL OR distinct_events >= 0),
        category_resolved_markets INTEGER  CHECK (category_resolved_markets IS NULL OR category_resolved_markets >= 0),
        category_distinct_events  INTEGER  CHECK (category_distinct_events IS NULL OR category_distinct_events >= 0),
        category_active_days      INTEGER  CHECK (category_active_days IS NULL OR category_active_days >= 0),

        -- Component scores (stored as JSON)
        component_scores_json   TEXT,

        -- Final score and verdict
        final_score             REAL NOT NULL  CHECK (final_score BETWEEN 0 AND 100),
        verdict                 TEXT NOT NULL  CHECK (verdict IN ('copy_candidate', 'watchlist', 'skip', 'incomplete')),
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
        trade_count             INTEGER  CHECK (trade_count IS NULL OR trade_count >= 0),
        max_drawdown            REAL,
        sharpe_ratio            REAL,
        sample_fraction         REAL,
        category_trade_count      INTEGER  CHECK (category_trade_count IS NULL OR category_trade_count >= 0),
        category_distinct_markets  INTEGER  CHECK (category_distinct_markets IS NULL OR category_distinct_markets >= 0),
        overall_trade_count       INTEGER  CHECK (overall_trade_count IS NULL OR overall_trade_count >= 0),
        largest_winner_share      REAL,
        top_3_concentration     REAL,

        -- Category gate values (Phase 2 / Chunk 3)
        category_resolved_markets INTEGER  CHECK (category_resolved_markets IS NULL OR category_resolved_markets >= 0),
        category_distinct_events  INTEGER  CHECK (category_distinct_events IS NULL OR category_distinct_events >= 0),
        category_active_days      INTEGER  CHECK (category_active_days IS NULL OR category_active_days >= 0),

        -- Component scores (stored as JSON)
        component_scores_json   TEXT,

        -- Final score and verdict
        final_score             REAL NOT NULL  CHECK (final_score BETWEEN 0 AND 100),
        verdict                 TEXT NOT NULL  CHECK (verdict IN ('copy_candidate', 'watchlist', 'skip', 'incomplete')),
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
        intended_stake      REAL    CHECK (intended_stake IS NULL OR intended_stake >= 0),
        executable_depth    REAL    CHECK (executable_depth IS NULL OR executable_depth >= 0),
        fill_percentage     REAL    CHECK (fill_percentage IS NULL OR (fill_percentage >= 0 AND fill_percentage <= 1)),
        spread              REAL    CHECK (spread IS NULL OR spread >= 0),
        best_bid_size       REAL    CHECK (best_bid_size IS NULL OR best_bid_size >= 0),
        best_ask_size       REAL    CHECK (best_ask_size IS NULL OR best_ask_size >= 0),
        trade_age_seconds   INTEGER  CHECK (trade_age_seconds IS NULL OR trade_age_seconds >= 0),
        seconds_to_market_end INTEGER  CHECK (seconds_to_market_end IS NULL OR seconds_to_market_end >= 0),
        market_active       INTEGER  CHECK (market_active IS NULL OR market_active IN (0, 1)),
        market_closed       INTEGER  CHECK (market_closed IS NULL OR market_closed IN (0, 1)),
        market_resolved     INTEGER  CHECK (market_resolved IS NULL OR market_resolved IN (0, 1)),

        -- Depth-walk details
        depth_walk_json     TEXT,
        insufficient_depth_reason TEXT,

        -- Component scores (stored as JSON)
        component_scores_json   TEXT,

        -- Final score and verdict
        final_score             REAL NOT NULL  CHECK (final_score BETWEEN 0 AND 100),
        verdict                 TEXT NOT NULL  CHECK (verdict IN ('copy_candidate', 'watchlist', 'skip', 'incomplete')),
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
        delay_seconds           REAL    CHECK (delay_seconds IS NULL OR delay_seconds >= 0),
        alpha_signal            REAL,
        price_retention_ratio   REAL    CHECK (price_retention_ratio IS NULL OR price_retention_ratio >= 0),
        slippage_pct            REAL,
        slippage                REAL,
        fill_percentage         REAL    CHECK (fill_percentage IS NULL OR (fill_percentage >= 0 AND fill_percentage <= 1)),
        wallet_score            REAL,
        days_since_last_trade   INTEGER  CHECK (days_since_last_trade IS NULL OR days_since_last_trade >= 0),
        copied_trade_pnl        REAL,
        copied_trade_count      INTEGER  CHECK (copied_trade_count IS NULL OR copied_trade_count >= 0),
        position_concentration  REAL,
        correlation_score       REAL,

        -- Component scores (stored as JSON)
        component_scores_json   TEXT,

        -- Final score and verdict
        final_score             REAL NOT NULL  CHECK (final_score BETWEEN 0 AND 100),
        verdict                 TEXT NOT NULL
                                    CHECK (verdict IN ('SHADOW_COPY_CANDIDATE', 'SHADOW_WATCHLIST',
                                                       'SHADOW_SKIP', 'SHADOW_INCOMPLETE')),
        missing_components_json TEXT,
        delay_scenario        TEXT
                                    CHECK (delay_scenario IS NULL OR delay_scenario IN (
                                        'theoretical_immediate',
                                        'delay_30_seconds',
                                        'delay_2_minutes',
                                        'delay_5_minutes',
                                        'delay_15_minutes',
                                        'actual_measured_delay'
                                    )),

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
    
    # Paper signal decisions (unapproved, paper-only). The
# ``signal_family`` and ``final_verdict`` columns store V1 lowercase
# enum values: 'copy_candidate' | 'watchlist' | 'skip' | 'incomplete'.
# ``shadow_verdict`` stores the V2 uppercase SHADOW_* enum.
# ``is_approved`` is enforced to be 0 or 1; the runtime never sets
# it to 1 (PR 4 paper signals are NEVER approved).
    """CREATE TABLE IF NOT EXISTS paper_signal_decisions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id    INTEGER NOT NULL REFERENCES copy_candidates(id),
        wallet_id       TEXT    NOT NULL REFERENCES wallets(id),
        signal_family   TEXT    NOT NULL
                            CHECK (signal_family IN ('copy_candidate', 'watchlist', 'skip', 'incomplete')),
        signal_reason   TEXT,
        wallet_score    REAL    CHECK (wallet_score IS NULL OR wallet_score BETWEEN 0 AND 100),
        trade_score     REAL    CHECK (trade_score IS NULL OR trade_score BETWEEN 0 AND 100),
        shadow_score    REAL    CHECK (shadow_score IS NULL OR shadow_score BETWEEN 0 AND 100),
        shadow_verdict  TEXT
                            CHECK (shadow_verdict IS NULL OR shadow_verdict IN (
                                'SHADOW_COPY_CANDIDATE', 'SHADOW_WATCHLIST',
                                'SHADOW_SKIP', 'SHADOW_INCOMPLETE'
                            )),
        final_verdict   TEXT NOT NULL
                            CHECK (final_verdict IN ('copy_candidate', 'watchlist', 'skip', 'incomplete')),
        is_approved     INTEGER NOT NULL DEFAULT 0
                            CHECK (is_approved IN (0, 1)),

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
    
    # Exit experiment registrations — canonical seven uppercase identifiers
    # from ``ExitTrack`` (see polycopy/scoring/exit_tracks.py). The CHECK
    # below is a defense-in-depth guard; the runtime
    # ``record_exit_experiments`` function only iterates the
    # ``CANONICAL_EXIT_TRACKS`` tuple. Lowercase legacy aliases
    # (``hold_to_resolution``, ``exit_24h``, etc.) are NOT valid and
    # are rejected at the SQL boundary (fresh DBs) and at the
    # application boundary (upgraded DBs).
    """CREATE TABLE IF NOT EXISTS exit_experiment_registrations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        paper_signal_id INTEGER NOT NULL REFERENCES paper_signal_decisions(id),
        experiment_type TEXT    NOT NULL
                                    CHECK (experiment_type IN (
                                        'HOLD_TO_RESOLUTION',
                                        'EXIT_24H',
                                        'EXIT_72H',
                                        'FAVORABLE_MOVE_005',
                                        'FAVORABLE_MOVE_010',
                                        'FAVORABLE_MOVE_015',
                                        'THESIS_OR_LIQUIDITY_FAILURE'
                                    )),
        status          TEXT    NOT NULL DEFAULT 'registered',
        registered_at   TEXT    NOT NULL,
        scheduled_at    TEXT,

        UNIQUE(paper_signal_id, experiment_type)
    );""",

    # Score component inputs log (raw evidence + normalized 0-100 score).
    # ``normalized_value`` is the canonical component score on the
    # [0, 100] scale. ``raw_value`` carries the original raw evidence
    # and is NOT constrained — it may be a count, ratio, JSON-free
    # float, etc. ``weight`` is a non-negative multiplier used by the
    # engine; it is bounded above by the per-decision weights
    # dictionary and not strictly enforced at the SQL layer (legacy
    # audit rows may exist with weight=null). ``quality`` is a free
    # tag string (e.g. 'observed', 'calculated', 'unknown') — not
    # constrained.
    """CREATE TABLE IF NOT EXISTS score_component_inputs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_ref_type TEXT NOT NULL,  -- wallet_score, trade_score, shadow_score
        decision_ref_id   INTEGER NOT NULL,
        component_name    TEXT NOT NULL,
        raw_value         REAL,
        normalized_value  REAL    CHECK (
            normalized_value IS NULL
            OR (normalized_value >= 0 AND normalized_value <= 100)
        ),
        weight            REAL    CHECK (weight IS NULL OR weight >= 0),
        quality           TEXT,
        formula           TEXT,
        note              TEXT,
        logged_at         TEXT NOT NULL
    );""",
    
    # ── Candidate price snapshot levels (bounded depth) ────────────────────────
    # Invariants (verified at persistence time by ``normalize_book_levels``):
    #   - side is always exactly 'BID' or 'ASK' (CHECK).
    #   - level_index starts at 0 and is contiguous per side (CHECK).
    #   - price is on the [0, 1] probability scale (CHECK).
    #   - size is strictly > 0 (CHECK). Zero-sized levels are dropped at
    #     normalize time, so the invariant is never violated by the
    #     runtime persistence path. SQL CHECK enforces it for fresh DBs;
    #     application-level validation enforces it on upgraded DBs.
    #   - cumulative_size / cumulative_notional are non-negative.
    """CREATE TABLE IF NOT EXISTS candidate_price_snapshot_levels (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_id         TEXT    NOT NULL
                                    REFERENCES candidate_price_snapshots(id),
        side                TEXT    NOT NULL
                                    CHECK (side IN ('BID', 'ASK')),
        level_index         INTEGER NOT NULL
                                    CHECK (level_index >= 0),
        price               REAL    NOT NULL
                                    CHECK (price >= 0 AND price <= 1),
        size                REAL    NOT NULL
                                    CHECK (size > 0),
        cumulative_size     REAL    NOT NULL
                                    CHECK (cumulative_size >= 0),
        cumulative_notional REAL    NOT NULL
                                    CHECK (cumulative_notional >= 0),
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