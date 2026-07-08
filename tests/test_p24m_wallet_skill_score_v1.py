from __future__ import annotations

import importlib
import inspect

import pytest

from polycopy.engine.wallet_scoring_inputs import WalletScoringInputCandidate
from polycopy.scoring.wallet_skill_score_v1 import (
    COMPONENT_CATEGORY_SPECIALIZATION,
    COMPONENT_CHRONOLOGICAL_CONSISTENCY,
    COMPONENT_CONCENTRATION_QUALITY,
    COMPONENT_INFORMATION_AND_PRICE_IMPROVEMENT_QUALITY,
    COMPONENT_RISK_AND_DRAWDOWN_QUALITY,
    COMPONENT_SAMPLE_RELIABILITY,
    COMPONENT_VERIFIED_REALIZED_PERFORMANCE,
    REASON_INSUFFICIENT_ACCOUNTED_TRADE_SAMPLE,
    REASON_UNSUPPORTED_IDENTITY_GROUPING,
    VERDICT_INCOMPLETE,
    VERDICT_WATCHLIST,
    WALLET_SKILL_SCORE_WEIGHTS_V1,
    WalletSkillScoreComponentV1,
    WalletSkillScoreConfigV1,
    average_available,
    clamp_0_100,
    compute_wallet_skill_score_v1,
    linear_score,
    midpoint_linear_score,
)


def candidate(**overrides) -> WalletScoringInputCandidate:
    values = {
        "identity_key": "0xwallet",
        "identity_group_by": "trader_address",
        "candidate_status": "score_input_ready",
        "ready_for_skill_score": True,
        "ready_for_auto_copy": True,
        "blocked_reasons": (),
        "warnings": (),
        "source_trades": 200,
        "total_ledger_rows": 200,
        "accounted_trades": 200,
        "accounting_coverage_pct": 1.0,
        "accountable_buy_coverage_pct": 1.0,
        "buy_only_limitation": False,
        "total_realized_pnl": 123.45,
        "roi": 1.0,
        "win_rate": 0.65,
        "profit_factor": 2.0,
    }
    values.update(overrides)
    return WalletScoringInputCandidate(**values)


def component(result, name):
    return next(component for component in result.components if component.name == name)


def test_a_weights_sum_to_100():
    assert sum(WALLET_SKILL_SCORE_WEIGHTS_V1.values()) == pytest.approx(100.0)


def test_b_parked_production_like_blocked_candidate_is_incomplete():
    result = compute_wallet_skill_score_v1(
        candidate(
            identity_key="0xsample_trader_a_do_not_use",
            candidate_status="blocked",
            ready_for_skill_score=False,
            ready_for_auto_copy=False,
            blocked_reasons=("no_ledger_rows",),
            source_trades=5,
            total_ledger_rows=0,
            accounted_trades=0,
            accounting_coverage_pct=None,
            accountable_buy_coverage_pct=None,
        )
    )

    assert result.verdict == VERDICT_INCOMPLETE
    assert result.score == 0.0
    assert result.eligible_for_ranking is False
    assert result.eligible_for_auto_copy is False
    assert "no_ledger_rows" in result.blocked_reasons
    assert "no_ledger_rows" in result.missing_essentials


def test_blocked_candidate_normalizes_malformed_ready_flags_false():
    result = compute_wallet_skill_score_v1(
        candidate(
            candidate_status="blocked",
            ready_for_skill_score=True,
            ready_for_auto_copy=True,
            blocked_reasons=("no_ledger_rows",),
            source_trades=5,
            total_ledger_rows=0,
            accounted_trades=0,
            accounting_coverage_pct=None,
            accountable_buy_coverage_pct=None,
        )
    )

    assert result.verdict == VERDICT_INCOMPLETE
    assert result.ready_for_skill_score is False
    assert result.ready_for_auto_copy is False
    assert result.eligible_for_ranking is False
    assert result.eligible_for_auto_copy is False


