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
  * uses the accepted PR #71 per-watch bounds WITHOUT raising them, and enforces
    a TRUE cohort-wide maximum new-trade record budget and Gamma-request budget.

The only tables ever written are those already written by the accepted PR #71
collector: ``source_trades``, ``source_trade_enrichments``, and the
``specialist_evidence_watchlist.last_collection_at`` bookkeeping column. No
approval / dispatch / candidate / paper-signal / execution-plane table is ever
touched.

PR #73 required corrections
----------------------------
* **Writer/constraint failure -> full cohort rollback.** ``collect_evidence``
  raises on any writer error / rollback / failed uniqueness preflight / non-empty
  ``error_message``. ``run_cohort`` rolls back the WHOLE cohort, preserves the
  original exception type/message in ``CohortResult.error``, does NOT update
  ``last_collection_at`` for any watch, and stops processing later watches
  immediately.
* **Honest cohort-wide bounds.** ``max_total_new_trades`` is a true cohort-wide
  maximum (shared remaining-record budget); Gamma is one shared cohort request
  budget (deduped + capped); the deadline is enforced DURING work; the RSS
  ceiling is enforced fail-closed; every CLI numeric option is validated against
  fixed safe minimums/maximums before provider construction / network / writable
  DB open. The result JSON returns configured limits, actual consumption,
  remaining budget, and the exact stop reason.
* **Async adapter cleanup.** ``await adapter.aclose()`` is preferred; sync
  ``close()`` is a fallback; ``asyncio.run`` is never called from inside the
  active event loop. The adapter is closed EXACTLY ONCE on every exit path
  (success, watch failure, commit failure, timeout, cancellation, other
  structured failure).
* **Structured provider-build failure.** ``adapter.build()`` runs INSIDE the
  structured exception path; a construction failure returns a normal
  ``CohortResult(status="failed", cohort_committed=False)`` with the original
  error preserved, the lock released, and the DB closed. The CLI prints the
  normal JSON failure schema and exits nonzero.
* **Truthful metrics on every exit path.** Per-watch and cohort totals are
  computed on both success and every failure path, with separate fields for raw
  examined / valid BUY / would-create / created / duplicate-observed / updated /
  enrichment created-updated-noop / processed-failed-unprocessed watches / request
  consumption / budgets / rollback state / stop reason. No conflation, no
  hard-coded duplicate counts, no unprocessed reported as completed.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from polycopy.db.database import Database
from polycopy.ingestion import specialist_evidence_collector as _collector
from polycopy.ingestion.gamma_budget import (
    CohortBudget,
    GammaBudgetExhausted,
    GammaResolutionError,
    SharedGammaBudget,
)
from polycopy.ingestion.specialist_evidence_collector import (
    CohortDeadlineExceeded,
    CohortRssExceeded,
    EvidenceCollectorConfig,
    EvidenceCollectionResult,
    WriterFailure,
)
from polycopy.ingestion.normalized_source_trade import HARD_MAX_RECORD_LIMIT
from polycopy.ingestion.specialist_evidence_watchlist import (
    _wallet_is_sample as _watch_wallet_is_sample,
)
from polycopy.runtime.locks import LockError, operational_job_lock

# ── Hard cohort bounds ──────────────────────────────────────────────────────
MAX_WATCH_IDS = 5
MIN_WATCH_IDS = 1
HARD_MAX_TOTAL_NEW_TRADES = HARD_MAX_RECORD_LIMIT * MAX_WATCH_IDS

# Safe CLI numeric option bounds (validated BEFORE provider/network/DB-open).
_CLI_LIMITS = {
    "max_new_trades_per_wallet": (1, HARD_MAX_RECORD_LIMIT),
    "max_total_new_trades": (1, HARD_MAX_TOTAL_NEW_TRADES),
    "max_gamma_requests": (0, 1_000_000),
    "timeout_seconds": (1.0, 3_600.0),
    "rss_mb_limit": (16.0, 1_000_000.0),
}

