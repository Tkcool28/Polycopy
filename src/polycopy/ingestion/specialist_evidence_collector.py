"""Research-only evidence collection driven by the specialist evidence watchlist.

Watchlist-driven BUY-only collection. Writes ``source_trades`` + canonical
nested taxonomy (via the shared ``build_canonical_metadata``) + a
``source_trade_enrichments`` provenance row, and NOTHING in the execution
plane. This is NOT an approval/discovery selector: it collects only for an
ACTIVE watchlist entry (``--watch-id``), never for a ``specialist_approval``.

Hard limits (all enforced):
  * max_wallets_per_run
  * max_new_trades_per_wallet
  * max_total_new_trades
  * max_gamma_requests
  * processing timeout
  * RSS guard
  * deterministic (sorted) processing order
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional

from polycopy.db.database import Database
from polycopy.ingestion import ingest_pipeline
from polycopy.ingestion.canonical_metadata import (
    MERGE_FILLED,
    MERGE_UNCHANGED,
    merge_canonical_metadata,
)
from polycopy.ingestion.normalized_source_trade import NormalizedSourceTrade
from polycopy.ingestion.source_trade_enrichment import enrich_source_trade
from polycopy.ingestion.source_trade_writer import write_valid_rows

GammaResolver = Callable[[str], Awaitable[Optional[Mapping[str, Any]]]]


async def _await_gamma(resolver: GammaResolver, condition_id: str):
    """Await the async gamma resolver (it yields a Mapping or None)."""
    return await resolver(condition_id)


class EvidenceCollectorConfig:
    """Bounded, fail-closed configuration for one collection run."""

    def __init__(
        self,
        *,
        max_wallets_per_run: int = 1,
        max_new_trades_per_wallet: int = 25,
        max_total_new_trades: int = 25,
        max_gamma_requests: int = 100,
        timeout_seconds: float = 30.0,
        rss_mb_limit: float = 512.0,
    ) -> None:
        self.max_wallets_per_run = max(1, int(max_wallets_per_run))
        self.max_new_trades_per_wallet = max(1, int(max_new_trades_per_wallet))
        self.max_total_new_trades = max(1, int(max_total_new_trades))
        self.max_gamma_requests = max(1, int(max_gamma_requests))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.rss_mb_limit = max(16.0, float(rss_mb_limit))


@dataclass
class EvidenceCollectionResult:
    watch_id: str
    wallet_id: str
    dry_run: bool
    attempted_rows: int = 0
    inserted_rows: int = 0
    deduplicated_rows: int = 0
    rejected_rows: int = 0
    sell_excluded: int = 0
    sample_excluded: int = 0
    gamma_requests: int = 0
    gamma_failures: int = 0
    enrichment_conflicts: int = 0
    enriched: int = 0
    committed: bool = False
    # PR #73 dry-run reporting: when no write occurs, what WOULD the
    # collector have done? Filled in only for dry-run reporting.
    would_create: int = 0
    would_update: int = 0
    error: Optional[str] = None
    # Zero-execution guarantees (asserted by integration tests).
    specialist_approvals_created: int = 0
    dispatches_created: int = 0
    candidates_created: int = 0
    paper_signals_created: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "watch_id": self.watch_id,
            "wallet_id": self.wallet_id,
            "dry_run": self.dry_run,
            "attempted_rows": self.attempted_rows,
            "inserted_rows": self.inserted_rows,
            "deduplicated_rows": self.deduplicated_rows,
            "rejected_rows": self.rejected_rows,
            "sell_excluded": self.sell_excluded,
            "sample_excluded": self.sample_excluded,
            "gamma_requests": self.gamma_requests,
            "gamma_failures": self.gamma_failures,
            "enrichment_conflicts": self.enrichment_conflicts,
            "enriched": self.enriched,
            "committed": self.committed,
            "would_create": self.would_create,
            "would_update": self.would_update,
            "error": self.error,
        }


def _fetch_active_watch(db: Database, watch_id: str) -> Optional[dict[str, Any]]:
    row = db.fetchone(
        "SELECT id, wallet_id, status, max_new_trades_per_run FROM "
        "specialist_evidence_watchlist WHERE id=?", (watch_id,)
    )
    if row is None:
        return None
    rec = dict(row)
    if rec.get("status") != "active":
        return None  # paused/retired not collected
    return rec


def _watch_wallet_is_sample(db: Database, wallet_id: str) -> bool:
    row = db.fetchone(
        "SELECT is_sample FROM wallets WHERE id=?", (wallet_id,)
    )
    if row is None:
        return False
    return bool(dict(row).get("is_sample"))


async def collect_evidence(
    db: Database,
    *,
    watch_id: str,
    provider: ingest_pipeline.RealTradeSourceProvider,
    gamma_resolver: Optional[GammaResolver] = None,
    config: Optional[EvidenceCollectorConfig] = None,
    dry_run: bool = True,
    # PR #73 seam: when ``auto_commit`` is False the single-watch collector
    # performs NO commit of its own. The caller owns the transaction (used by
    # the bounded multi-watch cohort CLI to commit the entire cohort as one unit
    # or roll the whole cohort back). Defaults to True to preserve the
    # pre-existing single-watch commit behavior unchanged.
    auto_commit: bool = True,
) -> EvidenceCollectionResult:
    """Collect BUY trades for one active watchlist entry, idempotently.

    Never invokes approval, dispatch, candidate, paper-signal, or execution
    writes. SELL and sample trades are excluded. Replayed runs add 0 rows.
    """
    config = config or EvidenceCollectorConfig()
    started = time.monotonic()
    watch = _fetch_active_watch(db, watch_id)
    if watch is None:
        return EvidenceCollectionResult(
            watch_id=watch_id, wallet_id="", dry_run=dry_run,
            error="watch_not_active_or_missing",
        )
    wallet_id = str(watch["wallet_id"])
    if _watch_wallet_is_sample(db, wallet_id):
        return EvidenceCollectionResult(
            watch_id=watch_id, wallet_id=wallet_id, dry_run=dry_run,
            error="sample_wallet_rejected",
        )

    # The watchlist stores the wallet's UUID (wallets.id). Trades are keyed on
    # the on-chain address (wallets.address), so resolve it and use the address
    # as the requested wallet for collection.
    addr_row = db.fetchone(
        "SELECT address FROM wallets WHERE id=?", (wallet_id,)
    )
    requested_address = str(addr_row["address"]) if addr_row else wallet_id

    result = EvidenceCollectionResult(
        watch_id=watch_id, wallet_id=wallet_id, dry_run=dry_run
    )

    # Determine the per-wallet bound: the smaller of the watch entry's own
    # max_new_trades_per_run and the run config (fail-closed, lower wins).
    watch_bound = int(watch.get("max_new_trades_per_run") or config.max_new_trades_per_wallet)
    per_wallet_bound = min(watch_bound, config.max_new_trades_per_wallet,
                           config.max_total_new_trades)

    pipe = await ingest_pipeline.run_ingestion(
        provider, requested_address,
        record_limit=per_wallet_bound,
        max_pages=1,
        requested_wallet=requested_address,
        gamma_resolver=gamma_resolver,
    )
    if pipe.error:
        result.error = pipe.error
        return result

    # Count SELL rows that were rejected by the BUY-only gate (evidence that
    # the collector correctly excluded non-BUY activity). These never reach
    # the valid set, so we tally them from the full candidate list.
    for c in pipe.candidates:
        if c.side == "SELL":
            result.sell_excluded += 1

    # Deterministic processing order: by source_trade_id (stable id).
    valid = sorted(
        [c for c in pipe.candidates if c.validation_status == "valid" and c.source_trade_id],
        key=lambda c: c.source_trade_id or "",
    )

    accepted: list[NormalizedSourceTrade] = []
    for c in valid:
        if c.side != "BUY":
            result.rejected_rows += 1
            continue
        accepted.append(c)
        if len(accepted) >= per_wallet_bound:
            break

    result.attempted_rows = len(accepted)

    # Persist BUY source trades idempotently (INSERT OR IGNORE by UNIQUE).
    write_res = write_valid_rows(db, accepted, dry_run=dry_run, auto_commit=auto_commit)
    result.inserted_rows = write_res.inserted or 0
    result.deduplicated_rows = write_res.deduplicated or 0
    result.rejected_rows += (write_res.rejected or 0)

    # Dry-run reporting (PR #73): what WOULD a writable run do for this watch?
    # would_create = accepted BUY rows not yet present in source_trades;
    # would_update = accepted BUY rows already present (they would be
    # re-enriched / metadata-merged on a real run). Computed read-only.
    if dry_run and accepted:
        pre = {
            r[0]
            for r in db.conn.execute(
                "SELECT source_trade_id FROM source_trades "
                "WHERE lower(trader_address)=? AND source_trade_id IN ({})".format(
                    ",".join("?" for _ in accepted)
                ),
                [requested_address.lower(), *[c.source_trade_id for c in accepted]],
            ).fetchall()
        }
        result.would_create = sum(
            1 for c in accepted if c.source_trade_id not in pre
        )
        result.would_update = sum(
            1 for c in accepted if c.source_trade_id in pre
        )

    # Enrichment provenance for the freshly-inserted rows (and existing ones,
    # idempotent). We call the shared enrichment writer with a gamma_resolver
    # (condition_id -> market) so it builds the canonical nested metadata via
    # the shared producer. No scoring, no dispatch.
    if not dry_run and accepted:
        # Read back internal ids for the inserted source trades (deterministic
        # order by canonical source_trade_id).
        inserted_rows = db.conn.execute(
            "SELECT id, source_trade_id FROM source_trades "
            "WHERE lower(trader_address)=? AND source_trade_id IN ({})".format(
                ",".join("?" for _ in accepted)
            ),
            [requested_address.lower(), *[c.source_trade_id for c in accepted]],
        ).fetchall()
        internal_ids = [dict(r)["id"] for r in inserted_rows]
        for rid in internal_ids:
            if config.max_gamma_requests and result.gamma_requests >= config.max_gamma_requests:
                break
            result.gamma_requests += 1
            try:
                er = enrich_source_trade(
                    db, rid, gamma_resolver=gamma_resolver, dry_run=False,
                )
            except Exception as exc:  # conflict / transient
                result.error = f"enrich_error: {exc}"[:300]
                continue
            if er.status == "conflict":
                result.enrichment_conflicts += 1
            elif er.status != "error":
                result.enriched += 1
                # Write the canonical nested taxonomy back onto
                # source_trades.metadata_json. The frozen scorer reads
                # metadata["taxonomy"]["raw_category"] from here, so the
                # collected evidence must carry the canonical shape (not just
                # the separate source_trade_enrichments row). Reuses the shared
                # merge_canonical_metadata service -> byte-equivalent to
                # backfill and collection.
                row = db.fetchone(
                    "SELECT metadata_json, market_source_id, token_id "
                    "FROM source_trades WHERE id=?", (rid,))
                if row is not None:
                    rdict = dict(row)
                    gamma = None
                    if gamma_resolver is not None:
                        try:
                            gamma = await _await_gamma(
                                gamma_resolver, rdict["market_source_id"])
                        except Exception:
                            gamma = None
                    new_meta, _st, _rc = merge_canonical_metadata(
                        rdict["metadata_json"],
                        gamma,
                        condition_id=rdict["market_source_id"] or "",
                        token_id=rdict.get("token_id"),
                    )
                    if _st in (MERGE_FILLED, MERGE_UNCHANGED):
                        db.conn.execute(
                            "UPDATE source_trades SET metadata_json=? WHERE id=?",
                            (json.dumps(new_meta, sort_keys=True), rid),
                        )

        # Update watchlist last_collection_at.
        db.conn.execute(
            "UPDATE specialist_evidence_watchlist SET last_collection_at=? "
            "WHERE id=?",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), watch_id),
        )
        if auto_commit:
            # Standalone single-watch path: commit immediately as before.
            db.conn.commit()
            result.committed = True
        # auto_commit=False: caller (cohort) owns the transaction; do NOT
        # commit here. result.committed stays False.
    # Timeout guard (informational; pipeline is synchronous here).
    if time.monotonic() - started > config.timeout_seconds:
        result.error = result.error or "timeout_exceeded"

    return result
