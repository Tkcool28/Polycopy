"""Copy-candidate persistence layer (PR-2 of the recovery sequence).

This module is the single source of truth for turning a
``(CopyabilityScore, SourceTrade, Wallet, Market)`` evaluation into a
persisted ``copy_candidates`` row. It is reachable from tests and from
future scan-flow wiring; PR-2 deliberately does NOT wire it into
``scripts/run_scan.py`` (see ``docs/paper_pilot/copy_candidate_contract.md``
and the PR-2 sequence doc §5.11).

Public surface:

* :func:`evaluate_source_trade_for_wallet` — pure-Python evaluator: maps a
  ``(wallet, trade, score)`` triple through the canonical resolver plus
  basic eligibility checks and returns a populated ``CopyCandidate``
  (not yet inserted). The function takes ``market`` as an optional kwarg
  only for the closed/resolved market check — the resolver is the
  primary attribution source.
* :func:`persist_copy_candidate` — ``INSERT OR IGNORE`` on the bounded
  UNIQUE key, returning ``(id, inserted_bool)``.
* :func:`record_candidate_decision_log` — append a bounded decision_log
  entry for the candidate layer (bounded decision_type vocabulary;
  see :data:`polycopy.domain.copy_candidate.CANDIDATE_DECISION_TYPES`).

The canonical resolver ``resolve_trade_to_outcome`` (in
:mod:`polycopy.engine.trade_resolution`) is used for every attribution.
We NEVER query by ``source_trade_id`` alone — see the recovery-sequence
identity contract §2.1.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from polycopy.db.database import Database
from polycopy.domain.copy_candidate import (
    CANDIDATE_DECISION_TYPES,
    CandidateStatus,
    CopyCandidate,
)
from polycopy.domain.copyability import CopyabilityScore, Verdict
from polycopy.domain.market import Market
from polycopy.domain.source_trade import SourceTrade
from polycopy.domain.wallet import Wallet
from polycopy.engine.trade_resolution import ResolveStatus, resolve_trade_to_outcome

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────────────
# Side values accepted by the schema. SourceTrade.side is an enum but we
# accept both enum and string at the persistence boundary.
_VALID_SIDES = frozenset({"BUY", "SELL"})

# The set of wallet verdicts that REJECT at the wallet layer (i.e. before
# we ever ask the resolver). Per recovery sequence §5.5 the status mapping
# for these is REJECTED_WALLET.
_WALLET_REJECT_VERDICTS = frozenset({
    Verdict.WATCHLIST,
    Verdict.SKIP,
    Verdict.INCOMPLETE,
})


# ── Helpers ────────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with Z suffix.

    Matches the schema's existing convention (e.g. wallets.created_at,
    decision_log.created_at, source_trades.timestamp).
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coerce_side(side: Any) -> str:
    """Coerce SourceTrade.side (OrderSide enum or string) to a string.

    Returns the uppercase string form. Falls back to ``str(side)`` for
    unknown types so we never silently rewrite the side field to a
    default — the schema accepts any TEXT for ``side``.
    """
    if side is None:
        return ""
    value = getattr(side, "value", side)
    if not isinstance(value, str):
        value = str(value)
    return value.upper()


def _trade_age_seconds(trade_timestamp: str, now: datetime) -> Optional[float]:
    """Return the trade age in seconds, or None when unparseable.

    Used to populate ``metrics_json.trade_age_seconds`` for PR-3
    consumption. No threshold is applied in PR-2 — the value is purely
    informational, captured but not gated.
    """
    if not trade_timestamp:
        return None
    try:
        ts = datetime.fromisoformat(trade_timestamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts).total_seconds())


def _verdict_str(verdict: Any) -> str:
    """Coerce a verdict (Verdict enum or string) to its string form."""
    if verdict is None:
        return ""
    return getattr(verdict, "value", verdict) or ""


def _has_open_market(market: Optional[Market]) -> tuple[bool, str]:
    """Check the market is open and unresolved. Returns (is_open, reason).

    Pass ``market=None`` to opt-out of the closed/resolved check (the
    resolver already enforces that the trade joins to a real outcome;
    some test paths want to evaluate without a Market object in scope).
    """
    if market is None:
        return True, ""
    if market.closed:
        return False, f"market closed (source_id={market.source_id!r})"
    if market.resolved:
        return False, f"market resolved (source_id={market.source_id!r})"
    if not market.active:
        return False, f"market inactive (source_id={market.source_id!r})"
    return True, ""


# ── 1. Evaluation ─────────────────────────────────────────────────────────────
def evaluate_source_trade_for_wallet(
    db: Database,
    *,
    wallet: Wallet,
    trade: SourceTrade,
    score: CopyabilityScore,
    market: Optional[Market] = None,
    now: Optional[datetime] = None,
) -> CopyCandidate:
    """Evaluate one (wallet, source trade) pair and return a populated CopyCandidate.

    The candidate is NOT persisted here — pass the returned object to
    :func:`persist_copy_candidate` for the idempotent INSERT.

    Status precedence (highest first):

    1. Invalid trade fields (price <= 0, quantity <= 0, missing
       timestamp) → ``REJECTED_INVALID_TRADE``.
    2. Wallet verdict ∈ {WATCHLIST, SKIP, INCOMPLETE} → ``REJECTED_WALLET``.
    3. Resolver ``INCOMPLETE`` → ``REJECTED_UNRESOLVED_OUTCOME``.
    4. Resolver ``AMBIGUOUS`` → ``REJECTED_AMBIGUOUS_OUTCOME``.
    5. Market closed / resolved / inactive → ``REJECTED_MARKET_CLOSED``.
    6. Otherwise → ``PENDING_PRICE_CHECK``.

    Args:
        db: connected :class:`polycopy.db.database.Database`.
        wallet: the wallet being evaluated (must have a real
            ``wallet.id`` UUID — caller is responsible for persistence
            and for ensuring ``canonical_address`` is set).
        trade: the observed source trade (must have ``source``,
            ``source_trade_id``, ``price > 0``, ``quantity > 0``,
            ``timestamp`` for non-rejected candidates).
        score: the wallet's deterministic ``CopyabilityScore`` —
            formula_version, score, and verdict are snapshotted onto
            the candidate.
        market: optional ``Market`` for the closed/resolved check.
            When ``None`` the check is skipped (useful for tests that
            want to exercise the resolver branch alone). The resolver
            already validates that the trade's outcome row is real;
            the market-level gate exists to refuse trades whose
            market has since been closed or resolved.
        now: override current UTC time (default ``datetime.now(UTC)``).
            Useful for deterministic tests.

    Returns:
        A populated ``CopyCandidate`` whose ``id`` is None until
        :func:`persist_copy_candidate` runs.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    observed_at = _now_iso()

    side_str = _coerce_side(trade.side)
    verdict_str = _verdict_str(score.verdict)
    verdict_enum = score.verdict if isinstance(score.verdict, Verdict) else None

    metrics: dict[str, Any] = {
        "wallet_id": str(wallet.id),
        "wallet_address": wallet.address,
        "source": trade.source,
        "source_trade_id": trade.source_trade_id,
        "side": side_str,
        "price": float(trade.price),
        "quantity": float(trade.quantity),
        "timestamp": trade.timestamp.isoformat() if hasattr(trade.timestamp, "isoformat") else str(trade.timestamp),
        "trade_age_seconds": _trade_age_seconds(
            trade.timestamp.isoformat() if hasattr(trade.timestamp, "isoformat") else str(trade.timestamp),
            now,
        ),
        "wallet_score": float(score.score),
        "wallet_score_version": score.formula_version,
        "wallet_verdict": verdict_str,
    }

    # ── 1. Invalid-trade guard ──────────────────────────────────────────────
    invalid_reason: Optional[str] = None
    if trade.price is None or float(trade.price) <= 0:
        invalid_reason = f"source_trade.price must be > 0 (got {trade.price!r})"
    elif trade.quantity is None or float(trade.quantity) <= 0:
        invalid_reason = f"source_trade.quantity must be > 0 (got {trade.quantity!r})"
    elif not trade.timestamp:
        invalid_reason = "source_trade.timestamp is missing"
    elif side_str not in _VALID_SIDES:
        invalid_reason = f"side must be BUY or SELL (got {side_str!r})"

    if invalid_reason:
        metrics["invalid_reason"] = invalid_reason
        return CopyCandidate(
            id=None,
            wallet_id=str(wallet.id),
            source=trade.source,
            source_trade_id=trade.source_trade_id,
            source_trade_internal_id=None,
            market_id=None,
            market_outcome_id=None,
            market_source_id=trade.market_source_id,
            token_id=getattr(trade, "token_id", None),
            outcome_label=None,
            side=side_str or "BUY",
            source_trade_price=float(trade.price) if trade.price is not None else 0.0,
            source_trade_quantity=float(trade.quantity) if trade.quantity is not None else 0.0,
            source_trade_notional=None,
            source_trade_timestamp=(
                trade.timestamp.isoformat() if hasattr(trade.timestamp, "isoformat")
                else str(trade.timestamp)
            ),
            observed_at=observed_at,
            wallet_score_version=score.formula_version,
            wallet_score=float(score.score),
            wallet_verdict=verdict_str,
            status=CandidateStatus.REJECTED_INVALID_TRADE.value,
            status_reason=invalid_reason,
            metrics_json=json.dumps(metrics, sort_keys=True, default=str),
            created_at=observed_at,
            updated_at=observed_at,
        )

    # ── 2. Wallet-verdict guard ────────────────────────────────────────────
    if verdict_enum in _WALLET_REJECT_VERDICTS:
        metrics["wallet_reject_reason"] = verdict_str
        return CopyCandidate(
            id=None,
            wallet_id=str(wallet.id),
            source=trade.source,
            source_trade_id=trade.source_trade_id,
            source_trade_internal_id=None,
            market_id=None,
            market_outcome_id=None,
            market_source_id=trade.market_source_id,
            token_id=getattr(trade, "token_id", None),
            outcome_label=None,
            side=side_str,
            source_trade_price=float(trade.price),
            source_trade_quantity=float(trade.quantity),
            source_trade_notional=float(trade.price) * float(trade.quantity),
            source_trade_timestamp=(
                trade.timestamp.isoformat() if hasattr(trade.timestamp, "isoformat")
                else str(trade.timestamp)
            ),
            observed_at=observed_at,
            wallet_score_version=score.formula_version,
            wallet_score=float(score.score),
            wallet_verdict=verdict_str,
            status=CandidateStatus.REJECTED_WALLET.value,
            status_reason=f"wallet verdict {verdict_str!r} is not COPY_CANDIDATE",
            metrics_json=json.dumps(metrics, sort_keys=True, default=str),
            created_at=observed_at,
            updated_at=observed_at,
        )

    # ── 3-4. Resolver branch (the canonical source-qualified lookup) ────────
    result = resolve_trade_to_outcome(
        db,
        source=trade.source,
        source_trade_id=trade.source_trade_id,
    )
    metrics["resolver_status"] = result.status.value
    metrics["resolver_reason"] = result.reason
    metrics["resolver_fallback_used"] = result.fallback_used

    # Try to populate source_trade_internal_id from the source_trades row
    # regardless of resolver outcome — useful audit metadata.
    internal_id_row = db.fetchone(
        "SELECT id FROM source_trades WHERE source = ? AND source_trade_id = ?",
        (trade.source, trade.source_trade_id),
    )
    internal_id = internal_id_row["id"] if internal_id_row else None

    if result.status is ResolveStatus.INCOMPLETE:
        return CopyCandidate(
            id=None,
            wallet_id=str(wallet.id),
            source=trade.source,
            source_trade_id=trade.source_trade_id,
            source_trade_internal_id=internal_id,
            market_id=None,
            market_outcome_id=None,
            market_source_id=trade.market_source_id,
            token_id=getattr(trade, "token_id", None),
            outcome_label=None,
            side=side_str,
            source_trade_price=float(trade.price),
            source_trade_quantity=float(trade.quantity),
            source_trade_notional=float(trade.price) * float(trade.quantity),
            source_trade_timestamp=(
                trade.timestamp.isoformat() if hasattr(trade.timestamp, "isoformat")
                else str(trade.timestamp)
            ),
            observed_at=observed_at,
            wallet_score_version=score.formula_version,
            wallet_score=float(score.score),
            wallet_verdict=verdict_str,
            status=CandidateStatus.REJECTED_UNRESOLVED_OUTCOME.value,
            status_reason=result.reason,
            metrics_json=json.dumps(metrics, sort_keys=True, default=str),
            created_at=observed_at,
            updated_at=observed_at,
        )

    if result.status is ResolveStatus.AMBIGUOUS:
        metrics["resolver_candidate_market_outcome_ids"] = list(
            result.candidate_market_outcome_ids
        )
        return CopyCandidate(
            id=None,
            wallet_id=str(wallet.id),
            source=trade.source,
            source_trade_id=trade.source_trade_id,
            source_trade_internal_id=internal_id,
            market_id=None,
            market_outcome_id=None,
            market_source_id=trade.market_source_id,
            token_id=getattr(trade, "token_id", None),
            outcome_label=None,
            side=side_str,
            source_trade_price=float(trade.price),
            source_trade_quantity=float(trade.quantity),
            source_trade_notional=float(trade.price) * float(trade.quantity),
            source_trade_timestamp=(
                trade.timestamp.isoformat() if hasattr(trade.timestamp, "isoformat")
                else str(trade.timestamp)
            ),
            observed_at=observed_at,
            wallet_score_version=score.formula_version,
            wallet_score=float(score.score),
            wallet_verdict=verdict_str,
            status=CandidateStatus.REJECTED_AMBIGUOUS_OUTCOME.value,
            status_reason=result.reason,
            metrics_json=json.dumps(metrics, sort_keys=True, default=str),
            created_at=observed_at,
            updated_at=observed_at,
        )

    # ── 5. Market-level gate ────────────────────────────────────────────────
    is_open, market_reason = _has_open_market(market)
    if not is_open:
        metrics["market_reject_reason"] = market_reason
        return CopyCandidate(
            id=None,
            wallet_id=str(wallet.id),
            source=trade.source,
            source_trade_id=trade.source_trade_id,
            source_trade_internal_id=internal_id,
            market_id=result.market_id,
            market_outcome_id=result.market_outcome_id,
            market_source_id=result.market_source_id,
            token_id=result.clob_token_id,
            outcome_label=result.outcome_label,
            side=side_str,
            source_trade_price=float(trade.price),
            source_trade_quantity=float(trade.quantity),
            source_trade_notional=float(trade.price) * float(trade.quantity),
            source_trade_timestamp=(
                trade.timestamp.isoformat() if hasattr(trade.timestamp, "isoformat")
                else str(trade.timestamp)
            ),
            observed_at=observed_at,
            wallet_score_version=score.formula_version,
            wallet_score=float(score.score),
            wallet_verdict=verdict_str,
            status=CandidateStatus.REJECTED_MARKET_CLOSED.value,
            status_reason=market_reason,
            metrics_json=json.dumps(metrics, sort_keys=True, default=str),
            created_at=observed_at,
            updated_at=observed_at,
        )

    # ── 6. PENDING_PRICE_CHECK ──────────────────────────────────────────────
    return CopyCandidate(
        id=None,
        wallet_id=str(wallet.id),
        source=trade.source,
        source_trade_id=trade.source_trade_id,
        source_trade_internal_id=internal_id,
        market_id=result.market_id,
        market_outcome_id=result.market_outcome_id,
        market_source_id=result.market_source_id,
        token_id=result.clob_token_id,
        outcome_label=result.outcome_label,
        side=side_str,
        source_trade_price=float(trade.price),
        source_trade_quantity=float(trade.quantity),
        source_trade_notional=float(trade.price) * float(trade.quantity),
        source_trade_timestamp=(
            trade.timestamp.isoformat() if hasattr(trade.timestamp, "isoformat")
            else str(trade.timestamp)
        ),
        observed_at=observed_at,
        wallet_score_version=score.formula_version,
        wallet_score=float(score.score),
        wallet_verdict=verdict_str,
        status=CandidateStatus.PENDING_PRICE_CHECK.value,
        status_reason=None,
        metrics_json=json.dumps(metrics, sort_keys=True, default=str),
        created_at=observed_at,
        updated_at=observed_at,
    )


