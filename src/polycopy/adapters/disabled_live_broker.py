"""DisabledLiveBroker — fail-closed guard that prevents any real trade execution.

If code accidentally routes to this broker, every method raises.
This is the default live broker to ensure no real-money path exists
unless explicitly replaced with a real ( authenticated) implementation.
"""

from __future__ import annotations

from polycopy.domain.order import Order, OrderSide, OrderType
from polycopy.domain.position import Position
from polycopy.providers.execution_broker import ExecutionBroker

_LIVE_DISABLED = (
    "LIVE EXECUTION IS DISABLED. This broker is a fail-closed guard. "
    "To enable live trading, configure a real ExecutionBroker implementation "
    "with explicit opt-in. Paper trading uses PaperBroker instead."
)


class DisabledLiveBroker(ExecutionBroker):
    """Fail-closed broker that raises on every operation.

    Install this as the live broker to guarantee no real trades can execute.
    """

    @property
    def is_live(self) -> bool:
        # Technically "live" in the sense that it's the live-broker slot,
        # but it refuses all operations — the point is to fail closed.
        return False

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
        raise RuntimeError(_LIVE_DISABLED)

    async def cancel_order(self, order_id: str) -> Order:
        raise RuntimeError(_LIVE_DISABLED)

    async def get_order(self, order_id: str) -> Order:
        raise RuntimeError(_LIVE_DISABLED)

    async def list_open_orders(self, wallet_id: str) -> list[Order]:
        raise RuntimeError(_LIVE_DISABLED)

    async def get_position(self, market_id: str, wallet_id: str, outcome: str) -> Position:
        raise RuntimeError(_LIVE_DISABLED)

    async def list_positions(self, wallet_id: str) -> list[Position]:
        raise RuntimeError(_LIVE_DISABLED)
