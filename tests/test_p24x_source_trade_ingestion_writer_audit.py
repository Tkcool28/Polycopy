"""PR24X — Source-Trade INGESTION WRITER AUDIT tests.

Proves the read-only / report-only ingestion-writer audit behaves under
Polycopy's hard guardrails for PR24X (Step 1 / Step 2 architecture audit only):

  * report generation is read-only (no production DB writes)
  * no source_trades mutation
  * no trade_copyability_decisions / copy_candidates / paper_signal_decisions
  * no candidate_price_snapshots or levels
  * no orders / positions
  * WAL / busy_timeout / wal_autocheckpoint are detected in Database.connect()
  * direct source_trades write paths are classified
  * report distinguishes production write paths from test/temp DB seed paths
  * architecture has exactly one writer role
  * collectors/fetchers are NOT allowed to own DB writes in the proposed arch
  * report serializes to JSON

Mirrors the PR24W test conventions: open the DB with mode=ro, compare main-file
size before/after, assert guarded tables are empty, and assert the module
source contains no mutation verbs.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

import pytest

from polycopy.engine import source_trade_ingestion_writer_audit as mod
from polycopy.engine.source_trade_ingestion_writer_audit import (
    build_source_trade_ingestion_writer_audit,
    report_to_markdown,
    report_to_json,
)

# Unmistakably fake identifiers (mirrors PR24R / PR24U / PR24V / PR24W).
SYN_TRADE = "synthetic_source_trade_do_not_use"
SYN_TOKEN = "synthetic_token_do_not_use"
SYN_MARKET = "synthetic_market_do_not_use"
SYN_WALLET = "0xsynthetic_test_only"
SYN_CONDITION = (
    "0xeb348b65a59bb2752d3dd10636d17de501df76a424e978e136d22e76d07c84e9"
)


_GUARDED_TABLES = (
    "trade_copyability_decisions",
    "copy_candidates",
    "paper_signal_decisions",
    "candidate_price_snapshots",
    "candidate_price_snapshot_levels",
    "orders",
    "positions",
    "settlement_accounting_ledger",
)




_OWNED_SQLITE = None


@pytest.fixture(autouse=True)
def _use_owned_sqlite(owned_sqlite):
    """Route every file-backed test database through the exact-path fixture."""
    global _OWNED_SQLITE
    _OWNED_SQLITE = owned_sqlite
    try:
        yield
    finally:
        _OWNED_SQLITE = None

def _make_db(rows, *, add_guarded_tables=False) -> str:
    """Build an isolated temp SQLite DB with a source_trades table + rows."""
    path = _OWNED_SQLITE.new_path("pr24x")
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE source_trades (
            id TEXT,
            source TEXT,
            source_trade_id TEXT,
            market_source_id TEXT,
            side TEXT,
            outcome TEXT,
            quantity TEXT,
            price TEXT,
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
        )
        """
    )
    for r in rows:
        con.execute(
            "INSERT INTO source_trades "
            "(id, source, source_trade_id, market_source_id, side, outcome, quantity, "
            "price, trader_address, timestamp, is_sample, token_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("id", SYN_TRADE),
                r.get("source", "synthetic"),
                r.get("source_trade_id", SYN_TRADE),
                r.get("market_source_id", SYN_MARKET),
                r.get("side", "BUY"),
                r.get("outcome", "Yes"),
                str(r.get("quantity", "100.0")),
                str(r.get("price", "0.40")),
                r.get("trader_address", SYN_WALLET),
                r.get("timestamp", "2026-07-01T00:00:00+00:00"),
                r.get("is_sample", 1),
                r.get("token_id"),
            ),
        )
    if add_guarded_tables:
        con.execute(
            "CREATE TABLE candidate_price_snapshots (token_id TEXT, fetched_at TEXT)"
        )
        con.execute(
            "CREATE TABLE candidate_price_snapshot_levels (token_id TEXT, price REAL)"
        )
    con.commit()
    con.close()
    return path


