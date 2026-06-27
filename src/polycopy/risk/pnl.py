"""P&L — profit and loss tracking with FIFO position closing.

This module provides:
- PnlEvent: a single P&L event (realized or unrealized)
- PnlTracker: tracks realized and unrealized P&L per wallet with FIFO closing
- PnlSnapshot: a point-in-time P&L summary

FIFO (first-in, first-out) closing: when selling shares, the oldest
shares are consumed first. This gives deterministic P&L calculation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


@dataclass
class PnlEvent:
    """A single P&L event.

    Attributes:
        event_id: unique event ID
        wallet_id: the wallet
        market_id: the market
        outcome: the outcome
        event_type: "realized" or "unrealized_change"
        quantity: shares involved
        cost_price: price of the shares being consumed (FIFO)
        proceeds_price: price at which shares were sold or current mark
        pnl: profit/loss for this event
        created_at: when the event was recorded (UTC)
        is_sample: True if from sample/fixture data
    """

    event_id: UUID
    wallet_id: UUID
    market_id: UUID
    outcome: str
    event_type: str  # "realized" or "unrealized_change"
    quantity: float
    cost_price: float
    proceeds_price: float
    pnl: float
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_sample: bool = False


@dataclass
class PnlSnapshot:
    """Point-in-time P&L summary for a wallet.

    Attributes:
        wallet_id: the wallet
        realized_pnl: total realized P&L from closed trades
        unrealized_pnl: total unrealized P&L from open positions
        total_pnl: realized + unrealized
        open_cost_basis: total cost basis of open positions
        open_market_value: total market value of open positions
        event_count: number of P&L events recorded
        as_of: timestamp of the snapshot (UTC)
        is_sample: True if computed from sample/fixture data
    """

    wallet_id: UUID
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    open_cost_basis: float = 0.0
    open_market_value: float = 0.0
    event_count: int = 0
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_sample: bool = False


class PnlTracker:
    """Tracks realized and unrealized P&L per wallet using FIFO closing.

    The tracker maintains a FIFO queue of open lots per (wallet, market, outcome).
    When a sell occurs, the oldest lots are consumed first, producing realized P&L.

    Unrealized P&L is computed on demand from remaining lots and current mark.
    """

    def __init__(self) -> None:
        # (wallet_id, market_id, outcome) → list of (quantity, price) lots
        self._lots: dict[tuple[UUID, UUID, str], list[tuple[float, float]]] = {}
        # wallet_id → list of PnlEvent
        self._events: dict[UUID, list[PnlEvent]] = {}

    def record_buy(
        self,
        wallet_id: UUID,
        market_id: UUID,
        outcome: str,
        quantity: float,
        price: float,
        is_sample: bool = False,
    ) -> None:
        """Record a buy — adds a new lot to the FIFO queue."""
        key = (wallet_id, market_id, outcome)
        if key not in self._lots:
            self._lots[key] = []
        self._lots[key].append((quantity, price))
        logger.debug(
            "FIFO buy: wallet=%s outcome=%s qty=%.4f price=%.4f (lot count: %d)",
            str(wallet_id)[:8],
            outcome,
            quantity,
            price,
            len(self._lots[key]),
        )

    def record_sell(
        self,
        wallet_id: UUID,
        market_id: UUID,
        outcome: str,
        quantity: float,
        price: float,
        is_sample: bool = False,
    ) -> list[PnlEvent]:
        """Record a sell — consumes oldest lots first (FIFO).

        Returns the list of realized P&L events (one per lot consumed).
        """
        key = (wallet_id, market_id, outcome)
        lots = self._lots.get(key, [])

        if not lots:
            logger.warning(
                "FIFO sell with no lots: wallet=%s outcome=%s qty=%.4f",
                str(wallet_id)[:8],
                outcome,
                quantity,
            )
            return []

        events: list[PnlEvent] = []
        remaining = quantity

        while remaining > 0 and lots:
            lot_qty, lot_price = lots[0]
            if lot_qty <= remaining:
                # Consume entire lot
                pnl = (price - lot_price) * lot_qty
                event = PnlEvent(
                    event_id=uuid4(),
                    wallet_id=wallet_id,
                    market_id=market_id,
                    outcome=outcome,
                    event_type="realized",
                    quantity=lot_qty,
                    cost_price=lot_price,
                    proceeds_price=price,
                    pnl=round(pnl, 6),
                    is_sample=is_sample,
                )
                events.append(event)
                remaining -= lot_qty
                lots.pop(0)
            else:
                # Partially consume lot
                pnl = (price - lot_price) * remaining
                event = PnlEvent(
                    event_id=uuid4(),
                    wallet_id=wallet_id,
                    market_id=market_id,
                    outcome=outcome,
                    event_type="realized",
                    quantity=remaining,
                    cost_price=lot_price,
                    proceeds_price=price,
                    pnl=round(pnl, 6),
                    is_sample=is_sample,
                )
                events.append(event)
                lots[0] = (lot_qty - remaining, lot_price)
                remaining = 0

        # Clean up empty lot lists
        if not lots:
            del self._lots[key]

        # Store events
        if events:
            if wallet_id not in self._events:
                self._events[wallet_id] = []
            self._events[wallet_id].extend(events)

        logger.debug(
            "FIFO sell: wallet=%s outcome=%s qty=%.4f price=%.4f → %d events, total_pnl=%.4f",
            str(wallet_id)[:8],
            outcome,
            quantity,
            price,
            len(events),
            sum(e.pnl for e in events),
        )
        return events

    def get_realized_pnl(self, wallet_id: UUID) -> float:
        """Total realized P&L for a wallet."""
        events = self._events.get(wallet_id, [])
        return sum(e.pnl for e in events if e.event_type == "realized")

    def get_unrealized_pnl(
        self,
        wallet_id: UUID,
        mark_prices: dict[tuple[UUID, str], float],
    ) -> float:
        """Compute unrealized P&L for a wallet given current mark prices.

        Args:
            wallet_id: the wallet
            mark_prices: dict of (market_id, outcome) → current mark price

        Returns:
            Total unrealized P&L across all open lots.
        """
        total = 0.0
        for (w, m, o), lots in self._lots.items():
            if w != wallet_id:
                continue
            mark = mark_prices.get((m, o))
            if mark is None:
                continue
            for qty, price in lots:
                total += (mark - price) * qty
        return total

    def get_open_quantity(self, wallet_id: UUID, market_id: UUID, outcome: str) -> float:
        """Get the total open quantity for a wallet/market/outcome."""
        key = (wallet_id, market_id, outcome)
        lots = self._lots.get(key, [])
        return sum(qty for qty, _ in lots)

    def get_open_cost_basis(self, wallet_id: UUID, market_id: UUID, outcome: str) -> float:
        """Get the total cost basis for open lots."""
        key = (wallet_id, market_id, outcome)
        lots = self._lots.get(key, [])
        return sum(qty * price for qty, price in lots)

    def snapshot(
        self,
        wallet_id: UUID,
        mark_prices: Optional[dict[tuple[UUID, str], float]] = None,
        is_sample: bool = False,
    ) -> PnlSnapshot:
        """Compute a point-in-time P&L snapshot for a wallet."""
        if mark_prices is None:
            mark_prices = {}

        realized = self.get_realized_pnl(wallet_id)
        unrealized = self.get_unrealized_pnl(wallet_id, mark_prices)

        # Compute open cost basis and market value
        open_cost = 0.0
        open_mv = 0.0
        for (w, m, o), lots in self._lots.items():
            if w != wallet_id:
                continue
            mark = mark_prices.get((m, o), 0.0)
            for qty, price in lots:
                open_cost += qty * price
                open_mv += qty * mark

        events = self._events.get(wallet_id, [])

        return PnlSnapshot(
            wallet_id=wallet_id,
            realized_pnl=round(realized, 6),
            unrealized_pnl=round(unrealized, 6),
            total_pnl=round(realized + unrealized, 6),
            open_cost_basis=round(open_cost, 6),
            open_market_value=round(open_mv, 6),
            event_count=len(events),
            is_sample=is_sample,
        )

    def get_events(self, wallet_id: UUID) -> list[PnlEvent]:
        """Return all P&L events for a wallet."""
        return list(self._events.get(wallet_id, []))

    @property
    def wallet_count(self) -> int:
        """Number of wallets with tracked P&L."""
        return len(self._events) + len({
            w for (w, _, _) in self._lots.keys()
            if w not in self._events
        })
