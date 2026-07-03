"""Typed paper-signal decision input contract (Chunk 5 — Phase 9).

Frozen dataclass that captures every persisted input the final
paper-signal decision consumed. The persistence path reads raw
columns from this object — never from scattered getattr fallbacks
on the result.

Why a typed contract:

- Replayability: reloading a paper-signal decision row can rebuild
  the typed input the verdict engine saw at evaluation time and
  recompute exactly.
- Identity: the typed input participates in the idempotency key
  through its serialized canonical form (fingerprint).
- Auditability: every column in the row maps to a named field.

Safety:

- ``is_approved`` is a required field. The runtime never sets it
  to 1 — PR 4 paper signals are always unapproved.
- ``auto_approve_requested`` exists ONLY as a defensive sentinel:
  if a caller requests auto-approval, the persisted decision
  forces ``is_approved = 0`` and records an explicit safety
  reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ---- Typed contracts ----------------------------------------------------


@dataclass(frozen=True)
class PaperSignalDecisionInput:
    """Frozen typed input contract for final paper-signal persistence.

    Every field corresponds to a column on the persisted
    ``paper_signal_decisions`` row, OR an immutable identity
    anchor that participates in the idempotency key.

    Required fields are the immutable identity anchors. Optional
    fields may be ``None`` when the underlying evidence is
    incomplete — the verdict engine already mapped that to
    INCOMPLETE.
    """

    # Required identity anchors
    candidate_id: int
    source_trade_id: str
    wallet_id: str

    # Required upstream decision ids (used for identity + audit)
    wallet_score_decision_id: Optional[int]
    category_score_decision_id: Optional[int]
    trade_score_decision_id: Optional[int]

    # Required snapshot identity
    price_snapshot_id: Optional[str]

    # Trade-level input (immutable)
    intended_stake: Optional[float]
    category_label: Optional[str]

    # Behavior classification
    behavior_classification: str  # canonical classification value

    # Formula versions
    wallet_formula_name: str
    wallet_formula_version: str
    category_formula_name: str
    category_formula_version: str
    trade_formula_name: str
    trade_formula_version: str

    # Verdict
    evaluation_timestamp: datetime  # immutable evaluation moment
    final_verdict: str  # canonical SignalVerdict value
    final_reason: str  # canonical SignalReason value
    is_approved: int  # MUST be 0 for PR 4 paper signals

    # Optional defensive sentinel: when True, the runtime must reject
    # the auto-approval request and force is_approved = 0 with an
    # explicit safety reason. Default False (no request).
    auto_approve_requested: bool = False


@dataclass(frozen=True)
class PaperSignalDecisionResult:
    """Frozen result of the final paper-signal decision.

    ``input`` retains the exact typed input. The persisted row
    carries a canonical-JSON serialization of ``input`` so the
    decision is replayable.
    """

    paper_signal_id: int
    candidate_id: int
    wallet_id: str
    signal_family: str
    signal_reason: str
    wallet_score: float
    trade_score: float
    shadow_score: float
    shadow_verdict: Optional[str]
    final_verdict: str
    final_reason: str
    is_approved: int
    source_data_timestamp: Optional[str]
    source_trade_id: Optional[str]
    price_snapshot_id: Optional[str]
    input: PaperSignalDecisionInput
    computed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ---- Safety reason tokens -----------------------------------------------

SAFETY_REASON_AUTO_APPROVE_REJECTED = (
    "auto_approve_rejected_for_paper_signal"
)