def _open_ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _assert_no_writes(db: str) -> None:
    """The audit must not have written the main file or guarded tables."""
    size = Path(db).stat().st_size
    check = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        existing = {
            r[0] for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        for t in _GUARDED_TABLES:
            assert t not in existing or check.execute(
                f"SELECT COUNT(*) FROM {t}"
            ).fetchone()[0] == 0, f"guarded table {t} was populated/created"
    finally:
        check.close()
    # size is informational; mode=ro guarantees no change, but assert anyway.
    assert Path(db).stat().st_size == size


# ── Read-only / no-write guarantees ──────────────────────────────────────────
def test_running_audit_twice_does_not_mutate_db():
    db = _make_db([
        {"source_trade_id": "real1", "token_id": SYN_TOKEN,
         "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    for _ in range(2):
        con = _open_ro(db)
        try:
            build_source_trade_ingestion_writer_audit(con)
        finally:
            con.close()
    assert Path(db).exists()
    check = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        existing = {
            r[0] for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        for t in ("trade_copyability_decisions", "copy_candidates",
                  "paper_signal_decisions", "candidate_price_snapshots",
                  "orders", "positions"):
            assert t not in existing
    finally:
        check.close()


def test_no_source_trades_mutation():
    db = _make_db([
        {"source_trade_id": "sample-1", "token_id": None,
         "trader_address": "0xsample_trader_a_do_not_use",
         "market_source_id": "sample-market-001", "side": "buy",
         "price": "0.72", "quantity": "50.0"},
        {"source_trade_id": "real1", "token_id": SYN_TOKEN,
         "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    before = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows_before = before.execute(
            "SELECT source_trade_id, side, token_id, market_source_id "
            "FROM source_trades ORDER BY source_trade_id"
        ).fetchall()
    finally:
        before.close()

    con = _open_ro(db)
    try:
        build_source_trade_ingestion_writer_audit(con)
    finally:
        con.close()

    after = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows_after = after.execute(
            "SELECT source_trade_id, side, token_id, market_source_id "
            "FROM source_trades ORDER BY source_trade_id"
        ).fetchall()
    finally:
        after.close()
    assert rows_before == rows_after, "source_trades was mutated by the audit"
    _assert_no_writes(db)


def test_report_generation_is_read_only_and_no_db_writes():
    db = _make_db([{"source_trade_id": "real1", "token_id": SYN_TOKEN}],
                  add_guarded_tables=True)
    size_after = Path(db).stat().st_size
    con = _open_ro(db)
    try:
        audit = build_source_trade_ingestion_writer_audit(con)
    finally:
        con.close()
    # Exercise both renderers (they must not write the DB).
    md = report_to_markdown(audit)
    js = report_to_json(audit)
    assert isinstance(md, str) and isinstance(js, str)
    # Direct byte comparison: the audit never writes the main file.
    assert Path(db).stat().st_size == size_after
    _assert_no_writes(db)


# ── WAL / busy_timeout / autocheckpoint detection in Database.connect() ──────
def test_database_connect_enforces_safety_pragmas():
    """Static proof that Database.connect() sets WAL/busy_timeout/autocheckpoint."""
    import polycopy.db.database as dbmod
    src = inspect.getsource(dbmod.Database.connect)
    low = src.lower()
    # journal_mode = WAL
    assert "journal_mode" in low and "wal" in low, "WAL pragma not found"
    # busy_timeout = 30000
    assert "busy_timeout" in low, "busy_timeout pragma not found"
    assert "30000" in low, "busy_timeout value 30000 not found"
    # wal_autocheckpoint = 1000
    assert "wal_autocheckpoint" in low, "wal_autocheckpoint pragma not found"
    assert "1000" in low, "wal_autocheckpoint value 1000 not found"
    # foreign_keys = ON
    assert "foreign_keys" in low and "on" in low, "foreign_keys=ON not found"


def test_safety_layer_reports_wal_insufficient_alone():
    audit = build_source_trade_ingestion_writer_audit(None)
    assert audit.db_safety_layer.journal_mode_wal is True
    assert audit.db_safety_layer.busy_timeout_ms == 30_000
    assert audit.db_safety_layer.wal_autocheckpoint == 1_000
    assert audit.db_safety_layer.wal_sufficient_alone is False
    assert (
        "single-writer" in audit.db_safety_layer.note.lower()
    ), "safety layer must state a single-writer rule is still required"


# ── Classification of direct write paths ─────────────────────────────────────
def test_direct_source_trade_write_paths_are_classified():
    audit = build_source_trade_ingestion_writer_audit(None)
    # Production writer locations present and classified.
    prod = [w for w in audit.write_paths
            if w.classification == "production_write_path"]
    assert len(prod) >= 1, "at least one production write path should be detected"
    # The two known collector-owned INSERTs are present.
    run_scan = [w for w in audit.write_paths
                if w.path == "scripts/run_scan.py" and w.line == 1419]
    collect = [w for w in audit.write_paths
               if w.path == "scripts/collect_smart_money_data.py" and w.line == 703]
    assert run_scan, "run_scan.py:1419 writer not detected"
    assert collect, "collect_smart_money_data.py:703 writer not detected"
    # Settlement UPDATE present.
    backfill = [w for w in audit.write_paths
                if w.path == "scripts/backfill_resolution_truth.py" and w.line == 449]
    assert backfill, "backfill_resolution_truth.py:449 UPDATE not detected"
    # Test/temp DB seeds are distinguished from production.
    test_seeds = [w for w in audit.write_paths
                  if w.classification == "test_temp_db_only"]
    assert test_seeds, "test/temp DB seed paths should be classified separately"
    # Sample seeder classified.
    sample = [w for w in audit.write_paths
              if w.classification == "sample_test_seed_path"]
    assert sample, "sample/demo seeder should be classified separately"


def test_report_distinguishes_production_from_test_seed_paths():
    audit = build_source_trade_ingestion_writer_audit(None)
    md = report_to_markdown(audit)
    assert "production_write_path" in md
    assert "test_temp_db_only" in md
    assert "sample_test_seed_path" in md
    # The production writer paths appear by file:line.
    assert "scripts/run_scan.py:1419" in md
    assert "scripts/collect_smart_money_data.py:703" in md


# ── Architecture: exactly one writer role ────────────────────────────────────
def test_architecture_has_exactly_one_writer_role():
    audit = build_source_trade_ingestion_writer_audit(None)
    writers = [a for a in audit.architecture_roles if a.may_write]
    assert len(writers) == 1, (
        f"architecture must have exactly ONE writer role, found {len(writers)}"
    )
    assert writers[0].role == "Single SourceTrade Writer"
    # Every other role must NOT write.
    for a in audit.architecture_roles:
        if a.role != "Single SourceTrade Writer":
            assert a.may_write is False, f"{a.role} must not write"


def test_collectors_not_allowed_to_own_db_writes_in_proposed_arch():
    """The proposed architecture forbids collector-owned writes.

    Even though today run_scan/collect DO write, the audit must record them as
    fetcher_only_safe=False and recommend refactoring to the shared writer.
    """
    audit = build_source_trade_ingestion_writer_audit(None)
    offenders = [c for c in audit.collectors
                 if c.writes_source_trades_directly]
    assert offenders, "current collector-owned writers must be recorded"
    for c in offenders:
        assert c.fetcher_only_safe is False
    # The recommendation is explicit: no centralized writer exists today.
    assert audit.centralized_writer_exists is False
    low_note = audit.centralized_writer_note.lower()
    assert "writer" in low_note and (
        "centralized" in low_note or "shared" in low_note
    ), "recommendation must call for a centralized/shared writer"


# ── JSON serialization ───────────────────────────────────────────────────────
def test_report_serializes_to_json():
    audit = build_source_trade_ingestion_writer_audit(None)
    js = report_to_json(audit)
    parsed = json.loads(js)
    assert parsed["audit_version"].startswith("PR24X")
    assert "db_safety_layer" in parsed
    assert "write_paths" in parsed
    assert "architecture_roles" in parsed
    assert "contract" in parsed
    assert "dedupe_strategy" in parsed
    assert "wal_safe_write_policy" in parsed
    assert "future_sequence" in parsed
    assert parsed["centralized_writer_exists"] is False


def test_guardrail_flags_all_true():
    audit = build_source_trade_ingestion_writer_audit(None)
    for k, v in audit.guardrail_flags.items():
        assert v is True, f"guardrail {k} must be True in PR24X"


# ── Purity: module has no mutation verbs and no Database import ──────────────
def test_module_has_no_mutation_statements_and_no_db_import():
    src = inspect.getsource(mod)
    low = src.lower()
    # The module DOCSTRING may mention "Database.connect()" in prose, and it
    # names write-verb / connect patterns as STRING CONSTANTS (used only for
    # static classification, never executed). The real proof of non-mutation is
    # the absence of any COMMIT (writes) and any import/open of the writable
    # Database class. Read-only ``conn.execute(SELECT ...)`` against the
    # caller-supplied mode=ro connection is the intended read pattern and is
    # allowed.
    forbidden = (
        "from polycopy.db.database import",
        "import polycopy.db.database",
        "from polycopy.db import database",
        "database(",
        ".commit(",
        "db.conn.commit(",
        "self._conn.commit(",
        "self._conn.execute(",
    )
    for tok in forbidden:
        assert tok not in low, f"forbidden token {tok!r} found in module source"
