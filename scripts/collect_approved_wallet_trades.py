#!/usr/bin/env python3
# ruff: noqa: E402
"""Recurring approved-wallet collector. Default is true no-write preview."""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from polycopy.db.database import Database
from polycopy.ingestion.approved_wallet_collector import (
    NETWORK_TIMEOUT_S,
    collect_sync,
    resolve_wallet,
    UnsafeCollectorConfiguration,
)
from polycopy.ingestion.normalized_source_trade import SOURCE_NAME
from polycopy.ingestion.source_trade_writer import write_valid_rows
from polycopy.runtime.locks import operational_job_lock
from polycopy.runtime.memory import MemoryLimitExceeded, check_rss_limit, get_max_rss_mb_from_env
from polycopy.utils.concurrency import LockError
from ingest_real_source_trades import _RealDataApiProvider


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Bounded canonical BUY collector for one approved wallet"
    )
    p.add_argument("--wallet", help="Must exactly match POLYCOPY_APPROVED_SOURCE_WALLET")
    p.add_argument("--write", action="store_true", help="Perform one bounded DB transaction")
    p.add_argument("--db-path", default=str(ROOT / "data" / "polycopy.db"))
    p.add_argument("--lock-timeout", type=float, default=30.0)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    try:
        wallet = resolve_wallet(args.wallet)
    except UnsafeCollectorConfiguration as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    provider = _RealDataApiProvider(timeout=NETWORK_TIMEOUT_S)
    try:
        result = collect_sync(provider, wallet)
    finally:
        import asyncio

        asyncio.run(provider.aclose())
    # No-write is intentionally side-effect-free: no lock file, DB, snapshots,
    # backup, experiment, report file, migration, or writer invocation.
    if not args.write:
        print(json.dumps(result.report(writes_performed=0), sort_keys=True))
        return 0 if not result.errors else 1
    try:
        with operational_job_lock("collect", timeout=args.lock_timeout):
            check_rss_limit("approved-wallet-collect:before-write", get_max_rss_mb_from_env())
            db = Database(Path(args.db_path))
            db.connect()
            try:
                pre = {
                    r[0]
                    for r in db.conn.execute(
                        "SELECT source_trade_id FROM source_trades WHERE source=?", (SOURCE_NAME,)
                    )
                }
                outcome = write_valid_rows(
                    db, result.accepted_rows, dry_run=False, pre_existing_ids=pre
                )
            finally:
                db.close()
            check_rss_limit("approved-wallet-collect:after-write", get_max_rss_mb_from_env())
    except LockError as exc:
        print(f"error: global operational lock unavailable: {exc}", file=sys.stderr)
        return 3
    except MemoryLimitExceeded as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    report = result.report(
        existing_canonical_records=outcome.existing_duplicates_recognized,
        writes_performed=outcome.inserted,
        inserted=outcome.inserted,
        deduplicated=outcome.deduplicated,
        committed=outcome.committed,
    )
    report["errors"] += outcome.errors
    print(json.dumps(report, sort_keys=True))
    return 0 if outcome.committed and not outcome.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
