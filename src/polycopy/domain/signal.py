"""Signal domain model — trading signal from an analysis source."""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class SignalStrength(str, enum.Enum):
    """Discrete signal strength categories."""

    STRONG_BUY = "strong_buy"
    BUY = "buy"
    NEUTRAL = "neutral"
    SELL = "sell"
    STRONG_SELL = "strong_sell"


class Signal(BaseModel):
    """A trading signal produced by an analysis model or rule."""

    id: UUID = Field(default_factory=uuid4, description="Unique signal ID.")
    market_id: UUID = Field(description="Market this signal applies to.")
    source: str = Field(description="Signal source, e.g. 'ensemble_model_v1'.")
    strength: SignalStrength = Field(description="Categorized signal strength.")
    confidence: float = Field(ge=0.0, le=1.0, description="Model confidence [0, 1].")
    edge_estimate: float = Field(description="Estimated edge (predicted_prob - market_prob).")
    predicted_prob: float = Field(ge=0.0, le=1.0, description="Model's predicted probability.")
    market_prob: float = Field(ge=0.0, le=1.0, description="Current market implied probability.")
    reasoning: str = Field(default="", description="Human/machine-readable reasoning.")
    produced_at: datetime = Field(description="When this signal was produced (UTC).")
    is_sample: bool = Field(default=False, description="True if this is sample/fixture data, not live.")
