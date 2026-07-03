"""Typed V2 shadow scoring engine (Chunk 5 — Phase 9).

This module is the *V2 shadow* research engine. It is intentionally
isolated from V1 (paper signal / live) — V2 outputs are never
allowed to influence V1 verdicts.

The compute function takes a frozen :class:`ShadowScoreInputV2` and
returns a frozen :class:`ShadowScoreResultV2`. The result keeps the
exact typed input so the decision is replayable.

Score composition (frozen weights, sum=100):

    delayed_entry_alpha:           30
    tradeable_price_retention:      20
    execution_feasibility:          15
    skill_persistence:              15
    copied_realized_performance:    10
    concentration_correlation:      10

Verdict rules:

    score >= 70  -> SHADOW_COPY_CANDIDATE
    50 <= score < 70 -> SHADOW_WATCHLIST
    score < 50   -> SHADOW_SKIP
    missing essential evidence -> SHADOW_INCOMPLETE

Missing-evidence policy:

    The engine NEVER silently substitutes zero for missing forward
    evidence. When ``input.missing_forward_reasons`` is non-empty
    OR a required component cannot be computed (e.g. missing
    source price for the delayed-entry alpha component), the
    result is ``SHADOW_INCOMPLETE`` with the missing reasons
    recorded verbatim.
"""

from __future__ import annotations

from typing import List, Tuple

from polycopy.scoring.helpers import clamp, linear_score, inverse_score
from polycopy.scoring.shadow_score_v2_typed import (
    DelayScenario,
    REASON_NO_CONCENTRATION,
    REASON_NO_COPIED_PERFORMANCE,
    REASON_NO_DELAYED_PRICE,
    REASON_NO_EXECUTION_EVIDENCE,
    REASON_NO_MEASURED_DELAY,
    REASON_NO_SOURCE_PRICE,
    REASON_NO_WALLET_PERSISTENCE,
    SHADOW_FORMULA_NAME,
    SHADOW_FORMULA_VERSION,
    SHADOW_WEIGHTS,
    ShadowScoreInputV2,
    ShadowScoreResultV2,
    VERDICT_COPY_CANDIDATE_MIN,
    VERDICT_SHADOW_COPY_CANDIDATE,
    VERDICT_SHADOW_INCOMPLETE,
    VERDICT_SHADOW_SKIP,
    VERDICT_SHADOW_WATCHLIST,
    VERDICT_WATCHLIST_MIN,
)


# ---- Component helpers ---------------------------------------------------

def _delayed_entry_alpha_component(
    inp: ShadowScoreInputV2,
) -> Tuple[float, str, str, List[str]]:
    """Score: lower delay + stronger alpha = better.

    For THEORETICAL_IMMEDIATE: delay=0, alpha derived from source
    vs trade price. For other scenarios: delay = scenario delay
    (or measured delay for ACTUAL_MEASURED_DELAY), alpha from the
    relative price change between source and delayed copy prices.

    Returns (raw_score, quality, formula_note, missing_reasons).
    Missing required evidence → missing_reasons is non-empty.
    """
    missing: List[str] = []

    delay_seconds: float
    if inp.delay_scenario is DelayScenario.THEORETICAL_IMMEDIATE:
        delay_seconds = 0.0
    elif inp.delay_scenario is DelayScenario.ACTUAL_MEASURED_DELAY:
        if inp.measured_delay_seconds is None:
            missing.append(REASON_NO_MEASURED_DELAY)
            return 0.0, "unknown", "actual measured delay missing", missing
        delay_seconds = float(inp.measured_delay_seconds)
    else:
        delay_seconds = float(inp.delay_scenario_seconds)

    if inp.source_price is None:
        missing.append(REASON_NO_SOURCE_PRICE)
    if inp.delayed_copy_price is None:
        missing.append(REASON_NO_DELAYED_PRICE)
    if missing:
        return 0.0, "unknown", "source or delayed price missing", missing

    # Alpha derived from price ratio: positive when delayed_copy
    # is on a more favorable side of the book than source. For
    # BUY scenarios we treat a lower delayed_copy price as more
    # favorable (alpha > 0); for SELL the inverse applies.
    # We don't know the side from the typed input here, so we
    # use a neutral symmetric alpha.
    src = float(inp.source_price)
    dly = float(inp.delayed_copy_price)
    if src <= 0.0:
        missing.append(REASON_NO_SOURCE_PRICE)
        return 0.0, "unknown", "source price <= 0", missing
    rel_change = (src - dly) / src  # positive when delayed < source

    # Map rel_change into [-0.2, +0.2] → [0, 100].
    alpha_score = clamp(linear_score(rel_change, -0.2, 0.2))

    # Delay penalty (inverted: faster is better).
    max_delay = 900.0
    delay_score = clamp(linear_score(max(0.0, max_delay - delay_seconds), 0.0, max_delay))

    raw = (delay_score * 0.3 + alpha_score * 0.7)
    return raw, "calculated", (
        f"scenario={inp.delay_scenario.value} delay={delay_seconds:.0f}s "
        f"alpha={rel_change:.4f}"
    ), missing


