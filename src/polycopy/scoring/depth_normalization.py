"""Bounded order-book depth normalization for PR 4.

Parses raw CLOB order-book levels into a deterministic, normalized
structure suitable for persistence and depth-walk consumption.

Rules:
- parse with Decimal
- reject NaN and Infinity
- reject price < 0 or > 1
- reject negative size
- ignore zero-size levels
- aggregate duplicate prices
- sort deterministically (asks asc, bids desc)
- reject crossed books
- compute cumulative size and cumulative notional
- bound by max levels and max cumulative notional per side
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

# ── Default capture limits (research-only; do not affect CLOB) ──────────────
DEFAULT_MAX_LEVELS_PER_SIDE = 25
DEFAULT_MAX_NOTIONAL_PER_SIDE = 100.0  # USDC-equivalent
DEPTH_BUFFER_FACTOR = 1.5

# ── Rejection reasons ───────────────────────────────────────────────────────
DEPTH_NOT_CAPTURED = "DEPTH_NOT_CAPTURED"
DEPTH_INSUFFICIENT_FOR_STAKE = "DEPTH_INSUFFICIENT_FOR_STAKE"
DEPTH_LEVELS_MALFORMED = "DEPTH_LEVELS_MALFORMED"
DEPTH_SNAPSHOT_MISMATCH = "DEPTH_SNAPSHOT_MISMATCH"


@dataclass(frozen=True, order=True)
class NormalizedLevel:
    """Single normalized order-book level."""

    price: Decimal
    size: Decimal
    cumulative_size: Decimal = Decimal("0")
    cumulative_notional: Decimal = Decimal("0")

    @property
    def notional(self) -> Decimal:
        return self.price * self.size


@dataclass(frozen=True)
class DepthWalkResult:
    """Result of a depth walk for a given side and intended stake."""

    side: str
    intended_notional: Decimal
    filled_notional: Decimal
    fill_percentage: float
    contracts_filled: Decimal
    vwap_fill_price: Optional[Decimal]
    slippage: Optional[float]
    levels_consumed: int
    remaining_notional: Decimal
    is_complete: bool  # True if fully filled
    insufficient_reason: Optional[str] = None


def normalize_book_levels(
    raw_bids: list[tuple],
    raw_asks: list[tuple],
    max_levels: int = DEFAULT_MAX_LEVELS_PER_SIDE,
    max_notional: Decimal = Decimal(str(DEFAULT_MAX_NOTIONAL_PER_SIDE)),
) -> tuple[list[NormalizedLevel], list[NormalizedLevel], Optional[str]]:
    """Normalize raw order-book levels into bounded, ordered lists.

    Each raw entry should be (price: str|float, size: str|float).

    Returns (bids, asks, error_reason) where error_reason is set if the
    book is crossed or malformed.
    """
    bids = _normalize_side_levels(raw_bids, "bid", max_levels, max_notional)
    if isinstance(bids, str):
        return [], [], bids

    asks = _normalize_side_levels(raw_asks, "ask", max_levels, max_notional)
    if isinstance(asks, str):
        return [], [], asks

    # Check crossed books
    if bids and asks:
        best_bid = bids[0].price
        best_ask = asks[0].price
        if best_bid >= best_ask:
            return bids, asks, DEPTH_LEVELS_MALFORMED

    return bids, asks, None


def compute_book_hash(bids: list[NormalizedLevel], asks: list[NormalizedLevel]) -> str:
    """Deterministic SHA-256 hash of normalized levels."""
    data = {
        "bids": [
            {"price": str(level.price), "size": str(level.size)} for level in bids
        ],
        "asks": [
            {"price": str(level.price), "size": str(level.size)} for level in asks
        ],
    }
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def walk_depth(
    levels: list[NormalizedLevel],
    side: str,
    intended_notional: Decimal,
) -> DepthWalkResult:
    """Walk order-book levels to fill an intended notional.

    BUY consumes asks ascending; SELL consumes bids descending.
    Does not extrapolate beyond stored levels.
    """
    if not levels:
        return DepthWalkResult(
            side=side,
            intended_notional=intended_notional,
            filled_notional=Decimal("0"),
            fill_percentage=0.0,
            contracts_filled=Decimal("0"),
            vwap_fill_price=None,
            slippage=None,
            levels_consumed=0,
            remaining_notional=intended_notional,
            is_complete=False,
            insufficient_reason=DEPTH_INSUFFICIENT_FOR_STAKE,
        )

    remaining = intended_notional
    total_filled_notional = Decimal("0")
    total_contracts = Decimal("0")
    levels_consumed = 0

    for level in levels:
        if remaining <= Decimal("0"):
            break
        consume = min(level.notional, remaining)
        contracts = consume / level.price
        total_filled_notional += consume
        total_contracts += contracts
        remaining -= consume
        levels_consumed += 1

    fill_pct = float(total_filled_notional / intended_notional * Decimal("100")) if intended_notional > 0 else 0.0
    vwap = total_filled_notional / total_contracts if total_contracts > 0 else None
    is_complete = remaining <= Decimal("0")

    # Best executable price
    best_price = levels[0].price if levels else None

    slippage = None
    if vwap is not None and best_price is not None:
        if side == "BUY":
            # Slippage = (vwap - best_ask) / best_ask
            slippage_raw = (vwap - best_price) / best_price
        else:
            # SELL: slippage = (best_bid - vwap) / best_bid
            slippage_raw = (best_price - vwap) / best_price
        slippage = float(slippage_raw)

    insufficient_reason = None
    if not is_complete:
        insufficient_reason = DEPTH_INSUFFICIENT_FOR_STAKE

    return DepthWalkResult(
        side=side,
        intended_notional=intended_notional,
        filled_notional=total_filled_notional,
        fill_percentage=fill_pct,
        contracts_filled=total_contracts,
        vwap_fill_price=vwap,
        slippage=slippage,
        levels_consumed=levels_consumed,
        remaining_notional=remaining,
        is_complete=is_complete,
        insufficient_reason=insufficient_reason,
    )


# ── Internal helpers ─────────────────────────────────────────────────────────

def _normalize_side_levels(
    raw_levels: list[tuple],
    side: str,
    max_levels: int,
    max_notional: Decimal,
) -> list[NormalizedLevel] | str:
    """Normalize one side of the book. Returns list or error string."""
    parsed: dict[Decimal, Decimal] = {}
    for entry in raw_levels:
        try:
            price = _parse_decimal(entry[0])
            size = _parse_decimal(entry[1])
        except (InvalidOperation, TypeError, ValueError):
            return DEPTH_LEVELS_MALFORMED

        if price is None or size is None:
            return DEPTH_LEVELS_MALFORMED
        if price < 0 or price > 1:
            return DEPTH_LEVELS_MALFORMED
        if size <= 0:
            continue  # ignore zero/negative size

        # Aggregate duplicate prices
        parsed[price] = parsed.get(price, Decimal("0")) + size

    if not parsed:
        return []

    # Sort deterministically
    if side == "bid":
        sorted_prices = sorted(parsed.keys(), reverse=True)
    else:
        sorted_prices = sorted(parsed.keys())  # ask asc

    levels: list[NormalizedLevel] = []
    cum_notional = Decimal("0")

    for price in sorted_prices:
        size = parsed[price]
        notional = price * size
        cum_notional += notional

        if len(levels) >= max_levels:
            break
        if cum_notional > max_notional and levels:
            # Truncate: don't include this level if it pushes over the cap
            # unless we have no levels yet
            break

        cum_size = sum(level.size for level in levels) + size
        levels.append(NormalizedLevel(
            price=price,
            size=size,
            cumulative_size=cum_size,
            cumulative_notional=cum_notional,
        ))

    return levels


def _parse_decimal(value) -> Optional[Decimal]:
    """Parse a value to Decimal, rejecting NaN/Infinity."""
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    return d