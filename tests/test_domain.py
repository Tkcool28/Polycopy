"""Tests for domain models."""

import uuid
from datetime import datetime, timezone

import pytest

from polycopy.domain.wallet import Wallet, WalletBalance
from polycopy.domain.market import Market, MarketOutcome
from polycopy.domain.signal import Signal, SignalStrength
from polycopy.domain.order import Order, OrderSide, OrderStatus, OrderType
from polycopy.domain.position import Position
from polycopy.domain.source_trade import SourceTrade
from polycopy.domain.decision_log import DecisionLogEntry
from polycopy.domain.experiment import ExperimentRun, ExperimentStatus
from polycopy.domain.raw_snapshot import RawSnapshot


UTC = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TestWallet:
    def test_create(self):
        w = Wallet(address="0xabc", label="test")
        assert w.address == "0xabc"
        assert w.label == "test"
        assert w.is_sample is False

    def test_empty_address_rejected(self):
        with pytest.raises(ValueError, match="address"):
            Wallet(address="   ")

    def test_sample_wallet(self):
        w = Wallet(address="0xsamp", is_sample=True)
        assert w.is_sample is True

    def test_balance(self):
        b = WalletBalance(currency="USDC", amount=100.0, as_of=UTC, is_sample=True)
        assert b.is_sample is True
        assert b.amount == 100.0


class TestMarket:
    def test_create(self):
        m = Market(
            source_id="cond-123",
            question="Will X happen?",
            source="polymarket",
            fetched_at=UTC,
        )
        assert m.active is True
        assert m.is_sample is False

    def test_outcomes(self):
        m = Market(
            source_id="cond-123",
            question="Test?",
            outcomes=[
                MarketOutcome(label="Yes", price=0.7),
                MarketOutcome(label="No", price=0.3),
            ],
            source="sample",
            fetched_at=UTC,
            is_sample=True,
        )
        assert len(m.outcomes) == 2
        assert m.outcomes[0].price == 0.7


class TestSignal:
    def test_create(self):
        s = Signal(
            market_id=uuid.uuid4(),
            source="test_model",
            strength=SignalStrength.BUY,
            confidence=0.8,
            edge_estimate=0.1,
            predicted_prob=0.75,
            market_prob=0.65,
            produced_at=UTC,
        )
        assert s.strength == SignalStrength.BUY


class TestOrder:
    def test_create(self):
        o = Order(
            market_id=uuid.uuid4(),
            wallet_id=uuid.uuid4(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            outcome="Yes",
            quantity=10.0,
            price=0.65,
            created_at=UTC,
        )
        assert o.status == OrderStatus.PENDING
        assert o.filled_quantity == 0.0


class TestPosition:
    def test_create_with_properties(self):
        p = Position(
            market_id=uuid.uuid4(),
            wallet_id=uuid.uuid4(),
            outcome="Yes",
            quantity=100.0,
            avg_entry_price=0.60,
            current_price=0.70,
            opened_at=UTC,
        )
        assert p.unrealized_pnl == pytest.approx(10.0)  # (0.70 - 0.60) * 100
        assert p.cost_basis == pytest.approx(60.0)
        assert p.market_value == pytest.approx(70.0)


class TestSourceTrade:
    def test_create(self):
        t = SourceTrade(
            source="polymarket_clob",
            source_trade_id="trade-123",
            market_source_id="cond-123",
            side=OrderSide.BUY,
            outcome="Yes",
            quantity=5.0,
            price=0.55,
            trader_address="0xtrader",
            timestamp=UTC,
            is_sample=True,
        )
        assert t.is_sample is True


class TestDecisionLogEntry:
    def test_create(self):
        d = DecisionLogEntry(
            wallet_id=uuid.uuid4(),
            market_id=uuid.uuid4(),
            decision_type="open_position",
            created_at=UTC,
        )
        assert d.rationale == ""


class TestExperimentRun:
    def test_create(self):
        e = ExperimentRun(label="backtest_v1")
        assert e.status == ExperimentStatus.PENDING


class TestRawSnapshot:
    def test_create(self):
        r = RawSnapshot(
            source="polymarket_gamma",
            endpoint="/markets",
            file_path="polymarket_gamma/2026-01-01/abc123.json",
            content_hash="abc123def456",
            size_bytes=1024,
            fetched_at=UTC,
            ingested_at=UTC,
        )
        assert r.hash_algo == "sha256"
        assert r.is_sample is False
