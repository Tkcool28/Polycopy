"""Tests for PR24R Trade Copyability bridge audit (read-only / dry-run).

These tests build throwaway in-memory / temp-file SQLite DBs with real
production schema columns and assert the bridge audit behaves correctly
without ever opening the production DB or writing to it.

The synthetic scoring test (Test 6 / 3) uses unmistakably fake identifiers
(synthetic_test_only / do_not_use) so the dry-run scoring path is proven
without ever being mistaken for production evidence.

Run:
  PYTHONPATH=src pytest tests/test_p24r_trade_copyability_bridge_audit.py -q
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from polycopy.engine import trade_copyability_bridge_audit as bridge_mod
from polycopy.engine.trade_copyability_bridge_audit import (
    build_trade_copyability_bridge_audit,
    canonicalize_source_side,
    report_to_human,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Minimal but realistic source_trades DDL (production columns, PART 4).
_SOURCE_TRADES_DDL = """
CREATE TABLE source_trades (
    id TEXT,
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

# Wider DDL exposing optional current-market/snapshot columns for the
# can_compute_score happy path (used only in synthetic scoring tests).
_SOURCE_TRADES_FULL_DDL = """
CREATE TABLE source_trades (
    id TEXT,
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
    current_copy_price REAL,
    estimated_fill_price REAL,
    price_deterioration_pct REAL,
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
    price_snapshot_fetched_at TEXT,
    evaluation_timestamp TEXT
);
"""

# Production guardrail tables (read-only counts only).
_GUARD_DDL = """
CREATE TABLE trade_copyability_decisions (
    id INTEGER PRIMARY KEY,
    wallet_id TEXT,
    source_trade_id TEXT,
    verdict TEXT
);
CREATE TABLE copy_candidates (
    id INTEGER PRIMARY KEY,
    wallet_id TEXT,
    source_trade_id TEXT,
    side TEXT
);
CREATE TABLE paper_signal_decisions (
    id INTEGER PRIMARY KEY,
    wallet_id TEXT,
    source_trade_id TEXT
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY
);
CREATE TABLE positions (
    id INTEGER PRIMARY KEY
);
"""


def _fresh_conn(tmp_path: Path, ddl: str = _SOURCE_TRADES_DDL) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "p24r_test.db"))
    db.execute(ddl)
    db.executescript(_GUARD_DDL)
    db.commit()
    return db


