#!/usr/bin/env python3
"""PR24Q — Trade Copyability review/report CLI (read-only).

Produces a Trade Copyability review report for the patched/defensive
Trade Copyability Score v1 (PR24P). This script is strictly READ-ONLY:

  * It imports ``polycopy.engine.trade_copyability_review_report``
    (pure report module), NOT ``polycopy.db.database`` (no write path).
  * It opens any production SQLite DB with ``mode=ro`` and never issues
    INSERT/UPDATE/DELETE/CREATE/DROP/ALTER statements.
  * It does no scoring writes, no candidate creation, no signal generation.

Usage:
  python3 scripts/report_trade_copyability_review.py
  python3 scripts/report_trade_copyability_review.py --json
  python3 scripts/report_trade_copyability_review.py --db-path /root/Polycopy/data/polycopy.db
  python3 scripts/report_trade_copyability_review.py --limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
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
        description="PR24Q Trade Copyability review report (read-only)."
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
        default=20,
        help="Max number of rows summarized per section.",
    )
    args = parser.parse_args(argv)

    # Import here (after sys.path bootstrap) and keep it pure/read-only.
    from polycopy.engine.trade_copyability_review_report import (
        build_trade_copyability_review_report,
        report_to_human,
    )

    db_path = args.db_path or _default_db_path()

    # Read-only open. Never mode=rw, never connect() via the ORM.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        report = build_trade_copyability_review_report(conn, limit=args.limit)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=False))
    else:
        print(report_to_human(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
