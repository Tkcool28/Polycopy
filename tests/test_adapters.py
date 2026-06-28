"""Tests for provider interfaces and adapters."""

import pytest

from polycopy.adapters.sample import (
    SampleMarketDataProvider,
    SampleResolutionProvider,
    SampleTradeFeedProvider,
    SampleWalletDataProvider,
)
from polycopy.adapters.bullpen import BullpenReadOnlyAdapter
from polycopy.adapters.paper_broker import PaperBroker
from polycopy.adapters.disabled_live_broker import DisabledLiveBroker
from polycopy.risk.gates import PaperMode
from polycopy.adapters.snapshot_provenance import SnapshotProvenance
from polycopy.adapters.polymarket import PolymarketPublicAdapter
from polycopy.domain.order import OrderSide, OrderType

# Fixed UUIDs for PaperBroker tests
_M1 = "00000000-0000-0000-0000-000000000001"
_W1 = "00000000-0000-0000-0000-000000000002"


class TestSampleAdapters:
    """Sample adapters always return is_sample=True data."""

    @pytest.mark.asyncio
    async def test_sample_wallet(self):
        provider = SampleWalletDataProvider()
        wallet = await provider.get_wallet("any")
        assert wallet.is_sample is True
        assert "SAMPLE" in wallet.address

    @pytest.mark.asyncio
    async def test_sample_market(self):
        provider = SampleMarketDataProvider()
        market = await provider.get_market("sample-market-001")
        assert market is not None
        assert market.is_sample is True
        assert "SAMPLE" in market.question

    @pytest.mark.asyncio
    async def test_sample_market_not_found(self):
        provider = SampleMarketDataProvider()
        market = await provider.get_market("nonexistent")
        assert market is None

    @pytest.mark.asyncio
    async def test_sample_trades(self):
        from datetime import datetime, timezone
        provider = SampleTradeFeedProvider()
        trades = await provider.get_recent_trades("any", datetime.now(timezone.utc))
        assert len(trades) == 1
        assert trades[0].is_sample is True

    @pytest.mark.asyncio
    async def test_sample_resolution_returns_none(self):
        provider = SampleResolutionProvider()
        result = await provider.check_resolution("any")
        assert result is None


class TestPolymarketResolutionProvider:
    """Resolution adapter only returns confirmed, valid resolutions."""

    @staticmethod
    def _payload(**overrides):
        payload = {
            "conditionId": "m1",
            "question": "Will it resolve?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["1", "0"]',
            "active": False,
            "closed": True,
            "resolved": True,
            "resolutionOutcome": "Yes",
        }
        payload.update(overrides)
        return payload

    async def _check(self, payload):
        class _Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return payload

        class _Client:
            is_closed = False

            async def get(self, _path):
                return _Response()

        adapter = PolymarketPublicAdapter("https://gamma.example", "https://clob.example")
        adapter._gamma_client = _Client()  # noqa: SLF001 - injected fake gamma client (P21: was _client)
        return await adapter.check_resolution("m1")

    @pytest.mark.asyncio
    async def test_open_market_cannot_resolve(self):
        result = await self._check(self._payload(active=True, closed=False, resolved=False, resolutionOutcome=None))
        assert result is None

    @pytest.mark.asyncio
    async def test_resolved_with_valid_outcome_returns_market(self):
        result = await self._check(self._payload())
        assert result is not None
        assert result.resolution_outcome == "Yes"

    @pytest.mark.asyncio
    async def test_disputed_or_missing_outcome_cannot_settle(self):
        assert await self._check(self._payload(disputed=True)) is None
        assert await self._check(self._payload(resolutionOutcome=None)) is None
        assert await self._check(self._payload(resolutionOutcome="Maybe")) is None


class TestBullpenSkeleton:
    """Bullpen adapter raises NotImplementedError for all methods."""

    def test_construction_warns(self):
        adapter = BullpenReadOnlyAdapter()
        assert adapter is not None

    @pytest.mark.asyncio
    async def test_get_wallet_raises(self):
        adapter = BullpenReadOnlyAdapter()
        with pytest.raises(NotImplementedError):
            await adapter.get_wallet("any")

    @pytest.mark.asyncio
    async def test_list_markets_raises(self):
        adapter = BullpenReadOnlyAdapter()
        with pytest.raises(NotImplementedError):
            await adapter.list_active_markets()


