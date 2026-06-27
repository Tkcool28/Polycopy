"""Tests for P04 counterfactual tracking — what-if analysis for all verdicts."""

import pytest
from uuid import uuid4

from polycopy.risk.counterfactual import (
    CounterfactualTracker,
)
from polycopy.domain.copyability import Verdict


class TestCounterfactualTracker:
    def test_four_scenarios_generated(self):
        tracker = CounterfactualTracker()
        results = tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.COPY_CANDIDATE,
            entry_price=0.60,
            exit_price=0.80,
            quantity=100.0,
        )
        assert len(results) == 4
        types = {r.scenario.scenario_type for r in results}
        assert types == {"full_copy", "skip", "half_size", "quarter_size"}

    def test_full_copy_profitable(self):
        tracker = CounterfactualTracker()
        results = tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.COPY_CANDIDATE,
            entry_price=0.60,
            exit_price=0.80,
            quantity=100.0,
        )
        full = [r for r in results if r.scenario.scenario_type == "full_copy"][0]
        assert full.pnl == pytest.approx(20.0)  # (0.80 - 0.60) * 100
        assert full.would_copy is True

    def test_skip_always_zero(self):
        tracker = CounterfactualTracker()
        results = tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.COPY_CANDIDATE,
            entry_price=0.60,
            exit_price=0.80,
            quantity=100.0,
        )
        skip = [r for r in results if r.scenario.scenario_type == "skip"][0]
        assert skip.pnl == 0.0
        assert skip.would_copy is False

    def test_half_size_pnl_is_half(self):
        tracker = CounterfactualTracker()
        results = tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.COPY_CANDIDATE,
            entry_price=0.60,
            exit_price=0.80,
            quantity=100.0,
        )
        full = [r for r in results if r.scenario.scenario_type == "full_copy"][0]
        half = [r for r in results if r.scenario.scenario_type == "half_size"][0]
        assert half.pnl == pytest.approx(full.pnl * 0.5)

    def test_quarter_size_pnl_is_quarter(self):
        tracker = CounterfactualTracker()
        results = tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.COPY_CANDIDATE,
            entry_price=0.60,
            exit_price=0.80,
            quantity=100.0,
        )
        full = [r for r in results if r.scenario.scenario_type == "full_copy"][0]
        quarter = [r for r in results if r.scenario.scenario_type == "quarter_size"][0]
        assert quarter.pnl == pytest.approx(full.pnl * 0.25)

    def test_loss_scenario(self):
        tracker = CounterfactualTracker()
        results = tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.SKIP,
            entry_price=0.70,
            exit_price=0.50,
            quantity=100.0,
        )
        full = [r for r in results if r.scenario.scenario_type == "full_copy"][0]
        assert full.pnl == pytest.approx(-20.0)
        assert full.would_copy is False
        assert "lost" in full.lesson.lower() or "lose" in full.lesson.lower()

    def test_profitable_scenarios_filter(self):
        tracker = CounterfactualTracker()
        wid = uuid4()
        tracker.analyze_verdict(
            wallet_id=wid,
            verdict=Verdict.COPY_CANDIDATE,
            entry_price=0.60,
            exit_price=0.80,
            quantity=100.0,
        )
        profitable = tracker.get_profitable_scenarios(wid)
        # full_copy, half_size, quarter_size are profitable; skip is not
        assert len(profitable) == 3

    def test_results_for_wallet(self):
        tracker = CounterfactualTracker()
        wid = uuid4()
        tracker.analyze_verdict(
            wallet_id=wid,
            verdict=Verdict.COPY_CANDIDATE,
            entry_price=0.60,
            exit_price=0.80,
            quantity=100.0,
        )
        assert len(tracker.get_results_for_wallet(wid)) == 4

    def test_result_count(self):
        tracker = CounterfactualTracker()
        tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.COPY_CANDIDATE,
            entry_price=0.60,
            exit_price=0.80,
            quantity=100.0,
        )
        tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.WATCHLIST,
            entry_price=0.60,
            exit_price=0.40,
            quantity=50.0,
        )
        assert tracker.result_count == 8

    def test_profitable_count(self):
        tracker = CounterfactualTracker()
        tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.COPY_CANDIDATE,
            entry_price=0.60,
            exit_price=0.80,  # profitable
            quantity=100.0,
        )
        tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.SKIP,
            entry_price=0.60,
            exit_price=0.40,  # unprofitable
            quantity=100.0,
        )
        # First: 3 profitable (full, half, quarter). Second: 0 profitable.
        assert tracker.profitable_count == 3

    def test_sample_flag(self):
        tracker = CounterfactualTracker()
        results = tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.COPY_CANDIDATE,
            entry_price=0.60,
            exit_price=0.80,
            quantity=100.0,
            is_sample=True,
        )
        assert all(r.is_sample for r in results)

    def test_return_pct(self):
        tracker = CounterfactualTracker()
        results = tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.COPY_CANDIDATE,
            entry_price=0.50,
            exit_price=0.70,
            quantity=100.0,
        )
        full = [r for r in results if r.scenario.scenario_type == "full_copy"][0]
        # (20 / 50) * 100 = 40%
        assert full.return_pct == pytest.approx(40.0)
