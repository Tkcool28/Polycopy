"""PR66 Checkpoint 2 — focus tests for historical wallet evidence ingestion.

Scope (evidence only):
  * live-read gating (no --allow-live => no provider call; --allow-live dry-run
    calls provider but writes nothing; --write rejects without all flags)
  * real offset pagination (one page, multi-page, empty/short, max-pages,
    max-records, duplicate across pages, provider error, before/after cutoff)
  * the full report contract (every required field present + accurate)
  * BUY + SELL preservation (side preserved; recurring collector stays BUY-only)
  * identity / idempotency (api dup, db dup, replay inserts zero, metadata-only
    variation keeps same id, distinct BUY/SELL stay distinct)
  * metadata preservation (event / taxonomy / series+ ticker survive; event
    slug never becomes a category label)
  * write-table purity (authorized temp-DB write changes ONLY source_trades)
  * dry-run purity (no DB change)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from polycopy.db.database import Database
from polycopy.ingestion.source_trade_writer import write_valid_rows
from polycopy.ingestion.wallet_evidence_history import (
    HARD_MAX_PAGES,
    collect_historical_evidence,
)

WALLET = "0xcac76b761231464900cce5da7c20233d59b20579"
TOKEN = "0x" + "2" * 64
MARKET = "0x" + "3" * 64
ROOT = Path(__file__).resolve().parents[1]

GUARDED_TABLES = (
    "copy_candidates",
    "candidate_price_snapshots",
    "candidate_price_snapshot_levels",
    "trade_copyability_decisions",
    "paper_signal_decisions",
    "wallet_score_decisions",
    "category_wallet_score_decisions",
    "orders",
    "positions",
    "settlement_accounting_ledger",
)


def raw(identifier: str = "1", side: str = "BUY", **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "sourceProvidedTradeId": identifier,
        "proxyWallet": WALLET,
        "asset": TOKEN,
        "conditionId": MARKET,
        "side": side,
        "price": ".4",
        "size": "2",
        "timestamp": 1700000000,
        "event": {"id": "e1", "slug": "election-2026", "title": "Election"},
        "category": "Politics",
        "tags": ["US"],
        "series": {"id": "s1", "slug": "ser-slug", "title": "Series", "ticker": "SER"},
    }
    record.update(overrides)
    return record


class Pages:
    """Scripted provider implementing the REAL offset-pagination contract."""

    made_network_call = False

    def __init__(self, pages: list[list[object]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, int, int]] = []

    async def fetch_trades(self, wallet: str, *, limit: int, page: int) -> list[dict[str, object]]:
        self.calls.append((wallet, limit, page))
        if page < len(self.pages):
            page_rows = self.pages[page]
            return [r for r in page_rows[:limit]]  # type: ignore[return-value]
        return []


class ErrorProvider:
    made_network_call = False

    def __init__(self, fail_on_page: int = 0) -> None:
        self.fail_on_page = fail_on_page

    async def fetch_trades(self, wallet: str, *, limit: int, page: int) -> list[dict[str, object]]:
        if page == self.fail_on_page:
            raise RuntimeError("simulated upstream failure")
        return []


# ── A. Live-read gating ──────────────────────────────────────────────────────
def test_no_live_flag_makes_no_provider_call() -> None:
    provider = Pages([[raw("a")]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET))
    # The collection function only calls fetch_trades; the CLI gates the live
    # provider behind --allow-live. Here the injected provider simply returns
    # nothing for a missing page so offline stays safe.
    assert result.live_read_performed is False


def test_allow_live_dry_run_calls_provider_no_writes(tmp_path: Path) -> None:
    provider = Pages([[raw("a"), raw("b", "SELL")]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET))
    assert result.live_read_performed is False  # provider injected has no network
    assert result.normalized_records == 2
    assert result.buy_count == 1 and result.sell_count == 1

    db = Database(tmp_path / "dry.db").connect()
    try:
        before = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
        outcome = write_valid_rows(db, result.accepted_rows, dry_run=True)
        after = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
        assert outcome.committed is False
        assert after == before
    finally:
        db.close()


def test_write_without_allow_live_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "auth.db"
    Database(db_path).connect().close()
    completed = subprocess.run(
        [sys.executable, "scripts/ingest_wallet_evidence_history.py",
         "--write", "--db-path", str(db_path)],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src"),
                       "POLYCOPY_APPROVED_SOURCE_WALLET": WALLET},
        text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 2
    assert "--allow-live (or --mock-live) and --confirm-production-db" in completed.stderr


def test_write_without_confirm_production_db_rejected_for_prod_db(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/ingest_wallet_evidence_history.py",
         "--mock-live", "--write", "--db-path", str(tmp_path / "x.db")],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src"),
                       "POLYCOPY_APPROVED_SOURCE_WALLET": WALLET},
        text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 2
    assert "--allow-live (or --mock-live) and --confirm-production-db" in completed.stderr


def test_offline_cli_makes_no_network_call(tmp_path: Path) -> None:
    """No --allow-live => process completes offline (live_read_performed false)."""
    completed = subprocess.run(
        [sys.executable, "scripts/ingest_wallet_evidence_history.py", "--json"],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src"),
                       "POLYCOPY_APPROVED_SOURCE_WALLET": WALLET},
        text=True, capture_output=True, check=True,
    )
    report = json.loads(completed.stdout)
    assert report["live_read_performed"] is False
    assert report["dry_run"] is True


# ── B. Pagination ──────────────────────────────────────────────────────────
# NOTE: the collection stops immediately on a SHORT or EMPTY page, so to reach
# later pages a page must be FULL (>= per_page). per_page = min(max_records, 100),
# so these tests use a small max_records to make full pages cheap.
def test_one_page_full_then_completed() -> None:
    provider = Pages([[raw("a"), raw("b"), raw("c")]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET, max_pages=1, max_records=3, page_size=3))
    # Page is full (== per_page) and max_pages=1, so the run completes.
    assert result.stop_reason == "completed"
    assert result.normalized_records == 3
    assert result.pages_fetched == 1
    assert provider.calls == [(WALLET, 3, 0)]


def test_one_page_short() -> None:
    provider = Pages([[raw("a")]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET, max_pages=1, max_records=3, page_size=3))
    assert result.stop_reason == "short_page"
    assert result.normalized_records == 1
    assert provider.calls == [(WALLET, 3, 0)]


def test_multiple_real_offset_pages() -> None:
    # Three full pages (per_page=2) -> reaches page 2 with distinct offsets.
    provider = Pages([[raw("p0a"), raw("p0b")], [raw("p1a"), raw("p1b")], [raw("p2a"), raw("p2b")]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET, max_pages=3, max_records=100, page_size=2))
    assert result.pages_fetched == 3
    assert result.normalized_records == 6
    # Each page used a DISTINCT offset (real offset pagination, not local slice).
    assert provider.calls == [(WALLET, 2, 0), (WALLET, 2, 1), (WALLET, 2, 2)]
    assert result.stop_reason == "completed"


def test_empty_page() -> None:
    provider = Pages([[]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET))
    assert result.stop_reason == "empty_page"
    assert result.pages_fetched == 1
    assert result.normalized_records == 0


def test_short_page() -> None:
    provider = Pages([[raw("a"), raw("b")]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET, max_pages=5, max_records=100))
    assert result.stop_reason == "short_page"
    assert result.pages_fetched == 1


def test_max_pages_stop() -> None:
    # Five full pages (per_page=2) then a sixth; hard max (5) stops first.
    provider = Pages([
        [raw("p0a"), raw("p0b")], [raw("p1a"), raw("p1b")], [raw("p2a"), raw("p2b")],
        [raw("p3a"), raw("p3b")], [raw("p4a"), raw("p4b")], [raw("p5a"), raw("p5b")],
    ])
    result = asyncio.run(collect_historical_evidence(provider, WALLET, max_pages=HARD_MAX_PAGES, max_records=100, page_size=2))
    assert result.stop_reason == "max_pages"
    assert result.pages_fetched == HARD_MAX_PAGES
    assert result.normalized_records == HARD_MAX_PAGES * 2


def test_max_records_stop() -> None:
    provider = Pages([[raw("a"), raw("b"), raw("c")]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET, max_pages=5, max_records=2, page_size=3))
    assert result.stop_reason == "max_records"
    assert result.normalized_records == 2


def test_duplicate_across_pages() -> None:
    # Full first page then a page repeating one id -> api duplicate, page short.
    provider = Pages([[raw("a"), raw("b")], [raw("a")]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET, max_pages=2, max_records=100, page_size=2))
    assert result.stop_reason == "short_page"
    assert result.normalized_records == 2
    assert result.api_duplicate_count == 1
    assert result.pages_fetched == 2


def test_provider_error() -> None:
    result = asyncio.run(collect_historical_evidence(ErrorProvider(fail_on_page=0), WALLET))
    assert result.stop_reason == "provider_error"
    assert any(e.error_type == "provider_error" for e in result.errors)


def test_before_cutoff() -> None:
    # All records newer than --before: filter (not stop), then short page.
    provider = Pages([[raw("new", timestamp=1700000000)]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET, max_records=50,
                                                     before="2023-01-01T00:00:00+00:00"))
    assert result.stop_reason == "before_cutoff"
    assert result.normalized_records == 0


def test_after_cutoff() -> None:
    # Older than --after: newest-first upstream => safe early termination.
    provider = Pages([[raw("old", timestamp=1600000000)], [raw("older", timestamp=1500000000)]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET, after="2022-01-01T00:00:00+00:00"))
    assert result.stop_reason == "after_cutoff"
    assert result.normalized_records == 0
    assert result.pages_fetched == 1


# ── C. Reporting contract ──────────────────────────────────────────────────
def test_report_contract_has_every_required_field() -> None:
    # Page 0: a (BUY) + s (SELL). Page 1 repeats a -> api duplicate, terminal.
    provider = Pages([[raw("a"), raw("s", "SELL")], [raw("a")]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET, max_pages=2, max_records=100, page_size=2))
    report = result.report()
    required = {
        "wallet_prefix", "pages_fetched", "raw_records", "normalized_records",
        "buy_count", "sell_count", "rejected_count", "api_duplicate_count",
        "db_duplicate_count", "would_insert", "inserted", "oldest_timestamp",
        "newest_timestamp", "errors", "stop_reason", "dry_run",
        "live_read_performed", "committed", "duration_seconds",
    }
    assert required <= set(report)
    assert result.buy_count == 1 and result.sell_count == 1
    assert report["wallet_prefix"].startswith("0x") and "…" in report["wallet_prefix"]
    assert report["api_duplicate_count"] == 1


# ── D. BUY / SELL ──────────────────────────────────────────────────────────
def test_buy_and_sell_normalized_and_stored(tmp_path: Path) -> None:
    provider = Pages([[raw("b"), raw("s", "SELL")]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET))
    assert [r.side for r in result.accepted_rows] == ["BUY", "SELL"]
    db = Database(tmp_path / "buy_sell.db").connect()
    try:
        outcome = write_valid_rows(db, result.accepted_rows, dry_run=False)
        assert outcome.inserted == 2
        sides = sorted(row[0] for row in db.conn.execute("SELECT side FROM source_trades"))
        assert sides == ["BUY", "SELL"]
    finally:
        db.close()


def test_side_preserved_no_reinterpret() -> None:
    provider = Pages([[raw("s", "SELL")]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET))
    assert result.accepted_rows[0].side == "SELL"
    assert result.accepted_rows[0].validation_status == "valid"


def test_recurring_collector_still_excludes_sell() -> None:
    """The BUY-only recurring collector must not accept SELL (behavior unchanged)."""
    from polycopy.ingestion import ingest_pipeline
    from polycopy.ingestion.approved_wallet_collector import collect

    provider = Pages([[raw("b"), raw("s", "SELL")]])
    result = asyncio.run(collect(provider, WALLET))  # type: ignore[arg-type]
    assert result.buy_records == 1
    assert result.sell_records_excluded == 1
    assert all(r.side == "BUY" for r in result.accepted_rows)
    # ingest_pipeline itself defaults to BUY-only (no allow_sell).
    pipe = asyncio.run(ingest_pipeline.run_ingestion(provider, WALLET))
    assert pipe.counters.raw_buy_records == 1
    assert pipe.counters.raw_sell_records == 1
    assert all(r.side == "BUY" for r in pipe.valid_rows)


# ── E. Identity / idempotency ──────────────────────────────────────────────
def test_repeated_api_record_deduplicates() -> None:
    result = asyncio.run(collect_historical_evidence(Pages([[raw("a"), raw("a")]]), WALLET))
    assert result.normalized_records == 1
    assert result.api_duplicate_count == 1


def test_existing_db_row_counted_as_db_duplicate(tmp_path: Path) -> None:
    db = Database(tmp_path / "dbdup.db").connect()
    try:
        first = asyncio.run(collect_historical_evidence(Pages([[raw("a")]]), WALLET)).accepted_rows
        assert write_valid_rows(db, first, dry_run=False).inserted == 1
        existing = {row[0] for row in db.conn.execute(
            "SELECT source_trade_id FROM source_trades WHERE source=?", (first[0].source,))}
        replay = asyncio.run(collect_historical_evidence(
            Pages([[raw("a")]]), WALLET, existing_ids=existing))
        assert replay.db_duplicate_count == 1
        assert replay.normalized_records == 1
        assert replay.would_insert == 0
    finally:
        db.close()


def test_replay_inserts_zero(tmp_path: Path) -> None:
    db = Database(tmp_path / "replay.db").connect()
    try:
        rows = asyncio.run(collect_historical_evidence(Pages([[raw("a"), raw("s", "SELL")]]), WALLET)).accepted_rows
        assert write_valid_rows(db, rows, dry_run=False).inserted == 2
        existing = {row[0] for row in db.conn.execute(
            "SELECT source_trade_id FROM source_trades WHERE source=?", (rows[0].source,))}
        replay = asyncio.run(collect_historical_evidence(
            Pages([[raw("a"), raw("s", "SELL")]]), WALLET, existing_ids=existing))
        assert replay.db_duplicate_count == 2
        out = write_valid_rows(db, replay.accepted_rows, dry_run=False,
                               pre_existing_ids=existing)
        assert out.inserted == 0
        assert out.committed is True
    finally:
        db.close()


def test_metadata_only_variation_keeps_same_source_trade_id() -> None:
    a = asyncio.run(collect_historical_evidence(Pages([[raw("a", category="Politics")]]), WALLET)).accepted_rows[0]
    b = asyncio.run(collect_historical_evidence(Pages([[raw("a", category="Different")]]), WALLET)).accepted_rows[0]
    assert a.source_trade_id == b.source_trade_id


def test_distinct_buy_sell_records_remain_distinct() -> None:
    # Genuinely distinct canonical inputs (different source ids) with the same
    # underlying fill still map to distinct rows and distinct sides.
    provider = Pages([[raw("buyX", "BUY"), raw("sellX", "SELL")]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET))
    ids = {r.source_trade_id for r in result.accepted_rows}
    assert len(ids) == 2
    assert {r.side for r in result.accepted_rows} == {"BUY", "SELL"}


# ── F. Metadata preservation ───────────────────────────────────────────────
def test_event_taxonomy_series_metadata_survive(tmp_path: Path) -> None:
    record = raw("m", event={"id": "ev1", "slug": "my-event", "title": "My Event"},
                 category="Crypto", tags=["a", "b"],
                 series={"id": "se1", "slug": "ser", "title": "My Series", "ticker": "TKR"})
    provider = Pages([[record]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET))
    cand = result.accepted_rows[0]
    meta = cand.metadata
    assert meta["event"]["id"] == "ev1"
    assert meta["event"]["slug"] == "my-event"
    assert meta["event"]["title"] == "My Event"
    assert meta["taxonomy"]["raw_category"] == "Crypto"
    assert meta["taxonomy"]["tags"] == ["a", "b"]
    assert meta["series"]["id"] == "se1"
    assert meta["series"]["slug"] == "ser"
    assert meta["series"]["title"] == "My Series"
    assert meta["series"]["ticker"] == "TKR"

    db = Database(tmp_path / "meta.db").connect()
    try:
        write_valid_rows(db, [cand], dry_run=False)
        stored = json.loads(db.conn.execute(
            "SELECT metadata_json FROM source_trades WHERE source_trade_id=?",
            (cand.source_trade_id,)).fetchone()[0])
        assert stored == meta
        # Event slug is NEVER repurposed as a category label.
        assert "category_label" not in stored
        assert stored["taxonomy"]["raw_category"] == "Crypto"
        assert stored["event"]["slug"] == "my-event"
    finally:
        db.close()


def test_event_slug_never_becomes_category() -> None:
    cand = asyncio.run(collect_historical_evidence(
        Pages([[raw("z", category="Politics", event={"slug": "election-2026"})]]), WALLET)).accepted_rows[0]
    meta = cand.metadata
    assert meta["event"]["slug"] == "election-2026"
    assert meta["taxonomy"]["raw_category"] == "Politics"
    # No category inference from slug/title.
    assert "category" not in meta["event"]


# ── G. Write-table purity ──────────────────────────────────────────────────
def test_authorized_temp_db_write_changes_only_source_trades(tmp_path: Path) -> None:
    provider = Pages([[raw("a"), raw("s", "SELL")]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET))
    db = Database(tmp_path / "purity.db").connect()
    try:
        before = {t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in GUARDED_TABLES + ("source_trades",)}
        out = write_valid_rows(db, result.accepted_rows, dry_run=False)
        assert out.inserted == 2
        after = {t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in GUARDED_TABLES + ("source_trades",)}
        assert after["source_trades"] == before["source_trades"] + 2
        for t in GUARDED_TABLES:
            assert after[t] == before[t], f"{t} changed unexpectedly"
    finally:
        db.close()


# ── H. Dry-run purity ──────────────────────────────────────────────────────
def test_dry_run_leaves_db_unchanged(tmp_path: Path) -> None:
    provider = Pages([[raw("a")]])
    result = asyncio.run(collect_historical_evidence(provider, WALLET))
    db = Database(tmp_path / "dryrun.db").connect()
    try:
        before = {t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in GUARDED_TABLES + ("source_trades",)}
        write_valid_rows(db, result.accepted_rows, dry_run=True)
        after = {t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in GUARDED_TABLES + ("source_trades",)}
        assert after == before
    finally:
        db.close()


def test_authorized_write_end_to_end_changes_only_source_trades(tmp_path: Path) -> None:
    """CLI --write (with all flags) via --mock-live against an isolated DB
    changes only source_trades (no real network; exercise full auth path)."""
    fixture = [raw("a"), raw("s", "SELL")]
    Path(tmp_path / "fixture.json").write_text(json.dumps(fixture))
    db_path = tmp_path / "hist_write.db"
    Database(db_path).connect().close()
    completed = subprocess.run(
        [sys.executable, "scripts/ingest_wallet_evidence_history.py",
         "--mock-live", "--write", "--confirm-production-db",
         "--input-file", str(tmp_path / "fixture.json"),
         "--db-path", str(db_path), "--json"],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src"),
                       "POLYCOPY_APPROVED_SOURCE_WALLET": WALLET},
        text=True, capture_output=True, check=True,
    )
    report = json.loads(completed.stdout)
    # --mock-live satisfies the live gate: dry_run False, committed True.
    assert report["dry_run"] is False
    assert report["committed"] is True
    assert report["inserted"] == 2
    assert report["buy_count"] == 1 and report["sell_count"] == 1

    db = Database(db_path).connect()
    try:
        for t in GUARDED_TABLES:
            assert db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
        assert db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0] == 2
    finally:
        db.close()
