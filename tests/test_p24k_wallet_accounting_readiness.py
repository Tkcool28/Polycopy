from __future__ import annotations

import pytest

from polycopy.engine.wallet_accounting_coverage import WalletAccountingCoverageRow
from polycopy.engine.wallet_accounting_readiness import (
    AccountingReadinessConfig,
    REASON_BUY_ONLY_ACCOUNTING_LIMITATION,
    REASON_INSUFFICIENT_ACCOUNTED_TRADES,
    REASON_INSUFFICIENT_ACCOUNTING_COVERAGE,
    REASON_NO_ACCOUNTED_TRADES,
    REASON_NO_LEDGER_ROWS,
    REASON_NO_SOURCE_TRADES,
    STATUS_INCOMPLETE,
    STATUS_LIMITED,
    STATUS_READY,
    accounting_readiness_from_coverage_row,
    evaluate_wallet_accounting_readiness,
)


def readiness(**overrides):
    values = {
        "identity_key": "0xwallet",
        "total_source_trades": 5,
        "total_ledger_rows": 5,
        "accounted_trades": 5,
        "accounting_coverage_pct": 1.0,
        "accountable_buy_coverage_pct": 1.0,
        "buy_only_limitation": False,
    }
    values.update(overrides)
    return evaluate_wallet_accounting_readiness(**values)


def test_current_parked_production_like_state_is_not_scoreable_or_copyable():
    result = readiness(total_source_trades=5, total_ledger_rows=0, accounted_trades=0, accounting_coverage_pct=None)

    assert result.status == STATUS_INCOMPLETE
    assert REASON_NO_LEDGER_ROWS in result.reasons
    assert result.ready_for_skill_score is False
    assert result.ready_for_auto_copy is False


def test_no_source_trades_is_incomplete():
    result = readiness(total_source_trades=0)

    assert result.status == STATUS_INCOMPLETE
    assert REASON_NO_SOURCE_TRADES in result.reasons
    assert result.ready_for_skill_score is False
    assert result.ready_for_auto_copy is False


def test_ledger_rows_exist_but_no_accounted_trades_is_incomplete():
    result = readiness(total_ledger_rows=3, accounted_trades=0, accounting_coverage_pct=0.0)

    assert result.status == STATUS_INCOMPLETE
    assert REASON_NO_ACCOUNTED_TRADES in result.reasons
    assert result.ready_for_skill_score is False
    assert result.ready_for_auto_copy is False


def test_accounted_trades_below_configured_threshold_is_incomplete():
    result = readiness(
        accounted_trades=1,
        accounting_coverage_pct=1.0,
        config=AccountingReadinessConfig(min_accounted_trades=2),
    )

    assert result.status == STATUS_INCOMPLETE
    assert REASON_INSUFFICIENT_ACCOUNTED_TRADES in result.reasons
    assert result.ready_for_skill_score is False
    assert result.ready_for_auto_copy is False


def test_accounting_coverage_below_configured_threshold_is_incomplete():
    result = readiness(
        total_ledger_rows=10,
        accounted_trades=4,
        accounting_coverage_pct=0.4,
        config=AccountingReadinessConfig(min_accounting_coverage_pct=0.5),
    )

    assert result.status == STATUS_INCOMPLETE
    assert REASON_INSUFFICIENT_ACCOUNTING_COVERAGE in result.reasons
    assert result.ready_for_skill_score is False
    assert result.ready_for_auto_copy is False


def test_buy_only_limitation_allows_skill_score_but_blocks_auto_copy():
    result = readiness(
        total_source_trades=7,
        total_ledger_rows=7,
        accounted_trades=2,
        accounting_coverage_pct=2 / 7,
        accountable_buy_coverage_pct=2 / 6,
        buy_only_limitation=True,
        config=AccountingReadinessConfig(min_accounted_trades=1, min_accounting_coverage_pct=0.0),
    )

    assert result.status == STATUS_LIMITED
    assert result.ready_for_skill_score is True
    assert result.ready_for_auto_copy is False
    assert REASON_BUY_ONLY_ACCOUNTING_LIMITATION in result.reasons
    assert REASON_BUY_ONLY_ACCOUNTING_LIMITATION in result.warnings


def test_fully_ready_allows_skill_score_and_auto_copy():
    result = readiness(
        total_source_trades=10,
        total_ledger_rows=10,
        accounted_trades=10,
        accounting_coverage_pct=1.0,
        accountable_buy_coverage_pct=1.0,
        buy_only_limitation=False,
    )

    assert result.status == STATUS_READY
    assert result.ready_for_skill_score is True
    assert result.ready_for_auto_copy is True
    assert result.reasons == ()


def test_none_accounting_coverage_does_not_crash_when_threshold_zero():
    result = readiness(total_ledger_rows=3, accounted_trades=3, accounting_coverage_pct=None)

    assert result.status == STATUS_READY
    assert result.ready_for_skill_score is True
    assert result.ready_for_auto_copy is True


def test_none_accounting_coverage_with_positive_threshold_is_incomplete():
    result = readiness(
        total_ledger_rows=3,
        accounted_trades=3,
        accounting_coverage_pct=None,
        config=AccountingReadinessConfig(min_accounting_coverage_pct=0.01),
    )

    assert result.status == STATUS_INCOMPLETE
    assert REASON_INSUFFICIENT_ACCOUNTING_COVERAGE in result.reasons
    assert result.ready_for_skill_score is False
    assert result.ready_for_auto_copy is False


def test_invalid_config_rejects_negative_min_accounted_trades():
    with pytest.raises(ValueError, match="min_accounted_trades"):
        AccountingReadinessConfig(min_accounted_trades=-1)


@pytest.mark.parametrize("coverage", [-0.01, 1.01])
def test_invalid_config_rejects_coverage_threshold_outside_zero_to_one(coverage):
    with pytest.raises(ValueError, match="min_accounting_coverage_pct"):
        AccountingReadinessConfig(min_accounting_coverage_pct=coverage)


def test_accepts_pr24j_wallet_accounting_coverage_row():
    row = WalletAccountingCoverageRow(
        identity_key="0xcoverage",
        group_by="trader_address",
        source_trades=7,
        buy_trades=6,
        sell_trades=1,
        total_ledger_rows=7,
        accounted_trades=2,
        accounting_coverage_pct=2 / 7,
        accountable_buy_coverage_pct=2 / 6,
        buy_only_limitation=True,
    )

    result = accounting_readiness_from_coverage_row(row)

    assert result.identity_key == "0xcoverage"
    assert result.status == STATUS_LIMITED
    assert result.total_source_trades == 7
    assert result.total_ledger_rows == 7
    assert result.accounted_trades == 2
    assert result.accounting_coverage_pct == pytest.approx(2 / 7)
    assert result.accountable_buy_coverage_pct == pytest.approx(2 / 6)
    assert result.buy_only_limitation is True
    assert result.ready_for_skill_score is True
    assert result.ready_for_auto_copy is False
