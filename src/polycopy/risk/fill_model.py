"""Fill model — follower-available execution with bid/ask, depth, slippage, fees.

This module provides:
- MarketDepth: order book depth summary (bid/ask levels)
- FillQuote: estimated fill price with slippage and fee breakdown
- FillModel: computes expected fill price given market state and order size
- ReviewDelay: configurable delay before an order can execute (paper_manual mode)

All values are deterministic given inputs — no randomness.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DepthLevel:
    """A single level in the order book."""

    price: float  # price per share [0, 1]
    volume: float  # shares available at this price


@dataclass
class MarketDepth:
    """Order book depth for one side of a market outcome.

    Attributes:
        best_price: best bid or ask price
        levels: ordered list of depth levels (best first)
        total_volume: sum of all volumes across levels
    """

    best_price: float
    levels: list[DepthLevel] = field(default_factory=list)

    @property
    def total_volume(self) -> float:
        return sum(level.volume for level in self.levels)

    def volume_at_price(self, max_price: float) -> float:
        """Total volume available at or better than max_price (for bids: >= max_price)."""
        return sum(level.volume for level in self.levels if level.price >= max_price)

    def volume_up_to(self, max_price: float) -> float:
        """Total volume available at or below max_price (for asks: <= max_price)."""
        return sum(level.volume for level in self.levels if level.price <= max_price)


@dataclass
class FillQuote:
    """Estimated fill quote for an order, including slippage and fees.

    Attributes:
        expected_price: expected execution price per share
        slippage: price impact vs best available price
        fee: total fee for the order
        fee_rate: fee as a fraction of notional
        total_cost: total cost (price * qty + fee)
        fillable_volume: maximum fillable quantity at the requested price
        is_complete_fill: whether the full quantity can be filled
        is_sample: True if computed from sample/fixture data
    """

    expected_price: float
    slippage: float
    fee: float
    fee_rate: float
    total_cost: float
    fillable_volume: float
    is_complete_fill: bool
    is_sample: bool = False

    @property
    def effective_price(self) -> float:
        """Price per share including fee impact: total_cost / fillable_volume."""
        if self.fillable_volume <= 0:
            return self.expected_price
        return self.total_cost / self.fillable_volume


@dataclass
class FillModel:
    """Computes expected fill price given market depth and order parameters.

    The model is deterministic: given the same depth and order size,
    it always produces the same quote.

    Slippage model: linear impact based on order size relative to depth.
    - If order fits entirely at best price → zero slippage
    - If order walks the book → average price across consumed levels
    - If order exceeds total depth → partial fill at worst available price

    Fee model: flat rate applied to notional value.
    """

    # Default fee rate: 0.1% of notional (sample/fixture default)
    default_fee_rate: float = 0.001

    def quote_fill(
        self,
        side: str,  # "buy" or "sell"
        quantity: float,
        depth: MarketDepth,
        fee_rate: Optional[float] = None,
        is_sample: bool = False,
    ) -> FillQuote:
        """Compute a fill quote for an order.

        Args:
            side: "buy" (consume ask side) or "sell" (consume bid side)
            quantity: desired quantity
            depth: current order book depth
            fee_rate: override fee rate (uses default if None)
            is_sample: label as sample data

        Returns:
            FillQuote with expected price, slippage, and fees.
        """
        if fee_rate is None:
            fee_rate = self.default_fee_rate

        if quantity <= 0:
            return FillQuote(
                expected_price=depth.best_price,
                slippage=0.0,
                fee=0.0,
                fee_rate=fee_rate,
                total_cost=0.0,
                fillable_volume=0.0,
                is_complete_fill=False,
                is_sample=is_sample,
            )

        if not depth.levels:
            # No depth available — cannot fill
            return FillQuote(
                expected_price=depth.best_price,
                slippage=0.0,
                fee=0.0,
                fee_rate=fee_rate,
                total_cost=0.0,
                fillable_volume=0.0,
                is_complete_fill=False,
                is_sample=is_sample,
            )

        # Walk the book to compute average price
        remaining = quantity
        total_notional = 0.0
        fillable = 0.0

        for level in depth.levels:
            if remaining <= 0:
                break
            fill_at_level = min(remaining, level.volume)
            total_notional += fill_at_level * level.price
            fillable += fill_at_level
            remaining -= fill_at_level

        if fillable <= 0:
            return FillQuote(
                expected_price=depth.best_price,
                slippage=0.0,
                fee=0.0,
                fee_rate=fee_rate,
                total_cost=0.0,
                fillable_volume=0.0,
                is_complete_fill=False,
                is_sample=is_sample,
            )

        avg_price = total_notional / fillable
        slippage = avg_price - depth.best_price
        notional = avg_price * fillable
        fee = notional * fee_rate
        total_cost = notional + fee
        is_complete = remaining <= 0  # fully filled

        return FillQuote(
            expected_price=round(avg_price, 6),
            slippage=round(slippage, 6),
            fee=round(fee, 6),
            fee_rate=fee_rate,
            total_cost=round(total_cost, 6),
            fillable_volume=fillable,
            is_complete_fill=is_complete,
            is_sample=is_sample,
        )


@dataclass
class ReviewDelay:
    """Configurable delay before an order can execute in paper_manual mode.

    In paper_manual mode, orders are held for `delay_seconds` before they
    can be filled. During this window, the order can be cancelled or
    modified. The delay gives the operator time to review.

    Attributes:
        delay_seconds: how long an order must wait before filling
        started_at: when the order was submitted (UTC)
    """

    delay_seconds: float
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def expires_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.delay_seconds)

    def is_eligible(self, now: Optional[datetime] = None) -> bool:
        """Check if the review delay has elapsed and the order can fill."""
        if now is None:
            now = datetime.now(timezone.utc)
        return (now - self.started_at).total_seconds() >= self.delay_seconds

    def seconds_remaining(self, now: Optional[datetime] = None) -> float:
        """Seconds remaining before eligibility. Returns 0 if already eligible."""
        if now is None:
            now = datetime.now(timezone.utc)
        remaining = self.delay_seconds - (now - self.started_at).total_seconds()
        return max(remaining, 0.0)
