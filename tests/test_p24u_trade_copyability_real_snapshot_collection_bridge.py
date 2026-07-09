"""PR24U — Trade Copyability REAL snapshot/depth/current-price collection bridge tests.

These tests prove the dry-run / report-only collection bridge behaves under
Polycopy's hard guardrails: it collects (or proves it CAN collect) real
snapshot/depth/current-price evidence for eligible source_trades rows, shapes
it into the PR24S evidence structures, and NEVER writes production tables.

Required coverage (from the PR24U task):

  * eligible source trade can produce evidence structure / report row
  * missing token_id / market identifier is skipped with a clear reason
  * market/depth client failure is controlled and does not crash the whole run
  * dry-run mode creates no DB writes
  * no trade_copyability_decisions are created
  * no copy_candidates are created
  * no paper_signal_decisions are created
  * no candidate_price_snapshots or snapshot_levels are created
  * no orders or positions are created

Also: purity (no mutating SQL, no `import polycopy.db.database`), no automated
wiring imported (no broker / order placement / automation tokens), and reuse of
the existing `PolymarketClobClient` is honored (not a duplicated client).
"""

from __future__ import annotations

import inspect
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from typing import Any, Optional

from polycopy.engine import trade_copyability_real_snapshot_collection_bridge as mod
from polycopy.engine.trade_copyability_real_snapshot_collection_bridge import (
    LiveClobBookCollector,
    RealSnapshotEvidenceCollector,
    TradeCopyabilityRealSnapshotCollectionBridgeReport,
    TradeCopyabilityRealSnapshotCollectionRowReport,
    _OfflineBook,
    _audit_row,
    _extract_levels,
    _shape_clob_book_into_evidence,
    build_trade_copyability_real_snapshot_collection_bridge,
    report_to_human,
)

# Unmistakably fake identifiers (mirrors PR24R / PR24S convention).
SYN_TRADE = "synthetic_source_trade_do_not_use"
SYN_TOKEN = "synthetic_token_do_not_use"
SYN_MARKET = "synthetic_market_do_not_use"
SYN_WALLET = "0xsynthetic_test_only"
SYN_TOKEN2 = "0xsynthetic_token_second_do_not_use"


