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

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


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

    # PR67 additive evidence-resolver provenance.  These optional fields keep
    # historical decision_input_json payloads readable without a migration.
    wallet_evidence_fingerprint: Optional[str] = None
    wallet_score_complete: Optional[bool] = None
    wallet_score_missing_reasons: tuple[str, ...] = ()
    taxonomy_status: Optional[str] = None
    taxonomy_source: Optional[str] = None
    category_evidence_fingerprint: Optional[str] = None
    category_score_status: Optional[str] = None
    category_score_missing_reasons: tuple[str, ...] = ()
    category_not_applicable_reason: Optional[str] = None
    evaluation_policy_name: Optional[str] = None
    trade_copyability_decision_id: Optional[int] = None


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


# ---- Canonical JSON serializer / deserializer (v12 audit) --------------

# Canonical JSON contract for ``decision_input_json``.
#
# Goals:
#   1. ``serialize_paper_signal_input`` MUST produce a byte-stable
#      string for a given typed input. Two runs that see identical
#      inputs MUST produce identical JSON (replayability).
#   2. ``deserialize_paper_signal_input`` MUST rebuild the exact
#      typed input the verdict engine saw at evaluation time, so a
#      future reload of the persisted row is byte-equivalent to a
#      fresh persistence.
#   3. Floats are serialized via the canonical ``repr`` form (round-
#      trip safe for the IEEE-754 doubles used by the runtime).
#      The optional ``intended_stake`` is included verbatim — no
#      float-to-string coercion that would drift across runs.
#   4. ``datetime`` fields are serialized as ISO-8601 strings.
#   5. Optional fields that are ``None`` serialize as JSON ``null``
#      (not the string ``"None"``, not an empty string).
#   6. Booleans serialize as JSON booleans (``true`` / ``false``).
#   7. Integer IDs (including ``None``) serialize as JSON integers
#      or null; they are NOT stringified.
#
# ``sort_keys=True`` + ``separators=(",", ":")`` make the output
# deterministic across dict-iteration order. ``ensure_ascii=False``
# is unnecessary because the keys and values are ASCII.

_CANONICAL_JSON_SEPARATORS = (",", ":")


