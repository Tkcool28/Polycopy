"""PR24S — Trade Copyability snapshot/depth/current-price evidence bridge.

This module prepares the missing evidence path discovered by PR24R so Trade
Copyability v1 can eventually receive real market evidence instead of being
blocked after source-trade mapping. It is PURE and NON-PERSISTING:

  * It reads ``source_trades`` and the existing ``candidate_price_snapshots`` /
    ``candidate_price_snapshot_levels`` tables (if present) through a
    caller-supplied read-only ``sqlite3.Connection`` opened with ``mode=ro``.
  * It normalizes order-book / depth levels into evidence fields and computes
    estimated fill price / executable depth / fill percentage / spread by
    REUSING the existing ``polycopy.scoring.depth_normalization`` code
    (``normalize_book_levels``, ``compute_book_hash``, ``walk_depth``).
  * It produces a report object only. It never writes the production DB.

It must NOT:
  * write the production DB,
  * import ``polycopy.db.database`` (no ORM / no write path),
  * create candidates / paper signals,
  * place orders or call broker / CLOB order placement,
  * wire timers or automation,
  * mutate formula behavior.

Side canonicalization reuses the PR24R helper so the strict scorer stays
exactly as delivered. SELL remains unsupported for v1 and is never eligible.

An injectable evidence provider (``SnapshotEvidenceProvider``) lets tests feed
synthetic ask/bid levels without any network access. The default production
smoke is ``--offline-only`` and never fetches live market data.

Zero production snapshot-evidence-ready is EXPECTED when no real snapshot /
depth / current-price evidence exists. This is success, not failure.

All ``ready_*`` flags are ALWAYS ``False``:
  * ready_to_wire_to_automation
  * ready_to_persist_decisions
  * ready_to_create_candidates
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from polycopy.scoring.depth_normalization import (
    DEPTH_NOT_CAPTURED,
    compute_book_hash,
    normalize_book_levels,
    walk_depth,
)
from polycopy.engine.trade_copyability_bridge_audit import canonicalize_source_side

# Reused readiness thresholds from Trade Copyability v1 (read-only reference;
# PR24S does not change them, only reports against them).
MIN_COPY_CANDIDATE_FILL_PERCENTAGE = 0.80


# ── Counted tables (read-only; any that exist are reported) ────────────────
_COUNT_TABLES = (
    "source_trades",
    "trade_copyability_decisions",
    "copy_candidates",
    "paper_signal_decisions",
    "candidate_price_snapshots",
    "candidate_price_snapshot_levels",
    "wallet_score_decisions",
    "settlement_accounting_ledger",
    "orders",
    "positions",
)

SELL_UNSUPPORTED_REASON = "sell_side_copyability_not_supported_v1"


# ── Column discovery candidates (PR24R-aligned) ────────────────────────────
_SOURCE_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "source_trade_id": ("source_trade_id", "id"),
    "trader_address": ("trader_address", "wallet_address", "wallet_id"),
    "wallet_id": ("wallet_id", "wallet_address", "trader_address"),
    "market_id": ("market_id", "condition_id", "clob_token_id",
                  "token_id", "outcome_token_id"),
    "token_id": ("token_id", "outcome_token_id", "clob_token_id",
                 "condition_id", "market_id"),
    "side": ("side", "trade_side"),
    "price": ("price", "source_price", "entry_price", "avg_price"),
    "size": ("size", "shares", "amount", "notional", "usd_size", "stake", "quantity"),
    "timestamp": ("timestamp", "created_at", "traded_at", "source_trade_timestamp"),
}


# ── Injectable provider interface (no network by default) ──────────────────
class SnapshotEvidenceProvider:
    """Pure interface for obtaining order-book depth for a token.

    The default implementation is OFFLINE and returns no levels. Tests inject
    a subclass that returns synthetic ask/bid levels. Production smoke never
    calls a live market-data client unless ``--allow-live-preview`` is set AND
    a read-only client is already wired (out of scope for this PR).
    """

    def fetch_depth(
        self,
        *,
        token_id: Optional[str],
        side: str = "BUY",
    ) -> tuple[list[tuple[Any, Any]], list[tuple[Any, Any]]]:
        """Return (ask_levels, bid_levels) as (price, size) tuples.

        Default returns empty books (offline). Subclasses override.
        """
        return [], []


# ── Dataclasses (PART 3) ───────────────────────────────────────────────────
@dataclass(frozen=True)
class SnapshotEvidenceLevel:
    """A single normalized order-book level (audit view)."""

    price: float
    size: float
    side: str
    level_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "price": self.price,
            "size": self.size,
            "side": self.side,
            "level_index": self.level_index,
        }


@dataclass(frozen=True)
class SnapshotEvidenceResult:
    """Depth/fill/spread evidence derived from a snapshot (read-only)."""

    token_id: Optional[str]
    source_trade_id: Optional[str]
    source_entry_price: Optional[float]
    current_copy_price: Optional[float]
    estimated_fill_price: Optional[float]
    intended_stake: Optional[float]
    executable_depth: Optional[float]
    fill_percentage: Optional[float]
    spread: Optional[float]
    best_bid: Optional[float]
    best_ask: Optional[float]
    best_bid_size: Optional[float]
    best_ask_size: Optional[float]
    depth_hash: Optional[str]
    depth_status: Optional[str]
    depth_status_reason: Optional[str]
    price_snapshot_fetched_at: Optional[str]
    evaluation_timestamp: Optional[str]
    seconds_to_market_end: Optional[float]
    market_active: Optional[bool]
    market_closed: Optional[bool]
    market_resolved: Optional[bool]
    missing_evidence: tuple[str, ...]
    # Derived deterioration (only when both source + copy price present).
    price_deterioration_pct: Optional[float] = None
    # True when this result came from an existing persisted snapshot row.
    from_existing_snapshot: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "source_trade_id": self.source_trade_id,
            "source_entry_price": self.source_entry_price,
            "current_copy_price": self.current_copy_price,
            "estimated_fill_price": self.estimated_fill_price,
            "intended_stake": self.intended_stake,
            "executable_depth": self.executable_depth,
            "fill_percentage": self.fill_percentage,
            "spread": self.spread,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "best_bid_size": self.best_bid_size,
            "best_ask_size": self.best_ask_size,
            "depth_hash": self.depth_hash,
            "depth_status": self.depth_status,
            "depth_status_reason": self.depth_status_reason,
            "price_snapshot_fetched_at": self.price_snapshot_fetched_at,
            "evaluation_timestamp": self.evaluation_timestamp,
            "seconds_to_market_end": self.seconds_to_market_end,
            "market_active": self.market_active,
            "market_closed": self.market_closed,
            "market_resolved": self.market_resolved,
            "missing_evidence": list(self.missing_evidence),
            "price_deterioration_pct": self.price_deterioration_pct,
            "from_existing_snapshot": self.from_existing_snapshot,
        }


@dataclass(frozen=True)
class TradeCopyabilitySnapshotEvidenceRowAudit:
    """Per-source-trade snapshot-evidence row audit."""

    source_trade_id: Optional[str]
    raw_side: Optional[str]
    canonical_side: Optional[str]
    token_id: Optional[str]
    market_id: Optional[str]
    source_entry_price: Optional[float]
    source_size: Optional[float]
    source_timestamp: Optional[str]
    can_attempt_snapshot_evidence: bool
    can_build_snapshot_evidence: bool
    evidence_blocked_reasons: tuple[str, ...]
    snapshot_evidence: Optional[SnapshotEvidenceResult]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_trade_id": self.source_trade_id,
            "raw_side": self.raw_side,
            "canonical_side": self.canonical_side,
            "token_id": self.token_id,
            "market_id": self.market_id,
            "source_entry_price": self.source_entry_price,
            "source_size": self.source_size,
            "source_timestamp": self.source_timestamp,
            "can_attempt_snapshot_evidence": self.can_attempt_snapshot_evidence,
            "can_build_snapshot_evidence": self.can_build_snapshot_evidence,
            "evidence_blocked_reasons": list(self.evidence_blocked_reasons),
            "snapshot_evidence": (
                self.snapshot_evidence.to_dict() if self.snapshot_evidence else None
            ),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class TradeCopyabilitySnapshotEvidenceFinding:
    """A single snapshot-evidence bridge finding."""

    key: str
    severity: str  # "info" | "warning" | "blocker"
    summary: str
    count: Optional[int] = None
    evidence: dict[str, Any] = field(default_factory=dict)
    recommendation: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "severity": self.severity,
            "summary": self.summary,
            "count": self.count,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
        }


@dataclass(frozen=True)
class TradeCopyabilitySnapshotEvidenceBridgeReport:
    """Full PR24S snapshot-evidence bridge report (read-only / dry-run)."""

    ready_to_wire_to_automation: bool
    ready_to_persist_decisions: bool
    ready_to_create_candidates: bool
    production_counts: dict[str, Any]
    source_trade_count: int
    raw_side_distribution: dict[str, int]
    canonical_side_distribution: dict[str, int]
    ingestion_side_inconsistency_present: bool
    snapshot_candidate_count: int
    snapshot_evidence_ready_count: int
    snapshot_evidence_blocked_count: int
    blocked_reason_counts: dict[str, int]
    row_audits: tuple[TradeCopyabilitySnapshotEvidenceRowAudit, ...]
    findings: tuple[TradeCopyabilitySnapshotEvidenceFinding, ...]
    recommended_next_step: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready_to_wire_to_automation": self.ready_to_wire_to_automation,
            "ready_to_persist_decisions": self.ready_to_persist_decisions,
            "ready_to_create_candidates": self.ready_to_create_candidates,
            "production_counts": self.production_counts,
            "source_trade_count": self.source_trade_count,
            "raw_side_distribution": self.raw_side_distribution,
            "canonical_side_distribution": self.canonical_side_distribution,
            "ingestion_side_inconsistency_present": self.ingestion_side_inconsistency_present,
            "snapshot_candidate_count": self.snapshot_candidate_count,
            "snapshot_evidence_ready_count": self.snapshot_evidence_ready_count,
            "snapshot_evidence_blocked_count": self.snapshot_evidence_blocked_count,
            "blocked_reason_counts": self.blocked_reason_counts,
            "row_audits": [r.to_dict() for r in self.row_audits],
            "findings": [f.to_dict() for f in self.findings],
            "recommended_next_step": self.recommended_next_step,
        }


# ── DB helpers (read-only; caller owns the connection) ─────────────────────
def _conn(conn_or_db: Any) -> sqlite3.Connection:
    if isinstance(conn_or_db, sqlite3.Connection):
        return conn_or_db
    maybe_conn = getattr(conn_or_db, "conn", None)
    if isinstance(maybe_conn, sqlite3.Connection):
        return maybe_conn
    raise TypeError("conn_or_db must be a sqlite3.Connection or Database-like object")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name = ?",
        (table,),
    ).fetchone() is not None


def _safe_count(conn: sqlite3.Connection, table: str) -> Optional[int]:
    if not _table_exists(conn, table):
        return None
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return None


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def _maybe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_text(value: Any) -> str:
    return str(value or "").strip()


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if ts is None or not isinstance(ts, str) or ts.strip() == "":
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── Field discovery (PART 4) ───────────────────────────────────────────────
def _discover_columns(conn: sqlite3.Connection) -> set[str]:
    cols: set[str] = set()
    if _table_exists(conn, "source_trades"):
        for r in conn.execute("PRAGMA table_info(source_trades)"):
            cols.add(r["name"])
    return cols


def _resolve_field_map(conn: sqlite3.Connection) -> dict[str, Optional[str]]:
    cols = _discover_columns(conn)
    mapping: dict[str, Optional[str]] = {}
    for logical, candidates in _SOURCE_FIELD_CANDIDATES.items():
        chosen = next((c for c in candidates if c in cols), None)
        mapping[logical] = chosen
    return mapping


def _pick(row: sqlite3.Row, logical: str, field_map: dict[str, Optional[str]]) -> Any:
    col = field_map.get(logical)
    if col and col in row.keys():
        return row[col]
    # Fallback: try any candidate column present in the row.
    for candidate in _SOURCE_FIELD_CANDIDATES.get(logical, ()):
        if candidate in row.keys():
            return row[candidate]
    return None


# ── PART 5: depth / fill calculation (reuses existing normalization) ───────
def build_snapshot_evidence(
    *,
    token_id: Optional[str],
    source_trade_id: Optional[str],
    source_entry_price: Optional[float],
    intended_stake: Optional[float],
    ask_levels: list[tuple[Any, Any]],
    bid_levels: list[tuple[Any, Any]],
    price_snapshot_fetched_at: Optional[str] = None,
    evaluation_timestamp: Optional[str] = None,
    seconds_to_market_end: Optional[float] = None,
    market_active: Optional[bool] = None,
    market_closed: Optional[bool] = None,
    market_resolved: Optional[bool] = None,
    from_existing_snapshot: bool = False,
) -> SnapshotEvidenceResult:
    """Compute snapshot evidence from real ask/bid levels.

    Reuses ``normalize_book_levels`` / ``compute_book_hash`` / ``walk_depth``.
    BUY copyability walks the ASK side (asks ascending). Missing market-state
    / market-end inputs are passed through as None (never invented).
    """
    missing: list[str] = []

    norm_bids, norm_asks, norm_err = normalize_book_levels(bid_levels, ask_levels)
    if norm_err is not None:
        return SnapshotEvidenceResult(
            token_id=token_id,
            source_trade_id=source_trade_id,
            source_entry_price=source_entry_price,
            current_copy_price=None,
            estimated_fill_price=None,
            intended_stake=_maybe_float(intended_stake),
            executable_depth=None,
            fill_percentage=None,
            spread=None,
            best_bid=None,
            best_ask=None,
            best_bid_size=None,
            best_ask_size=None,
            depth_hash=None,
            depth_status="malformed",
            depth_status_reason=norm_err,
            price_snapshot_fetched_at=price_snapshot_fetched_at,
            evaluation_timestamp=evaluation_timestamp,
            seconds_to_market_end=_maybe_float(seconds_to_market_end),
            market_active=market_active,
            market_closed=market_closed,
            market_resolved=market_resolved,
            missing_evidence=tuple(missing),
            from_existing_snapshot=from_existing_snapshot,
        )

    if not norm_asks:
        return SnapshotEvidenceResult(
            token_id=token_id,
            source_trade_id=source_trade_id,
            source_entry_price=source_entry_price,
            current_copy_price=None,
            estimated_fill_price=None,
            intended_stake=_maybe_float(intended_stake),
            executable_depth=None,
            fill_percentage=None,
            spread=None,
            best_bid=float(norm_bids[0].price) if norm_bids else None,
            best_ask=None,
            best_bid_size=float(norm_bids[0].size) if norm_bids else None,
            best_ask_size=None,
            depth_hash=compute_book_hash(norm_bids, norm_asks),
            depth_status="no_ask_depth",
            depth_status_reason=DEPTH_NOT_CAPTURED,
            price_snapshot_fetched_at=price_snapshot_fetched_at,
            evaluation_timestamp=evaluation_timestamp,
            seconds_to_market_end=_maybe_float(seconds_to_market_end),
            market_active=market_active,
            market_closed=market_closed,
            market_resolved=market_resolved,
            missing_evidence=tuple(missing),
            from_existing_snapshot=from_existing_snapshot,
        )

    best_ask = float(norm_asks[0].price)
    best_ask_size = float(norm_asks[0].size)
    best_bid = float(norm_bids[0].price) if norm_bids else 0.0
    best_bid_size = float(norm_bids[0].size) if norm_bids else 0.0

    # Spread = (best_ask - best_bid); relative spread vs best_ask.
    spread = None
    if best_ask > 0 and norm_bids:
        spread = (best_ask - best_bid) / best_ask

    stake = _maybe_float(intended_stake)
    walk = walk_depth(norm_asks, "BUY", Decimal(str(stake)) if stake is not None else Decimal("0"))

    estimated_fill_price = float(walk.vwap_fill_price) if walk.vwap_fill_price is not None else None
    executable_depth = float(walk.filled_notional)
    fill_percentage = float(walk.fill_percentage) if walk.fill_percentage is not None else None

    depth_hash = compute_book_hash(norm_bids, norm_asks)
    depth_status = "complete" if walk.is_complete else "partial"
    depth_status_reason = walk.insufficient_reason  # None when complete

    # Derived price deterioration (BUY): (copy - entry) / entry.
    price_deterioration_pct = None
    if source_entry_price not in (None, 0.0) and estimated_fill_price is not None:
        price_deterioration_pct = (estimated_fill_price - source_entry_price) / source_entry_price

    return SnapshotEvidenceResult(
        token_id=token_id,
        source_trade_id=source_trade_id,
        source_entry_price=source_entry_price,
        current_copy_price=best_ask,
        estimated_fill_price=estimated_fill_price,
        intended_stake=stake,
        executable_depth=executable_depth,
        fill_percentage=fill_percentage,
        spread=spread,
        best_bid=best_bid if norm_bids else None,
        best_ask=best_ask,
        best_bid_size=best_bid_size if norm_bids else None,
        best_ask_size=best_ask_size,
        depth_hash=depth_hash,
        depth_status=depth_status,
        depth_status_reason=depth_status_reason,
        price_snapshot_fetched_at=price_snapshot_fetched_at,
        evaluation_timestamp=evaluation_timestamp,
        seconds_to_market_end=_maybe_float(seconds_to_market_end),
        market_active=market_active,
        market_closed=market_closed,
        market_resolved=market_resolved,
        missing_evidence=tuple(missing),
        price_deterioration_pct=price_deterioration_pct,
        from_existing_snapshot=from_existing_snapshot,
    )


# ── Report builder ─────────────────────────────────────────────────────────
def build_trade_copyability_snapshot_evidence_bridge(
    conn_or_db: Any,
    *,
    limit: int = 20,
    provider: Optional[SnapshotEvidenceProvider] = None,
) -> TradeCopyabilitySnapshotEvidenceBridgeReport:
    """Build a read-only Trade Copyability snapshot-evidence bridge report.

    ``conn_or_db`` must be an already-open ``sqlite3.Connection`` opened
    read-only (``mode=ro``). The function performs only SELECT / PRAGMA reads
    and never mutates the database.
    """
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if provider is None:
        provider = SnapshotEvidenceProvider()  # offline default

    conn = _conn(conn_or_db)
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        source_rows: list[sqlite3.Row] = []
        if _table_exists(conn, "source_trades"):
            source_rows = list(conn.execute("SELECT * FROM source_trades"))

        production_counts = {t: _safe_count(conn, t) for t in _COUNT_TABLES}
        field_map = _resolve_field_map(conn)

        # Bind the connection onto a lightweight accessor so _audit_row can
        # read existing snapshots read-only per token.
        def read_existing_snapshot_for_token(tok: Optional[str]) -> Optional[dict[str, Any]]:
            if tok in (None, ""):
                return None
            if not _table_exists(conn, "candidate_price_snapshots"):
                return None
            snap = conn.execute(
                "SELECT * FROM candidate_price_snapshots WHERE token_id = ? "
                "ORDER BY fetched_at DESC LIMIT 1",
                (tok,),
            ).fetchone()
            if snap is None:
                return None
            return {
                "fetched_at": _row_get(snap, "fetched_at"),
                "created_at": _row_get(snap, "created_at"),
                "seconds_to_market_end": _row_get(snap, "seconds_to_market_end"),
                "market_active_at_fetch": _row_get(snap, "market_active_at_fetch"),
                "market_closed_at_fetch": _row_get(snap, "market_closed_at_fetch"),
                "market_resolved_at_fetch": _row_get(snap, "market_resolved_at_fetch"),
            }

        row_audits: list[TradeCopyabilitySnapshotEvidenceRowAudit] = []
        for row in source_rows:
            ra = _audit_row_with_existing(
                row, field_map, provider, read_existing_snapshot_for_token
            )
            row_audits.append(ra)

        # --- Distributions ---
        raw_side_distribution: Counter[str] = Counter()
        canonical_side_distribution: Counter[str] = Counter()
        for ra in row_audits:
            key = "<NULL>" if ra.raw_side is None else str(ra.raw_side)
            raw_side_distribution[key] += 1
            if ra.canonical_side is not None:
                canonical_side_distribution[ra.canonical_side] += 1

        # --- Ingestion side inconsistency (PART 6) ---
        buy_forms = sorted(k for k in raw_side_distribution if k.lower() == "buy")
        sell_forms = sorted(k for k in raw_side_distribution if k.lower() == "sell")
        ingestion_inconsistent = len(buy_forms) > 1 or len(sell_forms) > 1

        # --- Readiness counts ---
        attemptable = sum(1 for r in row_audits if r.can_attempt_snapshot_evidence)
        ready = sum(1 for r in row_audits if r.can_build_snapshot_evidence)
        blocked = sum(1 for r in row_audits if not r.can_build_snapshot_evidence)
        blocked_reason_counts: Counter[str] = Counter()
        for r in row_audits:
            for reason in r.evidence_blocked_reasons:
                blocked_reason_counts[reason] += 1

        # --- Findings ---
        findings: list[TradeCopyabilitySnapshotEvidenceFinding] = []
        if ingestion_inconsistent:
            findings.append(TradeCopyabilitySnapshotEvidenceFinding(
                key="ingestion_side_inconsistency",
                severity="warning",
                summary=(
                    "source_trades.side contains multiple exact string forms for "
                    "the same logical side. This may indicate inconsistent "
                    "ingestion normalization or multiple writer paths."
                ),
                count=sum(raw_side_distribution.get(k, 0) for k in buy_forms + sell_forms),
                evidence={
                    "raw_side_distribution": dict(raw_side_distribution),
                    "canonical_side_distribution": dict(canonical_side_distribution),
                    "affected_logical_sides": (
                        (["BUY"] if len(buy_forms) > 1 else [])
                        + (["SELL"] if len(sell_forms) > 1 else [])
                    ),
                },
                recommendation=(
                    "Investigate source_trades.side writer paths and decide whether "
                    "side should be normalized at ingestion. The bridge canonicalizes "
                    "defensively, but this inconsistency should not be silently patched "
                    "over forever."
                ),
            ))

        if blocked_reason_counts:
            findings.append(TradeCopyabilitySnapshotEvidenceFinding(
                key="snapshot_evidence_blocked_reason_summary",
                severity="info",
                summary=(
                    "Snapshot-evidence blocked reasons across source_trades (expected "
                    "gaps in token identity / existing snapshot / depth / current-price / "
                    "market-state evidence)."
                ),
                count=sum(blocked_reason_counts.values()),
                evidence={"blocked_reason_counts": dict(blocked_reason_counts)},
                recommendation=(
                    "PR24T should add or verify real snapshot/depth/current-price "
                    "collection before persisted dry-run decisions."
                ),
            ))

        # Severe partial fill finding (read-only signal).
        severe_partial = []
        for r in row_audits:
            ev = r.snapshot_evidence
            if ev is None or ev.fill_percentage is None:
                continue
            if ev.fill_percentage < MIN_COPY_CANDIDATE_FILL_PERCENTAGE:
                severe_partial.append(r)
        if severe_partial:
            findings.append(TradeCopyabilitySnapshotEvidenceFinding(
                key="partial_fill_below_copy_candidate_threshold",
                severity="info",
                summary=(
                    "One or more snapshot-evidence results show fill_percentage < 0.80; "
                    "these must not become copy_candidate in the future scoring path."
                ),
                count=len(severe_partial),
                evidence={
                    "rows": [r.source_trade_id for r in severe_partial],
                    "fill_percentages": [
                        r.snapshot_evidence.fill_percentage for r in severe_partial
                    ],
                },
            ))

        recommended_next_step = _recommended_next_step(ready)

        return TradeCopyabilitySnapshotEvidenceBridgeReport(
            ready_to_wire_to_automation=False,
            ready_to_persist_decisions=False,
            ready_to_create_candidates=False,
            production_counts=production_counts,
            source_trade_count=len(row_audits),
            raw_side_distribution=dict(raw_side_distribution),
            canonical_side_distribution=dict(canonical_side_distribution),
            ingestion_side_inconsistency_present=ingestion_inconsistent,
            snapshot_candidate_count=attemptable,
            snapshot_evidence_ready_count=ready,
            snapshot_evidence_blocked_count=blocked,
            blocked_reason_counts=dict(blocked_reason_counts),
            row_audits=tuple(row_audits[: max(limit, 0)] or row_audits),
            findings=tuple(findings),
            recommended_next_step=recommended_next_step,
        )
    finally:
        conn.row_factory = old_factory


def _audit_row_with_existing(
    row: sqlite3.Row,
    field_map: dict[str, Optional[str]],
    provider: SnapshotEvidenceProvider,
    read_existing_snapshot_for_token,
) -> TradeCopyabilitySnapshotEvidenceRowAudit:
    """_audit_row variant that reads existing snapshots read-only per token."""
    raw_side = _pick(row, "side", field_map)
    canonical_side, side_status, side_reason = canonicalize_source_side(raw_side)
    source_trade_id = _pick(row, "source_trade_id", field_map)
    market_id = _pick(row, "market_id", field_map)
    token_id = _pick(row, "token_id", field_map)
    source_price = _maybe_float(_pick(row, "price", field_map))
    source_size = _maybe_float(_pick(row, "size", field_map))
    source_timestamp = _pick(row, "timestamp", field_map)

    notes: list[str] = []
    blocked: list[str] = []
    snapshot_evidence: Optional[SnapshotEvidenceResult] = None

    can_attempt = True
    if canonical_side != "BUY":
        can_attempt = False
        if side_reason == SELL_UNSUPPORTED_REASON:
            blocked.append(SELL_UNSUPPORTED_REASON)
        elif side_status == "missing":
            blocked.append("missing_side")
        else:
            blocked.append("invalid_side")
    if source_trade_id in (None, ""):
        can_attempt = False
        blocked.append("missing_source_trade_id")
    if token_id in (None, ""):
        can_attempt = False
        blocked.append("missing_token_id")
    if source_price is None:
        can_attempt = False
        blocked.append("missing_source_entry_price")
    if source_size is None:
        can_attempt = False
        blocked.append("missing_source_size")

    can_build = can_attempt
    if can_attempt:
        ask_levels, bid_levels = provider.fetch_depth(token_id=token_id, side="BUY")
        has_depth = bool(ask_levels) or bool(bid_levels)
        if not has_depth:
            can_build = False
            blocked.append("missing_depth_levels")
            notes.append("no order-book/depth levels available from provider")
        existing = read_existing_snapshot_for_token(token_id)
        if existing is None:
            can_build = False
            blocked.append("missing_existing_price_snapshot")
            notes.append("no existing candidate_price_snapshots row for this token")
        else:
            assert existing is not None
            secs = existing.get("seconds_to_market_end")
            m_active = existing.get("market_active_at_fetch")
            m_closed = existing.get("market_closed_at_fetch")
            m_resolved = existing.get("market_resolved_at_fetch")
            if secs is None:
                can_build = False
                blocked.append("missing_seconds_to_market_end")
            if m_active is None and m_closed is None and m_resolved is None:
                can_build = False
                blocked.append("missing_market_state")
        if can_build:
            snapshot_evidence = build_snapshot_evidence(
                token_id=token_id,
                source_trade_id=source_trade_id,
                source_entry_price=source_price,
                intended_stake=source_size,
                ask_levels=ask_levels,
                bid_levels=bid_levels,
                price_snapshot_fetched_at=existing.get("fetched_at"),
                evaluation_timestamp=existing.get("created_at"),
                seconds_to_market_end=secs,
                market_active=bool(m_active) if m_active is not None else None,
                market_closed=bool(m_closed) if m_closed is not None else None,
                market_resolved=bool(m_resolved) if m_resolved is not None else None,
                from_existing_snapshot=True,
            )
            if (snapshot_evidence.fill_percentage is not None
                    and snapshot_evidence.fill_percentage < MIN_COPY_CANDIDATE_FILL_PERCENTAGE):
                blocked.append("partial_fill_below_copy_candidate_threshold")
                notes.append("partial fill below 80% copy-candidate threshold (future scoring blocker)")
    else:
        if canonical_side == "SELL":
            notes.append("SELL not eligible for v1 snapshot evidence.")
        elif "missing_token_id" in blocked:
            notes.append("row cannot attempt snapshot evidence without token_id")

    return TradeCopyabilitySnapshotEvidenceRowAudit(
        source_trade_id=source_trade_id if source_trade_id not in (None, "") else None,
        raw_side=raw_side if raw_side != "" else None,
        canonical_side=canonical_side,
        token_id=token_id if token_id not in (None, "") else None,
        market_id=market_id if market_id not in (None, "") else None,
        source_entry_price=source_price,
        source_size=source_size,
        source_timestamp=source_timestamp if source_timestamp not in (None, "") else None,
        can_attempt_snapshot_evidence=can_attempt,
        can_build_snapshot_evidence=can_build,
        evidence_blocked_reasons=tuple(sorted(set(blocked))),
        snapshot_evidence=snapshot_evidence,
        notes=tuple(notes),
    )


def _recommended_next_step(ready: int) -> str:
    parts: list[str] = []
    if ready == 0:
        parts.append(
            "PR24T should add or verify real snapshot/depth/current-price "
            "collection before persisted dry-run decisions."
        )
    else:
        parts.append(
            "PR24T should add a guarded persisted dry-run decision writer after review."
        )
    parts.append("Do not wire automation until persisted dry-run decisions are reviewed.")
    return " ".join(parts)


# ── Human rendering ─────────────────────────────────────────────────────────
def report_to_human(report: TradeCopyabilitySnapshotEvidenceBridgeReport) -> str:
    lines: list[str] = []
    lines.append("TRADE COPYABILITY SNAPSHOT EVIDENCE BRIDGE — READ ONLY / DRY RUN")
    lines.append("")
    lines.append(f"ready_to_wire_to_automation = {report.ready_to_wire_to_automation}")
    lines.append(f"ready_to_persist_decisions = {report.ready_to_persist_decisions}")
    lines.append(f"ready_to_create_candidates = {report.ready_to_create_candidates}")
    lines.append("")

    lines.append("== Production counts ==")
    for k, v in report.production_counts.items():
        lines.append(f"  {k}: {v}")
    lines.append("")

    lines.append(f"== Source trades inspected: {report.source_trade_count} ==")
    lines.append("")
    lines.append("== Raw source side distribution (exact casing) ==")
    for k, v in sorted(report.raw_side_distribution.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("== Canonical side distribution ==")
    if report.canonical_side_distribution:
        for k, v in sorted(report.canonical_side_distribution.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("== INGESTION SIDE NORMALIZATION AUDIT ==")
    lines.append(f"  ingestion_side_inconsistency_present = {report.ingestion_side_inconsistency_present}")
    if report.ingestion_side_inconsistency_present:
        lines.append("  WARNING: source_trades.side has mixed casing for the same logical side.")
        lines.append("  Recommendation: investigate source_trades.side writer paths before")
        lines.append("  changing ingestion normalization.")
    lines.append("")

    lines.append("== Snapshot evidence readiness ==")
    lines.append(f"  snapshot_candidate_count (can_attempt): {report.snapshot_candidate_count}")
    lines.append(f"  snapshot_evidence_ready_count: {report.snapshot_evidence_ready_count}")
    lines.append(f"  snapshot_evidence_blocked_count: {report.snapshot_evidence_blocked_count}")
    lines.append("")
    lines.append("== Top blocked reasons ==")
    if report.blocked_reason_counts:
        for k, v in sorted(report.blocked_reason_counts.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("== Findings ==")
    if report.findings:
        for f in report.findings:
            lines.append(f"  [{f.severity}] {f.key}: {f.summary}")
            if f.count is not None:
                lines.append(f"    count={f.count}")
            if f.evidence:
                lines.append(f"    evidence={json.dumps(f.evidence, sort_keys=True)}")
            if f.recommendation:
                lines.append(f"    -> {f.recommendation}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("== Row sample (limited) ==")
    for ra in report.row_audits:
        lines.append(f"  source_trade_id={ra.source_trade_id} raw_side={ra.raw_side} "
                     f"canonical_side={ra.canonical_side} token_id={ra.token_id}")
        lines.append(f"    can_attempt={ra.can_attempt_snapshot_evidence} "
                     f"can_build={ra.can_build_snapshot_evidence}")
        if ra.evidence_blocked_reasons:
            lines.append(f"    blocked={list(ra.evidence_blocked_reasons)}")
        if ra.snapshot_evidence is not None:
            ev = ra.snapshot_evidence
            lines.append(f"    evidence: cur_copy={ev.current_copy_price} "
                         f"est_fill={ev.estimated_fill_price} exec_depth={ev.executable_depth} "
                         f"fill_pct={ev.fill_percentage} spread={ev.spread} "
                         f"depth_hash={ev.depth_hash}")
        for n in ra.notes:
            lines.append(f"    note: {n}")
    lines.append("")

    lines.append("== Recommended next step ==")
    lines.append(f"  {report.recommended_next_step}")
    lines.append("")
    lines.append(
        "This report does not persist snapshots, decisions, candidates, paper "
        "signals, or orders."
    )
    return "\n".join(lines)


def report_to_dict(report: TradeCopyabilitySnapshotEvidenceBridgeReport) -> dict[str, Any]:
    return report.to_dict()


__all__ = [
    "SnapshotEvidenceLevel",
    "SnapshotEvidenceResult",
    "SnapshotEvidenceProvider",
    "TradeCopyabilitySnapshotEvidenceRowAudit",
    "TradeCopyabilitySnapshotEvidenceFinding",
    "TradeCopyabilitySnapshotEvidenceBridgeReport",
    "build_snapshot_evidence",
    "build_trade_copyability_snapshot_evidence_bridge",
    "report_to_human",
    "report_to_dict",
]
