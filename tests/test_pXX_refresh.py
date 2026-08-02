"""S4 canonical-market-truth refresh tests (PR71 Task 9).

Temp/scratch DBs only. Never opens production.

Proves the S4 contract: the refresh reuses the PROVEN
``source_trade_resolution`` path (build_market_state_provider /
PolymarketPublicAdapter.get_market / derive_winner_from_market_payload /
settle_source_trade_against_truth) — it does NOT carry its own resolution
parser. Exactly one selector, exact accepted source values, canonical
six-field BUY settlement, honest unresolved/error states, whole-market
conflict rollback, bookkeeping semantics, and zero execution artifacts.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import asyncio
import pytest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.db.database import Database  # noqa: E402
from polycopy.domain.market import Market, MarketOutcome  # noqa: E402
from polycopy.ingestion.normalized_source_trade import (  # noqa: E402
    SOURCE_NAME,
)
from polycopy.ingestion.source_trade_resolution import (  # noqa: E402
    SPECIALIST_REFRESH_SOURCES,
    select_markets_for_refresh,
)


def _load(n):
    s = importlib.util.spec_from_file_location(n, ROOT / "scripts" / n)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


refresh = _load("refresh_specialist_market_truth.py")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

COND = "0x" + "c" * 64
TOK_WIN = "0x" + "a" * 64
TOK_LOSE = "0x" + "b" * 64
WID = "uuid-wallet-000000000000000000000000"
ADDR = "0xwallet00000000000000000000000000000refr"
WATCH = "wl-active-000000000000000000000000000000"
WATCH_PAUSED = "wl-paused-000000000000000000000000000001"
WATCH_RETIRED = "wl-retired-00000000000000000000000000002"
WATCH_SAMPLE = "wl-sample-0000000000000000000000000000003"


def _tmp():
    raise RuntimeError("_tmp is provided by the module-owned SQLite fixture")


@pytest.fixture(autouse=True)
def _owned_sqlite_paths(monkeypatch, owned_sqlite):
    """Route this module's disposable SQLite files through pytest ownership."""
    monkeypatch.setitem(globals(), "_tmp", owned_sqlite.new_path)


def _open():
    p = _tmp()
    return Database(p).connect(), p


def _temp_v21_db():
    """Create a fresh v21 DB and return its Path (for production-gate tests)."""
    p = _tmp()
    Database(p).connect().close()
    return p


def _seed_wallet(db, wid=WID, address=ADDR, sample=0):
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (wid, address, "t", sample, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()


def _seed_watch(db, wid=WATCH, wallet=WID, status="active"):
    db.conn.execute(
        "INSERT INTO specialist_evidence_watchlist(id,wallet_id,status,source,"
        "reason,created_at,max_new_trades_per_run) VALUES (?,?,?,?,?,?,?)",
        (wid, wallet, status, "manual", "t", "2026-01-01T00:00:00Z", 25),
    )
    db.conn.commit()


def _insert_trade(
    db,
    tid,
    condition=COND,
    status="unresolved",
    winner=None,
    side="BUY",
    source=SOURCE_NAME,
    token=TOK_WIN,
    price=0.40,
    qty=10.0,
    trader=ADDR,
):
    db.conn.execute(
        "INSERT INTO source_trades("
        "id, source, source_trade_id, market_source_id, side, outcome, "
        "quantity, price, trader_address, timestamp, is_sample, token_id, "
        "resolution_status, winning_token_id, metadata_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, source, tid, condition, side, "Yes", qty, price,
         trader, "2026-02-01T00:00:00Z", 0, token,
         status, winner, json.dumps({}, sort_keys=True)),
    )
    db.conn.commit()


def _row(db, tid):
    return dict(
        db.conn.execute(
            "SELECT * FROM source_trades WHERE source_trade_id=?", (tid,)
        ).fetchone()
    )


def _gamma_market(*, condition, resolved, winner_token, loser_token=TOK_LOSE):
    outcomes = [
        MarketOutcome(label="Yes", price=0.5, clob_token_id=winner_token),
        MarketOutcome(label="No", price=0.5, clob_token_id=loser_token),
    ]
    return Market(
        source_id=condition,
        question="test",
        outcomes=outcomes,
        source="polymarket",
        active=False,
        closed=True,
        resolved=resolved,
        resolution_outcome="Yes" if resolved else None,
        fetched_at=datetime.now(timezone.utc),
    )


class _FakeProvider:
    """Async get_market stub keyed by condition id. Counts calls."""

    def __init__(self, by_condition=None, errors=None):
        self._cond = by_condition or {}
        self._errors = errors or {}
        self.calls = []

    async def get_market(self, market_id):
        self.calls.append(market_id)
        if market_id in self._errors:
            raise self._errors[market_id]
        return self._cond.get(market_id)


def _provider_resolved(condition=COND, winner=TOK_WIN):
    return _FakeProvider(by_condition={condition: _gamma_market(
        condition=condition, resolved=True, winner_token=winner)})


# ---------------------------------------------------------------------------
# 1. Refresh works without a markets row
# ---------------------------------------------------------------------------

def test_refresh_works_without_markets_row():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND)  # unresolved, no markets row
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 0, rc
    r = _row(db, "t1")
    assert r["resolution_status"] == "won"
    assert r["winning_token_id"] == TOK_WIN
    assert r["is_winning_trade"] == 1
    assert r["settlement_source"] == "source_trade_resolution"
    assert r["resolved_at"] is not None
    db.close()


# ---------------------------------------------------------------------------
# 2. Exactly one selector is required
# ---------------------------------------------------------------------------

