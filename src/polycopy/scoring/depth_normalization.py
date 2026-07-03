"""Bounded order-book depth normalization for PR 4.

Parses raw CLOB order-book levels into a deterministic, normalized
structure suitable for persistence and depth-walk consumption.

Rules (Phase 5):
- parse price and size with Decimal
- reject NaN
- reject positive or negative Infinity
- reject price < 0
- reject price > 1
- reject negative size (returns DEPTH_LEVELS_MALFORMED)
- ignore zero size
- aggregate duplicate prices
- sort asks ascending, bids descending
- reject crossed books (best_bid >= best_ask)
- compute cumulative size and cumulative notional per side
- enforce max levels per side (>= 1)
- enforce max cumulative notional exactly (truncate the offending level)
- never persist a level that would push cumulative notional past max_notional

Hash canonicalization (Phase 5):
- hash is derived from the exact normalized, bounded levels
- input includes side, level_index, canonical Decimal price, canonical Decimal size
- equivalent normalized books (after dedup + sort + truncation) hash identically
- different bounded persisted books hash differently

Depth walk (Phase 7):
- BUY consumes asks ascending
- SELL consumes bids descending
- no midpoint execution
- no extrapolation beyond stored levels
- no synthetic liquidity
- fill_percentage is a Decimal ratio in [0, 1]
- slippage is a Decimal fraction; safe-zero best price yields slippage=None
- partial fills are preserved truthfully with
  DEPTH_INSUFFICIENT_FOR_STAKE
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

# ── Default capture limits (research-only; do not affect CLOB) ──────────────
DEFAULT_MAX_LEVELS_PER_SIDE = 25
DEFAULT_MAX_NOTIONAL_PER_SIDE = 100.0  # USDC-equivalent
DEPTH_BUFFER_FACTOR = 1.5

# ── Rejection / status reasons ──────────────────────────────────────────────
DEPTH_NOT_CAPTURED = "DEPTH_NOT_CAPTURED"
DEPTH_INSUFFICIENT_FOR_STAKE = "DEPTH_INSUFFICIENT_FOR_STAKE"
DEPTH_LEVELS_MALFORMED = "DEPTH_LEVELS_MALFORMED"
DEPTH_SNAPSHOT_MISMATCH = "DEPTH_SNAPSHOT_MISMATCH"


@dataclass(frozen=True, order=True)
class NormalizedLevel:
    """Single normalized order-book level.

    `price` and `size` are Decimal. `cumulative_size` and
    `cumulative_notional` are post-truncation cumulative values as
    actually persisted.
    """

    price: Decimal
    size: Decimal
    cumulative_size: Decimal = Decimal("0")
    cumulative_notional: Decimal = Decimal("0")

    @property
    def notional(self) -> Decimal:
        return self.price * self.size


@dataclass(frozen=True)
class DepthWalkResult:
    """Result of a depth walk for a given side and intended notional.

    `fill_percentage` is a Decimal ratio on [0, 1] (e.g. 0.5 for
    half fill, 1.0 for full fill). Slipped intentionally from the
    pre-Phase-7 0-100 percentage scale so the trade-score formula
    can multiply by 100 directly.

    `slippage` is a Decimal fraction (not a percent); for BUY it
    is (vwap - best_ask) / best_ask, for SELL it is
    (best_bid - vwap) / best_bid. If the best executable price is
    zero, slippage is None rather than raising.

    `insufficient_reason` is set to DEPTH_INSUFFICIENT_FOR_STAKE on
    any non-complete fill and to None on complete fills.
    """

    side: str
    intended_notional: Decimal
    filled_notional: Decimal
    fill_percentage: Decimal  # ratio in [0, 1]
    contracts_filled: Decimal
    vwap_fill_price: Optional[Decimal]
    slippage: Optional[Decimal]
    levels_consumed: int
    remaining_notional: Decimal
    is_complete: bool
    insufficient_reason: Optional[str] = None


def normalize_book_levels(
    raw_bids: list[tuple],
    raw_asks: list[tuple],
    max_levels: int = DEFAULT_MAX_LEVELS_PER_SIDE,
    max_notional: Decimal = Decimal(str(DEFAULT_MAX_NOTIONAL_PER_SIDE)),
) -> tuple[list[NormalizedLevel], list[NormalizedLevel], Optional[str]]:
    """Normalize raw order-book levels into bounded, ordered lists.

    Each raw entry should be a (price, size) tuple — strings, ints,
    or Decimals. Tuple shape is validated; non-2-tuples return
    DEPTH_LEVELS_MALFORMED.

    Returns (bids, asks, error_reason). If either side is malformed
    or the normalized books are crossed, error_reason is set and
    both bid and ask lists are empty.
    """
    if max_levels <= 0:
        return [], [], DEPTH_LEVELS_MALFORMED
    if max_notional is None or max_notional <= 0:
        return [], [], DEPTH_LEVELS_MALFORMED

    bids_or_err = _normalize_side_levels(raw_bids, "bid", max_levels, max_notional)
    if isinstance(bids_or_err, str):
        return [], [], bids_or_err

    asks_or_err = _normalize_side_levels(raw_asks, "ask", max_levels, max_notional)
    if isinstance(asks_or_err, str):
        return [], [], asks_or_err

    bids = bids_or_err
    asks = asks_or_err

    # Crossed-book detection (only when both sides have at least one
    # level — one-sided books are allowed and return success).
    if bids and asks:
        best_bid = bids[0].price
        best_ask = asks[0].price
        if best_bid >= best_ask:
            return [], [], DEPTH_LEVELS_MALFORMED

    return bids, asks, None


def compute_book_hash(
    bids: list[NormalizedLevel],
    asks: list[NormalizedLevel],
) -> str:
    """Deterministic SHA-256 hash of normalized, bounded levels.

    The hash is derived from the EXACT levels that would be
    persisted: side + level_index + canonical Decimal price +
    canonical Decimal size. It does not depend on raw input order
    or pre-aggregation shape — only on the bounded persisted book.

    Two normalized books with identical bounded content produce
    identical hashes. Two books with any difference (price, size,
    side, level_index) produce different hashes.
    """
    payload: dict[str, list[dict[str, Any]]] = {"bids": [], "asks": []}
    for idx, level in enumerate(bids):
        payload["bids"].append({
            "level_index": idx,
            "price": _canonical_decimal(level.price),
            "size": _canonical_decimal(level.size),
        })
    for idx, level in enumerate(asks):
        payload["asks"].append({
            "level_index": idx,
            "price": _canonical_decimal(level.price),
            "size": _canonical_decimal(level.size),
        })
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def walk_depth(
    levels: list[NormalizedLevel],
    side: str,
    intended_notional: Decimal,
) -> DepthWalkResult:
    """Walk order-book levels to fill an intended notional.

    BUY consumes asks ascending; SELL consumes bids descending.
    Does NOT extrapolate beyond stored levels.

    Returns a truthful DepthWalkResult. If intended_notional is
    zero or negative the result is a degenerate zero-fill with
    insufficient_reason set to DEPTH_INSUFFICIENT_FOR_STAKE.

    The `fill_percentage` is a Decimal ratio on [0, 1]. The
    trade-score formula multiplies it by 100 to bring it onto the
    0-100 component-score scale.

    `slippage` is a Decimal fraction. If the best executable price
    is zero, slippage is None (we do not raise).
    """
    if intended_notional is None or intended_notional <= 0:
        return DepthWalkResult(
            side=side,
            intended_notional=intended_notional if intended_notional is not None else Decimal("0"),
            filled_notional=Decimal("0"),
            fill_percentage=Decimal("0"),
            contracts_filled=Decimal("0"),
            vwap_fill_price=None,
            slippage=None,
            levels_consumed=0,
            remaining_notional=Decimal("0"),
            is_complete=False,
            insufficient_reason=DEPTH_INSUFFICIENT_FOR_STAKE,
        )

    if not levels:
        return DepthWalkResult(
            side=side,
            intended_notional=intended_notional,
            filled_notional=Decimal("0"),
            fill_percentage=Decimal("0"),
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
        level_notional = level.notional
        consume = level_notional if level_notional <= remaining else remaining
        if level.price > 0:
            contracts = consume / level.price
        else:
            # Zero-price level contributes zero notional; no contracts
            # are produced. We do still increment levels_consumed? No —
            # since no contracts were filled, do not credit the level.
            continue
        total_filled_notional += consume
        total_contracts += contracts
        remaining -= consume
        levels_consumed += 1

    is_complete = remaining <= Decimal("0")
    fill_pct = (
        total_filled_notional / intended_notional
        if intended_notional > 0
        else Decimal("0")
    )

    vwap: Optional[Decimal] = None
    if total_contracts > 0:
        vwap = total_filled_notional / total_contracts

    slippage: Optional[Decimal] = None
    if vwap is not None and levels:
        best_price = levels[0].price
        if best_price > 0:
            if side == "BUY":
                slippage = (vwap - best_price) / best_price
            else:
                slippage = (best_price - vwap) / best_price

    insufficient_reason: Optional[str] = None
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
        remaining_notional=remaining if remaining > 0 else Decimal("0"),
        is_complete=is_complete,
        insufficient_reason=insufficient_reason,
    )


# ── Internal helpers ────────────────────────────────────────────────────────

# Tuple shapes accepted by _parse_entry. Two-element only; any
# other shape (including None) returns None which the caller
# converts to DEPTH_LEVELS_MALFORMED.
EXPECTED_TUPLE_LEN = 2


def _normalize_side_levels(
    raw_levels: list[tuple],
    side: str,
    max_levels: int,
    max_notional: Decimal,
) -> list[NormalizedLevel] | str:
    """Normalize one side of the book.

    Returns the bounded level list on success, or an error-reason
    string on failure.

    Truncation semantics: the first level is never truncated for
    size reasons — if its full notional exceeds max_notional, the
    level is included with its full size anyway (so a single-level
    book is always persisted). For the second and subsequent
    levels, a level whose full notional would push cumulative
    notional past the cap is TRUNCATED to fit the remaining
    capacity (not dropped).
    """
    parsed: dict[Decimal, Decimal] = {}
    for entry in raw_levels:
        price, size = _parse_entry(entry)
        if price is None or size is None:
            return DEPTH_LEVELS_MALFORMED

        # Per-entry price bounds (Phase 5): reject price < 0 or > 1.
        if price < 0 or price > 1:
            return DEPTH_LEVELS_MALFORMED

        if size < 0:
            # Negative size is a malformed input, not a silent skip.
            return DEPTH_LEVELS_MALFORMED
        if size == 0:
            continue

        # Aggregate duplicate prices
        parsed[price] = parsed.get(price, Decimal("0")) + size

    if not parsed:
        return []

    if side == "bid":
        sorted_prices = sorted(parsed.keys(), reverse=True)
    else:
        sorted_prices = sorted(parsed.keys())  # ask asc

    levels: list[NormalizedLevel] = []
    cum_notional = Decimal("0")
    cum_size = Decimal("0")

    for idx, price in enumerate(sorted_prices):
        if len(levels) >= max_levels:
            break

        size = parsed[price]
        level_notional = price * size

        # Every level is subject to the same cap. If the level's full
        # notional would push cumulative notional past max_notional,
        # truncate the size to fit exactly. The cap is the cap — there
        # is no exception for the first level, and cumulative_notional
        # must never exceed max_notional. A single-level snapshot whose
        # notional alone exceeds the cap is truncated to exactly the
        # cap's notional capacity; the rest of the book is dropped.
        remaining_notional = max_notional - cum_notional
        if remaining_notional <= 0:
            break

        if level_notional <= remaining_notional:
            # Whole level fits
            cum_size += size
            cum_notional += level_notional
            levels.append(NormalizedLevel(
                price=price,
                size=size,
                cumulative_size=cum_size,
                cumulative_notional=cum_notional,
            ))
            continue

        # Truncate: include only the portion that fits the cap.
        if price > 0:
            allowed_size = remaining_notional / price
            if allowed_size <= 0:
                break
            cum_size += allowed_size
            cum_notional += remaining_notional
            levels.append(NormalizedLevel(
                price=price,
                size=allowed_size,
                cumulative_size=cum_size,
                cumulative_notional=cum_notional,
            ))
            # Cap is now exactly full; no further levels can fit.
            break

        # Zero-price level: zero notional. It's already within the
        # cap. Include it (it's a valid deterministic entry) but
        # cumulative notional stays flat. This preserves the spec
        # rule "zero-price levels have zero notional and may be
        # retained if within max-level bounds".
        if len(levels) >= max_levels:
            break
        cum_size += size
        levels.append(NormalizedLevel(
            price=price,
            size=size,
            cumulative_size=cum_size,
            cumulative_notional=cum_notional,
        ))

    return levels


def _parse_entry(entry: Any) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """Parse a raw level entry.

    Returns (price, size) or (None, None) on malformed shape /
    unparseable values / non-finite decimals.

    Shape rules:
    - Must be a 2-tuple (list/tuple of length 2). 1-tuples, 3-tuples,
      dicts, strings, and None all return (None, None).
    - Decimal(str(value)) handles int, float-as-string, Decimal, and
      Decimal exponent notation (`1E-4`, `1.5e2`, etc.).
    - Whitespace-only strings parse normally (Decimal("  0.5  ") == 0.5).
    """
    if entry is None:
        return None, None
    if not isinstance(entry, (tuple, list)):
        return None, None
    if len(entry) != EXPECTED_TUPLE_LEN:
        return None, None
    price = _parse_decimal(entry[0])
    size = _parse_decimal(entry[1])
    if price is None or size is None:
        return None, None
    return price, size


def _parse_decimal(value: Any) -> Optional[Decimal]:
    """Parse a value to a finite Decimal.

    Returns None on:
    - None
    - Unparseable strings (InvalidOperation)
    - NaN
    - +Infinity / -Infinity

    Whitespace-containing strings and Decimal exponent notation
    parse normally.
    """
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    return d


def _canonical_decimal(d: Decimal) -> str:
    """Return the canonical Decimal string form (no leading zeros,
    no trailing zeros, exponent normalized).
    """
    return format(d, "f")