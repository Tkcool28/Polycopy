#!/usr/bin/env python3
# ruff: noqa: E402
"""PR25A approved-wallet trade bridge; dry-run is the default.

Production writes require explicit dual gates (--allow-live --confirm-production-db)
AND a verified SQLite online backup of the production DB, created before any
writable connection opens. The gates authorize production-DB *persistence* only;
they never enable live order execution.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

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
from polycopy.ingestion.source_trade_writer import create_verified_backup
from polycopy.runtime.locks import operational_job_lock
from polycopy.runtime.memory import MemoryLimitExceeded, check_rss_limit, get_max_rss_mb_from_env
from polycopy.utils.concurrency import LockError


# The canonical production DB. A --write that targets exactly this path is
# treated as a production write and is subject to the dual-gate + backup rules.
PRODUCTION_DB_PATH = (ROOT / "data" / "polycopy.db").resolve()
# Approved backup naming for the PR25A first bounded write.
_BACKUP_NAME_PREFIX = "polycopy.db.pr25a_online_backup_"


def _is_production_db(db_path: str) -> bool:
    """True iff the resolved db_path matches the canonical production DB."""
    try:
        return Path(db_path).resolve() == PRODUCTION_DB_PATH
    except OSError:
        return False


def _utc_stamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _gate_production_write(args: argparse.Namespace) -> int | None:
    """Enforce explicit production-write gates.

    Returns None if the write is permitted to proceed, or an exit code (2) if
    a production write is missing a required gate. A stderr message is printed.
    No backup, no DB open, no adapter call, no bridge call happens before this.
    """
    if not args.write:
        return None
    if not _is_production_db(args.db_path):
        # Non-production (test/temp) DB write: unchanged test-safe behavior.
        return None
    if not args.allow_live:
        print(
            "error: production write to the production DB requires --allow-live",
            file=sys.stderr,
        )
        return 2
    if not args.confirm_production_db:
        print(
            "error: production write to the production DB requires "
            "--confirm-production-db",
            file=sys.stderr,
        )
        return 2
    return None


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


class _Closable:
    """Wraps async adapters + the shared httpx client so the bridge can aclose
    them on ITS OWN event loop (via ``client_close_hooks``), never on a fresh
    loop in the CLI's ``finally``. Aclose runs on the same loop that ran the
    Gamma/CLOB requests, so no transport is re-bound to a closed loop.

    The bridge calls each hook as ``hook(loop)``; we return the ``aclose``
    coroutine so the bridge awaits it on that same loop.
    """

    def __init__(self, *clients: Any) -> None:
        self._clients = list(clients)

    def __call__(self, loop: Any) -> Any:
        return self.aclose()

    async def aclose(self) -> None:
        for client in self._clients:
            if client is None:
                continue
            aclose = getattr(client, "aclose", None)
            if aclose is None:
                continue
            coro = aclose()
            if coro is not None:
                await coro


def _make_closable(adapter: Any, http: Any) -> _Closable:
    return _Closable(adapter, http)


def _summary(report: dict) -> str:
    return (
        f"PR25A mode={report['mode']} wallet={report['wallet']} limit={report['limit']} "
        f"selected={report['selected']} rows={len(report['rows'])} "
        f"failures={len(report['failures'])} writes={report['write_counts']} "
        f"forbidden_delta={report['forbidden_table_delta']}"
    )


def _backup_prod_db(db_path: str) -> dict[str, Any] | None:
    """Create + verify a SQLite online backup of the production DB.

    Returns the backup metadata dict (for JSON output) on success, or None if
    the backup/verification failed (caller aborts before any writable open).
    """
    backup_path = f"{db_path}.{_BACKUP_NAME_PREFIX}{_utc_stamp()}"
    res = create_verified_backup(db_path, backup_path=backup_path)
    if not res.success:
        print(
            f"error: production backup failed: {res.error or 'verification unsatisfied'} "
            f"(integrity={res.integrity_check}, fk={res.foreign_key_violations}, "
            f"size={res.size})",
            file=sys.stderr,
        )
        return None
    return {
        "backup_path": res.path,
        "backup_timestamp_utc": _utc_stamp() if res.path else None,
        "backup_size_bytes": res.size,
        "backup_sha256": res.sha256,
        "backup_integrity_check": res.integrity_check,
        "backup_foreign_key_check_count": res.foreign_key_violations,
        "backup_schema_version": None,  # populated below when available
    }


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
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="Authorize production-DB persistence (NOT live order execution). "
        "Required with --write against the production DB.",
    )
    parser.add_argument(
        "--confirm-production-db",
        action="store_true",
        help="Confirm the target is the production DB and a verified backup is allowed. "
        "Required with --write against the production DB.",
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

    # Production-write gate check: abort BEFORE backup/DB-open/adapter/bridge.
    gate = _gate_production_write(args)
    if gate is not None:
        return gate

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
    # One _Closable wraps the shared async clients; the bridge acloses it on the
    # SAME loop that runs the Gamma/CLOB requests (see process_approved_wallet_trades).
    closable = _make_closable(adapter, http)
    client_close_hooks = [closable]
    db = None
    report: dict | None = None
    backup_meta: dict[str, Any] | None = None
    cleanup_errors: list[dict[str, str]] = []
    try:
        if args.write:
            with operational_job_lock("scan", timeout=args.lock_timeout):
                check_rss_limit("pr25a:before-write", get_max_rss_mb_from_env())
                # Verified online backup BEFORE opening the production DB writable.
                if _is_production_db(args.db_path):
                    backup_meta = _backup_prod_db(args.db_path)
                    if backup_meta is None:
                        return 1
                db = Database(Path(args.db_path)).connect()
                # Populate schema version for backup metadata when available.
                if backup_meta is not None:
                    try:
                        backup_meta["backup_schema_version"] = db.conn.execute(
                            "PRAGMA schema_version"
                        ).fetchone()[0]
                    except Exception:  # noqa: BLE001 - metadata best-effort
                        pass
                report_obj = process_approved_wallet_trades(
                    db,
                    wallet=wallet,
                    limit=args.limit,
                    dependencies=deps,
                    write=True,
                    write_authorization=_issue_write_capability(),
                    source_trade_id=args.source_trade_id,
                    client_close_hooks=client_close_hooks,
                )
                cleanup_errors = list(getattr(report_obj, "cleanup_errors", []))
                report = report_obj.as_dict()
                if backup_meta is not None:
                    report["backup"] = backup_meta
                check_rss_limit("pr25a:after-write", get_max_rss_mb_from_env())
        else:
            db = _ReadOnlyDb(Path(args.db_path))
            report_obj = process_approved_wallet_trades(
                db,
                wallet=wallet,
                limit=args.limit,
                dependencies=deps,
                write=False,
                source_trade_id=args.source_trade_id,
                client_close_hooks=client_close_hooks,
            )
            cleanup_errors = list(getattr(report_obj, "cleanup_errors", []))
            report = report_obj.as_dict()
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
        # Print the completed report BEFORE any cleanup, so a client aclose
        # failure can never erase an already-generated report. The async client
        # cleanup already ran inside the bridge (on its own loop) and is recorded
        # in ``cleanup_errors``; here we only close the DB.
        if report is not None:
            print(json.dumps(report, sort_keys=True))
            if not args.json:
                print(_summary(report))
        # Report DB close failures loudly (no silent suppression).
        try:
            if db is not None:
                db.close()
        except Exception as exc:  # noqa: BLE001 - report, do not swallow
            cleanup_errors.append({"type": type(exc).__name__, "error": str(exc)})
        # http/adapter are closed by the bridge's client_close_hooks on its loop.
        # Drop our references so nothing else can aclose them on a fresh loop.
        http = None
        adapter = None
    # Cleanup errors force exit code 1 and are reported to stderr, but they do
    # NOT erase the JSON already printed above.
    if cleanup_errors:
        for err in cleanup_errors:
            print(f"error: cleanup failed: {err['type']}: {err['error']}", file=sys.stderr)
        return 1
    if report is None:
        return 1
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