def test_refresh_requires_exactly_one_selector():
    db, _ = _open()
    _seed_wallet(db)
    # No selector.
    rc = refresh.main([
        "--db-path", str(db.db_path), "--write", "--allow-live",
        "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 2, rc
    # Two selectors.
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--wallet-id", WID, "--write", "--allow-live",
        "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 2, rc
    db.close()


# ---------------------------------------------------------------------------
# 3. wallet UUID resolves to canonical address
# ---------------------------------------------------------------------------

def test_wallet_uuid_resolves_to_canonical_address():
    db, _ = _open()
    _seed_wallet(db, wid=WID, address=ADDR)
    _insert_trade(db, "t1", condition=COND, trader=ADDR)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--wallet-id", WID,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 0, rc
    assert _row(db, "t1")["resolution_status"] == "won"
    db.close()


def test_unknown_wallet_refused():
    db, _ = _open()
    _seed_wallet(db, wid=WID, address=ADDR)
    _insert_trade(db, "t1", condition=COND, trader=ADDR)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--wallet-id", "uuid-unknown",
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 2, rc  # unknown wallet refused before any open/provider/network
    assert _row(db, "t1")["resolution_status"] == "unresolved"
    db.close()


def test_sample_wallet_refused():
    db, _ = _open()
    _seed_wallet(db, wid=WID, address=ADDR, sample=1)
    _insert_trade(db, "t1", condition=COND, trader=ADDR)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--wallet-id", WID,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 2, rc  # sample wallet refused (never silently settled)
    assert _row(db, "t1")["resolution_status"] == "unresolved"
    db.close()


# ---------------------------------------------------------------------------
# 4. watchlist uses specialist_evidence_watchlist.id
# ---------------------------------------------------------------------------

def test_watchlist_id_resolves_to_wallet_address():
    db, _ = _open()
    _seed_wallet(db, wid=WID, address=ADDR)
    _seed_watch(db, wid=WATCH, wallet=WID, status="active")
    _insert_trade(db, "t1", condition=COND, trader=ADDR)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--watch-id", WATCH,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 0, rc
    assert _row(db, "t1")["resolution_status"] == "won"
    db.close()


# ---------------------------------------------------------------------------
# 5. paused/retired/sample/unknown selection is refused
# ---------------------------------------------------------------------------

def test_paused_watch_refused():
    db, _ = _open()
    _seed_wallet(db, wid=WID, address=ADDR)
    _seed_watch(db, wid=WATCH_PAUSED, wallet=WID, status="paused")
    _insert_trade(db, "t1", condition=COND, trader=ADDR)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--watch-id", WATCH_PAUSED,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 2, rc
    assert _row(db, "t1")["resolution_status"] == "unresolved"
    db.close()


def test_retired_watch_refused():
    db, _ = _open()
    _seed_wallet(db, wid=WID, address=ADDR)
    _seed_watch(db, wid=WATCH_RETIRED, wallet=WID, status="retired")
    _insert_trade(db, "t1", condition=COND, trader=ADDR)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--watch-id", WATCH_RETIRED,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 2, rc
    assert _row(db, "t1")["resolution_status"] == "unresolved"
    db.close()


def test_sample_watch_refused():
    db, _ = _open()
    _seed_wallet(db, wid=WID, address=ADDR, sample=1)
    _seed_watch(db, wid=WATCH_SAMPLE, wallet=WID, status="active")
    _insert_trade(db, "t1", condition=COND, trader=ADDR)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--watch-id", WATCH_SAMPLE,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 2, rc
    assert _row(db, "t1")["resolution_status"] == "unresolved"
    db.close()


def test_unknown_watch_refused():
    db, _ = _open()
    _seed_wallet(db, wid=WID, address=ADDR)
    _insert_trade(db, "t1", condition=COND, trader=ADDR)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--watch-id", "wl-missing",
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 2, rc
    assert _row(db, "t1")["resolution_status"] == "unresolved"
    db.close()


# ---------------------------------------------------------------------------
# 6. zero / negative / >500 market limits refused
# ---------------------------------------------------------------------------

def test_zero_limit_refused():
    db, _ = _open()
    _seed_wallet(db)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--limit-markets", "0", "--write", "--allow-live",
        "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 2, rc
    db.close()


def test_negative_limit_refused():
    db, _ = _open()
    _seed_wallet(db)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--limit-markets", "-5", "--write", "--allow-live",
        "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 2, rc
    db.close()


def test_over_max_limit_refused():
    db, _ = _open()
    _seed_wallet(db)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--limit-markets", "501", "--write", "--allow-live",
        "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 2, rc
    db.close()


# ---------------------------------------------------------------------------
# 7. canonical SOURCE_NAME rows are selected
# ---------------------------------------------------------------------------

def test_canonical_source_name_selected():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND, source=SOURCE_NAME)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 0, rc
    assert _row(db, "t1")["resolution_status"] == "won"
    db.close()


# ---------------------------------------------------------------------------
# 8. polymarket_clob rows are selected
# ---------------------------------------------------------------------------

def test_polymarket_clob_source_selected():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND, source="polymarket_clob")
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 0, rc
    assert _row(db, "t1")["resolution_status"] == "won"
    db.close()


# ---------------------------------------------------------------------------
# 9. source="polymarket", sample, SELL, non-Polymarket excluded
# ---------------------------------------------------------------------------

def test_legacy_polymarket_source_excluded():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND, source="polymarket")
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 0, rc
    assert _row(db, "t1")["resolution_status"] == "unresolved"
    db.close()


def test_sample_trade_excluded():
    db, _ = _open()
    _seed_wallet(db)
    db.conn.execute(
        "INSERT INTO source_trades(id,source,source_trade_id,market_source_id,"
        "side,outcome,quantity,price,trader_address,timestamp,is_sample,"
        "token_id,resolution_status,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("t1", SOURCE_NAME, "t1", COND, "BUY", "Yes", 10.0, 0.4, ADDR,
         "2026-02-01T00:00:00Z", 1, TOK_WIN, "unresolved",
         json.dumps({}, sort_keys=True)),
    )
    db.conn.commit()
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 0, rc
    assert _row(db, "t1")["resolution_status"] == "unresolved"
    db.close()


def test_sell_trade_excluded_and_unchanged():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND, side="SELL")
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 0, rc
    r = _row(db, "t1")
    assert r["resolution_status"] == "unresolved"
    assert r["is_winning_trade"] is None
    assert r["realized_pnl"] is None
    db.close()


def test_non_polymarket_source_excluded():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND, source="kalshi")
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 0, rc
    assert _row(db, "t1")["resolution_status"] == "unresolved"
    db.close()


# ---------------------------------------------------------------------------
# 10. One provider call serves all linked trades for one market
# ---------------------------------------------------------------------------

def test_one_provider_call_per_market_all_linked():
    db, _ = _open()
    _seed_wallet(db)
    for i in range(3):
        _insert_trade(db, f"t{i}", condition=COND)
    prov = _provider_resolved()
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=prov)
    assert rc == 0, rc
    assert prov.calls.count(COND) == 1, prov.calls
    n = db.conn.execute(
        "SELECT COUNT(*) FROM source_trades WHERE resolution_status='won' "
        "AND winning_token_id=?", (TOK_WIN,)).fetchone()[0]
    assert n == 3, n
    db.close()


# ---------------------------------------------------------------------------
# 11. winning BUY receives all six fields
# ---------------------------------------------------------------------------

def test_winning_buy_six_fields():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND, price=0.40, qty=10.0)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved(winner=TOK_WIN))
    assert rc == 0, rc
    r = _row(db, "t1")
    assert r["resolution_status"] == "won"
    assert r["winning_token_id"] == TOK_WIN
    assert r["is_winning_trade"] == 1
    assert abs(r["realized_pnl"] - (1 - 0.40) * 10.0) < 1e-9
    assert r["settlement_source"] == "source_trade_resolution"
    assert r["resolved_at"] is not None
    db.close()


# ---------------------------------------------------------------------------
# 12. losing BUY receives correct negative P&L
# ---------------------------------------------------------------------------

def test_losing_buy_six_fields():
    db, _ = _open()
    _seed_wallet(db)
    # Trade token is the losing token -> lost.
    _insert_trade(db, "t1", condition=COND, token=TOK_LOSE, price=0.40, qty=10.0)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved(winner=TOK_WIN))
    assert rc == 0, rc
    r = _row(db, "t1")
    assert r["resolution_status"] == "lost"
    assert r["is_winning_trade"] == 0
    assert abs(r["realized_pnl"] - (-0.40 * 10.0)) < 1e-9
    assert r["settlement_source"] == "source_trade_resolution"
    db.close()


# ---------------------------------------------------------------------------
# 13. Unresolved upstream truth makes no winner/P&L claim
# ---------------------------------------------------------------------------

def test_unresolved_upstream_no_claim():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND)
    prov = _FakeProvider(by_condition={COND: _gamma_market(
        condition=COND, resolved=False, winner_token=None)})
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=prov)
    assert rc == 0, rc
    r = _row(db, "t1")
    assert r["resolution_status"] == "unresolved"
    assert r["winning_token_id"] is None
    assert r["is_winning_trade"] is None
    assert r["realized_pnl"] is None
    db.close()


# ---------------------------------------------------------------------------
# 14. Provider unavailable is distinct from not found
# ---------------------------------------------------------------------------

def test_provider_unavailable_distinct():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND)
    prov = _FakeProvider(errors={COND: RuntimeError("boom")})
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=prov)
    assert rc == 0, rc
    r = _row(db, "t1")
    assert r["resolution_status"] == "unresolved"
    bk = db.conn.execute(
        "SELECT last_status, last_error FROM specialist_market_refresh_state "
        "WHERE market_source_id=?", (COND,)).fetchone()
    assert dict(bk)["last_status"] == "provider_unavailable"
    assert dict(bk)["last_error"] == "provider_error:RuntimeError"
    db.close()


def test_provider_not_found_distinct():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND)
    prov = _FakeProvider(by_condition={COND: None})  # 404/unknown
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=prov)
    assert rc == 0, rc
    bk = db.conn.execute(
        "SELECT last_status FROM specialist_market_refresh_state "
        "WHERE market_source_id=?", (COND,)).fetchone()
    assert dict(bk)["last_status"] == "unavailable"
    db.close()


# ---------------------------------------------------------------------------
# 15. Routing HTTP error is recorded honestly
# ---------------------------------------------------------------------------

