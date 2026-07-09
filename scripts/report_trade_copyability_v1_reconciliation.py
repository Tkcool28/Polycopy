#!/usr/bin/env python3
"""PR24O — Trade Copyability v1 reconciliation audit CLI (read-only).

Produces a reconciliation audit report for the existing Trade Copyability
Score v1 implementation. This script is strictly READ-ONLY:

  * It imports ``polycopy.engine.trade_copyability_v1_reconciliation_audit``
    (pure audit module), NOT ``polycopy.db.database`` (no write path).
  * It opens any production SQLite DB with ``mode=ro`` and never issues
    INSERT/UPDATE/DELETE/CREATE/DROP/ALTER statements.
  * It does no scoring writes, no candidate creation, no signal generation.

Usage:
  python3 scripts/report_trade_copyability_v1_reconciliation.py
  python3 scripts/report_trade_copyability_v1_reconciliation.py --json
  python3 scripts/report_trade_copyability_v1_reconciliation.py --db-path /root/Polycopy/data/polycopy.db
  python3 scripts/report_trade_copyability_v1_reconciliation.py --limit 10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure the repo ``src`` is importable when run as a script.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
for _cand in (_REPO_ROOT / "src", _REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))


def _default_db_path() -> str:
    """Best-effort resolution of the production DB for read-only counts."""
    candidates = [
        os.environ.get("POLYCOPY_DB"),
        str(_REPO_ROOT / "data" / "polycopy.db"),
        "/root/Polycopy/data/polycopy.db",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    # Fall back to the first candidate (may not exist -> report None counts).
    return candidates[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "PR24O Trade Copyability v1 reconciliation audit (read-only)."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as valid JSON.",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to production SQLite DB (opened read-only).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Max number of production tables to list.",
    )
    args = parser.parse_args(argv)

    # Import here (after sys.path bootstrap) and keep it pure/read-only.
    from polycopy.engine.trade_copyability_v1_reconciliation_audit import (
        report_to_dict,
        report_to_human,
        run_reconciliation_audit,
    )

    db_path = args.db_path or _default_db_path()
    report = run_reconciliation_audit(db_path=db_path)

    if args.json:
        payload = report_to_dict(report)
        payload["db_path"] = db_path
        payload["limit"] = args.limit
        print(json.dumps(payload, indent=2, sort_keys=False))
    else:
        print(report_to_human(report, limit=args.limit))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
