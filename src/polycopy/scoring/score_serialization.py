"""Score and version serialization for PR 4.

Provides serialization utilities for:
- Wallet score v1 decisions
- Category wallet score v1 decisions  
- Trade copyability v1 decisions
- V2 shadow decisions
- Score component inputs

All writes are idempotent via deterministic idempotency keys.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional, Any

from polycopy.db.database import Database


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


def persist_wallet_score_v1(
    db: Database,
    wallet_id: str,
    result,
    *,
    idempotency_key: Optional[str] = None,
    candidate_id: Optional[int] = None,
    source_data_timestamp: Optional[str] = None,
) -> int:
    """Persist wallet score v1 decision to database.

    Returns the rowid of the inserted or ignored row.
    INSERT OR IGNORE on the idempotency key ensures no duplicates.
    """
    if idempotency_key is None:
        idempotency_key = generate_idempotency_key(
            formula_name="wallet_score",
            formula_version=result.formula_version,
            wallet_id=wallet_id,
            source_data_timestamp=source_data_timestamp,
        )

    now = datetime.now(timezone.utc).isoformat()

    row = db.execute(
        """INSERT OR IGNORE INTO wallet_score_decisions (
            wallet_id, formula_name, formula_version, idempotency_key,
            info_score, win_rate, profit_factor, trade_intervals_std, trade_count,
            max_drawdown, sharpe_ratio, sample_fraction,
            category_trade_count, category_distinct_markets, overall_trade_count,
            largest_winner_share, top_3_concentration,
            resolved_markets, active_trading_days, distinct_events,
            category_resolved_markets, category_distinct_events, category_active_days,
            component_scores_json, final_score, verdict, missing_essentials_json,
            eligibility_gate_failures_json, source_data_timestamp, computed_at, created_at,
            candidate_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id""",
        (
            wallet_id,
            "wallet_score",
            result.formula_version,
            idempotency_key,
            result.info_score if hasattr(result, "info_score") else None,
            result.win_rate if hasattr(result, "win_rate") else None,
            result.profit_factor if hasattr(result, "profit_factor") else None,
            result.trade_intervals_std if hasattr(result, "trade_intervals_std") else None,
            result.trade_count if hasattr(result, "trade_count") else None,
            result.max_drawdown if hasattr(result, "max_drawdown") else None,
            result.sharpe_ratio if hasattr(result, "sharpe_ratio") else None,
            result.sample_fraction if hasattr(result, "sample_fraction") else None,
            result.category_trade_count if hasattr(result, "category_trade_count") else None,
            result.category_distinct_markets if hasattr(result, "category_distinct_markets") else None,
            result.overall_trade_count if hasattr(result, "overall_trade_count") else None,
            result.largest_winner_share if hasattr(result, "largest_winner_share") else None,
            result.top_3_concentration if hasattr(result, "top_3_concentration") else None,
            result.resolved_markets if hasattr(result, "resolved_markets") else None,
            result.active_trading_days if hasattr(result, "active_trading_days") else None,
            result.distinct_events if hasattr(result, "distinct_events") else None,
            result.category_resolved_markets if hasattr(result, "category_resolved_markets") else None,
            result.category_distinct_events if hasattr(result, "category_distinct_events") else None,
            result.category_active_days if hasattr(result, "category_active_days") else None,
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
    )
    # rowcount will be 0 for IGNORE case, 1 for insert
    if row.rowcount == 0:
        # Check if already exists
        existing = db.fetchone(
            "SELECT id FROM wallet_score_decisions WHERE idempotency_key = ?",
            (idempotency_key,)
        )
        if existing:
            return existing["id"]
    db.conn.commit()
    return row.lastrowid or 0


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
    """Persist trade copyability v1 decision to database."""
    if idempotency_key is None:
        idempotency_key = generate_idempotency_key(
            formula_name="trade_copyability",
            formula_version=result.formula_version,
            wallet_id=wallet_id,
            source_trade_id=source_trade_id,
            source_data_timestamp=source_data_timestamp,
        )

    now = datetime.now(timezone.utc).isoformat()

    row = db.execute(
        """INSERT OR IGNORE INTO trade_copyability_decisions (
            wallet_id, source_trade_id, formula_name, formula_version, idempotency_key,
            price_deterioration_pct, side, intended_stake, executable_depth, fill_percentage,
            spread, best_bid_size, best_ask_size, trade_age_seconds, seconds_to_market_end,
            market_active, market_closed, market_resolved,
            component_scores_json, final_score, verdict, missing_essentials_json,
            rejection_reasons_json, source_data_timestamp, computed_at, created_at,
            candidate_id, price_snapshot_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id""",
        (
            wallet_id,
            source_trade_id,
            "trade_copyability",
            result.formula_version,
            idempotency_key,
            getattr(result, "price_deterioration_pct", None),
            getattr(result, "side", None),
            getattr(result, "intended_stake", None),
            getattr(result, "executable_depth", None),
            getattr(result, "fill_percentage", None),
            getattr(result, "spread", None),
            getattr(result, "best_bid_size", None),
            getattr(result, "best_ask_size", None),
            getattr(result, "trade_age_seconds", None),
            getattr(result, "seconds_to_market_end", None),
            getattr(result, "market_active", None),
            getattr(result, "market_closed", None),
            getattr(result, "market_resolved", None),
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
    )
    db.conn.commit()
    return row.lastrowid or 0


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
    """Persist v2 shadow decision to database (parallel to v1)."""
    if idempotency_key is None:
        idempotency_key = generate_idempotency_key(
            formula_name="shadow_score",
            formula_version=result.formula_version,
            wallet_id=wallet_id,
            source_trade_id=source_trade_id,
            source_data_timestamp=source_data_timestamp,
        )

    now = datetime.now(timezone.utc).isoformat()

    row = db.execute(
        """INSERT OR IGNORE INTO shadow_decisions (
            wallet_id, source_trade_id, formula_name, formula_version, idempotency_key,
            delay_seconds, alpha_signal, price_retention_ratio, slippage_pct, fill_percentage,
            wallet_score, days_since_last_trade, copied_trade_pnl, copied_trade_count,
            position_concentration, correlation_score,
            component_scores_json, final_score, verdict, missing_components_json,
            delay_scenario, source_data_timestamp, computed_at, created_at,
            candidate_id, v1_decision_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id""",
        (
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
    )
    db.conn.commit()
    return row.lastrowid or 0


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
    """Persist paper signal decision (unapproved)."""
    if idempotency_key is None:
        idempotency_key = generate_idempotency_key(
            formula_name="paper_signal",
            formula_version="1",
            source_trade_id=str(candidate_id),
        )

    now = datetime.now(timezone.utc).isoformat()

    row = db.execute(
        """INSERT OR IGNORE INTO paper_signal_decisions (
            candidate_id, wallet_id, signal_family, signal_reason,
            wallet_score, trade_score, shadow_score, shadow_verdict, final_verdict,
            source_data_timestamp, source_trade_id, price_snapshot_id,
            idempotency_key, computed_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id""",
        (
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
    )
    db.conn.commit()
    return row.lastrowid or 0


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
        row = db.execute(
            """INSERT OR IGNORE INTO exit_experiment_registrations (
                paper_signal_id, experiment_type, status, registered_at, scheduled_at
            ) VALUES (?, ?, ?, ?, ?)
            RETURNING id""",
            (
                paper_signal_id,
                exp_type,
                "registered",
                now.isoformat(),
                scheduled_at.isoformat() if scheduled_at else None,
            ),
        )
        db.conn.commit()
        if row.rowcount > 0:
            registered_ids.append(row.lastrowid)

    return registered_ids