# Persisted research-watch identities are intentionally compatible with both
# established generators: legacy/manual ``wl_<hex>`` and PR #72 discovery
# ``sew_<16 hex>``.  They remain opaque database identities after this narrow
# syntax preflight.
_WATCH_ID_RE = re.compile(r"^(?:wl_[0-9a-fA-F]{8,}|sew_[0-9a-fA-F]{16})$")

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
    status: str = "unprocessed"  # unprocessed | ok | error | rejected
    reason_codes: list[str] = field(default_factory=list)
    raw_trades_examined: int = 0
    valid_buy_trades: int = 0
    rows_would_create: int = 0
    rows_would_update: int = 0
    rows_created: int = 0
    duplicate_rows_observed: int = 0
    rows_updated: int = 0
    enrichment_rows_created: int = 0
    enrichment_rows_updated: int = 0
    enrichment_no_ops: int = 0
    gamma_requests: int = 0
    effective_new_trade_limit: int = 0
    stop_reason: Optional[str] = None

    @property
    def would_create(self) -> int:
        """Backward-compatible alias (authoritative: ``rows_would_create``)."""
        return self.rows_would_create

    @property
    def would_update(self) -> int:
        """Backward-compatible alias (authoritative: ``rows_updated``)."""
        return self.rows_updated

    @property
    def created(self) -> int:
        """Backward-compatible alias (authoritative: ``rows_created`` + enrich)."""
        return self.rows_created + self.enrichment_rows_created

    @property
    def updated(self) -> int:
        """Backward-compatible alias (authoritative: ``rows_updated`` + enrich)."""
        return self.rows_updated + self.enrichment_rows_updated

    def as_dict(self) -> dict[str, Any]:
        d = {
            "watch_id": self.watch_id,
            "wallet_id": self.wallet_id,
            "address": self.address,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "raw_trades_examined": self.raw_trades_examined,
            "valid_buy_trades": self.valid_buy_trades,
            "rows_would_create": self.rows_would_create,
            "rows_would_update": self.rows_would_update,
            "rows_created": self.rows_created,
            "duplicate_rows_observed": self.duplicate_rows_observed,
            "rows_updated": self.rows_updated,
            "enrichment_rows_created": self.enrichment_rows_created,
            "enrichment_rows_updated": self.enrichment_rows_updated,
            "enrichment_no_ops": self.enrichment_no_ops,
            "gamma_requests": self.gamma_requests,
            "effective_new_trade_limit": self.effective_new_trade_limit,
            "stop_reason": self.stop_reason,
        }
        # Backward-compatible aliases retained so existing callers/tests that
        # read the older names keep working; the rows_* fields above are the
        # authoritative, non-conflated metrics.
        d["created"] = self.rows_created + self.enrichment_rows_created
        d["updated"] = self.rows_updated + self.enrichment_rows_updated
        d["would_create"] = self.rows_would_create
        d["would_update"] = self.rows_would_update
        d["fetch_complete"] = self.status == "ok"
        return d


@dataclass
class CohortResult:
    """One consolidated, authoritative result for the whole cohort run."""

    status: str  # success | partial | failed
    dry_run: bool
    run_id: str
    watch_count_requested: int
    watch_count_completed: int = 0
    watch_count_failed: int = 0
    watch_count_rejected: int = 0
    watch_count_unprocessed: int = 0
    cohort_committed: bool = False
    rolled_back: bool = False
    reason_codes: list[str] = field(default_factory=list)
    error: Optional[str] = None
    stop_reason: Optional[str] = None
    totals: dict[str, int] = field(default_factory=dict)
    limits: dict[str, int] = field(default_factory=dict)
    consumption: dict[str, int] = field(default_factory=dict)
    remaining: dict[str, int] = field(default_factory=dict)
    watches: list[CohortWatchResult] = field(default_factory=list)
    # Private: the shared cohort budget, used by _compute_totals to read the
    # authoritative Gamma-request consumption (single dedupe cache + cap).
    _cohort_budget: Any = field(default=None, repr=False, compare=False)

    @property
    def watch_count_processed(self) -> int:
        """Authoritative processed count (alias of ``watch_count_completed``)."""
        return self.watch_count_completed

    @watch_count_processed.setter
    def watch_count_processed(self, value: int) -> None:
        self.watch_count_completed = value

    def as_dict(self) -> dict[str, Any]:
        d = {
            "status": self.status,
            "dry_run": self.dry_run,
            "run_id": self.run_id,
            "watch_count_requested": self.watch_count_requested,
            "watch_count_processed": self.watch_count_processed,
            "watch_count_failed": self.watch_count_failed,
            "watch_count_rejected": self.watch_count_rejected,
            "watch_count_unprocessed": self.watch_count_unprocessed,
            "cohort_committed": self.cohort_committed,
            "rolled_back": self.rolled_back,
            "reason_codes": list(self.reason_codes),
            "error": self.error,
            "stop_reason": self.stop_reason,
            "totals": dict(self.totals),
            "limits": dict(self.limits),
            "consumption": dict(self.consumption),
            "remaining": dict(self.remaining),
            "watches": [w.as_dict() for w in self.watches],
        }
        # Backward-compatible alias retained for existing callers/tests.
        d["watch_count_completed"] = self.watch_count_processed
        return d