def test_routing_http_error_recorded():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND)

    class _HttpError(Exception):
        def __init__(self):
            self.response = type("R", (), {"status_code": 422})()
            super().__init__("HTTP 422")

    prov = _FakeProvider(errors={COND: _HttpError()})
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=prov)
    assert rc == 0, rc
    bk = db.conn.execute(
        "SELECT last_status, last_error FROM specialist_market_refresh_state "
        "WHERE market_source_id=?", (COND,)).fetchone()
    assert dict(bk)["last_status"] == "routing_http_error"
    assert "422" in dict(bk)["last_error"]
    db.close()


# ---------------------------------------------------------------------------
# 16. Malformed / ambiguous / missing-winner truth makes no settlement claim
# ---------------------------------------------------------------------------

def test_malformed_payload_no_claim():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND)
    # Gamma returns resolved with no matching outcome winner -> incomplete truth
    mkt = Market(
        source_id=COND, question="t",
        outcomes=[MarketOutcome(label="Maybe", price=0.5, clob_token_id=TOK_WIN)],
        source="polymarket", active=False, closed=True, resolved=True,
        resolution_outcome="Yes", fetched_at=datetime.now(timezone.utc),
    )
    prov = _FakeProvider(by_condition={COND: mkt})
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=prov)
    assert rc == 0, rc
    assert _row(db, "t1")["resolution_status"] == "unresolved"
    bk = db.conn.execute(
        "SELECT last_status FROM specialist_market_refresh_state "
        "WHERE market_source_id=?", (COND,)).fetchone()
    # incomplete truth collapses to unresolved (no winner derivable)
    assert dict(bk)["last_status"] in ("unresolved", "missing_winning_token")
    db.close()


def test_ambiguous_no_claim():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND)
    mkt = Market(
        source_id=COND, question="t",
        outcomes=[
            MarketOutcome(label="Yes", price=0.5, clob_token_id=TOK_WIN),
            MarketOutcome(label="Yes", price=0.5, clob_token_id=TOK_LOSE),
        ],
        source="polymarket", active=False, closed=True, resolved=True,
        resolution_outcome="Yes", fetched_at=datetime.now(timezone.utc),
    )
    prov = _FakeProvider(by_condition={COND: mkt})
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=prov)
    assert rc == 0, rc
    assert _row(db, "t1")["resolution_status"] == "unresolved"
    bk = db.conn.execute(
        "SELECT last_status FROM specialist_market_refresh_state "
        "WHERE market_source_id=?", (COND,)).fetchone()
    assert dict(bk)["last_status"] == "ambiguous"
    db.close()


# ---------------------------------------------------------------------------
# 17. SELL rows remain byte-for-byte unchanged
# ---------------------------------------------------------------------------

def test_sell_unchanged_byte_for_byte():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND, side="SELL", price=0.42, qty=7.0)
    before = _row(db, "t1")
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 0, rc
    after = _row(db, "t1")
    for key in ("resolution_status", "winning_token_id", "is_winning_trade",
                "realized_pnl", "settlement_source", "resolved_at"):
        assert before[key] == after[key], key
    db.close()


# ---------------------------------------------------------------------------
# 18. Existing identical settlement is a no-op
# ---------------------------------------------------------------------------

def test_existing_identical_settlement_noop():
    db, _ = _open()
    _seed_wallet(db)
    # Pre-resolved identically to what the provider would produce.
    _insert_trade(db, "t1", condition=COND, status="won", winner=TOK_WIN,
                  price=0.40, qty=10.0)
    db.conn.execute(
        "UPDATE source_trades SET is_winning_trade=1, realized_pnl=?, "
        "settlement_source='source_trade_resolution', resolved_at='2026-03-01T00:00:00Z' "
        "WHERE source_trade_id='t1'",
        ((1 - 0.40) * 10.0,))
    db.conn.commit()
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved(winner=TOK_WIN))
    assert rc == 0, rc
    r = _row(db, "t1")
    assert r["resolution_status"] == "won"
    assert r["winning_token_id"] == TOK_WIN
    db.close()


# ---------------------------------------------------------------------------
# 19. Existing conflicting winner blocks all source-trade writes for market
# ---------------------------------------------------------------------------

def test_conflicting_winner_blocks_all_writes_for_market():
    db, _ = _open()
    _seed_wallet(db)
    # Two trades, same market, different already-stored winners.
    _insert_trade(db, "t1", condition=COND, status="won", winner=TOK_WIN,
                  token=TOK_WIN, price=0.40, qty=10.0)
    db.conn.execute(
        "UPDATE source_trades SET is_winning_trade=1, realized_pnl=6.0, "
        "settlement_source='source_trade_resolution', "
        "resolved_at='2026-03-01T00:00:00Z' WHERE source_trade_id='t1'")
    _insert_trade(db, "t2", condition=COND, status="won", winner=TOK_LOSE,
                  token=TOK_LOSE, price=0.40, qty=10.0)
    db.conn.execute(
        "UPDATE source_trades SET is_winning_trade=0, realized_pnl=-4.0, "
        "settlement_source='source_trade_resolution', "
        "resolved_at='2026-03-01T00:00:00Z' WHERE source_trade_id='t2'")
    db.conn.commit()
    # Provider says the winner is TOK_WIN (so t2's stored winner conflicts).
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved(winner=TOK_WIN))
    assert rc == 0, rc
    r1 = _row(db, "t1")
    r2 = _row(db, "t2")
    # Both retain their exact prior values; nothing updated.
    assert r1["winning_token_id"] == TOK_WIN
    assert r2["winning_token_id"] == TOK_LOSE
    assert r1["realized_pnl"] == 6.0
    assert r2["realized_pnl"] == -4.0
    # Exact market selection is a diagnostic override and records the conflict
    # without mutating either canonical source row.
    bk = db.conn.execute(
        "SELECT last_status, last_error FROM specialist_market_refresh_state "
        "WHERE market_source_id=?", (COND,)).fetchone()
    assert dict(bk) == {"last_status": "resolved", "last_error": "conflict"}
    db.close()


# ---------------------------------------------------------------------------
# 20. A forced bookkeeping/update failure rolls back all source-trade changes
# ---------------------------------------------------------------------------



def test_conflict_retry_budget_first_second_third_without_source_mutation(monkeypatch):
    """Conflict is retryable for three exact diagnostic refreshes only."""
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "conflict-a", condition=COND, status="won", winner=TOK_WIN,
                  token=TOK_WIN, price=0.40, qty=10.0)
    _insert_trade(db, "conflict-b", condition=COND, status="won", winner=TOK_LOSE,
                  token=TOK_LOSE, price=0.40, qty=10.0)
    db.conn.execute(
        "UPDATE source_trades SET is_winning_trade=1, realized_pnl=6.0, "
        "settlement_source='source_trade_resolution', resolved_at='2026-03-01T00:00:00Z' "
        "WHERE source_trade_id='conflict-a'"
    )
    db.conn.execute(
        "UPDATE source_trades SET is_winning_trade=0, realized_pnl=-4.0, "
        "settlement_source='source_trade_resolution', resolved_at='2026-03-01T00:00:00Z' "
        "WHERE source_trade_id='conflict-b'"
    )
    db.conn.commit()
    before = [dict(r) for r in db.conn.execute(
        "SELECT * FROM source_trades WHERE market_source_id=? ORDER BY id", (COND,)
    )]
    for expected in (1, 2, 3):
        provider = _provider_resolved(winner=TOK_WIN)
        assert refresh.main([
            "--db-path", str(db.db_path), "--market-source-id", COND,
            "--write", "--allow-live", "--confirm-production-db",
        ], provider=provider) == 0
        state = dict(db.conn.execute(
            "SELECT last_status, last_error, attempt_count, next_check_after "
            "FROM specialist_market_refresh_state WHERE market_source_id=?", (COND,)
        ).fetchone())
        assert state["last_status"] == "resolved"
        assert state["last_error"] == "conflict"
        assert state["attempt_count"] == expected
        if expected == 3:
            assert state["next_check_after"] == refresh._TERMINAL_NEXT_CHECK_AFTER
        else:
            assert state["next_check_after"] != refresh._TERMINAL_NEXT_CHECK_AFTER
    after = [dict(r) for r in db.conn.execute(
        "SELECT * FROM source_trades WHERE market_source_id=? ORDER BY id", (COND,)
    )]
    assert after == before
    db.close()