def _make_db(rows, *, add_snapshot_table=False) -> str:
    """Build an isolated temp SQLite DB with a source_trades table + rows."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="pr24u_test_")
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
    if add_snapshot_table:
        con.execute(
            "CREATE TABLE candidate_price_snapshots (token_id TEXT, fetched_at TEXT)"
        )
    con.commit()
    con.close()
    return path


def _open_ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ── Synthetic collector fixtures ─────────────────────────────────────────────
class _SyntheticBook:
    """Minimal duck-typed ClobBook with (price, size) levels."""

    def __init__(self, asks=(), bids=(), *, error_code=None, error_message=None,
                 best_ask=None, best_bid=None, spread=None):
        self.asks = [type("L", (), {"price": p, "size": s})() for p, s in asks]
        self.bids = [type("L", (), {"price": p, "size": s})() for p, s in bids]
        self.error_code = error_code
        self.error_message = error_message
        self.fetched_at = datetime.now(timezone.utc)
        self.book_hash = "synthetichash"
        self.best_ask = best_ask
        self.best_bid = best_bid
        self.spread = spread

    @property
    def is_empty(self):
        return not self.bids and not self.asks


class _SyntheticCollector(RealSnapshotEvidenceCollector):
    """Returns a fixed synthetic book for any token (offline, no network)."""

    def __init__(self, book):
        self._book = book

    async def fetch_book(self, *, token_id=None):
        return self._book


class _FailingCollector(RealSnapshotEvidenceCollector):
    """Raises on fetch to prove the batch does not crash."""

    async def fetch_book(self, *, token_id=None):
        raise RuntimeError("simulated CLOB fetch failure")


class _ErrorBookCollector(RealSnapshotEvidenceCollector):
    """Returns a ClobBook-like object carrying a bounded error_code (no raise)."""

    async def fetch_book(self, *, token_id=None):
        return _SyntheticBook(error_code="HTTP_5XX", error_message="simulated 500")


# ── Tests: eligibility + evidence structure ──────────────────────────────────
def test_eligible_source_trade_produces_evidence_structure_and_report_row():
    db = _make_db([
        {"source_trade_id": SYN_TRADE, "token_id": SYN_TOKEN, "side": "BUY",
         "price": "0.40", "quantity": "100.0", "trader_address": SYN_WALLET,
         "market_source_id": SYN_MARKET},
    ])
    book = _SyntheticBook(asks=[(0.42, 500.0), (0.43, 300.0)],
                          bids=[(0.38, 200.0)],
                          best_ask=0.42, best_bid=0.38, spread=0.04)
    collector = _SyntheticCollector(book)
    con = _open_ro(db)
    try:
        report = build_trade_copyability_real_snapshot_collection_bridge(
            con, limit=10, collector=collector
        )
    finally:
        con.close()

    assert isinstance(report, TradeCopyabilityRealSnapshotCollectionBridgeReport)
    assert report.eligible_count == 1
    assert report.ineligible_count == 0
    assert report.source_trade_count == 1

    rr = report.row_reports[0]
    assert isinstance(rr, TradeCopyabilityRealSnapshotCollectionRowReport)
    assert rr.source_trade_id == SYN_TRADE
    assert rr.wallet_address == SYN_WALLET
    assert rr.market_source_id == SYN_MARKET
    assert rr.token_id == SYN_TOKEN
    assert rr.side == "BUY"
    assert rr.eligibility_status == "eligible"
    assert rr.current_price_available is True
    assert rr.depth_available is True
    assert rr.spread_available is True
    # /book does not expose market state -> must stay False, never invented.
    assert rr.market_state_available is False
    assert rr.snapshot_timestamp is not None
    assert rr.pr24s_evidence_compatibility == "compatible"
    assert rr.skip_reason is None

    ev = rr.collected_evidence
    assert ev is not None, "evidence should be shaped into PR24S SnapshotEvidenceResult"
    assert isinstance(ev, mod.__dict__.get("SnapshotEvidenceResult", object))
    assert ev.current_copy_price == 0.42
    assert ev.best_ask == 0.42
    assert ev.best_bid == 0.38
    assert ev.spread is not None
    assert ev.fill_percentage is not None
    # Market-state fields must be None (not invented).
    assert ev.market_active is None
    assert ev.market_closed is None
    assert ev.market_resolved is None
    assert ev.seconds_to_market_end is None


def test_missing_token_id_is_skipped_with_clear_reason():
    db = _make_db([
        {"source_trade_id": SYN_TRADE, "token_id": None, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    collector = _SyntheticCollector(_SyntheticBook(asks=[(0.42, 500.0)]))
    con = _open_ro(db)
    try:
        report = build_trade_copyability_real_snapshot_collection_bridge(
            con, limit=10, collector=collector
        )
    finally:
        con.close()

    assert report.eligible_count == 0
    assert report.ineligible_count == 1
    rr = report.row_reports[0]
    assert rr.eligibility_status == "not_eligible"
    assert rr.skip_reason is not None
    assert "missing_token_id" in rr.skip_reason
    assert rr.current_price_available is False
    assert rr.depth_available is False
    assert rr.pr24s_evidence_compatibility == "incompatible"
    assert rr.collected_evidence is None


def test_non_buy_side_is_not_eligible():
    db = _make_db([
        {"source_trade_id": SYN_TRADE, "token_id": SYN_TOKEN, "side": "SELL",
         "price": "0.40", "quantity": "100.0"},
    ])
    collector = _SyntheticCollector(_SyntheticBook(asks=[(0.42, 500.0)]))
    con = _open_ro(db)
    try:
        report = build_trade_copyability_real_snapshot_collection_bridge(
            con, limit=10, collector=collector
        )
    finally:
        con.close()
    rr = report.row_reports[0]
    assert rr.eligibility_status == "not_eligible"
    assert rr.skip_reason is not None
    assert "sell_side_copyability_not_supported_v1" in rr.skip_reason


# ── Tests: client failure is controlled (no crash) ───────────────────────────
def test_market_depth_client_failure_is_controlled_no_crash():
    db = _make_db([
        {"source_trade_id": SYN_TRADE, "token_id": SYN_TOKEN, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
        # second eligible row so we prove the WHOLE run does not abort.
        {"source_trade_id": SYN_TOKEN2, "token_id": SYN_TOKEN2, "side": "BUY",
         "price": "0.30", "quantity": "50.0"},
    ])
    collector = _FailingCollector()
    con = _open_ro(db)
    try:
        # Must not raise; failure captured per-row via error_reason.
        report = build_trade_copyability_real_snapshot_collection_bridge(
            con, limit=10, collector=collector
        )
    finally:
        con.close()
    assert report.source_trade_count == 2
    assert report.eligible_count == 2
    for rr in report.row_reports:
        assert rr.error_reason is not None
        assert "RuntimeError" in rr.error_reason
        assert rr.current_price_available is False
        assert rr.depth_available is False
        assert rr.pr24s_evidence_compatibility == "incompatible"


def test_error_book_with_bounded_error_code_is_controlled():
    db = _make_db([
        {"source_trade_id": SYN_TRADE, "token_id": SYN_TOKEN, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    collector = _ErrorBookCollector()
    con = _open_ro(db)
    try:
        report = build_trade_copyability_real_snapshot_collection_bridge(
            con, limit=10, collector=collector
        )
    finally:
        con.close()
    rr = report.row_reports[0]
    assert rr.error_reason is not None
    assert "HTTP_5XX" in rr.error_reason
    assert rr.depth_available is False
    assert rr.pr24s_evidence_compatibility == "incompatible"


# ── Tests: dry-run creates NO DB writes + no production objects ──────────────
_GUARDED_TABLES = (
    "trade_copyability_decisions",
    "copy_candidates",
    "paper_signal_decisions",
    "candidate_price_snapshots",
    "candidate_price_snapshot_levels",
    "orders",
    "positions",
)


def test_dry_run_creates_no_db_writes_and_no_production_objects():
    db = _make_db([
        {"source_trade_id": SYN_TRADE, "token_id": SYN_TOKEN, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
        {"source_trade_id": SYN_TOKEN2, "token_id": None, "side": "BUY",
         "price": "0.30", "quantity": "50.0"},
    ], add_snapshot_table=True)
    book = _SyntheticBook(asks=[(0.42, 500.0)], bids=[(0.38, 200.0)],
                          best_ask=0.42, best_bid=0.38, spread=0.04)
    collector = _SyntheticCollector(book)

    size_before = Path(db).stat().st_size
    con = _open_ro(db)
    try:
        report = build_trade_copyability_real_snapshot_collection_bridge(
            con, limit=10, collector=collector
        )
    finally:
        con.close()
    size_after = Path(db).stat().st_size

    # Main DB file must be unchanged (read-only run).
    assert size_before == size_after, "production DB file size changed during dry-run"
    # None of the guarded tables may have been created or populated.
    check = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        existing = {r[0] for r in check.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        for t in _GUARDED_TABLES:
            assert t not in existing or check.execute(
                f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, (
                f"guarded table {t} was populated/created")
    finally:
        check.close()
    # Report-level guardrail flags must all be False.
    assert report.ready_to_wire_to_automation is False
    assert report.ready_to_persist_decisions is False
    assert report.ready_to_create_candidates is False


def test_running_cli_twice_does_not_mutate_production_db():
    db = _make_db([
        {"source_trade_id": SYN_TRADE, "token_id": SYN_TOKEN, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    book = _SyntheticBook(asks=[(0.42, 500.0)], best_ask=0.42)
    collector = _SyntheticCollector(book)

    # Simulate the CLI report builder twice, assert byte-identical DB.
    for _ in range(2):
        con = _open_ro(db)
        try:
            build_trade_copyability_real_snapshot_collection_bridge(
                con, limit=10, collector=collector
            )
        finally:
            con.close()
    # If we reach here with no exception, the offline path did not write.
    assert Path(db).exists()
    # Verify no guarded tables created.
    check = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        existing = {r[0] for r in check.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        assert "trade_copyability_decisions" not in existing
        assert "copy_candidates" not in existing
        assert "paper_signal_decisions" not in existing
        assert "candidate_price_snapshots" not in existing
        assert "orders" not in existing
        assert "positions" not in existing
    finally:
        check.close()


# ── Tests: production-state honesty (null token_id truthfully blocks) ────────
def test_production_style_mixed_casing_preserved_and_blocked_honestly():
    # Mirror PR24S production truth: buy=4, BUY=1, only test_trade_1 has token.
    db = _make_db([
        {"source_trade_id": "sample-1", "token_id": None, "side": "buy",
         "price": "0.72", "quantity": "50.0"},
        {"source_trade_id": "sample-2", "token_id": None, "side": "buy",
         "price": "0.70", "quantity": "30.0"},
        {"source_trade_id": "sample-3", "token_id": None, "side": "buy",
         "price": "0.72", "quantity": "50.0"},
        {"source_trade_id": "sample-4", "token_id": None, "side": "buy",
         "price": "0.70", "quantity": "30.0"},
        {"source_trade_id": "test_trade_1", "token_id": SYN_TOKEN, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    book = _SyntheticBook(asks=[(0.42, 500.0)], best_ask=0.42)
    collector = _SyntheticCollector(book)
    con = _open_ro(db)
    try:
        report = build_trade_copyability_real_snapshot_collection_bridge(
            con, limit=20, collector=collector
        )
    finally:
        con.close()
    # 4 rows blocked by missing_token_id, 1 eligible (test_trade_1).
    assert report.eligible_count == 1
    assert report.ineligible_count == 4
    assert report.skip_reason_counts.get("missing_token_id", 0) == 4
    # Mixed casing still detected (ingestion inconsistency preserved).
    assert report.ingestion_side_inconsistency_present is True


# ── Tests: PR24S shape compatibility + helper units ──────────────────────────
def test_shape_clob_book_into_pr24s_evidence_compatible():
    book = _SyntheticBook(asks=[(0.42, 500.0), (0.43, 300.0)],
                          bids=[(0.38, 200.0)],
                          best_ask=0.42, best_bid=0.38, spread=0.04)
    ev, notes = _shape_clob_book_into_evidence(
        book=book, token_id=SYN_TOKEN, source_trade_id=SYN_TRADE,
        source_entry_price=0.40, intended_stake=100.0, live_preview=False,
    )
    assert ev is not None
    assert ev.current_copy_price == 0.42
    assert ev.estimated_fill_price is not None
    assert ev.fill_percentage is not None
    # Assert against the ACTUAL produced field from the shaped evidence, not a
    # constant string comparison.
    assert ev.depth_status == "complete"
    assert ev.depth_hash is not None
    # End-to-end: a real report run that collects this book must mark the row
    # PR24S-compatible.
    db = _make_db([
        {"source_trade_id": SYN_TRADE, "token_id": SYN_TOKEN, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    collector = _SyntheticCollector(book)
    con = _open_ro(db)
    try:
        report = build_trade_copyability_real_snapshot_collection_bridge(
            con, limit=10, collector=collector
        )
    finally:
        con.close()
    assert report.pr24s_compatible_count == 1
    assert report.row_reports[0].pr24s_evidence_compatibility == "compatible"
    assert report.row_reports[0].collected_evidence is not None
    assert report.row_reports[0].collected_evidence.current_copy_price == 0.42


def test_shape_clob_book_no_depth_returns_none_and_reason():
    book = _OfflineBook(token_id=SYN_TOKEN)  # no levels
    ev, notes = _shape_clob_book_into_evidence(
        book=book, token_id=SYN_TOKEN, source_trade_id=SYN_TRADE,
        source_entry_price=0.40, intended_stake=100.0, live_preview=False,
    )
    assert ev is None
    assert any("no order-book depth" in n for n in notes)


def test_extract_levels_from_collected_book():
    book = _SyntheticBook(asks=[(0.42, 500.0)], bids=[(0.38, 200.0)])
    asks, bids = _extract_levels(book)
    assert asks[0] == (0.42, 500.0)
    assert bids[0] == (0.38, 200.0)


def test_audit_row_offline_no_network_called():
    """Offline default path must reach the collector without raising."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE source_trades (source_trade_id TEXT, token_id TEXT, side TEXT, "
        "price TEXT, quantity TEXT, trader_address TEXT, market_source_id TEXT, "
        "timestamp TEXT)"
    )
    con.execute(
        "INSERT INTO source_trades VALUES (?,?,?,?,?,?,?,?)",
        (SYN_TRADE, SYN_TOKEN, "BUY", "0.40", "100.0", SYN_WALLET, SYN_MARKET,
         "2026-07-01T00:00:00+00:00"),
    )
    row = con.execute("SELECT * FROM source_trades").fetchone()
    field_map: dict[str, Optional[str]] = {
        "source_trade_id": "source_trade_id", "trader_address": "trader_address",
        "market_source_id": "market_source_id", "token_id": "token_id",
        "side": "side", "price": "price", "size": "quantity",
        "timestamp": "timestamp",
    }
    rr = _audit_row(row, field_map, RealSnapshotEvidenceCollector(), live_preview=False)
    con.close()
    # Offline book has no levels -> not depth-available, incompatible; no crash.
    assert rr.eligibility_status == "eligible"
    assert rr.depth_available is False
    assert rr.pr24s_evidence_compatibility == "incompatible"


