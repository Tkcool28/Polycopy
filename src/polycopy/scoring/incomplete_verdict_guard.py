"""PR24E — Incomplete-verdict resolution-evidence guard.

Single source of truth for the rule that a wallet decision row MUST be
labeled ``incomplete`` whenever resolved-market evidence is missing or
zero. Used by both ``compute_wallet_score_v1`` (pre-persist) and the
``decision_verdicts`` writer in ``scripts/scan_pipeline_wiring.py`` (so
the parent ``wallet_score_decisions`` row and its companion
``decision_verdicts`` row never disagree).

Contract (PR24E):

  * If a wallet's ``resolved_markets`` is ``None`` or ``0``, the wallet
    decision cannot be a real verdict (COPY_CANDIDATE / WATCHLIST /
    SKIP). It MUST be ``INCOMPLETE``.
  * The companion ``category_resolved_markets`` is also treated as
    required evidence; if it is ``None`` or ``0`` the verdict is
    ``INCOMPLETE`` regardless of how good the other metrics look.
  * When forcing ``INCOMPLETE`` the helper also populates the structured
    reason buckets so audits and PR24A resolution tracking can tell
    evidence-gap decisions apart from deliberate ``SKIP`` decisions:

        missing_essentials_json includes any of the following that are
        missing: ``resolved_markets``, ``category_resolved_markets``,
        ``sample_fraction``, ``sharpe_ratio``, ``max_drawdown``.

        eligibility_failures_json always includes the canonical marker
        ``no_resolved_market_evidence``.

  * Hard invariant for the ``SKIP`` label (PR27): a wallet decision
    whose ``verdict == "skip"`` is NEVER persisted with BOTH
    ``missing_essentials_json`` and ``eligibility_failures_json``
    empty. Two sub-cases:

      - Sufficient resolved-market evidence AND caller-supplied
        eligibility_failures is empty: the helper appends the
        canonical ``score_below_copy_threshold`` marker to
        ``eligibility_failures`` so the row is auditable. The
        verdict and verdict_family stay ``skip`` — the helper
        does NOT promote to INCOMPLETE.

      - Resolution-evidence gap (verdict already forced to
        INCOMPLETE by Rule 1): the helper populates
        ``missing_essentials`` with the resolution-evidence keys
        and ensures ``eligibility_failures`` contains
        ``no_resolved_market_evidence``.

This module is pure-Python, deterministic, and has no I/O. It is safe
to import from any layer.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from typing import Any, Iterable, Mapping, Optional

# ---------------------------------------------------------------------------
# Canonical strings (kept in sync with CANONICAL_V1_VERDICTS in
# polycopy.scoring.persistence_validation and the CHECK constraint in
# db/schema_v10.py).
# ---------------------------------------------------------------------------

VERDICT_INCOMPLETE = "incomplete"
VERDICT_SKIP = "skip"
VERDICT_WATCHLIST = "watchlist"
VERDICT_COPY_CANDIDATE = "copy_candidate"

CANONICAL_FAMILY = {
    VERDICT_COPY_CANDIDATE,
    VERDICT_WATCHLIST,
    VERDICT_SKIP,
    VERDICT_INCOMPLETE,
}

# Canonical eligibility-failure marker (single source of truth).
NO_RESOLVED_MARKET_EVIDENCE = "no_resolved_market_evidence"

# PR24F canonical marker: any required non-resolution evidence is
# missing (``sample_fraction``, ``sharpe_ratio``, ``max_drawdown``,
# or — when ``require_category_resolution`` is True —
# ``category_resolved_markets``).
MISSING_REQUIRED_EVIDENCE = "missing_required_evidence"

# Canonical score-driven SKIP marker. PR27 invariant: a SKIP verdict
# must NEVER persist with both ``missing_essentials_json`` and
# ``eligibility_failures_json`` empty. When the SKIP is the legitimate
# output of the score formula (sufficient resolved-market evidence, no
# pre-existing eligibility failure), the helper appends this canonical
# marker to ``eligibility_failures`` so the row is auditable.
SCORE_BELOW_COPY_THRESHOLD = "score_below_copy_threshold"

# The set of evidence keys the helper tracks when forcing INCOMPLETE.
# PR24F: this is the canonical "required evidence" set — every key
# here MUST be present (None-or-zero for resolution keys, None-only
# for non-resolution keys) before a real verdict can be emitted.
RESOLUTION_EVIDENCE_KEYS: tuple[str, ...] = (
    "resolved_markets",
    "category_resolved_markets",
)
NON_RESOLUTION_REQUIRED_KEYS: tuple[str, ...] = (
    "sample_fraction",
    "sharpe_ratio",
    "max_drawdown",
)
REQUIRED_EVIDENCE_KEYS: tuple[str, ...] = (
    *RESOLUTION_EVIDENCE_KEYS,
    *NON_RESOLUTION_REQUIRED_KEYS,
)

# Backwards-compatibility alias: PR24E used this name as the union of
# all five keys. Older code (e.g. ``persist_decision_verdicts_and_components``
# in ``scripts/scan_pipeline_wiring.py``) imports it.
ALL_EVIDENCE_KEYS: tuple[str, ...] = REQUIRED_EVIDENCE_KEYS


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------


def lacks_resolution_evidence(
    resolved_markets: Optional[int],
    category_resolved_markets: Optional[int] = None,
    *,
    require_category_resolution: bool = True,
) -> bool:
    """Return ``True`` when the wallet has no usable resolved-market evidence.

    Either an outright-missing or a zero-valued ``resolved_markets`` is
    treated as insufficient. Category-level resolution is required when
    ``require_category_resolution`` is True (the default — the wallet
    formula currently weights category specialization).
    """
    if resolved_markets is None or resolved_markets == 0:
        return True
    if require_category_resolution:
        if category_resolved_markets is None or category_resolved_markets == 0:
            return True
    return False


def _is_missing(key: str, value: Any) -> bool:
    """Return ``True`` when ``value`` is unusable as evidence for ``key``.

    PR24F semantics — the rule depends on the key:

    * ``resolved_markets`` and ``category_resolved_markets``: a wallet
      has no usable resolution evidence when ``value`` is ``None`` OR
      zero. ``resolved_markets=0`` means no resolved markets exist;
      we don't treat zero as a positive count.
    * ``sample_fraction``, ``sharpe_ratio``, ``max_drawdown``: ``None``
      is missing, but a numeric zero is a real measured value and is
      treated as present. A Sharpe of 0.0 means "no excess return per
      unit risk" — that is information, not absence. Likewise zero
      drawdown means "no observed drawdown", which is also information.

    The ``key`` argument is the canonical evidence-field name; passing
    a key outside the required set falls back to the conservative
    rule (``None`` or zero ⇒ missing).
    """
    if value is None:
        return True
    if key in RESOLUTION_EVIDENCE_KEYS:
        # Resolution counts: zero is missing.
        if isinstance(value, (int, float)) and value == 0:
            return True
    # Non-resolution required evidence: ``None`` is the only missing
    # form. Zero is a legitimate measurement.
    return False


# ---------------------------------------------------------------------------
# Reason-bucket builders
# ---------------------------------------------------------------------------


def _build_missing_essentials(
    *,
    resolved_markets: Optional[int],
    category_resolved_markets: Optional[int],
    sample_fraction: Optional[float],
    sharpe_ratio: Optional[float],
    max_drawdown: Optional[float],
    existing: Optional[Iterable[str]] = None,
) -> list[str]:
    """Assemble a deduplicated, order-preserving list of missing essentials.

    Always-on rules:

      * ``resolved_markets`` missing → added.
      * ``category_resolved_markets`` missing → added.
      * ``sample_fraction`` missing → added (can't sanity-check the
        resolved-market sample size without it).
      * ``sharpe_ratio`` missing → added (risk component cannot be
        honestly evaluated without a realized-series statistic).
      * ``max_drawdown`` missing → added (paired with sharpe for risk).

    Anything in ``existing`` is preserved (so the helper composes with
    upstream "essential evidence" checks like missing ``trade_count``).
    """
    out: list[str] = []
    seen: set[str] = set()
    if existing:
        for item in existing:
            if item and item not in seen:
                out.append(item)
                seen.add(item)
    candidates: list[tuple[str, Any]] = [
        ("resolved_markets", resolved_markets),
        ("category_resolved_markets", category_resolved_markets),
        ("sample_fraction", sample_fraction),
        ("sharpe_ratio", sharpe_ratio),
        ("max_drawdown", max_drawdown),
    ]
    for key, value in candidates:
        if _is_missing(key, value) and key not in seen:
            out.append(key)
            seen.add(key)
    return out


def _build_eligibility_failures(
    *,
    lacks_resolution: bool,
    existing: Optional[Iterable[str]] = None,
) -> list[str]:
    """Assemble a deduplicated eligibility-failure list.

    When ``lacks_resolution`` is True the canonical
    ``no_resolved_market_evidence`` marker is always present so
    PR24A-style resolution-tracking filters can pivot on it.
    """
    out: list[str] = []
    seen: set[str] = set()
    if existing:
        for item in existing:
            if item and item not in seen:
                out.append(item)
                seen.add(item)
    if lacks_resolution and NO_RESOLVED_MARKET_EVIDENCE not in seen:
        out.append(NO_RESOLVED_MARKET_EVIDENCE)
        seen.add(NO_RESOLVED_MARKET_EVIDENCE)
    return out


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def derive_wallet_verdict_from_evidence(
    *,
    verdict: str,
    resolved_markets: Optional[int],
    category_resolved_markets: Optional[int] = None,
    sample_fraction: Optional[float] = None,
    sharpe_ratio: Optional[float] = None,
    max_drawdown: Optional[float] = None,
    missing_essentials: Optional[Iterable[str]] = None,
    eligibility_failures: Optional[Iterable[str]] = None,
    require_category_resolution: bool = True,
) -> dict[str, Any]:
    """Compute the corrected decision-record payload.

    Returns a dict with keys::

        verdict            (str)
        verdict_family     (str)  -- mirrors verdict for the v1 four-way family
        missing_essentials (list[str])
        eligibility_failures (list[str])

    PR24F contract — the helper enforces two completeness rules that
    must hold BEFORE any real verdict (SKIP / WATCHLIST / COPY_CANDIDATE)
    can be emitted:

      Rule 1a (zero-resolution evidence): if ``resolved_markets`` is
      ``None`` or zero, OR if ``category_resolved_markets`` is
      ``None`` or zero (and ``require_category_resolution`` is True),
      the verdict is forced to ``incomplete`` and ``eligibility_failures``
      includes ``no_resolved_market_evidence``.

      Rule 1b (missing required evidence): if Rule 1a does not fire
      AND any of ``sample_fraction``, ``sharpe_ratio``, ``max_drawdown``
      is ``None``, the verdict is forced to ``incomplete`` and
      ``eligibility_failures`` includes ``missing_required_evidence``.
      (Numeric zero values are treated as present for these three
      fields — a Sharpe of 0.0 is a real measurement, not absence.)

    When both rules leave the verdict untouched (i.e. all required
    evidence is present and usable), PR27's no-silent-skip rule still
    fires: a SKIP with no caller-supplied eligibility failure is
    annotated with ``score_below_copy_threshold`` so the persisted row
    is never silent.

    The function is pure and deterministic.
    """
    v = (verdict or "").lower()
    if v not in CANONICAL_FAMILY:
        v = VERDICT_INCOMPLETE

    lacks_resolution = lacks_resolution_evidence(
        resolved_markets,
        category_resolved_markets,
        require_category_resolution=require_category_resolution,
    )

    # Build the canonical missing-essentials list using the key-aware
    # missing rule. ``missing_essentials`` always reflects every key
    # whose value is unusable for this wallet.
    missing = _build_missing_essentials(
        resolved_markets=resolved_markets,
        category_resolved_markets=category_resolved_markets,
        sample_fraction=sample_fraction,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_drawdown,
        existing=missing_essentials,
    )
    failures = _build_eligibility_failures(
        lacks_resolution=lacks_resolution,
        existing=eligibility_failures,
    )

    # ------------------------------------------------------------------
    # Rule 1a — zero-resolution evidence ⇒ force INCOMPLETE.
    # ------------------------------------------------------------------
    if lacks_resolution:
        v = VERDICT_INCOMPLETE
        if NO_RESOLVED_MARKET_EVIDENCE not in failures:
            failures = [*failures, NO_RESOLVED_MARKET_EVIDENCE]

    # ------------------------------------------------------------------
    # Rule 1b — missing required non-resolution evidence ⇒ force
    # INCOMPLETE (only when Rule 1a did not fire). A wallet with
    # sufficient resolved-market evidence but a missing risk/sample
    # field is still not eligible for a real verdict.
    # ------------------------------------------------------------------
    elif any(
        key in missing
        for key in NON_RESOLUTION_REQUIRED_KEYS
    ):
        v = VERDICT_INCOMPLETE
        if MISSING_REQUIRED_EVIDENCE not in failures:
            failures = [*failures, MISSING_REQUIRED_EVIDENCE]

    # ------------------------------------------------------------------
    # PR27 invariant — a persisted ``skip`` must never have BOTH
    # ``missing_essentials`` empty AND ``eligibility_failures`` empty.
    #
    # Two cases:
    #
    # (a) Verdict was forced to INCOMPLETE by Rule 1a/1b. The persisted
    #     reason buckets are populated by the rules above. Nothing more
    #     to do here.
    #
    # (b) Verdict is SKIP with all required evidence present. Preserve
    #     the verdict but guarantee ``eligibility_failures`` is
    #     non-empty: every SKIP must carry at least one eligibility
    #     failure, regardless of whether the caller supplied a
    #     non-empty list, an empty list, or ``None``.
    # ------------------------------------------------------------------
    caller_missing = list(missing_essentials or [])
    caller_failures = list(eligibility_failures or [])

    if v == VERDICT_INCOMPLETE:
        # Case (a) — Rules 1a/1b already populated reason buckets. If
        # the caller supplied empty buckets AND the rules didn't fill
        # them in (which can only happen if Rule 1a/1b fired but the
        # resolved-markets key was, e.g., a 0 that the rule treats as
        # present), make sure the persisted row is still non-silent.
        # This branch is normally a no-op because the rules above
        # already populated ``failures``.
        if not caller_missing:
            # Re-derive from helper-computed missing list — this is
            # already non-empty when a rule fired.
            missing = list(missing) or list(REQUIRED_EVIDENCE_KEYS)
        if not failures and not caller_failures:
            # Defensive: if a caller passes ``verdict="incomplete"``
            # with no other reason, attach a marker so the row is
            # auditable.
            if lacks_resolution:
                failures = [NO_RESOLVED_MARKET_EVIDENCE]
            else:
                failures = [MISSING_REQUIRED_EVIDENCE]

    elif v == VERDICT_SKIP:
        # Case (b) — sufficient resolution evidence AND all required
        # non-resolution evidence present. Preserve the verdict.
        if not failures:
            failures = [SCORE_BELOW_COPY_THRESHOLD]

    return {
        "verdict": v,
        "verdict_family": v,
        "missing_essentials": missing,
        "eligibility_failures": failures,
    }


def enforce_wallet_decision_eligibility(
    *,
    verdict: str,
    verdict_family: Optional[str] = None,
    missing_essentials: Optional[Iterable[str]] = None,
    eligibility_failures: Optional[Iterable[str]] = None,
    resolved_markets: Optional[int],
    category_resolved_markets: Optional[int] = None,
    sample_fraction: Optional[float] = None,
    sharpe_ratio: Optional[float] = None,
    max_drawdown: Optional[float] = None,
    require_category_resolution: bool = True,
) -> dict[str, Any]:
    """Compatibility wrapper mirroring the persistence-row shape.

    Accepts the current ``wallet_score_decisions`` row shape (which has
    both ``verdict`` and ``verdict_family``) and returns the corrected
    values. ``verdict_family`` defaults to the post-correction
    ``verdict`` when omitted, which matches the existing writer logic
    in :func:`scripts.scan_pipeline_wiring._decision_verdict_family`.
    """
    derived = derive_wallet_verdict_from_evidence(
        verdict=verdict,
        resolved_markets=resolved_markets,
        category_resolved_markets=category_resolved_markets,
        sample_fraction=sample_fraction,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_drawdown,
        missing_essentials=missing_essentials,
        eligibility_failures=eligibility_failures,
        require_category_resolution=require_category_resolution,
    )
    out = dict(derived)
    if verdict_family is None:
        out["verdict_family"] = out["verdict"]
    else:
        # Keep verdict_family consistent with verdict. The schema CHECK
        # treats the four canonical values as a closed family, so any
        # time verdict is rewritten the family must follow.
        out["verdict_family"] = out["verdict"]
    return out


# ---------------------------------------------------------------------------
# WalletScoreResult / decision-record adapters
# ---------------------------------------------------------------------------


def apply_to_wallet_score_result(result: Any) -> Any:
    """Apply the guard to a ``WalletScoreResult``-like object.

    Returns a copy with ``verdict`` rewritten and the missing-eligibility
    fields populated. If ``result`` is a frozen dataclass, falls back to
    ``dataclasses.replace`` after constructing a new instance via
    ``copy.deepcopy`` so the input is never mutated.

    Recognized fields (looked up with ``getattr``):

      * ``verdict`` — string or ``WalletVerdict`` enum-like with ``.value``
      * ``missing_essentials`` — iterable of str
      * ``eligibility_gate_failures`` — iterable of str

    The result's ``input`` field is consulted for the resolved-evidence
    signals.
    """
    if result is None:
        return result

    inp = getattr(result, "input", None)
    if inp is None:
        # No typed input — fall back to attributes on the result itself.
        resolved_markets = getattr(result, "resolved_markets", None)
        category_resolved_markets = getattr(result, "category_resolved_markets", None)
        sample_fraction = getattr(result, "sample_fraction", None)
        sharpe_ratio = getattr(result, "sharpe_ratio", None)
        max_drawdown = getattr(result, "max_drawdown", None)
    else:
        resolved_markets = getattr(inp, "resolved_markets", None)
        category_resolved_markets = getattr(inp, "category_resolved_markets", None)
        sample_fraction = getattr(inp, "sample_fraction", None)
        sharpe_ratio = getattr(inp, "sharpe_ratio", None)
        max_drawdown = getattr(inp, "max_drawdown", None)

    verdict_obj = getattr(result, "verdict", None)
    verdict_str = (
        getattr(verdict_obj, "value", None) or str(verdict_obj or "")
    ).lower()

    corrected = enforce_wallet_decision_eligibility(
        verdict=verdict_str,
        missing_essentials=getattr(result, "missing_essentials", None),
        eligibility_failures=getattr(result, "eligibility_gate_failures", None),
        resolved_markets=resolved_markets,
        category_resolved_markets=category_resolved_markets,
        sample_fraction=sample_fraction,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_drawdown,
    )

    # Re-encode the corrected verdict back into the same enum if present.
    new_verdict = verdict_obj
    try:
        if verdict_obj is not None and hasattr(verdict_obj, "value"):
            # Same enum class, same case-insensitive value lookup.
            enum_cls = type(verdict_obj)
            try:
                new_verdict = enum_cls(corrected["verdict"])
            except (KeyError, ValueError):
                new_verdict = enum_cls(VERDICT_INCOMPLETE)
    except Exception:  # noqa: BLE001 — defensive
        new_verdict = corrected["verdict"]

    try:
        return replace(
            result,
            verdict=new_verdict,
            missing_essentials=corrected["missing_essentials"],
            eligibility_gate_failures=corrected["eligibility_failures"],
        )
    except Exception:  # noqa: BLE001 — fallback for non-dataclass
        copied = copy.deepcopy(result)
        try:
            setattr(copied, "verdict", new_verdict)
        except Exception:  # noqa: BLE001
            pass
        try:
            setattr(copied, "missing_essentials", corrected["missing_essentials"])
        except Exception:  # noqa: BLE001
            pass
        try:
            setattr(copied, "eligibility_gate_failures", corrected["eligibility_failures"])
        except Exception:  # noqa: BLE001
            pass
        return copied


def apply_to_decision_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the guard to a decision-row dict (the shape used by
    ``scan_pipeline_wiring``'s INSERT builder).

    Returns a new dict with corrected ``verdict``/``verdict_family``
    fields; original dict is not mutated.
    """
    resolved_markets = row.get("resolved_markets")
    category_resolved_markets = row.get("category_resolved_markets")
    sample_fraction = row.get("sample_fraction")
    sharpe_ratio = row.get("sharpe_ratio")
    max_drawdown = row.get("max_drawdown")
    # Some upstream callers store missing essentials under
    # ``missing_essentials`` and failures under
    # ``eligibility_failures``; if not, fall back to empty iterables.
    missing = row.get("missing_essentials") or []
    failures = row.get("eligibility_failures") or row.get("eligibility_gate_failures") or []

    verdict_str = row.get("verdict") or ""
    family = row.get("verdict_family")

    corrected = enforce_wallet_decision_eligibility(
        verdict=verdict_str,
        verdict_family=family,
        missing_essentials=missing,
        eligibility_failures=failures,
        resolved_markets=resolved_markets,
        category_resolved_markets=category_resolved_markets,
        sample_fraction=sample_fraction,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_drawdown,
    )

    out = dict(row)
    out["verdict"] = corrected["verdict"]
    out["verdict_family"] = corrected["verdict_family"]
    return out


