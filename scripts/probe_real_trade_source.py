#!/usr/bin/env python3
"""PR24Y — Real Wallet Trade Source Probe CLI.

Read-only source probe (Step 1/Step 2 architecture, pre-ingestion). It answers:
which existing API/adapter can reliably return real wallet trade history with
enough fields for copyability-ready BUY source trades?

Safety model
------------
  * Default mode: NO network, NO database, NO writes. Uses a fixture/fake
    provider so the report can be generated offline.
  * Live mode requires ``--allow-live-preview`` AND an explicit wallet. It then
    uses the existing ``PolymarketPublicAdapter.get_trades_by_address`` (data-api
    GET /trades?user=<addr>, unauthenticated) — a read-only public endpoint.
  * The CLI never opens the production DB. No --db-path is accepted (the probe
    does not need the DB; PR24Y is a source probe, not an ingestion run).
  * Bounds: default 25 records, hard max 100; at most 2 pages per wallet in
    PR24Y; single wallet by default (explicit hard max 5); no concurrent
    workers; no timers; no background process.

Usage:
  PYTHONPATH=src python3 scripts/probe_real_trade_source.py \
      --wallet-address 0x... --allow-live-preview
  PYTHONPATH=src python3 scripts/probe_real_trade_source.py \
      --wallet-file wallets.txt --allow-live-preview --limit 25
  PYTHONPATH=src python3 scripts/probe_real_trade_source.py \
      --out reports/pr24y_real_trade_source_probe.json --json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Optional

# Ensure the repo ``src`` is importable when run as a script.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
for _cand in (_REPO_ROOT / "src", _REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from polycopy.engine.real_trade_source_probe import (  # noqa: E402
    run_real_trade_source_probe,
    report_to_markdown,
    report_to_json,
    validate_wallet_inputs,
    DEFAULT_RECORD_LIMIT,
    HARD_MAX_RECORD_LIMIT,
    PR24Y_MAX_PAGES,
)


# ── Real provider (only constructed in live mode) ────────────────────────────
class _RealDataApiProvider:
    """Wraps the existing PolymarketPublicAdapter wallet-trades method.

    Read-only; returns raw data-api-shaped dicts (not SourceTrade objects) so
    the probe core stays DB-free and provider-agnostic. No DB dependency is
    introduced by this wrapper.
    """

    def __init__(self, timeout: float = 10.0) -> None:
        from polycopy.adapters.polymarket import PolymarketPublicAdapter

        self._adapter = PolymarketPublicAdapter(
            gamma_base_url="https://gamma-api.polymarket.com",
            clob_base_url="https://clob.polymarket.com",
            data_api_base_url="https://data-api.polymarket.com",
            timeout=timeout,
        )

    async def fetch_trades(
        self, wallet: str, *, limit: int, page: int
    ) -> list[dict[str, Any]]:
        self.made_network_call = True
        offset = page * limit
        from datetime import datetime, timezone

        trades = await self._adapter.get_trades_by_address(
            wallet,
            since=datetime(2000, 1, 1, tzinfo=timezone.utc),
            limit=min(limit, HARD_MAX_RECORD_LIMIT),
        )
        # get_trades_by_address already applied `since`; slice the page here.
        return [self._to_raw(t) for t in trades[offset : offset + limit]]

    @staticmethod
    def _to_raw(t: Any) -> dict[str, Any]:
        # Convert a domain SourceTrade into a raw data-api-shaped dict.
        side = getattr(t, "side", None)
        ts = getattr(t, "timestamp", None)
        return {
            "transactionHash": getattr(t, "source_trade_id", None),
            "proxyWallet": getattr(t, "trader_address", None),
            "asset": getattr(t, "token_id", None),
            "conditionId": getattr(t, "market_source_id", None),
            "side": side.value if side is not None else None,
            "outcome": getattr(t, "outcome", None),
            "size": getattr(t, "quantity", None),
            "price": getattr(t, "price", None),
            "timestamp": ts.timestamp() if ts is not None else None,
        }

    async def aclose(self) -> None:
        try:
            await self._adapter.aclose()
        except Exception:
            pass


# ── Fixture/fake provider (default mode) ─────────────────────────────────────
class _FixtureProvider:
    """Offline provider: returns no records so the report runs without network.

    This keeps the default CLI fully deterministic and CI-safe. Real data is
    only fetched under --allow-live-preview via _RealDataApiProvider.
    """

    async def fetch_trades(
        self, wallet: str, *, limit: int, page: int
    ) -> list[dict[str, Any]]:
        return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PR24Y real wallet trade source probe (read-only)."
    )
    parser.add_argument("--wallet-address", action="append", default=[],
                        help="Explicit wallet address (repeatable; max 5).")
    parser.add_argument("--wallet-file", default=None,
                        help="File with one wallet address per line.")
    parser.add_argument("--allow-live-preview", dest="allow_live_preview",
                        action="store_true", default=False,
                        help="(OFF by default) Perform a real read-only fetch "
                             "via data-api GET /trades?user=<addr>. Requires an "
                             "explicit wallet. Still non-persisting.")
    parser.add_argument("--limit", type=int, default=DEFAULT_RECORD_LIMIT,
                        help=f"Records per page (hard max {HARD_MAX_RECORD_LIMIT}).")
    parser.add_argument("--max-pages", type=int, default=PR24Y_MAX_PAGES,
                        help=f"Max pages per wallet in PR24Y (hard cap {PR24Y_MAX_PAGES}).")
    parser.add_argument("--json", action="store_true",
                        help="Emit the report as valid JSON.")
    parser.add_argument("--out", default=None,
                        help="Optional file to write the report to.")
    parser.add_argument("--main-db-path", default=None,
                        help="OPTIONAL main DB path for stat-only size/mtime capture "
                             "(os.stat; never opened). Defaults to data/polycopy.db "
                             "when the file exists; pass empty to disable.")
    args = parser.parse_args(argv)

    # Wallet resolution (explicit only; no discovery, no DB list).
    try:
        wallets = validate_wallet_inputs(
            args.wallet_address or None,
            args.wallet_file,
            max_wallets=5,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    limit = max(1, min(args.limit, HARD_MAX_RECORD_LIMIT))
    max_pages = max(1, min(args.max_pages, PR24Y_MAX_PAGES))

    # Provider selection: live only with the explicit flag; else fixture.
    if args.allow_live_preview:
        if not wallets:
            print("error: --allow-live-preview requires an explicit wallet",
                  file=sys.stderr)
            return 2
        provider: Any = _RealDataApiProvider()
    else:
        provider = _FixtureProvider()
        provider.made_network_call = False  # explicit; not counted as network

    # OPTIONAL stat-only DB capture (os.stat; never opens the DB).
    main_db_path: Optional[str] = None
    if args.main_db_path == "":
        main_db_path = None
    elif args.main_db_path:
        main_db_path = args.main_db_path
    else:
        default_db = _REPO_ROOT / "data" / "polycopy.db"
        if default_db.exists():
            main_db_path = str(default_db)

    try:
        result = asyncio.run(
            run_real_trade_source_probe(
                provider,
                wallets,
                allow_live_preview=args.allow_live_preview,
                record_limit=limit,
                max_pages=max_pages,
                main_db_path=main_db_path,
            )
        )
    finally:
        if hasattr(provider, "aclose"):
            try:
                asyncio.run(provider.aclose())
            except Exception:
                pass

    text = report_to_json(result) if args.json else report_to_markdown(result)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote report to {args.out} (read-only; DB untouched).")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
