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
  * one CURRENT provenance row in ``source_trade_enrichments`` (audit-only;
    never a second scoring authority), keyed by ``source_trade_internal_id``.

It never writes scoring decisions, approvals, dispatches, candidates, signals,
authorizations, risk, orders, fills, positions, marks, or settlements. The
scoring authority remains ``source_trades.metadata_json['taxonomy']['raw_category']``.

Hard contracts (all enforced, fail-closed)
------------------------------------------
* REAL GAMMA PATH: ``get_market_raw`` is the only market source. No second
  implementation, no behavior change to the adapter.
* PRODUCTION REFUSAL ORDERING: for a recognized production path, a requested
  write missing ANY of --write / --allow-live / --confirm-production-db is
  refused with exit 2 BEFORE checking file existence, opening read-only,
  reading schema, resolving selectors, making a network request, or opening
  writable.
* EXACT SELECTORS (write mode requires EXACTLY ONE of):
    --source-trade-id  -> source_trades.id
    --wallet-id        -> wallets.id, resolved to wallets.address
    --watch-id         -> specialist_evidence_watchlist.id -> wallet_id -> address
  sample / paused / retired selections are refused; a missing selector and
  multiple selectors are both refused.
* BOUNDS:
    * 1 <= --limit <= _MAX_LIMIT (hard maximum).
    * BUY only, is_sample = 0 only, Polymarket source only (filtered on the
      canonical ``source`` column value 'polymarket', not merely the id prefix).
    * deterministic ordering (ORDER BY source_trade_id).
    * at most ONE Gamma request per distinct market_source_id/condition ID.
    * --allow-live is required for any public network read.
    * dry-run may perform bounded public reads but makes ZERO DB writes.
* MERGE SAFETY: call ``merge_canonical_metadata`` once per selected trade with
  (existing metadata_json, authoritative raw Gamma market, exact
  market_source_id, exact token_id). Persist metadata_json ONLY when status is
  ``filled`` or ``unchanged``. On ``unavailable`` / ``conflict`` do NOT
  serialize/inspect/overwrite the merge output as a dict.
* ATOMICITY / IDEMPOTENCY: each trade's metadata update + its current
  enrichment provenance commit together inside a per-trade SAVEPOINT. A replay
  with equivalent evidence creates no duplicate enrichment row, makes no
  metadata change, preserves created_at, and leaves no decision/execution
  artifact.

Production guard (PR68): writes require ALL of --write --allow-live
--confirm-production-db on a recognized production path. Default is dry-run /
refusal.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
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
from polycopy.ingestion.normalized_source_trade import SOURCE_NAME  # noqa: E402
from polycopy.scoring.wallet_evidence import (  # noqa: E402
    CATEGORY_TAXONOMY_PARTIAL,
    CATEGORY_TAXONOMY_USABLE,
    classify_category_taxonomy,
)
from evidence_db import (  # noqa: E402
    DbConn,
    is_production_db,
    open_readonly,
    open_writable,
    require_write_gates,
)

# Canonical approved-wallet ingestion source (the authoritative writer used by
# source_trade_writer / ingest_real_source_trades / approved-wallet bridge).
# We accept this value plus other repository-PROVEN Polymarket source_trades
# writers — exact values only, never fuzzy matching or id prefixes.
#   * "polymarket_data_api_trades_user" -> SOURCE_NAME (canonical approved wallet)
#   * "polymarket_clob"                 -> collect_smart_money_data._persist_trade
# A bare "polymarket" literal is NOT a proven source_trades writer value here
# (it is used for the markets/raw_snapshots tables), so it is intentionally
# excluded unless future repository evidence proves otherwise.
POLYMARKET_SOURCES = frozenset({SOURCE_NAME, "polymarket_clob"})

PRODUCTION_DB_PATH = (REPO_ROOT / "data" / "polycopy.db").resolve()

# Hard maximum for --limit (inclusive).
_MAX_LIMIT = 500

# Real Gamma base URLs (read-only public endpoints, no auth/order placement).
_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Gamma resolution result (distinguish not-found from provider error) ──────


