"""Counterfactual tracking — what-if analysis for all verdicts.

This module provides:
- CounterfactualScenario: a hypothetical scenario (what if we had copied?)
- CounterfactualResult: the outcome of a counterfactual scenario
- CounterfactualTracker: runs what-if analysis across all verdict types

For every wallet that receives a verdict (COPY_CANDIDATE, WATCHLIST, SKIP, INCOMPLETE),
the tracker computes what the outcome would have been under each mode:
- What if we had copied this wallet's trades?
- What if we had skipped them?
- What if we had partially copied (reduced size)?

This enables retrospective analysis without risking capital.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from polycopy.domain.copyability import Verdict

logger = logging.getLogger(__name__)


@dataclass
class CounterfactualScenario:
    """A hypothetical copy-trading scenario.

    Attributes:
        scenario_id: unique scenario ID
        wallet_id: the wallet being analyzed
        verdict: the original verdict that triggered this analysis
        scenario_type: what-if mode (full_copy / skip / half_size / quarter_size)
        assumed_entry_price: price at which we assume entry
        assumed_exit_price: price at which we assume exit
        assumed_quantity: quantity we assume copying
    """

    scenario_id: UUID
    wallet_id: UUID
    verdict: Verdict
    scenario_type: str  # "full_copy" / "skip" / "half_size" / "quarter_size"
    assumed_entry_price: float
    assumed_exit_price: float
    assumed_quantity: float
    is_sample: bool = False


@dataclass
class CounterfactualResult:
    """Result of a counterfactual scenario.

    Attributes:
        scenario: the scenario that was evaluated
        pnl: hypothetical P&L (positive = profit, negative = loss)
        return_pct: return as percentage of cost basis
        would_copy: whether this scenario would have been profitable
        lesson: one-line takeaway
        computed_at: when the computation was done (UTC)
    """

    scenario: CounterfactualScenario
    pnl: float
    return_pct: float
    would_copy: bool
    lesson: str
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_sample(self) -> bool:
        return self.scenario.is_sample


class CounterfactualTracker:
    """Runs what-if counterfactual analysis for all verdicts.

    For each wallet verdict, the tracker generates scenarios:
    - full_copy: what if we copied at full size?
    - skip: what if we did nothing?
    - half_size: what if we copied at 50% size?
    - quarter_size: what if we copied at 25% size?

    The skip scenario always has P&L = 0 (baseline for comparison).
    """

    def __init__(self) -> None:
        self._results: list[CounterfactualResult] = []

    def analyze_verdict(
        self,
        wallet_id: UUID,
        verdict: Verdict,
        entry_price: float,
        exit_price: float,
        quantity: float = 1.0,
        is_sample: bool = False,
    ) -> list[CounterfactualResult]:
        """Generate and evaluate counterfactual scenarios for a verdict.

        Args:
            wallet_id: the wallet being analyzed
            verdict: the original copyability verdict
            entry_price: assumed entry price
            exit_price: assumed exit price (resolution price)
            quantity: base quantity to copy
            is_sample: True for sample/fixture data

        Returns:
            List of CounterfactualResult (one per scenario type)
        """
        scenarios = [
            CounterfactualScenario(
                scenario_id=uuid4(),
                wallet_id=wallet_id,
                verdict=verdict,
                scenario_type="full_copy",
                assumed_entry_price=entry_price,
                assumed_exit_price=exit_price,
                assumed_quantity=quantity,
                is_sample=is_sample,
            ),
            CounterfactualScenario(
                scenario_id=uuid4(),
                wallet_id=wallet_id,
                verdict=verdict,
                scenario_type="skip",
                assumed_entry_price=entry_price,
                assumed_exit_price=exit_price,
                assumed_quantity=0.0,
                is_sample=is_sample,
            ),
            CounterfactualScenario(
                scenario_id=uuid4(),
                wallet_id=wallet_id,
                verdict=verdict,
                scenario_type="half_size",
                assumed_entry_price=entry_price,
                assumed_exit_price=exit_price,
                assumed_quantity=quantity * 0.5,
                is_sample=is_sample,
            ),
            CounterfactualScenario(
                scenario_id=uuid4(),
                wallet_id=wallet_id,
                verdict=verdict,
                scenario_type="quarter_size",
                assumed_entry_price=entry_price,
                assumed_exit_price=exit_price,
                assumed_quantity=quantity * 0.25,
                is_sample=is_sample,
            ),
        ]

        results = []
        for scenario in scenarios:
            result = self._evaluate_scenario(scenario)
            results.append(result)
            self._results.append(result)

        logger.debug(
            "Counterfactual: wallet=%s verdict=%s → %d scenarios evaluated",
            str(wallet_id)[:8],
            verdict.value,
            len(results),
        )
        return results

    def _evaluate_scenario(self, scenario: CounterfactualScenario) -> CounterfactualResult:
        """Evaluate a single counterfactual scenario."""
        if scenario.scenario_type == "skip":
            return CounterfactualResult(
                scenario=scenario,
                pnl=0.0,
                return_pct=0.0,
                would_copy=False,
                lesson="Skip: no position taken (baseline).",
            )

        pnl = (scenario.assumed_exit_price - scenario.assumed_entry_price) * scenario.assumed_quantity
        cost_basis = scenario.assumed_entry_price * scenario.assumed_quantity
        return_pct = (pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0
        would_copy = pnl > 0

        if would_copy:
            lesson = f"{scenario.scenario_type}: would have earned +{pnl:.4f} ({return_pct:+.1f}%)."
        else:
            lesson = f"{scenario.scenario_type}: would have lost {pnl:.4f} ({return_pct:+.1f}%)."

        return CounterfactualResult(
            scenario=scenario,
            pnl=round(pnl, 6),
            return_pct=round(return_pct, 2),
            would_copy=would_copy,
            lesson=lesson,
        )

    def get_profitable_scenarios(
        self,
        wallet_id: Optional[UUID] = None,
    ) -> list[CounterfactualResult]:
        """Return all profitable counterfactual scenarios."""
        results = self._results
        if wallet_id is not None:
            results = [r for r in results if r.scenario.wallet_id == wallet_id]
        return [r for r in results if r.would_copy]

    def get_results_for_wallet(self, wallet_id: UUID) -> list[CounterfactualResult]:
        """Return all counterfactual results for a wallet."""
        return [r for r in self._results if r.scenario.wallet_id == wallet_id]

    def list_results(self) -> list[CounterfactualResult]:
        """Return all counterfactual results."""
        return list(self._results)

    @property
    def result_count(self) -> int:
        return len(self._results)

    @property
    def profitable_count(self) -> int:
        return sum(1 for r in self._results if r.would_copy)
