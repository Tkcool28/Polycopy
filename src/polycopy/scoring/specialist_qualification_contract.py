"""Specialist qualification contract — single source of truth for evidence gates.

This module defines the immutable qualification contract shared by the
authoritative wallet evaluator, the authoritative category evaluator, the
readiness/status monitor, and all downstream eligibility checks.

The contract enforces: **a high numerical score can never override
insufficient evidence.** Every gate is mandatory. Missing evidence
fails closed.

Wallet-level qualification (copy_candidate) requires ALL of:
  - wallet score >= 75
  - resolved markets >= 30
  - active trading days >= 20
  - distinct events >= 15

Category-level qualification (copy_candidate) requires ALL of:
  - category score >= 75
  - resolved category markets >= 15
  - distinct category events >= 8
  - category-active days >= 10

Category qualification is independently evaluated and must NOT be
inferred from the parent wallet verdict. At least one category must
independently qualify before the wallet is treated downstream as a
usable category specialist.

This module does NOT alter scoring weights, formulas, thresholds, or
formula versions. It only centralizes the gate definitions and provides
deterministic evaluation helpers so every consumer agrees.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# ---- Wallet gate thresholds (frozen, authoritative) -----------------------

WALLET_SCORE_THRESHOLD = 75
WALLET_MIN_RESOLVED_MARKETS = 30
WALLET_MIN_ACTIVE_TRADING_DAYS = 20
WALLET_MIN_DISTINCT_EVENTS = 15

# ---- Category gate thresholds (frozen, authoritative) ---------------------

CATEGORY_SCORE_THRESHOLD = 75
CATEGORY_MIN_RESOLVED_MARKETS = 15
CATEGORY_MIN_DISTINCT_EVENTS = 8
CATEGORY_MIN_ACTIVE_DAYS = 10

# ---- Canonical gate ordering (deterministic failure representation) -------
# Failures are always reported in this order regardless of which code path
# evaluates first. This guarantees deterministic serialization.

WALLET_GATE_ORDER: tuple[str, ...] = (
    "score",
    "resolved_markets",
    "active_trading_days",
    "distinct_events",
)

CATEGORY_GATE_ORDER: tuple[str, ...] = (
    "score",
    "category_resolved_markets",
    "category_distinct_events",
    "category_active_days",
)


@dataclass(frozen=True)
class WalletGateFailure:
    """A single wallet gate failure with deterministic ordering."""

    gate: str
    value: Any  # int for evidence gates, float for score gate, None for missing
    minimum: int

    @property
    def reason(self) -> str:
        if self.value is None:
            return f"{self.gate}=missing < {self.minimum}"
        return f"{self.gate}={self.value} < {self.minimum}"


@dataclass(frozen=True)
class CategoryGateFailure:
    """A single category gate failure with deterministic ordering."""

    gate: str
    value: Any  # int for evidence gates, float for score gate, None for missing
    minimum: int

    @property
    def reason(self) -> str:
        if self.value is None:
            return f"{self.gate}=missing < {self.minimum}"
        return f"{self.gate}={self.value} < {self.minimum}"


@dataclass(frozen=True)
class WalletQualification:
    """Result of wallet qualification evaluation.

    ``qualified`` is True only when every wallet gate passes.
    ``gate_failures`` is always ordered by :data:`WALLET_GATE_ORDER`.
    ``score`` is the numeric score used for the threshold check (may be
    None when score evidence is missing).
    """

    qualified: bool
    score: float | None
    gate_failures: tuple[str, ...] = field(default_factory=tuple)
    missing_evidence: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CategoryQualification:
    """Result of category qualification evaluation.

    ``qualified`` is True only when every category gate passes.
    ``gate_failures`` is always ordered by :data:`CATEGORY_GATE_ORDER`.
    ``score`` is the numeric score used for the threshold check (may be
    None when score evidence is missing).
    """

    qualified: bool
    score: float | None
    gate_failures: tuple[str, ...] = field(default_factory=tuple)
    missing_evidence: tuple[str, ...] = field(default_factory=tuple)


def evaluate_wallet_qualification(
    *,
    score: float | None,
    resolved_markets: int | None,
    active_trading_days: int | None,
    distinct_events: int | None,
) -> WalletQualification:
    """Evaluate wallet qualification against the frozen contract.

    Every gate is mandatory. Missing evidence (None) fails closed.
    The score gate is checked first in the canonical ordering, then
    the evidence gates in :data:`WALLET_GATE_ORDER`.

    Returns a :class:`WalletQualification` with deterministically
    ordered ``gate_failures``.
    """
    failures: list[WalletGateFailure] = []

    # Score gate
    if score is None:
        failures.append(WalletGateFailure("score", None, WALLET_SCORE_THRESHOLD))
    elif score < WALLET_SCORE_THRESHOLD:
        failures.append(
            WalletGateFailure("score", int(score) if score == int(score) else score, WALLET_SCORE_THRESHOLD)
        )

    # Evidence gates (in canonical order)
    _wallet_evidence_gates(
        resolved_markets, active_trading_days, distinct_events, failures
    )

    ordered_reasons = tuple(f.reason for f in _sort_wallet_failures(failures))
    qualified = len(failures) == 0
    missing = tuple(
        f.reason for f in _sort_wallet_failures(failures) if f.value is None
    )
    return WalletQualification(
        qualified=qualified,
        score=score,
        gate_failures=ordered_reasons,
        missing_evidence=missing,
    )


def _wallet_evidence_gates(
    resolved_markets: int | None,
    active_trading_days: int | None,
    distinct_events: int | None,
    failures: list[WalletGateFailure],
) -> None:
    """Append evidence gate failures in canonical order."""
    specs = (
        ("resolved_markets", resolved_markets, WALLET_MIN_RESOLVED_MARKETS),
        ("active_trading_days", active_trading_days, WALLET_MIN_ACTIVE_TRADING_DAYS),
        ("distinct_events", distinct_events, WALLET_MIN_DISTINCT_EVENTS),
    )
    for name, value, minimum in specs:
        if value is None:
            failures.append(WalletGateFailure(name, None, minimum))
        elif value < minimum:
            failures.append(WalletGateFailure(name, value, minimum))


def _sort_wallet_failures(
    failures: Sequence[WalletGateFailure],
) -> list[WalletGateFailure]:
    """Sort wallet gate failures by canonical WALLET_GATE_ORDER."""
    rank = {name: i for i, name in enumerate(WALLET_GATE_ORDER)}
    return sorted(failures, key=lambda f: rank.get(f.gate, len(rank)))


def evaluate_category_qualification(
    *,
    score: float | None,
    category_resolved_markets: int | None,
    category_distinct_events: int | None,
    category_active_days: int | None,
) -> CategoryQualification:
    """Evaluate category qualification against the frozen contract.

    Every gate is mandatory. Missing evidence (None) fails closed.
    The score gate is checked first in the canonical ordering, then
    the evidence gates in :data:`CATEGORY_GATE_ORDER`.

    Returns a :class:`CategoryQualification` with deterministically
    ordered ``gate_failures``.
    """
    failures: list[CategoryGateFailure] = []

    # Score gate
    if score is None:
        failures.append(
            CategoryGateFailure("score", None, CATEGORY_SCORE_THRESHOLD)
        )
    elif score < CATEGORY_SCORE_THRESHOLD:
        failures.append(
            CategoryGateFailure(
                "score",
                int(score) if score == int(score) else score,
                CATEGORY_SCORE_THRESHOLD,
            )
        )

    # Evidence gates (in canonical order)
    _category_evidence_gates(
        category_resolved_markets,
        category_distinct_events,
        category_active_days,
        failures,
    )

    ordered_reasons = tuple(f.reason for f in _sort_category_failures(failures))
    qualified = len(failures) == 0
    missing = tuple(
        f.reason for f in _sort_category_failures(failures) if f.value is None
    )
    return CategoryQualification(
        qualified=qualified,
        score=score,
        gate_failures=ordered_reasons,
        missing_evidence=missing,
    )


def _category_evidence_gates(
    category_resolved_markets: int | None,
    category_distinct_events: int | None,
    category_active_days: int | None,
    failures: list[CategoryGateFailure],
) -> None:
    """Append category evidence gate failures in canonical order."""
    specs = (
        (
            "category_resolved_markets",
            category_resolved_markets,
            CATEGORY_MIN_RESOLVED_MARKETS,
        ),
        (
            "category_distinct_events",
            category_distinct_events,
            CATEGORY_MIN_DISTINCT_EVENTS,
        ),
        (
            "category_active_days",
            category_active_days,
            CATEGORY_MIN_ACTIVE_DAYS,
        ),
    )
    for name, value, minimum in specs:
        if value is None:
            failures.append(CategoryGateFailure(name, None, minimum))
        elif value < minimum:
            failures.append(CategoryGateFailure(name, value, minimum))


def _sort_category_failures(
    failures: Sequence[CategoryGateFailure],
) -> list[CategoryGateFailure]:
    """Sort category gate failures by canonical CATEGORY_GATE_ORDER."""
    rank = {name: i for i, name in enumerate(CATEGORY_GATE_ORDER)}
    return sorted(failures, key=lambda f: rank.get(f.gate, len(rank)))


__all__ = [
    "CATEGORY_GATE_ORDER",
    "CATEGORY_MIN_ACTIVE_DAYS",
    "CATEGORY_MIN_DISTINCT_EVENTS",
    "CATEGORY_MIN_RESOLVED_MARKETS",
    "CATEGORY_SCORE_THRESHOLD",
    "WALLET_GATE_ORDER",
    "WALLET_MIN_ACTIVE_TRADING_DAYS",
    "WALLET_MIN_DISTINCT_EVENTS",
    "WALLET_MIN_RESOLVED_MARKETS",
    "WALLET_SCORE_THRESHOLD",
    "CategoryGateFailure",
    "CategoryQualification",
    "WalletGateFailure",
    "WalletQualification",
    "evaluate_category_qualification",
    "evaluate_wallet_qualification",
]
