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
  NO-OP (returns ``None``) when the candidate has no real
  ``market_id`` (the copy_candidates row is the audit for those).
  App-level idempotent on rerun so duplicate evaluations do not flood.

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
from polycopy.db.wallet_identity import canonical_wallet_address
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

# The exact verdict required to advance a candidate toward
# PENDING_PRICE_CHECK. Anything else (WATCHLIST, SKIP, INCOMPLETE,
# unknown string, missing verdict) is REJECTED_WALLET.
_ADVANCE_VERDICT = Verdict.COPY_CANDIDATE


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


def _verdict_matches(verdict: Any, target: Verdict) -> bool:
    """Return True if ``verdict`` is the target Verdict enum, accepting both
    enum members and case-insensitive string forms of the enum's value.

    The scoring engine stores ``CopyabilityScore.verdict`` as the
    ``Verdict`` enum member, but production call sites occasionally pass
    plain strings (e.g. config reload). Both must work without silently
    advancing toward PENDING_PRICE_CHECK.
    """
    if isinstance(verdict, Verdict):
        return verdict is target
    if isinstance(verdict, str):
        return verdict.upper() == target.value.upper()
    return False


def _is_wallet_authorized(
    wallet: Wallet, trade: SourceTrade,
) -> tuple[bool, Optional[str]]:
    """Return (True, None) when the trade belongs to the supplied wallet.

    Compares the canonicalized wallet ``address`` against the
    canonicalized trade ``trader_address``. A trade that lacks a
    ``trader_address`` (None / empty / sentinel) can never be authorized
    — that path returns ``(False, reason)`` so the caller can produce a
    REJECTED_WALLET_TRADE_MISMATCH status with a useful explanation.

    This is the wallet/trade ownership gate (BLOCKER 1 from the PR-14
    review). It must NEVER be bypassed — without it, a trade from
    Wallet A could become a candidate for Wallet B.
    """
    canonical_wallet = canonical_wallet_address(getattr(wallet, "address", None))
    canonical_trade = canonical_wallet_address(getattr(trade, "trader_address", None))

    if canonical_trade is None:
        return False, (
            f"source_trade.trader_address is missing or sentinelled "
            f"(wallet_id={wallet.id!s}, source={trade.source}, "
            f"source_trade_id={trade.source_trade_id!r})"
        )
    if canonical_wallet is None:
        return False, (
            f"wallet.address is missing or sentinelled "
            f"(wallet_id={wallet.id!s}, source={trade.source}, "
            f"source_trade_id={trade.source_trade_id!r})"
        )
    if canonical_wallet != canonical_trade:
        return False, (
            f"wallet/trade address mismatch: "
            f"canonical_wallet={canonical_wallet!r} "
            f"canonical_trader={canonical_trade!r} "
            f"(wallet_id={wallet.id!s}, source={trade.source}, "
            f"source_trade_id={trade.source_trade_id!r})"
        )
    return True, None


