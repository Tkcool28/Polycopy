"""PR #20 — bounded specialist-metric aggregation step.

This module wires :mod:`polycopy.scoring.specialist_metrics` and
:mod:`polycopy.scoring.specialist_metrics_persistence` into the
existing run_scan pipeline under a **bounded, idempotent** step
that mirrors the PR 19 hard-cap invariant.

Hard-cap invariant
==================

When ``max_aggregations`` is a positive integer:

.. code-block:: python

    len(rows_written) <= max_aggregations

ALWAYS. The runtime caps the number of *fresh* aggregation inserts
per scan; re-runs with the same idempotency keys are no-ops.

Selection algorithm
===================

We iterate the **fresh-insert wallet IDs from PR 19 Step 5b**
(``pr5c._fresh_insert_wallet_ids``) in deterministic sorted order.
For each wallet we aggregate its trades and persist:

  * one row at the wallet level with ``category_label = ""`` (no
    category resolvable from snapshot book-summary-json alone — that
    is the documented conservative behavior, see audit §4);
  * one additional row per resolved category, when present in
    ``candidate_price_snapshots.book_summary_json``.

Every row carries its own idempotency key so a partial run is
safe to resume.

Safety
======

This module NEVER writes to ``orders``, ``positions``, ``signals``,
``paper_signal_decisions.is_approved``, or any broker / CLOB path.
It only writes to the new ``wallet_specialist_aggregations`` table.
The PR 19 budget invariant (``len(fresh_insert_wallet_ids) <=
max_wallet_scores``) is preserved because we operate on that exact
set, not a fresh discovery sweep.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from polycopy.db.database import Database
from polycopy.scoring.specialist_metrics import aggregate_specialist_metrics
from polycopy.scoring.specialist_metrics_persistence import (
    persist_wallet_specialist_aggregation,
)

logger = logging.getLogger(__name__)


def _load_trades_for_wallet(
    db: Database,
    wallet_id: str,
) -> list[dict]:
    """Return all source_trades rows attributable to the given wallet_id.

    The join goes: ``wallets.id → wallets.canonical_address →
    source_trades.trader_address`` (case-insensitive). Sort order
    is intentionally omitted — the aggregator is order-insensitive.
    """
    rows = db.fetchall(
        """
        SELECT st.*
        FROM source_trades st
        JOIN wallets w ON LOWER(w.canonical_address) = LOWER(st.trader_address)
        WHERE w.id = ?
          AND st.trader_address IS NOT NULL
        """,
        (wallet_id,),
    )
    return [dict(r) for r in rows]


def _resolve_categories_from_snapshots(
    db: Database,
    wallet_id: str,
) -> dict[str, str]:
    """Best-effort map of market_source_id → category_label for a wallet.

    Categories are resolved ONLY from
    ``candidate_price_snapshots.book_summary_json``. This matches
    the existing paper-signal resolver behavior
    (:func:`polycopy.scoring.paper_signal._resolve_category_label_safe`).
    Markets without a category label in the snapshot are omitted —
    no synthetic fallback.
    """
    rows = db.fetchall(
        """
        SELECT DISTINCT cps.book_summary_json AS summary, st.market_source_id AS mid
        FROM source_trades st
        JOIN copy_candidates cc
          ON cc.wallet_id = ?
         AND cc.source_trade_id = st.source + ':' || st.source_trade_id
        JOIN candidate_price_snapshots cps
          ON cps.candidate_id = cc.id
        WHERE st.trader_address IS NOT NULL
        """,
        (wallet_id,),
    )

    out: dict[str, str] = {}
    for r in rows:
        mid = r["mid"]
        summary = r["summary"]
        if not isinstance(summary, str) or not summary.strip():
            continue
        try:
            parsed = json.loads(summary)
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        label = parsed.get("category_label") or parsed.get("category")
        if isinstance(label, str) and label.strip() and isinstance(mid, str):
            out[mid] = label.strip()
    return out


def compute_and_persist_wallet_specialist_aggregations(
    db: Database,
    *,
    fresh_insert_wallet_ids: list[str],
    max_aggregations: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """Compute + persist the aggregation rows for the bounded wallet slice.

    Parameters
    ----------
    db:
        Connected :class:`polycopy.db.database.Database`.
    fresh_insert_wallet_ids:
        The wallets Step 5b just inserted this run. We only operate
        on this exact set so the PR 19 budget invariant is preserved.
    max_aggregations:
        Optional hard cap on rows written (wallet-level + per-category
        combined). ``None`` means no cap — callers in tests may set
        ``None``; production always sets a positive cap.
    now:
        Optional wall-clock for ``created_at``.

    Returns
    -------
    dict[str, int]
        Counters: ``{"wallets_processed", "rows_written",
        "rows_skipped_idempotent", "errors"}``.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    counters = {
        "wallets_processed": 0,
        "rows_written": 0,
        "rows_skipped_idempotent": 0,
        "errors": 0,
    }

    # Sort deterministically so the bounded slice iterates in the
    # same order across runs.
    wallet_ids_sorted = sorted(set(fresh_insert_wallet_ids))

    for wallet_id in wallet_ids_sorted:
        # Apply the cap BEFORE doing any work so a partial run still
        # respects the invariant.
        if (
            max_aggregations is not None
            and max_aggregations > 0
            and counters["rows_written"] >= max_aggregations
        ):
            logger.info(
                "Specialist-aggregation budget exhausted (%d rows written); "
                "deferring remaining %d wallets to next run.",
                counters["rows_written"],
                len(wallet_ids_sorted) - counters["wallets_processed"],
            )
            break

        try:
            trades = _load_trades_for_wallet(db, wallet_id)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "specialist-metrics: failed to load trades for wallet_id=%s: %s",
                wallet_id,
                exc,
            )
            counters["errors"] += 1
            continue

        try:
            market_to_category = _resolve_categories_from_snapshots(db, wallet_id)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "specialist-metrics: failed to resolve categories for wallet_id=%s: %s",
                wallet_id,
                exc,
            )
            market_to_category = {}

        # Determine the wallet's source_data_timestamp from the
        # MAX(timestamp) of its trades — that is the point-in-time
        # anchor for the idempotency key.
        latest_ts: Optional[str] = None
        for t in trades:
            ts = t.get("timestamp")
            if isinstance(ts, str) and ts and (latest_ts is None or ts > latest_ts):
                latest_ts = ts

        # Wallet-level row (category_label = "" by design).
        category_trades = []
        for t in trades:
            mid = t.get("market_source_id")
            if isinstance(mid, str) and market_to_category.get(mid, ""):
                category_trades.append(t)
        # When no category resolves, category_trades is empty by
        # design — the wallet-level row is the only row written.

        unique_categories = sorted(
            {lbl for lbl in market_to_category.values() if lbl}
        )

        # Cap rows per wallet conservatively: 1 wallet-level row +
        # N category rows. We do NOT cap categories individually
        # because the union of categories is bounded by the
        # candidate_price_snapshots coverage of this wallet.

        rows_to_write: list[tuple[str, list[dict]]] = [
            ("", list(trades)),  # wallet-level row
        ]
        for cat in unique_categories:
            cat_trades = [
                t
                for t in trades
                if isinstance(t.get("market_source_id"), str)
                and market_to_category.get(t.get("market_source_id")) == cat
            ]
            rows_to_write.append((cat, cat_trades))

        for category_label, subset in rows_to_write:
            if (
                max_aggregations is not None
                and max_aggregations > 0
                and counters["rows_written"] >= max_aggregations
            ):
                break

            metrics = aggregate_specialist_metrics(
                wallet_id=wallet_id,
                category_label=category_label or None,
                all_trades_for_wallet=trades,
                category_trades_for_wallet=subset,
                now=now,
            )
            try:
                wrote = persist_wallet_specialist_aggregation(
                    db,
                    wallet_id=wallet_id,
                    category_label=category_label,
                    source_data_timestamp=latest_ts,
                    metrics=metrics,
                    now=now,
                )
                if wrote:
                    counters["rows_written"] += 1
                else:
                    counters["rows_skipped_idempotent"] += 1
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "specialist-metrics: persist failed for wallet_id=%s "
                    "category=%r: %s",
                    wallet_id,
                    category_label,
                    exc,
                )
                counters["errors"] += 1

        counters["wallets_processed"] += 1

    logger.info(
        "Specialist-aggregation: wallets_processed=%d rows_written=%d "
        "rows_skipped_idempotent=%d errors=%d",
        counters["wallets_processed"],
        counters["rows_written"],
        counters["rows_skipped_idempotent"],
        counters["errors"],
    )
    return counters