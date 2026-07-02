"""Copy-Adjusted Alpha Score v2 Shadow — frozen formula.

Score composition (weights sum to 100):
- delayed_entry_alpha: 30%
- tradeable_price_retention: 20%
- execution_feasibility: 15%
- skill_persistence: 15%
- copied_trade_realized_performance: 10%
- concentration_and_correlation: 10%

Verdict rules:
- 70.0000–100.0000 → SHADOW_COPY_CANDIDATE
- 50.0000–69.9999 → SHADOW_WATCHLIST
- below 50 → SHADOW_SKIP
- Missing forward outcome data → SHADOW_INCOMPLETE

Delay scenarios:
- Theoretical immediate
- 30 seconds
- 2 minutes
- 5 minutes
- 15 minutes
- Actual measured delay

V2 runs in parallel only — does not affect v1 verdict, signal creation,
paper approval, or production execution.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from polycopy.scoring.helpers import clamp, linear_score


class ShadowVerdict(str, enum.Enum):
    """Verdict for v2 shadow scoring."""

    SHADOW_COPY_CANDIDATE = "shadow_copy_candidate"
    SHADOW_WATCHLIST = "shadow_watchlist"
    SHADOW_SKIP = "shadow_skip"
    SHADOW_INCOMPLETE = "shadow_incomplete"


@dataclass
class ShadowScoreComponent:
    """Component score for v2 shadow."""

    name: str
    raw_score: float
    weight: float
    quality: str
    formula: str
    note: str = ""

    @property
    def weighted_score(self) -> float:
        return self.raw_score * (self.weight / 100.0)


@dataclass
class ShadowScoreResult:
    """Result of v2 shadow scoring."""

    wallet_id: str
    source_trade_id: str
    score: float
    verdict: ShadowVerdict
    components: list[ShadowScoreComponent] = field(default_factory=list)
    missing_components: list[str] = field(default_factory=list)
    delay_scenario: str = "actual_measured"  # theoretical_immediate, 30s, 2m, 5m, 15m, actual_measured
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    formula_version: str = "2-shadow"
    is_sample: bool = False


# Frozen weights (must sum to 100)
WEIGHTS = {
    "delayed_entry_alpha": 30.0,
    "tradeable_price_retention": 20.0,
    "execution_feasibility": 15.0,
    "skill_persistence": 15.0,
    "copied_trade_realized_performance": 10.0,
    "concentration_and_correlation": 10.0,
}

# Verdict thresholds
VERDICT_COPY_CANDIDATE_MIN = 70.0
VERDICT_WATCHLIST_MIN = 50.0


# Delay scenario bounds
DELAY_IMMEDIATE_SECONDS = 0
DELAY_30S_SECONDS = 30
DELAY_2M_SECONDS = 120
DELAY_5M_SECONDS = 300
DELAY_15M_SECONDS = 900


def _delayed_entry_alpha_component(
    delay_seconds: Optional[float],
    alpha_signal: Optional[float],
) -> tuple[float, str, str]:
    """Score: lower delay + stronger alpha = better.

    Delay: 0s → 100, max delay (configurable) → 0.
    Alpha: -0.2 → 0, +0.2 → 100 (normalized around 0).
    """
    if delay_seconds is None or alpha_signal is None:
        return 0.0, "unknown", "delay_seconds or alpha_signal missing"

    # Delay penalty (inverted: faster is better)
    # Max 15 minutes (900s) of delay modeled
    delay_score = clamp(linear_score(max(0, 900 - delay_seconds), 0, 900))

    # Alpha score (normalized around 0)
    # -0.2 → 0, 0 → 50, +0.2 → 100
    alpha_score = clamp(linear_score(alpha_signal, -0.2, 0.2))

    return (delay_score * 0.3 + alpha_score * 0.7), "calculated", f"delay={delay_seconds:.0f}s alpha={alpha_signal:.3f}"


def _price_retention_component(
    price_retention_ratio: Optional[float],
) -> tuple[float, str, str]:
    """Score: how much of the favorable price move remains.

    0% retention → 0, 100% → 100.
    """
    if price_retention_ratio is None:
        return 0.0, "unknown", "price_retention_ratio missing"

    return clamp(price_retention_ratio * 100.0), "calculated", f"retention={price_retention_ratio:.3f}"


def _execution_feasibility_component(
    slippage_pct: Optional[float],
    fill_percentage: Optional[float],
) -> tuple[float, str, str]:
    """Score: realistic execution.

    Lower slippage + higher fill = better.
    """
    if slippage_pct is None and fill_percentage is None:
        return 0.0, "unknown", "slippage_pct and fill_percentage missing"

    slip_score = 100.0
    if slippage_pct is not None:
        # 0% slippage → 100, 10%+ → 0
        slip_score = clamp(inverse_score(slippage_pct, 0.0, 0.10))

    fill_score = 100.0
    if fill_percentage is not None:
        fill_score = clamp(fill_percentage * 100.0)

    if slippage_pct is None:
        return fill_score, "calculated", f"fill_pct={fill_percentage:.2%}"
    if fill_percentage is None:
        return slip_score, "calculated", f"slippage_pct={slippage_pct:.2%}"

    return (slip_score + fill_score) / 2.0, "calculated", f"slippage={slippage_pct:.2%} fill={fill_percentage:.2%}"


def _skill_persistence_component(
    wallet_score: Optional[float],
    days_since_last_trade: Optional[int],
) -> tuple[float, str, str]:
    """Score: wallet skill stability over time.

    Wallet score 75+ → 100, score < 50 → 0.
    Days since last trade: 0 → 100, 30+ → 0.
    """
    if wallet_score is None and days_since_last_trade is None:
        return 0.0, "unknown", "wallet_score and days_since_last_trade missing"

    score = 100.0

    if wallet_score is not None:
        score *= min(1.0, wallet_score / 75.0)

    if days_since_last_trade is not None:
        # Linear decay from 0 days (100) to 30 days (0)
        time_decay = clamp(linear_score(30 - days_since_last_trade, 0, 30))
        score = (score + time_decay) / 2.0

    return clamp(score), "calculated", f"wallet_score={wallet_score} days_since={days_since_last_trade}"


def _copied_trade_performance_component(
    copied_trade_pnl: Optional[float],
    copied_trade_count: Optional[int],
) -> tuple[float, str, str]:
    """Score: realized performance of copied trades.

    Positive PnL → higher score.
    """
    if copied_trade_pnl is None or copied_trade_count is None:
        return 0.0, "unknown", "copied_trade_pnl or copied_trade_count missing"

    if copied_trade_count == 0:
        return 50.0, "calculated", "no_copied_trades_yet"

    # Normalize PnL: -100 → 0, 0 → 50, +100 → 100
    pnl_score = clamp(linear_score(copied_trade_pnl, -100.0, 100.0))

    return pnl_score, "calculated", f"pnl={copied_trade_pnl} count={copied_trade_count}"


def _concentration_correlation_component(
    position_concentration: Optional[float],
    correlation_score: Optional[float],
) -> tuple[float, str, str]:
    """Score: diversification and correlation management.

    Lower concentration + lower correlation = better.
    """
    if position_concentration is None and correlation_score is None:
        return 0.0, "unknown", "concentration and correlation missing"

    score = 100.0

    if position_concentration is not None:
        # 100% concentration in one position → 0, 0% → 100
        score *= (1 - position_concentration)

    if correlation_score is not None:
        # High correlation = bad
        score *= (1 - correlation_score)

    return clamp(score), "calculated", f"conc={position_concentration} corr={correlation_score}"


def compute_shadow_score_v2(
    wallet_id: str,
    source_trade_id: str,
    *,
    # Delayed entry
    delay_seconds: Optional[float] = None,
    alpha_signal: Optional[float] = None,

    # Price retention
    price_retention_ratio: Optional[float] = None,

    # Execution
    slippage_pct: Optional[float] = None,
    fill_percentage: Optional[float] = None,

    # Skill persistence
    wallet_score: Optional[float] = None,
    days_since_last_trade: Optional[int] = None,

    # Copied trade performance
    copied_trade_pnl: Optional[float] = None,
    copied_trade_count: Optional[int] = None,

    # Concentration
    position_concentration: Optional[float] = None,
    correlation_score: Optional[float] = None,

    # Metadata
    now: Optional[datetime] = None,
    is_sample: bool = False,
) -> ShadowScoreResult:
    """Compute Copy-Adjusted Alpha Score v2 Shadow."""
    if now is None:
        now = datetime.now(timezone.utc)

    components: list[ShadowScoreComponent] = []
    missing_components: list[str] = []

    # Check for missing essential components
    if alpha_signal is None:
        missing_components.append("alpha_signal")

    if missing_components:
        return ShadowScoreResult(
            wallet_id=wallet_id,
            source_trade_id=source_trade_id,
            score=0.0,
            verdict=ShadowVerdict.SHADOW_INCOMPLETE,
            components=components,
            missing_components=missing_components,
            computed_at=now,
            is_sample=is_sample,
        )

    # Compute components
    raw, quality, note = _delayed_entry_alpha_component(delay_seconds, alpha_signal)
    components.append(ShadowScoreComponent(
        name="delayed_entry_alpha",
        raw_score=raw,
        weight=WEIGHTS["delayed_entry_alpha"],
        quality=quality,
        formula="delay_score*0.3 + alpha_score*0.7",
        note=note,
    ))

    raw, quality, note = _price_retention_component(price_retention_ratio)
    components.append(ShadowScoreComponent(
        name="tradeable_price_retention",
        raw_score=raw,
        weight=WEIGHTS["tradeable_price_retention"],
        quality=quality,
        formula="retention_ratio * 100",
        note=note,
    ))

    raw, quality, note = _execution_feasibility_component(slippage_pct, fill_percentage)
    components.append(ShadowScoreComponent(
        name="execution_feasibility",
        raw_score=raw,
        weight=WEIGHTS["execution_feasibility"],
        quality=quality,
        formula="avg(inverse(slippage, 0, 0.1), fill_pct*100)",
        note=note,
    ))

    raw, quality, note = _skill_persistence_component(wallet_score, days_since_last_trade)
    components.append(ShadowScoreComponent(
        name="skill_persistence",
        raw_score=raw,
        weight=WEIGHTS["skill_persistence"],
        quality=quality,
        formula="wallet_score/75 * time_decay_to_30d",
        note=note,
    ))

    raw, quality, note = _copied_trade_performance_component(copied_trade_pnl, copied_trade_count)
    components.append(ShadowScoreComponent(
        name="copied_trade_realized_performance",
        raw_score=raw,
        weight=WEIGHTS["copied_trade_realized_performance"],
        quality=quality,
        formula="linear(pnl, -100, 100)",
        note=note,
    ))

    raw, quality, note = _concentration_correlation_component(
        position_concentration, correlation_score
    )
    components.append(ShadowScoreComponent(
        name="concentration_and_correlation",
        raw_score=raw,
        weight=WEIGHTS["concentration_and_correlation"],
        quality=quality,
        formula="(1-conc) * (1-corr) * 100",
        note=note,
    ))

    # Compute final score
    weighted_total = sum(c.weighted_score for c in components)
    final_score = clamp(round(weighted_total, 4))

    # Determine verdict
    if final_score >= VERDICT_COPY_CANDIDATE_MIN:
        verdict = ShadowVerdict.SHADOW_COPY_CANDIDATE
    elif final_score >= VERDICT_WATCHLIST_MIN:
        verdict = ShadowVerdict.SHADOW_WATCHLIST
    else:
        verdict = ShadowVerdict.SHADOW_SKIP

    return ShadowScoreResult(
        wallet_id=wallet_id,
        source_trade_id=source_trade_id,
        score=final_score,
        verdict=verdict,
        components=components,
        missing_components=missing_components,
        computed_at=now,
        is_sample=is_sample,
    )