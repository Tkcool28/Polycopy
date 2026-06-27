"""ResolutionProvider interface — check market resolution status and outcomes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from polycopy.domain.market import Market


class ResolutionProvider(ABC):
    """Provides market resolution data."""

    @abstractmethod
    async def check_resolution(self, market_id: str) -> Optional[Market]:
        """Check if a market has resolved. Returns updated Market with resolution_outcome if so, None if still open."""
        ...

    @abstractmethod
    async def list_resolved_since(self, since_timestamp: str, limit: int = 100) -> list[Market]:
        """List markets resolved since a given ISO-8601 timestamp."""
        ...
