#!/usr/bin/env python3
"""Enrich exactly one approved source trade (authoritative evidence resolution).

This is the bounded enrichment entry point. It operates on ONE exact
``source_trades.id`` (the canonical internal UUID), never an arbitrary wallet
history. It resolves + persists the durable ``source_trade_enrichments`` record.
It does NOT ingest trades, call the bridge, or execute anything.

Safety envelope:
  * Dry-run is the DEFAULT. No --allow-live => no Gamma network resolution.
  * No --write => no writes (even with --allow-live).
  * A production DB write requires --write --confirm-production-db (--allow-live
    optional, only needed for live Gamma enrichment).
  * Bounded: at most one source trade, at most one Gamma resolve.
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
from polycopy.ingestion.source_trade_enrichment import enrich_source_trade  # noqa: E402
from polycopy.ingestion.approved_wallet_collector import _raw_gamma_resolver_adapter  # noqa: E402
from polycopy.config.settings import Settings  # noqa: E402
from polycopy.adapters.polymarket import PolymarketPublicAdapter  # noqa: E402

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()


def _is_production_db(db_path: str) -> bool:
    try:
        return Path(db_path).resolve() == PRODUCTION_DB_PATH
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Enrich exactly one approved source trade (authoritative evidence)"
    )
    p.add_argument("--source-trade-id", required=True,
                   help="Exact internal source_trades.id UUID")
    p.add_argument("--write", action="store_true", help="Persist enrichment record")
    p.add_argument("--allow-live", action="store_true",
                   help="Authorize bounded Gamma network resolution")
    p.add_argument("--confirm-production-db", action="store_true",
                   help="Confirm target is the production DB")
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if not args.source_trade_id.strip():
        print("error: --source-trade-id must be non-empty", file=sys.stderr)
        return 2

    is_prod = _is_production_db(args.db_path)
    if args.write and is_prod:
        missing = []
        if not args.confirm_production_db:
            missing.append("--confirm-production-db")
        if missing:
            print("error: production enrichment write requires: " + ", ".join(missing),
                  file=sys.stderr)
            return 2

    db = Database(Path(args.db_path)).connect()
    try:
        gamma_resolver = None
        if args.allow_live:
            settings = Settings()
            adapter = PolymarketPublicAdapter(
                gamma_base_url=settings.gamma_base_url,
                clob_base_url=settings.clob_base_url,
                data_api_base_url=settings.data_api_base_url,
                timeout=10.0,
            )
            gamma_resolver = _raw_gamma_resolver_adapter(adapter)

        result = enrich_source_trade(
            db, args.source_trade_id,
            gamma_resolver=gamma_resolver,
            dry_run=not args.write,
        )
    finally:
        db.close()

    out = result.as_dict()
    out["mode"] = "write" if args.write else "dry-run"
    out["production_db"] = str(PRODUCTION_DB_PATH) if is_prod else args.db_path
    if args.json:
        print(json.dumps(out, sort_keys=True))
    else:
        print(f"source_trade_internal_id={out['source_trade_internal_id']}")
        print(f"enrichment_id={out['enrichment_id']}")
        print(f"status={out['status']}")
        print(f"created={out['created']} updated={out['updated']}")
        print(f"reason_codes={out['reason_codes']}")
        if out["error_message"]:
            print(f"error={out['error_message']}")
    return 0 if out["status"] in ("complete", "incomplete", "unavailable", "conflict", "error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
