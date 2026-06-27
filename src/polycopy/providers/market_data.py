"""MarketDataProvider interface — read prediction market data."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from polycopy.domain.market import Market


class MarketDataProvider(ABC):
    """Provides prediction market data (questions, odds, volumes, etc.)."""

    @abstractmethod
    async def get_market(self, market_id: str) -> Optional[Market]:
        """Fetch a single market by source ID. Returns None if not found."""
        ...

    @abstractmethod
    async def list_active_markets(self, limit: int = 100, offset: int = 0) -> list[Market]:
        """List currently active markets."""
        ...

    @abstractmethod
    async def search_markets(self, query: str, limit: int = 20) -> list[Market]:
        """Search markets by question text."""
        ...

    @abstractmethod
    async def get_markets_by_volume(self, limit: int = 20, min_volume_24h: float = 0) -> list[Market]:
        """Top markets by 24h volume."""
        ...