class GammaResult:
    """Outcome of resolving one condition id from the real Gamma provider."""

    __slots__ = ("state", "market", "reason")

    def __init__(self, state: str, market: Optional[dict[str, Any]] = None,
                 reason: Optional[str] = None) -> None:
        # state: found | not_found | provider_error | ambiguous | malformed
        self.state = state
        self.market = market
        self.reason = reason


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
    Polymarket source only (canonical ``source`` column), limited to
    ``args.limit``. ``address_filter`` is the resolved ``lower(trader_address)``
    for wallet/watch selectors, or None for a --source-trade-id selection
    (keyed by id instead).
    """
    # Polymarket-only is enforced on the canonical source column, not merely an
    # id prefix (a non-Polymarket row with a polymarket-looking id is excluded).
    # Only repository-PROVEN Polymarket source_trades writer values are accepted.
    placeholders = ", ".join("?" for _ in POLYMARKET_SOURCES)
    clauses = [
        "side = 'BUY'",
        "is_sample = 0",
        f"source IN ({placeholders})",
    ]
    params: list[Any] = list(POLYMARKET_SOURCES)
    if args.source_trade_id:
        clauses.append("id = ?")
        params.append(args.source_trade_id)
    else:
        # wallet-id / watch-id resolved to a canonical lower(address) filter.
        clauses.append("lower(trader_address) = ?")
        params.append(address_filter or "")
    sql = (
        "SELECT id, source, source_trade_id, market_source_id, token_id, "
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


async def _resolve_gamma_one(
    adapter: PolymarketPublicAdapter, condition_id: str
) -> GammaResult:
    """Resolve one condition id through the REAL get_market_raw route.

    Distinguishes:
      * found            -> authoritative Gamma dict returned
      * not_found        -> 404 / no exact match (honest "gamma_missing")
      * provider_error    -> HTTP/network error (NOT conflated with not_found)
      * ambiguous        -> provider returned multiple exact matches
      * malformed        -> provider returned an unexpected payload shape
    """
    try:
        market = await adapter.get_market_raw(condition_id)
    except ValueError as exc:
        msg = str(exc)
        if "ambiguous" in msg:
            return GammaResult("ambiguous", reason=msg)
        return GammaResult("malformed", reason=msg)
    except Exception as exc:  # HTTP / network / client failure
        return GammaResult("provider_error", reason=f"{type(exc).__name__}: {exc}")
    if market is None:
        return GammaResult("not_found")
    return GammaResult("found", market=market)


async def _resolve_gamma_batch(
    adapter: PolymarketPublicAdapter, condition_ids: list[str]
) -> dict[str, GammaResult]:
    """Resolve each distinct condition id at most ONCE.

    Returns a mapping ``{condition_id_lower: GammaResult}``. Identical
    condition ids are served from a single request (de-duplicated before the
    loop), so multiple trades sharing a condition id incur exactly one Gamma
    request.
    """
    out: dict[str, GammaResult] = {}
    seen: set[str] = set()
    for cid in condition_ids:
        key = (cid or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out[key] = await _resolve_gamma_one(adapter, cid)
    return out


# ── Provenance (one current row per trade; honest; audit-only) ───────────────


def _taxonomy_status(canonical_meta: dict[str, Any], merge_status: str) -> str:
    """Map to the enrichment table's taxonomy_status vocabulary.

    usable / partial / unavailable — derived from the canonical nested metadata
    classification, never claimed unless safe. For conflicts/unavailable we do
    not assert a usable label.
    """
    if merge_status in (MERGE_CONFLICT, MERGE_UNAVAILABLE):
        return "unavailable"
    try:
        cls = classify_category_taxonomy(canonical_meta)
    except Exception:
        return "unavailable"
    status = str(cls.status)
    if status == CATEGORY_TAXONOMY_USABLE and cls.category_label:
        return "usable"
    if status == CATEGORY_TAXONOMY_PARTIAL:
        return "partial"
    return "unavailable"


def _build_evidence(
    trade: dict[str, Any],
    canonical_meta: dict[str, Any],
    gamma: Optional[dict[str, Any]],
    merge_status: str,
    gamma_result: GammaResult,
    merge_reasons: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Build the honest current provenance payload (no scoring authority)."""
    # canonical_meta may be the raw preserved value (a malformed string) on
    # unavailable/conflict; only classify a real dict.
    safe_meta = canonical_meta if isinstance(canonical_meta, dict) else {}
    try:
        classification = classify_category_taxonomy(safe_meta)
    except Exception:
        classification = None
    # normalized_category may be populated ONLY when the merge is filled/unchanged
    # AND the canonical taxonomy classification is usable. On conflict/unavailable
    # it MUST remain NULL (we never claim a usable normalized taxonomy).
    usable = (
        classification is not None
        and str(classification.status) == CATEGORY_TAXONOMY_USABLE
        and classification.category_label
    )
    if merge_status in (MERGE_FILLED, MERGE_UNCHANGED) and usable:
        normalized_category = classification.category_label
    else:
        normalized_category = None

    # High-level status vocabulary for source_trade_enrichments.status.
    # A provider/network error takes precedence over an (unavailable) merge
    # result so it is never conflated with an ordinary gamma_missing.
    if gamma_result.state == "provider_error":
        status = "error"
    elif merge_status == MERGE_CONFLICT:
        status = "conflict"
    elif merge_status == MERGE_UNAVAILABLE:
        status = "unavailable"
    elif normalized_category:
        status = "complete"
    else:
        status = "incomplete"

    # market_slug only from a non-empty authoritative Gamma "slug" field.
    # Never fall back to question/title text.
    slug = None
    if gamma is not None:
        raw_slug = gamma.get("slug")
        if isinstance(raw_slug, str) and raw_slug.strip():
            slug = raw_slug

    # token_id from the SOURCE TRADE, never from Gamma's clobTokenIds.
    token_id = trade.get("token_id")
    condition_id = trade.get("market_source_id") or ""

    # Persist the EXACT merge reason codes (do not discard them), alongside the
    # merge status, gamma state, and any provider-specific reason.
    reason_codes: list[str] = []
    for r in (merge_reasons or []):
        if r and r not in reason_codes:
            reason_codes.append(r)
    reason_codes.append(f"merge:{merge_status}")
    reason_codes.append(f"gamma:{gamma_result.state}")
    if gamma_result.state == "provider_error":
        reason_codes.append("provider_error")
    if gamma_result.reason:
        reason_codes.append(gamma_result.reason)

    return {
        "status": status,
        "token_id": token_id,
        "condition_id": condition_id,
        "market_id": condition_id,
        "market_slug": slug,
        "normalized_category": normalized_category,
        "taxonomy_status": _taxonomy_status(safe_meta, merge_status),
        "gamma_source": "gamma_market_raw" if gamma is not None else None,
        "evidence_source": "backfill",
        "reason_codes": reason_codes,
    }


