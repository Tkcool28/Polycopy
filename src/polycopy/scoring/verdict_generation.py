"""Deterministic verdict generation for PR 4 (Phase 3).

Implements the full frozen decision table for the final paper-signal
verdict. The decision is purely a function of the typed inputs — no
I/O, no hidden state, no silent defaults.

Decision order (each rule is short-circuit):

  1. wallet_score or wallet_verdict missing           -> INCOMPLETE
  2. wallet_verdict == INCOMPLETE                      -> INCOMPLETE
  3. category_wallet_score or category_wallet_verdict missing
                                                        -> INCOMPLETE
  4. category_wallet_verdict == "incomplete"           -> INCOMPLETE
  5. trade_score or trade_verdict missing              -> INCOMPLETE
  6. trade_verdict == INCOMPLETE                       -> INCOMPLETE
  7. behavior is MARKET_MAKER_LP / ARBITRAGE_MULTI_LEG / HIGH_FREQUENCY_BOT
                                                        -> SKIP
  8. has_hard_exclusion                                -> SKIP
  9. wallet_score < 55                                 -> SKIP
  10. 55 <= wallet_score < 75                          -> WATCHLIST
      (with behavior cap if MIXED or UNKNOWN)
  11. wallet_score >= 75, trade_score < 70             -> SKIP
      (skipped_reason = "skilled_wallet_trade_not_copyable")
  12. wallet_score >= 75, category_wallet_verdict
      != "copy_candidate"                              -> WATCHLIST
  13. ALL gates pass                                   -> COPY_CANDIDATE

INCOMPLETE propagation: explicit INCOMPLETE verdicts (e.g.
WalletVerdict.INCOMPLETE) are never overridden by numeric
placeholder scores. The "missing" check fires first, so a None
wallet_score produces INCOMPLETE even if a stale numeric
placeholder is also present.

Behavior classification caps:
  - MARKET_MAKER_LP, ARBITRAGE_MULTI_LEG, HIGH_FREQUENCY_BOT -> SKIP
  - MIXED, UNKNOWN cap at WATCHLIST (cannot be COPY_CANDIDATE)
  - DIRECTIONAL does not cap
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


# ---- Canonical verdicts ---------------------------------------------------

class SignalVerdict(str, enum.Enum):
    """Final signal verdict families."""

    COPY_CANDIDATE = "copy_candidate"
    WATCHLIST = "watchlist"
    SKIP = "skip"
    INCOMPLETE = "incomplete"


# ---- Canonical reason constants (enum, not free-text) -------------------

class SignalReason(str, enum.Enum):
    """Canonical reason identifiers for SignalDecision.reason.

    Every non-None reason produced by generate_signal_verdict is a
    value of this enum. Phase 3 spec branches map 1:1 to enum members.
    """

    # 1-2. wallet
    MISSING_WALLET = "missing_wallet_score"
    WALLET_INCOMPLETE = "wallet_verdict_incomplete"
    # 3-4. category
    MISSING_CATEGORY = "missing_category_score"
    CATEGORY_INCOMPLETE = "category_verdict_incomplete"
    # 5-6. trade
    MISSING_TRADE = "missing_trade_score"
    TRADE_INCOMPLETE = "trade_verdict_incomplete"
    # 7-9. behavior SKIP
    BEHAVIOR_MARKET_MAKER = "behavior_market_maker_lp"
    BEHAVIOR_ARBITRAGE = "behavior_arbitrage_multi_leg"
    BEHAVIOR_HFT = "behavior_high_frequency_bot"
    # 10-11. behavior WATCHLIST cap
    BEHAVIOR_MIXED_CAP = "behavior_mixed_watchlist_cap"
    BEHAVIOR_UNKNOWN_CAP = "behavior_unknown_watchlist_cap"
    # 12. hard exclusion
    HARD_EXCLUSION = "hard_exclusion"
    # 13-14. wallet score thresholds
    WALLET_BELOW_55 = "wallet_score_below_55"
    WALLET_WATCHLIST_RANGE = "wallet_score_watchlist_range"
    # 15. skilled wallet, non-copyable trade
    SKILLED_TRADE_NOT_COPYABLE = "skilled_wallet_trade_not_copyable"
    # 16. category below copy candidate
    CATEGORY_NOT_COPY = "category_verdict_not_copy_candidate"
    # 17. all gates pass
    ALL_GATES_MET = "all_thresholds_met"


# ---- Typed input --------------------------------------------------------

@dataclass
class SignalDecisionInput:
    """Typed input for signal decision generation.

    The category fields are typed separately from the wallet/trade
    fields so that the INCOMPLETE-propagation rules for category
    can be enforced independently of the wallet/trade rules.
    """

    wallet_score: Optional[float]  # 0-100
    wallet_verdict: Optional[WalletVerdict]
    category_wallet_score: Optional[float]  # 0-100
    category_wallet_verdict: Optional[str]  # "copy_candidate" | "watchlist" | "skip" | "incomplete" | None
    trade_score: Optional[float]  # 0-100
    trade_verdict: Optional[TradeVerdict]
    behavior_classification: Optional[BehaviorClassificationResult]
    has_hard_exclusion: bool = False
    hard_exclusion_reason: Optional[str] = None


# ---- Typed result --------------------------------------------------------

@dataclass
class SignalDecision:
    """Final signal decision output."""

    verdict: SignalVerdict
    reason: Optional[str] = None
    skipped_reason: Optional[str] = None
    would_be_verdict: Optional[str] = None  # For cap overrides


# ---- Decision engine -----------------------------------------------------

def generate_signal_verdict(input_data: SignalDecisionInput) -> SignalDecision:
    """Generate the final signal verdict from the typed scoring inputs.

    Implements the full Phase 3 decision table. Pure function: no
    side effects, no I/O, no hidden state.
    """

    # ---- 1. wallet missing ----
    if input_data.wallet_score is None or input_data.wallet_verdict is None:
        return SignalDecision(
            verdict=SignalVerdict.INCOMPLETE,
            reason=SignalReason.MISSING_WALLET.value,
        )

    # ---- 2. wallet INCOMPLETE ----
    if input_data.wallet_verdict == WalletVerdict.INCOMPLETE:
        return SignalDecision(
            verdict=SignalVerdict.INCOMPLETE,
            reason=SignalReason.WALLET_INCOMPLETE.value,
        )

    # ---- 3. category missing ----
    if (input_data.category_wallet_score is None
            or input_data.category_wallet_verdict is None):
        return SignalDecision(
            verdict=SignalVerdict.INCOMPLETE,
            reason=SignalReason.MISSING_CATEGORY.value,
        )

    # ---- 4. category INCOMPLETE ----
    if input_data.category_wallet_verdict == "incomplete":
        return SignalDecision(
            verdict=SignalVerdict.INCOMPLETE,
            reason=SignalReason.CATEGORY_INCOMPLETE.value,
        )

    # ---- 5. trade missing ----
    if input_data.trade_score is None or input_data.trade_verdict is None:
        return SignalDecision(
            verdict=SignalVerdict.INCOMPLETE,
            reason=SignalReason.MISSING_TRADE.value,
        )

    # ---- 6. trade INCOMPLETE ----
    if input_data.trade_verdict == TradeVerdict.INCOMPLETE:
        return SignalDecision(
            verdict=SignalVerdict.INCOMPLETE,
            reason=SignalReason.TRADE_INCOMPLETE.value,
        )

    # ---- 7-9. behavior SKIP branches ----
    behavior = input_data.behavior_classification
    if behavior is not None and behavior.is_skip:
        classification = behavior.classification
        if classification == BehaviorClassification.MARKET_MAKER_LP:
            reason = SignalReason.BEHAVIOR_MARKET_MAKER
        elif classification == BehaviorClassification.ARBITRAGE_MULTI_LEG:
            reason = SignalReason.BEHAVIOR_ARBITRAGE
        elif classification == BehaviorClassification.HIGH_FREQUENCY_BOT:
            reason = SignalReason.BEHAVIOR_HFT
        else:
            # Defensive: any other "is_skip" classification still SKIPs
            # but with a generic marker. Spec only enumerates the three
            # above; the dict is small enough to be explicit.
            reason = SignalReason.HARD_EXCLUSION
        return SignalDecision(
            verdict=SignalVerdict.SKIP,
            reason=reason.value,
        )

    # ---- 12. hard exclusion ----
    if input_data.has_hard_exclusion:
        # The hard-exclusion reason is operator-supplied (e.g.
        # "REGULATED_MARKET"). Pass it through as-is so audits can
        # trace the exact regulatory or operational cause.
        return SignalDecision(
            verdict=SignalVerdict.SKIP,
            reason=input_data.hard_exclusion_reason,
        )

    # ---- 10-11. behavior WATCHLIST cap (applied as a flag, not a hard rule) ----
    behavior_cap_reason: Optional[SignalReason] = None
    # Frozen contract: ONLY BehaviorClassification.DIRECTIONAL may
    # become COPY_CANDIDATE. Any non-DIRECTIONAL classification
    # (UNKNOWN, MIXED, MARKET_MAKER_LP, ARBITRAGE_MULTI_LEG,
    # HIGH_FREQUENCY_BOT) caps the verdict at WATCHLIST. A missing
    # (None) classification is treated the same as UNKNOWN — we
    # cannot prove the wallet is directional, so we cannot allow
    # COPY_CANDIDATE. The runtime classification layer always
    # returns a result; this None branch is a defensive guard so
    # that a caller-side regression cannot silently promote an
    # unknown-behavior wallet to COPY_CANDIDATE.
    if behavior is None:
        behavior_cap_reason = SignalReason.BEHAVIOR_UNKNOWN_CAP
    elif behavior.is_watchlist_cap:
        if behavior.classification == BehaviorClassification.MIXED:
            behavior_cap_reason = SignalReason.BEHAVIOR_MIXED_CAP
        elif behavior.classification == BehaviorClassification.UNKNOWN:
            behavior_cap_reason = SignalReason.BEHAVIOR_UNKNOWN_CAP
        # Any other cap-capable classification (e.g. future-added) still
        # caps at WATCHLIST but uses the generic MIXED reason.

    # ---- 13. wallet_score < 55 → SKIP ----
    if input_data.wallet_score < 55.0:
        return SignalDecision(
            verdict=SignalVerdict.SKIP,
            reason=SignalReason.WALLET_BELOW_55.value,
        )

    # ---- 14. 55 <= wallet_score < 75 → WATCHLIST ----
    if input_data.wallet_score < 75.0:
        if behavior_cap_reason is not None:
            return SignalDecision(
                verdict=SignalVerdict.WATCHLIST,
                reason=behavior_cap_reason.value,
            )
        return SignalDecision(
            verdict=SignalVerdict.WATCHLIST,
            reason=SignalReason.WALLET_WATCHLIST_RANGE.value,
        )

    # ---- 15. wallet_score >= 75, trade_score < 70 → SKIP ----
    if input_data.trade_score < 70.0:
        return SignalDecision(
            verdict=SignalVerdict.SKIP,
            reason=(
                f"trade_score={input_data.trade_score:.4f} < 70 "
                f"(wallet_score={input_data.wallet_score:.4f})"
            ),
            skipped_reason=SignalReason.SKILLED_TRADE_NOT_COPYABLE.value,
        )

    # ---- 16. category below COPY_CANDIDATE → WATCHLIST ----
    if input_data.category_wallet_verdict != "copy_candidate":
        return SignalDecision(
            verdict=SignalVerdict.WATCHLIST,
            reason=SignalReason.CATEGORY_NOT_COPY.value,
        )

    # ---- behavior cap still applies to a fully-otherwise-eligible
    #      candidate: MIXED/UNKNOWN cannot be COPY_CANDIDATE ----
    if behavior_cap_reason is not None:
        return SignalDecision(
            verdict=SignalVerdict.WATCHLIST,
            reason=behavior_cap_reason.value,
        )

    # ---- 17. ALL gates pass → COPY_CANDIDATE ----
    return SignalDecision(
        verdict=SignalVerdict.COPY_CANDIDATE,
        reason=SignalReason.ALL_GATES_MET.value,
    )
