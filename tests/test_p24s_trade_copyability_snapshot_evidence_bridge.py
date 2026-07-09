"""Tests for PR24S Trade Copyability snapshot evidence bridge (read-only / dry-run).

These tests build throwaway in-memory / temp-file SQLite DBs with real
production schema columns and assert the snapshot-evidence bridge behaves
correctly without ever opening the production DB or writing to it.

Synthetic depth tests use unmistakably fake identifiers
(synthetic_test_only / do_not_use) so the evidence path is proven without ever
being mistaken for production evidence.

Run:
  PYTHONPATH=src pytest tests/test_p24s_trade_copyability_snapshot_evidence_bridge.py -q
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

from polycopy.engine import trade_copyability_snapshot_evidence_bridge as sev_mod
from polycopy.engine.trade_copyability_snapshot_evidence_bridge import (
    SnapshotEvidenceProvider,
    build_trade_copyability_snapshot_evidence_bridge,
    report_to_human,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Minimal but realistic source_trades DDL (production columns).
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

# Guardrail tables (read-only counts only).
_GUARD_DDL = """
CREATE TABLE trade_copyability_decisions (id INTEGER PRIMARY KEY, wallet_id TEXT, source_trade_id TEXT, verdict TEXT);
CREATE TABLE copy_candidates (id INTEGER PRIMARY KEY, wallet_id TEXT, source_trade_id TEXT, side TEXT);
CREATE TABLE paper_signal_decisions (id INTEGER PRIMARY KEY, wallet_id TEXT, source_trade_id TEXT);
CREATE TABLE orders (id INTEGER PRIMARY KEY);
CREATE TABLE positions (id INTEGER PRIMARY KEY);
CREATE TABLE candidate_price_snapshots (
    id TEXT, candidate_id INTEGER, snapshot_run_id TEXT, fetch_status TEXT,
    token_id TEXT, side TEXT, source_trade_price REAL, source_trade_quantity REAL,
    best_bid REAL, best_bid_size REAL, best_ask REAL, best_ask_size REAL,
    mid_price REAL, spread REAL, executable_price REAL, executable_side_depth REAL,
    expected_fill_price REAL, price_deterioration REAL, price_deterioration_pct REAL,
    trade_age_seconds INTEGER, market_end_at TEXT, seconds_to_market_end INTEGER,
    market_metadata_fetched_at TEXT, market_active_at_fetch INTEGER,
    market_closed_at_fetch INTEGER, market_resolved_at_fetch INTEGER,
    bid_level_count INTEGER, ask_level_count INTEGER, book_summary_json TEXT,
    book_hash TEXT, fetched_at TEXT, created_at TEXT
);
CREATE TABLE candidate_price_snapshot_levels (
    id INTEGER, snapshot_id TEXT, side TEXT, level_index INTEGER,
    price REAL, size REAL, cumulative_size REAL, cumulative_notional REAL,
    created_at TEXT
);
"""


def _fresh_conn(tmp_path: Path, with_snapshot_tables: bool = False) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "p24s_test.db"))
    db.execute(_SOURCE_TRADES_DDL)
    db.executescript(_GUARD_DDL if with_snapshot_tables else _GUARD_DDL.replace(
        "CREATE TABLE candidate_price_snapshots", "CREATE TABLE candidate_price_snapshots_DISABLED"
    ).replace(
        "CREATE TABLE candidate_price_snapshot_levels", "CREATE TABLE candidate_price_snapshot_levels_DISABLED"
    ))
    db.commit()
    return db


def _seed_source_side(db: sqlite3.Connection, rows: list[dict]) -> None:
    cols = [
        "source", "source_trade_id", "market_source_id", "side", "outcome",
        "quantity", "price", "trader_address", "timestamp", "is_sample", "token_id",
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
            f"INSERT INTO source_trades ({', '.join(cols)}) VALUES ({placeholders})", vals
        )
    db.commit()


def _seed_existing_snapshot(db: sqlite3.Connection, token_id: str, *,
                            seconds_to_market_end: Optional[int] = 24 * 3600,
                            market_active: Optional[int] = 1, market_closed: Optional[int] = 0,
                            market_resolved: Optional[int] = 0) -> None:
    db.execute(
        "INSERT INTO candidate_price_snapshots "
        "(id, candidate_id, snapshot_run_id, fetch_status, token_id, side, "
        "source_trade_price, source_trade_quantity, fetched_at, created_at, "
        "seconds_to_market_end, market_active_at_fetch, market_closed_at_fetch, "
        "market_resolved_at_fetch) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("snap-1", 1, "run-1", "ok", token_id, "BUY", 0.5, 100.0,
         "2026-01-01T00:00:05Z", "2026-01-01T00:00:10Z",
         seconds_to_market_end, market_active, market_closed, market_resolved),
    )
    db.commit()


def _guard_counts(db: sqlite3.Connection) -> dict:
    out = {}
    for t in ("trade_copyability_decisions", "copy_candidates",
              "paper_signal_decisions", "orders", "positions",
              "candidate_price_snapshots", "candidate_price_snapshot_levels"):
        try:
            out[t] = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = None
    return out


class SyntheticDepthProvider(SnapshotEvidenceProvider):
    """Injectable provider returning synthetic ask/bid levels (no network)."""

    def __init__(self, asks: list[tuple], bids: list[tuple]):
        self.asks = asks
        self.bids = bids

    def fetch_depth(self, *, token_id=None, side="BUY"):
        return list(self.asks), list(self.bids)


# -------------------------------------------------------------------------
# 1. Empty DB exits cleanly
# -------------------------------------------------------------------------
def test_empty_db_ready_false(tmp_path):
    db = sqlite3.connect(str(tmp_path / "empty.db"))
    db.executescript(_GUARD_DDL)
    db.commit()
    report = build_trade_copyability_snapshot_evidence_bridge(db)
    assert report.ready_to_wire_to_automation is False
    assert report.ready_to_persist_decisions is False
    assert report.ready_to_create_candidates is False
    assert report.source_trade_count == 0
    assert report.snapshot_evidence_ready_count == 0


# -------------------------------------------------------------------------
# 2. Production-style current data (4 buy + 1 BUY)
# -------------------------------------------------------------------------
def test_production_style_current_data(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [
        {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "BUY"},
    ])
    report = build_trade_copyability_snapshot_evidence_bridge(db)
    assert report.source_trade_count == 5
    assert report.raw_side_distribution == {"buy": 4, "BUY": 1}
    assert report.canonical_side_distribution == {"BUY": 5}
    assert report.ingestion_side_inconsistency_present is True
    assert report.ready_to_wire_to_automation is False
    assert report.ready_to_persist_decisions is False
    assert report.ready_to_create_candidates is False
    # No persistence: guard counts zero.
    before = _guard_counts(db)
    assert before["trade_copyability_decisions"] == 0
    assert before["copy_candidates"] == 0


# -------------------------------------------------------------------------
# 3. Missing token_id blocks snapshot attempt
# -------------------------------------------------------------------------
def test_missing_token_id_blocks_attempt(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [{
        "side": "BUY", "price": 0.5, "quantity": 50.0,
        "token_id": None,  # explicitly NULL
        "trader_address": "0xtrader_blocked_do_not_use",
    }])
    report = build_trade_copyability_snapshot_evidence_bridge(db)
    ra = report.row_audits[0]
    assert ra.canonical_side == "BUY"
    assert ra.can_attempt_snapshot_evidence is False
    assert "missing_token_id" in ra.evidence_blocked_reasons


# -------------------------------------------------------------------------
# 4. One token_id row can attempt snapshot evidence
# -------------------------------------------------------------------------
def test_one_token_id_row_can_attempt(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [{
        "side": "BUY", "price": 0.5, "quantity": 100.0,
        "token_id": "tok_attempt_do_not_use",
        "trader_address": "0xtrader_attempt_do_not_use",
    }])
    report = build_trade_copyability_snapshot_evidence_bridge(db)
    ra = report.row_audits[0]
    assert ra.can_attempt_snapshot_evidence is True
    assert ra.can_build_snapshot_evidence is False
    # No depth/snapshot available -> blocked accordingly.
    assert ("missing_depth_levels" in ra.evidence_blocked_reasons
            or "missing_existing_price_snapshot" in ra.evidence_blocked_reasons)


# -------------------------------------------------------------------------
# 5. Synthetic depth can build snapshot evidence
# -------------------------------------------------------------------------
def test_synthetic_depth_can_build_snapshot_evidence(tmp_path):
    # This data is synthetic_test_only and must never be production evidence.
    db = _fresh_conn(tmp_path, with_snapshot_tables=True)
    _seed_source_side(db, [{
        "source_trade_id": "synthetic_source_trade_do_not_use",
        "side": "BUY", "price": 0.5, "quantity": 100.0,
        "token_id": "synthetic_token_do_not_use",
        "market_id": "synthetic_market_do_not_use",
        "trader_address": "0xsynthetic_test_only",
    }])
    _seed_existing_snapshot(db, "synthetic_token_do_not_use")
    provider = SyntheticDepthProvider(
        asks=[(0.50, 200.0), (0.51, 200.0)],  # enough to fill 100 stake
        bids=[(0.49, 200.0)],
    )
    before = _guard_counts(db)
    report = build_trade_copyability_snapshot_evidence_bridge(
        db, provider=provider
    )
    ra = report.row_audits[0]
    assert ra.source_trade_id == "synthetic_source_trade_do_not_use"
    assert ra.token_id == "synthetic_token_do_not_use"
    assert ra.can_build_snapshot_evidence is True
    assert report.snapshot_evidence_ready_count > 0
    ev = ra.snapshot_evidence
    assert ev is not None
    assert ev.current_copy_price is not None
    assert ev.estimated_fill_price is not None
    assert ev.executable_depth is not None
    assert ev.fill_percentage is not None
    assert ev.spread is not None
    assert ev.depth_hash is not None
    # Ready flags remain False.
    assert report.ready_to_wire_to_automation is False
    assert report.ready_to_persist_decisions is False
    assert report.ready_to_create_candidates is False
    # No DB write.
    after = _guard_counts(db)
    assert after == before


# -------------------------------------------------------------------------
# 6. Partial fill is honest
# -------------------------------------------------------------------------
def test_partial_fill_honest(tmp_path):
    db = _fresh_conn(tmp_path, with_snapshot_tables=True)
    _seed_source_side(db, [{
        "source_trade_id": "synthetic_source_trade_do_not_use",
        "side": "BUY", "price": 0.5, "quantity": 1000.0,  # large intended stake
        "token_id": "synthetic_token_do_not_use",
        "trader_address": "0xsynthetic_test_only",
    }])
    _seed_existing_snapshot(db, "synthetic_token_do_not_use")
    provider = SyntheticDepthProvider(
        asks=[(0.50, 200.0), (0.51, 200.0)],  # only 400 notional available
        bids=[(0.49, 200.0)],
    )
    report = build_trade_copyability_snapshot_evidence_bridge(
        db, provider=provider
    )
    ra = report.row_audits[0]
    ev = ra.snapshot_evidence
    assert ev is not None
    assert ev.fill_percentage is not None
    assert ev.fill_percentage < 1.0  # honest partial fill
    # No candidate created.
    assert _guard_counts(db)["copy_candidates"] == 0


# -------------------------------------------------------------------------
# 7. Severe partial fill flagged
# -------------------------------------------------------------------------
def test_severe_partial_fill_flagged(tmp_path):
    db = _fresh_conn(tmp_path, with_snapshot_tables=True)
    _seed_source_side(db, [{
        "source_trade_id": "synthetic_source_trade_do_not_use",
        "side": "BUY", "price": 0.5, "quantity": 10000.0,  # huge stake
        "token_id": "synthetic_token_do_not_use",
        "trader_address": "0xsynthetic_test_only",
    }])
    _seed_existing_snapshot(db, "synthetic_token_do_not_use")
    provider = SyntheticDepthProvider(
        asks=[(0.50, 200.0)],  # only 200 notional -> 2% fill
        bids=[(0.49, 200.0)],
    )
    report = build_trade_copyability_snapshot_evidence_bridge(
        db, provider=provider
    )
    ra = report.row_audits[0]
    ev = ra.snapshot_evidence
    assert ev is not None
    assert ev.fill_percentage < 0.80
    assert "partial_fill_below_copy_candidate_threshold" in ra.evidence_blocked_reasons
    keys = {f.key for f in report.findings}
    assert "partial_fill_below_copy_candidate_threshold" in keys


# -------------------------------------------------------------------------
# 8. Missing market state blocks readiness
# -------------------------------------------------------------------------
def test_missing_market_state_blocks(tmp_path):
    db = _fresh_conn(tmp_path, with_snapshot_tables=True)
    _seed_source_side(db, [{
        "source_trade_id": "synthetic_source_trade_do_not_use",
        "side": "BUY", "price": 0.5, "quantity": 100.0,
        "token_id": "synthetic_token_do_not_use",
        "trader_address": "0xsynthetic_test_only",
    }])
    # Snapshot with NULL market-state columns.
    _seed_existing_snapshot(db, "synthetic_token_do_not_use",
                            market_active=None, market_closed=None, market_resolved=None)
    provider = SyntheticDepthProvider(
        asks=[(0.50, 200.0)], bids=[(0.49, 200.0)],
    )
    report = build_trade_copyability_snapshot_evidence_bridge(
        db, provider=provider
    )
    ra = report.row_audits[0]
    assert ra.can_build_snapshot_evidence is False
    assert "missing_market_state" in ra.evidence_blocked_reasons


# -------------------------------------------------------------------------
# 9. Missing seconds_to_market_end blocks readiness
# -------------------------------------------------------------------------
def test_missing_seconds_to_market_end_blocks(tmp_path):
    db = _fresh_conn(tmp_path, with_snapshot_tables=True)
    _seed_source_side(db, [{
        "source_trade_id": "synthetic_source_trade_do_not_use",
        "side": "BUY", "price": 0.5, "quantity": 100.0,
        "token_id": "synthetic_token_do_not_use",
        "trader_address": "0xsynthetic_test_only",
    }])
    # Snapshot with NULL seconds_to_market_end.
    _seed_existing_snapshot(db, "synthetic_token_do_not_use",
                            seconds_to_market_end=None)
    provider = SyntheticDepthProvider(
        asks=[(0.50, 200.0)], bids=[(0.49, 200.0)],
    )
    report = build_trade_copyability_snapshot_evidence_bridge(
        db, provider=provider
    )
    ra = report.row_audits[0]
    assert ra.can_build_snapshot_evidence is False
    assert "missing_seconds_to_market_end" in ra.evidence_blocked_reasons


# -------------------------------------------------------------------------
# 10. JSON output valid
# -------------------------------------------------------------------------
def test_json_output_valid(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [
        {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "BUY"},
    ])
    report = build_trade_copyability_snapshot_evidence_bridge(db)
    payload = json.loads(json.dumps(report.to_dict()))
    assert isinstance(payload, dict)
    assert payload["ready_to_wire_to_automation"] is False
    assert payload["ready_to_persist_decisions"] is False
    assert payload["ready_to_create_candidates"] is False
    assert payload["raw_side_distribution"] == {"buy": 4, "BUY": 1}
    assert payload["ingestion_side_inconsistency_present"] is True
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
    db_path = tmp_path / "p24s_test.db"
    proc = subprocess.run(
        [sys.executable,
         str(_REPO_ROOT / "scripts" / "report_trade_copyability_snapshot_evidence_bridge.py"),
         "--db-path", str(db_path), "--json"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT / "src")},
        cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ready_to_wire_to_automation"] is False
    cli_src = (_REPO_ROOT / "scripts" / "report_trade_copyability_snapshot_evidence_bridge.py").read_text()
    assert 'mode=ro' in cli_src
    assert "from polycopy.db.database import Database" not in cli_src
    assert "import polycopy.db.database" not in cli_src


# -------------------------------------------------------------------------
# 12. No production write SQL in module/CLI
# -------------------------------------------------------------------------
def test_no_write_sql_in_module_and_cli(tmp_path):
    mod_src = inspect.getsource(sev_mod)
    forbidden_verbs = (
        "insert into", "update ", "delete from", "delete ", "drop table",
        "alter table", "create table", "create index", "commit;", ".commit(",
        "executescript(", "Database(",
    )
    low = mod_src.lower()
    for tok in forbidden_verbs:
        assert tok not in low, f"module contains forbidden verb {tok!r}"
    cli_src = (_REPO_ROOT / "scripts" / "report_trade_copyability_snapshot_evidence_bridge.py").read_text()
    low_cli = cli_src.lower()
    for tok in forbidden_verbs:
        assert tok not in low_cli, f"CLI contains forbidden verb {tok!r}"
    assert "from polycopy.db.database import Database" not in mod_src
    assert "import polycopy.db.database" not in mod_src


# -------------------------------------------------------------------------
# 13. No wiring imports
# -------------------------------------------------------------------------
def test_no_wiring_imports(tmp_path):
    mod_src = inspect.getsource(sev_mod)
    low = mod_src.lower()
    forbidden_wiring = (
        "import polycopy.automation",
        "from polycopy.automation",
        "broker.",
        "create_copy_candidate",
        "create_paper_signal",
        "place_order",
        "submit_order",
        "specialist_aggregation",
    )
    for tok in forbidden_wiring:
        assert tok not in low, f"module contains wiring token {tok!r}"


# -------------------------------------------------------------------------
# 14. Ready flags always false (even with synthetic complete evidence)
# -------------------------------------------------------------------------
def test_ready_flags_always_false_even_with_evidence(tmp_path):
    db = _fresh_conn(tmp_path, with_snapshot_tables=True)
    _seed_source_side(db, [{
        "source_trade_id": "synthetic_source_trade_do_not_use",
        "side": "BUY", "price": 0.5, "quantity": 100.0,
        "token_id": "synthetic_token_do_not_use",
        "trader_address": "0xsynthetic_test_only",
    }])
    _seed_existing_snapshot(db, "synthetic_token_do_not_use")
    provider = SyntheticDepthProvider(
        asks=[(0.50, 200.0)], bids=[(0.49, 200.0)],
    )
    report = build_trade_copyability_snapshot_evidence_bridge(
        db, provider=provider
    )
    assert report.snapshot_evidence_ready_count > 0
    assert report.ready_to_wire_to_automation is False
    assert report.ready_to_persist_decisions is False
    assert report.ready_to_create_candidates is False


# -------------------------------------------------------------------------
# 15. Existing snapshot table read path (read-only, no new rows)
# -------------------------------------------------------------------------
def test_existing_snapshot_table_read_path(tmp_path):
    db = _fresh_conn(tmp_path, with_snapshot_tables=True)
    _seed_source_side(db, [{
        "source_trade_id": "synthetic_source_trade_do_not_use",
        "side": "BUY", "price": 0.5, "quantity": 100.0,
        "token_id": "synthetic_token_do_not_use",
        "trader_address": "0xsynthetic_test_only",
    }])
    _seed_existing_snapshot(db, "synthetic_token_do_not_use")
    provider = SyntheticDepthProvider(
        asks=[(0.50, 200.0)], bids=[(0.49, 200.0)],
    )
    before_snaps = db.execute("SELECT COUNT(*) FROM candidate_price_snapshots").fetchone()[0]
    report = build_trade_copyability_snapshot_evidence_bridge(
        db, provider=provider
    )
    after_snaps = db.execute("SELECT COUNT(*) FROM candidate_price_snapshots").fetchone()[0]
    # Bridge READ the existing snapshot; did not write a new one.
    assert after_snaps == before_snaps
    ra = report.row_audits[0]
    assert ra.snapshot_evidence is not None
    assert ra.snapshot_evidence.from_existing_snapshot is True


def test_human_report_contains_required_flags_and_note(tmp_path):
    db = _fresh_conn(tmp_path)
    _seed_source_side(db, [
        {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "buy"}, {"side": "BUY"},
    ])
    report = build_trade_copyability_snapshot_evidence_bridge(db)
    human = report_to_human(report)
    assert "TRADE COPYABILITY SNAPSHOT EVIDENCE BRIDGE — READ ONLY / DRY RUN" in human
    assert "ready_to_wire_to_automation = False" in human
    assert "ready_to_persist_decisions = False" in human
    assert "ready_to_create_candidates = False" in human
    assert ("This report does not persist snapshots, decisions, candidates, paper "
            "signals, or orders.") in human
    assert "INGESTION SIDE NORMALIZATION AUDIT" in human
    assert "PR24T" in human
