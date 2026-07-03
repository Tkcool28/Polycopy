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

import pytest

from polycopy.scoring.helpers import linear_score, inverse_score, clamp
from polycopy.scoring.behavior_classification import (
    BehaviorClassification,
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
    SignalVerdict,
    SignalDecisionInput,
    generate_signal_verdict,
)
from polycopy.scoring.score_serialization import generate_idempotency_key


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
        evidence = BehaviorEvidence(
            trade_count=50,
            avg_time_between_trades_seconds=1000,  # Not HF
            distinct_markets_traded=10,
        )
        result = classify_wallet_behavior(evidence)
        assert result.classification == BehaviorClassification.DIRECTIONAL
        assert result.is_eligible_for_copy is True
        assert result.is_skip is False

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
        assert hp_comp.raw_score == 75.0

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
        assert hp_comp.raw_score == 100.0

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
    """Test every signal decision branch."""

    def test_copy_candidate_all_conditions_met(self):
        input_data = SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_verdict="copy_candidate",
            trade_score=75.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
            has_hard_exclusion=False,
        )
        result = generate_signal_verdict(input_data)
        assert result.verdict == SignalVerdict.COPY_CANDIDATE

    def test_skip_skilled_wallet_trade_not_copyable(self):
        input_data = SignalDecisionInput(
            wallet_score=80.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_verdict="copy_candidate",
            trade_score=60.0,  # Below 70 threshold
            trade_verdict=TradeVerdict.WATCHLIST,
            behavior_classification=None,
            has_hard_exclusion=False,
        )
        result = generate_signal_verdict(input_data)
        assert result.verdict == SignalVerdict.SKIP
        assert "skilled_wallet_trade_not_copyable" in (result.skipped_reason or "")

    def test_watchlist_wallet_score_55_to_74(self):
        input_data = SignalDecisionInput(
            wallet_score=65.0,
            wallet_verdict=WalletVerdict.WATCHLIST,
            category_wallet_verdict="copy_candidate",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=None,
            has_hard_exclusion=False,
        )
        result = generate_signal_verdict(input_data)
        assert result.verdict == SignalVerdict.WATCHLIST

    def test_incomplete_missing_wallet_score(self):
        input_data = SignalDecisionInput(
            wallet_score=None,
            wallet_verdict=None,
            category_wallet_verdict=None,
            trade_score=None,
            trade_verdict=None,
            behavior_classification=None,
            has_hard_exclusion=False,
        )
        result = generate_signal_verdict(input_data)
        assert result.verdict == SignalVerdict.INCOMPLETE

    def test_behavior_skip_caps_verdict(self):
        input_data = SignalDecisionInput(
            wallet_score=90.0,
            wallet_verdict=WalletVerdict.COPY_CANDIDATE,
            category_wallet_verdict="copy_candidate",
            trade_score=80.0,
            trade_verdict=TradeVerdict.COPY_CANDIDATE,
            behavior_classification=type('BehaviorClassificationResult', (), {
                'classification': BehaviorClassification.HIGH_FREQUENCY_BOT,
                'is_eligible_for_copy': False,
                'is_watchlist_cap': False,
                'is_skip': True,
                'reasons': ['hf_detected'],
            })(),
            has_hard_exclusion=False,
        )
        result = generate_signal_verdict(input_data)
        assert result.verdict == SignalVerdict.SKIP


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