def test_c_score_input_ready_strong_realized_is_partial_but_incomplete():
    result = compute_wallet_skill_score_v1(
        candidate(roi=2.0, win_rate=0.90, profit_factor=5.0, accounted_trades=200)
    )

    assert result.score == pytest.approx(25.0)
    assert component(result, COMPONENT_VERIFIED_REALIZED_PERFORMANCE).weighted_score == pytest.approx(15.0)
    assert component(result, COMPONENT_SAMPLE_RELIABILITY).weighted_score == pytest.approx(10.0)
    assert result.verdict == VERDICT_INCOMPLETE
    assert set(result.missing_essentials) == {
        COMPONENT_INFORMATION_AND_PRICE_IMPROVEMENT_QUALITY,
        COMPONENT_CHRONOLOGICAL_CONSISTENCY,
        COMPONENT_RISK_AND_DRAWDOWN_QUALITY,
        COMPONENT_CATEGORY_SPECIALIZATION,
        COMPONENT_CONCENTRATION_QUALITY,
    }
    assert result.eligible_for_auto_copy is False


def test_d_strong_realized_performance_alone_cannot_make_copy_candidate():
    result = compute_wallet_skill_score_v1(
        candidate(
            roi=2.0,
            win_rate=0.90,
            profit_factor=5.0,
            accounted_trades=200,
            accounting_coverage_pct=1.0,
            accountable_buy_coverage_pct=1.0,
        )
    )

    assert result.score == pytest.approx(25.0)
    assert result.verdict == VERDICT_INCOMPLETE
    assert result.eligible_for_auto_copy is False
    assert result.ready_for_auto_copy is True


def test_e_weak_realized_performance_is_low_and_still_incomplete():
    result = compute_wallet_skill_score_v1(
        candidate(roi=-0.25, win_rate=0.40, profit_factor=0.80)
    )

    realized = component(result, COMPONENT_VERIFIED_REALIZED_PERFORMANCE)
    assert realized.normalized_score == pytest.approx((25.0 + (50.0 / 3.0) + 10.0) / 3.0)
    assert realized.weighted_score < 3.0
    assert result.verdict == VERDICT_INCOMPLETE


def test_f_sample_reliability_penalizes_low_accounted_trade_count():
    low_sample = compute_wallet_skill_score_v1(
        candidate(accounted_trades=5, accounting_coverage_pct=1.0, accountable_buy_coverage_pct=1.0)
    )
    enough_sample = compute_wallet_skill_score_v1(
        candidate(accounted_trades=30, accounting_coverage_pct=1.0, accountable_buy_coverage_pct=1.0)
    )

    assert component(low_sample, COMPONENT_SAMPLE_RELIABILITY).normalized_score < component(
        enough_sample,
        COMPONENT_SAMPLE_RELIABILITY,
    ).normalized_score
    assert REASON_INSUFFICIENT_ACCOUNTED_TRADE_SAMPLE in low_sample.warnings
    assert REASON_INSUFFICIENT_ACCOUNTED_TRADE_SAMPLE in low_sample.blocked_reasons


def test_g_buy_only_limited_candidate_preserves_warning_and_blocks_auto_copy():
    result = compute_wallet_skill_score_v1(
        candidate(
            candidate_status="score_input_limited",
            ready_for_skill_score=True,
            ready_for_auto_copy=False,
            warnings=("buy_only_accounting_limitation",),
            buy_only_limitation=True,
        )
    )

    assert result.ready_for_skill_score is True
    assert result.ready_for_auto_copy is False
    assert result.eligible_for_auto_copy is False
    assert "buy_only_accounting_limitation" in result.warnings


def test_h_unsupported_wallet_id_identity_is_incomplete():
    result = compute_wallet_skill_score_v1(
        candidate(identity_key="wallet-1", identity_group_by="wallet_id")
    )

    assert result.verdict == VERDICT_INCOMPLETE
    assert REASON_UNSUPPORTED_IDENTITY_GROUPING in result.blocked_reasons
    assert result.eligible_for_ranking is False
    assert result.eligible_for_auto_copy is False


def test_unsupported_identity_normalizes_malformed_ready_flags_false():
    result = compute_wallet_skill_score_v1(
        candidate(
            identity_key="wallet-1",
            identity_group_by="wallet_id",
            ready_for_skill_score=True,
            ready_for_auto_copy=True,
        )
    )

    assert result.verdict == VERDICT_INCOMPLETE
    assert result.ready_for_skill_score is False
    assert result.ready_for_auto_copy is False
    assert result.eligible_for_ranking is False
    assert result.eligible_for_auto_copy is False


