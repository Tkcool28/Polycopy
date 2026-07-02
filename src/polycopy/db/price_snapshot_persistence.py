"""Candidate price-snapshot persistence layer — PR-3 of the recovery sequence.

This module is the single source of truth for turning a populated
:class:`polycopy.domain.price_snapshot.PriceSnapshot` into a row in
``candidate_price_snapshots``. It is reachable from tests and from
future scan-flow wiring; PR-3 deliberately does NOT wire it into
``scripts/run_scan.py`` (see
``docs/paper_pilot/candidate_price_snapshot_contract.md``).

Public surface:

* :func:`persist_price_snapshot` — ``INSERT OR IGNORE`` on the bounded
  UNIQUE key ``(candidate_id, snapshot_run_id)``, returning
  ``(snapshot_id, inserted_bool)``.
* :func:`get_latest_price_snapshot` — return the most recent
  ``PriceSnapshot`` for one candidate, or ``None``. The "most recent"
  ordering is ``ORDER BY fetched_at DESC, id DESC`` (the index
  ``idx_cps_candidate_fetched`` serves this directly).
* :func:`_row_to_snapshot` — internal: convert a sqlite3.Row to a
  :class:`PriceSnapshot`. Exposed at module level so tests can use
  it for fixture construction.

The layer does NOT create signals, orders, positions, or
``decision_log`` rows. It does NOT touch the wallet scoring formula
or its thresholds. It does NOT change broker mode, paper mode, the
kill switch, timers, Caddy, systemd, or ``.env``.

The "latest" snapshot is a QUERY, not a foreign key — there is no
``latest_price_snapshot_id`` column on ``copy_candidates`` (contract
§6.6). This module is the single place that knows how to ask the
question.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from polycopy.db.database import Database
from polycopy.domain.price_snapshot import PriceSnapshot, SnapshotFetchStatus

logger = logging.getLogger(__name__)


# ── Public API ──────────────────────────────────────────────────────────────
def persist_price_snapshot(
    db: Database, snapshot: PriceSnapshot,
) -> tuple[str, bool]:
    """INSERT OR IGNORE the snapshot into ``candidate_price_snapshots``.

    The unique key is ``(candidate_id, snapshot_run_id)``. A duplicate
    insert (same key) is a no-op — the function returns
    ``(existing_id, False)`` and does NOT update the existing row.
    History is append-only: the persistence layer never silently
    rewrites a previous observation's book or status fields.

    The function returns ``(snapshot_id, inserted_bool)``:

      * ``inserted_bool=True`` — a NEW row was inserted; the returned
        id is the snapshot's own ``.id`` attribute.
      * ``inserted_bool=False`` — a row with the same
        ``(candidate_id, snapshot_run_id)`` already existed; the
        returned id is the EXISTING row's id (looked up from the DB).

    The function commits the transaction. Caller is responsible for
    any wrapping transaction logic when batching multiple snapshots
    in a single scan.
    """
    snapshot_id = snapshot.id

    # Fast path: try the INSERT OR IGNORE.
    cur = db.execute(
        "INSERT OR IGNORE INTO candidate_price_snapshots ("
        "id, candidate_id, snapshot_run_id, fetch_status, fetch_endpoint, "
        "fetch_http_status, fetch_latency_ms, request_attempts, "
        "fetch_error_code, fetch_error_message, "
        "token_id, side, source_trade_price, source_trade_quantity, "
        "source_trade_timestamp, "
        "best_bid, best_bid_size, best_ask, best_ask_size, "
        "mid_price, spread, "
        "executable_price, executable_side_depth, expected_fill_price, "
        "price_deterioration, price_deterioration_pct, "
        "mid_change, mid_change_pct, "
        "trade_age_seconds, market_end_at, seconds_to_market_end, "
        "market_metadata_fetched_at, "
        "market_active_at_fetch, market_closed_at_fetch, "
        "market_resolved_at_fetch, "
        "bid_level_count, ask_level_count, "
        "book_summary_json, book_hash, "
        "fetched_at, created_at"
        ") VALUES ("
        "?, ?, ?, ?, ?, "
        "?, ?, ?, "
        "?, ?, "
        "?, ?, ?, ?, "
        "?, "
        "?, ?, ?, ?, "
        "?, ?, "
        "?, ?, ?, "
        "?, ?, "
        "?, ?, "
        "?, ?, ?, "
        "?, "
        "?, ?, "
        "?, "
        "?, ?, "
        "?, ?, "
        "?, ?"
        ")",
        (
            snapshot_id,
            int(snapshot.candidate_id),
            snapshot.snapshot_run_id,
            snapshot.fetch_status,
            snapshot.fetch_endpoint,
            snapshot.fetch_http_status,
            snapshot.fetch_latency_ms,
            int(snapshot.request_attempts),
            snapshot.fetch_error_code,
            snapshot.fetch_error_message,
            snapshot.token_id,
            snapshot.side,
            float(snapshot.source_trade_price),
            float(snapshot.source_trade_quantity),
            snapshot.source_trade_timestamp,
            snapshot.best_bid,
            snapshot.best_bid_size,
            snapshot.best_ask,
            snapshot.best_ask_size,
            snapshot.mid_price,
            snapshot.spread,
            snapshot.executable_price,
            snapshot.executable_side_depth,
            snapshot.expected_fill_price,
            snapshot.price_deterioration,
            snapshot.price_deterioration_pct,
            snapshot.mid_change,
            snapshot.mid_change_pct,
            snapshot.trade_age_seconds,
            snapshot.market_end_at,
            snapshot.seconds_to_market_end,
            snapshot.market_metadata_fetched_at,
            snapshot.market_active_at_fetch,
            snapshot.market_closed_at_fetch,
            snapshot.market_resolved_at_fetch,
            snapshot.bid_level_count,
            snapshot.ask_level_count,
            snapshot.book_summary_json,
            snapshot.book_hash,
            snapshot.fetched_at,
            snapshot.created_at,
        ),
    )

    # ``rowcount`` on a successful INSERT OR IGNORE is 1; on a no-op
    # duplicate-skip it is 0. We commit either way so the function
    # leaves the DB in a clean state.
    db.conn.commit()
    inserted = bool(cur.rowcount and cur.rowcount > 0)

    if inserted:
        return snapshot_id, True

    # Duplicate-skip path. Look up the existing row's id so the
    # caller can use it as a stable handle (e.g. for follow-up
    # queries or for logging). The lookup is by the unique key the
    # caller supplied — not by the snapshot's own id, which is a
    # different value.
    existing = db.fetchone(
        "SELECT id FROM candidate_price_snapshots "
        "WHERE candidate_id = ? AND snapshot_run_id = ?",
        (int(snapshot.candidate_id), snapshot.snapshot_run_id),
    )
    if existing is None:
        # Defensive: should not happen — the INSERT OR IGNORE must
        # either insert or find an existing row. Surface clearly
        # rather than silently returning the input id.
        raise RuntimeError(
            "INSERT OR IGNORE was a no-op but no existing "
            "candidate_price_snapshots row was found for "
            f"candidate_id={snapshot.candidate_id}, "
            f"snapshot_run_id={snapshot.snapshot_run_id!r}"
        )
    return str(existing["id"]), False


def get_latest_price_snapshot(
    db: Database, candidate_id: int,
) -> Optional[PriceSnapshot]:
    """Return the most-recently-fetched ``PriceSnapshot`` for the candidate.

    Ordering is ``ORDER BY fetched_at DESC, id DESC`` (matches the
    index ``idx_cps_candidate_fetched``). Returns ``None`` when the
    candidate has no snapshots yet. The function does NOT create
    anything — it is a pure read.
    """
    row = db.fetchone(
        "SELECT * FROM candidate_price_snapshots "
        "WHERE candidate_id = ? "
        "ORDER BY fetched_at DESC, id DESC "
        "LIMIT 1",
        (int(candidate_id),),
    )
    if row is None:
        return None
    return _row_to_snapshot(row)


# ── Internal: row → domain ─────────────────────────────────────────────────
def _row_to_snapshot(row: Any) -> PriceSnapshot:
    """Convert a sqlite3.Row (or dict) to a :class:`PriceSnapshot`.

    The conversion is mechanical — every persisted column maps to one
    domain field. Book-level fields default to ``None`` when the
    underlying row has them NULL (e.g. a non-OK snapshot that never
    populated them).
    """
    def _opt(key: str) -> Any:
        """Return row[key] or None — defensive for tests that build
        minimal dict-like rows without every column present.
        """
        try:
            value = row[key]
        except (KeyError, IndexError):
            return None
        return value

    return PriceSnapshot(
        id=str(_opt("id")),
        candidate_id=int(_opt("candidate_id")),
        snapshot_run_id=str(_opt("snapshot_run_id")),
        fetch_status=str(_opt("fetch_status")),
        fetch_endpoint=_opt("fetch_endpoint"),
        fetch_http_status=_opt("fetch_http_status"),
        fetch_latency_ms=_opt("fetch_latency_ms"),
        request_attempts=int(_opt("request_attempts") or 1),
        fetch_error_code=_opt("fetch_error_code"),
        fetch_error_message=_opt("fetch_error_message"),
        token_id=_opt("token_id"),
        side=str(_opt("side")),
        source_trade_price=float(_opt("source_trade_price") or 0.0),
        source_trade_quantity=float(_opt("source_trade_quantity") or 0.0),
        source_trade_timestamp=str(_opt("source_trade_timestamp")),
        best_bid=_opt("best_bid"),
        best_bid_size=_opt("best_bid_size"),
        best_ask=_opt("best_ask"),
        best_ask_size=_opt("best_ask_size"),
        mid_price=_opt("mid_price"),
        spread=_opt("spread"),
        executable_price=_opt("executable_price"),
        executable_side_depth=_opt("executable_side_depth"),
        expected_fill_price=_opt("expected_fill_price"),
        price_deterioration=_opt("price_deterioration"),
        price_deterioration_pct=_opt("price_deterioration_pct"),
        mid_change=_opt("mid_change"),
        mid_change_pct=_opt("mid_change_pct"),
        trade_age_seconds=_opt("trade_age_seconds"),
        market_end_at=_opt("market_end_at"),
        seconds_to_market_end=_opt("seconds_to_market_end"),
        market_metadata_fetched_at=_opt("market_metadata_fetched_at"),
        market_active_at_fetch=_opt("market_active_at_fetch"),
        market_closed_at_fetch=_opt("market_closed_at_fetch"),
        market_resolved_at_fetch=_opt("market_resolved_at_fetch"),
        bid_level_count=_opt("bid_level_count"),
        ask_level_count=_opt("ask_level_count"),
        book_summary_json=_opt("book_summary_json"),
        book_hash=_opt("book_hash"),
        fetched_at=str(_opt("fetched_at")),
        created_at=str(_opt("created_at")),
    )


# ── Bounded validation helpers ──────────────────────────────────────────────
def assert_snapshot_status_is_bounded(snapshot: PriceSnapshot) -> None:
    """Raise ``ValueError`` if the snapshot's status is not in the bounded set.

    Used by tests to assert that the engine never invents a new
    status. The bounded set is the :class:`SnapshotFetchStatus` enum.
    """
    try:
        snapshot.fetch_status_enum
    except ValueError as exc:
        raise ValueError(
            f"PriceSnapshot.fetch_status is not bounded: {exc}"
        ) from exc


def count_snapshots_for_run(db: Database, snapshot_run_id: str) -> int:
    """Count rows in ``candidate_price_snapshots`` for a run.

    Convenience for tests + future scan reports. Uses the
    ``idx_cps_run`` index.
    """
    row = db.fetchone(
        "SELECT COUNT(*) AS n FROM candidate_price_snapshots "
        "WHERE snapshot_run_id = ?",
        (snapshot_run_id,),
    )
    return int(row["n"]) if row else 0


def count_snapshots_by_status(
    db: Database, snapshot_run_id: str,
) -> dict[str, int]:
    """Count rows per fetch_status for a run, keyed by status string.

    Convenience for the paper-research period when the operator wants
    to know "how many candidates failed with RATE_LIMITED in this
    run?" The returned dict is keyed by the string form of
    :class:`SnapshotFetchStatus` so it can be JSON-serialized as-is.
    """
    rows = db.fetchall(
        "SELECT fetch_status, COUNT(*) AS n "
        "FROM candidate_price_snapshots "
        "WHERE snapshot_run_id = ? "
        "GROUP BY fetch_status",
        (snapshot_run_id,),
    )
    out: dict[str, int] = {s.value: 0 for s in SnapshotFetchStatus}
    for r in rows:
        out[str(r["fetch_status"])] = int(r["n"])
    return out


__all__ = [
    "persist_price_snapshot",
    "get_latest_price_snapshot",
    "assert_snapshot_status_is_bounded",
    "count_snapshots_for_run",
    "count_snapshots_by_status",
    "_row_to_snapshot",
]
