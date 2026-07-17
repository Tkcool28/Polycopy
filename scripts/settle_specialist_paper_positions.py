#!/usr/bin/env python3
"""Specialist paper position settlement CLI (canonical path only).

Settles one specialist paper position (read-only from paper_positions; never the
legacy positions table) through authoritative market-resolution evidence. Never
uses sample fallback. Never deletes the only position record. Uses only
paper_position_settlements. Replaying the same evidence returns "already_settled";
conflicting evidence blocks.

Production writes require --write --confirm-production-db. Dry run is default.
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
from polycopy.execution.specialist_spine import settle_specialist_position  # noqa: E402

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()
# Canonical deployment production DB must never be touched by these CLIs.
REAL_PRODUCTION_DB_PATH = Path("/root/Polycopy/data/polycopy.db").resolve()
FORBIDDEN_PRODUCTION_PATHS = {PRODUCTION_DB_PATH, REAL_PRODUCTION_DB_PATH}


def _is_production_db(db_path: str) -> bool:
    try:
        return Path(db_path).resolve() in FORBIDDEN_PRODUCTION_PATHS
    except OSError:
        return False


def _require_write(args: argparse.Namespace) -> bool:
    # Dry run is always permitted (no write occurs); the real write is skipped
    # inside the command handler.
    if args.dry_run:
        return True
    if _is_production_db(args.db_path) and not (args.write and args.confirm_production_db):
        print("error: production write requires --write --confirm-production-db",
              file=sys.stderr)
        return False
    return True


def _cmd_settle(db: Database, args: argparse.Namespace) -> int:
    if not _require_write(args):
        return 2
    raw_evidence = None
    if args.evidence_json:
        try:
            raw_evidence = json.loads(args.evidence_json)
        except json.JSONDecodeError as exc:
            print(f"error: invalid --evidence-json: {exc}", file=sys.stderr)
            return 2
    if args.dry_run:
        pos = db.fetchone("SELECT id, outcome, quantity, avg_entry_price FROM "
                          "paper_positions WHERE id=?", (args.position_id,))
        out = {
            "status": "dry_run",
            "position_id": args.position_id,
            "would_settle": pos is not None,
            "resolution_outcome": args.resolution_outcome,
            "evidence_source": args.evidence_source,
            "evidence_hash": args.evidence_hash,
        }
        print(json.dumps(out, indent=1) if args.json else _fmt(out))
        return 0
    res = settle_specialist_position(
        db, args.position_id,
        resolution_outcome=args.resolution_outcome,
        evidence_source=args.evidence_source,
        raw_evidence=raw_evidence,
    )
    db.commit()
    out = {
        "status": res.status,
        "position_id": res.position_id,
        "settlement_id": res.settlement_id,
        "is_winner": res.is_winner,
        "payout": res.payout,
        "realized_pnl": res.realized_pnl,
        "reason": res.reason,
    }
    print(json.dumps(out, indent=1) if args.json else _fmt(out))
    return 0 if res.status in ("settled", "already_settled") else 1


def _fmt(d: dict) -> str:
    lines = [f"status={d.get('status')}", f"position={d.get('position_id')}"]
    for k in ("settlement_id", "is_winner", "payout", "realized_pnl", "reason"):
        if d.get(k) is not None:
            lines.append(f"{k}={d[k]}")
    if d.get("dry_run"):
        lines.append("dry_run=true")
    if d.get("would_settle") is not None:
        lines.append(f"would_settle={d['would_settle']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Settle one specialist paper position")
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Validate; no write")
    p.add_argument("--write", action="store_true")
    p.add_argument("--confirm-production-db", action="store_true")
    p.add_argument("--position-id", required=True)
    p.add_argument("--resolution-outcome", required=True,
                   help="Authoritative market resolution outcome label (e.g. 'Yes' "
                        "for a winning Yes side). Compared against the position's "
                        "resolution outcome by the spine; use the raw outcome label, "
                        "not a 'resolved_*' code.")
    p.add_argument("--evidence-source", default="operator_cli",
                   help="Authoritative resolution evidence source")
    p.add_argument("--evidence-hash", default="", help="Evidence hash for audit")
    p.add_argument("--evidence-json", help="Optional raw evidence JSON")
    p.add_argument("--limit", type=int, default=1)
    args = p.parse_args(argv)

    db = Database(Path(args.db_path)).connect()
    try:
        return _cmd_settle(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