def _redact_address(address: str) -> str:
    """Redact a canonical address to 0x1234…abcd form."""
    a = (address or "").strip()
    if len(a) >= 12:
        return f"{a[:6]}…{a[-4:]}"
    return a or "—"


def _rollback_cohort(db: Database, result: CohortResult) -> bool:
    """Attempt rollback without ever claiming it succeeded when it did not.

    All writable failure paths use this helper.  It keeps an already-recorded
    primary failure (notably a failed commit) intact and appends any secondary
    rollback failure as diagnostic context rather than replacing that cause.
    """
    try:
        db.conn.rollback()
    except Exception as exc:
        result.rolled_back = False
        if "rollback_failure" not in result.reason_codes:
            result.reason_codes.append("rollback_failure")
        detail = f"{type(exc).__name__}: {exc}"
        if result.error:
            result.error = f"{result.error}; rollback_failure: {detail}"
        else:
            result.error = f"rollback_failure: {detail}"
        return False
    result.rolled_back = True
    return True


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
      * each id is a supported persisted identity (legacy/manual ``wl_<hex>``
        or discovery ``sew_<16 hex>``);
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


def _validate_cli_numeric(name: str, value: float) -> float:
    """Validate a CLI numeric option against fixed safe bounds.

    Raises ``CohortValidationError`` (with empty rejected set) on any out-of-range
    or non-finite value. Called BEFORE provider construction / network / DB-open.
    """
    lo, hi = _CLI_LIMITS[name]
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise CohortValidationError(
            f"{name}={value!r} is not a valid number", rejected_watch_ids=[]
        )
    if not (lo <= v <= hi):
        raise CohortValidationError(
            f"{name}={value} out of safe range [{lo}, {hi}]",
            rejected_watch_ids=[],
        )
    return v


