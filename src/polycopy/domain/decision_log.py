"""Decision log domain model — audit trail for trade decisions."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class DecisionLogEntry(BaseModel):
    """An immutable audit entry recording why a trade decision was made."""

    id: UUID = Field(default_factory=uuid4, description="Unique log entry ID.")
    wallet_id: UUID = Field(description="Wallet making the decision.")
    market_id: UUID = Field(description="Market the decision concerns.")
    decision_type: str = Field(description="Type of decision, e.g. 'open_position', 'close_position', 'skip'.")
    signal_ids: list[UUID] = Field(default_factory=list, description="Signals that influenced this decision.")
    order_id: UUID | None = Field(default=None, description="Order placed, if any.")
    rationale: str = Field(default="", description="Human-readable rationale.")
    metrics: dict[str, Any] = Field(default_factory=dict, description="Structured decision metrics (scores, thresholds, etc.).")
    created_at: datetime = Field(description="Decision timestamp (UTC).")
    is_sample: bool = Field(default=False, description="True if this is sample/fixture data, not live.")
