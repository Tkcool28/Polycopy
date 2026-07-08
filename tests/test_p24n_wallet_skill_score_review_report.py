from __future__ import annotations

import json
import sqlite3

from polycopy.engine.wallet_skill_score_review_report import (
    build_wallet_skill_score_review_report,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE wallet_score_decisions (
            id INTEGER PRIMARY KEY,
            wallet_id TEXT NOT NULL,
            formula_name TEXT NOT NULL,
            formula_version TEXT NOT NULL,
            final_score REAL NOT NULL,
            verdict TEXT NOT NULL,
            component_scores_json TEXT,
            missing_essentials_json TEXT,
            eligibility_failures_json TEXT,
            source_data_timestamp TEXT,
            computed_at TEXT NOT NULL
        );
        CREATE TABLE category_wallet_score_decisions (
            id INTEGER PRIMARY KEY,
            wallet_id TEXT NOT NULL,
            category_label TEXT NOT NULL,
            formula_name TEXT NOT NULL,
            formula_version TEXT NOT NULL,
            final_score REAL NOT NULL,
            verdict TEXT NOT NULL,
            component_scores_json TEXT,
            missing_essentials_json TEXT,
            category_gate_failures_json TEXT,
            source_data_timestamp TEXT,
            computed_at TEXT NOT NULL
        );
        CREATE TABLE trade_copyability_decisions (
            id INTEGER PRIMARY KEY,
            wallet_id TEXT NOT NULL,
            source_trade_id TEXT NOT NULL,
            price_deterioration_pct REAL,
            fill_percentage REAL,
            spread REAL,
            trade_age_seconds INTEGER,
            insufficient_depth_reason TEXT,
            final_score REAL NOT NULL,
            verdict TEXT NOT NULL,
            component_scores_json TEXT,
            missing_essentials_json TEXT,
            rejection_reasons_json TEXT,
            computed_at TEXT NOT NULL
        );
        CREATE TABLE paper_signal_decisions (
            id INTEGER PRIMARY KEY,
            candidate_id INTEGER NOT NULL,
            wallet_id TEXT NOT NULL,
            final_verdict TEXT NOT NULL,
            computed_at TEXT NOT NULL
        );
        CREATE TABLE performance_summaries (
            id INTEGER PRIMARY KEY,
            wallet_id TEXT NOT NULL,
            strategy_label TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            total_pnl REAL NOT NULL,
            realized_pnl REAL NOT NULL,
            unrealized_pnl REAL NOT NULL,
            win_rate REAL NOT NULL,
            max_drawdown REAL NOT NULL,
            trade_count INTEGER NOT NULL
        );
        CREATE TABLE exit_experiment_registrations (
            id INTEGER PRIMARY KEY,
            paper_signal_id INTEGER NOT NULL,
            experiment_type TEXT NOT NULL,
            status TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            scheduled_at TEXT
        );
        """
    )
    return conn


def test_report_answers_reason_component_and_outcome_questions():
    conn = _conn()
    missing_components = json.dumps(
        [
            {
                "name": "information_and_price_improvement_quality",
                "raw_score": None,
                "weight": 30.0,
                "quality": "missing",
                "formula": "missing",
                "note": "price_improvement_evidence_missing",
            },
            {
                "name": "verified_realized_performance",
                "raw_score": 100.0,
                "weight": 15.0,
                "quality": "strong",
                "formula": "realized",
                "note": "ok",
            },
        ]
    )
    conn.executemany(
        """
        INSERT INTO wallet_score_decisions (
            id, wallet_id, formula_name, formula_version, final_score, verdict,
            component_scores_json, missing_essentials_json, eligibility_failures_json,
            source_data_timestamp, computed_at
        ) VALUES (?, ?, 'wallet_score', '1', ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                1,
                "wallet-watch",
                60.0,
                "watchlist",
                "[]",
                "[]",
                "[]",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
            (
                2,
                "wallet-high-fail",
                88.0,
                "copy_candidate",
                "[]",
                "[]",
                "[]",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
            (
                3,
                "wallet-low-improve",
                20.0,
                "skip",
                "[]",
                "[]",
                "[]",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
            (
                4,
                "wallet-incomplete",
                25.0,
                "incomplete",
                missing_components,
                json.dumps(["information_and_price_improvement_quality"]),
                json.dumps(["missing_price_evidence"]),
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        ],
    )
    conn.executemany(
        """
        INSERT INTO performance_summaries (
            wallet_id, strategy_label, start_date, end_date, total_pnl, realized_pnl,
            unrealized_pnl, win_rate, max_drawdown, trade_count
        ) VALUES (?, 'default', '2026-01-02', ?, ?, ?, 0.0, ?, 0.0, ?)
        """,
        [
            ("wallet-watch", "2026-02-01T00:00:00Z", 12.5, 12.5, 0.7, 8),
            ("wallet-high-fail", "2026-02-01T00:00:00Z", -9.0, -9.0, 0.2, 5),
            ("wallet-low-improve", "2026-02-01T00:00:00Z", 4.0, 4.0, 0.8, 4),
        ],
    )

    report = build_wallet_skill_score_review_report(conn)

    assert report.wallet_score_decisions == 4
    assert report.incomplete_reasons[0].name == "information_and_price_improvement_quality"
    assert any(item.name == "missing_price_evidence" for item in report.incomplete_reasons)
    assert report.missing_components[0].name == "information_and_price_improvement_quality"
    assert report.watchlisted_wallets_later_performed_well[0].wallet_id == "wallet-watch"
    assert report.high_scores_that_failed[0].wallet_id == "wallet-high-fail"
    assert report.low_scores_that_later_improved[0].wallet_id == "wallet-low-improve"


def test_report_answers_category_copyability_and_exit_method_questions():
    conn = _conn()
    conn.execute(
        """
        INSERT INTO category_wallet_score_decisions (
            wallet_id, category_label, formula_name, formula_version, final_score,
            verdict, component_scores_json, missing_essentials_json,
            category_gate_failures_json, source_data_timestamp, computed_at
        ) VALUES ('wallet-cat', 'politics', 'category_wallet_score', '1', 70.0,
                  'watchlist', '[]', '[]', '[]', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO performance_summaries (
            wallet_id, strategy_label, start_date, end_date, total_pnl, realized_pnl,
            unrealized_pnl, win_rate, max_drawdown, trade_count
        ) VALUES ('wallet-cat', 'default', '2026-01-02', '2026-02-01T00:00:00Z', 6.0, 6.0, 0.0, 0.6, 0.0, 3)
        """
    )
    conn.execute(
        """
        INSERT INTO trade_copyability_decisions (
            wallet_id, source_trade_id, price_deterioration_pct, fill_percentage,
            spread, trade_age_seconds, insufficient_depth_reason, final_score,
            verdict, component_scores_json, missing_essentials_json,
            rejection_reasons_json, computed_at
        ) VALUES ('wallet-cat', 'trade-1', 0.20, 0.5, 0.12, 900,
                  'depth_insufficient_for_stake', 10.0, 'skip', '[]', '[]',
                  '["price_deterioration_too_high", "insufficient_liquidity"]',
                  '2026-01-01T00:00:00Z')
        """
    )
    conn.executemany(
        """
        INSERT INTO exit_experiment_registrations (
            paper_signal_id, experiment_type, status, registered_at, scheduled_at
        ) VALUES (1, ?, ?, '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')
        """,
        [("exit_24h", "registered"), ("exit_24h", "completed"), ("exit_72h", "registered")],
    )

    report = build_wallet_skill_score_review_report(conn)

    assert report.category_signals[0].category_label == "politics"
    assert report.category_signals[0].later_total_pnl == 6.0
    assert report.copyability_failures.price_deterioration == 1
    assert report.copyability_failures.delay == 1
    assert report.copyability_failures.spread == 1
    assert report.copyability_failures.liquidity == 1
    assert report.copyability_failures.explicit_rejection_reasons[0].count == 1
    exit_24h = next(item for item in report.exit_methods if item.exit_method == "exit_24h")
    assert exit_24h.registrations == 2
    assert {item.name for item in exit_24h.statuses} == {"registered", "completed"}


def test_report_is_read_only_compatible_with_missing_optional_tables():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    report = build_wallet_skill_score_review_report(conn)

    assert report.wallet_score_decisions == 0
    assert "no_wallet_score_decisions_available" in report.notes
    assert "read_only_report_no_formula_or_automation_changes" in report.notes
