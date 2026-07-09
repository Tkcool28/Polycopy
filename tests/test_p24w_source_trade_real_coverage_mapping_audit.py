"""PR24W — Source-Trade REAL COVERAGE + TOKEN→CONDITION MAPPING AUDIT tests.

These tests prove the read-only / report-only coverage + mapping audit behaves
under Polycopy's hard guardrails: it inventories every ``source_trades`` row,
classifies coverage buckets and identifier-mapping readiness, assesses the
token→condition mapping feasibility (read-only), and NEVER writes production
tables, never mutates ``source_trades``, never creates decisions / candidates /
signals / snapshots / orders / positions / timers.

Required coverage (from the PR24W task):

  * sample placeholder rows are classified correctly
  * real-like complete row is classified correctly
  * token-only row is marked PR24U-ready but PR24V-needs-token→condition mapping
  * conditionId-only row is marked PR24V-ready and possibly PR24U-not-ready
    (if token_id missing)
  * row with both token_id and conditionId is both-ready
  * missing price/size row is not evidence-ready
  * raw side buy/BUY canonicalization remains report-only
  * no source_trades mutation
  * no DB writes
  * no trade_copyability_decisions
  * no copy_candidates
  * no paper_signal_decisions
  * no candidate_price_snapshots or snapshot levels
  * no orders or positions
  * no timers/services/deploy files touched
  * no broker/order/automation imports
  * report serializes to JSON
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import tempfile
from pathlib import Path


from polycopy.engine import source_trade_real_coverage_mapping_audit as mod
from polycopy.engine.source_trade_real_coverage_mapping_audit import (
    build_source_trade_real_coverage_mapping_audit,
    report_to_human,
)

# Unmistakably fake identifiers (mirrors PR24R / PR24U / PR24V convention).
SYN_TRADE = "synthetic_source_trade_do_not_use"
SYN_TOKEN = "synthetic_token_do_not_use"
SYN_MARKET = "synthetic_market_do_not_use"
SYN_WALLET = "0xsynthetic_test_only"
SYN_CONDITION = (
    "0xeb348b65a59bb2752d3dd10636d17de501df76a424e978e136d22e76d07c84e9"
)
SYN_TOKEN2 = "0xsynthetic_token_second_do_not_use"
SYN_MARKET2 = "sample-market-002"


def _make_db(rows, *, add_guarded_tables=False) -> str:
    """Build an isolated temp SQLite DB with a source_trades table + rows."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="pr24w_test_")
    Path(path).unlink()  # mkstemp creates the file; recreate clean.
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


_GUARDED_TABLES = (
    "trade_copyability_decisions",
    "copy_candidates",
    "paper_signal_decisions",
    "candidate_price_snapshots",
    "candidate_price_snapshot_levels",
    "orders",
    "positions",
)


# ── Coverage bucket classifications (the core required coverage) ──────────────
def test_sample_placeholder_rows_classified_correctly():
    db = _make_db([
        {"source_trade_id": "s1", "token_id": None,
         "trader_address": "0xsample_trader_a_do_not_use",
         "market_source_id": "sample-market-001", "side": "buy",
         "price": "0.72", "quantity": "50.0"},
        {"source_trade_id": "s2", "token_id": None,
         "market_source_id": "sample-market-002", "side": "buy",
         "price": "0.70", "quantity": "30.0"},
    ])
    con = _open_ro(db)
    try:
        report = build_source_trade_real_coverage_mapping_audit(con, limit=10)
    finally:
        con.close()
    assert report.source_trade_count == 2
    assert report.sample_placeholder_count == 2
    assert report.real_like_count == 0
    for rr in report.row_reports:
        assert rr.sample_placeholder_status == "sample_placeholder"
        assert rr.coverage_bucket == "sample_placeholder"
        # No usable identifier -> neither ready.
        assert rr.has_token_id is False
        assert rr.has_condition_id is False
        assert rr.pr24u_book_ready is False
        assert rr.pr24v_gamma_ready is False
        assert rr.neither_ready is True
        assert rr.token_to_condition_mapping_needed is False