# ---------------------------------------------------------------------------
# PR24G — Legacy decision-row repair planning
# ---------------------------------------------------------------------------
#
# Pre-PR27 / pre-PR24F / pre-PR24E rows can persist the old bad shape:
#
#   * verdict = "skip" with both reason buckets empty
#   * no resolved-market evidence
#
# These rows are auditable evidence of a contract violation. We don't
# want to silently delete them (that loses history), and we don't want
# to manually patch them with raw SQL (that hides the bug). Instead we
# want a tested, idempotent maintenance path that re-derives the row's
# verdict and reason buckets through the current shared guard so the
# persisted shape matches the current contract.
#
# This helper is the planning layer — it computes what WOULD change
# without touching the database. The companion script
# ``scripts/repair_legacy_decision_verdicts.py`` then either prints the
# plan (dry-run) or applies the planned UPDATE (apply).


def _coerce_json_list(raw: Any) -> list[str]:
    """Best-effort parse of a JSON-list column into ``list[str]``.

    SQLite TEXT columns store Python ``json.dumps(list)`` as ``"[]"``
    or ``"[\\\"a\\\"]"``. Tolerates None, empty string, malformed
    input, and non-list JSON values by returning ``[]``.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x is not None]
    if not isinstance(raw, str):
        return []
    s = raw.strip()
    if not s:
        return []
    try:
        parsed = json.loads(s)
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, list):
        return [str(x) for x in parsed if x is not None]
    return []


def _is_legacy_suspect_shape(
    *,
    verdict: Any,
    missing_essentials: list[str],
    eligibility_failures: list[str],
) -> bool:
    """Return True for the legacy pre-PR24F bad shape.

    Criteria (from the brief):
      * ``verdict`` is exactly the string ``"skip"`` (case-insensitive)
      * ``missing_essentials`` is empty
      * ``eligibility_failures`` is empty

    A row that ALREADY has the correct shape (non-empty buckets) is
    never re-repaired — the helper reports ``repair_needed=False``.
    """
    v = (str(verdict or "")).strip().lower()
    return v == VERDICT_SKIP and not missing_essentials and not eligibility_failures


def derive_legacy_wallet_decision_repair(row: Mapping[str, Any]) -> dict[str, Any]:
    """Compute the repair plan for a single ``wallet_score_decisions`` row.

    Pure function — does NOT touch the database. Returns a plan dict
    with the following keys::

        repair_needed (bool)
            True only when the row matches the legacy suspect shape AND
            the current shared guard would change either the verdict
            or the reason buckets. Rows that already satisfy PR24F are
            reported as ``repair_needed=False`` (no-op).

        old_verdict (str)
        old_missing_essentials (list[str])
        old_eligibility_failures (list[str])
        old_verdict_family (str | None)

        new_verdict (str)
        new_verdict_family (str)
        new_missing_essentials (list[str])
        new_eligibility_failures (list[str])

        wallet_id (str)
            Carried through from the input row for logging / scripting.

        row_id (int | None)
            Carried through from the input row when present.

        updated_payload (dict)
            A copy of ``row`` with verdict / verdict_family /
            missing_essentials / eligibility_failures fields rewritten
            to the new values. Empty when ``repair_needed`` is False.

        ambiguous_companion (bool)
            Always False here. The companion (``decision_verdicts``)
            linkage ambiguity is computed by the script, not the helper,
            because it requires a separate DB query.

    The helper is the single source of truth for "what should this row
    look like under PR24F". The companion script only decides whether
    the plan should be printed (dry-run) or applied (write).
    """
    # Parse the legacy JSON buckets safely.
    old_missing = _coerce_json_list(row.get("missing_essentials_json"))
    old_failures = _coerce_json_list(row.get("eligibility_failures_json"))
    old_verdict = row.get("verdict")
    old_family = row.get("verdict_family")

    # Short-circuit when the row already has the correct shape.
    legacy_shape = _is_legacy_suspect_shape(
        verdict=old_verdict,
        missing_essentials=old_missing,
        eligibility_failures=old_failures,
    )

    # Read the five evidence columns. The caller passes whatever they
    # have — None / 0 are both valid inputs to the guard.
    resolved_markets = row.get("resolved_markets")
    category_resolved_markets = row.get("category_resolved_markets")
    sample_fraction = row.get("sample_fraction")
    sharpe_ratio = row.get("sharpe_ratio")
    max_drawdown = row.get("max_drawdown")

    corrected = enforce_wallet_decision_eligibility(
        verdict=str(old_verdict or ""),
        verdict_family=old_family,
        missing_essentials=old_missing,
        eligibility_failures=old_failures,
        resolved_markets=resolved_markets,
        category_resolved_markets=category_resolved_markets,
        sample_fraction=sample_fraction,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_drawdown,
    )

    new_verdict = corrected["verdict"]
    new_family = corrected["verdict_family"]
    new_missing = list(corrected["missing_essentials"])
    new_failures = list(corrected["eligibility_failures"])

    # A row needs repair only if BOTH:
    #   (a) it has the legacy suspect shape (verdict=skip + empty buckets), AND
    #   (b) the current guard would actually rewrite it (verdict flipped
    #       or any reason bucket differs).
    # A row whose verdict is already correctly incomplete/skip with
    # proper buckets is left alone — re-running is idempotent.
    buckets_changed = (
        new_verdict != (str(old_verdict or "").lower())
        or new_missing != old_missing
        or new_failures != old_failures
    )
    repair_needed = legacy_shape and buckets_changed

    plan: dict[str, Any] = {
        "repair_needed": repair_needed,
        "old_verdict": str(old_verdict or ""),
        "old_verdict_family": old_family,
        "old_missing_essentials": old_missing,
        "old_eligibility_failures": old_failures,
        "new_verdict": new_verdict,
        "new_verdict_family": new_family,
        "new_missing_essentials": new_missing,
        "new_eligibility_failures": new_failures,
        "wallet_id": row.get("wallet_id"),
        "row_id": row.get("id"),
        "updated_payload": {},
        "ambiguous_companion": False,
    }

    if repair_needed:
        updated = dict(row)
        updated["verdict"] = new_verdict
        updated["verdict_family"] = new_family
        # Persist reason buckets as JSON strings (matches the schema).
        updated["missing_essentials_json"] = json.dumps(new_missing)
        updated["eligibility_failures_json"] = json.dumps(new_failures)
        plan["updated_payload"] = updated

    return plan


# ---------------------------------------------------------------------------
# PR24H — Category decision evidence guard
# ---------------------------------------------------------------------------
#
# Pre-PR24H the wallet-level guard chain (PR24E / PR27 / PR24F / PR24G)
# only protected ``wallet_score_decisions``. Category-specialist
# verdicts (``category_wallet_score_decisions``) and their companion
# ``decision_verdicts`` rows could in principle persist a real verdict
# (SKIP / WATCHLIST / COPY_CANDIDATE) with missing required category
# evidence — a different but symmetric safety gap to the one PR24E
# closed at the wallet level.
#
# PR24H extends the same shared-guard discipline to the category side
# so specialist scoring can be considered safe to enable later (PR20,
# category-specialist scoring) without re-introducing the silent-skip
# failure mode.
#
# Schema reality (verified from ``db/schema_v10.py``):
#
#   * ``category_wallet_score_decisions`` HAS:
#       - category_resolved_markets INTEGER
#       - sample_fraction           REAL
#       - sharpe_ratio              REAL
#       - max_drawdown              REAL
#       - missing_essentials_json   TEXT
#       - category_gate_failures_json TEXT
#
#   * ``category_wallet_score_decisions`` does NOT have:
#       - resolved_markets
#       - eligibility_failures_json  (the analog is
#                                    category_gate_failures_json)
#
#   * No schema migration is required. The required-evidence set
#     uses the four columns that already exist on the category row.
#
# Category required-evidence contract:
#
#   * ``category_resolved_markets`` — None or zero ⇒ missing resolution
#     evidence. The category table does not carry a separate
#     ``resolved_markets`` column (that field is wallet-level only).
#
#   * ``sample_fraction`` / ``sharpe_ratio`` / ``max_drawdown`` —
#     None ⇒ missing required non-resolution evidence. Numeric zero
#     is treated as a real measured value (matches the wallet policy
#     from PR24F: zero Sharpe / zero drawdown / zero sample_fraction
#     is information, not absence).
#
# PR27 invariant on the category side:
#
#   * A category decision whose ``verdict == "skip"`` is NEVER
#     persisted with both ``missing_essentials_json`` and
#     ``category_gate_failures_json`` empty. Two sub-cases:
#       - Resolution / required-evidence gap → forced INCOMPLETE
#         with reason buckets populated by the helper.
#       - Sufficient evidence and caller-supplied gate failures
#         empty → the helper appends ``score_below_copy_threshold``
#         to ``category_gate_failures`` so the row is auditable.
#         Verdict / verdict_family stay ``skip``.
#
# Canonical markers used here mirror the wallet-side markers:
#
#   * ``no_resolved_market_evidence`` — zero / missing category
#     resolution evidence.
#   * ``missing_required_evidence`` — any required non-resolution
#     category evidence field is missing.
#   * ``score_below_copy_threshold`` — score-driven category skip
#     with sufficient evidence and no caller-supplied failure.
#
# This module is pure-Python, deterministic, and has no I/O. It is
# safe to import from any layer (compute, persistence, wiring).


# Required category-evidence keys (PR24H contract). Mirrors the
# wallet-side keys but uses ``category_resolved_markets`` instead of
# ``resolved_markets`` (the category table doesn't have a
# ``resolved_markets`` column). The other three non-resolution keys
# already exist on the category row (per schema_v10).
CATEGORY_RESOLUTION_KEYS: tuple[str, ...] = (
    "category_resolved_markets",
)
CATEGORY_NON_RESOLUTION_KEYS: tuple[str, ...] = (
    "sample_fraction",
    "sharpe_ratio",
    "max_drawdown",
)
CATEGORY_REQUIRED_EVIDENCE_KEYS: tuple[str, ...] = (
    *CATEGORY_RESOLUTION_KEYS,
    *CATEGORY_NON_RESOLUTION_KEYS,
)


def lacks_category_resolution_evidence(
    category_resolved_markets: Optional[int],
) -> bool:
    """Return ``True`` when the wallet has no usable category resolution
    evidence.

    Either an outright-missing or zero-valued
    ``category_resolved_markets`` is treated as insufficient. This is
    the category-side analog of
    :func:`lacks_resolution_evidence` and is intentionally narrow —
    the category table does not carry a separate ``resolved_markets``
    column, so there is only one resolution-count field to inspect.
    """
    if category_resolved_markets is None:
        return True
    if isinstance(category_resolved_markets, (int, float)):
        return category_resolved_markets == 0
    return False


def _category_is_missing(key: str, value: Any) -> bool:
    """Return ``True`` when ``value`` is unusable as category evidence
    for ``key`` (PR24H key-aware rule).

    Mirrors :func:`_is_missing` semantics:

      * ``category_resolved_markets``: None or zero ⇒ missing.
      * ``sample_fraction`` / ``sharpe_ratio`` / ``max_drawdown``:
        None ⇒ missing. Numeric zero is treated as present (a real
        measurement, not absence).
    """
    if value is None:
        return True
    if key in CATEGORY_RESOLUTION_KEYS:
        if isinstance(value, (int, float)) and value == 0:
            return True
    return False


def _build_category_missing_essentials(
    *,
    category_resolved_markets: Optional[int],
    sample_fraction: Optional[float],
    sharpe_ratio: Optional[float],
    max_drawdown: Optional[float],
    existing: Optional[Iterable[str]] = None,
) -> list[str]:
    """Assemble a deduplicated, order-preserving list of missing
    category essentials.

    Always-on rules:

      * ``category_resolved_markets`` missing → added.
      * ``sample_fraction`` missing → added.
      * ``sharpe_ratio`` missing → added.
      * ``max_drawdown`` missing → added.

    Anything in ``existing`` is preserved so the helper composes
    with upstream "essential evidence" checks (e.g. missing
    ``trade_count`` carried by the typed ``CategoryWalletScoreInputV1``).
    """
    out: list[str] = []
    seen: set[str] = set()
    if existing:
        for item in existing:
            if item and item not in seen:
                out.append(item)
                seen.add(item)
    candidates: list[tuple[str, Any]] = [
        ("category_resolved_markets", category_resolved_markets),
        ("sample_fraction", sample_fraction),
        ("sharpe_ratio", sharpe_ratio),
        ("max_drawdown", max_drawdown),
    ]
    for key, value in candidates:
        if _category_is_missing(key, value) and key not in seen:
            out.append(key)
            seen.add(key)
    return out


def _build_category_gate_failures(
    *,
    lacks_resolution: bool,
    missing_required_non_resolution: bool,
    existing: Optional[Iterable[str]] = None,
) -> list[str]:
    """Assemble a deduplicated list of category gate failures.

    Mirrors :func:`_build_eligibility_failures` but uses the
    category-side canonical markers. ``existing`` is preserved so the
    helper composes with upstream gate failures carried by
    ``CategoryWalletScoreResultV1.category_gate_failures``.
    """
    out: list[str] = []
    seen: set[str] = set()
    if existing:
        for item in existing:
            if item and item not in seen:
                out.append(item)
                seen.add(item)
    if lacks_resolution and NO_RESOLVED_MARKET_EVIDENCE not in seen:
        out.append(NO_RESOLVED_MARKET_EVIDENCE)
        seen.add(NO_RESOLVED_MARKET_EVIDENCE)
    if (
        missing_required_non_resolution
        and MISSING_REQUIRED_EVIDENCE not in seen
    ):
        out.append(MISSING_REQUIRED_EVIDENCE)
        seen.add(MISSING_REQUIRED_EVIDENCE)
    return out


def derive_category_verdict_from_evidence(
    *,
    verdict: str,
    category_resolved_markets: Optional[int],
    sample_fraction: Optional[float] = None,
    sharpe_ratio: Optional[float] = None,
    max_drawdown: Optional[float] = None,
    missing_essentials: Optional[Iterable[str]] = None,
    category_gate_failures: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Compute the corrected category decision-record payload.

    Returns a dict with keys::

        verdict               (str)
        verdict_family        (str)  -- mirrors verdict
        missing_essentials    (list[str])
        category_gate_failures (list[str])

    PR24H contract — the helper enforces two completeness rules that
    must hold BEFORE any real verdict (SKIP / WATCHLIST /
    COPY_CANDIDATE) can be emitted:

      Rule 1a (zero category-resolution evidence): if
      ``category_resolved_markets`` is ``None`` or zero, the verdict
      is forced to ``incomplete`` and ``category_gate_failures``
      includes ``no_resolved_market_evidence``.

      Rule 1b (missing required non-resolution evidence): if Rule 1a
      does not fire AND any of ``sample_fraction``, ``sharpe_ratio``,
      ``max_drawdown`` is ``None``, the verdict is forced to
      ``incomplete`` and ``category_gate_failures`` includes
      ``missing_required_evidence``. (Numeric zero values are treated
      as present for these three fields.)

    When both rules leave the verdict untouched (i.e. all required
    evidence is present), PR27's no-silent-skip rule still fires: a
    SKIP with no caller-supplied gate failure is annotated with
    ``score_below_copy_threshold`` so the persisted row is never
    silent.

    The function is pure and deterministic.
    """
    v = (verdict or "").lower()
    if v not in CANONICAL_FAMILY:
        v = VERDICT_INCOMPLETE

    lacks_resolution = lacks_category_resolution_evidence(
        category_resolved_markets,
    )

    missing = _build_category_missing_essentials(
        category_resolved_markets=category_resolved_markets,
        sample_fraction=sample_fraction,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_drawdown,
        existing=missing_essentials,
    )

    # Rule 1b fires only when resolution evidence IS present but a
    # non-resolution required field is missing.
    missing_required_non_resolution = any(
        key in missing for key in CATEGORY_NON_RESOLUTION_KEYS
    )

    failures = _build_category_gate_failures(
        lacks_resolution=lacks_resolution,
        missing_required_non_resolution=missing_required_non_resolution,
        existing=category_gate_failures,
    )

    # ------------------------------------------------------------------
    # Rule 1a — zero category-resolution evidence ⇒ force INCOMPLETE.
    # ------------------------------------------------------------------
    if lacks_resolution:
        v = VERDICT_INCOMPLETE

    # ------------------------------------------------------------------
    # Rule 1b — missing required non-resolution evidence ⇒ force
    # INCOMPLETE (only when Rule 1a did not fire). A category decision
    # with sufficient resolution evidence but a missing
    # sample/risk field is still not eligible for a real verdict.
    # ------------------------------------------------------------------
    elif missing_required_non_resolution:
        v = VERDICT_INCOMPLETE

    # ------------------------------------------------------------------
    # PR27 invariant — a category "skip" must never have BOTH
    # ``missing_essentials`` empty AND ``category_gate_failures``
    # empty.
    #
    # (a) Verdict forced to INCOMPLETE by Rule 1a/1b — reason buckets
    #     populated above.
    # (b) Verdict is SKIP with all required evidence present and no
    #     caller-supplied gate failure — append the canonical marker
    #     so the row is auditable. The verdict stays ``skip``.
    # ------------------------------------------------------------------
    if v == VERDICT_INCOMPLETE:
        # Defensive: if Rule 1a/1b fired but the helper didn't end up
        # populating any reason bucket (theoretically possible when
        # existing=[] and zero/missing resolution was an int 0 the
        # helper treated as "present" via key-aware rules), attach a
        # marker so the row is never silent.
        if not missing and not failures:
            if lacks_resolution:
                failures = [NO_RESOLVED_MARKET_EVIDENCE]
            else:
                failures = [MISSING_REQUIRED_EVIDENCE]

    elif v == VERDICT_SKIP:
        # Sufficient resolution + required non-resolution evidence.
        # Preserve the verdict; guarantee ``category_gate_failures``
        # is non-empty.
        if not failures:
            failures = [SCORE_BELOW_COPY_THRESHOLD]

    return {
        "verdict": v,
        "verdict_family": v,
        "missing_essentials": missing,
        "category_gate_failures": failures,
    }


