#!/usr/bin/env python3
"""PR24R — Trade Copyability bridge audit CLI (read-only / dry-run only).

Proves how source_trades will be transformed into a TradeCopyabilityInputV1-
compatible audit input before any real wiring. This script is strictly
READ-ONLY:

  * It imports ``polycopy.engine.trade_copyability_bridge_audit`` (pure
    read-only bridge module), NOT ``polycopy.db.database`` (no write path).
  * It opens any production SQLite DB with ``mode=ro`` and never issues
    INSERT/UPDATE/DELETE/CREATE/DROP/ALTER statements.
  * It does no scoring writes, no candidate creation, no signal generation,
    no order placement.

Usage:
  PYTHONPATH=src python3 scripts/report_trade_copyability_bridge_audit.py
  PYTHONPATH=src python3 scripts/report_trade_copyability_bridge_audit.py --json
  PYTHONPATH=src python3 scripts/report_trade_copyability_bridge_audit.py --db-path /root/Polycopy/data/polycopy.db --limit 20
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
        description="PR24R Trade Copyability bridge audit (read-only / dry-run)."
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
        help="Max number of rows summarized in the human report.",
    )
    parser.add_argument(
        "--include-rows",
        action="store_true",
        help="Include full per-row audit output (human report shows rows by default; "
             "this is a no-op placeholder retained for CLI parity).",
    )
    args = parser.parse_args(argv)

    # Import here (after sys.path bootstrap) and keep it pure/read-only.
    from polycopy.engine.trade_copyability_bridge_audit import (
        build_trade_copyability_bridge_audit,
        report_to_human,
    )

    db_path = args.db_path or _default_db_path()

    # Read-only open. Never mode=rw, never connect() via the ORM.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        report = build_trade_copyability_bridge_audit(conn, limit=args.limit)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=False))
    else:
        print(report_to_human(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