def test_insufficient_sample_downgrades_otherwise_copy_candidate_to_watchlist(monkeypatch):
    import polycopy.scoring.wallet_skill_score_v1 as module

    def high_evidence_components(_candidate, _config):
        return tuple(
            WalletSkillScoreComponentV1(
                name=name,
                weight=weight,
                raw_value={"test": "synthetic_complete_evidence"},
                normalized_score=100.0,
                weighted_score=weight,
                quality="strong",
                missing=False,
                blocking=False,
                note="synthetic_complete_evidence_for_sample_gate_test",
            )
            for name, weight in WALLET_SKILL_SCORE_WEIGHTS_V1.items()
        )

    monkeypatch.setattr(module, "_components", high_evidence_components)

    result = module.compute_wallet_skill_score_v1(
        candidate(
            accounted_trades=5,
            accounting_coverage_pct=1.0,
            accountable_buy_coverage_pct=1.0,
        )
    )

    assert result.score == pytest.approx(100.0)
    assert REASON_INSUFFICIENT_ACCOUNTED_TRADE_SAMPLE in result.blocked_reasons
    assert result.verdict == VERDICT_WATCHLIST
    assert result.eligible_for_auto_copy is False


def test_i_missing_price_improvement_metric_is_not_faked():
    result = compute_wallet_skill_score_v1(candidate())
    price_component = component(result, COMPONENT_INFORMATION_AND_PRICE_IMPROVEMENT_QUALITY)

    assert price_component.normalized_score is None
    assert price_component.weighted_score == 0.0
    assert price_component.missing is True
    assert price_component.blocking is True
    assert price_component.note == "price_improvement_evidence_missing"


def test_j_helpers():
    assert clamp_0_100(-1) == 0.0
    assert clamp_0_100(101) == 100.0
    assert linear_score(0.4, 0.0, 0.8) == pytest.approx(50.0)
    assert linear_score(1.0, 0.0, 0.8) == 100.0
    assert midpoint_linear_score(-0.50, -0.50, 0.0, 1.0) == 0.0
    assert midpoint_linear_score(0.0, -0.50, 0.0, 1.0) == 50.0
    assert midpoint_linear_score(1.0, -0.50, 0.0, 1.0) == 100.0
    assert average_available([None, 20.0, 40.0]) == pytest.approx(30.0)
    assert average_available([None, None]) is None
    with pytest.raises(ValueError):
        linear_score(1.0, 1.0, 1.0)
    with pytest.raises(ValueError):
        midpoint_linear_score(1.0, 0.0, 0.0, 1.0)


def test_k_module_import_and_compute_do_not_open_db(monkeypatch):
    import sqlite3

    def fail_connect(*args, **kwargs):
        raise AssertionError("sqlite3.connect must not be called")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    module = importlib.import_module("polycopy.scoring.wallet_skill_score_v1")
    result = module.compute_wallet_skill_score_v1(candidate())

    assert result.identity_key == "0xwallet"


def test_l_module_has_no_db_or_automation_imports():
    import polycopy.scoring.wallet_skill_score_v1 as module

    source = inspect.getsource(module)
    forbidden_import_fragments = (
        "import sqlite3",
        "from sqlite3",
        "import Database",
        "from polycopy.db",
        "from polycopy.broker",
        "import broker",
        "import order",
        "run_scan",
        "collect",
        "settle",
        "update",
    )
    assert all(fragment not in source for fragment in forbidden_import_fragments)


def test_m_result_raw_inputs_contains_honest_pr24l_fields():
    result = compute_wallet_skill_score_v1(
        candidate(
            total_realized_pnl=-10.5,
            roi=-0.1,
            win_rate=0.4,
            profit_factor=0.9,
            accounted_trades=12,
            accounting_coverage_pct=0.75,
            buy_only_limitation=True,
        )
    )

    assert result.raw_inputs["total_realized_pnl"] == -10.5
    assert result.raw_inputs["roi"] == -0.1
    assert result.raw_inputs["win_rate"] == 0.4
    assert result.raw_inputs["profit_factor"] == 0.9
    assert result.raw_inputs["accounted_trades"] == 12
    assert result.raw_inputs["accounting_coverage_pct"] == 0.75
    assert result.raw_inputs["buy_only_limitation"] is True


def test_config_defaults_are_frozen_contract_values():
    config = WalletSkillScoreConfigV1()
    assert config.copy_candidate_min_score == 75.0
    assert config.watchlist_min_score == 55.0
    assert config.min_accounted_trades == 30
    assert config.min_accounting_coverage_pct == 0.80
    assert config.require_price_improvement_evidence_for_copy_candidate is True
    assert config.require_risk_evidence_for_copy_candidate is True