def test_market_conflict_rolls_back_via_savepoint():
    db, _ = _open()
    _seed_wallet(db)
    # One unresolved trade (would be updated) + one conflicting resolved trade.
    _insert_trade(db, "t1", condition=COND, status="unresolved", token=TOK_WIN,
                  price=0.40, qty=10.0)
    # A second market with a stored different winner that conflicts with the
    # provider truth, attached to the SAME market_source_id by sharing COND.
    _insert_trade(db, "t2", condition=COND, status="won", winner=TOK_LOSE,
                  token=TOK_LOSE, price=0.40, qty=10.0)
    db.conn.execute(
        "UPDATE source_trades SET is_winning_trade=0, realized_pnl=-4.0, "
        "settlement_source='source_trade_resolution', "
        "resolved_at='2026-03-01T00:00:00Z' WHERE source_trade_id='t2'")
    db.conn.commit()
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved(winner=TOK_WIN))
    assert rc == 0, rc
    # t1 was unresolved and SHOULD have been updated... but the market SAVEPOINT
    # rolls back the whole market because t2 conflicts. So t1 stays unresolved.
    r1 = _row(db, "t1")
    assert r1["resolution_status"] == "unresolved", r1
    r2 = _row(db, "t2")
    assert r2["winning_token_id"] == TOK_LOSE  # exact existing value retained
    db.close()


# ---------------------------------------------------------------------------
# 21. Adapter aclose runs on success and provider exception
# ---------------------------------------------------------------------------

def test_adapter_aclose_runs_on_success():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND)
    closed = {}

    class _ClosingProvider:
        async def get_market(self, market_id):
            return _gamma_market(condition=market_id, resolved=True,
                                 winner_token=TOK_WIN)

        async def aclose(self):
            closed["ran"] = True

    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_ClosingProvider())
    assert rc == 0, rc
    assert closed.get("ran") is True
    db.close()


def test_adapter_aclose_runs_on_provider_exception():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND)
    closed = {}

    class _ClosingProvider:
        async def get_market(self, market_id):
            raise RuntimeError("boom")

        async def aclose(self):
            closed["ran"] = True

    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_ClosingProvider())
    assert rc == 0, rc
    assert closed.get("ran") is True
    db.close()


# ---------------------------------------------------------------------------
# 22. Dry-run performs zero writes
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--allow-live",  # dry-run (no --write)
    ], provider=_provider_resolved())
    assert rc == 0, rc
    r = _row(db, "t1")
    assert r["resolution_status"] == "unresolved"
    # No refresh-state row in dry-run.
    n = db.conn.execute(
        "SELECT COUNT(*) FROM specialist_market_refresh_state").fetchone()[0]
    assert n == 0, n
    db.close()


# ---------------------------------------------------------------------------
# 23. Unconfirmed production write invokes none of the open/build paths
# ---------------------------------------------------------------------------

def test_unconfirmed_production_write_invokes_no_paths():
    prod = ROOT / "data" / "polycopy.db"
    # Use a sentinel production path; refusal must occur before open/build.
    rc = refresh.main([
        "--db-path", str(prod), "--market-source-id", COND,
        "--write",  # missing --allow-live / --confirm-production-db
    ], provider=_provider_resolved())
    assert rc != 0, "production write without full gates must be refused"
    # Provide no DB open/build exercised: assert via return code only.
    assert rc == 2


# ---------------------------------------------------------------------------
# 24. Replay creates no duplicate refresh-state rows
# ---------------------------------------------------------------------------

def test_replay_no_duplicate_bookkeeping():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND)
    for _ in range(2):
        rc = refresh.main([
            "--db-path", str(db.db_path), "--market-source-id", COND,
            "--write", "--allow-live", "--confirm-production-db",
        ], provider=_provider_resolved())
        assert rc == 0, rc
    n = db.conn.execute(
        "SELECT COUNT(*) FROM specialist_market_refresh_state "
        "WHERE market_source_id=?", (COND,)).fetchone()[0]
    assert n == 1, n
    # Once canonical truth is terminal, the second invocation is suppressed
    # before a provider call and does not consume another scheduling attempt.
    bk = db.conn.execute(
        "SELECT attempt_count FROM specialist_market_refresh_state "
        "WHERE market_source_id=?", (COND,)).fetchone()
    assert dict(bk)["attempt_count"] == 1, dict(bk)
    db.close()


# ---------------------------------------------------------------------------
# 25. Zero approval/dispatch/candidate/signal/execution artifacts created
# ---------------------------------------------------------------------------

def test_zero_execution_artifacts():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=_provider_resolved())
    assert rc == 0, rc
    tables = [
        "specialist_approvals", "approved_specialist_trade_dispatches",
        "paper_signal_decisions", "paper_signal_execution_authorizations",
        "execution_risk_decisions", "paper_orders", "paper_fills",
        "paper_positions", "copy_candidates", "candidate_price_snapshots",
        "signals", "orders", "positions", "marks", "settlements",
    ]
    for t in tables:
        try:
            n = db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            n = 0
        assert n == 0, f"unexpected artifact in {t}: {n}"
    db.close()


# ---------------------------------------------------------------------------
# 26. Bookkeeping failure after source updates rolls back the settlement
# ---------------------------------------------------------------------------

def test_bookkeeping_failure_rolls_back_source_settlement():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND, price=0.40, qty=10.0)
    # A provider that settles successfully, but whose bookkeeping writer fails.
    class _BoomBookkeeping:
        def __init__(self, provider, error):
            self._provider = provider
            self._error = error
            self.calls = 0

        def __call__(self, db_conn, outcome):
            self.calls += 1
            raise self._error

    prov = _provider_resolved(winner=TOK_WIN)
    boom = RuntimeError("bookkeeping-boom")
    writer = _BoomBookkeeping(prov, boom)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=prov, bookkeeping_writer=writer)
    # Fail closed: controlled nonzero result.
    assert rc != 0, "bookkeeping failure must not return success"
    # All six settlement columns remain unchanged (rolled back).
    r = _row(db, "t1")
    assert r["resolution_status"] == "unresolved", r
    assert r["winning_token_id"] is None
    assert r["is_winning_trade"] is None
    assert r["realized_pnl"] is None
    assert r["settlement_source"] is None
    assert r["resolved_at"] is None
    # No refresh-state row was committed.
    n = db.conn.execute(
        "SELECT COUNT(*) FROM specialist_market_refresh_state").fetchone()[0]
    assert n == 0, n
    db.close()


# ---------------------------------------------------------------------------
# 27. Mid-market source-update failure rolls back the first update too
# ---------------------------------------------------------------------------