def enforce_category_decision_eligibility(
    *,
    verdict: str,
    verdict_family: Optional[str] = None,
    missing_essentials: Optional[Iterable[str]] = None,
    category_gate_failures: Optional[Iterable[str]] = None,
    category_resolved_markets: Optional[int],
    sample_fraction: Optional[float] = None,
    sharpe_ratio: Optional[float] = None,
    max_drawdown: Optional[float] = None,
) -> dict[str, Any]:
    """Compatibility wrapper for the category row shape.

    Accepts a category decision payload and returns the corrected
    verdict / verdict_family / missing_essentials /
    category_gate_failures. ``verdict_family`` is always made
    consistent with the corrected ``verdict``.
    """
    derived = derive_category_verdict_from_evidence(
        verdict=verdict,
        category_resolved_markets=category_resolved_markets,
        sample_fraction=sample_fraction,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_drawdown,
        missing_essentials=missing_essentials,
        category_gate_failures=category_gate_failures,
    )
    out = dict(derived)
    # Keep verdict_family consistent with verdict (the canonical
    # four-way family is closed; any verdict rewrite must be
    # followed by verdict_family).
    out["verdict_family"] = out["verdict"]
    return out


def apply_to_category_score_result(result: Any) -> Any:
    """Apply the category guard to a ``CategoryWalletScoreResultV1``-like
    object.

    Reads the category evidence columns from the typed input attached
    at ``result.input`` when present (the runtime path); falls back to
    attributes on the result itself when no typed input is attached
    (legacy / test fixtures).

    Recognized fields (looked up with ``getattr``):

      * ``verdict`` — string or ``WalletVerdict`` enum-like with
        ``.value``
      * ``missing_essentials`` — iterable of str
      * ``category_gate_failures`` — iterable of str

    Returns a copy with ``verdict`` rewritten and the reason buckets
    populated. The input is never mutated.
    """
    if result is None:
        return result

    inp = getattr(result, "input", None)
    if inp is None:
        category_resolved_markets = getattr(
            result, "category_resolved_markets", None
        )
        sample_fraction = getattr(result, "sample_fraction", None)
        sharpe_ratio = getattr(result, "sharpe_ratio", None)
        max_drawdown = getattr(result, "max_drawdown", None)
    else:
        category_resolved_markets = getattr(
            inp, "category_resolved_markets", None
        )
        sample_fraction = getattr(inp, "sample_fraction", None)
        sharpe_ratio = getattr(inp, "sharpe_ratio", None)
        max_drawdown = getattr(inp, "max_drawdown", None)

    verdict_obj = getattr(result, "verdict", None)
    verdict_str = (
        getattr(verdict_obj, "value", None) or str(verdict_obj or "")
    ).lower()

    corrected = enforce_category_decision_eligibility(
        verdict=verdict_str,
        missing_essentials=getattr(result, "missing_essentials", None),
        category_gate_failures=getattr(
            result, "category_gate_failures", None
        ),
        category_resolved_markets=category_resolved_markets,
        sample_fraction=sample_fraction,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_drawdown,
    )

    new_verdict = verdict_obj
    try:
        if verdict_obj is not None and hasattr(verdict_obj, "value"):
            enum_cls = type(verdict_obj)
            try:
                new_verdict = enum_cls(corrected["verdict"])
            except (KeyError, ValueError):
                new_verdict = enum_cls(VERDICT_INCOMPLETE)
    except Exception:  # noqa: BLE001 — defensive
        new_verdict = corrected["verdict"]

    try:
        return replace(
            result,
            verdict=new_verdict,
            missing_essentials=corrected["missing_essentials"],
            category_gate_failures=corrected["category_gate_failures"],
        )
    except Exception:  # noqa: BLE001 — fallback for non-dataclass
        copied = copy.deepcopy(result)
        try:
            setattr(copied, "verdict", new_verdict)
        except Exception:  # noqa: BLE001
            pass
        try:
            setattr(copied, "missing_essentials", corrected["missing_essentials"])
        except Exception:  # noqa: BLE001
            pass
        try:
            setattr(
                copied,
                "category_gate_failures",
                corrected["category_gate_failures"],
            )
        except Exception:  # noqa: BLE001
            pass
        return copied


__all__ = [
    "VERDICT_INCOMPLETE",
    "VERDICT_SKIP",
    "VERDICT_WATCHLIST",
    "VERDICT_COPY_CANDIDATE",
    "CANONICAL_FAMILY",
    "NO_RESOLVED_MARKET_EVIDENCE",
    "MISSING_REQUIRED_EVIDENCE",
    "SCORE_BELOW_COPY_THRESHOLD",
    "RESOLUTION_EVIDENCE_KEYS",
    "NON_RESOLUTION_REQUIRED_KEYS",
    "REQUIRED_EVIDENCE_KEYS",
    "ALL_EVIDENCE_KEYS",
    "CATEGORY_RESOLUTION_KEYS",
    "CATEGORY_NON_RESOLUTION_KEYS",
    "CATEGORY_REQUIRED_EVIDENCE_KEYS",
    "lacks_resolution_evidence",
    "lacks_category_resolution_evidence",
    "derive_wallet_verdict_from_evidence",
    "enforce_wallet_decision_eligibility",
    "apply_to_wallet_score_result",
    "apply_to_decision_row",
    "derive_legacy_wallet_decision_repair",
    "derive_category_verdict_from_evidence",
    "enforce_category_decision_eligibility",
    "apply_to_category_score_result",
]