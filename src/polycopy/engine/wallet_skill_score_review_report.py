"""Read-only Wallet Skill Score review report for PR24N.

This module compares persisted wallet/category/trade score decisions against later
observed outcomes.  It never changes formulas, writes to SQLite, creates copy
candidates, places orders, or wires automation; callers provide an already-open
read-only connection or an in-memory test connection.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

VERDICT_INCOMPLETE = "incomplete"
VERDICT_WATCHLIST = "watchlist"
DEFAULT_HIGH_SCORE_THRESHOLD = 75.0
DEFAULT_LOW_SCORE_THRESHOLD = 55.0
DEFAULT_GOOD_OUTCOME_PNL = 0.0
DEFAULT_FAILED_OUTCOME_PNL = 0.0
DEFAULT_PRICE_DETERIORATION_FAILURE_PCT = 0.05
DEFAULT_DELAY_FAILURE_SECONDS = 300
DEFAULT_SPREAD_FAILURE = 0.10
DEFAULT_LIQUIDITY_FILL_FAILURE_PCT = 1.0


@dataclass(frozen=True)
class CountItem:
    name: str
    count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WalletOutcomeItem:
    wallet_id: str
    score: float
    verdict: str
    decision_id: int | None = None
    computed_at: str | None = None
    later_total_pnl: float | None = None
    later_realized_pnl: float | None = None
    later_win_rate: float | None = None
    later_trade_count: int | None = None
    later_end_date: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CategorySignalReviewItem:
    category_label: str
    decision_count: int
    average_score: float | None
    later_total_pnl: float | None
    later_realized_pnl: float | None
    later_trade_count: int
    wallets_with_later_outcomes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CopyabilityFailureSummary:
    price_deterioration: int = 0
    delay: int = 0
    spread: int = 0
    liquidity: int = 0
    explicit_rejection_reasons: tuple[CountItem, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "price_deterioration": self.price_deterioration,
            "delay": self.delay,
            "spread": self.spread,
            "liquidity": self.liquidity,
            "explicit_rejection_reasons": [item.to_dict() for item in self.explicit_rejection_reasons],
        }


@dataclass(frozen=True)
class ExitMethodReviewItem:
    exit_method: str
    registrations: int
    statuses: tuple[CountItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_method": self.exit_method,
            "registrations": self.registrations,
            "statuses": [item.to_dict() for item in self.statuses],
        }


@dataclass(frozen=True)
class WalletSkillScoreReviewReport:
    wallet_score_decisions: int
    category_score_decisions: int
    trade_copyability_decisions: int
    paper_signal_decisions: int
    incomplete_reasons: tuple[CountItem, ...]
    missing_components: tuple[CountItem, ...]
    watchlisted_wallets_later_performed_well: tuple[WalletOutcomeItem, ...]
    high_scores_that_failed: tuple[WalletOutcomeItem, ...]
    low_scores_that_later_improved: tuple[WalletOutcomeItem, ...]
    category_signals: tuple[CategorySignalReviewItem, ...]
    copyability_failures: CopyabilityFailureSummary
    exit_methods: tuple[ExitMethodReviewItem, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet_score_decisions": self.wallet_score_decisions,
            "category_score_decisions": self.category_score_decisions,
            "trade_copyability_decisions": self.trade_copyability_decisions,
            "paper_signal_decisions": self.paper_signal_decisions,
            "incomplete_reasons": [item.to_dict() for item in self.incomplete_reasons],
            "missing_components": [item.to_dict() for item in self.missing_components],
            "watchlisted_wallets_later_performed_well": [item.to_dict() for item in self.watchlisted_wallets_later_performed_well],
            "high_scores_that_failed": [item.to_dict() for item in self.high_scores_that_failed],
            "low_scores_that_later_improved": [item.to_dict() for item in self.low_scores_that_later_improved],
            "category_signals": [item.to_dict() for item in self.category_signals],
            "copyability_failures": self.copyability_failures.to_dict(),
            "exit_methods": [item.to_dict() for item in self.exit_methods],
            "notes": list(self.notes),
        }


def build_wallet_skill_score_review_report(
    conn_or_db: Any,
    *,
    limit: int = 10,
    high_score_threshold: float = DEFAULT_HIGH_SCORE_THRESHOLD,
    low_score_threshold: float = DEFAULT_LOW_SCORE_THRESHOLD,
    good_outcome_pnl: float = DEFAULT_GOOD_OUTCOME_PNL,
    failed_outcome_pnl: float = DEFAULT_FAILED_OUTCOME_PNL,
    price_deterioration_failure_pct: float = DEFAULT_PRICE_DETERIORATION_FAILURE_PCT,
    delay_failure_seconds: int = DEFAULT_DELAY_FAILURE_SECONDS,
    spread_failure: float = DEFAULT_SPREAD_FAILURE,
    liquidity_fill_failure_pct: float = DEFAULT_LIQUIDITY_FILL_FAILURE_PCT,
) -> WalletSkillScoreReviewReport:
    """Build a read-only review report from persisted decisions and outcomes.

    Later outcomes are taken from ``performance_summaries`` rows whose
    ``end_date`` is later than the score decision's ``computed_at`` or
    ``source_data_timestamp`` when available.  If the DB has no later outcome
    evidence, affected sections are empty rather than fabricated.
    """

    if limit < 0:
        raise ValueError("limit must be >= 0")

    conn = _conn(conn_or_db)
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        wallet_rows = _fetch_rows(conn, "wallet_score_decisions")
        category_rows = _fetch_rows(conn, "category_wallet_score_decisions")
        trade_rows = _fetch_rows(conn, "trade_copyability_decisions")
        paper_rows = _fetch_rows(conn, "paper_signal_decisions")
        performance_rows = _fetch_rows(conn, "performance_summaries")
        exit_rows = _fetch_rows(conn, "exit_experiment_registrations")

        incomplete_reasons = _top_items(_incomplete_reasons(wallet_rows, category_rows, trade_rows), limit)
        missing_components = _top_items(_missing_components(wallet_rows, category_rows, trade_rows), limit)
        outcomes_by_wallet = _performance_by_wallet(performance_rows)
        def watchlisted_performed_well(row: sqlite3.Row, outcome: sqlite3.Row) -> bool:
            pnl = _outcome_pnl(outcome)
            return _norm(row["verdict"]) == VERDICT_WATCHLIST and pnl is not None and pnl > good_outcome_pnl

        def high_score_failed(row: sqlite3.Row, outcome: sqlite3.Row) -> bool:
            pnl = _outcome_pnl(outcome)
            return _score(row) >= high_score_threshold and pnl is not None and pnl < failed_outcome_pnl

        def low_score_improved(row: sqlite3.Row, outcome: sqlite3.Row) -> bool:
            pnl = _outcome_pnl(outcome)
            return _score(row) <= low_score_threshold and pnl is not None and pnl > good_outcome_pnl

        watchlisted = _wallet_outcome_items(
            wallet_rows,
            outcomes_by_wallet,
            predicate=watchlisted_performed_well,
            reason="watchlisted_then_positive_later_pnl",
            limit=limit,
            reverse=True,
        )
        high_failed = _wallet_outcome_items(
            wallet_rows,
            outcomes_by_wallet,
            predicate=high_score_failed,
            reason="high_score_then_negative_later_pnl",
            limit=limit,
            reverse=False,
        )
        low_improved = _wallet_outcome_items(
            wallet_rows,
            outcomes_by_wallet,
            predicate=low_score_improved,
            reason="low_score_then_positive_later_pnl",
            limit=limit,
            reverse=True,
        )
        category_signals = _category_signals(category_rows, outcomes_by_wallet, limit=limit)
        copyability_failures = _copyability_failures(
            trade_rows,
            price_deterioration_failure_pct=price_deterioration_failure_pct,
            delay_failure_seconds=delay_failure_seconds,
            spread_failure=spread_failure,
            liquidity_fill_failure_pct=liquidity_fill_failure_pct,
            limit=limit,
        )
        exit_methods = _exit_methods(exit_rows, limit=limit)
        notes = _notes(wallet_rows, category_rows, trade_rows, paper_rows, performance_rows, exit_rows)
        return WalletSkillScoreReviewReport(
            wallet_score_decisions=len(wallet_rows),
            category_score_decisions=len(category_rows),
            trade_copyability_decisions=len(trade_rows),
            paper_signal_decisions=len(paper_rows),
            incomplete_reasons=incomplete_reasons,
            missing_components=missing_components,
            watchlisted_wallets_later_performed_well=tuple(watchlisted),
            high_scores_that_failed=tuple(high_failed),
            low_scores_that_later_improved=tuple(low_improved),
            category_signals=tuple(category_signals),
            copyability_failures=copyability_failures,
            exit_methods=tuple(exit_methods),
            notes=tuple(notes),
        )
    finally:
        conn.row_factory = old_factory


def _conn(conn_or_db: Any) -> sqlite3.Connection:
    if isinstance(conn_or_db, sqlite3.Connection):
        return conn_or_db
    maybe_conn = getattr(conn_or_db, "conn", None)
    if isinstance(maybe_conn, sqlite3.Connection):
        return maybe_conn
    raise TypeError("conn_or_db must be a sqlite3.Connection or Database-like object")


def _fetch_rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    if not _table_exists(conn, table):
        return []
    return list(conn.execute(f"SELECT * FROM {table}"))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone() is not None


def _json_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
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


def _score(row: sqlite3.Row) -> float:
    try:
        return float(_row_get(row, "final_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _incomplete_reasons(*row_groups: Iterable[sqlite3.Row]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for rows in row_groups:
        for row in rows:
            if _norm(_row_get(row, "verdict")) != VERDICT_INCOMPLETE:
                continue
            for column in ("missing_essentials_json", "eligibility_failures_json", "category_gate_failures_json", "rejection_reasons_json"):
                for reason in _json_list(_row_get(row, column)):
                    if str(reason):
                        counter[str(reason)] += 1
    return counter


def _missing_components(*row_groups: Iterable[sqlite3.Row]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for rows in row_groups:
        for row in rows:
            missing_names = {str(item) for item in _json_list(_row_get(row, "missing_essentials_json")) if str(item)}
            components = _json_list(_row_get(row, "component_scores_json"))
            component_names = set()
            for component in components:
                if not isinstance(component, dict):
                    continue
                name = str(component.get("name") or "")
                if not name:
                    continue
                component_names.add(name)
                quality = _norm(component.get("quality"))
                note = _norm(component.get("note"))
                raw_score = component.get("raw_score", component.get("normalized_score"))
                if quality == "missing" or raw_score is None or "missing" in note:
                    counter[name] += 1
            for name in missing_names - component_names:
                counter[name] += 1
    return counter


def _top_items(counter: Counter[str], limit: int) -> tuple[CountItem, ...]:
    return tuple(CountItem(name=name, count=count) for name, count in counter.most_common(limit or None))


def _performance_by_wallet(rows: Iterable[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        wallet_id = str(_row_get(row, "wallet_id", "") or "")
        if wallet_id:
            grouped[wallet_id].append(row)
    for wallet_rows in grouped.values():
        wallet_rows.sort(key=lambda r: str(_row_get(r, "end_date", "") or ""))
    return grouped


def _later_outcome(decision: sqlite3.Row, outcomes_by_wallet: dict[str, list[sqlite3.Row]]) -> sqlite3.Row | None:
    wallet_id = str(_row_get(decision, "wallet_id", "") or "")
    if not wallet_id:
        return None
    cutoff = str(_row_get(decision, "source_data_timestamp") or _row_get(decision, "computed_at") or "")
    candidates = outcomes_by_wallet.get(wallet_id, [])
    later = [row for row in candidates if not cutoff or str(_row_get(row, "end_date", "") or "") > cutoff]
    if not later:
        return None
    return later[-1]


def _outcome_pnl(row: sqlite3.Row | None) -> float | None:
    if row is None:
        return None
    for column in ("total_pnl", "realized_pnl"):
        value = _row_get(row, column)
        if value is not None:
            return float(value)
    return None


def _wallet_outcome_items(
    wallet_rows: Iterable[sqlite3.Row],
    outcomes_by_wallet: dict[str, list[sqlite3.Row]],
    *,
    predicate: Any,
    reason: str,
    limit: int,
    reverse: bool,
) -> list[WalletOutcomeItem]:
    items: list[WalletOutcomeItem] = []
    for row in wallet_rows:
        outcome = _later_outcome(row, outcomes_by_wallet)
        if outcome is None or not predicate(row, outcome):
            continue
        items.append(_wallet_item(row, outcome, reason=reason))
    items.sort(key=lambda item: item.later_total_pnl if item.later_total_pnl is not None else 0.0, reverse=reverse)
    return items[:limit] if limit else items


def _wallet_item(row: sqlite3.Row, outcome: sqlite3.Row, *, reason: str) -> WalletOutcomeItem:
    return WalletOutcomeItem(
        wallet_id=str(_row_get(row, "wallet_id", "") or ""),
        score=_score(row),
        verdict=str(_row_get(row, "verdict", "") or ""),
        decision_id=_row_get(row, "id"),
        computed_at=_row_get(row, "computed_at"),
        later_total_pnl=_maybe_float(_row_get(outcome, "total_pnl")),
        later_realized_pnl=_maybe_float(_row_get(outcome, "realized_pnl")),
        later_win_rate=_maybe_float(_row_get(outcome, "win_rate")),
        later_trade_count=_maybe_int(_row_get(outcome, "trade_count")),
        later_end_date=_row_get(outcome, "end_date"),
        reason=reason,
    )


def _category_signals(
    category_rows: Iterable[sqlite3.Row],
    outcomes_by_wallet: dict[str, list[sqlite3.Row]],
    *,
    limit: int,
) -> list[CategorySignalReviewItem]:
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"scores": [], "wallets": set(), "pnl": 0.0, "realized": 0.0, "trades": 0})
    for row in category_rows:
        label = str(_row_get(row, "category_label", "uncategorized") or "uncategorized")
        wallet_id = str(_row_get(row, "wallet_id", "") or "")
        bucket = buckets[label]
        bucket["scores"].append(_score(row))
        outcome = _later_outcome(row, outcomes_by_wallet)
        if outcome is not None:
            bucket["wallets"].add(wallet_id)
            bucket["pnl"] += float(_row_get(outcome, "total_pnl", 0.0) or 0.0)
            bucket["realized"] += float(_row_get(outcome, "realized_pnl", 0.0) or 0.0)
            bucket["trades"] += int(_row_get(outcome, "trade_count", 0) or 0)
    items = [
        CategorySignalReviewItem(
            category_label=label,
            decision_count=len(bucket["scores"]),
            average_score=(sum(bucket["scores"]) / len(bucket["scores"])) if bucket["scores"] else None,
            later_total_pnl=bucket["pnl"] if bucket["wallets"] else None,
            later_realized_pnl=bucket["realized"] if bucket["wallets"] else None,
            later_trade_count=int(bucket["trades"]),
            wallets_with_later_outcomes=len(bucket["wallets"]),
        )
        for label, bucket in buckets.items()
    ]
    items.sort(key=lambda item: (item.later_total_pnl is not None, item.later_total_pnl or 0.0, item.decision_count), reverse=True)
    return items[:limit] if limit else items


def _copyability_failures(
    trade_rows: Iterable[sqlite3.Row],
    *,
    price_deterioration_failure_pct: float,
    delay_failure_seconds: int,
    spread_failure: float,
    liquidity_fill_failure_pct: float,
    limit: int,
) -> CopyabilityFailureSummary:
    price = delay = spread = liquidity = 0
    reasons: Counter[str] = Counter()
    for row in trade_rows:
        verdict = _norm(_row_get(row, "verdict"))
        if verdict not in {VERDICT_INCOMPLETE, "skip"}:
            continue
        for reason in _json_list(_row_get(row, "rejection_reasons_json")) + _json_list(_row_get(row, "missing_essentials_json")):
            if str(reason):
                reasons[str(reason)] += 1
        deterioration = _maybe_float(_row_get(row, "price_deterioration_pct"))
        trade_age = _maybe_float(_row_get(row, "trade_age_seconds"))
        row_spread = _maybe_float(_row_get(row, "spread"))
        fill = _maybe_float(_row_get(row, "fill_percentage"))
        insufficient_depth_reason = str(_row_get(row, "insufficient_depth_reason", "") or "")
        if deterioration is not None and deterioration >= price_deterioration_failure_pct:
            price += 1
        if trade_age is not None and trade_age >= delay_failure_seconds:
            delay += 1
        if row_spread is not None and row_spread >= spread_failure:
            spread += 1
        if (fill is not None and fill < liquidity_fill_failure_pct) or insufficient_depth_reason:
            liquidity += 1
    return CopyabilityFailureSummary(
        price_deterioration=price,
        delay=delay,
        spread=spread,
        liquidity=liquidity,
        explicit_rejection_reasons=_top_items(reasons, limit),
    )


def _exit_methods(rows: Iterable[sqlite3.Row], *, limit: int) -> list[ExitMethodReviewItem]:
    buckets: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        method = str(_row_get(row, "experiment_type", "unknown") or "unknown")
        status = str(_row_get(row, "status", "unknown") or "unknown")
        buckets[method][status] += 1
    items = [
        ExitMethodReviewItem(
            exit_method=method,
            registrations=sum(statuses.values()),
            statuses=_top_items(statuses, limit),
        )
        for method, statuses in buckets.items()
    ]
    items.sort(key=lambda item: item.registrations, reverse=True)
    return items[:limit] if limit else items


def _notes(
    wallet_rows: list[sqlite3.Row],
    category_rows: list[sqlite3.Row],
    trade_rows: list[sqlite3.Row],
    paper_rows: list[sqlite3.Row],
    performance_rows: list[sqlite3.Row],
    exit_rows: list[sqlite3.Row],
) -> list[str]:
    notes: list[str] = ["read_only_report_no_formula_or_automation_changes"]
    if not performance_rows:
        notes.append("no_performance_summaries_available_for_later_outcome_sections")
    if not wallet_rows:
        notes.append("no_wallet_score_decisions_available")
    if not category_rows:
        notes.append("no_category_wallet_score_decisions_available")
    if not trade_rows:
        notes.append("no_trade_copyability_decisions_available")
    if not paper_rows:
        notes.append("no_paper_signal_decisions_available")
    if not exit_rows:
        notes.append("no_exit_experiment_registrations_available")
    return notes


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "CategorySignalReviewItem",
    "CopyabilityFailureSummary",
    "CountItem",
    "ExitMethodReviewItem",
    "WalletOutcomeItem",
    "WalletSkillScoreReviewReport",
    "build_wallet_skill_score_review_report",
]
