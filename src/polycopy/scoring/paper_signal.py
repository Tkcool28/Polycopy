"""Paper signal generation module for PR 4.

Consumes persisted candidates and fresh price snapshots.
Calculates and persists v1 scores and v2 shadow output.
Emits paper-only unapproved signals for qualified candidates.

This module replaces the placeholder _generate_signals in run_scan.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from polycopy.config.settings import get_settings
from polycopy.db.database import Database
from polycopy.db.copy_candidate_persistence import CandidateStatus
from polycopy.db.price_snapshot_persistence import get_latest_snapshot_for_candidate
from polycopy.domain.copy_candidate import CopyCandidate
from polycopy.scoring.behavior_classification import (
    BehaviorClassification,
    BehaviorClassificationResult,
    BehaviorEvidence,
    classify_wallet_behavior,
)
from polycopy.scoring.wallet_score_v1 import (
    WalletVerdict,
    compute_wallet_score_v1,
)
from polycopy.scoring.trade_score_v1 import (
    TradeVerdict,
    compute_trade_score_v1,
)
from polycopy.scoring.shadow_score_v2 import (
    ShadowVerdict,
    compute_shadow_score_v2,
)
from polycopy.scoring.verdict_generation import (
    SignalVerdict,
    SignalDecisionInput,
    generate_signal_verdict,
)
from polycopy.scoring.score_serialization import (
    generate_idempotency_key,
    persist_wallet_score_v1,
    persist_trade_score_v1,
    persist_shadow_score_v2,
    persist_paper_signal,
    record_exit_experiments,
)

logger = logging.getLogger(__name__)


def _load_behavior_evidence_for_wallet(
    db: Database, wallet_id: str
) -> BehaviorEvidence:
    """Load trade data to construct behavior evidence."""
    # TODO: Implement using actual wallet trade history
    # For now, return default unknown evidence
    return BehaviorEvidence()


def _load_category_wallet_verdict(
    db: Database, wallet_id: str, category: str
) -> Optional[str]:
    """Load category-specific wallet verdict if available."""
    row = db.fetchone(
        """SELECT verdict FROM category_wallet_score_decisions 
           WHERE wallet_id = ? AND category_label = ?
           ORDER BY created_at DESC LIMIT 1""",
        (wallet_id, category),
    )
    return row["verdict"] if row else None


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

    settings = get_settings()

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
            # Get latest price snapshot
            snapshot = get_latest_snapshot_for_candidate(db, candidate_id, now)

            if snapshot is None:
                # No fresh snapshot → INCOMPLETE, do not hit CLOB
                results["incomplete"] += 1
                continue

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

            # Generate signal verdict
            signal_decision = generate_signal_verdict(
                input_data=SignalDecisionInput(
                    wallet_score=wallet_score_result.score,
                    wallet_verdict=wallet_score_result.verdict,
                    category_wallet_verdict=None,  # TODO: implement category scoring
                    trade_score=trade_score_result.score,
                    trade_verdict=trade_score_result.verdict,
                    behavior_classification=behavior_result if hasattr(behavior_result, 'reasons') else None,
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