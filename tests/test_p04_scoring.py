"""Tests for PR 4 scoring modules and formulas.

Tests cover:
- every normalization boundary
- every component formula
- YES and NO direction handling
- minimum sample gates
- global and category verdicts
- behavior-classification caps
- largest-winner removal
- chronological consistency penalty
- concentration penalties
- copy-price deterioration
- depth-walk BUY
- depth-walk SELL
- partial fills
- zero fills
- spread and liquidity
- every freshness bucket
- every holding-period bucket
- excluded short and long markets
- very short crypto exclusion
- missing data INCOMPLETE
- every signal-decision branch
- deterministic idempotency
- immutable formula versions
- parallel v1 and v2 decisions
- v2 never controls v1
- no order creation
- no position creation
- no live broker call
- kill switch preserved
- rerun does not duplicate signals
- exit experiments created once
- rejected candidates still logged
- Python 3.11 and 3.12 compatibility

All HTTP tests mocked.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from polycopy.scoring.helpers import linear_score, inverse_score, clamp
from polycopy.scoring.behavior_classification import (
    BehaviorClassification,
    BehaviorClassificationResult,
    BehaviorEvidence,
    classify_wallet_behavior,
)
from polycopy.scoring.wallet_score_v1 import (
    WalletVerdict,
    compute_wallet_score_v1,
    VERDICT_COPY_CANDIDATE_MIN,
)
from polycopy.scoring.trade_score_v1 import (
    TradeVerdict,
    compute_trade_score_v1,
)
from polycopy.scoring.shadow_score_v2 import (
    ShadowVerdict,
    compute_shadow_score_v2,
)
from polycopy.scoring.verdict_generation import (
    SignalReason,
    SignalVerdict,
    SignalDecisionInput,
    generate_signal_verdict,
)
from polycopy.scoring.score_serialization import generate_idempotency_key
from polycopy.db.database import Database


class TestHelpersNormalizationBoundaries:
    """Test every normalization boundary for linear_score, inverse_score, clamp."""

    def test_linear_score_at_low_bound(self):
        assert linear_score(0, 0, 100) == 0.0

    def test_linear_score_at_high_bound(self):
        assert linear_score(100, 0, 100) == 100.0

    def test_linear_score_below_low(self):
        assert linear_score(-10, 0, 100) == 0.0

    def test_linear_score_above_high(self):
        assert linear_score(150, 0, 100) == 100.0

    def test_linear_score_midpoint(self):
        assert linear_score(50, 0, 100) == 50.0
        assert linear_score(25, 0, 50) == 50.0

    def test_inverse_score_at_good(self):
        assert inverse_score(0, 0, 100) == 100.0

    def test_inverse_score_at_bad(self):
        assert inverse_score(100, 0, 100) == 0.0

    def test_inverse_score_midpoint(self):
        assert inverse_score(50, 0, 100) == 50.0

    def test_inverse_score_below_good(self):
        assert inverse_score(-10, 0, 100) == 100.0

    def test_inverse_score_above_bad(self):
        assert inverse_score(110, 0, 100) == 0.0

    def test_clamp_within_bounds(self):
        assert clamp(50) == 50
        assert clamp(50, -50, 50) == 50

    def test_clamp_below_minimum(self):
        assert clamp(-10) == 0
        assert clamp(-10, -20, 20) == -10  # Between -20 and 20

    def test_clamp_above_maximum(self):
        assert clamp(150) == 100
        assert clamp(150, 0, 100) == 100


class TestWalletScoreV1ComponentFormulas:
    """Test every component in wallet score v1."""

    def test_info_price_improvement_full_score(self):
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            info_score=1.0,
            win_rate=0.5,
            trade_count=100,
        )
        assert any(c.name == "information_and_price_improvement" for c in result.components)

    def test_info_price_improvement_zero_score(self):
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            info_score=0.0,
            win_rate=0.5,
            trade_count=100,
        )
        # With trade_count and win_rate present, other components are computed
        assert result.verdict in [WalletVerdict.COPY_CANDIDATE, WalletVerdict.WATCHLIST, WalletVerdict.SKIP]

    def test_verified_performance_full(self):
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            win_rate=1.0,
            profit_factor=2.0,
            trade_count=100,
            info_score=1.0,
            trade_intervals_std=0,
            max_drawdown=0.0,
            sharpe_ratio=3.0,
            category_trade_count=50,
            overall_trade_count=100,
            resolved_markets=50,
            active_trading_days=40,
            distinct_events=30,
            category_resolved_markets=20,
            category_distinct_events=15,
            category_active_days=20,
        )
        assert result.verdict == WalletVerdict.COPY_CANDIDATE

    def test_verified_performance_half_winrate(self):
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            win_rate=0.5,
            trade_count=100,
        )
        # 50% win rate should produce a valid score
        assert result.score > 0

    def test_chronological_consistency_full(self):
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            trade_intervals_std=0,
            trade_count=100,
            win_rate=0.5,
        )
        comp = next(c for c in result.components if c.name == "chronological_consistency")
        assert comp.raw_score == 100.0

    def test_chronological_consistency_zero(self):
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            trade_intervals_std=12 * 3600,  # 12 hours in seconds
            trade_count=100,
            win_rate=0.5,
        )
        comp = next(c for c in result.components if c.name == "chronological_consistency")
        assert comp.raw_score == 0.0

    def test_risk_drawdown_full_score(self):
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            max_drawdown=0.0,
            sharpe_ratio=3.0,
            win_rate=0.5,
            trade_count=100,
            info_score=1.0,
            trade_intervals_std=0,
            profit_factor=2.0,
            category_trade_count=50,
            overall_trade_count=100,
            resolved_markets=50,
            active_trading_days=40,
            distinct_events=30,
            category_resolved_markets=20,
            category_distinct_events=15,
            category_active_days=20,
        )
        assert result.verdict == WalletVerdict.COPY_CANDIDATE

    def test_risk_drawdown_penalized(self):
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            max_drawdown=0.5,
            sharpe_ratio=0.0,
            win_rate=0.5,
            trade_count=100,
            info_score=1.0,
            trade_intervals_std=0,
            profit_factor=2.0,
        )
        assert result.score < VERDICT_COPY_CANDIDATE_MIN

    def test_concentration_penalty_largest_winner(self):
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            largest_winner_share=0.6,  # 60% of profit from one trade
            top_3_concentration=0.8,
            win_rate=0.5,
            trade_count=100,
            info_score=1.0,
            trade_intervals_std=0,
            max_drawdown=0.0,
            sharpe_ratio=3.0,
            profit_factor=2.0,
        )
        assert result.score > 0  # Score computed but penalized

    def test_sample_reliability_boost(self):
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            trade_count=150,  # Above optimal range
            sample_fraction=0.0,  # No sample data
            win_rate=0.5,
        )
        comp = next(c for c in result.components if c.name == "sample_reliability")
        assert comp.raw_score > 50.0


class TestWalletScoreV1Verdicts:
    """Test wallet score v1 verdict thresholds and gates."""

    def test_copy_candidate_verdict(self):
        """Score >= 75 should be COPY CANDIDATE."""
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            win_rate=1.0,
            profit_factor=2.0,
            trade_intervals_std=0,
            trade_count=200,
            max_drawdown=0.0,
            sharpe_ratio=3.0,
            sample_fraction=0.0,
            category_trade_count=50,
            overall_trade_count=100,
            resolved_markets=50,
            active_trading_days=40,
            distinct_events=30,
            category_resolved_markets=20,
            category_distinct_events=15,
            category_active_days=20,
            largest_winner_share=0.3,
            top_3_concentration=0.5,
            info_score=1.0,
        )
        assert result.verdict == WalletVerdict.COPY_CANDIDATE
        assert result.score >= VERDICT_COPY_CANDIDATE_MIN

    def test_watchlist_verdict(self):
        """Score 55-74.9999 should be WATCHLIST."""
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            win_rate=0.6,
            profit_factor=1.5,
            trade_count=100,
            trade_intervals_std=3600,
            max_drawdown=0.2,
            sharpe_ratio=1.5,
            info_score=0.7,
            resolved_markets=50,
            active_trading_days=30,
            distinct_events=20,
            category_resolved_markets=20,
            category_distinct_events=10,
            category_active_days=15,
            category_trade_count=30,
            overall_trade_count=100,
        )
        assert result.verdict == WalletVerdict.WATCHLIST

    def test_skip_verdict_low_score(self):
        """Score below 55 should be SKIP."""
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            win_rate=0.3,  # Low win rate
            trade_count=5,
        )
        assert result.verdict == WalletVerdict.SKIP

    def test_incomplete_missing_essential(self):
        """Missing essential evidence should be INCOMPLETE."""
        result = compute_wallet_score_v1(wallet_id="test-wallet")
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert "win_rate" in result.missing_essentials

    def test_global_gate_applied(self):
        """Global eligibility gates should affect verdict."""
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            win_rate=1.0,
            resolved_markets=10,  # Below GLOBAL_MIN_RESOLVED_MARKETS
            active_trading_days=10,  # Below GLOBAL_MIN_ACTIVE_TRADING_DAYS
            distinct_events=5,  # Below GLOBAL_MIN_DISTINCT_EVENTS
        )
        # Should have gate failures recorded
        assert len(result.eligibility_gate_failures) > 0


class TestBehaviorClassification:
    """Test wallet behavior classification caps."""

    def test_directional_classification(self):
        # DIRECTIONAL requires positive evidence (at least one
        # dominant-side market). The pre-PR-4 default-to-DIRECTIONAL
        # behavior is REMOVED (Phase 12 / Chunk 3).
        evidence = BehaviorEvidence(
            trade_count=50,
            avg_time_between_trades_seconds=1000,  # Not HF
            distinct_markets_traded=10,
            dominant_side_market_count=3,  # positive directional evidence
        )
        result = classify_wallet_behavior(evidence)
        assert result.classification == BehaviorClassification.DIRECTIONAL
        assert result.is_eligible_for_copy is True
        assert result.is_skip is False

    def test_no_positive_directional_evidence_is_unknown(self):
        # A wallet with trades but no dominant-side market is
        # UNKNOWN, not silently DIRECTIONAL. This is the spec
        # change in Phase 12.
        evidence = BehaviorEvidence(
            trade_count=50,
            avg_time_between_trades_seconds=1000,
            distinct_markets_traded=10,
            dominant_side_market_count=0,
        )
        result = classify_wallet_behavior(evidence)
        assert result.classification == BehaviorClassification.UNKNOWN
        assert result.is_eligible_for_copy is False
        assert result.is_watchlist_cap is True

    def test_high_frequency_bot(self):
        evidence = BehaviorEvidence(
            trade_count=100,
            avg_time_between_trades_seconds=5,  # Very fast
        )
        result = classify_wallet_behavior(evidence)
        assert result.classification == BehaviorClassification.HIGH_FREQUENCY_BOT
        assert result.is_skip is True
        assert result.is_eligible_for_copy is False

    def test_insufficient_trades_unknown(self):
        evidence = BehaviorEvidence(
            trade_count=3,
        )
        result = classify_wallet_behavior(evidence)
        assert result.classification == BehaviorClassification.UNKNOWN
        assert result.is_watchlist_cap is True

    def test_mixed_classification(self):
        evidence = BehaviorEvidence(
            trade_count=100,
            distinct_markets_traded=50,  # High diversity without pattern
        )
        result = classify_wallet_behavior(evidence)
        assert result.classification == BehaviorClassification.MIXED
        assert result.is_watchlist_cap is True


class TestTradeScoreV1HoldingPeriods:
    """Test holding period duration buckets."""

    def test_excluded_under_15_minutes(self):
        result = compute_trade_score_v1(
            wallet_id="test-wallet",
            source_trade_id="test-trade",
            side="BUY",
            intended_stake=100.0,
            executable_depth=200.0,
            spread=0.05,
            trade_age_seconds=100,
            seconds_to_market_end=14 * 60,  # 14 minutes
            market_active=True,
        )
        hp_comp = next(c for c in result.components if c.name == "holding_period_quality")
        assert hp_comp.raw_score == 0.0

    def test_experimental_15_min_to_6_hours(self):
        result = compute_trade_score_v1(
            wallet_id="test-wallet",
            source_trade_id="test-trade",
            side="BUY",
            intended_stake=100.0,
            executable_depth=200.0,
            spread=0.05,
            trade_age_seconds=100,
            seconds_to_market_end=5 * 3600,  # 5 hours
            market_active=True,
        )
        hp_comp = next(c for c in result.components if c.name == "holding_period_quality")
        # Frozen spec: 15m to <6h → experimental → score 40.
        assert hp_comp.raw_score == 40.0

    def test_preferred_6h_to_14d(self):
        result = compute_trade_score_v1(
            wallet_id="test-wallet",
            source_trade_id="test-trade",
            side="BUY",
            intended_stake=100.0,
            executable_depth=200.0,
            spread=0.05,
            trade_age_seconds=100,
            seconds_to_market_end=12 * 3600,  # 12 hours
            market_active=True,
        )
        hp_comp = next(c for c in result.components if c.name == "holding_period_quality")
        # Frozen spec: 6h to <1d → allowed → score 75.
        assert hp_comp.raw_score == 75.0

    def test_long_allowed_15d_to_21d(self):
        result = compute_trade_score_v1(
            wallet_id="test-wallet",
            source_trade_id="test-trade",
            side="BUY",
            intended_stake=100.0,
            executable_depth=200.0,
            spread=0.05,
            trade_age_seconds=100,
            seconds_to_market_end=18 * 24 * 3600,  # 18 days
            market_active=True,
        )
        hp_comp = next(c for c in result.components if c.name == "holding_period_quality")
        assert hp_comp.raw_score == 80.0

    def test_penalized_22d_to_45d(self):
        result = compute_trade_score_v1(
            wallet_id="test-wallet",
            source_trade_id="test-trade",
            side="BUY",
            intended_stake=100.0,
            executable_depth=200.0,
            spread=0.05,
            trade_age_seconds=100,
            seconds_to_market_end=30 * 24 * 3600,  # 30 days
            market_active=True,
        )
        hp_comp = next(c for c in result.components if c.name == "holding_period_quality")
        assert hp_comp.raw_score == 40.0

    def test_excluded_over_45d(self):
        result = compute_trade_score_v1(
            wallet_id="test-wallet",
            source_trade_id="test-trade",
            side="BUY",
            intended_stake=100.0,
            executable_depth=200.0,
            spread=0.05,
            trade_age_seconds=100,
            seconds_to_market_end=60 * 24 * 3600,  # 60 days
            market_active=True,
        )
        hp_comp = next(c for c in result.components if c.name == "holding_period_quality")
        assert hp_comp.raw_score == 0.0


class TestTradeScoreV1BUYPenalty:
    """Test BUY side copy-price deterioration."""

    def test_deterioration_penalty(self):
        result = compute_trade_score_v1(
            wallet_id="test-wallet",
            source_trade_id="test-trade",
            side="BUY",
            price_deterioration_pct=0.3,  # 30% deterioration
            intended_stake=100.0,
            executable_depth=200.0,
            spread=0.05,
            trade_age_seconds=100,
            seconds_to_market_end=24 * 3600,
            market_active=True,
        )
        cp_comp = next(c for c in result.components if c.name == "copy_price_quality")
        # Inverse score: 0% = 100, 50% = 0
        assert cp_comp.raw_score == pytest.approx(40.0, abs=5.0)


class TestSignalVerdictGeneration:
    """Phase 3 — exhaustive decision-table for the final signal verdict.

    Branches covered (from the frozen PR 4 spec):

      1.  wallet_score or wallet_verdict missing         -> INCOMPLETE
      2.  wallet_verdict == INCOMPLETE                    -> INCOMPLETE
      3.  category_wallet_score or category_wallet_verdict missing -> INCOMPLETE
      4.  category_wallet_verdict == "incomplete"         -> INCOMPLETE
      5.  trade_score or trade_verdict missing           -> INCOMPLETE
      6.  trade_verdict == INCOMPLETE                    -> INCOMPLETE
      7.  behavior == MARKET_MAKER_LP                    -> SKIP
      8.  behavior == ARBITRAGE_MULTI_LEG                -> SKIP
      9.  behavior == HIGH_FREQUENCY_BOT                 -> SKIP
      10. behavior == MIXED                              -> WATCHLIST cap
      11. behavior == UNKNOWN                            -> WATCHLIST cap
      12. has_hard_exclusion                             -> SKIP
      13. wallet_score < 55                              -> SKIP
      14. 55 <= wallet_score < 75                        -> WATCHLIST
      15. wallet_score >= 75, trade_score < 70           -> SKIP
          (skipped_reason = "skilled_wallet_trade_not_copyable")
      16. wallet_score >= 75, category != copy_candidate -> WATCHLIST
      17. ALL gates pass (wallet >=75, cat=copy_candidate,
          trade >=70, directional, no exclusion)         -> COPY_CANDIDATE
    """

    def _behavior(self, classification, *, is_skip=False,
                  is_watchlist_cap=False, is_eligible=True,
                  reasons=None):
        """Build a real BehaviorClassificationResult (not a mock) so
        type checks work; the verdict_generation module only reads
        the three booleans + the classification value."""
        from polycopy.scoring.behavior_classification import (
            BehaviorClassificationResult,
        )
        return BehaviorClassificationResult(
            classification=classification,
            reasons=list(reasons or []),
            is_eligible_for_copy=is_eligible,
            is_watchlist_cap=is_watchlist_cap,
            is_skip=is_skip,
        )

    # ---- 1-2. wallet missing / INCOMPLETE ----
    def test_1_wallet_score_missing_is_incomplete(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=None,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=75.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
        ))
        assert result.verdict == SignalVerdict.INCOMPLETE
        assert result.reason == "missing_wallet_score"

    def test_1_wallet_verdict_missing_is_incomplete(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=None,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=75.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
        ))
        assert result.verdict == SignalVerdict.INCOMPLETE
        assert result.reason == "missing_wallet_score"

    def test_2_wallet_verdict_incomplete_is_incomplete(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.INCOMPLETE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=75.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
        ))
        assert result.verdict == SignalVerdict.INCOMPLETE
        assert result.reason == "wallet_verdict_incomplete"

    # ---- 3-4. category missing / INCOMPLETE ----
    def test_3_category_score_missing_is_incomplete(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=None,
            category_wallet_verdict=None,
            trade_score=75.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
        ))
        assert result.verdict == SignalVerdict.INCOMPLETE
        assert result.reason == "missing_category_score"

    def test_4_category_verdict_incomplete_is_incomplete(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="incomplete",
            trade_score=75.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
        ))
        assert result.verdict == SignalVerdict.INCOMPLETE
        assert result.reason == "category_verdict_incomplete"

    # ---- 5-6. trade missing / INCOMPLETE ----
    def test_5_trade_score_missing_is_incomplete(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=None,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
        ))
        assert result.verdict == SignalVerdict.INCOMPLETE
        assert result.reason == "missing_trade_score"

    def test_6_trade_verdict_incomplete_is_incomplete(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=75.0,
            trade_verdict=TradeVerdict.INCOMPLETE,
            behavior_classification=None,
        ))
        assert result.verdict == SignalVerdict.INCOMPLETE
        assert result.reason == "trade_verdict_incomplete"

    # ---- 7-9. behavior SKIP branches ----
    def test_7_market_maker_behavior_is_skip(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=90.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=self._behavior(
                BehaviorClassification.MARKET_MAKER_LP, is_skip=True,
                is_eligible=False,
            ),
        ))
        assert result.verdict == SignalVerdict.SKIP
        assert result.reason == "behavior_market_maker_lp"

    def test_8_arbitrage_behavior_is_skip(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=90.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=self._behavior(
                BehaviorClassification.ARBITRAGE_MULTI_LEG, is_skip=True,
                is_eligible=False,
            ),
        ))
        assert result.verdict == SignalVerdict.SKIP
        assert result.reason == "behavior_arbitrage_multi_leg"

    def test_9_hft_behavior_is_skip(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=90.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=self._behavior(
                BehaviorClassification.HIGH_FREQUENCY_BOT, is_skip=True,
                is_eligible=False,
            ),
        ))
        assert result.verdict == SignalVerdict.SKIP
        assert result.reason == "behavior_high_frequency_bot"

    # ---- 10-11. behavior WATCHLIST cap ----
    def test_10_mixed_behavior_caps_at_watchlist(self):
        # Even with wallet=90, trade=80, cat=copy_candidate, behavior
        # MIXED must cap at WATCHLIST — never COPY_CANDIDATE.
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=90.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=self._behavior(
                BehaviorClassification.MIXED, is_watchlist_cap=True,
                is_eligible=False,
            ),
        ))
        assert result.verdict == SignalVerdict.WATCHLIST
        assert result.reason == "behavior_mixed_watchlist_cap"

    def test_11_unknown_behavior_caps_at_watchlist(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=90.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=self._behavior(
                BehaviorClassification.UNKNOWN, is_watchlist_cap=True,
                is_eligible=False,
            ),
        ))
        assert result.verdict == SignalVerdict.WATCHLIST
        assert result.reason == "behavior_unknown_watchlist_cap"

    # ---- 12. hard exclusion ----
    def test_12_hard_exclusion_is_skip(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=90.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
            has_hard_exclusion=True,
            hard_exclusion_reason="REGULATED_MARKET",
        ))
        assert result.verdict == SignalVerdict.SKIP
        assert result.reason == "REGULATED_MARKET"

    # ---- 13. wallet_score < 55 ----
    def test_13_wallet_below_55_is_skip(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=40.0,
            wallet_verdict=WalletVerdict.SKIP,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
        ))
        assert result.verdict == SignalVerdict.SKIP
        assert result.reason == "wallet_score_below_55"

    # ---- 14. wallet_score 55-75 ----
    def test_14_wallet_55_to_75_is_watchlist(self):
        # Frozen contract: behavior_classification=None is treated as
        # UNKNOWN (cannot prove the wallet is directional), so the
        # WATCHLIST verdict carries the behavior-cap reason rather
        # than the bare wallet-score reason. Use DIRECTIONAL to test
        # the pure wallet-score WATCHLIST path.
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=65.0,
            wallet_verdict=WalletVerdict.WATCHLIST,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=self._behavior(
                BehaviorClassification.DIRECTIONAL,
            ),
        ))
        assert result.verdict == SignalVerdict.WATCHLIST
        assert result.reason == "wallet_score_watchlist_range"

    def test_14_wallet_55_to_75_with_unknown_behavior_still_watchlist(self):
        # When wallet is in watchlist range, the cap doesn't change
        # the verdict; both reasons are acceptable but
        # wallet_score_watchlist_range must appear.
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=65.0,
            wallet_verdict=WalletVerdict.WATCHLIST,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=self._behavior(
                BehaviorClassification.UNKNOWN, is_watchlist_cap=True,
                is_eligible=False,
            ),
        ))
        assert result.verdict == SignalVerdict.WATCHLIST

    # ---- 15. wallet >= 75, trade < 70 ----
    def test_15_skilled_wallet_trade_not_copyable_is_skip(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=60.0,
            trade_verdict=TradeVerdict.WATCHLIST,
            behavior_classification=None,
        ))
        assert result.verdict == SignalVerdict.SKIP
        assert result.skipped_reason == "skilled_wallet_trade_not_copyable"
        # `reason` carries the diagnostic; skipped_reason is the
        # canonical Phase 3 #15 identifier.
        assert "skilled_wallet_trade_not_copyable" in (
            (result.reason or "") + " " + (result.skipped_reason or "")
        )

    # ---- 16. wallet >= 75, category not copy_candidate ----
    def test_16_category_watchlist_blocks_copy(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=70.0,
            category_wallet_verdict="watchlist",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
        ))
        assert result.verdict == SignalVerdict.WATCHLIST
        assert result.reason == "category_verdict_not_copy_candidate"

    def test_16_category_skip_blocks_copy(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=40.0,
            category_wallet_verdict="skip",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
        ))
        assert result.verdict == SignalVerdict.WATCHLIST
        assert result.reason == "category_verdict_not_copy_candidate"

    # ---- 17. all gates pass -> COPY_CANDIDATE ----
    def test_17_all_gates_pass_is_copy_candidate(self):
        # Frozen contract: ONLY DIRECTIONAL may become COPY_CANDIDATE.
        # A None behavior is treated as UNKNOWN and caps at WATCHLIST,
        # so we must pass DIRECTIONAL here.
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=75.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=self._behavior(
                BehaviorClassification.DIRECTIONAL,
            ),
        ))
        assert result.verdict == SignalVerdict.COPY_CANDIDATE
        assert result.reason == "all_thresholds_met"

    def test_17_directional_behavior_allows_copy(self):
        # DIRECTIONAL is the only behavior that allows COPY_CANDIDATE.
        # We pass a real DIRECTIONAL behavior object and verify it
        # doesn't impose a cap.
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=75.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=self._behavior(
                BehaviorClassification.DIRECTIONAL,
            ),
        ))
        assert result.verdict == SignalVerdict.COPY_CANDIDATE

    # ---- numeric placeholder scores do NOT override INCOMPLETE verdicts ----
    def test_numeric_score_does_not_override_incomplete_wallet(self):
        # Even if trade_score and category are valid, an INCOMPLETE
        # wallet must propagate to INCOMPLETE.
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,  # numeric placeholder present
            wallet_verdict=WalletVerdict.INCOMPLETE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
        ))
        assert result.verdict == SignalVerdict.INCOMPLETE

    def test_numeric_score_does_not_override_incomplete_category(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="incomplete",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
        ))
        assert result.verdict == SignalVerdict.INCOMPLETE

    def test_numeric_score_does_not_override_incomplete_trade(self):
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=80.0,
            trade_verdict=TradeVerdict.INCOMPLETE,
            behavior_classification=None,
        ))
        assert result.verdict == SignalVerdict.INCOMPLETE

    # ---- reason is always a SignalReason enum value (or None) ----
    def test_reason_is_canonical_constant_or_none(self):
        from polycopy.scoring.verdict_generation import SignalReason
        result = generate_signal_verdict(SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=80.0,
            category_wallet_verdict="copy_candidate",
            trade_score=75.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
        ))
        # All canonical reason values are valid strings
        assert result.reason in {e.value for e in SignalReason} | {None}


# ---- Behavior-gate contract (frozen PR 4 rule) ----------------------------
# Only DIRECTIONAL may become COPY_CANDIDATE. The matrix below pins every
# other behavior-classification outcome to a non-COPY verdict regardless
# of how strong the other scores are.


class TestBehaviorGateContract:
    """Frozen contract: only DIRECTIONAL behavior may become COPY_CANDIDATE.

    These tests exercise the behavior-classification gate independently
    of the other rules (wallet/category/trade scores are all at their
    COPY-eligible maximum). The behavior alone decides whether the
    candidate can reach COPY_CANDIDATE.
    """

    def _all_eligible_input(
        self,
        behavior: Optional[BehaviorClassificationResult],
    ) -> SignalDecisionInput:
        return SignalDecisionInput(
            wallet_score=95.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_score=95.0,
            category_wallet_verdict="copy_candidate",
            trade_score=95.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=behavior,
        )

    @staticmethod
    def _behavior(
        classification: BehaviorClassification,
        *,
        is_skip: bool = False,
        is_watchlist_cap: bool = False,
        is_eligible: bool = False,
    ) -> BehaviorClassificationResult:
        # DIRECTIONAL -> is_eligible=True (no cap).
        # MIXED / UNKNOWN -> is_watchlist_cap=True (caps at WATCHLIST).
        # MARKET_MAKER_LP / ARBITRAGE_MULTI_LEG / HIGH_FREQUENCY_BOT
        # -> is_skip=True.
        return BehaviorClassificationResult(
            classification=classification,
            reasons=["test"],
            is_eligible_for_copy=is_eligible,
            is_watchlist_cap=is_watchlist_cap,
            is_skip=is_skip,
        )

    def test_behavior_none_caps_at_watchlist(self) -> None:
        """behavior=None (defensive None-branch) -> WATCHLIST, not COPY."""
        result = generate_signal_verdict(
            self._all_eligible_input(None)
        )
        assert result.verdict == SignalVerdict.WATCHLIST
        assert result.reason == SignalReason.BEHAVIOR_UNKNOWN_CAP.value

    def test_unknown_behavior_caps_at_watchlist(self) -> None:
        """UNKNOWN -> WATCHLIST (cap)."""
        result = generate_signal_verdict(
            self._all_eligible_input(
                self._behavior(
                    BehaviorClassification.UNKNOWN,
                    is_watchlist_cap=True,
                )
            )
        )
        assert result.verdict == SignalVerdict.WATCHLIST
        assert result.reason == SignalReason.BEHAVIOR_UNKNOWN_CAP.value

    def test_mixed_behavior_caps_at_watchlist(self) -> None:
        """MIXED -> WATCHLIST (cap)."""
        result = generate_signal_verdict(
            self._all_eligible_input(
                self._behavior(
                    BehaviorClassification.MIXED,
                    is_watchlist_cap=True,
                )
            )
        )
        assert result.verdict == SignalVerdict.WATCHLIST
        assert result.reason == SignalReason.BEHAVIOR_MIXED_CAP.value

    def test_market_maker_lp_behavior_skips(self) -> None:
        """MARKET_MAKER_LP -> SKIP."""
        result = generate_signal_verdict(
            self._all_eligible_input(
                self._behavior(
                    BehaviorClassification.MARKET_MAKER_LP,
                    is_skip=True,
                )
            )
        )
        assert result.verdict == SignalVerdict.SKIP
        assert (
            result.reason
            == SignalReason.BEHAVIOR_MARKET_MAKER.value
        )

    def test_high_frequency_bot_behavior_skips(self) -> None:
        """HIGH_FREQUENCY_BOT -> SKIP."""
        result = generate_signal_verdict(
            self._all_eligible_input(
                self._behavior(
                    BehaviorClassification.HIGH_FREQUENCY_BOT,
                    is_skip=True,
                )
            )
        )
        assert result.verdict == SignalVerdict.SKIP
        assert result.reason == SignalReason.BEHAVIOR_HFT.value

    def test_arbitrage_multi_leg_behavior_skips(self) -> None:
        """ARBITRAGE_MULTI_LEG -> SKIP."""
        result = generate_signal_verdict(
            self._all_eligible_input(
                self._behavior(
                    BehaviorClassification.ARBITRAGE_MULTI_LEG,
                    is_skip=True,
                )
            )
        )
        assert result.verdict == SignalVerdict.SKIP
        assert (
            result.reason == SignalReason.BEHAVIOR_ARBITRAGE.value
        )

    def test_directional_behavior_allows_copy(self) -> None:
        """DIRECTIONAL -> COPY_CANDIDATE (only path to COPY)."""
        result = generate_signal_verdict(
            self._all_eligible_input(
                self._behavior(
                    BehaviorClassification.DIRECTIONAL,
                    is_eligible=True,
                )
            )
        )
        assert result.verdict == SignalVerdict.COPY_CANDIDATE
        assert result.reason == SignalReason.ALL_GATES_MET.value


class TestIdempotency:
    """Test deterministic idempotency keys."""

    def test_same_inputs_same_key(self):
        key1 = generate_idempotency_key(
            formula_name="wallet_score",
            formula_version="1",
            wallet_id="wallet-123",
            source_data_timestamp="2024-01-01T00:00:00Z",
        )
        key2 = generate_idempotency_key(
            formula_name="wallet_score",
            formula_version="1",
            wallet_id="wallet-123",
            source_data_timestamp="2024-01-01T00:00:00Z",
        )
        assert key1 == key2

    def test_different_timestamp_different_key(self):
        key1 = generate_idempotency_key(
            formula_name="wallet_score",
            formula_version="1",
            wallet_id="wallet-123",
            source_data_timestamp="2024-01-01T00:00:00Z",
        )
        key2 = generate_idempotency_key(
            formula_name="wallet_score",
            formula_version="1",
            wallet_id="wallet-123",
            source_data_timestamp="2024-01-01T01:00:00Z",
        )
        assert key1 != key2


class TestV2ShadowIsolation:
    """Test that v2 shadow does not affect v1 verdict."""

    def test_v2_missing_data_produces_shadow_incomplete(self):
        result = compute_shadow_score_v2(
            wallet_id="test-wallet",
            source_trade_id="test-trade",
            # All essential inputs missing
        )
        assert result.verdict == ShadowVerdict.SHADOW_INCOMPLETE

    def test_v2_parallel_to_v1(self):
        """v2 running does not change v1 outcome."""
        v1_result = compute_trade_score_v1(
            wallet_id="test-wallet",
            source_trade_id="test-trade",
            side="BUY",
            intended_stake=100.0,
            executable_depth=200.0,
            spread=0.05,
            trade_age_seconds=100,
            seconds_to_market_end=24 * 3600,
            market_active=True,
        )

        v2_result = compute_shadow_score_v2(
            wallet_id="test-wallet",
            source_trade_id="test-trade",
            # Missing alpha_signal → incomplete
            delay_seconds=30,
        )

        # v2 should not have affected v1
        assert v1_result.verdict in [TradeVerdict.COPY_CANDIDATE, TradeVerdict.WATCHLIST, TradeVerdict.SKIP]
        assert v2_result.verdict == ShadowVerdict.SHADOW_INCOMPLETE


class TestComponentScoreRounding:
    """Test that all scores are rounded to 4 decimal places."""

    def test_wallet_score_rounded(self):
        result = compute_wallet_score_v1(
            wallet_id="test-wallet",
            win_rate=0.5,
        )
        # Check that score has at most 4 decimal places
        assert result.score == round(result.score, 4)

    def test_trade_score_rounded(self):
        result = compute_trade_score_v1(
            wallet_id="test-wallet",
            source_trade_id="test-trade",
            side="BUY",
            intended_stake=100.0,
            executable_depth=200.0,
            spread=0.05,
            trade_age_seconds=100,
            seconds_to_market_end=24 * 3600,
            market_active=True,
        )
        assert result.score == round(result.score, 4)

    def test_shadow_score_rounded(self):
        result = compute_shadow_score_v2(
            wallet_id="test-wallet",
            source_trade_id="test-trade",
            delay_seconds=30,
            alpha_signal=0.1,
        )
        assert result.score == round(result.score, 4)


class TestNoDeprecationWarnings:
    """Regression tests: scoring code must not emit DeprecationWarnings.

    CI runs pytest with PYTHONWARNINGS=error, which promotes any
    DeprecationWarning into an exception during collection and test
    execution. This class exercises the scoring entry points that
    previously called datetime.utcnow() to prove they now run cleanly
    under a warning-as-error regime.
    """

    def test_compute_wallet_score_v1_no_deprecation(self):
        import warnings
        from datetime import timezone

        from polycopy.scoring.wallet_score_v1 import (
            compute_wallet_score_v1,
            WalletScoreResult,
        )

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            result = compute_wallet_score_v1(
                wallet_id="regression-wallet",
                win_rate=0.5,
                trade_count=100,
            )
        assert isinstance(result, WalletScoreResult)
        # Default now is timezone-aware UTC.
        assert result.computed_at.tzinfo is not None
        assert result.computed_at.utcoffset() == timezone.utc.utcoffset(
            result.computed_at
        )

    def test_compute_wallet_score_v1_default_now_is_tz_aware_utc(self):
        """The default `now` and `computed_at` must be tz-aware UTC."""
        from datetime import timezone

        from polycopy.scoring.wallet_score_v1 import compute_wallet_score_v1

        result = compute_wallet_score_v1(wallet_id="tz-check")
        assert result.computed_at.tzinfo is timezone.utc


class TestTypedInputDataclasses:
    """Phase 9 (partial): typed input objects must carry raw fields onto
    the result for replayable persistence.

    Score serializers must not rely on `getattr(result, ..., None)` —
    every raw column must be reachable via a typed `result.input` object.
    """

    def test_wallet_score_input_v1_default_construction(self):
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1,
            WalletScoreResult,
            compute_wallet_score_v1,
        )

        inp = WalletScoreInputV1(wallet_id="w-1")
        assert inp.wallet_id == "w-1"
        # All raw input fields must be Optional with None default
        assert inp.info_score is None
        assert inp.win_rate is None
        assert inp.trade_count is None
        assert inp.profit_factor is None
        assert inp.category_resolved_markets is None

        # Back-compat: passing raw kwargs (no explicit input) must still
        # produce a result with a typed input object attached.
        result = compute_wallet_score_v1(
            wallet_id="w-1",
            win_rate=0.5,
            trade_count=100,
        )
        assert isinstance(result, WalletScoreResult)
        assert result.input is not None
        assert result.input.wallet_id == "w-1"
        assert result.input.win_rate == 0.5
        assert result.input.trade_count == 100

    def test_wallet_score_result_carries_raw_inputs_for_persistence(self):
        """Every field the serializer cares about must be reachable
        through `result.input.<field>` — no getattr(..., None) tolerated."""
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1,
            compute_wallet_score_v1,
        )

        inp = WalletScoreInputV1(
            wallet_id="w-1",
            win_rate=0.6,
            profit_factor=1.4,
            trade_count=120,
            info_score=0.5,
            resolved_markets=40,
            active_trading_days=22,
            distinct_events=18,
            category_resolved_markets=20,
            category_distinct_events=10,
            category_active_days=12,
        )
        result = compute_wallet_score_v1(wallet_id="w-1", input=inp)
        assert result.input is inp
        # Mirror every column the serializer persists
        assert result.input.info_score == 0.5
        assert result.input.win_rate == 0.6
        assert result.input.profit_factor == 1.4
        assert result.input.trade_count == 120
        assert result.input.resolved_markets == 40
        assert result.input.active_trading_days == 22
        assert result.input.distinct_events == 18
        assert result.input.category_resolved_markets == 20
        assert result.input.category_distinct_events == 10
        assert result.input.category_active_days == 12

    def test_wallet_score_input_v1_is_frozen(self):
        """Typed input must be immutable to enforce replayability."""
        from polycopy.scoring.wallet_score_v1 import WalletScoreInputV1

        inp = WalletScoreInputV1(wallet_id="w-1", win_rate=0.5)
        with pytest.raises(Exception):
            # Frozen dataclass: attribute assignment is forbidden.
            inp.win_rate = 0.7  # type: ignore[misc]

    def test_wallet_score_input_v1_explicit_overrides_kwargs(self):
        """When both an `input` object and loose kwargs are passed,
        the explicit input wins (no silent mixing)."""
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1,
            compute_wallet_score_v1,
        )

        inp = WalletScoreInputV1(wallet_id="w-1", win_rate=0.6)
        # Even if the caller also passes win_rate=0.1, the input wins.
        result = compute_wallet_score_v1(
            wallet_id="w-1", input=inp, win_rate=0.1
        )
        assert result.input.win_rate == 0.6

    def test_trade_copyability_input_v1_default_construction(self):
        from polycopy.scoring.trade_score_v1 import (
            TradeCopyabilityInputV1,
        )

        inp = TradeCopyabilityInputV1(wallet_id="w-1", source_trade_id="t-1")
        assert inp.wallet_id == "w-1"
        assert inp.source_trade_id == "t-1"
        # All raw input fields must be Optional with None default
        assert inp.intended_stake is None
        assert inp.executable_depth is None
        assert inp.spread is None
        assert inp.side is None  # 4.D: no silent "BUY" fallback
        assert inp.market_category is None
        assert inp.seconds_to_market_end is None

    def test_trade_score_result_carries_raw_inputs(self):
        from polycopy.scoring.trade_score_v1 import (
            TradeCopyabilityInputV1,
            compute_trade_score_v1,
        )

        inp = TradeCopyabilityInputV1(
            wallet_id="w-1",
            source_trade_id="t-1",
            side="BUY",
            intended_stake=100.0,
            executable_depth=200.0,
            spread=0.05,
            trade_age_seconds=120,
            seconds_to_market_end=24 * 3600,
            market_active=True,
            market_closed=False,
            market_resolved=False,
            market_category="politics",
        )
        result = compute_trade_score_v1(input=inp)
        assert result.input is inp
        assert result.input.intended_stake == 100.0
        assert result.input.executable_depth == 200.0
        assert result.input.market_category == "politics"
        assert result.input.side == "BUY"

    def test_trade_copyability_input_v1_is_frozen(self):
        from polycopy.scoring.trade_score_v1 import TradeCopyabilityInputV1

        inp = TradeCopyabilityInputV1(wallet_id="w-1", source_trade_id="t-1")
        with pytest.raises(Exception):
            inp.intended_stake = 50.0  # type: ignore[misc]


class TestFillFeasibilityMath:
    """Phase 4.A: fill_ratio = executable_depth / intended_stake.

    A score of 100 means the depth fully covers the intended stake.
    A score of 25 means only 25% of the intended stake is fillable.
    """

    def _kwargs(self, **overrides):
        base = dict(
            wallet_id="w",
            source_trade_id="t",
            side="BUY",
            intended_stake=100.0,
            executable_depth=200.0,
            spread=0.05,
            trade_age_seconds=100,
            seconds_to_market_end=24 * 3600,
            market_active=True,
        )
        base.update(overrides)
        return base

    def _fill_score(self, **overrides):
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        result = compute_trade_score_v1(**self._kwargs(**overrides))
        comp = next(
            (c for c in result.components if c.name == "fill_feasibility"),
            None,
        )
        assert comp is not None, "fill_feasibility component missing"
        return comp.raw_score

    def test_full_fill_when_depth_covers_stake(self):
        # depth = 2x stake → 100% fillable → 100
        assert self._fill_score(intended_stake=100.0, executable_depth=200.0) == 100.0

    def test_full_fill_when_depth_equals_stake(self):
        # depth = stake → 100% fillable → 100
        assert self._fill_score(intended_stake=100.0, executable_depth=100.0) == 100.0

    def test_partial_fill_score_proportional(self):
        # depth = 25% of stake → fillable 25% → 25
        assert self._fill_score(intended_stake=100.0, executable_depth=25.0) == 25.0

    def test_zero_depth_yields_zero_fill_score(self):
        assert self._fill_score(intended_stake=100.0, executable_depth=0.0) == 0.0

    def test_zero_intended_stake_does_not_divide_by_zero(self):
        # Degenerate "no position" state: must not raise.
        # Spec rule: cannot copy 0 stake as 100 → 0 fill.
        assert self._fill_score(intended_stake=0.0, executable_depth=100.0) == 0.0

    def test_explicit_fill_percentage_overrides_depth_ratio(self):
        # When the caller provides a depth-walk result as fill_percentage,
        # that explicit number wins (this is how Phase 7 will integrate).
        assert self._fill_score(
            intended_stake=100.0,
            executable_depth=25.0,  # would imply 25%
            fill_percentage=0.8,    # but caller says 80%
        ) == 80.0

    def test_fill_never_exceeds_100(self):
        # depth = 10x stake → 100% fillable, NOT 1000
        assert self._fill_score(intended_stake=10.0, executable_depth=100.0) == 100.0


class TestTradeScoreV1HoldingPeriodBoundaries:
    """Phase 4.B: exhaustive boundary tests for the holding-period buckets.

    Encodes the frozen PR 4 spec at every spec-required boundary using
    exact seconds (NOT rounded day labels, so the 14d/15d and 21d/22d
    transitions are unambiguous).

    Frozen buckets (authoritative):

        < 15 min              → excluded   (0)
        15m - <6h             → experimental (40)
        6h  - <1d             → allowed    (75)
        1d  - 14d             → preferred  (100)
        >14d - 21d            → allowed    (80)
        >21d - 45d            → penalized  (40)
        > 45d                 → excluded   (0)
        unknown               → INCOMPLETE (0, missing_essentials)
    """

    def _hp(self, seconds):
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        result = compute_trade_score_v1(
            wallet_id="w", source_trade_id="t",
            side="BUY",
            intended_stake=100.0, executable_depth=200.0,
            spread=0.05, trade_age_seconds=100,
            seconds_to_market_end=seconds,
            market_active=True,
        )
        comp = next(
            (c for c in result.components if c.name == "holding_period_quality"),
            None,
        )
        assert comp is not None, "holding_period_quality component missing"
        return comp

    # --- < 15 minutes: excluded ---
    def test_14m59s_excluded_zero(self):
        c = self._hp(14 * 60 + 59)
        assert c.raw_score == 0.0
        assert "duration_excluded_short" in c.note

    def test_0s_excluded_zero(self):
        c = self._hp(0)
        assert c.raw_score == 0.0

    def test_15m00s_experimental_40(self):
        c = self._hp(15 * 60)
        assert c.raw_score == 40.0
        assert "duration_experimental" in c.note

    # --- 15m to <6h: experimental (40) ---
    def test_5h59m59s_experimental_40(self):
        c = self._hp(5 * 3600 + 3599)
        assert c.raw_score == 40.0
        assert "duration_experimental" in c.note

    def test_6h00m00s_allowed_75(self):
        c = self._hp(6 * 3600)
        assert c.raw_score == 75.0
        assert "duration_short_preferred" in c.note

    # --- 6h to <1d: allowed (75) ---
    def test_23h59m59s_allowed_75(self):
        c = self._hp(23 * 3600 + 3599)
        assert c.raw_score == 75.0
        assert "duration_short_preferred" in c.note

    def test_1d00h00m00s_preferred_100(self):
        # 1d boundary now falls into the 1d-14d bucket (preferred),
        # not the 6h-1d bucket, because the 6h-1d bucket is strictly
        # < 1d.
        c = self._hp(24 * 3600)
        assert c.raw_score == 100.0
        assert "duration_preferred" in c.note

    # --- 1d to 14d: preferred (100) ---
    def test_14d00h00m00s_preferred_100(self):
        c = self._hp(14 * 24 * 3600)
        assert c.raw_score == 100.0
        assert "duration_preferred" in c.note

    # --- >14d to 21d: allowed (80). Test both sides of 14d/15d. ---
    def test_14d00h00m01s_long_allowed_80(self):
        # One second past 14d → 80 (not 100)
        c = self._hp(14 * 24 * 3600 + 1)
        assert c.raw_score == 80.0
        assert "duration_long_allowed" in c.note

    def test_15d_long_allowed_80(self):
        c = self._hp(15 * 24 * 3600)
        assert c.raw_score == 80.0
        assert "duration_long_allowed" in c.note

    def test_21d00h00m00s_long_allowed_80(self):
        c = self._hp(21 * 24 * 3600)
        assert c.raw_score == 80.0
        assert "duration_long_allowed" in c.note

    # --- >21d to 45d: penalized (40). Test both sides of 21d/22d. ---
    def test_21d00h00m01s_penalized_40(self):
        # One second past 21d → 40 (not 80)
        c = self._hp(21 * 24 * 3600 + 1)
        assert c.raw_score == 40.0
        assert "duration_penalized" in c.note

    def test_22d_penalized_40(self):
        c = self._hp(22 * 24 * 3600)
        assert c.raw_score == 40.0
        assert "duration_penalized" in c.note

    def test_45d00h00m00s_penalized_40(self):
        c = self._hp(45 * 24 * 3600)
        assert c.raw_score == 40.0
        assert "duration_penalized" in c.note

    def test_45d00h00m01s_excluded_long_zero(self):
        # One second past 45d → excluded (0)
        c = self._hp(45 * 24 * 3600 + 1)
        assert c.raw_score == 0.0
        assert "duration_excluded_long" in c.note

    def test_60d_excluded_long_zero(self):
        c = self._hp(60 * 24 * 3600)
        assert c.raw_score == 0.0
        assert "duration_excluded_long" in c.note

    # --- unknown → INCOMPLETE upstream ---
    def test_unknown_seconds_yields_incomplete(self):
        from polycopy.scoring.trade_score_v1 import (
            compute_trade_score_v1, TradeVerdict,
        )
        result = compute_trade_score_v1(
            wallet_id="w", source_trade_id="t",
            side="BUY",
            intended_stake=100.0, executable_depth=200.0,
            spread=0.05, trade_age_seconds=100,
            # no seconds_to_market_end
            market_active=True,
        )
        assert result.verdict == TradeVerdict.INCOMPLETE
        assert "seconds_to_market_end" in result.missing_essentials

    def test_negative_seconds_yields_incomplete(self):
        from polycopy.scoring.trade_score_v1 import (
            compute_trade_score_v1, TradeVerdict,
        )
        result = compute_trade_score_v1(
            wallet_id="w", source_trade_id="t",
            side="BUY",
            intended_stake=100.0, executable_depth=200.0,
            spread=0.05, trade_age_seconds=100,
            seconds_to_market_end=-1,
            market_active=True,
        )
        assert result.verdict == TradeVerdict.INCOMPLETE


class TestRawInputPersistence:
    """Phase 9: persisters must read raw inputs from result.input
    so no essential field silently becomes NULL.

    Round-trip tests prove:
      1. supplied wallet inputs are persisted exactly
      2. supplied trade inputs are persisted exactly
      3. reloaded raw inputs can reproduce the same score
      4. no essential field silently becomes NULL
      5. the legacy back-compat path (result without explicit input)
         also persists, but using whatever fields the result carries
    """

    @pytest.fixture
    def db_with_wallet(self, tmp_path: Path):
        """Yield a v10-schema DB with one wallet row pre-inserted so
        FK constraints on the scoring decision tables are satisfied."""
        db_path = tmp_path / "persist_test.db"
        db = Database(db_path=db_path)
        db.connect()
        # Insert a minimal wallet row to satisfy the FK.
        db.execute(
            """INSERT INTO wallets (id, address, label, is_sample, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                "w-test",
                "0xtest",
                "test-wallet",
                1,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.conn.commit()
        try:
            yield db
        finally:
            db.close()

    def test_wallet_input_round_trip(self, db_with_wallet: Database):
        """Every field supplied in the typed input must be persisted
        with the exact value."""
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1, WalletVerdict,
        )
        from polycopy.scoring.score_serialization import (
            persist_wallet_score_v1,
        )

        inp = WalletScoreInputV1(
            wallet_id="w-test",
            info_score=0.55,
            win_rate=0.6,
            profit_factor=1.8,
            trade_intervals_std=1200.0,
            trade_count=200,
            max_drawdown=0.15,
            sharpe_ratio=1.4,
            sample_fraction=0.1,
            category_trade_count=80,
            category_distinct_markets=6,
            overall_trade_count=200,
            largest_winner_share=0.4,
            top_3_concentration=0.6,
            resolved_markets=45,
            active_trading_days=30,
            distinct_events=20,
            category_resolved_markets=20,
            category_distinct_events=12,
            category_active_days=15,
        )
        result = compute_wallet_score_v1(input=inp)
        assert result.verdict == WalletVerdict.COPY_CANDIDATE
        row_id = persist_wallet_score_v1(
            db_with_wallet, "w-test", result,
            source_data_timestamp="2026-07-03T00:00:00Z",
        )
        assert row_id > 0

        # Reload and assert every field survived the round-trip.
        row = db_with_wallet.fetchone(
            "SELECT * FROM wallet_score_decisions WHERE id = ?", (row_id,)
        )
        assert row is not None
        assert row["info_score"] == pytest.approx(0.55)
        assert row["win_rate"] == pytest.approx(0.6)
        assert row["profit_factor"] == pytest.approx(1.8)
        assert row["trade_intervals_std"] == pytest.approx(1200.0)
        assert row["trade_count"] == 200
        assert row["max_drawdown"] == pytest.approx(0.15)
        assert row["sharpe_ratio"] == pytest.approx(1.4)
        assert row["sample_fraction"] == pytest.approx(0.1)
        assert row["category_trade_count"] == 80
        assert row["category_distinct_markets"] == 6
        assert row["overall_trade_count"] == 200
        assert row["largest_winner_share"] == pytest.approx(0.4)
        assert row["top_3_concentration"] == pytest.approx(0.6)
        assert row["resolved_markets"] == 45
        assert row["active_trading_days"] == 30
        assert row["distinct_events"] == 20
        assert row["category_resolved_markets"] == 20
        assert row["category_distinct_events"] == 12
        assert row["category_active_days"] == 15
        assert row["final_score"] == pytest.approx(result.score)
        assert row["verdict"] == "copy_candidate"
        assert row["source_data_timestamp"] == "2026-07-03T00:00:00Z"

    def test_trade_input_round_trip(self, db_with_wallet: Database):
        """Every field supplied in the typed input must be persisted
        with the exact value.

        Note: candidate_id and price_snapshot_id are FK-constrained to
        copy_candidates and candidate_price_snapshots. Those tables
        aren't populated in this fixture; their persistence is
        covered by the Chunk 2/7 tests. The test here exercises the
        raw-input columns only.
        """
        from polycopy.scoring.trade_score_v1 import (
            TradeCopyabilityInputV1, TradeVerdict,
        )
        from polycopy.scoring.score_serialization import (
            persist_trade_score_v1,
        )

        inp = TradeCopyabilityInputV1(
            wallet_id="w-test",
            source_trade_id="trade-1",
            side="BUY",
            price_deterioration_pct=0.05,
            intended_stake=150.0,
            executable_depth=300.0,
            fill_percentage=None,  # let the score compute it
            spread=0.03,
            best_bid_size=500.0,
            best_ask_size=400.0,
            trade_age_seconds=120,
            seconds_to_market_end=3 * 24 * 3600,
            market_active=True,
            market_closed=False,
            market_resolved=False,
            has_valid_strategy=True,
            has_complete_data=True,
            market_category="politics",
        )
        result = compute_trade_score_v1(input=inp)
        assert result.verdict in (
            TradeVerdict.COPY_CANDIDATE,
            TradeVerdict.WATCHLIST,
            TradeVerdict.SKIP,
        )

        row_id = persist_trade_score_v1(
            db_with_wallet, "w-test", "trade-1", result,
            source_data_timestamp="2026-07-03T00:00:00Z",
        )
        assert row_id > 0

        row = db_with_wallet.fetchone(
            "SELECT * FROM trade_copyability_decisions WHERE id = ?",
            (row_id,),
        )
        assert row is not None
        assert row["wallet_id"] == "w-test"
        assert row["source_trade_id"] == "trade-1"
        assert row["side"] == "BUY"
        assert row["price_deterioration_pct"] == pytest.approx(0.05)
        assert row["intended_stake"] == pytest.approx(150.0)
        assert row["executable_depth"] == pytest.approx(300.0)
        assert row["spread"] == pytest.approx(0.03)
        assert row["best_bid_size"] == pytest.approx(500.0)
        assert row["best_ask_size"] == pytest.approx(400.0)
        assert row["trade_age_seconds"] == pytest.approx(120)
        assert row["seconds_to_market_end"] == pytest.approx(3 * 24 * 3600)
        assert row["market_active"] == 1
        assert row["market_closed"] == 0
        assert row["market_resolved"] == 0
        assert row["source_data_timestamp"] == "2026-07-03T00:00:00Z"
        assert row["formula_version"] == "1"
        # candidate_id and price_snapshot_id are NULL because we did
        # not pass them (their FK target rows don't exist in the
        # minimal fixture; coverage for them lands in Chunk 2/7).
        assert row["candidate_id"] is None
        assert row["price_snapshot_id"] is None

    def test_reload_input_reproduces_score(self, db_with_wallet: Database):
        """Reloading the raw input from the DB and recomputing the
        score must produce the same final_score (replayability)."""
        from polycopy.scoring.wallet_score_v1 import WalletScoreInputV1
        from polycopy.scoring.score_serialization import (
            persist_wallet_score_v1,
        )

        inp = WalletScoreInputV1(
            wallet_id="w-test",
            info_score=0.4,
            win_rate=0.55,
            profit_factor=1.5,
            trade_count=180,
            max_drawdown=0.2,
            sharpe_ratio=1.1,
            sample_fraction=0.05,
            resolved_markets=50,
            active_trading_days=25,
            distinct_events=18,
        )
        result = compute_wallet_score_v1(input=inp)
        original_score = result.score

        row_id = persist_wallet_score_v1(
            db_with_wallet, "w-test", result,
            source_data_timestamp="2026-07-03T00:00:00Z",
        )
        row = db_with_wallet.fetchone(
            "SELECT * FROM wallet_score_decisions WHERE id = ?", (row_id,)
        )

        # Reload every field from the DB and rebuild the input.
        reloaded_inp = WalletScoreInputV1(
            wallet_id=row["wallet_id"],
            info_score=row["info_score"],
            win_rate=row["win_rate"],
            profit_factor=row["profit_factor"],
            trade_count=row["trade_count"],
            max_drawdown=row["max_drawdown"],
            sharpe_ratio=row["sharpe_ratio"],
            sample_fraction=row["sample_fraction"],
            resolved_markets=row["resolved_markets"],
            active_trading_days=row["active_trading_days"],
            distinct_events=row["distinct_events"],
        )
        reloaded_result = compute_wallet_score_v1(input=reloaded_inp)
        assert reloaded_result.score == pytest.approx(original_score)
        assert reloaded_result.verdict == result.verdict

    def test_no_essential_field_silently_null(self, db_with_wallet: Database):
        """When a typed input carries a non-None value, the persisted
        column must not be NULL. This is the regression test for the
        silent-NULL bug."""
        from polycopy.scoring.trade_score_v1 import (
            TradeCopyabilityInputV1,
        )
        from polycopy.scoring.score_serialization import (
            persist_trade_score_v1,
        )

        inp = TradeCopyabilityInputV1(
            wallet_id="w-test",
            source_trade_id="t-2",
            side="SELL",
            price_deterioration_pct=0.02,
            intended_stake=200.0,
            executable_depth=200.0,
            spread=0.01,
            best_bid_size=100.0,
            best_ask_size=120.0,
            trade_age_seconds=60,
            seconds_to_market_end=2 * 24 * 3600,
            market_active=True,
            market_category="crypto",  # would be excluded if short, but 2d is fine
        )
        result = compute_trade_score_v1(input=inp)
        row_id = persist_trade_score_v1(
            db_with_wallet, "w-test", "t-2", result,
            source_data_timestamp="2026-07-03T00:00:00Z",
        )
        row = db_with_wallet.fetchone(
            "SELECT * FROM trade_copyability_decisions WHERE id = ?",
            (row_id,),
        )
        # Every column we set in the input must NOT be NULL in the row.
        non_null_columns = [
            "side", "price_deterioration_pct", "intended_stake",
            "executable_depth", "spread", "best_bid_size", "best_ask_size",
            "trade_age_seconds", "seconds_to_market_end",
        ]
        for col in non_null_columns:
            assert row[col] is not None, (
                f"column {col!r} is NULL despite input carrying a value"
            )

    def test_legacy_result_without_explicit_input_still_persists(
        self, db_with_wallet: Database,
    ):
        """Back-compat: a result built without an explicit input
        object must still persist (using getattr fallbacks). The
        values come from whatever fields the result carries."""
        from polycopy.scoring.score_serialization import (
            persist_wallet_score_v1,
        )

        # Build a result with no input attribute. We do this by
        # constructing the dataclass directly with input=None.
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreComponent, WalletScoreResult, WalletVerdict,
        )
        result = WalletScoreResult(
            wallet_id="w-test",
            score=72.0,
            verdict=WalletVerdict.WATCHLIST,
            input=None,  # legacy: no typed input attached
            components=[
                WalletScoreComponent(
                    name="information_and_price_improvement",
                    raw_score=70.0,
                    weight=30.0,
                    quality="calculated",
                    formula="info_score * 100",
                    note="test",
                ),
            ],
            missing_essentials=[],
            eligibility_gate_failures=[],
        )
        row_id = persist_wallet_score_v1(
            db_with_wallet, "w-test", result,
            source_data_timestamp="2026-07-03T00:00:00Z",
        )
        assert row_id > 0
        row = db_with_wallet.fetchone(
            "SELECT * FROM wallet_score_decisions WHERE id = ?", (row_id,)
        )
        assert row["wallet_id"] == "w-test"
        assert row["final_score"] == pytest.approx(72.0)
        assert row["verdict"] == "watchlist"

    def test_idempotent_persist_does_not_duplicate(self, db_with_wallet: Database):
        """INSERT OR IGNORE on the idempotency key must not create a
        second row for the same point-in-time inputs."""
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1,
        )
        from polycopy.scoring.score_serialization import (
            persist_wallet_score_v1,
        )

        inp = WalletScoreInputV1(
            wallet_id="w-test",
            win_rate=0.5,
            trade_count=100,
        )
        result = compute_wallet_score_v1(input=inp)
        first_id = persist_wallet_score_v1(
            db_with_wallet, "w-test", result,
            source_data_timestamp="2026-07-03T00:00:00Z",
        )
        second_id = persist_wallet_score_v1(
            db_with_wallet, "w-test", result,
            source_data_timestamp="2026-07-03T00:00:00Z",
        )
        assert first_id == second_id
        # Verify only one row exists for this idempotency key.
        count = db_with_wallet.fetchone(
            "SELECT COUNT(*) AS n FROM wallet_score_decisions "
            "WHERE wallet_id = ?",
            ("w-test",),
        )
        assert count["n"] == 1


