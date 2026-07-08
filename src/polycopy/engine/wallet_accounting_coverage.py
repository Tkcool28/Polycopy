"""Read-only wallet accounting coverage report for PR24J."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Literal

from polycopy.db.wallet_identity import canonical_wallet_address
from polycopy.engine.settlement_accounting import ACCOUNTED, EXCLUDED_UNSUPPORTED_SIDE

GroupBy = Literal["trader_address", "wallet_id"]
MISSING_TRADER_ADDRESS = "missing_trader_address"
MISSING_WALLET_ID = "missing_wallet_id"
# Backward-compatible aliases if earlier local code imported these names.
MISSING_TRADER = MISSING_TRADER_ADDRESS
MISSING_WALLET = MISSING_WALLET_ID


@dataclass(frozen=True)
class WalletAccountingCoverageRow:
    identity_key: str
    group_by: str
    wallet_id: str | None = None
    trader_address: str | None = None
    source_trades: int = 0
    buy_trades: int = 0
    sell_trades: int = 0
    total_ledger_rows: int = 0
    accounted_trades: int = 0
    excluded_missing_token: int = 0
    excluded_unresolved: int = 0
    excluded_unknown: int = 0
    excluded_ambiguous: int = 0
    excluded_trades: int = 0
    excluded_unsupported_side: int = 0
    excluded_other: int = 0
    source_trades_with_trader_address: int = 0
    source_trades_missing_trader_address: int = 0
    total_cost_basis: float = 0.0
    total_payout: float = 0.0
    total_realized_pnl: float = 0.0
    roi: float | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    accounting_coverage_pct: float | None = None
    accountable_buy_coverage_pct: float | None = None
    buy_only_limitation: bool = False

    @property
    def ledger_rows(self) -> int:
        return self.total_ledger_rows

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WalletAccountingCoverageReport:
    group_by: str
    total_source_trades: int
    source_trades_with_trader_address: int
    source_trades_missing_trader_address: int
    total_buy_trades: int
    total_sell_trades: int
    total_ledger_rows: int
    accounted_trades: int
    excluded_missing_token: int
    excluded_unresolved: int
    excluded_unknown: int
    excluded_ambiguous: int
    excluded_trades: int
    excluded_unsupported_side: int
    excluded_other: int
    accounting_coverage_pct: float | None
    accountable_buy_coverage_pct: float | None
    buy_only_limitation: bool
    total_wallets: int
    mapped_wallets: int
    unmapped_trader_addresses: int
    row_count: int
    rows: list[WalletAccountingCoverageRow]

    @property
    def buy_trades(self) -> int:
        return self.total_buy_trades

    @property
    def sell_trades(self) -> int:
        return self.total_sell_trades

    def to_dict(self, *, include_rows: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        payload["rows"] = [row.to_dict() for row in self.rows] if include_rows else []
        return payload


def _conn(conn_or_db: Any) -> sqlite3.Connection:
    if isinstance(conn_or_db, sqlite3.Connection):
        return conn_or_db
    maybe_conn = getattr(conn_or_db, "conn", None)
    if isinstance(maybe_conn, sqlite3.Connection):
        return maybe_conn
    raise TypeError("conn_or_db must be a sqlite3.Connection or Database-like object")


def _pct(numerator: int, denominator: int) -> float | None:
    return None if denominator <= 0 else numerator / denominator


def _identity_for_trader(value: Any) -> str:
    return canonical_wallet_address(value) or MISSING_TRADER_ADDRESS


def _identity_for_wallet(value: Any) -> str:
    if value is None:
        return MISSING_WALLET_ID
    text = str(value).strip()
    return text or MISSING_WALLET_ID


def _fetch_wallet_maps(conn: sqlite3.Connection) -> tuple[int, dict[str, str | None], list[sqlite3.Row]]:
    rows = conn.execute("SELECT id, address, canonical_address FROM wallets").fetchall()
    by_address: dict[str, set[str]] = {}
    for row in rows:
        for col in ("canonical_address", "address"):
            canonical = canonical_wallet_address(row[col])
            if canonical:
                by_address.setdefault(canonical, set()).add(str(row["id"]))
    return (
        len(rows),
        {addr: next(iter(ids)) if len(ids) == 1 else None for addr, ids in by_address.items()},
        rows,
    )


def _merge_row(
    key: str,
    *,
    group_by: GroupBy,
    wallet_id: str | None,
    trader_address: str | None,
    source: dict[str, int],
    ledger: dict[str, int | float],
) -> WalletAccountingCoverageRow:
    source_trades = int(source.get("source_trades", 0))
    source_buy = int(source.get("buy_trades", 0))
    source_sell = int(source.get("sell_trades", 0))
    total_ledger_rows = int(ledger.get("total_ledger_rows", 0))
    accounted_trades = int(ledger.get("accounted_trades", 0))
    unsupported = int(ledger.get("excluded_unsupported_side", 0))
    buy_trades = max(source_buy, int(ledger.get("buy_trades", 0)))
    sell_trades = max(source_sell, int(ledger.get("sell_trades", 0)))
    cost_basis = float(ledger.get("total_cost_basis", 0) or 0)
    realized_pnl = float(ledger.get("total_realized_pnl", 0) or 0)
    gross_loss = float(ledger.get("gross_loss", 0) or 0)
    gross_profit = float(ledger.get("gross_profit", 0) or 0)
    won_lost = int(ledger.get("won_trades", 0)) + int(ledger.get("lost_trades", 0))
    return WalletAccountingCoverageRow(
        identity_key=key,
        group_by=group_by,
        wallet_id=wallet_id,
        trader_address=trader_address,
        source_trades=source_trades,
        buy_trades=buy_trades,
        sell_trades=sell_trades,
        total_ledger_rows=total_ledger_rows,
        accounted_trades=accounted_trades,
        excluded_missing_token=int(ledger.get("excluded_missing_token", 0)),
        excluded_unresolved=int(ledger.get("excluded_unresolved", 0)),
        excluded_unknown=int(ledger.get("excluded_unknown", 0)),
        excluded_ambiguous=int(ledger.get("excluded_ambiguous", 0)),
        excluded_trades=max(total_ledger_rows - accounted_trades, 0),
        excluded_unsupported_side=unsupported,
        excluded_other=int(ledger.get("excluded_other", 0)),
        source_trades_with_trader_address=int(source.get("source_trades_with_trader_address", 0)),
        source_trades_missing_trader_address=int(source.get("source_trades_missing_trader_address", 0)),
        total_cost_basis=cost_basis,
        total_payout=float(ledger.get("total_payout", 0) or 0),
        total_realized_pnl=realized_pnl,
        roi=None if cost_basis <= 0 else realized_pnl / cost_basis,
        win_rate=_pct(int(ledger.get("won_trades", 0)), won_lost),
        profit_factor=None if gross_loss <= 0 else gross_profit / gross_loss,
        accounting_coverage_pct=_pct(accounted_trades, total_ledger_rows),
        accountable_buy_coverage_pct=_pct(accounted_trades, buy_trades),
        buy_only_limitation=sell_trades > 0 or unsupported > 0,
    )


def build_wallet_accounting_coverage_report(
    conn_or_db: Any,
    *,
    group_by: GroupBy = "trader_address",
    limit: int | None = None,
    min_source_trades: int = 1,
    include_empty_wallets: bool = False,
) -> WalletAccountingCoverageReport:
    """Build a read-only wallet accounting coverage report.

    Totals are computed across all identities before row filtering/limiting.
    ``source_trades`` has no wallet_id, so wallet grouping keeps source rows in
    ``missing_wallet_id`` and does not infer/fabricate wallet ids.
    """
    if group_by not in {"trader_address", "wallet_id"}:
        raise ValueError("group_by must be 'trader_address' or 'wallet_id'")
    if min_source_trades < 0:
        raise ValueError("min_source_trades must be >= 0")
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")

    conn = _conn(conn_or_db)
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        total_wallets, wallet_by_address, wallet_rows = _fetch_wallet_maps(conn)

        source_by_key: dict[str, dict[str, int]] = {}
        ledger_by_key: dict[str, dict[str, int | float]] = {}
        wallet_id_by_key: dict[str, str | None] = {}
        trader_by_key: dict[str, str | None] = {}

        if group_by == "trader_address":
            for row in conn.execute(
                """
                SELECT trader_address, COUNT(*) AS source_trades,
                       SUM(CASE WHEN UPPER(COALESCE(side, '')) = 'BUY' THEN 1 ELSE 0 END) AS buy_trades,
                       SUM(CASE WHEN UPPER(COALESCE(side, '')) = 'SELL' THEN 1 ELSE 0 END) AS sell_trades
                  FROM source_trades
                 GROUP BY trader_address
                """
            ):
                key = _identity_for_trader(row["trader_address"])
                bucket = source_by_key.setdefault(
                    key,
                    {
                        "source_trades": 0,
                        "buy_trades": 0,
                        "sell_trades": 0,
                        "source_trades_with_trader_address": 0,
                        "source_trades_missing_trader_address": 0,
                    },
                )
                source_count = int(row["source_trades"] or 0)
                bucket["source_trades"] += source_count
                bucket["buy_trades"] += int(row["buy_trades"] or 0)
                bucket["sell_trades"] += int(row["sell_trades"] or 0)
                if key == MISSING_TRADER_ADDRESS:
                    bucket["source_trades_missing_trader_address"] += source_count
                else:
                    bucket["source_trades_with_trader_address"] += source_count
                trader_by_key.setdefault(key, None if key == MISSING_TRADER_ADDRESS else key)
                wallet_id_by_key.setdefault(key, wallet_by_address.get(key))

            for row in conn.execute(
                """
                SELECT trader_address, COUNT(*) AS total_ledger_rows,
                       SUM(CASE WHEN UPPER(COALESCE(side, '')) = 'BUY' THEN 1 ELSE 0 END) AS buy_trades,
                       SUM(CASE WHEN UPPER(COALESCE(side, '')) = 'SELL' THEN 1 ELSE 0 END) AS sell_trades,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? THEN 1 ELSE 0 END) AS accounted_trades,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = 'excluded_missing_token' THEN 1 ELSE 0 END) AS excluded_missing_token,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = 'excluded_unresolved' THEN 1 ELSE 0 END) AS excluded_unresolved,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = 'excluded_unknown' THEN 1 ELSE 0 END) AS excluded_unknown,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = 'excluded_ambiguous' THEN 1 ELSE 0 END) AS excluded_ambiguous,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? THEN 1 ELSE 0 END) AS excluded_unsupported_side,
                       SUM(CASE WHEN COALESCE(accounting_status, '') NOT IN (?, 'excluded_missing_token', 'excluded_unresolved', 'excluded_unknown', 'excluded_ambiguous', ?) THEN 1 ELSE 0 END) AS excluded_other,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? THEN COALESCE(cost_basis, 0) ELSE 0 END) AS total_cost_basis,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? THEN COALESCE(payout, 0) ELSE 0 END) AS total_payout,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? THEN COALESCE(realized_pnl, 0) ELSE 0 END) AS total_realized_pnl,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? AND COALESCE(realized_pnl, 0) > 0 THEN realized_pnl ELSE 0 END) AS gross_profit,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? AND COALESCE(realized_pnl, 0) < 0 THEN -realized_pnl ELSE 0 END) AS gross_loss,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? AND resolution_status = 'won' THEN 1 ELSE 0 END) AS won_trades,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? AND resolution_status = 'lost' THEN 1 ELSE 0 END) AS lost_trades
                  FROM settlement_accounting_ledger
                 GROUP BY trader_address
                """,
                (ACCOUNTED, EXCLUDED_UNSUPPORTED_SIDE, ACCOUNTED, EXCLUDED_UNSUPPORTED_SIDE, ACCOUNTED, ACCOUNTED, ACCOUNTED, ACCOUNTED, ACCOUNTED, ACCOUNTED, ACCOUNTED),
            ):
                key = _identity_for_trader(row["trader_address"])
                bucket = ledger_by_key.setdefault(key, {})
                for name in (
                    "total_ledger_rows",
                    "buy_trades",
                    "sell_trades",
                    "accounted_trades",
                    "excluded_missing_token",
                    "excluded_unresolved",
                    "excluded_unknown",
                    "excluded_ambiguous",
                    "excluded_unsupported_side",
                    "excluded_other",
                    "won_trades",
                    "lost_trades",
                ):
                    bucket[name] = bucket.get(name, 0) + int(row[name] or 0)
                for name in ("total_cost_basis", "total_payout", "total_realized_pnl", "gross_profit", "gross_loss"):
                    bucket[name] = bucket.get(name, 0) + float(row[name] or 0)
                trader_by_key.setdefault(key, None if key == MISSING_TRADER_ADDRESS else key)
                wallet_id_by_key.setdefault(key, wallet_by_address.get(key))
        else:
            row = conn.execute(
                """
                SELECT COUNT(*) AS source_trades,
                       SUM(CASE WHEN UPPER(COALESCE(side, '')) = 'BUY' THEN 1 ELSE 0 END) AS buy_trades,
                       SUM(CASE WHEN UPPER(COALESCE(side, '')) = 'SELL' THEN 1 ELSE 0 END) AS sell_trades
                  FROM source_trades
                """
            ).fetchone()
            if row and int(row["source_trades"] or 0) > 0:
                source_count = int(row["source_trades"] or 0)
                source_by_key[MISSING_WALLET_ID] = {
                    "source_trades": source_count,
                    "buy_trades": int(row["buy_trades"] or 0),
                    "sell_trades": int(row["sell_trades"] or 0),
                    "source_trades_with_trader_address": 0,
                    "source_trades_missing_trader_address": source_count,
                }
                wallet_id_by_key[MISSING_WALLET_ID] = None
            for row in conn.execute(
                """
                SELECT wallet_id, COUNT(*) AS total_ledger_rows,
                       SUM(CASE WHEN UPPER(COALESCE(side, '')) = 'BUY' THEN 1 ELSE 0 END) AS buy_trades,
                       SUM(CASE WHEN UPPER(COALESCE(side, '')) = 'SELL' THEN 1 ELSE 0 END) AS sell_trades,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? THEN 1 ELSE 0 END) AS accounted_trades,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = 'excluded_missing_token' THEN 1 ELSE 0 END) AS excluded_missing_token,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = 'excluded_unresolved' THEN 1 ELSE 0 END) AS excluded_unresolved,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = 'excluded_unknown' THEN 1 ELSE 0 END) AS excluded_unknown,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = 'excluded_ambiguous' THEN 1 ELSE 0 END) AS excluded_ambiguous,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? THEN 1 ELSE 0 END) AS excluded_unsupported_side,
                       SUM(CASE WHEN COALESCE(accounting_status, '') NOT IN (?, 'excluded_missing_token', 'excluded_unresolved', 'excluded_unknown', 'excluded_ambiguous', ?) THEN 1 ELSE 0 END) AS excluded_other,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? THEN COALESCE(cost_basis, 0) ELSE 0 END) AS total_cost_basis,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? THEN COALESCE(payout, 0) ELSE 0 END) AS total_payout,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? THEN COALESCE(realized_pnl, 0) ELSE 0 END) AS total_realized_pnl,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? AND COALESCE(realized_pnl, 0) > 0 THEN realized_pnl ELSE 0 END) AS gross_profit,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? AND COALESCE(realized_pnl, 0) < 0 THEN -realized_pnl ELSE 0 END) AS gross_loss,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? AND resolution_status = 'won' THEN 1 ELSE 0 END) AS won_trades,
                       SUM(CASE WHEN COALESCE(accounting_status, '') = ? AND resolution_status = 'lost' THEN 1 ELSE 0 END) AS lost_trades
                  FROM settlement_accounting_ledger
                 GROUP BY wallet_id
                """,
                (ACCOUNTED, EXCLUDED_UNSUPPORTED_SIDE, ACCOUNTED, EXCLUDED_UNSUPPORTED_SIDE, ACCOUNTED, ACCOUNTED, ACCOUNTED, ACCOUNTED, ACCOUNTED, ACCOUNTED, ACCOUNTED),
            ):
                key = _identity_for_wallet(row["wallet_id"])
                ledger_by_key[key] = {
                    "total_ledger_rows": int(row["total_ledger_rows"] or 0),
                    "buy_trades": int(row["buy_trades"] or 0),
                    "sell_trades": int(row["sell_trades"] or 0),
                    "accounted_trades": int(row["accounted_trades"] or 0),
                    "excluded_missing_token": int(row["excluded_missing_token"] or 0),
                    "excluded_unresolved": int(row["excluded_unresolved"] or 0),
                    "excluded_unknown": int(row["excluded_unknown"] or 0),
                    "excluded_ambiguous": int(row["excluded_ambiguous"] or 0),
                    "excluded_unsupported_side": int(row["excluded_unsupported_side"] or 0),
                    "excluded_other": int(row["excluded_other"] or 0),
                    "total_cost_basis": float(row["total_cost_basis"] or 0),
                    "total_payout": float(row["total_payout"] or 0),
                    "total_realized_pnl": float(row["total_realized_pnl"] or 0),
                    "gross_profit": float(row["gross_profit"] or 0),
                    "gross_loss": float(row["gross_loss"] or 0),
                    "won_trades": int(row["won_trades"] or 0),
                    "lost_trades": int(row["lost_trades"] or 0),
                }
                wallet_id_by_key[key] = None if key == MISSING_WALLET_ID else key

        if include_empty_wallets:
            for row in wallet_rows:
                if group_by == "wallet_id":
                    key = _identity_for_wallet(row["id"])
                    wallet_id_by_key.setdefault(key, None if key == MISSING_WALLET_ID else key)
                else:
                    key = canonical_wallet_address(row["canonical_address"]) or canonical_wallet_address(row["address"])
                    if key:
                        trader_by_key.setdefault(key, key)
                        wallet_id_by_key.setdefault(key, str(row["id"]))

        all_keys = set(source_by_key) | set(ledger_by_key) | set(wallet_id_by_key) | set(trader_by_key)
        all_rows = [
            _merge_row(
                key,
                group_by=group_by,
                wallet_id=wallet_id_by_key.get(key),
                trader_address=trader_by_key.get(key),
                source=source_by_key.get(key, {}),
                ledger=ledger_by_key.get(key, {}),
            )
            for key in all_keys
        ]

        total_source = sum(r.source_trades for r in all_rows)
        source_with = sum(r.source_trades_with_trader_address for r in all_rows)
        source_missing = sum(r.source_trades_missing_trader_address for r in all_rows)
        total_buy = sum(r.buy_trades for r in all_rows)
        total_sell = sum(r.sell_trades for r in all_rows)
        total_ledger = sum(r.total_ledger_rows for r in all_rows)
        accounted = sum(r.accounted_trades for r in all_rows)
        missing_token = sum(r.excluded_missing_token for r in all_rows)
        unresolved = sum(r.excluded_unresolved for r in all_rows)
        unknown = sum(r.excluded_unknown for r in all_rows)
        ambiguous = sum(r.excluded_ambiguous for r in all_rows)
        unsupported = sum(r.excluded_unsupported_side for r in all_rows)
        other = sum(r.excluded_other for r in all_rows)
        mapped_wallets = len({r.wallet_id for r in all_rows if r.wallet_id and r.total_ledger_rows > 0})
        unmapped = 0
        if group_by == "trader_address":
            unmapped = sum(
                1
                for r in all_rows
                if r.identity_key != MISSING_TRADER_ADDRESS and r.source_trades > 0 and r.wallet_id is None
            )

        rows = [r for r in all_rows if r.source_trades >= min_source_trades]
        rows.sort(
            key=lambda r: (r.accounting_coverage_pct is not None, r.accounting_coverage_pct or -1, r.accounted_trades, r.source_trades, r.identity_key),
            reverse=True,
        )
        if limit is not None:
            rows = rows[:limit]

        return WalletAccountingCoverageReport(
            group_by=group_by,
            total_source_trades=total_source,
            source_trades_with_trader_address=source_with,
            source_trades_missing_trader_address=source_missing,
            total_buy_trades=total_buy,
            total_sell_trades=total_sell,
            total_ledger_rows=total_ledger,
            accounted_trades=accounted,
            excluded_missing_token=missing_token,
            excluded_unresolved=unresolved,
            excluded_unknown=unknown,
            excluded_ambiguous=ambiguous,
            excluded_trades=max(total_ledger - accounted, 0),
            excluded_unsupported_side=unsupported,
            excluded_other=other,
            accounting_coverage_pct=_pct(accounted, total_ledger),
            accountable_buy_coverage_pct=_pct(accounted, total_buy),
            buy_only_limitation=total_sell > 0 or unsupported > 0,
            total_wallets=total_wallets,
            mapped_wallets=mapped_wallets,
            unmapped_trader_addresses=unmapped,
            row_count=len(rows),
            rows=rows,
        )
    finally:
        conn.row_factory = old_factory


__all__ = [
    "MISSING_TRADER_ADDRESS",
    "MISSING_WALLET_ID",
    "MISSING_TRADER",
    "MISSING_WALLET",
    "WalletAccountingCoverageReport",
    "WalletAccountingCoverageRow",
    "build_wallet_accounting_coverage_report",
]
