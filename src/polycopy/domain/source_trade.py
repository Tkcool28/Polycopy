"""Source trade domain model — a trade observed from an external source (e.g. wallet tracker)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from polycopy.domain.order import OrderSide


# ── Legacy sentinel normalization ──────────────────────────────────────────────
# Some upstream sources historically emitted the literal strings "unknown",
# "anonymous", "missing", "0x" or "0x0" as a stand-in for "no wallet
# attribution". These were persisted to ``source_trades.trader_address`` on
# pre-v5 databases. They MUST be treated identically to ``NULL`` (no
# attribution): excluded from wallet discovery and from ``evaluate_wallet``
# scoring.
#
# The set is intentionally lowercased and stripped before comparison; real
# 0x addresses pass through ``is_sentinel_trader_address`` unchanged.
#
# Round-9 stabilization: also include the zero-address
# ``0x0000000000000000000000000000000000000000`` (Ethereum's burn /
# null-address). The data-api sometimes emits it as a stand-in for
# "no attributable trader" — pre-v9 it passed through as a "real"
# wallet and got scored.
LEGACY_TRADER_ADDRESS_SENTINELS: frozenset[str] = frozenset(
    {
        "unknown",
        "anonymous",
        "missing",
        "0x",
        "0x0",
        "0x0000000000000000000000000000000000000000",
    }
)


def is_sentinel_trader_address(value: Optional[str]) -> bool:
    """Return True if ``value`` is a legacy sentinel or otherwise empty.

    Matches:
      - ``None``
      - empty string and whitespace-only strings
      - case-insensitive matches against ``LEGACY_TRADER_ADDRESS_SENTINELS``
        (after ``str.strip()``).

    Real 0x addresses (any string starting with "0x" plus at least 40 hex
    chars, or any other non-sentinel non-empty value) return ``False``.
    Defensive: we deliberately do NOT validate the 0x format here; any
    non-sentinel, non-empty value passes through so we don't accidentally
    drop a real wallet due to a malformed address.
    """
    if value is None:
        return True
    if not isinstance(value, str):
        return True
    stripped = value.strip()
    if not stripped:
        return True
    return stripped.lower() in LEGACY_TRADER_ADDRESS_SENTINELS


class SourceTrade(BaseModel):
    """A trade observed from an external data source (not our own order).

    Note: ``trader_address`` is ``Optional[str]``. ``None`` means wallet
    attribution is missing/anonymous (e.g. data-api row with no proxyWallet).
    Anonymous trades are still persisted as market-level observations but
    MUST NOT be promoted to ``Wallet`` rows or scored by ``evaluate_wallet``.
    """

    id: UUID = Field(default_factory=uuid4, description="Internal trade ID.")
    source: str = Field(description="Data source, e.g. 'polymarket_clob', 'bullpen'.")
    source_trade_id: str = Field(description="Trade ID in the source system.")
    market_source_id: str = Field(description="Source-specific market ID.")
    side: OrderSide = Field(description="Buy or sell.")
    outcome: str = Field(description="Outcome token, e.g. 'Yes'.")
    quantity: float = Field(gt=0.0, description="Trade quantity.")
    price: float = Field(ge=0.0, le=1.0, description="Trade price [0, 1].")
    trader_address: Optional[str] = Field(
        default=None,
        description=(
            "Public 0x address of the trader. None means wallet attribution is "
            "missing/anonymous — the trade has no attributable wallet."
        ),
    )
    timestamp: datetime = Field(description="Trade timestamp (UTC).")
    is_sample: bool = Field(default=False, description="True if this is sample/fixture data, not live.")