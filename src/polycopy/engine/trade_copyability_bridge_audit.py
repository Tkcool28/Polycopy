"""PR24R — Trade Copyability bridge audit (read-only / dry-run only).

This module proves how ``source_trades`` will be transformed into a
``TradeCopyabilityInputV1``-compatible audit input BEFORE any real
wiring. It is PURE and READ-ONLY:

  * It imports ONLY ``sqlite3`` (caller-supplied connection), dataclasses,
    json, datetime helpers, and the strict Trade Copyability v1 scorer
    primitives (``TradeCopyabilityInputV1``, ``compute_trade_score_v1``,
    ``calculate_buy_price_deterioration_pct``).
  * It does NOT import ``polycopy.db.database`` (no ORM / no write path).
  * It does NOT import broker, order placement, automation runners, paper
    signal creators, or copy candidate creators.
  * It never issues INSERT/UPDATE/DELETE/CREATE/DROP/ALTER.
  * It never mutates the database. The caller owns the connection (opened
    with ``mode=ro``) and the bridge only performs SELECT / PRAGMA reads.

The bridge CANONICALIZES known raw source side casing (buy/BUY -> BUY,
sell/SELL -> SELL) so the downstream strict scorer stays exactly as PR24P
delivered it (no side normalization inside the scorer). SELL remains
UNSUPPORTED for v1 and must never become eligible.

Two readiness booleans are deliberately separated (CORRECTION 2):

  * ``can_build_input`` — True only when the bridge can map the raw source
    row into a real ``TradeCopyabilityInputV1`` audit shell using real
    source data, without inventing values and WITHOUT requiring
    snapshot/depth/current-price evidence.
  * ``can_compute_score`` — True only when ``can_build_input`` is True AND
    enough real current-market / snapshot evidence exists for a meaningful
    dry-run score attempt.

``bridge_ready_count`` is defined explicitly as ``build_input_ready_count``
(can_build_input=True). It does NOT mean the row is scoreable.

Zero production score attempts is EXPECTED when current-price / depth /
fill / timing evidence is unavailable. The dry-run scoring path is
validated by synthetic_test_only unit coverage (CORRECTION 4).

``ready_to_wire_to_automation`` is ALWAYS ``False``.
``ready_to_persist_decisions`` is ALWAYS ``False``.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from polycopy.scoring.trade_score_v1 import (
    TradeCopyabilityInputV1,
    compute_trade_score_v1,
)


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

# SELL is known but unsupported for v1.
SELL_UNSUPPORTED_REASON = "sell_side_copyability_not_supported_v1"

# Required current-market / snapshot evidence for can_compute_score=True.
# These are the bridge-level evidence gaps that block a dry-run score when
# the source row could otherwise be mapped. They are kept stable and
# documented (PART 8 / CORRECTION 4).
_SCORE_EVIDENCE_FIELDS = (
    "current_copy_price",
    "estimated_fill_price",
    "price_deterioration_pct",
    "intended_stake",
    "executable_depth",
    "fill_percentage",
    "spread",
    "seconds_to_market_end",
    "market_active",
    "market_closed",
    "market_resolved",
    "price_snapshot_fetched_at",
    "evaluation_timestamp",
)

# Human-readable blocker token per missing score-evidence field.
_SCORE_EVIDENCE_BLOCKER = {
    "current_copy_price": "missing_current_copy_price",
    "estimated_fill_price": "missing_estimated_fill_price",
    "price_deterioration_pct": "missing_price_deterioration_evidence",
    "intended_stake": "missing_intended_stake",
    "executable_depth": "missing_depth_snapshot",
    "fill_percentage": "missing_fill_percentage",
    "spread": "missing_spread",
    "seconds_to_market_end": "missing_seconds_to_market_end",
    "market_active": "missing_market_state",
    "market_closed": "missing_market_state",
    "market_resolved": "missing_market_state",
    "price_snapshot_fetched_at": "missing_price_snapshot_timing",
    "evaluation_timestamp": "missing_price_snapshot_timing",
}


# ── Column discovery candidates (PART 4) ───────────────────────────────────
# (logical field -> list of accepted column names, in priority order)
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
    "timestamp": ("timestamp", "created_at", "traded_at",
                  "source_trade_timestamp"),
    "market_end": ("end_timestamp", "market_end_timestamp", "close_time",
                   "end_date"),
}


# ── Dataclasses (PART 3) ────────────────────────────────────────────────────
@dataclass(frozen=True)
class TradeCopyabilityBridgeRowAudit:
    """Per-source-trade bridge audit row."""

    source_trade_id: Optional[str]
    trader_address: Optional[str]
    wallet_id: Optional[str]
    market_id: Optional[str]
    token_id: Optional[str]
    raw_side: Optional[str]
    canonical_side: Optional[str]
    side_status: str
    source_price: Optional[float]
    source_size: Optional[float]
    source_timestamp: Optional[str]
    market_end_timestamp: Optional[str]
    can_build_input: bool
    can_compute_score: bool
    bridge_blocked_reasons: tuple[str, ...]
    missing_input_fields: tuple[str, ...]
    dry_run_verdict: Optional[str]
    dry_run_score: Optional[float]
    dry_run_missing_essentials: tuple[str, ...]
    dry_run_rejection_reasons: tuple[str, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_trade_id": self.source_trade_id,
            "trader_address": self.trader_address,
            "wallet_id": self.wallet_id,
            "market_id": self.market_id,
            "token_id": self.token_id,
            "raw_side": self.raw_side,
            "canonical_side": self.canonical_side,
            "side_status": self.side_status,
            "source_price": self.source_price,
            "source_size": self.source_size,
            "source_timestamp": self.source_timestamp,
            "market_end_timestamp": self.market_end_timestamp,
            "can_build_input": self.can_build_input,
            "can_compute_score": self.can_compute_score,
            "bridge_blocked_reasons": list(self.bridge_blocked_reasons),
            "missing_input_fields": list(self.missing_input_fields),
            "dry_run_verdict": self.dry_run_verdict,
            "dry_run_score": self.dry_run_score,
            "dry_run_missing_essentials": list(self.dry_run_missing_essentials),
            "dry_run_rejection_reasons": list(self.dry_run_rejection_reasons),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class TradeCopyabilityBridgeAuditFinding:
    """A single bridge audit finding."""

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
class TradeCopyabilityBridgeAuditReport:
    """Full PR24R bridge audit report (read-only / dry-run)."""

    ready_to_wire_to_automation: bool
    ready_to_persist_decisions: bool
    production_counts: dict[str, Any]
    source_trade_count: int
    raw_side_distribution: dict[str, int]
    canonical_side_distribution: dict[str, int]
    side_canonicalization_counts: dict[str, int]
    build_input_ready_count: int
    compute_score_ready_count: int
    bridge_ready_count: int
    bridge_blocked_count: int
    bridge_blocked_reason_counts: dict[str, int]
    score_attempt_count: int
    dry_run_verdict_counts: dict[str, int]
    dry_run_rejection_reason_counts: dict[str, int]
    dry_run_missing_essential_counts: dict[str, int]
    findings: tuple[TradeCopyabilityBridgeAuditFinding, ...]
    row_audits: tuple[TradeCopyabilityBridgeRowAudit, ...]
    recommended_next_step: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready_to_wire_to_automation": self.ready_to_wire_to_automation,
            "ready_to_persist_decisions": self.ready_to_persist_decisions,
            "production_counts": self.production_counts,
            "source_trade_count": self.source_trade_count,
            "raw_side_distribution": self.raw_side_distribution,
            "canonical_side_distribution": self.canonical_side_distribution,
            "side_canonicalization_counts": self.side_canonicalization_counts,
            "build_input_ready_count": self.build_input_ready_count,
            "compute_score_ready_count": self.compute_score_ready_count,
            "bridge_ready_count": self.bridge_ready_count,
            "bridge_blocked_count": self.bridge_blocked_count,
            "bridge_blocked_reason_counts": self.bridge_blocked_reason_counts,
            "score_attempt_count": self.score_attempt_count,
            "dry_run_verdict_counts": self.dry_run_verdict_counts,
            "dry_run_rejection_reason_counts": self.dry_run_rejection_reason_counts,
            "dry_run_missing_essential_counts": self.dry_run_missing_essential_counts,
            "findings": [f.to_dict() for f in self.findings],
            "row_audits": [r.to_dict() for r in self.row_audits],
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
    if key in row.keys():
        return row[key]
    return default


def _maybe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_text(value: Any) -> str:
    return str(value or "").strip()


# ── PART 5: side canonicalization ──────────────────────────────────────────
def canonicalize_source_side(raw_side: Any) -> tuple[Optional[str], str, Optional[str]]:
    """Canonicalize a raw source side value (defensive; scorer stays strict).

    Returns ``(canonical_side, side_status, blocked_reason)``.

      * None or blank -> (None, "missing", "missing_side")
      * "buy"/"BUY" (case-insensitive exact) -> ("BUY", "canonicalized_buy", None)
      * "sell"/"SELL" (case-insensitive exact) ->
          ("SELL", "canonicalized_sell_unsupported_v1",
           "sell_side_copyability_not_supported_v1")
      * anything else -> (None, "invalid", "invalid_side")

    The bridge may canonicalize buy/BUY before the scorer. SELL remains
    unsupported for v1 and must not become eligible. Malformed side must
    not be bridge-ready.
    """
    raw = _norm_text(raw_side)
    if raw == "":
        return None, "missing", "missing_side"
    if raw.lower() == "buy":
        return "BUY", "canonicalized_buy", None
    if raw.lower() == "sell":
        return ("SELL", "canonicalized_sell_unsupported_v1",
                SELL_UNSUPPORTED_REASON)
    return None, "invalid", "invalid_side"


# ── PART 4 + PART 6: extract + readiness decision ──────────────────────────
def _discover_columns(conn: sqlite3.Connection) -> set[str]:
    cols: set[str] = set()
    if _table_exists(conn, "source_trades"):
        for r in conn.execute("PRAGMA table_info(source_trades)"):
            cols.add(r["name"])
    return cols


def _pick(row: sqlite3.Row, logical: str) -> Any:
    for candidate in _SOURCE_FIELD_CANDIDATES.get(logical, ()):
        if candidate in row.keys():
            return row[candidate]
    return None


def _resolve_field_map(conn: sqlite3.Connection) -> dict[str, Optional[str]]:
    """Map each logical field to the actual column present (or None)."""
    cols = _discover_columns(conn)
    mapping: dict[str, Optional[str]] = {}
    for logical, candidates in _SOURCE_FIELD_CANDIDATES.items():
        chosen = next((c for c in candidates if c in cols), None)
        mapping[logical] = chosen
    return mapping


def _audit_row(row: sqlite3.Row, field_map: dict[str, Optional[str]]) -> TradeCopyabilityBridgeRowAudit:
    """Build a per-row bridge audit (read-only). May attempt a dry-run score."""
    raw_side = _pick(row, "side")
    canonical_side, side_status, side_reason = canonicalize_source_side(raw_side)
    source_trade_id = _pick(row, "source_trade_id")
    trader_address = _pick(row, "trader_address")
    wallet_id = _pick(row, "wallet_id")
    market_id = _pick(row, "market_id")
    token_id = _pick(row, "token_id")
    source_price = _maybe_float(_pick(row, "price"))
    source_size = _maybe_float(_pick(row, "size"))
    source_timestamp = _pick(row, "timestamp")
    market_end = _pick(row, "market_end")

    notes: list[str] = []
    missing_input_fields: list[str] = []
    bridge_blocked_reasons: list[str] = []

    # --- Minimum evidence for can_build_input=True (CORRECTION 2) ---
    if source_trade_id in (None, ""):
        missing_input_fields.append("source_trade_id")
    if canonical_side != "BUY":
        # SELL unsupported, missing, or invalid -> cannot build eligible input.
        if side_reason == SELL_UNSUPPORTED_REASON:
            bridge_blocked_reasons.append(SELL_UNSUPPORTED_REASON)
        elif side_status == "missing":
            bridge_blocked_reasons.append("missing_side")
        else:
            bridge_blocked_reasons.append("invalid_side")
    trader_identity = trader_address or wallet_id
    if trader_identity in (None, ""):
        missing_input_fields.append("trader_identity")
    market_identity = market_id or token_id
    if market_identity in (None, ""):
        missing_input_fields.append("market_identity")
    if source_price is None:
        missing_input_fields.append("source_entry_price")
    if source_size is None:
        missing_input_fields.append("source_size")
    # Timestamp is required if the source_trades schema exposes one.
    if "timestamp" in field_map and field_map.get("timestamp") is not None:
        if source_timestamp in (None, ""):
            missing_input_fields.append("source_trade_timestamp")

    can_build_input = (
        canonical_side == "BUY"
        and source_trade_id not in (None, "")
        and trader_identity not in (None, "")
        and market_identity not in (None, "")
        and source_price is not None
        and source_size is not None
        and ("timestamp" not in field_map
             or field_map.get("timestamp") is None
             or source_timestamp not in (None, ""))
    )

    # --- Score-evidence gap analysis (CORRECTION 2 / PART 6 / PART 8) ---
    # The bridge deliberately does NOT synthesize snapshot/depth/current-price
    # evidence. It reports the gap as blocked reasons.
    score_gap_blockers: list[str] = []
    if can_build_input:
        # These are the additional required current-market/snapshot evidence.
        # We check the actual source_trades row for any real columns that map
        # to these logical fields; absence means the runtime bridge has not
        # collected them yet.
        score_evidence_present = _score_evidence_present_in_row(row, field_map)
        for field_name in _SCORE_EVIDENCE_FIELDS:
            if not score_evidence_present.get(field_name, False):
                blocker = _SCORE_EVIDENCE_BLOCKER.get(
                    field_name, f"missing_{field_name}"
                )
                if blocker not in score_gap_blockers:
                    score_gap_blockers.append(blocker)
        can_compute_score = len(score_gap_blockers) == 0
    else:
        can_compute_score = False

    bridge_blocked_reasons.extend(score_gap_blockers)

    # --- Dry-run scoring (PART 7) ---
    dry_run_verdict: Optional[str] = None
    dry_run_score: Optional[float] = None
    dry_run_missing: list[str] = []
    dry_run_rejection: list[str] = []

    if can_compute_score:
        # can_compute_score=True implies all of these are non-None (guaranteed
        # by the can_build_input gate above). Cast defensively for the type
        # checker; the audit never synthesizes into these.
        assert source_trade_id is not None
        assert trader_identity is not None
        assert canonical_side is not None
        assert source_price is not None
        assert source_size is not None
        try:
            # Build the typed audit input strictly from real source fields.
            audit_input = _build_audit_input(
                row=row,
                field_map=field_map,
                source_trade_id=source_trade_id,
                trader_identity=trader_identity,
                market_identity=market_identity,
                token_id=token_id,
                canonical_side=canonical_side,
                source_price=source_price,
                source_size=source_size,
                source_timestamp=source_timestamp,
            )
            result = compute_trade_score_v1(input=audit_input)
            dry_run_verdict = result.verdict.value if result.verdict else None
            dry_run_score = result.score
            dry_run_missing = list(result.missing_essentials)
            dry_run_rejection = list(result.rejection_reasons)
            if dry_run_rejection:
                notes.append(
                    "dry-run score attempted; strict scorer returned "
                    f"rejections={dry_run_rejection}"
                )
        except Exception as exc:  # pragma: no cover - defensive
            notes.append(f"dry-run score attempt raised: {exc!r}")
            bridge_blocked_reasons.append("dry_run_score_error")
    else:
        if side_status == "canonicalized_sell_unsupported_v1":
            notes.append(
                "SELL blocked before scoring; v1 does not support SELL copyability."
            )
        elif canonical_side != "BUY":
            notes.append("side not BUY-eligible; no dry-run score attempted.")
        else:
            notes.append(
                "no dry-run score attempted; required current-market/snapshot "
                "evidence unavailable in source_trades."
            )

    return TradeCopyabilityBridgeRowAudit(
        source_trade_id=source_trade_id if source_trade_id not in (None, "") else None,
        trader_address=trader_address if trader_address not in (None, "") else None,
        wallet_id=wallet_id if wallet_id not in (None, "") else None,
        market_id=market_id if market_id not in (None, "") else None,
        token_id=token_id if token_id not in (None, "") else None,
        raw_side=raw_side if raw_side != "" else None,
        canonical_side=canonical_side,
        side_status=side_status,
        source_price=source_price,
        source_size=source_size,
        source_timestamp=source_timestamp if source_timestamp not in (None, "") else None,
        market_end_timestamp=market_end if market_end not in (None, "") else None,
        can_build_input=can_build_input,
        can_compute_score=can_compute_score,
        bridge_blocked_reasons=tuple(sorted(set(bridge_blocked_reasons))),
        missing_input_fields=tuple(sorted(set(missing_input_fields))),
        dry_run_verdict=dry_run_verdict,
        dry_run_score=dry_run_score,
        dry_run_missing_essentials=tuple(dry_run_missing),
        dry_run_rejection_reasons=tuple(dry_run_rejection),
        notes=tuple(notes),
    )


def _score_evidence_present_in_row(
    row: sqlite3.Row, field_map: dict[str, Optional[str]]
) -> dict[str, bool]:
    """Detect which current-market/snapshot evidence exists in the row.

    source_trades almost never carries these columns today; this mapping
    is forward-compatible so a future collection bridge is audited correctly.
    We never synthesize missing values.
    """
    def has(*names: str) -> bool:
        for n in names:
            if n in row.keys() and _row_get(row, n) not in (None, ""):
                return True
        return False

    return {
        "current_copy_price": has("current_copy_price", "current_price"),
        "estimated_fill_price": has("estimated_fill_price", "estimated_fill"),
        "price_deterioration_pct": has("price_deterioration_pct"),
        "intended_stake": has("intended_stake", "intended_notional"),
        "executable_depth": has("executable_depth"),
        "fill_percentage": has("fill_percentage"),
        "spread": has("spread", "best_bid_size", "best_ask_size"),
        "seconds_to_market_end": has(
            "seconds_to_market_end", "market_end_timestamp", "end_timestamp",
            "close_time", "end_date",
        ),
        "market_active": has("market_active"),
        "market_closed": has("market_closed"),
        "market_resolved": has("market_resolved"),
        "price_snapshot_fetched_at": has("price_snapshot_fetched_at", "snapshot_fetched_at"),
        "evaluation_timestamp": has("evaluation_timestamp"),
    }


def _build_audit_input(
    *,
    row: sqlite3.Row,
    field_map: dict[str, Optional[str]],
    source_trade_id: str,
    trader_identity: str,
    market_identity: Optional[str],
    token_id: Optional[str],
    canonical_side: str,
    source_price: float,
    source_size: float,
    source_timestamp: Optional[str],
) -> TradeCopyabilityInputV1:
    """Construct a TradeCopyabilityInputV1 strictly from real source fields.

    This only runs when can_compute_score=True. We map any present
    current-market/snapshot evidence 1:1 to the typed input. We do NOT
    invent missing values — missing evidence means can_compute_score was
    False and we never reach here.
    """
    def f(*names: str) -> Any:
        for n in names:
            if n in row.keys() and _row_get(row, n) not in (None, ""):
                return _row_get(row, n)
        return None

    def f_float(*names: str) -> Optional[float]:
        return _maybe_float(f(*names))

    wal_id = (trader_identity or market_identity
              or source_trade_id or "synthetic_wallet_do_not_use")
    return TradeCopyabilityInputV1(
        wallet_id=wal_id,
        source_trade_id=source_trade_id,
        side=canonical_side,
        # Price trace (optional; present only when real)
        source_entry_price=source_price,
        current_copy_price=f_float("current_copy_price", "current_price"),
        estimated_fill_price=f_float("estimated_fill_price", "estimated_fill"),
        # Fill / depth (optional; present only when real)
        intended_stake=f_float("intended_stake", "intended_notional", "size", "amount"),
        executable_depth=f_float("executable_depth"),
        fill_percentage=f_float("fill_percentage"),
        spread=f_float("spread"),
        best_bid_size=f_float("best_bid_size"),
        best_ask_size=f_float("best_ask_size"),
        # Freshness / holding period (optional; present only when real)
        trade_age_seconds=f_float("trade_age_seconds"),
        seconds_to_market_end=f_float(
            "seconds_to_market_end",
            "market_end_timestamp",
            "end_timestamp",
            "close_time",
            "end_date",
        ),
        market_active=f("market_active"),
        market_closed=f("market_closed"),
        market_resolved=f("market_resolved"),
        # Snapshot timing (optional; present only when real)
        source_trade_timestamp=source_timestamp,
        price_snapshot_fetched_at=f("price_snapshot_fetched_at", "snapshot_fetched_at"),
        evaluation_timestamp=f("evaluation_timestamp"),
    )


# ── Findings (CORRECTION 1: ingestion-side inconsistency) ──────────────────
def _ingestion_side_inconsistency_finding(
    raw_side_distribution: dict[str, int],
) -> Optional[TradeCopyabilityBridgeAuditFinding]:
    """Fire when the same logical side appears under multiple raw string forms."""
    buy_forms = sorted(k for k in raw_side_distribution if k.lower() == "buy")
    sell_forms = sorted(k for k in raw_side_distribution if k.lower() == "sell")
    fired = False
    affected = []
    if len(buy_forms) > 1:
        fired = True
        affected.append("BUY")
    if len(sell_forms) > 1:
        fired = True
        affected.append("SELL")
    if not fired:
        return None
    evidence = {
        "raw_side_distribution": dict(raw_side_distribution),
        "affected_logical_sides": affected,
    }
    return TradeCopyabilityBridgeAuditFinding(
        key="ingestion_side_inconsistency",
        severity="warning",
        summary=(
            "source_trades.side contains multiple exact string forms for the "
            "same logical side. This may indicate inconsistent ingestion "
            "normalization or multiple writer paths."
        ),
        count=sum(raw_side_distribution.get(k, 0) for k in buy_forms + sell_forms),
        evidence=evidence,
        recommendation=(
            "Investigate source_trades.side writer paths and decide whether "
            "side should be normalized at ingestion. The PR24R bridge "
            "canonicalizes defensively, but this inconsistency should not be "
            "silently patched over forever."
        ),
    )


# ── Report builder ─────────────────────────────────────────────────────────
def build_trade_copyability_bridge_audit(
    conn_or_db: Any,
    *,
    limit: int = 20,
) -> TradeCopyabilityBridgeAuditReport:
    """Build a read-only Trade Copyability bridge audit / dry-run report.

    ``conn_or_db`` must be an already-open ``sqlite3.Connection`` opened
    read-only (``mode=ro``). The function performs only SELECT / PRAGMA
    reads and never mutates the database.
    """
    if limit < 0:
        raise ValueError("limit must be >= 0")

    conn = _conn(conn_or_db)
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        source_rows: list[sqlite3.Row] = []
        if _table_exists(conn, "source_trades"):
            source_rows = list(conn.execute("SELECT * FROM source_trades"))

        production_counts = {t: _safe_count(conn, t) for t in _COUNT_TABLES}
        field_map = _resolve_field_map(conn)

        row_audits: list[TradeCopyabilityBridgeRowAudit] = []
        for row in source_rows:
            row_audits.append(_audit_row(row, field_map))

        # --- Distributions ---
        raw_side_distribution: Counter[str] = Counter()
        canonical_side_distribution: Counter[str] = Counter()
        side_canonicalization_counts: Counter[str] = Counter()
        for ra in row_audits:
            key = "<NULL>" if ra.raw_side is None else str(ra.raw_side)
            raw_side_distribution[key] += 1
            if ra.canonical_side is not None:
                canonical_side_distribution[ra.canonical_side] += 1
            side_canonicalization_counts[ra.side_status] += 1

        # --- Readiness counts ---
        build_input_ready = sum(1 for r in row_audits if r.can_build_input)
        compute_score_ready = sum(1 for r in row_audits if r.can_compute_score)
        bridge_ready = build_input_ready
        bridge_blocked = sum(1 for r in row_audits if not r.can_build_input)
        score_attempts = sum(1 for r in row_audits if r.dry_run_verdict is not None)

        bridge_blocked_reason_counts: Counter[str] = Counter()
        for r in row_audits:
            for reason in r.bridge_blocked_reasons:
                bridge_blocked_reason_counts[reason] += 1

        dry_run_verdict_counts: Counter[str] = Counter()
        dry_run_rejection_counts: Counter[str] = Counter()
        dry_run_missing_counts: Counter[str] = Counter()
        for r in row_audits:
            if r.dry_run_verdict is not None:
                dry_run_verdict_counts[r.dry_run_verdict] += 1
            for reason in r.dry_run_rejection_reasons:
                dry_run_rejection_counts[reason] += 1
            for ess in r.dry_run_missing_essentials:
                dry_run_missing_counts[ess] += 1

        # --- Findings ---
        findings: list[TradeCopyabilityBridgeAuditFinding] = []
        inc = _ingestion_side_inconsistency_finding(dict(raw_side_distribution))
        if inc is not None:
            findings.append(inc)

        # SELL-supports / malformed summary
        sell_count = (
            raw_side_distribution.get("SELL", 0)
            + raw_side_distribution.get("sell", 0)
        )
        if sell_count:
            findings.append(TradeCopyabilityBridgeAuditFinding(
                key="source_side_sell_present",
                severity="info",
                summary=(
                    "source_trades contains SELL/sell rows; v1 must never make "
                    "SELL eligible (sell_side_copyability_not_supported_v1)."
                ),
                count=sell_count,
                evidence={"raw_side_distribution": dict(raw_side_distribution)},
            ))
        malformed = sum(
            v for k, v in raw_side_distribution.items()
            if k not in ("BUY", "SELL", "buy", "sell", "<NULL>")
        )
        if malformed:
            findings.append(TradeCopyabilityBridgeAuditFinding(
                key="source_side_malformed",
                severity="warning",
                summary="source_trades has malformed side values (not buy/BUY/sell/SELL).",
                count=malformed,
                evidence={"raw_side_distribution": dict(raw_side_distribution)},
                recommendation="Bridge must reject malformed side before scoring.",
            ))

        # Zero production score attempts is EXPECTED (CORRECTION 4)
        if score_attempts == 0:
            findings.append(TradeCopyabilityBridgeAuditFinding(
                key="zero_production_score_attempts_expected",
                severity="info",
                summary=(
                    "Zero production score attempts is expected when "
                    "current-price/depth/fill/timing evidence is unavailable. "
                    "The dry-run scoring path is validated by synthetic_test_only "
                    "unit coverage."
                ),
                count=0,
                evidence={
                    "compute_score_ready_count": compute_score_ready,
                    "top_bridge_blocked_reasons": dict(bridge_blocked_reason_counts),
                },
            ))

        # Missing-evidence summary (only if any build-ready but not score-ready)
        if bridge_blocked_reason_counts:
            findings.append(TradeCopyabilityBridgeAuditFinding(
                key="bridge_blocked_reason_summary",
                severity="info",
                summary=(
                    "Bridge blocked reasons across source_trades (expected gaps "
                    "in current-market/snapshot evidence)."
                ),
                count=sum(bridge_blocked_reason_counts.values()),
                evidence={"bridge_blocked_reason_counts": dict(bridge_blocked_reason_counts)},
                recommendation=(
                    "PR24S should add the missing snapshot/depth/current-price "
                    "collection bridge before persisted decisions."
                ),
            ))

        recommended_next_step = _recommended_next_step(
            has_ingestion_inconsistency=inc is not None,
            build_input_ready=build_input_ready,
            compute_score_ready=compute_score_ready,
        )

        return TradeCopyabilityBridgeAuditReport(
            ready_to_wire_to_automation=False,
            ready_to_persist_decisions=False,
            production_counts=production_counts,
            source_trade_count=len(row_audits),
            raw_side_distribution=dict(raw_side_distribution),
            canonical_side_distribution=dict(canonical_side_distribution),
            side_canonicalization_counts=dict(side_canonicalization_counts),
            build_input_ready_count=build_input_ready,
            compute_score_ready_count=compute_score_ready,
            bridge_ready_count=bridge_ready,
            bridge_blocked_count=bridge_blocked,
            bridge_blocked_reason_counts=dict(bridge_blocked_reason_counts),
            score_attempt_count=score_attempts,
            dry_run_verdict_counts=dict(dry_run_verdict_counts),
            dry_run_rejection_reason_counts=dict(dry_run_rejection_counts),
            dry_run_missing_essential_counts=dict(dry_run_missing_counts),
            findings=tuple(findings),
            row_audits=tuple(row_audits[: max(limit, 0)] or row_audits),
            recommended_next_step=recommended_next_step,
        )
    finally:
        conn.row_factory = old_factory


# ── Recommended next step (CORRECTION 5) ───────────────────────────────────
def _recommended_next_step(
    *,
    has_ingestion_inconsistency: bool,
    build_input_ready: int,
    compute_score_ready: int,
) -> str:
    parts: list[str] = []
    if has_ingestion_inconsistency:
        parts.append(
            "Investigate source_trades.side ingestion normalization/writer "
            "paths. Bridge canonicalization is defensive and should not hide "
            "upstream inconsistency."
        )
    if compute_score_ready == 0:
        parts.append(
            "Add or verify snapshot/current-price/depth/fill/market-end "
            "evidence before persisted dry-run decisions."
        )
    if build_input_ready > 0 and compute_score_ready == 0:
        parts.append(
            "The source-trade mapping layer exists, but current-market "
            "evidence is missing."
        )
    if build_input_ready == 0:
        parts.append(
            "PR24S should add the missing snapshot/depth/current-price "
            "collection bridge before persisted decisions."
        )
    elif build_input_ready > 0 and compute_score_ready == 0:
        parts.append(
            "PR24S should add a persisted dry-run decision writer with guards, "
            "after review."
        )
    parts.append("Do not wire automation until persisted dry-run decisions are reviewed.")
    return " ".join(parts)


# ── Human rendering ─────────────────────────────────────────────────────────
def report_to_human(report: TradeCopyabilityBridgeAuditReport) -> str:
    lines: list[str] = []
    lines.append("TRADE COPYABILITY BRIDGE AUDIT — READ ONLY / DRY RUN")
    lines.append("")
    lines.append(f"ready_to_wire_to_automation = {report.ready_to_wire_to_automation}")
    lines.append(f"ready_to_persist_decisions = {report.ready_to_persist_decisions}")
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
    lines.append("== Side canonicalization summary ==")
    for k, v in sorted(report.side_canonicalization_counts.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"  {k}: {v}")
    lines.append("")

    lines.append("== Bridge readiness summary ==")
    lines.append(f"  build_input_ready_count (bridge_ready_count): {report.build_input_ready_count}")
    lines.append(f"  compute_score_ready_count: {report.compute_score_ready_count}")
    lines.append(f"  bridge_blocked_count: {report.bridge_blocked_count}")
    lines.append(f"  score_attempt_count: {report.score_attempt_count}")
    lines.append("")

    lines.append("== Top bridge blocked reasons ==")
    if report.bridge_blocked_reason_counts:
        for k, v in sorted(report.bridge_blocked_reason_counts.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("== Dry-run scoring summary ==")
    if report.score_attempt_count:
        lines.append(f"  score_attempt_count: {report.score_attempt_count}")
        for k, v in sorted(report.dry_run_verdict_counts.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"  dry_run_verdict {k}: {v}")
        if report.dry_run_rejection_reason_counts:
            lines.append("  dry_run_rejection_reasons:")
            for k, v in sorted(report.dry_run_rejection_reason_counts.items(), key=lambda x: (-x[1], x[0])):
                lines.append(f"    {k}: {v}")
        if report.dry_run_missing_essential_counts:
            lines.append("  dry_run_missing_essentials:")
            for k, v in sorted(report.dry_run_missing_essential_counts.items(), key=lambda x: (-x[1], x[0])):
                lines.append(f"    {k}: {v}")
    else:
        lines.append("  Zero production score attempts is expected when "
                     "current-price/depth/fill/timing evidence is unavailable. "
                     "The dry-run scoring path is validated by synthetic_test_only "
                     "unit coverage.")
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
        lines.append(f"  source_trade_id={ra.source_trade_id} "
                     f"raw_side={ra.raw_side} canonical_side={ra.canonical_side} "
                     f"side_status={ra.side_status}")
        lines.append(f"    can_build_input={ra.can_build_input} "
                     f"can_compute_score={ra.can_compute_score}")
        if ra.bridge_blocked_reasons:
            lines.append(f"    blocked={list(ra.bridge_blocked_reasons)}")
        if ra.dry_run_verdict is not None:
            lines.append(f"    dry_run_verdict={ra.dry_run_verdict} "
                         f"dry_run_score={ra.dry_run_score}")
        for n in ra.notes:
            lines.append(f"    note: {n}")
    lines.append("")

    lines.append("== Recommended next step ==")
    lines.append(f"  {report.recommended_next_step}")
    lines.append("")
    lines.append(
        "This report does not persist decisions, create candidates, create "
        "paper signals, or place orders."
    )
    return "\n".join(lines)


def report_to_dict(report: TradeCopyabilityBridgeAuditReport) -> dict[str, Any]:
    return report.to_dict()


__all__ = [
    "TradeCopyabilityBridgeRowAudit",
    "TradeCopyabilityBridgeAuditFinding",
    "TradeCopyabilityBridgeAuditReport",
    "build_trade_copyability_bridge_audit",
    "canonicalize_source_side",
    "report_to_human",
    "report_to_dict",
]
