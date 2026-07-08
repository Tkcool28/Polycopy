"""Pure settlement-accounting helpers for PR24I.

This module converts already-settled ``source_trades`` rows into an
accounting-ready ledger entry. It does not fetch live data, score wallets,
copy trades, or write to the database.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

ACCOUNTED = "accounted"
EXCLUDED_UNRESOLVED = "excluded_unresolved"
EXCLUDED_UNKNOWN = "excluded_unknown"
EXCLUDED_AMBIGUOUS = "excluded_ambiguous"
EXCLUDED_MISSING_TOKEN = "excluded_missing_token"
EXCLUDED_MISSING_PRICE = "excluded_missing_price"
EXCLUDED_MISSING_QUANTITY = "excluded_missing_quantity"
EXCLUDED_INVALID_PRICE = "excluded_invalid_price"
EXCLUDED_INVALID_QUANTITY = "excluded_invalid_quantity"
EXCLUDED_UNSUPPORTED_SIDE = "excluded_unsupported_side"
EXCLUDED_NO_COST_BASIS = "excluded_no_cost_basis"

ACCOUNTING_STATUSES: frozenset[str] = frozenset(
    {
        ACCOUNTED,
        EXCLUDED_UNRESOLVED,
        EXCLUDED_UNKNOWN,
        EXCLUDED_AMBIGUOUS,
        EXCLUDED_MISSING_TOKEN,
        EXCLUDED_MISSING_PRICE,
        EXCLUDED_MISSING_QUANTITY,
        EXCLUDED_INVALID_PRICE,
        EXCLUDED_INVALID_QUANTITY,
        EXCLUDED_UNSUPPORTED_SIDE,
        EXCLUDED_NO_COST_BASIS,
    }
)

RESOLUTION_TO_EXCLUSION: dict[str, str] = {
    "unresolved": EXCLUDED_UNRESOLVED,
    "unknown": EXCLUDED_UNKNOWN,
    "ambiguous": EXCLUDED_AMBIGUOUS,
}

SELL_UNSUPPORTED_REASON = "sell_side_accounting_not_supported_in_pr24i"


@dataclass(frozen=True)
class SettlementAccountingEntry:
    source_trade_id: str
    wallet_id: str | None
    trader_address: str | None
    market_id: str | None
    market_source_id: str | None
    token_id: str | None
    winning_token_id: str | None
    side: str | None
    outcome: str | None
    quantity: float | None
    price: float | None
    cost_basis: float | None
    payout: float | None
    realized_pnl: float | None
    roi: float | None
    resolution_status: str
    is_winning_trade: int | None
    accounting_status: str
    accounting_reason: str | None
    settlement_source: str | None
    resolved_at: str | None


@dataclass(frozen=True)
class SettlementAccountingSummary:
    total_trades: int
    accounted_trades: int
    excluded_trades: int

    won_trades: int
    lost_trades: int
    unknown_trades: int
    ambiguous_trades: int
    unresolved_trades: int
    missing_token_trades: int

    total_cost_basis: float
    total_payout: float
    total_realized_pnl: float
    roi: float | None

    gross_profit: float
    gross_loss: float
    profit_factor: float | None

    win_rate: float | None

    max_drawdown: float | None
    max_loss_streak: int


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    if isinstance(obj, sqlite3.Row):
        try:
            return obj[key]
        except (IndexError, KeyError):
            return None
    getter = getattr(obj, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except Exception:
            pass
    return getattr(obj, key, None)


def _str_or_none(value: Any, *, blank_is_none: bool = True) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    s = str(value)
    if blank_is_none:
        s = s.strip()
        return s or None
    return s


def _number_state(value: Any) -> tuple[float | None, str | None]:
    """Return (float_value, error_kind) where error_kind is missing/invalid/None."""
    if value is None:
        return None, "missing"
    if isinstance(value, bool):
        return None, "invalid"
    if isinstance(value, str) and value.strip() == "":
        return None, "missing"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None, "invalid"
    if math.isnan(f) or math.isinf(f):
        return None, "invalid"
    return f, None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entry(
    source_trade: Any,
    *,
    quantity: float | None,
    price: float | None,
    cost_basis: float | None,
    payout: float | None,
    realized_pnl: float | None,
    roi: float | None,
    accounting_status: str,
    accounting_reason: str | None = None,
) -> SettlementAccountingEntry:
    token_id = _str_or_none(_get(source_trade, "token_id"))
    return SettlementAccountingEntry(
        source_trade_id=str(_get(source_trade, "id") or _get(source_trade, "source_trade_id")),
        wallet_id=_str_or_none(_get(source_trade, "wallet_id")),
        trader_address=_str_or_none(_get(source_trade, "trader_address")),
        market_id=_str_or_none(_get(source_trade, "market_id")),
        market_source_id=_str_or_none(_get(source_trade, "market_source_id")),
        token_id=token_id,
        winning_token_id=_str_or_none(_get(source_trade, "winning_token_id")),
        side=_str_or_none(_get(source_trade, "side")),
        outcome=_str_or_none(_get(source_trade, "outcome")),
        quantity=quantity,
        price=price,
        cost_basis=cost_basis,
        payout=payout,
        realized_pnl=realized_pnl,
        roi=roi,
        resolution_status=str(_get(source_trade, "resolution_status") or "unresolved"),
        is_winning_trade=_int_or_none(_get(source_trade, "is_winning_trade")),
        accounting_status=accounting_status,
        accounting_reason=accounting_reason,
        settlement_source=_str_or_none(_get(source_trade, "settlement_source")),
        resolved_at=_str_or_none(_get(source_trade, "resolved_at")),
    )


def build_settlement_accounting_entry(source_trade: Any) -> SettlementAccountingEntry:
    """Build one accounting entry from a settled ``source_trades``-shaped row.

    Only won/lost BUY trades can produce accounted P&L. Ambiguous,
    unknown, unresolved, NULL-token, invalid-input, and SELL rows are
    explicitly excluded with a bounded status and no fabricated P&L.
    """
    token_raw = _get(source_trade, "token_id")
    resolution_status = str(_get(source_trade, "resolution_status") or "unresolved")
    side = (_str_or_none(_get(source_trade, "side")) or "").upper()
    price, price_error = _number_state(_get(source_trade, "price"))
    quantity, quantity_error = _number_state(_get(source_trade, "quantity"))

    # PR24A2: NULL token coverage is distinct from blank-token unknown.
    if token_raw is None:
        return _entry(
            source_trade,
            quantity=quantity,
            price=price,
            cost_basis=None,
            payout=None,
            realized_pnl=None,
            roi=None,
            accounting_status=EXCLUDED_MISSING_TOKEN,
            accounting_reason="source_trade_token_id_is_null",
        )

    if resolution_status in RESOLUTION_TO_EXCLUSION:
        return _entry(
            source_trade,
            quantity=quantity,
            price=price,
            cost_basis=None,
            payout=None,
            realized_pnl=None,
            roi=None,
            accounting_status=RESOLUTION_TO_EXCLUSION[resolution_status],
            accounting_reason=f"resolution_status_{resolution_status}",
        )

    if resolution_status not in {"won", "lost"}:
        return _entry(
            source_trade,
            quantity=quantity,
            price=price,
            cost_basis=None,
            payout=None,
            realized_pnl=None,
            roi=None,
            accounting_status=EXCLUDED_UNKNOWN,
            accounting_reason=f"unsupported_resolution_status_{resolution_status}",
        )

    if side != "BUY":
        return _entry(
            source_trade,
            quantity=quantity,
            price=price,
            cost_basis=None,
            payout=None,
            realized_pnl=None,
            roi=None,
            accounting_status=EXCLUDED_UNSUPPORTED_SIDE,
            accounting_reason=SELL_UNSUPPORTED_REASON if side == "SELL" else "only_buy_supported_in_pr24i",
        )

    if price_error == "missing":
        return _entry(
            source_trade,
            quantity=quantity,
            price=None,
            cost_basis=None,
            payout=None,
            realized_pnl=None,
            roi=None,
            accounting_status=EXCLUDED_MISSING_PRICE,
            accounting_reason="source_trade_price_missing",
        )
    if price_error == "invalid" or price is None or price < 0 or price > 1:
        return _entry(
            source_trade,
            quantity=quantity,
            price=price,
            cost_basis=None,
            payout=None,
            realized_pnl=None,
            roi=None,
            accounting_status=EXCLUDED_INVALID_PRICE,
            accounting_reason="source_trade_price_invalid",
        )
    if quantity_error == "missing":
        return _entry(
            source_trade,
            quantity=None,
            price=price,
            cost_basis=None,
            payout=None,
            realized_pnl=None,
            roi=None,
            accounting_status=EXCLUDED_MISSING_QUANTITY,
            accounting_reason="source_trade_quantity_missing",
        )
    if quantity_error == "invalid" or quantity is None or quantity < 0:
        return _entry(
            source_trade,
            quantity=quantity,
            price=price,
            cost_basis=None,
            payout=None,
            realized_pnl=None,
            roi=None,
            accounting_status=EXCLUDED_INVALID_QUANTITY,
            accounting_reason="source_trade_quantity_invalid",
        )

    cost_basis = price * quantity
    if resolution_status == "won":
        payout = 1.0 * quantity
        realized_pnl = payout - cost_basis
    else:
        payout = 0.0
        realized_pnl = -cost_basis
    roi = realized_pnl / cost_basis if cost_basis > 0 else None
    return _entry(
        source_trade,
        quantity=quantity,
        price=price,
        cost_basis=cost_basis,
        payout=payout,
        realized_pnl=realized_pnl,
        roi=roi,
        accounting_status=ACCOUNTED,
    )


def aggregate_accounting_entries(
    entries: Iterable[SettlementAccountingEntry],
) -> SettlementAccountingSummary:
    rows = list(entries)
    accounted = [e for e in rows if e.accounting_status == ACCOUNTED]
    excluded = [e for e in rows if e.accounting_status != ACCOUNTED]

    won_trades = sum(1 for e in accounted if e.resolution_status == "won")
    lost_trades = sum(1 for e in accounted if e.resolution_status == "lost")
    unknown_trades = sum(1 for e in rows if e.resolution_status == "unknown")
    ambiguous_trades = sum(1 for e in rows if e.resolution_status == "ambiguous")
    unresolved_trades = sum(1 for e in rows if e.resolution_status == "unresolved")
    missing_token_trades = sum(
        1 for e in rows if e.accounting_status == EXCLUDED_MISSING_TOKEN
    )

    total_cost_basis = sum(e.cost_basis or 0.0 for e in accounted)
    total_payout = sum(e.payout or 0.0 for e in accounted)
    total_realized_pnl = sum(e.realized_pnl or 0.0 for e in accounted)
    gross_profit = sum(e.realized_pnl or 0.0 for e in accounted if (e.realized_pnl or 0.0) > 0)
    gross_loss = abs(sum(e.realized_pnl or 0.0 for e in accounted if (e.realized_pnl or 0.0) < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    win_rate_denominator = won_trades + lost_trades
    win_rate = won_trades / win_rate_denominator if win_rate_denominator > 0 else None
    roi = total_realized_pnl / total_cost_basis if total_cost_basis > 0 else None

    max_loss_streak = 0
    current_loss_streak = 0
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0 if accounted else None
    for entry in accounted:
        pnl = entry.realized_pnl or 0.0
        if pnl < 0:
            current_loss_streak += 1
            max_loss_streak = max(max_loss_streak, current_loss_streak)
        else:
            current_loss_streak = 0
        equity += pnl
        peak = max(peak, equity)
        if max_drawdown is not None:
            max_drawdown = max(max_drawdown, peak - equity)

    return SettlementAccountingSummary(
        total_trades=len(rows),
        accounted_trades=len(accounted),
        excluded_trades=len(excluded),
        won_trades=won_trades,
        lost_trades=lost_trades,
        unknown_trades=unknown_trades,
        ambiguous_trades=ambiguous_trades,
        unresolved_trades=unresolved_trades,
        missing_token_trades=missing_token_trades,
        total_cost_basis=total_cost_basis,
        total_payout=total_payout,
        total_realized_pnl=total_realized_pnl,
        roi=roi,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        win_rate=win_rate,
        max_drawdown=max_drawdown,
        max_loss_streak=max_loss_streak,
    )


__all__ = [
    "ACCOUNTING_STATUSES",
    "ACCOUNTED",
    "EXCLUDED_AMBIGUOUS",
    "EXCLUDED_INVALID_PRICE",
    "EXCLUDED_INVALID_QUANTITY",
    "EXCLUDED_MISSING_PRICE",
    "EXCLUDED_MISSING_QUANTITY",
    "EXCLUDED_MISSING_TOKEN",
    "EXCLUDED_NO_COST_BASIS",
    "EXCLUDED_UNKNOWN",
    "EXCLUDED_UNRESOLVED",
    "EXCLUDED_UNSUPPORTED_SIDE",
    "SELL_UNSUPPORTED_REASON",
    "SettlementAccountingEntry",
    "SettlementAccountingSummary",
    "aggregate_accounting_entries",
    "build_settlement_accounting_entry",
]
