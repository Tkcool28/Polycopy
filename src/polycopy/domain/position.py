"""Position domain model — held position in a prediction market."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Position(BaseModel):
    """A current position in a prediction market outcome."""

    id: UUID = Field(default_factory=uuid4, description="Internal position ID.")
    market_id: UUID = Field(description="Market this position is on.")
    wallet_id: UUID = Field(description="Wallet that holds this position.")
    outcome: str = Field(description="Outcome token, e.g. 'Yes'.")
    quantity: float = Field(gt=0.0, description="Number of shares/tokens held.")
    avg_entry_price: float = Field(ge=0.0, le=1.0, description="Volume-weighted average entry price.")
    current_price: float = Field(ge=0.0, le=1.0, description="Current market price for unrealized P&L.")
    realized_pnl: float = Field(default=0.0, description="P&L from partial closes.")
    opened_at: datetime = Field(description="When position was first opened (UTC).")
    updated_at: Optional[datetime] = Field(default=None, description="Last position update (UTC).")
    is_sample: bool = Field(default=False, description="True if this is sample/fixture data, not live.")

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.avg_entry_price) * self.quantity

    @property
    def cost_basis(self) -> float:
        return self.avg_entry_price * self.quantity

    @property
    def market_value(self) -> float:
        return self.current_price * self.quantity
