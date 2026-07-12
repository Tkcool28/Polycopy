"""PR25A bounded approved-wallet bridge.

This is deliberately the sole writer orchestration surface. It never writes
source trades or execution tables; its explicit authorization object is only
created by the CLI after the shared operational lock/RSS guard is active.
"""
from __future__ import annotations

import asyncio
import inspect
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from polycopy.db.copy_candidate_persistence import persist_copy_candidate
from polycopy.db.levels_persistence import persist_depth_levels
from polycopy.db.price_snapshot_persistence import persist_price_snapshot
from polycopy.db.wallet_identity import canonical_wallet_address
from polycopy.scoring.depth_normalization import (
    DEFAULT_MAX_LEVELS_PER_SIDE,
    NormalizedLevel,
    compute_book_hash,
    normalize_book_levels,
)
from polycopy.domain.copy_candidate import CandidateStatus, CopyCandidate
from polycopy.domain.market import Market
from polycopy.engine.price_snapshots import _now_iso, snapshot_one
from polycopy.ingestion.normalized_source_trade import SOURCE_NAME
from polycopy.scoring.paper_signal import (
    PersistedPaperSignalInputs,
    compute_bridge_trade_copyability_and_paper_input,
    persist_bridge_trade_copyability_v1,
)

MAX_LIMIT = 10
# PR25A: bounded evidence capture; this is deliberately the existing frozen
# depth-normalization limit rather than a bridge-specific widening.
BRIDGE_MAX_DEPTH_LEVELS_PER_SIDE = DEFAULT_MAX_LEVELS_PER_SIDE
ALLOWED_WRITE_TABLES = frozenset(
    {
        "wallets",
        "markets",
        "market_outcomes",
        "copy_candidates",
        "candidate_price_snapshots",
        "candidate_price_snapshot_levels",
        "trade_copyability_decisions",
        "paper_signal_decisions",
    }
)
FORBIDDEN_WRITE_TABLES = frozenset(
    {
        "source_trades",
        "orders",
        "positions",
        "approvals",
        "fills",
        "settlement",
        "config",
        "decision_log",
        "wallet_score_decisions",
        "category_wallet_score_decisions",
        "shadow_score_decisions",
        "exit_experiment_registrations",
    }
)


class GammaProvider(Protocol):
    def get_market(self, condition_id: str) -> Any: ...


class BookProvider(Protocol):
    async def fetch_book(self, token_id: str) -> Any: ...


# Deliberately opaque identity capability.  Only the CLI imports the private
# issuer after its lock and RSS guards are active; direct callers cannot forge it.
_WRITE_CAPABILITY = object()


def _issue_write_capability() -> object:
    return _WRITE_CAPABILITY


@dataclass(frozen=True)
class BridgeDependencies:
    gamma: GammaProvider
    clob: BookProvider | None = None


@dataclass
class BridgeReport:
    wallet: str
    limit: int
    mode: str
    selected: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)
    write_counts: dict[str, int] = field(default_factory=dict)
    forbidden_table_delta: dict[str, int] = field(default_factory=dict)
    failures: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "limit": self.limit,
            "mode": self.mode,
            "dry_run": self.mode == "ro",
            "selected": self.selected,
            "rows": self.rows,
            "write_counts": self.write_counts,
            "forbidden_table_delta": self.forbidden_table_delta,
            "failures": self.failures,
            "allowed_write_tables": sorted(ALLOWED_WRITE_TABLES),
            "forbidden_write_tables": sorted(FORBIDDEN_WRITE_TABLES),
        }


def validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or limit <= 0 or limit > MAX_LIMIT:
        raise ValueError(f"--limit must be between 1 and {MAX_LIMIT}")
    return limit


def select_approved_source_trades(
    db: Any,
    wallet: str,
    *,
    limit: int,
    source_trade_id: str | None = None,
) -> list[Any]:
    validate_limit(limit)
    address = canonical_wallet_address(wallet)
    if address is None:
        raise ValueError("approved wallet is malformed")
    where = [
        "source = ?",
        "lower(trader_address) = ?",
        "side = 'BUY'",
        "COALESCE(is_sample, 0) = 0",
        "source_trade_id IS NOT NULL",
        "trim(source_trade_id) != ''",
    ]
    params: list[Any] = [SOURCE_NAME, address]
    if source_trade_id is not None:
        if not source_trade_id.strip():
            raise ValueError("--source-trade-id must be non-empty")
        where.append("source_trade_id = ?")
        params.append(source_trade_id)
    params.append(limit)
    return db.fetchall(
        "SELECT id, source, source_trade_id, market_source_id, side, outcome, "
        "quantity, price, trader_address, timestamp, token_id FROM source_trades "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY timestamp ASC, source_trade_id ASC, id ASC LIMIT ?",
        tuple(params),
    )


