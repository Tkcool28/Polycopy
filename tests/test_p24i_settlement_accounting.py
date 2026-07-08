from __future__ import annotations

import math

import pytest

from polycopy.engine.settlement_accounting import (
    SELL_UNSUPPORTED_REASON,
    aggregate_accounting_entries,
    build_settlement_accounting_entry,
)


def trade(**overrides):
    base = {
        "id": "t1",
        "wallet_id": None,
        "trader_address": "0xabc",
        "market_id": "m1",
        "market_source_id": "ms1",
        "token_id": "yes-token",
        "winning_token_id": "yes-token",
        "side": "BUY",
        "outcome": "YES",
        "quantity": 100.0,
        "price": 0.4,
        "resolution_status": "won",
        "is_winning_trade": 1,
        "settlement_source": "test",
        "resolved_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def entry(**overrides):
    return build_settlement_accounting_entry(trade(**overrides))


def test_buy_won_accounted():
    e = entry(price=0.40, quantity=100, resolution_status="won")
    assert e.cost_basis == pytest.approx(40)
    assert e.payout == pytest.approx(100)
    assert e.realized_pnl == pytest.approx(60)
    assert e.roi == pytest.approx(1.5)
    assert e.accounting_status == "accounted"


def test_buy_lost_accounted():
    e = entry(
        price=0.60,
        quantity=100,
        resolution_status="lost",
        is_winning_trade=0,
        winning_token_id="no-token",
    )
    assert e.cost_basis == pytest.approx(60)
    assert e.payout == pytest.approx(0)
    assert e.realized_pnl == pytest.approx(-60)
    assert e.roi == pytest.approx(-1.0)
    assert e.accounting_status == "accounted"


@pytest.mark.parametrize(
    ("status", "price", "expected_pnl", "expected_roi"),
    [
        ("won", 0.0, 100.0, None),
        ("won", 1.0, 0.0, 0.0),
        ("lost", 0.0, -0.0, None),
        ("lost", 1.0, -100.0, -1.0),
    ],
)
def test_boundary_prices(status, price, expected_pnl, expected_roi):
    e = entry(price=price, quantity=100, resolution_status=status)
    assert e.cost_basis == pytest.approx(price * 100)
    assert e.realized_pnl == pytest.approx(expected_pnl)
    assert e.roi == expected_roi
    assert e.accounting_status == "accounted"


@pytest.mark.parametrize(
    ("status", "accounting_status"),
    [
        ("ambiguous", "excluded_ambiguous"),
        ("unknown", "excluded_unknown"),
        ("unresolved", "excluded_unresolved"),
    ],
)
def test_non_won_lost_statuses_excluded(status, accounting_status):
    e = entry(resolution_status=status)
    assert e.payout is None
    assert e.realized_pnl is None
    assert e.roi is None
    assert e.accounting_status == accounting_status


def test_missing_token_excluded():
    e = entry(token_id=None, resolution_status="won")
    assert e.accounting_status == "excluded_missing_token"
    assert e.realized_pnl is None


@pytest.mark.parametrize(
    ("price", "accounting_status"),
    [
        (None, "excluded_missing_price"),
        (math.nan, "excluded_invalid_price"),
        (math.inf, "excluded_invalid_price"),
        (-0.01, "excluded_invalid_price"),
        (1.01, "excluded_invalid_price"),
    ],
)
def test_missing_or_invalid_price_excluded(price, accounting_status):
    e = entry(price=price, resolution_status="won")
    assert e.accounting_status == accounting_status
    assert e.realized_pnl is None


@pytest.mark.parametrize(
    ("quantity", "accounting_status"),
    [
        (None, "excluded_missing_quantity"),
        (math.nan, "excluded_invalid_quantity"),
        (math.inf, "excluded_invalid_quantity"),
        (-1, "excluded_invalid_quantity"),
    ],
)
def test_missing_or_invalid_quantity_excluded(quantity, accounting_status):
    e = entry(quantity=quantity, resolution_status="won")
    assert e.accounting_status == accounting_status
    assert e.realized_pnl is None


def test_sell_excluded_with_explicit_reason():
    e = entry(side="SELL", resolution_status="won")
    assert e.accounting_status == "excluded_unsupported_side"
    assert e.accounting_reason == SELL_UNSUPPORTED_REASON
    assert e.realized_pnl is None


def test_same_market_multi_fill_aggregation():
    entries = [
        entry(id="t1", price=0.40, quantity=50, resolution_status="won"),
        entry(id="t2", price=0.55, quantity=50, resolution_status="won"),
    ]
    summary = aggregate_accounting_entries(entries)
    assert summary.total_realized_pnl == pytest.approx(52.5)
    assert summary.total_cost_basis == pytest.approx(47.5)
    assert summary.roi == pytest.approx(52.5 / 47.5)


def test_mixed_outcome_aggregation():
    entries = [
        entry(id="won", price=0.40, quantity=100, resolution_status="won"),
        entry(
            id="lost",
            price=0.60,
            quantity=100,
            resolution_status="lost",
            is_winning_trade=0,
        ),
        entry(id="amb", resolution_status="ambiguous"),
        entry(id="unk", resolution_status="unknown"),
        entry(id="unres", resolution_status="unresolved"),
        entry(id="missing", token_id=None, resolution_status="won"),
    ]
    summary = aggregate_accounting_entries(entries)
    assert summary.accounted_trades == 2
    assert summary.excluded_trades == 4
    assert summary.total_realized_pnl == pytest.approx(0)
    assert summary.gross_profit == pytest.approx(60)
    assert summary.gross_loss == pytest.approx(60)
    assert summary.profit_factor == pytest.approx(1)
    assert summary.win_rate == pytest.approx(0.5)


def test_drawdown_and_loss_streak_use_input_order():
    entries = [
        entry(id="t1", price=0.0, quantity=10, resolution_status="won"),
        entry(id="t2", price=0.5, quantity=10, resolution_status="lost"),
        entry(id="t3", price=0.5, quantity=10, resolution_status="lost"),
        entry(id="t4", price=0.7, quantity=10, resolution_status="won"),
        entry(id="t5", price=0.2, quantity=10, resolution_status="lost"),
    ]
    summary = aggregate_accounting_entries(entries)
    assert [e.realized_pnl for e in entries] == [10, -5, -5, 3, -2]
    assert summary.max_loss_streak == 2
    assert summary.max_drawdown == pytest.approx(10)
