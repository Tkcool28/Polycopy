"""Order domain model — paper or live order on a prediction market."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class OrderSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, enum.Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


class Order(BaseModel):
    """An order placed via a broker (paper or live)."""

    id: UUID = Field(default_factory=uuid4, description="Internal order ID.")
    market_id: UUID = Field(description="Market this order is on.")
    wallet_id: UUID = Field(description="Wallet placing this order.")
    side: OrderSide = Field(description="Buy or sell.")
    order_type: OrderType = Field(description="Limit or market.")
    outcome: str = Field(description="Outcome token, e.g. 'Yes'.")
    quantity: float = Field(gt=0.0, description="Number of shares/tokens.")
    price: float = Field(ge=0.0, le=1.0, description="Limit price [0, 1]. For market orders, current best.")
    status: OrderStatus = Field(default=OrderStatus.PENDING, description="Current order status.")
    filled_quantity: float = Field(default=0.0, ge=0.0, description="Quantity filled so far.")
    source_order_id: Optional[str] = Field(default=None, description="Broker's order ID, if assigned.")
    signal_id: Optional[UUID] = Field(default=None, description="Signal that triggered this order, if any.")
    created_at: datetime = Field(description="Order creation time (UTC).")
    updated_at: Optional[datetime] = Field(default=None, description="Last status change (UTC).")
    is_sample: bool = Field(default=False, description="True if this is sample/fixture data, not live.")
