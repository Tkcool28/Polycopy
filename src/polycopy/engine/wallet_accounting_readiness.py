"""Pure wallet accounting readiness guard for future scoring/copying.

This module intentionally does not query SQLite or open database connections.  It
turns already-computed wallet accounting coverage metrics into a small, stable
readiness result that scoring/copying code can require before treating a wallet
as scoreable or copyable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ReadinessStatus = Literal["ready", "incomplete", "limited"]

STATUS_READY: ReadinessStatus = "ready"
STATUS_INCOMPLETE: ReadinessStatus = "incomplete"
STATUS_LIMITED: ReadinessStatus = "limited"

REASON_NO_SOURCE_TRADES = "no_source_trades"
REASON_NO_LEDGER_ROWS = "no_ledger_rows"
REASON_NO_ACCOUNTED_TRADES = "no_accounted_trades"
REASON_INSUFFICIENT_ACCOUNTED_TRADES = "insufficient_accounted_trades"
REASON_INSUFFICIENT_ACCOUNTING_COVERAGE = "insufficient_accounting_coverage"
REASON_BUY_ONLY_ACCOUNTING_LIMITATION = "buy_only_accounting_limitation"


@dataclass(frozen=True)
class AccountingReadinessConfig:
    """Thresholds for deciding whether wallet accounting is usable.

    ``min_accounting_coverage_pct`` is expressed as a 0..1 fraction, matching
    the PR24J coverage rows.  The default permits any non-missing numeric
    coverage once ledger/accounted-trade minimums are satisfied.
    """

    min_accounted_trades: int = 1
    min_accounting_coverage_pct: float = 0.0
    require_no_buy_only_limitation_for_auto_copy: bool = True

    def __post_init__(self) -> None:
        if self.min_accounted_trades < 0:
            raise ValueError("min_accounted_trades must be >= 0")
        if not 0.0 <= self.min_accounting_coverage_pct <= 1.0:
            raise ValueError("min_accounting_coverage_pct must be between 0 and 1")


@dataclass(frozen=True)
class AccountingReadinessResult:
    identity_key: str
    status: ReadinessStatus
    ready_for_skill_score: bool
    ready_for_auto_copy: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    total_source_trades: int
    total_ledger_rows: int
    accounted_trades: int
    accounting_coverage_pct: float | None
    accountable_buy_coverage_pct: float | None
    buy_only_limitation: bool


def accounting_readiness_from_coverage_row(
    row: Any,
    *,
    config: AccountingReadinessConfig | None = None,
) -> AccountingReadinessResult:
    """Evaluate readiness from a PR24J ``WalletAccountingCoverageRow``-like object.

    The function is deliberately duck-typed so this module stays pure and does
    not need to import PR24J's SQL/reporting module.  Objects need the same
    attribute names as ``WalletAccountingCoverageRow``.
    """

    total_ledger_rows = getattr(row, "total_ledger_rows", None)
    if total_ledger_rows is None:
        total_ledger_rows = getattr(row, "ledger_rows")

    return evaluate_wallet_accounting_readiness(
        identity_key=str(getattr(row, "identity_key")),
        total_source_trades=int(getattr(row, "source_trades")),
        total_ledger_rows=int(total_ledger_rows),
        accounted_trades=int(getattr(row, "accounted_trades")),
        accounting_coverage_pct=getattr(row, "accounting_coverage_pct"),
        accountable_buy_coverage_pct=getattr(row, "accountable_buy_coverage_pct", None),
        buy_only_limitation=bool(getattr(row, "buy_only_limitation")),
        config=config,
    )


def evaluate_wallet_accounting_readiness(
    *,
    identity_key: str,
    total_source_trades: int,
    total_ledger_rows: int,
    accounted_trades: int,
    accounting_coverage_pct: float | None,
    accountable_buy_coverage_pct: float | None = None,
    buy_only_limitation: bool = False,
    config: AccountingReadinessConfig | None = None,
) -> AccountingReadinessResult:
    """Return the pure accounting-readiness decision for one wallet identity.

    This guard does not score, rank, copy, query the database, or consume
    specialist aggregation.  Future scoring/copying code should call this before
    treating a wallet as scoreable/copyable.
    """

    cfg = config or AccountingReadinessConfig()
    reasons: list[str] = []
    warnings: list[str] = []

    if total_source_trades == 0:
        reasons.append(REASON_NO_SOURCE_TRADES)
    if total_ledger_rows == 0:
        reasons.append(REASON_NO_LEDGER_ROWS)
    if accounted_trades == 0:
        reasons.append(REASON_NO_ACCOUNTED_TRADES)
    if accounted_trades < cfg.min_accounted_trades:
        reasons.append(REASON_INSUFFICIENT_ACCOUNTED_TRADES)
    if accounting_coverage_pct is None:
        if cfg.min_accounting_coverage_pct > 0:
            reasons.append(REASON_INSUFFICIENT_ACCOUNTING_COVERAGE)
    elif accounting_coverage_pct < cfg.min_accounting_coverage_pct:
        reasons.append(REASON_INSUFFICIENT_ACCOUNTING_COVERAGE)

    if reasons:
        return AccountingReadinessResult(
            identity_key=identity_key,
            status=STATUS_INCOMPLETE,
            ready_for_skill_score=False,
            ready_for_auto_copy=False,
            reasons=tuple(dict.fromkeys(reasons)),
            warnings=tuple(warnings),
            total_source_trades=total_source_trades,
            total_ledger_rows=total_ledger_rows,
            accounted_trades=accounted_trades,
            accounting_coverage_pct=accounting_coverage_pct,
            accountable_buy_coverage_pct=accountable_buy_coverage_pct,
            buy_only_limitation=buy_only_limitation,
        )

    if buy_only_limitation:
        warnings.append(REASON_BUY_ONLY_ACCOUNTING_LIMITATION)
        if cfg.require_no_buy_only_limitation_for_auto_copy:
            return AccountingReadinessResult(
                identity_key=identity_key,
                status=STATUS_LIMITED,
                ready_for_skill_score=True,
                ready_for_auto_copy=False,
                reasons=(REASON_BUY_ONLY_ACCOUNTING_LIMITATION,),
                warnings=tuple(warnings),
                total_source_trades=total_source_trades,
                total_ledger_rows=total_ledger_rows,
                accounted_trades=accounted_trades,
                accounting_coverage_pct=accounting_coverage_pct,
                accountable_buy_coverage_pct=accountable_buy_coverage_pct,
                buy_only_limitation=buy_only_limitation,
            )

    return AccountingReadinessResult(
        identity_key=identity_key,
        status=STATUS_READY,
        ready_for_skill_score=True,
        ready_for_auto_copy=True,
        reasons=(),
        warnings=tuple(warnings),
        total_source_trades=total_source_trades,
        total_ledger_rows=total_ledger_rows,
        accounted_trades=accounted_trades,
        accounting_coverage_pct=accounting_coverage_pct,
        accountable_buy_coverage_pct=accountable_buy_coverage_pct,
        buy_only_limitation=buy_only_limitation,
    )


__all__ = [
    "AccountingReadinessConfig",
    "AccountingReadinessResult",
    "REASON_BUY_ONLY_ACCOUNTING_LIMITATION",
    "REASON_INSUFFICIENT_ACCOUNTED_TRADES",
    "REASON_INSUFFICIENT_ACCOUNTING_COVERAGE",
    "REASON_NO_ACCOUNTED_TRADES",
    "REASON_NO_LEDGER_ROWS",
    "REASON_NO_SOURCE_TRADES",
    "STATUS_INCOMPLETE",
    "STATUS_LIMITED",
    "STATUS_READY",
    "accounting_readiness_from_coverage_row",
    "evaluate_wallet_accounting_readiness",
]
