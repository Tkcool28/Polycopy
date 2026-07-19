#!/usr/bin/env python3
"""Bounded research-wallet discovery CLI (PR #72).

Safe front door into the PR #71 evidence/scoring pipeline.

Defaults to DRY-RUN (no network, no DB write, structured JSON only).

Required before ANY writable DB open:
  --allow-live      authorize bounded public network reads
  --write           perform the DB write
  --confirm-production-db   confirm the target is the production DB
  (all three) PLUS the global operational lock and the complete bound set
  (market / trade-per-market / wallet-count).

Write scope is EXACTLY: wallets, specialist_evidence_watchlist.
This CLI never calls collect_smart_money_data.run_collection or evaluate_wallet.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
for _cand in (_REPOSITORY_ROOT / "src", _REPOSITORY_ROOT / "scripts", _REPOSITORY_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from polycopy.ingestion.bounded_research_wallet_discovery import (  # noqa: E402
    _default_bounds,
    discover,
)
from polycopy.runtime.locks import operational_job_lock  # noqa: E402
import evidence_db as ed  # noqa: E402

PRODUCTION_DB_PATH = (_REPOSITORY_ROOT / "data" / "polycopy.db").resolve()

DEFAULT_LOCK_TIMEOUT = 30.0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bounded research-wallet discovery bridge (PR72)")
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--market-limit", type=int, default=None,
                   help="Max active markets to fetch (safe default: 10, max: 10)")
    p.add_argument("--trade-limit-per-market", type=int, default=None,
                   help="Max trades per market (safe default: 100, max: 100)")
    p.add_argument("--max-wallets", type=int, default=None,
                   help="Stop after this many wallets (safe default: 5, max: 5)")
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="(Default) no network, no DB write. Structured JSON only.")
    p.add_argument("--write", action="store_true",
                   help="Perform DB write (requires --allow-live --confirm-production-db)")
    p.add_argument("--allow-live", action="store_true",
                   help="Authorize bounded public network reads.")
    p.add_argument("--confirm-production-db", action="store_true",
                   help="Confirm the target is the production DB.")
    p.add_argument("--add-to-watchlist", action="store_true",
                   help="Also add an active research watch per discovered wallet.")
    p.add_argument("--output-json", action="store_true",
                   help="Output structured JSON to stdout.")
    p.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT,
                   help="Global operational lock timeout in seconds.")
    return p


def _apply_safe_bounds(bounds: dict, args: argparse.Namespace) -> dict:
    """Apply safe defaults and cap bounds to safe limits."""
    # Market limit: max 10
    ml = args.market_limit
    if ml is None:
        ml = bounds["market_limit"]
    bounds["market_limit"] = min(ml, 10)

    # Trade limit per market: max 100
    tl = args.trade_limit_per_market
    if tl is None:
        tl = bounds["trade_limit_per_market"]
    bounds["trade_limit_per_market"] = min(tl, 100)

    # Max wallets: max 5
    mw = args.max_wallets
    if mw is None:
        mw = bounds["max_wallets"]
    bounds["max_wallets"] = min(mw, 5)

    return bounds


def _make_adapter():
    """Construct the bounded public adapter for live reads."""
    from polycopy.adapters.polymarket import PolymarketPublicAdapter
    return PolymarketPublicAdapter(
        gamma_base_url="https://gamma-api.polymarket.com",
        clob_base_url="https://clob.polymarket.com",
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Determine if we want writes (dry-run is default)
    want_write = args.write and not args.dry_run

    # Validate production write gates BEFORE any DB open
    if want_write and not ed.require_write_gates(args, db_path=args.db_path):
        print("error: production write requires --write --allow-live "
              "--confirm-production-db", file=sys.stderr)
        return 2

    # Build bounds with safe limits
    bounds = _apply_safe_bounds(_default_bounds(), args)

    if want_write:
        # Validate gates again (belt-and-suspenders) before opening writable DB
        if not ed.require_write_gates(args, db_path=args.db_path):
            print("error: write gates not satisfied", file=sys.stderr)
            return 2

        # Construct adapter OUTSIDE the lock (network I/O happens later)
        adapter = _make_adapter() if args.allow_live else None

        # Acquire lock and open DB only after gates pass
        db = ed.open_writable(args.db_path, args)

        try:
            with operational_job_lock("discovery", timeout=args.lock_timeout):
                # Now perform the discovery inside the lock
                result = discover(
                    db,
                    adapter=adapter,
                    add_watches=args.add_to_watchlist,
                    bounds=bounds,
                    live=args.allow_live,
                    perform_writes=True,
                )
                db.commit()
        except Exception as e:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:
                pass
            print(json.dumps({"error": str(e), "run_id": result.run_id if 'result' in dir() else None}, indent=2))
            return 1
        finally:
            try:
                db.close()
            except Exception:
                pass
            # Close adapter if it was opened
            if adapter is not None:
                try:
                    import asyncio
                    asyncio.run(adapter.aclose())
                except Exception:
                    pass
    else:
        # Dry-run: open DB read-only, no writes
        # Perform bounded public reads only when --allow-live is supplied
        db = ed.open_readonly(args.db_path)

        try:
            result = discover(
                db,
                adapter=_make_adapter() if args.allow_live else None,
                add_watches=args.add_to_watchlist,
                bounds=bounds,
                live=args.allow_live,
                perform_writes=False,
            )
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"error": str(e), "run_id": ""}, indent=2))
            return 1
        finally:
            try:
                db.close()
            except Exception:
                pass

    out = result.as_dict()
    if args.output_json:
        print(json.dumps(out, indent=2))
    else:
        print(f"dry_run={out['dry_run']} "
              f"markets_requested={out['markets_requested']} "
              f"markets_completed={out['markets_completed']} "
              f"markets_partial={out['markets_partial']} "
              f"markets_failed={out['markets_failed']} "
              f"trades_examined={out['trades_examined']} "
              f"would_create_wallets={out['would_create_wallets']} "
              f"existing_wallets={out['existing_wallets']} "
              f"new_wallets={out['new_wallets']} "
              f"would_create_w={out['would_create_watches']} "
              f"watches_created={out['watches_created']} "
              f"candidates={len(out['candidates'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())