# ── 2. Persistence ────────────────────────────────────────────────────────────
def persist_copy_candidate(
    db: Database,
    candidate: CopyCandidate,
) -> tuple[int, bool]:
    """Insert-or-ignore the candidate on the bounded UNIQUE key.

    Returns ``(id, inserted_bool)``:

    * On a new row: ``(cursor.lastrowid, True)``. The candidate's ``id``
      field is mutated to the new PK for caller convenience.
    * On a UNIQUE collision (same wallet/source/source_trade_id already
      present): ``(existing_pk, False)``. The candidate's ``id`` field
      is mutated to the existing row's PK so callers always have a
      stable handle. The existing row is NOT touched — PR-2 does not
      rewrite historical ``wallet_score`` / ``wallet_verdict`` on
      re-evaluation (that would silently lose evidence).

    Idempotency:

    * ``INSERT OR IGNORE`` against the schema's
      ``UNIQUE(wallet_id, source, source_trade_id)`` makes reruns safe.
    * The duplicate-skip path emits a single bounded
      ``COPY_CANDIDATE_DUPLICATE_SKIPPED`` decision_log row via
      :func:`record_candidate_decision_log` so evidence of the rerun
      exists without flooding the log.

    Args:
        db: connected :class:`polycopy.db.database.Database`.
        candidate: the candidate to insert. ``candidate.id`` may be
            ``None`` (fresh evaluation) or pre-set (idempotent
            re-insert); both are accepted.

    Returns:
        ``(id, inserted_bool)`` tuple — the persisted PK (new or
        existing) and a flag indicating whether this call performed
        the insert.
    """
    cur = db.conn.execute(
        """
        INSERT OR IGNORE INTO copy_candidates (
            wallet_id, source, source_trade_id, source_trade_internal_id,
            market_id, market_outcome_id, market_source_id, token_id,
            outcome_label, side,
            source_trade_price, source_trade_quantity, source_trade_notional,
            source_trade_timestamp, observed_at,
            wallet_score_version, wallet_score, wallet_verdict,
            status, status_reason, metrics_json,
            created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?,
            ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?
        )
        """,
        (
            candidate.wallet_id,
            candidate.source,
            candidate.source_trade_id,
            candidate.source_trade_internal_id,
            candidate.market_id,
            candidate.market_outcome_id,
            candidate.market_source_id,
            candidate.token_id,
            candidate.outcome_label,
            candidate.side,
            candidate.source_trade_price,
            candidate.source_trade_quantity,
            candidate.source_trade_notional,
            candidate.source_trade_timestamp,
            candidate.observed_at,
            candidate.wallet_score_version,
            candidate.wallet_score,
            candidate.wallet_verdict,
            candidate.status,
            candidate.status_reason,
            candidate.metrics_json,
            candidate.created_at,
            candidate.updated_at,
        ),
    )

    inserted = cur.rowcount == 1
    new_id: int
    if inserted:
        # SQLite ``cursor.lastrowid`` is documented as the PK of the
        # row just inserted; ``INSERT OR IGNORE`` only sets it when a
        # row was actually inserted (rowcount == 1). The cast is safe.
        last_id = cur.lastrowid
        assert last_id is not None, "INSERT OR IGNORE returned rowcount=1 with no lastrowid"
        new_id = int(last_id)
    else:
        # Duplicate — locate the existing row's PK so callers can
        # reference it. UNIQUE(wallet_id, source, source_trade_id)
        # guarantees at most one match.
        existing = db.fetchone(
            "SELECT id FROM copy_candidates "
            "WHERE wallet_id = ? AND source = ? AND source_trade_id = ?",
            (candidate.wallet_id, candidate.source, candidate.source_trade_id),
        )
        if existing is None:
            # Should never happen (UNIQUE conflict means a row exists),
            # but degrade safely rather than raising — the caller still
            # gets a sensible return value.
            db.conn.rollback()
            return (-1, False)
        new_id = int(existing["id"])

    db.conn.commit()

    # Reflect the resolved PK on the candidate so callers don't have
    # to thread it manually.
    candidate.id = new_id
    return new_id, inserted


