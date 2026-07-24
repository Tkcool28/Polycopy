"""PR24V — Trade Copyability MARKET-STATE / END-TIME EVIDENCE BRIDGE tests.

These tests prove the dry-run / report-only market-state bridge behaves under
Polycopy's hard guardrails: it resolves market metadata for eligible
source_trades rows (via the reused Gamma client path), reports market state /
end time / seconds_to_market_end honestly, and NEVER writes production tables.

Required coverage (from the PR24V task):

  1. eligible row with resolvable metadata produces market-state evidence/report row
  2. missing market identifier is skipped with a clear reason
  3. metadata client failure is controlled and does not crash the whole run
  4. market state is not invented when unavailable
  5. seconds_to_market_end is computed only when end time is available
  6. negative seconds_to_market_end or closed/resolved markets reported honestly
  7. dry-run creates no DB writes
  8. no trade_copyability_decisions are created
  9. no copy_candidates are created
 10. no paper_signal_decisions are created
 11. no candidate_price_snapshots or snapshot_levels are created
 12. no orders or positions are created
 13. no timers/services/deploy files are touched
 14. no broker/order/automation imports appear

Also: purity (no mutating SQL, no `import polycopy.db.database`), and reuse of
the existing `PolymarketPublicAdapter` (not a duplicated client).
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from typing import Any

from polycopy.engine import trade_copyability_market_state_evidence_bridge as mod
from polycopy.engine.trade_copyability_market_state_evidence_bridge import (
    LiveGammaMarketStateProvider,
    OfflineMarketStateProvider,
    TradeCopyabilityMarketStateBridgeReport,
    _MarketStateFetchResult,
    _normalize_market_state,
    _resolve_lookup_identifier,
    build_trade_copyability_market_state_evidence_bridge,
    report_to_human,
)

# Unmistakably fake identifiers (mirrors PR24R / PR24S / PR24U convention).
SYN_TRADE = "synthetic_source_trade_do_not_use"
SYN_TOKEN = "synthetic_token_do_not_use"
SYN_MARKET = "synthetic_market_do_not_use"  # NOT conditionId-shaped -> unresolvable
SYN_WALLET = "0xsynthetic_test_only"
SYN_CONDITION = "0xabcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"  # 64-hex




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

def _make_db(rows, *, add_extra_tables=False) -> str:
    """Build an isolated temp SQLite DB with a source_trades table + rows."""
    path = _OWNED_SQLITE.new_path("pr24v")
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
                r.get("market_source_id"),
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
    if add_extra_tables:
        # Tables whose absence/emptiness must be proven preserved by dry-run.
        con.execute("CREATE TABLE trade_copyability_decisions (x INTEGER)")
        con.execute("CREATE TABLE copy_candidates (x INTEGER)")
        con.execute("CREATE TABLE paper_signal_decisions (x INTEGER)")
        con.execute("CREATE TABLE candidate_price_snapshots (x INTEGER)")
        con.execute("CREATE TABLE candidate_price_snapshot_levels (x INTEGER)")
        con.execute("CREATE TABLE orders (x INTEGER)")
        con.execute("CREATE TABLE positions (x INTEGER)")
    con.commit()
    con.close()
    return path


def _open_ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ── Synthetic market-state providers ────────────────────────────────────────
class _FakeMarket:
    """Minimal duck-typed Market with the attributes the bridge consumes."""

    def __init__(self, *, active=None, closed=None, resolved=None, end_date=None,
                 source_id=SYN_CONDITION):
        self.active = active
        self.closed = closed
        self.resolved = resolved
        self.end_date = end_date
        self.source_id = source_id
        self.fetched_at = datetime.now(timezone.utc)


class _ResolvingProvider:
    """Returns a synthetic market for any condition_id (records calls)."""

    def __init__(self, market: Any, *, raise_on=None):
        self._market = market
        self.raise_on = raise_on
        self.calls = 0
        self.called_ids: list[str] = []

    async def fetch_market_state(self, *, condition_id: str):
        self.calls += 1
        self.called_ids.append(condition_id)
        if self.raise_on is not None and self.calls >= self.raise_on:
            raise RuntimeError("synthetic metadata client failure")
        return _MarketStateFetchResult(
            condition_id=condition_id,
            fetched=True,
            market=self._market,
            error_code=None,
            error_message=None,
            fetched_at=datetime.now(timezone.utc),
        )


class _NotFoundProvider:
    """Returns MARKET_NOT_FOUND for every id (mirrors Gamma 404)."""

    async def fetch_market_state(self, *, condition_id: str):
        return _MarketStateFetchResult(
            condition_id=condition_id,
            fetched=True,
            market=None,
            error_code="MARKET_NOT_FOUND",
            error_message="Gamma get_market returned None",
            fetched_at=datetime.now(timezone.utc),
        )


# ── Test 1: eligible row with resolvable metadata produces a report row ─────
def test_resolvable_metadata_produces_report_row():
    path = _make_db([
        {
            "source_trade_id": "t_resolvable",
            "market_source_id": SYN_CONDITION,
            "token_id": SYN_TOKEN,
            "side": "BUY",
            "price": "0.40",
            "quantity": "100.0",
            "is_sample": 0,
        },
    ])
    con = _open_ro(path)
    future = datetime.now(timezone.utc) + timedelta(days=3)
    provider = _ResolvingProvider(_FakeMarket(active=True, closed=False, resolved=False, end_date=future))
    report = build_trade_copyability_market_state_evidence_bridge(
        con, limit=20, provider=provider, live_preview=True, db_path=path
    )
    con.close()

    assert isinstance(report, TradeCopyabilityMarketStateBridgeReport)
    assert len(report.row_reports) == 1
    rr = report.row_reports[0]
    assert rr.eligibility_status == "eligible"
    assert rr.mappability_status == "mappable"
    assert rr.metadata_lookup_identifier_used == SYN_CONDITION
    # Market state actually resolved (not invented): values present.
    assert rr.market_active_available is True
    assert rr.market_active_value is True
    assert rr.market_closed_available is True
    assert rr.market_closed_value is False
    assert rr.market_resolved_available is True
    assert rr.market_resolved_value is False
    # end time + seconds computed from a real future end_date.
    assert rr.market_end_time_available is True
    assert rr.market_end_time_value is not None
    assert rr.seconds_to_market_end_available is True
    assert rr.seconds_to_market_end_value is not None
    assert rr.seconds_to_market_end_value > 0
    assert rr.metadata_fetched_at is not None
    assert rr.pr24u_pr24s_combinable is True
    # Provider was actually called (not just a name check).
    assert provider.calls == 1
    assert provider.called_ids == [SYN_CONDITION]


# ── Test 2: missing market identifier is skipped with a clear reason ─────────
def test_missing_market_identifier_skipped_with_reason():
    # Only token_id present (no market_source_id) -> unresolvable_token_id_only.
    path = _make_db([
        {
            "source_trade_id": "t_token_only",
            "market_source_id": None,
            "token_id": SYN_TOKEN,
            "side": "BUY",
            "price": "0.40",
            "quantity": "100.0",
            "is_sample": 0,
        },
    ])
    con = _open_ro(path)
    report = build_trade_copyability_market_state_evidence_bridge(
        con, limit=20, provider=OfflineMarketStateProvider(), db_path=path
    )
    con.close()

    assert len(report.row_reports) == 1
    rr = report.row_reports[0]
    assert rr.eligibility_status == "eligible"  # token_id alone still input-eligible
    assert rr.mappability_status == "unresolvable_token_id_only"
    assert rr.metadata_lookup_identifier_used is None
    assert rr.skip_reason == "token_id_cannot_resolve_market_state_without_condition_mapping"
    # No market state invented.
    assert rr.market_active_available is False
    assert rr.market_active_value is None
    assert rr.seconds_to_market_end_available is False
    assert rr.pr24u_pr24s_combinable is False


# ── Test 2b: non-conditionId identifier (sample placeholder) skipped ────────
def test_non_condition_id_identifier_skipped():
    path = _make_db([
        {
            "source_trade_id": "t_sample_market",
            "market_source_id": "sample-market-001",
            "token_id": None,
            "side": "BUY",
            "price": "0.70",
            "quantity": "30.0",
            "is_sample": 1,
        },
    ])
    con = _open_ro(path)
    report = build_trade_copyability_market_state_evidence_bridge(
        con, limit=20, provider=OfflineMarketStateProvider(), db_path=path
    )
    con.close()
    rr = report.row_reports[0]
    assert rr.mappability_status == "sample_skipped"
    assert rr.skip_reason is not None
    assert rr.market_active_available is False
    assert rr.metadata_fetched_at is None


# ── Test 3: metadata client failure controlled; whole run does not crash ─────
def test_metadata_client_failure_controlled_no_crash():
    path = _make_db([
        {"source_trade_id": "t_a", "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0", "is_sample": 0},
        {"source_trade_id": "t_b", "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0", "is_sample": 0},
    ])
    con = _open_ro(path)
    # Raise on the very first call -> every row must still complete, capturing error.
    provider = _ResolvingProvider(_FakeMarket(active=True), raise_on=1)
    report = build_trade_copyability_market_state_evidence_bridge(
        con, limit=20, provider=provider, live_preview=True, db_path=path
    )
    con.close()
    assert len(report.row_reports) == 2
    for rr in report.row_reports:
        assert rr.error_reason is not None
        assert "RuntimeError" in rr.error_reason
        # Market state was NOT invented despite the error.
        assert rr.market_active_available is False
        assert rr.market_active_value is None


# ── Test 3b: MARKET_NOT_FOUND (404) is honest, not invented ──────────────────
def test_market_not_found_not_invented():
    path = _make_db([
        {"source_trade_id": "t_unknown", "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0", "is_sample": 0},
    ])
    con = _open_ro(path)
    report = build_trade_copyability_market_state_evidence_bridge(
        con, limit=20, provider=_NotFoundProvider(), live_preview=True, db_path=path
    )
    con.close()
    rr = report.row_reports[0]
    assert rr.error_reason is not None
    assert "MARKET_NOT_FOUND" in rr.error_reason
    assert rr.market_active_available is False
    assert rr.market_active_value is None
    assert rr.metadata_fetched_at is not None  # the attempted fetch recorded its time


# ── Test 4: market state not invented when unavailable (offline dry-run) ─────
def test_no_invention_offline_dry_run():
    path = _make_db([
        {"source_trade_id": "t_real", "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0", "is_sample": 0},
    ])
    con = _open_ro(path)
    report = build_trade_copyability_market_state_evidence_bridge(
        con, limit=20, provider=OfflineMarketStateProvider(), db_path=path
    )
    con.close()
    rr = report.row_reports[0]
    assert rr.market_active_available is False
    assert rr.market_active_value is None
    assert rr.market_closed_available is False
    assert rr.market_closed_value is None
    assert rr.market_resolved_available is False
    assert rr.market_resolved_value is None
    assert rr.market_end_time_available is False
    assert rr.market_end_time_value is None
    assert rr.seconds_to_market_end_available is False
    assert rr.seconds_to_market_end_value is None
    assert rr.metadata_fetched_at is None


# ── Test 5: seconds_to_market_end computed ONLY when end time present ────────
def test_seconds_only_when_end_time_present():
    now = datetime.now(timezone.utc)
    # No end_date -> seconds must NOT be available.
    res_no_end = _MarketStateFetchResult(
        condition_id=SYN_CONDITION, fetched=True,
        market=_FakeMarket(active=True, end_date=None), error_code=None,
    )
    norm = _normalize_market_state(res_no_end, now)
    assert norm["market_end_time_available"] is False
    assert norm["seconds_to_market_end_available"] is False
    assert norm["seconds_to_market_end_value"] is None

    # With end_date -> seconds available and positive.
    future = now + timedelta(days=1)
    res_end = _MarketStateFetchResult(
        condition_id=SYN_CONDITION, fetched=True,
        market=_FakeMarket(active=True, end_date=future), error_code=None,
    )
    norm2 = _normalize_market_state(res_end, now)
    assert norm2["market_end_time_available"] is True
    assert norm2["seconds_to_market_end_available"] is True
    assert norm2["seconds_to_market_end_value"] == int(future.timestamp() - now.timestamp())


# ── Test 6: negative seconds / closed / resolved reported honestly ───────────
def test_negative_seconds_and_closed_resolved_honest():
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=2)
    # Past end_date -> negative seconds, reported honestly (not clamped).
    res = _MarketStateFetchResult(
        condition_id=SYN_CONDITION, fetched=True,
        market=_FakeMarket(active=False, closed=True, resolved=True, end_date=past),
        error_code=None,
    )
    norm = _normalize_market_state(res, now)
    assert norm["market_active_value"] is False
    assert norm["market_closed_value"] is True
    assert norm["market_resolved_value"] is True
    # Negative seconds must be reported, NOT clamped to 0.
    assert norm["seconds_to_market_end_available"] is True
    assert norm["seconds_to_market_end_value"] < 0
    assert any("negative" in n for n in norm["notes"])


# ── Test 7-12: dry-run creates NO DB writes across guarded tables ────────────
def test_dry_run_creates_no_db_writes_and_no_decisions():
    path = _make_db([
        {"source_trade_id": "t_real", "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0", "is_sample": 0},
        {"source_trade_id": "t_sample", "market_source_id": "sample-market-001",
         "side": "BUY", "price": "0.70", "quantity": "30.0", "is_sample": 1},
    ], add_extra_tables=True)

    # Snapshot size/mtime BEFORE.
    size_before = Path(path).stat().st_size
    mtime_before = Path(path).stat().st_mtime

    con = _open_ro(path)
    report = build_trade_copyability_market_state_evidence_bridge(
        con, limit=20, provider=OfflineMarketStateProvider(), db_path=path
    )
    con.close()

    # Snapshot size/mtime AFTER (opened read-only).
    size_after = Path(path).stat().st_size
    mtime_after = Path(path).stat().st_mtime
    assert size_after == size_before, "DB file size changed during dry-run (write detected)"
    assert mtime_after == mtime_before, "DB mtime changed during dry-run (write detected)"

    # All guarded tables remain EMPTY (no rows inserted).
    con_rw = sqlite3.connect(path)
    for table in (
        "trade_copyability_decisions",
        "copy_candidates",
        "paper_signal_decisions",
        "candidate_price_snapshots",
        "candidate_price_snapshot_levels",
        "orders",
        "positions",
    ):
        count = con_rw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count == 0, f"{table} was populated during dry-run"
    # source_trades unchanged count.
    st_count = con_rw.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
    assert st_count == 2
    # Confirm report flags all False.
    assert report.ready_to_wire_to_automation is False
    assert report.ready_to_persist_decisions is False
    assert report.ready_to_create_candidates is False
    con_rw.close()


# ── Test 13: no timers / services / deploy files touched (module has none) ───
def test_no_timer_service_or_deploy_artifacts():
    src = inspect.getsource(mod)
    for token in ("systemctl", ".timer", ".service", "deploy", "restart",
                  "enable_timer", "cron", "supervisor"):
        assert token not in src, f"forbidden token {token!r} found in module source"


# ── Test 14: no broker / order / automation imports appear ───────────────────
def test_no_broker_order_automation_imports():
    src = inspect.getsource(mod)
    forbidden = [
        "import polycopy.db.database",
        "import polycopy.automation",
        "from polycopy.automation",
        "broker.",
        "create_copy_candidate",
        "create_paper_signal",
        "place_order",
        "submit_order",
        "specialist_aggregation",
    ]
    for token in forbidden:
        assert token not in src, f"forbidden wiring token {token!r} found in module source"

    # Also confirm the module does NOT import the write-capable ORM.
    import ast
    tree = ast.parse(src)
    imports = [
        (getattr(n, "module", None) or "")
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
    ]
    flat = " ".join(imports)
    assert "polycopy.db.database" not in flat


# ── Test: reuse of existing PolymarketPublicAdapter (not a duplicate) ────────
def test_reuses_existing_polymarket_public_adapter():
    # LiveGammaMarketStateProvider MUST wrap a PolymarketPublicAdapter instance,
    # and that adapter's class name is PolymarketPublicAdapter (not a new dup).
    from polycopy.adapters.polymarket import PolymarketPublicAdapter
    from polycopy.config.settings import Settings

    settings = Settings()
    adapter = PolymarketPublicAdapter(
        gamma_base_url=settings.gamma_base_url,
        clob_base_url=settings.clob_base_url,
        data_api_base_url=settings.data_api_base_url,
    )
    provider = LiveGammaMarketStateProvider(adapter=adapter)
    # The adapter must be the reused one (duck-type get_market present).
    assert hasattr(adapter, "get_market")
    assert provider._adapter is adapter
    # Confirm the reused adapter does NOT import the write ORM either.
    import inspect as _inspect
    assert "polycopy.db.database" not in _inspect.getsource(PolymarketPublicAdapter)


# ── Test: identifier resolution priority (market_source_id over token) ───────
def test_identifier_resolution_priority():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE source_trades (source_trade_id TEXT, market_source_id TEXT, token_id TEXT)")
    con.row_factory = sqlite3.Row
    field_map = {
        "source_trade_id": "source_trade_id",
        "market_source_id": "market_source_id",
        "token_id": "token_id",
        "trader_address": None, "wallet_id": None, "side": None,
        "price": None, "size": None, "timestamp": None,
    }
    # Both present -> market_source_id (conditionId) wins.
    row = con.execute(
        "SELECT * FROM source_trades"
    ).fetchone() or None
    # Build a real row object via a temp table insert.
    con.execute(
        "INSERT INTO source_trades VALUES (?,?,?)",
        ("t1", SYN_CONDITION, SYN_TOKEN),
    )
    row = con.execute("SELECT * FROM source_trades").fetchone()
    ident, kind, status, skip = _resolve_lookup_identifier(row, field_map)
    assert ident == SYN_CONDITION
    assert kind == "market_source_id"
    assert status == "resolved_via_market_source_id"
    con.close()


# ── Test: report_to_human and report_to_dict serialize ───────────────────────
def test_report_serializes():
    path = _make_db([
        {"source_trade_id": "t_real", "market_source_id": SYN_CONDITION, "side": "BUY",
         "price": "0.40", "quantity": "100.0", "is_sample": 0},
    ])
    con = _open_ro(path)
    report = build_trade_copyability_market_state_evidence_bridge(
        con, limit=20, provider=OfflineMarketStateProvider(), db_path=path
    )
    con.close()

    human = report_to_human(report)
    assert "MARKET-STATE" in human
    d = report.to_dict()
    assert d["source_trade_count"] == 1
    # JSON round-trips.
    import json
    json.dumps(d, default=str)
    # row report dict carries every required PR24V field.
    rr = d["row_reports"][0]
    for key in (
        "source_trade_id", "wallet_address", "market_source_id", "token_id", "side",
        "eligibility_status", "mappability_status", "metadata_lookup_identifier_used",
        "market_active_available", "market_active_value", "market_closed_available",
        "market_closed_value", "market_resolved_available", "market_resolved_value",
        "market_end_time_available", "market_end_time_value",
        "seconds_to_market_end_available", "seconds_to_market_end_value",
        "market_identifier_mapping_status", "metadata_fetched_at",
        "skip_reason", "error_reason",
    ):
        assert key in rr, f"required field {key!r} missing from row report"