def _tradeable_price_retention_component(
    inp: ShadowScoreInputV2,
) -> Tuple[float, str, str, List[str]]:
    """Score: how much of the favorable price move remains."""
    missing: List[str] = []
    if (
        inp.source_price is None
        or inp.delayed_copy_price is None
        or inp.source_price <= 0.0
    ):
        missing.append(REASON_NO_DELAYED_PRICE)
        return 0.0, "unknown", "price retention missing", missing
    src = float(inp.source_price)
    dly = float(inp.delayed_copy_price)
    # Retention = 1 - (move relative to source). Clamp to [0, 1].
    retention = 1.0 - abs((dly - src) / src)
    retention = max(0.0, min(1.0, retention))
    raw = clamp(retention * 100.0)
    return raw, "calculated", "1 - |delayed - source| / source", missing


def _execution_feasibility_component(
    inp: ShadowScoreInputV2,
) -> Tuple[float, str, str, List[str]]:
    """Score: realistic execution — slippage + fill."""
    missing: List[str] = []
    if inp.slippage is None and inp.fill_percentage is None:
        missing.append(REASON_NO_EXECUTION_EVIDENCE)
        return 0.0, "unknown", "no execution evidence", missing

    slip_score = 100.0
    if inp.slippage is not None:
        slip_score = clamp(inverse_score(float(inp.slippage), 0.0, 0.10))

    fill_score = 100.0
    if inp.fill_percentage is not None:
        fill_score = clamp(float(inp.fill_percentage) * 100.0)

    if inp.slippage is None:
        raw = fill_score
    elif inp.fill_percentage is None:
        raw = slip_score
    else:
        raw = (slip_score + fill_score) / 2.0

    return (
        raw,
        "calculated",
        f"avg(inverse(slippage,0,0.1), fill_pct*100); "
        f"slippage={inp.slippage} fill={inp.fill_percentage}",
        missing,
    )


def _skill_persistence_component(
    inp: ShadowScoreInputV2,
) -> Tuple[float, str, str, List[str]]:
    """Score: wallet skill persistence signal."""
    missing: List[str] = []
    if inp.wallet_skill_persistence_input is None:
        missing.append(REASON_NO_WALLET_PERSISTENCE)
        return 0.0, "unknown", "no wallet persistence input", missing
    raw = clamp(float(inp.wallet_skill_persistence_input))
    return raw, "calculated", "wallet_skill_persistence_input", missing


def _copied_realized_performance_component(
    inp: ShadowScoreInputV2,
) -> Tuple[float, str, str, List[str]]:
    """Score: realized performance of previously copied trades."""
    missing: List[str] = []
    if inp.copied_realized_performance_input is None:
        missing.append(REASON_NO_COPIED_PERFORMANCE)
        return 0.0, "unknown", "no copied realized performance input", missing
    raw = clamp(float(inp.copied_realized_performance_input))
    return raw, "calculated", "copied_realized_performance_input", missing


def _concentration_correlation_component(
    inp: ShadowScoreInputV2,
) -> Tuple[float, str, str, List[str]]:
    """Score: concentration / correlation safety."""
    missing: List[str] = []
    if inp.concentration_correlation_input is None:
        missing.append(REASON_NO_CONCENTRATION)
        return 0.0, "unknown", "no concentration/correlation input", missing
    raw = clamp(float(inp.concentration_correlation_input))
    return raw, "calculated", "concentration_correlation_input", missing


# ---- Verdict decision ----------------------------------------------------


