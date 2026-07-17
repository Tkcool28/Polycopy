#!/usr/bin/env python3
"""Market-centric resolution refresh for specialist-evidence source trades.

Routable WITHOUT a ``markets`` row (keyed only by ``market_source_id`` on
``source_trades``). For each distinct UNRESOLVED ``market_source_id``:

  * unresolved upstream  -> record checked state, NO truth mutation;
  * resolved upstream    -> update EVERY linked ``source_trades`` resolution
                          columns consistently (resolution_status + winning_token_id).

Writes ONLY:
  * ``source_trades.resolution_status`` / ``winning_token_id`` / ``resolved_at``
  * ``specialist_market_refresh_state`` (scheduling/bookkeeping only — it is
    NOT the scoring authority).

Never writes scoring decisions, approvals, dispatches, candidates, or any
execution-plane artifact. The canonical truth remains on ``source_trades``.

Production guard (PR68): writes require ALL of --write --allow-live
--confirm-production-db. Default is dry-run / refusal.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
for _cand in (REPO_ROOT / "src", REPO_ROOT / "scripts", REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from evidence_db import (  # noqa: E402
    DbConn,
    open_readonly,
    open_writable,
    require_write_gates,
)

PRODUCTION_DB_PATH = (REPO_ROOT / "data" / "polycopy.db").resolve()

_MAX_MARKETS = 500


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _distinct_unresolved(db: DbConn, args: argparse.Namespace) -> list[str]:
    clauses = ["resolution_status IS NULL OR resolution_status != 'resolved'"]
    params: list = []
    if args.market_source_id:
        clauses.append("lower(market_source_id) = ?")
        params.append(args.market_source_id.lower())
    if args.wallet_id:
        clauses.append("lower(trader_address) = ?")
        params.append(args.wallet_id.lower())
    if args.watch_id:
        wrow = db.fetchone(
            "SELECT w.address FROM specialist_evidence_watchlist wl "
            "JOIN wallets w ON w.id = wl.wallet_id WHERE wl.watch_id = ?",
            (args.watch_id,),
        )
        if wrow is None:
            return []
        clauses.append("lower(trader_address) = ?")
        params.append(str(wrow["address"]).lower())
    sql = (
        "SELECT DISTINCT market_source_id FROM source_trades WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY market_source_id LIMIT {args.limit_markets}"
    )
    return [r["market_source_id"] for r in db.conn.execute(sql, params).fetchall()]


def _parse_resolution(market: dict) -> tuple[Optional[str], Optional[str]]:
    """Return (status, winning_token_id) from an authoritative Gamma market.

    Accepts Gamma-style ``resolutionStatus`` / ``winner`` (token id)."""
    status = (market.get("resolutionStatus") or market.get("resolution_status")
              or "").lower()
    winner = market.get("winner") or market.get("winning_token_id")
    if status == "resolved" and winner:
        return "resolved", str(winner)
    return status or "unresolved", None


def _run(db: DbConn, get_market: Callable[[str], Optional[dict]],
         args: argparse.Namespace, do_write: bool) -> dict:
    markets = _distinct_unresolved(db, args)
    counts = {
        "markets_checked": 0, "resolved": 0, "unresolved": 0,
        "conflict": 0, "updated_trades": 0, "written": 0,
    }
    for cid in markets:
        counts["markets_checked"] += 1
        market = get_market(cid)
        status, winner = _parse_resolution(market or {})
        # Record bookkeeping (idempotent upsert on market_source_id PK).
        if do_write:
            db.conn.execute(
                "INSERT INTO specialist_market_refresh_state"
                "(market_source_id, last_checked_at, last_status, "
                "next_check_after, attempt_count, resolved_at) "
                "VALUES (?,?,?,?,1,?) "
                "ON CONFLICT(market_source_id) DO UPDATE SET "
                "last_checked_at=excluded.last_checked_at, "
                "last_status=excluded.last_status, "
                "attempt_count=attempt_count+1, "
                "resolved_at=excluded.resolved_at",
                (cid, _now(), status, _now(),
                 _now() if status == "resolved" else None),
            )
        if status != "resolved":
            counts["unresolved"] += 1
            continue
        # Resolved: update ALL linked trades consistently.
        linked = db.conn.execute(
            "SELECT id, winning_token_id FROM source_trades "
            "WHERE lower(market_source_id) = ?",
            (cid.lower(),),
        ).fetchall()
        if not linked:
            counts["resolved"] += 1
            continue
        existing_winners = {r["winning_token_id"] for r in linked}
        if len(existing_winners) > 1 and None not in existing_winners:
            # Conflicting winner evidence already on disk -> block, do not
            # overwrite.
            counts["conflict"] += 1
            continue
        if do_write:
            db.conn.execute(
                "UPDATE source_trades SET resolution_status='resolved', "
                "winning_token_id=?, resolved_at=? "
                "WHERE lower(market_source_id) = ?",
                (winner, _now(), cid.lower()),
            )
            counts["updated_trades"] += len(linked)
            counts["written"] += 1
        counts["resolved"] += 1
    if do_write:
        db.conn.commit()
    return counts


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--market-source-id")
    p.add_argument("--wallet-id")
    p.add_argument("--watch-id")
    p.add_argument("--limit-markets", type=int, default=100)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--write", action="store_true")
    p.add_argument("--allow-live", action="store_true")
    p.add_argument("--confirm-production-db", action="store_true")
    p.add_argument("--json", action="store_true")
    return p


def main(argv=None, *, get_market=None) -> int:
    args = _build_parser().parse_args(argv)
    if args.limit_markets > _MAX_MARKETS:
        print(f"error: --limit-markets exceeds bound {_MAX_MARKETS}", file=sys.stderr)
        return 2
    # Fail-closed write gate (3 gates required on production paths).
    do_write = require_write_gates(args, db_path=args.db_path)
    if args.write and not do_write:
        print(
            "error: production write requires --write --allow-live "
            "--confirm-production-db",
            file=sys.stderr,
        )
        return 2
    db = open_writable(args.db_path, args) if do_write else open_readonly(args.db_path)
    try:
        counts = _run(db, get_market or (lambda cid: None), args, do_write)
    finally:
        db.close()
    if args.json:
        print(json.dumps(counts, indent=2))
    else:
        mode = "WRITE" if do_write else "dry-run"
        print(f"[{mode}] refresh: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
