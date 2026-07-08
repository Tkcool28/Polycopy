"""Source-trade settlement helper (PR24A).

Given a source_trade row and the market-resolution truth for its
market, compute the trade's settlement status without writing to the
database. This is the pure helper that downstream layers (PR20
specialist, copy-candidate settlement, the backfill script) will
consume.

The function is intentionally read-only. The persistence layer is
responsible for converting the returned :class:`SourceTradeSettlement`
into an UPDATE statement; that UPDATE uses the operational lock and
is gated by ``backfill_resolution_truth --apply`` (or an equivalent
future ingestion path).

Allowed ``resolution_status`` values:

* ``"unresolved"`` — market truth is unresolved (no winner yet).
* ``"won"``        — trade token exactly equals the winning token.
* ``"lost"``       — winning token is known and differs from the
  trade token.
* ``"ambiguous"``  — truth is ambiguous (multiple winners or no
  matching outcome).
* ``"unknown"``    — we cannot decide (missing trade token, missing
  winning token, missing cost fields).

P/L is computed only when both ``price`` and ``quantity`` are usable
floats. Otherwise ``realized_pnl`` stays ``None`` and the caller
records status only. We never invent a number.

Binary payoff convention:

* winning trade: ``realized_pnl = (1.0 - price) * quantity``
* losing trade:  ``realized_pnl = -price * quantity``

This matches the standard Polymarket binary outcome: a ``YES`` share
pays ``$1`` if it wins, ``$0`` otherwise. Buying at ``price`` and
winning returns ``$1`` per share; cost was ``price * quantity``; so
net is ``(1 - price) * quantity``. Selling (closing a position) is
not modeled here because the price/quantity we have is the entry-side
record from ``source_trades``.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from polycopy.engine.market_resolution_truth import MarketResolutionTruth


# Allowed status values, exported as a frozenset so callers can
# validate their own writes against this contract.
SETTLEMENT_STATUSES: frozenset[str] = frozenset(
    {"unresolved", "won", "lost", "ambiguous", "unknown"}
)


@dataclass(frozen=True)
class SourceTradeSettlement:
    """Result of settling one ``source_trades`` row against a truth record.

    Fields mirror the ``source_trades`` settlement columns added in
    PR24A (v14 schema):

    * ``resolution_status`` — one of :data:`SETTLEMENT_STATUSES`.
    * ``is_winning_trade`` — ``1`` for won, ``0`` for lost,
      ``None`` for unresolved / ambiguous / unknown. Stored as
      INTEGER in the DB; the field is plain int-or-None here so
      callers don't have to think about the coercion.
    * ``winning_token_id`` — the winning token captured at the time
      of settlement. Mirrors ``source_trades.winning_token_id``.
    * ``realized_pnl`` — binary-payoff realized P/L or ``None``.
    * ``settlement_source`` — provenance tag (e.g.
      ``"backfill_resolution_truth"``).
    * ``resolved_at`` — ISO-8601 UTC timestamp of settlement.
    """

    resolution_status: str
    is_winning_trade: Optional[int]
    winning_token_id: Optional[str]
    realized_pnl: Optional[float]
    settlement_source: Optional[str]
    resolved_at: Optional[str]


def _safe_float(value: Any) -> Optional[float]:
    """Coerce a price/quantity field into ``float | None``.

    Returns ``None`` for None, empty strings, non-numeric strings,
    NaN, and infinities. SQLite stores REAL floats; we accept ints
    too.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is a subclass of int but we want a real number; treat
        # True/False as missing to avoid silent 1/0 surprises.
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _safe_str_token(value: Any) -> Optional[str]:
    """Coerce a token-id field into ``str | None`` (empty/whitespace -> None)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        s = str(value).strip()
    except Exception:
        return None
    return s or None


def settle_source_trade_against_truth(
    *,
    source_trade: Mapping[str, Any],
    market_truth: MarketResolutionTruth,
    settlement_source: str = "manual_test_fixture",
    resolved_at: Optional[str] = None,
) -> SourceTradeSettlement:
    """Compute the settlement record for one trade against one truth.

    Parameters
    ----------

    * ``source_trade`` — a dict-shaped view of the ``source_trades``
      row. Must expose at least ``token_id``, ``price``, ``quantity``.
      sqlite3.Row, dataclass, and plain dict are all supported via
      ``.get`` / ``getattr`` duck typing.
    * ``market_truth`` — the normalized truth record for the market
      this trade belongs to.
    * ``settlement_source`` — provenance tag for the
      ``settlement_source`` column.
    * ``resolved_at`` — ISO-8601 UTC timestamp. ``None`` means the
      persistence layer should stamp ``now()``.

    Returns
    -------

    A :class:`SourceTradeSettlement`. Never raises for the canonical
    error paths; program errors (non-mapping source_trade) propagate.

    Decision order
    --------------

    1. Truth is unresolved → status ``unresolved``, no P/L.
    2. Truth is ambiguous → status ``ambiguous``, no P/L.
    3. Trade has no ``token_id`` → status ``unknown``, no P/L.
    4. Truth has no ``winning_token_id`` (impossible after step 1, but
       defensive) → status ``unknown``, no P/L.
    5. Trade token == winning token → status ``won``,
       ``is_winning_trade=1``, P/L = ``(1 - price) * quantity``.
    6. Trade token != winning token → status ``lost``,
       ``is_winning_trade=0``, P/L = ``-price * quantity``.

    P/L is left ``None`` when price or quantity is missing / unusable.
    """
    # Pull values defensively; accept dict, sqlite3.Row, or any object
    # with attrs. sqlite3.Row does NOT inherit from Mapping; it only
    # supports ``row['col']`` bracket access.
    def _get(obj: Any, key: str) -> Any:
        if isinstance(obj, Mapping):
            return obj.get(key)
        if isinstance(obj, sqlite3.Row):
            try:
                return obj[key]
            except (IndexError, KeyError):
                return None
        return getattr(obj, key, None)

    trade_token = _safe_str_token(_get(source_trade, "token_id"))
    price = _safe_float(_get(source_trade, "price"))
    quantity = _safe_float(_get(source_trade, "quantity"))

    # 1. Truth unresolved.
    if not market_truth.resolved or market_truth.winning_token_id is None:
        return SourceTradeSettlement(
            resolution_status="unresolved",
            is_winning_trade=None,
            winning_token_id=None,
            realized_pnl=None,
            settlement_source=settlement_source,
            resolved_at=resolved_at,
        )

    winning_token = market_truth.winning_token_id

    # 2. Trade missing token → unknown.
    if trade_token is None:
        return SourceTradeSettlement(
            resolution_status="unknown",
            is_winning_trade=None,
            winning_token_id=winning_token,
            realized_pnl=None,
            settlement_source=settlement_source,
            resolved_at=resolved_at,
        )

    # 3. Won / lost.
    won = trade_token == winning_token
    if won:
        pnl: Optional[float]
        if price is not None and quantity is not None:
            pnl = (1.0 - price) * quantity
        else:
            pnl = None
        return SourceTradeSettlement(
            resolution_status="won",
            is_winning_trade=1,
            winning_token_id=winning_token,
            realized_pnl=pnl,
            settlement_source=settlement_source,
            resolved_at=resolved_at,
        )

    # Lost branch.
    pnl = (-price * quantity) if (price is not None and quantity is not None) else None
    return SourceTradeSettlement(
        resolution_status="lost",
        is_winning_trade=0,
        winning_token_id=winning_token,
        realized_pnl=pnl,
        settlement_source=settlement_source,
        resolved_at=resolved_at,
    )


__all__ = [
    "SETTLEMENT_STATUSES",
    "SourceTradeSettlement",
    "settle_source_trade_against_truth",
]