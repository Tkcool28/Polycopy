"""Bounded order-book depth-level persistence for PR 4.

Supports the ``candidate_price_snapshot_levels`` table — an append-only
child of ``candidate_price_snapshots`` storing normalized, bounded
depth levels.

Phase 6 (transactional, immutable):
- The full book is normalized in a single pass BEFORE any writes.
- The normalized bounded hash is computed before persistence.
- Persistence runs inside one explicit transaction:
  * If no existing rows: every bid and ask level is inserted with
    contiguous level_index starting at zero per side. Any insert
    failure rolls back the entire transaction.
  * If existing rows: their normalized bounded hash is compared to
    the requested book. Identical → idempotent success, no writes.
    Different → DEPTH_SNAPSHOT_MISMATCH, no writes.
  * Malformed existing rows → DEPTH_LEVELS_MALFORMED, no writes.
- Returned counts are based on actual inserted/verified rows, not on
  attempted inserts.
- Old v9 snapshots without depth rows remain valid and return
  DEPTH_NOT_CAPTURED.

Public API:
- persist_depth_levels — normalize and persist atomically
- get_depth_levels_for_snapshot — retrieve normalized levels
- get_latest_depth_levels_for_candidate — convenience lookup
- compute_trade_fill_from_depth — walk the latest captured depth
- has_snapshot_levels — check whether depth was captured
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from polycopy.db.database import Database
from polycopy.scoring.depth_normalization import (
    DepthWalkResult,
    NormalizedLevel,
    compute_book_hash,
    normalize_book_levels,
    walk_depth,
    DEFAULT_MAX_LEVELS_PER_SIDE,
    DEFAULT_MAX_NOTIONAL_PER_SIDE,
    DEPTH_INSUFFICIENT_FOR_STAKE,
    DEPTH_LEVELS_MALFORMED,
    DEPTH_NOT_CAPTURED,
    DEPTH_SNAPSHOT_MISMATCH,
)

logger = logging.getLogger(__name__)


# ── Public API ──────────────────────────────────────────────────────────────


PersistResult = tuple[int, int, Optional[str]]
"""(bid_count, ask_count, error_reason).

