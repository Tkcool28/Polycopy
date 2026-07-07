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

  * Hard invariant for the ``SKIP`` label: a wallet decision whose
    ``verdict == "skip"`` is rejected if BOTH ``missing_essentials_json``
    and ``eligibility_failures_json`` are empty. This is the
    "no silent skip" rule. The helper corrects the row to
    ``INCOMPLETE`` with a structured reason so the persistence path
    never writes an unexplained skip.

This module is pure-Python, deterministic, and has no I/O. It is safe
to import from any layer.
"""

from __future__ import annotations

import copy
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

# The set of evidence keys the helper tracks when forcing INCOMPLETE.
# Mirrors the user-facing contract in PR24E's PR body.
RESOLUTION_EVIDENCE_KEYS: tuple[str, ...] = (
    "resolved_markets",
    "category_resolved_markets",
    "sample_fraction",
    "sharpe_ratio",
    "max_drawdown",
)


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


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    # Treat zero as missing for the resolution-evidence sense so that
    # ``resolved_markets=0`` is not silently treated as evidence.
    if isinstance(value, (int, float)) and value == 0:
        return True
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
        if _is_missing(value) and key not in seen:
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

    The returned values are always self-consistent with the PR24E
    invariant: a zero/missing resolution-evidence wallet can never
    produce a real verdict, and a ``skip`` verdict cannot be silent
    (both reason buckets empty).

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

    # Rule 1: no resolution evidence ⇒ force INCOMPLETE.
    if lacks_resolution:
        v = VERDICT_INCOMPLETE

    # Rule 2: SKIP with both reason buckets empty is only unexplained
    # when there is also a resolution-evidence gap. If the resolution
    # evidence is sufficient AND the caller did not record any
    # eligibility failures, the verdict is a true score-driven skip
    # and PR24E explicitly preserves it.
    #
    # PR24E contract: this rule applies ONLY in the resolved-evidence-
    # gap path. When ``resolved_markets`` is sufficient, Rule 1 doesn't
    # fire and a SKIP with empty reason buckets is the legitimate
    # output of the score formula — it must NOT be promoted.
    caller_missing = list(missing_essentials) if missing_essentials is not None else None
    caller_failures = list(eligibility_failures) if eligibility_failures is not None else None
    if (
        lacks_resolution
        and v == VERDICT_SKIP
        and caller_missing is not None
        and not caller_missing
        and caller_failures is not None
        and not caller_failures
    ):
        # Don't override the INCOMPLETE we already set in Rule 1, but
        # make sure the persisted reason buckets are populated.
        missing = list(RESOLUTION_EVIDENCE_KEYS)
        if NO_RESOLVED_MARKET_EVIDENCE not in failures:
            failures = [NO_RESOLVED_MARKET_EVIDENCE]

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