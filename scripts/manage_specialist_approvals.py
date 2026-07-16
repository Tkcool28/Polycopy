#!/usr/bin/env python3
"""Manual specialist-approval CLI.

Operations: approve, list, inspect, disable, revoke.

Production safeguards:
  * Writes to the production DB require BOTH --write AND --confirm-production-db.
  * --dry-run (default for write ops) performs no write.
  * Only a human reviewer creates approvals; no discovery/scorer path calls this.
  * Wallet addresses are redacted in human-readable output; exact address appears
    only in explicit --json output.
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
from polycopy.execution.specialist_approval import (  # noqa: E402
    create_approval,
    get_approval,
    list_approvals,
    revoke_approval,
    set_enabled,
)

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()


def _redact(addr: str) -> str:
    if not addr:
        return addr
    if len(addr) <= 10:
        return "[REDACTED]"
    return f"{addr[:6]}…{addr[-4:]}"


def _status_of(rec) -> str:
    if rec.revoked_at is not None:
        return "revoked"
    return "active" if rec.enabled else "disabled"


def _is_production_db(db_path: str) -> bool:
    try:
        return Path(db_path).resolve() == PRODUCTION_DB_PATH
    except OSError:
        return False


def _require_write(args: argparse.Namespace) -> bool:
    if args.dry_run:
        return False
    is_prod = _is_production_db(args.db_path)
    if is_prod and not (args.write and args.confirm_production_db):
        print("error: production write requires --write --confirm-production-db",
              file=sys.stderr)
        return False
    return True


def _cmd_approve(db: Database, args: argparse.Namespace) -> int:
    if not _require_write(args):
        return 2
    try:
        rec = create_approval(
            db,
            wallet_address=args.wallet,
            specialist_category=args.category,
            wallet_score_decision_id=args.wallet_score_decision_id,
            category_score_decision_id=args.category_score_decision_id,
            formula_name=args.formula_name,
            formula_version=args.formula_version,
            evidence_fingerprint=args.evidence_fingerprint,
            evidence_report_path=args.evidence_report_reference,
            approval_reason=args.reason,
            monitoring_enabled=not args.no_monitoring,
            reviewer=args.reviewer,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"created approval_id={rec.approval_id}")
    print(f"wallet={_redact(rec.wallet_address)}")
    print(f"category={rec.specialist_category} formula_version={rec.formula_version}")
    print(f"status={_status_of(rec)}")
    return 0


def _cmd_list(db: Database, args: argparse.Namespace) -> int:
    recs = list_approvals(db)
    if args.json:
        print(json.dumps([
            {
                "approval_id": r.approval_id,
                "wallet_address": r.wallet_address if args.exact else _redact(r.wallet_address),
                "specialist_category": r.specialist_category,
                "formula_version": r.formula_version,
                "status": r.status,
                "enabled": r.enabled,
                "revoked_at": r.revoked_at,
                "monitoring_enabled": r.monitoring_enabled,
            }
            for r in recs
        ], indent=1))
    else:
        if not recs:
            print("(no approvals)")
        for r in recs:
            flag = "" if r.enabled else " [disabled]"
            rev = " [revoked]" if r.revoked_at else ""
            print(f"{r.approval_id} {_redact(r.wallet_address)} "
                  f"cat={r.specialist_category} v{r.formula_version} "
                  f"status={_status_of(r)}{flag}{rev}")
    return 0


def _cmd_inspect(db: Database, args: argparse.Namespace) -> int:
    try:
        rec = get_approval(db, args.approval_id)
    except KeyError:
        print("error: unknown approval_id", file=sys.stderr)
        return 2
    redacted = not args.exact
    out = {
        "approval_id": rec.approval_id,
        "wallet_address": rec.wallet_address if not redacted else _redact(rec.wallet_address),
        "specialist_category": rec.specialist_category,
        "wallet_score_decision_id": rec.wallet_score_decision_id,
        "category_score_decision_id": rec.category_score_decision_id,
        "formula_name": rec.formula_name,
        "formula_version": rec.formula_version,
        "evidence_fingerprint": rec.evidence_fingerprint,
        "reviewer": rec.reviewer,
        "approval_reason": rec.approval_reason,
        "evidence_report_path": rec.evidence_report_path,
        "monitoring_enabled": rec.monitoring_enabled,
        "approved_at": rec.approved_at,
        "revoked_at": rec.revoked_at,
        "revoked_by": rec.revoked_by,
        "revocation_reason": rec.revocation_reason,
        "status": _status_of(rec),
        "enabled": rec.enabled,
    }
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        for k, v in out.items():
            print(f"{k}={v}")
    return 0


def _cmd_disable(db: Database, args: argparse.Namespace) -> int:
    if not _require_write(args):
        return 2
    try:
        set_enabled(db, args.approval_id, enabled=False, updated_by=args.reviewer or "cli")
    except KeyError:
        print("error: unknown approval_id", file=sys.stderr)
        return 2
    print(f"disabled approval_id={args.approval_id}")
    return 0


def _cmd_revoke(db: Database, args: argparse.Namespace) -> int:
    if not _require_write(args):
        return 2
    try:
        rec = revoke_approval(db, args.approval_id,
                              revoked_by=args.reviewer or "cli",
                              revocation_reason=args.reason)
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"revoked approval_id={rec.approval_id}")
    print(f"revoked_at={rec.revoked_at}")
    print(f"reason={rec.revocation_reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Manage specialist approvals (manual only)")
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--json", action="store_true", help="Exact output (incl. wallet)")
    sub = p.add_subparsers(dest="op", required=True)

    # Common mutation flags shared by every subcommand so --write /
    # --confirm-production-db can be passed after the subcommand name.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        help="No write (default for mutating ops)")
    common.add_argument("--write", action="store_true", help="Persist mutation")
    common.add_argument("--confirm-production-db", action="store_true",
                        help="Confirm target is the production DB")

    a = sub.add_parser("approve", parents=[common], help="Create a manual approval")
    a.add_argument("--wallet", required=True)
    a.add_argument("--category", required=True)
    a.add_argument("--wallet-score-decision-id", required=True)
    a.add_argument("--category-score-decision-id", required=True)
    a.add_argument("--formula-name", required=True)
    a.add_argument("--formula-version", required=True)
    a.add_argument("--evidence-fingerprint", required=True)
    a.add_argument("--reviewer", required=True)
    a.add_argument("--reason", required=True)
    a.add_argument("--evidence-report-reference")
    a.add_argument("--no-monitoring", action="store_true")

    list_parser = sub.add_parser("list", parents=[common], help="List approvals (read-only)")
    list_parser.add_argument("--exact", action="store_true", help="Show full wallet")

    i = sub.add_parser("inspect", parents=[common], help="Inspect one approval (read-only)")
    i.add_argument("--approval-id", required=True)
    i.add_argument("--exact", action="store_true", help="Show full wallet")

    d = sub.add_parser("disable", parents=[common], help="Disable an approval (preserves history)")
    d.add_argument("--approval-id", required=True)
    d.add_argument("--reviewer", default="cli")

    r = sub.add_parser("revoke", parents=[common], help="Revoke an approval (preserves history)")
    r.add_argument("--approval-id", required=True)
    r.add_argument("--reason", default="manual_revocation")
    r.add_argument("--reviewer", default="cli")

    args = p.parse_args(argv)

    db = Database(Path(args.db_path)).connect()
    try:
        if args.op == "approve":
            return _cmd_approve(db, args)
        if args.op == "list":
            return _cmd_list(db, args)
        if args.op == "inspect":
            return _cmd_inspect(db, args)
        if args.op == "disable":
            return _cmd_disable(db, args)
        if args.op == "revoke":
            return _cmd_revoke(db, args)
        p.error("unknown operation")
        return 2  # unreachable
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
