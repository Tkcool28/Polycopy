#!/usr/bin/env python3
"""S3: real historical taxonomy backfill for specialist evidence source trades.

Fills canonical nested taxonomy/event/series metadata onto existing
``source_trades`` rows using the SHARED
``canonical_metadata.merge_canonical_metadata`` service, fed by the REAL
Polymarket Gamma market fetched through
``PolymarketPublicAdapter.get_market_raw`` (the canonical condition-ID route).
Writes ONLY:

  * ``source_trades.metadata_json`` — safe merge (fills missing, leaves
    unchanged when equivalent, BLOCKS on conflict, leaves unavailable).
  * a provenance row in ``source_trade_enrichments`` (audit-only; never a
    second scoring authority).

It never writes scoring decisions, approvals, dispatches, candidates, signals,
authorizations, risk, orders, fills, positions, marks, or settlements. The
scoring authority remains ``source_trades.metadata_json['taxonomy']['raw_category']``.

Hard contracts (all enforced, fail-closed)
------------------------------------------
* REAL GAMMA PATH: ``get_market_raw`` is the only market source. No second
  implementation, no behavior change to the adapter.
* EXACT SELECTORS (write mode requires EXACTLY ONE of):
    --source-trade-id  -> source_trades.id
    --wallet-id        -> wallets.id, resolved to wallets.address
    --watch-id         -> specialist_evidence_watchlist.id, resolved to
                          wallet_id -> wallets.address
  sample / paused / retired selections are refused; a missing selector and
  multiple selectors are both refused.
* BOUNDS:
    * 1 <= --limit <= _MAX_LIMIT (hard maximum).
    * BUY only, is_sample = 0 only, Polymarket source only.
    * deterministic ordering (ORDER BY source_trade_id).
    * at most ONE Gamma request per distinct market_source_id/condition ID.
    * --allow-live is required for any public network read.
    * dry-run may perform bounded public reads but makes ZERO DB writes.
* MERGE SAFETY: call ``merge_canonical_metadata`` once per selected trade with
  (existing metadata_json, authoritative raw Gamma market, exact
  market_source_id, exact token_id). Persist metadata_json ONLY when status is
  ``filled`` or ``unchanged``. On ``unavailable`` / ``conflict`` do NOT
  serialize/inspect/overwrite the merge output as a dict.
* ATOMICITY / IDEMPOTENCY: canonical metadata update + its enrichment
  provenance commit together per trade. Replay with equivalent evidence
  creates no duplicate enrichment row, makes no metadata change, preserves
  created_at, and leaves no decision/execution artifact.

Production guard (PR68): writes require ALL of --write --allow-live
--confirm-production-db. Default is dry-run / refusal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
for _cand in (REPO_ROOT / "src", REPO_ROOT / "scripts", REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from polycopy.adapters.polymarket import PolymarketPublicAdapter  # noqa: E402
from polycopy.ingestion.canonical_metadata import (  # noqa: E402
    MERGE_CONFLICT,
    MERGE_FILLED,
    MERGE_UNCHANGED,
    MERGE_UNAVAILABLE,
    merge_canonical_metadata,
)
from evidence_db import (  # noqa: E402
    DbConn,
    open_readonly,
    open_writable,
    require_write_gates,
)

PRODUCTION_DB_PATH = (REPO_ROOT / "data" / "polycopy.db").resolve()

# Hard maximum for --limit (inclusive).
_MAX_LIMIT = 500

# Real Gamma base URLs (read-only public endpoints, no auth/order placement).
_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

_STATUS_TO_ENRICHMENT = {
    MERGE_FILLED: "complete",
    MERGE_UNCHANGED: "complete",
    MERGE_CONFLICT: "conflict",
    MERGE_UNAVAILABLE: "unavailable",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Selector resolution (fail-closed) ────────────────────────────────────────


def _resolve_selector(
    db: DbConn, args: argparse.Namespace
) -> tuple[Optional[str], Optional[str]]:
    """Map the chosen selector to a ``lower(trader_address)`` filter.

    Returns ``(address_filter, error)``. ``address_filter`` is the resolved
    ``lower(trader_address)`` for wallet/watch selectors, or ``None`` for a
    valid ``--source-trade-id`` selection (keyed by id instead). ``error`` is
    a human string when the selection is invalid (no selector, multiple
    selectors, sample/paused/retired, or not found).
    """
    chosen = [
        bool(args.source_trade_id),
        bool(args.wallet_id),
        bool(args.watch_id),
    ]
    if sum(chosen) == 0:
        return None, "no_selector"
    if sum(chosen) > 1:
        return None, "multiple_selectors"

    if args.source_trade_id:
        return None, None

    if args.wallet_id:
        row = db.fetchone(
            "SELECT address, is_sample FROM wallets WHERE id=?",
            (args.wallet_id,),
        )
        if row is None:
            return None, "wallet_not_found"
        if bool(row["is_sample"]):
            return None, "sample_wallet_refused"
        return str(row["address"]).lower(), None

    # --watch-id
    row = db.fetchone(
        "SELECT w.address, w.is_sample, wl.status "
        "FROM specialist_evidence_watchlist wl "
        "JOIN wallets w ON w.id = wl.wallet_id WHERE wl.id=?",
        (args.watch_id,),
    )
    if row is None:
        return None, "watch_not_found"
    if bool(row["is_sample"]):
        return None, "sample_wallet_refused"
    if row["status"] != "active":
        return None, f"watch_{row['status']}_refused"
    return str(row["address"]).lower(), None


def _select_trades(
    db: DbConn, args: argparse.Namespace, address_filter: Optional[str]
) -> list[dict]:
    """Return non-sample BUY Polymarket source_trades per the selector.

    Deterministic ordering by source_trade_id. Bounds: BUY only, is_sample=0,
    Polymarket source only, limited to ``args.limit``. ``address_filter`` is
    the resolved ``lower(trader_address)`` for wallet/watch selectors, or None
    for a --source-trade-id selection (keyed by id instead).
    """
    clauses = [
        "side = 'BUY'",
        "is_sample = 0",
        "lower(source_trade_id) LIKE 'polymarket:%'",
    ]
    params: list[Any] = []
    if args.source_trade_id:
        clauses.append("id = ?")
        params.append(args.source_trade_id)
    else:
        # wallet-id / watch-id resolved to a canonical lower(address) filter.
        clauses.append("lower(trader_address) = ?")
        params.append(address_filter or "")
    sql = (
        "SELECT id, source_trade_id, market_source_id, token_id, "
        "trader_address, metadata_json FROM source_trades "
        "WHERE " + " AND ".join(clauses) + " ORDER BY source_trade_id LIMIT ?"
    )
    params.append(args.limit)
    return [dict(r) for r in db.conn.execute(sql, params).fetchall()]


# ── Real Gamma resolution (one request per condition id) ─────────────────────


def _make_adapter() -> PolymarketPublicAdapter:
    return PolymarketPublicAdapter(
        gamma_base_url=_GAMMA_BASE_URL,
        clob_base_url="https://clob.polymarket.com",
        data_api_base_url="https://data-api.polymarket.com",
    )


async def _resolve_gamma_market(
    adapter: PolymarketPublicAdapter, condition_id: str
) -> Optional[dict[str, Any]]:
    """Fetch the authoritative raw Gamma market for one condition id.

    Uses ONLY ``PolymarketPublicAdapter.get_market_raw`` (the canonical
    condition-ID route). Returns the raw dict (with ``clobTokenIds``) or None
    when not found / ambiguous / network-failed.
    """
    try:
        return await adapter.get_market_raw(condition_id)
    except Exception:
        return None


async def _resolve_gamma_batch(
    adapter: PolymarketPublicAdapter, condition_ids: list[str]
) -> dict[str, Optional[dict[str, Any]]]:
    """Resolve each distinct condition id at most ONCE.

    Returns a mapping ``{condition_id_lower: raw_gamma_market_or_None}``. The
    loop awaits every distinct id; identical condition ids are served from a
    single request (de-duplicated before the loop), so multiple trades
    sharing a condition id incur exactly one Gamma request.
    """
    out: dict[str, Optional[dict[str, Any]]] = {}
    seen: set[str] = set()
    for cid in condition_ids:
        key = (cid or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out[key] = await _resolve_gamma_market(adapter, cid)
    return out


# ── Provenance (idempotent, honest, audit-only) ──────────────────────────────


def _write_provenance(
    db: DbConn,
    internal_id: str,
    status: str,
    reason_codes: list[str],
    gamma_market: Optional[dict[str, Any]],
) -> bool:
    """Write an idempotent provenance row (no second scoring authority).

    The enrichment row is keyed by ``(internal_id)`` via a deterministic
    ``enrichment_id`` derived from the trade id so a replay with equivalent
    evidence writes the SAME id (``INSERT OR IGNORE`` -> 0 rows on replay).
    Returns True iff a NEW row was inserted.

    Honest provenance: status, taxonomy_status, gamma_source, reason codes and
    fetched_at are recorded; normalized_category is recorded only as the
    audit-only classification (never a scoring authority — the scorer reads
    ``source_trades.metadata_json['taxonomy']['raw_category']``).
    """
    enrichment_id = f"bk:{internal_id}"
    g_cid = (gamma_market or {}).get("conditionId")
    g_tok = (gamma_market or {}).get("tokenId") or (gamma_market or {}).get("token_id")
    g_slug = (gamma_market or {}).get("slug") or (gamma_market or {}).get("question")
    category = (gamma_market or {}).get("category")
    cur = db.conn.execute(
        "INSERT OR IGNORE INTO source_trade_enrichments "
        "(enrichment_id, source_trade_internal_id, status, token_id, "
        "condition_id, market_slug, normalized_category, taxonomy_status, "
        "evidence_source, gamma_source, reason_codes_json, fetched_at, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            enrichment_id, internal_id,
            _STATUS_TO_ENRICHMENT.get(status, "unavailable"),
            g_tok, g_cid, g_slug, category,
            status, "backfill",
            "gamma_market_raw" if gamma_market is not None else None,
            json.dumps(reason_codes, sort_keys=True), _now(), _now(), _now(),
        ),
    )
    return cur.rowcount > 0


# ── Run ──────────────────────────────────────────────────────────────────────


async def _run_async(
    db: DbConn,
    args: argparse.Namespace,
    do_write: bool,
    address_filter: Optional[str],
) -> dict:
    trades = _select_trades(db, args, address_filter)
    counts = {
        "selected": len(trades),
        "filled": 0,
        "unchanged": 0,
        "conflict": 0,
        "unavailable": 0,
        "written": 0,
    }
    if not trades:
        return counts

    # Real Gamma: resolve each distinct condition id at most once. A real
    # adapter is always constructed because --allow-live already gates any
    # public read (dry-run performs bounded public reads; write performs them
    # and then writes).
    adapter = _make_adapter()
    condition_ids = [t["market_source_id"] for t in trades]
    gamma_by_cid = await _resolve_gamma_batch(adapter, condition_ids)
    try:
        await adapter.aclose()
    except Exception:
        pass

    for t in trades:
        cid = (t.get("market_source_id") or "").lower()
        gamma = gamma_by_cid.get(cid)
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

        if not do_write:
            # Dry-run: zero DB writes, but bounded public reads already happened.
            continue

        if status in (MERGE_FILLED, MERGE_UNCHANGED):
            # Persist only on filled/unchanged. The merge output is a valid
            # dict here by contract; serialize and overwrite metadata_json.
            db.conn.execute(
                "UPDATE source_trades SET metadata_json = ? WHERE id = ?",
                (json.dumps(new_meta, sort_keys=True), t["id"]),
            )
            if _write_provenance(db, t["id"], status, reasons, gamma):
                counts["written"] += 1
        else:
            # unavailable / conflict: do NOT serialize, inspect, or overwrite
            # the merge output. Record honest provenance only.
            if _write_provenance(db, t["id"], status, reasons, gamma):
                counts["written"] += 1

    if do_write:
        db.conn.commit()
    return counts


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--source-trade-id", help="source_trades.id")
    p.add_argument("--wallet-id", help="wallets.id (resolved to address)")
    p.add_argument(
        "--watch-id", help="specialist_evidence_watchlist.id (resolved to address)"
    )
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--dry-run", action="store_true",
                   help="No writes (default for this CLI).")
    p.add_argument("--write", action="store_true",
                   help="Persist merged metadata (refused on prod without gate).")
    p.add_argument("--allow-live", action="store_true",
                   help="Required for any public network read.")
    p.add_argument("--confirm-production-db", action="store_true")
    p.add_argument("--json", action="store_true")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    # Bounds: 1 <= limit <= _MAX_LIMIT.
    if args.limit < 1 or args.limit > _MAX_LIMIT:
        print(
            f"error: --limit must be in [1, {_MAX_LIMIT}] (got {args.limit})",
            file=sys.stderr,
        )
        return 2

    # Fail-closed selector resolution (open read-only first).
    db_ro = open_readonly(args.db_path)
    try:
        address_filter, sel_err = _resolve_selector(db_ro, args)
    finally:
        db_ro.close()
    if sel_err is not None:
        print(f"error: selector invalid: {sel_err}", file=sys.stderr)
        return 2

    # --allow-live is required for ANY public network read (even dry-run).
    if not args.allow_live:
        print(
            "error: --allow-live is required to read public Gamma data",
            file=sys.stderr,
        )
        return 2

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
        counts = asyncio.run(_run_async(db, args, do_write, address_filter))
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
