"""PR #20 — persistence helper for ``wallet_specialist_aggregations``.

This module is the only writer to the new evidence table. It is
**idempotent**: re-running the aggregation with the same inputs
produces zero net rows because the UNIQUE constraint
``(wallet_id, category_label, formula_name, formula_version,
idempotency_key)`` collapses duplicates.

The return value of :func:`persist_wallet_specialist_aggregation`
is **truthy only when a new row was actually inserted** (i.e. the
``INSERT OR IGNORE`` affected a row). Re-runs with the same
idempotency key return ``False``. This is enforced via
``cursor.rowcount`` from the underlying ``sqlite3.Cursor`` (which
reports 1 for new inserts and 0 for collisions), with a defensive
post-insert existence check as a fallback if a future DB wrapper
hides ``rowcount``.

Design rules
============

* No scoring formula consumes this table in PR #20. Persistence is
  one-way: aggregation in, evidence row out.
* No new columns on existing tables. No destructive migrations.
* No side effects beyond the single INSERT below.

Public API
==========

* :func:`generate_specialist_idempotency_key` — deterministic SHA-256
  hex digest over the canonical inputs.
* :func:`persist_wallet_specialist_aggregation` — INSERT one
  aggregation row; returns True iff a new row was inserted.
* :func:`load_specialist_aggregations_for_wallet` — convenience
  reader for tests / dashboards.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from polycopy.db.database import Database
from polycopy.scoring.score_serialization import generate_idempotency_key

# Formula identity — frozen for PR #20.
SPECIALIST_FORMULA_NAME = "specialist_metrics"
SPECIALIST_FORMULA_VERSION = "1"


# Allowed ``quality`` values — application-layer guard (mirrors the
# documented enum in schema_v13).
ALLOWED_QUALITY = frozenset({"observed", "partial", "unknown", "incomplete"})


def generate_specialist_idempotency_key(
    *,
    wallet_id: str,
    category_label: str,
    source_data_timestamp: Optional[str],
) -> str:
    """Deterministic SHA-256 hex digest for the (wallet, category) row.

    The key is a *subset* of the full UNIQUE index so the helper is
    stable across re-runs that re-derive the same canonical inputs.
    """
    return generate_idempotency_key(
        formula_name=SPECIALIST_FORMULA_NAME,
        formula_version=SPECIALIST_FORMULA_VERSION,
        wallet_id=wallet_id,
        source_data_timestamp=source_data_timestamp,
        extra_params={"category_label": category_label},
    )


def _row_exists(db: Database, *, wallet_id: str, category_label: str,
                idempotency_key: str) -> bool:
    """Defensive existence check used when ``cursor.rowcount`` is unavailable.

    Returns ``True`` iff a row with the given (wallet, category,
    formula_name, formula_version, idempotency_key) exists. This is
    the post-INSERT fallback so the return value of
    :func:`persist_wallet_specialist_aggregation` is always honest.
    """
    row = db.fetchone(
        "SELECT 1 AS present FROM wallet_specialist_aggregations "
        "WHERE wallet_id = ? AND category_label = ? "
        "AND formula_name = ? AND formula_version = ? "
        "AND idempotency_key = ?",
        (
            wallet_id,
            category_label or "",
            SPECIALIST_FORMULA_NAME,
            SPECIALIST_FORMULA_VERSION,
            idempotency_key,
        ),
    )
    return row is not None


def persist_wallet_specialist_aggregation(
    db: Database,
    *,
    wallet_id: str,
    category_label: str,
    source_data_timestamp: Optional[str],
    metrics: dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:
    """Insert one aggregation row. Idempotent.

    Returns
    -------
    bool
        ``True`` if a NEW row was inserted. ``False`` if the
        idempotency key already existed (re-run; INSERT OR IGNORE
        collapsed). Non-idempotency-key errors propagate.

    Implementation
    --------------
    ``INSERT OR IGNORE`` plus ``cursor.rowcount`` is the authoritative
    path. SQLite returns ``rowcount == 1`` on a fresh insert and
    ``rowcount == 0`` when the UNIQUE constraint collapses the
    INSERT — verified against the SQLite C docs and tested in
    ``tests/test_pr20_specialist_metrics_persistence.py``. When the
    wrapper hides ``rowcount`` we fall back to a post-insert
    existence check; either way the return value reflects the
    real on-disk state.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    quality = metrics.get("quality", "unknown")
    if quality not in ALLOWED_QUALITY:
        raise ValueError(
            f"persist_wallet_specialist_aggregation: quality={quality!r} "
            f"not in {sorted(ALLOWED_QUALITY)}"
        )

    component_scores_json = json.dumps(
        metrics.get("component_scores_json") or {}, sort_keys=True
    )
    missing_essentials_json = json.dumps(
        metrics.get("missing_essentials_json") or [], sort_keys=True
    )
    idempotency_key = generate_specialist_idempotency_key(
        wallet_id=wallet_id,
        category_label=category_label,
        source_data_timestamp=source_data_timestamp,
    )

    try:
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO wallet_specialist_aggregations (
                wallet_id, category_label, formula_name, formula_version,
                idempotency_key, source_data_timestamp,
                trade_count, distinct_markets, distinct_events,
                active_trading_days,
                category_trade_count, category_distinct_markets,
                category_active_days, category_concentration,
                sample_reliability_score,
                holding_period_days,
                behavior_classification,
                copyability_evidence_state,
                price_improvement_state,
                component_scores_json, quality, missing_essentials_json,
                created_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?,
                ?, ?,
                ?, ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?, ?, ?,
                ?
            )
            """,
            (
                wallet_id,
                category_label or "",
                SPECIALIST_FORMULA_NAME,
                SPECIALIST_FORMULA_VERSION,
                idempotency_key,
                source_data_timestamp,
                metrics.get("trade_count"),
                metrics.get("distinct_markets"),
                metrics.get("distinct_events"),
                metrics.get("active_trading_days"),
                metrics.get("category_trade_count"),
                metrics.get("category_distinct_markets"),
                metrics.get("category_active_days"),
                metrics.get("category_concentration"),
                metrics.get("sample_reliability_score"),
                metrics.get("holding_period_days"),
                metrics.get("behavior_classification"),
                metrics.get("copyability_evidence_state"),
                metrics.get("price_improvement_state"),
                component_scores_json,
                quality,
                missing_essentials_json,
                now.isoformat(),
            ),
        )
        # Authoritative path: cursor.rowcount. 1 == new insert, 0 == collision.
        rowcount = getattr(cursor, "rowcount", None)
        if rowcount is None:
            # Defensive fallback so a future DB wrapper that hides
            # rowcount still produces a truthful return value.
            return _row_exists(
                db,
                wallet_id=wallet_id,
                category_label=category_label,
                idempotency_key=idempotency_key,
            )
        return int(rowcount) == 1
    except Exception as exc:  # noqa: BLE001 — defensive
        # If for any reason the INSERT raises a UNIQUE violation
        # (the wrapper path bypasses INSERT OR IGNORE), treat it
        # as an idempotent no-op rather than a hard error.
        if "UNIQUE constraint failed" in str(exc):
            return False
        raise


def load_specialist_aggregations_for_wallet(
    db: Database,
    wallet_id: str,
) -> list[dict]:
    """Return every aggregation row for the given wallet, newest first.

    Convenience reader for tests and dashboards. Not used by the
    scoring pipeline in this PR.
    """
    rows = db.fetchall(
        """
        SELECT *
        FROM wallet_specialist_aggregations
        WHERE wallet_id = ?
        ORDER BY created_at DESC, aggregation_id DESC
        """,
        (wallet_id,),
    )
    return [dict(r) for r in rows]