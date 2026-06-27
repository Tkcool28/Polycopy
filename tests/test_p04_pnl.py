"""Tests for P04 P&L tracker — FIFO position closing."""

import pytest
from uuid import uuid4

from polycopy.risk.pnl import (
    PnlTracker,
)


class TestPnlTracker:
    def test_single_buy_no_pnl(self):
        tracker = PnlTracker()
        wid = uuid4()
        mid = uuid4()
        tracker.record_buy(wid, mid, "Yes", 100.0, 0.60)
        assert tracker.get_realized_pnl(wid) == 0.0

    def test_buy_and_sell_full_profit(self):
        tracker = PnlTracker()
        wid = uuid4()
        mid = uuid4()
        tracker.record_buy(wid, mid, "Yes", 100.0, 0.60)
        events = tracker.record_sell(wid, mid, "Yes", 100.0, 0.70)
        assert len(events) == 1
        assert events[0].pnl == pytest.approx(10.0)  # (0.70 - 0.60) * 100
        assert tracker.get_realized_pnl(wid) == pytest.approx(10.0)

    def test_buy_and_sell_full_loss(self):
        tracker = PnlTracker()
        wid = uuid4()
        mid = uuid4()
        tracker.record_buy(wid, mid, "Yes", 100.0, 0.60)
        events = tracker.record_sell(wid, mid, "Yes", 100.0, 0.50)
        assert events[0].pnl == pytest.approx(-10.0)

    def test_fifo_multiple_lots(self):
        """Sell consumes oldest lots first."""
        tracker = PnlTracker()
        wid = uuid4()
        mid = uuid4()
        tracker.record_buy(wid, mid, "Yes", 10.0, 0.50)  # lot 1
        tracker.record_buy(wid, mid, "Yes", 10.0, 0.70)  # lot 2

        # Sell 15 shares: consumes lot 1 (10@0.50) + 5 from lot 2 (5@0.70)
        events = tracker.record_sell(wid, mid, "Yes", 15.0, 0.80)
        assert len(events) == 2
        # lot 1: (0.80 - 0.50) * 10 = 3.0
        assert events[0].pnl == pytest.approx(3.0)
        # lot 2: (0.80 - 0.70) * 5 = 0.5
        assert events[1].pnl == pytest.approx(0.5)
        # Total realized: 3.5
        assert tracker.get_realized_pnl(wid) == pytest.approx(3.5)

    def test_fifo_partial_lot(self):
        """Sell partially consumes a lot, leaving remainder."""
        tracker = PnlTracker()
        wid = uuid4()
        mid = uuid4()
        tracker.record_buy(wid, mid, "Yes", 20.0, 0.60)
        events = tracker.record_sell(wid, mid, "Yes", 5.0, 0.70)
        assert len(events) == 1
        assert events[0].pnl == pytest.approx(0.5)  # (0.70 - 0.60) * 5
        # Remaining: 15 shares at 0.60
        assert tracker.get_open_quantity(wid, mid, "Yes") == 15.0

    def test_open_cost_basis(self):
        tracker = PnlTracker()
        wid = uuid4()
        mid = uuid4()
        tracker.record_buy(wid, mid, "Yes", 10.0, 0.50)
        tracker.record_buy(wid, mid, "Yes", 10.0, 0.70)
        assert tracker.get_open_cost_basis(wid, mid, "Yes") == pytest.approx(12.0)

    def test_sell_no_lots_returns_empty(self):
        tracker = PnlTracker()
        events = tracker.record_sell(uuid4(), uuid4(), "Yes", 10.0, 0.50)
        assert events == []

    def test_snapshot(self):
        tracker = PnlTracker()
        wid = uuid4()
        mid = uuid4()
        tracker.record_buy(wid, mid, "Yes", 100.0, 0.60)
        tracker.record_sell(wid, mid, "Yes", 50.0, 0.70)

        snapshot = tracker.snapshot(wid)
        assert snapshot.realized_pnl == pytest.approx(5.0)  # (0.70-0.60)*50
        assert snapshot.unrealized_pnl == pytest.approx(0.0)  # no mark prices
        assert snapshot.event_count == 1

    def test_snapshot_with_mark(self):
        tracker = PnlTracker()
        wid = uuid4()
        mid = uuid4()
        tracker.record_buy(wid, mid, "Yes", 100.0, 0.60)
        mark_prices = {(mid, "Yes"): 0.70}

        snapshot = tracker.snapshot(wid, mark_prices=mark_prices)
        assert snapshot.unrealized_pnl == pytest.approx(10.0)  # (0.70-0.60)*100
        assert snapshot.total_pnl == pytest.approx(10.0)

    def test_get_events(self):
        tracker = PnlTracker()
        wid = uuid4()
        mid = uuid4()
        tracker.record_buy(wid, mid, "Yes", 10.0, 0.50)
        tracker.record_sell(wid, mid, "Yes", 10.0, 0.60)
        events = tracker.get_events(wid)
        assert len(events) == 1
        assert events[0].event_type == "realized"

    def test_sample_flag(self):
        tracker = PnlTracker()
        wid = uuid4()
        mid = uuid4()
        tracker.record_buy(wid, mid, "Yes", 10.0, 0.50, is_sample=True)
        tracker.record_sell(wid, mid, "Yes", 10.0, 0.60, is_sample=True)
        events = tracker.get_events(wid)
        assert events[0].is_sample is True

    def test_wallet_count(self):
        tracker = PnlTracker()
        tracker.record_buy(uuid4(), uuid4(), "Yes", 1.0, 0.5)
        tracker.record_buy(uuid4(), uuid4(), "Yes", 1.0, 0.5)
        assert tracker.wallet_count == 2

    def test_close_position_removes_lots(self):
        tracker = PnlTracker()
        wid = uuid4()
        mid = uuid4()
        tracker.record_buy(wid, mid, "Yes", 10.0, 0.50)
        tracker.record_sell(wid, mid, "Yes", 10.0, 0.60)
        assert tracker.get_open_quantity(wid, mid, "Yes") == 0.0