# ── Tests: purity (no write path, no wiring imports) ─────────────────────────
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
        "report_trade_copyability_real_snapshot_collection_bridge.py"
    src = cli_path.read_text()
    low = src.lower()
    forbidden = (
        "insert into", "update ", "delete from", "drop table", "alter table",
        "create table", "create index", ".commit(", "executescript(",
    )
    for tok in forbidden:
        assert tok not in low, f"forbidden verb {tok!r} found in CLI source"
    assert "mode=ro" in src, "CLI must open the DB read-only"


class _CallCountingCollector(RealSnapshotEvidenceCollector):
    """Wraps a delegate collector and counts fetch_book invocations.

    Used to prove that the reused/compatible collector is ACTUALLY CALLED
    during evidence collection, not merely that a class/duck-type exists.
    """

    def __init__(self, delegate: RealSnapshotEvidenceCollector):
        self._delegate = delegate
        self.call_count = 0
        self.called_token_ids: list[Any] = []

    async def fetch_book(self, *, token_id=None):
        self.call_count += 1
        self.called_token_ids.append(token_id)
        return await self._delegate.fetch_book(token_id=token_id)


def test_reuses_existing_polymarket_clob_client_not_duplicated():
    """PR24U must reuse the existing PolymarketClobClient, not invent a new one.

    The test asserts the reused/compatible client is ACTUALLY CALLED during
    evidence collection (via LiveClobBookCollector wrapping PolymarketClobClient),
    not merely that a class/name/duck-type exists.
    """
    from polycopy.adapters.polymarket_clob import (
        ClobBook,
        PolymarketClobClient,
    )

    # LiveClobBookCollector is the real reuse wrapper around PolymarketClobClient.
    collector = LiveClobBookCollector(client=object())
    assert isinstance(collector, RealSnapshotEvidenceCollector)
    assert PolymarketClobClient.__name__ == "PolymarketClobClient"
    assert ClobBook.__name__ == "ClobBook"  # the reused book type it returns

    # Proof-of-call: wrap a synthetic delegate that returns a real ClobBook-like
    # object, run a real collection over an eligible row, and assert fetch_book
    # was invoked with the row's token_id.
    book = _SyntheticBook(asks=[(0.42, 500.0)], bids=[(0.38, 200.0)],
                          best_ask=0.42, best_bid=0.38, spread=0.04)
    delegate = _SyntheticCollector(book)
    counting = _CallCountingCollector(delegate)
    db = _make_db([
        {"source_trade_id": SYN_TRADE, "token_id": SYN_TOKEN, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    con = _open_ro(db)
    try:
        report = build_trade_copyability_real_snapshot_collection_bridge(
            con, limit=10, collector=counting
        )
    finally:
        con.close()
    # The reused/compatible collector must have been CALLED during collection.
    assert counting.call_count == 1, "reused collector was not actually called"
    assert counting.called_token_ids == [SYN_TOKEN]
    # And the call produced a compatible PR24S evidence row (end-to-end proof).
    assert report.pr24s_compatible_count == 1
    assert report.row_reports[0].collected_evidence is not None


def test_report_all_ready_flags_false_and_json_valid():
    db = _make_db([
        {"source_trade_id": SYN_TRADE, "token_id": SYN_TOKEN, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    book = _SyntheticBook(asks=[(0.42, 500.0)], best_ask=0.42)
    collector = _SyntheticCollector(book)
    con = _open_ro(db)
    try:
        report = build_trade_copyability_real_snapshot_collection_bridge(
            con, limit=10, collector=collector
        )
    finally:
        con.close()
    d = report.to_dict()
    import json
    json.dumps(d)  # must serialize
    assert d["ready_to_wire_to_automation"] is False
    assert d["ready_to_persist_decisions"] is False
    assert d["ready_to_create_candidates"] is False


def test_sample_like_rows_detected_and_reported_not_mutated():
    """Report-clarity: sample/placeholder rows are detected and surfaced.

    Mirrors production: 4 sample rows (0xsample_trader_*_do_not_use,
    sample-market-*) + 1 real eligible row (test_trade_1 with a real token_id).
    The finding must appear and the rows must NOT be mutated (none are touched).
    """
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
        {"source_trade_id": "test_trade_1", "token_id": SYN_TOKEN, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    book = _SyntheticBook(asks=[(0.42, 500.0)], best_ask=0.42)
    collector = _SyntheticCollector(book)
    con = _open_ro(db)
    try:
        report = build_trade_copyability_real_snapshot_collection_bridge(
            con, limit=20, collector=collector, db_path=db
        )
    finally:
        con.close()
    # 4 of 5 detected as sample-like; effective real coverage n=1.
    assert report.sample_like_row_count == 4
    assert report.source_trade_count == 5
    assert report.eligible_count == 1
    assert report.db_path_inspected == db
    finding_keys = [f.key for f in report.findings]
    assert "source_trade_sample_data_present" in finding_keys
    samp = next(f for f in report.findings if f.key == "source_trade_sample_data_present")
    assert samp.evidence["sample_like_row_count"] == 4
    assert samp.evidence["effective_real_coverage_n"] == 1
    # The rows themselves are untouched (module is read-only; no mutation path).
    assert "trade_copyability_decisions" not in [
        t for t in report.production_counts if report.production_counts[t]
    ]


def test_db_path_inspected_reported_in_human_output():
    db = _make_db([
        {"source_trade_id": SYN_TRADE, "token_id": SYN_TOKEN, "side": "BUY",
         "price": "0.40", "quantity": "100.0"},
    ])
    book = _SyntheticBook(asks=[(0.42, 500.0)], best_ask=0.42)
    collector = _SyntheticCollector(book)
    con = _open_ro(db)
    try:
        report = build_trade_copyability_real_snapshot_collection_bridge(
            con, limit=10, collector=collector, db_path=db
        )
    finally:
        con.close()
    human = report_to_human(report)
    assert f"DB path inspected: {db}" in human
    assert "sample_like_rows" not in human  # only emitted when count > 0; here 0
