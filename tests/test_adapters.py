"""Tests for provider interfaces and adapters."""

import json
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

        def test_missing_file_fails_verify(self, tmp_path):
            from polycopy.domain.raw_snapshot import RawSnapshot
            from datetime import datetime, timezone

            provenance = SnapshotProvenance(
                snapshot_dir=tmp_path / "snaps"
            )
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


class TestPolymarketConditionLookup:
        """get_market must route hex condition IDs to the query-param
        ``GET /markets?condition_ids=<hex>`` endpoint (which returns a
        list), not the numeric-only ``GET /markets/{id}`` path that
        422s on hex input. Numeric market IDs keep the path lookup.
        """

        COND = (
            "0x01dffa7abae7e5d9b7fb44b06d537c5ac932e2ca422ab4b53366672f5e2dc7d6"
        )
        TOK_YES = (
            "58585097107933138034126275600204468509"
        )
        TOK_NO = (
            "104431860535489654020481219089291817898241901940037260095979653681449084465327"
        )

        @staticmethod
        def _market_payload(condition_id, outcomes, clob_token_ids):
            return {
                "id": 561249,
                "question": "Will it happen?",
                "conditionId": condition_id,
                "slug": "will-it-happen",
                "active": True,
                "closed": False,
                "resolved": False,
                "resolutionOutcome": None,
                "outcomes": json.dumps(outcomes),
                "outcomePrices": json.dumps(["0.5", "0.5"]),
                "clobTokenIds": json.dumps(clob_token_ids),
                "volume24hr": 100.0,
            }

        class _Resp:
            def __init__(self, status, payload):
                self.status_code = status
                self._payload = payload

            def raise_for_status(self):
                if self.status_code >= 400:
                    from httpx import HTTPStatusError

                    raise HTTPStatusError(
                        f"Client error '{self.status_code}'",
                        request=None,
                        response=self,
                    )

            def json(self):
                return self._payload

        class _Client:
            is_closed = False

            def __init__(self, handler):
                self._handler = handler
                self.last = None

            async def get(self, path, params=None):
                self.last = (path, params)
                return self._handler(path, params)

        def _adapter(self, handler):
            a = PolymarketPublicAdapter("https://gamma.example", "https://clob.example")
            a._gamma_client = self._Client(handler)
            return a

        @pytest.mark.asyncio
        async def test_condition_id_uses_query_param(self):
            captured = {}

            def handler(path, params):
                captured["path"] = path
                captured["params"] = params
                return self._Resp(
                    200, [self._market_payload(self.COND, ["Yes", "No"],
                                              [self.TOK_YES, self.TOK_NO])]
                )

            a = self._adapter(handler)
            market = await a.get_market(self.COND)
            assert captured["path"] == "/markets"
            assert captured["params"] == {"condition_ids": self.COND}
            assert market is not None
            assert market.source_id == self.COND

        @pytest.mark.asyncio
        async def test_exact_condition_match_selected(self):
            payload = self._market_payload(
                self.COND, ["Yes", "No"], [self.TOK_YES, self.TOK_NO]
            )

            def handler(path, params):
                # An unrelated market first, then the exact match.
                return self._Resp(
                    200,
                    [
                        self._market_payload(
                            "0x" + "a" * 64, ["Up", "Down"], ["t1", "t2"]
                        ),
                        payload,
                    ],
                )

            a = self._adapter(handler)
            market = await a.get_market(self.COND)
            assert market is not None
            assert market.source_id == self.COND
            labels = [o.label for o in market.outcomes]
            assert labels == ["Yes", "No"]
            token_by_label = {o.label: o.clob_token_id for o in market.outcomes}
            assert token_by_label["Yes"] == self.TOK_YES
            assert token_by_label["No"] == self.TOK_NO

        @pytest.mark.asyncio
        async def test_empty_list_is_not_found(self):
            def handler(path, params):
                return self._Resp(200, [])

            a = self._adapter(handler)
            assert await a.get_market(self.COND) is None

        @pytest.mark.asyncio
        async def test_unrelated_list_entry_is_not_found(self):
            def handler(path, params):
                return self._Resp(
                    200,
                    [self._market_payload("0x" + "b" * 64, ["A"], ["ta"])],
                )

            a = self._adapter(handler)
            assert await a.get_market(self.COND) is None

        @pytest.mark.asyncio
        async def test_multiple_exact_matches_fail_closed(self):
            payload = self._market_payload(
                self.COND, ["Yes", "No"], [self.TOK_YES, self.TOK_NO]
            )

            def handler(path, params):
                return self._Resp(200, [payload, dict(payload)])

            a = self._adapter(handler)
            with pytest.raises(ValueError):
                await a.get_market(self.COND)

        @pytest.mark.asyncio
        async def test_422_surfaces_meaningfully(self):
            def handler(path, params):
                return self._Resp(422, {"type": "validation error", "error": "invalid integer"})

            a = self._adapter(handler)
            # The bridge classifies adapter exceptions as Gamma failures;
            # the underlying cause must be a specific, inspectable error.
            with pytest.raises(Exception) as exc:
                await a.get_market(self.COND)
            assert "422" in str(exc.value) or "invalid integer" in str(exc.value)

        @pytest.mark.asyncio
        async def test_malformed_identifier_rejected(self):
            a = self._adapter(lambda p, params: self._Resp(200, []))
            with pytest.raises(ValueError):
                await a.get_market("not-a-valid-id")
            with pytest.raises(ValueError):
                await a.get_market("0x" + "z" * 10)
            with pytest.raises(ValueError):
                await a.get_market("")

        @pytest.mark.asyncio
        async def test_numeric_market_id_keeps_path_lookup(self):
            captured = {}

            def handler(path, params):
                captured["path"] = path
                captured["params"] = params
                return self._Resp(
                    200,
                    self._market_payload(
                        self.COND, ["Yes", "No"], [self.TOK_YES, self.TOK_NO]
                    ),
                )

            a = self._adapter(handler)
            market = await a.get_market("561249")
            assert captured["path"] == "/markets/561249"
            assert captured["params"] is None
            assert market is not None

        @pytest.mark.asyncio
        async def test_observed_production_shape_parses(self):
            # Mirrors the live Gamma payload that previously 422'd on the
            # path lookup: outcomes + clobTokenIds as JSON-string arrays.
            def handler(path, params):
                return self._Resp(
                    200,
                    [
                        self._market_payload(
                            self.COND, ["Yes", "No"], [self.TOK_YES, self.TOK_NO]
                        )
                    ],
                )

            a = self._adapter(handler)
            market = await a.get_market(self.COND)
            assert market is not None
            # Exact source token appears exactly once, paired with its label.
            no_outcome = [o for o in market.outcomes if o.label == "No"]
            assert len(no_outcome) == 1
            assert no_outcome[0].clob_token_id == self.TOK_NO
            # PR25A _hydrate should accept the resulting market.
            # _hydrate is synchronous and drives its own loop via _await;
            # _Gamma.get_market is a plain (non-awaitable) method so
            # _await returns it directly without needing an event loop.
            from polycopy.engine.approved_wallet_trade_bridge import _hydrate

            class _Gamma:
                def get_market(self, cid):
                    return market

            class _Row:
                _COND = self.COND
                _TOK_NO = self.TOK_NO

                def __getitem__(self, key):
                    return {
                        "market_source_id": self._COND,
                        "token_id": self._TOK_NO,
                        "outcome": "No",
                    }[key]

            row = _Row()
            result_market, outcome, error = _hydrate(_Gamma(), row)
            assert error is None
            assert result_market is not None
            assert outcome.label == "No"
            assert outcome.clob_token_id == self.TOK_NO