# ── Provider (constructed only AFTER the lock is held) ──────────────────────
class _CliProvider:
    """Thin wrapper over ``PolymarketPublicAdapter.get_trades_by_address``.

    Mirrors the PR #71 single-watch CLI provider contract
    (``RealTradeSourceProvider``). ``made_network_call`` signals the ingestion
    pipeline to count real HTTP activity.
    """

    made_network_call = True

    def __init__(self, adapter, *, timeout: float, deadline_ts: Optional[float] = None) -> None:
        self._adapter = adapter
        self._timeout = timeout
        self._deadline_ts = deadline_ts

    async def fetch_trades(self, wallet: str, *, limit: int, page: int):
        from datetime import datetime, timezone

        remaining = self._timeout
        if self._deadline_ts is not None:
            remaining = min(remaining, self._deadline_ts - time.monotonic())
        if remaining <= 0:
            raise CohortDeadlineExceeded("cohort deadline exceeded before provider fetch")
        try:
            return await asyncio.wait_for(
                self._adapter.get_trades_by_address(
                    wallet,
                    # ``datetime.min`` is a naive year-1 value whose
                    # ``timestamp()`` conversion underflows on this platform
                    # before the public request can be made. Polymarket-era
                    # Unix-second trade data is post-epoch, so UTC epoch keeps
                    # the intended no-meaningful-lower-bound semantics safely.
                    since=datetime.fromtimestamp(0, tz=timezone.utc),
                    limit=limit,
                    offset=page * limit,
                    return_raw=True,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError as exc:
            raise CohortDeadlineExceeded("provider fetch exceeded cohort deadline") from exc


@dataclass
class CohortRunConfig:
    """Bounded, fail-closed configuration shared by EVERY watch."""

    max_new_trades_per_wallet: int = 25
    max_total_new_trades: int = 25  # TRUE cohort-wide maximum
    max_gamma_requests: int = 100   # TRUE cohort-wide maximum
    timeout_seconds: float = 30.0
    rss_mb_limit: float = 512.0
    resolve_gamma: bool = False
    enforce_deadline: bool = True


def build_run_config(args: Any) -> CohortRunConfig:
    """Map CLI args -> one bounded config shared by the whole cohort.

    Every numeric option is validated against fixed safe bounds here (raise-safe
    on malformed value) — BEFORE any provider construction / network / DB-open
    performed by the caller.
    """
    cfg = CohortRunConfig(
        max_new_trades_per_wallet=int(
            _validate_cli_numeric(
                "max_new_trades_per_wallet",
                getattr(args, "max_new_trades_per_wallet", 25),
            )
        ),
        max_total_new_trades=int(
            _validate_cli_numeric(
                "max_total_new_trades", getattr(args, "max_total_new_trades", 25)
            )
        ),
        max_gamma_requests=int(
            _validate_cli_numeric(
                "max_gamma_requests", getattr(args, "max_gamma_requests", 100)
            )
        ),
        timeout_seconds=_validate_cli_numeric(
            "timeout_seconds", getattr(args, "timeout_seconds", 30.0)
        ),
        rss_mb_limit=_validate_cli_numeric(
            "rss_mb_limit", getattr(args, "rss_mb_limit", 512.0)
        ),
        resolve_gamma=getattr(args, "resolve_gamma", False),
    )
    return cfg


def _effective_record_limits(config: CohortRunConfig) -> tuple[int, int]:
    """Return actual per-watch and cohort record caps for this invocation.

    CLI callers are rejected before this point when they exceed the public
    bounds.  Programmatic callers can still construct ``CohortRunConfig``
    directly, so cap those values here as well: the provider must never be
    asked for more than its authoritative per-request limit, and a five-watch
    cohort cannot claim more than five such requests.  The returned per-watch
    value also accounts for a smaller total budget.
    """
    per_wallet = max(1, min(int(config.max_new_trades_per_wallet), HARD_MAX_RECORD_LIMIT))
    total = max(1, min(int(config.max_total_new_trades), HARD_MAX_TOTAL_NEW_TRADES))
    return min(per_wallet, total), total


# ── Adapter lifecycle: close exactly once ───────────────────────────────────
async def _close_adapter(real_adapter) -> None:
    """Close the provider adapter EXACTLY once, preferring async ``aclose``.

    * Prefer and ``await adapter.aclose()`` when available.
    * Use synchronous ``close()`` only as a fallback.
    * Never call ``asyncio.run()`` from inside the active event loop.
    * Idempotent: safe to call even if already closed.
    """
    if real_adapter is None:
        return
    aclose = getattr(real_adapter, "aclose", None)
    if aclose is not None and asyncio.iscoroutinefunction(aclose):
        try:
            await aclose()
        except Exception:
            pass
        return
    close = getattr(real_adapter, "close", None)
    if close is not None:
        try:
            close()
        except Exception:
            pass


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
    lock_already_held: bool = False,
) -> CohortResult:
    """Collect evidence for an explicit cohort as ONE bounded, atomic operator run.

    Contract:
      * validate all watch ids BEFORE any provider/network/DB-mutating work;
      * acquire the global operational lock ONCE (provider built only after);
      * call the accepted single-watch collector per watch, deterministically,
        in caller-owned transaction mode (``auto_commit=False``);
      * commit the ENTIRE cohort as one transaction, or roll the whole cohort
        back on the first unhandled watch failure.

    On any unhandled cohort-level error the result carries ``status='failed'``
    and ``cohort_committed=False``. ``rolled_back`` is true only when the
    attempted rollback actually succeeded; ``error`` preserves the original
    exception text.
    """
    run_id = f"cohort_{uuid.uuid4().hex}"
    result = CohortResult(
        status="partial",
        dry_run=dry_run,
        run_id=run_id,
        watch_count_requested=len(watch_ids),
    )

    # Normalize direct programmatic configs too.  A watch cannot use more than
    # the shared cohort budget, so do not advertise an unreachable configured
    # per-wallet cap as its effective bound.
    effective_per_wallet_limit, effective_total_limit = _effective_record_limits(config)
    result.limits = {
        "max_new_trades_per_wallet": effective_per_wallet_limit,
        "max_total_new_trades": effective_total_limit,
        "max_gamma_requests": config.max_gamma_requests,
        "timeout_seconds": int(config.timeout_seconds),
        "rss_mb_limit": int(config.rss_mb_limit),
    }

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
        result.stop_reason = "validation_error"
        for wid in exc.rejected_watch_ids:
            result.watches.append(
                CohortWatchResult(
                    watch_id=wid,
                    status="rejected",
                    reason_codes=["validation_error"],
                )
            )
        result.watch_count_failed = len(exc.rejected_watch_ids)
        result.watch_count_unprocessed = result.watch_count_requested - result.watch_count_failed
        _compute_totals(result)
        return result

    # Shared cohort-wide budgets. The remaining-record budget is TRUE cohort-
    # wide (decremented across watches, never reset per watch). Gamma is one
    # shared budget with per-condition dedupe. The Gamma *base* resolver closes
    # over ``real_adapter`` (built later, after the lock) so when --resolve-gamma
    # is set we resolve through the real adapter's get_market_raw.
    real_adapter = None

    def _make_gamma_base(use_adapter: bool):
        async def _gamma_base(cond):
            if not use_adapter or real_adapter is None:
                return None
            # Native cohort flow: production's async adapter method is awaited
            # on this active loop.  The non-awaitable branch is only a legacy
            # fixture compatibility seam, never a coroutine bridge.
            value = real_adapter.get_market_raw(cond)
            if hasattr(value, "__await__"):
                return await value
            return value

        return _gamma_base

    shared_gamma = SharedGammaBudget(
        _make_gamma_base(config.resolve_gamma) if config.resolve_gamma else gamma_resolver,
        budget=config.max_gamma_requests,
    )
    cohort_budget = CohortBudget(
        remaining_records=effective_total_limit,
        gamma=shared_gamma,
        deadline_ts=(
            time.monotonic() + config.timeout_seconds
            if config.enforce_deadline
            else None
        ),
        rss_mb_limit=config.rss_mb_limit,
    )
    # Link the shared budget so _compute_totals can read authoritative Gamma
    # consumption regardless of which watches completed/failed.
    result._cohort_budget = cohort_budget

    # Build the shared collector config (same bounds for every watch).
    ev_cfg = EvidenceCollectorConfig(
        max_wallets_per_run=1,
        max_new_trades_per_wallet=effective_per_wallet_limit,
        max_total_new_trades=effective_total_limit,
        max_gamma_requests=config.max_gamma_requests,
        timeout_seconds=config.timeout_seconds,
        rss_mb_limit=config.rss_mb_limit,
    )

    # Acquire the global operational lock ONCE for the entire cohort. The
    # provider is constructed only after the lock is held (testable via the
    # injected adapter factory). The orchestrator owns the adapter lifecycle
    # (built once, closed exactly once).
    try:
        with (
            contextlib.nullcontext()
            if lock_already_held
            else operational_job_lock("collect", timeout=lock_timeout, lock_path=lock_path)
        ):
            # Normalize: an injected CLI "spec" exposes build()/close(); a
            # plain adapter is used directly. Build AFTER the lock is held, and
            # INSIDE the structured exception path so a build() failure returns
            # a normal structured result (correction 5).
            try:
                # RSS/deadline must be checked before provider construction,
                # including dry-runs; unknown RSS is fail-closed.
                _collector._checkpoint(cohort_budget)
                real_adapter = (
                    adapter.build() if callable(getattr(adapter, "build", None)) else adapter
                )
            except (CohortDeadlineExceeded, CohortRssExceeded) as exc:
                # Directly supplied adapters are already constructed even
                # though no provider work was authorized; close them once.
                await _close_adapter(real_adapter if real_adapter is not None else adapter)
                result.status = "failed"
                result.cohort_committed = False
                # The resource gate runs before any writable work, so no
                # rollback was attempted and reporting one would be untrue.
                result.rolled_back = False
                result.reason_codes.append(exc.stop_reason)
                result.stop_reason = exc.stop_reason
                result.error = f"{type(exc).__name__}: {exc}"
                result.watch_count_unprocessed = result.watch_count_requested
                _compute_totals(result)
                return result
            except Exception as exc:
                result.status = "failed"
                result.cohort_committed = False
                result.rolled_back = False
                result.reason_codes.append("provider_build_error")
                result.stop_reason = "provider_build_error"
                result.error = f"{type(exc).__name__}: {exc}"
                result.watch_count_unprocessed = result.watch_count_requested
                _compute_totals(result)
                return result

            provider = _CliProvider(
                real_adapter,
                timeout=config.timeout_seconds,
                deadline_ts=cohort_budget.deadline_ts,
            )

            completed = 0
            failed = 0
            commit_attempted = False
            try:
                if not dry_run:
                    # One bounded caller-owned transaction for the WHOLE cohort.
                    # We rely on Python's default sqlite3 transaction handling
                    # (isolation_level=""): the connection auto-begins an
                    # implicit transaction on the first DML and keeps it open
                    # until the cohort explicitly commits or rolls back.
                    pass
                for wid in ordered:
                    # Cohort-wide bounds enforced DURING the loop (correction 2):
                    # the shared record budget and deadline are checked before
                    # EACH watch, not only after a watch returns.
                    if cohort_budget.remaining_records <= 0:
                        # Cohort-wide record budget reached: stop cleanly and
                        # KEEP the in-budget writes already staged (commit),
                        # rather than rolling them back. Later watches are
                        # marked unprocessed and never called.
                        cohort_budget.stop_reason = "record_budget_exhausted"
                        for pending in ordered[completed + failed:]:
                            wres = CohortWatchResult(watch_id=pending)
                            wres.status = "unprocessed"
                            wres.stop_reason = "record_budget_exhausted"
                            result.watches.append(wres)
                        if not dry_run:
                            try:
                                commit_attempted = True
                                db.conn.commit()
                                result.cohort_committed = True
                                result.rolled_back = False
                            except Exception as exc:
                                result.cohort_committed = False
                                result.status = "failed"
                                result.stop_reason = "commit_failure"
                                result.reason_codes.append("commit_failure")
                                result.error = f"{type(exc).__name__}: {exc}"
                                _rollback_cohort(db, result)
                                _compute_totals(result)
                                return result
                        else:
                            result.cohort_committed = False
                        result.status = "success"
                        result.stop_reason = "record_budget_exhausted"
                        result.reason_codes.append("record_budget_exhausted")
                        _compute_totals(result)
                        return result
                    if (
                        cohort_budget.deadline_ts is not None
                        and time.monotonic() > cohort_budget.deadline_ts
                    ):
                        cohort_budget.stop_reason = "deadline_exceeded"
                        for pending in ordered[completed + failed:]:
                            wres = CohortWatchResult(watch_id=pending)
                            wres.status = "unprocessed"
                            wres.stop_reason = "deadline_exceeded"
                            result.watches.append(wres)
                        result.status = "failed"
                        result.cohort_committed = False
                        result.stop_reason = "deadline_exceeded"
                        result.reason_codes.append("deadline_exceeded")
                        result.error = f"CohortDeadlineExceeded: {result.stop_reason}"
                        if not dry_run:
                            _rollback_cohort(db, result)
                        _compute_totals(result)
                        return result
                    wres = CohortWatchResult(watch_id=wid)
                    try:
                        single = await _collector.collect_evidence(
                            db,
                            watch_id=wid,
                            provider=provider,
                            gamma_resolver=shared_gamma if config.resolve_gamma else gamma_resolver,
                            config=ev_cfg,
                            dry_run=dry_run,
                            auto_commit=False,  # caller owns the transaction
                            cohort_budget=cohort_budget,
                        )
                        _fold_single(db, wres, single)
                        wres.status = "ok"
                        completed += 1
                        result.watches.append(wres)
                    except Exception as exc:
                        failed += 1
                        wres.status = "error"
                        wres.reason_codes.append(f"watch_error: {type(exc).__name__}")
                        # Authoritative stop reason: use the sentinel's own
                        # stop_reason when present (writer/gamma/deadline/rss),
                        # else a provider/network error becomes "watch_error"
                        # with the original exception preserved.
                        if isinstance(exc, WriterFailure):
                            stop_reason = getattr(exc, "stop_reason", "writer_failure")
                        elif isinstance(exc, GammaResolutionError):
                            stop_reason = "gamma_resolution_error"
                        elif isinstance(exc, GammaBudgetExhausted):
                            stop_reason = "gamma_budget_exhausted"
                        elif isinstance(exc, CohortDeadlineExceeded):
                            stop_reason = "deadline_exceeded"
                        elif isinstance(exc, CohortRssExceeded):
                            stop_reason = "rss_limit_exceeded"
                        else:
                            stop_reason = "watch_error"
                        wres.stop_reason = stop_reason
                        result.watches.append(wres)
                        # The remaining (later) watches must be marked
                        # unprocessed and never called (correction 1): stop
                        # processing immediately on the first watch failure.
                        for pending in ordered[completed + failed:]:
                            pw = CohortWatchResult(watch_id=pending)
                            pw.status = "unprocessed"
                            pw.stop_reason = stop_reason
                            result.watches.append(pw)
                        result.watch_count_processed = completed
                        result.watch_count_failed = failed
                        result.watch_count_unprocessed = len(ordered) - completed - failed
                        result.status = "failed"
                        result.cohort_committed = False
                        result.error = f"{type(exc).__name__}: {exc}"
                        result.stop_reason = stop_reason
                        result.reason_codes.append("watch_failure")
                        # Roll back the ENTIRE cohort (watches 1..n-1 included).
                        if not dry_run:
                            _rollback_cohort(db, result)
                        _compute_totals(result)
                        return result

                # All watches processed without an unhandled failure.
                if not dry_run:
                    commit_attempted = True
                    db.conn.commit()
                    result.cohort_committed = True
                    result.rolled_back = False
                else:
                    # Dry-run: nothing to commit; rows were never written.
                    result.cohort_committed = False
                result.watch_count_processed = completed
                result.watch_count_failed = failed
                result.watch_count_unprocessed = len(ordered) - completed - failed
                result.status = "success"
                result.stop_reason = cohort_budget.stop_reason
            except asyncio.CancelledError:
                # Cancellation is not an ordinary provider failure, but writable
                # staged rows must never survive it. Preserve cancellation for
                # the caller after rollback; the inner finally closes adapter.
                if not dry_run:
                    _rollback_cohort(db, result)
                raise
            except Exception as exc:
                # Defensive: any leak outside the per-watch loop.
                result.watch_count_processed = completed
                result.watch_count_failed = failed
                result.watch_count_unprocessed = len(ordered) - completed - failed
                result.status = "failed"
                result.cohort_committed = False
                result.error = f"{type(exc).__name__}: {exc}"
                result.stop_reason = (
                    "commit_failure"
                    if commit_attempted
                    else (getattr(exc, "stop_reason", "cohort_error") or "cohort_error")
                )
                result.reason_codes.append(result.stop_reason)
                if not dry_run:
                    _rollback_cohort(db, result)
                _compute_totals(result)
                return result
            finally:
                # Close the provider's underlying adapter exactly once.
                await _close_adapter(real_adapter)
                real_adapter = None
    except LockError as exc:
        # Lock contention: ZERO provider/network/DB-mutating activity occurred.
        result.status = "failed"
        result.cohort_committed = False
        result.rolled_back = False
        result.reason_codes.append("operational_lock_unavailable")
        result.stop_reason = "operational_lock_unavailable"
        result.error = f"LockError: {exc}"
        result.watch_count_unprocessed = result.watch_count_requested
        _compute_totals(result)
        return result
    except Exception as exc:
        # Any structured failure outside the lock block (e.g. cancellation).
        try:
            await _close_adapter(real_adapter)
        except Exception:
            pass
        result.status = "failed"
        result.cohort_committed = False
        # This outer boundary has no established transaction ownership; do not
        # claim a rollback that was never attempted.
        result.rolled_back = False
        result.reason_codes.append("cohort_error")
        result.stop_reason = getattr(exc, "stop_reason", "cohort_error") or "cohort_error"
        result.error = f"{type(exc).__name__}: {exc}"
        result.watch_count_unprocessed = result.watch_count_requested
        _compute_totals(result)
        return result

    _compute_totals(result)
    return result


def _fold_single(db: Database, wres: CohortWatchResult, single: EvidenceCollectionResult) -> None:
    """Fold one accepted single-watch result into the per-watch cohort slice."""
    wres.wallet_id = single.wallet_id or ""
    wres.raw_trades_examined = single.raw_trades_examined
    wres.valid_buy_trades = single.valid_buy_trades
    wres.rows_would_create = single.rows_would_create
    wres.rows_would_update = single.rows_would_update
    wres.rows_created = single.rows_created
    wres.duplicate_rows_observed = single.duplicate_rows_observed
    wres.rows_updated = single.rows_updated
    wres.enrichment_rows_created = single.enrichment_rows_created
    wres.enrichment_rows_updated = single.enrichment_rows_updated
    wres.enrichment_no_ops = single.enrichment_no_ops
    wres.gamma_requests = single.gamma_requests
    wres.effective_new_trade_limit = single.effective_new_trade_limit
    wres.stop_reason = single.stop_reason
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


def _compute_totals(result: CohortResult) -> None:
    """Compute honest per-watch and cohort totals on BOTH success and failure."""
    totals = {
        "raw_trades_examined": 0,
        "valid_buy_trades": 0,
        "rows_would_create": 0,
        "rows_would_update": 0,
        "rows_created": 0,
        "duplicate_rows_observed": 0,
        "rows_updated": 0,
        "enrichment_rows_created": 0,
        "enrichment_rows_updated": 0,
        "enrichment_no_ops": 0,
        "gamma_requests": 0,
    }
    processed = 0
    failed = 0
    rejected = 0
    unprocessed = 0
    for w in result.watches:
        totals["raw_trades_examined"] += w.raw_trades_examined
        totals["valid_buy_trades"] += w.valid_buy_trades
        totals["rows_would_create"] += w.rows_would_create
        totals["rows_would_update"] += w.rows_would_update
        totals["rows_created"] += w.rows_created
        totals["duplicate_rows_observed"] += w.duplicate_rows_observed
        totals["rows_updated"] += w.rows_updated
        totals["enrichment_rows_created"] += w.enrichment_rows_created
        totals["enrichment_rows_updated"] += w.enrichment_rows_updated
        totals["enrichment_no_ops"] += w.enrichment_no_ops
        totals["gamma_requests"] += w.gamma_requests
        if w.status == "ok":
            processed += 1
        elif w.status == "error":
            failed += 1
        elif w.status == "rejected":
            # Rejection is a completed validation failure, not a watch left
            # unprocessed.  Count it in both the broad failed metric and its
            # explicit rejected slice so clients can report either truthfully.
            failed += 1
            rejected += 1
        else:
            unprocessed += 1
    # Unprocessed = requested minus processed minus failed (covers rejected and
    # not-yet-processed watches on early stop).
    if result.watch_count_requested:
        unprocessed = (
            result.watch_count_requested - processed - failed
        )
    result.totals = totals
    result.watch_count_processed = processed
    result.watch_count_failed = failed
    result.watch_count_rejected = rejected
    result.watch_count_unprocessed = max(0, unprocessed)

    # Truthful cohort-level write counts: if the cohort rolled back (did NOT
    # commit), nothing was persisted, so the cohort-level "created" tallies
    # must be zeroed. Per-watch slices still report what each watch attempted.
    if not result.cohort_committed and result.rolled_back:
        totals["rows_created"] = 0
        totals["rows_updated"] = 0
        totals["enrichment_rows_created"] = 0
        totals["enrichment_rows_updated"] = 0

    # Consumption / remaining for the result JSON. The Gamma request count is
    # authoritative from the SHARED cohort budget (one dedupe cache + one cap),
    # not the per-watch slice max — a failed watch may never read the final
    # counter, so the slice max would under-report.
    gamma_used = (
        result._cohort_budget.gamma.used if result._cohort_budget is not None else 0
    )
    records_used = (
        result.limits.get("max_total_new_trades", 0) - result._cohort_budget.remaining_records
        if result._cohort_budget is not None
        else 0
    )
    result.consumption = {
        "fresh_rows_created_or_projected": max(0, records_used),
        "duplicates_observed": totals["duplicate_rows_observed"],
        "gamma_requests": gamma_used,
    }
    result.remaining = {
        "max_total_new_trades": (
            max(0, result._cohort_budget.remaining_records)
            if result._cohort_budget is not None
            else result.limits.get("max_total_new_trades", 0)
        ),
        "max_gamma_requests": max(
            0, result.limits.get("max_gamma_requests", 0) - gamma_used
        ),
    }


__all__ = [
    "HARD_MAX_RECORD_LIMIT",
    "HARD_MAX_TOTAL_NEW_TRADES",
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
