"""Wallet behavior classification for copyability scoring.

Classifies wallet trading patterns to determine eligibility for copying.
Only DIRECTIONAL wallets may receive COPY CANDIDATE verdict.
Other classifications are capped to WATCHLIST or SKIP but retained for research.

Classifications:
- DIRECTIONAL: Focused on price movement predictions (copyable)
- MARKET_MAKER_LP: Continuous two-sided market making
- ARBITRAGE_MULTI_LEG: Multi-leg arbitrage strategies
- HIGH_FREQUENCY_BOT: Rapid short-interval trading
- MIXED: Mixed behavior patterns
- UNKNOWN: Insufficient data to classify
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class BehaviorClassification(str, enum.Enum):
    """Trading behavior classification for wallet eligibility."""

    DIRECTIONAL = "directional"
    MARKET_MAKER_LP = "market_maker_lp"
    ARBITRAGE_MULTI_LEG = "arbitrage_multi_leg"
    HIGH_FREQUENCY_BOT = "high_frequency_bot"
    MIXED = "mixed"
    UNKNOWN = "unknown"


@dataclass
class BehaviorEvidence:
    """Evidence collected for behavior classification.

    All fields are observable metrics from wallet trade history.
    None → evidence not available.
    """

    # Trade-level metrics
    trade_count: Optional[int] = None
    avg_trades_per_day: Optional[float] = None
    avg_time_between_trades_seconds: Optional[float] = None

    # Market-level diversity
    distinct_markets_traded: Optional[int] = None
    is_two_sided_market_making: Optional[bool] = None

    # Multi-leg detection
    is_multi_leg_pattern: Optional[bool] = None

    # Arbitrage pattern detection
    is_price_arbitrage_pattern: Optional[bool] = None


@dataclass
class BehaviorClassificationResult:
    """Result of wallet behavior classification.

    classification: The determined classification
    reasons: List of human-readable reasons supporting the classification
    is_eligible_for_copy: True if can receive COPY CANDIDATE verdict
    is_watchlist_cap: True if capped to WATCHLIST (MIXED/UNKNOWN)
    is_skip: True if should be SKIP (MM/Arbitrage/HF bot)
    """

    classification: BehaviorClassification
    reasons: list[str] = field(default_factory=list)
    is_eligible_for_copy: bool = False
    is_watchlist_cap: bool = False
    is_skip: bool = False

    @property
    def verdict_cap(self) -> Optional[str]:
        """Maximum verdict this classification can receive."""
        if self.is_eligible_for_copy:
            return None  # No cap
        if self.is_watchlist_cap:
            return "watchlist"
        if self.is_skip:
            return "skip"
        return None


def classify_wallet_behavior(evidence: BehaviorEvidence) -> BehaviorClassificationResult:
    """Classify wallet behavior from trade evidence.

    Transparent heuristics:
    - HF bot: < 10 seconds avg time between trades AND high trade count
    - Market maker LP: Two-sided making on same market (>50% both sides)
    - Arbitrage: Multi-leg pattern detected OR rapid cross-market timing
    - MIXED: Multiple conflicting patterns
    - UNKNOWN: Insufficient evidence

    Classification rules:
    - DIRECTIONAL: Not HF, not MM, not Arbitrage → eligible for copy
    - MARKET_MAKER_LP, ARBITRAGE_MULTI_LEG, HIGH_FREQUENCY_BOT: SKIP for copying
    - MIXED, UNKNOWN: WATCHLIST cap (cannot be COPY CANDIDATE)
    """
    reasons: list[str] = []

    if evidence.trade_count is None or evidence.trade_count < 5:
        # Insufficient trades for reliable classification
        reasons.append("insufficient_trades_for_classification")
        return BehaviorClassificationResult(
            classification=BehaviorClassification.UNKNOWN,
            reasons=reasons,
            is_eligible_for_copy=False,
            is_watchlist_cap=True,
            is_skip=False,
        )

    # Check for high-frequency bot pattern
    avg_time = evidence.avg_time_between_trades_seconds
    trade_count = evidence.trade_count
    if avg_time is not None and avg_time < 10.0 and trade_count > 50:
        reasons.append(f"high_frequency_detected_avg_time={avg_time:.1f}s_trade_count={trade_count}")
        return BehaviorClassificationResult(
            classification=BehaviorClassification.HIGH_FREQUENCY_BOT,
            reasons=reasons,
            is_eligible_for_copy=False,
            is_watchlist_cap=False,
            is_skip=True,
        )

    # Check for market maker LP pattern
    is_mm = evidence.is_two_sided_market_making
    if is_mm is True:
        reasons.append("two_sided_market_making_detected")
        return BehaviorClassificationResult(
            classification=BehaviorClassification.MARKET_MAKER_LP,
            reasons=reasons,
            is_eligible_for_copy=False,
            is_watchlist_cap=False,
            is_skip=True,
        )

    # Check for arbitrage multi-leg pattern
    if evidence.is_multi_leg_pattern is True:
        reasons.append("multi_leg_arbitrage_pattern_detected")
        return BehaviorClassificationResult(
            classification=BehaviorClassification.ARBITRAGE_MULTI_LEG,
            reasons=reasons,
            is_eligible_for_copy=False,
            is_watchlist_cap=False,
            is_skip=True,
        )

    # Check for price arbitrage pattern
    if evidence.is_price_arbitrage_pattern is True:
        reasons.append("price_arbitrage_pattern_detected")
        return BehaviorClassificationResult(
            classification=BehaviorClassification.ARBITRAGE_MULTI_LEG,
            reasons=reasons,
            is_eligible_for_copy=False,
            is_watchlist_cap=False,
            is_skip=True,
        )

    # Check for mixed pattern (multiple conflicting behaviors)
    markets = evidence.distinct_markets_traded
    if markets is not None and markets > 20:
        reasons.append(f"high_market_diversity_without_clear_pattern_markets={markets}")
        return BehaviorClassificationResult(
            classification=BehaviorClassification.MIXED,
            reasons=reasons,
            is_eligible_for_copy=False,
            is_watchlist_cap=True,
            is_skip=False,
        )

    # Default: classified as directional if basic evidence exists
    reasons.append("pattern_not_detected_defaulting_to_directional")
    return BehaviorClassificationResult(
        classification=BehaviorClassification.DIRECTIONAL,
        reasons=reasons,
        is_eligible_for_copy=True,
        is_watchlist_cap=False,
        is_skip=False,
    )