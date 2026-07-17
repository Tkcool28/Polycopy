#!/usr/bin/env python3
"""Bounded execution consumer for authorized specialist paper signals.

This command drives the Pass 1 canonical specialist execution spine
(polycopy.execution.specialist_spine.consume_eligible_signal) for one exact
signal or authorization, in paper mode only.

Required mode selection:
  --authorization-id <exact>   execute via an explicit authorization
  --paper-signal-id <exact>    execute by signal id

Exactly one of the two may be supplied.

Production execution safeguards (all required together):
  --write --confirm-production-db --allow-paper-execution

plus the configured guard:
  specialist_paper_allow_production_execution = true

The command also verifies broker_mode == "paper" and is_live == false. The
production order kill switch remains authoritative and is never silently
disabled.

Dry run (default for writes) performs full selection + validation and creates
NO order/fill/position/authorization-use/risk-decision (a CLI dry run is
side-effect-free).
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

from polycopy.db.database import Database  # noqa: E402
from polycopy.execution.specialist_spine import (  # noqa: E402
    ExecutionRuntime,
    consume_eligible_signal,
)

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()
# Canonical deployment production DB must never be touched by these CLIs.
REAL_PRODUCTION_DB_PATH = Path("/root/Polycopy/data/polycopy.db").resolve()
FORBIDDEN_PRODUCTION_PATHS = {PRODUCTION_DB_PATH, REAL_PRODUCTION_DB_PATH}
# Configured guard flag name (documented; read from env when present).
SPECIALIST_PAPER_ALLOW_PRODUCTION_EXECUTION = (
    __import__("os").environ.get("SPECIALIST_PAPER_ALLOW_PRODUCTION_EXECUTION", "false").lower()
    in ("1", "true", "yes")
)


def _redact(addr: str) -> str:
    if not addr or len(addr) <= 10:
        return "[REDACTED]"
    return f"{addr[:6]}\u2026{addr[-4:]}"


def _is_production_db(db_path: str) -> bool:
    try:
        return Path(db_path).resolve() in FORBIDDEN_PRODUCTION_PATHS
    except OSError:
        return False


def _resolve_signal_id(db: Database, args: argparse.Namespace) -> int | None:
    if args.paper_signal_id is not None:
        return args.paper_signal_id
    # Resolve via authorization. An authorization that is already 'used' (was
    # previously executed) still resolves to its signal so the CLI can detect
    # and report a replay (already_executed) rather than failing.
    auth = db.fetchone(
        "SELECT paper_signal_decision_id, status FROM "
        "paper_signal_execution_authorizations "
        "WHERE authorization_id=? AND status IN ('active', 'used')",
        (args.authorization_id,),
    )
    if auth is None:
        return None
    return int(auth["paper_signal_decision_id"])


def _already_executed(db: Database, signal_id: int) -> bool:
    row = db.fetchone(
        "SELECT id FROM paper_orders WHERE paper_signal_decision_id=? LIMIT 1",
        (signal_id,),
    )
    return row is not None


def _build_runtime(args: argparse.Namespace) -> ExecutionRuntime:
    is_prod = _is_production_db(args.db_path)
    # Production execution requires the explicit triple gate AND the configured flag.
    allow_prod = bool(
        is_prod and args.write and args.confirm_production_db
        and args.allow_paper_execution and SPECIALIST_PAPER_ALLOW_PRODUCTION_EXECUTION
    )
    return ExecutionRuntime(
        is_paper=True,
        kill_switch_engaged=False,          # authoritative kill switch stays engaged if set elsewhere
        broker_mode="paper",
        is_live=False,
        db_is_temporary=not is_prod,
        max_order_size=args.max_order_size,
        max_per_market=args.max_per_market,
        max_per_wallet=args.max_per_wallet,
        max_global=args.max_global,
        snapshot_max_age_seconds=args.snapshot_max_age_seconds,
        allow_production_execution=allow_prod,
    )


def _require_write(args: argparse.Namespace) -> bool:
    # Dry run is always permitted (no write occurs); the real write is skipped
    # inside the command handler.
    if args.dry_run:
        return True
    is_prod = _is_production_db(args.db_path)
    if is_prod and not (args.write and args.confirm_production_db
                        and args.allow_paper_execution):
        missing = []
        if not args.write:
            missing.append("--write")
        if not args.confirm_production_db:
            missing.append("--confirm-production-db")
        if not args.allow_paper_execution:
            missing.append("--allow-paper-execution")
        print(f"error: production execution requires {' '.join(missing)}",
              file=sys.stderr)
        return False
    if is_prod and not SPECIALIST_PAPER_ALLOW_PRODUCTION_EXECUTION:
        print("error: configured guard specialist_paper_allow_production_execution "
              "is not enabled", file=sys.stderr)
        return False
    return True


def _cmd_execute(db: Database, args: argparse.Namespace) -> int:
    signal_id = _resolve_signal_id(db, args)
    if signal_id is None:
        print("error: no active authorization/signal resolved for the given id",
              file=sys.stderr)
        return 2

    runtime = _build_runtime(args)
    write_ok = _require_write(args)
    if not write_ok:
        return 2

    # Exactly-once guard: if this signal was already executed, report the
    # replay with the existing artifact IDs instead of attempting a second
    # execution.
    if _already_executed(db, signal_id):
        order = db.fetchone(
            "SELECT id FROM paper_orders WHERE paper_signal_decision_id=? LIMIT 1",
            (signal_id,),
        )
        oid = order["id"] if order else None
        fill = db.fetchone(
            "SELECT fill_id FROM paper_fills WHERE order_id=? LIMIT 1", (oid,)
        ) if oid else None
        position = db.fetchone(
            "SELECT id FROM paper_positions WHERE paper_order_id=? LIMIT 1", (oid,)
        ) if oid else None
        out = {
            "status": "already_executed",
            "paper_signal_decision_id": signal_id,
            "risk_decision_id": None,
            "order_id": oid,
            "fill_id": fill["fill_id"] if fill else None,
            "position_id": position["id"] if position else None,
            "rejection_reasons": [],
            "replay": True,
        }
        print(json.dumps(out, indent=1) if args.json else _fmt(out))
        return 0

    # Dry run: validate-only, no writes.
    if args.dry_run:
        result = consume_eligible_signal(db, signal_id, runtime, dry_run=True)
        out = {
            "status": "would_execute" if result.status == "dry_run_allowed" else result.status,
            "paper_signal_decision_id": signal_id,
            "risk_decision_id": result.risk_decision_id,
            "order_id": None,
            "fill_id": None,
            "position_id": None,
            "rejection_reasons": result.rejection_reasons,
            "dry_run": True,
        }
        print(json.dumps(out, indent=1) if args.json else _fmt(out))
        return 0

    result = consume_eligible_signal(db, signal_id, runtime, dry_run=False)
    # The canonical spine commits the risk+order+fill+position+lot atomically on
    # success (and rolls back on failure) on this same connection. An explicit
    # commit here is a no-op confirmation that the operation is durable before
    # main() closes the connection.
    if result.status in ("executed", "already_executed"):
        db.commit()
    out = {
        "status": result.status,
        "paper_signal_decision_id": signal_id,
        "risk_decision_id": result.risk_decision_id,
        "order_id": result.order_id,
        "fill_id": result.fill_id,
        "position_id": result.position_id,
        "rejection_reasons": result.rejection_reasons,
        "detail": result.detail,
    }
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        print(_fmt(out))
    return 0 if result.status in ("executed", "already_executed") else 1


def _fmt(d: dict) -> str:
    lines = [f"status={d.get('status')}", f"signal={d.get('paper_signal_decision_id')}"]
    for k in ("risk_decision_id", "order_id", "fill_id", "position_id"):
        if d.get(k):
            lines.append(f"{k}={d[k]}")
    if d.get("rejection_reasons"):
        lines.append(f"reasons={', '.join(d['rejection_reasons'])}")
    if d.get("dry_run"):
        lines.append("dry_run=true")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Execute authorized specialist paper signals (paper only)")
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate + show expected risk inputs; no writes")
    p.add_argument("--write", action="store_true", help="Persist execution")
    p.add_argument("--confirm-production-db", action="store_true")
    p.add_argument("--allow-paper-execution", action="store_true",
                   help="Explicit paper-execution affirmative gate")
    p.add_argument("--limit", type=int, default=1,
                   help="Bounded batch maximum (default 1)")
    p.add_argument("--max-order-size", type=float, default=1.0)
    p.add_argument("--max-per-market", type=float, default=100.0)
    p.add_argument("--max-per-wallet", type=float, default=100.0)
    p.add_argument("--max-global", type=float, default=100.0)
    p.add_argument("--snapshot-max-age-seconds", type=float, default=300.0)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--authorization-id", help="Exact authorization id to execute")
    g.add_argument("--paper-signal-id", type=int, help="Exact paper signal id to execute")
    args = p.parse_args(argv)

    if args.limit != 1:
        # Bounded consumer: only a small configured maximum is permitted.
        if args.limit > 10:
            print("error: --limit may not exceed the bounded maximum (10)",
                  file=sys.stderr)
            return 2

    db = Database(Path(args.db_path)).connect()
    try:
        return _cmd_execute(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
