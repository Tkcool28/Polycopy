"""Copy-candidate domain model — PR-2 of the recovery sequence.

A ``CopyCandidate`` is the persisted artifact produced when a
``CopyabilityScore`` (with its deterministic verdict) is evaluated against
one specific observed source trade. It is the stable, idempotent input
that PR-3 (fresh price/spread/fill/slippage) and PR-4 (real signal
generator) consume.

PR-2 scope (this PR):

  * Persists the (wallet_id, source, source_trade_id, market_id, …) tuple.
  * Status is bounded to ``CandidateStatus`` (see below).
  * Idempotency via ``UNIQUE(wallet_id, source, source_trade_id)`` and
    ``INSERT OR IGNORE`` semantics — see
    :mod:`polycopy.db.copy_candidate_persistence`.

PR-2 explicitly EXCLUDES (these belong to PR-3+):

  * ``predicted_prob``, ``market_prob``, ``expected_value``,
    ``edge_estimate``, ``expected_fill_price``, ``spread``,
    ``estimated_slippage``, ``signal_id``, ``expires_at``,
    ``approved_at``, ``approved_by`` — no values are invented here.
  * Any fresh price refresh (PR-3).
  * Any signal generation (PR-4).
  * Any order / approval path (PR-5).

Identity contract (per the recovery sequence §2.1):

  * The schema's real uniqueness on ``copy_candidates`` is
    ``UNIQUE(wallet_id, source, source_trade_id)``. ``source_trade_id``
    is **not** globally unique — two providers can legitimately emit the
    same string under different ``source`` values.
  * The resolver ``resolve_trade_to_outcome(db, *, source, source_trade_id)``
    is the canonical source-qualified lookup (see
    :mod:`polycopy.engine.trade_resolution`); every persistence call site
    MUST go through it.

The model is a Pydantic ``BaseModel`` to match the existing domain
convention in this repo (``CopyabilityScore``, ``SourceTrade``, ``Market``,
``Wallet``, ``DecisionLogEntry``).
"""

from __future__ import annotations

import enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Candidate status (bounded set for PR-2) ────────────────────────────────────
class CandidateStatus(str, enum.Enum):
    """Bounded set of statuses a ``CopyCandidate`` may carry in PR-2.

    The status reflects the OUTCOME of evaluating a
    ``(CopyabilityScore, SourceTrade)`` pair through the canonical resolver
    plus the basic eligibility checks. PR-3 may add additional statuses
    (e.g. ``REJECTED_PRICE_DRIFT``, ``REJECTED_NO_LIQUIDITY``) once fresh
    price data is available; this PR deliberately stops at the bounded
    set below to avoid inventing values that need a refresh pipeline.

    Values:

    ``PENDING_PRICE_CHECK``
        COPY_CANDIDATE verdict + resolver OK + market active + valid
        trade fields. Awaiting fresh price/spread evaluation in PR-3.

    ``REJECTED_WALLET``
        Wallet verdict ∈ {WATCHLIST, SKIP, INCOMPLETE}. Not a candidate.

    ``REJECTED_UNRESOLVED_OUTCOME``
        Resolver returned ``INCOMPLETE`` — the source trade cannot be
        unambiguously mapped to a market outcome (no token match, no
        legacy label match).

    ``REJECTED_AMBIGUOUS_OUTCOME``
        Resolver returned ``AMBIGUOUS`` — multiple outcomes matched;
        the candidate layer refuses to pick one.

    ``REJECTED_MARKET_CLOSED``
        The market row indicates ``closed = 1`` or ``resolved = 1``.

    ``REJECTED_STALE_TRADE``
        Reserved for PR-2; only emitted if the repo defines a
        trade-recency threshold. As of PR-2 the repo does NOT define
        one (the scoring engine has RECENCY_FRESH_SECONDS / STALE but
        those are score-decay thresholds, not trade-eligibility
        thresholds). The implementation therefore records
        ``trade_age_seconds`` in ``metrics_json`` and DOES NOT emit
        this status. The value is kept in the enum so a future PR
        can introduce a threshold without changing the schema.

    ``REJECTED_INVALID_TRADE``
        source_trade.price <= 0 or quantity <= 0 or timestamp is
        missing/unparseable.
    """

    PENDING_PRICE_CHECK = "PENDING_PRICE_CHECK"
    REJECTED_WALLET = "REJECTED_WALLET"
    REJECTED_UNRESOLVED_OUTCOME = "REJECTED_UNRESOLVED_OUTCOME"
    REJECTED_AMBIGUOUS_OUTCOME = "REJECTED_AMBIGUOUS_OUTCOME"
    REJECTED_MARKET_CLOSED = "REJECTED_MARKET_CLOSED"
    REJECTED_STALE_TRADE = "REJECTED_STALE_TRADE"
    REJECTED_INVALID_TRADE = "REJECTED_INVALID_TRADE"


