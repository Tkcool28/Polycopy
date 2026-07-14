#!/usr/bin/env python3
"""PR69 report-only short-horizon specialist wallet audit.

Default mode is offline: no network, no database, and no files are written.
``--allow-live`` enables only bounded public GETs; this command never opens a
DB, writes an approval/candidate/order/position, or invokes a bridge.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from polycopy.discovery.short_horizon_specialists import discover_short_horizon_specialists  # noqa: E402

MAX_MARKETS = 10
MAX_TRADES_PER_MARKET = 100
MAX_LEADERBOARD = 20


def _load_fixture(path: str | None) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    if not path:
        return [], {}, []
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict):
        raise ValueError("fixture must be an object with markets, market_trades, leaderboard")
    markets = payload.get("markets", [])
    trades = payload.get("market_trades", {})
    leaderboard = payload.get("leaderboard", [])
    if not isinstance(markets, list) or not isinstance(trades, dict) or not isinstance(leaderboard, list):
        raise ValueError("fixture fields have invalid types")
    return markets, trades, leaderboard


async def _live() -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    from polycopy.adapters.polymarket import PolymarketPublicAdapter
    adapter = PolymarketPublicAdapter("https://gamma-api.polymarket.com", "https://clob.polymarket.com", timeout=10.0)
    try:
        markets = await adapter.list_active_markets_raw(limit=MAX_MARKETS)
        leaderboard = await adapter.get_public_leaderboard(limit=MAX_LEADERBOARD)
        # Typed public trades contain no fabricated resolution/P&L fields. The
        # pure engine therefore reports incomplete scores honestly.
        trade_map: dict[str, list[dict[str, Any]]] = {}
        for market in markets:
            mid = str(market.get("conditionId") or "")
            result = await adapter.fetch_trades_for_market(mid, limit=MAX_TRADES_PER_MARKET, max_pages=1, max_rows=MAX_TRADES_PER_MARKET)
            if result.status != "complete":
                continue
            trade_map[mid.lower()] = [{"source_trade_id": t.source_trade_id, "conditionId": t.market_source_id, "proxyWallet": t.trader_address, "timestamp": t.timestamp.isoformat(), "side": t.side.value} for t in result.trades]
        return markets, trade_map, leaderboard
    finally:
        await adapter.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--input-file")
    parser.add_argument("--output-dir", help="explicit report directory; absent means no file write")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.allow_live and args.input_file:
        parser.error("--allow-live cannot be combined with offline --input-file")
    try:
        if args.allow_live:
            markets, trades, leaderboard = asyncio.run(_live())
        else:
            markets, trades, leaderboard = _load_fixture(args.input_file)
        report = discover_short_horizon_specialists(markets, trades, leaderboard, now=datetime.now(timezone.utc)).to_dict()
        report["live_read_performed"] = bool(args.allow_live)
        report["db_opened"] = False
        report["writes_performed"] = False
        encoded = json.dumps(report, sort_keys=True, separators=(",", ":"), default=str)
        if args.output_dir:
            destination = Path(args.output_dir)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "short_horizon_specialist_wallet_audit.json").write_text(encoded + "\n")
        print(encoded)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
