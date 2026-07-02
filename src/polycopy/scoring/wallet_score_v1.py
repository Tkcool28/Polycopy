"""Persistent Specialist Wallet Score v1 — frozen formula.

Score composition (weights sum to 100):
- information_and_price_improvement: 30%
- verified_realized_performance: 15%
- chronological_consistency: 15%
- risk_and_drawdown_quality: 10%
- sample_reliability: 10%
- category_specialization: 15%
- concentration_quality: 5%

Verdict rules:
- 75.0000–100.0000 → COPY CANDIDATE
- 55.0000–74.9999 → WATCHLIST
- below 55 → SKIP
- Missing essential evidence → INCOMPLETE

Global minimums for normal verdict:
- 30 resolved markets
- 20 active trading days
- 15 distinct events

Category minimums for COPY CANDIDATE:
- 15 resolved category markets
- 8 distinct category events
- 10 category-active days
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from polycopy.scoring.helpers import clamp, linear_score, inverse_score


class WalletVerdict(str, enum.Enum):
    """Verdict for wallet score evaluation."""

    COPY_CANDIDATE = "copy_candidate"
    WATCHLIST = "watchlist"
    SKIP = "skip"
    INCOMPLETE = "incomplete"


@dataclass
class WalletScoreComponent:
    """Component score with raw value, weight, and quality tag."""

    name: str
    raw_score: float  # 0-100 before weighting
    weight: float  # 0-100
    quality: str  # "observed", "calculated", "inferred", "unknown"
    formula: str
    note: str = ""

    @property
    def weighted_score(self) -> float:
        """Contribution to final score after weighting."""
        return self.raw_score * (self.weight / 100.0)


@dataclass
class WalletScoreResult:
    """Result of wallet v1 scoring with full component breakdown."""

    wallet_id: str
    score: float  # Final 0-100 score
    verdict: WalletVerdict
    components: list[WalletScoreComponent] = field(default_factory=list)
    missing_essentials: list[str] = field(default_factory=list)
    eligibility_gate_failures: list[str] = field(default_factory=list)
    computed_at: datetime = field(default_factory=datetime.utcnow)
    formula_version: str = "1"
    is_sample: bool = False


# Frozen weights (must sum to 100)
WEIGHTS = {
    "information_and_price_improvement": 30.0,
    "verified_realized_performance": 15.0,
    "chronological_consistency": 15.0,
    "risk_and_drawdown_quality": 10.0,
    "sample_reliability": 10.0,
    "category_specialization": 15.0,
    "concentration_quality": 5.0,
}

# Verdict thresholds
VERDICT_COPY_CANDIDATE_MIN = 75.0
VERDICT_WATCHLIST_MIN = 55.0

# Global eligibility minimums
GLOBAL_MIN_RESOLVED_MARKETS = 30
GLOBAL_MIN_ACTIVE_TRADING_DAYS = 20
GLOBAL_MIN_DISTINCT_EVENTS = 15

# Category eligibility minimums for COPY CANDIDATE
CATEGORY_MIN_RESOLVED_MARKETS = 15
CATEGORY_MIN_DISTINCT_EVENTS = 8
CATEGORY_MIN_ACTIVE_DAYS = 10


def _info_price_improvement_component(
    info_score: Optional[float],
) -> tuple[float, str, str]:
    """Score: 0-100 info score → 0-100.

    Information and price-improvement quality.
    Null or missing → 0 with UNKNOWN quality.
    """
    if info_score is None:
        return 0.0, "unknown", "info_score=None (missing)"
    return clamp(info_score * 100.0), "calculated", f"info_score={info_score:.3f}"


def _realized_performance_component(
    win_rate: Optional[float],
    profit_factor: Optional[float],
) -> tuple[float, str, str]:
    """Score: win_rate and profit_factor combined.

    Verified realized performance.
    Both missing → 0 with UNKNOWN quality.
    Neither missing → average of normalized values.
    """
    if win_rate is None and profit_factor is None:
        return 0.0, "unknown", "win_rate and profit_factor both missing"

    wr_score = 0.0
    pf_score = 0.0

    if win_rate is not None:
        # Win rate: 0% → 0, 100% → 100
        wr_score = clamp(win_rate * 100.0)
    if profit_factor is not None:
        # Profit factor: 1.0 → 50, 2.0+ → 100, linear above 2.0 to 3.0 = 100
        pf_score = clamp(linear_score(profit_factor, 1.0, 2.0))

    # Average of available scores
    if win_rate is None:
        return pf_score, "calculated", f"profit_factor={profit_factor:.3f}"
    if profit_factor is None:
        return wr_score, "calculated", f"win_rate={win_rate:.3f}"
    return (wr_score + pf_score) / 2.0, "calculated", f"win_rate={win_rate:.3f} profit_factor={profit_factor:.3f}"


def _chronological_consistency_component(
    trade_intervals_std: Optional[float],
    trade_count: Optional[int],
) -> tuple[float, str, str]:
    """Score: lower std = more consistent → higher score.

    Chronological consistency: penalize erratic timing.
    std_dev = 0 → 100, std_dev = 12h → 0.
    """
    if trade_intervals_std is None or trade_count is None:
        return 0.0, "unknown", "missing trade_intervals_std or trade_count"

    if trade_count < 5:
        return 0.0, "observed", "insufficient_trades_for_chrono_consistency"

    # Convert std dev to hours and score
    std_hours = trade_intervals_std / 3600.0
    score = clamp(inverse_score(std_hours, 0.0, 12.0))
    return score, "observed", f"trade_intervals_std={std_hours:.2f}h"


def _risk_drawdown_component(
    max_drawdown: Optional[float],
    sharpe_ratio: Optional[float],
) -> tuple[float, str, str]:
    """Score: lower drawdown + higher Sharpe = better.

    Risk and drawdown quality.
    Drawdown: 0% → 100, 50% → 0.
    Sharpe: clamped to MAX_SHARPE = 3.0.
    Combined average.
    """
    MAX_SHARPE = 3.0

    if max_drawdown is None and sharpe_ratio is None:
        return 0.0, "unknown", "missing both max_drawdown and sharpe_ratio"

    dd_score = 0.0
    sr_score = 0.0

    if max_drawdown is not None:
        # Drawdown penalty: 0% → 100, 50% → 0
        dd_score = clamp(inverse_score(max_drawdown, 0.0, 0.5))

    if sharpe_ratio is not None:
        sr_score = clamp(sharpe_ratio / MAX_SHARPE * 100.0)

    if max_drawdown is None:
        return sr_score, "calculated", f"sharpe_ratio={sharpe_ratio:.3f}"
    if sharpe_ratio is None:
        return dd_score, "calculated", f"max_drawdown={max_drawdown:.3f}"

    return (dd_score + sr_score) / 2.0, "calculated", f"drawdown={max_drawdown:.3f} sharpe={sharpe_ratio:.3f}"


def _sample_reliability_component(
    trade_count: Optional[int],
    sample_fraction: Optional[float],
) -> tuple[float, str, str]:
    """Score: more trades + more real vs sample = better.

    Sample reliability: real trades count more than sample trades.
    """
    if trade_count is None:
        return 0.0, "unknown", "missing trade_count"

    # Base score on trade count: 0 at 5 trades, 100 at 200+
    count_score = clamp(linear_score(trade_count, 5.0, 200.0))

    if sample_fraction is None:
        return count_score, "observed", f"trade_count={trade_count} sample_fraction unknown"

    # Adjust for sample fraction
    # 0% sample = full score, 100% sample = 50% score
    sample_penalty = clamp(linear_score(sample_fraction, 0.0, 1.0) / 2.0)
    final_score = max(0.0, count_score - sample_penalty)

    return final_score, "observed", f"trade_count={trade_count} sample_fraction={sample_fraction:.2f}"


def _category_specialization_component(
    category_trade_count: Optional[int],
    category_distinct_markets: Optional[int],
    overall_trade_count: Optional[int],
) -> tuple[float, str, str]:
    """Score: concentration in category signals specialization.

    Category specialization: trade share in category.
    """
    if category_trade_count is None or overall_trade_count is None:
        return 0.0, "unknown", "missing category trade counts"

    if overall_trade_count == 0:
        return 0.0, "observed", "zero overall trades"

    # Share of trades in category: 10% → 0, 40%+ → 100
    share = category_trade_count / overall_trade_count
    score = clamp(linear_score(share, 0.10, 0.40))

    # Boost for distinct markets in category
    if category_distinct_markets is not None:
        market_score = clamp(linear_score(category_distinct_markets, 1.0, 5.0))
        score = (score + market_score) / 2.0

    return score, "observed", f"category_share={share:.2f}"


def _concentration_quality_component(
    largest_winner_share: Optional[float],
    top_3_concentration: Optional[float],
) -> tuple[float, str, str]:
    """Score: lower concentration = higher score (diversification).

    Largest winner removal: if one trade dominates, penalize.
    """
    if largest_winner_share is None and top_3_concentration is None:
        return 0.0, "unknown", "missing concentration metrics"

    score = 100.0

    if largest_winner_share is not None:
        # If one trade is >50% of profit, penalize severely
        penalty = clamp((largest_winner_share - 0.5) * 200.0) if largest_winner_share > 0.5 else 0.0
        score -= penalty
        note = f"largest_winner_share={largest_winner_share:.2f}"
    else:
        note = "largest_winner_share unknown"

    if top_3_concentration is not None:
        # Top 3 should not dominate (>70% penalty)
        penalty = clamp((top_3_concentration - 0.7) * 200.0) if top_3_concentration > 0.7 else 0.0
        score -= penalty
        note += f" top3_conc={top_3_concentration:.2f}"

    return clamp(score), "observed", note


def compute_wallet_score_v1(
    wallet_id: str,
    *,
    # Information and price improvement
    info_score: Optional[float] = None,

    # Verified realized performance
    win_rate: Optional[float] = None,
    profit_factor: Optional[float] = None,

    # Chronological consistency
    trade_intervals_std: Optional[float] = None,
    trade_count: Optional[int] = None,

    # Risk and drawdown
    max_drawdown: Optional[float] = None,
    sharpe_ratio: Optional[float] = None,

    # Sample reliability
    sample_fraction: Optional[float] = None,

    # Category metrics
    category_trade_count: Optional[int] = None,
    category_distinct_markets: Optional[int] = None,
    overall_trade_count: Optional[int] = None,

    # Concentration
    largest_winner_share: Optional[float] = None,
    top_3_concentration: Optional[float] = None,

    # Eligibility gate values
    resolved_markets: Optional[int] = None,
    active_trading_days: Optional[int] = None,
    distinct_events: Optional[int] = None,

    # Category eligibility
    category_resolved_markets: Optional[int] = None,
    category_distinct_events: Optional[int] = None,
    category_active_days: Optional[int] = None,

    # Metadata
    now: Optional[datetime] = None,
    is_sample: bool = False,
) -> WalletScoreResult:
    """Compute Persistent Specialist Wallet Score v1.

    All inputs optional. Missing essential evidence produces INCOMPLETE.
    """
    if now is None:
        now = datetime.utcnow()

    components: list[WalletScoreComponent] = []
    missing_essentials: list[str] = []
    gate_failures: list[str] = []

    # Check essential evidence
    essential_fields = ["trade_count", "win_rate"]
    if trade_count is None:
        missing_essentials.append("trade_count")
    if win_rate is None:
        missing_essentials.append("win_rate")

    # Check global eligibility gates (do not disqualify, but affect score)
    if resolved_markets is not None and resolved_markets < GLOBAL_MIN_RESOLVED_MARKETS:
        gate_failures.append(f"resolved_markets={resolved_markets} < {GLOBAL_MIN_RESOLVED_MARKETS}")

    if active_trading_days is not None and active_trading_days < GLOBAL_MIN_ACTIVE_TRADING_DAYS:
        gate_failures.append(f"active_trading_days={active_trading_days} < {GLOBAL_MIN_ACTIVE_TRADING_DAYS}")

    if distinct_events is not None and distinct_events < GLOBAL_MIN_DISTINCT_EVENTS:
        gate_failures.append(f"distinct_events={distinct_events} < {GLOBAL_MIN_DISTINCT_EVENTS}")

    # Check category eligibility gates
    if category_resolved_markets is not None and category_resolved_markets < CATEGORY_MIN_RESOLVED_MARKETS:
        gate_failures.append(f"category_resolved_markets={category_resolved_markets} < {CATEGORY_MIN_RESOLVED_MARKETS}")

    if category_distinct_events is not None and category_distinct_events < CATEGORY_MIN_DISTINCT_EVENTS:
        gate_failures.append(f"category_distinct_events={category_distinct_events} < {CATEGORY_MIN_DISTINCT_EVENTS}")

    if category_active_days is not None and category_active_days < CATEGORY_MIN_ACTIVE_DAYS:
        gate_failures.append(f"category_active_days={category_active_days} < {CATEGORY_MIN_ACTIVE_DAYS}")

    # If essential evidence is missing, return INCOMPLETE
    if missing_essentials:
        # Still compute partial score for audit
        return WalletScoreResult(
            wallet_id=wallet_id,
            score=0.0,
            verdict=WalletVerdict.INCOMPLETE,
            components=components,
            missing_essentials=missing_essentials,
            eligibility_gate_failures=gate_failures,
            computed_at=now,
            is_sample=is_sample,
        )

    # Compute components
    raw, quality, note = _info_price_improvement_component(info_score)
    components.append(WalletScoreComponent(
        name="information_and_price_improvement",
        raw_score=raw,
        weight=WEIGHTS["information_and_price_improvement"],
        quality=quality,
        formula="info_score * 100 (clamped)",
        note=note,
    ))

    raw, quality, note = _realized_performance_component(win_rate, profit_factor)
    components.append(WalletScoreComponent(
        name="verified_realized_performance",
        raw_score=raw,
        weight=WEIGHTS["verified_realized_performance"],
        quality=quality,
        formula="avg(win_rate*100, profit_factor normalized to 1-2)",
        note=note,
    ))

    raw, quality, note = _chronological_consistency_component(trade_intervals_std, trade_count)
    components.append(WalletScoreComponent(
        name="chronological_consistency",
        raw_score=raw,
        weight=WEIGHTS["chronological_consistency"],
        quality=quality,
        formula="inverse_score(trade_intervals_std_hours, 0, 12)",
        note=note,
    ))

    raw, quality, note = _risk_drawdown_component(max_drawdown, sharpe_ratio)
    components.append(WalletScoreComponent(
        name="risk_and_drawdown_quality",
        raw_score=raw,
        weight=WEIGHTS["risk_and_drawdown_quality"],
        quality=quality,
        formula="avg(inverse(drawdown, 0, 0.5), sharpe/3*100)",
        note=note,
    ))

    raw, quality, note = _sample_reliability_component(trade_count, sample_fraction)
    components.append(WalletScoreComponent(
        name="sample_reliability",
        raw_score=raw,
        weight=WEIGHTS["sample_reliability"],
        quality=quality,
        formula="trade_count linear ramps 5→200, penalized by sample_fraction",
        note=note,
    ))

    raw, quality, note = _category_specialization_component(
        category_trade_count, category_distinct_markets, overall_trade_count
    )
    components.append(WalletScoreComponent(
        name="category_specialization",
        raw_score=raw,
        weight=WEIGHTS["category_specialization"],
        quality=quality,
        formula="linear_score(category_share, 0.1, 0.4) with market bonus",
        note=note,
    ))

    raw, quality, note = _concentration_quality_component(largest_winner_share, top_3_concentration)
    components.append(WalletScoreComponent(
        name="concentration_quality",
        raw_score=raw,
        weight=WEIGHTS["concentration_quality"],
        quality=quality,
        formula="100 - largest_winner_penalty - top3_penalty",
        note=note,
    ))

    # Compute final score (sum of weighted scores)
    weighted_total = sum(c.weighted_score for c in components)
    final_score = clamp(round(weighted_total, 4))

    # Determine verdict
    # If category gates failed, cap at WATCHLIST
    category_gates_failed = any(
        "category_" in f for f in gate_failures
    ) if gate_failures else False

    if final_score >= VERDICT_COPY_CANDIDATE_MIN and not category_gates_failed:
        verdict = WalletVerdict.COPY_CANDIDATE
    elif final_score >= VERDICT_WATCHLIST_MIN:
        verdict = WalletVerdict.WATCHLIST
    else:
        verdict = WalletVerdict.SKIP

    return WalletScoreResult(
        wallet_id=wallet_id,
        score=final_score,
        verdict=verdict,
        components=components,
        missing_essentials=missing_essentials,
        eligibility_gate_failures=gate_failures,
        computed_at=now,
        is_sample=is_sample,
    )