def test_mid_update_failure_rolls_back_first_update():
    db, _ = _open()
    _seed_wallet(db)
    # Two linked BUY rows; force the SECOND UPDATE to fail after the first.
    _insert_trade(db, "t1", condition=COND, price=0.40, qty=10.0)
    _insert_trade(db, "t2", condition=COND, price=0.40, qty=10.0)
    prov = _provider_resolved(winner=TOK_WIN)

    class _FailingBookkeeping:
        def __init__(self):
            self.calls = 0

        def __call__(self, db_conn, outcome):
            self.calls += 1  # never reached because source update fails first

    class _FailingConn:
        """Wrap sqlite3 conn; make the 2nd UPDATE fail, 1st succeeds."""

        def __init__(self, real):
            self._real = real
            self._updates = 0

        def __getattr__(self, name):
            return getattr(self._real, name)

        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("UPDATE SOURCE_TRADES"):
                self._updates += 1
                if self._updates == 2:
                    raise RuntimeError("second-update-boom")
            return self._real.execute(sql, params if params is not None else [])

    failing = _FailingConn(db.conn)
    writer = _FailingBookkeeping()

    class _FailingDb:
        """DbConn-like: real read methods, failing conn for UPDATEs."""
        conn = failing

        def execute(self, sql, params=None):
            return db.conn.execute(sql, params)

        def fetchone(self, sql, params=None):
            return db.conn.execute(sql, params).fetchone()

        def fetchall(self, sql, params=None):
            return db.conn.execute(sql, params).fetchall()

    failing_db = _FailingDb()
    # Patch resolve_selected_markets' savepoint path by injecting the failing db.
    # main() opens its own db; instead drive the helper directly under the
    # failing connection so the SAVEPOINT rollback is exercised truthfully.
    from polycopy.ingestion.source_trade_resolution import (
        resolve_selected_markets, ResolveReport,
    )
    report = ResolveReport(dry_run=False, live_read_performed=True)
    try:
        asyncio.run(resolve_selected_markets(
            failing_db,
            markets=[COND],
            provider=prov,
            apply=True,
            report=report,
            bookkeeping_writer=writer,
        ))
        assert False, "expected RuntimeError from 2nd update"
    except RuntimeError as e:
        assert "second-update-boom" in str(e), e
    # The first UPDATE must also be rolled back to the SAVEPOINT.
    r1 = db.conn.execute(
        "SELECT resolution_status FROM source_trades WHERE id='t1'").fetchone()
    r2 = db.conn.execute(
        "SELECT resolution_status FROM source_trades WHERE id='t2'").fetchone()
    assert r1[0] == "unresolved", r1
    assert r2[0] == "unresolved", r2
    # No partial bookkeeping row exists.
    n = db.conn.execute(
        "SELECT COUNT(*) FROM specialist_market_refresh_state").fetchone()[0]
    assert n == 0, n
    db.close()


# ---------------------------------------------------------------------------
# 28. Source updates and bookkeeping commit together (no separate txn)
# ---------------------------------------------------------------------------

def test_source_and_bookkeeping_commit_together():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND, price=0.40, qty=10.0)
    written = {}

    def _spy_bookkeeping(db_conn, outcome):
        written["called"] = True
        refresh._upsert_bookkeeping(db_conn, outcome)  # also perform the real upsert

    prov = _provider_resolved(winner=TOK_WIN)
    rc = refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=prov, bookkeeping_writer=_spy_bookkeeping)
    assert rc == 0, rc
    assert written.get("called") is True
    r = _row(db, "t1")
    assert r["resolution_status"] == "won"
    assert r["winning_token_id"] == TOK_WIN
    # Bookkeeping row exists AND matches the committed source settlement.
    bk = db.conn.execute(
        "SELECT last_status FROM specialist_market_refresh_state "
        "WHERE market_source_id=?", (COND,)).fetchone()
    assert dict(bk)["last_status"] == "resolved"
    db.close()


# ---------------------------------------------------------------------------
# 29. Artifact counts report the real existing count (no silent zero)
# ---------------------------------------------------------------------------

def test_artifact_counts_report_real_existing_count():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1", condition=COND, price=0.40, qty=10.0)
    # Seed a pre-existing FORBIDDEN artifact row so the count is non-zero.
    db.conn.execute(
        "INSERT INTO specialist_approvals(approval_id, wallet_address, "
        "specialist_category, formula_name, formula_version, reviewer, "
        "approved_at, created_at, updated_at) VALUES ("
        "'ap-1', ?, 'macro', 'f', 'v1', 'tester', "
        "'2026-03-01T00:00:00Z', '2026-03-01T00:00:00Z', '2026-03-01T00:00:00Z')",
        (ADDR,))
    db.conn.commit()
    prov = _provider_resolved(winner=TOK_WIN)
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = refresh.main([
            "--db-path", str(db.db_path), "--market-source-id", COND,
            "--json", "--write", "--allow-live", "--confirm-production-db",
        ], provider=prov)
    assert rc == 0, rc
    report = json.loads(buf.getvalue())
    # artifact_counts reports the ACTUAL existing count (1), not a silent 0.
    assert report["artifact_counts"].get("specialist_approvals") == 1, report
    # artifact_delta remains zero after S4 (no new artifact created).
    assert report["artifact_delta"] == {}, report
    # Observational proof: the forbidden table still has exactly 1 row and S4
    # added none.
    n = db.conn.execute(
        "SELECT COUNT(*) FROM specialist_approvals").fetchone()[0]
    assert n == 1, n
    db.close()


# ---------------------------------------------------------------------------
# 29b. A genuinely absent optional table reports zero (not an error)
# ---------------------------------------------------------------------------

def test_missing_artifact_table_reports_zero():
    db, _ = _open()
    _seed_wallet(db)
    # No artifact tables are seeded beyond the v21 base. Force a table that is
    # guaranteed absent by monkeypatching the tuple to a known-missing name.
    real = refresh._FORBIDDEN_ARTIFACT_TABLES
    try:
        refresh._FORBIDDEN_ARTIFACT_TABLES = ("zzz_no_such_artifact_table_xyz",)
        counts = refresh._count_artifacts(db)
    finally:
        refresh._FORBIDDEN_ARTIFACT_TABLES = real
    # Absent table -> zero, no exception.
    assert counts == {"zzz_no_such_artifact_table_xyz": 0}, counts
    db.close()


# ---------------------------------------------------------------------------
# 29c. A non-"no such table" error propagates (never masked as zero)
# ---------------------------------------------------------------------------

def test_artifact_count_error_propagates_not_zero():
    db, _ = _open()
    _seed_wallet(db)
    # Make the COUNT(*) query on a PRESENT table fail with a non-absence error.
    # We corrupt the column name so SQLite raises "no such column" (a real
    # programming/schema error that must NOT be swallowed as zero).
    import sqlite3

    real_fetchone = db.fetchone

    def _boom(sql, params=None):
        if sql.strip().upper().startswith("SELECT COUNT(*)"):
            raise sqlite3.OperationalError("no such column: bogus_col")
        return real_fetchone(sql, params)

    db.fetchone = _boom
    # specialty_approvals present; its COUNT(*) will raise.
    with pytest.raises(sqlite3.OperationalError):
        refresh._count_artifacts(db)
    db.close()


# ---------------------------------------------------------------------------
# 30. Production dry-run is allowed read-only with --allow-live
# ---------------------------------------------------------------------------

def test_production_dry_run_allowed_readonly():
    # Safely patched production-path target (a temp v21 DB, but we exercise the
    # production-gate CODE path by monkeypatching is_production_db to True).
    import refresh_specialist_market_truth as _m
    real_is_prod = _m.is_production_db
    tmp = _temp_v21_db()
    _m.is_production_db = lambda p: True  # treat tmp as production for the gate
    try:
        opened = {"writable": 0, "readonly": 0}
        real_open_w = _m.open_writable
        real_open_r = _m.open_readonly

        def _fake_w(path, args=None):
            opened["writable"] += 1
            return real_open_w(path, args)

        def _fake_r(path):
            opened["readonly"] += 1
            return real_open_r(path)

        _m.open_writable = _fake_w
        _m.open_readonly = _fake_r
        try:
            rc = _m.main([
                "--db-path", str(tmp), "--market-source-id", COND,
                "--allow-live",  # dry-run, no --write
            ], provider=_provider_resolved())
            assert rc == 0, rc
            # Read-only used, writable NOT used.
            assert opened["readonly"] == 1, opened
            assert opened["writable"] == 0, opened
        finally:
            _m.open_writable = real_open_w
            _m.open_readonly = real_open_r
    finally:
        _m.is_production_db = real_is_prod


# ---------------------------------------------------------------------------
# 31. Unconfirmed production write touches no DB/provider symbol
# ---------------------------------------------------------------------------