def _evidence_hash(ev: dict[str, Any]) -> str:
    canonical = json.dumps(ev, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_provenance(
    db: DbConn,
    trade: dict[str, Any],
    ev: dict[str, Any],
    merge_status: str,
) -> tuple[bool, bool]:
    """Upsert the single CURRENT provenance row keyed by source_trade_internal_id.

    Returns ``(changed, is_new)``:
      * insert when absent (created_at = now)
      * update current provenance when evidence materially changes
        (evidence_hash differs) — preserves created_at, updates updated_at
      * equivalent replay performs no write (changed = False)
      * never creates a duplicate row (UNIQUE(source_trade_internal_id))
      * never silently ignores newer conflict/unavailable evidence
    Does NOT mutate enrichment_id to bypass uniqueness.
    """
    internal_id = trade["id"]
    now = _now()
    ev_hash = _evidence_hash(ev)
    existing = db.fetchone(
        "SELECT enrichment_id, evidence_hash, created_at FROM "
        "source_trade_enrichments WHERE source_trade_internal_id=?",
        (internal_id,),
    )
    if existing is None:
        enrichment_id = f"bk:{internal_id}"
        db.conn.execute(
            "INSERT INTO source_trade_enrichments ("
            "enrichment_id, source_trade_internal_id, status, token_id, "
            "condition_id, market_id, market_slug, normalized_category, "
            "taxonomy_status, evidence_source, gamma_source, evidence_hash, "
            "reason_codes_json, fetched_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                enrichment_id, internal_id, ev["status"], ev["token_id"],
                ev["condition_id"], ev["market_id"], ev["market_slug"],
                ev["normalized_category"], ev["taxonomy_status"],
                ev["evidence_source"], ev["gamma_source"], ev_hash,
                json.dumps(ev["reason_codes"], sort_keys=True), now, now, now,
            ),
        )
        return True, True

    # Existing current row: only update when evidence materially changed.
    if existing["evidence_hash"] == ev_hash:
        return False, False
    db.conn.execute(
        "UPDATE source_trade_enrichments SET "
        "status=?, token_id=?, condition_id=?, market_id=?, market_slug=?, "
        "normalized_category=?, taxonomy_status=?, evidence_source=?, "
        "gamma_source=?, evidence_hash=?, reason_codes_json=?, "
        "fetched_at=?, updated_at=? WHERE source_trade_internal_id=?",
        (
            ev["status"], ev["token_id"], ev["condition_id"], ev["market_id"],
            ev["market_slug"], ev["normalized_category"], ev["taxonomy_status"],
            ev["evidence_source"], ev["gamma_source"], ev_hash,
            json.dumps(ev["reason_codes"], sort_keys=True), now, now,
            internal_id,
        ),
    )
    return True, False


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
        "provider_error": 0,
        "written": 0,
    }
    if not trades:
        return counts

    # Real Gamma: resolve each distinct condition id at most once.
    adapter = _make_adapter()
    condition_ids = [t["market_source_id"] for t in trades]
    try:
        gamma_by_cid = await _resolve_gamma_batch(adapter, condition_ids)
    finally:
        try:
            await adapter.aclose()
        except Exception:
            pass

    for t in trades:
        cid = (t.get("market_source_id") or "").lower()
        gamma_result = gamma_by_cid.get(cid, GammaResult("not_found"))
        gamma = gamma_result.market if gamma_result.state == "found" else None

        new_meta, merge_status, merge_reasons = merge_canonical_metadata(
            t["metadata_json"], gamma,
            condition_id=t["market_source_id"] or "", token_id=t.get("token_id"),
        )
        if merge_status == MERGE_FILLED:
            counts["filled"] += 1
        elif merge_status == MERGE_UNCHANGED:
            counts["unchanged"] += 1
        elif merge_status == MERGE_CONFLICT:
            counts["conflict"] += 1
        else:
            counts["unavailable"] += 1
        if gamma_result.state == "provider_error":
            counts["provider_error"] += 1

        if not do_write:
            # Dry-run: zero DB writes, but bounded public reads already happened.
            continue

        # Per-trade atomic SAVEPOINT: metadata + current provenance commit
        # together; a provenance failure rolls back the metadata change too.
        db.conn.execute("SAVEPOINT s3_backfill")
        try:
            if merge_status in (MERGE_FILLED, MERGE_UNCHANGED):
                # Persist only on filled/unchanged. The merge output is a valid
                # dict here by contract; serialize and overwrite metadata_json.
                db.conn.execute(
                    "UPDATE source_trades SET metadata_json = ? WHERE id = ?",
                    (json.dumps(new_meta, sort_keys=True), t["id"]),
                )
            # Build honest provenance from canonical + source-trade values.
            ev = _build_evidence(t, new_meta, gamma, merge_status, gamma_result,
                                 merge_reasons=merge_reasons)
            changed, _is_new = _write_provenance(db, t, ev, merge_status)
            if changed:
                counts["written"] += 1
            db.conn.execute("RELEASE SAVEPOINT s3_backfill")
        except Exception:
            db.conn.execute("ROLLBACK TO SAVEPOINT s3_backfill")
            db.conn.execute("RELEASE SAVEPOINT s3_backfill")
            raise

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

    # PRODUCTION REFUSAL ORDERING: for a recognized production path, refuse a
    # requested write missing ANY gate BEFORE opening SQLite, reading schema,
    # resolving selectors, or touching the network.
    if args.write and is_production_db(args.db_path):
        if not require_write_gates(args, db_path=args.db_path):
            print(
                "error: production write refused — requires --write "
                "--allow-live --confirm-production-db",
                file=sys.stderr,
            )
            return 2

    # Fail-closed selector resolution (open read-only first; not a write).
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
    except Exception as exc:  # fail-closed: never crash with a raw traceback
        print(f"error: backfill failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
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