# ── Decision types (bounded set written to ``decision_log``) ──────────────────
# These string constants are the only ``decision_type`` values that
# :mod:`polycopy.db.copy_candidate_persistence.record_candidate_decision_log`
# will emit. They are not stored as a separate enum to keep the decision_log
# ``decision_type`` column a free-form string (matching the existing repo
# convention — see ``docs/paper_pilot/smart_wallet_signal_path_audit.md``
# §3.8) while still bounding the candidate-layer vocabulary.
CANDIDATE_DECISION_TYPES: frozenset[str] = frozenset(
    {
        "COPY_CANDIDATE_CREATED",
        "COPY_CANDIDATE_DUPLICATE_SKIPPED",
        "COPY_CANDIDATE_REJECTED_WALLET",
        "COPY_CANDIDATE_REJECTED_UNRESOLVED_OUTCOME",
        "COPY_CANDIDATE_REJECTED_AMBIGUOUS_OUTCOME",
        "COPY_CANDIDATE_REJECTED_MARKET_CLOSED",
        "COPY_CANDIDATE_REJECTED_STALE_TRADE",
        "COPY_CANDIDATE_REJECTED_INVALID_TRADE",
    }
)


# ── Domain object ─────────────────────────────────────────────────────────────
class CopyCandidate(BaseModel):
    """Persisted artifact of evaluating one (wallet, trade) pair.

    See module docstring for the bounded scope and the explicit
    out-of-scope fields (predicted_prob / market_prob / expected_value /
    edge / fill / spread / slippage / signal_id are NOT here).

    The ``id`` is None until the row is inserted; the persistence layer
    fills it from ``cursor.lastrowid`` after a successful
    ``INSERT OR IGNORE``. If the insert was a duplicate (no new row), the
    persistence layer also updates ``id`` to the existing row's PK so
    callers always have a stable handle.

    The ``created_at`` and ``updated_at`` fields are ISO-8601 UTC
    strings (matching the schema's existing convention for timestamp
    columns) and are populated at construction time. The persistence
    layer refreshes ``updated_at`` on every successful insert (and
    on the duplicate path leaves it as the existing row's value — no
    silent rewrite).
    """

    # ── Identity ────────────────────────────────────────────────────────────
    id: Optional[int] = Field(
        default=None,
        description=(
            "Auto-increment PK assigned by SQLite after a successful "
            "INSERT OR IGNORE. None until persisted. The persistence "
            "layer also writes the EXISTING row's PK here on the "
            "duplicate-skip path so callers always have a handle."
        ),
    )
    wallet_id: str = Field(
        description=(
            "wallets.id (TEXT UUID). FK to wallets(id). Always set."
        ),
    )
    source: str = Field(
        description=(
            "Upstream source name (e.g. 'polymarket_data_api'). "
            "MUST match the source_trades.source value of the underlying "
            "trade; together with source_trade_id forms the stable identity."
        ),
    )
    source_trade_id: str = Field(
        description=(
            "Upstream trade id within ``source``. Together with ``source`` "
            "forms the UNIQUE-key for copy_candidates."
        ),
    )
    source_trade_internal_id: Optional[str] = Field(
        default=None,
        description=(
            "source_trades.id (TEXT UUID). FK to source_trades(id). "
            "NULL when the underlying source_trades row could not be "
            "looked up (resolver INCOMPLETE before any row was read)."
        ),
    )

    # ── Market / outcome attribution (NULL when rejected pre-attribution) ───
    market_id: Optional[str] = Field(
        default=None,
        description="markets.id (TEXT UUID). FK to markets(id).",
    )
    market_outcome_id: Optional[int] = Field(
        default=None,
        description="market_outcomes.id (INTEGER). FK to market_outcomes(id).",
    )
    market_source_id: Optional[str] = Field(
        default=None,
        description=(
            "The upstream market identifier (markets.source_id, e.g. "
            "Polymarket conditionId). Echoed for audit even when "
            "market_id is NULL."
        ),
    )
    token_id: Optional[str] = Field(
        default=None,
        description=(
            "market_outcomes.clob_token_id for the matched outcome. "
            "NULL when the resolver used the legacy label fallback or "
            "the trade was rejected at upstream stages."
        ),
    )
    outcome_label: Optional[str] = Field(
        default=None,
        description="market_outcomes.label for the matched outcome (e.g. 'Yes').",
    )
    side: str = Field(
        description="'BUY' or 'SELL' (string form).",
    )

    # ── Trade fields (snapped at candidate-creation time) ──────────────────
    source_trade_price: float = Field(
        description=(
            "Observed trade price [0, 1] taken from source_trades.price "
            "at the time the candidate was created. NOT refreshed by PR-2."
        ),
    )
    source_trade_quantity: float = Field(
        description="Observed trade quantity taken from source_trades.quantity.",
    )
    source_trade_notional: Optional[float] = Field(
        default=None,
        description=(
            "source_trade_price * source_trade_quantity. Stored explicitly "
            "for audit; the schema also allows NULL if either side is zero."
        ),
    )
    source_trade_timestamp: str = Field(
        description=(
            "ISO-8601 UTC timestamp of the source trade. Echoed from "
            "source_trades.timestamp for queries that want to filter "
            "without joining source_trades."
        ),
    )
    observed_at: str = Field(
        description=(
            "ISO-8601 UTC timestamp at which this candidate row was created "
            "(first persistence). Mirrors created_at at insert time."
        ),
    )

    # ── Wallet-score snapshot ───────────────────────────────────────────────
    wallet_score_version: str = Field(
        description=(
            "CopyabilityScore.formula_version. Always 'v1' in PR-2; the "
            "column is kept versioned so a future scoring-formula bump "
            "can re-materialize candidates deterministically."
        ),
    )
    wallet_score: float = Field(
        ge=0.0, le=100.0,
        description="CopyabilityScore.score [0, 100].",
    )
    wallet_verdict: str = Field(
        description=(
            "CopyabilityScore.verdict.value — one of "
            "'copy_candidate' / 'watchlist' / 'skip' / 'incomplete'. "
            "Stored verbatim from CopyabilityScore."
        ),
    )

    # ── Status + audit ─────────────────────────────────────────────────────
    status: str = Field(
        description=(
            "Bounded status — see CandidateStatus. Always populated; "
            "never NULL."
        ),
    )
    status_reason: Optional[str] = Field(
        default=None,
        description=(
            "Short human-readable reason for the status. Used in "
            "decision_log.rationale and surfaced in test failure messages."
        ),
    )
    metrics_json: Optional[str] = Field(
        default=None,
        description=(
            "JSON object containing per-candidate audit metrics "
            "(resolver reason, trade_age_seconds, score components, "
            "verdict breakdown, etc.). TEXT column in the schema; "
            "NULL is allowed but discouraged for non-rejected candidates."
        ),
    )

    # ── Timestamps ──────────────────────────────────────────────────────────
    created_at: str = Field(
        description="ISO-8601 UTC timestamp at first insert.",
    )
    updated_at: str = Field(
        description=(
            "ISO-8601 UTC timestamp at last successful insert. For the "
            "duplicate-skip path this stays equal to the existing row's "
            "value — PR-2 does NOT silently rewrite history on reruns."
        ),
    )

    # ── Convenience predicates ──────────────────────────────────────────────
    @property
    def is_pending_price_check(self) -> bool:
        return self.status == CandidateStatus.PENDING_PRICE_CHECK.value

    @property
    def is_rejected(self) -> bool:
        return self.status != CandidateStatus.PENDING_PRICE_CHECK.value

    @property
    def status_enum(self) -> CandidateStatus:
        """Return the status as a CandidateStatus enum member.

        Validates the status string is in the bounded set. Used by the
        persistence layer to map a persisted row to its typed enum.
        """
        try:
            return CandidateStatus(self.status)
        except ValueError as exc:
            raise ValueError(
                f"CopyCandidate.status is not in the bounded set: {self.status!r}"
            ) from exc

    # ── Field-validation helpers ────────────────────────────────────────────
    @staticmethod
    def decision_type_for_status(status: CandidateStatus, *, created: bool) -> str:
        """Return the bounded decision_type to write to decision_log.

        ``created=True`` maps to ``COPY_CANDIDATE_CREATED`` (only for
        ``PENDING_PRICE_CHECK``); other statuses map to their bounded
        ``COPY_CANDIDATE_REJECTED_*`` form. ``created=False`` returns
        ``COPY_CANDIDATE_DUPLICATE_SKIPPED`` for the idempotent rerun path.

        This is the single source of truth for the candidate layer's
        decision_type vocabulary — keeps the bounded set documented in
        one place.
        """
        if created:
            if status is CandidateStatus.PENDING_PRICE_CHECK:
                return "COPY_CANDIDATE_CREATED"
            return f"COPY_CANDIDATE_REJECTED_{status.name.removeprefix('REJECTED_')}"
        return "COPY_CANDIDATE_DUPLICATE_SKIPPED"

    def to_metrics_dict(self) -> dict[str, Any]:
        """Return a dict view of the optional ``metrics_json`` payload.

        Caller is expected to have serialized ``metrics_json`` via
        ``json.dumps`` at construction time (the field is stored as TEXT
        to keep the persistence layer SQLite-only, no JSON1 dependency).
        This helper is for test/inspection convenience only.
        """
        if not self.metrics_json:
            return {}
        import json
        return json.loads(self.metrics_json)


__all__ = [
    "CandidateStatus",
    "CANDIDATE_DECISION_TYPES",
    "CopyCandidate",
]