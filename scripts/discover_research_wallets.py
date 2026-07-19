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
import asyncio
import json
import os
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
for _cand in (_REPOSITORY_ROOT / "src", _REPOSITORY_ROOT / "scripts", _REPOSITORY_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from polycopy.ingestion.bounded_research_wallet_discovery import (  # noqa: E402
    _default_bounds,
    discover_candidates,
    persist_candidates,
)
from polycopy.runtime.locks import LockError, operational_job_lock  # noqa: E402
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

    # Mutually exclusive group for write mode
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="(Default) no network, no DB write. Structured JSON only.")
    mode.add_argument("--write", action="store_true",
                      help="Perform DB write (requires --allow-live --confirm-production-db)")

    p.add_argument("--allow-live", action="store_true",
                   help="Authorize bounded public network reads.")
    p.add_argument("--confirm-production-db", action="store_true",
                   help="Confirm the target is the production DB.")
    p.add_argument("--add-to-watchlist", action="store_true",
                   help="Also add an active research watch per discovered wallet.")
    p.add_argument("--output-json", type=str, default=None,
                   help="Output structured JSON to filesystem path (atomic write).")
    p.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT,
                   help="Global operational lock timeout in seconds.")
    return p


def _validate_bounds(args: argparse.Namespace) -> int:
    """Validate bounds; returns error code (0 = OK, 1 = invalid)."""
    errors = []

    for name, value, min_val, max_val in [
        ("market-limit", args.market_limit, 1, 10),
        ("trade-limit-per-market", args.trade_limit_per_market, 1, 100),
        ("max-wallets", args.max_wallets, 1, 5),
    ]:
        if value is not None:
            if value < min_val:
                errors.append(f"error: {name} must be >= {min_val}, got {value}")
            if value > max_val:
                errors.append(f"error: {name} must be <= {max_val}, got {value}")

    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    return 0


def _apply_bounds(bounds: dict, args: argparse.Namespace) -> dict:
    """Apply user-provided bounds, already validated to be in-range."""
    if args.market_limit is not None:
        bounds["market_limit"] = args.market_limit
    if args.trade_limit_per_market is not None:
        bounds["trade_limit_per_market"] = args.trade_limit_per_market
    if args.max_wallets is not None:
        bounds["max_wallets"] = args.max_wallets
    return bounds


def _make_adapter() -> Any:  # noqa: F821
    """Construct and return a PolymarketPublicAdapter. Overrideable for testing."""
    from polycopy.adapters.polymarket import PolymarketPublicAdapter
    return PolymarketPublicAdapter(
        gamma_base_url="https://gamma-api.polymarket.com",
        clob_base_url="https://clob.polymarket.com",
    )


def _write_json_atomic(output_path: Path, data: dict) -> None:
    """Write JSON atomically using temp file + os.replace()."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(".tmp")
    try:
        temp_path.write_text(json.dumps(data, indent=2))
        os.replace(temp_path, output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Determine mode: no --write and no --dry-run -> dry-run; --dry-run -> dry-run; --write -> write
    # Mutual exclusion means: if --write is True, --dry-run is False and vice versa
    want_write = args.write  # True if --write passed, False if --dry-run or neither

    # Validate bounds early (before any gates/ops)
    if _validate_bounds(args) != 0:
        return 1

    # Build bounds (validated to be in-range)
    bounds = _apply_bounds(_default_bounds(), args)

    # Validate production write gates BEFORE any DB open or adapter construction
    if want_write and not ed.require_write_gates(args, db_path=args.db_path):
        print("error: production write requires --write --allow-live "
              "--confirm-production-db", file=sys.stderr)
        return 2

    # Initialize variables for exception/finally handling
    adapter = None
    db = None
    result = None

    # Acquire lock BEFORE adapter construction or network discovery
    try:
        with operational_job_lock("discovery", timeout=args.lock_timeout):
            # Construct adapter INSIDE lock
            if args.allow_live:
                adapter = _make_adapter()

            # Perform async discovery (inside lock) BEFORE writable DB open
            if adapter is not None:
                discovery_result = asyncio.run(discover_candidates(adapter, bounds))
            else:
                discovery_result = {
                    "markets_requested": 0,
                    "markets_completed": 0,
                    "markets_partial": 0,
                    "markets_failed": 0,
                    "trades_examined": 0,
                    "candidates": [],
                }

            if want_write:
                # Open writable DB only AFTER discovery (inside lock)
                db = ed.open_writable(args.db_path, args)
            else:
                # Dry-run: open read-only DB for state lookup (inside lock)
                db = ed.open_readonly(args.db_path)

            # Persist candidates (inside lock)
            result = persist_candidates(
                db,
                discovery_result,
                add_to_watchlist=args.add_to_watchlist,
                bounds=bounds,
                perform_writes=want_write,
            )

            if want_write:
                db.commit()

    except LockError:
        # Lock contention - return nonzero, no provider/DB constructed
        print(json.dumps({"error": "lock_unavailable", "run_id": "", "status": "failed"}))
        return 4

    except Exception as e:  # noqa: BLE001
        # Preserve original exception, attempt rollback, report error
        run_id = result.run_id if result is not None else ""
        try:
            if db is not None:
                db.rollback()
        except Exception:
            pass
        print(json.dumps({"error": str(e), "run_id": run_id, "status": "failed"}))
        return 1

    finally:
        # Close DB
        try:
            if db is not None:
                db.close()
        except Exception:
            pass
        # Close adapter in all paths
        if adapter is not None:
            try:
                asyncio.run(adapter.aclose())
            except Exception:
                pass

    # Handle output
    out = result.as_dict()
    if args.output_json:
        _write_json_atomic(Path(args.output_json), out)
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