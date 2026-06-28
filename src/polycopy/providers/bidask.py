"""Bid/ask snapshot provider for paper order preview.

Provides executable bid/ask price and depth for a (market, outcome) pair.
Used by the preview endpoint to compute spread, slippage, and fillability.

In this paper-only system, the snapshot can come from:
1. A configured/fixture depth table (for deterministic testing)
2. An optional Polymarket adapter (if network is available)

The provider never makes authenticated calls — only read-only public data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from polycopy.risk.fill_model import MarketDepth, DepthLevel

logger = logging.getLogger(__name__)


@dataclass
class BidAskSnapshot:
    """A point-in-time bid/ask snapshot for a (market, outcome) pair.

    Attributes:
        market_id: internal UUID of the market
        outcome: outcome label (e.g. "Yes")
        bid: best bid price (price per share, [0,1])
        ask: best ask price (price per share, [0,1])
        bid_volume: volume available at best bid
        ask_volume: volume available at best ask
        bid_depth: full bid-side depth levels
        ask_depth: full ask-side depth levels
        snapshot_time: when the snapshot was taken
    """
    market_id: str
    outcome: str
    bid: float
    ask: float
    bid_volume: float = 0.0
    ask_volume: float = 0.0
    bid_depth: list = field(default_factory=list)
    ask_depth: list = field(default_factory=list)
    snapshot_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def ask_depth_model(self) -> MarketDepth:
        """Convert ask depth levels to a MarketDepth for FillModel."""
        if self.ask_depth:
            levels = [DepthLevel(price=level["price"], volume=level["volume"]) for level in self.ask_depth]
        else:
            # Single level at best ask with default volume
            levels = [DepthLevel(price=self.ask, volume=self.ask_volume)]
        return MarketDepth(
            best_price=self.ask,
            levels=levels,
        )

    def bid_depth_model(self) -> MarketDepth:
        """Convert bid depth levels to a MarketDepth for FillModel (sell side)."""
        if self.bid_depth:
            levels = [DepthLevel(price=level["price"], volume=level["volume"]) for level in self.bid_depth]
        else:
            levels = [DepthLevel(price=self.bid, volume=self.bid_volume)]
        return MarketDepth(
            best_price=self.bid,
            levels=levels,
        )


class BidAskProvider:
    """Provides bid/ask snapshots for paper order preview.

    Uses a configurable in-memory depth table for deterministic fills.
    The table can be populated from real Polymarket data or fixtures.
    """

    def __init__(self) -> None:
        # Keys: (market_id, outcome) → BidAskSnapshot
        self._snapshots: dict[tuple[str, str], BidAskSnapshot] = {}

    def set_snapshot(
        self,
        market_id: str,
        outcome: str,
        bid: float,
        ask: float,
        bid_volume: float = 1000.0,
        ask_volume: float = 1000.0,
        bid_depth: list | None = None,
        ask_depth: list | None = None,
    ) -> BidAskSnapshot:
        """Set a bid/ask snapshot for a market outcome (for testing/fixture)."""
        snapshot = BidAskSnapshot(
            market_id=market_id,
            outcome=outcome,
            bid=bid,
            ask=ask,
            bid_volume=bid_volume,
            ask_volume=ask_volume,
            bid_depth=bid_depth or [],
            ask_depth=ask_depth or [],
            snapshot_time=datetime.now(timezone.utc),
        )
        self._snapshots[(market_id, outcome)] = snapshot
        return snapshot

    def get_snapshot(self, market_id: str, outcome: str) -> Optional[BidAskSnapshot]:
        """Get the current bid/ask snapshot for a market outcome."""
        return self._snapshots.get((market_id, outcome))

    def has_snapshot(self, market_id: str, outcome: str) -> bool:
        """Check if a snapshot exists for a market outcome."""
        return (market_id, outcome) in self._snapshots

    def clear(self) -> None:
        """Remove all snapshots."""
        self._snapshots.clear()
