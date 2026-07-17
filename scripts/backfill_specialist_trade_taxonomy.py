#!/usr/bin/env python3
"""Bounded historical taxonomy backfill for specialist evidence source trades.

Fills canonical nested taxonomy/event/series metadata onto existing
``source_trades`` rows using the SHARED canonical_metadata.merge_canonical_metadata
service. Writes ONLY:

  * ``source_trades.metadata_json`` (safe merge — fills missing, leaves
    unchanged when equivalent, BLOCKS on conflict, leaves unavailable when
    Gamma is missing).
  * a provenance row in ``source_trade_enrichments``.

It never writes scoring decisions, approvals, dispatches, candidates, or any
execution-plane artifact. The scoring authority remains
``source_trades.metadata_json['taxonomy']['raw_category']``.

Production guard (PR68): writes require ALL of --write --allow-live
--confirm-production-db. Default is dry-run / refusal.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _cand in (REPO_ROOT / "src", REPO_ROOT / "scripts", REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from polycopy.ingestion.canonical_metadata import (  # noqa: E402
    merge_canonical_metadata,
    MERGE_FILLED,
    MERGE_UNCHANGED,
    MERGE_CONFLICT,
)
from evidence_db import (  # noqa: E402
    DbConn,
    open_readonly,
    open_writable,
    require_write_gates,
)

PRODUCTION_DB_PATH = (REPO_ROOT / "data" / "polycopy.db").resolve()

# Backfill never touches execution-plane tables; this set is for the guard's
# production-path refusal only.
_MAX_LIMIT = 500


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _select_trades(db: DbConn, args: argparse.Namespace) -> list[dict]:
    """Return non-sample BUY source_trades per the selectors, ordered
    deterministically by source_trade_id."""
    clauses = ["side = 'BUY'", "is_sample = 0", "lower(source_trade_id) LIKE 'polymarket:%'"]
    params: list = []
    if args.source_trade_id:
        clauses.append("source_trade_id = ?")
        params.append(args.source_trade_id)
    if args.wallet_id:
        clauses.append("lower(trader_address) = ?")
        params.append(args.wallet_id.lower())
    if args.watch_id:
        # Resolve the watch's wallet address via the watchlist.
        wrow = db.fetchone(
            "SELECT w.address FROM specialist_evidence_watchlist wl "
            "JOIN wallets w ON w.id = wl.wallet_id WHERE wl.watch_id = ?",
            (args.watch_id,),
        )
        if wrow is None:
            return []
        clauses.append("lower(trader_address) = ?")
        params.append(str(wrow["address"]).lower())
    limit = args.limit
    sql = (
        "SELECT id, source_trade_id, market_source_id, token_id, "
        "trader_address, metadata_json FROM source_trades "
        "WHERE " + " AND ".join(clauses) + " ORDER BY source_trade_id LIMIT ?"
    )
    params.append(limit)
    return [dict(r) for r in db.conn.execute(sql, params).fetchall()]


def _gamma_resolver_factory(db: DbConn):
    """Resolve a condition_id to a market dict from local source_trades
    metadata, or None. For backfill we prefer a trusted Gamma lookup, but
    the shared merge needs a Mapping with conditionId/category/tags/events.
    We synthesize the minimal Mapping from the trade's OWN existing
    metadata if it already has a gamma block; otherwise the caller must
    supply a resolver. For the CLI, we use the trade's stored gamma block
    as the authoritative market dict (idempotent backfill of incomplete
    rows)."""

    def _resolve(condition_id: str):
        row = db.fetchone(
            "SELECT metadata_json FROM source_trades "
            "WHERE lower(market_source_id) = ? LIMIT 1",
            (condition_id.lower(),),
        )
        if row is None:
            return None
        meta = row["metadata_json"]
        if not meta:
            return None
        try:
            m = json.loads(meta)
        except (TypeError, ValueError):
            return None
        gamma = m.get("gamma")
        if not isinstance(gamma, dict):
            return None
        return gamma

    return _resolve


def _write_provenance(db: DbConn, internal_id: str, status: str,
                      reason_codes: list[str], gamma_market) -> bool:
    """Write an idempotent provenance row. Returns True if a NEW row was
    inserted (skips if one already exists for this trade from a prior
    backfill run)."""
    eid = f"bk_{internal_id}_{_now().replace(':', '').replace('-', '')}"
    g_cid = (gamma_market or {}).get("conditionId")
    g_tok = (gamma_market or {}).get("tokenId") or (gamma_market or {}).get("token_id")
    g_slug = (gamma_market or {}).get("slug")
    cur = db.conn.execute(
        "INSERT OR IGNORE INTO source_trade_enrichments "
        "(enrichment_id, source_trade_internal_id, status, token_id, "
        "condition_id, market_slug, normalized_category, taxonomy_status, "
        "evidence_source, gamma_source, reason_codes_json, fetched_at, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            eid, internal_id, "complete" if status in (MERGE_FILLED, MERGE_UNCHANGED)
            else ("conflict" if status == MERGE_CONFLICT else "unavailable"),
            g_tok, g_cid, g_slug,
            (gamma_market or {}).get("category"),
            status, "backfill", "gamma_cache",
            json.dumps(reason_codes), _now(), _now(), _now(),
        ),
    )
    return cur.rowcount > 0


def _run(db: DbConn, args: argparse.Namespace, do_write: bool) -> dict:
    trades = _select_trades(db, args)
    resolver = _gamma_resolver_factory(db)
    counts = {
        "selected": len(trades), "filled": 0, "unchanged": 0,
        "conflict": 0, "unavailable": 0, "written": 0,
    }
    for t in trades:
        gamma = resolver(t["market_source_id"])
        new_meta, status, reasons = merge_canonical_metadata(
            t["metadata_json"], gamma,
            condition_id=t["market_source_id"] or "", token_id=t.get("token_id"),
        )
        if status == MERGE_FILLED:
            counts["filled"] += 1
        elif status == MERGE_UNCHANGED:
            counts["unchanged"] += 1
        elif status == MERGE_CONFLICT:
            counts["conflict"] += 1
        else:
            counts["unavailable"] += 1
        if do_write and status in (MERGE_FILLED, MERGE_UNCHANGED):
            db.conn.execute(
                "UPDATE source_trades SET metadata_json = ? WHERE id = ?",
                (json.dumps(new_meta, sort_keys=True), t["id"]),
            )
            if _write_provenance(db, t["id"], status, reasons, gamma):
                counts["written"] += 1
        elif do_write and status == MERGE_CONFLICT:
            # Record conflict provenance but DO NOT overwrite.
            if _write_provenance(db, t["id"], status, reasons, gamma):
                counts["written"] += 1
    if do_write:
        db.conn.commit()
    return counts


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--source-trade-id")
    p.add_argument("--wallet-id")
    p.add_argument("--watch-id")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--dry-run", action="store_true",
                   help="No writes (default for this CLI).")
    p.add_argument("--write", action="store_true",
                   help="Persist merged metadata (refused on prod without gate).")
    p.add_argument("--allow-live", action="store_true")
    p.add_argument("--confirm-production-db", action="store_true")
    p.add_argument("--json", action="store_true")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if args.limit > _MAX_LIMIT:
        print(f"error: --limit exceeds bound {_MAX_LIMIT}", file=sys.stderr)
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
        counts = _run(db, args, do_write)
    finally:
        db.close()
    if args.json:
        print(json.dumps(counts, indent=2))
    else:
        mode = "WRITE" if do_write else "dry-run"
        print(f"[{mode}] backfill: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
