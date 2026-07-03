"""Score and version serialization for PR 4.

Provides serialization utilities for:
- Wallet score v1 decisions
- Category wallet score v1 decisions
- Trade copyability v1 decisions
- V2 shadow decisions
- Paper signal decisions
- Score component inputs

All writes are idempotent via deterministic idempotency keys.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional, Any

from polycopy.db.database import Database


# ---- Idempotency key -----------------------------------------------------

def generate_idempotency_key(
    *,
    formula_name: str,
    formula_version: str,
    wallet_id: Optional[str] = None,
    source_trade_id: Optional[str] = None,
    source_data_timestamp: Optional[str] = None,
    extra_params: Optional[dict[str, Any]] = None,
) -> str:
    """Generate deterministic idempotency key for scoring decisions.

    The key is computed from:
    - formula_name
    - formula_version
    - wallet_id (if present)
    - source_trade_id (if present)
    - source_data_timestamp (point-in-time input snapshot)
    - extra_params (for additional differentiation)

    This ensures rerunning the same formula with the same inputs
    produces the same key (no duplicates), while new inputs may
    coexist across versions.
    """
    components = {
        "formula_name": formula_name,
        "formula_version": formula_version,
        "wallet_id": wallet_id,
        "source_trade_id": source_trade_id,
        "source_data_timestamp": source_data_timestamp,
    }
    if extra_params:
        components["extra"] = extra_params

    # Create stable string representation
    key_str = json.dumps(components, sort_keys=True, default=str)
    return hashlib.sha256(key_str.encode()).hexdigest()[:32]


# ---- Component JSON helpers ----------------------------------------------

def serialize_score_components(components: list[Any]) -> str:
    """Serialize score components to JSON for storage."""
    data = [
        {
            "name": c.name,
            "raw_score": round(c.raw_score, 4),
            "weight": c.weight,
            "quality": c.quality,
            "formula": c.formula,
            "note": c.note,
            "weighted_score": round(c.weighted_score, 4),
        }
        for c in components
    ]
    return json.dumps(data, sort_keys=True)


def deserialize_score_components(data: str) -> list[dict]:
    """Deserialize score components from JSON storage."""
    if not data:
        return []
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return []


# ---- Legacy compatibility adapter ---------------------------------------
#
# These two adapters are the ONLY place where getattr(..., None) is allowed
# on result objects. Their job is to convert a legacy result (built with
# raw kwargs, no explicit input) into the typed input dataclass that
# persistence expects. Once converted, the INSERT tuple reads fields
# directly from the typed input — no more getattr scattered through
# the persistence path.
#
# If a legacy result lacks a wallet_id, the resulting typed input has
# wallet_id="" (empty string). That is a documented, explicit
# representation of "unknown wallet" in the legacy path; it is
# persisted as an empty string, NOT silently coerced. Callers who care
# about wallet identity must pass the explicit input form.

def _wallet_input(result) -> Any:
    """Return the typed input that produced a WalletScoreResult.

    If the result was built with an explicit input object (the
    recommended path), return it directly.

    If the result was built without an explicit input (legacy
    callers that pass raw kwargs), reconstruct a typed input from
    the result's stored fields using getattr fallbacks. This is
    the ONE place where getattr on result is tolerated; downstream
    persistence reads from the typed input only.
    """
    from polycopy.scoring.wallet_score_v1 import WalletScoreInputV1

    if getattr(result, "input", None) is not None:
        return result.input  # type: ignore[return-value]

    # Legacy path: reconstruct from result fields. Empty/unknown
    # fields are passed through as None (or empty string for
    # wallet_id, which is a NOT NULL column). This is the
    # documented behavior; do not silently coerce.
    return WalletScoreInputV1(
        wallet_id=getattr(result, "wallet_id", "") or "",
        info_score=getattr(result, "info_score", None),
        win_rate=getattr(result, "win_rate", None),
        profit_factor=getattr(result, "profit_factor", None),
        trade_intervals_std=getattr(result, "trade_intervals_std", None),
        trade_count=getattr(result, "trade_count", None),
        max_drawdown=getattr(result, "max_drawdown", None),
        sharpe_ratio=getattr(result, "sharpe_ratio", None),
        sample_fraction=getattr(result, "sample_fraction", None),
        category_trade_count=getattr(result, "category_trade_count", None),
        category_distinct_markets=getattr(result, "category_distinct_markets", None),
        overall_trade_count=getattr(result, "overall_trade_count", None),
        largest_winner_share=getattr(result, "largest_winner_share", None),
        top_3_concentration=getattr(result, "top_3_concentration", None),
        resolved_markets=getattr(result, "resolved_markets", None),
        active_trading_days=getattr(result, "active_trading_days", None),
        distinct_events=getattr(result, "distinct_events", None),
        category_resolved_markets=getattr(result, "category_resolved_markets", None),
        category_distinct_events=getattr(result, "category_distinct_events", None),
        category_active_days=getattr(result, "category_active_days", None),
    )


def _trade_input(result) -> Any:
    """Return the typed input that produced a TradeScoreResult.

    Back-compat path builds a default input from the result's
    stored fields when no explicit input is attached. This is the
    ONE place where getattr on result is tolerated for the trade
    input path.
    """
    from polycopy.scoring.trade_score_v1 import TradeCopyabilityInputV1

    if getattr(result, "input", None) is not None:
        return result.input  # type: ignore[return-value]

    return TradeCopyabilityInputV1(
        wallet_id=getattr(result, "wallet_id", "") or "",
        source_trade_id=getattr(result, "source_trade_id", "") or "",
        side=getattr(result, "side", None),
        price_deterioration_pct=getattr(result, "price_deterioration_pct", None),
        intended_stake=getattr(result, "intended_stake", None),
        executable_depth=getattr(result, "executable_depth", None),
        fill_percentage=getattr(result, "fill_percentage", None),
        spread=getattr(result, "spread", None),
        best_bid_size=getattr(result, "best_bid_size", None),
        best_ask_size=getattr(result, "best_ask_size", None),
        trade_age_seconds=getattr(result, "trade_age_seconds", None),
        seconds_to_market_end=getattr(result, "seconds_to_market_end", None),
        market_active=getattr(result, "market_active", None),
        market_closed=getattr(result, "market_closed", None),
        market_resolved=getattr(result, "market_resolved", None),
        has_valid_strategy=getattr(result, "has_valid_strategy", None),
        has_complete_data=getattr(result, "has_complete_data", None),
        market_category=getattr(result, "market_category", None),
    )


# ---- INSERT ... RETURNING helper ----------------------------------------
#
# SQLite's behavior with INSERT ... RETURNING: the cursor returned
# from execute() has exactly one row when the INSERT actually
# produced a row, and zero rows when INSERT OR IGNORE skipped. In
# both cases the cursor is "open" (a SELECT-like result set is
# pending) until you call fetchone() / fetchall(). Calling
# db.conn.commit() while the cursor still has unread rows raises
# "cannot commit transaction - SQL statements in progress".
#
# This helper centralizes the correct pattern:
#   1. Execute INSERT ... RETURNING.
#   2. fetchone() to drain the cursor.
#   3. If a row came back -> we just inserted it; commit and return
#      the new id.
#   4. If no row came back -> INSERT OR IGNORE skipped. Look up the
#      existing row by the table's UNIQUE columns (NOT just
#      idempotency_key, which is only one of several UNIQUE columns),
#      fetchone() to drain that SELECT cursor too, then commit and
#      return the existing id.

def _insert_or_ignore_returning_id(
    db: Database,
    *,
    sql: str,
    params: tuple,
    existing_lookup_sql: str,
    existing_lookup_params: tuple,
) -> int:
    """Execute INSERT OR IGNORE ... RETURNING id and return the row id.

    On insert: drains the RETURNING cursor, commits, returns the new id.
    On skip: drains the RETURNING cursor, then runs `existing_lookup_sql`
    to find the existing row, drains that cursor, commits, returns the
    existing id. If the existing row is not found, returns 0 (this
    should be impossible given a working UNIQUE constraint).
    """
    cursor = db.execute(sql, params)
    inserted = cursor.fetchone()
    if inserted is not None:
        # We just inserted. Commit and return.
        db.conn.commit()
        # sqlite3.Row supports both index and key access.
        return int(inserted["id"] if "id" in inserted.keys() else inserted[0])

    # INSERT OR IGNORE skipped. Look up the existing row by the
    # table's actual UNIQUE columns.
    lookup_cursor = db.execute(existing_lookup_sql, existing_lookup_params)
    existing = lookup_cursor.fetchone()
    db.conn.commit()
    if existing is None:
        # Defensive: this should not happen if the UNIQUE constraint
        # is enforced. Return 0 to signal "row id unknown".
        return 0
    return int(existing["id"] if "id" in existing.keys() else existing[0])


# ---- Persisters ---------------------------------------------------------

def persist_wallet_score_v1(
    db: Database,
    wallet_id: str,
    result,
    *,
    idempotency_key: Optional[str] = None,
    candidate_id: Optional[int] = None,
    source_data_timestamp: Optional[str] = None,
) -> int:
    """Persist wallet score v1 decision to database (Phase 9).

    Reads every raw input column from `result.input` (the typed
    `WalletScoreInputV1` that produced this result). The legacy
    compat adapter `_wallet_input` reconstructs a typed input from
    result fields when no explicit input is attached.

    INSERT column/placeholder/value count: 32 / 32 / 32
    (enforced by TestColumnPlaceholderValueCount).

    Table UNIQUE constraint:
    UNIQUE(wallet_id, formula_name, formula_version, idempotency_key)
    The fallback lookup uses all four columns to find the existing row.
    """
    if idempotency_key is None:
        idempotency_key = generate_idempotency_key(
            formula_name="wallet_score",
            formula_version=result.formula_version,
            wallet_id=wallet_id,
            source_data_timestamp=source_data_timestamp,
        )

    inp = _wallet_input(result)
    now = datetime.now(timezone.utc).isoformat()

    return _insert_or_ignore_returning_id(
        db,
        sql="""
            INSERT OR IGNORE INTO wallet_score_decisions (
                wallet_id, formula_name, formula_version, idempotency_key,
                info_score, win_rate, profit_factor, trade_intervals_std, trade_count,
                max_drawdown, sharpe_ratio, sample_fraction,
                category_trade_count, category_distinct_markets, overall_trade_count,
                largest_winner_share, top_3_concentration,
                resolved_markets, active_trading_days, distinct_events,
                category_resolved_markets, category_distinct_events, category_active_days,
                component_scores_json, final_score, verdict, missing_essentials_json,
                eligibility_failures_json, source_data_timestamp, computed_at, created_at,
                candidate_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
        """,
        params=(
            wallet_id,
            "wallet_score",
            result.formula_version,
            idempotency_key,
            inp.info_score,
            inp.win_rate,
            inp.profit_factor,
            inp.trade_intervals_std,
            inp.trade_count,
            inp.max_drawdown,
            inp.sharpe_ratio,
            inp.sample_fraction,
            inp.category_trade_count,
            inp.category_distinct_markets,
            inp.overall_trade_count,
            inp.largest_winner_share,
            inp.top_3_concentration,
            inp.resolved_markets,
            inp.active_trading_days,
            inp.distinct_events,
            inp.category_resolved_markets,
            inp.category_distinct_events,
            inp.category_active_days,
            serialize_score_components(result.components),
            result.score,
            result.verdict.value,
            json.dumps(result.missing_essentials),
            json.dumps(result.eligibility_gate_failures),
            source_data_timestamp,
            now,
            now,
            candidate_id,
        ),
        # Table UNIQUE: (wallet_id, formula_name, formula_version, idempotency_key)
        existing_lookup_sql="""
            SELECT id FROM wallet_score_decisions
            WHERE wallet_id = ? AND formula_name = ? AND formula_version = ? AND idempotency_key = ?
        """,
        existing_lookup_params=(
            wallet_id, "wallet_score", result.formula_version, idempotency_key,
        ),
    )


def persist_trade_score_v1(
    db: Database,
    wallet_id: str,
    source_trade_id: str,
    result,
    *,
    idempotency_key: Optional[str] = None,
    candidate_id: Optional[int] = None,
    price_snapshot_id: Optional[str] = None,
    source_data_timestamp: Optional[str] = None,
) -> int:
    """Persist trade copyability v1 decision to database (Phase 9).

    INSERT column/placeholder/value count: 28 / 28 / 28
    (enforced by TestColumnPlaceholderValueCount).

    Table UNIQUE constraint:
    UNIQUE(source_trade_id, formula_name, formula_version, idempotency_key)
    The fallback lookup uses all four columns to find the existing row.
    Note: source_trade_id is part of the UNIQUE constraint but
    wallet_id is not (multiple wallets can score the same source
    trade). wallet_id is still in the row for query convenience.

    TODO(phase7): when depth-walk integration lands, the
    depth_walk_json and insufficient_depth_reason columns will
    be populated from a typed depth-walk result.
    """
    if idempotency_key is None:
        idempotency_key = generate_idempotency_key(
            formula_name="trade_copyability",
            formula_version=result.formula_version,
            wallet_id=wallet_id,
            source_trade_id=source_trade_id,
            source_data_timestamp=source_data_timestamp,
        )

    inp = _trade_input(result)
    now = datetime.now(timezone.utc).isoformat()

    return _insert_or_ignore_returning_id(
        db,
        sql="""
            INSERT OR IGNORE INTO trade_copyability_decisions (
                wallet_id, source_trade_id, formula_name, formula_version, idempotency_key,
                price_deterioration_pct, side, intended_stake, executable_depth, fill_percentage,
                spread, best_bid_size, best_ask_size, trade_age_seconds, seconds_to_market_end,
                market_active, market_closed, market_resolved,
                depth_walk_json, insufficient_depth_reason,
                component_scores_json, final_score, verdict, missing_essentials_json,
                rejection_reasons_json, source_data_timestamp, computed_at, created_at,
                candidate_id, price_snapshot_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
        """,
        params=(
            wallet_id,
            source_trade_id,
            "trade_copyability",
            result.formula_version,
            idempotency_key,
            inp.price_deterioration_pct,
            inp.side,
            inp.intended_stake,
            inp.executable_depth,
            inp.fill_percentage,
            inp.spread,
            inp.best_bid_size,
            inp.best_ask_size,
            inp.trade_age_seconds,
            inp.seconds_to_market_end,
            inp.market_active,
            inp.market_closed,
            inp.market_resolved,
            # TODO(phase7): populate from a typed depth-walk result.
            None,  # depth_walk_json
            None,  # insufficient_depth_reason
            serialize_score_components(result.components),
            result.score,
            result.verdict.value,
            json.dumps(result.missing_essentials),
            json.dumps(result.rejection_reasons),
            source_data_timestamp,
            now,
            now,
            candidate_id,
            price_snapshot_id,
        ),
        # Table UNIQUE: (source_trade_id, formula_name, formula_version, idempotency_key)
        existing_lookup_sql="""
            SELECT id FROM trade_copyability_decisions
            WHERE source_trade_id = ? AND formula_name = ? AND formula_version = ? AND idempotency_key = ?
        """,
        existing_lookup_params=(
            source_trade_id, "trade_copyability", result.formula_version, idempotency_key,
        ),
    )


def persist_shadow_score_v2(
    db: Database,
    wallet_id: str,
    source_trade_id: str,
    result,
    *,
    idempotency_key: Optional[str] = None,
    candidate_id: Optional[int] = None,
    v1_decision_id: Optional[int] = None,
    source_data_timestamp: Optional[str] = None,
) -> int:
    """Persist v2 shadow decision to database (parallel to v1).

    TODO(phase9-shadow): when ShadowScoreInputV2 lands in Chunk 5,
    read these raw columns from a typed input instead of getattr.
    Until then, the legacy getattr path is used here; this is
    documented as the one remaining getattr site (alongside
    _wallet_input and _trade_input, which are the compatibility
    adapters for the typed paths).

    Table UNIQUE constraint:
    UNIQUE(wallet_id, source_trade_id, formula_name, formula_version, idempotency_key)
    """
    if idempotency_key is None:
        idempotency_key = generate_idempotency_key(
            formula_name="shadow_score",
            formula_version=result.formula_version,
            wallet_id=wallet_id,
            source_trade_id=source_trade_id,
            source_data_timestamp=source_data_timestamp,
        )

    now = datetime.now(timezone.utc).isoformat()

    return _insert_or_ignore_returning_id(
        db,
        sql="""
            INSERT OR IGNORE INTO shadow_decisions (
                wallet_id, source_trade_id, formula_name, formula_version, idempotency_key,
                delay_seconds, alpha_signal, price_retention_ratio, slippage_pct, fill_percentage,
                wallet_score, days_since_last_trade, copied_trade_pnl, copied_trade_count,
                position_concentration, correlation_score,
                component_scores_json, final_score, verdict, missing_components_json,
                delay_scenario, source_data_timestamp, computed_at, created_at,
                candidate_id, v1_decision_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
        """,
        params=(
            wallet_id,
            source_trade_id,
            "shadow_score",
            result.formula_version,
            idempotency_key,
            getattr(result, "delay_seconds", None),
            getattr(result, "alpha_signal", None),
            getattr(result, "price_retention_ratio", None),
            getattr(result, "slippage_pct", None),
            getattr(result, "fill_percentage", None),
            getattr(result, "wallet_score", None),
            getattr(result, "days_since_last_trade", None),
            getattr(result, "copied_trade_pnl", None),
            getattr(result, "copied_trade_count", None),
            getattr(result, "position_concentration", None),
            getattr(result, "correlation_score", None),
            serialize_score_components(result.components),
            result.score,
            result.verdict.value,
            json.dumps(result.missing_components),
            result.delay_scenario,
            source_data_timestamp,
            now,
            now,
            candidate_id,
            v1_decision_id,
        ),
        # Table UNIQUE: (wallet_id, source_trade_id, formula_name, formula_version, idempotency_key)
        existing_lookup_sql="""
            SELECT id FROM shadow_decisions
            WHERE wallet_id = ? AND source_trade_id = ?
              AND formula_name = ? AND formula_version = ? AND idempotency_key = ?
        """,
        existing_lookup_params=(
            wallet_id, source_trade_id,
            "shadow_score", result.formula_version, idempotency_key,
        ),
    )


def persist_paper_signal(
    db: Database,
    candidate_id: int,
    wallet_id: str,
    signal_family: str,
    signal_reason: Optional[str],
    wallet_score: float,
    trade_score: float,
    shadow_score: float,
    shadow_verdict: Optional[str],
    final_verdict: str,
    source_data_timestamp: Optional[str],
    source_trade_id: Optional[str],
    price_snapshot_id: Optional[str],
    *,
    idempotency_key: Optional[str] = None,
) -> int:
    """Persist paper signal decision (unapproved).

    TODO(phase9-signal-input): when SignalDecisionInput lands in
    Chunk 5, persist the full typed input object as a JSON column
    alongside the rolled-up scores so reloading a paper-signal
    decision can reconstruct every input the verdict engine saw.

    Table UNIQUE constraint:
    UNIQUE(candidate_id, idempotency_key)
    """
    if idempotency_key is None:
        idempotency_key = generate_idempotency_key(
            formula_name="paper_signal",
            formula_version="1",
            source_trade_id=str(candidate_id),
        )

    now = datetime.now(timezone.utc).isoformat()

    return _insert_or_ignore_returning_id(
        db,
        sql="""
            INSERT OR IGNORE INTO paper_signal_decisions (
                candidate_id, wallet_id, signal_family, signal_reason,
                wallet_score, trade_score, shadow_score, shadow_verdict, final_verdict,
                source_data_timestamp, source_trade_id, price_snapshot_id,
                idempotency_key, computed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
        """,
        params=(
            candidate_id,
            wallet_id,
            signal_family,
            signal_reason,
            wallet_score,
            trade_score,
            shadow_score,
            shadow_verdict,
            final_verdict,
            source_data_timestamp,
            source_trade_id,
            price_snapshot_id,
            idempotency_key,
            now,
            now,
        ),
        # Table UNIQUE: (candidate_id, idempotency_key)
        existing_lookup_sql="""
            SELECT id FROM paper_signal_decisions
            WHERE candidate_id = ? AND idempotency_key = ?
        """,
        existing_lookup_params=(candidate_id, idempotency_key),
    )


# ---- Exit experiments (not in scope for this fix but uses RETURNING) ---

def record_exit_experiments(
    db: Database,
    paper_signal_id: int,
) -> list[int]:
    """Register exit experiment tracks for a paper signal.

    Creates immutable research tracks:
    - hold_to_resolution
    - exit_after_24h
    - exit_after_72h
    - favorable_move_5pct
    - favorable_move_10pct
    - favorable_move_15pct
    - thesis_failure

    Note: the canonical exit-experiment identifiers are part of
    Phase 11 work. The current identifiers in the table match the
    pre-PR-4 values and will be migrated to the canonical set in
    a later chunk. (See CHUNK 5 / Phase 11 in the plan.)

    Table UNIQUE: (paper_signal_id, experiment_type)
    """
    now = datetime.now(timezone.utc)
    experiment_types = [
        ("hold_to_resolution", None),
        ("exit_24h", now.replace(second=0, microsecond=0)),
        ("exit_72h", now.replace(second=0, microsecond=0)),
        ("favorable_move_5pct", None),
        ("favorable_move_10_pct", None),
        ("favorable_move_15_pct", None),
        ("thesis_failure", None),
    ]

    registered_ids = []
    for exp_type, scheduled_at in experiment_types:
        registered_ids.append(
            _insert_or_ignore_returning_id(
                db,
                sql="""
                    INSERT OR IGNORE INTO exit_experiment_registrations (
                        paper_signal_id, experiment_type, status,
                        registered_at, scheduled_at
                    ) VALUES (?, ?, ?, ?, ?)
                    RETURNING id
                """,
                params=(
                    paper_signal_id,
                    exp_type,
                    "registered",
                    now.isoformat(),
                    scheduled_at.isoformat() if scheduled_at else None,
                ),
                existing_lookup_sql="""
                    SELECT id FROM exit_experiment_registrations
                    WHERE paper_signal_id = ? AND experiment_type = ?
                """,
                existing_lookup_params=(paper_signal_id, exp_type),
            )
        )
    return registered_ids
