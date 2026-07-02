"""Snapshot engine for PR-3 of the recovery sequence.

The engine orchestrates a single fresh CLOB-book observation for one
``copy_candidates`` row. It is the bounded, deterministic seam between:

  * the persistence layer (``copy_candidates`` + ``markets`` + …)
  * the injected book provider (the
    :class:`polycopy.adapters.polymarket_clob.PolymarketClobClient` in
    production-like wiring, a fake / ``BidAskProvider`` adapter in tests)

…and emits one :class:`polycopy.domain.price_snapshot.PriceSnapshot`
that the persistence layer can then ``INSERT OR IGNORE``.

Public surface:

  * :func:`snapshot_one` — produce one completed ``PriceSnapshot`` for
    one candidate (does NOT persist; pass the result to
    :func:`polycopy.db.price_snapshot_persistence.persist_price_snapshot`
    if you want a row in ``candidate_price_snapshots``).
  * :func:`_compute_executable_fields` — small helper that derives
    side-aware ``executable_price`` / ``executable_side_depth`` /
    ``expected_fill_price`` / ``price_deterioration`` /
    ``mid_change`` from a normalized :class:`ClobBook` and a
    ``(side, source_trade_price)`` pair. Exposed at module level so
    tests can assert on the pure computation without a DB.

The engine does NOT call any HTTP. The book provider is injected. The
engine does NOT persist. The persistence layer is a separate module.
The engine does NOT create signals, orders, positions, or
``decision_log`` rows.

Idempotency contract:

  * Rerunning :func:`snapshot_one` with the same ``snapshot_run_id``
    on the same candidate produces the SAME ``PriceSnapshot.id`` and
    the same row content. The persistence layer's ``INSERT OR IGNORE``
    ensures the duplicate write is a no-op.
  * Rerunning with a NEW ``snapshot_run_id`` produces a NEW
    observation. The candidate is a candidate; the snapshot is the
    audit log.

Market-end metadata contract:

  * ``market_end_at`` is COPIED from ``markets.end_date`` at snapshot
    time. NULL / unparseable values are preserved verbatim
    (``market_end_at`` is the raw string; ``seconds_to_market_end`` is
    NULL when the source is NULL / unparseable).
  * Negative ``seconds_to_market_end`` values are valid audit evidence
    and are preserved.

The engine is async (matches the rest of the polycopy adapters' style
— see :class:`polycopy.adapters.polymarket.PolymarketPublicAdapter`)
and depends on the SQL sync ``Database`` for persistence. The
``snapshot_one`` function does NOT touch the DB; it is pure logic over
its inputs. The orchestrator that wires ``snapshot_one`` →
``persist_price_snapshot`` lives in the script layer (PR-3 deliberately
defers that wiring).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from polycopy.adapters.polymarket_clob import BookProvider, ClobBook
from polycopy.domain.price_snapshot import PriceSnapshot, SnapshotFetchStatus
from polycopy.engine.trade_resolution import ResolveStatus  # noqa: F401  (re-exported for tests)

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _now_iso(now: Optional[datetime] = None) -> str:
    """Return an ISO-8601 UTC string with Z suffix.

    Matches the schema's existing convention (``wallets.created_at``,
    ``decision_log.created_at``, ``source_trades.timestamp``,
    ``candidate_price_snapshots.fetched_at`` / ``created_at``).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string to a UTC ``datetime``; ``None`` on failure.

    Accepts both ``...Z`` and ``+00:00``-style timezone offsets. Naive
    timestamps are assumed UTC (matching the schema's convention). This
    helper NEVER raises — a malformed market end-date or trade
    timestamp must produce a ``None`` and let the caller decide what to
    do (PR-3 preserves NULL rather than crashing).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _trade_age_seconds_int(
    trade_timestamp: Any, snapshot_now: datetime,
) -> Optional[int]:
    """Return trade age in seconds, rounded to int; ``None`` on failure.

    Negative deltas are clamped to 0 — a snapshot cannot be taken
    "before" the trade. This is a presentation-level clamp; the
    raw ``source_trade_timestamp`` is preserved on the snapshot.
    """
    ts = _parse_iso_datetime(trade_timestamp)
    if ts is None:
        return None
    delta = (snapshot_now - ts).total_seconds()
    return max(0, int(delta))


def _seconds_to_market_end(
    market_end_value: Any, snapshot_now: datetime,
) -> Optional[int]:
    """Return integer seconds to market end; ``None`` on NULL / unparseable.

    Negative deltas are valid audit evidence and are preserved
    verbatim (per PR-3 contract §5 — Todd's correction).
    """
    dt = _parse_iso_datetime(market_end_value)
    if dt is None:
        return None
    return int((dt - snapshot_now).total_seconds())


def _safe_pct(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    """Compute ``numerator / denominator``; ``None`` when either is missing
    or the denominator is zero. PR-3 does NOT cap or rewrite the result;
    that belongs to a later formula layer.
    """
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return None
    return float(numerator) / float(denominator)


# ── Executable + deterioration computation (pure) ──────────────────────────
def _compute_executable_fields(
    *,
    side: str,
    source_trade_price: float,
    book: ClobBook,
) -> dict[str, Any]:
    """Derive side-aware executable fields from a normalized book.

    Returns a dict with:

      * ``executable_price``     — best_ask for BUY, best_bid for SELL
      * ``executable_side_depth`` — best_ask_size for BUY, best_bid_size for SELL
      * ``expected_fill_price``  — equal to executable_price in PR-3;
        depth-weighted fill simulation is a later stage
      * ``price_deterioration``  — signed, side-aware (see contract §3.3)
      * ``price_deterioration_pct`` — ``price_deterioration / source_trade_price``
      * ``mid_change``           — ``mid_price - source_trade_price``
      * ``mid_change_pct``       — ``mid_change / source_trade_price``

    Any field whose value would be missing (e.g. executable_price
    when the candidate's side is one-sided) is ``None``. The engine
    does NOT invent a value.

    The computation is side-aware:

      * BUY: positive ``price_deterioration`` = ask is higher than the
        source trade price (we'd pay more). Negative = ask is lower
        (we'd pay less).
      * SELL: positive = bid is lower than the source trade price
        (we'd receive less). Negative = bid is higher.

    ``expected_fill_price`` is set equal to ``executable_price`` in
    PR-3. The PR-3 spec deliberately does NOT perform depth-weighted
    fill simulation; that belongs to a later stage.
    """
    side_norm = (side or "").upper()
    if side_norm not in {"BUY", "SELL"}:
        raise ValueError(f"side must be BUY or SELL (got {side!r})")

    best_bid = book.best_bid
    best_ask = book.best_ask
    best_bid_size = book.best_bid_size
    best_ask_size = book.best_ask_size
    mid = book.mid_price

    if side_norm == "BUY":
        executable_price = best_ask
        executable_side_depth = best_ask_size
    else:  # SELL
        executable_price = best_bid
        executable_side_depth = best_bid_size

    # PR-3 expected fill == executable. Later stages may depth-weight.
    expected_fill_price = executable_price

    # Side-aware deterioration: positive = our copy price is worse
    # than the source trade; negative = better; zero = same.
    if executable_price is None or source_trade_price is None:
        price_deterioration = None
    elif side_norm == "BUY":
        price_deterioration = float(executable_price) - float(source_trade_price)
    else:  # SELL
        price_deterioration = float(source_trade_price) - float(executable_price)

    price_deterioration_pct = _safe_pct(price_deterioration, source_trade_price)

    # Neutral market movement (independent of side).
    if mid is None or source_trade_price is None:
        mid_change = None
    else:
        mid_change = float(mid) - float(source_trade_price)
    mid_change_pct = _safe_pct(mid_change, source_trade_price)

    return {
        "executable_price": executable_price,
        "executable_side_depth": executable_side_depth,
        "expected_fill_price": expected_fill_price,
        "price_deterioration": price_deterioration,
        "price_deterioration_pct": price_deterioration_pct,
        "mid_change": mid_change,
        "mid_change_pct": mid_change_pct,
    }


# ── Status classification helpers (pure) ───────────────────────────────────
def _classify_book_status(book: ClobBook) -> SnapshotFetchStatus:
    """Map a parsed ``ClobBook`` to a ``SnapshotFetchStatus``.

    Pre-condition: ``book.error_code is None`` (a successful 2xx parse).
    The engine does NOT call this on an errored book — those are routed
    via :func:`_classify_error_status` instead.
    """
    if book.is_empty:
        return SnapshotFetchStatus.EMPTY_BOOK
    if book.is_one_sided:
        return SnapshotFetchStatus.ONE_SIDED_BOOK
    return SnapshotFetchStatus.OK


def _classify_error_status(book: ClobBook) -> SnapshotFetchStatus:
    """Map an errored ``ClobBook`` to a ``SnapshotFetchStatus``.

    Reads only ``book.error_code`` (set by the adapter on every
    failure path). The mapping is bounded and exhaustive.
    """
    code = book.error_code
    if code in (None, "OK"):
        return SnapshotFetchStatus.OK
    if code == "HTTP_429":
        return SnapshotFetchStatus.RATE_LIMITED
    if code in ("HTTP_4XX", "HTTP_5XX", "CONNECTION_ERROR"):
        return SnapshotFetchStatus.HTTP_ERROR
    if code == "TIMEOUT":
        return SnapshotFetchStatus.TIMEOUT
    if code in (
        "PARSE_ERROR_JSON",
        "PARSE_ERROR_STRUCTURE",
        "PARSE_ERROR_NUMERIC",
        "PARSE_ERROR_PRICE_RANGE",
        "PARSE_ERROR_NEGATIVE_SIZE",
        "PARSE_ERROR_CROSSED",
    ):
        return SnapshotFetchStatus.PARSE_ERROR
    if code == "EMPTY_TOKEN":
        # EMPTY_TOKEN comes from the engine's MISSING_TOKEN check
        # (the adapter would have errored if we passed through).
        return SnapshotFetchStatus.MISSING_TOKEN
    # Unknown error code: surface as HTTP_ERROR — bounded, never invent
    # a new status. The bounded code is enforced by the enum, not by
    # this classifier.
    return SnapshotFetchStatus.HTTP_ERROR


# ── Public API ──────────────────────────────────────────────────────────────
def snapshot_one(
    db: Any,  # polycopy.db.database.Database — Any to avoid import cycle
    *,
    candidate_id: int,
    snapshot_run_id: str,
    now: Optional[datetime] = None,
    book_provider: Optional[BookProvider] = None,
    # For tests: a pre-loaded candidate row + market row + outcome row
    # bypass the DB. The engine does NOT use them when they are None.
    candidate: Optional[Any] = None,
    market: Optional[Any] = None,
    outcome: Optional[Any] = None,
) -> PriceSnapshot:
    """Produce one completed :class:`PriceSnapshot` for one candidate.

    Args:
        db: a connected :class:`polycopy.db.database.Database`. Used
            when ``candidate`` / ``market`` are not pre-loaded by the
            caller (the production path).
        candidate_id: the ``copy_candidates.id`` to snapshot.
        snapshot_run_id: the UUID for this run. ``UNIQUE(candidate_id,
            snapshot_run_id)`` — rerunning with the same id is a no-op
            at the persistence layer.
        now: override the snapshot clock (default ``datetime.now(UTC)``).
            Useful for deterministic tests.
        book_provider: an injected :class:`BookProvider`. Required when
            the candidate is eligible for a CLOB call (PENDING +
            market open + token present). Tests can pass a fake.
        candidate: optional pre-loaded candidate (a
            :class:`polycopy.domain.copy_candidate.CopyCandidate` or a
            raw DB row dict-like). When provided, the engine skips the
            ``SELECT … FROM copy_candidates`` lookup.
        market: optional pre-loaded market (a
            :class:`polycopy.domain.market.Market` or a raw DB row
            dict-like). When provided, the engine skips the
            ``SELECT … FROM markets`` lookup.
        outcome: optional pre-loaded market outcome (a
            :class:`polycopy.domain.market.MarketOutcome` or a raw DB
            row dict-like). When provided, the engine skips the
            ``SELECT … FROM market_outcomes`` lookup.

    Returns:
        A populated :class:`PriceSnapshot` whose ``id`` is a fresh
        UUIDv4. The snapshot is NOT persisted — pass it to
        :func:`polycopy.db.price_snapshot_persistence.persist_price_snapshot`
        to write the row.

    The function does NOT raise on bounded failure paths. Every
    failure path produces a completed snapshot with the bounded
    ``fetch_status`` set. The only exceptions are programmer errors
    (invalid ``side``, missing required argument).
    """
    import uuid as _uuid

    if now is None:
        now = datetime.now(timezone.utc)
    fetched_at_iso = _now_iso(now)

    # ── 1. Load candidate ────────────────────────────────────────────────
    if candidate is None:
        candidate = _load_candidate(db, candidate_id)
    if candidate is None:
        # Engine contract: produce a MISSING_TOKEN-equivalent snapshot
        # for a missing candidate. We have no source_trade fields to
        # populate, so use the minimum a snapshot requires.
        return PriceSnapshot(
            id=str(_uuid.uuid4()),
            candidate_id=candidate_id,
            snapshot_run_id=snapshot_run_id,
            fetch_status=SnapshotFetchStatus.NOT_PENDING.value,
            fetch_error_code="CANDIDATE_NOT_FOUND",
            fetch_error_message=(
                f"copy_candidates row with id={candidate_id} does not exist"
            ),
            side="BUY",
            source_trade_price=0.0,
            source_trade_quantity=0.0,
            source_trade_timestamp=fetched_at_iso,
            trade_age_seconds=None,
            fetched_at=fetched_at_iso,
            created_at=fetched_at_iso,
        )

    # Pull fields off the candidate — works for both domain objects and
    # raw sqlite3.Row-like dicts.
    cand_status = _row_get(candidate, "status", "")
    cand_side = _row_get(candidate, "side", "BUY")
    cand_source_trade_price = _row_get(candidate, "source_trade_price", 0.0)
    cand_source_trade_quantity = _row_get(candidate, "source_trade_quantity", 0.0)
    cand_source_trade_timestamp = _row_get(
        candidate, "source_trade_timestamp", fetched_at_iso,
    )
    cand_token_id = _row_get(candidate, "token_id", None)
    cand_market_id = _row_get(candidate, "market_id", None)
    cand_market_outcome_id = _row_get(candidate, "market_outcome_id", None)

    # ── 2. NOT_PENDING gate ──────────────────────────────────────────────
    if cand_status != "PENDING_PRICE_CHECK":
        return PriceSnapshot(
            id=str(_uuid.uuid4()),
            candidate_id=candidate_id,
            snapshot_run_id=snapshot_run_id,
            fetch_status=SnapshotFetchStatus.NOT_PENDING.value,
            fetch_error_code="STATUS_NOT_PENDING",
            fetch_error_message=(
                f"candidate status {cand_status!r} is not PENDING_PRICE_CHECK"
            ),
            token_id=cand_token_id,
            side=str(cand_side),
            source_trade_price=float(cand_source_trade_price),
            source_trade_quantity=float(cand_source_trade_quantity),
            source_trade_timestamp=str(cand_source_trade_timestamp),
            trade_age_seconds=_trade_age_seconds_int(
                cand_source_trade_timestamp, now,
            ),
            fetched_at=fetched_at_iso,
            created_at=fetched_at_iso,
        )

    # ── 3. MISSING_TOKEN gate (token may be NULL even when status OK) ───
    if not cand_token_id:
        return PriceSnapshot(
            id=str(_uuid.uuid4()),
            candidate_id=candidate_id,
            snapshot_run_id=snapshot_run_id,
            fetch_status=SnapshotFetchStatus.MISSING_TOKEN.value,
            fetch_error_code="TOKEN_ID_NULL",
            fetch_error_message=(
                f"candidate.market_outcome.clob_token_id is NULL "
                f"(market_outcome_id={cand_market_outcome_id})"
            ),
            token_id=None,
            side=str(cand_side),
            source_trade_price=float(cand_source_trade_price),
            source_trade_quantity=float(cand_source_trade_quantity),
            source_trade_timestamp=str(cand_source_trade_timestamp),
            trade_age_seconds=_trade_age_seconds_int(
                cand_source_trade_timestamp, now,
            ),
            fetched_at=fetched_at_iso,
            created_at=fetched_at_iso,
        )

    # ── 4. Load market + outcome (for state + end-date snapshot) ───────
    if market is None:
        market = _load_market(db, cand_market_id)
    market_active: Optional[int] = None
    market_closed: Optional[int] = None
    market_resolved: Optional[int] = None
    market_end_at_raw: Any = None
    market_metadata_fetched_at: Any = None
    if market is not None:
        market_active = int(_row_get(market, "active", 0))
        market_closed = int(_row_get(market, "closed", 0))
        market_resolved = int(_row_get(market, "resolved", 0))
        market_end_at_raw = _row_get(market, "end_date", None)
        market_metadata_fetched_at = _row_get(market, "fetched_at", None)

    # ── 5. MARKET_NOT_OPEN gate ─────────────────────────────────────────
    if market is None:
        # Missing market row — treat as NOT_OPEN so the audit is honest
        # about why we did not call CLOB.
        return PriceSnapshot(
            id=str(_uuid.uuid4()),
            candidate_id=candidate_id,
            snapshot_run_id=snapshot_run_id,
            fetch_status=SnapshotFetchStatus.MARKET_NOT_OPEN.value,
            fetch_error_code="MARKET_ROW_MISSING",
            fetch_error_message=(
                f"markets row with id={cand_market_id} does not exist"
            ),
            token_id=cand_token_id,
            side=str(cand_side),
            source_trade_price=float(cand_source_trade_price),
            source_trade_quantity=float(cand_source_trade_quantity),
            source_trade_timestamp=str(cand_source_trade_timestamp),
            trade_age_seconds=_trade_age_seconds_int(
                cand_source_trade_timestamp, now,
            ),
            market_end_at=None,
            seconds_to_market_end=None,
            market_metadata_fetched_at=None,
            market_active_at_fetch=None,
            market_closed_at_fetch=None,
            market_resolved_at_fetch=None,
            fetched_at=fetched_at_iso,
            created_at=fetched_at_iso,
        )
    if not market_active or market_closed or market_resolved:
        # market is closed / resolved / inactive
        seconds_to_end = _seconds_to_market_end(market_end_at_raw, now)
        return PriceSnapshot(
            id=str(_uuid.uuid4()),
            candidate_id=candidate_id,
            snapshot_run_id=snapshot_run_id,
            fetch_status=SnapshotFetchStatus.MARKET_NOT_OPEN.value,
            fetch_error_code="MARKET_NOT_OPEN",
            fetch_error_message=(
                f"market state at snapshot: active={market_active}, "
                f"closed={market_closed}, resolved={market_resolved}"
            ),
            token_id=cand_token_id,
            side=str(cand_side),
            source_trade_price=float(cand_source_trade_price),
            source_trade_quantity=float(cand_source_trade_quantity),
            source_trade_timestamp=str(cand_source_trade_timestamp),
            trade_age_seconds=_trade_age_seconds_int(
                cand_source_trade_timestamp, now,
            ),
            market_end_at=(
                str(market_end_at_raw) if market_end_at_raw is not None else None
            ),
            seconds_to_market_end=seconds_to_end,
            market_metadata_fetched_at=(
                str(market_metadata_fetched_at)
                if market_metadata_fetched_at is not None
                else None
            ),
            market_active_at_fetch=market_active,
            market_closed_at_fetch=market_closed,
            market_resolved_at_fetch=market_resolved,
            fetched_at=fetched_at_iso,
            created_at=fetched_at_iso,
        )

    # ── 6. Validate side + source_trade_price (programmer-error guards) ─
    if str(cand_side).upper() not in {"BUY", "SELL"}:
        raise ValueError(
            f"candidate.side must be BUY or SELL (got {cand_side!r})"
        )
    if float(cand_source_trade_price) <= 0:
        raise ValueError(
            f"candidate.source_trade_price must be > 0 "
            f"(got {cand_source_trade_price!r})"
        )

    # ── 7. Call the book provider ───────────────────────────────────────
    if book_provider is None:
        # No provider injected — surface as HTTP_ERROR so the audit
        # is honest about the missing dependency. Tests always inject
        # a provider; production wiring (deferred) will inject the
        # real PolymarketClobClient behind a clob_enabled gate.
        seconds_to_end = _seconds_to_market_end(market_end_at_raw, now)
        return PriceSnapshot(
            id=str(_uuid.uuid4()),
            candidate_id=candidate_id,
            snapshot_run_id=snapshot_run_id,
            fetch_status=SnapshotFetchStatus.HTTP_ERROR.value,
            fetch_error_code="NO_BOOK_PROVIDER",
            fetch_error_message=(
                "snapshot_one() called without an injected book_provider; "
                "no CLOB call was made"
            ),
            token_id=cand_token_id,
            side=str(cand_side),
            source_trade_price=float(cand_source_trade_price),
            source_trade_quantity=float(cand_source_trade_quantity),
            source_trade_timestamp=str(cand_source_trade_timestamp),
            trade_age_seconds=_trade_age_seconds_int(
                cand_source_trade_timestamp, now,
            ),
            market_end_at=(
                str(market_end_at_raw) if market_end_at_raw is not None else None
            ),
            seconds_to_market_end=seconds_to_end,
            market_metadata_fetched_at=(
                str(market_metadata_fetched_at)
                if market_metadata_fetched_at is not None
                else None
            ),
            market_active_at_fetch=market_active,
            market_closed_at_fetch=market_closed,
            market_resolved_at_fetch=market_resolved,
            fetched_at=fetched_at_iso,
            created_at=fetched_at_iso,
        )

    import asyncio
    book: ClobBook = asyncio.get_event_loop().run_until_complete(
        book_provider.fetch_book(cand_token_id)
    )

    # ── 8. Classify result → bounded fetch status ────────────────────────
    if book.error_code is not None:
        status = _classify_error_status(book)
    else:
        status = _classify_book_status(book)

    # ── 9. Build snapshot fields from book + market metadata ─────────────
    seconds_to_end = _seconds_to_market_end(market_end_at_raw, now)
    book_summary_json: Optional[str] = None
    book_hash: Optional[str] = None
    executable: dict[str, Any] = {}
    if status is SnapshotFetchStatus.OK:
        executable = _compute_executable_fields(
            side=str(cand_side),
            source_trade_price=float(cand_source_trade_price),
            book=book,
        )
        book_summary_json = _bounded_book_summary_json(
            book=book, side=str(cand_side),
        )
        book_hash = book.book_hash

    return PriceSnapshot(
        id=str(_uuid.uuid4()),
        candidate_id=candidate_id,
        snapshot_run_id=snapshot_run_id,
        fetch_status=status.value,
        # Bounded audit label per contract §8. NEVER the full URL
        # with the token query parameter — the token must not be
        # persisted, even as a query string. The token itself is
        # recorded separately in ``token_id`` and comes from the
        # ``market_outcomes.clob_token_id`` (or ``copy_candidates``)
        # row, which is the persistent, owned source of truth.
        fetch_endpoint="clob/book",
        fetch_http_status=book.http_status,
        fetch_latency_ms=book.latency_ms,
        request_attempts=book.request_attempts,
        fetch_error_code=book.error_code,
        fetch_error_message=book.error_message,
        token_id=cand_token_id,
        side=str(cand_side),
        source_trade_price=float(cand_source_trade_price),
        source_trade_quantity=float(cand_source_trade_quantity),
        source_trade_timestamp=str(cand_source_trade_timestamp),
        best_bid=book.best_bid if status is SnapshotFetchStatus.OK else None,
        best_bid_size=(
            book.best_bid_size if status is SnapshotFetchStatus.OK else None
        ),
        best_ask=book.best_ask if status is SnapshotFetchStatus.OK else None,
        best_ask_size=(
            book.best_ask_size if status is SnapshotFetchStatus.OK else None
        ),
        mid_price=book.mid_price if status is SnapshotFetchStatus.OK else None,
        spread=book.spread if status is SnapshotFetchStatus.OK else None,
        executable_price=executable.get("executable_price"),
        executable_side_depth=executable.get("executable_side_depth"),
        expected_fill_price=executable.get("expected_fill_price"),
        price_deterioration=executable.get("price_deterioration"),
        price_deterioration_pct=executable.get("price_deterioration_pct"),
        mid_change=executable.get("mid_change"),
        mid_change_pct=executable.get("mid_change_pct"),
        trade_age_seconds=_trade_age_seconds_int(
            cand_source_trade_timestamp, now,
        ),
        market_end_at=(
            str(market_end_at_raw) if market_end_at_raw is not None else None
        ),
        seconds_to_market_end=seconds_to_end,
        market_metadata_fetched_at=(
            str(market_metadata_fetched_at)
            if market_metadata_fetched_at is not None
            else None
        ),
        market_active_at_fetch=market_active,
        market_closed_at_fetch=market_closed,
        market_resolved_at_fetch=market_resolved,
        bid_level_count=(
            book.bid_level_count if status is SnapshotFetchStatus.OK else None
        ),
        ask_level_count=(
            book.ask_level_count if status is SnapshotFetchStatus.OK else None
        ),
        book_summary_json=book_summary_json,
        book_hash=book_hash,
        fetched_at=fetched_at_iso,
        created_at=fetched_at_iso,
    )


# ── Internal helpers ────────────────────────────────────────────────────────
def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read a field from a domain object OR a sqlite3.Row-like mapping.

    Domain objects use attribute access; sqlite3.Row uses
    ``row[key]``; dicts use ``row[key]``. PR-3 stays agnostic to which
    one the test fixture hands us.
    """
    if row is None:
        return default
    if hasattr(row, key):
        # Pydantic v2 BaseModel — prefer the attribute. Pydantic v1
        # and dataclass instances also work this way.
        return getattr(row, key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def _load_candidate(db: Any, candidate_id: int) -> Any:
    """Load a candidate row by primary key. ``None`` when missing.

    Returns whatever ``db.fetchone(...)`` returns (typically a
    ``sqlite3.Row``). The engine reads the fields it needs via
    :func:`_row_get` so the call site does not have to materialize a
    full :class:`CopyCandidate`.
    """
    return db.fetchone(
        "SELECT id, wallet_id, source, source_trade_id, "
        "source_trade_internal_id, market_id, market_outcome_id, "
        "market_source_id, token_id, outcome_label, side, "
        "source_trade_price, source_trade_quantity, "
        "source_trade_notional, source_trade_timestamp, observed_at, "
        "wallet_score_version, wallet_score, wallet_verdict, "
        "status, status_reason, created_at, updated_at "
        "FROM copy_candidates WHERE id = ?",
        (candidate_id,),
    )


def _load_market(db: Any, market_id: Optional[str]) -> Any:
    """Load a market row by primary key. ``None`` when missing."""
    if not market_id:
        return None
    return db.fetchone(
        "SELECT id, source_id, source, question, active, closed, "
        "resolved, resolution_outcome, end_date, fetched_at, volume_24h "
        "FROM markets WHERE id = ?",
        (market_id,),
    )


def _bounded_book_summary_json(*, book: ClobBook, side: str) -> str:
    """Build the bounded JSON summary required by the contract (§8).

    Schema (sorted keys, stable):

        {
          "best_bid": ...,
          "best_bid_size": ...,
          "best_ask": ...,
          "best_ask_size": ...,
          "bid_levels": ...,
          "ask_levels": ...,
          "executable_side": "ASK" or "BID"
        }
    """
    import json
    payload = {
        "best_ask": book.best_ask,
        "best_ask_size": book.best_ask_size,
        "best_bid": book.best_bid,
        "best_bid_size": book.best_bid_size,
        "bid_levels": book.bid_level_count,
        "ask_levels": book.ask_level_count,
        "executable_side": "ASK" if str(side).upper() == "BUY" else "BID",
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


__all__ = [
    "snapshot_one",
    "_compute_executable_fields",
    "_classify_book_status",
    "_classify_error_status",
]
