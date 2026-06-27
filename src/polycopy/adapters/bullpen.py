"""Bullpen read-only adapter skeleton.

Bullpen CLI is NOT installed on this host (per Phase 1 audit).
This skeleton defines the adapter shape but raises NotImplementedError
for all operations. It will be filled in when Bullpen is available.
"""

from __future__ import annotations

import logging
from typing import Optional

from polycopy.domain.market import Market
from polycopy.domain.source_trade import SourceTrade
from polycopy.domain.wallet import Wallet
from polycopy.providers.market_data import MarketDataProvider
from polycopy.providers.trade_feed import TradeFeedProvider
from polycopy.providers.wallet_data import WalletDataProvider

logger = logging.getLogger(__name__)

_BULPEN_UNAVAILABLE = (
    "Bullpen CLI is not installed. This adapter is a skeleton pending "
    "Bullpen availability. See Phase 1 data-capability-audit.md."
)


class BullpenReadOnlyAdapter(WalletDataProvider, MarketDataProvider, TradeFeedProvider):
    """Skeleton adapter for Bullpen CLI data source.

    All methods raise NotImplementedError until Bullpen CLI is discovered/installed.
    """

    def __init__(self) -> None:
        logger.warning(_BULPEN_UNAVAILABLE)

    # ── WalletDataProvider ──────────────────────────────────────────────────

    async def get_wallet(self, wallet_address: str) -> Wallet:
        raise NotImplementedError(_BULPEN_UNAVAILABLE)

    async def list_wallets(self) -> list[Wallet]:
        raise NotImplementedError(_BULPEN_UNAVAILABLE)

    # ── MarketDataProvider ──────────────────────────────────────────────────

    async def get_market(self, market_id: str) -> Optional[Market]:
        raise NotImplementedError(_BULPEN_UNAVAILABLE)

    async def list_active_markets(self, limit: int = 100, offset: int = 0) -> list[Market]:
        raise NotImplementedError(_BULPEN_UNAVAILABLE)

    async def search_markets(self, query: str, limit: int = 20) -> list[Market]:
        raise NotImplementedError(_BULPEN_UNAVAILABLE)

    async def get_markets_by_volume(self, limit: int = 20, min_volume_24h: float = 0) -> list[Market]:
        raise NotImplementedError(_BULPEN_UNAVAILABLE)

    # ── TradeFeedProvider ────────────────────────────────────────────────────

    async def get_recent_trades(self, market_source_id: str, since, limit: int = 100) -> list[SourceTrade]:
        raise NotImplementedError(_BULPEN_UNAVAILABLE)

    async def get_trades_by_address(self, trader_address: str, since, limit: int = 100) -> list[SourceTrade]:
        raise NotImplementedError(_BULPEN_UNAVAILABLE)
