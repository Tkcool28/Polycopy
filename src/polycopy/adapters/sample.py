"""Sample / fixture adapters — labeled demo data for development and testing.

ALL data from these adapters is marked is_sample=True. They never make
network calls. Use these when live data is unavailable or for unit tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from polycopy.domain.market import Market, MarketOutcome
from polycopy.domain.source_trade import SourceTrade
from polycopy.domain.wallet import Wallet, WalletBalance
from polycopy.providers.market_data import MarketDataProvider
from polycopy.providers.resolution import ResolutionProvider
from polycopy.providers.trade_feed import TradeFeedProvider
from polycopy.providers.wallet_data import WalletDataProvider

# ── Fixed UUIDs for reproducible sample data ────────────────────────────────
_SAMPLE_WALLET_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_SAMPLE_MARKET_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_SAMPLE_TRADE_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")

_UTC_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class SampleWalletDataProvider(WalletDataProvider):
    """Returns a static sample wallet with labeled demo balances."""

    async def get_wallet(self, wallet_address: str) -> Wallet:
        return Wallet(
            id=_SAMPLE_WALLET_ID,
            address="0xSAMPLE_WALLET_ADDRESS_DO_NOT_USE_IN_PROD",
            label="sample-wallet  [SAMPLE DATA]",
            balances=[
                WalletBalance(
                    currency="USDC",
                    amount=1000.0,
                    as_of=_UTC_NOW,
                    is_sample=True,
                ),
            ],
            is_sample=True,
        )

    async def list_wallets(self) -> list[Wallet]:
        return [await self.get_wallet("sample")]


class SampleMarketDataProvider(MarketDataProvider):
    """Returns static sample markets with labeled demo data."""

    async def get_market(self, market_id: str) -> Optional[Market]:
        if market_id == "sample-market-001":
            return self._make_sample_market()
        return None

    async def list_active_markets(self, limit: int = 100, offset: int = 0) -> list[Market]:
        return [self._make_sample_market()]

    async def search_markets(self, query: str, limit: int = 20) -> list[Market]:
        return [self._make_sample_market()]

    async def get_markets_by_volume(self, limit: int = 20, min_volume_24h: float = 0) -> list[Market]:
        return [self._make_sample_market()]

    def _make_sample_market(self) -> Market:
        return Market(
            id=_SAMPLE_MARKET_ID,
            source_id="sample-market-001",
            question="Will X happen by 2026-12-31?  [SAMPLE DATA]",
            outcomes=[
                MarketOutcome(label="Yes", price=0.65, volume=50000.0),
                MarketOutcome(label="No", price=0.35, volume=30000.0),
            ],
            source="sample",
            active=True,
            closed=False,
            resolved=False,
            volume_24h=80000.0,
            fetched_at=_UTC_NOW,
            is_sample=True,
        )


class SampleTradeFeedProvider(TradeFeedProvider):
    """Returns static sample trades with labeled demo data."""

    async def get_recent_trades(
        self, market_source_id: str, since: datetime, limit: int = 100
    ) -> list[SourceTrade]:
        return [self._make_sample_trade()]

    async def get_trades_by_address(
        self, trader_address: str, since: datetime, limit: int = 100
    ) -> list[SourceTrade]:
        return [self._make_sample_trade()]

    def _make_sample_trade(self) -> SourceTrade:
        return SourceTrade(
            id=_SAMPLE_TRADE_ID,
            source="sample",
            source_trade_id="sample-trade-001",
            market_source_id="sample-market-001",
            side="buy",  # type: ignore[arg-type]
            outcome="Yes",
            quantity=10.0,
            price=0.65,
            trader_address="0xSAMPLE_TRADER_ADDRESS_DO_NOT_USE_IN_PROD",
            timestamp=_UTC_NOW,
            is_sample=True,
        )


class SampleResolutionProvider(ResolutionProvider):
    """Returns None for all markets (sample resolution not available)."""

    async def check_resolution(self, market_id: str) -> Optional[Market]:
        # Sample data never resolves — return None (market still open)
        return None

    async def list_resolved_since(self, since_timestamp: str, limit: int = 100) -> list[Market]:
        return []
