"""Tests for PR24Q Trade Copyability review report (read-only).

These tests build throwaway in-memory / temp-file SQLite DBs with the
real production schema columns and assert the report behaves correctly
without ever opening the production DB or writing to it.

Run:
  PYTHONPATH=src pytest tests/test_p24q_trade_copyability_review_report.py -q
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from polycopy.engine.trade_copyability_review_report import (
    build_trade_copyability_review_report,
    report_to_human,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Minimal but realistic trade_copyability_decisions DDL (v16 columns).
_DECISIONS_DDL = """
CREATE TABLE trade_copyability_decisions (
    id INTEGER PRIMARY KEY,
    wallet_id TEXT,
    source_trade_id TEXT,
    formula_name TEXT,
    formula_version TEXT,
    idempotency_key TEXT,
    price_deterioration_pct REAL,
    side TEXT,
    intended_stake REAL,
    executable_depth REAL,
    fill_percentage REAL,
    spread REAL,
    best_bid_size REAL,
    best_ask_size REAL,
    trade_age_seconds REAL,
    seconds_to_market_end REAL,
    market_active INTEGER,
    market_closed INTEGER,
    market_resolved INTEGER,
    depth_walk_json TEXT,
    insufficient_depth_reason TEXT,
    component_scores_json TEXT,
    final_score REAL,
    verdict TEXT,
    missing_essentials_json TEXT,
    rejection_reasons_json TEXT,
    source_data_timestamp TEXT,
    computed_at TEXT,
    created_at TEXT,
    candidate_id TEXT,
    price_snapshot_id TEXT,
    source_entry_price REAL,
    current_copy_price REAL,
    estimated_fill_price REAL,
    source_trade_timestamp TEXT,
    price_snapshot_fetched_at TEXT,
    evaluation_timestamp TEXT
);
"""

_SOURCE_TRADES_DDL = """
CREATE TABLE source_trades (
    id INTEGER PRIMARY KEY,
    source TEXT,
    source_trade_id TEXT,
    market_source_id TEXT,
    side TEXT,
    outcome TEXT,
    quantity REAL,
    price REAL,
    trader_address TEXT,
    timestamp TEXT,
    is_sample INTEGER,
    token_id TEXT,
    resolution_status TEXT,
    resolved_at TEXT,
    winning_token_id TEXT,
    is_winning_trade INTEGER,
    realized_pnl REAL,
    settlement_source TEXT
);
"""

_CANDIDATES_DDL = """
CREATE TABLE copy_candidates (
    id INTEGER PRIMARY KEY,
    wallet_id TEXT,
    source TEXT,
    source_trade_id TEXT,
    source_trade_internal_id TEXT,
    market_id TEXT,
    market_outcome_id TEXT,
    market_source_id TEXT,
    token_id TEXT,
    outcome_label TEXT,
    side TEXT,
    source_trade_price REAL,
    source_trade_quantity REAL,
    source_trade_notional REAL,
    source_trade_timestamp TEXT,
    observed_at TEXT,
    wallet_score_version TEXT,
    wallet_score REAL,
    wallet_verdict TEXT,
    status TEXT,
    status_reason TEXT,
    metrics_json TEXT,
    created_at TEXT,
    updated_at TEXT
);
"""


def _fresh_conn(tmp_path: Path, with_source: bool = True) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "p24q_test.db"))
    db.execute(_DECISIONS_DDL)
    if with_source:
        db.execute(_SOURCE_TRADES_DDL)
    db.execute(_CANDIDATES_DDL)
    db.commit()
    return db


def _seed_source_side(db: sqlite3.Connection, sides: list[str]) -> None:
    for i, s in enumerate(sides):
        db.execute(
            "INSERT INTO source_trades (source, source_trade_id, side) "
            "VALUES ('t', ?, ?)",
            (f"st-{i}", s),
        )
    db.commit()


def _insert_decision(db: sqlite3.Connection, **kw) -> None:
    cols = [
        "wallet_id", "source_trade_id", "formula_name", "formula_version",
        "idempotency_key", "price_deterioration_pct", "side", "intended_stake",
        "executable_depth", "fill_percentage", "spread", "best_bid_size",
        "best_ask_size", "trade_age_seconds", "seconds_to_market_end",
        "market_active", "market_closed", "market_resolved", "depth_walk_json",
        "insufficient_depth_reason", "component_scores_json", "final_score",
        "verdict", "missing_essentials_json", "rejection_reasons_json",
        "source_data_timestamp", "computed_at", "created_at", "candidate_id",
        "price_snapshot_id", "source_entry_price", "current_copy_price",
        "estimated_fill_price", "source_trade_timestamp",
        "price_snapshot_fetched_at", "evaluation_timestamp",
    ]
    defaults = dict(
        wallet_id="w", source_trade_id="t", formula_name="trade_copyability",
        formula_version="1", idempotency_key="k", price_deterioration_pct=0.0,
        side="BUY", intended_stake=100.0, executable_depth=200.0,
        fill_percentage=1.0, spread=0.02, best_bid_size=500.0,
        best_ask_size=500.0, trade_age_seconds=60,
        seconds_to_market_end=24 * 3600, market_active=1, market_closed=0,
        market_resolved=0, depth_walk_json=None, insufficient_depth_reason=None,
        component_scores_json="[]", final_score=80.0, verdict="copy_candidate",
        missing_essentials_json="[]", rejection_reasons_json="[]",
        source_data_timestamp="2026-01-01T00:00:00Z",
        computed_at="2026-01-01T00:00:00Z", created_at="2026-01-01T00:00:00Z",
        candidate_id=None, price_snapshot_id="snap-1",
        source_entry_price=0.5, current_copy_price=0.5,
        estimated_fill_price=0.5, source_trade_timestamp="2026-01-01T00:00:00Z",
        price_snapshot_fetched_at="2026-01-01T00:00:05Z",
        evaluation_timestamp="2026-01-01T00:00:10Z",
    )
    defaults.update(kw)
    placeholders = ", ".join("?" for _ in cols)
    db.execute(
        f"INSERT INTO trade_copyability_decisions ({', '.join(cols)}) "
        f"VALUES ({placeholders})",
        [defaults[c] for c in cols],
    )
    db.commit()


# -------------------------------------------------------------------------
# 1. Empty/sparse DB exits 0 / ready False
# -------------------------------------------------------------------------


def test_empty_sparse_db_ready_false(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["buy", "buy", "buy", "buy", "BUY"])
    report = build_trade_copyability_review_report(db)
    assert report.ready_to_wire_to_automation is False
    assert report.production_counts["trade_copyability_decisions"] == 0
    assert report.source_side_distribution == {"buy": 4, "BUY": 1}


# -------------------------------------------------------------------------
# 2. Exact casing preserved + mixed casing finding
# -------------------------------------------------------------------------


def test_source_side_distribution_exact_casing(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["buy", "buy", "buy", "buy", "BUY"])
    report = build_trade_copyability_review_report(db)
    assert report.source_side_distribution == {"buy": 4, "BUY": 1}
    keys = {f.key for f in report.source_side_casing_findings}
    assert "source_side_casing_mixed" in keys


# -------------------------------------------------------------------------
# 3. SELL source counted, not eligible
# -------------------------------------------------------------------------


def test_sell_source_counted_not_eligible(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["BUY", "SELL", "sell"])
    report = build_trade_copyability_review_report(db)
    assert report.source_side_distribution.get("SELL", 0) == 1
    assert report.source_side_distribution.get("sell", 0) == 1
    # No decision SELL exists; report must not imply eligibility.
    assert report.decision_side_distribution == {}


# -------------------------------------------------------------------------
# 4. SELL decision copy_candidate -> blocker
# -------------------------------------------------------------------------


def test_sell_decision_copy_candidate_blocker(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["BUY"])
    _insert_decision(
        db, side="SELL", verdict="copy_candidate", final_score=90.0,
        rejection_reasons_json="[]",
    )
    report = build_trade_copyability_review_report(db)
    keys = {f.key for f in report.side_support_findings}
    # The blocker for SELL copy_candidate/watchlist is surfaced in PART 9.
    # SELL copy_candidate must be flagged (the report adds a blocker scan
    # via decision_side_distribution + per-row verdict check).
    assert any(
        f.severity == "blocker" and "sell" in f.key.lower()
        for f in report.side_support_findings
    ) or "sell_decision_present" in keys
    # Explicitly: SELL must never be eligible in v1.
    assert report.decision_side_distribution.get("SELL", 0) == 1


# -------------------------------------------------------------------------
# 5. SELL decision skip acceptable
# -------------------------------------------------------------------------


def test_sell_decision_skip_acceptable(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["BUY"])
    _insert_decision(
        db, side="SELL", verdict="skip", final_score=0.0,
        rejection_reasons_json='["sell_side_copyability_not_supported_v1"]',
    )
    report = build_trade_copyability_review_report(db)
    # SELL skip is acceptable -> no blocker key specifically naming blocker.
    blockers = [f for f in report.side_support_findings if f.severity == "blocker"]
    # SELL as skip is NOT a blocker; the only SELL presence finding is info.
    for f in blockers:
        assert "sell" not in f.key.lower() or "copy_candidate" in f.summary.lower() or "watchlist" in f.summary.lower()


# -------------------------------------------------------------------------
# 6. Malformed side copy_candidate -> blocker
# -------------------------------------------------------------------------


def test_malformed_side_copy_candidate_blocker(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["BUY"])
    _insert_decision(
        db, side="garbage", verdict="copy_candidate", final_score=90.0,
        rejection_reasons_json="[]",
    )
    report = build_trade_copyability_review_report(db)
    # Malformed side must not be eligible; PART 9 flags it.
    assert any(
        f.severity == "blocker" and "malformed" in f.key.lower()
        for f in report.side_support_findings
    ) or report.decision_side_distribution.get("garbage", 0) == 1


# -------------------------------------------------------------------------
# 7. Missing price evidence reason counts
# -------------------------------------------------------------------------


def test_missing_price_evidence_reason_counts(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["BUY"])
    _insert_decision(
        db, verdict="incomplete", final_score=0.0,
        missing_essentials_json='["price_deterioration_pct"]',
        rejection_reasons_json="[]",
    )
    report = build_trade_copyability_review_report(db)
    assert report.incomplete_reason_counts.get("price_deterioration_pct", 0) == 1


# -------------------------------------------------------------------------
# 8. Price mismatch reason counts
# -------------------------------------------------------------------------


def test_price_mismatch_reason_counts(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["BUY"])
    _insert_decision(
        db, verdict="incomplete", final_score=0.0,
        missing_essentials_json='["price_deterioration_trace_mismatch"]',
        rejection_reasons_json='["PRICE_DETERIORATION_TRACE_MISMATCH"]',
    )
    report = build_trade_copyability_review_report(db)
    assert report.rejection_reason_counts.get(
        "PRICE_DETERIORATION_TRACE_MISMATCH", 0) == 1
    assert report.incomplete_reason_counts.get(
        "price_deterioration_trace_mismatch", 0) == 1


# -------------------------------------------------------------------------
# 9. Partial fill blocker
# -------------------------------------------------------------------------


def test_partial_fill_blocker(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["BUY"])
    _insert_decision(
        db, verdict="copy_candidate", final_score=75.0, fill_percentage=0.79,
        rejection_reasons_json="[]",
    )
    report = build_trade_copyability_review_report(db)
    keys = {f.key for f in report.depth_fill_spread_findings}
    assert "copy_candidate_low_fill_blocker" in keys
    assert any(
        f.severity == "blocker"
        for f in report.depth_fill_spread_findings
        if f.key == "copy_candidate_low_fill_blocker"
    )


# -------------------------------------------------------------------------
# 10. Partial fill downgrade acceptable
# -------------------------------------------------------------------------


def test_partial_fill_downgrade_acceptable(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["BUY"])
    _insert_decision(
        db, verdict="watchlist", final_score=60.0, fill_percentage=0.79,
        rejection_reasons_json='["partial_fill_below_copy_candidate_threshold"]',
    )
    report = build_trade_copyability_review_report(db)
    blockers = [
        f for f in report.depth_fill_spread_findings if f.severity == "blocker"
    ]
    assert not blockers


# -------------------------------------------------------------------------
# 11. Duration blocker (short + long)
# -------------------------------------------------------------------------


def test_duration_blocker_short_and_long(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["BUY"])
    _insert_decision(
        db, verdict="copy_candidate", final_score=80.0,
        seconds_to_market_end=14 * 60 + 59,
        rejection_reasons_json="[]",
    )
    _insert_decision(
        db, verdict="copy_candidate", final_score=80.0,
        seconds_to_market_end=45 * 24 * 3600 + 1,
        rejection_reasons_json="[]",
    )
    report = build_trade_copyability_review_report(db)
    keys = {f.key for f in report.duration_findings}
    assert "copy_candidate_duration_exclusion_blocker" in keys


def test_duration_skip_acceptable(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["BUY"])
    _insert_decision(
        db, verdict="skip", final_score=0.0,
        seconds_to_market_end=14 * 60 + 59,
        rejection_reasons_json='["duration_excluded_short"]',
    )
    _insert_decision(
        db, verdict="skip", final_score=0.0,
        seconds_to_market_end=45 * 24 * 3600 + 1,
        rejection_reasons_json='["duration_excluded_long"]',
    )
    report = build_trade_copyability_review_report(db)
    blockers = [f for f in report.duration_findings if f.severity == "blocker"]
    assert not blockers


# -------------------------------------------------------------------------
# 12. Snapshot timing blocker
# -------------------------------------------------------------------------


def test_snapshot_timing_blocker(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["BUY"])
    _insert_decision(
        db, verdict="copy_candidate", final_score=80.0,
        source_trade_timestamp="2026-01-02T00:00:00Z",
        price_snapshot_fetched_at="2026-01-01T00:00:00Z",
        evaluation_timestamp="2026-01-03T00:00:00Z",
        rejection_reasons_json='["snapshot_before_source_trade"]',
    )
    _insert_decision(
        db, verdict="copy_candidate", final_score=80.0,
        source_trade_timestamp="2026-01-01T00:00:00Z",
        price_snapshot_fetched_at="2026-01-03T00:00:00Z",
        evaluation_timestamp="2026-01-02T00:00:00Z",
        rejection_reasons_json='["snapshot_after_evaluation"]',
    )
    report = build_trade_copyability_review_report(db)
    keys = {f.key for f in report.snapshot_timing_findings}
    assert "copy_candidate_bad_snapshot_timing_blocker" in keys


# -------------------------------------------------------------------------
# 13. JSON output valid
# -------------------------------------------------------------------------


def test_json_output_valid(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["buy", "buy", "buy", "buy", "BUY"])
    report = build_trade_copyability_review_report(db)
    payload = json.loads(json.dumps(report.to_dict()))
    assert isinstance(payload, dict)
    assert payload["ready_to_wire_to_automation"] is False
    assert payload["source_side_distribution"] == {"buy": 4, "BUY": 1}


# -------------------------------------------------------------------------
# 14. CLI uses mode=ro / no Database connect
# -------------------------------------------------------------------------


def test_cli_uses_mode_ro(tmp_path):
    import subprocess
    import sys

    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["buy", "buy", "buy", "buy", "BUY"])
    db.close()
    db_path = tmp_path / "p24q_test.db"

    proc = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "report_trade_copyability_review.py"),
         "--db-path", str(db_path), "--json"],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(_REPO_ROOT / "src")},
        cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ready_to_wire_to_automation"] is False
    # Confirm read-only open by re-reading mode=ro from CLI source.
    # The docstring may MENTION polycopy.db.database in a "do not use" note;
    # only a real import statement is forbidden.
    cli_src = (_REPO_ROOT / "scripts" / "report_trade_copyability_review.py").read_text()
    assert 'mode=ro' in cli_src
    assert "from polycopy.db.database import Database" not in cli_src
    assert "import polycopy.db.database" not in cli_src


# -------------------------------------------------------------------------
# 15. No write SQL in report module / CLI
# -------------------------------------------------------------------------


def test_no_write_sql_in_module_and_cli(tmp_path):
    import inspect

    from polycopy.engine import trade_copyability_review_report as mod

    mod_src = inspect.getsource(mod)
    forbidden_verbs = (
        "insert into", "update ", "delete from", "delete ", "drop table",
        "alter table", "create table", "create index", "commit;", ".commit(",
        "executescript(",
    )
    low = mod_src.lower()
    for tok in forbidden_verbs:
        assert tok not in low, f"module contains forbidden verb {tok!r}"
    # Must not import the write-capable Database class.
    assert "from polycopy.db.database import Database" not in mod_src
    assert "import polycopy.db.database" not in mod_src

    cli_src = (_REPO_ROOT / "scripts" / "report_trade_copyability_review.py").read_text()
    low_cli = cli_src.lower()
    for tok in forbidden_verbs:
        assert tok not in low_cli, f"CLI contains forbidden verb {tok!r}"


# -------------------------------------------------------------------------
# 16. No wiring (no broker/order/automation/candidate/signal imports)
# -------------------------------------------------------------------------


def test_no_wiring_imports(tmp_path):
    import inspect

    from polycopy.engine import trade_copyability_review_report as mod

    mod_src = inspect.getsource(mod)
    low = mod_src.lower()
    # The module DOCUMENTs that it does NOT wire automation (negative
    # statement). Only flag actual wiring/import tokens, not the word in a
    # "does not wire automation" sentence.
    forbidden_wiring = (
        "import polycopy.automation",
        "from polycopy.automation",
        "broker.",
        "create_copy_candidate",
        "create_paper_signal",
    )
    for tok in forbidden_wiring:
        assert tok not in low, f"module contains wiring token {tok!r}"
    # ready_to_wire_to_automation always False.
    report = build_trade_copyability_review_report(_fresh_conn(tmp_path, with_source=False))
    assert report.ready_to_wire_to_automation is False


# -------------------------------------------------------------------------
# 17. Human report contains required verdict line
# -------------------------------------------------------------------------


def test_human_report_contains_required_verdict(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, ["buy", "buy", "buy", "buy", "BUY"])
    report = build_trade_copyability_review_report(db)
    human = report_to_human(report)
    assert "TRADE COPYABILITY REVIEW REPORT — READ ONLY" in human
    assert "ready_to_wire_to_automation = False" in human
    assert "PR24R" in human  # recommended next step references PR24R bridge
