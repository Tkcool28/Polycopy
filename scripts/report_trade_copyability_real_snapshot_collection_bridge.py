#!/usr/bin/env python3
"""PR24U — Trade Copyability REAL snapshot/depth/current-price collection bridge CLI.

Report-only / dry-run bridge that PROVES whether real snapshot/depth/current-price
evidence can be collected for eligible ``source_trades`` rows and shaped into the
PR24S evidence structures.

This script is strictly READ-ONLY and NON-PERSISTING:

  * It imports ``polycopy.engine.trade_copyability_real_snapshot_collection_bridge``
    (pure read-only module), NOT ``polycopy.db.database`` (no write path).
  * It opens any SQLite DB with ``mode=ro`` and never issues
    INSERT/UPDATE/DELETE/CREATE/DROP/ALTER statements.
  * It does no snapshot persistence, no candidate creation, no signal
    generation, no order placement.
  * By DEFAULT (``--offline-only`` / dry-run) it performs NO live network fetch.
  * With ``--allow-live-preview`` it performs a REAL read-only ``CLOB /book``
    fetch per eligible token via the EXISTING ``PolymarketClobClient`` adapter
    (reused, not duplicated). STILL non-persisting — the run is a live evidence
    *preview* only. Network/auth/parse failures are captured per-row and never
    crash the batch.

Usage:
  PYTHONPATH=src python3 scripts/report_trade_copyability_real_snapshot_collection_bridge.py \\
    --db-path /root/Polycopy/data/polycopy.db --limit 20
  PYTHONPATH=src python3 scripts/report_trade_copyability_real_snapshot_collection_bridge.py \\
    --db-path /root/Polycopy/data/polycopy.db --limit 20 --json
  PYTHONPATH=src python3 scripts/report_trade_copyability_real_snapshot_collection_bridge.py \\
    --db-path /root/Polycopy/data/polycopy.db --allow-live-preview   # real /book, still no write
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

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


def _build_live_collector() -> Any:
    """Build a live collector reusing the existing PolymarketClobClient.

    The client is a read-only async HTTP adapter; we hand it an
    ``httpx.AsyncClient`` (no auth, no signing). The base URL defaults to the
    public CLOB endpoint. ``PolymarketClobClient.fetch_book`` is a coroutine, so
    the collector's ``fetch_book`` is awaited by the bridge (offline path uses
    ``asyncio.run``; live path uses the same await pattern).
    """
    import httpx

    from polycopy.adapters.polymarket_clob import PolymarketClobClient
    from polycopy.engine.trade_copyability_real_snapshot_collection_bridge import (
        LiveClobBookCollector,
    )

    http_client = httpx.AsyncClient(timeout=10.0)
    client = PolymarketClobClient(http_client=http_client, requests_per_minute=30)
    return LiveClobBookCollector(client=client)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PR24U Trade Copyability real snapshot/depth/price collection bridge "
                    "(read-only / dry-run / report-only)."
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as valid JSON.")
    parser.add_argument("--db-path", default=None, help="Path to production SQLite DB (opened read-only).")
    parser.add_argument("--limit", type=int, default=20, help="Max number of rows summarized in the human report.")
    parser.add_argument(
        "--offline-only",
        dest="offline_only",
        action="store_true",
        default=True,
        help="Do not fetch live market data (default: True).",
    )
    parser.add_argument(
        "--allow-live-preview",
        dest="allow_live_preview",
        action="store_true",
        default=False,
        help="Perform a REAL read-only CLOB /book fetch per eligible token (reuses "
             "PolymarketClobClient). Still non-persisting. Off by default.",
    )
    args = parser.parse_args(argv)

    # Import here (after sys.path bootstrap) and keep it pure/read-only.
    from polycopy.engine.trade_copyability_real_snapshot_collection_bridge import (
        RealSnapshotEvidenceCollector,
        build_trade_copyability_real_snapshot_collection_bridge,
        report_to_human,
    )

    db_path = args.db_path or _default_db_path()

    # Read-only open. Never mode=rw, never connect() via the ORM.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    collector = RealSnapshotEvidenceCollector()  # offline default
    live_preview = False
    try:
        if args.allow_live_preview:
            collector = _build_live_collector()
            live_preview = True
        report = build_trade_copyability_real_snapshot_collection_bridge(
            conn, limit=args.limit, collector=collector, live_preview=live_preview
        )
    finally:
        conn.close()
        # Best-effort close of any underlying httpx client used by the collector.
        client = getattr(getattr(collector, "_client", None), "_http", None)
        try:
            if client is not None:
                client.close()
        except Exception:
            pass
        # Drain any pending asyncio loop warnings from the live client.
        try:
            asyncio.get_event_loop().close()
        except Exception:
            pass

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=False))
    else:
        print(report_to_human(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
