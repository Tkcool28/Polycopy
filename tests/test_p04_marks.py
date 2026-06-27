"""Tests for P04 marks — mark-to-market pricing."""

import pytest
from uuid import uuid4

from polycopy.risk.marks import (
    MarkEngine,
    MarkPrice,
    PositionMark,
)


class TestMarkPrice:
    def test_spread(self):
        m = MarkPrice(
            market_id=uuid4(),
            outcome="Yes",
            mark_price=0.65,
            bid_price=0.63,
            ask_price=0.67,
        )
        assert m.spread == pytest.approx(0.04)

    def test_mid_price(self):
        m = MarkPrice(
            market_id=uuid4(),
            outcome="Yes",
            mark_price=0.65,
            bid_price=0.63,
            ask_price=0.67,
        )
        assert m.mid_price == pytest.approx(0.65)


class TestPositionMark:
    def test_unrealized_pnl(self):
        pm = PositionMark(
            position_id=uuid4(),
            market_id=uuid4(),
            wallet_id=uuid4(),
            outcome="Yes",
            quantity=100.0,
            avg_entry_price=0.60,
            mark_price=0.70,
        )
        assert pm.unrealized_pnl == pytest.approx(10.0)

    def test_cost_basis(self):
        pm = PositionMark(
            position_id=uuid4(),
            market_id=uuid4(),
            wallet_id=uuid4(),
            outcome="Yes",
            quantity=100.0,
            avg_entry_price=0.60,
            mark_price=0.70,
        )
        assert pm.cost_basis == pytest.approx(60.0)

    def test_market_value(self):
        pm = PositionMark(
            position_id=uuid4(),
            market_id=uuid4(),
            wallet_id=uuid4(),
            outcome="Yes",
            quantity=100.0,
            avg_entry_price=0.60,
            mark_price=0.70,
        )
        assert pm.market_value == pytest.approx(70.0)

    def test_return_pct(self):
        pm = PositionMark(
            position_id=uuid4(),
            market_id=uuid4(),
            wallet_id=uuid4(),
            outcome="Yes",
            quantity=100.0,
            avg_entry_price=0.60,
            mark_price=0.80,
        )
        # (80-60)/60 * 100 = 33.33%
        assert pm.return_pct == pytest.approx(33.33, abs=0.01)


class TestMarkEngine:
    def test_update_and_get(self):
        engine = MarkEngine()
        mid = uuid4()
        mark = MarkPrice(
            market_id=mid,
            outcome="Yes",
            mark_price=0.65,
            bid_price=0.63,
            ask_price=0.67,
            source="test",
        )
        engine.update_price(mark)
        result = engine.get_mark(mid, "Yes")
        assert result is not None
        assert result.mark_price == 0.65

    def test_mark_none_returns_none(self):
        engine = MarkEngine()
        assert engine.get_mark(uuid4(), "Yes") is None

    def test_mark_position_mid_price(self):
        engine = MarkEngine(use_conservative_mark=False)
        mid = uuid4()
        wid = uuid4()
        engine.update_price(MarkPrice(
            market_id=mid,
            outcome="Yes",
            mark_price=0.65,
            bid_price=0.63,
            ask_price=0.67,
        ))
        pm = engine.mark_position(
            position_id=uuid4(),
            market_id=mid,
            wallet_id=wid,
            outcome="Yes",
            quantity=100.0,
            avg_entry_price=0.60,
        )
        assert pm is not None
        assert pm.mark_price == 0.65  # mid price

    def test_mark_position_conservative(self):
        engine = MarkEngine(use_conservative_mark=True)
        mid = uuid4()
        engine.update_price(MarkPrice(
            market_id=mid,
            outcome="Yes",
            mark_price=0.65,
            bid_price=0.63,
            ask_price=0.67,
        ))
        pm = engine.mark_position(
            position_id=uuid4(),
            market_id=mid,
            wallet_id=uuid4(),
            outcome="Yes",
            quantity=100.0,
            avg_entry_price=0.60,
        )
        assert pm is not None
        assert pm.mark_price == 0.63  # bid (conservative)

    def test_mark_position_no_price(self):
        engine = MarkEngine()
        pm = engine.mark_position(
            position_id=uuid4(),
            market_id=uuid4(),
            wallet_id=uuid4(),
            outcome="Yes",
            quantity=100.0,
            avg_entry_price=0.60,
        )
        assert pm is None

    def test_list_marks(self):
        engine = MarkEngine()
        engine.update_price(MarkPrice(
            market_id=uuid4(), outcome="Yes",
            mark_price=0.5, bid_price=0.49, ask_price=0.51,
        ))
        engine.update_price(MarkPrice(
            market_id=uuid4(), outcome="No",
            mark_price=0.4, bid_price=0.39, ask_price=0.41,
        ))
        assert engine.mark_count == 2

    def test_overwrite_price(self):
        engine = MarkEngine()
        mid = uuid4()
        engine.update_price(MarkPrice(
            market_id=mid, outcome="Yes",
            mark_price=0.5, bid_price=0.49, ask_price=0.51,
        ))
        engine.update_price(MarkPrice(
            market_id=mid, outcome="Yes",
            mark_price=0.6, bid_price=0.59, ask_price=0.61,
        ))
        assert engine.mark_count == 1
        assert engine.get_mark(mid, "Yes").mark_price == 0.6
