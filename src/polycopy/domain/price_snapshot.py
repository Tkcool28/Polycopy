"""Candidate price-snapshot domain model — PR-3 of the recovery sequence.

A ``PriceSnapshot`` is the persisted artifact produced by one invocation of
``polycopy.engine.price_snapshots.snapshot_one`` against one
``copy_candidates`` row. It is the durable, idempotent, append-only record
of a fresh CLOB /book observation, plus every derived field the eventual
Trade Copyability Score (PR 4+) needs.

PR-3 scope (this PR):

  * Persists the (candidate_id, snapshot_run_id, …) tuple plus the
    side-aware executable price, executable-side depth, side-aware
    price deterioration, mid-price change, and the trade / market /
    time metadata at snapshot time.
  * Status is bounded to ``SnapshotFetchStatus`` (see below). One row
    per (candidate, run) pair; re-running with the same ``snapshot_run_id``
    is a no-op (INSERT OR IGNORE).
  * Idempotency via ``UNIQUE(candidate_id, snapshot_run_id)`` and
    ``INSERT OR IGNORE`` semantics — see
    :mod:`polycopy.db.price_snapshot_persistence`.

PR-3 explicitly EXCLUDES (these belong to PR-4+):

  * ``copyability_score`` — the final score that the Trade Copyability
    Score formula will produce.
  * ``expected_value``, ``edge_estimate``, ``model_probability``,
    ``signal_id``, ``order_id``, ``approval_state`` — no values are
    invented here.
  * Any change to the wallet scoring formula, its thresholds, or its
    verdict boundaries.
  * Any change to ``_generate_signals`` in ``scripts/run_scan.py``.

Snapshot semantics:

  * The ``id`` is a UUIDv4 string assigned at construction time. The
    persistence layer does not regenerate it on duplicate-skip.
  * The ``candidate_id`` is the FK to ``copy_candidates.id``. The
    candidate's ``side`` and source-trade fields are snapshotted
    verbatim — later edits to the candidate row do not rewrite the
    snapshot.
  * The ``fetched_at`` field is the UTC ISO-8601 timestamp captured at
    the start of the snapshot. The ``created_at`` field is the
    persistence-side insert time (also UTC ISO-8601).
  * The market-end metadata is COPIED from ``markets.end_date`` at
    snapshot time so the snapshot is self-contained; later market
    metadata edits do not rewrite the historical observation.
  * Negative ``seconds_to_market_end`` values are valid audit evidence
    and are preserved.
  * The price-deterioration / mid-change percentages are raw (not
    capped) — any capping for display or scoring belongs in a later
    formula layer.

The model is a Pydantic ``BaseModel`` to match the existing domain
convention in this repo (``CopyabilityScore``, ``CopyCandidate``,
``SourceTrade``, ``Market``, ``Wallet``, ``DecisionLogEntry``,
``PriceSnapshot``).
"""

from __future__ import annotations

import enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Fetch status (bounded set for PR-3) ───────────────────────────────────────
class SnapshotFetchStatus(str, enum.Enum):
    """Bounded set of fetch statuses a ``PriceSnapshot`` may carry in PR-3.

    Each status corresponds to one of the deterministic outcomes of
    :func:`polycopy.engine.price_snapshots.snapshot_one`. The status is
    the primary axis a downstream audit (or PR 4) uses to decide whether
    a snapshot has a usable book (and therefore an executable price and
    deterioration) or is a bounded-reason NO-OP that must not be retried
    in the same run.

    Values:

    ``OK``
        Both best bid and best ask are present and the candidate's
        required executable side (ASK for BUY, BID for SELL) is
        populated. The snapshot has a full set of book fields.

    ``EMPTY_BOOK``
        The CLOB response carried no valid bid levels and no valid ask
        levels. The snapshot is recorded as bounded evidence; no
        executable price is available.

    ``ONE_SIDED_BOOK``
        The CLOB response carried bids XOR asks, OR the candidate's
        required executable side is missing. The snapshot is recorded
        as bounded evidence; no executable price is available for the
        candidate's side.

    ``MISSING_TOKEN``
        The candidate's market outcome row lacks a ``clob_token_id``.
        No CLOB call was made; the snapshot is recorded as bounded
        evidence.

    ``NOT_PENDING``
        The candidate's status is not ``PENDING_PRICE_CHECK``. No CLOB
        call was made; the snapshot is recorded as bounded evidence.

    ``MARKET_NOT_OPEN``
        The persisted market row was closed, resolved, or inactive at
        snapshot time. No CLOB call was made; the snapshot is recorded
        as bounded evidence.

    ``RATE_LIMITED``
        The CLOB endpoint returned HTTP 429 (rate limited). No usable
        book was obtained. The snapshot is recorded as bounded evidence.

    ``HTTP_ERROR``
        The CLOB endpoint returned a non-429 HTTP error (4xx/5xx other
        than 429) after the retry ceiling was exhausted. The snapshot
        is recorded as bounded evidence.

    ``TIMEOUT``
        The CLOB endpoint timed out after the retry ceiling was
        exhausted. The snapshot is recorded as bounded evidence.

    ``PARSE_ERROR``
        The CLOB response was structurally invalid (crossed book,
        malformed numeric values, NaN, infinity, etc.). The snapshot is
        recorded as bounded evidence.

    PR-3 does NOT add a ``PENDING`` / ``RETRYING`` status. A snapshot is
    always a completed observation — either a usable book or a bounded
    reason. Retries with the same ``snapshot_run_id`` are a no-op; a
    retry with a NEW ``snapshot_run_id`` creates a NEW observation.
    """

    OK = "OK"
    EMPTY_BOOK = "EMPTY_BOOK"
    ONE_SIDED_BOOK = "ONE_SIDED_BOOK"
    MISSING_TOKEN = "MISSING_TOKEN"
    NOT_PENDING = "NOT_PENDING"
    MARKET_NOT_OPEN = "MARKET_NOT_OPEN"
    RATE_LIMITED = "RATE_LIMITED"
    HTTP_ERROR = "HTTP_ERROR"
    TIMEOUT = "TIMEOUT"
    PARSE_ERROR = "PARSE_ERROR"


