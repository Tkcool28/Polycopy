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
from typing import TYPE_CHECKING, Any, Optional

from polycopy.db.database import Database

if TYPE_CHECKING:
    from polycopy.scoring.paper_signal_input import PaperSignalDecisionInput


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


def _category_input(result) -> Any:
    """Return the typed input that produced a CategoryWalletScoreResultV1.

    Mirrors the wallet-input adapter. If the result has an explicit
    input attached, return it directly. Otherwise reconstruct a
    typed input from the result's stored fields. This is the ONE
    place where getattr on result is tolerated for the category
    input path.
    """
    from polycopy.scoring.category_wallet_score_v1 import (
        CategoryWalletScoreInputV1,
    )

    if getattr(result, "input", None) is not None:
        return result.input  # type: ignore[return-value]

    return CategoryWalletScoreInputV1(
        wallet_id=getattr(result, "wallet_id", "") or "",
        category_label=getattr(result, "category_label", "") or "",
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
        category_resolved_markets=getattr(result, "category_resolved_markets", None),
        category_distinct_events=getattr(result, "category_distinct_events", None),
        category_active_days=getattr(result, "category_active_days", None),
        source_data_timestamp=getattr(result, "source_data_timestamp", None),
        is_sample=getattr(result, "is_sample", False),
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


# ---- Depth-walk audit evidence serialization (Task 2.5) -------------------
#
# Phase 7 + Phase 9: the typed DepthWalkResult attached to the
# trade input is the SOLE source of truth for the depth-walk audit
# columns. We do NOT read these fields from scattered getattr
# fallbacks on the result — every value comes from the typed input.
#
# JSON serialization rules:
# - Decimal values are serialized as canonical decimal strings
#   (no float conversion) so that round-tripping preserves the
#   exact precision used by the score.
# - Nullable values use JSON null (not the string "None").
# - Booleans use JSON true/false.
# - Sort keys for deterministic hashing.
#
# Equivalent Decimal values (e.g. Decimal("5.0") and Decimal("5"))
# serialize identically because normalize() removes trailing zeros
# before str() conversion.

def _serialize_decimal(d) -> Optional[str]:
    """Serialize a Decimal value as a canonical decimal string.

    Returns None when the value is None — callers serialize None
    as JSON null.

    The canonical form is: normalize() first, then format as a
    fixed-point string. This gives Decimal("5.00") and
    Decimal("5") identical serializations ("5") and avoids
    scientific notation unless the value genuinely requires it.
    """
    if d is None:
        return None
    # Use normalize() to strip trailing zeros, then format as
    # fixed-point ("f"). This produces a deterministic, exact
    # string representation.
    return format(d.normalize(), "f") if d.is_finite() else format(d, "f")


def _serialize_depth_walk(input_obj) -> Optional[str]:
    """Serialize the depth-walk audit JSON for persistence.

    Reads every field from `input_obj.depth_walk_result` (typed
    DepthWalkResult). When no depth walk result is present but
    `input_obj.depth_status_reason` is set, an audit envelope
    reflecting the rejection status is persisted instead so the
    absence is itself documented.

    Returns None ONLY when no depth evidence of any kind was
    available — i.e. neither a typed result nor a status reason.

    Canonical JSON keys (sorted):
    - side (str)
    - intended_notional (str Decimal)
    - filled_notional (str Decimal)
    - fill_percentage (str Decimal)
    - contracts_filled (str Decimal)
    - vwap_fill_price (str Decimal or null)
    - slippage (str Decimal or null)
    - levels_consumed (int)
    - remaining_notional (str Decimal)
    - is_complete (bool)
    - insufficient_reason (str or null)
    - depth_hash (str or null)
    - price_snapshot_id (str or null)
    """
    dw = getattr(input_obj, "depth_walk_result", None)
    depth_hash = getattr(input_obj, "depth_hash", None)
    price_snapshot_id = getattr(input_obj, "price_snapshot_id", None)

    if dw is None:
        # No typed result. Persist an envelope recording the
        # rejection status (if any) so the absence is documented.
        status = getattr(input_obj, "depth_status_reason", None)
        if status is None:
            return None
        payload: dict[str, Any] = {
            "depth_status_reason": status,
            "is_complete": False,
            "depth_hash": depth_hash,
            "price_snapshot_id": price_snapshot_id,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    payload = {
        "side": dw.side,
        "intended_notional": _serialize_decimal(dw.intended_notional),
        "filled_notional": _serialize_decimal(dw.filled_notional),
        "fill_percentage": _serialize_decimal(dw.fill_percentage),
        "contracts_filled": _serialize_decimal(dw.contracts_filled),
        "vwap_fill_price": _serialize_decimal(dw.vwap_fill_price),
        "slippage": _serialize_decimal(dw.slippage),
        "levels_consumed": dw.levels_consumed,
        "remaining_notional": _serialize_decimal(dw.remaining_notional),
        "is_complete": bool(dw.is_complete),
        "insufficient_reason": dw.insufficient_reason,
        "depth_hash": depth_hash,
        "price_snapshot_id": price_snapshot_id,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _serialize_insufficient_reason(input_obj) -> Optional[str]:
    """Compute the insufficient_depth_reason column value.

    Priority:
      1. The typed DepthWalkResult's `insufficient_reason` (when
         present). This is the authoritative reason — set by the
         walk itself.
      2. The typed input's `depth_status_reason` (DEPTH_NOT_CAPTURED /
         DEPTH_LEVELS_MALFORMED / DEPTH_SNAPSHOT_MISMATCH) when no
         depth walk result is available.
      3. NULL when neither applies (e.g. full fill, no depth evidence).

    Full fills must NEVER receive DEPTH_INSUFFICIENT_FOR_STAKE.
    """
    dw = getattr(input_obj, "depth_walk_result", None)
    if dw is not None and dw.insufficient_reason is not None:
        return dw.insufficient_reason
    if dw is None:
        status = getattr(input_obj, "depth_status_reason", None)
        if status is not None:
            return status
    return None


def _effective_fill_percentage(input_obj) -> Optional[float]:
    """Effective fill_percentage for column storage (REAL).

    Reads from the typed DepthWalkResult when present (preferred),
    otherwise from the raw input. Decimal ratio on [0, 1] is
    cast to float for the REAL column.
    """
    dw = getattr(input_obj, "depth_walk_result", None)
    if dw is not None:
        return float(dw.fill_percentage)
    return getattr(input_obj, "fill_percentage", None)


def _effective_executable_depth(input_obj) -> Optional[float]:
    """Effective executable_depth for column storage (REAL).

    The typed DepthWalkResult's `filled_notional` (in USDC) is the
    SOLE source of truth when present. Otherwise the raw input
    value is used.
    """
    dw = getattr(input_obj, "depth_walk_result", None)
    if dw is not None:
        return float(dw.filled_notional)
    return getattr(input_obj, "executable_depth", None)


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
    to find the existing row, drains that cursor too, commits, returns
    the existing id.

    Raises :class:`PersistenceError` when neither path produces a
    row id. Callers MUST NOT use ``int(cur.lastrowid or 0)`` to
    mask this failure — a row whose id is unknown is a
    persistence bug, not a silent skip.
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
        # is enforced. Raise so the caller can surface the failure
        # clearly instead of silently writing rows whose id is 0.
        raise PersistenceError(
            "INSERT OR IGNORE skipped AND the existing-row lookup "
            "returned no row — UNIQUE constraint may not be enforced "
            "or the lookup SQL is incorrect."
        )
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
    # ---- Application-level validation (mirrors schema CHECKs) -------
    inp = _wallet_input(result)
    from polycopy.scoring.persistence_validation import validate_wallet_row
    validate_wallet_row(
        final_score=result.score,
        verdict=result.verdict,
        trade_count=inp.trade_count,
        category_trade_count=inp.category_trade_count,
        category_distinct_markets=inp.category_distinct_markets,
        overall_trade_count=inp.overall_trade_count,
        resolved_markets=inp.resolved_markets,
        active_trading_days=inp.active_trading_days,
        distinct_events=inp.distinct_events,
        category_resolved_markets=inp.category_resolved_markets,
        category_distinct_events=inp.category_distinct_events,
        category_active_days=inp.category_active_days,
    )

    if idempotency_key is None:
        idempotency_key = generate_idempotency_key(
            formula_name="wallet_score",
            formula_version=result.formula_version,
            wallet_id=wallet_id,
            source_data_timestamp=source_data_timestamp,
        )

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


def persist_category_score_v1(
    db: Database,
    wallet_id: str,
    category_label: str,
    result,
    *,
    idempotency_key: Optional[str] = None,
    source_data_timestamp: Optional[str] = None,
) -> int:
    """Persist category wallet score v1 decision to database (Phase 2).

    Reads every raw input column from `result.input` (the typed
    ``CategoryWalletScoreInputV1`` that produced this result). The
    adapter `_category_input` reconstructs a typed input from
    result fields when no explicit input is attached.

    Idempotency identity (frozen):
        (wallet_id, category_label, formula_name, formula_version,
         idempotency_key)
    The idempotency_key is derived deterministically from the
    formula name/version + the source-data timestamp by default.
    Identical point-in-time inputs → identical key → single row.
    A later source-data snapshot → different key → new immutable
    row. ``category_label`` participates in the identity.

    INSERT column/placeholder/value count: 28 / 28 / 28
    (enforced by ``TestCategoryColumnPlaceholderValueCount``).
    """
    if idempotency_key is None:
        idempotency_key = generate_idempotency_key(
            formula_name="category_wallet_score",
            formula_version=result.formula_version,
            wallet_id=wallet_id,
            source_data_timestamp=source_data_timestamp,
            extra_params={"category_label": category_label},
        )

    inp = _category_input(result)
    # ---- Application-level validation (mirrors schema CHECKs) -------
    from polycopy.scoring.persistence_validation import validate_category_row
    validate_category_row(
        final_score=result.score,
        verdict=result.verdict,
        trade_count=inp.trade_count,
        category_trade_count=inp.category_trade_count,
        category_distinct_markets=inp.category_distinct_markets,
        overall_trade_count=inp.overall_trade_count,
        category_resolved_markets=inp.category_resolved_markets,
        category_distinct_events=inp.category_distinct_events,
        category_active_days=inp.category_active_days,
    )

    now = datetime.now(timezone.utc).isoformat()

    return _insert_or_ignore_returning_id(
        db,
        sql="""
            INSERT OR IGNORE INTO category_wallet_score_decisions (
                wallet_id, category_label, formula_name, formula_version, idempotency_key,
                info_score, win_rate, profit_factor, trade_intervals_std, trade_count,
                max_drawdown, sharpe_ratio, sample_fraction,
                category_trade_count, category_distinct_markets, overall_trade_count,
                largest_winner_share, top_3_concentration,
                category_resolved_markets, category_distinct_events, category_active_days,
                component_scores_json, final_score, verdict, missing_essentials_json,
                category_gate_failures_json, source_data_timestamp, computed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
        """,
        params=(
            wallet_id,
            category_label,
            "category_wallet_score",
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
            inp.category_resolved_markets,
            inp.category_distinct_events,
            inp.category_active_days,
            serialize_score_components(result.components),
            result.score,
            result.verdict.value,
            json.dumps(result.missing_essentials),
            json.dumps(result.category_gate_failures),
            source_data_timestamp,
            now,
            now,
        ),
        # Table UNIQUE: (wallet_id, category_label, formula_name, formula_version, idempotency_key)
        existing_lookup_sql="""
            SELECT id FROM category_wallet_score_decisions
            WHERE wallet_id = ? AND category_label = ? AND formula_name = ? AND formula_version = ? AND idempotency_key = ?
        """,
        existing_lookup_params=(
            wallet_id, category_label, "category_wallet_score", result.formula_version, idempotency_key,
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
    """Persist trade copyability v1 decision to database (Phase 9 + Phase 7).

    INSERT column/placeholder/value count: 30 / 30 / 30
    (enforced by TestColumnPlaceholderValueCount).

    Table UNIQUE constraint:
    UNIQUE(source_trade_id, formula_name, formula_version, idempotency_key)
    The fallback lookup uses all four columns to find the existing row.
    Note: source_trade_id is part of the UNIQUE constraint but
    wallet_id is not (multiple wallets can score the same source
    trade). wallet_id is still in the row for query convenience.

    Depth-walk audit evidence (Phase 7 + Phase 9):
    - depth_walk_json is the canonical JSON serialization of
      result.input.depth_walk_result (or a rejection envelope if
      depth_status_reason is set without a typed result).
    - insufficient_depth_reason is set from the typed result's
      insufficient_reason, falling back to depth_status_reason.
    - fill_percentage and executable_depth are the EFFECTIVE values
      (typed depth walk overrides raw input fields).
    - intended_stake and price_snapshot_id come from the typed input.
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
    # ---- Application-level validation (mirrors schema CHECKs) -------
    from polycopy.scoring.persistence_validation import validate_trade_row
    validate_trade_row(
        final_score=result.score,
        verdict=result.verdict,
        intended_stake=inp.intended_stake,
        executable_depth=inp.executable_depth,
        fill_percentage=inp.fill_percentage,
        trade_age_seconds=inp.trade_age_seconds,
        seconds_to_market_end=inp.seconds_to_market_end,
        market_active=inp.market_active,
        market_closed=inp.market_closed,
        market_resolved=inp.market_resolved,
    )

    now = datetime.now(timezone.utc).isoformat()

    # Depth-walk audit evidence (Phase 7 + Phase 9). Every value
    # comes from the typed input — no scattered getattr fallbacks
    # on the result.
    depth_walk_json = _serialize_depth_walk(inp)
    insufficient_reason = _serialize_insufficient_reason(inp)
    eff_fill_pct = _effective_fill_percentage(inp)
    eff_exec_depth = _effective_executable_depth(inp)

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
            eff_exec_depth,
            eff_fill_pct,
            inp.spread,
            inp.best_bid_size,
            inp.best_ask_size,
            inp.trade_age_seconds,
            inp.seconds_to_market_end,
            inp.market_active,
            inp.market_closed,
            inp.market_resolved,
            # Depth-walk audit evidence (Phase 7 + Phase 9)
            depth_walk_json,
            insufficient_reason,
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

    Reads every raw input column from the typed
    :class:`ShadowScoreInputV2` attached to ``result.input``. A
    defensive legacy fallback (in the ``else`` branch below) remains
    for tests that explicitly build a non-typed ``ShadowScoreResult``;
    the runtime always supplies a typed input.

    Table UNIQUE constraint:
    UNIQUE(wallet_id, source_trade_id, formula_name, formula_version, idempotency_key)
    """
    if idempotency_key is None:
        # Identity (Chunk 5 §5.1): wallet_id, source_trade_id,
        # candidate_id, delay_scenario, formula_name, formula_version,
        # source_data_timestamp, price_snapshot_id, depth_hash.
        # ``extra_params`` carries the scenario- and snapshot-specific
        # bits so a changed scenario / snapshot creates a new row.
        scenario = getattr(result, "delay_scenario", None) or "unknown"
        if hasattr(scenario, "value"):
            scenario = scenario.value
        snapshot_id = getattr(result, "price_snapshot_id", None) or "missing"
        depth_hash = getattr(result, "depth_hash", None) or "missing"
        cand_id = (
            int(candidate_id) if candidate_id is not None else None
        )
        # Prefer the typed input when present (Chunk 5 contract).
        typed_in = getattr(result, "input", None)
        if typed_in is not None:
            try:
                scenario = typed_in.delay_scenario.value
            except AttributeError:
                scenario = str(getattr(typed_in, "delay_scenario", scenario))
            snapshot_id = (
                getattr(typed_in, "price_snapshot_id", None) or "missing"
            )
            depth_hash = getattr(typed_in, "depth_hash", None) or "missing"
            cand_id = getattr(typed_in, "candidate_id", None)
            src_ts = getattr(typed_in, "source_data_timestamp", None)
            if src_ts is not None:
                source_data_timestamp = src_ts

        idempotency_key = generate_idempotency_key(
            formula_name="shadow_score",
            formula_version=result.formula_version,
            wallet_id=wallet_id,
            source_trade_id=source_trade_id,
            source_data_timestamp=source_data_timestamp,
            extra_params={
                "candidate_id": (
                    str(cand_id) if cand_id is not None else "missing"
                ),
                "delay_scenario": str(scenario),
                "price_snapshot_id": str(snapshot_id),
                "depth_hash": str(depth_hash),
            },
        )

    inp = getattr(result, "input", None)
    now = datetime.now(timezone.utc).isoformat()

    # Pull fields from the typed input contract.
    if inp is not None:
        source_price = getattr(inp, "source_price", None)
        delayed_copy_price = getattr(inp, "delayed_copy_price", None)
        slippage = getattr(inp, "slippage", None)
        spread = getattr(inp, "spread", None)
        intended_stake = getattr(inp, "intended_stake", None)
        executable_depth = getattr(inp, "executable_depth", None)
        wallet_skill_persistence_input = getattr(
            inp, "wallet_skill_persistence_input", None
        )
        copied_realized_performance_input = getattr(
            inp, "copied_realized_performance_input", None
        )
        concentration_correlation_input = getattr(
            inp, "concentration_correlation_input", None
        )
        measured_delay_seconds = getattr(inp, "measured_delay_seconds", None)
        price_snapshot_id = getattr(inp, "price_snapshot_id", None)
        depth_hash = getattr(inp, "depth_hash", None)
        # Repair 2d — offset audit fields, sourced from the typed
        # input. The runtime enforces 0 <= actual_observed <= 600;
        # the validator below rejects anything outside that range.
        target_delay_seconds = getattr(
            inp, "target_delay_seconds", None
        )
        actual_observed_delay_seconds = getattr(
            inp, "actual_observed_delay_seconds", None
        )
        delay_error_seconds = getattr(
            inp, "delay_error_seconds", None
        )
        # delay_scenario is an enum on the typed input; persist its
        # canonical string value.
        delay_scenario = getattr(inp, "delay_scenario", None)
        delay_scenario_str = (
            delay_scenario.value
            if hasattr(delay_scenario, "value")
            else str(delay_scenario) if delay_scenario is not None else None
        )
        # For backward compatibility with the existing alpha_signal /
        # price_retention_ratio legacy columns: derive them from the
        # typed input when possible (without silently inventing data).
        alpha_signal = None
        price_retention_ratio = None
        fill_percentage = getattr(inp, "fill_percentage", None)
    else:
        # Defensive: typed input missing. Persist only the fields the
        # result object exposes. This branch is exercised only by
        # tests that explicitly build a legacy ShadowScoreResult;
        # the runtime always supplies a typed input.
        source_price = None
        delayed_copy_price = None
        slippage = getattr(result, "slippage_pct", None)
        spread = None
        intended_stake = None
        executable_depth = None
        wallet_skill_persistence_input = None
        copied_realized_performance_input = None
        concentration_correlation_input = None
        measured_delay_seconds = None
        price_snapshot_id = None
        depth_hash = None
        # Repair 2d — defensive defaults when no typed input.
        target_delay_seconds = None
        actual_observed_delay_seconds = None
        delay_error_seconds = None
        delay_scenario_str = getattr(result, "delay_scenario", None)
        alpha_signal = None
        price_retention_ratio = None
        fill_percentage = getattr(result, "fill_percentage", None)

    missing_forward_reasons_json = json.dumps(
        list(getattr(result, "missing_forward_reasons", []) or []),
        sort_keys=True,
    )

    # ---- Application-level validation (mirrors schema CHECKs) -------
    from polycopy.scoring.persistence_validation import validate_shadow_row
    # Resolve delay_seconds from the typed input when present.
    typed_in = getattr(result, "input", None)
    if typed_in is not None:
        ds_attr = getattr(typed_in, "delay_scenario_seconds", None)
        delay_seconds_val = ds_attr() if callable(ds_attr) else ds_attr
    else:
        delay_seconds_val = getattr(result, "delay_seconds", None)
    validate_shadow_row(
        final_score=result.score,
        verdict=result.verdict,
        delay_scenario=delay_scenario_str,
        delay_seconds=delay_seconds_val,
        fill_percentage=fill_percentage,
        measured_delay_seconds=measured_delay_seconds,
        copied_trade_count=getattr(result, "copied_trade_count", None),
        days_since_last_trade=getattr(result, "days_since_last_trade", None),
    )
    # ---- Repair 2d — offset-audit bounds (0..600s on actual) ----
    # Shadow V2 audit fields. The validator above covers
    # ``measured_delay_seconds`` (legacy single field) but the v12
    # typed contract exposes three separate offset fields. We enforce
    # the upper bound inline so an obviously corrupt timestamp can't
    # land in the audit trail.
    if actual_observed_delay_seconds is not None:
        try:
            ao = float(actual_observed_delay_seconds)
        except (TypeError, ValueError):
            ao = None
        else:
            if ao < 0.0 or ao > 600.0:
                raise ValueError(
                    f"actual_observed_delay_seconds={ao} outside [0, 600]"
                )

    return _insert_or_ignore_returning_id(
        db,
        sql="""
            INSERT OR IGNORE INTO shadow_decisions (
                wallet_id, source_trade_id, formula_name, formula_version, idempotency_key,
                delay_seconds, source_price, delayed_copy_price,
                alpha_signal, price_retention_ratio,
                slippage_pct, slippage, spread, fill_percentage,
                intended_stake, executable_depth,
                wallet_skill_persistence_input,
                copied_realized_performance_input,
                concentration_correlation_input,
                days_since_last_trade, copied_trade_pnl, copied_trade_count,
                position_concentration, correlation_score, wallet_score,
                measured_delay_seconds,
                component_scores_json, final_score, verdict,
                missing_components_json, missing_forward_reasons_json,
                delay_scenario,
                source_data_timestamp, price_snapshot_id, depth_hash,
                computed_at, created_at,
                candidate_id, v1_decision_id,
                target_delay_seconds, actual_observed_delay_seconds, delay_error_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
        """,
        params=(
            wallet_id,
            source_trade_id,
            "shadow_score",
            result.formula_version,
            idempotency_key,
            getattr(result, "delay_seconds", None),
            source_price,
            delayed_copy_price,
            alpha_signal,
            price_retention_ratio,
            # Legacy slippage_pct column (preserved for back-compat).
            getattr(result, "slippage_pct", None),
            slippage,
            spread,
            fill_percentage,
            intended_stake,
            executable_depth,
            wallet_skill_persistence_input,
            copied_realized_performance_input,
            concentration_correlation_input,
            getattr(result, "days_since_last_trade", None),
            getattr(result, "copied_trade_pnl", None),
            getattr(result, "copied_trade_count", None),
            getattr(result, "position_concentration", None),
            getattr(result, "correlation_score", None),
            # Legacy wallet_score column (preserved for back-compat).
            getattr(result, "wallet_score", None),
            measured_delay_seconds,
            _serialize_shadow_components(result),
            result.score,
            _verdict_to_str(result.verdict),
            json.dumps(
                list(getattr(result, "missing_components", []) or []),
                sort_keys=True,
            ),
            missing_forward_reasons_json,
            delay_scenario_str,
            source_data_timestamp,
            price_snapshot_id,
            depth_hash,
            now,
            now,
            candidate_id,
            v1_decision_id,
            # Repair 2d — v12 offset-audit columns, sourced from the
            # typed input (or None when the defensive legacy branch ran).
            target_delay_seconds,
            actual_observed_delay_seconds,
            delay_error_seconds,
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


def _verdict_to_str(verdict: object) -> str:
    """Return the canonical string form of a verdict.

    Accepts:
      * ``ShadowVerdict`` (legacy enum) — uses ``.value``.
      * Plain str (new typed contract) — returned as-is.
    """
    if hasattr(verdict, "value") and not isinstance(verdict, str):
        return str(verdict.value)
    return str(verdict)


def _serialize_shadow_components(result: object) -> str:
    """Serialize a shadow result's component scores to canonical JSON.

    Accepts either the typed ``ShadowScoreResultV2`` (which stores
    ``component_scores`` as a tuple of dicts) or the legacy
    ``ShadowScoreResult`` (which stores ``components`` as a list of
    ``ShadowScoreComponent`` instances).
    """
    candidates = (
        list(getattr(result, "component_scores", []) or [])
        or list(getattr(result, "components", []) or [])
    )
    out = []
    for c in candidates:
        if isinstance(c, dict):
            out.append(
                {
                    "name": c.get("name"),
                    "raw_score": c.get("raw_score"),
                    "weight": c.get("weight"),
                    "weighted_score": c.get("weighted_score"),
                    "quality": c.get("quality"),
                    "formula": c.get("formula"),
                    "note": c.get("note"),
                }
            )
            continue
        # Legacy dataclass path.
        out.append(
            {
                "name": getattr(c, "name", None),
                "raw_score": getattr(c, "raw_score", None),
                "weight": getattr(c, "weight", None),
                "weighted_score": getattr(c, "weighted_score", None),
                "quality": getattr(c, "quality", None),
                "formula": getattr(c, "formula", None),
                "note": getattr(c, "note", ""),
            }
        )
    return json.dumps(out)


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
    typed_input: Optional["PaperSignalDecisionInput"] = None,
) -> int:
    """Persist paper signal decision (unapproved).

    Reads every column from the typed
    :class:`PaperSignalDecisionInput` when provided (``typed_input``),
    which is the Chunk 5 contract. The typed input is the source of
    truth for the audit columns
    (``decision_input_json``, ``wallet_score_decision_id``,
    ``category_score_decision_id``, ``trade_score_decision_id``) — the
    positional kwargs may be omitted or stale, but the audit fields
    come from the typed contract. A legacy adapter path remains for
    callers that have not been migrated yet — that path writes
    ``NULL`` for the audit columns and never fabricates data.

    Safety invariants enforced here:

      * ``is_approved`` is forced to ``0`` regardless of what the
        typed input requests. PR 4 paper signals are NEVER approved.
        A defensive ``auto_approve_requested`` flag on the typed
        input is honored by recording an explicit safety reason
        (``auto_approve_rejected_for_paper_signal``) on the row's
        ``signal_reason`` column.
      * No CLOB / HTTP / broker / order / position side-effects.

    Table UNIQUE constraint:
    UNIQUE(candidate_id, idempotency_key)
    """
    from polycopy.scoring.paper_signal_input import (
        SAFETY_REASON_AUTO_APPROVE_REJECTED,
        serialize_paper_signal_input,
    )

    if idempotency_key is None:
        idempotency_key = generate_idempotency_key(
            formula_name="paper_signal",
            formula_version="1",
            source_trade_id=str(candidate_id),
        )

    # Determine whether the caller attempted to auto-approve. The
    # typed contract makes that explicit so we can record a safety
    # reason; the legacy kwargs path has no such hook, so we treat
    # an explicit wallet_id is_approved=1 in the future as a guard
    # check — currently only the typed contract can carry that flag.
    auto_approve_attempted = bool(
        typed_input is not None and getattr(typed_input, "auto_approve_requested", False)
    )

    # ---- Forced invariants -------------------------------------------------
    # PR 4 paper signals are always unapproved.
    persisted_reason = signal_reason
    if auto_approve_attempted:
        persisted_reason = (
            f"{signal_reason or ''}|{SAFETY_REASON_AUTO_APPROVE_REJECTED}"
            if signal_reason
            else SAFETY_REASON_AUTO_APPROVE_REJECTED
        )

    # ---- Application-level validation (mirrors schema CHECKs) -------
    from polycopy.scoring.persistence_validation import validate_paper_signal_row
    validate_paper_signal_row(
        signal_family=signal_family,
        wallet_score=wallet_score,
        trade_score=trade_score,
        shadow_score=shadow_score,
        shadow_verdict=shadow_verdict,
        final_verdict=final_verdict,
        is_approved=0,  # PR 4 always-0 invariant; explicit for clarity.
        auto_approve_requested=auto_approve_attempted,
    )

    # ---- Identity derivation ----------------------------------------------
    # When the typed input is present, the identity MUST include the
    # decision ids, the intended stake, the category label, the
    # verdict, and the formula versions. This guarantees that a
    # materially changed input produces a new immutable row.
    # Repair 1 strengthens the identity to also include the trade
    # decision id, so a changed trade-decision id (e.g. a re-run
    # that re-persisted trade scoring) yields a new paper-signal
    # row rather than silently re-using the previous audit row.
    if typed_input is not None:
        verdict_for_idem = str(getattr(typed_input, "final_verdict", final_verdict))
        reason_for_idem = str(getattr(typed_input, "final_reason", persisted_reason or ""))
        stake_for_idem = (
            f"{float(typed_input.intended_stake):.2f}"
            if typed_input.intended_stake is not None else "missing"
        )
        cat_for_idem = typed_input.category_label or "missing"
        wallet_dec_id = typed_input.wallet_score_decision_id
        cat_dec_id = typed_input.category_score_decision_id
        trade_dec_id = typed_input.trade_score_decision_id
        snap_for_idem = typed_input.price_snapshot_id or "missing"
        idempotency_key = generate_idempotency_key(
            formula_name="paper_signal",
            formula_version=typed_input.trade_formula_version or "1",
            wallet_id=typed_input.wallet_id or wallet_id,
            source_trade_id=typed_input.source_trade_id or source_trade_id,
            source_data_timestamp=source_data_timestamp,
            extra_params={
                "candidate_id": str(candidate_id),
                "snapshot_id": str(snap_for_idem),
                "wallet_decision_id": (
                    str(wallet_dec_id) if wallet_dec_id is not None else "missing"
                ),
                "category_decision_id": (
                    str(cat_dec_id) if cat_dec_id is not None else "missing"
                ),
                "trade_decision_id": (
                    str(trade_dec_id) if trade_dec_id is not None else "missing"
                ),
                "intended_stake": stake_for_idem,
                "category_label": cat_for_idem,
                "verdict": verdict_for_idem,
                "reason": reason_for_idem,
                "trade_formula_name": typed_input.trade_formula_name,
                "trade_formula_version": typed_input.trade_formula_version,
                "wallet_formula_name": typed_input.wallet_formula_name,
                "wallet_formula_version": typed_input.wallet_formula_version,
                "category_formula_name": typed_input.category_formula_name,
                "category_formula_version": typed_input.category_formula_version,
            },
        )

    now = datetime.now(timezone.utc).isoformat()

    # ---- Audit columns (Repair 1 — v12) -----------------------------------
    # The typed input is the source of truth for the four audit
    # columns. Legacy callers that omit ``typed_input`` get NULLs —
    # we never invent audit data on their behalf.
    audit_decision_input_json: Optional[str] = None
    audit_wallet_decision_id: Optional[int] = None
    audit_category_decision_id: Optional[int] = None
    audit_trade_decision_id: Optional[int] = None
    if typed_input is not None:
        audit_decision_input_json = serialize_paper_signal_input(typed_input)
        audit_wallet_decision_id = typed_input.wallet_score_decision_id
        audit_category_decision_id = typed_input.category_score_decision_id
        audit_trade_decision_id = typed_input.trade_score_decision_id

    return _insert_or_ignore_returning_id(
        db,
        sql="""
            INSERT OR IGNORE INTO paper_signal_decisions (
                candidate_id, wallet_id, signal_family, signal_reason,
                wallet_score, trade_score, shadow_score, shadow_verdict, final_verdict,
                source_data_timestamp, source_trade_id, price_snapshot_id,
                idempotency_key, computed_at, created_at,
                decision_input_json,
                wallet_score_decision_id,
                category_score_decision_id,
                trade_score_decision_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?)
            RETURNING id
        """,
        params=(
            candidate_id,
            wallet_id,
            signal_family,
            persisted_reason,
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
            # Audit columns (Repair 1). Null for legacy callers that
            # omitted typed_input; non-null when the typed contract
            # is the source of truth.
            audit_decision_input_json,
            audit_wallet_decision_id,
            audit_category_decision_id,
            audit_trade_decision_id,
        ),
        # Table UNIQUE: (candidate_id, idempotency_key)
        existing_lookup_sql="""
            SELECT id FROM paper_signal_decisions
            WHERE candidate_id = ? AND idempotency_key = ?
        """,
        existing_lookup_params=(candidate_id, idempotency_key),
    )


# ---- Exit experiments (not in scope for this fix but uses RETURNING) ---


class PersistenceError(RuntimeError):
    """Raised when a persistence helper cannot return a row id.

    Used in place of ``int(x or 0)`` for lastrowid handling so the
    caller can surface a clear error rather than silently writing
    a row whose id is unknown.
    """

def record_exit_experiments(
    db: Database,
    paper_signal_id: int,
    *,
    signal_evaluation_timestamp: Optional["datetime"] = None,
) -> list[int]:
    """Register the canonical seven exit experiment tracks for a paper signal.

    Canonical identifiers (Chunk 5 §5.3 — exactly seven):

        HOLD_TO_RESOLUTION
        EXIT_24H
        EXIT_72H
        FAVORABLE_MOVE_005
        FAVORABLE_MOVE_010
        FAVORABLE_MOVE_015
        THESIS_OR_LIQUIDITY_FAILURE

    Scheduling:

        HOLD_TO_RESOLUTION              scheduled_at = NULL
        EXIT_24H                        signal_evaluation_timestamp + 24h
        EXIT_72H                        signal_evaluation_timestamp + 72h
        FAVORABLE_MOVE_005              scheduled_at = NULL
        FAVORABLE_MOVE_010              scheduled_at = NULL
        FAVORABLE_MOVE_015              scheduled_at = NULL
        THESIS_OR_LIQUIDITY_FAILURE     scheduled_at = NULL

    The +24h / +72h scheduling MUST derive from the immutable
    ``signal_evaluation_timestamp``. When the timestamp is missing,
    we fall back to wall-clock now() ONLY for the registration
    moment (registered_at); the scheduled_at for EXIT_24H / EXIT_72H
    becomes NULL in that case so we never silently invent a future
    timestamp — the research row is preserved with a clear audit
    gap rather than a fabricated schedule.

    This function NEVER places orders, opens positions, or talks to
    any broker / CLOB / HTTP endpoint.

    Table UNIQUE: (paper_signal_id, experiment_type)
    """
    from polycopy.scoring.exit_tracks import (
        CANONICAL_EXIT_TRACKS,
        ExitTrack,
        compute_scheduled_at,
    )

    # ---- Application-level validation (mirrors schema CHECKs) -------
    from polycopy.scoring.persistence_validation import validate_exit_track_batch
    validate_exit_track_batch(CANONICAL_EXIT_TRACKS)

    now = datetime.now(timezone.utc)
    registered_ids: list[int] = []

    for track in CANONICAL_EXIT_TRACKS:
        if (
            track in (ExitTrack.EXIT_24H, ExitTrack.EXIT_72H)
            and signal_evaluation_timestamp is not None
        ):
            scheduled_at = compute_scheduled_at(
                track,
                signal_evaluation_timestamp=signal_evaluation_timestamp,
            )
        else:
            scheduled_at = None

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
                    track.value,
                    "registered",
                    now.isoformat(),
                    scheduled_at.isoformat() if scheduled_at else None,
                ),
                existing_lookup_sql="""
                    SELECT id FROM exit_experiment_registrations
                    WHERE paper_signal_id = ? AND experiment_type = ?
                """,
                existing_lookup_params=(paper_signal_id, track.value),
            )
        )
    return registered_ids
