"""Tests for P04 fill model — bid/ask, depth, slippage, fees, review delay."""

import pytest
from datetime import datetime, timezone, timedelta

from polycopy.risk.fill_model import (
    DepthLevel,
    FillModel,
    MarketDepth,
    ReviewDelay,
)


class TestMarketDepth:
    def test_total_volume(self):
        d = MarketDepth(
            best_price=0.65,
            levels=[
                DepthLevel(price=0.65, volume=100.0),
                DepthLevel(price=0.64, volume=200.0),
            ],
        )
        assert d.total_volume == 300.0

    def test_empty_depth(self):
        d = MarketDepth(best_price=0.5)
        assert d.total_volume == 0.0

    def test_volume_at_price(self):
        d = MarketDepth(
            best_price=0.65,
            levels=[
                DepthLevel(price=0.65, volume=100.0),
                DepthLevel(price=0.64, volume=200.0),
                DepthLevel(price=0.63, volume=300.0),
            ],
        )
        assert d.volume_at_price(0.64) == 300.0  # 0.65 + 0.64

    def test_volume_up_to(self):
        d = MarketDepth(
            best_price=0.35,
            levels=[
                DepthLevel(price=0.35, volume=100.0),
                DepthLevel(price=0.36, volume=200.0),
                DepthLevel(price=0.37, volume=300.0),
            ],
        )
        assert d.volume_up_to(0.36) == 300.0  # 0.35 + 0.36


class TestFillModel:
    def test_simple_fill_no_slippage(self):
        model = FillModel(default_fee_rate=0.001)
        depth = MarketDepth(
            best_price=0.65,
            levels=[DepthLevel(price=0.65, volume=1000.0)],
        )
        quote = model.quote_fill(side="buy", quantity=10.0, depth=depth)
        assert quote.expected_price == 0.65
        assert quote.slippage == 0.0
        assert quote.is_complete_fill is True
        assert quote.fillable_volume == 10.0

    def test_fill_with_slippage(self):
        """Order walks the book — average price is higher than best."""
        model = FillModel(default_fee_rate=0.0)
        depth = MarketDepth(
            best_price=0.60,
            levels=[
                DepthLevel(price=0.60, volume=5.0),
                DepthLevel(price=0.65, volume=10.0),
            ],
        )
        quote = model.quote_fill(side="buy", quantity=10.0, depth=depth)
        # (5*0.60 + 5*0.65) / 10 = 0.625
        assert quote.expected_price == pytest.approx(0.625)
        assert quote.slippage == pytest.approx(0.025)
        assert quote.is_complete_fill is True

    def test_partial_fill(self):
        """Order exceeds available depth — partial fill."""
        model = FillModel(default_fee_rate=0.0)
        depth = MarketDepth(
            best_price=0.60,
            levels=[DepthLevel(price=0.60, volume=5.0)],
        )
        quote = model.quote_fill(side="buy", quantity=10.0, depth=depth)
        assert quote.fillable_volume == 5.0
        assert quote.is_complete_fill is False

    def test_fee_calculation(self):
        model = FillModel(default_fee_rate=0.01)
        depth = MarketDepth(
            best_price=0.50,
            levels=[DepthLevel(price=0.50, volume=100.0)],
        )
        quote = model.quote_fill(side="buy", quantity=100.0, depth=depth)
        # notional = 0.50 * 100 = 50.0, fee = 50.0 * 0.01 = 0.5
        assert quote.fee == pytest.approx(0.5)
        assert quote.total_cost == pytest.approx(50.5)

    def test_zero_quantity(self):
        model = FillModel()
        depth = MarketDepth(
            best_price=0.5,
            levels=[DepthLevel(price=0.5, volume=100.0)],
        )
        quote = model.quote_fill(side="buy", quantity=0.0, depth=depth)
        assert quote.fillable_volume == 0.0
        assert quote.total_cost == 0.0

    def test_empty_depth(self):
        model = FillModel()
        depth = MarketDepth(best_price=0.5, levels=[])
        quote = model.quote_fill(side="buy", quantity=10.0, depth=depth)
        assert quote.fillable_volume == 0.0
        assert quote.total_cost == 0.0

    def test_is_sample_flag(self):
        model = FillModel()
        depth = MarketDepth(
            best_price=0.5,
            levels=[DepthLevel(price=0.5, volume=100.0)],
        )
        quote = model.quote_fill(side="buy", quantity=1.0, depth=depth, is_sample=True)
        assert quote.is_sample is True

    def test_effective_price(self):
        model = FillModel(default_fee_rate=0.01)
        depth = MarketDepth(
            best_price=0.50,
            levels=[DepthLevel(price=0.50, volume=100.0)],
        )
        quote = model.quote_fill(side="buy", quantity=100.0, depth=depth)
        # total_cost = 50.5, fillable = 100 → effective = 0.505
        assert quote.effective_price == pytest.approx(0.505)


class TestReviewDelay:
    def test_eligible_immediately_with_zero_delay(self):
        delay = ReviewDelay(delay_seconds=0.0)
        assert delay.is_eligible() is True

    def test_not_eligible_before_delay(self):
        delay = ReviewDelay(delay_seconds=60.0)
        assert delay.is_eligible() is False

    def test_eligible_after_delay(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(seconds=61)
        delay = ReviewDelay(delay_seconds=60.0, started_at=started)
        assert delay.is_eligible(now) is True

    def test_seconds_remaining(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(seconds=10)
        delay = ReviewDelay(delay_seconds=30.0, started_at=started)
        remaining = delay.seconds_remaining(now)
        assert remaining == pytest.approx(20.0, abs=1.0)

    def test_expires_at(self):
        now = datetime.now(timezone.utc)
        delay = ReviewDelay(delay_seconds=30.0, started_at=now)
        assert delay.expires_at == now + timedelta(seconds=30)
