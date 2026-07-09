"""PR24W — Source-Trade REAL COVERAGE + TOKEN→CONDITION MAPPING AUDIT.

This is a READ-ONLY / REPORT-ONLY audit of the current ``source_trades``
inventory, its real (production-like) coverage, its ingestion quality, and its
identifier-mapping readiness — the Step-1/Step-2 inputs ("find/ingest source
trades" and "normalize and validate trades") of the master chain, BEFORE any
persistence / scoring / candidate / signal / order / timer work.

This module is PURE and NON-PERSISTING (same guardrails as every Polycopy
read-only audit PR):

  * It reads ``source_trades`` (and the read-only mapping tables
    ``market_outcomes`` / ``markets``) through a caller-supplied
    ``sqlite3.Connection`` opened with ``mode=ro``.
  * It performs ONLY SELECT / PRAGMA reads. It never issues INSERT/UPDATE/
    DELETE/DROP/ALTER/CREATE, never calls the connection's transaction-flush
    routine, never uses the scripting executor.
  * It does NOT import ``polycopy.db.database`` (the write-capable ORM). The
    token→condition mapping assessment is a *read-only join feasibility* check
    against the live schema — it does NOT write a mapping table or backfill.
  * It reuses the existing ``canonicalize_source_side`` helper from the PR24R
    bridge audit (no duplicate side logic).
  * It never creates candidates / paper signals / orders / positions, never
    wires automation, never tunes any formula.

Two report axes per row:

  * **Coverage bucket** — is this row a seeded/sample/placeholder, or a
    real-like production trade, and what required identifier/field is missing?
  * **Identifier-mapping readiness** — does the row carry a ``token_id``
    (PR24U /book-ready), a conditionId-shaped ``market_source_id``
    (PR24V Gamma-ready), both, or neither; and does it need a
    token→condition mapping before PR24V market-state can attach?

All ``ready_*`` flags are ALWAYS ``False``:

  * ready_to_wire_to_automation
  * ready_to_persist_decisions
  * ready_to_create_candidates
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from polycopy.engine.trade_copyability_bridge_audit import canonicalize_source_side

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

# ── Column discovery candidates (PR24R / PR24U aligned) ─────────────────────
_SOURCE_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "source_trade_id": ("source_trade_id", "id"),
    "source": ("source",),
    "trader_address": ("trader_address", "wallet_address", "wallet_id"),
    "market_source_id": ("market_source_id", "market_id", "condition_id"),
    "token_id": ("token_id", "outcome_token_id", "clob_token_id"),
    "side": ("side", "trade_side"),
    "price": ("price", "source_price", "entry_price", "avg_price"),
    "size": ("size", "shares", "amount", "notional", "usd_size", "stake", "quantity"),
    "timestamp": ("timestamp", "created_at", "traded_at", "source_trade_timestamp"),
    "is_sample": ("is_sample",),
    "outcome": ("outcome", "outcome_label"),
}

# Plausible conditionId shape: 0x + at least 8 hex chars (Polymarket conditionIds
# are 64-hex). Used to decide if a market_source_id is Gamma-resolution-shaped.
_CONDITION_ID_RE = re.compile(r"^0x[0-9a-fA-F]{8,}$")

# Tokens that strongly indicate a seeded/sample/placeholder row rather than a
# real production trade. Used ONLY for report clarity; the rows are never
# mutated, deleted, backfilled, or normalized.
_SAMPLE_WALLET_MARKERS = ("_do_not_use", "sample_trader", "sample_wallet", "0xsample")
_SAMPLE_MARKET_MARKERS = ("sample-market", "sample_market", "sample-market-")
_SAMPLE_TRADE_ID_MARKERS = ("sample-trade", "sample_trade")
_SAMPLE_SOURCE_MARKERS = ("sample",)


# ── Dataclasses ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SourceTradeCoverageRowReport:
    """Per-``source_trades`` row inventory + coverage + identifier-mapping report.

    Carries the exact fields the PR24W task requires for every row, plus the
    readiness axes (PR24U /book-ready vs PR24V Gamma-ready) and the
    token→condition mapping need.
    """

    source_trade_id: Optional[str]
    wallet_address: Optional[str]
    market_source_id: Optional[str]
    token_id: Optional[str]
    raw_side: Optional[str]
    canonical_side: Optional[str]
    price: Optional[float]
    quantity: Optional[float]
    timestamp: Optional[str]
    sample_placeholder_status: str  # "sample_placeholder" | "real_like"
    sample_reason: Optional[str]
    coverage_bucket: str  # one of the PR24W coverage buckets
    identifier_quality: str  # "both" | "token_only" | "condition_only" | "neither"
    has_token_id: bool
    has_condition_id: bool
    both_token_and_condition: bool
    neither_token_nor_condition: bool
    non_condition_placeholder_market_id: bool
    pr24u_book_ready: bool
    pr24v_gamma_ready: bool
    both_ready: bool
    neither_ready: bool
    token_to_condition_mapping_needed: bool
    copyability_evidence_readiness: str  # status string (see _copyability_readiness)
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_trade_id": self.source_trade_id,
            "wallet_address": self.wallet_address,
            "market_source_id": self.market_source_id,
            "token_id": self.token_id,
            "raw_side": self.raw_side,
            "canonical_side": self.canonical_side,
            "price": self.price,
            "quantity": self.quantity,
            "timestamp": self.timestamp,
            "sample_placeholder_status": self.sample_placeholder_status,
            "sample_reason": self.sample_reason,
            "coverage_bucket": self.coverage_bucket,
            "identifier_quality": self.identifier_quality,
            "has_token_id": self.has_token_id,
            "has_condition_id": self.has_condition_id,
            "both_token_and_condition": self.both_token_and_condition,
            "neither_token_nor_condition": self.neither_token_nor_condition,
            "non_condition_placeholder_market_id": self.non_condition_placeholder_market_id,
            "pr24u_book_ready": self.pr24u_book_ready,
            "pr24v_gamma_ready": self.pr24v_gamma_ready,
            "both_ready": self.both_ready,
            "neither_ready": self.neither_ready,
            "token_to_condition_mapping_needed": self.token_to_condition_mapping_needed,
            "copyability_evidence_readiness": self.copyability_evidence_readiness,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class SourceTradeRealCoverageMappingFinding:
    """A single PR24W finding."""

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
class TokenConditionMappingFeasibility:
    """Read-only assessment of whether token→condition mapping is possible."""

    mapping_join_possible_via_market_outcomes: bool
    market_outcomes_table_present: bool
    market_outcomes_with_clob_token_id: Optional[int]
    markets_table_present: bool
    resolve_trade_to_outcome_helper_exists: bool
    mapping_helper_already_exists: bool
    smallest_future_helper: str
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping_join_possible_via_market_outcomes": (
                self.mapping_join_possible_via_market_outcomes
            ),
            "market_outcomes_table_present": self.market_outcomes_table_present,
            "market_outcomes_with_clob_token_id": (
                self.market_outcomes_with_clob_token_id
            ),
            "markets_table_present": self.markets_table_present,
            "resolve_trade_to_outcome_helper_exists": (
                self.resolve_trade_to_outcome_helper_exists
            ),
            "mapping_helper_already_exists": self.mapping_helper_already_exists,
            "smallest_future_helper": self.smallest_future_helper,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class SourceTradeRealCoverageMappingAuditReport:
    """Full PR24W real-coverage + token→condition mapping audit report."""

    ready_to_wire_to_automation: bool
    ready_to_persist_decisions: bool
    ready_to_create_candidates: bool
    production_counts: dict[str, Any]
    db_path_inspected: Optional[str]

    source_trade_count: int
    raw_side_distribution: dict[str, int]
    canonical_side_distribution: dict[str, int]
    ingestion_side_inconsistency_present: bool

    coverage_bucket_counts: dict[str, int]
    sample_placeholder_count: int
    real_like_count: int
    effective_real_usable_coverage: int

    identifier_quality_counts: dict[str, int]
    has_token_id_count: int
    has_condition_id_count: int
    both_token_and_condition_count: int
    neither_token_nor_condition_count: int
    non_condition_placeholder_market_id_count: int

    pr24u_book_ready_count: int
    pr24v_gamma_ready_count: int
    both_ready_count: int
    neither_ready_count: int
    token_to_condition_mapping_needed_count: int

    copyability_evidence_readiness_counts: dict[str, int]

    token_condition_mapping_feasibility: TokenConditionMappingFeasibility
    ingestion_gap_summary: str

    findings: tuple[SourceTradeRealCoverageMappingFinding, ...]
    row_reports: tuple[SourceTradeCoverageRowReport, ...]
    recommended_next_step: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready_to_wire_to_automation": self.ready_to_wire_to_automation,
            "ready_to_persist_decisions": self.ready_to_persist_decisions,
            "ready_to_create_candidates": self.ready_to_create_candidates,
            "production_counts": self.production_counts,
            "db_path_inspected": self.db_path_inspected,
            "source_trade_count": self.source_trade_count,
            "raw_side_distribution": self.raw_side_distribution,
            "canonical_side_distribution": self.canonical_side_distribution,
            "ingestion_side_inconsistency_present": (
                self.ingestion_side_inconsistency_present
            ),
            "coverage_bucket_counts": self.coverage_bucket_counts,
            "sample_placeholder_count": self.sample_placeholder_count,
            "real_like_count": self.real_like_count,
            "effective_real_usable_coverage": self.effective_real_usable_coverage,
            "identifier_quality_counts": self.identifier_quality_counts,
            "has_token_id_count": self.has_token_id_count,
            "has_condition_id_count": self.has_condition_id_count,
            "both_token_and_condition_count": self.both_token_and_condition_count,
            "neither_token_nor_condition_count": (
                self.neither_token_nor_condition_count
            ),
            "non_condition_placeholder_market_id_count": (
                self.non_condition_placeholder_market_id_count
            ),
            "pr24u_book_ready_count": self.pr24u_book_ready_count,
            "pr24v_gamma_ready_count": self.pr24v_gamma_ready_count,
            "both_ready_count": self.both_ready_count,
            "neither_ready_count": self.neither_ready_count,
            "token_to_condition_mapping_needed_count": (
                self.token_to_condition_mapping_needed_count
            ),
            "copyability_evidence_readiness_counts": (
                self.copyability_evidence_readiness_counts
            ),
            "token_condition_mapping_feasibility": (
                self.token_condition_mapping_feasibility.to_dict()
            ),
            "ingestion_gap_summary": self.ingestion_gap_summary,
            "findings": [f.to_dict() for f in self.findings],
            "row_reports": [r.to_dict() for r in self.row_reports],
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


def _is_condition_id_like(value: Any) -> bool:
    """Return True if ``value`` is plausibly a Polymarket conditionId."""
    return bool(_CONDITION_ID_RE.match(_norm_text(value)))


# ── Sample / placeholder detection (report text ONLY) ───────────────────────
def _row_looks_sample_like(row: sqlite3.Row) -> bool:
    """Heuristic: does this source_trades row look seeded/sample/placeholder?

    Pure read-only inspection of a single row. Returns True when the wallet
    address, market identifier, source_trade_id, or source contains a known
    sample marker. This is REPORT TEXT ONLY — it does not classify eligibility,
    does not change behavior, and never mutates the row.
    """
    wallet = _norm_text(_row_get(row, "trader_address"))
    market = _norm_text(_row_get(row, "market_source_id"))
    trade_id = _norm_text(_row_get(row, "source_trade_id"))
    source = _norm_text(_row_get(row, "source"))
    low_wallet = wallet.lower()
    low_market = market.lower()
    low_trade = trade_id.lower()
    low_source = source.lower()
    for marker in _SAMPLE_WALLET_MARKERS:
        if marker in low_wallet:
            return True
    for marker in _SAMPLE_MARKET_MARKERS:
        if marker in low_market:
            return True
    for marker in _SAMPLE_TRADE_ID_MARKERS:
        if marker in low_trade:
            return True
    for marker in _SAMPLE_SOURCE_MARKERS:
        if marker in low_source and (not _is_condition_id_like(market)):
            # 'sample' source with a non-conditionId market => seeded sample
            return True
    return False


def _pick(row: sqlite3.Row, logical: str, field_map: dict[str, Optional[str]]) -> Any:
    col = field_map.get(logical)
    if col and col in row.keys():
        return row[col]
    for candidate in _SOURCE_FIELD_CANDIDATES.get(logical, ()):
        if candidate in row.keys():
            return row[candidate]
    return None


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


# ── Per-row coverage + identifier-mapping classification ─────────────────────
def _classify_row(
    row: sqlite3.Row,
    field_map: dict[str, Optional[str]],
) -> SourceTradeCoverageRowReport:
    raw_side = _pick(row, "side", field_map)
    canonical_side, side_status, side_reason = canonicalize_source_side(raw_side)
    source_trade_id = _pick(row, "source_trade_id", field_map)
    wallet_address = _pick(row, "trader_address", field_map)
    market_source_id = _pick(row, "market_source_id", field_map)
    token_id = _pick(row, "token_id", field_map)
    price = _maybe_float(_pick(row, "price", field_map))
    quantity = _maybe_float(_pick(row, "size", field_map))
    timestamp = _pick(row, "timestamp", field_map)
    is_sample_flag = _pick(row, "is_sample", field_map)

    notes: list[str] = []

    # Identifier quality
    has_token = token_id not in (None, "")
    has_condition = _is_condition_id_like(market_source_id)
    both = has_token and has_condition
    neither = (not has_token) and (not has_condition)
    non_condition_placeholder = (not has_condition) and _norm_text(
        market_source_id
    ) not in ("",) and (not neither)
    if has_token and has_condition:
        identifier_quality = "both"
    elif has_token and not has_condition:
        identifier_quality = "token_only"
    elif has_condition and not has_token:
        identifier_quality = "condition_only"
    else:
        identifier_quality = "neither"

    # Readiness axes
    pr24u_book_ready = has_token  # /book keys on token_id
    pr24v_gamma_ready = has_condition  # Gamma get_market keys on conditionId
    both_ready = pr24u_book_ready and pr24v_gamma_ready
    neither_ready = (not pr24u_book_ready) and (not pr24v_gamma_ready)
    # A token-only row can reach /book but cannot resolve Gamma market state
    # without a token→condition mapping.
    token_to_condition_mapping_needed = has_token and (not has_condition)

    # Sample vs real-like determination: a row is "real-like" if it carries at
    # least one production-shaped identifier (real conditionId-shaped market OR a
    # real token_id). The 4 seeded rows carry neither (sample-market-* + NULL
    # token), so they are sample_placeholder even though test_trade_1 also
    # carries is_sample=1 yet has real identifiers.
    looks_sample = _row_looks_sample_like(row)
    has_real_identifier = has_token or has_condition
    if looks_sample and (not has_real_identifier):
        sample_status = "sample_placeholder"
        sample_reason = (
            "seeded/sample/placeholder row: market_source_id is a non-condition "
            "placeholder and token_id is NULL; row carries sample markers "
            "(e.g. source/market/wallet/trade_id). is_sample flag = "
            f"{is_sample_flag!r}."
        )
    elif has_real_identifier:
        sample_status = "real_like"
        parts = []
        if has_condition:
            parts.append("real conditionId-shaped market_source_id")
        if has_token:
            parts.append("real token_id")
        sample_reason = (
            "real-like: row carries " + " and ".join(parts) + ". "
            f"(is_sample flag = {is_sample_flag!r}, but the identifiers are "
            "production-shaped and resolvable; this is the single usable row.)"
        )
    else:
        sample_status = "unknown"
        sample_reason = "could not classify as sample or real-like"

    # Coverage bucket (priority order)
    if sample_status == "sample_placeholder":
        coverage_bucket = "sample_placeholder"
    elif side_status == "invalid":
        coverage_bucket = "invalid_side_or_unsupported_side"
        notes.append("invalid side value; not copyable for v1")
    elif canonical_side == "SELL":
        coverage_bucket = "invalid_side_or_unsupported_side"
        notes.append("SELL side unsupported for v1 copyability")
    elif price is None or quantity is None:
        coverage_bucket = "real_like_unusable_missing_price_or_size"
        missing = []
        if price is None:
            missing.append("price")
        if quantity is None:
            missing.append("size/quantity")
        notes.append("missing required field(s): " + ", ".join(missing))
    elif both:
        coverage_bucket = "real_like_complete"
    elif has_token and not has_condition:
        coverage_bucket = "real_like_token_only"
        notes.append(
            "token-only: PR24U /book-ready but PR24V Gamma needs a "
            "token→condition mapping to resolve market state"
        )
    elif has_condition and not has_token:
        coverage_bucket = "real_like_condition_only"
        notes.append("condition-only: PR24V Gamma-ready; missing token_id for /book")
    else:
        # neither identifier: cannot attach any evidence path
        coverage_bucket = "real_like_missing_token_id"
        notes.append(
            "no usable identifier (no token_id, no conditionId-shaped market)"
        )

    # Copyability evidence readiness: BUY + price + size + >=1 identifier
    if canonical_side != "BUY":
        if side_status == "missing":
            copy_readiness = "blocked_missing_side"
        elif canonical_side == "SELL":
            copy_readiness = "blocked_sell_unsupported_v1"
        else:
            copy_readiness = "blocked_invalid_side"
    elif price is None or quantity is None:
        copy_readiness = "blocked_missing_price_or_size"
    elif neither:
        copy_readiness = "blocked_no_usable_identifier"
    elif both_ready:
        copy_readiness = "ready_both_paths"
    elif pr24u_book_ready:
        copy_readiness = "ready_pr24u_only_needs_mapping_for_pr24v"
    elif pr24v_gamma_ready:
        copy_readiness = "ready_pr24v_only_needs_token_for_pr24u"
    else:
        copy_readiness = "blocked_unknown"

    return SourceTradeCoverageRowReport(
        source_trade_id=(
            source_trade_id if source_trade_id not in (None, "") else None
        ),
        wallet_address=(
            wallet_address if wallet_address not in (None, "") else None
        ),
        market_source_id=(
            market_source_id if market_source_id not in (None, "") else None
        ),
        token_id=token_id if token_id not in (None, "") else None,
        raw_side=raw_side if raw_side not in (None, "") else None,
        canonical_side=canonical_side,
        price=price,
        quantity=quantity,
        timestamp=timestamp if timestamp not in (None, "") else None,
        sample_placeholder_status=sample_status,
        sample_reason=sample_reason,
        coverage_bucket=coverage_bucket,
        identifier_quality=identifier_quality,
        has_token_id=has_token,
        has_condition_id=has_condition,
        both_token_and_condition=both,
        neither_token_nor_condition=neither,
        non_condition_placeholder_market_id=bool(non_condition_placeholder),
        pr24u_book_ready=pr24u_book_ready,
        pr24v_gamma_ready=pr24v_gamma_ready,
        both_ready=both_ready,
        neither_ready=neither_ready,
        token_to_condition_mapping_needed=token_to_condition_mapping_needed,
        copyability_evidence_readiness=copy_readiness,
        notes=tuple(notes),
    )


# ── Token→condition mapping feasibility (read-only join assessment) ─────────
def _assess_token_condition_mapping(
    conn: sqlite3.Connection,
) -> TokenConditionMappingFeasibility:
    """Assess, read-only, whether token_id → condition_id can be resolved.

    The repo already persists ``market_outcomes.clob_token_id`` (v7) and joins
    it to ``markets.source_id`` (the conditionId). The existing
    ``resolve_trade_to_outcome`` helper (``engine/trade_resolution.py``) already
    performs exactly this read-only join: ``token_id → market_outcomes WHERE
    clob_token_id = ? → markets.source_id``. This audit only CHECKS feasibility;
    it does NOT write a mapping table or backfill.
    """
    mo_present = _table_exists(conn, "market_outcomes")
    m_present = _table_exists(conn, "markets")

    mo_with_token: Optional[int] = None
    if mo_present:
        try:
            mo_with_token = conn.execute(
                "SELECT COUNT(*) FROM market_outcomes "
                "WHERE clob_token_id IS NOT NULL AND clob_token_id != ''"
            ).fetchone()[0]
        except sqlite3.Error:
            mo_with_token = None

    join_possible = bool(mo_present and m_present and mo_with_token)
    helper_exists = _resolve_trade_to_outcome_helper_exists()

    notes: list[str] = []
    if join_possible:
        notes.append(
            f"token→condition join is feasible now: market_outcomes has "
            f"{mo_with_token} row(s) with a populated clob_token_id that can "
            f"join to markets.source_id (conditionId)."
        )
    else:
        notes.append(
            "token→condition join is NOT feasible until market_outcomes is "
            "populated with clob_token_id values for the tokens of interest."
        )
    if helper_exists:
        notes.append(
            "resolve_trade_to_outcome (engine/trade_resolution.py) already "
            "resolves token_id → market_outcomes → markets.source_id "
            "(conditionId) read-only; no new writer is required to READ the "
            "mapping."
        )

    smallest_helper = (
        "Read-only helper map_token_to_condition_id(conn, token_id) -> Optional[str]: "
        "SELECT m.source_id FROM market_outcomes mo JOIN markets m ON "
        "m.id = mo.market_id WHERE mo.clob_token_id = ? LIMIT 1. This reuses "
        "the existing join; it is NOT a persistence writer and must not backfill "
        "production rows. PR24W does NOT implement it."
    )

    return TokenConditionMappingFeasibility(
        mapping_join_possible_via_market_outcomes=join_possible,
        market_outcomes_table_present=mo_present,
        market_outcomes_with_clob_token_id=mo_with_token,
        markets_table_present=m_present,
        resolve_trade_to_outcome_helper_exists=helper_exists,
        mapping_helper_already_exists=helper_exists,
        smallest_future_helper=smallest_helper,
        notes=tuple(notes),
    )


def _resolve_trade_to_outcome_helper_exists() -> bool:
    """Return True if the existing token→outcome resolver module is importable.

    This is a read-only static check (no DB access, no network). The resolver
    itself issues SELECTs only.
    """
    try:
        from polycopy.engine.trade_resolution import resolve_trade_to_outcome  # noqa: F401
        return True
    except Exception:
        return False


# ── Report builder ─────────────────────────────────────────────────────────
def build_source_trade_real_coverage_mapping_audit(
    conn_or_db: Any,
    *,
    limit: int = 50,
    db_path: Optional[str] = None,
) -> SourceTradeRealCoverageMappingAuditReport:
    """Build a read-only source-trade real-coverage + mapping audit.

    ``conn_or_db`` must be an already-open ``sqlite3.Connection`` opened
    read-only (``mode=ro``). The function performs only SELECT / PRAGMA reads
    and never mutates the database.
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

        row_reports: list[SourceTradeCoverageRowReport] = [
            _classify_row(row, field_map) for row in source_rows
        ]

        # --- Distributions ---
        raw_side_distribution: Counter[str] = Counter()
        canonical_side_distribution: Counter[str] = Counter()
        for rr in row_reports:
            key = "<NULL>" if rr.raw_side is None else str(rr.raw_side)
            raw_side_distribution[key] += 1
            if rr.canonical_side is not None:
                canonical_side_distribution[rr.canonical_side] += 1

        ingestion_inconsistent = (
            sum(1 for k in raw_side_distribution if k.lower() == "buy") > 1
            or sum(1 for k in raw_side_distribution if k.lower() == "sell") > 1
        )

        # --- Coverage buckets ---
        coverage_bucket_counts: Counter[str] = Counter()
        for rr in row_reports:
            coverage_bucket_counts[rr.coverage_bucket] += 1
        sample_placeholder_count = coverage_bucket_counts.get("sample_placeholder", 0)
        real_like_count = sum(
            1 for rr in row_reports if rr.sample_placeholder_status == "real_like"
        )
        # Effective REAL usable coverage: real-like rows that can actually attach
        # evidence (BUY + price + size + at least one identifier).
        effective_real_usable = sum(
            1
            for rr in row_reports
            if rr.sample_placeholder_status == "real_like"
            and rr.copyability_evidence_readiness
            in (
                "ready_both_paths",
                "ready_pr24u_only_needs_mapping_for_pr24v",
                "ready_pr24v_only_needs_token_for_pr24u",
            )
        )

        # --- Identifier quality / readiness ---
        identifier_quality_counts: Counter[str] = Counter()
        for rr in row_reports:
            identifier_quality_counts[rr.identifier_quality] += 1
        has_token_count = sum(1 for rr in row_reports if rr.has_token_id)
        has_condition_count = sum(1 for rr in row_reports if rr.has_condition_id)
        both_count = sum(1 for rr in row_reports if rr.both_token_and_condition)
        neither_count = sum(
            1 for rr in row_reports if rr.neither_token_nor_condition
        )
        non_cond_ph_count = sum(
            1 for rr in row_reports if rr.non_condition_placeholder_market_id
        )
        pr24u_ready_count = sum(1 for rr in row_reports if rr.pr24u_book_ready)
        pr24v_ready_count = sum(1 for rr in row_reports if rr.pr24v_gamma_ready)
        both_ready_count = sum(1 for rr in row_reports if rr.both_ready)
        neither_ready_count = sum(1 for rr in row_reports if rr.neither_ready)
        mapping_needed_count = sum(
            1 for rr in row_reports if rr.token_to_condition_mapping_needed
        )
        copy_readiness_counts: Counter[str] = Counter()
        for rr in row_reports:
            copy_readiness_counts[rr.copyability_evidence_readiness] += 1

        # --- Token→condition mapping feasibility (read-only) ---
        mapping_feas = _assess_token_condition_mapping(conn)

        # --- Ingestion gap summary ---
        ingestion_gap_summary = _build_ingestion_gap_summary(
            source_trade_count=len(row_reports),
            sample_placeholder_count=sample_placeholder_count,
            real_like_count=real_like_count,
            has_token_count=has_token_count,
            has_condition_count=has_condition_count,
            both_count=both_count,
            effective_real_usable=effective_real_usable,
        )

        # --- Findings ---
        findings: list[SourceTradeRealCoverageMappingFinding] = []

        if sample_placeholder_count:
            findings.append(
                SourceTradeRealCoverageMappingFinding(
                    key="sample_placeholder_rows_present",
                    severity="info",
                    summary=(
                        f"Of {len(row_reports)} source_trades, {sample_placeholder_count} "
                        "are seeded/sample/placeholder rows (non-condition market ids "
                        "like 'sample-market-*' + NULL token_id + sample markers). "
                        "Real usable production-like coverage is effectively "
                        f"n={effective_real_usable}. This is report text only; the rows "
                        "are NOT deleted, mutated, backfilled, or normalized."
                    ),
                    count=sample_placeholder_count,
                    evidence={
                        "source_trade_count": len(row_reports),
                        "sample_placeholder_count": sample_placeholder_count,
                        "real_like_count": real_like_count,
                        "effective_real_usable_coverage": effective_real_usable,
                    },
                    recommendation=(
                        "Treat the single real-like row (test_trade_1: real "
                        "conditionId + real token_id) as the only currently-usable "
                        "evidence target. A future ingestion PR should populate real "
                        "wallet trades with both token_id and conditionId-shaped "
                        "market_source_id for broader real coverage."
                    ),
                )
            )

        if mapping_needed_count:
            findings.append(
                SourceTradeRealCoverageMappingFinding(
                    key="token_only_rows_need_mapping",
                    severity="warning",
                    summary=(
                        f"{mapping_needed_count} row(s) carry a token_id but NO "
                        "conditionId-shaped market_source_id. They are PR24U /book-ready "
                        "but CANNOT resolve PR24V Gamma market state without a "
                        "token→condition mapping."
                    ),
                    count=mapping_needed_count,
                    evidence={"token_to_condition_mapping_needed_count": mapping_needed_count},
                    recommendation=(
                        "Wire the existing read-only token→condition join "
                        "(resolve_trade_to_outcome / market_outcomes→markets) as a "
                        "dedicated helper before PR24V market-state can attach to "
                        "token-only rows. Do NOT write a mapping table in PR24W."
                    ),
                )
            )
        else:
            findings.append(
                SourceTradeRealCoverageMappingFinding(
                    key="no_token_only_rows_currently",
                    severity="info",
                    summary=(
                        "Current data has ZERO token-only rows (the 4 sample rows "
                        "carry neither token nor condition; the 1 real-like row "
                        "carries both). The token→condition mapping gap is a future "
                        "risk, not a present blocker for the single usable row."
                    ),
                    count=0,
                    evidence={
                        "token_to_condition_mapping_needed_count": mapping_needed_count,
                        "has_token_count": has_token_count,
                        "has_condition_count": has_condition_count,
                        "both_count": both_count,
                    },
                    recommendation=(
                        "No mapping writer needed for current data, but the helper "
                        "should be added before real token-only ingestion lands."
                    ),
                )
            )

        if ingestion_inconsistent:
            findings.append(
                SourceTradeRealCoverageMappingFinding(
                    key="ingestion_side_inconsistency",
                    severity="warning",
                    summary=(
                        "source_trades.side contains multiple exact string forms for the "
                        "same logical side (e.g. buy vs BUY). PR24T normalization guard "
                        "handles future writes; existing production rows were intentionally "
                        "not backfilled. This PR does not normalize them."
                    ),
                    count=sum(
                        raw_side_distribution.get(k, 0)
                        for k in raw_side_distribution
                        if k.lower() in ("buy", "sell")
                    ),
                    evidence={
                        "raw_side_distribution": dict(raw_side_distribution),
                        "canonical_side_distribution": dict(canonical_side_distribution),
                    },
                    recommendation=(
                        "Leave existing rows as-is (no backfill per PR24T). Future "
                        "writes normalize via normalize_side_for_persistence."
                    ),
                )
            )

        findings.append(
            SourceTradeRealCoverageMappingFinding(
                key="token_condition_mapping_feasibility",
                severity="info" if mapping_feas.mapping_join_possible_via_market_outcomes
                else "warning",
                summary=(
                    "Token→condition mapping is "
                    + (
                        "ALREADY feasible read-only via the existing "
                        "market_outcomes.clob_token_id → markets.source_id join "
                        "(which resolve_trade_to_outcome already performs). "
                        if mapping_feas.mapping_join_possible_via_market_outcomes
                        else "NOT yet feasible (market_outcomes.clob_token_id not populated). "
                    )
                    + "PR24W does NOT implement a production mapping writer."
                ),
                count=mapping_feas.market_outcomes_with_clob_token_id,
                evidence=mapping_feas.to_dict(),
                recommendation=(
                    "Reuse the existing resolver; add a thin read-only "
                    "map_token_to_condition_id helper when token-only rows appear. "
                    "No backfill of production rows."
                ),
            )
        )

        recommended_next_step = (
            "PR24W is report-only. It proves effective real coverage is n=1 and that "
            "a token→condition mapping path already exists read-only. Next: (a) a "
            "guarded ingestion PR that populates REAL wallet trades with both token_id "
            "and conditionId-shaped market_source_id; (b) a thin read-only "
            "map_token_to_condition_id helper for token-only rows; (c) only then, a "
            "persistence/scoring PR that lands real coverage into candidates/signals. "
            "Do NOT wire automation or persist decisions until those land and are reviewed."
        )

        return SourceTradeRealCoverageMappingAuditReport(
            ready_to_wire_to_automation=False,
            ready_to_persist_decisions=False,
            ready_to_create_candidates=False,
            production_counts=production_counts,
            db_path_inspected=db_path,
            source_trade_count=len(row_reports),
            raw_side_distribution=dict(raw_side_distribution),
            canonical_side_distribution=dict(canonical_side_distribution),
            ingestion_side_inconsistency_present=ingestion_inconsistent,
            coverage_bucket_counts=dict(coverage_bucket_counts),
            sample_placeholder_count=sample_placeholder_count,
            real_like_count=real_like_count,
            effective_real_usable_coverage=effective_real_usable,
            identifier_quality_counts=dict(identifier_quality_counts),
            has_token_id_count=has_token_count,
            has_condition_id_count=has_condition_count,
            both_token_and_condition_count=both_count,
            neither_token_nor_condition_count=neither_count,
            non_condition_placeholder_market_id_count=non_cond_ph_count,
            pr24u_book_ready_count=pr24u_ready_count,
            pr24v_gamma_ready_count=pr24v_ready_count,
            both_ready_count=both_ready_count,
            neither_ready_count=neither_ready_count,
            token_to_condition_mapping_needed_count=mapping_needed_count,
            copyability_evidence_readiness_counts=dict(copy_readiness_counts),
            token_condition_mapping_feasibility=mapping_feas,
            ingestion_gap_summary=ingestion_gap_summary,
            findings=tuple(findings),
            row_reports=tuple(row_reports[: max(limit, 0)] or row_reports),
            recommended_next_step=recommended_next_step,
        )
    finally:
        conn.row_factory = old_factory


