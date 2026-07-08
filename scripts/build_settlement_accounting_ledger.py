#!/usr/bin/env python3
"""Build the PR24I settlement accounting ledger.

Default mode is a dry-run. ``--apply`` is required to write rows, and
apply mode acquires the global operational lock. The script reads only
persisted ``source_trades`` settlement truth; it never fetches live data
or runs collection/settlement/backfill jobs.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from polycopy.config.settings import get_settings
from polycopy.db.database import Database
from polycopy.engine.settlement_accounting import (
    ACCOUNTED,
    ACCOUNTING_STATUSES,
    SettlementAccountingEntry,
    aggregate_accounting_entries,
    build_settlement_accounting_entry,
)
from polycopy.runtime.locks import operational_job_lock
from polycopy.utils.concurrency import LockError

logger = logging.getLogger("build_settlement_accounting_ledger")


@dataclass
class LedgerBuildReport:
    dry_run: bool
    trades_seen: int = 0
    ledger_rows_planned: int = 0
    ledger_rows_written: int = 0
    accounted: int = 0
    excluded_unresolved: int = 0
    excluded_unknown: int = 0
    excluded_ambiguous: int = 0
    excluded_missing_token: int = 0
    excluded_missing_price: int = 0
    excluded_missing_quantity: int = 0
    excluded_unsupported_side: int = 0
    total_cost_basis: float = 0.0
    total_payout: float = 0.0
    total_realized_pnl: float = 0.0
    roi: float | None = None
    profit_factor: float | None = None
    win_rate: float | None = None
    started_at: str = ""
    finished_at: str = ""
    rows: list[dict[str, Any]] | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="build_settlement_accounting_ledger",
        description=(
            "Build PR24I settlement accounting ledger rows from existing "
            "source_trades settlement truth. Defaults to dry-run."
        ),
    )
    p.add_argument("--dry-run", action="store_true", help="Print planned writes only.")
    p.add_argument("--apply", action="store_true", help="Write ledger rows.")
    p.add_argument("--limit", type=int, default=None, help="Maximum source trades to scan.")
    p.add_argument("--wallet-id", default=None, help="Limit to a wallet_id if present.")
    p.add_argument("--trader-address", default=None, help="Limit to source_trades.trader_address.")
    p.add_argument("--market-id", default=None, help="Limit to joined markets.id.")
    p.add_argument("--json", action="store_true", help="Emit parseable JSON.")
    p.add_argument("--lock-timeout", type=float, default=30.0, help="Apply lock timeout seconds.")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose logging.")
    return p


def _fetch_source_trades(
    db: Database,
    *,
    wallet_id: str | None,
    trader_address: str | None,
    market_id: str | None,
    limit: int | None,
) -> list[sqlite3.Row]:
    # source_trades does not currently have a wallet_id column. Keep the
    # projected field NULL so the ledger schema is ready for future links.
    sql_parts = [
        """
        SELECT st.id AS id,
               NULL AS wallet_id,
               st.trader_address AS trader_address,
               m.id AS market_id,
               st.market_source_id AS market_source_id,
               st.token_id AS token_id,
               st.winning_token_id AS winning_token_id,
               st.side AS side,
               st.outcome AS outcome,
               st.quantity AS quantity,
               st.price AS price,
               st.resolution_status AS resolution_status,
               st.is_winning_trade AS is_winning_trade,
               st.settlement_source AS settlement_source,
               st.resolved_at AS resolved_at,
               st.timestamp AS timestamp
          FROM source_trades st
          LEFT JOIN markets m
            ON m.source_id = st.market_source_id
        """
    ]
    where: list[str] = []
    params: list[Any] = []
    if wallet_id is not None:
        # No runtime writer populates source_trades.wallet_id today. This
        # filter returns no rows rather than guessing a wallet mapping.
        where.append("0 = 1")
    if trader_address is not None:
        where.append("st.trader_address = ?")
        params.append(trader_address)
    if market_id is not None:
        where.append("m.id = ?")
        params.append(market_id)
    if where:
        sql_parts.append("WHERE " + " AND ".join(where))
    sql_parts.append("ORDER BY COALESCE(st.resolved_at, st.timestamp, st.id), st.id")
    if limit is not None and limit > 0:
        sql_parts.append("LIMIT ?")
        params.append(int(limit))
    return list(db.conn.execute("\n".join(sql_parts), tuple(params)).fetchall())


def _ledger_id(entry: SettlementAccountingEntry) -> str:
    return f"settlement-ledger:{entry.source_trade_id}"


def _entry_params(entry: SettlementAccountingEntry, now: str) -> tuple[Any, ...]:
    return (
        _ledger_id(entry),
        entry.source_trade_id,
        entry.wallet_id,
        entry.trader_address,
        entry.market_id,
        entry.market_source_id,
        entry.token_id,
        entry.winning_token_id,
        entry.side,
        entry.outcome,
        entry.quantity,
        entry.price,
        entry.cost_basis,
        entry.payout,
        entry.realized_pnl,
        entry.roi,
        entry.resolution_status,
        entry.is_winning_trade,
        entry.accounting_status,
        entry.accounting_reason,
        entry.settlement_source,
        entry.resolved_at,
        now,
        now,
    )


def _upsert_entries(db: Database, entries: list[SettlementAccountingEntry]) -> int:
    if not entries:
        return 0
    now = _now_iso()
    before = db.conn.total_changes
    db.conn.executemany(
        """
        INSERT INTO settlement_accounting_ledger (
            id, source_trade_id, wallet_id, trader_address, market_id,
            market_source_id, token_id, winning_token_id, side, outcome,
            quantity, price, cost_basis, payout, realized_pnl, roi,
            resolution_status, is_winning_trade, accounting_status,
            accounting_reason, settlement_source, resolved_at, created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_trade_id) DO UPDATE SET
            wallet_id = excluded.wallet_id,
            trader_address = excluded.trader_address,
            market_id = excluded.market_id,
            market_source_id = excluded.market_source_id,
            token_id = excluded.token_id,
            winning_token_id = excluded.winning_token_id,
            side = excluded.side,
            outcome = excluded.outcome,
            quantity = excluded.quantity,
            price = excluded.price,
            cost_basis = excluded.cost_basis,
            payout = excluded.payout,
            realized_pnl = excluded.realized_pnl,
            roi = excluded.roi,
            resolution_status = excluded.resolution_status,
            is_winning_trade = excluded.is_winning_trade,
            accounting_status = excluded.accounting_status,
            accounting_reason = excluded.accounting_reason,
            settlement_source = excluded.settlement_source,
            resolved_at = excluded.resolved_at,
            updated_at = excluded.updated_at
        """,
        [_entry_params(entry, now) for entry in entries],
    )
    return db.conn.total_changes - before


def _report_from_entries(
    *,
    dry_run: bool,
    trades_seen: int,
    entries: list[SettlementAccountingEntry],
    rows: bool,
) -> LedgerBuildReport:
    summary = aggregate_accounting_entries(entries)
    report = LedgerBuildReport(
        dry_run=dry_run,
        trades_seen=trades_seen,
        ledger_rows_planned=len(entries),
        accounted=summary.accounted_trades,
        total_cost_basis=summary.total_cost_basis,
        total_payout=summary.total_payout,
        total_realized_pnl=summary.total_realized_pnl,
        roi=summary.roi,
        profit_factor=summary.profit_factor,
        win_rate=summary.win_rate,
        started_at=_now_iso(),
    )
    for status in ACCOUNTING_STATUSES:
        if status == ACCOUNTED:
            continue
        if hasattr(report, status):
            setattr(report, status, sum(1 for e in entries if e.accounting_status == status))
    if rows:
        report.rows = [asdict(entry) for entry in entries]
    return report


def build_report(
    db: Database,
    *,
    dry_run: bool,
    wallet_id: str | None,
    trader_address: str | None,
    market_id: str | None,
    limit: int | None,
    include_rows: bool = False,
) -> tuple[LedgerBuildReport, list[SettlementAccountingEntry]]:
    trades = _fetch_source_trades(
        db,
        wallet_id=wallet_id,
        trader_address=trader_address,
        market_id=market_id,
        limit=limit,
    )
    entries = [build_settlement_accounting_entry(row) for row in trades]
    report = _report_from_entries(
        dry_run=dry_run,
        trades_seen=len(trades),
        entries=entries,
        rows=include_rows,
    )
    return report, entries


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)
    dry_run = not args.apply

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    db = Database(Path(get_settings().db_path)).connect()

    def _run() -> int:
        report, entries = build_report(
            db,
            dry_run=dry_run,
            wallet_id=args.wallet_id,
            trader_address=args.trader_address,
            market_id=args.market_id,
            limit=args.limit,
            include_rows=args.json,
        )
        if not dry_run:
            report.ledger_rows_written = _upsert_entries(db, entries)
            db.conn.commit()
        report.finished_at = _now_iso()
        if args.json:
            print(json.dumps(asdict(report), indent=2, default=str))
        else:
            mode = "dry-run" if dry_run else "APPLIED"
            print(f"Settlement accounting ledger ({mode}) summary:")
            for key, value in asdict(report).items():
                if key != "rows":
                    print(f"  {key}: {value}")
        return 0

    try:
        if dry_run:
            return _run()
        with operational_job_lock(
            "settlement_accounting_ledger",
            timeout=float(args.lock_timeout),
        ):
            return _run()
    except LockError:
        print(
            "ERROR: operational lock unavailable; another job is running. "
            "Re-run with --dry-run or wait for the other job to finish.",
            file=sys.stderr,
        )
        return 3
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