class TestPaperBroker:
    """PaperBroker simulates trades without real execution."""

    @pytest.mark.asyncio
    async def test_place_order_fills(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        order = await broker.place_order(
            market_id=_M1,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            outcome="Yes",
            quantity=10.0,
            price=0.65,
            wallet_id=_W1,
        )
        assert order.quantity == 10.0
        assert order.filled_quantity == 10.0
        assert broker.is_live is False

    @pytest.mark.asyncio
    async def test_position_created_on_buy(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(_M1, OrderSide.BUY, OrderType.MARKET, "Yes", 10.0, 0.60, _W1)
        pos = await broker.get_position(_M1, _W1, "Yes")
        assert pos is not None
        assert pos.quantity == 10.0
        assert pos.avg_entry_price == pytest.approx(0.60)

    @pytest.mark.asyncio
    async def test_position_avg_price_on_second_buy(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(_M1, OrderSide.BUY, OrderType.MARKET, "Yes", 10.0, 0.60, _W1)
        await broker.place_order(_M1, OrderSide.BUY, OrderType.MARKET, "Yes", 10.0, 0.70, _W1)
        pos = await broker.get_position(_M1, _W1, "Yes")
        assert pos is not None
        assert pos.quantity == 20.0
        assert pos.avg_entry_price == pytest.approx(0.65)  # (10*0.60 + 10*0.70) / 20

    @pytest.mark.asyncio
    async def test_sell_closes_position(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(_M1, OrderSide.BUY, OrderType.MARKET, "Yes", 10.0, 0.60, _W1)
        await broker.place_order(_M1, OrderSide.SELL, OrderType.MARKET, "Yes", 10.0, 0.70, _W1)
        pos = await broker.get_position(_M1, _W1, "Yes")
        assert pos is None  # fully closed

    @pytest.mark.asyncio
    async def test_list_positions(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(_M1, OrderSide.BUY, OrderType.MARKET, "Yes", 10.0, 0.60, _W1)
        positions = await broker.list_positions(_W1)
        assert len(positions) == 1


class TestDisabledLiveBroker:
    """DisabledLiveBroker raises on every operation — fail-closed guarantee."""

    def test_is_live_false(self):
        broker = DisabledLiveBroker()
        assert broker.is_live is False

    @pytest.mark.asyncio
    async def test_place_order_raises(self):
        broker = DisabledLiveBroker()
        with pytest.raises(RuntimeError, match="LIVE EXECUTION IS DISABLED"):
            await broker.place_order("m1", OrderSide.BUY, OrderType.MARKET, "Yes", 10.0, 0.5, "w1")

    @pytest.mark.asyncio
    async def test_cancel_raises(self):
        broker = DisabledLiveBroker()
        with pytest.raises(RuntimeError, match="LIVE EXECUTION IS DISABLED"):
            await broker.cancel_order("any")

    @pytest.mark.asyncio
    async def test_list_positions_raises(self):
        broker = DisabledLiveBroker()
        with pytest.raises(RuntimeError, match="LIVE EXECUTION IS DISABLED"):
            await broker.list_positions("w1")


class TestSnapshotProvenance:
    """Snapshot provenance saves files and verifies hashes."""

    def test_save_and_verify(self, tmp_path):
        provenance = SnapshotProvenance(snapshot_dir=tmp_path / "snaps")
        snapshot = provenance.save(
            source="polymarket_gamma",
            endpoint="/markets",
            data={"markets": [{"id": "abc"}]},
            query_params={"active": "true"},
        )
        assert snapshot.content_hash != ""
        assert snapshot.size_bytes > 0
        assert snapshot.is_sample is False

        # Verify
        assert provenance.verify(snapshot) is True

    def test_save_sample_data(self, tmp_path):
        provenance = SnapshotProvenance(snapshot_dir=tmp_path / "snaps")
        snapshot = provenance.save(
            source="sample",
            endpoint="/fixtures",
            data={"sample": True},
            is_sample=True,
        )
        assert snapshot.is_sample is True

    def test_corrupt_file_fails_verify(self, tmp_path):
        provenance = SnapshotProvenance(snapshot_dir=tmp_path / "snaps")
        snapshot = provenance.save(
            source="test",
            endpoint="/test",
            data={"key": "value"},
        )
        # Corrupt the file
        file_path = tmp_path / "snaps" / snapshot.file_path
        file_path.write_bytes(b"corrupted")

        assert provenance.verify(snapshot) is False

    def test_missing_file_fails_verify(self, tmp_path):
        from polycopy.domain.raw_snapshot import RawSnapshot
        from datetime import datetime, timezone

        provenance = SnapshotProvenance(snapshot_dir=tmp_path / "snaps")
        snapshot = RawSnapshot(
            source="test",
            endpoint="/test",
            file_path="nonexistent/file.json",
            content_hash="abc123",
            size_bytes=10,
            fetched_at=datetime.now(timezone.utc),
            ingested_at=datetime.now(timezone.utc),
        )
        assert provenance.verify(snapshot) is False
