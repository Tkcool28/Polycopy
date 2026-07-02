"""Bounded order-book depth-level persistence for PR 4.

Supports the ``candidate_price_snapshot_levels`` table — an append-only
child of ``candidate_price_snapshots`` storing normalized, bounded
depth levels.

Public surface:
- persist_depth_levels — persist normalized levels for a snapshot
- get_depth_levels_for_snapshot — retrieve levels for a snapshot
- get_latest_depth_levels_for_candidate — convenience lookup
- DEPTH_NOT_CAPTURED — sentinel for snapshots with no levels
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from polycopy.db.database import Database
from polycopy.scoring.depth_normalization import (
    NormalizedLevel,
    DepthWalkResult,
    normalize_book_levels,
    compute_book_hash,
    walk_depth,
    DEFAULT_MAX_LEVELS_PER_SIDE,
    DEFAULT_MAX_NOTIONAL_PER_SIDE,
    DEPTH_NOT_CAPTURED,
    DEPTH_INSUFFICIENT_FOR_STAKE,
    DEPTH_LEVELS_MALFORMED,
    DEPTH_SNAPSHOT_MISMATCH,
)

logger = logging.getLogger(__name__)


# ── Public API ──────────────────────────────────────────────────────────────

def persist_depth_levels(
    db: Database,
    snapshot_id: str,
    raw_bids: list[tuple],
    raw_asks: list[tuple],
    *,
    max_levels: int = DEFAULT_MAX_LEVELS_PER_SIDE,
    max_notional: Decimal = Decimal(str(DEFAULT_MAX_NOTIONAL_PER_SIDE)),
) -> tuple[int, int, Optional[str]]:
    """Normalize and persist order-book depth levels for a snapshot.

    Steps:
    1. Normalize raw bids/asks
    2. Check for crossed books or malformed data
    3. INSERT OR IGNORE each level
    4. Return (bid_count, ask_count, error_reason)

    Existing levels for the same snapshot are left untouched
    (INSERT OR IGNORE).
    """
    bids, asks, error = normalize_book_levels(
        raw_bids, raw_asks, max_levels, max_notional,
    )
    if error:
        return 0, 0, error

    now = datetime.now(timezone.utc).isoformat()
    bid_count = 0
    ask_count = 0

    for idx, level in enumerate(bids):
        try:
            db.execute(
                """INSERT OR IGNORE INTO candidate_price_snapshot_levels
                   (snapshot_id, side, level_index, price, size,
                    cumulative_size, cumulative_notional, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    "BID",
                    idx,
                    float(level.price),
                    float(level.size),
                    float(level.cumulative_size),
                    float(level.cumulative_notional),
                    now,
                ),
            )
            bid_count += 1
        except Exception as e:
            logger.warning("Failed to persist bid level %d: %s", idx, e)

    for idx, level in enumerate(asks):
        try:
            db.execute(
                """INSERT OR IGNORE INTO candidate_price_snapshot_levels
                   (snapshot_id, side, level_index, price, size,
                    cumulative_size, cumulative_notional, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    "ASK",
                    idx,
                    float(level.price),
                    float(level.size),
                    float(level.cumulative_size),
                    float(level.cumulative_notional),
                    now,
                ),
            )
            ask_count += 1
        except Exception as e:
            logger.warning("Failed to persist ask level %d: %s", idx, e)

    db.conn.commit()
    return bid_count, ask_count, None


def get_depth_levels_for_snapshot(
    db: Database,
    snapshot_id: str,
) -> tuple[list[NormalizedLevel], list[NormalizedLevel]]:
    """Retrieve normalized depth levels for a snapshot.

    Returns (bids, asks) ordered by (side, level_index).
    If no levels exist, returns empty lists.
    """
    rows = db.fetchall(
        "SELECT side, level_index, price, size, cumulative_size, cumulative_notional "
        "FROM candidate_price_snapshot_levels "
        "WHERE snapshot_id = ? "
        "ORDER BY side, level_index",
        (snapshot_id,),
    )

    bids: list[NormalizedLevel] = []
    asks: list[NormalizedLevel] = []

    for row in rows:
        level = NormalizedLevel(
            price=Decimal(str(row["price"])),
            size=Decimal(str(row["size"])),
            cumulative_size=Decimal(str(row["cumulative_size"])),
            cumulative_notional=Decimal(str(row["cumulative_notional"])),
        )
        if row["side"] == "BID":
            bids.append(level)
        else:
            asks.append(level)

    return bids, asks


def has_snapshot_levels(db: Database, snapshot_id: str) -> bool:
    """Check whether a snapshot has depth levels stored."""
    row = db.fetchone(
        "SELECT COUNT(*) AS n FROM candidate_price_snapshot_levels "
        "WHERE snapshot_id = ?",
        (snapshot_id,),
    )
    return bool(row and row["n"] > 0)


def get_latest_depth_levels_for_candidate(
    db: Database,
    candidate_id: int,
) -> tuple[Optional[str], list[NormalizedLevel], list[NormalizedLevel], Optional[str]]:
    """Get the latest snapshot ID + depth levels for a candidate.

    Returns (snapshot_id, bids, asks, error_reason).
    If no snapshot exists or no levels exist, error_reason is set.
    """
    row = db.fetchone(
        "SELECT id FROM candidate_price_snapshots "
        "WHERE candidate_id = ? AND fetch_status = 'OK' "
        "ORDER BY fetched_at DESC, id DESC LIMIT 1",
        (int(candidate_id),),
    )
    if row is None:
        return None, [], [], DEPTH_NOT_CAPTURED

    snapshot_id = str(row["id"])
    bids, asks = get_depth_levels_for_snapshot(db, snapshot_id)

    if not bids and not asks:
        return snapshot_id, [], [], DEPTH_NOT_CAPTURED

    return snapshot_id, bids, asks, None


def compute_trade_fill_from_depth(
    db: Database,
    candidate_id: int,
    side: str,
    intended_notional: Decimal,
) -> tuple[Optional[DepthWalkResult], Optional[str]]:
    """Compute trade fill feasibility from the latest depth levels.

    Returns (walk_result, error_reason). If depth is insufficient or
    missing, a DepthWalkResult with is_complete=False is returned
    with the appropriate reason.
    """
    snapshot_id, bids, asks, error = get_latest_depth_levels_for_candidate(
        db, candidate_id,
    )

    if error:
        return None, error
    if snapshot_id is None:
        return None, DEPTH_NOT_CAPTURED

    if side == "BUY":
        result = walk_depth(asks, "BUY", intended_notional)
    else:
        result = walk_depth(bids, "SELL", intended_notional)

    return result, None


__all__ = [
    "persist_depth_levels",
    "get_depth_levels_for_snapshot",
    "has_snapshot_levels",
    "get_latest_depth_levels_for_candidate",
    "compute_trade_fill_from_depth",
    "DEPTH_NOT_CAPTURED",
    "DEPTH_INSUFFICIENT_FOR_STAKE",
    "DEPTH_LEVELS_MALFORMED",
    "DEPTH_SNAPSHOT_MISMATCH",
]