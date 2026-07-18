#!/usr/bin/env python3
"""Research-only evidence collection CLI (watchlist-driven, BUY-only).

Collects bounded recent BUY trades for one ACTIVE watchlist entry, persists
them idempotently with canonical nested taxonomy, and writes enrichment
provenance. It does NOT use specialist approval as a selector, and it creates
NO approval, dispatch, candidate, paper-signal, or execution artifact.

Production safeguards (PR68 pattern):
  * Writes to the production DB require --write AND --confirm-production-db.
  * --dry-run (default) performs no write.
  * Bounds are fail-closed (lower of run config and the watch entry's own cap).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _cand in (_REPO_ROOT / "src", _REPO_ROOT / "scripts", _REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from polycopy.ingestion.specialist_evidence_collector import (  # noqa: E402
    EvidenceCollectorConfig,
    collect_evidence,
)
from evidence_db import (  # noqa: E402
    open_readonly,
    open_writable,
    require_write_gates,
)

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()


async def _run(db, args):
    # Build a real provider via the public adapter (read-only /trades).
    from datetime import datetime
    from polycopy.adapters.polymarket import PolymarketPublicAdapter

    adapter = PolymarketPublicAdapter(
        gamma_base_url="https://gamma-api.polymarket.com",
        clob_base_url="https://clob.polymarket.com",
        data_api_base_url="https://data-api.polymarket.com",
        timeout=min(10.0, args.timeout_seconds),
    )

    class _Provider:
        made_network_call = True

        async def fetch_trades(self, wallet, *, limit, page):
            # The public data-api ignores the conditionId filter; offset-based
            # pagination returns raw dicts the pipeline normalizes.
            return await adapter.get_trades_by_address(
                wallet, since=datetime.min, limit=limit, offset=page * limit,
                return_raw=True,
            )

    provider = _Provider()

    gamma_resolver = None
    if args.resolve_gamma:
        async def gamma_resolver(condition_id):
            return await adapter.get_market_raw(condition_id)

    cfg = EvidenceCollectorConfig(
        max_wallets_per_run=1,
        max_new_trades_per_wallet=args.max_new_trades_per_wallet,
        max_total_new_trades=args.max_total_new_trades,
        max_gamma_requests=args.max_gamma_requests,
        timeout_seconds=args.timeout_seconds,
        rss_mb_limit=args.rss_mb_limit,
    )
    result = await collect_evidence(
        db, watch_id=args.watch_id, provider=provider,
        gamma_resolver=gamma_resolver, config=cfg, dry_run=args.dry_run,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Collect research evidence for a watch")
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--watch-id", required=True)
    p.add_argument("--max-new-trades-per-wallet", type=int, default=25)
    p.add_argument("--max-total-new-trades", type=int, default=25)
    p.add_argument("--max-gamma-requests", type=int, default=100)
    p.add_argument("--timeout-seconds", type=float, default=30.0)
    p.add_argument("--rss-mb-limit", type=float, default=512.0)
    p.add_argument("--resolve-gamma", action="store_true",
                   help="Resolve Gamma taxonomy during collection (network)")
    p.add_argument("--dry-run", action="store_true", help="No write (default)")
    p.add_argument("--write", action="store_true", help="Persist mutation")
    p.add_argument("--confirm-production-db", action="store_true",
                   help="Confirm target is the production DB")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    # Collection persists source trades; require the full three-gate set on
    # production paths. Dry-run (default) opens read-only.
    if not require_write_gates(args, db_path=args.db_path):
        print(
            "error: production write requires --write --allow-live "
            "--confirm-production-db",
            file=sys.stderr,
        )
        return 2
    db = open_writable(args.db_path, args) if args.write else open_readonly(args.db_path)
    try:
        result = asyncio.run(_run(db, args))
    finally:
        db.close()

    out = result.as_dict()
    if args.json:
        print(json.dumps(out, indent=1))
    else:
        for k, v in out.items():
            print(f"{k}={v}")
    # Non-zero if the run errored.
    return 1 if result.error and result.inserted_rows == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
