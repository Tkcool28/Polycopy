"""PaperBroker — simulated execution engine for paper trading.

All orders are tracked in SQLite. No real API calls are made.
Positions are computed from filled orders. is_live = False always.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from polycopy.domain.order import Order, OrderSide, OrderStatus, OrderType
from polycopy.domain.position import Position
from polycopy.providers.execution_broker import ExecutionBroker

logger = logging.getLogger(__name__)


class PaperBroker(ExecutionBroker):
    """Paper trading broker — simulated order execution with in-memory state.

    In production, this would persist to SQLite via the Database class.
    For this scaffold, we maintain in-memory state that can be swapped
    for DB persistence in a later phase.
    """

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}  # key = f"{market_id}:{wallet_id}:{outcome}"

    @property
    def is_live(self) -> bool:
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
        """Place a paper order. Market orders fill immediately at the given price."""
        now = datetime.now(timezone.utc)
        order_id = str(uuid4())

        # For paper trading, we immediately fill the order
        if order_type == OrderType.MARKET:
            status = OrderStatus.FILLED
            filled_quantity = quantity
        elif order_type == OrderType.LIMIT:
            # In a full simulation, limit orders would rest on the book.
            # For the scaffold, we fill immediately at the limit price.
            status = OrderStatus.FILLED
            filled_quantity = quantity
        else:
            status = OrderStatus.PENDING
            filled_quantity = 0.0

        order = Order(
            id=uuid4(),
            market_id=UUID(market_id),
            wallet_id=UUID(wallet_id),
            side=side,
            order_type=order_type,
            outcome=outcome,
            quantity=quantity,
            price=price,
            status=status,
            filled_quantity=filled_quantity,
            created_at=now,
            updated_at=now,
        )

        self._orders[order_id] = order
        logger.info(
            "Paper order %s: %s %s %s qty=%.4f price=%.4f → %s",
            order_id, side.value, outcome, market_id, quantity, price, status.value,
        )

        # Update position if filled
        if status == OrderStatus.FILLED:
            self._update_position(order)

        return order

    async def cancel_order(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(f"Order {order_id} not found")
        if order.status not in (OrderStatus.PENDING, OrderStatus.ACCEPTED):
            raise ValueError(f"Cannot cancel order in status {order.status.value}")
        order = order.model_copy(update={"status": OrderStatus.CANCELLED, "updated_at": datetime.now(timezone.utc)})
        self._orders[order_id] = order
        return order

    async def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    async def list_open_orders(self, wallet_id: str) -> list[Order]:
        return [
            o for o in self._orders.values()
            if str(o.wallet_id) == wallet_id and o.status in (OrderStatus.PENDING, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED)
        ]

    async def get_position(self, market_id: str, wallet_id: str, outcome: str) -> Optional[Position]:
        key = f"{market_id}:{wallet_id}:{outcome}"
        return self._positions.get(key)

    async def list_positions(self, wallet_id: str) -> list[Position]:
        return [p for p in self._positions.values() if str(p.wallet_id) == wallet_id]

    def _update_position(self, order: Order) -> None:
        """Update position after a filled order."""
        key = f"{order.market_id}:{order.wallet_id}:{order.outcome}"
        existing = self._positions.get(key)

        now = datetime.now(timezone.utc)
        if existing is None:
            if order.side == OrderSide.BUY:
                self._positions[key] = Position(
                    market_id=order.market_id,
                    wallet_id=order.wallet_id,
                    outcome=order.outcome,
                    quantity=order.filled_quantity,
                    avg_entry_price=order.price,
                    current_price=order.price,
                    opened_at=now,
                    updated_at=now,
                )
            else:
                logger.warning("Paper sell with no position — skipping position update for %s", key)
        else:
            if order.side == OrderSide.BUY:
                new_qty = existing.quantity + order.filled_quantity
                new_avg = (existing.cost_basis + order.filled_quantity * order.price) / new_qty
                self._positions[key] = existing.model_copy(update={
                    "quantity": new_qty,
                    "avg_entry_price": new_avg,
                    "current_price": order.price,
                    "updated_at": now,
                })
            else:  # SELL
                new_qty = existing.quantity - order.filled_quantity
                if new_qty <= 0:
                    # Position fully closed
                    del self._positions[key]
                    logger.info("Paper position closed: %s", key)
                else:
                    realized = (order.price - existing.avg_entry_price) * order.filled_quantity
                    self._positions[key] = existing.model_copy(update={
                        "quantity": new_qty,
                        "current_price": order.price,
                        "realized_pnl": existing.realized_pnl + realized,
                        "updated_at": now,
                    })
