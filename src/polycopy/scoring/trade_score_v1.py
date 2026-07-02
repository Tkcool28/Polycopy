"""Trade Copyability Score v1 — frozen formula.

Score composition (weights sum to 100):
- copy_price_quality: 30%
- fill_feasibility: 25%
- liquidity_and_spread_quality: 15%
- trade_freshness: 10%
- holding_period_quality: 10%
- market_and_resolution_quality: 5%
- strategy_and_data_quality: 5%

Verdict rules:
- 70.0000–100.0000 → COPY CANDIDATE
- 50.0000–69.9999 → WATCHLIST
- below 50 → SKIP
- Missing essential evidence → INCOMPLETE

Duration rules:
- Under 15 minutes: excluded
- 15 minutes to under 6 hours: experimental only (score 75 minimum)
- 6 hours to under 1 day: allowed, score 75
- 1–14 days: preferred, score 100
- 15–21 days: allowed, score 80
- 22–45 days: penalized, score 40
- Over 45 days: excluded
- Unknown: INCOMPLETE
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from polycopy.scoring.helpers import clamp, linear_score, inverse_score


class TradeVerdict(str, enum.Enum):
    """Verdict for trade copyability evaluation."""

    COPY_CANDIDATE = "copy_candidate"
    WATCHLIST = "watchlist"
    SKIP = "skip"
    INCOMPLETE = "incomplete"


@dataclass
class TradeScoreComponent:
    """Component score for trade copyability."""

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
class TradeScoreResult:
    """Result of trade v1 copyability scoring."""

    wallet_id: str
    source_trade_id: str
    score: float
    verdict: TradeVerdict
    components: list[TradeScoreComponent] = field(default_factory=list)
    missing_essentials: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    formula_version: str = "1"
    is_sample: bool = False


# Frozen weights (must sum to 100)
WEIGHTS = {
    "copy_price_quality": 30.0,
    "fill_feasibility": 25.0,
    "liquidity_and_spread_quality": 15.0,
    "trade_freshness": 10.0,
    "holding_period_quality": 10.0,
    "market_and_resolution_quality": 5.0,
    "strategy_and_data_quality": 5.0,
}

# Verdict thresholds
VERDICT_COPY_CANDIDATE_MIN = 70.0
VERDICT_WATCHLIST_MIN = 50.0


# Duration buckets and their scores
DURATION_EXCLUDED_SHORT = 15 * 60  # 15 minutes in seconds
DURATION_EXPERIMENTAL_MIN = 15 * 60
DURATION_PREFERRED_MIN = 6 * 3600  # 6 hours
DURATION_MAX = 24 * 3600  # 1 day (preferred threshold)
DURATION_PENALIZED_MIN = 15 * 24 * 3600  # 15 days
DURATION_PENALIZED_MAX = 45 * 24 * 3600  # 45 days


def _copy_price_quality_component(
    price_deterioration_pct: Optional[float],
    side: str,
) -> tuple[float, str, str]:
    """Score: deterioration affects score.

    BUY: deterioration positive when copy price exceeds source price.
    SELL: deterioration positive when copy price is below source price.

    Score: 0% deter = 100, 50%+ deter = 0.
    """
    if price_deterioration_pct is None:
        return 0.0, "unknown", "price_deterioration_pct missing"

    # For positive deterioration (worse), lower score
    # 0% deterioration → 100, 50% deterioration → 0
    score = clamp(inverse_score(price_deterioration_pct, 0.0, 0.5))
    return score, "observed", f"deterioration_pct={price_deterioration_pct:.2%}"


def _fill_feasibility_component(
    intended_stake: Optional[float],
    executable_depth: Optional[float],
    fill_percentage: Optional[float],
) -> tuple[float, str, str]:
    """Score: can we fill the intended stake?

    Score factors:
    - Fill percentage: 100% → 100, 0% → 0
    - Depth ratio: stake fits → 100, insufficient → 0-100
    """
    if intended_stake is None or executable_depth is None:
        return 0.0, "unknown", "intended_stake or executable_depth missing"

    # Fill percentage component
    fill_pct_score = 0.0
    if fill_percentage is not None:
        fill_pct_score = clamp(fill_percentage * 100.0)
    else:
        # Derive from depth ratio
        fill_pct_score = clamp(linear_score(min(intended_stake / executable_depth, 1.0), 0.0, 1.0))

    return fill_pct_score, "observed", f"stake={intended_stake} depth={executable_depth} fill_pct={fill_pct_score:.1f}"


def _liquidity_spread_component(
    spread: Optional[float],
    best_bid_size: Optional[float],
    best_ask_size: Optional[float],
    intended_stake: Optional[float],
) -> tuple[float, str, str]:
    """Score: tighter spread and more depth = better.

    Spread: 0% → 100, 20%+ → 0.
    Liquidity: stake < 10% of depth → 100, stake > 100% → 0.
    """
    if spread is None:
        return 0.0, "unknown", "spread missing"

    # Spread score: 0% → 100, 20% → 0
    spread_score = clamp(inverse_score(spread, 0.0, 0.20))

    # Liquidity score
    liquidity_score = 100.0
    if intended_stake is not None and best_bid_size is not None and best_ask_size is not None:
        total_depth = best_bid_size + best_ask_size
        if total_depth > 0:
            stake_ratio = intended_stake / total_depth
            liquidity_score = clamp(inverse_score(stake_ratio, 0.1, 1.0))

    return (spread_score + liquidity_score) / 2.0, "observed", f"spread={spread:.3f}"


def _trade_freshness_component(
    trade_age_seconds: Optional[float],
) -> tuple[float, str, str]:
    """Score: fresher trades are better.

    0s old → 100, 3600s old → 0.
    """
    if trade_age_seconds is None:
        return 0.0, "unknown", "trade_age_seconds missing"

    score = clamp(inverse_score(trade_age_seconds, 0.0, 3600.0))
    return score, "observed", f"age_seconds={trade_age_seconds:.0f}"


def _holding_period_component(
    seconds_to_market_end: Optional[float],
) -> tuple[float, str, str]:
    """Score based on market duration remaining.

    Duration rules:
    - Under 15 min: excluded (0 score)
    - 15 min - 6h: experimental (score 75)
    - 6h - 1d: allowed (score 100)
    - 1d - 14d: preferred (score 100)
    - 15d - 21d: allowed (score 80)
    - 22d - 45d: penalized (score 40)
    - Over 45d: excluded (0 score)
    """
    if seconds_to_market_end is None or seconds_to_market_end < 0:
        return 0.0, "unknown", "invalid or missing seconds_to_market_end"

    age_days = seconds_to_market_end / (24 * 3600)

    if seconds_to_market_end < DURATION_EXCLUDED_SHORT:
        # Under 15 minutes: excluded
        return 0.0, "observed", f"duration_excluded_short={age_days:.4f}d (< 15min)"

    if seconds_to_market_end < DURATION_PREFERRED_MIN:
        # 15 min to under 6 hours: experimental only
        return 75.0, "observed", f"duration_experimental={age_days:.4f}d (15min-6h)"

    if seconds_to_market_end <= DURATION_MAX:
        return 100.0, "observed", f"duration_short_preferred={age_days:.4f}d (6h-1d)"

    if seconds_to_market_end <= 14 * 24 * 3600:
        # 1d to 14 days: preferred
        return 100.0, "observed", f"duration_preferred={age_days:.4f}d (1d-14d)"

    if seconds_to_market_end <= 21 * 24 * 3600:
        # 15-21 days: allowed with penalty
        return 80.0, "observed", f"duration_long_allowed={age_days:.4f}d (15d-21d)"

    if seconds_to_market_end <= DURATION_PENALIZED_MAX:
        # 22-45 days: penalized
        return 40.0, "observed", f"duration_penalized={age_days:.4f}d (22d-45d)"

    # Over 45 days: excluded
    return 0.0, "observed", f"duration_excluded_long={age_days:.4f}d (>45d)"


def _market_resolution_component(
    market_active: Optional[bool],
    market_closed: Optional[bool],
    market_resolved: Optional[bool],
) -> tuple[float, str, str]:
    """Score for market state.

    Closed or resolved market → 0.
    Inactive → 0.
    Active → 100.
    """
    if market_active is None:
        return 0.0, "unknown", "market_active missing"

    if market_closed or market_resolved:
        return 0.0, "observed", "market_closed_or_resolved"

    if not market_active:
        return 0.0, "observed", "market_inactive"

    return 100.0, "observed", "market_active"


def _strategy_data_component(
    has_valid_strategy: Optional[bool],
    has_complete_data: Optional[bool],
) -> tuple[float, str, str]:
    """Score for strategy and data quality.

    Both true → 100.
    Either missing → 0-100.
    """
    if has_valid_strategy is None and has_complete_data is None:
        return 0.0, "unknown", "strategy and data flags missing"

    score = 100.0
    if has_valid_strategy is None or has_valid_strategy is False:
        score -= 50.0
    if has_complete_data is None or has_complete_data is False:
        score -= 50.0

    return clamp(score), "observed", f"strategy={has_valid_strategy} data={has_complete_data}"


def compute_trade_score_v1(
    wallet_id: str,
    source_trade_id: str,
    *,
    # Copy price quality
    price_deterioration_pct: Optional[float] = None,
    side: str = "BUY",

    # Fill feasibility
    intended_stake: Optional[float] = None,
    executable_depth: Optional[float] = None,
    fill_percentage: Optional[float] = None,

    # Liquidity and spread
    spread: Optional[float] = None,
    best_bid_size: Optional[float] = None,
    best_ask_size: Optional[float] = None,

    # Freshness
    trade_age_seconds: Optional[float] = None,

    # Holding period
    seconds_to_market_end: Optional[float] = None,

    # Market state
    market_active: Optional[bool] = None,
    market_closed: Optional[bool] = None,
    market_resolved: Optional[bool] = None,

    # Strategy and data
    has_valid_strategy: Optional[bool] = None,
    has_complete_data: Optional[bool] = None,

    # Metadata
    now: Optional[datetime] = None,
    is_sample: bool = False,
) -> TradeScoreResult:
    """Compute Trade Copyability Score v1."""
    if now is None:
        now = datetime.now(timezone.utc)

    components: list[TradeScoreComponent] = []
    missing_essentials: list[str] = []
    rejection_reasons: list[str] = []

    # Check essential evidence
    if intended_stake is None:
        missing_essentials.append("intended_stake")
    if executable_depth is None:
        missing_essentials.append("executable_depth")
    if spread is None:
        missing_essentials.append("spread")
    if trade_age_seconds is None:
        missing_essentials.append("trade_age_seconds")

    if missing_essentials:
        return TradeScoreResult(
            wallet_id=wallet_id,
            source_trade_id=source_trade_id,
            score=0.0,
            verdict=TradeVerdict.INCOMPLETE,
            components=components,
            missing_essentials=missing_essentials,
            computed_at=now,
            is_sample=is_sample,
        )

    # Compute components
    raw, quality, note = _copy_price_quality_component(price_deterioration_pct, side)
    components.append(TradeScoreComponent(
        name="copy_price_quality",
        raw_score=raw,
        weight=WEIGHTS["copy_price_quality"],
        quality=quality,
        formula="inverse(deterioration_pct, 0, 0.5) * 100",
        note=note,
    ))

    raw, quality, note = _fill_feasibility_component(
        intended_stake, executable_depth, fill_percentage
    )
    components.append(TradeScoreComponent(
        name="fill_feasibility",
        raw_score=raw,
        weight=WEIGHTS["fill_feasibility"],
        quality=quality,
        formula="fill_percentage * 100 or depth_ratio",
        note=note,
    ))

    raw, quality, note = _liquidity_spread_component(
        spread, best_bid_size, best_ask_size, intended_stake
    )
    components.append(TradeScoreComponent(
        name="liquidity_and_spread_quality",
        raw_score=raw,
        weight=WEIGHTS["liquidity_and_spread_quality"],
        quality=quality,
        formula="avg(inverse(spread, 0, 0.2), inverse(stake_ratio, 0.1, 1.0))",
        note=note,
    ))

    raw, quality, note = _trade_freshness_component(trade_age_seconds)
    components.append(TradeScoreComponent(
        name="trade_freshness",
        raw_score=raw,
        weight=WEIGHTS["trade_freshness"],
        quality=quality,
        formula="inverse(trade_age_seconds, 0, 3600)",
        note=note,
    ))

    raw, quality, note = _holding_period_component(seconds_to_market_end)
    components.append(TradeScoreComponent(
        name="holding_period_quality",
        raw_score=raw,
        weight=WEIGHTS["holding_period_quality"],
        quality=quality,
        formula="duration_buckets",
        note=note,
    ))

    raw, quality, note = _market_resolution_component(
        market_active, market_closed, market_resolved
    )
    components.append(TradeScoreComponent(
        name="market_and_resolution_quality",
        raw_score=raw,
        weight=WEIGHTS["market_and_resolution_quality"],
        quality=quality,
        formula="active=100, closed/resolved=0",
        note=note,
    ))

    raw, quality, note = _strategy_data_component(has_valid_strategy, has_complete_data)
    components.append(TradeScoreComponent(
        name="strategy_and_data_quality",
        raw_score=raw,
        weight=WEIGHTS["strategy_and_data_quality"],
        quality=quality,
        formula="100 if both true, else partial/deduction",
        note=note,
    ))

    # Compute final score
    weighted_total = sum(c.weighted_score for c in components)
    final_score = clamp(round(weighted_total, 4))

    # Determine verdict
    if final_score >= VERDICT_COPY_CANDIDATE_MIN:
        verdict = TradeVerdict.COPY_CANDIDATE
    elif final_score >= VERDICT_WATCHLIST_MIN:
        verdict = TradeVerdict.WATCHLIST
    else:
        verdict = TradeVerdict.SKIP

    return TradeScoreResult(
        wallet_id=wallet_id,
        source_trade_id=source_trade_id,
        score=final_score,
        verdict=verdict,
        components=components,
        missing_essentials=missing_essentials,
        rejection_reasons=rejection_reasons,
        computed_at=now,
        is_sample=is_sample,
    )