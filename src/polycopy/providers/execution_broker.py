"""ExecutionBroker interface — place/cancel orders and manage positions.

This is the ONLY interface that can execute trades. Concrete implementations
must enforce the fail-closed guarantee: PaperBroker simulates, DisabledLiveBroker
raises, and a future PolymarketBroker requires explicit opt-in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from polycopy.domain.order import Order, OrderSide, OrderType
from polycopy.domain.position import Position


class ExecutionBroker(ABC):
    """Interface for order execution and position management."""

    @abstractmethod
    async def place_order(
        self,
        market_id: str,
        side: OrderSide,
        order_type: OrderType,
        outcome: str,
        quantity: float,
        price: float,
        wallet_id: str,
    ) -> Order:
        """Place an order. Returns the Order (status may be pending, accepted, or failed)."""
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> Order:
        """Cancel a pending order. Returns the updated Order."""
        ...

    @abstractmethod
    async def get_order(self, order_id: str) -> Optional[Order]:
        """Get an order by ID."""
        ...

    @abstractmethod
    async def list_open_orders(self, wallet_id: str) -> list[Order]:
        """List open/pending orders for a wallet."""
        ...

    @abstractmethod
    async def get_position(self, market_id: str, wallet_id: str, outcome: str) -> Optional[Position]:
        """Get current position for a market outcome."""
        ...

    @abstractmethod
    async def list_positions(self, wallet_id: str) -> list[Position]:
        """List all open positions for a wallet."""
        ...

    @property
    @abstractmethod
    def is_live(self) -> bool:
        """True if this broker executes real trades. PaperBroker returns False."""
        ...
