"""PR24U — Trade Copyability REAL snapshot/depth/current-price collection bridge.

PR24S proved that the *evidence-consuming* path works (it shaped offline
synthetic depth into ``SnapshotEvidenceResult``), but production had NO real
snapshot / depth / current-price evidence to collect. PR24U closes that gap by
PROVING whether real evidence can be collected for eligible ``source_trades``
rows and shaped into the PR24S evidence structures.

This module is PURE and NON-PERSISTING (same guardrails as every Polycopy
read-only audit PR):

  * It reads ``source_trades`` (and existing ``candidate_price_snapshots``
    read-only) through a caller-supplied ``sqlite3.Connection`` opened with
    ``mode=ro``.
  * It SHAPES collected market evidence into the PR24S evidence structures by
    REUSING ``polycopy.engine.trade_copyability_snapshot_evidence_bridge.
    build_snapshot_evidence`` and ``SnapshotEvidenceResult`` — it does NOT
    reinvent the evidence dataclass.
  * Real market-data collection REUSES the existing read-only adapter
    ``polycopy.adapters.polymarket_clob.PolymarketClobClient`` (``GET /book``).
    No new duplicate market-data / CLOB / Polymarket client is invented.
  * It is a DRY-RUN / report-only bridge. It never writes the production DB,
    never creates candidates / paper signals / orders / positions, never wires
    automation, never tunes any formula.

Two collection modes:

  * ``--allow-live-preview`` OFF (DEFAULT): pure dry-run. No network call is
    made. The module proves which rows are *eligible* for collection and what
    fields would be available, using an injectable provider
    (``RealSnapshotEvidenceCollector``) that defaults to offline / no levels.
  * ``--allow-live-preview`` ON: a real read-only ``ClobBook`` is fetched per
    eligible token via ``PolymarketClobClient`` and shaped into the PR24S
    evidence structures. STILL no persistence — the run is a live evidence
    *preview* only. Network/auth/parse failures are captured per-row and never
    crash the batch (the whole run is wrapped so one failed token does not
    abort the others).

Eligibility (mappable to a real collection attempt) reuses PR24S / PR24R
logic: canonical side == BUY, ``source_trade_id`` present, ``token_id``
present (NOT NULL — NULL token_id cannot be collected and is blocked,
never invented), ``price`` parseable, ``quantity`` parseable.

All ``ready_*`` flags are ALWAYS ``False``:

  * ready_to_wire_to_automation
  * ready_to_persist_decisions
  * ready_to_create_candidates
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from polycopy.engine.trade_copyability_snapshot_evidence_bridge import (
    SnapshotEvidenceResult,
    build_snapshot_evidence,
)
from polycopy.engine.trade_copyability_bridge_audit import canonicalize_source_side

# Reused readiness thresholds from Trade Copyability v1 (read-only reference;
# PR24U does not change them, only reports against them).
MIN_COPY_CANDIDATE_FILL_PERCENTAGE = 0.80

SELL_UNSUPPORTED_REASON = "sell_side_copyability_not_supported_v1"


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


# ── Column discovery candidates (PR24R / PR24S aligned) ─────────────────────
_SOURCE_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "source_trade_id": ("source_trade_id", "id"),
    "trader_address": ("trader_address", "wallet_address", "wallet_id"),
    "wallet_id": ("wallet_id", "wallet_address", "trader_address"),
    # market_source_id is the real market identifier in source_trades.
    "market_source_id": ("market_source_id", "market_id", "condition_id"),
    "token_id": ("token_id", "outcome_token_id", "clob_token_id",
                 "condition_id", "market_id"),
    "side": ("side", "trade_side"),
    "price": ("price", "source_price", "entry_price", "avg_price"),
    "size": ("size", "shares", "amount", "notional", "usd_size", "stake", "quantity"),
    "timestamp": ("timestamp", "created_at", "traded_at", "source_trade_timestamp"),
}


# ── Injectable collector interface (mirrors PR24S provider shape) ───────────
class RealSnapshotEvidenceCollector:
    """Pure interface for OBTAINING real order-book evidence for a token.

    The default implementation is OFFLINE and returns no fetch result. Tests
    inject a subclass that returns a synthetic ``ClobBook``-like object (or a
    real one). Production live preview uses a thin async adapter around
    ``PolymarketClobClient`` (``LiveClobBookCollector``) but ONLY when the
    caller explicitly opts in via ``--allow-live-preview``; otherwise the
    offline default is used and NO network call is made.

    The returned object only needs duck-typed attributes used by
    :func:`_shape_clob_book_into_evidence`: ``bids``, ``asks`` (each a list of
    ``(price, size)``-ish items with ``.price`` / ``.size``), ``error_code``,
    ``error_message``, ``fetched_at`` (datetime), ``book_hash``, ``best_bid``,
    ``best_ask``, ``spread``. ``PolymarketClobClient.ClobBook`` already
    satisfies this contract.
    """

    async def fetch_book(self, *, token_id: Optional[str]) -> Any:
        """Return a ClobBook-like object (offline default: synthetic empty)."""
        # Offline default: no levels, no network.
        return _OfflineBook(token_id=token_id or "")


@dataclass
class _OfflineBook:
    """Minimal offline stand-in for a ClobBook with no levels."""

    token_id: str
    bids: list[Any] = field(default_factory=list)
    asks: list[Any] = field(default_factory=list)
    error_code: Optional[str] = "OFFLINE_NO_FETCH"
    error_message: Optional[str] = "offline collector; no live fetch performed"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    book_hash: Optional[str] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread: Optional[float] = None

    @property
    def is_empty(self) -> bool:
        return not self.bids and not self.asks


class LiveClobBookCollector(RealSnapshotEvidenceCollector):
    """Async collector that REUSES ``PolymarketClobClient`` (read-only /book).

    This is the ONLY place PR24U touches a real network client, and it is
    reachable ONLY when ``--allow-live-preview`` is set. It never writes.
    A failed fetch returns a ClobBook carrying a bounded ``error_code`` rather
    than raising, so one bad token does not abort the whole batch.
    """

    def __init__(self, *, client: Any) -> None:
        # ``client`` is a ``PolymarketClobClient`` (or any async ``fetch_book``).
        self._client = client

    async def fetch_book(self, *, token_id: Optional[str]) -> Any:
        if not token_id:
            return _OfflineBook(token_id="")
        return await self._client.fetch_book(token_id)


# ── Dataclasses (evidence shapes REUSE the PR24S SnapshotEvidenceResult) ────
@dataclass(frozen=True)
class TradeCopyabilityRealSnapshotCollectionRowReport:
    """Per-source-trade REAL snapshot-collection dry-run report row.

    This is the canonical per-row report the PR body requires. It carries the
    exact fields enumerated in the PR24U task spec plus PR24S-compatibility
    status and skip/error reasons.
    """

    source_trade_id: Optional[str]
    wallet_address: Optional[str]
    market_source_id: Optional[str]
    token_id: Optional[str]
    side: Optional[str]
    eligibility_status: str  # "eligible" | "not_eligible"
    current_price_available: bool
    depth_available: bool
    spread_available: bool
    market_state_available: bool
    snapshot_timestamp: Optional[str]
    pr24s_evidence_compatibility: str  # "compatible" | "partial" | "incompatible"
    skip_reason: Optional[str] = None
    error_reason: Optional[str] = None
    # Optional richer detail (for JSON report only).
    collected_evidence: Optional[SnapshotEvidenceResult] = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_trade_id": self.source_trade_id,
            "wallet_address": self.wallet_address,
            "market_source_id": self.market_source_id,
            "token_id": self.token_id,
            "side": self.side,
            "eligibility_status": self.eligibility_status,
            "current_price_available": self.current_price_available,
            "depth_available": self.depth_available,
            "spread_available": self.spread_available,
            "market_state_available": self.market_state_available,
            "snapshot_timestamp": self.snapshot_timestamp,
            "pr24s_evidence_compatibility": self.pr24s_evidence_compatibility,
            "skip_reason": self.skip_reason,
            "error_reason": self.error_reason,
            "collected_evidence": (
                self.collected_evidence.to_dict() if self.collected_evidence else None
            ),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class TradeCopyabilityRealSnapshotCollectionFinding:
    """A single PR24U finding (mirrors PR24S finding shape)."""

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
class TradeCopyabilityRealSnapshotCollectionBridgeReport:
    """Full PR24U real snapshot-collection bridge report (read-only / dry-run)."""

    ready_to_wire_to_automation: bool
    ready_to_persist_decisions: bool
    ready_to_create_candidates: bool
    production_counts: dict[str, Any]
    source_trade_count: int
    eligible_count: int
    ineligible_count: int
    raw_side_distribution: dict[str, int]
    canonical_side_distribution: dict[str, int]
    ingestion_side_inconsistency_present: bool
    live_preview_enabled: bool
    # Field availability across eligible rows that were attempted.
    current_price_available_count: int
    depth_available_count: int
    spread_available_count: int
    market_state_available_count: int
    pr24s_compatible_count: int
    pr24s_partial_count: int
    pr24s_incompatible_count: int
    skip_reason_counts: dict[str, int]
    sample_like_row_count: int
    db_path_inspected: Optional[str]
    row_reports: tuple[TradeCopyabilityRealSnapshotCollectionRowReport, ...]
    findings: tuple[TradeCopyabilityRealSnapshotCollectionFinding, ...]
    recommended_next_step: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready_to_wire_to_automation": self.ready_to_wire_to_automation,
            "ready_to_persist_decisions": self.ready_to_persist_decisions,
            "ready_to_create_candidates": self.ready_to_create_candidates,
            "production_counts": self.production_counts,
            "source_trade_count": self.source_trade_count,
            "eligible_count": self.eligible_count,
            "ineligible_count": self.ineligible_count,
            "raw_side_distribution": self.raw_side_distribution,
            "canonical_side_distribution": self.canonical_side_distribution,
            "ingestion_side_inconsistency_present": self.ingestion_side_inconsistency_present,
            "live_preview_enabled": self.live_preview_enabled,
            "current_price_available_count": self.current_price_available_count,
            "depth_available_count": self.depth_available_count,
            "spread_available_count": self.spread_available_count,
            "market_state_available_count": self.market_state_available_count,
            "pr24s_compatible_count": self.pr24s_compatible_count,
            "pr24s_partial_count": self.pr24s_partial_count,
            "pr24s_incompatible_count": self.pr24s_incompatible_count,
            "skip_reason_counts": self.skip_reason_counts,
            "sample_like_row_count": self.sample_like_row_count,
            "db_path_inspected": self.db_path_inspected,
            "row_reports": [r.to_dict() for r in self.row_reports],
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


# Tokens that strongly indicate a seeded/sample/placeholder row rather than a
# real production trade. Used ONLY for report clarity; the rows are never
# mutated, deleted, backfilled, or normalized.
_SAMPLE_WALLET_MARKERS = ("_do_not_use", "sample_trader", "sample_wallet", "0xsample")
_SAMPLE_MARKET_MARKERS = ("sample-market", "sample_market", "sample-market-")
_SAMPLE_TRADE_ID_MARKERS = ("sample-trade", "sample_trade")


def _row_looks_sample_like(row: sqlite3.Row) -> bool:
    """Heuristic: does this source_trades row look seeded/sample/placeholder?

    Pure read-only inspection of a single row. Returns True when the wallet
    address, market identifier, or source_trade_id contains a known sample
    marker. This is REPORT TEXT ONLY — it does not classify eligibility, does
    not change behavior, and never mutates the row.
    """
    wallet = _norm_text(_row_get(row, "trader_address"))
    market = _norm_text(_row_get(row, "market_source_id"))
    trade_id = _norm_text(_row_get(row, "source_trade_id"))
    low_wallet = wallet.lower()
    low_market = market.lower()
    low_trade = trade_id.lower()
    for marker in _SAMPLE_WALLET_MARKERS:
        if marker in low_wallet:
            return True
    for marker in _SAMPLE_MARKET_MARKERS:
        if marker in low_market:
            return True
    for marker in _SAMPLE_TRADE_ID_MARKERS:
        if marker in low_trade:
            return True
    return False


def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


# ── Field discovery (PR24R-aligned) ─────────────────────────────────────────
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


# ── Shape a collected ClobBook into the PR24S evidence structures ──────────
def _extract_levels(book: Any) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Convert a ClobBook (or duck-typed object) into (ask_levels, bid_levels).

    PolymarketClobClient.ClobBook.attrs ``bids`` / ``asks`` are lists of
    ``ClobBookLevel(price, size)``. Returns (ask, bid) as (price, size) tuples.
    """
    def _to_pairs(levels: list[Any]) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for lv in levels or []:
            try:
                out.append((float(lv.price), float(lv.size)))
            except (TypeError, ValueError, AttributeError):
                continue
        return out

    bids = _to_pairs(getattr(book, "bids", []) or [])
    asks = _to_pairs(getattr(book, "asks", []) or [])
    return asks, bids


