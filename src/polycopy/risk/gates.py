"""Risk gates — configurable hard safety guards for order creation and exposure.

This module provides:
- PaperMode enum: research_only / paper_manual / paper_auto (default paper_manual)
- OrderKillSwitch: global kill switch that blocks ALL order creation
- ExposureLimit: per-market, per-wallet, and global exposure caps
- RiskGate: composite gate that must pass before any order is placed

All gates are fail-closed: any error or missing data blocks the order.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class PaperMode(str, enum.Enum):
    """Broker paper execution modes.

    - research_only: read-only, no order creation at all
    - paper_manual: paper orders require explicit confirmation (default)
    - paper_auto: paper orders fill automatically after risk gates pass
    """

    RESEARCH_ONLY = "research_only"
    PAPER_MANUAL = "paper_manual"
    PAPER_AUTO = "paper_auto"


class GateVerdict(str, enum.Enum):
    """Result of a risk gate check."""

    PASS = "pass"
    BLOCKED = "blocked"
    NEEDS_REVIEW = "needs_review"


@dataclass
class GateResult:
    """A single gate check result."""

    verdict: GateVerdict
    gate_name: str
    reason: str
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_passed(self) -> bool:
        return self.verdict == GateVerdict.PASS

    @property
    def is_blocked(self) -> bool:
        return self.verdict in (GateVerdict.BLOCKED, GateVerdict.NEEDS_REVIEW)


class OrderKillSwitch:
    """Global order-creation kill switch.

    When engaged (active=True), ALL order creation is blocked regardless
    of any other state. This is the ultimate safety mechanism — it cannot
    be bypassed by configuration or mode changes.

    The kill switch is NOT persisted — it resets to inactive on process
    startup. Engaging it is an explicit operator action.
    """

    def __init__(self, active: bool = False) -> None:
        self._active = active
        self._engaged_at: Optional[datetime] = None
        self._engaged_by: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return self._active

    def engage(self, engaged_by: str = "operator") -> None:
        """Activate the kill switch — blocks all order creation."""
        self._active = True
        self._engaged_at = datetime.now(timezone.utc)
        self._engaged_by = engaged_by
        logger.critical("ORDER KILL SWITCH ENGAGED by %s — all orders blocked.", engaged_by)

    def disengage(self) -> None:
        """Deactivate the kill switch — orders can flow again (if other gates pass)."""
        self._active = False
        self._engaged_at = None
        self._engaged_by = None
        logger.info("Order kill switch disengated — orders allowed if gates pass.")

    def check(self) -> GateResult:
        """Check if order creation is allowed."""
        if self._active:
            engaged_at = self._engaged_at.isoformat() if self._engaged_at is not None else "unknown"
            return GateResult(
                verdict=GateVerdict.BLOCKED,
                gate_name="order_kill_switch",
                reason=f"Kill switch engaged by {self._engaged_by} at {engaged_at}",
            )
        return GateResult(
            verdict=GateVerdict.PASS,
            gate_name="order_kill_switch",
            reason="Kill switch inactive.",
        )


@dataclass
class ExposureLimits:
    """Configurable exposure limits for paper trading.

    All limits are in notional currency units (e.g. USDC).
    Zero or negative values mean "no limit" for that dimension.

    Attributes:
        max_per_market: max total notional exposure per market (across all wallets)
        max_per_wallet: max total notional exposure per wallet (across all markets)
        max_per_outcome: max notional exposure per (market, outcome) pair
        max_global: max total notional exposure across all wallets and markets
        max_order_size: max notional size of a single order
    """

    max_per_market: float = 0.0  # 0 = unlimited
    max_per_wallet: float = 0.0
    max_per_outcome: float = 0.0
    max_global: float = 0.0
    max_order_size: float = 0.0

    def check(
        self,
        order_notional: float,
        market_exposure: float,
        wallet_exposure: float,
        outcome_exposure: float,
        global_exposure: float,
    ) -> GateResult:
        """Check an order against all exposure limits.

        Returns GateResult with verdict=BLOCKED if any limit is breached,
        PASS otherwise.
        """
        now = datetime.now(timezone.utc)

        # Check single-order size first
        if self.max_order_size > 0 and order_notional > self.max_order_size:
            return GateResult(
                verdict=GateVerdict.BLOCKED,
                gate_name="exposure_limit.order_size",
                reason=(
                    f"Order notional {order_notional:.2f} exceeds max "
                    f"{self.max_order_size:.2f}."
                ),
                checked_at=now,
            )

        # Per-market limit
        if self.max_per_market > 0 and market_exposure + order_notional > self.max_per_market:
            return GateResult(
                verdict=GateVerdict.BLOCKED,
                gate_name="exposure_limit.per_market",
                reason=(
                    f"Market exposure {market_exposure:.2f} + order {order_notional:.2f} "
                    f"exceeds max {self.max_per_market:.2f}."
                ),
                checked_at=now,
            )

        # Per-wallet limit
        if self.max_per_wallet > 0 and wallet_exposure + order_notional > self.max_per_wallet:
            return GateResult(
                verdict=GateVerdict.BLOCKED,
                gate_name="exposure_limit.per_wallet",
                reason=(
                    f"Wallet exposure {wallet_exposure:.2f} + order {order_notional:.2f} "
                    f"exceeds max {self.max_per_wallet:.2f}."
                ),
                checked_at=now,
            )

        # Per-outcome limit
        if self.max_per_outcome > 0 and outcome_exposure + order_notional > self.max_per_outcome:
            return GateResult(
                verdict=GateVerdict.BLOCKED,
                gate_name="exposure_limit.per_outcome",
                reason=(
                    f"Outcome exposure {outcome_exposure:.2f} + order {order_notional:.2f} "
                    f"exceeds max {self.max_per_outcome:.2f}."
                ),
                checked_at=now,
            )

        # Global limit
        if self.max_global > 0 and global_exposure + order_notional > self.max_global:
            return GateResult(
                verdict=GateVerdict.BLOCKED,
                gate_name="exposure_limit.global",
                reason=(
                    f"Global exposure {global_exposure:.2f} + order {order_notional:.2f} "
                    f"exceeds max {self.max_global:.2f}."
                ),
                checked_at=now,
            )

        return GateResult(
            verdict=GateVerdict.PASS,
            gate_name="exposure_limit",
            reason="All exposure limits pass.",
            checked_at=now,
        )


class RiskGate:
    """Composite risk gate that must pass before any order is created.

    Combines:
    - OrderKillSwitch (global kill switch)
    - PaperMode check (research_only blocks everything)
    - ExposureLimits (per-market, per-wallet, per-outcome, global, order size)

    Usage:
        gate = RiskGate(kill_switch, paper_mode, exposure_limits)
        result = gate.check(order_notional=..., market_exposure=..., ...)
        if result.is_blocked:
            # reject order
    """

    def __init__(
        self,
        kill_switch: OrderKillSwitch,
        paper_mode: PaperMode,
        exposure_limits: ExposureLimits,
    ) -> None:
        self.kill_switch = kill_switch
        self.paper_mode = paper_mode
        self.exposure_limits = exposure_limits

    def check(
        self,
        order_notional: float,
        market_exposure: float = 0.0,
        wallet_exposure: float = 0.0,
        outcome_exposure: float = 0.0,
        global_exposure: float = 0.0,
    ) -> GateResult:
        """Run all risk gates in priority order.

        Returns the FIRST blocking result, or PASS if all gates pass.
        Fail-closed: any error blocks the order.
        """
        # 1. Kill switch (highest priority)
        ks_result = self.kill_switch.check()
        if ks_result.is_blocked:
            return ks_result

        # 2. Paper mode — research_only blocks all orders
        if self.paper_mode == PaperMode.RESEARCH_ONLY:
            return GateResult(
                verdict=GateVerdict.BLOCKED,
                gate_name="paper_mode.research_only",
                reason="Mode is research_only — order creation disabled.",
            )

        # 3. Exposure limits
        el_result = self.exposure_limits.check(
            order_notional=order_notional,
            market_exposure=market_exposure,
            wallet_exposure=wallet_exposure,
            outcome_exposure=outcome_exposure,
            global_exposure=global_exposure,
        )
        if el_result.is_blocked:
            return el_result

        return GateResult(
            verdict=GateVerdict.PASS,
            gate_name="risk_gate",
            reason="All risk gates pass.",
        )

    @property
    def requires_manual_confirm(self) -> bool:
        """Whether the current mode requires manual confirmation for orders."""
        return self.paper_mode == PaperMode.PAPER_MANUAL
