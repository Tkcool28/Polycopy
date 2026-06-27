"""TradeFeedProvider interface — stream/source trade events."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from polycopy.domain.source_trade import SourceTrade


class TradeFeedProvider(ABC):
    """Provides trade events from a data source (CLOB, Bullpen, etc.)."""

    @abstractmethod
    async def get_recent_trades(
        self, market_source_id: str, since: datetime, limit: int = 100
    ) -> list[SourceTrade]:
        """Fetch recent trades for a market since a timestamp."""
        ...

    @abstractmethod
    async def get_trades_by_address(
        self, trader_address: str, since: datetime, limit: int = 100
    ) -> list[SourceTrade]:
        """Fetch recent trades by a specific trader address."""
        ...
