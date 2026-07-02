"""Deterministic verdict generation for PR 4.

Decision mapping:
- Wallet score >=75 AND category verdict COPY CANDIDATE AND trade copyability >=70 AND no hard exclusion
  → COPY CANDIDATE / ADVANCE TO PAPER SIGNAL EVALUATION
- Wallet score >=75 but trade copyability <70
  → SKIP with reason SKILLED_WALLET_TRADE_NOT_COPYABLE
- Wallet score 55–74.9999
  → WATCHLIST
- Wallet score below 55
  → SKIP
- Missing essential evidence
  → INCOMPLETE

Behavior classification caps:
- Only DIRECTIONAL may receive normal COPY CANDIDATE
- MIXED and UNKNOWN capped at WATCHLIST
- MARKET_MAKER_LP, ARBITRAGE_MULTI_LEG, HIGH_FREQUENCY_BOT → SKIP but retained for research
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from polycopy.scoring.wallet_score_v1 import WalletVerdict
from polycopy.scoring.trade_score_v1 import TradeVerdict
from polycopy.scoring.behavior_classification import (
    BehaviorClassification,
    BehaviorClassificationResult,
)


class SignalVerdict(str, enum.Enum):
    """Final signal verdict families."""

    COPY_CANDIDATE = "copy_candidate"
    WATCHLIST = "watchlist"
    SKIP = "skip"
    INCOMPLETE = "incomplete"


@dataclass
class SignalDecisionInput:
    """Input for signal decision generation."""

    wallet_score: Optional[float]  # 0-100
    wallet_verdict: Optional[WalletVerdict]
    category_wallet_verdict: Optional[str]  # "copy_candidate" or other
    trade_score: Optional[float]  # 0-100
    trade_verdict: Optional[TradeVerdict]
    behavior_classification: Optional[BehaviorClassificationResult]
    has_hard_exclusion: bool = False
    hard_exclusion_reason: Optional[str] = None


@dataclass
class SignalDecision:
    """Final signal decision output."""

    verdict: SignalVerdict
    reason: Optional[str] = None
    skipped_reason: Optional[str] = None
    would_be_verdict: Optional[str] = None  # For cap overrides


def generate_signal_verdict(input_data: SignalDecisionInput) -> SignalDecision:
    """Generate deterministic signal verdict from scoring results.

    All four canonical verdict families:
    - COPY_CANDIDATE
    - WATCHLIST
    - SKIP
    - INCOMPLETE
    """
    # Check for missing essential evidence first
    if input_data.wallet_score is None or input_data.wallet_verdict is None:
        return SignalDecision(
            verdict=SignalVerdict.INCOMPLETE,
            reason="missing_wallet_score",
        )

    if input_data.trade_score is None or input_data.trade_verdict is None:
        return SignalDecision(
            verdict=SignalVerdict.INCOMPLETE,
            reason="missing_trade_score",
        )

    # Check behavior classification caps
    behavior_caps_verdict = False
    behavior_cap_reason = None

    if input_data.behavior_classification is not None:
        if input_data.behavior_classification.is_skip:
            return SignalDecision(
                verdict=SignalVerdict.SKIP,
                reason=f"behavior_classification={input_data.behavior_classification.classification.value}",
            )
        if input_data.behavior_classification.is_watchlist_cap:
            behavior_caps_verdict = True
            behavior_cap_reason = f"behavior_cap={input_data.behavior_classification.classification.value}"

    # Check hard exclusion
    if input_data.has_hard_exclusion:
        return SignalDecision(
            verdict=SignalVerdict.SKIP,
            reason=input_data.hard_exclusion_reason,
        )

    # Decision mapping
    wallet_score = input_data.wallet_score
    trade_score = input_data.trade_score

    # Wallet score below 55 → SKIP
    if wallet_score < 55.0:
        return SignalDecision(
            verdict=SignalVerdict.SKIP,
            reason="wallet_score_below_threshold",
        )

    # Wallet score 55-74.9999 → WATCHLIST (or capped to WATCHLIST if behavior cap)
    if 55.0 <= wallet_score < 75.0:
        verdict = SignalVerdict.WATCHLIST
        if behavior_caps_verdict:
            return SignalDecision(
                verdict=verdict,
                reason=behavior_cap_reason,
            )
        return SignalDecision(verdict=verdict, reason="wallet_score_watchlist_range")

    # Wallet score >=75
    if wallet_score >= 75.0:
        # Check trade copyability threshold
        if trade_score < 70.0:
            return SignalDecision(
                verdict=SignalVerdict.SKIP,
                skipped_reason="skilled_wallet_trade_not_copyable",
                reason=f"trade_score={trade_score:.4f} < 70",
            )

        # Check category verdict
        if input_data.category_wallet_verdict is not None:
            if input_data.category_wallet_verdict != "copy_candidate":
                return SignalDecision(
                    verdict=SignalVerdict.WATCHLIST,
                    reason=f"category_verdict={input_data.category_wallet_verdict}_not_copy_candidate",
                )

        # All conditions met for COPY CANDIDATE
        if behavior_caps_verdict:
            return SignalDecision(
                verdict=SignalVerdict.WATCHLIST,
                reason=behavior_cap_reason,
            )

        return SignalDecision(
            verdict=SignalVerdict.COPY_CANDIDATE,
            reason="all_thresholds_met",
        )

    # Should not reach here, but be safe
    return SignalDecision(
        verdict=SignalVerdict.INCOMPLETE,
        reason="unreachable_state",
    )