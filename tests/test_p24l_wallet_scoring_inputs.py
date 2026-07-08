from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace
import importlib
import sqlite3

import pytest

import polycopy.engine.wallet_scoring_inputs as wallet_scoring_inputs
from polycopy.engine.wallet_accounting_coverage import WalletAccountingCoverageRow
from polycopy.engine.wallet_accounting_readiness import (
    AccountingReadinessConfig,
    REASON_BUY_ONLY_ACCOUNTING_LIMITATION,
    REASON_INSUFFICIENT_ACCOUNTED_TRADES,
    REASON_MISSING_IDENTITY_GROUPING,
    REASON_NO_LEDGER_ROWS,
    REASON_UNSUPPORTED_IDENTITY_GROUPING,
)
from polycopy.engine.wallet_scoring_inputs import (
    STATUS_BLOCKED,
    STATUS_SCORE_INPUT_LIMITED,
    STATUS_SCORE_INPUT_READY,
    WalletScoringInputAdapterSummary,
    WalletScoringInputCandidate,
    build_wallet_scoring_input_candidate,
    build_wallet_scoring_input_candidates,
)


def coverage_row(**overrides) -> WalletAccountingCoverageRow:
    values = {
        "identity_key": "0xwallet",
        "group_by": "trader_address",
        "source_trades": 10,
        "buy_trades": 10,
        "sell_trades": 0,
        "total_ledger_rows": 10,
        "accounted_trades": 10,
        "total_realized_pnl": 25.5,
        "roi": 0.255,
        "win_rate": 0.6,
        "profit_factor": 2.5,
        "accounting_coverage_pct": 1.0,
        "accountable_buy_coverage_pct": 1.0,
        "buy_only_limitation": False,
    }
    values.update(overrides)
    return WalletAccountingCoverageRow(**values)


def assert_adapter_invariants(candidate: WalletScoringInputCandidate) -> None:
    if candidate.candidate_status != STATUS_BLOCKED:
        assert candidate.blocked_reasons == ()
    else:
        assert candidate.ready_for_skill_score is False
        assert candidate.ready_for_auto_copy is False
        assert candidate.blocked_reasons


def test_ready_coverage_row_becomes_score_input_ready():
    candidate = build_wallet_scoring_input_candidate(coverage_row())

    assert candidate.identity_key == "0xwallet"
    assert candidate.identity_group_by == "trader_address"
    assert candidate.candidate_status == STATUS_SCORE_INPUT_READY
    assert candidate.ready_for_skill_score is True
    assert candidate.ready_for_auto_copy is True
    assert candidate.blocked_reasons == ()
    assert candidate.warnings == ()
    assert_adapter_invariants(candidate)


def test_missing_identity_grouping_is_blocked_even_when_numerically_ready():
    candidate = build_wallet_scoring_input_candidate(coverage_row(group_by=None))

    assert candidate.identity_group_by is None
    assert candidate.candidate_status == STATUS_BLOCKED
    assert candidate.ready_for_skill_score is False
    assert candidate.ready_for_auto_copy is False
    assert REASON_MISSING_IDENTITY_GROUPING in candidate.blocked_reasons
    assert_adapter_invariants(candidate)


def test_wallet_id_grouped_row_is_blocked_even_when_numerically_ready():
    candidate = build_wallet_scoring_input_candidate(
        coverage_row(identity_key="wallet-1", group_by="wallet_id")
    )

    assert candidate.identity_key == "wallet-1"
    assert candidate.identity_group_by == "wallet_id"
    assert candidate.candidate_status == STATUS_BLOCKED
    assert candidate.ready_for_skill_score is False
    assert candidate.ready_for_auto_copy is False
    assert REASON_UNSUPPORTED_IDENTITY_GROUPING in candidate.blocked_reasons
    assert_adapter_invariants(candidate)


def test_limited_readiness_keeps_score_input_visible_without_blocked_reasons():
    candidate = build_wallet_scoring_input_candidate(
        coverage_row(
            source_trades=7,
            buy_trades=6,
            sell_trades=1,
            total_ledger_rows=7,
            accounted_trades=2,
            accounting_coverage_pct=2 / 7,
            accountable_buy_coverage_pct=2 / 6,
            buy_only_limitation=True,
        )
    )

    assert candidate.candidate_status == STATUS_SCORE_INPUT_LIMITED
    assert candidate.ready_for_skill_score is True
    assert candidate.ready_for_auto_copy is False
    assert candidate.blocked_reasons == ()
    assert REASON_BUY_ONLY_ACCOUNTING_LIMITATION in candidate.warnings
    assert_adapter_invariants(candidate)