def test_unconfirmed_production_write_touches_no_symbols():
    import refresh_specialist_market_truth as _m
    real_is_prod = _m.is_production_db
    tmp = _temp_v21_db()
    _m.is_production_db = lambda p: True  # production path
    try:
        calls = {"open_readonly": 0, "open_writable": 0,
                 "build_market_state_provider": 0}
        real_or = _m.open_readonly
        real_ow = _m.open_writable
        real_b = _m.build_market_state_provider

        def _or(path):
            calls["open_readonly"] += 1
            return real_or(path)

        def _ow(path, args=None):
            calls["open_writable"] += 1
            return real_ow(path, args)

        def _b():
            calls["build_market_state_provider"] += 1
            return real_b()

        _m.open_readonly = _or
        _m.open_writable = _ow
        _m.build_market_state_provider = _b
        # Also patch selector resolution to prove it does not run.
        real_validate = _m._validate_selector_readonly
        calls["_validate_selector_readonly"] = 0

        def _validate(args):
            calls["_validate_selector_readonly"] += 1
            return real_validate(args)

        _m._validate_selector_readonly = _validate
        try:
            rc = _m.main([
                "--db-path", str(tmp), "--market-source-id", COND,
                "--write",  # missing --allow-live / --confirm-production-db
            ], provider=_provider_resolved())
            assert rc != 0, "production write without full gates refused"
            assert rc == 2, rc
            # None of these symbols ran.
            assert calls["open_readonly"] == 0, calls
            assert calls["open_writable"] == 0, calls
            assert calls["build_market_state_provider"] == 0, calls
            assert calls["_validate_selector_readonly"] == 0, calls
        finally:
            _m.open_readonly = real_or
            _m.open_writable = real_ow
            _m.build_market_state_provider = real_b
            _m._validate_selector_readonly = real_validate
    finally:
        _m.is_production_db = real_is_prod


# ---------------------------------------------------------------------------
# SPECIALIST_REFRESH_SAFETY: bounded cohort, scheduling, and global lock
# ---------------------------------------------------------------------------

def test_watch_id_cohort_selects_five_active_watches_once_and_deterministically():
    db, _ = _open()
    watch_args = []
    markets = {}
    for i in range(5):
        wid = f"wallet-cohort-{i}"
        address = f"0xcohort{i}"
        watch = f"watch-cohort-{i}"
        condition = COND[:-1] + str(i)
        _seed_wallet(db, wid=wid, address=address)
        _seed_watch(db, wid=watch, wallet=wid, status="active")
        _insert_trade(db, f"cohort-{i}", condition=condition, trader=address)
        watch_args.extend(["--watch-id", watch])
        markets[condition] = _gamma_market(
            condition=condition, resolved=True, winner_token=TOK_WIN
        )
    provider = _FakeProvider(by_condition=markets)
    rc = refresh.main(
        ["--db-path", str(db.db_path), *watch_args, "--write", "--allow-live",
         "--confirm-production-db"], provider=provider
    )
    assert rc == 0
    assert provider.calls == sorted(markets)
    assert all(_row(db, f"cohort-{i}")["resolution_status"] == "won" for i in range(5))
    db.close()


def test_five_watch_cohort_selects_104_distinct_markets_and_excludes_noncohort():
    """Five active watches form one 104-market cohort; shared markets collapse."""
    db, _ = _open()
    watches = []
    addresses = []
    for wallet_index in range(5):
        wallet_id = f"wallet-104-{wallet_index}"
        address = f"0xcohort104{wallet_index}"
        watch_id = f"watch-104-{wallet_index}"
        _seed_wallet(db, wid=wallet_id, address=address)
        _seed_watch(db, wid=watch_id, wallet=wallet_id, status="active")
        watches.append(watch_id)
        addresses.append(address)

    # 103 unique cohort markets spread across all five watches.
    expected = set()
    for market_index in range(103):
        market_id = f"0x{market_index:064x}"
        expected.add(market_id)
        _insert_trade(
            db, f"cohort-104-{market_index}", condition=market_id,
            trader=addresses[market_index % len(addresses)],
        )

    # A single market observed by two watched wallets must be one selected ID.
    shared_market = f"0x{103:064x}"
    expected.add(shared_market)
    _insert_trade(db, "cohort-shared-a", condition=shared_market, trader=addresses[0])
    _insert_trade(db, "cohort-shared-b", condition=shared_market, trader=addresses[1])

    noncohort_market = f"0x{104:064x}"
    _insert_trade(db, "noncohort", condition=noncohort_market, trader="0xoutside")

    selected = select_markets_for_refresh(
        db, watch_ids=watches, limit_markets=500,
        now="2026-04-01T00:00:00+00:00",
    )
    assert selected == sorted(expected)
    assert len(selected) == 104
    assert selected.count(shared_market) == 1
    assert noncohort_market not in selected
    db.close()


def test_schedule_compares_legacy_timestamp_offsets_as_instants():
    db, _ = _open()
    _seed_wallet(db)
    now = "2026-04-02T00:00:00+00:00"
    schedules = {
        "z-due": "2026-04-01T23:00:00Z",
        "z-not-due": "2026-04-02T01:00:00Z",
        "zero-due": "2026-04-02T00:00:00+00:00",
        "zero-not-due": "2026-04-02T00:01:00+00:00",
        "offset-due": "2026-04-02T02:00:00+03:00",
        "offset-not-due": "2026-04-02T04:00:00+03:00",
    }
    due = {"z-due", "zero-due", "offset-due"}
    condition_for_trade = {}
    for index, (trade_id, next_check_after) in enumerate(schedules.items()):
        condition = f"0x{index + 200:064x}"
        condition_for_trade[trade_id] = condition
        _insert_trade(db, trade_id, condition=condition)
        db.conn.execute(
            "INSERT INTO specialist_market_refresh_state "
            "(market_source_id, last_status, attempt_count, next_check_after) "
            "VALUES (?, 'unresolved', 1, ?)",
            (condition, next_check_after),
        )
    db.conn.commit()

    selected = set(select_markets_for_refresh(
        db, wallet_address=ADDR, limit_markets=100, now=now,
    ))
    assert selected == {condition_for_trade[trade_id] for trade_id in due}
    db.close()


def test_bookkeeping_writes_canonical_utc_timestamps(monkeypatch):
    db, _ = _open()
    offset_now = datetime(2026, 4, 1, 10, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    monkeypatch.setattr(refresh, "_utcnow", lambda: offset_now)
    outcome = refresh.MarketRefreshOutcome(COND)
    outcome.last_status = "unresolved"
    refresh._upsert_bookkeeping(db, outcome)
    state = dict(db.conn.execute(
        "SELECT last_checked_at, next_check_after FROM specialist_market_refresh_state "
        "WHERE market_source_id=?", (COND,),
    ).fetchone())
    assert state == {
        "last_checked_at": "2026-04-01T05:00:00+00:00",
        "next_check_after": "2026-04-02T05:00:00+00:00",
    }
    db.close()


def test_write_report_keeps_initial_markets_selected_after_resolution(capsys):
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "report-selected", condition=COND)
    assert refresh.main([
        "--db-path", str(db.db_path), "--market-source-id", COND,
        "--write", "--allow-live", "--confirm-production-db", "--json",
    ], provider=_provider_resolved()) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["markets_selected"] == 1
    assert report["updated"] == 1
    db.close()


def test_watch_id_cohort_refuses_duplicate_six_and_inactive_before_provider():
    db, _ = _open()
    _seed_wallet(db)
    _seed_watch(db, wid=WATCH, wallet=WID, status="active")
    _seed_watch(db, wid=WATCH_PAUSED, wallet=WID, status="paused")
    _insert_trade(db, "t1")
    provider = _provider_resolved()
    base = ["--db-path", str(db.db_path), "--allow-live", "--write",
            "--confirm-production-db"]
    assert refresh.main([*base, "--watch-id", WATCH, "--watch-id", WATCH], provider=provider) == 2
    six_watch_args = [
        value for i in range(6) for value in ("--watch-id", f"six-{i}")
    ]
    assert refresh.main([*base, *six_watch_args], provider=provider) == 2
    assert refresh.main([*base, "--watch-id", WATCH, "--watch-id", WATCH_PAUSED], provider=provider) == 2
    assert provider.calls == []
    assert _row(db, "t1")["resolution_status"] == "unresolved"
    db.close()


