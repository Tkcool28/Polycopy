"""Scoring package — deterministic copyability scoring engine.

PR 4 introduces frozen formula modules for wallet score v1, trade
copyability v1, v2 shadow scoring, and paper signal generation.
"""

from polycopy.scoring.helpers import linear_score, inverse_score, clamp
from polycopy.scoring.behavior_classification import (
    BehaviorClassification,
    BehaviorEvidence,
    BehaviorClassificationResult,
    classify_wallet_behavior,
)
from polycopy.scoring.wallet_score_v1 import (
    WalletVerdict,
    WalletScoreComponent,
    WalletScoreResult,
    compute_wallet_score_v1,
    VERDICT_COPY_CANDIDATE_MIN as WALLET_VERDICT_COPY_CANDIDATE_MIN,
    VERDICT_WATCHLIST_MIN as WALLET_VERDICT_WATCHLIST_MIN,
)
from polycopy.scoring.trade_score_v1 import (
    TradeVerdict,
    TradeScoreResult,
    compute_trade_score_v1,
    VERDICT_COPY_CANDIDATE_MIN as TRADE_VERDICT_COPY_CANDIDATE_MIN,
    VERDICT_WATCHLIST_MIN as TRADE_VERDICT_WATCHLIST_MIN,
)
from polycopy.scoring.shadow_score_v2 import (
    ShadowVerdict,
    ShadowScoreResult,
    compute_shadow_score_v2,
)
from polycopy.scoring.verdict_generation import (
    SignalVerdict,
    SignalDecisionInput,
    SignalDecision,
    generate_signal_verdict,
)
from polycopy.scoring.score_serialization import (
    generate_idempotency_key,
    persist_wallet_score_v1,
    persist_trade_score_v1,
    persist_shadow_score_v2,
    persist_paper_signal,
    record_exit_experiments,
)

__all__ = [
    # Helpers
    "linear_score",
    "inverse_score",
    "clamp",
    # Behavior classification
    "BehaviorClassification",
    "BehaviorEvidence",
    "BehaviorClassificationResult",
    "classify_wallet_behavior",
    # Wallet score v1
    "WalletVerdict",
    "WalletScoreComponent",
    "WalletScoreResult",
    "compute_wallet_score_v1",
    "WALLET_VERDICT_COPY_CANDIDATE_MIN",
    "WALLET_VERDICT_WATCHLIST_MIN",
    # Trade score v1
    "TradeVerdict",
    "TradeScoreResult",
    "compute_trade_score_v1",
    "TRADE_VERDICT_COPY_CANDIDATE_MIN",
    "TRADE_VERDICT_WATCHLIST_MIN",
    # V2 shadow
    "ShadowVerdict",
    "ShadowScoreResult",
    "compute_shadow_score_v2",
    # Verdict generation
    "SignalVerdict",
    "SignalDecisionInput",
    "SignalDecision",
    "generate_signal_verdict",
    # Serialization
    "generate_idempotency_key",
    "persist_wallet_score_v1",
    "persist_trade_score_v1",
    "persist_shadow_score_v2",
    "persist_paper_signal",
    "record_exit_experiments",
]