# ── Domain object ─────────────────────────────────────────────────────────────
class PriceSnapshot(BaseModel):
    """Persisted artifact of one fresh CLOB observation for a candidate.

    See module docstring for the bounded scope and the explicit
    out-of-scope fields (copyability_score / expected_value / edge /
    model_probability / signal_id / order_id / approval_state are NOT
    here).

    The ``id`` is a UUIDv4 string assigned at construction time. The
    persistence layer does not regenerate it on duplicate-skip — the
    first-insert id is the canonical handle for the observation.

    The ``fetched_at`` and ``created_at`` fields are ISO-8601 UTC
    strings matching the schema's existing convention for timestamp
    columns. ``fetched_at`` is captured at the start of
    ``snapshot_one``; ``created_at`` is the persistence-side insert
    time. For a non-OK snapshot both timestamps are still populated so
    the row can be ordered and audited even when the book itself is
    unavailable.
    """

    # ── Identity ────────────────────────────────────────────────────────────
    id: str = Field(
        description=(
            "UUIDv4 PK. Assigned at construction time and preserved "
            "verbatim through persistence (including the duplicate-skip "
            "path)."
        ),
    )
    candidate_id: int = Field(
        description=(
            "copy_candidates.id (INTEGER PK). FK to copy_candidates(id). "
            "Always set; the snapshot cannot exist without a candidate."
        ),
    )

    # ── Run identity + fetch provenance ─────────────────────────────────────
    snapshot_run_id: str = Field(
        description=(
            "UUIDv4 chosen by the caller of snapshot_one. UNIQUE with "
            "candidate_id; rerunning with the same id is a no-op."
        ),
    )
    fetch_status: str = Field(
        description="Bounded status — see SnapshotFetchStatus. Always populated.",
    )
    fetch_endpoint: Optional[str] = Field(
        default=None,
        description=(
            "Bounded audit label for the endpoint the CLOB call was issued "
            "against (e.g. ``'clob/book'``). NEVER the full URL with the token "
            "query parameter — the token is recorded separately in ``token_id`` "
            "from the persistent source of truth (market_outcomes / "
            "copy_candidates). NULL when no CLOB call was made (NOT_PENDING, "
            "MISSING_TOKEN, MARKET_NOT_OPEN, missing candidate)."
        ),
    )
    fetch_http_status: Optional[int] = Field(
        default=None,
        description="Last HTTP status code observed; NULL when no CLOB call was made.",
    )
    fetch_latency_ms: Optional[int] = Field(
        default=None,
        description="Total wall-clock latency of the CLOB /book attempt(s), ms.",
    )
    request_attempts: int = Field(
        default=1,
        ge=1,
        description="Total outbound HTTP request attempts (1 + retries).",
    )
    fetch_error_code: Optional[str] = Field(
        default=None,
        description=(
            "Short machine-readable error code (e.g. 'crossed_book', "
            "'negative_price'). NULL for OK snapshots."
        ),
    )
    fetch_error_message: Optional[str] = Field(
        default=None,
        description="Longer human-readable error message. NULL for OK snapshots.",
    )

    # ── Token + candidate-side identity (snapshotted) ─────────────────────
    token_id: Optional[str] = Field(
        default=None,
        description=(
            "market_outcomes.clob_token_id snapshotted from the candidate "
            "row. NULL when MISSING_TOKEN / NOT_PENDING / MARKET_NOT_OPEN."
        ),
    )
    side: str = Field(
        description="'BUY' or 'SELL' (string form), snapshotted from the candidate row.",
    )
    source_trade_price: float = Field(
        description="Observed trade price [0, 1] from the candidate row.",
    )
    source_trade_quantity: float = Field(
        description="Observed trade quantity from the candidate row.",
    )
    source_trade_timestamp: str = Field(
        description="ISO-8601 UTC trade timestamp from the candidate row.",
    )

    # ── Normalized book values (NULL for non-OK snapshots) ─────────────────
    best_bid: Optional[float] = Field(
        default=None, description="Highest valid bid price. NULL for non-OK snapshots.",
    )
    best_bid_size: Optional[float] = Field(
        default=None,
        description="Aggregated size at best_bid. NULL for non-OK snapshots.",
    )
    best_ask: Optional[float] = Field(
        default=None, description="Lowest valid ask price. NULL for non-OK snapshots.",
    )
    best_ask_size: Optional[float] = Field(
        default=None,
        description="Aggregated size at best_ask. NULL for non-OK snapshots.",
    )
    mid_price: Optional[float] = Field(
        default=None,
        description="(best_bid + best_ask) / 2. NULL for non-OK snapshots.",
    )
    spread: Optional[float] = Field(
        default=None,
        description="best_ask - best_bid. NULL for non-OK snapshots.",
    )

    # ── Executable values (side-aware) ─────────────────────────────────────
    executable_price: Optional[float] = Field(
        default=None,
        description=(
            "Side-aware executable price: best_ask for BUY, best_bid for "
            "SELL. NULL for non-OK snapshots."
        ),
    )
    executable_side_depth: Optional[float] = Field(
        default=None,
        description=(
            "Side-aware depth at the executable price: best_ask_size for "
            "BUY, best_bid_size for SELL. NULL for non-OK snapshots."
        ),
    )
    expected_fill_price: Optional[float] = Field(
        default=None,
        description=(
            "Deterministic expected fill price (PR-3 sets it equal to "
            "executable_price; depth-weighted fill simulation belongs to a "
            "later stage). NULL for non-OK snapshots."
        ),
    )

    # ── Deterioration (raw, uncapped) ─────────────────────────────────────
    price_deterioration: Optional[float] = Field(
        default=None,
        description=(
            "Side-aware: positive = our available copy price is worse; "
            "zero = same; negative = better. BUY: executable_price - "
            "source_trade_price. SELL: source_trade_price - "
            "executable_price. NULL for non-OK snapshots."
        ),
    )
    price_deterioration_pct: Optional[float] = Field(
        default=None,
        description=(
            "price_deterioration / source_trade_price. Raw (not capped); "
            "any capping for display or scoring belongs in a later "
            "formula layer. NULL for non-OK snapshots."
        ),
    )
    mid_change: Optional[float] = Field(
        default=None,
        description=(
            "mid_price - source_trade_price. Neutral market movement "
            "reference (independent of side). NULL for non-OK snapshots."
        ),
    )
    mid_change_pct: Optional[float] = Field(
        default=None,
        description=(
            "mid_change / source_trade_price. Raw. NULL for non-OK snapshots."
        ),
    )

    # ── Time values ────────────────────────────────────────────────────────
    trade_age_seconds: Optional[int] = Field(
        default=None,
        description=(
            "Seconds between source_trade_timestamp and snapshot_now. "
            "Always populated (even for non-OK snapshots) — it does not "
            "depend on the book."
        ),
    )
    market_end_at: Optional[str] = Field(
        default=None,
        description=(
            "ISO-8601 UTC market end timestamp COPIED from markets.end_date "
            "at snapshot time. NULL if markets.end_date is NULL/unparseable. "
            "This is the declared metadata end, NOT a guaranteed execution "
            "cutoff."
        ),
    )
    seconds_to_market_end: Optional[int] = Field(
        default=None,
        description=(
            "Integer seconds between snapshot_now and market_end_at. NULL "
            "when market_end_at is NULL/unparseable. Negative values are "
            "valid audit evidence and are preserved."
        ),
    )
    market_metadata_fetched_at: Optional[str] = Field(
        default=None,
        description=(
            "ISO-8601 UTC timestamp from markets.fetched_at — when the "
            "persisted market row was last ingested. Copied into the "
            "snapshot at snapshot time so the snapshot records how fresh "
            "the underlying market metadata was."
        ),
    )

    # ── Market-state snapshot ──────────────────────────────────────────────
    market_active_at_fetch: Optional[int] = Field(
        default=None,
        description="markets.active at snapshot time. 0/1. NULL when the market is missing.",
    )
    market_closed_at_fetch: Optional[int] = Field(
        default=None,
        description="markets.closed at snapshot time. 0/1. NULL when the market is missing.",
    )
    market_resolved_at_fetch: Optional[int] = Field(
        default=None,
        description="markets.resolved at snapshot time. 0/1. NULL when the market is missing.",
    )

    # ── Book-level summary + audit hash ───────────────────────────────────
    bid_level_count: Optional[int] = Field(
        default=None,
        description="Count of valid bid levels used in the calculation. NULL for non-OK.",
    )
    ask_level_count: Optional[int] = Field(
        default=None,
        description="Count of valid ask levels used in the calculation. NULL for non-OK.",
    )
    book_summary_json: Optional[str] = Field(
        default=None,
        description=(
            "Bounded JSON summary of the book (best_bid, best_bid_size, "
            "best_ask, best_ask_size, bid_levels, ask_levels, "
            "executable_side). NULL for non-OK snapshots."
        ),
    )
    book_hash: Optional[str] = Field(
        default=None,
        description=(
            "SHA-256 hex digest of a canonical, sorted, stable-serialized "
            "view of all valid price levels used for the calculation. "
            "Audit-comparison only. NULL for non-OK snapshots."
        ),
    )

    # ── Timestamps ──────────────────────────────────────────────────────────
    fetched_at: str = Field(
        description=(
            "ISO-8601 UTC timestamp at which the snapshot began (the "
            "``snapshot_now`` captured at the start of ``snapshot_one``). "
            "Always populated."
        ),
    )
    created_at: str = Field(
        description="ISO-8601 UTC timestamp at which the row was inserted.",
    )

    # ── Convenience predicates ──────────────────────────────────────────────
    @property
    def is_ok(self) -> bool:
        return self.fetch_status == SnapshotFetchStatus.OK.value

    @property
    def fetch_status_enum(self) -> SnapshotFetchStatus:
        """Return the status as a SnapshotFetchStatus enum member.

        Validates the status string is in the bounded set. Used by the
        persistence layer to map a persisted row to its typed enum.
        """
        try:
            return SnapshotFetchStatus(self.fetch_status)
        except ValueError as exc:
            raise ValueError(
                f"PriceSnapshot.fetch_status is not in the bounded set: "
                f"{self.fetch_status!r}"
            ) from exc

    def to_metrics_dict(self) -> dict[str, Any]:
        """Return a dict view of the snapshot's key fields for tests / audit.

        Convenience helper that summarizes the snapshot's identity, status,
        book fields, and time fields. Used by tests and by the eventual
        scoring / dashboard layer. NOT a serialization — booleans stay
        booleans, None stays None.
        """
        return {
            "id": self.id,
            "candidate_id": self.candidate_id,
            "snapshot_run_id": self.snapshot_run_id,
            "fetch_status": self.fetch_status,
            "fetch_http_status": self.fetch_http_status,
            "request_attempts": self.request_attempts,
            "token_id": self.token_id,
            "side": self.side,
            "source_trade_price": self.source_trade_price,
            "source_trade_quantity": self.source_trade_quantity,
            "best_bid": self.best_bid,
            "best_bid_size": self.best_bid_size,
            "best_ask": self.best_ask,
            "best_ask_size": self.best_ask_size,
            "mid_price": self.mid_price,
            "spread": self.spread,
            "executable_price": self.executable_price,
            "executable_side_depth": self.executable_side_depth,
            "expected_fill_price": self.expected_fill_price,
            "price_deterioration": self.price_deterioration,
            "price_deterioration_pct": self.price_deterioration_pct,
            "mid_change": self.mid_change,
            "mid_change_pct": self.mid_change_pct,
            "trade_age_seconds": self.trade_age_seconds,
            "market_end_at": self.market_end_at,
            "seconds_to_market_end": self.seconds_to_market_end,
            "fetched_at": self.fetched_at,
        }


__all__ = ["SnapshotFetchStatus", "PriceSnapshot"]
