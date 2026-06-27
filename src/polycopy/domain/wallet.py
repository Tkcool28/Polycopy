"""Wallet and balance domain models."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class WalletBalance(BaseModel):
    """Balance snapshot for a single currency/asset."""

    currency: str = Field(description="Currency or asset ticker, e.g. 'USDC'.")
    amount: float = Field(ge=0.0, description="Non-negative balance amount.")
    as_of: datetime = Field(description="UTC timestamp of this balance snapshot.")
    is_sample: bool = Field(default=False, description="True if this is sample/fixture data, not live.")


class Wallet(BaseModel):
    """Wallet identity and balances."""

    id: UUID = Field(default_factory=uuid4, description="Unique wallet identifier.")
    address: str = Field(description="Public address or label for this wallet.")
    label: str = Field(default="default", description="Human-readable label.")
    balances: list[WalletBalance] = Field(default_factory=list, description="Current balance snapshots.")
    is_sample: bool = Field(default=False, description="True if this is sample/fixture data, not live.")

    @field_validator("address")
    @classmethod
    def _address_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("wallet address must not be empty")
        return v
