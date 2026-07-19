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
    persist_candidates,
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


async def _async_discover_and_persist(
    adapter,
    bounds: dict,
) -> dict:
    """Async discovery returning in-memory candidates with provenance."""
    markets = await adapter.list_active_markets(limit=bounds["market_limit"])

    discovery_result = {
        "markets_requested": 0,
        "markets_completed": 0,
        "markets_partial": 0,
        "markets_failed": 0,
        "trades_examined": 0,
        "candidates": [],  # Each: {canonical_address, source_market_id, source_trade_id_or_hash}
    }

    market_limit = bounds["market_limit"]
    trade_limit = bounds["trade_limit_per_market"]
    markets_fetched = 0

    for market in markets:
        market_id = getattr(market, "source_id", None) or getattr(market, "condition_id", None)
        if market_id is None:
            continue

        discovery_result["markets_requested"] += 1

        fetch_result = await adapter.fetch_trades_for_market(
            market_source_id=str(market_id),
            limit=trade_limit,
            max_pages=1,
            max_rows=trade_limit,
        )

        if fetch_result.status == "complete":
            discovery_result["markets_completed"] += 1
            trades = list(fetch_result)
            discovery_result["trades_examined"] += len(trades)
            for trade in trades:
                addr = getattr(trade, "trader_address", None)
                if addr is not None and str(addr).strip():
                    trade_id = getattr(trade, "source_trade_id", None)
                    if not trade_id:
                        trade_id = getattr(trade, "transaction_hash", None)
                    discovery_result["candidates"].append({
                        "canonical_address": str(addr).lower().strip(),
                        "source_market_id": str(market_id),
                        "source_trade_id_or_hash": trade_id,
                    })
        elif fetch_result.status == "partial":
            discovery_result["markets_partial"] += 1
        else:
            discovery_result["markets_failed"] += 1

        markets_fetched += 1
        if markets_fetched >= market_limit:
            break

    return discovery_result


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

    # Acquire lock BEFORE adapter construction or network discovery
    lock = operational_job_lock("discovery", timeout=args.lock_timeout)
    locked = lock.__enter__()
    if not locked:
        print("error: could not acquire operational lock", file=sys.stderr)
        return 3

    adapter = None
    db = None

    try:
        # Construct adapter INSIDE lock
        if args.allow_live:
            from polycopy.adapters.polymarket import PolymarketPublicAdapter
            adapter = PolymarketPublicAdapter(
                gamma_base_url="https://gamma-api.polymarket.com",
                clob_base_url="https://clob.polymarket.com",
            )

        # Perform async discovery (inside lock) BEFORE writable DB open
        if adapter is not None:
            discovery_result = asyncio.run(_async_discover_and_persist(adapter, bounds))
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

            # Persist candidates (inside lock, in transaction)
            result = persist_candidates(
                db,
                discovery_result,
                add_to_watchlist=args.add_to_watchlist,
                bounds=bounds,
                perform_writes=True,
            )
            db.commit()
        else:
            # Dry-run: open read-only DB for state lookup (inside lock)
            db = ed.open_readonly(args.db_path)
            # Persist with perform_writes=False for read-only state check
            result = persist_candidates(
                db,
                discovery_result,
                add_to_watchlist=args.add_to_watchlist,
                bounds=bounds,
                perform_writes=False,
            )
    except Exception as e:  # noqa: BLE001
        try:
            if db is not None:
                db.rollback()
        except Exception:
            pass
        if result is None:
            print(json.dumps({"error": str(e), "run_id": ""}, indent=2))
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
        lock.__exit__(None, None, None)

    # Handle output
    out = result.as_dict()
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_suffix(".tmp")
        try:
            temp_path.write_text(json.dumps(out, indent=2))
            os.replace(temp_path, output_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
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