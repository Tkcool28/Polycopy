"""Pure wallet scoring input adapter for PR24L.

This module adapts already-computed wallet accounting coverage rows into a stable
input shape for future wallet scoring.  It deliberately does not query SQLite,
score wallets, rank wallets, create copy candidates, or wire automation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from polycopy.engine.wallet_accounting_readiness import (
    AccountingReadinessConfig,
    accounting_readiness_from_coverage_row,
)

STATUS_SCORE_INPUT_READY = "score_input_ready"
STATUS_SCORE_INPUT_LIMITED = "score_input_limited"
STATUS_BLOCKED = "blocked"


@dataclass(frozen=True)
class WalletScoringInputCandidate:
    identity_key: str
    identity_group_by: str | None
    candidate_status: str
    ready_for_skill_score: bool
    ready_for_auto_copy: bool
    blocked_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    source_trades: int
    total_ledger_rows: int
    accounted_trades: int
    accounting_coverage_pct: float | None
    accountable_buy_coverage_pct: float | None
    buy_only_limitation: bool
    total_realized_pnl: float
    roi: float | None
    win_rate: float | None
    profit_factor: float | None


@dataclass(frozen=True)
class WalletScoringInputAdapterSummary:
    total_rows: int
    score_input_ready: int
    score_input_limited: int
    blocked: int
    auto_copy_ready: int
    auto_copy_blocked: int
    candidates: tuple[WalletScoringInputCandidate, ...]


def build_wallet_scoring_input_candidate(
    row: Any,
    *,
    readiness_config: AccountingReadinessConfig | None = None,
) -> WalletScoringInputCandidate:
    """Adapt one coverage row into a wallet scoring input candidate.

    The row is treated as read-only and must already contain coverage/accounting
    metrics.  Readiness is delegated to PR24K's pure accounting-readiness guard.
    """

    readiness = accounting_readiness_from_coverage_row(row, config=readiness_config)

    if not readiness.ready_for_skill_score:
        candidate_status = STATUS_BLOCKED
        ready_for_skill_score = False
        ready_for_auto_copy = False
        blocked_reasons = readiness.reasons
        warnings = readiness.warnings
    elif not readiness.ready_for_auto_copy:
        candidate_status = STATUS_SCORE_INPUT_LIMITED
        ready_for_skill_score = True
        ready_for_auto_copy = False
        blocked_reasons = ()
        warnings = _dedupe_tuple((*readiness.warnings, *readiness.reasons))
    else:
        candidate_status = STATUS_SCORE_INPUT_READY
        ready_for_skill_score = True
        ready_for_auto_copy = True
        blocked_reasons = ()
        warnings = readiness.warnings

    return WalletScoringInputCandidate(
        identity_key=readiness.identity_key,
        identity_group_by=readiness.identity_group_by,
        candidate_status=candidate_status,
        ready_for_skill_score=ready_for_skill_score,
        ready_for_auto_copy=ready_for_auto_copy,
        blocked_reasons=blocked_reasons,
        warnings=warnings,
        source_trades=readiness.total_source_trades,
        total_ledger_rows=readiness.total_ledger_rows,
        accounted_trades=readiness.accounted_trades,
        accounting_coverage_pct=readiness.accounting_coverage_pct,
        accountable_buy_coverage_pct=readiness.accountable_buy_coverage_pct,
        buy_only_limitation=readiness.buy_only_limitation,
        total_realized_pnl=getattr(row, "total_realized_pnl"),
        roi=getattr(row, "roi"),
        win_rate=getattr(row, "win_rate"),
        profit_factor=getattr(row, "profit_factor"),
    )


def build_wallet_scoring_input_candidates(
    rows: Any,
    *,
    readiness_config: AccountingReadinessConfig | None = None,
) -> WalletScoringInputAdapterSummary:
    """Adapt coverage rows into a summary while preserving input order."""

    candidates = tuple(
        build_wallet_scoring_input_candidate(row, readiness_config=readiness_config)
        for row in rows
    )
    return WalletScoringInputAdapterSummary(
        total_rows=len(candidates),
        score_input_ready=sum(
            1 for candidate in candidates if candidate.candidate_status == STATUS_SCORE_INPUT_READY
        ),
        score_input_limited=sum(
            1 for candidate in candidates if candidate.candidate_status == STATUS_SCORE_INPUT_LIMITED
        ),
        blocked=sum(1 for candidate in candidates if candidate.candidate_status == STATUS_BLOCKED),
        auto_copy_ready=sum(1 for candidate in candidates if candidate.ready_for_auto_copy),
        auto_copy_blocked=sum(1 for candidate in candidates if not candidate.ready_for_auto_copy),
        candidates=candidates,
    )


def _dedupe_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


__all__ = [
    "STATUS_BLOCKED",
    "STATUS_SCORE_INPUT_LIMITED",
    "STATUS_SCORE_INPUT_READY",
    "WalletScoringInputAdapterSummary",
    "WalletScoringInputCandidate",
    "build_wallet_scoring_input_candidate",
    "build_wallet_scoring_input_candidates",
]