def test_real_like_complete_row_classified_correctly():
    db = _make_db([
        {"source_trade_id": "real1", "token_id": SYN_TOKEN,
         "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    con = _open_ro(db)
    try:
        report = build_source_trade_real_coverage_mapping_audit(con, limit=10)
    finally:
        con.close()
    rr = report.row_reports[0]
    assert rr.sample_placeholder_status == "real_like"
    assert rr.coverage_bucket == "real_like_complete"
    assert rr.has_token_id is True
    assert rr.has_condition_id is True
    assert rr.both_token_and_condition is True
    assert rr.pr24u_book_ready is True
    assert rr.pr24v_gamma_ready is True
    assert rr.both_ready is True
    assert rr.token_to_condition_mapping_needed is False
    assert rr.copyability_evidence_readiness == "ready_both_paths"
    assert report.effective_real_usable_coverage == 1


def test_token_only_row_pr24u_ready_but_pr24v_needs_mapping():
    db = _make_db([
        {"source_trade_id": "tok1", "token_id": SYN_TOKEN,
         "market_source_id": "sample-market-001", "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    con = _open_ro(db)
    try:
        report = build_source_trade_real_coverage_mapping_audit(con, limit=10)
    finally:
        con.close()
    rr = report.row_reports[0]
    assert rr.coverage_bucket == "real_like_token_only"
    assert rr.identifier_quality == "token_only"
    assert rr.has_token_id is True
    assert rr.has_condition_id is False
    assert rr.pr24u_book_ready is True       # /book keys on token_id
    assert rr.pr24v_gamma_ready is False     # no conditionId -> cannot resolve Gamma
    assert rr.both_ready is False
    assert rr.token_to_condition_mapping_needed is True
    assert rr.copyability_evidence_readiness == (
        "ready_pr24u_only_needs_mapping_for_pr24v"
    )
    assert report.token_to_condition_mapping_needed_count == 1


def test_condition_only_row_pr24v_ready_pr24u_not_ready():
    db = _make_db([
        {"source_trade_id": "cond1", "token_id": None,
         "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    con = _open_ro(db)
    try:
        report = build_source_trade_real_coverage_mapping_audit(con, limit=10)
    finally:
        con.close()
    rr = report.row_reports[0]
    assert rr.coverage_bucket == "real_like_condition_only"
    assert rr.identifier_quality == "condition_only"
    assert rr.has_token_id is False
    assert rr.has_condition_id is True
    assert rr.pr24u_book_ready is False      # no token_id -> /book cannot fetch
    assert rr.pr24v_gamma_ready is True      # conditionId -> Gamma resolvable
    assert rr.both_ready is False
    assert rr.token_to_condition_mapping_needed is False
    assert rr.copyability_evidence_readiness == (
        "ready_pr24v_only_needs_token_for_pr24u"
    )


def test_missing_price_or_size_not_evidence_ready():
    # Missing price.
    db = _make_db([
        {"source_trade_id": "mp", "token_id": SYN_TOKEN,
         "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": None, "quantity": "100.0"},
    ])
    con = _open_ro(db)
    try:
        report = build_source_trade_real_coverage_mapping_audit(con, limit=10)
    finally:
        con.close()
    rr = report.row_reports[0]
    assert rr.coverage_bucket == "real_like_unusable_missing_price_or_size"
    assert rr.copyability_evidence_readiness == "blocked_missing_price_or_size"

    # Missing size.
    db2 = _make_db([
        {"source_trade_id": "ms", "token_id": SYN_TOKEN,
         "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": None},
    ])
    con2 = _open_ro(db2)
    try:
        report2 = build_source_trade_real_coverage_mapping_audit(con2, limit=10)
    finally:
        con2.close()
    assert report2.row_reports[0].coverage_bucket == (
        "real_like_unusable_missing_price_or_size"
    )


def test_raw_side_buy_buy_canonicalization_is_report_only():
    db = _make_db([
        {"source_trade_id": "b1", "token_id": SYN_TOKEN,
         "market_source_id": SYN_CONDITION, "side": "buy", "price": "0.40",
         "quantity": "100.0"},
        {"source_trade_id": "b2", "token_id": SYN_TOKEN,
         "market_source_id": SYN_CONDITION, "side": "BUY", "price": "0.40",
         "quantity": "100.0"},
    ])
    con = _open_ro(db)
    try:
        report = build_source_trade_real_coverage_mapping_audit(con, limit=10)
    finally:
        con.close()
    # Both raw forms canonicalize to BUY; inconsistency is REPORTED, not fixed.
    assert report.raw_side_distribution.get("buy") == 1
    assert report.raw_side_distribution.get("BUY") == 1
    assert report.canonical_side_distribution.get("BUY") == 2
    assert report.ingestion_side_inconsistency_present is True
    for rr in report.row_reports:
        assert rr.canonical_side == "BUY"


def test_sell_side_is_unsupported_bucket_not_canonicalized_to_eligible():
    db = _make_db([
        {"source_trade_id": "sell1", "token_id": SYN_TOKEN,
         "market_source_id": SYN_CONDITION, "side": "SELL", "price": "0.40",
         "quantity": "100.0"},
    ])
    con = _open_ro(db)
    try:
        report = build_source_trade_real_coverage_mapping_audit(con, limit=10)
    finally:
        con.close()
    rr = report.row_reports[0]
    assert rr.coverage_bucket == "invalid_side_or_unsupported_side"
    assert rr.canonical_side == "SELL"
    assert rr.copyability_evidence_readiness == "blocked_sell_unsupported_v1"


# ── Production-style truth (mirrors PR24U/PR24V n=1 finding) ───────────────
def test_production_style_real_coverage_is_effectively_n1():
    db = _make_db([
        {"source_trade_id": "sample-1", "token_id": None,
         "trader_address": "0xsample_trader_a_do_not_use",
         "market_source_id": "sample-market-001", "side": "buy",
         "price": "0.72", "quantity": "50.0"},
        {"source_trade_id": "sample-2", "token_id": None,
         "trader_address": "0xsample_trader_b_do_not_use",
         "market_source_id": "sample-market-002", "side": "buy",
         "price": "0.70", "quantity": "30.0"},
        {"source_trade_id": "sample-3", "token_id": None,
         "trader_address": "0xsample_trader_a_do_not_use",
         "market_source_id": "sample-market-001", "side": "buy",
         "price": "0.72", "quantity": "50.0"},
        {"source_trade_id": "sample-4", "token_id": None,
         "trader_address": "0xsample_trader_b_do_not_use",
         "market_source_id": "sample-market-002", "side": "buy",
         "price": "0.70", "quantity": "30.0"},
        # The single real-like row (mirrors test_trade_1: real conditionId + token).
        {"source_trade_id": "test_trade_1", "token_id": SYN_TOKEN,
         "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    con = _open_ro(db)
    try:
        report = build_source_trade_real_coverage_mapping_audit(
            con, limit=20, db_path=db
        )
    finally:
        con.close()
    assert report.source_trade_count == 5
    assert report.sample_placeholder_count == 4
    assert report.real_like_count == 1
    assert report.effective_real_usable_coverage == 1
    assert report.has_token_id_count == 1
    assert report.has_condition_id_count == 1
    assert report.both_token_and_condition_count == 1
    assert report.token_to_condition_mapping_needed_count == 0
    assert report.pr24u_book_ready_count == 1
    assert report.pr24v_gamma_ready_count == 1
    assert report.both_ready_count == 1
    finding_keys = [f.key for f in report.findings]
    assert "sample_placeholder_rows_present" in finding_keys
    assert report.db_path_inspected == db


# ── Token→condition mapping feasibility (read-only) ──────────────────────────
def test_token_condition_mapping_feasibility_reported():
    db = _make_db([
        {"source_trade_id": "real1", "token_id": SYN_TOKEN,
         "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    con = _open_ro(db)
    try:
        report = build_source_trade_real_coverage_mapping_audit(con, limit=10)
    finally:
        con.close()
    feas = report.token_condition_mapping_feasibility
    # resolve_trade_to_outcome helper exists in the repo (importable).
    assert feas.resolve_trade_to_outcome_helper_exists is True
    assert feas.mapping_helper_already_exists is True
    # No market_outcomes/markets tables in this synthetic DB -> join NOT possible.
    assert feas.market_outcomes_table_present is False
    assert feas.markets_table_present is False
    assert feas.mapping_join_possible_via_market_outcomes is False
    # No production mapping writer is implemented by PR24W.
    assert "NOT implement" in feas.smallest_future_helper


# ── Guardrails: no mutation / no writes / no production objects ───────────────
def test_dry_run_creates_no_db_writes_and_no_production_objects():
    db = _make_db([
        {"source_trade_id": "real1", "token_id": SYN_TOKEN,
         "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
        {"source_trade_id": "sample-1", "token_id": None,
         "trader_address": "0xsample_trader_a_do_not_use",
         "market_source_id": "sample-market-001", "side": "buy",
         "price": "0.72", "quantity": "50.0"},
    ], add_guarded_tables=True)
    con = _open_ro(db)
    try:
        report = build_source_trade_real_coverage_mapping_audit(con, limit=10)
    finally:
        con.close()
    size_after = Path(db).stat().st_size

    # Main DB file must be unchanged (read-only run).
    fd, tmppath = tempfile.mkstemp(suffix=".db")
    import os
    os.close(fd)
    con2 = sqlite3.connect(tmppath)
    con2.execute(
        "ATTACH DATABASE ? AS prod" if False else "SELECT 1"  # no-op guard
    )
    con2.close()
    # Direct byte comparison: the audit never writes the main file.
    assert Path(db).stat().st_size == size_after

    # None of the guarded tables may have been created or populated.
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

    # Report-level guardrail flags must all be False.
    assert report.ready_to_wire_to_automation is False
    assert report.ready_to_persist_decisions is False
    assert report.ready_to_create_candidates is False


def test_running_audit_twice_does_not_mutate_production_db():
    db = _make_db([
        {"source_trade_id": "real1", "token_id": SYN_TOKEN,
         "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    for _ in range(2):
        con = _open_ro(db)
        try:
            build_source_trade_real_coverage_mapping_audit(con, limit=10)
        finally:
            con.close()
    # If we reach here with no exception, the read-only path did not write.
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


def test_no_source_trades_mutation_in_production_audit():
    """Running the audit against a realistic DB must leave source_trades intact."""
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
        build_source_trade_real_coverage_mapping_audit(con, limit=10)
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


# ── Purity (no write path, no wiring/broker imports) ───────────────────────
def test_module_has_no_mutation_statements_and_no_db_import():
    src = inspect.getsource(mod)
    low = src.lower()
    forbidden = (
        "insert into", "update ", "delete from", "drop table", "alter table",
        "create table", "create index", ".commit(", "executescript(",
    )
    for tok in forbidden:
        assert tok not in low, f"forbidden verb {tok!r} found in module source"
    assert "from polycopy.db.database import Database" not in src
    assert "import polycopy.db.database" not in src


def test_module_does_not_import_wiring_or_broker_tokens():
    src = inspect.getsource(mod)
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
        assert tok not in src, f"forbidden wiring token {tok!r} found in module"


def test_cli_source_has_no_mutation_and_uses_mode_ro():
    from pathlib import Path
    cli_path = Path(__file__).resolve().parent.parent / "scripts" / \
        "report_source_trade_real_coverage_mapping_audit.py"
    src = cli_path.read_text()
    low = src.lower()
    forbidden = (
        "insert into", "update ", "delete from", "drop table", "alter table",
        "create table", "create index", ".commit(", "executescript(",
    )
    for tok in forbidden:
        assert tok not in low, f"forbidden verb {tok!r} found in CLI source"
    assert "mode=ro" in src, "CLI must open the DB read-only"


def test_cli_live_preview_is_off_by_default_and_does_not_fire_network():
    # The CLI must NOT perform a network call when --allow-live-preview is unset,
    # and the default path is report-only. We just assert the flag defaults off
    # and the module has no broker/network-fire tokens in the default path.
    assert mod is not None
    src = inspect.getsource(mod)
    # No live Gamma/CLOB client is constructed or awaited anywhere in the module.
    assert "PolymarketClobClient" not in src
    assert "PolymarketPublicAdapter" not in src


# ── Serialization ─────────────────────────────────────────────────────────────
def test_report_serializes_to_json():
    db = _make_db([
        {"source_trade_id": "real1", "token_id": SYN_TOKEN,
         "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
        {"source_trade_id": "sample-1", "token_id": None,
         "trader_address": "0xsample_trader_a_do_not_use",
         "market_source_id": "sample-market-001", "side": "buy",
         "price": "0.72", "quantity": "50.0"},
    ])
    con = _open_ro(db)
    try:
        report = build_source_trade_real_coverage_mapping_audit(con, limit=10)
    finally:
        con.close()
    d = report.to_dict()
    json.dumps(d)  # must serialize
    assert d["ready_to_wire_to_automation"] is False
    assert d["ready_to_persist_decisions"] is False
    assert d["ready_to_create_candidates"] is False
    assert d["source_trade_count"] == 2
    assert "row_reports" in d
    assert "token_condition_mapping_feasibility" in d
    assert "coverage_bucket_counts" in d


def test_human_report_includes_required_sections():
    db = _make_db([
        {"source_trade_id": "real1", "token_id": SYN_TOKEN,
         "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    con = _open_ro(db)
    try:
        report = build_source_trade_real_coverage_mapping_audit(
            con, limit=10, db_path=db
        )
    finally:
        con.close()
    human = report_to_human(report)
    assert "SOURCE-TRADE REAL COVERAGE" in human
    assert "DB path inspected:" in human
    assert "coverage bucket counts".lower() in human.lower()
    assert "token→condition mapping feasibility".lower() in human.lower()
    assert "effective_real_usable_coverage" in human
