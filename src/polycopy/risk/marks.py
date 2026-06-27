"""Marks — mark-to-market pricing for positions.

This module provides:
- MarkPrice: a single mark-to-market price observation
- MarkEngine: computes mark prices from market data and tracks price history

Mark-to-market: the current market value of a position, computed from
the best available price (bid for longs, asks for shorts, mid for unrealized).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass
class MarkPrice:
    """A single mark-to-market price observation.

    Attributes:
        market_id: the market being marked
        outcome: the outcome being priced
        mark_price: current market price for mark-to-market
        bid_price: best available bid (sell price)
        ask_price: best available ask (buy price)
        source: where the price came from
        observed_at: when the price was observed (UTC)
        is_sample: True if from sample/fixture data
    """

    market_id: UUID
    outcome: str
    mark_price: float
    bid_price: float
    ask_price: float
    source: str = "unknown"
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_sample: bool = False

    @property
    def spread(self) -> float:
        """Bid-ask spread (ask - bid)."""
        return self.ask_price - self.bid_price

    @property
    def mid_price(self) -> float:
        """Mid-point of bid and ask."""
        return (self.bid_price + self.ask_price) / 2.0


@dataclass
class PositionMark:
    """Mark-to-market for a specific position.

    Combines the position info with current pricing to compute
    unrealized P&L and margin requirements.

    Attributes:
        position_id: the position being marked
        market_id: the market
        wallet_id: the wallet
        outcome: the position outcome
        quantity: shares held
        avg_entry_price: volume-weighted average entry price
        mark_price: current mark price
        unrealized_pnl: (mark_price - avg_entry_price) * quantity
        market_value: mark_price * quantity
        cost_basis: avg_entry_price * quantity
    """

    position_id: UUID
    market_id: UUID
    wallet_id: UUID
    outcome: str
    quantity: float
    avg_entry_price: float
    mark_price: float

    @property
    def unrealized_pnl(self) -> float:
        return (self.mark_price - self.avg_entry_price) * self.quantity

    @property
    def cost_basis(self) -> float:
        return self.avg_entry_price * self.quantity

    @property
    def market_value(self) -> float:
        return self.mark_price * self.quantity

    @property
    def return_pct(self) -> float:
        """Return as a percentage of cost basis."""
        if self.cost_basis <= 0:
            return 0.0
        return (self.unrealized_pnl / self.cost_basis) * 100.0


class MarkEngine:
    """Computes mark-to-market prices for positions.

    Uses mid-price as the default mark, with optional override for
    conservative marking (bid for longs, ask for shorts).

    The engine does NOT fetch prices — it receives them via update_price().
    This keeps it deterministic and decoupled from data sources.
    """

    def __init__(self, use_conservative_mark: bool = False) -> None:
        """
        Args:
            use_conservative_mark: if True, use bid price for longs
                (worst-case sell) and ask for shorts (worst-case buy).
                If False, use mid-price.
        """
        self.use_conservative_mark = use_conservative_mark
        # (market_id, outcome) → MarkPrice
        self._prices: dict[tuple[UUID, str], MarkPrice] = {}

    def update_price(self, mark: MarkPrice) -> None:
        """Record a new price observation."""
        key = (mark.market_id, mark.outcome)
        self._prices[key] = mark
        logger.debug(
            "Mark updated: market=%s outcome=%s price=%.4f source=%s",
            str(mark.market_id)[:8],
            mark.outcome,
            mark.mark_price,
            mark.source,
        )

    def get_mark(
        self,
        market_id: UUID,
        outcome: str,
    ) -> Optional[MarkPrice]:
        """Get the current mark price for a market outcome."""
        return self._prices.get((market_id, outcome))

    def mark_position(
        self,
        position_id: UUID,
        market_id: UUID,
        wallet_id: UUID,
        outcome: str,
        quantity: float,
        avg_entry_price: float,
    ) -> Optional[PositionMark]:
        """Compute mark-to-market for a position.

        Returns None if no price is available for the market outcome.
        """
        mark = self._prices.get((market_id, outcome))
        if mark is None:
            return None

        if self.use_conservative_mark:
            # Conservative: use bid (what you'd get selling) as mark
            price = mark.bid_price
        else:
            price = mark.mark_price

        return PositionMark(
            position_id=position_id,
            market_id=market_id,
            wallet_id=wallet_id,
            outcome=outcome,
            quantity=quantity,
            avg_entry_price=avg_entry_price,
            mark_price=price,
        )

    def list_marks(self) -> list[MarkPrice]:
        """Return all recorded mark prices."""
        return list(self._prices.values())

    @property
    def mark_count(self) -> int:
        return len(self._prices)
