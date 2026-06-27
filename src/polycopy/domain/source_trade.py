"""Source trade domain model — a trade observed from an external source (e.g. wallet tracker)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from polycopy.domain.order import OrderSide


class SourceTrade(BaseModel):
    """A trade observed from an external data source (not our own order)."""

    id: UUID = Field(default_factory=uuid4, description="Internal trade ID.")
    source: str = Field(description="Data source, e.g. 'polymarket_clob', 'bullpen'.")
    source_trade_id: str = Field(description="Trade ID in the source system.")
    market_source_id: str = Field(description="Source-specific market ID.")
    side: OrderSide = Field(description="Buy or sell.")
    outcome: str = Field(description="Outcome token, e.g. 'Yes'.")
    quantity: float = Field(gt=0.0, description="Trade quantity.")
    price: float = Field(ge=0.0, le=1.0, description="Trade price [0, 1].")
    trader_address: str = Field(description="Public address of the trader.")
    timestamp: datetime = Field(description="Trade timestamp (UTC).")
    is_sample: bool = Field(default=False, description="True if this is sample/fixture data, not live.")
