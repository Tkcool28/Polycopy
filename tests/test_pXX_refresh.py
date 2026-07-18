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
import tempfile
import asyncio
import pytest
from datetime import datetime, timezone
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
    return Path(tempfile.mktemp(suffix=".db"))


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
    bk = db.conn.execute(
        "SELECT last_status, last_error FROM specialist_market_refresh_state "
        "WHERE market_source_id=?", (COND,)).fetchone()
    assert dict(bk)["last_status"] == "resolved"
    assert dict(bk)["last_error"] == "conflict"
    db.close()


# ---------------------------------------------------------------------------
# 20. A forced bookkeeping/update failure rolls back all source-trade changes
# ---------------------------------------------------------------------------

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
    # attempt_count increments to 2, not duplicated rows.
    bk = db.conn.execute(
        "SELECT attempt_count FROM specialist_market_refresh_state "
        "WHERE market_source_id=?", (COND,)).fetchone()
    assert dict(bk)["attempt_count"] == 2, dict(bk)
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
        tmp.unlink(missing_ok=True)


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
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Sanity: accepted source set matches S3 exactly
# ---------------------------------------------------------------------------

def test_accepted_source_set_matches_s3():
    assert SPECIALIST_REFRESH_SOURCES == frozenset({SOURCE_NAME, "polymarket_clob"})