def _await(value: Any) -> Any:
    """Run a possibly-awaitable value to completion from this sync caller.

    Only used for isolated async calls (e.g. the in-memory snapshot book
    provider inside :func:`snapshot_one`). The per-row Gamma/CLOB fetches in
    :func:`process_approved_wallet_trades` run on the batch's single shared
    event loop via :func:`_run_on_loop`, not here.
    """
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    raise RuntimeError("PR25A synchronous bridge cannot run in an active event loop")


def _run_on_loop(loop: Any, value: Any) -> Any:
    """Execute one Gamma/CLOB adapter call on the batch's single event loop.

    Awaitable providers (the real adapters) are run with
    ``loop.run_until_complete``; synchronous/mock providers (tests) are
    returned as-is. This keeps every selected row's async call on the SAME
    shared loop — no ``asyncio.run()`` per row, so shared async clients stay
    bound to one loop and later rows cannot hit a closed-loop ``RuntimeError``.
    """
    if inspect.isawaitable(value):
        return loop.run_until_complete(value)
    return value


def _hydrate(
    gamma: GammaProvider, row: Any, market: Any | None = None,
) -> tuple[Market | None, Any | None, str | None]:
    condition, token, label = (
        str(row[key] or "").strip()
        for key in ("market_source_id", "token_id", "outcome")
    )
    if not condition or not token or not label:
        return None, None, "missing_condition_token_or_outcome"
    if market is None:
        try:
            market = _await(gamma.get_market(condition))
        except Exception as exc:
            return None, None, f"gamma_error:{type(exc).__name__}"
    if market is None:
        return None, None, "gamma_market_missing"
    if str(getattr(market, "source_id", "")) != condition:
        return None, None, "gamma_condition_conflict"
    matches = [
        outcome
        for outcome in getattr(market, "outcomes", [])
        if str(getattr(outcome, "clob_token_id", "")) == token
    ]
    if len(matches) != 1:
        return None, None, "outcome_missing" if not matches else "outcome_ambiguous"
    if str(getattr(matches[0], "label", "")) != label:
        return None, None, "outcome_label_conflict"
    return market, matches[0], None


def _safe_persist_market(
    db: Any, market: Market, outcome: Any
) -> tuple[str | None, int | None, str | None, bool]:
    """Insert a missing Gamma mapping only; reject persisted conflicts."""
    existing = db.fetchone(
        "SELECT id FROM markets WHERE source=? AND source_id=?",
        (market.source, market.source_id),
    )
    if existing is None:
        market_id = str(market.id)
        db.execute(
            "INSERT INTO markets (id, source_id, source, question, active, closed, "
            "resolved, resolution_outcome, volume_24h, end_date, fetched_at, "
            "is_sample) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                market_id,
                market.source_id,
                market.source,
                market.question,
                int(market.active),
                int(market.closed),
                int(market.resolved),
                market.resolution_outcome,
                market.volume_24h,
                market.end_date.isoformat() if market.end_date else None,
                market.fetched_at.isoformat(),
                int(market.is_sample),
            ),
        )
        db.execute(
            "INSERT INTO market_outcomes (market_id, label, price, volume, "
            "clob_token_id) VALUES (?, ?, ?, ?, ?)",
            (
                market_id,
                outcome.label,
                outcome.price,
                outcome.volume,
                outcome.clob_token_id,
            ),
        )
        db.conn.commit()
        outcome_id = db.fetchone(
            "SELECT id FROM market_outcomes WHERE market_id=? AND clob_token_id=?",
            (market_id, outcome.clob_token_id),
        )["id"]
        return market_id, int(outcome_id), None, True
    market_id = str(existing["id"])
    rows = db.fetchall(
        "SELECT id, label, clob_token_id FROM market_outcomes "
        "WHERE market_id=? AND clob_token_id=?",
        (market_id, outcome.clob_token_id),
    )
    if len(rows) != 1:
        return None, None, "persisted_token_missing_or_ambiguous", False
    if str(rows[0]["label"]) != str(outcome.label):
        return None, None, "persisted_mapping_conflict", False
    return market_id, int(rows[0]["id"]), None, False


