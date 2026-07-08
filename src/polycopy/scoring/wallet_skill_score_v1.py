"""Pure Wallet Skill Score v1 formula for PR24M.

This module consumes PR24L ``WalletScoringInputCandidate`` objects and returns a
transparent, versioned score result.  It deliberately does not query databases,
rank wallets, create copy candidates, place orders, or wire into automation.
"""

from __future__ import annotations

from dataclasses import dataclass

from polycopy.engine.wallet_scoring_inputs import WalletScoringInputCandidate

WALLET_SKILL_FORMULA_NAME = "Persistent Specialist Wallet Score"
WALLET_SKILL_FORMULA_VERSION = "1"

VERDICT_INCOMPLETE = "incomplete"
VERDICT_SKIP = "skip"
VERDICT_WATCHLIST = "watchlist"
VERDICT_COPY_CANDIDATE = "copy_candidate"

SUPPORTED_IDENTITY_GROUP_BY = "trader_address"
REASON_UNSUPPORTED_IDENTITY_GROUPING = "unsupported_identity_grouping"
REASON_INSUFFICIENT_ACCOUNTED_TRADE_SAMPLE = "insufficient_accounted_trade_sample"
WARNING_BUY_ONLY_ACCOUNTING_LIMITATION = "buy_only_accounting_limitation"

COMPONENT_INFORMATION_AND_PRICE_IMPROVEMENT_QUALITY = (
    "information_and_price_improvement_quality"
)
COMPONENT_VERIFIED_REALIZED_PERFORMANCE = "verified_realized_performance"
COMPONENT_CHRONOLOGICAL_CONSISTENCY = "chronological_consistency"
COMPONENT_RISK_AND_DRAWDOWN_QUALITY = "risk_and_drawdown_quality"
COMPONENT_SAMPLE_RELIABILITY = "sample_reliability"
COMPONENT_CATEGORY_SPECIALIZATION = "category_specialization"
COMPONENT_CONCENTRATION_QUALITY = "concentration_quality"

WALLET_SKILL_SCORE_WEIGHTS_V1: dict[str, float] = {
    COMPONENT_INFORMATION_AND_PRICE_IMPROVEMENT_QUALITY: 30.0,
    COMPONENT_VERIFIED_REALIZED_PERFORMANCE: 15.0,
    COMPONENT_CHRONOLOGICAL_CONSISTENCY: 15.0,
    COMPONENT_RISK_AND_DRAWDOWN_QUALITY: 10.0,
    COMPONENT_SAMPLE_RELIABILITY: 10.0,
    COMPONENT_CATEGORY_SPECIALIZATION: 15.0,
    COMPONENT_CONCENTRATION_QUALITY: 5.0,
}


@dataclass(frozen=True)
class WalletSkillScoreComponentV1:
    name: str
    weight: float
    raw_value: object | None
    normalized_score: float | None
    weighted_score: float
    quality: str
    missing: bool
    blocking: bool
    note: str


@dataclass(frozen=True)
class WalletSkillScoreResultV1:
    identity_key: str
    identity_group_by: str | None
    formula_name: str
    formula_version: str
    score: float
    verdict: str
    ready_for_skill_score: bool
    ready_for_auto_copy: bool
    eligible_for_ranking: bool
    eligible_for_auto_copy: bool
    components: tuple[WalletSkillScoreComponentV1, ...]
    missing_essentials: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    raw_inputs: dict[str, object]
    weights: dict[str, float]


@dataclass(frozen=True)
class WalletSkillScoreConfigV1:
    copy_candidate_min_score: float = 75.0
    watchlist_min_score: float = 55.0
    min_accounted_trades: int = 30
    min_accounting_coverage_pct: float = 0.80
    require_price_improvement_evidence_for_copy_candidate: bool = True
    require_risk_evidence_for_copy_candidate: bool = True


def clamp_0_100(value: float) -> float:
    """Clamp a numeric score into the inclusive 0..100 range."""

    return max(0.0, min(100.0, float(value)))


def linear_score(value: float, low: float, high: float) -> float:
    """Return 0 at/below low, 100 at/above high, and linear between."""

    if high <= low:
        raise ValueError("high must be greater than low")
    numeric_value = float(value)
    if numeric_value <= low:
        return 0.0
    if numeric_value >= high:
        return 100.0
    return clamp_0_100(((numeric_value - low) / (high - low)) * 100.0)