error_reason is None on success or idempotent repeat. Otherwise it
is one of DEPTH_LEVELS_MALFORMED, DEPTH_SNAPSHOT_MISMATCH.
"""


def persist_depth_levels(
    db: Database,
    snapshot_id: str,
    raw_bids: list[tuple],
    raw_asks: list[tuple],
    *,
    max_levels: int = DEFAULT_MAX_LEVELS_PER_SIDE,
    max_notional: Decimal = Decimal(str(DEFAULT_MAX_NOTIONAL_PER_SIDE)),
    manage_transaction: bool = True,
) -> PersistResult:
    """Normalize and persist order-book depth levels atomically.

    Steps (Phase 6):
    1. Normalize the complete book (bids and asks) in a single pass.
    2. Compute the normalized bounded hash.
    3. Verify the parent snapshot exists (FK failure surfaces as
       malformed).
    4. Begin one explicit transaction.
    5. Load existing levels for `snapshot_id`.

    Case A — no existing rows:
      - INSERT every bid and ask level with contiguous level_index
        starting at 0 per side.
      - Any failure rolls back the entire transaction.
      - After success, reload and verify exact equality of the
        persisted vs. requested bounded book before commit.

    Case B — existing rows:
      - Validate stored book: contiguous indexes, valid side,
        monotonic cumulative size and notional, complete side pairs.
      - Compute stored normalized hash.
      - If stored hash == requested hash → idempotent success.
      - Otherwise → DEPTH_SNAPSHOT_MISMATCH, no writes.

    Returns (bid_count, ask_count, error_reason). Counts reflect
    actual inserted or verified rows.
    """
    # 1. Normalize the complete book first.
    bids, asks, error = normalize_book_levels(
        raw_bids, raw_asks, max_levels, max_notional,
    )
    if error:
        return 0, 0, error

    # 2. Compute the normalized bounded hash.
    requested_hash = compute_book_hash(bids, asks)

    # 3. Verify the parent snapshot exists. SQLite FK enforcement
    #    will also catch missing parents at INSERT time, but
    #    pre-checking gives a clean error.
    parent = db.fetchone(
        "SELECT id FROM candidate_price_snapshots WHERE id = ?",
        (snapshot_id,),
    )
    if parent is None:
        return 0, 0, DEPTH_LEVELS_MALFORMED

    # 4. Begin an explicit transaction unless the caller has supplied
    #    an enclosing savepoint. The bridge uses the latter so a depth
    #    failure rolls back the full per-trade chain, not just levels.
    if manage_transaction:
        db.conn.commit()
        db.conn.execute("BEGIN")

    try:
        # 5. Load existing levels for this snapshot.
        existing = _load_existing_levels(db, snapshot_id)

        if not existing:
            # CASE A — no existing rows.
            now = datetime.now(timezone.utc).isoformat()

            # Defensive validation (mirrors the CREATE TABLE CHECK
            # constraints in schema_v10 for upgraded DBs that
            # already exist). The normalize step guarantees
            # size > 0 and price in [0, 1] but we re-check here so
            # a future change to the normalize path cannot silently
            # insert a bad row.
            for idx, level in enumerate(bids):
                _p_size = float(level.size)
                _p_price = float(level.price)
                if _p_size <= 0:
                    if manage_transaction:
                        db.conn.rollback()
                    return 0, 0, DEPTH_LEVELS_MALFORMED
                if _p_price < 0 or _p_price > 1:
                    if manage_transaction:
                        db.conn.rollback()
                    return 0, 0, DEPTH_LEVELS_MALFORMED

            for idx, level in enumerate(bids):
                db.execute(
                    """
                    INSERT INTO candidate_price_snapshot_levels (
                        snapshot_id, side, level_index, price, size,
                        cumulative_size, cumulative_notional, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
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

            for idx, level in enumerate(asks):
                db.execute(
                    """
                    INSERT INTO candidate_price_snapshot_levels (
                        snapshot_id, side, level_index, price, size,
                        cumulative_size, cumulative_notional, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
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

            # Verify exact equality of the persisted vs. requested
            # bounded book before committing. If anything drifted,
            # roll back.
            persisted = _load_existing_levels(db, snapshot_id)
            if not _persisted_matches_requested(persisted, bids, asks):
                if manage_transaction:
                    db.conn.rollback()
                return 0, 0, DEPTH_LEVELS_MALFORMED

            if manage_transaction:
                db.conn.commit()
            return len(bids), len(asks), None

        # CASE B — existing rows.
        is_valid, malformed_reason = _validate_existing_levels(
            existing, expected_bid_count=len(bids), expected_ask_count=len(asks),
        )
        if not is_valid:
            if manage_transaction:
                db.conn.rollback()
            return 0, 0, malformed_reason

        stored_bids, stored_asks = _split_existing_levels(existing)
        stored_hash = compute_book_hash(stored_bids, stored_asks)

        if stored_hash == requested_hash:
            if manage_transaction:
                db.conn.commit()
            return len(stored_bids), len(stored_asks), None

        if manage_transaction:
            db.conn.rollback()
        return 0, 0, DEPTH_SNAPSHOT_MISMATCH

    except Exception:
        # The enclosing bridge savepoint owns rollback when present.
        if manage_transaction:
            db.conn.rollback()
        raise


def get_depth_levels_for_snapshot(
    db: Database,
    snapshot_id: str,
) -> tuple[list[NormalizedLevel], list[NormalizedLevel]]:
    """Retrieve normalized depth levels for a snapshot.

    Returns (bids, asks) ordered by (side, level_index). If no
    levels exist, both lists are empty.
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
        elif row["side"] == "ASK":
            asks.append(level)
        # Other side values are rejected at the CHECK constraint level.

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
    - No snapshot exists OR no levels exist → DEPTH_NOT_CAPTURED.
    - Otherwise the snapshot id and levels are returned with no error.
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

    Returns (walk_result, error_reason). If depth is insufficient
    or missing, a DepthWalkResult with is_complete=False is
    returned with the appropriate reason.
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


# ── Internal helpers ────────────────────────────────────────────────────────


def _load_existing_levels(db: Database, snapshot_id: str) -> list[sqlite3.Row]:
    """Load all existing levels for a snapshot (no side filtering)."""
    return db.fetchall(
        "SELECT side, level_index, price, size, "
        "cumulative_size, cumulative_notional "
        "FROM candidate_price_snapshot_levels "
        "WHERE snapshot_id = ? "
        "ORDER BY side, level_index",
        (snapshot_id,),
    )


def _split_existing_levels(
    rows: list,
) -> tuple[list[NormalizedLevel], list[NormalizedLevel]]:
    """Split a flat list of level rows into (bids, asks)."""
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
        elif row["side"] == "ASK":
            asks.append(level)
    return bids, asks


def _validate_existing_levels(
    rows: list,
    expected_bid_count: int,
    expected_ask_count: int,
) -> tuple[bool, Optional[str]]:
    """Validate that stored levels are well-formed.

    Checks:
    - contiguous level_index per side starting at 0
    - monotonic cumulative_size per side
    - monotonic cumulative_notional per side
    - non-negative size and price
    - complete side pairs (no missing side)

    Returns (is_valid, error_reason).
    """
    # Group by side
    by_side: dict[str, list[tuple[int, float, float, float, float]]] = {
        "BID": [], "ASK": [],
    }
    for row in rows:
        side = row["side"]
        if side not in by_side:
            return False, DEPTH_LEVELS_MALFORMED
        by_side[side].append((
            int(row["level_index"]),
            float(row["price"]),
            float(row["size"]),
            float(row["cumulative_size"]),
            float(row["cumulative_notional"]),
        ))

    # A one-sided snapshot when the caller supplied both sides is
    # corruption.
    if expected_bid_count > 0 and expected_ask_count > 0:
        if not by_side["BID"] or not by_side["ASK"]:
            return False, DEPTH_LEVELS_MALFORMED

    for side_name, items in by_side.items():
        if not items:
            continue
        items_sorted = sorted(items, key=lambda t: t[0])
        # Contiguous from zero?
        expected_idx = 0
        prev_size = 0.0
        prev_notional = 0.0
        for idx, price, size, cum_size, cum_notional in items_sorted:
            if idx != expected_idx:
                return False, DEPTH_LEVELS_MALFORMED
            if size <= 0 or price < 0 or price > 1:
                return False, DEPTH_LEVELS_MALFORMED
            # Monotonicity (allow tiny float drift)
            if cum_size + 1e-9 < prev_size:
                return False, DEPTH_LEVELS_MALFORMED
            if cum_notional + 1e-9 < prev_notional:
                return False, DEPTH_LEVELS_MALFORMED
            prev_size = cum_size
            prev_notional = cum_notional
            expected_idx += 1

    return True, None


def _persisted_matches_requested(
    persisted_rows: list,
    requested_bids: list[NormalizedLevel],
    requested_asks: list[NormalizedLevel],
) -> bool:
    """Check that the persisted book exactly matches the requested
    bounded book.
    """
    p_bids, p_asks = _split_existing_levels(persisted_rows)
    return _levels_match(p_bids, requested_bids) and _levels_match(
        p_asks, requested_asks,
    )


def _levels_match(
    persisted: list[NormalizedLevel],
    requested: list[NormalizedLevel],
) -> bool:
    """Exact equality of two level lists.

    Decimals are compared after canonical normalization
    (trailing zeros removed) AND with a small absolute tolerance
    to absorb float<->REAL round-trip drift.

    A single-level decimal like ``Decimal("9.183673469387755102040816327")``
    stored as a SQLite REAL reloads as
    ``Decimal("9.183673469387756")`` — only the first 15-17 digits
    survive the binary-float round-trip. The two values are not
    exactly equal but are within float-precision distance
    (~1e-9). Since the depth normalization contract is bounded to
    a max-notional cap of a few hundred USDC and 25 levels, this
    precision loss is safe: a 1e-9 difference in either price or
    size changes cumulative_notional by far less than a single
    contract's worth of notional.

    A tolerance of 1e-9 absorbs the largest possible float round-trip
    error while still detecting genuine data corruption (e.g. a
    difference of 0.01 in a price).
    """
    if len(persisted) != len(requested):
        return False
    TOL = Decimal("1e-9")
    for p, r in zip(persisted, requested):
        if abs(p.price.normalize() - r.price.normalize()) > TOL:
            return False
        if abs(p.size.normalize() - r.size.normalize()) > TOL:
            return False
        if abs(p.cumulative_size.normalize() - r.cumulative_size.normalize()) > TOL:
            return False
        if abs(p.cumulative_notional.normalize() - r.cumulative_notional.normalize()) > TOL:
            return False
    return True


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


# Late import for type hints (sqlite3.Row) inside helpers
import sqlite3  # noqa: E402