def _wallet(db: Any, address: str) -> tuple[str | None, str | None, bool]:
    rows = db.fetchall(
        "SELECT id, address, canonical_address FROM wallets "
        "WHERE canonical_address=? OR lower(address)=? ORDER BY id",
        (address, address),
    )
    ids = {str(row["id"]) for row in rows}
    if len(ids) > 1:
        return None, "wallet_identity_conflict", False
    if rows:
        row = rows[0]
        if row["canonical_address"] not in (None, address):
            return None, "wallet_canonical_conflict", False
        return str(row["id"]), None, False
    wallet_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO wallets (id, address, canonical_address, created_at) "
        "VALUES (?, ?, ?, ?)",
        (wallet_id, address, address, now),
    )
    db.conn.commit()
    return wallet_id, None, True


def _candidate(row: Any, wallet_id: str, market_id: str, outcome_id: int, now_iso: str) -> CopyCandidate:
    """Build a CopyCandidate using a single, externally-provided ``now_iso``.

    The shared timestamp is reused for ``observed_at``, ``created_at``, and
    ``updated_at`` so the deterministic paper-signal snapshot lookup
    (``fetched_at <= candidate.created_at``) is guaranteed to find the
    snapshot we insert a moment later in the same trade.
    """
    return CopyCandidate(
        wallet_id=wallet_id,
        source=str(row["source"]),
        source_trade_id=str(row["source_trade_id"]),
        source_trade_internal_id=str(row["id"]),
        market_id=market_id,
        market_outcome_id=outcome_id,
        market_source_id=str(row["market_source_id"]),
        token_id=str(row["token_id"]),
        outcome_label=str(row["outcome"]),
        side="BUY",
        source_trade_price=float(row["price"]),
        source_trade_quantity=float(row["quantity"]),
        source_trade_notional=float(row["price"]) * float(row["quantity"]),
        source_trade_timestamp=str(row["timestamp"]),
        observed_at=now_iso,
        wallet_score_version="unavailable",
        wallet_score=0.0,
        wallet_verdict="unavailable",
        status=CandidateStatus.PENDING_PRICE_CHECK.value,
        status_reason=None,
        metrics_json=None,
        created_at=now_iso,
        updated_at=now_iso,
    )


