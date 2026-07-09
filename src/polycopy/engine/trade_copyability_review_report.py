"""PR24Q — Trade Copyability review/report (read-only).

This module produces a REVIEW REPORT for Trade Copyability Score v1
decisions and the raw source trades they are derived from. It is a PURE,
READ-ONLY report. It does NOT:

  * import ``polycopy.db.database`` (no ORM / no write path),
  * write to any database,
  * rebuild or retune the Trade Copyability v1 formula,
  * wire anything into automation,
  * create copy candidates or paper signals.

It inspects the *existing* Trade Copyability Score v1 implementation
(the patched/defensive v1 delivered by PR24P) and reports:

  1. Production counts for the relevant tables (if they exist).
  2. Exact raw side distribution (source_trades) and decision side
     distribution (trade_copyability_decisions), without normalization.
  3. Side-casing findings (raw source casing is mixed: buy / BUY).
  4. Verdict / score distribution from trade_copyability_decisions.
  5. Incomplete / rejection reason analysis (JSON fields).
  6. Price-evidence review (deterioration + v16 trace columns).
  7. Depth / fill / spread review (including the PART-7 80% rule).
  8. Duration review (PART-8 hard exclusions).
  9. Side-support review (SELL must never be copy_candidate/watchlist).
  10. Snapshot-timing review (PART-9 impossible point-in-time).
  11. Later-outcome / paper-result review (only if data exists).

``ready_to_wire_to_automation`` is ALWAYS ``False``. This report recommends
a PR24R bridge audit/reconciliation before any paper_signal wiring.

The caller provides an already-open read-only ``sqlite3.Connection``
(opened with ``mode=ro``). The module never opens a connection itself.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

VERDICT_INCOMPLETE = "incomplete"
VERDICT_WATCHLIST = "watchlist"
VERDICT_COPY_CANDIDATE = "copy_candidate"
VERDICT_SKIP = "skip"

# v1 decisions that are eligible (passed the strict BUY-only scorer).
_ELIGIBLE_VERDICTS = {VERDICT_COPY_CANDIDATE, VERDICT_WATCHLIST}

# Reason tokens the report tracks (from PR24P hardening).
_REASON_TOKENS = (
    "price_deterioration_pct",
    "PRICE_DETERIORATION_TRACE_MISMATCH",
    "DEPTH_NOT_CAPTURED",
    "DEPTH_LEVELS_MALFORMED",
    "DEPTH_SNAPSHOT_MISMATCH",
    "DEPTH_INSUFFICIENT_FOR_STAKE",
    "partial_fill_below_copy_candidate_threshold",
    "duration_excluded_short",
    "duration_excluded_long",
    "sell_side_copyability_not_supported_v1",
    "invalid_side",
    "missing_side",
    "snapshot_before_source_trade",
    "snapshot_after_evaluation",
    "invalid_price_snapshot_timestamp",
)

# Tables whose counts are reported (if they exist).
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

# Later-outcome tables (read-only join/summary if present).
_LATER_OUTCOME_TABLES = (
    "settlement_accounting_ledger",
    "paper_signal_decisions",
    "exit_experiment_registrations",
)

RECOMMENDED_NEXT_STEP = (
    "PR24R should add a bridge audit/reconciliation before any paper_signal "
    "wiring. The bridge must canonicalize raw source side casing "
    "(buy/BUY -> BUY, sell/SELL -> SELL) before calling the strict Trade "
    "Copyability v1 scorer."
)


@dataclass(frozen=True)
class TradeCopyabilityReviewFinding:
    """A single review finding."""

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
class TradeCopyabilityReviewReport:
    """Full PR24Q review report (read-only)."""

    formula_name: str
    formula_version: str
    ready_to_wire_to_automation: bool
    production_counts: dict[str, Any]
    source_side_distribution: dict[str, int]
    decision_side_distribution: dict[str, int]
    source_side_casing_findings: tuple[TradeCopyabilityReviewFinding, ...]
    incomplete_reason_counts: dict[str, int]
    rejection_reason_counts: dict[str, int]
    verdict_counts: dict[str, int]
    score_distribution: dict[str, Any]
    price_evidence_findings: tuple[TradeCopyabilityReviewFinding, ...]
    depth_fill_spread_findings: tuple[TradeCopyabilityReviewFinding, ...]
    duration_findings: tuple[TradeCopyabilityReviewFinding, ...]
    side_support_findings: tuple[TradeCopyabilityReviewFinding, ...]
    snapshot_timing_findings: tuple[TradeCopyabilityReviewFinding, ...]
    later_outcome_findings: tuple[TradeCopyabilityReviewFinding, ...]
    recommended_next_step: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "formula_name": self.formula_name,
            "formula_version": self.formula_version,
            "ready_to_wire_to_automation": self.ready_to_wire_to_automation,
            "production_counts": self.production_counts,
            "source_side_distribution": self.source_side_distribution,
            "decision_side_distribution": self.decision_side_distribution,
            "source_side_casing_findings": [
                f.to_dict() for f in self.source_side_casing_findings
            ],
            "incomplete_reason_counts": self.incomplete_reason_counts,
            "rejection_reason_counts": self.rejection_reason_counts,
            "verdict_counts": self.verdict_counts,
            "score_distribution": self.score_distribution,
            "price_evidence_findings": [
                f.to_dict() for f in self.price_evidence_findings
            ],
            "depth_fill_spread_findings": [
                f.to_dict() for f in self.depth_fill_spread_findings
            ],
            "duration_findings": [f.to_dict() for f in self.duration_findings],
            "side_support_findings": [
                f.to_dict() for f in self.side_support_findings
            ],
            "snapshot_timing_findings": [
                f.to_dict() for f in self.snapshot_timing_findings
            ],
            "later_outcome_findings": [
                f.to_dict() for f in self.later_outcome_findings
            ],
            "recommended_next_step": self.recommended_next_step,
        }


# --------------------------------------------------------------------------
# DB helpers (read-only; caller owns the connection)
# --------------------------------------------------------------------------


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


def _fetch_rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    if not _table_exists(conn, table):
        return []
    return list(conn.execute(f"SELECT * FROM {table}"))


def _safe_count(conn: sqlite3.Connection, table: str) -> Optional[int]:
    if not _table_exists(conn, table):
        return None
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return None


def _json_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return [str(value)]
    if parsed is None:
        return []
    if isinstance(parsed, list):
        return parsed
    return [parsed]


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _maybe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Report builder
# --------------------------------------------------------------------------


def build_trade_copyability_review_report(
    conn_or_db: Any,
    *,
    limit: int = 20,
    formula_name: str = "trade_copyability",
    formula_version: str = "1",
) -> TradeCopyabilityReviewReport:
    """Build a read-only Trade Copyability review report.

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
        source_rows = _fetch_rows(conn, "source_trades")
        decision_rows = _fetch_rows(conn, "trade_copyability_decisions")
        candidate_rows = _fetch_rows(conn, "copy_candidates")
        paper_rows = _fetch_rows(conn, "paper_signal_decisions")
        ledger_rows = _fetch_rows(conn, "settlement_accounting_ledger")
        exit_rows = _fetch_rows(conn, "exit_experiment_registrations")

        production_counts = {
            t: _safe_count(conn, t) for t in _COUNT_TABLES
        }

        source_side_distribution = _side_distribution(source_rows, "side")
        decision_side_distribution = _side_distribution(decision_rows, "side")

        # --- PART 3: side casing findings ---
        casing_findings = _side_casing_findings(source_side_distribution)

        # --- PART 4/5: verdict, score, reasons ---
        verdict_counts = _verdict_counts(decision_rows)
        score_distribution = _score_distribution(decision_rows)
        incomplete_reason_counts = _reason_counts(decision_rows, "missing_essentials_json")
        rejection_reason_counts = _reason_counts(decision_rows, "rejection_reasons_json")

        # --- PART 6: price evidence findings ---
        price_findings = _price_evidence_findings(decision_rows)

        # --- PART 7: depth/fill/spread findings ---
        depth_findings = _depth_fill_spread_findings(decision_rows)

        # --- PART 8: duration findings ---
        duration_findings = _duration_findings(decision_rows)

        # --- PART 9: side support findings ---
        side_findings = _side_support_findings(
            source_side_distribution,
            decision_side_distribution,
            candidate_rows,
        ) + _side_support_blocker_scan(decision_rows)

        # --- PART 10: snapshot timing findings ---
        snapshot_findings = _snapshot_timing_findings(decision_rows)

        # --- PART 11: later outcome findings ---
        later_findings = _later_outcome_findings(
            decision_rows, paper_rows, ledger_rows, exit_rows
        )

        return TradeCopyabilityReviewReport(
            formula_name=formula_name,
            formula_version=formula_version,
            ready_to_wire_to_automation=False,
            production_counts=production_counts,
            source_side_distribution=source_side_distribution,
            decision_side_distribution=decision_side_distribution,
            source_side_casing_findings=tuple(casing_findings),
            incomplete_reason_counts=dict(incomplete_reason_counts),
            rejection_reason_counts=dict(rejection_reason_counts),
            verdict_counts=verdict_counts,
            score_distribution=score_distribution,
            price_evidence_findings=tuple(price_findings),
            depth_fill_spread_findings=tuple(depth_findings),
            duration_findings=tuple(duration_findings),
            side_support_findings=tuple(side_findings),
            snapshot_timing_findings=tuple(snapshot_findings),
            later_outcome_findings=tuple(later_findings),
            recommended_next_step=RECOMMENDED_NEXT_STEP,
        )
    finally:
        conn.row_factory = old_factory


