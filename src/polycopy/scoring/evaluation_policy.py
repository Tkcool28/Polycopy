"""Typed execution policy for the one canonical paper evaluator."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class EvaluationExecutionPolicy:
    persist_wallet_score: bool = True
    persist_category_score: bool = True
    persist_trade_copyability: bool = True
    persist_paper_signal: bool = True
    persist_shadow: bool = True
    persist_exit_experiments: bool = True
    allow_candidate_creation: bool = False
    allow_snapshot_creation: bool = False
    allow_approval: bool = False

    @classmethod
    def decision_only(cls) -> "EvaluationExecutionPolicy":
        return cls(persist_shadow=False, persist_exit_experiments=False)


DEFAULT_EVALUATION_POLICY = EvaluationExecutionPolicy()
DECISION_ONLY_EVALUATION_POLICY = EvaluationExecutionPolicy.decision_only()
