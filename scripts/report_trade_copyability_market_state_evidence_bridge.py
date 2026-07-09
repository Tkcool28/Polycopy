#!/usr/bin/env python3
"""PR24V — Trade Copyability MARKET-STATE / END-TIME EVIDENCE BRIDGE CLI.

Report-only / dry-run bridge that PROVES whether the missing market metadata
required before honest Trade Copyability decisions can be obtained for eligible
``source_trades`` rows:

  market_active / market_closed / market_resolved / end_date /
  seconds_to_market_end / identifier mapping status / fetched_at.

This script is strictly READ-ONLY and NON-PERSISTING:

  * It imports ``polycopy.engine.trade_copyability_market_state_evidence_bridge``
    (pure read-only module), NOT ``polycopy.db.database`` (no write path).
  * It opens any SQLite DB with ``mode=ro`` and never issues
    INSERT/UPDATE/DELETE/CREATE/DROP/ALTER statements.
  * It does no persistence, no candidate creation, no signal generation, no
    order placement.
  * By DEFAULT (``--offline-only`` / dry-run) it performs NO live network fetch.
  * With ``--allow-live-preview`` it performs a REAL read-only Gamma
    ``get_market`` fetch per resolvable condition_id via the EXISTING
    ``PolymarketPublicAdapter`` (reused, not duplicated). STILL non-persisting
    — the run is a live metadata *preview* only. Network/auth/parse failures are
    captured per-row and never crash the batch.

Usage:
  PYTHONPATH=src python3 scripts/report_trade_copyability_market_state_evidence_bridge.py \
    --db-path /root/Polycopy/data/polycopy.db --limit 20
  PYTHONPATH=src python3 scripts/report_trade_copyability_market_state_evidence_bridge.py \
    --db-path /root/Polycopy/data/polycopy.db --limit 20 --json
  PYTHONPATH=src python3 scripts/report_trade_copyability_market_state_evidence_bridge.py \
    --db-path /root/Polycopy/data/polycopy.db --allow-live-preview   # real Gamma, still no write
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


def _build_live_provider() -> Any:
    """Build a live metadata provider reusing the existing PolymarketPublicAdapter.

    The adapter is a read-only async HTTP client (Gamma /markets/{conditionId});
    we hand it its default settings (no auth, no signing). Its
    ``get_market(condition_id)`` is a coroutine returning a ``Market`` with
    active/closed/resolved/end_date/source_id/fetched_at. That is the reusable
    client — PR24V does NOT invent a duplicate.
    """
    from polycopy.config.settings import Settings
    from polycopy.adapters.polymarket import PolymarketPublicAdapter
    from polycopy.engine.trade_copyability_market_state_evidence_bridge import (
        LiveGammaMarketStateProvider,
    )

    settings = Settings()
    adapter = PolymarketPublicAdapter(
        gamma_base_url=settings.gamma_base_url,
        clob_base_url=settings.clob_base_url,
        data_api_base_url=settings.data_api_base_url,
    )
    return LiveGammaMarketStateProvider(adapter=adapter)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PR24V Trade Copyability market-state / end-time evidence bridge "
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
        help="Perform a REAL read-only Gamma get_market fetch per resolvable condition_id "
             "(reuses PolymarketPublicAdapter). Still non-persisting. Off by default.",
    )
    args = parser.parse_args(argv)

    # Import here (after sys.path bootstrap) and keep it pure/read-only.
    from polycopy.engine.trade_copyability_market_state_evidence_bridge import (
        OfflineMarketStateProvider,
        build_trade_copyability_market_state_evidence_bridge,
        report_to_human,
    )

    db_path = args.db_path or _default_db_path()

    # Read-only open. Never mode=rw, never connect() via the ORM.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    provider = OfflineMarketStateProvider()  # offline default
    live_preview = False
    adapter = None
    try:
        if args.allow_live_preview:
            provider = _build_live_provider()
            live_preview = True
            adapter = getattr(provider, "_adapter", None)
        report = build_trade_copyability_market_state_evidence_bridge(
            conn, limit=args.limit, provider=provider, live_preview=live_preview,
            db_path=db_path,
        )
    finally:
        conn.close()
        # Best-effort close of any underlying Gamma httpx client.
        if adapter is not None:
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(adapter.aclose())
                loop.close()
            except Exception:
                pass
        # Drain any pending asyncio loop warnings.
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