def test_terminal_and_next_check_after_suppress_provider_and_retry_schedule(monkeypatch):
    db, _ = _open()
    _seed_wallet(db)
    frozen = datetime(2026, 4, 1, tzinfo=UTC)
    monkeypatch.setattr(refresh, "_utcnow", lambda: frozen)
    _insert_trade(db, "terminal", condition=COND, status="won", winner=TOK_WIN)
    _insert_trade(db, "pending", condition=COND[:-1] + "d")
    pending = COND[:-1] + "d"
    first = _FakeProvider(by_condition={pending: _gamma_market(
        condition=pending, resolved=False, winner_token=None)})
    assert refresh.main(["--db-path", str(db.db_path), "--wallet-id", WID,
                         "--write", "--allow-live", "--confirm-production-db"], provider=first) == 0
    assert first.calls == [pending]
    state = dict(db.conn.execute(
        "SELECT attempt_count, next_check_after FROM specialist_market_refresh_state WHERE market_source_id=?",
        (pending,)).fetchone())
    assert state["attempt_count"] == 1
    assert state["next_check_after"] == (frozen + timedelta(hours=24)).isoformat()
    early = _FakeProvider()
    assert refresh.main(["--db-path", str(db.db_path), "--wallet-id", WID,
                         "--write", "--allow-live", "--confirm-production-db"], provider=early) == 0
    assert early.calls == []
    monkeypatch.setattr(refresh, "_utcnow", lambda: frozen + timedelta(hours=25))
    unavailable = _FakeProvider(by_condition={pending: None})
    assert refresh.main(["--db-path", str(db.db_path), "--wallet-id", WID,
                         "--write", "--allow-live", "--confirm-production-db"], provider=unavailable) == 0
    state = dict(db.conn.execute(
        "SELECT attempt_count, last_status, next_check_after FROM specialist_market_refresh_state WHERE market_source_id=?",
        (pending,)).fetchone())
    assert state["attempt_count"] == 1 and state["last_status"] == "unavailable"
    assert state["next_check_after"] == (frozen + timedelta(hours=97)).isoformat()
    outcome = refresh.MarketRefreshOutcome(pending)
    outcome.last_status, outcome.last_error = "provider_unavailable", "provider_error:RuntimeError"
    refresh._upsert_bookkeeping(db, outcome)
    db.commit()
    state = dict(db.conn.execute(
        "SELECT attempt_count, last_status, last_error, next_check_after FROM specialist_market_refresh_state WHERE market_source_id=?",
        (pending,)).fetchone())
    assert state["attempt_count"] == 2
    assert state["last_status"] == "provider_unavailable"
    assert state["last_error"] == "provider_error:RuntimeError"
    assert state["next_check_after"] == (frozen + timedelta(hours=49)).isoformat()
    db.close()


@pytest.mark.parametrize(
    ("last_status", "prior_attempts", "expected_attempts", "expected_delay"),
    [
        ("resolved", 0, 1, None),
        ("unresolved", 2, 3, 24),
        ("unresolved", 3, 4, 24),
        ("unavailable", 0, 1, 72),
        ("unavailable", 2, 1, 72),
        ("provider_unavailable", 0, 1, 24),
        ("provider_unavailable", 2, 1, 24),
        ("routing_http_error", 2, 1, 24),
    ],
)
def test_bookkeeping_applies_status_specific_retry_policy(
    monkeypatch, last_status, prior_attempts, expected_attempts, expected_delay
):
    """Unresolved has no attempt ceiling; failures retain their bounded budget."""
    db, _ = _open()
    frozen = datetime(2026, 4, 1, tzinfo=UTC)
    monkeypatch.setattr(refresh, "_utcnow", lambda: frozen)
    market_id = f"policy-{last_status}-{prior_attempts}"
    if prior_attempts:
        db.conn.execute(
            "INSERT INTO specialist_market_refresh_state "
            "(market_source_id, last_status, attempt_count, next_check_after) "
            "VALUES (?, 'unresolved', ?, NULL)",
            (market_id, prior_attempts),
        )
    outcome = refresh.MarketRefreshOutcome(market_id)
    outcome.last_status = last_status
    outcome.last_error = "honest-error" if last_status not in {"resolved", "unresolved"} else None
    refresh._upsert_bookkeeping(db, outcome)
    state = dict(db.conn.execute(
        "SELECT attempt_count, last_status, last_error, next_check_after "
        "FROM specialist_market_refresh_state WHERE market_source_id=?",
        (market_id,),
    ).fetchone())
    assert state["attempt_count"] == expected_attempts
    assert state["last_status"] == last_status
    assert state["last_error"] == outcome.last_error
    expected_deadline = (
        refresh._TERMINAL_NEXT_CHECK_AFTER
        if expected_delay is None
        else (frozen + timedelta(hours=expected_delay)).isoformat()
    )
    assert state["next_check_after"] == expected_deadline
    db.close()


@pytest.mark.parametrize(
    "failure_status",
    ["unavailable", "provider_unavailable", "routing_http_error"],
)
def test_bookkeeping_caps_failure_after_unresolved_observations(
    monkeypatch, failure_status
):
    """A failure after unresolved starts a fresh consecutive failure budget."""
    db, _ = _open()
    frozen = datetime(2026, 4, 1, tzinfo=UTC)
    monkeypatch.setattr(refresh, "_utcnow", lambda: frozen)
    market_id = f"prior-unresolved-{failure_status}"
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state "
        "(market_source_id, last_status, attempt_count, next_check_after) "
        "VALUES (?, 'unresolved', 3, NULL)",
        (market_id,),
    )
    outcome = refresh.MarketRefreshOutcome(market_id)
    outcome.last_status = failure_status
    outcome.last_error = f"honest-{failure_status}-error"
    refresh._upsert_bookkeeping(db, outcome)
    state = dict(db.conn.execute(
        "SELECT attempt_count, last_status, last_error, next_check_after "
        "FROM specialist_market_refresh_state WHERE market_source_id=?",
        (market_id,),
    ).fetchone())
    assert state["attempt_count"] == 1
    assert state["last_status"] == failure_status
    assert state["last_error"] == outcome.last_error
    assert state["next_check_after"] == (
        frozen + timedelta(hours=72 if failure_status == "unavailable" else 24)
    ).isoformat()
    db.close()


@pytest.mark.parametrize(
    ("status", "attempts", "next_check_after", "selected"),
    [
        ("unresolved", 3, "2026-03-31T23:59:59+00:00", True),
        ("unresolved", 4, None, True),
        ("unresolved", 4, "2026-04-01T01:00:00+00:00", False),
        ("unavailable", 3, None, False),
        ("provider_unavailable", 3, "2026-03-31T23:59:59+00:00", False),
        ("routing_http_error", 3, None, False),
    ],
)
def test_selector_preserves_legacy_unresolved_but_suppresses_failure_ceiling(
    status, attempts, next_check_after, selected
):
    db, _ = _open()
    _seed_wallet(db)
    market_id = f"0x{attempts:063x}{len(status):x}"
    _insert_trade(db, f"legacy-{status}-{next_check_after}", condition=market_id)
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state "
        "(market_source_id, last_status, attempt_count, next_check_after) "
        "VALUES (?, ?, ?, ?)",
        (market_id, status, attempts, next_check_after),
    )
    db.conn.commit()
    actual = select_markets_for_refresh(
        db, wallet_address=ADDR, limit_markets=100,
        now="2026-04-01T00:00:00+00:00",
    )
    assert (market_id in actual) is selected
    db.close()


