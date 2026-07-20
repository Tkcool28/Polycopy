"""PR #73 — Bounded multi-watch evidence collection orchestration.

Orchestrates the ACCEPTED PR #71 single-watch evidence collector
(``polycopy.ingestion.specialist_evidence_collector.collect_evidence``) for an
explicit cohort of up to five ACTIVE watch IDs in ONE operator invocation.

This module does NOT reimplement evidence collection. It:
  * validates every supplied watch ID BEFORE any network/provider/DB activity;
  * acquires the global operational lock ONCE for the whole cohort;
  * constructs the provider only AFTER the lock is held;
  * calls the accepted underlying collector once per watch, deterministically,
    in caller-owned transaction mode (``auto_commit=False``);
  * commits or rolls back the ENTIRE cohort as one SQLite transaction.

Hard bounds:
  * exactly 1..5 unique watch IDs;
  * no wallet-address, no discovery, no implicit "all active" expansion;
  * no automatic retries, no scheduler, no timer;
  * uses the accepted PR #71 per-watch bounds without raising them.

The only tables ever written are those already written by the accepted PR #71
collector: ``source_trades``, ``source_trade_enrichments``, and the
``specialist_evidence_watchlist.last_collection_at`` bookkeeping column. No
approval / dispatch / candidate / paper-signal / execution-plane table is ever
touched.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from polycopy.db.database import Database
from polycopy.ingestion import specialist_evidence_collector as _collector
from polycopy.ingestion.specialist_evidence_collector import (
    EvidenceCollectorConfig,
    EvidenceCollectionResult,
)
from polycopy.ingestion.specialist_evidence_watchlist import (
    _wallet_is_sample as _watch_wallet_is_sample,
)
from polycopy.runtime.locks import FileLock, LockError, operational_job_lock

# ── Hard cohort bounds ──────────────────────────────────────────────────────
MAX_WATCH_IDS = 5
MIN_WATCH_IDS = 1

# Watch id shape accepted by the research watchlist (``wl_<hex>``).
_WATCH_ID_RE = re.compile(r"^wl_[0-9a-fA-F]{8,}$")

# Tables legitimately written by the accepted PR #71 collector. Any SQL write
# against a table outside this set is a contract violation (tests assert it).
ALLOWED_WRITE_TABLES = (
    "source_trades",
    "source_trade_enrichments",
    "specialist_evidence_watchlist",
)

# Execution / approval plane tables that MUST NEVER change under this CLI.
FORBIDDEN_WRITE_TABLES = (
    "wallets",
    "specialist_approvals",
    "approved_specialist_trade_dispatches",
    "copy_candidates",
    "candidate_price_snapshots",
    "paper_signal_decisions",
    "paper_signal_execution_authorizations",
    "execution_risk_decisions",
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "paper_position_lots",
    "paper_position_marks",
    "paper_position_settlements",
)

GammaResolver = Callable[[str], Awaitable[Optional[Mapping[str, Any]]]]


class CohortValidationError(ValueError):
    """Raised when the explicit watch-id set fails a pre-flight rule.

    Carries the exact ``rejected_watch_ids`` so the consolidated result can
    report them individually.
    """

    def __init__(self, message: str, *, rejected_watch_ids: Optional[list[str]] = None):
        super().__init__(message)
        self.rejected_watch_ids = list(rejected_watch_ids or [])


@dataclass
class CohortWatchResult:
    """Per-watch slice of the consolidated cohort result."""

    watch_id: str
    wallet_id: str = ""
    address: str = ""
    status: str = "skipped"
    reason_codes: list[str] = field(default_factory=list)
    trades_examined: int = 0
    valid_buy_trades: int = 0
    created: int = 0
    updated: int = 0
    would_create: int = 0
    would_update: int = 0
    fetch_complete: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "watch_id": self.watch_id,
            "wallet_id": self.wallet_id,
            "address": self.address,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "trades_examined": self.trades_examined,
            "valid_buy_trades": self.valid_buy_trades,
            "created": self.created,
            "updated": self.updated,
            "would_create": self.would_create,
            "would_update": self.would_update,
            "fetch_complete": self.fetch_complete,
        }


@dataclass
class CohortResult:
    """One consolidated, authoritative result for the whole cohort run."""

    status: str  # success | partial | failed
    dry_run: bool
    run_id: str
    watch_count_requested: int
    watch_count_completed: int = 0
    watch_count_failed: int = 0
    cohort_committed: bool = False
    reason_codes: list[str] = field(default_factory=list)
    error: Optional[str] = None
    totals: dict[str, int] = field(default_factory=dict)
    watches: list[CohortWatchResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "dry_run": self.dry_run,
            "run_id": self.run_id,
            "watch_count_requested": self.watch_count_requested,
            "watch_count_completed": self.watch_count_completed,
            "watch_count_failed": self.watch_count_failed,
            "cohort_committed": self.cohort_committed,
            "reason_codes": list(self.reason_codes),
            "error": self.error,
            "totals": dict(self.totals),
            "watches": [w.as_dict() for w in self.watches],
        }


def _redact_address(address: str) -> str:
    """Redact a canonical address to 0x1234…abcd form."""
    a = (address or "").strip()
    if len(a) >= 12:
        return f"{a[:6]}…{a[-4:]}"
    return a or "—"


# ── Validation (no network, no provider, no DB write) ──────────────────────
def validate_watch_ids(
    db: Database,
    watch_ids: Sequence[str],
    *,
    reject_inactive: bool = True,
) -> list[str]:
    """Validate and normalize the explicit cohort BEFORE any network/DB mutation.

    Rules enforced (fail-closed):
      * count 1..5;
      * each id well-formed (``wl_<hex>``);
      * no duplicates (deterministic dedupe, reported);
      * no two ids may map to the SAME wallet (duplicate wallet membership);
      * each watch must be ACTIVE (when ``reject_inactive``);
      * the watch's wallet must exist and must NOT be a sample wallet;
      * no hidden expansion beyond the supplied ids.

    Raises ``CohortValidationError`` with ``rejected_watch_ids`` on the first
    failing rule. Returns the deterministic, deduplicated, sorted order used for
    the run.
    """
    ids = list(watch_ids or [])

    if not (MIN_WATCH_IDS <= len(ids) <= MAX_WATCH_IDS):
        raise CohortValidationError(
            f"watch id count must be between {MIN_WATCH_IDS} and "
            f"{MAX_WATCH_IDS}, got {len(ids)}",
            rejected_watch_ids=list(ids),
        )

    # Malformed ids rejected outright.
    malformed = [i for i in ids if not _WATCH_ID_RE.match(i or "")]
    if malformed:
        raise CohortValidationError(
            f"malformed watch id(s): {malformed}", rejected_watch_ids=malformed
        )

    # Deterministic dedupe (report, do not silently drop).
    seen: dict[str, int] = {}
    deduped: list[str] = []
    for i in ids:
        if i in seen:
            seen[i] += 1
            continue
        seen[i] = 1
        deduped.append(i)
    duplicates = [i for i, c in seen.items() if c > 1]
    if duplicates:
        # Preserve the supplied cohort but record the duplicate as rejected.
        raise CohortValidationError(
            f"duplicate watch id(s) supplied: {duplicates} "
            f"(deduplicated order: {deduped})",
            rejected_watch_ids=duplicates,
        )

    # Validate each watch against the DB (read-only selects only).
    wallet_by_watch: dict[str, str] = {}
    for wid in deduped:
        row = db.fetchone(
            "SELECT id, wallet_id, status FROM specialist_evidence_watchlist "
            "WHERE id=?",
            (wid,),
        )
        if row is None:
            raise CohortValidationError(
                f"watch id not found: {wid}", rejected_watch_ids=[wid]
            )
        rec = dict(row)
        if reject_inactive and rec.get("status") != "active":
            raise CohortValidationError(
                f"watch {wid} is not active (status={rec.get('status')!r})",
                rejected_watch_ids=[wid],
            )
        wallet_id = str(rec["wallet_id"])
        wallet_by_watch[wid] = wallet_id

        # Wallet must exist.
        wrow = db.fetchone("SELECT id FROM wallets WHERE id=?", (wallet_id,))
        if wrow is None:
            raise CohortValidationError(
                f"watch {wid} references a missing wallet {wallet_id}",
                rejected_watch_ids=[wid],
            )
        # Sample wallets rejected.
        if _watch_wallet_is_sample(db, wallet_id):
            raise CohortValidationError(
                f"watch {wid} references a sample wallet {wallet_id}",
                rejected_watch_ids=[wid],
            )

    # Duplicate wallet membership behind different watch ids rejected.
    by_wallet: dict[str, list[str]] = {}
    for wid, wal in wallet_by_watch.items():
        by_wallet.setdefault(wal, []).append(wid)
    dup_wallets = {w: ws for w, ws in by_wallet.items() if len(ws) > 1}
    if dup_wallets:
        flat = [w for ws in dup_wallets.values() for w in ws]
        raise CohortValidationError(
            f"duplicate wallet behind multiple watch ids: {dup_wallets}",
            rejected_watch_ids=flat,
        )

    # Deterministic processing order: sorted by watch id (stable id).
    return sorted(deduped)


# ── Provider (constructed only AFTER the lock is held) ──────────────────────
class _CliProvider:
    """Thin wrapper over ``PolymarketPublicAdapter.get_trades_by_address``.

    Mirrors the PR #71 single-watch CLI provider contract
    (``RealTradeSourceProvider``). ``made_network_call`` signals the ingestion
    pipeline to count real HTTP activity.
    """

    made_network_call = True

    def __init__(self, adapter, *, timeout: float) -> None:
        self._adapter = adapter
        self._timeout = timeout

    async def fetch_trades(self, wallet: str, *, limit: int, page: int):
        from datetime import datetime

        return await self._adapter.get_trades_by_address(
            wallet,
            since=datetime.min,
            limit=limit,
            offset=page * limit,
            return_raw=True,
        )


@dataclass
class CohortRunConfig:
    """Bounded, fail-closed configuration shared by EVERY watch."""

    max_new_trades_per_wallet: int = 25
    max_total_new_trades: int = 25
    max_gamma_requests: int = 100
    timeout_seconds: float = 30.0
    rss_mb_limit: float = 512.0
    resolve_gamma: bool = False


def build_run_config(args: Any) -> CohortRunConfig:
    """Map CLI args -> one bounded config shared by the whole cohort."""
    return CohortRunConfig(
        max_new_trades_per_wallet=getattr(args, "max_new_trades_per_wallet", 25),
        max_total_new_trades=getattr(args, "max_total_new_trades", 25),
        max_gamma_requests=getattr(args, "max_gamma_requests", 100),
        timeout_seconds=getattr(args, "timeout_seconds", 30.0),
        rss_mb_limit=getattr(args, "rss_mb_limit", 512.0),
        resolve_gamma=getattr(args, "resolve_gamma", False),
    )


# ── Orchestration ────────────────────────────────────────────────────────────
async def run_cohort(
    db: Database,
    *,
    watch_ids: Sequence[str],
    adapter,
    dry_run: bool,
    config: CohortRunConfig,
    gamma_resolver: Optional[GammaResolver] = None,
    lock_timeout: float = 30.0,
    lock_path: Optional[Any] = None,
) -> CohortResult:
    """Collect evidence for an explicit cohort as ONE bounded, atomic operator run.

    Contract:
      * validate all watch ids BEFORE any provider/network/DB-mutating work;
      * acquire the global operational lock ONCE (provider built only after);
      * call the accepted single-watch collector per watch, deterministically,
        in caller-owned transaction mode (``auto_commit=False``);
      * commit the ENTIRE cohort as one transaction, or roll the whole cohort
        back on the first unhandled watch failure.

    On any unhandled cohort-level error the result carries ``status='failed'``,
    ``cohort_committed=False``, and the ORIGINAL exception text.
    """
    run_id = f"cohort_{uuid.uuid4().hex}"
    result = CohortResult(
        status="partial",
        dry_run=dry_run,
        run_id=run_id,
        watch_count_requested=len(watch_ids),
    )

    # Normalize / validate up-front (no lock, no provider, no network, no write).
    # A validation failure is a clean FAILURE: zero provider/network/DB-mutating
    # activity has occurred yet, and the cohort is not committed.
    try:
        ordered = validate_watch_ids(db, watch_ids)
    except CohortValidationError as exc:
        result.status = "failed"
        result.cohort_committed = False
        result.reason_codes.append("validation_error")
        result.error = str(exc)
        # Report each rejected watch as a failed slice (deterministic order).
        for wid in exc.rejected_watch_ids:
            result.watches.append(
                CohortWatchResult(
                    watch_id=wid,
                    status="rejected",
                    reason_codes=["validation_error"],
                )
            )
        result.watch_count_failed = len(exc.rejected_watch_ids)
        return result

    # Build the shared collector config (same bounds for every watch).
    ev_cfg = EvidenceCollectorConfig(
        max_wallets_per_run=1,
        max_new_trades_per_wallet=config.max_new_trades_per_wallet,
        max_total_new_trades=config.max_total_new_trades,
        max_gamma_requests=config.max_gamma_requests,
        timeout_seconds=config.timeout_seconds,
        rss_mb_limit=config.rss_mb_limit,
    )

    # Acquire the global operational lock ONCE for the entire cohort. The
    # provider is constructed only after the lock is held (testable via the
    # injected adapter factory). The orchestrator owns the adapter lifecycle
    # (built once, closed exactly once).
    try:
        with operational_job_lock("collect", timeout=lock_timeout, lock_path=lock_path):
            # Normalize: an injected CLI "spec" exposes build()/close(); a
            # plain adapter is used directly. Build AFTER the lock is held.
            real_adapter = adapter.build() if callable(getattr(adapter, "build", None)) else adapter
            provider = _CliProvider(real_adapter, timeout=config.timeout_seconds)

            # Default gamma resolver (real network; used only when --resolve-gamma).
            async def _gamma_resolver(condition_id):
                from polycopy.adapters.polymarket import PolymarketPublicAdapter

                return await real_adapter.get_market_raw(condition_id)

            resolved_gamma = _gamma_resolver if config.resolve_gamma else gamma_resolver

            completed = 0
            failed = 0
            try:
                if not dry_run:
                    # One bounded caller-owned transaction for the WHOLE cohort.
                    # We rely on Python's default sqlite3 transaction handling
                    # (isolation_level=""): the connection auto-begins an
                    # implicit transaction on the first DML and keeps it open
                    # until the cohort explicitly commits or rolls back. Every
                    # staged write (source_trades INSERTs, enrichment metadata
                    # UPDATEs, watchlist last_collection_at UPDATEs, provenance
                    # SAVEPOINTs) is part of this single implicit transaction.
                    # A manual BEGIN + isolation_level=None proved unsafe
                    # (a read-PRAGMA in the accepted write path forced a
                    # premature COMMIT); instead we simply never commit
                    # internally and let the cohort own the single final
                    # commit()/rollback().
                    pass
                for wid in ordered:
                    wres = CohortWatchResult(watch_id=wid)
                    try:
                        single = await _collector.collect_evidence(
                            db,
                            watch_id=wid,
                            provider=provider,
                            gamma_resolver=resolved_gamma,
                            config=ev_cfg,
                            dry_run=dry_run,
                            auto_commit=False,  # caller owns the transaction
                        )
                        _fold_single(db, wres, single)
                        if wres.status == "error":
                            raise RuntimeError(
                                f"watch {wid} failed: {wres.reason_codes}"
                            )
                        completed += 1
                        result.watches.append(wres)
                    except Exception as exc:  # cohort-level fail-closed
                        failed += 1
                        wres.status = "error"
                        wres.reason_codes.append(f"watch_error: {type(exc).__name__}")
                        result.watches.append(wres)
                        # Roll back the ENTIRE cohort (watches 1..n-1 included).
                        try:
                            db.conn.rollback()
                        except Exception:
                            pass
                        result.watch_count_completed = completed
                        result.watch_count_failed = failed
                        result.status = "failed"
                        result.cohort_committed = False
                        result.error = f"{type(exc).__name__}: {exc}"
                        return result

                # All watches processed without an unhandled failure.
                if not dry_run:
                    db.conn.commit()
                    result.cohort_committed = True
                else:
                    # Dry-run: nothing to commit; rows were never written.
                    result.cohort_committed = False
                result.watch_count_completed = completed
                result.watch_count_failed = failed
                result.status = "success"
            except Exception as exc:
                # Defensive: any leak outside the per-watch loop.
                try:
                    db.conn.rollback()
                except Exception:
                    pass
                result.watch_count_completed = completed
                result.watch_count_failed = failed
                result.status = "failed"
                result.cohort_committed = False
                result.error = f"{type(exc).__name__}: {exc}"
                return result
            finally:
                # Close the provider's underlying adapter exactly once.
                try:
                    close = getattr(real_adapter, "close", None)
                    if close is not None:
                        if asyncio.iscoroutinefunction(close):
                            asyncio.run(close())
                        else:
                            close()
                except Exception:
                    pass
    except LockError as exc:
        # Lock contention: ZERO provider/network/DB-mutating activity occurred.
        result.status = "failed"
        result.cohort_committed = False
        result.reason_codes.append("operational_lock_unavailable")
        result.error = f"LockError: {exc}"
        return result

    _compute_totals(result)
    return result


def _fold_single(db: Database, wres: CohortWatchResult, single: EvidenceCollectionResult) -> None:
    """Fold one accepted single-watch result into the per-watch cohort slice."""
    wres.wallet_id = single.wallet_id or ""
    if single.error:
        wres.status = "error"
        wres.reason_codes.append(single.error)
        return
    # Resolve redacted canonical address from the DB for reporting.
    if wres.wallet_id:
        row = None
        try:
            row = db.fetchone(
                "SELECT canonical_address, address FROM wallets WHERE id=?",
                (wres.wallet_id,),
            )
        except Exception:
            row = None
        if row is not None:
            wres.address = _redact_address(
                str(dict(row).get("canonical_address") or dict(row).get("address") or "")
            )
    wres.status = "ok"
    wres.trades_examined = single.attempted_rows
    wres.valid_buy_trades = single.inserted_rows
    wres.created = single.inserted_rows
    wres.updated = single.deduplicated_rows
    wres.would_create = single.would_create
    wres.would_update = single.would_update
    wres.fetch_complete = single.error is None


def _compute_totals(result: CohortResult) -> None:
    totals = {
        "trades_examined": 0,
        "valid_buy_trades": 0,
        "writes_created": 0,
        "writes_updated": 0,
        "duplicates_rejected": 0,
    }
    for w in result.watches:
        totals["trades_examined"] += w.trades_examined
        totals["valid_buy_trades"] += w.valid_buy_trades
        totals["writes_created"] += w.created
        totals["writes_updated"] += w.updated
        totals["duplicates_rejected"] += 0
    result.totals = totals


__all__ = [
    "MAX_WATCH_IDS",
    "MIN_WATCH_IDS",
    "ALLOWED_WRITE_TABLES",
    "FORBIDDEN_WRITE_TABLES",
    "CohortResult",
    "CohortWatchResult",
    "CohortRunConfig",
    "CohortValidationError",
    "validate_watch_ids",
    "build_run_config",
    "run_cohort",
]
