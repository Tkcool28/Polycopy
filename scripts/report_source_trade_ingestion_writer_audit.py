#!/usr/bin/env python3
"""PR24X — Source-Trade INGESTION WRITER AUDIT CLI.

Audit-only / design-only / report-only that inspects existing source_trade
ingestion/write paths and produces a WAL-safe single-writer ingestion plan.
Step 1 / Step 2 of the master chain (find/ingest, normalize/validate), BEFORE
any real persistence / scoring / candidate / signal / order / timer work.

This script is strictly READ-ONLY and NON-PERSISTING:

  * It imports
    ``polycopy.engine.source_trade_ingestion_writer_audit``
    (pure read-only module), NOT ``polycopy.db.database`` (no write path).
  * It opens any SQLite DB with ``mode=ro`` and never issues
    INSERT/UPDATE/DELETE/CREATE/DROP/ALTER statements.
  * It does no decisions, no candidate creation, no signal generation, no
    order placement, no backfill, no live fetch, no real ingestion.
  * By DEFAULT it performs NO network call. Live probing is intentionally out
    of scope for PR24X (SAFE/PARKED/PAPER-ONLY).

Usage:
  PYTHONPATH=src python3 scripts/report_source_trade_ingestion_writer_audit.py \
    --db-path /root/Polycopy/data/polycopy.db
  PYTHONPATH=src python3 scripts/report_source_trade_ingestion_writer_audit.py \
    --db-path /root/Polycopy/data/polycopy.db --json
  PYTHONPATH=src python3 scripts/report_source_trade_ingestion_writer_audit.py \
    --json --out reports/pr24x_source_trade_ingestion_writer_audit.json
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
    # Fall back to the first candidate (may not exist -> inventory omitted).
    return candidates[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PR24X source-trade ingestion writer audit "
                    "(read-only / report-only)."
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the report as valid JSON (default: Markdown to stdout).",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Path to production SQLite DB (opened read-only).",
    )
    parser.add_argument(
        "--out", default=None,
        help="Optional file to write the report to (does NOT write the DB).",
    )
    parser.add_argument(
        "--repo-root", default=str(_REPO_ROOT),
        help="Repo root used for static write-path inspection.",
    )
    args = parser.parse_args(argv)

    # Import here (after sys.path bootstrap) and keep it pure/read-only.
    from polycopy.engine.source_trade_ingestion_writer_audit import (
        build_source_trade_ingestion_writer_audit,
        report_to_markdown,
        report_to_json,
    )

    db_path = args.db_path or _default_db_path()
    conn: sqlite3.Connection | None = None
    if db_path and os.path.exists(db_path):
        # mode=ro + immutable-friendly read-only open. No writes possible.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    try:
        audit = build_source_trade_ingestion_writer_audit(
            conn, repo_root=args.repo_root
        )
    finally:
        if conn is not None:
            conn.close()

    if args.json:
        text = report_to_json(audit)
    else:
        text = report_to_markdown(audit)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote report to {args.out} (read-only; DB untouched).")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
