#!/usr/bin/env python3
"""Research-only specialist evidence watchlist CLI.

Operations: add, pause, resume, retire, list, inspect.

Production safeguards (PR68 pattern):
  * Writes to the production DB require BOTH --write AND --confirm-production-db.
  * --dry-run (default for write ops) performs no write.
  * Sample wallets are rejected; one active watch per wallet.
  * This CLI NEVER creates a specialist_approval, dispatch, candidate, or
    execution artifact. Watchlist membership grants research permission only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _cand in (_REPO_ROOT / "src", _REPO_ROOT / "scripts", _REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from polycopy.ingestion import specialist_evidence_watchlist as wl  # noqa: E402
from evidence_db import (  # noqa: E402
    open_readonly,
    open_writable,
    require_write_gates,
)

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()


def _cmd_add(db, args: argparse.Namespace) -> int:
    try:
        wid = wl.add_watch(
            db, wallet_id=args.wallet_id, source=args.source, reason=args.reason,
            created_by=args.created_by, max_new_trades_per_run=args.max_new_trades_per_run,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"added watch_id={wid} wallet={args.wallet_id} status=active")
    return 0


def _cmd_pause(db, args: argparse.Namespace) -> int:
    ok = wl.pause_watch(db, args.watch_id)
    print(f"{'paused' if ok else 'no-op'} watch_id={args.watch_id}")
    return 0 if ok else 1


def _cmd_resume(db, args: argparse.Namespace) -> int:
    ok = wl.resume_watch(db, args.watch_id)
    print(f"{'resumed' if ok else 'no-op'} watch_id={args.watch_id}")
    return 0 if ok else 1


def _cmd_retire(db, args: argparse.Namespace) -> int:
    ok = wl.retire_watch(db, args.watch_id)
    print(f"{'retired' if ok else 'no-op'} watch_id={args.watch_id}")
    return 0 if ok else 1


def _cmd_list(db, args: argparse.Namespace) -> int:
    rows = wl.list_watches(db, status=args.status)
    if args.json:
        print(json.dumps(rows, indent=1))
    else:
        if not rows:
            print("(no watches)")
        for r in rows:
            print(f"{r['id']} wallet={r['wallet_id']} status={r['status']} "
                  f"source={r['source']} last_collection={r['last_collection_at']}")
    return 0


def _cmd_inspect(db, args: argparse.Namespace) -> int:
    r = wl.inspect_watch(db, args.watch_id)
    if r is None:
        print("error: unknown watch_id", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(r, indent=1))
    else:
        for k, v in r.items():
            print(f"{k}={v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Manage research-only evidence watchlist")
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="op", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true")
    common.add_argument("--write", action="store_true")
    common.add_argument("--allow-live", action="store_true",
                        help="Authorize network/Gamma access for this write")
    common.add_argument("--confirm-production-db", action="store_true")

    a = sub.add_parser("add", parents=[common], help="Add an active watch")
    a.add_argument("--wallet-id", required=True)
    a.add_argument("--source", default="manual", choices=["manual", "discovery"])
    a.add_argument("--reason")
    a.add_argument("--created-by", default="cli")
    a.add_argument("--max-new-trades-per-run", type=int, default=25)

    pp = sub.add_parser("pause", parents=[common], help="Pause a watch")
    pp.add_argument("--watch-id", required=True)
    pr = sub.add_parser("resume", parents=[common], help="Resume a paused watch")
    pr.add_argument("--watch-id", required=True)
    rt = sub.add_parser("retire", parents=[common], help="Retire a watch")
    rt.add_argument("--watch-id", required=True)

    li = sub.add_parser("list", parents=[common], help="List watches (read-only)")
    li.add_argument("--status", choices=["active", "paused", "retired"])

    ins = sub.add_parser("inspect", parents=[common], help="Inspect a watch")
    ins.add_argument("--watch-id", required=True)

    args = p.parse_args(argv)

    # Write ops (add/pause/resume/retire) require the production gates.
    write_ops = {"add", "pause", "resume", "retire"}
    if args.op in write_ops:
        if not require_write_gates(args, db_path=args.db_path):
            print("error: production write requires --write --allow-live "
                  "--confirm-production-db", file=sys.stderr)
            return 2
        db = open_writable(args.db_path, args)
    else:
        db = open_readonly(args.db_path)
    try:
        if args.op == "add":
            return _cmd_add(db, args)
        if args.op == "pause":
            return _cmd_pause(db, args)
        if args.op == "resume":
            return _cmd_resume(db, args)
        if args.op == "retire":
            return _cmd_retire(db, args)
        if args.op == "list":
            return _cmd_list(db, args)
        if args.op == "inspect":
            return _cmd_inspect(db, args)
        p.error("unknown operation")
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