def test_limited_candidate_warnings_include_readiness_warnings_and_reasons(monkeypatch):
    def fake_readiness(row, *, config=None):
        return SimpleNamespace(
            identity_key=row.identity_key,
            identity_group_by=row.group_by,
            ready_for_skill_score=True,
            ready_for_auto_copy=False,
            reasons=("limited_reason",),
            warnings=("limited_warning",),
            total_source_trades=row.source_trades,
            total_ledger_rows=row.total_ledger_rows,
            accounted_trades=row.accounted_trades,
            accounting_coverage_pct=row.accounting_coverage_pct,
            accountable_buy_coverage_pct=row.accountable_buy_coverage_pct,
            buy_only_limitation=row.buy_only_limitation,
        )

    monkeypatch.setattr(
        wallet_scoring_inputs,
        "accounting_readiness_from_coverage_row",
        fake_readiness,
    )

    candidate = build_wallet_scoring_input_candidate(coverage_row(identity_key="0xlimited"))

    assert candidate.candidate_status == STATUS_SCORE_INPUT_LIMITED
    assert candidate.blocked_reasons == ()
    assert candidate.warnings == ("limited_warning", "limited_reason")
    assert_adapter_invariants(candidate)


def test_limited_candidate_warnings_include_readiness_reasons_once():
    candidate = build_wallet_scoring_input_candidate(coverage_row(buy_only_limitation=True))

    assert candidate.candidate_status == STATUS_SCORE_INPUT_LIMITED
    assert candidate.blocked_reasons == ()
    assert candidate.warnings.count(REASON_BUY_ONLY_ACCOUNTING_LIMITATION) == 1
    assert_adapter_invariants(candidate)


def test_parked_production_like_row_is_blocked_with_no_ledger_reason():
    candidate = build_wallet_scoring_input_candidate(
        coverage_row(
            identity_key="0xsample_trader_a_do_not_use",
            group_by="trader_address",
            source_trades=5,
            buy_trades=5,
            total_ledger_rows=0,
            accounted_trades=0,
            accounting_coverage_pct=None,
            accountable_buy_coverage_pct=None,
        )
    )

    assert candidate.identity_key == "0xsample_trader_a_do_not_use"
    assert candidate.identity_group_by == "trader_address"
    assert candidate.source_trades == 5
    assert candidate.candidate_status == STATUS_BLOCKED
    assert candidate.ready_for_skill_score is False
    assert candidate.ready_for_auto_copy is False
    assert REASON_NO_LEDGER_ROWS in candidate.blocked_reasons
    assert candidate.warnings == ()
    assert_adapter_invariants(candidate)


def test_readiness_config_is_passed_to_readiness_guard():
    candidate = build_wallet_scoring_input_candidate(
        coverage_row(accounted_trades=1, accounting_coverage_pct=1.0),
        readiness_config=AccountingReadinessConfig(min_accounted_trades=2),
    )

    assert candidate.candidate_status == STATUS_BLOCKED
    assert REASON_INSUFFICIENT_ACCOUNTED_TRADES in candidate.blocked_reasons
    assert_adapter_invariants(candidate)


def test_identity_and_metrics_are_copied_from_coverage_row():
    candidate = build_wallet_scoring_input_candidate(
        coverage_row(
            identity_key="0xmetrics",
            source_trades=13,
            total_ledger_rows=11,
            accounted_trades=8,
            total_realized_pnl=Decimal("-42.25"),
            roi=-0.125,
            win_rate=0.375,
            profit_factor=0.8,
            accounting_coverage_pct=8 / 11,
            accountable_buy_coverage_pct=8 / 13,
        )
    )

    assert candidate.identity_key == "0xmetrics"
    assert candidate.identity_group_by == "trader_address"
    assert candidate.source_trades == 13
    assert candidate.total_ledger_rows == 11
    assert candidate.accounted_trades == 8
    assert candidate.total_realized_pnl == Decimal("-42.25")
    assert type(candidate.total_realized_pnl) is Decimal
    assert candidate.roi == pytest.approx(-0.125)
    assert candidate.win_rate == pytest.approx(0.375)
    assert candidate.profit_factor == pytest.approx(0.8)
    assert candidate.accounting_coverage_pct == pytest.approx(8 / 11)
    assert candidate.accountable_buy_coverage_pct == pytest.approx(8 / 13)
    assert_adapter_invariants(candidate)