def midpoint_linear_score(
    value: float,
    low_zero: float,
    mid_fifty: float,
    high_hundred: float,
) -> float:
    """Piecewise linear score anchored at 0/50/100 points."""

    if not low_zero < mid_fifty < high_hundred:
        raise ValueError("expected low_zero < mid_fifty < high_hundred")
    numeric_value = float(value)
    if numeric_value <= low_zero:
        return 0.0
    if numeric_value == mid_fifty:
        return 50.0
    if numeric_value >= high_hundred:
        return 100.0
    if numeric_value < mid_fifty:
        return clamp_0_100(
            ((numeric_value - low_zero) / (mid_fifty - low_zero)) * 50.0
        )
    return clamp_0_100(
        50.0 + ((numeric_value - mid_fifty) / (high_hundred - mid_fifty)) * 50.0
    )


def average_available(scores: tuple[float | None, ...] | list[float | None]) -> float | None:
    """Average non-None scores, returning None when no scores are available."""

    available = [float(score) for score in scores if score is not None]
    if not available:
        return None
    return sum(available) / len(available)


def compute_wallet_skill_score_v1(
    candidate: WalletScoringInputCandidate,
    *,
    config: WalletSkillScoreConfigV1 | None = None,
) -> WalletSkillScoreResultV1:
    """Compute a pure, transparent Wallet Skill Score v1 result."""

    active_config = config or WalletSkillScoreConfigV1()
    warnings = list(candidate.warnings)
    blocked_reasons = list(candidate.blocked_reasons)
    missing_essentials: list[str] = []

    if candidate.candidate_status == "blocked" or not candidate.ready_for_skill_score:
        return _result(
            candidate,
            score=0.0,
            verdict=VERDICT_INCOMPLETE,
            components=(),
            missing_essentials=_dedupe((*candidate.blocked_reasons,)),
            blocked_reasons=_dedupe((*blocked_reasons,)),
            warnings=_dedupe((*warnings,)),
            eligible_for_ranking=False,
            eligible_for_auto_copy=False,
        )

    if candidate.identity_group_by != SUPPORTED_IDENTITY_GROUP_BY:
        blocked_reasons.append(REASON_UNSUPPORTED_IDENTITY_GROUPING)
        return _result(
            candidate,
            score=0.0,
            verdict=VERDICT_INCOMPLETE,
            components=(),
            missing_essentials=(REASON_UNSUPPORTED_IDENTITY_GROUPING,),
            blocked_reasons=_dedupe((*blocked_reasons,)),
            warnings=_dedupe((*warnings,)),
            eligible_for_ranking=False,
            eligible_for_auto_copy=False,
        )

    components = _components(candidate, active_config)
    for component in components:
        if component.missing and component.blocking:
            missing_essentials.append(component.name)

    sample_component = _component_by_name(components, COMPONENT_SAMPLE_RELIABILITY)
    if candidate.accounted_trades < active_config.min_accounted_trades:
        warnings.append(REASON_INSUFFICIENT_ACCOUNTED_TRADE_SAMPLE)
        blocked_reasons.append(REASON_INSUFFICIENT_ACCOUNTED_TRADE_SAMPLE)
    if candidate.buy_only_limitation and WARNING_BUY_ONLY_ACCOUNTING_LIMITATION not in warnings:
        warnings.append(WARNING_BUY_ONLY_ACCOUNTING_LIMITATION)

    blocking_evidence_missing = bool(missing_essentials)
    score = sum(component.weighted_score for component in components)
    if blocking_evidence_missing:
        verdict = VERDICT_INCOMPLETE
    elif score >= active_config.copy_candidate_min_score:
        verdict = VERDICT_COPY_CANDIDATE
    elif score >= active_config.watchlist_min_score:
        verdict = VERDICT_WATCHLIST
    else:
        verdict = VERDICT_SKIP

    if sample_component and sample_component.blocking and sample_component.missing:
        blocked_reasons.append(sample_component.note)

    eligible_for_ranking = (
        candidate.ready_for_skill_score
        and candidate.identity_group_by == SUPPORTED_IDENTITY_GROUP_BY
        and verdict != VERDICT_INCOMPLETE
    )
    eligible_for_auto_copy = (
        verdict == VERDICT_COPY_CANDIDATE
        and candidate.ready_for_auto_copy
        and not candidate.buy_only_limitation
        and not blocking_evidence_missing
        and REASON_INSUFFICIENT_ACCOUNTED_TRADE_SAMPLE not in blocked_reasons
    )

    return _result(
        candidate,
        score=score,
        verdict=verdict,
        components=components,
        missing_essentials=_dedupe((*missing_essentials,)),
        blocked_reasons=_dedupe((*blocked_reasons,)),
        warnings=_dedupe((*warnings,)),
        eligible_for_ranking=eligible_for_ranking,
        eligible_for_auto_copy=eligible_for_auto_copy,
    )


