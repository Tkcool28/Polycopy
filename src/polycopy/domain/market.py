"""Market and outcome domain models."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class MarketOutcome(BaseModel):
    """A single outcome in a prediction market.

    ``clob_token_id`` is the upstream CLOB token identifier (from Gamma's
    ``clobTokenIds`` JSON-array field, zipped positionally with
    ``outcomes``/``outcomePrices``). It is the persistence-side identity
    that lets ``source_trades.token_id`` be joined to ``market_outcomes``
    by token instead of by denormalized label. Optional because legacy
    payloads may not carry ``clobTokenIds`` and an absent / malformed
    array produces ``clob_token_id=None`` for every outcome (treated as
    INCOMPLETE by the canonical mapping helper, not silently mapped).
    """

    label: str = Field(description="Outcome text, e.g. 'Yes' or 'No'.")
    price: float = Field(ge=0.0, le=1.0, description="Current implied probability [0, 1].")
    volume: float = Field(default=0.0, ge=0.0, description="Volume on this outcome.")
    clob_token_id: Optional[str] = Field(
        default=None,
        description=(
            "Polymarket CLOB token id for this outcome, taken from the "
            "positionally-indexed Gamma clobTokenIds array. None means the "
            "Gamma payload did not include clobTokenIds (or it was "
            "malformed / length-mismatched) for this outcome."
        ),
    )


class Market(BaseModel):
    """A prediction market from a source (Polymarket, etc.)."""

    id: UUID = Field(default_factory=uuid4, description="Internal market ID.")
    source_id: str = Field(description="Source-specific market ID (e.g. Polymarket condition_id).")
    question: str = Field(description="Market question/title.")
    outcomes: list[MarketOutcome] = Field(default_factory=list, description="Possible outcomes.")
    source: str = Field(description="Data source name, e.g. 'polymarket'.")
    active: bool = Field(default=True, description="Whether the market is currently active.")
    closed: bool = Field(default=False, description="Whether the market has closed.")
    resolved: bool = Field(default=False, description="Whether the market has resolved.")
    resolution_outcome: Optional[str] = Field(default=None, description="Winning outcome label, if resolved.")
    volume_24h: float = Field(default=0.0, ge=0.0, description="24-hour volume.")
    end_date: Optional[datetime] = Field(default=None, description="Market expiry/close date (UTC).")
    fetched_at: datetime = Field(description="When this data was fetched (UTC).")
    is_sample: bool = Field(default=False, description="True if this is sample/fixture data, not live.")
