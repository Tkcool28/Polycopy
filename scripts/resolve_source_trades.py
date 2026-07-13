#!/usr/bin/env python3
"""PR66 bounded source-trade resolver CLI.

Read-only dry-run by default. Live truth requires --allow-live. Applying
writes requires --allow-live AND --apply AND --confirm-production-db, behind
the operational job lock, and writes ONLY source_trades via a plain sqlite3
connection (never the project Database class, so no migration can run).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polycopy.ingestion.source_trade_resolution import (  # noqa: E402
    build_market_state_provider,
    resolve_source_trades,
)
from polycopy.runtime.locks import operational_job_lock  # noqa: E402

HARD_CAP = 500
DEFAULT_LIMIT = 50
APPROVED_WALLET_ENV = "POLYCOPY_APPROVED_SOURCE_WALLET"


def _wallet_prefix(wallet: str | None) -> str | None:
    if not wallet:
        return None
    return wallet[:12]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PR66 bounded source-trades resolver")
    parser.add_argument("--wallet")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--all-statuses", dest="unresolved_only", action="store_false", default=True
    )
    parser.add_argument(
        "--unresolved-only", dest="unresolved_only", action="store_true", default=True,
        help="(default) only examine unresolved source trades",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--apply", "--write", dest="apply", action="store_true")
    parser.add_argument("--confirm-production-db", action="store_true")
    parser.add_argument("--db-path", default=str(ROOT / "data" / "polycopy.db"))
    args = parser.parse_args(argv)

    if not 1 <= args.limit <= HARD_CAP:
        parser.error(f"--limit must be 1..{HARD_CAP}")

    # Gating: apply requires all three gates.
    if args.apply and not (args.allow_live and args.confirm_production_db):
        print(
            "error: --apply requires --allow-live and --confirm-production-db",
            file=sys.stderr,
        )
        return 2

    wallet = args.wallet or ""
    if not wallet:
        import os

        wallet = os.environ.get(APPROVED_WALLET_ENV, "")

    provider = build_market_state_provider() if args.allow_live else None

    if not args.apply:
        # Read-only URI: no migration, no mutation. Dry-run proof.
        connection = sqlite3.connect(f"file:{args.db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            report = resolve_source_trades(
                connection,
                provider=provider,
                wallet=wallet or None,
                limit=args.limit,
                unresolved_only=args.unresolved_only,
                apply=False,
            ).as_dict()
        finally:
            connection.close()
    else:
        with operational_job_lock("pr66-resolve-source-trades"):
            # Plain writable sqlite3 connection (NOT project Database class):
            # avoids any auto-migration while still allowing the bounded UPDATE.
            connection = sqlite3.connect(f"file:{args.db_path}", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                report = resolve_source_trades(
                    connection,
                    provider=provider,
                    wallet=wallet or None,
                    limit=args.limit,
                    unresolved_only=args.unresolved_only,
                    apply=True,
                ).as_dict()
                connection.commit()
                report["committed"] = True
            except Exception:
                connection.rollback()
                report = resolve_source_trades(
                    connection, apply=False
                ).as_dict()  # type: ignore[call-arg]
                report["committed"] = False
                raise
            finally:
                connection.close()

    report["wallet_prefix"] = _wallet_prefix(wallet or None)
    print(json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
