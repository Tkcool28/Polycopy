"""PaperBroker — simulated execution engine with risk gates and fill model.

All orders are tracked in SQLite. No real API calls are made.
Integrates:
- RiskGate (kill switch + exposure limits + paper mode)
- FillModel (bid/ask/depth/slippage/fees)
- ReviewDelay (paper_manual mode confirmation window)
- PnlTracker (FIFO P&L)
- MarkEngine (mark-to-market)
- SettlementEngine (idempotent resolution)

Positions are computed from filled orders. is_live = False always.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from polycopy.domain.order import Order, OrderSide, OrderStatus, OrderType
from polycopy.domain.position import Position
from polycopy.providers.execution_broker import ExecutionBroker
from polycopy.risk.gates import (
    ExposureLimits,
    OrderKillSwitch,
    PaperMode,
    RiskGate,
)
from polycopy.risk.fill_model import FillModel, MarketDepth
from polycopy.risk.pnl import PnlTracker
from polycopy.risk.marks import MarkEngine
from polycopy.risk.settlement import SettlementEngine, SettlementEvidence

logger = logging.getLogger(__name__)


class PaperBroker(ExecutionBroker):
    """Paper trading broker — simulated order execution with full risk controls.

    Execution flow:
    1. RiskGate.check() — must pass before any order is accepted
    2. FillModel.quoteFill() — compute expected fill with slippage/fees
    3. ReviewDelay — in paper_manual mode, hold for review window
    4. Order fills (or is rejected if review not complete)
    5. PnlTracker.record_buy/sell — update FIFO lots
    6. Position updated from filled orders

    Attributes:
        is_live: always False (paper broker never executes real trades)
    """

    def __init__(
        self,
        paper_mode: PaperMode = PaperMode.PAPER_MANUAL,
        exposure_limits: Optional[ExposureLimits] = None,
        fill_model: Optional[FillModel] = None,
        review_delay_seconds: float = 30.0,
        fee_rate: float = 0.001,
    ) -> None:
        self._kill_switch = OrderKillSwitch()
        self._paper_mode = paper_mode
        self._exposure_limits = exposure_limits or ExposureLimits()
        self._fill_model = fill_model or FillModel(default_fee_rate=fee_rate)
        self._review_delay_seconds = review_delay_seconds

        # Composite risk gate
        self._risk_gate = RiskGate(
            kill_switch=self._kill_switch,
            paper_mode=self._paper_mode,
            exposure_limits=self._exposure_limits,
        )

        # State
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}  # key = f"{market_id}:{wallet_id}:{outcome}"
        self._depth: dict[tuple[str, str], MarketDepth] = {}  # (market_id, outcome) → depth

        # Sub-engines
        self._pnl = PnlTracker()
        self._marks = MarkEngine(use_conservative_mark=False)
        self._settlement = SettlementEngine()

    @property
    def is_live(self) -> bool:
        return False

    @property
    def kill_switch(self) -> OrderKillSwitch:
        return self._kill_switch

    @property
    def risk_gate(self) -> RiskGate:
        return self._risk_gate

    @property
    def pnl(self) -> PnlTracker:
        return self._pnl

    @property
    def marks(self) -> MarkEngine:
        return self._marks

    @property
    def settlement(self) -> SettlementEngine:
        return self._settlement

    def set_depth(self, market_id: str, outcome: str, depth: MarketDepth) -> None:
        """Set the order book depth for a market outcome (for fill simulation)."""
        self._depth[(market_id, outcome)] = depth

    def get_depth(self, market_id: str, outcome: str) -> Optional[MarketDepth]:
        """Get current depth for a market outcome."""
        return self._depth.get((market_id, outcome))

    async def place_order(
        self,
        market_id: str,
        side: OrderSide,
        order_type: OrderType,
        outcome: str,
        quantity: float,
        price: float,
        wallet_id: str,
        now: Optional[datetime] = None,
        is_sample: bool = False,
    ) -> Order:
        """Place a paper order with full risk checks and fill simulation.

        Steps:
        1. Risk gate check (kill switch, paper mode, exposure)
        2. Fill quote (slippage, fees)
        3. Review delay check (paper_manual mode)
        4. Fill or hold order
        5. Update position and P&L
        """
        if now is None:
            now = datetime.now(timezone.utc)

        # Normalize side to enum if passed as string
        if isinstance(side, str):
            side = OrderSide(side)

        order_notional = price * quantity

        # ── 1. Risk gate ──────────────────────────────────────────────────
        market_exposure = self._compute_market_exposure(market_id)
        wallet_exposure = self._compute_wallet_exposure(wallet_id)
        outcome_exposure = self._compute_outcome_exposure(market_id, outcome)
        global_exposure = self._compute_global_exposure()

        gate_result = self._risk_gate.check(
            order_notional=order_notional,
            market_exposure=market_exposure,
            wallet_exposure=wallet_exposure,
            outcome_exposure=outcome_exposure,
            global_exposure=global_exposure,
        )

        if gate_result.is_blocked:
            # Create rejected order
            order = Order(
                id=uuid4(),
                market_id=UUID(market_id),
                wallet_id=UUID(wallet_id),
                side=side,
                order_type=order_type,
                outcome=outcome,
                quantity=quantity,
                price=price,
                status=OrderStatus.REJECTED,
                filled_quantity=0.0,
                created_at=now,
                updated_at=now,
                is_sample=is_sample,
            )
            self._orders[str(order.id)] = order
            logger.warning(
                "Order REJECTED by risk gate [%s]: %s",
                gate_result.gate_name,
                gate_result.reason,
            )
            return order

        # ── 2. Fill quote ─────────────────────────────────────────────────
        depth = self._depth.get((market_id, outcome))
        if depth:
            quote = self._fill_model.quote_fill(
                side=side.value,
                quantity=quantity,
                depth=depth,
                is_sample=is_sample,
            )
            fill_price = quote.expected_price
            fill_qty = quote.fillable_volume
            logger.info(
                "Fill quote: expected=%.4f slippage=%.4f fee=%.4f fillable=%.4f/%s",
                fill_price, quote.slippage, quote.fee, fill_qty, quantity,
            )
        else:
            # No depth available — fill at requested price (no slippage)
            fill_price = price
            fill_qty = quantity
            logger.info("No depth available — filling at requested price %.4f", price)

        # ── 3. Review delay (paper_manual mode) ───────────────────────────
        if self._paper_mode == PaperMode.PAPER_MANUAL:
            # Order is created as PENDING — caller must call confirm_and_fill()
            # after the review delay elapses.
            order = Order(
                id=uuid4(),
                market_id=UUID(market_id),
                wallet_id=UUID(wallet_id),
                side=side,
                order_type=order_type,
                outcome=outcome,
                quantity=quantity,
                price=fill_price,
                status=OrderStatus.PENDING,
                filled_quantity=0.0,
                created_at=now,
                updated_at=now,
                is_sample=is_sample,
            )
            self._orders[str(order.id)] = order
            logger.info(
                "Order %s created as PENDING (paper_manual — review required).",
                str(order.id)[:8],
            )
            return order

        # ── 4. Auto-fill (paper_auto mode) ───────────────────────────────
        order = await self._fill_order(
            market_id=market_id,
            wallet_id=wallet_id,
            side=side,
            order_type=order_type,
            outcome=outcome,
            quantity=quantity,
            fill_price=fill_price,
            fill_qty=fill_qty,
            now=now,
            is_sample=is_sample,
        )
        return order

    async def confirm_and_fill(
        self,
        order_id: str,
        now: Optional[datetime] = None,
    ) -> Order:
        """Confirm a pending paper order and fill it (after review delay).

        Only valid in paper_manual mode. Returns the order with updated status.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(f"Order {order_id} not found")
        if order.status != OrderStatus.PENDING:
            raise ValueError(
                f"Order {order_id} is {order.status.value}, not pending."
            )

        # Check review delay
        from polycopy.risk.fill_model import ReviewDelay
        review = ReviewDelay(
            delay_seconds=self._review_delay_seconds,
            started_at=order.created_at,
        )
        if not review.is_eligible(now):
            remaining = review.seconds_remaining(now)
            logger.info(
                "Order %s still in review — %.1fs remaining.",
                order_id[:8],
                remaining,
            )
            return order  # Still pending

        # Fill the order
        return await self._fill_order(
            market_id=str(order.market_id),
            wallet_id=str(order.wallet_id),
            side=order.side,
            order_type=order.order_type,
            outcome=order.outcome,
            quantity=order.quantity,
            fill_price=order.price,
            fill_qty=order.quantity,
            now=now,
            is_sample=order.is_sample,
            existing_order_id=order_id,
        )

    async def _fill_order(
        self,
        market_id: str,
        wallet_id: str,
        side: OrderSide | str,
        order_type: OrderType | str,
        outcome: str,
        quantity: float,
        fill_price: float,
        fill_qty: float,
        now: datetime,
        is_sample: bool,
        existing_order_id: Optional[str] = None,
    ) -> Order:
        """Internal: fill an order and update position + P&L."""
        if isinstance(side, str):
            side = OrderSide(side)
        if isinstance(order_type, str):
            order_type = OrderType(order_type)
        if existing_order_id:
            order = self._orders[existing_order_id]
            order = order.model_copy(update={
                "status": OrderStatus.FILLED,
                "filled_quantity": fill_qty,
                "price": fill_price,
                "updated_at": now,
            })
        else:
            order = Order(
                id=uuid4(),
                market_id=UUID(market_id),
                wallet_id=UUID(wallet_id),
                side=side,
                order_type=order_type,
                outcome=outcome,
                quantity=quantity,
                price=fill_price,
                status=OrderStatus.FILLED,
                filled_quantity=fill_qty,
                created_at=now,
                updated_at=now,
                is_sample=is_sample,
            )

        order_id_str = str(order.id)
        self._orders[order_id_str] = order

        logger.info(
            "Paper order %s: %s %s %s qty=%.4f price=%.4f → FILLED (filled=%.4f)",
            order_id_str[:8], side.value, outcome, market_id, quantity, fill_price, fill_qty,
        )

        # Update position
        if fill_qty > 0:
            self._update_position(order)

        return order

    async def cancel_order(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(f"Order {order_id} not found")
        if order.status not in (OrderStatus.PENDING, OrderStatus.ACCEPTED):
            raise ValueError(f"Cannot cancel order in status {order.status.value}")
        order = order.model_copy(update={"status": OrderStatus.CANCELLED, "updated_at": datetime.now(timezone.utc)})
        self._orders[order_id] = order
        return order

    async def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    async def list_open_orders(self, wallet_id: str) -> list[Order]:
        return [
            o for o in self._orders.values()
            if str(o.wallet_id) == wallet_id and o.status in (OrderStatus.PENDING, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED)
        ]

    async def get_position(self, market_id: str, wallet_id: str, outcome: str) -> Optional[Position]:
        key = f"{market_id}:{wallet_id}:{outcome}"
        return self._positions.get(key)

    async def list_positions(self, wallet_id: str) -> list[Position]:
        return [p for p in self._positions.values() if str(p.wallet_id) == wallet_id]

    def _update_position(self, order: Order) -> None:
        """Update position after a filled order, and record P&L event."""
        key = f"{order.market_id}:{order.wallet_id}:{order.outcome}"
        existing = self._positions.get(key)
        now = datetime.now(timezone.utc)

        if order.side == OrderSide.BUY:
            # Record FIFO lot
            self._pnl.record_buy(
                wallet_id=order.wallet_id,
                market_id=order.market_id,
                outcome=order.outcome,
                quantity=order.filled_quantity,
                price=order.price,
                is_sample=order.is_sample,
            )
            if existing is None:
                self._positions[key] = Position(
                    market_id=order.market_id,
                    wallet_id=order.wallet_id,
                    outcome=order.outcome,
                    quantity=order.filled_quantity,
                    avg_entry_price=order.price,
                    current_price=order.price,
                    opened_at=now,
                    updated_at=now,
                    is_sample=order.is_sample,
                )
            else:
                new_qty = existing.quantity + order.filled_quantity
                new_avg = (existing.cost_basis + order.filled_quantity * order.price) / new_qty
                self._positions[key] = existing.model_copy(update={
                    "quantity": new_qty,
                    "avg_entry_price": new_avg,
                    "current_price": order.price,
                    "updated_at": now,
                })

        elif order.side == OrderSide.SELL:
            # Record FIFO sell → realized P&L events
            events = self._pnl.record_sell(
                wallet_id=order.wallet_id,
                market_id=order.market_id,
                outcome=order.outcome,
                quantity=order.filled_quantity,
                price=order.price,
                is_sample=order.is_sample,
            )

            if existing is None:
                logger.warning("Paper sell with no position — skipping %s", key)
            else:
                new_qty = existing.quantity - order.filled_quantity
                if new_qty <= 0:
                    del self._positions[key]
                    logger.info("Paper position closed: %s", key)
                else:
                    realized = sum(e.pnl for e in events)
                    self._positions[key] = existing.model_copy(update={
                        "quantity": new_qty,
                        "current_price": order.price,
                        "realized_pnl": existing.realized_pnl + realized,
                        "updated_at": now,
                    })

    def _compute_market_exposure(self, market_id: str) -> float:
        """Compute total notional exposure for a market."""
        total = 0.0
        for key, pos in self._positions.items():
            if key.startswith(f"{market_id}:"):
                total += pos.cost_basis
        return total

    def _compute_wallet_exposure(self, wallet_id: str) -> float:
        """Compute total notional exposure for a wallet."""
        total = 0.0
        for key, pos in self._positions.items():
            if f":{wallet_id}:" in key:
                total += pos.cost_basis
        return total

    def _compute_outcome_exposure(self, market_id: str, outcome: str) -> float:
        """Compute total notional exposure for a (market, outcome) pair."""
        total = 0.0
        for k, pos in self._positions.items():
            if k.startswith(f"{market_id}:") and f":{outcome}" in k:
                total += pos.cost_basis
        return total

    def _compute_global_exposure(self) -> float:
        """Compute total global notional exposure."""
        return sum(pos.cost_basis for pos in self._positions.values())

    def settle_market(
        self,
        market_id: str,
        resolution_outcome: str,
        evidence: SettlementEvidence,
    ) -> list:
        """Settle all positions for a market using resolution evidence.

        Returns list of SettlementResult for each position settled.
        """
        results = []
        keys_to_settle = [
            k for k in self._positions
            if k.startswith(f"{market_id}:")
        ]
        for key in keys_to_settle:
            pos = self._positions[key]
            result = self._settlement.settle_position(
                position_id=pos.id,
                market_id=pos.market_id,
                wallet_id=pos.wallet_id,
                outcome=pos.outcome,
                quantity=pos.quantity,
                avg_entry_price=pos.avg_entry_price,
                evidence=evidence,
                is_sample=pos.is_sample,
            )
            results.append(result)
        return results
