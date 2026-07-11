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
from polycopy.domain.copy_candidate import CandidateStatus, CopyCandidate
from polycopy.domain.market import Market
from polycopy.engine.price_snapshots import _now_iso, snapshot_one
from polycopy.ingestion.normalized_source_trade import SOURCE_NAME
from polycopy.scoring.paper_signal import persist_bridge_incomplete_paper_signal

MAX_LIMIT = 10
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
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    raise RuntimeError("PR25A synchronous bridge cannot run in an active event loop")


def _hydrate(
    gamma: GammaProvider, row: Any
) -> tuple[Market | None, Any | None, str | None]:
    condition, token, label = (
        str(row[key] or "").strip()
        for key in ("market_source_id", "token_id", "outcome")
    )
    if not condition or not token or not label:
        return None, None, "missing_condition_token_or_outcome"
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
        market, outcome, error = _hydrate(dependencies.gamma, row)
        detail["stages"]["gamma"] = "ok" if error is None else error
        if error:
            _record_skip(report, detail, error)
            continue
        if dependencies.clob is None:
            book, error = None, "no_book_provider"
        else:
            try:
                book = _await(dependencies.clob.fetch_book(str(row["token_id"])))
                detail["request_count"] = int(getattr(book, "request_attempts", 1))
            except Exception as exc:
                book, error = None, f"clob_error:{type(exc).__name__}"
            else:
                error = (
                    None
                    if book
                    and not getattr(book, "error_code", None)
                    and getattr(book, "bids", None)
                    and getattr(book, "asks", None)
                    else "clob_evidence_invalid"
                )
        detail["stages"]["clob_preflight"] = "ok" if error is None else error
        if error:
            _record_skip(report, detail, error)
            continue
        if not write:
            detail["actions"].append("would_write_allowlisted_evidence_only")
            report.rows.append(detail)
            continue
        wallet_id, error, wallet_new = _wallet(db, address)
        detail["stages"]["wallet"] = (
            "inserted" if wallet_new else "ok" if error is None else error
        )
        if error:
            detail["skip_reason"] = error
            report.rows.append(detail)
            continue
        assert wallet_id is not None
        market_id, outcome_id, error, market_new = _safe_persist_market(db, market, outcome)
        detail["stages"]["market_mapping"] = (
            "inserted" if market_new else "ok" if error is None else error
        )
        if error:
            detail["skip_reason"] = error
            report.rows.append(detail)
            continue
        assert market_id is not None and outcome_id is not None
        # Capture one shared ``now`` so the candidate's ``created_at`` is
        # identical to the snapshot's ``fetched_at``; otherwise the
        # deterministic paper-signal loader lookup
        # (``fetched_at <= candidate.created_at``) would miss the just-
        # inserted snapshot. Use ``_now_iso`` (seconds-precision) so the
        # string is bit-identical to ``snapshot_one``'s ``fetched_at``.
        now_iso = _now_iso(datetime.now(timezone.utc))
        candidate_id, candidate_new = persist_copy_candidate(
            db, _candidate(row, wallet_id, market_id, outcome_id, now_iso)
        )
        detail["stages"]["candidate"] = "inserted" if candidate_new else "replayed"

        class FetchedBook:
            async def fetch_book(self, token_id: str) -> Any:
                return book

        snapshot = snapshot_one(
            db,
            candidate_id=candidate_id,
            snapshot_run_id=f"pr25a:{row['id']}",
            book_provider=FetchedBook(),
            now=datetime.fromisoformat(now_iso.replace("Z", "+00:00")),
        )
        snapshot_id, snapshot_new = persist_price_snapshot(db, snapshot)
        persisted_snapshot = db.fetchone(
            "SELECT best_bid, best_bid_size, best_ask, best_ask_size, spread, "
            "trade_age_seconds, seconds_to_market_end, market_active_at_fetch, "
            "market_closed_at_fetch, market_resolved_at_fetch, expected_fill_price, "
            "fetched_at FROM candidate_price_snapshots WHERE id=?",
            (snapshot_id,),
        )
        assert persisted_snapshot is not None
        bids = [(level.price, level.size) for level in book.bids]
        asks = [(level.price, level.size) for level in book.asks]
        bid_n, ask_n, level_error = persist_depth_levels(db, snapshot_id, bids, asks)
        detail["stages"]["snapshot"] = "inserted" if snapshot_new else "replayed"
        detail["stages"]["levels"] = level_error or f"ok:{bid_n}/{ask_n}"
        detail["snapshot_id"] = snapshot_id
        detail["snapshot_token_id"] = snapshot.token_id
        if level_error:
            detail["skip_reason"] = level_error
            report.rows.append(detail)
            continue
        signal_id = persist_bridge_incomplete_paper_signal(db, candidate_id)
        detail["stages"]["paper"] = "persisted"
        detail["actions"].extend(["candidate", "snapshot", "depth_levels", "canonical_paper"])
        detail["paper_signal_id"] = signal_id
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