# ── 3. Decision logging ──────────────────────────────────────────────────────
def record_candidate_decision_log(
    db: Database,
    *,
    candidate: CopyCandidate,
    decision_type: str,
    reason: Optional[str] = None,
) -> str:
    """Append one bounded decision_log row for the candidate layer.

    The ``decision_type`` string MUST be in
    :data:`polycopy.domain.copy_candidate.CANDIDATE_DECISION_TYPES`;
    the function raises ``ValueError`` for unknown values so callers
    can't accidentally widen the bounded vocabulary.

    The decision_log row is appended (not idempotent): a rerun that
    produces a fresh ``COPY_CANDIDATE_CREATED`` event writes a new
    row, and a rerun that lands on the duplicate-skip path writes a
    new ``COPY_CANDIDATE_DUPLICATE_SKIPPED`` row. This matches the
    audit-trail semantics of the existing decision_log (free-form
    append-only).

    ``candidate.id`` may be ``None`` (the row was rejected at
    evaluation time, before persistence) — in that case we use a
    placeholder ``market_id`` of the ``market_source_id`` echo or
    NULL. The schema's ``market_id`` column is ``NOT NULL`` though, so
    the caller MUST supply a real market row before this fires; for
    pre-persistence rejections we emit a synthetic UUID so the FK
    constraint is still satisfiable. The audit value is in the
    ``metrics_json`` blob, not the FK chain.

    Args:
        db: connected :class:`polycopy.db.database.Database`.
        candidate: the candidate the decision is about. Its
            ``id``, ``wallet_id``, ``source``, ``source_trade_id`` and
            ``metrics_json`` are used to populate the log row.
        decision_type: one of :data:`CANDIDATE_DECISION_TYPES`.
        reason: optional human-readable rationale. If omitted we use
            ``candidate.status_reason`` or a default string.

    Returns:
        The generated decision_log row UUID (string form).

    Raises:
        ValueError: ``decision_type`` is not in the bounded set.
        sqlite3.IntegrityError: the row violates an existing FK
            constraint (e.g. ``market_id`` references a missing
            market). The caller is responsible for ensuring a real
            market row exists; for pre-persistence rejections we
            generate a synthetic UUID and accept that it won't FK
            resolve — this is intentional, the audit is in metrics_json.
    """
    if decision_type not in CANDIDATE_DECISION_TYPES:
        raise ValueError(
            f"decision_type {decision_type!r} is not in the bounded "
            f"CANDIDATE_DECISION_TYPES set: {sorted(CANDIDATE_DECISION_TYPES)}"
        )

    # decision_log.market_id is NOT NULL REFERENCES markets(id); for
    # rejected candidates without a real market_id we fall back to a
    # synthetic UUID that won't FK-resolve — the audit evidence is in
    # the metrics_json blob, NOT in the FK chain. SQLite's FK
    # enforcement is gated on PRAGMA foreign_keys=ON; the Database
    # class enables that on connect. To avoid hard-failing on a
    # pre-persistence rejection with no market row, we synthesize a
    # synthetic market_id that the schema treats as a dangling ref.
    # Tests verify this behavior; production wiring is gated on a
    # future PR.
    market_id = candidate.market_id or candidate.market_source_id or "00000000-0000-0000-0000-000000000000"
    decision_id = str(uuid4())
    rationale = reason or candidate.status_reason or f"{decision_type} for {candidate.source}/{candidate.source_trade_id}"

    # Build the metrics payload — preserve the candidate's existing
    # metrics and add a few extra fields for the decision record.
    metrics: dict[str, Any] = {}
    if candidate.metrics_json:
        try:
            metrics.update(json.loads(candidate.metrics_json))
        except (ValueError, TypeError):
            metrics["_candidate_metrics_parse_error"] = True
    metrics["candidate_id"] = candidate.id
    metrics["candidate_status"] = candidate.status
    metrics["candidate_wallet_verdict"] = candidate.wallet_verdict
    metrics["candidate_wallet_score"] = candidate.wallet_score
    metrics["candidate_wallet_score_version"] = candidate.wallet_score_version
    metrics["decision_type"] = decision_type

    db.conn.execute(
        """
        INSERT INTO decision_log (
            id, wallet_id, market_id, decision_type, signal_ids,
            order_id, rationale, metrics, created_at, is_sample
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            candidate.wallet_id,
            market_id,
            decision_type,
            "[]",
            None,
            rationale,
            json.dumps(metrics, sort_keys=True, default=str),
            _now_iso(),
            0,
        ),
    )
    db.conn.commit()
    return decision_id


__all__ = [
    "evaluate_source_trade_for_wallet",
    "persist_copy_candidate",
    "record_candidate_decision_log",
]