def _counts(db: Any, tables: frozenset[str]) -> dict[str, int]:
    existing = {
        str(row["name"])
        for row in db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return {
        table: int(db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"])
        for table in tables
        if table in existing
    }


def _delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {key: after[key] - value for key, value in before.items() if after[key] - value}


def _as_optional_bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def _snapshot_value(snapshot: Any, field: str) -> Any:
    try:
        return snapshot[field]
    except (KeyError, TypeError, IndexError):
        return getattr(snapshot, field)


def _bounded_book_levels(book: Any) -> tuple[list[tuple[Any, Any]], list[tuple[Any, Any]], int, int, str | None]:
    """Validate, sort, and cap provider levels before persistence."""
    raw_bids = [(level.price, level.size) for level in book.bids]
    raw_asks = [(level.price, level.size) for level in book.asks]
    bids, asks, error = normalize_book_levels(
        raw_bids, raw_asks, max_levels=BRIDGE_MAX_DEPTH_LEVELS_PER_SIDE,
    )
    if error:
        return [], [], len(raw_bids), len(raw_asks), error
    return (
        [(level.price, level.size) for level in bids],
        [(level.price, level.size) for level in asks],
        len(raw_bids),
        len(raw_asks),
        None,
    )


class _CommitShield:
    """Facade that lets legacy persistence helpers join our savepoint."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.conn = self

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._db.conn.execute(*args, **kwargs)

    def fetchone(self, *args: Any, **kwargs: Any) -> Any:
        return self._db.fetchone(*args, **kwargs)

    def fetchall(self, *args: Any, **kwargs: Any) -> Any:
        return self._db.fetchall(*args, **kwargs)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _source_trade_error(row: Any) -> str | None:
    try:
        price, quantity = float(row["price"]), float(row["quantity"])
    except (TypeError, ValueError):
        return "invalid_price_or_quantity"
    if not math.isfinite(price) or not math.isfinite(quantity) or price <= 0 or quantity <= 0:
        return "invalid_price_or_quantity"
    try:
        timestamp = str(row["timestamp"])
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "invalid_timestamp"
    if not str(row["market_source_id"] or "").strip() or not str(row["token_id"] or "").strip():
        return "missing_condition_or_token"
    return None


def _record_skip(report: BridgeReport, detail: dict[str, Any], reason: str) -> None:
    detail["skip_reason"] = reason
    report.rows.append(detail)
    report.failures.append({"source_trade_id": detail["source_trade_id_prefix"], "reason": reason})


def _evaluate_dry_run_decision(
    detail: dict[str, Any],
    row: Any,
    market: Any,
    outcome: Any,
    book: Any,
    bids: list[Any],
    asks: list[Any],
    wallet_address: str,
) -> None:
    """Run the full in-memory Trade Copyability v1 + paper-signal evaluation
    for a dry-run row, recording verdicts and would-write actions — without any
    database persistence.

    Uses the SAME pure helpers the write path uses (``snapshot_one`` builder,
    ``compute_bridge_trade_copyability_and_paper_input``) on an in-memory
    candidate/snapshot/depth so a dry-run and a real write exercise identical
    scoring logic. The would-write action list advertises every allowlisted
    evidence table/object a real ``--write`` would persist for this row.
    """
    now = datetime.now(timezone.utc)
    now_iso = _now_iso(now)
    market_id = str(getattr(market, "id", None) or getattr(market, "source_id", "dry-run"))
    outcome_id = 0
    try:
        outcome_id = int(getattr(outcome, "id", 0) or 0)
    except (TypeError, ValueError):
        outcome_id = 0
    # In-memory candidate (same builder the write path uses).
    candidate = _candidate(row, wallet_address, market_id, outcome_id, now_iso)
    # In-memory snapshot via production's pure builder (no DB).
    class _FetchedBook:
        async def fetch_book(self, token_id: str) -> Any:
            return book

    snapshot = snapshot_one(
        None,
        candidate_id=0,
        candidate=candidate,
        market=market,
        outcome=outcome,
        snapshot_run_id=f"pr25a:{row['id']}",
        now=now,
        book_provider=_FetchedBook(),
    )
    snapshot_dict = snapshot.model_dump()
    depth_hash = compute_book_hash(
        [NormalizedLevel(price=p, size=s) for p, s in bids],
        [NormalizedLevel(price=p, size=s) for p, s in asks],
    )
    notional = candidate.source_trade_notional
    inputs = PersistedPaperSignalInputs(
        candidate=candidate.model_dump(),
        snapshot=snapshot_dict,
        snapshot_id=snapshot.id,
        source_trade=dict(row),
        depth_bids=tuple(NormalizedLevel(price=p, size=s) for p, s in bids),
        depth_asks=tuple(NormalizedLevel(price=p, size=s) for p, s in asks),
        depth_hash=depth_hash,
        depth_status_reason=None,
        wallet_id=wallet_address,
        source_trade_id=str(row["source_trade_id"]),
        intended_stake=float(notional) if notional is not None else None,
        side="BUY",
        price_deterioration_pct=snapshot_dict.get("price_deterioration_pct"),
        behavior_evidence_cutoff=snapshot_dict.get("fetched_at"),
    )
    trade_result, _typed_input, _trade_idem = compute_bridge_trade_copyability_and_paper_input(
        inputs=inputs, now=now,
    )
    detail["stages"]["trade_copyability"] = trade_result.verdict.value
    detail["trade_copyability_score"] = float(trade_result.score)
    detail["stages"]["paper"] = "evaluated"
    detail["paper_signal_verdict"] = _typed_input.final_verdict
    detail["paper_signal_reason"] = _typed_input.final_reason
    detail["actions"].extend(
        [
            "would_write_allowlisted_evidence_only",
            "candidate",
            "snapshot",
            "depth_levels",
            "trade_copyability_v1",
            "canonical_paper",
        ]
    )


def process_approved_wallet_trades(
    db: Any,
    *,
    wallet: str,
    limit: int,
    dependencies: BridgeDependencies,
    write: bool = False,
    write_authorization: object | None = None,
    source_trade_id: str | None = None,
) -> BridgeReport:
    address = canonical_wallet_address(wallet)
    if address is None:
        raise ValueError("approved wallet is malformed")
    if write and write_authorization is not _WRITE_CAPABILITY:
        raise PermissionError("PR25A writes require CLI authorization")
    rows = select_approved_source_trades(
        db, address, limit=limit, source_trade_id=source_trade_id
    )
    report = BridgeReport(
        wallet=address,
        limit=limit,
        mode="rw" if write else "ro",
        selected=len(rows),
    )
    before = _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES) if write else {}

    # One event loop for the entire batch's Gamma + CLOB fetches. Shared async
    # clients stay bound to this single loop for every selected row. Each async
    # adapter call is guarded per-row (try/except) so ONE trade's Gamma/CLOB
    # failure cannot abort the rest of the batch or skip its own per-row report.
    #
    # CLOB is only requested AFTER source validation AND a successful Gamma
    # hydration + exact token mapping, preserving the original guarded ordering.
    #
    # The loop is closed before the dry-run evaluation / write persistence
    # because snapshot_one() raises inside a running loop (it manages its own
    # self-contained loop). Phase B therefore runs with no loop running, exactly
    # as the original code did — so --write semantics are preserved unchanged.
    loop = asyncio.new_event_loop()
    staged: list[tuple[Any, dict[str, Any], Any, Any, Any, list[Any], list[Any]]] = []
    try:
        for row in rows:
            detail: dict[str, Any] = {
                "source_trade_id_prefix": str(row["source_trade_id"])[:24],
                "source_trade_internal_id": str(row["id"]),
                "stages": {},
                "request_count": 0,
                "actions": [],
                "skip_reason": None,
            }
            source_error = _source_trade_error(row)
            detail["stages"]["source_validation"] = "ok" if source_error is None else source_error
            if source_error:
                _record_skip(report, detail, source_error)
                continue
            # Gamma request (guarded per row).
            try:
                market = _run_on_loop(
                    loop, dependencies.gamma.get_market(str(row["market_source_id"]))
                )
            except Exception as exc:
                detail["stages"]["gamma"] = f"gamma_error:{type(exc).__name__}"
                _record_skip(report, detail, detail["stages"]["gamma"])
                continue
            # Exact condition/token/outcome hydration.
            market, outcome, error = _hydrate(dependencies.gamma, row, market=market)
            detail["stages"]["gamma"] = "ok" if error is None else error
            if error:
                _record_skip(report, detail, error)
                continue
            # CLOB request (guarded per row) — only after successful Gamma.
            if dependencies.clob is None:
                detail["stages"]["clob_preflight"] = "no_book_provider"
                _record_skip(report, detail, "no_book_provider")
                continue
            try:
                book = _run_on_loop(
                    loop, dependencies.clob.fetch_book(str(row["token_id"]))
                )
                detail["request_count"] = int(getattr(book, "request_attempts", 1))
            except Exception as exc:
                detail["stages"]["clob_preflight"] = f"clob_error:{type(exc).__name__}"
                _record_skip(report, detail, detail["stages"]["clob_preflight"])
                continue
            clob_error = (
                None
                if book
                and not getattr(book, "error_code", None)
                and getattr(book, "bids", None)
                and getattr(book, "asks", None)
                else "clob_evidence_invalid"
            )
            detail["stages"]["clob_preflight"] = "ok" if clob_error is None else clob_error
            if clob_error:
                _record_skip(report, detail, clob_error)
                continue
            # Depth normalization (sync, guarded).
            try:
                bids, asks, raw_bid_n, raw_ask_n, level_error = _bounded_book_levels(book)
            except (AttributeError, TypeError, ValueError) as exc:
                bids, asks, raw_bid_n, raw_ask_n, level_error = [], [], 0, 0, f"depth_normalization_error:{type(exc).__name__}"
            detail["raw_bid_level_count"] = raw_bid_n
            detail["raw_ask_level_count"] = raw_ask_n
            detail["persisted_bid_level_count"] = len(bids)
            detail["persisted_ask_level_count"] = len(asks)
            if level_error:
                detail["stages"]["levels"] = level_error
                _record_skip(report, detail, level_error)
                continue
            # Defer dry-run evaluation / write persistence to Phase B (outside
            # the running loop, where snapshot_one is safe).
            staged.append((row, detail, market, outcome, book, bids, asks))
    finally:
        loop.close()

    # ── Phase B: process staged rows with no running event loop ──────────────
    for row, detail, market, outcome, book, bids, asks in staged:
        if not write:
            # The dry-run executes the full in-memory Trade Copyability v1 +
            # paper-signal evaluation (reusing the exact pure calls the write
            # path uses) WITHOUT any persistence. The allowlisted evidence
            # tables are reported as would-write actions so the operator can see
            # precisely what a real --write would persist.
            _evaluate_dry_run_decision(
                detail, row, market, outcome, book, bids, asks, address,
            )
            report.rows.append(detail)
            continue

        # Every allowlisted write for this source trade joins exactly one
        # savepoint. Legacy owners see a commit-shield facade, so their
        # local commits cannot escape this atomic boundary.
        savepoint = "pr25a_trade"
        db.conn.execute(f"SAVEPOINT {savepoint}")
        tx_db = _CommitShield(db)
        try:
            wallet_id, error, wallet_new = _wallet(tx_db, address)
            detail["stages"]["wallet"] = (
                "inserted" if wallet_new else "ok" if error is None else error
            )
            if error:
                raise RuntimeError(error)
            assert wallet_id is not None
            market_id, outcome_id, error, market_new = _safe_persist_market(tx_db, market, outcome)
            detail["stages"]["market_mapping"] = (
                "inserted" if market_new else "ok" if error is None else error
            )
            if error:
                raise RuntimeError(error)
            assert market_id is not None and outcome_id is not None
            now_iso = _now_iso(datetime.now(timezone.utc))
            candidate_id, candidate_new = persist_copy_candidate(
                tx_db, _candidate(row, wallet_id, market_id, outcome_id, now_iso)
            )
            detail["stages"]["candidate"] = "inserted" if candidate_new else "replayed"

            class FetchedBook:
                async def fetch_book(self, token_id: str) -> Any:
                    return book

            snapshot = snapshot_one(
                tx_db,
                candidate_id=candidate_id,
                snapshot_run_id=f"pr25a:{row['id']}",
                book_provider=FetchedBook(),
                now=datetime.fromisoformat(now_iso.replace("Z", "+00:00")),
            )
            snapshot_id, snapshot_new = persist_price_snapshot(tx_db, snapshot)
            persisted_snapshot = tx_db.fetchone(
                "SELECT id FROM candidate_price_snapshots WHERE id=?", (snapshot_id,)
            )
            if persisted_snapshot is None:
                raise RuntimeError("snapshot_persistence_missing")
            bid_n, ask_n, level_error = persist_depth_levels(
                tx_db, snapshot_id, bids, asks,
                max_levels=BRIDGE_MAX_DEPTH_LEVELS_PER_SIDE,
                manage_transaction=False,
            )
            detail["stages"]["snapshot"] = "inserted" if snapshot_new else "replayed"
            detail["stages"]["levels"] = level_error or f"ok:{bid_n}/{ask_n}"
            detail["snapshot_id"] = snapshot_id
            detail["snapshot_token_id"] = snapshot.token_id
            if level_error:
                raise RuntimeError(level_error)
            trade_decision_id, signal_id = persist_bridge_trade_copyability_v1(tx_db, candidate_id)
            detail["stages"]["trade_copyability"] = "persisted"
            detail["stages"]["paper"] = "persisted"
            detail["actions"].extend([
                "candidate", "snapshot", "depth_levels", "trade_copyability_v1", "canonical_paper",
            ])
            detail["trade_copyability_decision_id"] = trade_decision_id
            detail["paper_signal_id"] = signal_id
            db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as exc:
            db.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            db.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            reason = str(exc) if isinstance(exc, RuntimeError) else f"persistence_error:{type(exc).__name__}"
            _record_skip(report, detail, reason)
            continue
        report.rows.append(detail)
    if write:
        after = _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES)
        report.write_counts = _delta(
            {key: value for key, value in before.items() if key in ALLOWED_WRITE_TABLES},
            {key: value for key, value in after.items() if key in ALLOWED_WRITE_TABLES},
        )
        report.forbidden_table_delta = _delta(
            {key: value for key, value in before.items() if key in FORBIDDEN_WRITE_TABLES},
            {key: value for key, value in after.items() if key in FORBIDDEN_WRITE_TABLES},
        )
        if report.forbidden_table_delta:
            raise RuntimeError(
                f"forbidden PR25A write detected: {report.forbidden_table_delta}"
            )
    return report


__all__ = [
    "ALLOWED_WRITE_TABLES",
    "FORBIDDEN_WRITE_TABLES",
    "BridgeDependencies",
    "BridgeReport",
    "MAX_LIMIT",
    "process_approved_wallet_trades",
    "select_approved_source_trades",
    "validate_limit",
]
