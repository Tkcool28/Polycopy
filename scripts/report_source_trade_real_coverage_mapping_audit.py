#!/usr/bin/env python3
"""PR24W — Source-Trade REAL COVERAGE + TOKEN→CONDITION MAPPING AUDIT CLI.

Report-only / read-only audit that inspects the current ``source_trades``
inventory, its real (production-like) coverage, its ingestion quality, and its
identifier-mapping readiness — Step 1/Step 2 of the master chain, BEFORE
any persistence / scoring / candidate / signal / order / timer work.

This script is strictly READ-ONLY and NON-PERSISTING:

  * It imports
    ``polycopy.engine.source_trade_real_coverage_mapping_audit``
    (pure read-only module), NOT ``polycopy.db.database`` (no write path).
  * It opens any SQLite DB with ``mode=ro`` and never issues
    INSERT/UPDATE/DELETE/CREATE/DROP/ALTER statements.
  * It does no decisions, no candidate creation, no signal generation, no
    order placement, no backfill.
  * By DEFAULT it performs NO live network call. A live identifier-lookup
    preview is OPT-IN only via ``--allow-live-preview`` (still non-persisting)
    and is intentionally OFF in this PR (SAFE/PARKED/PAPER-ONLY).

Usage:
  PYTHONPATH=src python3 scripts/report_source_trade_real_coverage_mapping_audit.py \
    --db-path /root/Polycopy/data/polycopy.db --limit 20
  PYTHONPATH=src python3 scripts/report_source_trade_real_coverage_mapping_audit.py \
    --db-path /root/Polycopy/data/polycopy.db --limit 20 --json
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
        description="PR24W source-trade real-coverage + token→condition mapping "
                    "audit (read-only / report-only)."
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the report as valid JSON.",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Path to production SQLite DB (opened read-only).",
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="Max number of rows summarized in the human report.",
    )
    parser.add_argument(
        "--allow-live-preview",
        dest="allow_live_preview",
        action="store_true",
        default=False,
        help="(OPT-IN, OFF BY DEFAULT) Perform a real read-only identifier "
             "lookup preview. NOT fired against production in this PR "
             "(SAFE/PARKED/PAPER-ONLY). Still non-persisting.",
    )
    args = parser.parse_args(argv)

    # Import here (after sys.path bootstrap) and keep it pure/read-only.
    from polycopy.engine.source_trade_real_coverage_mapping_audit import (
        build_source_trade_real_coverage_mapping_audit,
        report_to_human,
    )

    db_path = args.db_path or _default_db_path()

    # Read-only open. Never mode=rw, never connect() via the ORM.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # NOTE: PR24W's mapping audit is read-only/structural. The live preview
        # flag is accepted for CLI parity but intentionally NOT used to fire any
        # network call in this PR (honoring SAFE/PARKED/PAPER-ONLY). A future
        # PR may wire it to a read-only Gamma look-up.
        report = build_source_trade_real_coverage_mapping_audit(
            conn, limit=args.limit, db_path=db_path,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=False))
    else:
        print(report_to_human(report))

    # Honest note when live preview was requested but not exercised.
    if args.allow_live_preview:
        sys.stderr.write(
            "[pr24w] --allow-live-preview was set but PR24W does NOT fire any "
            "network call in this report-only PR. Identifier-mapping feasibility "
            "is assessed structurally against the existing read-only join.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
