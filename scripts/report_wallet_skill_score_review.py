#!/usr/bin/env python3
"""Read-only Wallet Skill Score review report (PR24N)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from polycopy.config.settings import get_settings
from polycopy.engine.wallet_skill_score_review_report import (
    WalletSkillScoreReviewReport,
    build_wallet_skill_score_review_report,
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="report_wallet_skill_score_review",
        description="Read-only PR24N report comparing wallet skill scores to later outcomes.",
    )
    p.add_argument("--db", type=Path, default=None, help="SQLite DB path; defaults to configured Polycopy DB.")
    p.add_argument("--limit", type=int, default=10, help="Rows per ranked section; totals are unfiltered.")
    p.add_argument("--json", action="store_true", help="Emit parseable JSON.")
    p.add_argument("--high-score-threshold", type=float, default=75.0)
    p.add_argument("--low-score-threshold", type=float, default=55.0)
    p.add_argument("--good-outcome-pnl", type=float, default=0.0)
    p.add_argument("--failed-outcome-pnl", type=float, default=0.0)
    p.add_argument("--price-deterioration-failure-pct", type=float, default=0.05)
    p.add_argument("--delay-failure-seconds", type=int, default=300)
    p.add_argument("--spread-failure", type=float, default=0.10)
    p.add_argument("--liquidity-fill-failure-pct", type=float, default=1.0)
    return p


def _human(report: WalletSkillScoreReviewReport) -> str:
    lines: list[str] = []
    lines.append("Wallet Skill Score Review Report")
    lines.append("Mode: read-only evaluator/report; no formula, tuning, trading, DB write, or automation changes")
    lines.append("")
    lines.append("Decision counts:")
    lines.append(f"- wallet_score_decisions: {report.wallet_score_decisions}")
    lines.append(f"- category_wallet_score_decisions: {report.category_score_decisions}")
    lines.append(f"- trade_copyability_decisions: {report.trade_copyability_decisions}")
    lines.append(f"- paper_signal_decisions: {report.paper_signal_decisions}")
    lines.append("")
    _append_counts(lines, "Most common incomplete reasons", report.incomplete_reasons)
    _append_counts(lines, "Most often missing components", report.missing_components)
    _append_wallet_items(lines, "Watchlisted wallets that later performed well", report.watchlisted_wallets_later_performed_well)
    _append_wallet_items(lines, "High scores that later failed", report.high_scores_that_failed)
    _append_wallet_items(lines, "Low scores that later improved", report.low_scores_that_later_improved)
    lines.append("Category signals:")
    if not report.category_signals:
        lines.append("- none")
    for item in report.category_signals:
        lines.append(
            f"- {item.category_label}: decisions={item.decision_count}, "
            f"avg_score={_fmt(item.average_score)}, later_total_pnl={_fmt(item.later_total_pnl)}, "
            f"later_trades={item.later_trade_count}, wallets_with_later_outcomes={item.wallets_with_later_outcomes}"
        )
    lines.append("")
    lines.append("Copyability failures:")
    lines.append(f"- price_deterioration: {report.copyability_failures.price_deterioration}")
    lines.append(f"- delay: {report.copyability_failures.delay}")
    lines.append(f"- spread: {report.copyability_failures.spread}")
    lines.append(f"- liquidity: {report.copyability_failures.liquidity}")
    _append_counts(lines, "Explicit copyability rejection/missing reasons", report.copyability_failures.explicit_rejection_reasons)
    lines.append("Exit methods:")
    if not report.exit_methods:
        lines.append("- none")
    for item in report.exit_methods:
        status_text = ", ".join(f"{status.name}={status.count}" for status in item.statuses) or "none"
        lines.append(f"- {item.exit_method}: registrations={item.registrations}; {status_text}")
    lines.append("")
    _append_texts(lines, "Notes", report.notes)
    return "\n".join(lines)


def _append_counts(lines: list[str], title: str, items: Any) -> None:
    lines.append(f"{title}:")
    if not items:
        lines.append("- none")
    for item in items:
        lines.append(f"- {item.name}: {item.count}")
    lines.append("")


def _append_wallet_items(lines: list[str], title: str, items: Any) -> None:
    lines.append(f"{title}:")
    if not items:
        lines.append("- none")
    for item in items:
        lines.append(
            f"- {item.wallet_id}: score={item.score:.4f}, verdict={item.verdict}, "
            f"later_total_pnl={_fmt(item.later_total_pnl)}, later_win_rate={_fmt(item.later_win_rate)}, "
            f"later_trades={item.later_trade_count}, reason={item.reason}"
        )
    lines.append("")


def _append_texts(lines: list[str], title: str, items: Any) -> None:
    lines.append(f"{title}:")
    if not items:
        lines.append("- none")
    for item in items:
        lines.append(f"- {item}")


def _fmt(value: float | None) -> str:
    return "None" if value is None else f"{value:.6f}"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db_path = args.db or Path(get_settings().db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = build_wallet_skill_score_review_report(
            conn,
            limit=args.limit,
            high_score_threshold=args.high_score_threshold,
            low_score_threshold=args.low_score_threshold,
            good_outcome_pnl=args.good_outcome_pnl,
            failed_outcome_pnl=args.failed_outcome_pnl,
            price_deterioration_failure_pct=args.price_deterioration_failure_pct,
            delay_failure_seconds=args.delay_failure_seconds,
            spread_failure=args.spread_failure,
            liquidity_fill_failure_pct=args.liquidity_fill_failure_pct,
        )
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(_human(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