def _components(
    candidate: WalletScoringInputCandidate,
    config: WalletSkillScoreConfigV1,
) -> tuple[WalletSkillScoreComponentV1, ...]:
    return (
        _missing_component(
            COMPONENT_INFORMATION_AND_PRICE_IMPROVEMENT_QUALITY,
            "price_improvement_evidence_missing",
        ),
        _verified_realized_performance_component(candidate),
        _missing_component(
            COMPONENT_CHRONOLOGICAL_CONSISTENCY,
            "chronological_window_evidence_missing",
        ),
        _missing_component(
            COMPONENT_RISK_AND_DRAWDOWN_QUALITY,
            "risk_drawdown_evidence_missing",
        ),
        _sample_reliability_component(candidate, config),
        _missing_component(
            COMPONENT_CATEGORY_SPECIALIZATION,
            "category_specialization_evidence_missing",
        ),
        _missing_component(
            COMPONENT_CONCENTRATION_QUALITY,
            "concentration_evidence_missing",
        ),
    )


def _missing_component(name: str, note: str) -> WalletSkillScoreComponentV1:
    return _component(
        name=name,
        raw_value=None,
        normalized_score=None,
        quality="missing",
        missing=True,
        blocking=True,
        note=note,
    )


def _verified_realized_performance_component(
    candidate: WalletScoringInputCandidate,
) -> WalletSkillScoreComponentV1:
    roi_score = (
        midpoint_linear_score(candidate.roi, -0.50, 0.0, 1.00)
        if candidate.roi is not None
        else None
    )
    win_rate_score = (
        midpoint_linear_score(candidate.win_rate, 0.35, 0.50, 0.65)
        if candidate.win_rate is not None
        else None
    )
    profit_factor_score = (
        midpoint_linear_score(candidate.profit_factor, 0.75, 1.00, 2.00)
        if candidate.profit_factor is not None
        else None
    )
    normalized_score = average_available((roi_score, win_rate_score, profit_factor_score))
    if normalized_score is None:
        return _component(
            name=COMPONENT_VERIFIED_REALIZED_PERFORMANCE,
            raw_value={
                "total_realized_pnl": candidate.total_realized_pnl,
                "roi": candidate.roi,
                "win_rate": candidate.win_rate,
                "profit_factor": candidate.profit_factor,
            },
            normalized_score=None,
            quality="missing",
            missing=True,
            blocking=True,
            note="realized_performance_evidence_missing",
        )
    return _component(
        name=COMPONENT_VERIFIED_REALIZED_PERFORMANCE,
        raw_value={
            "total_realized_pnl": candidate.total_realized_pnl,
            "roi": candidate.roi,
            "win_rate": candidate.win_rate,
            "profit_factor": candidate.profit_factor,
            "roi_score": roi_score,
            "win_rate_score": win_rate_score,
            "profit_factor_score": profit_factor_score,
        },
        normalized_score=normalized_score,
        quality=_quality_label(normalized_score),
        missing=False,
        blocking=False,
        note="realized_performance_from_roi_win_rate_profit_factor",
    )


def _sample_reliability_component(
    candidate: WalletScoringInputCandidate,
    config: WalletSkillScoreConfigV1,
) -> WalletSkillScoreComponentV1:
    count_score = linear_score(candidate.accounted_trades, 0.0, config.min_accounted_trades)
    coverage_score = (
        linear_score(candidate.accounting_coverage_pct, 0.0, config.min_accounting_coverage_pct)
        if candidate.accounting_coverage_pct is not None
        else None
    )
    buy_coverage_score = (
        linear_score(
            candidate.accountable_buy_coverage_pct,
            0.0,
            config.min_accounting_coverage_pct,
        )
        if candidate.accountable_buy_coverage_pct is not None
        else None
    )
    normalized_score = average_available((count_score, coverage_score, buy_coverage_score))
    if normalized_score is None:
        return _component(
            name=COMPONENT_SAMPLE_RELIABILITY,
            raw_value={
                "source_trades": candidate.source_trades,
                "total_ledger_rows": candidate.total_ledger_rows,
                "accounted_trades": candidate.accounted_trades,
                "accounting_coverage_pct": candidate.accounting_coverage_pct,
                "accountable_buy_coverage_pct": candidate.accountable_buy_coverage_pct,
            },
            normalized_score=None,
            quality="missing",
            missing=True,
            blocking=True,
            note="sample_reliability_evidence_missing",
        )
    return _component(
        name=COMPONENT_SAMPLE_RELIABILITY,
        raw_value={
            "source_trades": candidate.source_trades,
            "total_ledger_rows": candidate.total_ledger_rows,
            "accounted_trades": candidate.accounted_trades,
            "accounting_coverage_pct": candidate.accounting_coverage_pct,
            "accountable_buy_coverage_pct": candidate.accountable_buy_coverage_pct,
            "count_score": count_score,
            "coverage_score": coverage_score,
            "buy_coverage_score": buy_coverage_score,
        },
        normalized_score=normalized_score,
        quality=_quality_label(normalized_score),
        missing=False,
        blocking=False,
        note="sample_reliability_from_accounted_trades_and_coverage",
    )