def _seed_source_side(db: sqlite3.Connection, rows: list[dict]) -> None:
    cols = [
        "source", "source_trade_id", "market_source_id", "side", "outcome",
        "quantity", "price", "trader_address", "timestamp", "is_sample",
        "token_id",
    ]
    for i, r in enumerate(rows):
        sd = r.get("source_trade_id", f"st-{i}")
        vals = (
            r.get("source", "t"),
            sd,
            r.get("market_source_id", "mkt-1"),
            r.get("side"),
            r.get("outcome", "Yes"),
            r.get("quantity", 10.0),
            r.get("price", 0.5),
            r.get("trader_address", f"0xtrader_{i}_do_not_use"),
            r.get("timestamp", "2026-01-01T00:00:00Z"),
            r.get("is_sample", 1),
            r.get("token_id", f"tok_{i}_do_not_use"),
        )
        placeholders = ", ".join("?" for _ in vals)
        db.execute(
            f"INSERT INTO source_trades ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
    db.commit()


def _seed_full(db: sqlite3.Connection, rows: list[dict]) -> None:
    cols = [
        "source", "source_trade_id", "market_source_id", "side", "outcome",
        "quantity", "price", "trader_address", "timestamp", "is_sample",
        "token_id", "current_copy_price", "estimated_fill_price",
        "price_deterioration_pct", "intended_stake", "executable_depth",
        "fill_percentage", "spread", "best_bid_size", "best_ask_size",
        "trade_age_seconds", "seconds_to_market_end", "market_active",
        "market_closed", "market_resolved", "price_snapshot_fetched_at",
        "evaluation_timestamp",
    ]
    for i, r in enumerate(rows):
        sd = r.get("source_trade_id", f"st-{i}")
        vals = (
            r.get("source", "t"),
            sd,
            r.get("market_source_id", "mkt-1"),
            r.get("side", "BUY"),
            r.get("outcome", "Yes"),
            r.get("quantity", 100.0),
            r.get("price", 0.5),
            r.get("trader_address", "0xsynthetic_test_only"),
            r.get("timestamp", "2026-01-01T00:00:00Z"),
            r.get("is_sample", 1),
            r.get("token_id", "synthetic_token_do_not_use"),
            r.get("current_copy_price", 0.5),
            r.get("estimated_fill_price", 0.5),
            r.get("price_deterioration_pct", 0.0),
            r.get("intended_stake", 100.0),
            r.get("executable_depth", 200.0),
            r.get("fill_percentage", 1.0),
            r.get("spread", 0.02),
            r.get("best_bid_size", 500.0),
            r.get("best_ask_size", 500.0),
            r.get("trade_age_seconds", 60.0),
            r.get("seconds_to_market_end", 24 * 3600.0),
            r.get("market_active", 1),
            r.get("market_closed", 0),
            r.get("market_resolved", 0),
            r.get("price_snapshot_fetched_at", "2026-01-01T00:00:05Z"),
            r.get("evaluation_timestamp", "2026-01-01T00:00:10Z"),
        )
        placeholders = ", ".join("?" for _ in vals)
        db.execute(
            f"INSERT INTO source_trades ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
    db.commit()


def _guard_counts(db: sqlite3.Connection) -> dict:
    out = {}
    for t in ("trade_copyability_decisions", "copy_candidates",
              "paper_signal_decisions", "orders", "positions"):
        out[t] = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return out


# -------------------------------------------------------------------------
# 1. Empty DB / no source_trades exits cleanly
# -------------------------------------------------------------------------


def test_empty_db_no_source_trades_ready_false(tmp_path):
    db = sqlite3.connect(str(tmp_path / "empty.db"))
    db.executescript(_GUARD_DDL)
    db.commit()
    report = build_trade_copyability_bridge_audit(db)
    assert report.ready_to_wire_to_automation is False
    assert report.ready_to_persist_decisions is False
    assert report.source_trade_count == 0
    assert report.bridge_ready_count == 0
    assert report.score_attempt_count == 0


# -------------------------------------------------------------------------
# 2. Side canonicalization
# -------------------------------------------------------------------------


def test_canonicalize_source_side():
    assert canonicalize_source_side("buy") == ("BUY", "canonicalized_buy", None)
    assert canonicalize_source_side("BUY") == ("BUY", "canonicalized_buy", None)
    assert canonicalize_source_side("sell") == (
        "SELL", "canonicalized_sell_unsupported_v1",
        "sell_side_copyability_not_supported_v1",
    )
    assert canonicalize_source_side("SELL") == (
        "SELL", "canonicalized_sell_unsupported_v1",
        "sell_side_copyability_not_supported_v1",
    )
    assert canonicalize_source_side(None) == (None, "missing", "missing_side")
    assert canonicalize_source_side("") == (None, "missing", "missing_side")
    assert canonicalize_source_side("garbage") == (None, "invalid", "invalid_side")


# -------------------------------------------------------------------------
# 3. Mixed casing finding (CORRECTION 1)
# -------------------------------------------------------------------------


def test_ingestion_side_inconsistency_finding(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [
        {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "BUY"},
    ])
    report = build_trade_copyability_bridge_audit(db)
    # Exact raw casing preserved.
    assert report.raw_side_distribution == {"buy": 4, "BUY": 1}
    # Canonical BUY count correct.
    assert report.canonical_side_distribution == {"BUY": 5}
    # Finding emitted.
    keys = {f.key for f in report.findings}
    assert "ingestion_side_inconsistency" in keys
    finding = next(f for f in report.findings if f.key == "ingestion_side_inconsistency")
    assert finding.severity == "warning"
    assert finding.evidence["affected_logical_sides"] == ["BUY"]


# -------------------------------------------------------------------------
# 4. SELL unsupported
# -------------------------------------------------------------------------


def test_sell_unsupported_not_bridge_ready(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [{"side": "BUY"}, {"side": "SELL"}, {"side": "sell"}])
    report = build_trade_copyability_bridge_audit(db)
    # SELL raw count present.
    assert report.raw_side_distribution.get("SELL", 0) == 1
    assert report.raw_side_distribution.get("sell", 0) == 1
    # No canonical BUY/SELL eligibility for SELL rows.
    sell_rows = [r for r in report.row_audits
                 if r.raw_side in ("SELL", "sell")]
    for r in sell_rows:
        assert r.canonical_side == "SELL"
        assert r.can_build_input is False
        assert "sell_side_copyability_not_supported_v1" in r.bridge_blocked_reasons
        assert r.dry_run_verdict is None
    # No copy_candidate / watchlist eligibility.
    assert report.dry_run_verdict_counts == {}


# -------------------------------------------------------------------------
# 5. Missing current price/depth blocks score
# -------------------------------------------------------------------------


def test_missing_current_price_depth_blocks_score(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [{
        "side": "BUY",
        "price": 0.5,
        "quantity": 50.0,
        "trader_address": "0xtrader_blocked_do_not_use",
        "token_id": "tok_blocked_do_not_use",
        "timestamp": "2026-01-01T00:00:00Z",
    }])
    report = build_trade_copyability_bridge_audit(db)
    ra = report.row_audits[0]
    # Source row maps (can_build_input may be True) but cannot score.
    assert ra.canonical_side == "BUY"
    assert ra.can_compute_score is False
    assert report.score_attempt_count == 0
    # Blocked reasons explain missing evidence.
    blockers = set(ra.bridge_blocked_reasons)
    assert blockers & {
        "missing_current_copy_price",
        "missing_depth_snapshot",
        "missing_fill_percentage",
        "missing_seconds_to_market_end",
        "missing_spread",
        "missing_market_state",
        "missing_price_snapshot_timing",
    }


# -------------------------------------------------------------------------
# 6. Complete synthetic BUY can dry-run score (CORRECTION 3)
# -------------------------------------------------------------------------


def test_complete_synthetic_buy_do_not_use_can_dry_run_score_without_persistence(tmp_path):
    # This data is synthetic_test_only and must never be interpreted as
    # production evidence.
    db = _fresh_conn(tmp_path, ddl=_SOURCE_TRADES_FULL_DDL)
    _seed_full(db, [{
        "source_trade_id": "synthetic_source_trade_do_not_use",
        "trader_address": "0xsynthetic_test_only",
        "wallet_id": "synthetic_wallet_do_not_use",
        "market_source_id": "synthetic_market_do_not_use",
        "token_id": "synthetic_token_do_not_use",
        "side": "BUY",
        "price": 0.5,
        "quantity": 100.0,
        "current_copy_price": 0.5,
        "estimated_fill_price": 0.5,
        "price_deterioration_pct": 0.0,
        "intended_stake": 100.0,
        "executable_depth": 200.0,
        "fill_percentage": 1.0,
        "spread": 0.02,
        "best_bid_size": 500.0,
        "best_ask_size": 500.0,
        "trade_age_seconds": 60.0,
        "seconds_to_market_end": 24 * 3600.0,
        "market_active": 1,
        "market_closed": 0,
        "market_resolved": 0,
        "timestamp": "2026-01-01T00:00:00Z",
        "price_snapshot_fetched_at": "2026-01-01T00:00:05Z",
        "evaluation_timestamp": "2026-01-01T00:00:10Z",
    }])
    before = _guard_counts(db)
    report = build_trade_copyability_bridge_audit(db)
    ra = report.row_audits[0]
    # Synthetic IDs appear in the row audit.
    assert ra.source_trade_id == "synthetic_source_trade_do_not_use"
    assert "synthetic_test_only" in (ra.trader_address or "")
    assert "synthetic_token_do_not_use" in (ra.token_id or "")
    # Dry-run score was attempted.
    assert ra.can_build_input is True
    assert ra.can_compute_score is True
    assert report.score_attempt_count == 1
    assert ra.dry_run_verdict is not None
    assert ra.dry_run_score is not None
    # Ready flags still False.
    assert report.ready_to_wire_to_automation is False
    assert report.ready_to_persist_decisions is False
    # No DB write occurred.
    after = _guard_counts(db)
    assert after == before


# -------------------------------------------------------------------------
# 7. No persistence
# -------------------------------------------------------------------------


def test_no_persistence_after_report(tmp_path):
    db = _fresh_conn(tmp_path, ddl=_SOURCE_TRADES_FULL_DDL)
    _seed_full(db, [{
        "source_trade_id": "synthetic_source_trade_do_not_use",
        "trader_address": "0xsynthetic_test_only",
        "token_id": "synthetic_token_do_not_use",
        "side": "BUY",
        "price": 0.5,
        "current_copy_price": 0.5,
        "estimated_fill_price": 0.5,
        "intended_stake": 100.0,
        "executable_depth": 200.0,
        "fill_percentage": 1.0,
        "spread": 0.02,
        "seconds_to_market_end": 24 * 3600.0,
        "market_active": 1,
    }])
    before = _guard_counts(db)
    build_trade_copyability_bridge_audit(db)
    after = _guard_counts(db)
    assert after["trade_copyability_decisions"] == before["trade_copyability_decisions"] == 0
    assert after["copy_candidates"] == before["copy_candidates"] == 0
    assert after["paper_signal_decisions"] == before["paper_signal_decisions"] == 0


# -------------------------------------------------------------------------
# 8. Production-style current data (4 buy + 1 BUY)
# -------------------------------------------------------------------------


def test_production_style_current_data(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [
        {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "BUY"},
    ])
    report = build_trade_copyability_bridge_audit(db)
    assert report.source_trade_count == 5
    assert report.raw_side_distribution == {"buy": 4, "BUY": 1}
    assert report.canonical_side_distribution == {"BUY": 5}
    assert report.raw_side_distribution.get("SELL", 0) == 0
    assert report.ready_to_wire_to_automation is False
    assert report.ready_to_persist_decisions is False


# -------------------------------------------------------------------------
# 9. Human report contains required title and flags
# -------------------------------------------------------------------------


def test_human_report_contains_required_title_and_flags(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [
        {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "BUY"},
    ])
    report = build_trade_copyability_bridge_audit(db)
    human = report_to_human(report)
    assert "TRADE COPYABILITY BRIDGE AUDIT — READ ONLY / DRY RUN" in human
    assert "ready_to_wire_to_automation = False" in human
    assert "ready_to_persist_decisions = False" in human
    assert ("This report does not persist decisions, create candidates, create "
            "paper signals, or place orders.") in human
    assert "PR24S" in human


# -------------------------------------------------------------------------
# 10. JSON output valid
# -------------------------------------------------------------------------


def test_json_output_valid(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [
        {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "BUY"},
    ])
    report = build_trade_copyability_bridge_audit(db)
    payload = json.loads(json.dumps(report.to_dict()))
    assert isinstance(payload, dict)
    assert payload["ready_to_wire_to_automation"] is False
    assert payload["ready_to_persist_decisions"] is False
    assert payload["raw_side_distribution"] == {"buy": 4, "BUY": 1}
    assert payload["canonical_side_distribution"] == {"BUY": 5}
    # Round-trips through json.tool.
    json.dumps(payload)


# -------------------------------------------------------------------------
# 11. CLI opens mode=ro / no Database connect
# -------------------------------------------------------------------------


def test_cli_uses_mode_ro(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [
        {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "BUY"},
    ])
    db.close()
    db_path = tmp_path / "p24r_test.db"

    proc = subprocess.run(
        [sys.executable,
         str(_REPO_ROOT / "scripts" / "report_trade_copyability_bridge_audit.py"),
         "--db-path", str(db_path), "--json"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT / "src")},
        cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ready_to_wire_to_automation"] is False
    # Confirm read-only open by static source check.
    cli_src = (_REPO_ROOT / "scripts" / "report_trade_copyability_bridge_audit.py").read_text()
    assert 'mode=ro' in cli_src
    assert "from polycopy.db.database import Database" not in cli_src
    assert "import polycopy.db.database" not in cli_src


# -------------------------------------------------------------------------
# 12. No write SQL in module/CLI
# -------------------------------------------------------------------------


def test_no_write_sql_in_module_and_cli(tmp_path):
    mod_src = inspect.getsource(bridge_mod)
    forbidden_verbs = (
        "insert into", "update ", "delete from", "delete ", "drop table",
        "alter table", "create table", "create index", "commit;", ".commit(",
        "executescript(", "Database(",
    )
    low = mod_src.lower()
    for tok in forbidden_verbs:
        assert tok not in low, f"module contains forbidden verb {tok!r}"

    cli_src = (_REPO_ROOT / "scripts" / "report_trade_copyability_bridge_audit.py").read_text()
    low_cli = cli_src.lower()
    for tok in forbidden_verbs:
        assert tok not in low_cli, f"CLI contains forbidden verb {tok!r}"

    # Must not import the write-capable Database class.
    assert "from polycopy.db.database import Database" not in mod_src
    assert "import polycopy.db.database" not in mod_src


# -------------------------------------------------------------------------
# 13. No wiring imports
# -------------------------------------------------------------------------


def test_no_wiring_imports(tmp_path):
    mod_src = inspect.getsource(bridge_mod)
    low = mod_src.lower()
    # Only flag actual wiring/import tokens (the module DOCUMENTS that it
    # does NOT wire automation/order placement, so negative sentences
    # containing "automation" / "order placement" must not trip this check).
    forbidden_wiring = (
        "import polycopy.automation",
        "from polycopy.automation",
        "broker.",
        "create_copy_candidate",
        "create_paper_signal",
        "place_order",
        "submit_order",
    )
    for tok in forbidden_wiring:
        assert tok not in low, f"module contains wiring token {tok!r}"


# -------------------------------------------------------------------------
# 14. Ready flags always false (even with complete synthetic score)
# -------------------------------------------------------------------------


def test_ready_flags_always_false_even_with_score(tmp_path):
    db = _fresh_conn(tmp_path, ddl=_SOURCE_TRADES_FULL_DDL)
    _seed_full(db, [{
        "source_trade_id": "synthetic_source_trade_do_not_use",
        "trader_address": "0xsynthetic_test_only",
        "token_id": "synthetic_token_do_not_use",
        "side": "BUY",
        "price": 0.5,
        "current_copy_price": 0.5,
        "estimated_fill_price": 0.5,
        "intended_stake": 100.0,
        "executable_depth": 200.0,
        "fill_percentage": 1.0,
        "spread": 0.02,
        "seconds_to_market_end": 24 * 3600.0,
        "market_active": 1,
    }])
    report = build_trade_copyability_bridge_audit(db)
    assert report.score_attempt_count == 1
    assert report.ready_to_wire_to_automation is False
    assert report.ready_to_persist_decisions is False


# -------------------------------------------------------------------------
# 15. SELL can never be eligible (even with complete other fields)
# -------------------------------------------------------------------------


def test_sell_never_eligible_even_if_complete(tmp_path):
    db = _fresh_conn(tmp_path, ddl=_SOURCE_TRADES_FULL_DDL)
    _seed_full(db, [{
        "source_trade_id": "synthetic_sell_do_not_use",
        "trader_address": "0xsynthetic_test_only",
        "token_id": "synthetic_token_do_not_use",
        "side": "SELL",
        "price": 0.5,
        "current_copy_price": 0.5,
        "estimated_fill_price": 0.5,
        "intended_stake": 100.0,
        "executable_depth": 200.0,
        "fill_percentage": 1.0,
        "spread": 0.02,
        "seconds_to_market_end": 24 * 3600.0,
        "market_active": 1,
    }])
    report = build_trade_copyability_bridge_audit(db)
    ra = report.row_audits[0]
    # SELL blocked before eligible score, even though other evidence is present.
    assert ra.canonical_side == "SELL"
    assert ra.can_build_input is False
    assert ra.can_compute_score is False
    assert ra.dry_run_verdict is None
    assert "sell_side_copyability_not_supported_v1" in ra.bridge_blocked_reasons
    # No copy_candidate / watchlist verdict anywhere.
    assert "copy_candidate" not in report.dry_run_verdict_counts
    assert "watchlist" not in report.dry_run_verdict_counts


# -------------------------------------------------------------------------
# CORRECTION 6.1 ingestion_side_inconsistency finding (covered by test 3)
# CORRECTION 6.2 can_build_input lower bar
# -------------------------------------------------------------------------


def test_can_build_input_lower_bar_no_score(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [{
        "side": "BUY",
        "price": 0.5,
        "quantity": 50.0,
        "trader_address": "0xtrader_lower_do_not_use",
        "token_id": "tok_lower_do_not_use",
        "timestamp": "2026-01-01T00:00:00Z",
    }])
    report = build_trade_copyability_bridge_audit(db)
    ra = report.row_audits[0]
    # Can map (build input) but no current-market evidence -> cannot score.
    assert ra.canonical_side == "BUY"
    assert ra.can_build_input is True
    assert ra.can_compute_score is False
    assert report.score_attempt_count == 0
    # Blocked reasons identify missing current-market evidence.
    assert "missing_current_copy_price" in ra.bridge_blocked_reasons


# -------------------------------------------------------------------------
# CORRECTION 6.3 can_compute_score higher bar (covered by test 6)
# CORRECTION 6.4 synthetic test naming (covered by test 6)
# CORRECTION 6.5 zero production score attempts not failure
# -------------------------------------------------------------------------


def test_zero_production_score_attempts_not_failure(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [
        {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "BUY"},
    ])
    report = build_trade_copyability_bridge_audit(db)
    # Exits cleanly, no score attempts, module not failed.
    assert report.source_trade_count == 5
    assert report.score_attempt_count == 0
    assert report.ready_to_wire_to_automation is False
    assert report.ready_to_persist_decisions is False
    # Explicit finding: zero attempts expected, not a failure.
    keys = {f.key for f in report.findings}
    assert "zero_production_score_attempts_expected" in keys
    # Missing-evidence summary present.
    assert "bridge_blocked_reason_summary" in keys


def test_recommended_next_step_includes_pr24s(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [
        {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "BUY"},
    ])
    report = build_trade_copyability_bridge_audit(db)
    assert "PR24S" in report.recommended_next_step
    assert "Do not wire automation until persisted dry-run decisions are reviewed." in report.recommended_next_step
