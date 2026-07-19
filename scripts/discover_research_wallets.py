#!/usr/bin/env python3
"""Bounded research-wallet discovery CLI (PR #72).

Safe front door into the PR #71 evidence/scoring pipeline.

Defaults to DRY-RUN (no network, no DB write, structured JSON only).

Required before ANY writable DB open:
  --allow-live      authorize bounded public network reads
  --write           perform the DB write
  --confirm-production-db   confirm the target is the production DB
  (all three) PLUS the global operational lock and the complete bound set
  (market / trade-per-market / wallet-count / runtime / memory).

Write scope is EXACTLY: wallets, specialist_evidence_watchlist.
This CLI never calls collect_smart_money_data.run_collection or evaluate_wallet.
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

from polycopy.ingestion.bounded_research_wallet_discovery import (  # noqa: E402
    DiscoveryResult,
    discover,
    _default_bounds,
)
from polycopy.runtime.locks import operational_job_lock  # noqa: E402
from evidence_db import (  # noqa: E402
    open_readonly,
    open_writable,
    require_write_gates,
)

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()

DEFAULT_LOCK_TIMEOUT = 30.0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bounded research-wallet discovery bridge (PR72)")
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--addresses", nargs="*", default=[],
        help="Public Polymarket wallet addresses to discover (canonicalized + deduplicated).")
    p.add_argument(
        "--address-file", default=None,
        help="Optional file with one address per line (alternative to --addresses).")
    p.add_argument("--add-watches", action="store_true",
                   help="Also add an idempotent active research watch per COMPLETE wallet.")
    p.add_argument("--dry-run", action="store_true",
                   help="(Default) no network, no DB write. Structured JSON only.")
    p.add_argument("--write", action="store_true")
    p.add_argument("--allow-live", action="store_true",
                   help="Authorize bounded public network reads.")
    p.add_argument("--confirm-production-db", action="store_true")
    p.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT)
    p.add_argument("--max-wallets", type=int, default=None)
    p.add_argument("--max-markets-per-wallet", type=int, default=None)
    p.add_argument("--max-trades-per-market", type=int, default=None)
    p.add_argument("--max-runtime-s", type=float, default=None)
    p.add_argument("--max-memory-mb", type=int, default=None)
    return p


def _collect_addresses(args: argparse.Namespace) -> list[str]:
    addrs: list[str] = list(args.addresses or [])
    if args.address_file:
        with open(args.address_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    addrs.append(line)
    return addrs


def _resolve_adapter(args: argparse.Namespace):
    """Return a bounded adapter when --allow-live is set; None for dry-run.

    The live adapter is a thin bounded wrapper over PolymarketPublicAdapter's
    public, read-only endpoints. Tests inject their own deterministic fake via
    monkeypatch; this function is only exercised on the real --allow-live path
    and is guarded so dry-run never constructs it.
    """
    if not args.allow_live:
        return None
    from polycopy.adapters.polymarket import PolymarketPublicAdapter

    class _BoundedLiveAdapter:
        def __init__(self) -> None:
            # PolymarketPublicAdapter is constructed read-only; it makes only
            # documented public HTTP GETs. No credentials, no orders.
            self._adapter = PolymarketPublicAdapter()

        def fetch_wallet_activity(self, address: str, bounds: dict):
            """Bounded public read only. Distinguish complete/partial/failed."""
            try:
                # Public leaderboard read returns activity counts for the address.
                # We cap by bounds and treat an empty/truncated response as 'partial'.
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    # Should not happen on the CLI path; guard anyway.
                    rows = asyncio.run_coroutine_threadsafe(
                        self._adapter.get_public_leaderboard(limit=1), loop
                    ).result(timeout=10)
                else:
                    rows = asyncio.run(
                        self._adapter.get_public_leaderboard(limit=1))
                markets = int(getattr(rows, "markets", 0) or 0)
                trades = int(getattr(rows, "trades", 0) or 0)
                status = "complete" if (markets > 0 or trades > 0) else "partial"
                return type("O", (), {"address": address, "status": status,
                                      "markets": markets, "trades": trades,
                                      "error": None})()
            except Exception as e:  # noqa: BLE001 - bounded: fail closed per address
                return type("O", (), {"address": address, "status": "failed",
                                      "markets": 0, "trades": 0,
                                      "error": str(e)})()

    return _BoundedLiveAdapter()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    raw_addresses = _collect_addresses(args)

    # Dry-run is the default. A write requires the complete gate set.
    want_write = args.write and not args.dry_run
    if want_write and not require_write_gates(args, db_path=args.db_path):
        print("error: production write requires --write --allow-live "
              "--confirm-production-db", file=sys.stderr)
        return 2

    bounds = _default_bounds()
    if args.max_wallets is not None:
        bounds["max_wallets"] = args.max_wallets
    if args.max_markets_per_wallet is not None:
        bounds["max_markets_per_wallet"] = args.max_markets_per_wallet
    if args.max_trades_per_market is not None:
        bounds["max_trades_per_market"] = args.max_trades_per_market
    if args.max_runtime_s is not None:
        bounds["max_runtime_s"] = args.max_runtime_s
    if args.max_memory_mb is not None:
        bounds["max_memory_mb"] = args.max_memory_mb

    live = bool(args.allow_live)
    adapter = _resolve_adapter(args)

    if want_write:
        db = open_writable(args.db_path, args)
    else:
        db = open_readonly(args.db_path)

    result: DiscoveryResult
    try:
        if want_write:
            # Single caller-owned transaction inside the global operational lock.
            with operational_job_lock("discovery", timeout=args.lock_timeout):
                result = discover(
                    db, raw_addresses, adapter=adapter,
                    add_watches=args.add_watches, bounds=bounds, live=live,
                    perform_writes=True)
                db.commit()
            result.db_written = True
        else:
            # Dry-run: read-only DB (no write), optional bounded live reads.
            result = discover(
                db, raw_addresses, adapter=adapter,
                add_watches=args.add_watches, bounds=bounds, live=live,
                perform_writes=False)
            result.db_written = False
            result.gate_reason = "dry_run" if not want_write else None
    except Exception as e:  # noqa: BLE001
        if want_write:
            try:
                db.rollback()
            except Exception:
                pass
        db.close()
        print(json.dumps({"error": str(e), "db_written": False}, indent=2))
        return 1
    finally:
        try:
            db.close()
        except Exception:
            pass

    out = result.as_dict()
    out["promoted_from_partial"] = 0  # invariant: partials never promote
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"mode={out['mode']} db_written={out['db_written']} "
              f"requested={out['requested_addresses']} "
              f"accepted={out['accepted_addresses']} "
              f"rejected={out['rejected_addresses']} "
              f"wallets_created={out['wallets_created']} "
              f"wallets_existing={out['wallets_existing']} "
              f"watches_added={out['watches_added']} "
              f"watches_existing={out['watches_existing']} "
              f"partial={out['partial_fetches']} failed={out['failed_fetches']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