def _component(
    *,
    name: str,
    raw_value: object | None,
    normalized_score: float | None,
    quality: str,
    missing: bool,
    blocking: bool,
    note: str,
) -> WalletSkillScoreComponentV1:
    weight = WALLET_SKILL_SCORE_WEIGHTS_V1[name]
    weighted_score = 0.0 if normalized_score is None else normalized_score * weight / 100.0
    return WalletSkillScoreComponentV1(
        name=name,
        weight=weight,
        raw_value=raw_value,
        normalized_score=normalized_score,
        weighted_score=weighted_score,
        quality=quality,
        missing=missing,
        blocking=blocking,
        note=note,
    )


def _result(
    candidate: WalletScoringInputCandidate,
    *,
    score: float,
    verdict: str,
    components: tuple[WalletSkillScoreComponentV1, ...],
    missing_essentials: tuple[str, ...],
    blocked_reasons: tuple[str, ...],
    warnings: tuple[str, ...],
    eligible_for_ranking: bool,
    eligible_for_auto_copy: bool,
) -> WalletSkillScoreResultV1:
    return WalletSkillScoreResultV1(
        identity_key=candidate.identity_key,
        identity_group_by=candidate.identity_group_by,
        formula_name=WALLET_SKILL_FORMULA_NAME,
        formula_version=WALLET_SKILL_FORMULA_VERSION,
        score=score,
        verdict=verdict,
        ready_for_skill_score=candidate.ready_for_skill_score,
        ready_for_auto_copy=candidate.ready_for_auto_copy,
        eligible_for_ranking=eligible_for_ranking,
        eligible_for_auto_copy=eligible_for_auto_copy,
        components=components,
        missing_essentials=missing_essentials,
        blocked_reasons=blocked_reasons,
        warnings=warnings,
        raw_inputs=_raw_inputs(candidate),
        weights=dict(WALLET_SKILL_SCORE_WEIGHTS_V1),
    )


def _raw_inputs(candidate: WalletScoringInputCandidate) -> dict[str, object]:
    return {
        "identity_key": candidate.identity_key,
        "identity_group_by": candidate.identity_group_by,
        "source_trades": candidate.source_trades,
        "total_ledger_rows": candidate.total_ledger_rows,
        "accounted_trades": candidate.accounted_trades,
        "accounting_coverage_pct": candidate.accounting_coverage_pct,
        "accountable_buy_coverage_pct": candidate.accountable_buy_coverage_pct,
        "buy_only_limitation": candidate.buy_only_limitation,
        "total_realized_pnl": candidate.total_realized_pnl,
        "roi": candidate.roi,
        "win_rate": candidate.win_rate,
        "profit_factor": candidate.profit_factor,
        "ready_for_skill_score": candidate.ready_for_skill_score,
        "ready_for_auto_copy": candidate.ready_for_auto_copy,
        "candidate_status": candidate.candidate_status,
        "warnings": candidate.warnings,
        "blocked_reasons": candidate.blocked_reasons,
    }


def _component_by_name(
    components: tuple[WalletSkillScoreComponentV1, ...],
    name: str,
) -> WalletSkillScoreComponentV1 | None:
    for component in components:
        if component.name == name:
            return component
    return None


def _quality_label(score: float) -> str:
    if score >= 80.0:
        return "strong"
    if score >= 55.0:
        return "adequate"
    if score > 0.0:
        return "weak"
    return "poor"


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


__all__ = [
    "WALLET_SKILL_FORMULA_NAME",
    "WALLET_SKILL_FORMULA_VERSION",
    "WALLET_SKILL_SCORE_WEIGHTS_V1",
    "VERDICT_INCOMPLETE",
    "VERDICT_SKIP",
    "VERDICT_WATCHLIST",
    "VERDICT_COPY_CANDIDATE",
    "WalletSkillScoreComponentV1",
    "WalletSkillScoreConfigV1",
    "WalletSkillScoreResultV1",
    "average_available",
    "clamp_0_100",
    "compute_wallet_skill_score_v1",
    "linear_score",
    "midpoint_linear_score",
]