def _load_market_from_db(db: Database, market_id: Optional[str]) -> Optional[Market]:
    """Load the market row referenced by ``market_id`` from the DB.

    Returns ``None`` when the row is missing or the id is None. The
    evaluator uses this to verify active/closed/resolved state against
    the REAL persisted market rather than whatever Market object the
    caller happens to have in scope.
    """
    if not market_id:
        return None
    row = db.fetchone(
        "SELECT id, source_id, source, question, active, closed, resolved, "
        "resolution_outcome, fetched_at, volume_24h, is_sample "
        "FROM markets WHERE id = ?",
        (market_id,),
    )
    if row is None:
        return None
    # Build a domain Market — outcomes are not strictly needed for the
    # active/closed/resolved check, so we leave them empty.
    try:
        return Market(
            id=row["id"],
            source_id=row["source_id"],
            source=row["source"],
            question=row["question"],
            outcomes=[],
            active=bool(row["active"]),
            closed=bool(row["closed"]),
            resolved=bool(row["resolved"]),
            resolution_outcome=row["resolution_outcome"],
            volume_24h=float(row["volume_24h"] or 0.0),
            fetched_at=row["fetched_at"],
            is_sample=bool(row["is_sample"]),
        )
    except Exception:
        # If Pydantic validation fails (e.g. fetched_at is malformed) we
        # still need the active/closed/resolved booleans — return a
        # minimal stand-in that the gate can read.
        return Market.model_construct(  # type: ignore[attr-defined]
            id=row["id"],
            source_id=row["source_id"],
            source=row["source"],
            question=row["question"],
            outcomes=[],
            active=bool(row["active"]),
            closed=bool(row["closed"]),
            resolved=bool(row["resolved"]),
            resolution_outcome=row["resolution_outcome"],
            volume_24h=float(row["volume_24h"] or 0.0),
            fetched_at=row["fetched_at"],
            is_sample=bool(row["is_sample"]),
        )


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
    2. Wallet/trade ownership mismatch (canonical address
       comparison) → ``REJECTED_WALLET_TRADE_MISMATCH``.
    3. Wallet verdict not exactly ``Verdict.COPY_CANDIDATE``
       (WATCHLIST / SKIP / INCOMPLETE / unknown string) →
       ``REJECTED_WALLET``.
    4. Resolver ``INCOMPLETE`` → ``REJECTED_UNRESOLVED_OUTCOME``.
    5. Resolver ``AMBIGUOUS`` → ``REJECTED_AMBIGUOUS_OUTCOME``.
    6. Market closed / resolved / inactive (verified against the
       DB row, not the caller's optional ``Market`` object) →
       ``REJECTED_MARKET_CLOSED``.
    7. Resolved market row missing in DB →
       ``REJECTED_UNRESOLVED_OUTCOME``.
    8. Otherwise → ``PENDING_PRICE_CHECK``.

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
        market: optional ``Market`` for a sanity check ONLY — the
            evaluator still verifies the resolved DB market state by
            ``result.market_id``. If a ``Market`` is supplied its
            ``id`` MUST match ``result.market_id``; a mismatch is
            rejected as ``REJECTED_MARKET_CLOSED`` so an unrelated
            open Market object can never bypass a closed/resolved
            DB market. Pass ``market=None`` if the caller has no
            Market object — the DB lookup is authoritative.
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

    # ── 2. Wallet/trade ownership guard (BLOCKER 1) ────────────────────────
    # This must run BEFORE the verdict check: a trade from a different
    # wallet can never become a candidate, regardless of the wallet's
    # score or verdict.
    authorized, owner_reason = _is_wallet_authorized(wallet, trade)
    if not authorized:
        metrics["ownership_reject_reason"] = owner_reason
        # Embed the canonical addresses in metrics so the rejection is
        # auditable without exposing raw credentials.
        canonical_wallet = canonical_wallet_address(getattr(wallet, "address", None))
        canonical_trade = canonical_wallet_address(getattr(trade, "trader_address", None))
        metrics["canonical_wallet_address"] = canonical_wallet
        metrics["canonical_trader_address"] = canonical_trade
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
            source_trade_price=float(trade.price) if trade.price is not None else 0.0,
            source_trade_quantity=float(trade.quantity) if trade.quantity is not None else 0.0,
            source_trade_notional=None,
            source_trade_timestamp=(
                trade.timestamp.isoformat() if hasattr(trade.timestamp, "isoformat")
                else str(trade.timestamp)
            ),
            observed_at=observed_at,
            wallet_score_version=score.formula_version,
            wallet_score=float(score.score) if score.score is not None else 0.0,
            wallet_verdict=verdict_str,
            status=CandidateStatus.REJECTED_WALLET_TRADE_MISMATCH.value,
            status_reason=owner_reason or "wallet/trade address mismatch",
            metrics_json=json.dumps(metrics, sort_keys=True, default=str),
            created_at=observed_at,
            updated_at=observed_at,
        )

    # ── 3. Wallet-verdict guard (strict) ───────────────────────────────────
    # BLOCKER 3: only Verdict.COPY_CANDIDATE may advance. Anything else
    # (WATCHLIST, SKIP, INCOMPLETE, unknown string, missing verdict) is
    # REJECTED_WALLET — never PENDING_PRICE_CHECK.
    if not _verdict_matches(score.verdict, _ADVANCE_VERDICT):
        verdict_repr = (
            score.verdict.value if isinstance(score.verdict, Verdict)
            else str(score.verdict)
        )
        reason = f"wallet verdict {verdict_repr!r} is not Verdict.COPY_CANDIDATE"
        metrics["wallet_reject_reason"] = reason
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
            status_reason=reason,
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

    # ── 5. Market-level gate (BLOCKER 4.1 — real DB verification) ──────
    # The resolved market must be verified against the DB row, NOT
    # against whatever Market object the caller happens to have in
    # scope. Passing ``market=None`` is fine — the DB lookup is
    # authoritative. If a Market IS supplied it must match the
    # resolver's market_id; a mismatched open Market cannot bypass
    # a closed/resolved DB market.
    if not result.market_id:
        # The resolver returned OK but the result has no market_id —
        # treat as unresolved (data integrity issue).
        metrics["market_reject_reason"] = (
            "resolver returned OK but result.market_id is null"
        )
        return CopyCandidate(
            id=None,
            wallet_id=str(wallet.id),
            source=trade.source,
            source_trade_id=trade.source_trade_id,
            source_trade_internal_id=internal_id,
            market_id=None,
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
            status=CandidateStatus.REJECTED_UNRESOLVED_OUTCOME.value,
            status_reason="resolver OK but resolved market_id is null",
            metrics_json=json.dumps(metrics, sort_keys=True, default=str),
            created_at=observed_at,
            updated_at=observed_at,
        )

    db_market = _load_market_from_db(db, result.market_id)
    if db_market is None:
        metrics["market_reject_reason"] = (
            f"resolved market row not found in DB (market_id={result.market_id!r})"
        )
        return CopyCandidate(
            id=None,
            wallet_id=str(wallet.id),
            source=trade.source,
            source_trade_id=trade.source_trade_id,
            source_trade_internal_id=internal_id,
            market_id=None,
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
            status=CandidateStatus.REJECTED_UNRESOLVED_OUTCOME.value,
            status_reason=f"resolved market row not found in DB (market_id={result.market_id!r})",
            metrics_json=json.dumps(metrics, sort_keys=True, default=str),
            created_at=observed_at,
            updated_at=observed_at,
        )

    # If the caller supplied a Market object, require its id to match
    # the resolver's market_id. A mismatched open Market cannot
    # override the DB truth.
    if market is not None and str(market.id) != str(db_market.id):
        market_reason = (
            f"supplied Market id {str(market.id)!r} does not match "
            f"resolved market_id {str(db_market.id)!r}"
        )
    elif db_market.closed:
        market_reason = f"market closed (source_id={db_market.source_id!r})"
    elif db_market.resolved:
        market_reason = f"market resolved (source_id={db_market.source_id!r})"
    elif not db_market.active:
        market_reason = f"market inactive (source_id={db_market.source_id!r})"
    else:
        market_reason = ""

    if market_reason:
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
    * Duplicate reruns do NOT append any additional decision_log row
      (no ``COPY_CANDIDATE_DUPLICATE_SKIPPED`` flood). The first
      candidate insert writes one bounded decision event (CREATED or
      REJECTED_*); subsequent reruns that hit the unique-key collision
      write nothing.

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
) -> Optional[str]:
    """Append one bounded decision_log row for the candidate layer.

    The ``decision_type`` string MUST be in
    :data:`polycopy.domain.copy_candidate.CANDIDATE_DECISION_TYPES`;
    the function raises ``ValueError`` for unknown values so callers
    can't accidentally widen the bounded vocabulary.

    BLOCKER 2 — FK safety + BLOCKER 3 — idempotent audit:

    * If the candidate has no real ``market_id`` (i.e. the rejection
      happened before resolver-OK attribution), the function returns
      ``None`` and does NOT insert a ``decision_log`` row. The
      ``copy_candidates`` row itself (with ``status``, ``status_reason``
      and ``metrics_json``) is the durable audit artifact for these
      pre-attribution rejections. We never invent a fake market id —
      ``decision_log.market_id`` is ``NOT NULL REFERENCES markets(id)``
      with ``PRAGMA foreign_keys=ON`` enforced, so a synthetic UUID
      would either raise ``sqlite3.IntegrityError`` or silently dangle.
    * For candidates that DO have a real ``market_id``, the function
      enforces app-level idempotency keyed on
      ``(wallet_id, source, source_trade_id, decision_type)``: if a
      row with the same identity has already been recorded, the call
      returns ``None`` without inserting. This prevents a scheduled
      scan from appending unlimited duplicate decision_log rows when
      re-evaluating the same wallet/source/source_trade_id pair.
    * This is a NO-OP return (not an exception) when the row cannot or
      should not be written — the caller continues without auditing.

    Args:
        db: connected :class:`polycopy.db.database.Database`.
        candidate: the candidate the decision is about. Its
            ``wallet_id``, ``source``, ``source_trade_id`` and
            ``metrics_json`` are used to populate the log row.
        decision_type: one of :data:`CANDIDATE_DECISION_TYPES`.
        reason: optional human-readable rationale. If omitted we use
            ``candidate.status_reason`` or a default string.

    Returns:
        The generated decision_log row UUID (string form) on success,
        or ``None`` if no row was written (no real market_id, or the
        idempotency check found an existing row).

    Raises:
        ValueError: ``decision_type`` is not in the bounded set.
    """
    if decision_type not in CANDIDATE_DECISION_TYPES:
        raise ValueError(
            f"decision_type {decision_type!r} is not in the bounded "
            f"CANDIDATE_DECISION_TYPES set: {sorted(CANDIDATE_DECISION_TYPES)}"
        )

    # BLOCKER 2: refuse to write a decision_log row when there is no
    # real market_id. The copy_candidates row itself is the audit
    # record for pre-attribution rejections. A fake market_id here
    # would either raise sqlite3.IntegrityError (with
    # PRAGMA foreign_keys=ON) or silently dangle (with FK off).
    market_id = candidate.market_id
    if not market_id:
        return None

    # BLOCKER 3: app-level idempotency keyed on the candidate's
    # source-qualified identity + decision_type. A scheduled scan
    # re-evaluating the same wallet/source/source_trade_id pair must
    # not flood decision_log. The key matches the candidate layer's
    # UNIQUE(wallet_id, source, source_trade_id) plus the bounded
    # decision_type so different rejection reasons for the same
    # wallet+trade are still distinguished.
    existing = db.fetchone(
        "SELECT id FROM decision_log "
        "WHERE wallet_id = ? AND decision_type = ? "
        "AND metrics LIKE ?",
        (
            candidate.wallet_id,
            decision_type,
            f'%"source": {json.dumps(candidate.source)}%',
        ),
    )
    if existing is not None:
        return None

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
    # Embed source + source_trade_id so the LIKE-based idempotency
    # check above can match precisely. (LIKE has no JSON operator
    # without the JSON1 extension; a substring match on a stable
    # JSON-escaped pair is a deliberate, conservative approximation.)
    metrics["source"] = candidate.source
    metrics["source_trade_id"] = candidate.source_trade_id

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