def _shape_clob_book_into_evidence(
    *,
    book: Any,
    token_id: Optional[str],
    source_trade_id: Optional[str],
    source_entry_price: Optional[float],
    intended_stake: Optional[float],
    live_preview: bool,
) -> tuple[Optional[SnapshotEvidenceResult], list[str]]:
    """Shape a collected ClobBook into PR24S ``SnapshotEvidenceResult``.

    Returns (result, notes). ``result`` is None when the book has no usable
    levels or an error code. Reuses ``build_snapshot_evidence`` so the evidence
    shape is byte-for-byte compatible with PR24S consumers.
    """
    notes: list[str] = []
    ask_levels, bid_levels = _extract_levels(book)

    has_depth = bool(ask_levels) or bool(bid_levels)
    err_code = getattr(book, "error_code", None)

    if not has_depth:
        reason = "no_depth_levels"
        if err_code and err_code not in ("OFFLINE_NO_FETCH",):
            reason = f"{reason}:{err_code}"
        notes.append(f"no order-book depth collected ({reason})")
        return None, notes

    fetched_at = _to_iso(getattr(book, "fetched_at", None))
    try:
        result = build_snapshot_evidence(
            token_id=token_id,
            source_trade_id=source_trade_id,
            source_entry_price=source_entry_price,
            intended_stake=intended_stake,
            ask_levels=ask_levels,
            bid_levels=bid_levels,
            price_snapshot_fetched_at=fetched_at,
            evaluation_timestamp=fetched_at,
            seconds_to_market_end=None,  # NOT collected by /book; market-state not available
            market_active=None,
            market_closed=None,
            market_resolved=None,
            from_existing_snapshot=False,
        )
    except Exception as exc:  # defensive: builder should not raise, but never crash batch
        notes.append(f"evidence build error: {type(exc).__name__}: {exc}"[:500])
        return None, notes

    # Honest partial-fill note (read-only signal, mirrored from PR24S).
    if (result.fill_percentage is not None
            and result.fill_percentage < MIN_COPY_CANDIDATE_FILL_PERCENTAGE):
        notes.append("partial fill below 80% copy-candidate threshold (future scoring blocker)")
    if live_preview:
        notes.append("evidence shaped from real CLOB /book fetch (dry-run, not persisted)")
    return result, notes


