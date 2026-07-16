#!/usr/bin/env python3
"""Manual execution-authorization CLI for approved specialist paper signals.

Operations: authorize, inspect, list, revoke.

An execution authorization is the explicit human gate that permits ONE eligible
paper signal to be executed (consumed) by the canonical specialist spine
(scripts/execute_authorized_specialist_signals.py). It does NOT create an order
and grants no authority beyond a single future execution attempt.

Production safeguards:
  * Writes to the production DB require BOTH --write AND --confirm-production-db.
  * --dry-run (default for write ops) performs no write and returns the proposed
    authorization id without persisting.
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
from polycopy.execution.specialist_approval import get_approval, normalize_wallet  # noqa: E402
from polycopy.execution.specialist_spine import (  # noqa: E402
    create_execution_authorization,
)

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()
# Canonical deployment production DB must never be touched by these CLIs.
REAL_PRODUCTION_DB_PATH = Path("/root/Polycopy/data/polycopy.db").resolve()
FORBIDDEN_PRODUCTION_PATHS = {PRODUCTION_DB_PATH, REAL_PRODUCTION_DB_PATH}

# Frozen eligible verdicts (mirror specialist_spine.ELIGIBLE_*_VERDICT).
ELIGIBLE_SIGNAL_VERDICT = "copy_candidate"
ELIGIBLE_COPYABILITY_VERDICT = "copy_candidate"
DEFAULT_POLICY_VERSION = "specialist_paper_execution_v1"


def _redact(addr: str) -> str:
    if not addr:
        return addr
    if len(addr) <= 10:
        return "[REDACTED]"
    return f"{addr[:6]}\u2026{addr[-4:]}"


def _is_production_db(db_path: str) -> bool:
    try:
        return Path(db_path).resolve() in FORBIDDEN_PRODUCTION_PATHS
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


def _fetch_authorization_row(db: Database, authorization_id: str) -> dict | None:
    row = db.fetchone(
        "SELECT * FROM paper_signal_execution_authorizations WHERE authorization_id=?",
        (authorization_id,),
    )
    return dict(row) if row is not None else None


def _validate_authorization_inputs(db: Database, *, paper_signal_decision_id: int,
                                   specialist_approval_id: str) -> list[str]:
    """Return a list of reason codes; empty means the authorization is permissible."""
    reasons: list[str] = []

    # 1. Approval exists / enabled / not revoked.
    try:
        ap = get_approval(db, specialist_approval_id)
    except KeyError:
        return ["approval_missing"]
    if not ap.enabled or ap.revoked_at is not None:
        reasons.append("approval_disabled_or_revoked")
        return reasons

    # 2. Signal exists.
    sig = db.fetchone(
        "SELECT * FROM paper_signal_decisions WHERE id=?", (paper_signal_decision_id,)
    )
    if sig is None:
        return ["signal_missing"]
    # Normalize sqlite3.Row -> dict for optional-key access below.
    sig_d = dict(sig)

    # 3. Verdict eligible.
    if sig["final_verdict"] != ELIGIBLE_SIGNAL_VERDICT:
        reasons.append(f"signal_verdict_not_eligible:{sig['final_verdict']}")

    # 4. Legacy is_approved invariant (must remain 0).
    if int(sig_d.get("is_approved", 0)) != 0:
        reasons.append("legacy_is_approved_nonzero:contract_violation")

    # 5. Source trade belongs to approval wallet.
    st = db.fetchone(
        "SELECT trader_address FROM source_trades WHERE id=?",
        (sig["source_trade_id"],),
    )
    if st is None:
        reasons.append("source_trade_missing")
    else:
        try:
            if normalize_wallet(st["trader_address"]) != normalize_wallet(ap.wallet_address):
                reasons.append("source_trade_wallet_mismatch")
        except Exception:
            reasons.append("source_trade_wallet_unparseable")

    # 6. Category match: the durable dispatch denormalized the approval category.
    disp = db.fetchone(
        "SELECT category FROM approved_specialist_trade_dispatches "
        "WHERE specialist_approval_id=? AND source_trade_internal_id=?",
        (specialist_approval_id, sig["source_trade_id"]),
    )
    if disp is None:
        reasons.append("no_dispatch_for_approval_and_signal")
    elif disp["category"] != ap.specialist_category:
        reasons.append("category_mismatch")

    # 7. Trade-copyability decision eligible.
    tc = db.fetchone(
        "SELECT verdict FROM trade_copyability_decisions "
        "WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
        (sig["candidate_id"],),
    )
    if tc is None:
        reasons.append("copyability_decision_missing")
    elif tc["verdict"] != ELIGIBLE_COPYABILITY_VERDICT:
        reasons.append(f"copyability_not_eligible:{tc['verdict']}")

    return reasons


def _cmd_authorize(db: Database, args: argparse.Namespace) -> int:
    # Dry-run: validate and report the proposed authorization without persisting.
    reasons = _validate_authorization_inputs(
        db, paper_signal_decision_id=args.paper_signal_decision_id,
        specialist_approval_id=args.specialist_approval_id,
    )
    if reasons:
        print(f"error: authorization invalid: {', '.join(reasons)}", file=sys.stderr)
        return 1
    if args.dry_run or not _require_write(args):
        if not _require_write(args):
            return 2
        print("dry-run: authorization valid; no write performed")
        return 0
    try:
        # Re-fetch the dispatch-supplied link fields so the persisted row is
        # internally consistent (source_trade_id + candidate_id).
        sig = db.fetchone(
            "SELECT source_trade_id, candidate_id FROM paper_signal_decisions WHERE id=?",
            (args.paper_signal_decision_id,),
        )
        authorization_id = create_execution_authorization(
            db,
            paper_signal_decision_id=args.paper_signal_decision_id,
            specialist_approval_id=args.specialist_approval_id,
            source_trade_id=sig["source_trade_id"],
            candidate_id=sig["candidate_id"],
            authorized_by=args.reviewer,
            authorization_reason=args.reason,
            review_notes=args.review_notes,
            policy_version=args.policy_version,
        )
        # Persist the authorization (or no-op for an idempotent active lookup)
        # before the connection is closed in main().
        db.commit()
    except (KeyError, ValueError) as exc:
        db.rollback()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({
            "authorization_id": authorization_id,
            "paper_signal_decision_id": args.paper_signal_decision_id,
            "specialist_approval_id": args.specialist_approval_id,
            "status": "active",
        }, indent=1))
        return 0
    print(f"created authorization_id={authorization_id}")
    print(f"paper_signal_decision_id={args.paper_signal_decision_id}")
    print(f"specialist_approval_id={args.specialist_approval_id}")
    print("status=active")
    return 0


def _cmd_inspect(db: Database, args: argparse.Namespace) -> int:
    row = _fetch_authorization_row(db, args.authorization_id)
    if row is None:
        print("error: unknown authorization_id", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(row, indent=1))
    else:
        for k, v in row.items():
            print(f"{k}={v}")
    return 0


def _cmd_list(db: Database, args: argparse.Namespace) -> int:
    rows = db.fetchall(
        "SELECT * FROM paper_signal_execution_authorizations ORDER BY created_at DESC"
    )
    if not rows:
        print("(no authorizations)")
        return 0
    out = []
    for r in rows:
        d = dict(r)
        if not args.json:
            # Redact any wallet-like field if present (defensive; none stored here).
            pass
        out.append(d)
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        for r in out:
            print(f"{r['authorization_id']} signal={r['paper_signal_decision_id']} "
                  f"approval={r['specialist_approval_id']} status={r['status']} "
                  f"policy={r['policy_version']}")
    return 0


def _cmd_revoke(db: Database, args: argparse.Namespace) -> int:
    if not _require_write(args):
        return 2
    row = _fetch_authorization_row(db, args.authorization_id)
    if row is None:
        print("error: unknown authorization_id", file=sys.stderr)
        return 2
    if row["status"] in ("used", "revoked"):
        print(f"error: authorization already {row['status']}; revocation permitted "
              f"only before use", file=sys.stderr)
        return 1
    # Revocation is only allowed before the authorization is consumed.
    db.execute(
        "UPDATE paper_signal_execution_authorizations SET status='revoked', "
        "revoked_by=?, revocation_reason=?, updated_at=? WHERE authorization_id=?",
        (args.reviewer or "cli", args.reason, _now_iso(), args.authorization_id),
    )
    db.commit()
    print(f"revoked authorization_id={args.authorization_id}")
    print(f"revoked_by={args.reviewer or 'cli'}")
    print(f"reason={args.reason}")
    return 0


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Manage manual execution authorizations for specialist paper signals")
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--json", action="store_true", help="Exact output")
    sub = p.add_subparsers(dest="op", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Exact output")
    common.add_argument("--dry-run", action="store_true",
                        help="Validate and report without persisting (default for writes)")
    common.add_argument("--write", action="store_true", help="Persist mutation")
    common.add_argument("--confirm-production-db", action="store_true",
                        help="Confirm target is the production DB")

    a = sub.add_parser("authorize", parents=[common], help="Authorize one eligible signal")
    a.add_argument("--paper-signal-decision-id", type=int, required=True)
    a.add_argument("--specialist-approval-id", required=True)
    a.add_argument("--reviewer", required=True)
    a.add_argument("--reason", required=True)
    a.add_argument("--review-notes")
    a.add_argument("--policy-version", default=DEFAULT_POLICY_VERSION)

    i = sub.add_parser("inspect", parents=[common], help="Inspect one authorization")
    i.add_argument("--authorization-id", required=True)
    i.add_argument("--exact", action="store_true",
                   help="Require an exact id match (no fuzzy fallback)")

    sub.add_parser("list", parents=[common], help="List authorizations")

    r = sub.add_parser("revoke", parents=[common], help="Revoke an unused authorization")
    r.add_argument("--authorization-id", required=True)
    r.add_argument("--reviewer", default="cli")
    r.add_argument("--reason", default="manual_revocation")

    args = p.parse_args(argv)
    if _is_production_db(args.db_path):
        print("error: refusing to operate against the production database "
              "(/root/Polycopy/data/polycopy.db). These CLIs are test/temp only.",
              file=sys.stderr)
        return 2
    db = Database(Path(args.db_path)).connect()
    try:
        if args.op == "authorize":
            return _cmd_authorize(db, args)
        if args.op == "inspect":
            return _cmd_inspect(db, args)
        if args.op == "list":
            return _cmd_list(db, args)
        if args.op == "revoke":
            return _cmd_revoke(db, args)
        p.error("unknown operation")
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
