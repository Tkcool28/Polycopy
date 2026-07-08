#!/usr/bin/env python3
"""Read-only wallet/trader accounting coverage report (PR24J)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from polycopy.config.settings import get_settings
from polycopy.engine.wallet_accounting_coverage import (
    WalletAccountingCoverageReport,
    build_wallet_accounting_coverage_report,
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="report_wallet_accounting_coverage",
        description="Read-only PR24J wallet/trader accounting coverage report.",
    )
    p.add_argument("--group-by", choices=["trader_address", "wallet_id"], default="trader_address")
    p.add_argument("--limit", type=int, default=None, help="Limit output rows only; totals are unfiltered.")
    p.add_argument("--json", action="store_true", help="Emit parseable JSON.")
    p.add_argument("--min-source-trades", type=int, default=1, help="Minimum source trades per output row.")
    p.add_argument("--include-rows", action="store_true", help="Include row detail in JSON output.")
    p.add_argument("--include-empty-wallets", action="store_true", help="Include wallets with no source/ledger rows.")
    return p


def _json_report(report: WalletAccountingCoverageReport, *, include_rows: bool) -> dict[str, Any]:
    data = asdict(report)
    if not include_rows:
        data.pop("rows", None)
    return data


def _pct(value: float | None) -> str:
    return "None" if value is None else f"{value:.6f}"


def _human(report: WalletAccountingCoverageReport, *, group_by: str) -> str:
    lines: list[str] = []
    lines.append("Wallet Accounting Coverage Report")
    lines.append(f"Grouping: {group_by}")
    lines.append("")
    if report.total_ledger_rows == 0:
        lines.append("settlement_accounting_ledger has 0 rows.")
        lines.append("Run build_settlement_accounting_ledger.py --dry-run first to preview ledger rows.")
        lines.append("Do not run --apply on production without approval.")
        lines.append("")
    if group_by == "wallet_id" and report.mapped_wallets == 0:
        lines.append("wallet_id linkage is not populated; report will not fabricate wallet IDs from trader_address.")
        lines.append("")
    lines.append("Totals:")
    for name in [
        "total_source_trades",
        "source_trades_with_trader_address",
        "source_trades_missing_trader_address",
        "total_wallets",
        "mapped_wallets",
        "unmapped_trader_addresses",
        "total_ledger_rows",
        "accounted_trades",
        "excluded_missing_token",
        "excluded_unresolved",
        "excluded_unknown",
        "excluded_ambiguous",
        "excluded_unsupported_side",
        "excluded_other",
        "buy_trades",
        "sell_trades",
    ]:
        lines.append(f"- {name}: {getattr(report, name)}")
    lines.append(f"- coverage %: {_pct(report.accounting_coverage_pct)}")
    lines.append(f"- accountable BUY coverage %: {_pct(report.accountable_buy_coverage_pct)}")
    lines.append(f"- BUY-only limitation: {report.buy_only_limitation}")
    lines.append("")
    lines.append("Top coverage rows:")
    lines.append(
        "identity_key | source_trades | ledger_rows | accounted | missing_token | "
        "unresolved | unknown | ambiguous | unsupported_side | coverage_pct | pnl"
    )
    for row in report.rows:
        lines.append(
            f"{row.identity_key} | {row.source_trades} | {row.ledger_rows} | "
            f"{row.accounted_trades} | {row.excluded_missing_token} | "
            f"{row.excluded_unresolved} | {row.excluded_unknown} | "
            f"{row.excluded_ambiguous} | {row.excluded_unsupported_side} | "
            f"{_pct(row.accounting_coverage_pct)} | {row.total_realized_pnl:.6f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db_path = Path(get_settings().db_path)
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = build_wallet_accounting_coverage_report(
            conn,
            group_by=args.group_by,
            limit=args.limit,
            min_source_trades=args.min_source_trades,
            include_empty_wallets=args.include_empty_wallets,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(_json_report(report, include_rows=args.include_rows), indent=2, default=str))
    else:
        print(_human(report, group_by=args.group_by))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
