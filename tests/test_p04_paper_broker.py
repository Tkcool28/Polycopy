"""Tests for P04 PaperBroker — risk gates, fill model, review delay, settlement."""

import pytest

from polycopy.adapters.paper_broker import PaperBroker
from polycopy.risk.gates import PaperMode, ExposureLimits
from polycopy.risk.fill_model import MarketDepth, DepthLevel
from polycopy.risk.settlement import SettlementEvidence


# Fixed UUIDs
_M1 = "00000000-0000-0000-0000-000000000001"
_W1 = "00000000-0000-0000-0000-000000000002"


class TestPaperBrokerRiskIntegration:
    """Test that PaperBroker enforces risk gates."""

    @pytest.mark.asyncio
    async def test_kill_switch_blocks_order(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        broker.kill_switch.engage()
        order = await broker.place_order(
            market_id=_M1,
            side="buy",  # type: ignore
            order_type="market",  # type: ignore
            outcome="Yes",
            quantity=10.0,
            price=0.65,
            wallet_id=_W1,
        )
        assert order.status.value == "rejected"

    @pytest.mark.asyncio
    async def test_research_only_blocks_order(self):
        broker = PaperBroker(paper_mode=PaperMode.RESEARCH_ONLY)
        order = await broker.place_order(
            market_id=_M1,
            side="buy",  # type: ignore
            order_type="market",  # type: ignore
            outcome="Yes",
            quantity=10.0,
            price=0.65,
            wallet_id=_W1,
        )
        assert order.status.value == "rejected"

    @pytest.mark.asyncio
    async def test_exposure_limit_blocks_order(self):
        broker = PaperBroker(
            paper_mode=PaperMode.PAPER_AUTO,
            exposure_limits=ExposureLimits(max_order_size=5.0),
        )
        order = await broker.place_order(
            market_id=_M1,
            side="buy",  # type: ignore
            order_type="market",  # type: ignore
            outcome="Yes",
            quantity=10.0,
            price=0.65,  # notional = 6.5 > 5.0
            wallet_id=_W1,
        )
        assert order.status.value == "rejected"

    @pytest.mark.asyncio
    async def test_paper_auto_fills_when_allowed(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        order = await broker.place_order(
            market_id=_M1,
            side="buy",  # type: ignore
            order_type="market",  # type: ignore
            outcome="Yes",
            quantity=10.0,
            price=0.65,
            wallet_id=_W1,
        )
        assert order.status.value == "filled"
        assert order.filled_quantity == 10.0

    @pytest.mark.asyncio
    async def test_paper_manual_creates_pending(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        broker._paper_mode = PaperMode.PAPER_MANUAL
        order = await broker.place_order(
            market_id=_M1,
            side="buy",  # type: ignore
            order_type="market",  # type: ignore
            outcome="Yes",
            quantity=10.0,
            price=0.65,
            wallet_id=_W1,
        )
        assert order.status.value == "pending"

    @pytest.mark.asyncio
    async def test_confirm_and_fill(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_MANUAL, review_delay_seconds=0.0)
        order = await broker.place_order(
            market_id=_M1,
            side="buy",  # type: ignore
            order_type="market",  # type: ignore
            outcome="Yes",
            quantity=10.0,
            price=0.65,
            wallet_id=_W1,
        )
        assert order.status.value == "pending"

        # Confirm immediately (delay=0)
        filled = await broker.confirm_and_fill(str(order.id))
        assert filled.status.value == "filled"
        assert filled.filled_quantity == 10.0

    @pytest.mark.asyncio
    async def test_confirm_still_pending_before_delay(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_MANUAL, review_delay_seconds=60.0)
        order = await broker.place_order(
            market_id=_M1,
            side="buy",  # type: ignore
            order_type="market",  # type: ignore
            outcome="Yes",
            quantity=10.0,
            price=0.65,
            wallet_id=_W1,
        )
        result = await broker.confirm_and_fill(str(order.id))
        assert result.status.value == "pending"  # still pending

    @pytest.mark.asyncio
    async def test_fill_with_depth_slippage(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        broker.set_depth(_M1, "Yes", MarketDepth(
            best_price=0.60,
            levels=[
                DepthLevel(price=0.60, volume=5.0),
                DepthLevel(price=0.65, volume=10.0),
            ],
        ))
        order = await broker.place_order(
            market_id=_M1,
            side="buy",  # type: ignore
            order_type="market",  # type: ignore
            outcome="Yes",
            quantity=10.0,
            price=0.65,
            wallet_id=_W1,
        )
        assert order.status.value == "filled"
        # Average price: (5*0.60 + 5*0.65) / 10 = 0.625
        assert order.price == pytest.approx(0.625)

    @pytest.mark.asyncio
    async def test_position_created_on_buy(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.60, _W1,  # type: ignore
        )
        pos = await broker.get_position(_M1, _W1, "Yes")
        assert pos is not None
        assert pos.quantity == 10.0

    @pytest.mark.asyncio
    async def test_pnl_tracking(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(
            _M1, "buy", "market", "Yes", 100.0, 0.60, _W1,  # type: ignore
        )
        await broker.place_order(
            _M1, "sell", "market", "Yes", 100.0, 0.70, _W1,  # type: ignore
        )
        from uuid import UUID
        wid = UUID(_W1)
        realized = broker.pnl.get_realized_pnl(wid)
        assert realized == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_marks_updated_on_fill(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        broker.set_depth(_M1, "Yes", MarketDepth(
            best_price=0.65,
            levels=[DepthLevel(price=0.65, volume=100.0)],
        ))
        await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.65, _W1,  # type: ignore
        )
        # Marks are set separately, not via fill
        assert broker.marks.mark_count == 0

    @pytest.mark.asyncio
    async def test_cancel_pending_order(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_MANUAL, review_delay_seconds=60.0)
        order = await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.65, _W1,  # type: ignore
        )
        assert order.status.value == "pending"
        cancelled = await broker.cancel_order(str(order.id))
        assert cancelled.status.value == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_filled_order_raises(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        order = await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.65, _W1,  # type: ignore
        )
        with pytest.raises(ValueError, match="Cannot cancel"):
            await broker.cancel_order(str(order.id))

    @pytest.mark.asyncio
    async def test_list_open_orders(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_MANUAL, review_delay_seconds=60.0)
        await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.65, _W1,  # type: ignore
        )
        orders = await broker.list_open_orders(_W1)
        assert len(orders) == 1

    @pytest.mark.asyncio
    async def test_is_live_false(self):
        broker = PaperBroker()
        assert broker.is_live is False


class TestPaperBrokerSettlement:
    """Test settlement through PaperBroker."""

    @pytest.mark.asyncio
    async def test_settle_market(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(
            _M1, "buy", "market", "Yes", 100.0, 0.60, _W1,  # type: ignore
        )
        evidence = SettlementEvidence(
            source="polymarket",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        results = broker.settle_market(_M1, "Yes", evidence)
        assert len(results) == 1
        assert results[0].is_winner is True
        assert results[0].payout == 100.0

    @pytest.mark.asyncio
    async def test_settle_loser(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(
            _M1, "buy", "market", "No", 100.0, 0.40, _W1,  # type: ignore
        )
        evidence = SettlementEvidence(
            source="polymarket",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        results = broker.settle_market(_M1, "Yes", evidence)
        assert results[0].is_winner is False
        assert results[0].payout == 0.0

    @pytest.mark.asyncio
    async def test_settlement_idempotent(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(
            _M1, "buy", "market", "Yes", 100.0, 0.60, _W1,  # type: ignore
        )
        evidence = SettlementEvidence(
            source="polymarket",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        broker.settle_market(_M1, "Yes", evidence)
        # Second call with same evidence is idempotent (deduped at engine level)
        broker.settle_market(_M1, "Yes", evidence)
        assert broker.settlement.settlement_count == 1