def test_legacy_null_refresh_state_terminal_markets_skip_cli_provider_call():
    """Legacy terminal state remains terminal even without a retry deadline."""
    db, _ = _open()
    _seed_wallet(db)
    attempt_terminal = COND[:-1] + "e"
    resolved_terminal = COND[:-1] + "f"
    _insert_trade(db, "legacy-attempt", condition=attempt_terminal)
    _insert_trade(db, "legacy-resolved", condition=resolved_terminal)
    db.conn.executemany(
        "INSERT INTO specialist_market_refresh_state "
        "(market_source_id, last_status, attempt_count, next_check_after) "
        "VALUES (?, ?, ?, NULL)",
        [
            (attempt_terminal, "provider_unavailable", 3),
            (resolved_terminal, "resolved", 1),
        ],
    )
    db.conn.commit()
    provider = _FakeProvider(by_condition={
        attempt_terminal: _gamma_market(
            condition=attempt_terminal, resolved=True, winner_token=TOK_WIN
        ),
        resolved_terminal: _gamma_market(
            condition=resolved_terminal, resolved=True, winner_token=TOK_WIN
        ),
    })

    rc = refresh.main([
        "--db-path", str(db.db_path), "--wallet-id", WID,
        "--write", "--allow-live", "--confirm-production-db",
    ], provider=provider)

    assert rc == 0
    assert provider.calls == [resolved_terminal]
    assert _row(db, "legacy-attempt")["resolution_status"] == "unresolved"
    assert _row(db, "legacy-resolved")["resolution_status"] == "won"
    db.close()


def test_periodic_selection_uses_canonical_truth_and_bounded_conflict_retry():
    db, _ = _open()
    _seed_wallet(db)
    unresolved_legacy_resolved = "0x" + "1" * 64
    unresolved_conflict = "0x" + "2" * 64
    clean_terminal = "0x" + "3" * 64
    exhausted_conflict = "0x" + "4" * 64
    _insert_trade(db, "canonical-unresolved-resolved-bk", condition=unresolved_legacy_resolved)
    _insert_trade(db, "canonical-unresolved-conflict-bk", condition=unresolved_conflict)
    _insert_trade(db, "canonical-clean-terminal", condition=clean_terminal,
                  status="won", winner=TOK_WIN)
    _insert_trade(db, "canonical-terminal-conflict-exhausted", condition=exhausted_conflict,
                  status="won", winner=TOK_WIN)
    for market, status, error, attempts in (
        (unresolved_legacy_resolved, "resolved", None, 1),
        (unresolved_conflict, "resolved", "conflict", 1),
        (clean_terminal, "resolved", None, 1),
        (exhausted_conflict, "resolved", "conflict", 3),
    ):
        db.conn.execute(
            "INSERT INTO specialist_market_refresh_state "
            "(market_source_id, last_checked_at, last_status, last_error, "
            "attempt_count, next_check_after) VALUES (?, ?, ?, ?, ?, ?)",
            (market, "2026-04-01T00:00:00Z", status, error, attempts,
             "2026-03-31T00:00:00Z"),
        )
    db.conn.commit()
    selected = select_markets_for_refresh(
        db, wallet_address=ADDR, now="2026-04-02T00:00:00Z", limit_markets=20,
    )
    assert unresolved_legacy_resolved in selected
    assert unresolved_conflict in selected
    assert clean_terminal not in selected
    assert exhausted_conflict not in selected
    db.close()


def test_selection_prioritizes_unseen_then_oldest_due_deterministically():
    db, _ = _open()
    _seed_wallet(db)
    unseen = "0x" + "f" * 64
    old_high = "0x" + "e" * 64
    old_low = "0x" + "1" * 64
    newer = "0x" + "2" * 64
    for index, market in enumerate((unseen, old_high, old_low, newer)):
        _insert_trade(db, f"fair-{index}", condition=market)
    for market, checked in (
        (old_high, "2026-03-01T00:00:00Z"),
        (old_low, "2026-03-01T00:00:00Z"),
        (newer, "2026-03-15T00:00:00Z"),
    ):
        db.conn.execute(
            "INSERT INTO specialist_market_refresh_state "
            "(market_source_id, last_checked_at, last_status, attempt_count, "
            "next_check_after) VALUES (?, ?, 'unresolved', 1, ?)",
            (market, checked, "2026-03-20T00:00:00Z"),
        )
    db.conn.commit()
    selected = select_markets_for_refresh(
        db, wallet_address=ADDR, now="2026-04-02T00:00:00Z", limit_markets=3,
    )
    assert selected == [unseen, old_low, old_high]
    db.close()


def test_bounded_periodic_selection_rotates_backlog_across_cycles():
    """Each bounded due cycle covers unseen backlog before fresh selections."""
    db, _ = _open()
    _seed_wallet(db)
    markets = [
        "0x" + "01" * 32,
        "0x" + "02" * 32,
        "0x" + "f0" * 32,
        "0x" + "f1" * 32,
        "0x" + "ff" * 32,
    ]
    for index, market in enumerate(markets):
        _insert_trade(db, f"cycle-{index}", condition=market)

    now = "2026-04-01T00:00:00+00:00"
    first = select_markets_for_refresh(
        db, wallet_address=ADDR, now=now, limit_markets=2,
    )
    assert first == markets[:2]

    def mark_checked(selected, checked_at):
        for market in selected:
            db.conn.execute(
                "INSERT INTO specialist_market_refresh_state "
                "(market_source_id, last_checked_at, last_status, attempt_count, "
                "next_check_after) VALUES (?, ?, 'unresolved', 1, ?) "
                "ON CONFLICT(market_source_id) DO UPDATE SET "
                "last_checked_at=excluded.last_checked_at, "
                "last_status=excluded.last_status, "
                "attempt_count=excluded.attempt_count, "
                "next_check_after=excluded.next_check_after",
                (market, checked_at, "2026-04-01T01:00:00+00:00"),
            )
        db.conn.commit()

    mark_checked(first, now)
    second = select_markets_for_refresh(
        db, wallet_address=ADDR, now="2026-04-02T00:00:00+00:00", limit_markets=2,
    )
    assert second == markets[2:4]
    mark_checked(second, "2026-04-02T00:00:00+00:00")

    third = select_markets_for_refresh(
        db, wallet_address=ADDR, now="2026-04-03T00:00:00+00:00", limit_markets=2,
    )
    # The final unseen market wins before the freshly checked second-cycle
    # rows; equal first-cycle timestamps use normalized ID deterministically.
    assert third == [markets[4], markets[0]]
    assert select_markets_for_refresh(
        db, wallet_address=ADDR, now="2026-04-03T00:00:00+00:00", limit_markets=2,
    ) == third
    db.close()


def test_lock_refusal_prevents_provider_and_mutation_and_success_releases(monkeypatch):
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "t1")
    provider = _provider_resolved()
    monkeypatch.setattr(
        refresh, "operational_job_lock",
        lambda *_a, **_k: (_ for _ in ()).throw(refresh.LockError("held")),
    )
    rc = refresh.main(["--db-path", str(db.db_path), "--market-source-id", COND,
                       "--write", "--allow-live", "--confirm-production-db"], provider=provider)
    assert rc == 2 and provider.calls == []
    assert _row(db, "t1")["resolution_status"] == "unresolved"
    assert db.conn.execute("SELECT COUNT(*) FROM specialist_market_refresh_state").fetchone()[0] == 0

    events = []
    class _Lock:
        def __enter__(self):
            events.append("entered")
            return self
        def __exit__(self, *_exc):
            events.append("released")
    monkeypatch.setattr(refresh, "operational_job_lock", lambda *_a, **_k: _Lock())
    assert refresh.main(["--db-path", str(db.db_path), "--market-source-id", COND,
                         "--allow-live", "--lock-timeout", "0"], provider=provider) == 0
    assert events == ["entered", "released"]
    assert _row(db, "t1")["resolution_status"] == "unresolved"  # dry-run gate preserved
    db.close()


# ---------------------------------------------------------------------------
# Sanity: accepted source set matches S3 exactly
# ---------------------------------------------------------------------------

def test_accepted_source_set_matches_s3():
    assert SPECIALIST_REFRESH_SOURCES == frozenset({SOURCE_NAME, "polymarket_clob"})