def test_adapter_does_not_mutate_input_row():
    row = coverage_row(identity_key="0ximmutable", buy_only_limitation=True)
    before = asdict(row)

    build_wallet_scoring_input_candidate(row)

    assert asdict(row) == before


def test_batch_summary_preserves_order_counts_statuses_and_blocked_rows():
    rows = [
        coverage_row(
            identity_key="0xsample_trader_a_do_not_use",
            source_trades=5,
            total_ledger_rows=0,
            accounted_trades=0,
            accounting_coverage_pct=None,
        ),
        coverage_row(
            identity_key="limited",
            source_trades=7,
            buy_trades=6,
            sell_trades=1,
            total_ledger_rows=7,
            accounted_trades=2,
            accounting_coverage_pct=2 / 7,
            accountable_buy_coverage_pct=2 / 6,
            buy_only_limitation=True,
        ),
        coverage_row(identity_key="ready"),
        coverage_row(identity_key="wallet-1", group_by="wallet_id"),
    ]

    summary = build_wallet_scoring_input_candidates(rows)

    assert isinstance(summary, WalletScoringInputAdapterSummary)
    assert summary.total_rows == 4
    assert summary.score_input_ready == 1
    assert summary.score_input_limited == 1
    assert summary.blocked == 2
    assert summary.auto_copy_ready == 1
    assert summary.auto_copy_blocked == 3
    assert tuple(candidate.identity_key for candidate in summary.candidates) == (
        "0xsample_trader_a_do_not_use",
        "limited",
        "ready",
        "wallet-1",
    )
    assert tuple(candidate.candidate_status for candidate in summary.candidates) == (
        STATUS_BLOCKED,
        STATUS_SCORE_INPUT_LIMITED,
        STATUS_SCORE_INPUT_READY,
        STATUS_BLOCKED,
    )
    for candidate in summary.candidates:
        assert_adapter_invariants(candidate)


def test_empty_batch_returns_empty_summary():
    summary = build_wallet_scoring_input_candidates([])

    assert summary == WalletScoringInputAdapterSummary(
        total_rows=0,
        score_input_ready=0,
        score_input_limited=0,
        blocked=0,
        auto_copy_ready=0,
        auto_copy_blocked=0,
        candidates=(),
    )


def test_module_import_and_helper_run_are_db_free_with_in_memory_rows(monkeypatch):
    def fail_connect(*args, **kwargs):
        raise AssertionError("wallet_scoring_inputs import/helper must not open sqlite")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    module = importlib.reload(wallet_scoring_inputs)

    candidate = module.build_wallet_scoring_input_candidate(coverage_row(identity_key="0xinmemory"))

    assert candidate.identity_key == "0xinmemory"
    assert candidate.candidate_status == STATUS_SCORE_INPUT_READY
    assert_adapter_invariants(candidate)


def test_candidate_exposes_no_score_rank_or_copy_candidate_fields_and_does_not_sort():
    summary = build_wallet_scoring_input_candidates(
        [
            coverage_row(identity_key="low", total_realized_pnl=-50.0, roi=-0.5),
            coverage_row(identity_key="high", total_realized_pnl=500.0, roi=5.0),
            coverage_row(identity_key="mid", total_realized_pnl=10.0, roi=0.1),
        ]
    )

    assert tuple(candidate.identity_key for candidate in summary.candidates) == ("low", "high", "mid")
    for candidate in summary.candidates:
        assert "score" not in candidate.__dataclass_fields__
        assert "rank" not in candidate.__dataclass_fields__
        assert "copy_candidate" not in candidate.__dataclass_fields__
        assert "wallet_skill_score" not in candidate.__dataclass_fields__
        assert not hasattr(candidate, "score")
        assert not hasattr(candidate, "rank")
        assert not hasattr(candidate, "copy_candidate")
        assert not hasattr(candidate, "wallet_skill_score")