def _build_ingestion_gap_summary(
    *,
    source_trade_count: int,
    sample_placeholder_count: int,
    real_like_count: int,
    has_token_count: int,
    has_condition_count: int,
    both_count: int,
    effective_real_usable: int,
) -> str:
    return (
        f"source_trades total = {source_trade_count}; "
        f"sample/placeholder = {sample_placeholder_count}; "
        f"real-like = {real_like_count}; "
        f"effective REAL usable coverage = n={effective_real_usable}. "
        "Why n=1: 4 of 5 rows are seeded sample/placeholder rows "
        "(source='sample', is_sample=1, market_source_id='sample-market-*', "
        "token_id=NULL) created by scripts/run_scan.py _get_sample_trades; only "
        "test_trade_1 carries real identifiers (a conditionId-shaped "
        "market_source_id AND a real token_id). Real rows are NOT being collected "
        "at scale, and the sample rows were intentionally seeded and are still "
        "present. To unlock persistence/scoring, a real source_trade must have: "
        "canonical BUY side, parseable price, parseable size/quantity, AND at "
        "least one of (token_id for PR24U /book, conditionId-shaped "
        "market_source_id for PR24V Gamma). A token-only row additionally needs a "
        "token→condition mapping before PR24V market-state can attach."
    )


# ── Human rendering ─────────────────────────────────────────────────────────
def report_to_human(report: SourceTradeRealCoverageMappingAuditReport) -> str:
    lines: list[str] = []
    lines.append(
        "SOURCE-TRADE REAL COVERAGE + TOKEN→CONDITION MAPPING AUDIT — "
        "READ ONLY / REPORT-ONLY"
    )
    lines.append("")
    lines.append(f"ready_to_wire_to_automation = {report.ready_to_wire_to_automation}")
    lines.append(f"ready_to_persist_decisions = {report.ready_to_persist_decisions}")
    lines.append(f"ready_to_create_candidates = {report.ready_to_create_candidates}")
    lines.append("")
    lines.append(f"DB path inspected: {report.db_path_inspected}")
    lines.append("")
    lines.append("== Production counts (read-only; must be unchanged by this PR) ==")
    for k, v in report.production_counts.items():
        lines.append(f"  {k}: {v}")
    lines.append("")

    lines.append(f"== Source trades inspected: {report.source_trade_count} ==")
    lines.append(f"  sample_placeholder: {report.sample_placeholder_count}")
    lines.append(f"  real_like: {report.real_like_count}")
    lines.append(
        f"  effective_real_usable_coverage: n={report.effective_real_usable_coverage}"
    )
    lines.append("")

    lines.append("== Coverage bucket counts ==")
    for k, v in sorted(
        report.coverage_bucket_counts.items(), key=lambda x: (-x[1], x[0])
    ):
        lines.append(f"  {k}: {v}")
    lines.append("")

    lines.append("== Raw source side distribution (exact casing) ==")
    for k, v in sorted(
        report.raw_side_distribution.items(), key=lambda x: (-x[1], x[0])
    ):
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("== Canonical side distribution ==")
    if report.canonical_side_distribution:
        for k, v in sorted(
            report.canonical_side_distribution.items(), key=lambda x: (-x[1], x[0])
        ):
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(
        f"== INGESTION SIDE NORMALIZATION AUDIT ==\n"
        f"  ingestion_side_inconsistency_present = "
        f"{report.ingestion_side_inconsistency_present}"
    )
    if report.ingestion_side_inconsistency_present:
        lines.append(
            "  NOTE: mixed casing present; NOT backfilled (PR24T left existing rows)."
        )
    lines.append("")

    lines.append("== Identifier quality + readiness counts ==")
    lines.append(f"  has_token_id: {report.has_token_id_count}")
    lines.append(f"  has_condition_id: {report.has_condition_id_count}")
    lines.append(f"  both_token_and_condition: {report.both_token_and_condition_count}")
    lines.append(
        f"  neither_token_nor_condition: {report.neither_token_nor_condition_count}"
    )
    lines.append(
        f"  non_condition_placeholder_market_id: "
        f"{report.non_condition_placeholder_market_id_count}"
    )
    lines.append(f"  pr24u_book_ready (/book, token_id): {report.pr24u_book_ready_count}")
    lines.append(
        f"  pr24v_gamma_ready (Gamma, conditionId): {report.pr24v_gamma_ready_count}"
    )
    lines.append(f"  both_ready: {report.both_ready_count}")
    lines.append(f"  neither_ready: {report.neither_ready_count}")
    lines.append(
        f"  token_to_condition_mapping_needed: "
        f"{report.token_to_condition_mapping_needed_count}"
    )
    lines.append("")
    lines.append("== Copyability evidence readiness counts ==")
    for k, v in sorted(
        report.copyability_evidence_readiness_counts.items(),
        key=lambda x: (-x[1], x[0]),
    ):
        lines.append(f"  {k}: {v}")
    lines.append("")

    feas = report.token_condition_mapping_feasibility
    lines.append("== Token→Condition mapping feasibility (read-only) ==")
    lines.append(
        f"  mapping_join_possible_via_market_outcomes = "
        f"{feas.mapping_join_possible_via_market_outcomes}"
    )
    lines.append(f"  market_outcomes_table_present = {feas.market_outcomes_table_present}")
    lines.append(
        f"  market_outcomes_with_clob_token_id = {feas.market_outcomes_with_clob_token_id}"
    )
    lines.append(f"  markets_table_present = {feas.markets_table_present}")
    lines.append(
        f"  resolve_trade_to_outcome_helper_exists = "
        f"{feas.resolve_trade_to_outcome_helper_exists}"
    )
    lines.append(f"  mapping_helper_already_exists = {feas.mapping_helper_already_exists}")
    lines.append(f"  smallest_future_helper: {feas.smallest_future_helper}")
    for n in feas.notes:
        lines.append(f"    note: {n}")
    lines.append("")

    lines.append("== Ingestion gap summary ==")
    lines.append(f"  {report.ingestion_gap_summary}")
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

    lines.append("== Per-row report ==")
    for rr in report.row_reports:
        lines.append(
            f"  source_trade_id={rr.source_trade_id} wallet={rr.wallet_address} "
            f"market={rr.market_source_id} token={rr.token_id} side={rr.raw_side}"
        )
        lines.append(
            f"    canonical={rr.canonical_side} price={rr.price} size={rr.quantity} "
            f"ts={rr.timestamp}"
        )
        lines.append(
            f"    sample_status={rr.sample_placeholder_status} "
            f"bucket={rr.coverage_bucket} id_quality={rr.identifier_quality}"
        )
        lines.append(
            f"    has_token={rr.has_token_id} has_condition={rr.has_condition_id} "
            f"pr24u_ready={rr.pr24u_book_ready} pr24v_ready={rr.pr24v_gamma_ready} "
            f"both_ready={rr.both_ready}"
        )
        lines.append(
            f"    mapping_needed={rr.token_to_condition_mapping_needed} "
            f"copy_readiness={rr.copyability_evidence_readiness}"
        )
        if rr.sample_reason:
            lines.append(f"    reason: {rr.sample_reason}")
        for n in rr.notes:
            lines.append(f"    note: {n}")
    lines.append("")

    lines.append("== Recommended next step ==")
    lines.append(f"  {report.recommended_next_step}")
    lines.append("")
    lines.append(
        "This report performs NO production writes: no decisions, candidates, "
        "paper signals, snapshots, orders, or positions. Default mode is "
        "read-only / dry-run / report-only."
    )
    return "\n".join(lines)


def report_to_dict(report: SourceTradeRealCoverageMappingAuditReport) -> dict[str, Any]:
    return report.to_dict()


__all__ = [
    "SourceTradeCoverageRowReport",
    "SourceTradeRealCoverageMappingFinding",
    "TokenConditionMappingFeasibility",
    "SourceTradeRealCoverageMappingAuditReport",
    "build_source_trade_real_coverage_mapping_audit",
    "report_to_human",
    "report_to_dict",
]