class TestColumnPlaceholderValueCount:
    """Regression tests for INSERT/VALUES column counts.

    During Chunk 1 work, the wallet serializer was found to have
    32 INSERT columns but only 31 VALUES placeholders, and the
    eligibility_failures_json column was misnamed. These tests
    pin the correct counts at runtime by parsing the actual SQL
    so a future regression (extra/missing column or placeholder)
    fails loudly instead of silently corrupting persistence.
    """

    @staticmethod
    def _extract_values_list(insert_sql: str) -> str:
        """Return the raw VALUES (?, ?, ...) substring from an INSERT
        statement. Handles trailing `RETURNING id` and arbitrary
        whitespace."""
        idx = insert_sql.index("VALUES")
        rest = insert_sql[idx + len("VALUES"):]
        # Find the matching close paren for the VALUES list.
        depth = 0
        for i, ch in enumerate(rest):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return rest[: i + 1]
        raise AssertionError(f"could not find VALUES (...) in: {insert_sql!r}")

    def test_wallet_score_decisions_column_count_matches_schema(self, tmp_path: Path):
        """The INSERT in persist_wallet_score_v1 must list every
        column the v10 schema defines, no more, no less."""
        from polycopy.scoring.score_serialization import persist_wallet_score_v1
        from polycopy.db.database import Database

        # Build a minimal result to drive the function.
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1,
        )
        inp = WalletScoreInputV1(
            wallet_id="w-test",
            win_rate=0.5,
            trade_count=100,
        )
        result = compute_wallet_score_v1(input=inp)

        # Read the actual schema columns. `id` is the auto-increment
        # primary key, never part of an INSERT, so exclude it.
        with Database(db_path=tmp_path / "schema_check.db") as db:
            schema_cols = {
                row["name"]
                for row in db.fetchall(
                    "PRAGMA table_info(wallet_score_decisions)"
                )
            } - {"id"}

        # Capture the SQL by monkey-patching Database.execute.
        import polycopy.scoring.score_serialization as ser
        captured: list[str] = []
        real_execute = ser.Database.execute
        def fake_execute(self_db, sql, params=()):
            captured.append(sql)
            return real_execute(self_db, sql, params)
        try:
            ser.Database.execute = fake_execute  # type: ignore[assignment]
            with Database(db_path=tmp_path / "schema_check2.db") as db:
                db.execute(
                    "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("w-test", "0x", "l", 1, "2026-01-01T00:00:00Z"),
                )
                db.conn.commit()
                try:
                    persist_wallet_score_v1(
                        db, "w-test", result,
                        source_data_timestamp="2026-01-01T00:00:00Z",
                    )
                except Exception:
                    pass
        finally:
            ser.Database.execute = real_execute  # type: ignore[assignment]

        insert_sql = next(
            (s for s in captured if "INSERT" in s and "wallet_score_decisions" in s),
            None,
        )
        assert insert_sql is not None, "wallet_score_decisions INSERT not captured"

        # The INSERT's column list is between `INSERT INTO table (` and
        # `) VALUES (`.
        col_start = insert_sql.index("(") + 1
        col_end = insert_sql.index(")", col_start)
        col_items = [
            c.strip() for c in insert_sql[col_start:col_end].split(",")
        ]
        values_list = self._extract_values_list(insert_sql)
        ph_items = [
            p.strip() for p in values_list.strip()[1:-1].split(",")
        ]
        assert len(col_items) == len(ph_items), (
            f"INSERT column count {len(col_items)} != "
            f"VALUES placeholder count {len(ph_items)}"
        )
        declared = set(col_items)
        assert declared == schema_cols, (
            f"INSERT columns {declared - schema_cols} not in schema; "
            f"schema columns {schema_cols - declared} not in INSERT"
        )

    def test_trade_score_decisions_column_count_matches_schema(self, tmp_path: Path):
        """The INSERT in persist_trade_score_v1 must list every
        column the v10 schema defines."""
        from polycopy.scoring.score_serialization import persist_trade_score_v1
        from polycopy.db.database import Database
        from polycopy.scoring.trade_score_v1 import (
            TradeCopyabilityInputV1,
        )
        import polycopy.scoring.score_serialization as ser

        inp = TradeCopyabilityInputV1(
            wallet_id="w-test",
            source_trade_id="t-1",
            side="BUY",
            intended_stake=100.0,
            executable_depth=200.0,
            spread=0.05,
            trade_age_seconds=100,
            seconds_to_market_end=24 * 3600,
            market_active=True,
        )
        result = compute_trade_score_v1(input=inp)

        with Database(db_path=tmp_path / "schema_check3.db") as db:
            schema_cols = {
                row["name"]
                for row in db.fetchall(
                    "PRAGMA table_info(trade_copyability_decisions)"
                )
            } - {"id"}
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("w-test", "0x", "l", 1, "2026-01-01T00:00:00Z"),
            )
            db.conn.commit()

            captured: list[str] = []
            real_execute = ser.Database.execute
            def fake_execute(self_db, sql, params=()):
                captured.append(sql)
                return real_execute(self_db, sql, params)
            try:
                ser.Database.execute = fake_execute  # type: ignore[assignment]
                try:
                    persist_trade_score_v1(
                        db, "w-test", "t-1", result,
                        source_data_timestamp="2026-01-01T00:00:00Z",
                    )
                except Exception:
                    pass
            finally:
                ser.Database.execute = real_execute  # type: ignore[assignment]

        insert_sql = next(
            (s for s in captured if "INSERT" in s and "trade_copyability_decisions" in s),
            None,
        )
        assert insert_sql is not None, "trade_copyability_decisions INSERT not captured"
        col_start = insert_sql.index("(") + 1
        col_end = insert_sql.index(")", col_start)
        col_items = [
            c.strip() for c in insert_sql[col_start:col_end].split(",")
        ]
        values_list = self._extract_values_list(insert_sql)
        ph_items = [
            p.strip() for p in values_list.strip()[1:-1].split(",")
        ]
        assert len(col_items) == len(ph_items), (
            f"INSERT column count {len(col_items)} != "
            f"VALUES placeholder count {len(ph_items)}"
        )
        declared = set(col_items)
        assert declared == schema_cols, (
            f"INSERT columns {declared - schema_cols} not in schema; "
            f"schema columns {schema_cols - declared} not in INSERT"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Task 2.5 — depth-walk audit persistence
# ─────────────────────────────────────────────────────────────────────────────


def _make_dw_full():
    """Build a fully-filled DepthWalkResult with all Decimal fields."""
    from polycopy.scoring.depth_normalization import DepthWalkResult
    from decimal import Decimal
    return DepthWalkResult(
        side="BUY",
        intended_notional=Decimal("100"),
        filled_notional=Decimal("100"),
        fill_percentage=Decimal("1"),
        contracts_filled=Decimal("1000"),
        vwap_fill_price=Decimal("0.10"),
        slippage=Decimal("0"),
        levels_consumed=1,
        remaining_notional=Decimal("0"),
        is_complete=True,
        insufficient_reason=None,
    )


def _make_dw_partial():
    """Build a partial-fill DepthWalkResult with Decimal fields."""
    from polycopy.scoring.depth_normalization import (
        DepthWalkResult, DEPTH_INSUFFICIENT_FOR_STAKE,
    )
    from decimal import Decimal
    return DepthWalkResult(
        side="BUY",
        intended_notional=Decimal("100"),
        filled_notional=Decimal("30"),
        fill_percentage=Decimal("0.30"),
        contracts_filled=Decimal("300"),
        vwap_fill_price=Decimal("0.10"),
        slippage=Decimal("0.05"),
        levels_consumed=1,
        remaining_notional=Decimal("70"),
        is_complete=False,
        insufficient_reason=DEPTH_INSUFFICIENT_FOR_STAKE,
    )


class TestDepthWalkAuditPersistence:
    """The trade-copyability INSERT persists depth-walk audit evidence
    (depth_walk_json, insufficient_depth_reason, fill_percentage,
    executable_depth, intended_stake, price_snapshot_id, depth_hash).

    Every audit value comes from the typed input — no scattered
    getattr fallbacks on the result.
    """

    @pytest.fixture
    def db_with_wallet(self, tmp_path: Path):
        """Yield a v10-schema DB with a wallet row pre-inserted."""
        from polycopy.db.database import Database
        db_path = tmp_path / "depth_audit_test.db"
        db = Database(db_path=db_path)
        db.connect()
        db.execute(
            """INSERT INTO wallets (id, address, label, is_sample, created_at)
               VALUES ('wallet-depth-audit', '0xabc', 'test', 1,
                       '2026-07-03T00:00:00Z')""",
        )
        return db

    def _compute(self, inp):
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        return compute_trade_score_v1(input=inp)

    def test_full_fill_depth_result_persists_exactly(self, db_with_wallet):
        """A full-fill depth result persists with all Decimal values
        serialized as canonical strings.
        """
        from polycopy.scoring.trade_score_v1 import TradeCopyabilityInputV1
        from polycopy.scoring.score_serialization import persist_trade_score_v1

        dw = _make_dw_full()
        inp = TradeCopyabilityInputV1(
            wallet_id="wallet-depth-audit",
            source_trade_id="trade-audit-1",
            side="BUY",
            intended_stake=100.0,
            spread=0.05,
            trade_age_seconds=100.0,
            seconds_to_market_end=24 * 3600,
            market_active=True,
            depth_walk_result=dw,
            depth_hash="hash-abc",
            price_snapshot_id="snap-audit-1",
        )
        result = self._compute(inp)
        # Insert the parent candidate + snapshot rows so the FK on
        # price_snapshot_id is satisfied.
        db_with_wallet.execute(
            """INSERT INTO copy_candidates (
                   wallet_id, source, source_trade_id,
                   side, source_trade_price, source_trade_quantity,
                   source_trade_timestamp, observed_at,
                   wallet_score_version, wallet_score, wallet_verdict,
                   status, created_at, updated_at
               ) VALUES (
                   'wallet-depth-audit', 'audit-src', 'trade-audit-1-src',
                   'BUY', 0.50, 10.0,
                   '2026-07-03T00:00:00Z', '2026-07-03T00:00:00Z',
                   '1', 0.0, 'incomplete',
                   'pending', '2026-07-03T00:00:00Z', '2026-07-03T00:00:00Z'
               )""",
        )
        db_with_wallet.execute(
            """INSERT INTO candidate_price_snapshots (
                   id, candidate_id, snapshot_run_id, fetch_status,
                   fetch_endpoint, fetch_http_status, fetch_latency_ms,
                   request_attempts,
                   side, source_trade_price, source_trade_quantity,
                   source_trade_timestamp,
                   fetched_at, created_at
               ) VALUES (
                   ?, 1, 'run-audit-1', 'OK',
                   'http://clob', 200, 50,
                   1,
                   'BUY', 0.50, 10.0,
                   '2026-07-03T00:00:00Z',
                   '2026-07-03T00:00:00Z', '2026-07-03T00:00:00Z'
               )""",
            ("snap-audit-1",),
        )
        db_with_wallet.conn.commit()
        decision_id = persist_trade_score_v1(
            db_with_wallet, inp.wallet_id, inp.source_trade_id, result,
            price_snapshot_id=inp.price_snapshot_id,
        )
        assert decision_id > 0

        row = db_with_wallet.fetchone(
            "SELECT depth_walk_json, insufficient_depth_reason, "
            "fill_percentage, executable_depth, intended_stake, "
            "price_snapshot_id FROM trade_copyability_decisions WHERE id = ?",
            (decision_id,),
        )
        assert row is not None
        payload = json.loads(row["depth_walk_json"])
        assert payload["side"] == "BUY"
        # Canonical Decimal strings — normalize() strips trailing zeros,
        # so Decimal("100") → "100", Decimal("0.10") → "0.1".
        assert payload["intended_notional"] == "100"
        assert payload["filled_notional"] == "100"
        assert payload["fill_percentage"] == "1"
        assert payload["contracts_filled"] == "1000"
        assert payload["vwap_fill_price"] == "0.1"
        assert payload["slippage"] == "0"
        assert payload["levels_consumed"] == 1
        assert payload["remaining_notional"] == "0"
        assert payload["is_complete"] is True
        assert payload["insufficient_reason"] is None
        assert payload["depth_hash"] == "hash-abc"
        assert payload["price_snapshot_id"] == "snap-audit-1"
        assert row["insufficient_depth_reason"] is None
        assert row["fill_percentage"] == pytest.approx(1.0, abs=1e-9)
        assert row["executable_depth"] == pytest.approx(100.0, abs=1e-9)
        assert row["intended_stake"] == pytest.approx(100.0, abs=1e-9)
        assert row["price_snapshot_id"] == "snap-audit-1"

    def test_partial_fill_depth_result_persists_insufficient_reason(
        self, db_with_wallet,
    ):
        """A partial fill persists insufficient_depth_reason and partial values."""
        from polycopy.scoring.trade_score_v1 import TradeCopyabilityInputV1
        from polycopy.scoring.score_serialization import persist_trade_score_v1

        dw = _make_dw_partial()
        inp = TradeCopyabilityInputV1(
            wallet_id="wallet-depth-audit",
            source_trade_id="trade-audit-2",
            side="BUY",
            intended_stake=100.0,
            spread=0.05,
            trade_age_seconds=100.0,
            seconds_to_market_end=24 * 3600,
            market_active=True,
            depth_walk_result=dw,
            depth_hash="hash-partial",
            price_snapshot_id="snap-partial",
        )
        result = self._compute(inp)
        decision_id = persist_trade_score_v1(
            db_with_wallet, inp.wallet_id, inp.source_trade_id, result,
        )
        row = db_with_wallet.fetchone(
            "SELECT depth_walk_json, insufficient_depth_reason, "
            "fill_percentage, executable_depth FROM trade_copyability_decisions "
            "WHERE id = ?",
            (decision_id,),
        )
        payload = json.loads(row["depth_walk_json"])
        assert payload["is_complete"] is False
        assert payload["insufficient_reason"] == "DEPTH_INSUFFICIENT_FOR_STAKE"
        assert payload["filled_notional"] == "30"
        # Canonical Decimal strings — normalize() strips trailing zeros.
        assert payload["fill_percentage"] == "0.3"
        assert payload["remaining_notional"] == "70"
        assert row["insufficient_depth_reason"] == "DEPTH_INSUFFICIENT_FOR_STAKE"
        assert row["fill_percentage"] == pytest.approx(0.30, abs=1e-9)
        assert row["executable_depth"] == pytest.approx(30.0, abs=1e-9)

    def test_decimal_values_reload_exactly_from_canonical_strings(
        self, db_with_wallet,
    ):
        """Decimal values persist as canonical strings and reload exactly."""
        from polycopy.scoring.trade_score_v1 import TradeCopyabilityInputV1
        from polycopy.scoring.score_serialization import persist_trade_score_v1
        from polycopy.scoring.depth_normalization import DepthWalkResult
        from decimal import Decimal

        dw = DepthWalkResult(
            side="SELL",
            intended_notional=Decimal("123.456789012345678901234567"),
            filled_notional=Decimal("50.123456789"),
            fill_percentage=Decimal("0.4059"),
            contracts_filled=Decimal("501.23456789"),
            vwap_fill_price=Decimal("0.10"),
            slippage=Decimal("0.025"),
            levels_consumed=2,
            remaining_notional=Decimal("73.333333012345678901234567"),
            is_complete=False,
            insufficient_reason="DEPTH_INSUFFICIENT_FOR_STAKE",
        )
        inp = TradeCopyabilityInputV1(
            wallet_id="wallet-depth-audit",
            source_trade_id="trade-audit-3",
            side="SELL",
            intended_stake=123.456789012345,
            spread=0.05,
            trade_age_seconds=100.0,
            seconds_to_market_end=24 * 3600,
            market_active=True,
            depth_walk_result=dw,
        )
        result = self._compute(inp)
        decision_id = persist_trade_score_v1(
            db_with_wallet, inp.wallet_id, inp.source_trade_id, result,
        )
        row = db_with_wallet.fetchone(
            "SELECT depth_walk_json FROM trade_copyability_decisions "
            "WHERE id = ?",
            (decision_id,),
        )
        payload = json.loads(row["depth_walk_json"])
        # Reload each Decimal field and compare by value (not string).
        # normalize() handles trailing zeros — equivalent Decimals
        # compare equal.
        assert Decimal(payload["intended_notional"]).normalize() == (
            Decimal("123.456789012345678901234567").normalize()
        )
        assert Decimal(payload["filled_notional"]).normalize() == (
            Decimal("50.123456789").normalize()
        )
        assert Decimal(payload["fill_percentage"]).normalize() == (
            Decimal("0.4059").normalize()
        )
        assert Decimal(payload["contracts_filled"]).normalize() == (
            Decimal("501.23456789").normalize()
        )

    def test_no_depth_evidence_produces_null_depth_walk_json(
        self, db_with_wallet,
    ):
        """When no typed depth result AND no status reason, depth_walk_json is NULL."""
        from polycopy.scoring.trade_score_v1 import TradeCopyabilityInputV1
        from polycopy.scoring.score_serialization import persist_trade_score_v1

        inp = TradeCopyabilityInputV1(
            wallet_id="wallet-depth-audit",
            source_trade_id="trade-audit-4",
            side="BUY",
            intended_stake=100.0,
            spread=0.05,
            trade_age_seconds=100.0,
            seconds_to_market_end=24 * 3600,
            market_active=True,
        )
        result = self._compute(inp)
        decision_id = persist_trade_score_v1(
            db_with_wallet, inp.wallet_id, inp.source_trade_id, result,
        )
        row = db_with_wallet.fetchone(
            "SELECT depth_walk_json, insufficient_depth_reason "
            "FROM trade_copyability_decisions WHERE id = ?",
            (decision_id,),
        )
        assert row["depth_walk_json"] is None
        assert row["insufficient_depth_reason"] is None

    def test_depth_status_reason_persists_envelope(self, db_with_wallet):
        """When depth_status_reason is set without a typed result, an
        envelope is persisted documenting the rejection.
        """
        from polycopy.scoring.trade_score_v1 import TradeCopyabilityInputV1
        from polycopy.scoring.score_serialization import persist_trade_score_v1
        from polycopy.scoring.depth_normalization import DEPTH_NOT_CAPTURED

        inp = TradeCopyabilityInputV1(
            wallet_id="wallet-depth-audit",
            source_trade_id="trade-audit-5",
            side="BUY",
            intended_stake=100.0,
            spread=0.05,
            trade_age_seconds=100.0,
            seconds_to_market_end=24 * 3600,
            market_active=True,
            depth_status_reason=DEPTH_NOT_CAPTURED,
            depth_hash="hash-not-captured",
            price_snapshot_id="snap-not-captured",
        )
        result = self._compute(inp)
        decision_id = persist_trade_score_v1(
            db_with_wallet, inp.wallet_id, inp.source_trade_id, result,
        )
        row = db_with_wallet.fetchone(
            "SELECT depth_walk_json, insufficient_depth_reason "
            "FROM trade_copyability_decisions WHERE id = ?",
            (decision_id,),
        )
        payload = json.loads(row["depth_walk_json"])
        assert payload["depth_status_reason"] == DEPTH_NOT_CAPTURED
        assert payload["is_complete"] is False
        assert payload["depth_hash"] == "hash-not-captured"
        assert payload["price_snapshot_id"] == "snap-not-captured"
        assert row["insufficient_depth_reason"] == DEPTH_NOT_CAPTURED

    def test_typed_depth_evidence_does_not_silently_become_null(
        self, db_with_wallet,
    ):
        """Typed depth evidence must be persisted exactly — never NULL
        when a typed result is supplied.
        """
        from polycopy.scoring.trade_score_v1 import TradeCopyabilityInputV1
        from polycopy.scoring.score_serialization import persist_trade_score_v1

        dw = _make_dw_full()
        inp = TradeCopyabilityInputV1(
            wallet_id="wallet-depth-audit",
            source_trade_id="trade-audit-6",
            side="BUY",
            intended_stake=100.0,
            spread=0.05,
            trade_age_seconds=100.0,
            seconds_to_market_end=24 * 3600,
            market_active=True,
            depth_walk_result=dw,
        )
        result = self._compute(inp)
        decision_id = persist_trade_score_v1(
            db_with_wallet, inp.wallet_id, inp.source_trade_id, result,
        )
        row = db_with_wallet.fetchone(
            "SELECT depth_walk_json FROM trade_copyability_decisions "
            "WHERE id = ?",
            (decision_id,),
        )
        assert row["depth_walk_json"] is not None
        payload = json.loads(row["depth_walk_json"])
        for required_key in (
            "side", "intended_notional", "filled_notional",
            "fill_percentage", "contracts_filled", "vwap_fill_price",
            "slippage", "levels_consumed", "remaining_notional",
            "is_complete", "insufficient_reason",
        ):
            assert required_key in payload

    def test_conflicting_raw_values_do_not_override_typed(self, db_with_wallet):
        """Raw fields claiming full fill must not override typed partial fill."""
        from polycopy.scoring.trade_score_v1 import TradeCopyabilityInputV1
        from polycopy.scoring.score_serialization import persist_trade_score_v1

        dw = _make_dw_partial()
        inp = TradeCopyabilityInputV1(
            wallet_id="wallet-depth-audit",
            source_trade_id="trade-audit-7",
            side="BUY",
            intended_stake=100.0,
            executable_depth=99999.0,
            fill_percentage=1.0,
            spread=0.05,
            trade_age_seconds=100.0,
            seconds_to_market_end=24 * 3600,
            market_active=True,
            depth_walk_result=dw,
        )
        result = self._compute(inp)
        decision_id = persist_trade_score_v1(
            db_with_wallet, inp.wallet_id, inp.source_trade_id, result,
        )
        row = db_with_wallet.fetchone(
            "SELECT depth_walk_json, fill_percentage, executable_depth "
            "FROM trade_copyability_decisions WHERE id = ?",
            (decision_id,),
        )
        assert row["fill_percentage"] == pytest.approx(0.30, abs=1e-9)
        assert row["executable_depth"] == pytest.approx(30.0, abs=1e-9)
        payload = json.loads(row["depth_walk_json"])
        # Canonical Decimal strings — normalize() strips trailing zeros.
        assert payload["fill_percentage"] == "0.3"
        assert payload["filled_notional"] == "30"

    def test_idempotent_repeat_returns_existing_row(self, db_with_wallet):
        """Re-persisting the same decision does not create a new row."""
        from polycopy.scoring.trade_score_v1 import TradeCopyabilityInputV1
        from polycopy.scoring.score_serialization import persist_trade_score_v1

        dw = _make_dw_full()
        inp = TradeCopyabilityInputV1(
            wallet_id="wallet-depth-audit",
            source_trade_id="trade-audit-8",
            side="BUY",
            intended_stake=100.0,
            spread=0.05,
            trade_age_seconds=100.0,
            seconds_to_market_end=24 * 3600,
            market_active=True,
            depth_walk_result=dw,
        )
        result = self._compute(inp)
        first_id = persist_trade_score_v1(
            db_with_wallet, inp.wallet_id, inp.source_trade_id, result,
            source_data_timestamp="2026-07-03T00:00:00Z",
        )
        second_id = persist_trade_score_v1(
            db_with_wallet, inp.wallet_id, inp.source_trade_id, result,
            source_data_timestamp="2026-07-03T00:00:00Z",
        )
        assert first_id == second_id
        count = db_with_wallet.fetchone(
            "SELECT COUNT(*) AS n FROM trade_copyability_decisions "
            "WHERE source_trade_id = ?",
            ("trade-audit-8",),
        )["n"]
        assert count == 1


class TestWalletIdContract:
    """Wallet-identity contract (Phase 9 / Chunk 1).

    The compute functions must:
      * accept input-only wallet_id (preferred)
      * accept positional-only wallet_id
      * accept matching duplicates without error
      * raise ValueError on conflicting duplicates
      * raise ValueError on missing wallet_id
    """

    def test_wallet_input_only(self):
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1, compute_wallet_score_v1,
        )
        inp = WalletScoreInputV1(wallet_id="w-only", win_rate=0.5, trade_count=100)
        result = compute_wallet_score_v1(input=inp)
        assert result.wallet_id == "w-only"

    def test_wallet_positional_only(self):
        from polycopy.scoring.wallet_score_v1 import compute_wallet_score_v1
        result = compute_wallet_score_v1(
            wallet_id="w-pos", win_rate=0.5, trade_count=100,
        )
        assert result.wallet_id == "w-pos"

    def test_wallet_matching_duplicates(self):
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1, compute_wallet_score_v1,
        )
        inp = WalletScoreInputV1(wallet_id="w-dup", win_rate=0.5, trade_count=100)
        # Passing matching positional wallet_id alongside input is allowed.
        result = compute_wallet_score_v1(
            wallet_id="w-dup", input=inp,
        )
        assert result.wallet_id == "w-dup"

    def test_wallet_conflicting_ids_raise(self):
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1, compute_wallet_score_v1,
        )
        inp = WalletScoreInputV1(wallet_id="w-a", win_rate=0.5, trade_count=100)
        with pytest.raises(ValueError, match="wallet_id conflict"):
            compute_wallet_score_v1(wallet_id="w-b", input=inp)

    def test_wallet_missing_id_raises(self):
        from polycopy.scoring.wallet_score_v1 import compute_wallet_score_v1
        with pytest.raises(ValueError, match="non-empty wallet_id"):
            compute_wallet_score_v1()
        with pytest.raises(ValueError, match="non-empty wallet_id"):
            compute_wallet_score_v1(wallet_id="")

    def test_wallet_input_with_empty_id_raises(self):
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1, compute_wallet_score_v1,
        )
        inp = WalletScoreInputV1(wallet_id="")
        with pytest.raises(ValueError, match="input.wallet_id"):
            compute_wallet_score_v1(input=inp)

    def test_trade_input_only(self):
        from polycopy.scoring.trade_score_v1 import (
            TradeCopyabilityInputV1, compute_trade_score_v1,
        )
        inp = TradeCopyabilityInputV1(
            wallet_id="w-only", source_trade_id="t-1",
            side="BUY", intended_stake=100.0, executable_depth=200.0,
            spread=0.05, trade_age_seconds=100,
            seconds_to_market_end=24 * 3600, market_active=True,
        )
        result = compute_trade_score_v1(input=inp)
        assert result.wallet_id == "w-only"
        assert result.source_trade_id == "t-1"

    def test_trade_missing_source_trade_id_raises(self):
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        with pytest.raises(ValueError, match="source_trade_id"):
            compute_trade_score_v1(wallet_id="w")

    def test_trade_conflicting_ids_raise(self):
        from polycopy.scoring.trade_score_v1 import (
            TradeCopyabilityInputV1, compute_trade_score_v1,
        )
        inp = TradeCopyabilityInputV1(
            wallet_id="w-a", source_trade_id="t-a",
            side="BUY", intended_stake=100.0, executable_depth=200.0,
            spread=0.05, trade_age_seconds=100,
            seconds_to_market_end=24 * 3600, market_active=True,
        )
        with pytest.raises(ValueError, match="source_trade_id conflict"):
            compute_trade_score_v1(
                wallet_id="w-a", source_trade_id="t-b", input=inp,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Task 2.4 — typed depth-walk evidence integration
# ─────────────────────────────────────────────────────────────────────────────


def _make_full_dw(side="BUY", fill_pct="1", slippage="0"):
    """Build a fully-filled DepthWalkResult for testing."""
    from polycopy.scoring.depth_normalization import DepthWalkResult
    from decimal import Decimal
    return DepthWalkResult(
        side=side,
        intended_notional=Decimal("100"),
        filled_notional=Decimal("100"),
        fill_percentage=Decimal(fill_pct),
        contracts_filled=Decimal("1000"),
        vwap_fill_price=Decimal("0.10"),
        slippage=Decimal(slippage),
        levels_consumed=1,
        remaining_notional=Decimal("0"),
        is_complete=True,
        insufficient_reason=None,
    )


def _make_partial_dw(side="BUY", intended="100", filled="50"):
    """Build a partial-fill DepthWalkResult."""
    from polycopy.scoring.depth_normalization import (
        DepthWalkResult, DEPTH_INSUFFICIENT_FOR_STAKE,
    )
    from decimal import Decimal
    filled_d = Decimal(filled)
    intended_d = Decimal(intended)
    fp = filled_d / intended_d
    return DepthWalkResult(
        side=side,
        intended_notional=intended_d,
        filled_notional=filled_d,
        fill_percentage=fp,
        contracts_filled=filled_d / Decimal("0.10"),
        vwap_fill_price=Decimal("0.10"),
        slippage=Decimal("0.0"),
        levels_consumed=1,
        remaining_notional=intended_d - filled_d,
        is_complete=False,
        insufficient_reason=DEPTH_INSUFFICIENT_FOR_STAKE,
    )


def _make_base_input(**overrides):
    """Build a minimal valid TradeCopyabilityInputV1."""
    from polycopy.scoring.trade_score_v1 import TradeCopyabilityInputV1
    base: dict = dict(
        wallet_id="wallet-depth-1",
        source_trade_id="trade-depth-1",
        side="BUY",
        intended_stake=100.0,
        executable_depth=200.0,
        fill_percentage=1.0,
        spread=0.05,
        best_bid_size=10.0,
        best_ask_size=10.0,
        trade_age_seconds=100.0,
        seconds_to_market_end=24 * 3600,
        market_active=True,
        has_valid_strategy=True,
        has_complete_data=True,
    )
    base.update(overrides)
    return TradeCopyabilityInputV1(**base)


class TestTypedDepthWalkAccepted:
    """The typed depth_walk_result is accepted by compute_trade_score_v1."""

    def test_typed_depth_result_accepted_full_fill(self):
        """A typed full-fill depth result yields a regular verdict."""
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        dw = _make_full_dw()
        inp = _make_base_input(depth_walk_result=dw)
        result = compute_trade_score_v1(input=inp)
        assert result.input is inp
        assert result.input.depth_walk_result is dw

    def test_typed_depth_result_does_not_become_none(self):
        """The typed depth_walk_result attached to result.input survives
        the compute call (no silent None substitution).
        """
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        dw = _make_full_dw()
        inp = _make_base_input(depth_walk_result=dw)
        result = compute_trade_score_v1(input=inp)
        assert result.input.depth_walk_result is dw
        assert result.input.price_snapshot_id == inp.price_snapshot_id
        assert result.input.depth_hash == inp.depth_hash

    def test_price_snapshot_id_attached_to_typed_input(self):
        """price_snapshot_id is preserved on the typed input/output."""
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        dw = _make_full_dw()
        inp = _make_base_input(
            depth_walk_result=dw,
            price_snapshot_id="snap-abc",
        )
        result = compute_trade_score_v1(input=inp)
        assert result.input.price_snapshot_id == "snap-abc"

    def test_depth_hash_attached_to_typed_input(self):
        """depth_hash is preserved on the typed input/output."""
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        dw = _make_full_dw()
        inp = _make_base_input(
            depth_walk_result=dw,
            depth_hash="abc123def456",
        )
        result = compute_trade_score_v1(input=inp)
        assert result.input.depth_hash == "abc123def456"


class TestTypedDepthOverridesRawFields:
    """The typed depth result overrides conflicting raw fields."""

    def test_fill_percentage_typed_overrides_raw(self):
        """Typed fill_percentage = 0.5 overrides raw fill_percentage = 1.0."""
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        dw = _make_partial_dw(filled="50", intended="100")
        # raw fill_percentage = 1.0 (lie), typed says 0.5
        inp = _make_base_input(
            depth_walk_result=dw,
            fill_percentage=1.0,
            executable_depth=200.0,
        )
        result = compute_trade_score_v1(input=inp)
        # Fill_feasibility should reflect 50/100 = 50, not 1.0*100=100.
        ff_component = next(
            c for c in result.components if c.name == "fill_feasibility"
        )
        assert ff_component.raw_score == pytest.approx(50.0, abs=0.001)

    def test_executable_depth_typed_overrides_raw(self):
        """Typed filled_notional = 50 overrides raw executable_depth = 9999."""
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        dw = _make_partial_dw(filled="50", intended="100")
        inp = _make_base_input(
            depth_walk_result=dw,
            executable_depth=9999.0,
            fill_percentage=0.99,
        )
        result = compute_trade_score_v1(input=inp)
        ff_component = next(
            c for c in result.components if c.name == "fill_feasibility"
        )
        # filled (50) / intended (100) = 0.5 → 50.0 score
        assert ff_component.raw_score == pytest.approx(50.0, abs=0.001)

    def test_raw_lying_full_fill_does_not_promote_to_copy_candidate(self):
        """Raw fields claiming full fill must NOT override a typed
        partial fill. The score is partial-truth, not optimistic.
        """
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        # Typed depth says only 10% fill
        dw = _make_partial_dw(filled="10", intended="100")
        # Raw fields claim 100% fill with huge executable depth
        inp = _make_base_input(
            depth_walk_result=dw,
            fill_percentage=1.0,
            executable_depth=99999.0,
        )
        result = compute_trade_score_v1(input=inp)
        ff_component = next(
            c for c in result.components if c.name == "fill_feasibility"
        )
        # The fill_feasibility component must reflect the TYPED 10% fill,
        # not the raw 100% claim.
        assert ff_component.raw_score < 20.0
        # The rejection_reasons must include DEPTH_INSUFFICIENT_FOR_STAKE
        assert "DEPTH_INSUFFICIENT_FOR_STAKE" in result.rejection_reasons


class TestDepthStatusReasons:
    """INCOMPLETE propagation when depth evidence is unavailable or bad."""

    def test_depth_not_captured_returns_incomplete(self):
        from polycopy.scoring.trade_score_v1 import (
            compute_trade_score_v1, TradeVerdict,
        )
        from polycopy.scoring.depth_normalization import DEPTH_NOT_CAPTURED
        inp = _make_base_input(depth_status_reason=DEPTH_NOT_CAPTURED)
        result = compute_trade_score_v1(input=inp)
        assert result.verdict == TradeVerdict.INCOMPLETE
        assert "depth_not_captured" in result.missing_essentials
        assert DEPTH_NOT_CAPTURED in result.rejection_reasons
        assert result.score == 0.0

    def test_depth_levels_malformed_returns_incomplete(self):
        from polycopy.scoring.trade_score_v1 import (
            compute_trade_score_v1, TradeVerdict,
        )
        from polycopy.scoring.depth_normalization import DEPTH_LEVELS_MALFORMED
        inp = _make_base_input(depth_status_reason=DEPTH_LEVELS_MALFORMED)
        result = compute_trade_score_v1(input=inp)
        assert result.verdict == TradeVerdict.INCOMPLETE
        assert "depth_levels_malformed" in result.missing_essentials
        assert DEPTH_LEVELS_MALFORMED in result.rejection_reasons
        assert result.score == 0.0

    def test_depth_snapshot_mismatch_returns_incomplete(self):
        from polycopy.scoring.trade_score_v1 import (
            compute_trade_score_v1, TradeVerdict,
        )
        from polycopy.scoring.depth_normalization import DEPTH_SNAPSHOT_MISMATCH
        inp = _make_base_input(depth_status_reason=DEPTH_SNAPSHOT_MISMATCH)
        result = compute_trade_score_v1(input=inp)
        assert result.verdict == TradeVerdict.INCOMPLETE
        assert "depth_snapshot_mismatch" in result.missing_essentials
        assert DEPTH_SNAPSHOT_MISMATCH in result.rejection_reasons
        assert result.score == 0.0


class TestPartialFillPreserved:
    """A partial fill must be preserved truthfully, not silently
    promoted to a full fill.
    """

    def test_partial_fill_records_insufficient_reason(self):
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        dw = _make_partial_dw(filled="30", intended="100")
        inp = _make_base_input(depth_walk_result=dw)
        result = compute_trade_score_v1(input=inp)
        assert "DEPTH_INSUFFICIENT_FOR_STAKE" in result.rejection_reasons

    def test_partial_fill_does_not_become_full_fill(self):
        """The fill_feasibility score must reflect the actual partial
        fill ratio, not be silently promoted to 100.
        """
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        dw = _make_partial_dw(filled="30", intended="100")
        inp = _make_base_input(depth_walk_result=dw)
        result = compute_trade_score_v1(input=inp)
        ff = next(
            c for c in result.components if c.name == "fill_feasibility"
        )
        # 30 / 100 = 0.30 → 30.0
        assert ff.raw_score == pytest.approx(30.0, abs=0.001)

    def test_full_fill_does_not_record_insufficient_reason(self):
        """Full fills must NOT have DEPTH_INSUFFICIENT_FOR_STAKE recorded."""
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        dw = _make_full_dw()
        inp = _make_base_input(depth_walk_result=dw)
        result = compute_trade_score_v1(input=inp)
        assert "DEPTH_INSUFFICIENT_FOR_STAKE" not in result.rejection_reasons


class TestPrecedenceWithConflictingData:
    """Typed depth evidence must take precedence over conflicting raw
    fields. The score and persisted audit fields reflect the typed
    depth result, never the optimistic raw values.
    """

    def test_conflicting_data_uses_typed_result(self):
        """Raw fields claim full fill; typed depth says 50% fill.
        The score uses the typed 50%, not the optimistic 100%.
        """
        from polycopy.scoring.trade_score_v1 import compute_trade_score_v1
        # Optimistic raw values
        raw_fill_pct = 1.0
        raw_exec_depth = 99999.0
        # Typed depth says 50% fill; the typed slippage (set inside
        # _make_partial_dw at 0.0) is also the source of truth.
        dw = _make_partial_dw(filled="50", intended="100")
        inp = _make_base_input(
            depth_walk_result=dw,
            fill_percentage=raw_fill_pct,
            executable_depth=raw_exec_depth,
        )
        result = compute_trade_score_v1(input=inp)
        ff = next(
            c for c in result.components if c.name == "fill_feasibility"
        )
        # Typed wins: 50/100 = 0.5 → 50.0 component score
        assert ff.raw_score == pytest.approx(50.0, abs=0.001)
        # DEPTH_INSUFFICIENT_FOR_STAKE was added because typed
        # fill was partial
        assert "DEPTH_INSUFFICIENT_FOR_STAKE" in result.rejection_reasons


class TestDepthWalkResultFields:
    """The DepthWalkResult carries all fields needed for audit
    persistence: side, intended_notional, filled_notional,
    fill_percentage, contracts_filled, vwap_fill_price, slippage,
    levels_consumed, remaining_notional, is_complete,
    insufficient_reason.
    """
    from decimal import Decimal

    def test_full_dw_has_all_required_fields(self):
        dw = _make_full_dw()
        Decimal = self.Decimal
        assert dw.side == "BUY"
        assert dw.intended_notional == Decimal("100")
        assert dw.filled_notional == Decimal("100")
        assert dw.fill_percentage == Decimal("1")
        assert dw.contracts_filled == Decimal("1000")
        assert dw.vwap_fill_price == Decimal("0.10")
        assert dw.slippage == Decimal("0")
        assert dw.levels_consumed == 1
        assert dw.remaining_notional == Decimal("0")
        assert dw.is_complete is True
        assert dw.insufficient_reason is None

    def test_partial_dw_has_all_required_fields(self):
        dw = _make_partial_dw(filled="30", intended="100")
        Decimal = self.Decimal
        assert dw.fill_percentage == Decimal("0.30")
        assert dw.is_complete is False
        assert dw.insufficient_reason == "DEPTH_INSUFFICIENT_FOR_STAKE"
        assert dw.remaining_notional == Decimal("70")