# --------------------------------------------------------------------------
# Distribution / reason helpers
# --------------------------------------------------------------------------


def _side_distribution(rows: Iterable[sqlite3.Row], column: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        raw = _row_get(row, column, None)
        key = "<NULL>" if raw is None else str(raw)
        counter[key] += 1
    return dict(counter)


def _verdict_counts(rows: Iterable[sqlite3.Row]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        counter[_norm(_row_get(row, "verdict")) or "<unknown>"] += 1
    return dict(counter)


def _score_distribution(rows: Iterable[sqlite3.Row]) -> dict[str, Any]:
    if not rows:
        return {
            "has_decisions": False,
            "min": None,
            "max": None,
            "avg": None,
            "buckets": {
                "0": 0,
                "1_49.999": 0,
                "50_69.999": 0,
                "ge_70": 0,
            },
            "copy_candidate": 0,
            "watchlist": 0,
            "skip": 0,
            "incomplete": 0,
        }
    scores: list[float] = []
    buckets = {"0": 0, "1_49.999": 0, "50_69.999": 0, "ge_70": 0}
    copy_candidate = watchlist = skip = incomplete = 0
    for row in rows:
        score = _maybe_float(_row_get(row, "final_score"))
        verdict = _norm(_row_get(row, "verdict"))
        if score is not None:
            scores.append(score)
            if score <= 0.0:
                buckets["0"] += 1
            elif score < 50.0:
                buckets["1_49.999"] += 1
            elif score < 70.0:
                buckets["50_69.999"] += 1
            else:
                buckets["ge_70"] += 1
        if verdict == VERDICT_COPY_CANDIDATE:
            copy_candidate += 1
        elif verdict == VERDICT_WATCHLIST:
            watchlist += 1
        elif verdict == VERDICT_SKIP:
            skip += 1
        elif verdict == VERDICT_INCOMPLETE:
            incomplete += 1
    return {
        "has_decisions": True,
        "min": min(scores) if scores else None,
        "max": max(scores) if scores else None,
        "avg": (sum(scores) / len(scores)) if scores else None,
        "buckets": buckets,
        "copy_candidate": copy_candidate,
        "watchlist": watchlist,
        "skip": skip,
        "incomplete": incomplete,
    }


def _reason_counts(rows: Iterable[sqlite3.Row], column: str) -> Counter[str]:
    counter: Counter[str] = Counter()
    for row in rows:
        for reason in _json_list(_row_get(row, column)):
            if str(reason):
                counter[str(reason)] += 1
    return counter


# --------------------------------------------------------------------------
# PART 3: side casing findings
# --------------------------------------------------------------------------


def _side_casing_findings(source_side_distribution: dict[str, int]) -> list[TradeCopyabilityReviewFinding]:
    findings: list[TradeCopyabilityReviewFinding] = []
    # Normalize raw casing to intent buckets.
    buy_intent = sum(
        v for k, v in source_side_distribution.items()
        if k.lower() == "buy"
    )
    sell_intent = sum(
        v for k, v in source_side_distribution.items()
        if k.lower() == "sell"
    )
    has_mixed_buy = (
        "buy" in source_side_distribution and "BUY" in source_side_distribution
    )
    if has_mixed_buy:
        findings.append(TradeCopyabilityReviewFinding(
            key="source_side_casing_mixed",
            severity="warning",
            summary=(
                "source_trades contains mixed side casing; future bridge must "
                "canonicalize raw BUY intent before strict scorer."
            ),
            count=buy_intent,
            evidence={
                "distribution": dict(source_side_distribution),
                "buy_intent": buy_intent,
                "sell_intent": sell_intent,
            },
            recommendation=(
                "PR24R bridge should canonicalize raw buy/BUY to exact BUY and "
                "raw sell/SELL to exact SELL before calling Trade Copyability v1."
            ),
        ))
    elif buy_intent and "buy" in source_side_distribution:
        # Only lowercase present — still worth noting raw casing is non-canonical.
        findings.append(TradeCopyabilityReviewFinding(
            key="source_side_casing_non_canonical",
            severity="info",
            summary=(
                "source_trades uses lowercase 'buy' rather than canonical 'BUY'; "
                "future bridge should canonicalize before strict scorer."
            ),
            count=buy_intent,
            evidence={"distribution": dict(source_side_distribution)},
            recommendation=(
                "PR24R bridge should canonicalize raw buy/BUY to exact BUY "
                "before calling Trade Copyability v1."
            ),
        ))
    return findings


# --------------------------------------------------------------------------
# PART 6: price evidence findings
# --------------------------------------------------------------------------


def _price_evidence_findings(rows: list[sqlite3.Row]) -> list[TradeCopyabilityReviewFinding]:
    findings: list[TradeCopyabilityReviewFinding] = []
    if not rows:
        findings.append(TradeCopyabilityReviewFinding(
            key="no_trade_decisions_yet",
            severity="info",
            summary="No trade_copyability_decisions rows yet; price review is empty.",
            count=0,
        ))
        return findings

    missing_deterioration = 0
    trace_present = 0
    trace_absent = 0
    mismatch_rejections = 0
    out_of_range = 0
    missing_snapshot_id = 0
    missing_depth_hash = 0
    for row in rows:
        if _maybe_float(_row_get(row, "price_deterioration_pct")) is None:
            missing_deterioration += 1
        trace_fields = [
            _maybe_float(_row_get(row, "source_entry_price")),
            _maybe_float(_row_get(row, "current_copy_price")),
            _maybe_float(_row_get(row, "estimated_fill_price")),
        ]
        if any(v is not None for v in trace_fields):
            trace_present += 1
        else:
            trace_absent += 1
        for reason in _json_list(_row_get(row, "rejection_reasons_json")):
            if str(reason) == "PRICE_DETERIORATION_TRACE_MISMATCH":
                mismatch_rejections += 1
        for col in ("source_entry_price", "current_copy_price", "estimated_fill_price"):
            v = _maybe_float(_row_get(row, col))
            if v is not None and (v <= 0.0 or v > 1.0):
                out_of_range += 1
                break
        if not _row_get(row, "price_snapshot_id"):
            missing_snapshot_id += 1
        if not _row_get(row, "depth_hash"):
            missing_depth_hash += 1

    findings.append(TradeCopyabilityReviewFinding(
        key="price_evidence_summary",
        severity="info",
        summary="Price-evidence review of trade_copyability_decisions.",
        count=len(rows),
        evidence={
            "rows_missing_price_deterioration_pct": missing_deterioration,
            "rows_with_trace_fields_present": trace_present,
            "rows_with_trace_fields_absent": trace_absent,
            "rows_with_mismatch_rejection": mismatch_rejections,
            "rows_with_price_out_of_range": out_of_range,
            "rows_missing_price_snapshot_id": missing_snapshot_id,
            "rows_missing_depth_hash": missing_depth_hash,
        },
    ))
    return findings


# --------------------------------------------------------------------------
# PART 7: depth / fill / spread findings
# --------------------------------------------------------------------------


def _depth_fill_spread_findings(rows: list[sqlite3.Row]) -> list[TradeCopyabilityReviewFinding]:
    findings: list[TradeCopyabilityReviewFinding] = []
    if not rows:
        findings.append(TradeCopyabilityReviewFinding(
            key="no_trade_decisions_yet",
            severity="info",
            summary="No trade_copyability_decisions rows yet; depth/fill review is empty.",
            count=0,
        ))
        return findings

    fill_below_80 = 0
    partial_fill_downgraded = 0
    insufficient_depth = 0
    missing_depth_walk = 0
    high_spread = 0
    copy_candidate_with_low_fill = 0
    for row in rows:
        verdict = _norm(_row_get(row, "verdict"))
        fill = _maybe_float(_row_get(row, "fill_percentage"))
        spread = _maybe_float(_row_get(row, "spread"))
        insufficient_reason = str(_row_get(row, "insufficient_depth_reason", "") or "")
        if fill is not None and fill < 0.80:
            fill_below_80 += 1
        if "partial_fill_below_copy_candidate_threshold" in [
            str(r) for r in _json_list(_row_get(row, "rejection_reasons_json"))
        ]:
            partial_fill_downgraded += 1
        if insufficient_reason or "DEPTH_INSUFFICIENT_FOR_STAKE" in [
            str(r) for r in _json_list(_row_get(row, "rejection_reasons_json"))
        ]:
            insufficient_depth += 1
        if not _row_get(row, "depth_walk_json"):
            missing_depth_walk += 1
        if spread is not None and spread >= 0.10:
            high_spread += 1
        if verdict == VERDICT_COPY_CANDIDATE and fill is not None and fill < 0.80:
            copy_candidate_with_low_fill += 1

    if copy_candidate_with_low_fill:
        findings.append(TradeCopyabilityReviewFinding(
            key="copy_candidate_low_fill_blocker",
            severity="blocker",
            summary=(
                "A trade_copyability decision has verdict copy_candidate but "
                "fill_percentage < 0.80, violating the PART-7 80% rule."
            ),
            count=copy_candidate_with_low_fill,
            evidence={"fill_threshold": 0.80},
            recommendation=(
                "Investigate the strict scorer; copy_candidate must never occur "
                "below MIN_COPY_CANDIDATE_FILL_PERCENTAGE."
            ),
        ))
    findings.append(TradeCopyabilityReviewFinding(
        key="depth_fill_spread_summary",
        severity="info",
        summary="Depth/fill/spread review of trade_copyability_decisions.",
        count=len(rows),
        evidence={
            "rows_fill_percentage_below_0_80": fill_below_80,
            "rows_partial_fill_downgraded": partial_fill_downgraded,
            "rows_depth_insufficient": insufficient_depth,
            "rows_missing_depth_walk_json": missing_depth_walk,
            "rows_high_spread_ge_0_10": high_spread,
            "copy_candidate_with_fill_below_0_80": copy_candidate_with_low_fill,
        },
    ))
    return findings


# --------------------------------------------------------------------------
# PART 8: duration findings
# --------------------------------------------------------------------------


def _duration_findings(rows: list[sqlite3.Row]) -> list[TradeCopyabilityReviewFinding]:
    findings: list[TradeCopyabilityReviewFinding] = []
    if not rows:
        findings.append(TradeCopyabilityReviewFinding(
            key="no_trade_decisions_yet",
            severity="info",
            summary="No trade_copyability_decisions rows yet; duration review is empty.",
            count=0,
        ))
        return findings

    short_lt_15m = 0
    long_gt_45d = 0
    excluded_short = 0
    excluded_long = 0
    unknown_negative = 0
    copy_candidate_with_hard_exclusion = 0
    for row in rows:
        verdict = _norm(_row_get(row, "verdict"))
        secs = _maybe_float(_row_get(row, "seconds_to_market_end"))
        reasons = [str(r) for r in _json_list(_row_get(row, "rejection_reasons_json"))]
        if secs is not None:
            if secs < 0:
                unknown_negative += 1
            elif secs < 15 * 60:
                short_lt_15m += 1
            elif secs > 45 * 24 * 3600:
                long_gt_45d += 1
        if "duration_excluded_short" in reasons:
            excluded_short += 1
        if "duration_excluded_long" in reasons:
            excluded_long += 1
        if verdict == VERDICT_COPY_CANDIDATE and secs is not None and (
            secs < 15 * 60 or secs > 45 * 24 * 3600
        ):
            copy_candidate_with_hard_exclusion += 1

    if copy_candidate_with_hard_exclusion:
        findings.append(TradeCopyabilityReviewFinding(
            key="copy_candidate_duration_exclusion_blocker",
            severity="blocker",
            summary=(
                "A trade_copyability decision has verdict copy_candidate despite "
                "a hard duration exclusion (<15m or >45d)."
            ),
            count=copy_candidate_with_hard_exclusion,
            evidence={"short_min_seconds": 15 * 60, "long_max_seconds": 45 * 24 * 3600},
            recommendation=(
                "Hard duration exclusions must block copy_candidate; investigate "
                "the strict scorer."
            ),
        ))
    findings.append(TradeCopyabilityReviewFinding(
        key="duration_summary",
        severity="info",
        summary="Duration review of trade_copyability_decisions.",
        count=len(rows),
        evidence={
            "rows_seconds_lt_15m": short_lt_15m,
            "rows_seconds_gt_45d": long_gt_45d,
            "rows_duration_excluded_short": excluded_short,
            "rows_duration_excluded_long": excluded_long,
            "rows_unknown_or_negative_seconds": unknown_negative,
            "copy_candidate_with_hard_exclusion": copy_candidate_with_hard_exclusion,
        },
    ))
    return findings


# --------------------------------------------------------------------------
# PART 9: side support findings
# --------------------------------------------------------------------------


def _side_support_findings(
    source_side_distribution: dict[str, int],
    decision_side_distribution: dict[str, int],
    candidate_rows: Iterable[sqlite3.Row],
) -> list[TradeCopyabilityReviewFinding]:
    findings: list[TradeCopyabilityReviewFinding] = []

    sell_source = source_side_distribution.get("SELL", 0) + source_side_distribution.get("sell", 0)
    sell_decision = decision_side_distribution.get("SELL", 0) + decision_side_distribution.get("sell", 0)
    candidate_side = _side_distribution(candidate_rows, "side")
    sell_candidate = candidate_side.get("SELL", 0) + candidate_side.get("sell", 0)

    if sell_decision:
        findings.append(TradeCopyabilityReviewFinding(
            key="sell_decision_present",
            severity="info",
            summary=(
                "SELL present in trade_copyability_decisions; v1 must only ever "
                "SKIP SELL (sell_side_copyability_not_supported_v1)."
            ),
            count=sell_decision,
            evidence={
                "decision_side_distribution": dict(decision_side_distribution),
                "sell_source": sell_source,
                "sell_candidate": sell_candidate,
            },
        ))

    # Malformed side present in source_trades?
    malformed_source = sum(
        v for k, v in source_side_distribution.items()
        if k not in ("BUY", "SELL", "buy", "sell", "<NULL>")
    )
    if malformed_source:
        findings.append(TradeCopyabilityReviewFinding(
            key="source_side_malformed",
            severity="warning",
            summary="source_trades has malformed side values (not buy/BUY/sell/SELL).",
            count=malformed_source,
            evidence={"distribution": dict(source_side_distribution)},
            recommendation="PR24R bridge must reject malformed side before scoring.",
        ))
    return findings


def _side_support_blocker_scan(
    decision_rows: list[sqlite3.Row],
) -> list[TradeCopyabilityReviewFinding]:
    """PART 9 critical scan: SELL/malformed side must never be eligible.

    Returns blocker findings when a decision has an eligible verdict
    (copy_candidate / watchlist) but a SELL or malformed side.
    """
    findings: list[TradeCopyabilityReviewFinding] = []
    sell_blockers = 0
    malformed_blockers = 0
    for row in decision_rows:
        verdict = _norm(_row_get(row, "verdict"))
        if verdict not in _ELIGIBLE_VERDICTS:
            continue
        side = str(_row_get(row, "side", "") or "")
        if side in ("SELL", "sell"):
            sell_blockers += 1
        elif side not in ("BUY", "buy"):
            malformed_blockers += 1
    if sell_blockers:
        findings.append(TradeCopyabilityReviewFinding(
            key="sell_decision_eligible_blocker",
            severity="blocker",
            summary=(
                "SELL decision has an eligible verdict (copy_candidate/watchlist) "
                "in v1; SELL must never be eligible for Trade Copyability v1."
            ),
            count=sell_blockers,
            evidence={"expected": "sell_side_copyability_not_supported_v1 -> SKIP"},
            recommendation=(
                "Strict scorer must SKIP SELL before scoring; investigate any "
                "eligible SELL decision."
            ),
        ))
    if malformed_blockers:
        findings.append(TradeCopyabilityReviewFinding(
            key="malformed_side_eligible_blocker",
            severity="blocker",
            summary=(
                "A malformed side decision has an eligible verdict "
                "(copy_candidate/watchlist); malformed side must be INCOMPLETE."
            ),
            count=malformed_blockers,
            evidence={"expected": "invalid_side -> INCOMPLETE"},
            recommendation="Strict scorer must reject malformed side as INCOMPLETE.",
        ))
    return findings


# --------------------------------------------------------------------------
# PART 10: snapshot timing findings
# --------------------------------------------------------------------------


def _snapshot_timing_findings(rows: list[sqlite3.Row]) -> list[TradeCopyabilityReviewFinding]:
    findings: list[TradeCopyabilityReviewFinding] = []
    if not rows:
        findings.append(TradeCopyabilityReviewFinding(
            key="no_trade_decisions_yet",
            severity="info",
            summary="No trade_copyability_decisions rows yet; snapshot timing review is empty.",
            count=0,
        ))
        return findings

    missing_trace = 0
    invalid_timing = 0
    copy_candidate_with_bad_timing = 0
    for row in rows:
        verdict = _norm(_row_get(row, "verdict"))
        reasons = [str(r) for r in _json_list(_row_get(row, "rejection_reasons_json"))]
        st = _row_get(row, "source_trade_timestamp")
        pf = _row_get(row, "price_snapshot_fetched_at")
        et = _row_get(row, "evaluation_timestamp")
        if not st or not pf or not et:
            missing_trace += 1
        bad = (
            "snapshot_before_source_trade" in reasons
            or "snapshot_after_evaluation" in reasons
            or "invalid_price_snapshot_timestamp" in reasons
        )
        if bad:
            invalid_timing += 1
        if verdict == VERDICT_COPY_CANDIDATE and bad:
            copy_candidate_with_bad_timing += 1

    if copy_candidate_with_bad_timing:
        findings.append(TradeCopyabilityReviewFinding(
            key="copy_candidate_bad_snapshot_timing_blocker",
            severity="blocker",
            summary=(
                "A trade_copyability decision has verdict copy_candidate with an "
                "impossible snapshot timing rejection."
            ),
            count=copy_candidate_with_bad_timing,
            evidence={"reasons": [
                "snapshot_before_source_trade",
                "snapshot_after_evaluation",
                "invalid_price_snapshot_timestamp",
            ]},
            recommendation="Impossible snapshot timing must block copy_candidate.",
        ))
    findings.append(TradeCopyabilityReviewFinding(
        key="snapshot_timing_summary",
        severity="info",
        summary="Snapshot timing review of trade_copyability_decisions.",
        count=len(rows),
        evidence={
            "rows_missing_trace_timestamps": missing_trace,
            "rows_invalid_snapshot_timing": invalid_timing,
            "copy_candidate_with_bad_timing": copy_candidate_with_bad_timing,
        },
    ))
    return findings


# --------------------------------------------------------------------------
# PART 11: later outcome findings
# --------------------------------------------------------------------------


def _later_outcome_findings(
    decision_rows: list[sqlite3.Row],
    paper_rows: list[sqlite3.Row],
    ledger_rows: list[sqlite3.Row],
    exit_rows: list[sqlite3.Row],
) -> list[TradeCopyabilityReviewFinding]:
    findings: list[TradeCopyabilityReviewFinding] = []
    has_decisions = bool(decision_rows)
    has_paper = bool(paper_rows)
    has_ledger = bool(ledger_rows)
    has_exit = bool(exit_rows)

    if not (has_decisions or has_paper or has_ledger or has_exit):
        findings.append(TradeCopyabilityReviewFinding(
            key="no_later_outcome_evidence",
            severity="info",
            summary=(
                "No trade decisions, paper signals, ledger entries, or exit "
                "registrations exist yet; no later-outcome evidence to review."
            ),
            count=0,
        ))
        return findings

    findings.append(TradeCopyabilityReviewFinding(
        key="later_outcome_availability",
        severity="info",
        summary="Later-outcome data availability (read-only summary only).",
        count=None,
        evidence={
            "trade_copyability_decisions": len(decision_rows),
            "paper_signal_decisions": len(paper_rows),
            "settlement_accounting_ledger": len(ledger_rows),
            "exit_experiment_registrations": len(exit_rows),
        },
        recommendation=(
            "Do not infer performance when data does not exist; no later "
            "copyability outcome data is fabricated."
        ),
    ))
    return findings


# --------------------------------------------------------------------------
# Human rendering
# --------------------------------------------------------------------------


def report_to_human(report: TradeCopyabilityReviewReport) -> str:
    lines: list[str] = []
    lines.append("TRADE COPYABILITY REVIEW REPORT — READ ONLY")
    lines.append(f"formula: {report.formula_name} v{report.formula_version}")
    lines.append(f"ready_to_wire_to_automation = {report.ready_to_wire_to_automation}")
    lines.append("")
    lines.append("== Production counts ==")
    for k, v in report.production_counts.items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("== Source side distribution (source_trades, exact raw) ==")
    for k, v in sorted(report.source_side_distribution.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("== Decision side distribution (trade_copyability_decisions, exact) ==")
    for k, v in sorted(report.decision_side_distribution.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"  {k}: {v}")
    lines.append("")
    if report.source_side_casing_findings:
        lines.append("== Side casing findings ==")
        for f in report.source_side_casing_findings:
            lines.append(f"  [{f.severity}] {f.key}: {f.summary}")
            if f.recommendation:
                lines.append(f"    -> {f.recommendation}")
    lines.append("")
    lines.append("== Verdict / score distribution ==")
    sd = report.score_distribution
    lines.append(f"  has_decisions: {sd.get('has_decisions')}")
    if sd.get("has_decisions"):
        lines.append(f"  min/avg/max: {sd.get('min')} / {sd.get('avg')} / {sd.get('max')}")
        lines.append(f"  buckets: {sd.get('buckets')}")
        lines.append(
            f"  copy_candidate={sd.get('copy_candidate')} "
            f"watchlist={sd.get('watchlist')} skip={sd.get('skip')} "
            f"incomplete={sd.get('incomplete')}"
        )
    else:
        lines.append("  no trade copyability decisions yet (not a failure)")
    lines.append("")
    lines.append("== Incomplete / rejection reasons ==")
    if report.incomplete_reason_counts:
        lines.append("  incomplete_reasons:")
        for k, v in report.incomplete_reason_counts.items():
            lines.append(f"    {k}: {v}")
    else:
        lines.append("  incomplete_reasons: (empty)")
    if report.rejection_reason_counts:
        lines.append("  rejection_reasons:")
        for k, v in report.rejection_reason_counts.items():
            lines.append(f"    {k}: {v}")
    else:
        lines.append("  rejection_reasons: (empty)")
    lines.append("")

    def _section(title: str, findings):
        lines.append(f"== {title} ==")
        if not findings:
            lines.append("  (none)")
            return
        for f in findings:
            lines.append(f"  [{f.severity}] {f.key}: {f.summary}")
            if f.count is not None:
                lines.append(f"    count={f.count}")
            if f.evidence:
                lines.append(f"    evidence={json.dumps(f.evidence, sort_keys=True)}")
            if f.recommendation:
                lines.append(f"    -> {f.recommendation}")

    _section("Price evidence findings", report.price_evidence_findings)
    lines.append("")
    _section("Depth/fill/spread findings", report.depth_fill_spread_findings)
    lines.append("")
    _section("Duration findings", report.duration_findings)
    lines.append("")
    _section("Side support findings", report.side_support_findings)
    lines.append("")
    _section("Snapshot timing findings", report.snapshot_timing_findings)
    lines.append("")
    _section("Later outcome findings", report.later_outcome_findings)
    lines.append("")
    lines.append("== Recommended next step ==")
    lines.append(f"  {report.recommended_next_step}")
    return "\n".join(lines)


def report_to_dict(report: TradeCopyabilityReviewReport) -> dict[str, Any]:
    return report.to_dict()


__all__ = [
    "RECOMMENDED_NEXT_STEP",
    "TradeCopyabilityReviewFinding",
    "TradeCopyabilityReviewReport",
    "build_trade_copyability_review_report",
    "report_to_human",
    "report_to_dict",
]