def _classify_compatibility(
    book: Any,
    has_depth: bool,
    err_code: Optional[str],
) -> str:
    """Classify PR24S evidence compatibility for a collected book."""
    if err_code and err_code not in ("OFFLINE_NO_FETCH",):
        return "incompatible"
    if has_depth:
        return "compatible"
    return "incompatible"


# ── Per-row audit (builds the canonical report row) ────────────────────────
def _audit_row(
    row: sqlite3.Row,
    field_map: dict[str, Optional[str]],
    collector: RealSnapshotEvidenceCollector,
    *,
    live_preview: bool,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> TradeCopyabilityRealSnapshotCollectionRowReport:
    raw_side = _pick(row, "side", field_map)
    canonical_side, side_status, side_reason = canonicalize_source_side(raw_side)
    source_trade_id = _pick(row, "source_trade_id", field_map)
    wallet_address = _pick(row, "trader_address", field_map)
    market_source_id = _pick(row, "market_source_id", field_map)
    token_id = _pick(row, "token_id", field_map)
    source_price = _maybe_float(_pick(row, "price", field_map))
    source_size = _maybe_float(_pick(row, "size", field_map))

    notes: list[str] = []
    skip_reasons: list[str] = []
    error_reason: Optional[str] = None

    eligible = True
    if canonical_side != "BUY":
        eligible = False
        if side_reason == SELL_UNSUPPORTED_REASON:
            skip_reasons.append(SELL_UNSUPPORTED_REASON)
        elif side_status == "missing":
            skip_reasons.append("missing_side")
        else:
            skip_reasons.append("invalid_side")
    if source_trade_id in (None, ""):
        eligible = False
        skip_reasons.append("missing_source_trade_id")
    if token_id in (None, ""):
        eligible = False
        skip_reasons.append("missing_token_id")
    if source_price is None:
        eligible = False
        skip_reasons.append("missing_source_entry_price")
    if source_size is None:
        eligible = False
        skip_reasons.append("missing_source_size")

    # Defaults (not-eligible path).
    current_price_available = False
    depth_available = False
    spread_available = False
    market_state_available = False
    snapshot_timestamp: Optional[str] = None
    pr24s_compat = "incompatible"
    collected: Optional[SnapshotEvidenceResult] = None

    if eligible:
        # Collect (offline default, or live /book if opted in).
        try:
            if live_preview:
                if loop is None:
                    loop = asyncio.new_event_loop()
                book = loop.run_until_complete(
                    collector.fetch_book(token_id=token_id)
                )
            else:
                book = asyncio.run(collector.fetch_book(token_id=token_id))
        except Exception as exc:  # controlled: never crash the whole run
            error_reason = f"{type(exc).__name__}: {exc}"[:300]
            book = _OfflineBook(token_id=token_id or "")

        ask_levels, bid_levels = _extract_levels(book)
        has_depth = bool(ask_levels) or bool(bid_levels)
        err_code = getattr(book, "error_code", None)
        if err_code and err_code not in ("OFFLINE_NO_FETCH",):
            err_detail = getattr(book, "error_message", None)
            error_reason = f"{err_code}: {err_detail}"[:300]

        depth_available = has_depth
        best_ask = getattr(book, "best_ask", None)
        best_bid = getattr(book, "best_bid", None)
        spread = getattr(book, "spread", None)
        current_price_available = best_ask is not None
        spread_available = spread is not None and best_bid is not None
        # /book does NOT return market state -> never claim it is available.
        market_state_available = False
        snapshot_timestamp = _to_iso(getattr(book, "fetched_at", None))
        pr24s_compat = _classify_compatibility(book, has_depth, err_code)

        collected, shape_notes = _shape_clob_book_into_evidence(
            book=book,
            token_id=token_id,
            source_trade_id=source_trade_id,
            source_entry_price=source_price,
            intended_stake=source_size,
            live_preview=live_preview,
        )
        notes.extend(shape_notes)
        if collected is None and has_depth:
            notes.append("depth present but evidence shape incomplete")
    else:
        if "missing_token_id" in skip_reasons:
            notes.append("row cannot collect real evidence without token_id")

    return TradeCopyabilityRealSnapshotCollectionRowReport(
        source_trade_id=source_trade_id if source_trade_id not in (None, "") else None,
        wallet_address=wallet_address if wallet_address not in (None, "") else None,
        market_source_id=market_source_id if market_source_id not in (None, "") else None,
        token_id=token_id if token_id not in (None, "") else None,
        side=raw_side if raw_side != "" else None,
        eligibility_status="eligible" if eligible else "not_eligible",
        current_price_available=current_price_available,
        depth_available=depth_available,
        spread_available=spread_available,
        market_state_available=market_state_available,
        snapshot_timestamp=snapshot_timestamp,
        pr24s_evidence_compatibility=pr24s_compat,
        skip_reason=";".join(sorted(set(skip_reasons))) if skip_reasons else None,
        error_reason=error_reason,
        collected_evidence=collected,
        notes=tuple(notes),
    )


# ── Report builder ──────────────────────────────────────────────────────────
def build_trade_copyability_real_snapshot_collection_bridge(
    conn_or_db: Any,
    *,
    limit: int = 20,
    collector: Optional[RealSnapshotEvidenceCollector] = None,
    live_preview: bool = False,
    db_path: Optional[str] = None,
) -> TradeCopyabilityRealSnapshotCollectionBridgeReport:
    """Build a read-only Trade Copyability REAL snapshot-collection report.

    ``conn_or_db`` must be an already-open ``sqlite3.Connection`` opened
    read-only (``mode=ro``). The function performs only SELECT / PRAGMA reads
    and never mutates the database. Real collection is performed through the
    injected ``collector`` (offline default; live /book only when
    ``live_preview=True`` AND a live collector was injected).
    """
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if collector is None:
        collector = RealSnapshotEvidenceCollector()  # offline default

    conn = _conn(conn_or_db)
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    loop: Optional[asyncio.AbstractEventLoop] = None
    try:
        source_rows: list[sqlite3.Row] = []
        if _table_exists(conn, "source_trades"):
            source_rows = list(conn.execute("SELECT * FROM source_trades"))

        production_counts = {t: _safe_count(conn, t) for t in _COUNT_TABLES}
        field_map = _resolve_field_map(conn)

        row_reports: list[TradeCopyabilityRealSnapshotCollectionRowReport] = []
        for row in source_rows:
            rr = _audit_row(
                row, field_map, collector, live_preview=live_preview, loop=loop
            )
            row_reports.append(rr)

        # Report-clarity only: count rows that look seeded/sample/placeholder.
        # Heuristic markers in wallet/market/trade_id. Never mutates the rows.
        sample_like_row_count = sum(1 for row in source_rows if _row_looks_sample_like(row))

        # --- Distributions ---
        raw_side_distribution: Counter[str] = Counter()
        canonical_side_distribution: Counter[str] = Counter()
        for rr in row_reports:
            key = "<NULL>" if rr.side is None else str(rr.side)
            raw_side_distribution[key] += 1
            if rr.side is not None:
                cs, _, _ = canonicalize_source_side(rr.side)
                if cs is not None:
                    canonical_side_distribution[cs] += 1

        ingestion_inconsistent = (
            sum(1 for k in raw_side_distribution if k.lower() == "buy") > 1
            or sum(1 for k in raw_side_distribution if k.lower() == "sell") > 1
        )

        eligible = [rr for rr in row_reports if rr.eligibility_status == "eligible"]
        ineligible = [rr for rr in row_reports if rr.eligibility_status != "eligible"]

        current_price_available_count = sum(1 for rr in eligible if rr.current_price_available)
        depth_available_count = sum(1 for rr in eligible if rr.depth_available)
        spread_available_count = sum(1 for rr in eligible if rr.spread_available)
        market_state_available_count = sum(1 for rr in eligible if rr.market_state_available)
        pr24s_compatible_count = sum(
            1 for rr in eligible if rr.pr24s_evidence_compatibility == "compatible"
        )
        pr24s_partial_count = sum(
            1 for rr in eligible if rr.pr24s_evidence_compatibility == "partial"
        )
        pr24s_incompatible_count = sum(
            1 for rr in eligible if rr.pr24s_evidence_compatibility == "incompatible"
        )

        skip_reason_counts: Counter[str] = Counter()
        for rr in ineligible:
            if rr.skip_reason:
                for part in rr.skip_reason.split(";"):
                    skip_reason_counts[part] += 1

        # --- Findings ---
        findings: list[TradeCopyabilityRealSnapshotCollectionFinding] = []
        if ingestion_inconsistent:
            findings.append(TradeCopyabilityRealSnapshotCollectionFinding(
                key="ingestion_side_inconsistency",
                severity="warning",
                summary=(
                    "source_trades.side contains multiple exact string forms for the "
                    "same logical side (e.g. buy vs BUY). PR24T normalization guard "
                    "handles future writes; existing production rows were intentionally "
                    "not backfilled. This PR does not normalize them."
                ),
                count=sum(raw_side_distribution.get(k, 0)
                          for k in raw_side_distribution if k.lower() in ("buy", "sell")),
                evidence={
                    "raw_side_distribution": dict(raw_side_distribution),
                    "canonical_side_distribution": dict(canonical_side_distribution),
                },
                recommendation=(
                    "Leave existing rows as-is (no backfill per PR24T). Future writes "
                    "normalize via normalize_side_for_persistence."
                ),
            ))

        if sample_like_row_count:
            effective_real_n = len(row_reports) - sample_like_row_count
            findings.append(TradeCopyabilityRealSnapshotCollectionFinding(
                key="source_trade_sample_data_present",
                severity="info",
                summary=(
                    f"Of {len(row_reports)} source_trades inspected, {sample_like_row_count} "
                    "appear to be seeded/sample/placeholder rows (wallet/market/trade_id "
                    "contain sample markers such as 0xsample_trader_*_do_not_use and "
                    "sample-market-*). Real usable production-like evidence coverage is "
                    f"effectively n={max(effective_real_n, 0)}. This is report text only; "
                    "the rows are NOT deleted, mutated, backfilled, or normalized."
                ),
                count=sample_like_row_count,
                evidence={
                    "source_trade_count": len(row_reports),
                    "sample_like_row_count": sample_like_row_count,
                    "effective_real_coverage_n": max(effective_real_n, 0),
                },
                recommendation=(
                    "Treat the single non-sample eligible row (test_trade_1) as the only "
                    "currently-real evidence-collection target. A future ingestion PR should "
                    "populate real rows with token_id for broader coverage."
                ),
            ))

        if skip_reason_counts:
            findings.append(TradeCopyabilityRealSnapshotCollectionFinding(
                key="collection_skip_reason_summary",
                severity="info",
                summary=(
                    "Rows skipped from real evidence collection (expected gaps: no "
                    "token_id, non-BUY side, missing price/size/id). NULL token_id rows "
                    "cannot be collected and are blocked honestly, never invented."
                ),
                count=sum(skip_reason_counts.values()),
                evidence={"skip_reason_counts": dict(skip_reason_counts)},
                recommendation=(
                    "A future ingestion PR should populate token_id for real trades so "
                    "real /book evidence can be collected. Until then, collection is "
                    "limited to rows that already carry a token_id."
                ),
            ))

        # Live-preview honest finding: /book does not expose market state.
        if live_preview:
            findings.append(TradeCopyabilityRealSnapshotCollectionFinding(
                key="live_preview_market_state_unavailable",
                severity="info",
                summary=(
                    "Live CLOB /book fetch provides current price, depth, spread, and a "
                    "snapshot timestamp, but NOT market state (active/closed/resolved) or "
                    "seconds_to_market_end. Those fields remain unavailable after "
                    "collection and must come from a separate Gamma/market-state source "
                    "before any future scoring wiring."
                ),
                count=market_state_available_count,
                evidence={
                    "market_state_available_count": market_state_available_count,
                    "depth_available_count": depth_available_count,
                    "current_price_available_count": current_price_available_count,
                    "spread_available_count": spread_available_count,
                },
                recommendation=(
                    "PR24V (deferred) should wire a Gamma market-state client to supply "
                    "market_active/closed/resolved + seconds_to_market_end; PR24U proves "
                    "the /book half of the evidence is collectable."
                ),
            ))

        if not live_preview:
            findings.append(TradeCopyabilityRealSnapshotCollectionFinding(
                key="dry_run_offline_no_network",
                severity="info",
                summary=(
                    "Default dry-run mode performed NO network fetch. Eligibility and "
                    "field-availability were proven structurally against source_trades. "
                    "Use --allow-live-preview to perform a real read-only /book fetch "
                    "(still non-persisting)."
                ),
                recommendation="Run with --allow-live-preview for a live evidence preview.",
            ))

        recommended_next_step = (
            "PR24U proves the real /book collection path is wireable and reuses "
            "PolymarketClobClient. Next: (a) a guarded persistence writer that lands "
            "collected evidence into candidate_price_snapshots read-only by PR24S, and "
            "(b) a Gamma market-state client for market_active/closed/resolved + "
            "seconds_to_market_end. Do not wire automation or persist decisions until "
            "those land and are reviewed."
        )

        return TradeCopyabilityRealSnapshotCollectionBridgeReport(
            ready_to_wire_to_automation=False,
            ready_to_persist_decisions=False,
            ready_to_create_candidates=False,
            production_counts=production_counts,
            source_trade_count=len(row_reports),
            eligible_count=len(eligible),
            ineligible_count=len(ineligible),
            raw_side_distribution=dict(raw_side_distribution),
            canonical_side_distribution=dict(canonical_side_distribution),
            ingestion_side_inconsistency_present=ingestion_inconsistent,
            live_preview_enabled=live_preview,
            current_price_available_count=current_price_available_count,
            depth_available_count=depth_available_count,
            spread_available_count=spread_available_count,
            market_state_available_count=market_state_available_count,
            pr24s_compatible_count=pr24s_compatible_count,
            pr24s_partial_count=pr24s_partial_count,
            pr24s_incompatible_count=pr24s_incompatible_count,
            skip_reason_counts=dict(skip_reason_counts),
            sample_like_row_count=sample_like_row_count,
            db_path_inspected=db_path,
            row_reports=tuple(row_reports[: max(limit, 0)] or row_reports),
            findings=tuple(findings),
            recommended_next_step=recommended_next_step,
        )
    finally:
        conn.row_factory = old_factory
        if loop is not None:
            loop.close()


# ── Human rendering ──────────────────────────────────────────────────────────
def report_to_human(report: TradeCopyabilityRealSnapshotCollectionBridgeReport) -> str:
    lines: list[str] = []
    lines.append("TRADE COPYABILITY REAL SNAPSHOT/DEPTH/PRICE COLLECTION BRIDGE — "
                 "READ ONLY / DRY RUN / REPORT-ONLY")
    lines.append("")
    lines.append(f"ready_to_wire_to_automation = {report.ready_to_wire_to_automation}")
    lines.append(f"ready_to_persist_decisions = {report.ready_to_persist_decisions}")
    lines.append(f"ready_to_create_candidates = {report.ready_to_create_candidates}")
    lines.append(f"live_preview_enabled = {report.live_preview_enabled}")
    lines.append("")
    lines.append(f"DB path inspected: {report.db_path_inspected}")

    lines.append("")
    lines.append("== Production counts (read-only; must be unchanged by this PR) ==")
    for k, v in report.production_counts.items():
        lines.append(f"  {k}: {v}")
    lines.append("")

    lines.append(f"== Source trades inspected: {report.source_trade_count} ==")
    lines.append(f"  eligible_for_collection: {report.eligible_count}")
    lines.append(f"  ineligible_for_collection: {report.ineligible_count}")
    if report.sample_like_row_count:
        effective = max(report.source_trade_count - report.sample_like_row_count, 0)
        lines.append(
            f"  sample_like_rows: {report.sample_like_row_count} "
            f"(seeded/placeholder; NOT deleted/mutated/backfilled)"
        )
        lines.append(
            f"  effective_real_production_like_coverage: n={effective}"
        )
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
        lines.append("  NOTE: mixed casing present; NOT backfilled (PR24T left existing rows).")
    lines.append("")

    lines.append("== Eligible-row field availability (real collection) ==")
    lines.append(f"  current_price_available_count: {report.current_price_available_count}")
    lines.append(f"  depth_available_count: {report.depth_available_count}")
    lines.append(f"  spread_available_count: {report.spread_available_count}")
    lines.append(f"  market_state_available_count: {report.market_state_available_count} "
                 "(/book does not expose market state)")
    lines.append("")
    lines.append("== PR24S evidence compatibility (collected shaped into PR24S structs) ==")
    lines.append(f"  compatible: {report.pr24s_compatible_count}")
    lines.append(f"  partial: {report.pr24s_partial_count}")
    lines.append(f"  incompatible: {report.pr24s_incompatible_count}")
    lines.append("")
    lines.append("== Skip reasons (ineligible rows) ==")
    if report.skip_reason_counts:
        for k, v in sorted(report.skip_reason_counts.items(), key=lambda x: (-x[1], x[0])):
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

    lines.append("== Per-row report (exact PR24U required fields) ==")
    for rr in report.row_reports:
        lines.append(
            f"  source_trade_id={rr.source_trade_id} wallet={rr.wallet_address} "
            f"market={rr.market_source_id} token={rr.token_id} side={rr.side}"
        )
        lines.append(
            f"    eligibility={rr.eligibility_status} "
            f"cur_price={rr.current_price_available} depth={rr.depth_available} "
            f"spread={rr.spread_available} market_state={rr.market_state_available} "
            f"ts={rr.snapshot_timestamp} pr24s={rr.pr24s_evidence_compatibility}"
        )
        if rr.skip_reason:
            lines.append(f"    skip_reason={rr.skip_reason}")
        if rr.error_reason:
            lines.append(f"    error_reason={rr.error_reason}")
        for n in rr.notes:
            lines.append(f"    note: {n}")
    lines.append("")

    lines.append("== Recommended next step ==")
    lines.append(f"  {report.recommended_next_step}")
    lines.append("")
    lines.append(
        "This report performs NO production writes: no snapshots, decisions, "
        "candidates, paper signals, orders, or positions. Default mode is dry-run."
    )
    return "\n".join(lines)


def report_to_dict(report: TradeCopyabilityRealSnapshotCollectionBridgeReport) -> dict[str, Any]:
    return report.to_dict()


__all__ = [
    "RealSnapshotEvidenceCollector",
    "LiveClobBookCollector",
    "TradeCopyabilityRealSnapshotCollectionRowReport",
    "TradeCopyabilityRealSnapshotCollectionFinding",
    "TradeCopyabilityRealSnapshotCollectionBridgeReport",
    "build_trade_copyability_real_snapshot_collection_bridge",
    "report_to_human",
    "report_to_dict",
]
