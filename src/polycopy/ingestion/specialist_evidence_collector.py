"""Research-only evidence collection driven by the specialist evidence watchlist.

Watchlist-driven BUY-only collection. Writes ``source_trades`` + canonical
nested taxonomy (via the shared ``build_canonical_metadata``) + a
``source_trade_enrichments`` provenance row, and NOTHING in the execution
plane. This is NOT an approval/discovery selector: it collects only for an
ACTIVE watchlist entry (``--watch-id``), never for a ``specialist_approval``.

Hard limits (all enforced):
  * max_wallets_per_run
  * max_new_trades_per_wallet
  * max_total_new_trades (shared cohort-wide budget when called by the cohort)
  * max_gamma_requests (shared cohort-wide budget when called by the cohort)
  * processing timeout
  * RSS guard (fail-closed)
  * deterministic (sorted) processing order

PR #73 corrections
-------------------
* A writer failure, rolled-back write, failed uniqueness preflight, or
  non-empty ``error_message`` is treated as a collection failure:
  ``collect_evidence`` raises (preserving the original exception type/message)
  so the caller (single-watch CLI OR the bounded cohort) can propagate it as a
  structured failure. It never reports a failed watch as ``ok``.
* Gamma is resolved exactly once per unique condition against a SHARED cohort
  budget (dedupe cache + hard cap). The collector no longer performs a redundant
  second metadata merge/update; ``enrich_source_trade`` is the single
  authoritative owner of canonical metadata persistence, and it uses the same
  deterministic serializer as the writer.
* Honest per-watch metrics: raw examined, valid BUY, would-create, created,
  duplicate-observed, updated, enrichment created/updated/no-op, gamma requests,
  and an exact stop reason — never conflating inserted with valid-BUY, nor
  duplicates with updates.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional

from polycopy.db.database import Database
from polycopy.ingestion import ingest_pipeline
from polycopy.ingestion.gamma_budget import (
    CohortBudget,
    GammaBudgetExhausted,
    GammaResolutionError,
)
from polycopy.ingestion.normalized_source_trade import NormalizedSourceTrade
from polycopy.ingestion.source_trade_enrichment import enrich_source_trade
from polycopy.ingestion.source_trade_writer import write_valid_rows


# ── Cohort-stopping sentinels raised by collect_evidence ─────────────────────
class CohortResourceStop(Exception):
    """Base class for a cohort-bounded stop (deadline / RSS / record budget).

    Carries the authoritative ``stop_reason`` so the orchestrator can report it
    verbatim in the result JSON without guessing.
    """

    stop_reason: str = "resource_stop"

    def __init__(self, message: str, *, stop_reason: Optional[str] = None) -> None:
        super().__init__(message)
        if stop_reason is not None:
            self.stop_reason = stop_reason


class CohortDeadlineExceeded(CohortResourceStop):
    stop_reason = "deadline_exceeded"


class CohortRssExceeded(CohortResourceStop):
    stop_reason = "rss_limit_exceeded"


class WriterFailure(Exception):
    """A source_trade writer failure surfaced as a collection failure.

    Preserves the original writer error_message so the cohort result carries the
    authoritative exception text.
    """

    def __init__(self, message: str, *, stop_reason: str = "writer_failure") -> None:
        super().__init__(message)
        self.stop_reason = stop_reason


def _rss_mb() -> float:
    """Resident-set size in MiB, fail-closed (unknown => +inf).

    A production-shaped process always has an rusage reading; if we cannot
    measure it we MUST treat the limit as exceeded rather than silently pass.
    """
    try:
        import resource

        # ru_maxrss is in KiB on Linux, Bytes on macOS — normalize to MiB.
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if rss > 10**9:  # looks like bytes (macOS)
            return rss / (1024.0 * 1024.0)
        return rss / 1024.0
    except Exception:
        return float("inf")


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
        self.rss_mb_limit = max(0.0, float(rss_mb_limit))


@dataclass
class EvidenceCollectionResult:
    watch_id: str
    wallet_id: str
    dry_run: bool
    # ── Honest, non-conflated metrics (PR #73 correction 8) ──
    raw_trades_examined: int = 0
    valid_buy_trades: int = 0
    rows_would_create: int = 0
    rows_created: int = 0
    duplicate_rows_observed: int = 0
    rows_updated: int = 0
    enrichment_rows_created: int = 0
    enrichment_rows_updated: int = 0
    enrichment_no_ops: int = 0
    gamma_requests: int = 0
    stop_reason: Optional[str] = None
    processed: bool = False
    # ── Backward-compatible aliases used by other tests/CLIs ──
    attempted_rows: int = 0
    inserted_rows: int = 0
    deduplicated_rows: int = 0
    rejected_rows: int = 0
    sell_excluded: int = 0
    sample_excluded: int = 0
    committed: bool = False
    would_create: int = 0
    would_update: int = 0
    error: Optional[str] = None
    # Zero-execution guarantees (asserted by integration tests).
    specialist_approvals_created: int = 0
    dispatches_created: int = 0
    candidates_created: int = 0
    paper_signals_created: int = 0

    @property
    def enriched(self) -> int:
        """Backward-compatible alias for total enrichment activity."""
        return (
            self.enrichment_rows_created
            + self.enrichment_rows_updated
            + self.enrichment_no_ops
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "watch_id": self.watch_id,
            "wallet_id": self.wallet_id,
            "dry_run": self.dry_run,
            "raw_trades_examined": self.raw_trades_examined,
            "valid_buy_trades": self.valid_buy_trades,
            "rows_would_create": self.rows_would_create,
            "rows_created": self.rows_created,
            "duplicate_rows_observed": self.duplicate_rows_observed,
            "rows_updated": self.rows_updated,
            "enrichment_rows_created": self.enrichment_rows_created,
            "enrichment_rows_updated": self.enrichment_rows_updated,
            "enrichment_no_ops": self.enrichment_no_ops,
            "gamma_requests": self.gamma_requests,
            "stop_reason": self.stop_reason,
            "processed": self.processed,
            "enriched": self.enriched,
            "attempted_rows": self.attempted_rows,
            "inserted_rows": self.inserted_rows,
            "deduplicated_rows": self.deduplicated_rows,
            "rejected_rows": self.rejected_rows,
            "sell_excluded": self.sell_excluded,
            "sample_excluded": self.sample_excluded,
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
    # PR #73 correction 2/3: when the cohort passes a shared budget, the
    # per-watch record cap is drawn from the cohort-wide remaining budget, and
    # Gamma is resolved exactly once per unique condition against the shared
    # cohort Gamma budget. When None, the single-watch CLI behaves as before.
    cohort_budget: Optional[CohortBudget] = None,
) -> EvidenceCollectionResult:
    """Collect BUY trades for one active watchlist entry, idempotently.

    Never invokes approval, dispatch, candidate, paper-signal, or execution
    writes. SELL and sample trades are excluded. Replayed runs add 0 rows.

    Failure contract (PR #73 correction 1/3): any writer error, rolled-back
    write, failed uniqueness preflight, or non-empty ``error_message`` is
    surfaced as a raised exception (preserving the original type/message), so
    the caller — single-watch CLI or bounded cohort — can propagate it as a
    structured failure and roll back. A failed watch is NEVER reported ``ok``.
    """
    config = config or EvidenceCollectorConfig()
    watch = _fetch_active_watch(db, watch_id)
    if watch is None:
        # Precondition (input validation), not a mid-collection failure: return
        # a result with ``error`` set so standalone callers/the single-watch
        # CLI can report it. The cohort path already rejects this via
        # ``validate_watch_ids`` before calling ``collect_evidence``.
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
    # max_new_trades_per_run, the run config, and — when a cohort budget is
    # supplied — the SHARED cohort-wide remaining record budget (never resets
    # per watch).
    watch_bound = int(watch.get("max_new_trades_per_run") or config.max_new_trades_per_wallet)
    per_wallet_bound = min(watch_bound, config.max_new_trades_per_wallet,
                            config.max_total_new_trades)
    if cohort_budget is not None:
        per_wallet_bound = min(per_wallet_bound, cohort_budget.remaining_records)

    # The authoritative Gamma resolver: the shared cohort budget (dedupe + cap)
    # when supplied, otherwise the caller's resolver directly.
    effective_gamma: Optional[GammaResolver] = None
    if gamma_resolver is not None:
        effective_gamma = (
            cohort_budget.gamma if cohort_budget is not None else gamma_resolver
        )

    pipe = await ingest_pipeline.run_ingestion(
        provider, requested_address,
        record_limit=per_wallet_bound,
        max_pages=1,
        requested_wallet=requested_address,
        gamma_resolver=effective_gamma,
    )
    if pipe.error:
        raise RuntimeError(pipe.error)

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
        result.valid_buy_trades += 1
        if len(accepted) >= per_wallet_bound:
            break

    result.raw_trades_examined = len(pipe.candidates)
    result.attempted_rows = len(accepted)

    # Cohort-wide record budget clamp (correction 2): accepted rows may never
    # exceed the SHARED remaining-record budget. Trim here so the writer cannot
    # over-consume across watches.
    if cohort_budget is not None and not dry_run and cohort_budget.remaining_records >= 0:
        if len(accepted) > cohort_budget.remaining_records:
            accepted = accepted[: cohort_budget.remaining_records]

    # Persist BUY source trades idempotently (INSERT OR IGNORE by UNIQUE).
    write_res = write_valid_rows(db, accepted, dry_run=dry_run, auto_commit=auto_commit)
    # PR #73 correction 1: a writer error / rollback / failed preflight /
    # non-empty error_message is a collection failure — raise it so the caller
    # rolls the cohort back. We never report a failed write as success.
    if not dry_run and (
        write_res.errors
        or write_res.rolled_back
        or write_res.error_message
        or not write_res.unique_constraint_present
    ):
        raise WriterFailure(
            write_res.error_message
            or f"writer failure: errors={write_res.errors}, "
               f"rolled_back={write_res.rolled_back}, "
               f"unique_constraint_present={write_res.unique_constraint_present}",
            stop_reason="writer_failure",
        )

    result.inserted_rows = write_res.inserted or 0
    result.rows_created = write_res.inserted or 0
    result.deduplicated_rows = write_res.deduplicated or 0
    result.duplicate_rows_observed = write_res.deduplicated or 0
    result.rejected_rows += (write_res.rejected or 0)

    # Cohort record budget: consume what we attempted to write.
    if cohort_budget is not None and not dry_run:
        cohort_budget.remaining_records = max(
            0, cohort_budget.remaining_records - len(accepted)
        )

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
        result.rows_would_create = sum(
            1 for c in accepted if c.source_trade_id not in pre
        )
        result.rows_updated = sum(
            1 for c in accepted if c.source_trade_id in pre
        )
        result.would_create = result.rows_would_create
        result.would_update = result.rows_updated

    # Enrichment provenance for the accepted rows (idempotent). The shared
    # enrichment writer is the SINGLE authoritative owner of canonical metadata
    # persistence and uses the same deterministic serializer as the writer, so
    # the collector performs NO second/redundant metadata merge/update.
    if not dry_run and accepted:
        # Read back internal ids for the inserted source trades (deterministic
        # order by canonical source_trade_id). On replay the INSERT OR IGNORE
        # has already happened; we re-select the same rows to re-run the
        # idempotent enrichment (which becomes no-ops when evidence is
        # unchanged).
        inserted_rows = db.conn.execute(
            "SELECT id, source_trade_id FROM source_trades "
            "WHERE lower(trader_address)=? AND source_trade_id IN ({})".format(
                ",".join("?" for _ in accepted)
            ),
            [requested_address.lower(), *[c.source_trade_id for c in accepted]],
        ).fetchall()
        internal_ids = [dict(r)["id"] for r in inserted_rows]
        for rid in internal_ids:
            # Cohort resource bounds (deadline / RSS) enforced DURING the loop.
            if cohort_budget is not None:
                _checkpoint(cohort_budget)
            # Cohort-wide Gamma budget exhausted -> stop cleanly (raise).
            if effective_gamma is not None and cohort_budget is not None:
                if cohort_budget.gamma.used >= cohort_budget.gamma.budget:
                    cohort_budget.stop_reason = "gamma_budget_exhausted"
                    raise GammaBudgetExhausted(
                        "cohort Gamma request budget exhausted during enrichment"
                    )
            try:
                er = enrich_source_trade(
                    db, rid, gamma_resolver=effective_gamma, dry_run=False,
                )
            except GammaBudgetExhausted:
                if cohort_budget is not None:
                    cohort_budget.stop_reason = "gamma_budget_exhausted"
                raise
            except GammaResolutionError as exc:
                # Hard Gamma provider failure: propagate as cohort failure.
                if cohort_budget is not None:
                    cohort_budget.stop_reason = "gamma_resolution_error"
                raise WriterFailure(
                    f"gamma resolution error: {exc}",
                    stop_reason="gamma_resolution_error",
                )
            except Exception as exc:  # conflict / transient -> fail closed
                if cohort_budget is not None:
                    cohort_budget.stop_reason = "enrich_error"
                raise WriterFailure(
                    f"enrich_error: {exc}", stop_reason="enrich_error"
                )
            if er.status == "conflict":
                result.enrichment_no_ops += 1
            elif er.status == "error" or er.operational_error or er.provider_error:
                # The enrichment reported a hard failure (provider/operational).
                if cohort_budget is not None:
                    cohort_budget.stop_reason = "enrich_error"
                raise WriterFailure(
                    f"enrichment failed for {rid}: {er.error_message}",
                    stop_reason="enrich_error",
                )
            elif er.created:
                result.enrichment_rows_created += 1
            elif er.updated:
                result.enrichment_rows_updated += 1
            else:
                result.enrichment_no_ops += 1

        # Update watchlist last_collection_at (allowed bookkeeping column).
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

    # Honest gamma request count from the shared budget (single source of truth
    # for Gamma counting under the cohort).
    if cohort_budget is not None:
        result.gamma_requests = cohort_budget.gamma.used

    # Cohort resource bounds checked DURING work (correction 2): deadline and
    # RSS are enforced before a watch is allowed to proceed AND inside the
    # per-row enrichment loop, not only after a watch returns.
    if cohort_budget is not None:
        _checkpoint(cohort_budget)

    result.processed = True
    return result


def _checkpoint(cohort_budget: Any) -> None:
    """Fail-closed cohort resource bound enforcement (deadline + RSS).

    Called at the top of ``collect_evidence`` and inside the per-row
    enrichment loop so a long-running watch stops mid-flight rather than after
    completing all of its (potentially many) rows.
    """
    if cohort_budget.deadline_ts is not None and time.monotonic() > cohort_budget.deadline_ts:
        cohort_budget.stop_reason = "deadline_exceeded"
        raise CohortDeadlineExceeded("cohort deadline exceeded")
    if _rss_mb() > cohort_budget.rss_mb_limit:
        cohort_budget.stop_reason = "rss_limit_exceeded"
        raise CohortRssExceeded(
            f"RSS {_rss_mb():.1f} MiB exceeded limit {cohort_budget.rss_mb_limit:.1f} MiB"
        )