def _classify_verdict(score: float) -> str:
    if score >= VERDICT_COPY_CANDIDATE_MIN:
        return VERDICT_SHADOW_COPY_CANDIDATE
    if score >= VERDICT_WATCHLIST_MIN:
        return VERDICT_SHADOW_WATCHLIST
    return VERDICT_SHADOW_SKIP


# ---- Public API ----------------------------------------------------------


def compute_shadow_score_v2_from_input(
    inp: ShadowScoreInputV2,
) -> ShadowScoreResultV2:
    """Compute V2 shadow score from a frozen typed input.

    Returns a frozen :class:`ShadowScoreResultV2`. The result
    retains the exact typed input.

    The engine NEVER silently substitutes zero for missing forward
    evidence: every missing reason is surfaced in the result's
    ``missing_forward_reasons`` tuple.
    """
    # Aggregate missing reasons from callers and components.
    reasons: List[str] = list(inp.missing_forward_reasons)
    component_records: List[dict] = []

    def _run(
        name: str,
        weight: float,
        run,
    ) -> float:
        raw, quality, formula_note, missing = run(inp)
        if missing:
            reasons.extend(missing)
        weighted = raw * (weight / 100.0)
        component_records.append(
            {
                "name": name,
                "raw_score": round(raw, 4),
                "weight": weight,
                "quality": quality,
                "formula": formula_note,
                "note": formula_note,
                "weighted_score": round(weighted, 4),
            }
        )
        return weighted

    weighted_total = 0.0
    weighted_total += _run(
        "delayed_entry_alpha",
        SHADOW_WEIGHTS["delayed_entry_alpha"],
        _delayed_entry_alpha_component,
    )
    weighted_total += _run(
        "tradeable_price_retention",
        SHADOW_WEIGHTS["tradeable_price_retention"],
        _tradeable_price_retention_component,
    )
    weighted_total += _run(
        "execution_feasibility",
        SHADOW_WEIGHTS["execution_feasibility"],
        _execution_feasibility_component,
    )
    weighted_total += _run(
        "skill_persistence",
        SHADOW_WEIGHTS["skill_persistence"],
        _skill_persistence_component,
    )
    weighted_total += _run(
        "copied_realized_performance",
        SHADOW_WEIGHTS["copied_realized_performance"],
        _copied_realized_performance_component,
    )
    weighted_total += _run(
        "concentration_correlation",
        SHADOW_WEIGHTS["concentration_correlation"],
        _concentration_correlation_component,
    )

    final_score = clamp(round(weighted_total, 4))

    # SHADOW_INCOMPLETE when ANY missing reason is present, OR the
    # score's primary dependency (delayed-entry alpha) was missing.
    primary_missing = any(
        r in reasons for r in (
            REASON_NO_SOURCE_PRICE,
            REASON_NO_DELAYED_PRICE,
            REASON_NO_MEASURED_DELAY,
        )
    )
    if reasons or primary_missing:
        verdict = VERDICT_SHADOW_INCOMPLETE
        final_score = 0.0
    else:
        verdict = _classify_verdict(final_score)

    return ShadowScoreResultV2(
        wallet_id=inp.wallet_id,
        source_trade_id=inp.source_trade_id,
        candidate_id=inp.candidate_id,
        delay_scenario=inp.delay_scenario,
        score=final_score,
        verdict=verdict,
        input=inp,
        component_scores=tuple(component_records),
        missing_forward_reasons=tuple(sorted(set(reasons))),
        formula_name=SHADOW_FORMULA_NAME,
        formula_version=SHADOW_FORMULA_VERSION,
        measured_delay_seconds=inp.measured_delay_seconds,
    )


def compute_measured_delay_seconds(
    *,
    source_trade_timestamp: str,
    candidate_snapshot_timestamp: str,
) -> float:
    """Derive the actual measured delay between the source trade and
    the candidate's snapshot.

    Both timestamps MUST be ISO-8601 (the canonical form used
    throughout the runtime). Returns the absolute difference in
    seconds, clamped to >= 0.
    """
    src = _parse_iso(source_trade_timestamp)
    snap = _parse_iso(candidate_snapshot_timestamp)
    return max(0.0, (snap - src).total_seconds())


def _parse_iso(value: str):
    from datetime import datetime
    # Accept trailing 'Z' for UTC.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt