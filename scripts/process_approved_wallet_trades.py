#!/usr/bin/env python3
# ruff: noqa: E402
"""PR25A approved-wallet trade bridge; dry-run is the default."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import httpx

from polycopy.adapters.polymarket import PolymarketPublicAdapter
from polycopy.adapters.polymarket_clob import PolymarketClobClient
from polycopy.config.settings import Settings
from polycopy.db.database import Database
from polycopy.engine.approved_wallet_trade_bridge import (
    BridgeDependencies,
    MAX_LIMIT,
    _issue_write_capability,
    process_approved_wallet_trades,
)
from polycopy.ingestion.approved_wallet_collector import (
    UnsafeCollectorConfiguration,
    resolve_wallet,
)
from polycopy.runtime.locks import operational_job_lock
from polycopy.runtime.memory import MemoryLimitExceeded, check_rss_limit, get_max_rss_mb_from_env
from polycopy.utils.concurrency import LockError


class _ReadOnlyDb:
    """Small DB facade: no migration/pragma/metadata write is possible in dry run."""

    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        self.conn.close()


def _summary(report: dict) -> str:
    return (
        f"PR25A mode={report['mode']} wallet={report['wallet']} limit={report['limit']} "
        f"selected={report['selected']} rows={len(report['rows'])} "
        f"failures={len(report['failures'])} writes={report['write_counts']} "
        f"forbidden_delta={report['forbidden_table_delta']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded approved-wallet paper bridge (dry run by default)"
    )
    parser.add_argument("--wallet", help="Must exactly match POLYCOPY_APPROVED_SOURCE_WALLET")
    parser.add_argument(
        "--source-trade-id",
        help="Exact public external source_trade_id; internal IDs are rejected by lookup",
    )
    parser.add_argument("--limit", type=int, required=True, help=f"1..{MAX_LIMIT}")
    parser.add_argument(
        "--write", action="store_true", help="Persist only PR25A allowlisted evidence tables"
    )
    parser.add_argument("--db-path", default=str(ROOT / "data" / "polycopy.db"))
    parser.add_argument("--lock-timeout", type=float, default=30.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        wallet = resolve_wallet(args.wallet)
        if args.limit <= 0 or args.limit > MAX_LIMIT:
            raise ValueError(f"--limit must be between 1 and {MAX_LIMIT}")
    except (UnsafeCollectorConfiguration, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    settings = Settings()
    adapter = PolymarketPublicAdapter(settings.gamma_base_url, settings.clob_base_url, timeout=10.0)
    http = httpx.AsyncClient(base_url=settings.clob_base_url, timeout=10.0)
    clob = PolymarketClobClient(
        http_client=http,
        base_url=settings.clob_base_url,
        timeout_seconds=10.0,
        max_retries=min(3, settings.clob_max_retries),
        requests_per_minute=settings.clob_rpm,
    )
    deps = BridgeDependencies(gamma=adapter, clob=clob)
    db = None
    try:
        if args.write:
            with operational_job_lock("scan", timeout=args.lock_timeout):
                check_rss_limit("pr25a:before-write", get_max_rss_mb_from_env())
                db = Database(Path(args.db_path)).connect()
                report = process_approved_wallet_trades(
                    db,
                    wallet=wallet,
                    limit=args.limit,
                    dependencies=deps,
                    write=True,
                    write_authorization=_issue_write_capability(),
                    source_trade_id=args.source_trade_id,
                ).as_dict()
                check_rss_limit("pr25a:after-write", get_max_rss_mb_from_env())
        else:
            db = _ReadOnlyDb(Path(args.db_path))
            report = process_approved_wallet_trades(
                db,
                wallet=wallet,
                limit=args.limit,
                dependencies=deps,
                write=False,
                source_trade_id=args.source_trade_id,
            ).as_dict()
    except LockError as exc:
        print(f"error: global operational lock unavailable: {exc}", file=sys.stderr)
        return 3
    except MemoryLimitExceeded as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()
        asyncio.run(adapter.aclose())
        asyncio.run(http.aclose())
    print(json.dumps(report, sort_keys=True))
    if not args.json:
        print(_summary(report))
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
