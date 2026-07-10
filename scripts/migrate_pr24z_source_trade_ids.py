#!/usr/bin/env python3
"""CLI for the isolated PR24Z canonical source_trade_id migration.

Default is dry-run.  Use --apply only against an explicitly supplied copied/temp
DB unless production execution has been separately authorized.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from polycopy.migrations.pr24z_canonical_identity import (
    DEFAULT_REFERENCE_PATH,
    migrate,
    write_reports,
)


def main() -> int:
    p = argparse.ArgumentParser(description="PR24Z one-time canonical source_trade_id migration")
    p.add_argument("--db-path", required=True, help="SQLite DB path. Tests/review should pass a temp copy.")
    p.add_argument("--reference-path", default=str(DEFAULT_REFERENCE_PATH))
    p.add_argument("--reports-dir", default="reports")
    p.add_argument("--marker-path", default=None)
    p.add_argument("--apply", action="store_true", help="Apply migration to the supplied DB. Default dry-run only.")
    p.add_argument("--json", action="store_true", help="Print JSON result")
    args = p.parse_args()

    res = migrate(
        Path(args.db_path),
        reference_path=Path(args.reference_path),
        marker_path=Path(args.marker_path) if args.marker_path else None,
        apply=args.apply,
        reports_dir=Path(args.reports_dir),
    )
    write_reports(res, Path(args.reports_dir), allow_write=args.apply)
    if args.json:
        print(json.dumps(res.as_dict(), indent=2, sort_keys=True))
    else:
        print(f"ok={res.ok} state={res.state} rows_updated={res.rows_updated} already_migrated={res.already_migrated} error={res.error}")
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
