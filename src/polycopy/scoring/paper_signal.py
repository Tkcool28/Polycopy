"""Paper signal generation module for PR 4.

Consumes persisted candidates and fresh price snapshots.
Calculates and persists v1 scores and v2 shadow output.
Emits paper-only unapproved signals for qualified candidates.

This module replaces the placeholder _generate_signals in run_scan.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from polycopy.db.database import Database
from polycopy.db.copy_candidate_persistence import CandidateStatus
from polycopy.db.price_snapshot_persistence import (
    get_latest_price_snapshot as get_latest_snapshot_for_candidate,
)
from polycopy.scoring.behavior_classification import (
    BehaviorClassificationResult,
    classify_wallet_behavior,
    load_behavior_evidence as _load_behavior_evidence_for_wallet,
)
from polycopy.scoring.wallet_score_v1 import (
    WalletScoreResult,
    compute_wallet_score_v1,
)
from polycopy.scoring.trade_score_v1 import (
    TradeScoreResult,
    compute_trade_score_v1,
)
from polycopy.scoring.shadow_score_v2 import (
    ShadowScoreResult,
    compute_shadow_score_v2,
)
from polycopy.scoring.verdict_generation import (
    SignalVerdict,
    SignalDecisionInput,
    generate_signal_verdict,
)
from polycopy.scoring.score_serialization import (
    persist_wallet_score_v1,
    persist_trade_score_v1,
    persist_shadow_score_v2,
    persist_paper_signal,
    record_exit_experiments,
)

logger = logging.getLogger(__name__)


# ---- Category decision loader (Task 3.7) -------------------------------


CATEGORY_FORMULA_VERSION = "1"


@dataclass(frozen=True)
class PersistedCategoryDecision:
    """Narrow typed result of loading a persisted
    ``category_wallet_score_decisions`` row.

    Carries the canonical fields the paper-signal decision
    engine needs:

      - score (0-100)
      - verdict: copy_candidate | watchlist | skip | incomplete
      - category_label: never None
      - source_data_timestamp: point-in-time identity (may be None
        for legacy rows)
      - decision_id: row id, for audit

    Constructed only by :func:`load_persisted_category_decision`.
    """

    decision_id: int
    wallet_id: str
    category_label: str
    score: float
    verdict: str
    source_data_timestamp: Optional[str]


def load_persisted_category_decision(
    db: Database,
    wallet_id: str,
    category_label: Optional[str],
) -> Optional[PersistedCategoryDecision]:
    """Load the latest ``category_wallet_score_decisions`` row
    for ``(wallet_id, category_label, formula_version="1")``.

    Filtering by ``category_label`` is mandatory — the function
    MUST NOT return a "latest" decision for some other category.
    A missing or empty ``category_label`` returns ``None`` (the
    caller treats this as INCOMPLETE).

    The "latest" row is the row with the largest
    ``source_data_timestamp``; ties are broken by ``id DESC`` so
    a more recently inserted row wins.
    """
    if not category_label or not category_label.strip():
        return None

    row = db.fetchone(
        """
        SELECT id, wallet_id, category_label, final_score, verdict,
               source_data_timestamp
        FROM category_wallet_score_decisions
        WHERE wallet_id = ? AND category_label = ? AND formula_name = ?
          AND formula_version = ?
        ORDER BY COALESCE(source_data_timestamp, '') DESC, id DESC
        LIMIT 1
        """,
        (wallet_id, category_label, "category_wallet_score",
         CATEGORY_FORMULA_VERSION),
    )
    if row is None:
        return None
    return PersistedCategoryDecision(
        decision_id=int(row["id"]),
        wallet_id=str(row["wallet_id"]),
        category_label=str(row["category_label"]),
        score=float(row["final_score"]) if row["final_score"] is not None else 0.0,
        verdict=str(row["verdict"]),
        source_data_timestamp=(
            str(row["source_data_timestamp"])
            if row["source_data_timestamp"] is not None
            else None
        ),
    )


# ---- Category label resolution (Task 3.7) ------------------------------


def resolve_category_label(
    db: Database,
    candidate_row: dict,
    snapshot_row: Optional[dict],
) -> Optional[str]:
    """Resolve the category_label for a candidate.

    The runtime path may need a category label before any
    category score has been computed. The label is taken from
    the snapshot's ``book_summary_json`` field if present
    (canonical key ``category_label``), otherwise from the
    candidate's ``market_outcome_id`` lookup, otherwise None.

    The function never invents a label. It returns None if no
    label is available; the caller treats None as INCOMPLETE.
    """
    if snapshot_row is not None:
        summary = snapshot_row.get("book_summary_json")
        if isinstance(summary, str) and summary:
            import json as _json
            try:
                parsed = _json.loads(summary)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                label = parsed.get("category_label")
                if isinstance(label, str) and label.strip():
                    return label.strip()

    outcome_id = candidate_row.get("market_outcome_id") if hasattr(candidate_row, "get") else None
    if outcome_id is None and "market_outcome_id" in candidate_row.keys():
        outcome_id = candidate_row["market_outcome_id"]
    if outcome_id is not None:
        try:
            row = db.fetchone(
                "SELECT m.id, m.question FROM market_outcomes mo "
                "JOIN markets m ON m.id = mo.market_id "
                "WHERE mo.id = ?",
                (outcome_id,),
            )
        except Exception:
            row = None
        if row is not None:
            # Use the market id as a stable category label
            # surrogate; the persisted category decision will
            # be queried with this exact label.
            market_id_value = row["id"] if "id" in row.keys() else None
            if market_id_value:
                label = f"market:{market_id_value}"
                return label

    return None


def _load_snapshot_metrics(snapshot: Optional[dict]) -> dict:
    """Extract metrics from price snapshot for trade scoring."""
    if snapshot is None:
        return {}

    return {
        "best_bid": snapshot.get("best_bid"),
        "best_bid_size": snapshot.get("best_bid_size"),
        "best_ask": snapshot.get("best_ask"),
        "best_ask_size": snapshot.get("best_ask_size"),
        "spread": snapshot.get("spread"),
        "trade_age_seconds": snapshot.get("trade_age_seconds"),
        "seconds_to_market_end": snapshot.get("seconds_to_market_end"),
        "market_active": bool(snapshot.get("market_active_at_fetch")),
        "market_closed": bool(snapshot.get("market_closed_at_fetch")),
        "market_resolved": bool(snapshot.get("market_resolved_at_fetch")),
    }


# ---- Verdict integration (Task 3.7) ------------------------------------


def _build_category_inputs(
    db: Database,
    candidate_row: dict,
    snapshot_row: Optional[dict],
) -> tuple[Optional[float], Optional[str]]:
    """Return (category_score, category_verdict) for use in
    :class:`SignalDecisionInput`.

    Either both are non-None (a persisted decision exists) or
    both are None (no label / no decision). The signal engine
    maps None → INCOMPLETE.
    """
    label = resolve_category_label(db, candidate_row, snapshot_row)
    if label is None:
        return None, None
    persisted = load_persisted_category_decision(db, candidate_row["wallet_id"], label)
    if persisted is None:
        return None, None
    return persisted.score, persisted.verdict


# ---- Pure decision function (Task 3.7) ---------------------------------


def generate_paper_signal_decision(
    *,
    wallet_score_result: WalletScoreResult,
    trade_score_result: TradeScoreResult,
    behavior_result: BehaviorClassificationResult,
    category_score: Optional[float],
    category_verdict: Optional[str],
    shadow_result: Optional[ShadowScoreResult],
    has_hard_exclusion: bool = False,
    hard_exclusion_reason: Optional[str] = None,
) -> SignalVerdict:
    """Pure function that builds a SignalDecisionInput from typed
    scoring outputs and returns the canonical verdict.

    This is the single decision boundary for Chunk 3. It is
    PURE (no I/O, no side effects). The orchestration loop in
    :func:`generate_paper_signals` is responsible for I/O and
    persistence.

    Behavior caps (Phase 12):
      - MARKET_MAKER_LP / ARBITRAGE_MULTI_LEG / HIGH_FREQUENCY_BOT
        → SKIP (handled by verdict_generation)
      - MIXED / UNKNOWN → WATCHLIST cap

    Category constraints (Phase 2 / Phase 3):
      - category_verdict == INCOMPLETE → INCOMPLETE
      - category_verdict != COPY_CANDIDATE → blocks COPY_CANDIDATE
        (decision engine returns WATCHLIST via the CATEGORY_NOT_COPY
        branch)

    Shadow isolation (Phase 15, deferred to Chunk 5):
      - The shadow result is NOT consumed by this function. It is
        persisted for research but does not affect the verdict.
    """
    # Build the typed input. Shadow result is intentionally NOT
    # passed — shadow never controls v1 (Phase 15 / spec).
    signal_input = SignalDecisionInput(
        wallet_score=wallet_score_result.score,
        wallet_verdict=wallet_score_result.verdict,
        category_wallet_score=category_score,
        category_wallet_verdict=category_verdict,
        trade_score=trade_score_result.score,
        trade_verdict=trade_score_result.verdict,
        behavior_classification=behavior_result,
        has_hard_exclusion=has_hard_exclusion,
        hard_exclusion_reason=hard_exclusion_reason,
    )
    decision = generate_signal_verdict(signal_input)
    return decision.verdict


# ---- Backwards-compat behavior loader alias ---------------------------


#: Backwards-compat name preserved for the original placeholder
#: signature. The new behavior loader is the real one — no
#: empty-evidence scaffolding remains in the active path.
_load_behavior_evidence_for_wallet_legacy = _load_behavior_evidence_for_wallet


def generate_paper_signals(
    db: Database,
    *,
    now: Optional[datetime] = None,
) -> dict:
    """Generate paper signals for pending copy candidates.

    Workflow:
    1. Load pending copy candidates (PENDING_PRICE_CHECK status)
    2. For each candidate with a fresh snapshot:
       a. Calculate wallet score v1
       b. Classify wallet behavior
       c. Calculate trade copyability v1
       d. Calculate v2 shadow (parallel, no effect on v1)
       e. Generate signal verdict
       f. Persist paper signal decision (unapproved)
       g. Register exit experiments for COPY_CANDIDATE signals
    3. For candidates without fresh snapshot: emit INCOMPLETE

    Returns dict with counts of each verdict type.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    results = {
        "copy_candidate": 0,
        "watchlist": 0,
        "skip": 0,
        "incomplete": 0,
        "errors": [],
    }

    # Load pending candidates (PENDING_PRICE_CHECK status)
    candidates = db.fetchall(
        """SELECT * FROM copy_candidates 
           WHERE status = ?
           ORDER BY created_at ASC""",
        (CandidateStatus.PENDING_PRICE_CHECK.value,),
    )

    for cand in candidates:
        candidate_id = cand["id"]
        wallet_id = cand["wallet_id"]
        source_trade_id = cand["source_trade_id"]

        try:
            # Get latest price snapshot. The real persistence
            # helper returns a PriceSnapshot domain object; we
            # convert it to a dict-shaped mapping for the rest
            # of the loop (kept as a dict to avoid touching
            # every downstream reader in this chunk).
            snapshot_obj = get_latest_snapshot_for_candidate(
                db, candidate_id
            )
            if snapshot_obj is None:
                # No fresh snapshot → INCOMPLETE, do not hit CLOB
                results["incomplete"] += 1
                continue
            try:
                snapshot = dict(snapshot_obj)
            except TypeError:
                snapshot = vars(snapshot_obj)

            # Load behavior evidence and classify
            evidence = _load_behavior_evidence_for_wallet(db, wallet_id)
            behavior_result = classify_wallet_behavior(evidence)

            # Get wallet score (use existing or compute)
            wallet_score_result = compute_wallet_score_v1(
                wallet_id=wallet_id,
                now=now,
                # TODO: pass actual wallet metrics
            )

            # Persist wallet score v1
            persist_wallet_score_v1(
                db,
                wallet_id,
                wallet_score_result,
                source_data_timestamp=snapshot.get("fetched_at"),
            )

            # Extract snapshot metrics for trade scoring
            snapshot_metrics = _load_snapshot_metrics(snapshot)

            # Compute trade copyability v1
            trade_score_result = compute_trade_score_v1(
                wallet_id=wallet_id,
                source_trade_id=source_trade_id,
                intended_stake=cand.get("source_trade_notional") or 100.0,
                executable_depth=snapshot_metrics.get("best_bid_size", 0) or snapshot_metrics.get("best_ask_size", 0),
                spread=snapshot_metrics.get("spread"),
                trade_age_seconds=snapshot_metrics.get("trade_age_seconds"),
                seconds_to_market_end=snapshot_metrics.get("seconds_to_market_end"),
                market_active=snapshot_metrics.get("market_active"),
                market_closed=snapshot_metrics.get("market_closed"),
                market_resolved=snapshot_metrics.get("market_resolved"),
                now=now,
            )

            # Persist trade score v1
            persist_trade_score_v1(
                db,
                wallet_id,
                source_trade_id,
                trade_score_result,
                source_data_timestamp=snapshot.get("fetched_at"),
                candidate_id=candidate_id,
                price_snapshot_id=snapshot.get("id"),
            )

            # Compute v2 shadow (parallel)
            # Only if forward outcome data is available
            shadow_result = compute_shadow_score_v2(
                wallet_id=wallet_id,
                source_trade_id=source_trade_id,
                now=now,
                # Most inputs missing → will produce SHADOW_INCOMPLETE
            )

            persist_shadow_score_v2(
                db,
                wallet_id,
                source_trade_id,
                shadow_result,
                source_data_timestamp=snapshot.get("fetched_at"),
            )

            # Generate signal verdict (Task 3.7).
            #
            # Category inputs are loaded from the persisted
            # category_wallet_score_decisions table. The label
            # is resolved from the snapshot's book_summary_json
            # or the candidate's market_outcome_id. A missing
            # label or missing decision produces (None, None)
            # which the verdict engine maps to INCOMPLETE.
            #
            # Shadow output is persisted for research but is NOT
            # consumed by the verdict engine (Phase 15).
            category_score, category_verdict = _build_category_inputs(
                db, dict(cand), snapshot
            )
            signal_decision = generate_signal_verdict(
                input_data=SignalDecisionInput(
                    wallet_score=wallet_score_result.score,
                    wallet_verdict=wallet_score_result.verdict,
                    category_wallet_score=category_score,
                    category_wallet_verdict=category_verdict,
                    trade_score=trade_score_result.score,
                    trade_verdict=trade_score_result.verdict,
                    behavior_classification=behavior_result,
                    has_hard_exclusion=False,
                ),
            )

            # Persist paper signal decision (always unapproved for PR4)
            paper_signal_id = persist_paper_signal(
                db,
                candidate_id,
                wallet_id,
                signal_decision.verdict.value,
                signal_decision.reason,
                wallet_score_result.score,
                trade_score_result.score,
                shadow_result.score,
                shadow_result.verdict.value if shadow_result else None,
                signal_decision.verdict.value,
                snapshot.get("fetched_at"),
                source_trade_id,
                snapshot.get("id"),
            )

            if signal_decision.verdict == SignalVerdict.COPY_CANDIDATE:
                results["copy_candidate"] += 1
                # Register exit experiments for research
                record_exit_experiments(db, paper_signal_id)
            elif signal_decision.verdict == SignalVerdict.WATCHLIST:
                results["watchlist"] += 1
            elif signal_decision.verdict == SignalVerdict.SKIP:
                results["skip"] += 1
            else:
                results["incomplete"] += 1

        except Exception as e:
            logger.error("Failed to process candidate %s: %s", candidate_id, e)
            results["errors"].append(str(e))

    return results