def _canonicalize(value: Any) -> Any:
    """Recursively normalize a value for canonical JSON serialization.

    * ``datetime`` → ISO-8601 string (``isoformat()``).
    * ``None`` / ``bool`` / ``int`` / ``float`` / ``str`` → preserved
      (floats use Python's repr, which is round-trip safe).
    * ``tuple`` / ``list`` → list (recursively normalized).
    * ``dict`` → dict (recursively normalized).
    * ``frozenset`` / ``set`` → sorted list (recursively normalized).
    * Anything else → ``repr(value)`` as a defensive fallback. The
      typed input contract keeps every field within the supported
      set, so this branch is unreachable in the happy path. It is
      intentionally NOT silenced so an unexpected type surfaces
      during review.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    if isinstance(value, dict):
        return {
            str(k): _canonicalize(v) for k, v in value.items()
        }
    if isinstance(value, (frozenset, set)):
        return [_canonicalize(v) for v in sorted(value, key=repr)]
    return repr(value)


def serialize_paper_signal_input(
    input: "PaperSignalDecisionInput",
) -> str:
    """Serialize a typed ``PaperSignalDecisionInput`` to canonical JSON.

    The output is byte-stable across runs that see identical
    inputs: identical inputs produce identical JSON strings. This
    is the source-of-truth audit record that v12 persists in
    ``paper_signal_decisions.decision_input_json``.

    The serialization is a flat ``dict`` of every field on the
    typed contract, emitted with ``sort_keys=True`` so iteration
    order never affects the byte sequence.
    """
    payload = {
        "candidate_id": _canonicalize(input.candidate_id),
        "source_trade_id": _canonicalize(input.source_trade_id),
        "wallet_id": _canonicalize(input.wallet_id),
        "wallet_score_decision_id": _canonicalize(
            input.wallet_score_decision_id
        ),
        "category_score_decision_id": _canonicalize(
            input.category_score_decision_id
        ),
        "trade_score_decision_id": _canonicalize(
            input.trade_score_decision_id
        ),
        "price_snapshot_id": _canonicalize(input.price_snapshot_id),
        "intended_stake": _canonicalize(input.intended_stake),
        "category_label": _canonicalize(input.category_label),
        "behavior_classification": _canonicalize(
            input.behavior_classification
        ),
        "wallet_formula_name": _canonicalize(input.wallet_formula_name),
        "wallet_formula_version": _canonicalize(
            input.wallet_formula_version
        ),
        "category_formula_name": _canonicalize(
            input.category_formula_name
        ),
        "category_formula_version": _canonicalize(
            input.category_formula_version
        ),
        "trade_formula_name": _canonicalize(input.trade_formula_name),
        "trade_formula_version": _canonicalize(
            input.trade_formula_version
        ),
        "evaluation_timestamp": _canonicalize(
            input.evaluation_timestamp
        ),
        "final_verdict": _canonicalize(input.final_verdict),
        "final_reason": _canonicalize(input.final_reason),
        "is_approved": _canonicalize(input.is_approved),
        "auto_approve_requested": _canonicalize(
            input.auto_approve_requested
        ),
        "wallet_evidence_fingerprint": _canonicalize(
            input.wallet_evidence_fingerprint
        ),
        "wallet_score_complete": _canonicalize(input.wallet_score_complete),
        "wallet_score_missing_reasons": _canonicalize(
            input.wallet_score_missing_reasons
        ),
        "taxonomy_status": _canonicalize(input.taxonomy_status),
        "taxonomy_source": _canonicalize(input.taxonomy_source),
        "category_evidence_fingerprint": _canonicalize(
            input.category_evidence_fingerprint
        ),
        "category_score_status": _canonicalize(input.category_score_status),
        "category_score_missing_reasons": _canonicalize(
            input.category_score_missing_reasons
        ),
        "category_not_applicable_reason": _canonicalize(
            input.category_not_applicable_reason
        ),
        "evaluation_policy_name": _canonicalize(input.evaluation_policy_name),
        "trade_copyability_decision_id": _canonicalize(
            input.trade_copyability_decision_id
        ),
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=_CANONICAL_JSON_SEPARATORS,
        ensure_ascii=False,
        allow_nan=False,
    )


def deserialize_paper_signal_input(
    payload: str,
) -> "PaperSignalDecisionInput":
    """Rebuild a ``PaperSignalDecisionInput`` from its canonical JSON.

    Inverse of :func:`serialize_paper_signal_input`. ``evaluation_timestamp``
    is parsed back into a ``datetime`` (timezone-aware, UTC when the
    serialized form omitted a tzinfo, which matches the runtime
    convention of always writing tz-aware ISO-8601 from
    ``datetime.now(timezone.utc).isoformat()``).
    """
    obj = json.loads(payload)
    et_raw = obj.get("evaluation_timestamp")
    if isinstance(et_raw, str):
        # Accept trailing 'Z' for UTC, mirroring the runtime's
        # isoformat() output for ``datetime.now(timezone.utc)``.
        et_value = et_raw
        if et_value.endswith("Z"):
            et_value = et_value[:-1] + "+00:00"
        et: datetime = datetime.fromisoformat(et_value)
        if et.tzinfo is None:
            et = et.replace(tzinfo=timezone.utc)
    elif et_raw is None:
        # ``evaluation_timestamp`` is a required field on the typed
        # contract. The serializer never writes null for it, and a
        # deserializer that finds null would otherwise fabricate a
        # timestamp on the audit trail — which would silently break
        # replayability. Surface the issue loudly.
        raise ValueError(
            "evaluation_timestamp is required and must not be null"
        )
    else:
        raise ValueError(
            f"evaluation_timestamp must be a string or null, got {type(et_raw).__name__}"
        )
    return PaperSignalDecisionInput(
        candidate_id=int(obj["candidate_id"]),
        source_trade_id=str(obj["source_trade_id"]),
        wallet_id=str(obj["wallet_id"]),
        wallet_score_decision_id=(
            int(obj["wallet_score_decision_id"])
            if obj.get("wallet_score_decision_id") is not None
            else None
        ),
        category_score_decision_id=(
            int(obj["category_score_decision_id"])
            if obj.get("category_score_decision_id") is not None
            else None
        ),
        trade_score_decision_id=(
            int(obj["trade_score_decision_id"])
            if obj.get("trade_score_decision_id") is not None
            else None
        ),
        price_snapshot_id=(
            str(obj["price_snapshot_id"])
            if obj.get("price_snapshot_id") is not None
            else None
        ),
        intended_stake=(
            float(obj["intended_stake"])
            if obj.get("intended_stake") is not None
            else None
        ),
        category_label=(
            str(obj["category_label"])
            if obj.get("category_label") is not None
            else None
        ),
        behavior_classification=str(obj["behavior_classification"]),
        wallet_formula_name=str(obj["wallet_formula_name"]),
        wallet_formula_version=str(obj["wallet_formula_version"]),
        category_formula_name=str(obj["category_formula_name"]),
        category_formula_version=str(obj["category_formula_version"]),
        trade_formula_name=str(obj["trade_formula_name"]),
        trade_formula_version=str(obj["trade_formula_version"]),
        evaluation_timestamp=et,
        final_verdict=str(obj["final_verdict"]),
        final_reason=str(obj["final_reason"]),
        is_approved=int(obj["is_approved"]),
        auto_approve_requested=bool(obj.get("auto_approve_requested", False)),
        wallet_evidence_fingerprint=(
            str(obj["wallet_evidence_fingerprint"])
            if obj.get("wallet_evidence_fingerprint") is not None else None
        ),
        wallet_score_complete=(
            bool(obj["wallet_score_complete"])
            if obj.get("wallet_score_complete") is not None else None
        ),
        wallet_score_missing_reasons=tuple(
            str(value) for value in obj.get("wallet_score_missing_reasons", ())
        ),
        taxonomy_status=(
            str(obj["taxonomy_status"])
            if obj.get("taxonomy_status") is not None else None
        ),
        taxonomy_source=(
            str(obj["taxonomy_source"])
            if obj.get("taxonomy_source") is not None else None
        ),
        category_evidence_fingerprint=(
            str(obj["category_evidence_fingerprint"])
            if obj.get("category_evidence_fingerprint") is not None else None
        ),
        category_score_status=(
            str(obj["category_score_status"])
            if obj.get("category_score_status") is not None else None
        ),
        category_score_missing_reasons=tuple(
            str(value) for value in obj.get("category_score_missing_reasons", ())
        ),
        category_not_applicable_reason=(
            str(obj["category_not_applicable_reason"])
            if obj.get("category_not_applicable_reason") is not None else None
        ),
        evaluation_policy_name=(
            str(obj["evaluation_policy_name"])
            if obj.get("evaluation_policy_name") is not None else None
        ),
        trade_copyability_decision_id=(
            int(obj["trade_copyability_decision_id"])
            if obj.get("trade_copyability_decision_id") is not None else None
        ),
    )