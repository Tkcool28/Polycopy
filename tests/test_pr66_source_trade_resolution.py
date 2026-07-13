"""PR66 Checkpoint 3 routing-correction focused tests.

Covers the corrected live market-resolution routing used by the
source-trade resolver:

  * condition ID -> GET /markets?condition_ids=<hex> (NOT numeric path)
  * numeric Gamma ID -> GET /markets/{id}
  * token ID alone -> missing_market_identity (not routable)
  * invalid route HTTP error -> routing_http_error (NOT malformed_payload)
  * valid unresolved -> unresolved; valid resolved single-winner -> truth
  * ambiguous -> non-writable
  * same condition id queried once across many trades (dedup + caching)
  * BUY still uses frozen helper unchanged
  * SELL remains documentation-only

No real network. The trusted provider is faked via an async ``get_market``
stub returning scripted Market objects, so we exercise
``derive_winner_from_market_payload`` exactly as the live path would.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from polycopy.domain.market import Market, MarketOutcome  # noqa: E402
from polycopy.ingestion.source_trade_resolution import (  # noqa: E402
    classify_market_identity,
    resolve_source_trades,
    _route_type_for,
)


# ---------------------------------------------------------------------------
# Fake market-state provider (no network); exposes get_market
# ---------------------------------------------------------------------------


class _HttpError(Exception):
    """Minimal stand-in for httpx.HTTPStatusError."""

    def __init__(self, status: int) -> None:
        self.response = type("R", (), {"status_code": status})()
        super().__init__(f"HTTP {status}")


class FakeMarketStateProvider:
    """Async stub returning scripted Market objects / scripted errors.

    Routing is explicit by identifier so tests prove which route was used:
      * key is a condition id -> condition route
      * key is a numeric id   -> numeric route
    ``route_log`` records every get_market call (identifier + which route).
    """

    def __init__(
        self,
        by_condition: dict[str, Any] | None = None,
        by_numeric: dict[str, Any] | None = None,
    ) -> None:
        self._cond = by_condition or {}
        self._num = by_numeric or {}
        self.route_log: list[tuple[str, str]] = []

    async def get_market(self, market_id: str) -> Optional[Market]:
        rt = _route_type_for(market_id)
        self.route_log.append((market_id, rt))
        if rt == "gamma_condition_id":
            val = self._cond.get(market_id)
        elif rt == "gamma_numeric_id":
            val = self._num.get(market_id)
        else:
            val = None
        if isinstance(val, Exception):
            raise val
        return val


def _gamma_market(
    *,
    condition_id: str,
    resolved: bool,
    closed: bool = True,
    outcome_label: Optional[str] = "Yes",
    winner_token: Optional[str] = "win-tok",
    loser_token: str = "lose-tok",
) -> Market:
    outcomes = [
        MarketOutcome(label=outcome_label or "Yes", price=0.5, clob_token_id=winner_token),
        MarketOutcome(label="No", price=0.5, clob_token_id=loser_token),
    ]
    return Market(
        source_id=condition_id,
        question="test",
        outcomes=outcomes,
        source="polymarket",
        active=False,
        closed=closed,
        resolved=resolved,
        resolution_outcome=outcome_label,
        fetched_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# In-memory schema (v17-minimal: source_trades + index)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE source_trades (
    id TEXT PRIMARY KEY,
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
    settlement_source TEXT,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_trades_wallet_timestamp
    ON source_trades(trader_address, timestamp);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


WALLET = "0xabc1230000000000000000000000000000000000"


def _insert(c: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    cols = [
        "id", "source", "source_trade_id", "market_source_id", "side",
        "outcome", "quantity", "price", "trader_address", "timestamp",
        "is_sample", "token_id", "resolution_status", "resolved_at",
        "winning_token_id", "is_winning_trade", "realized_pnl",
        "settlement_source", "metadata_json",
    ]
    placeholders = ",".join("?" for _ in cols)
    for r in rows:
        c.execute(
            f"INSERT INTO source_trades ({','.join(cols)}) VALUES ({placeholders})",
            tuple(r.get(col) for col in cols),
        )


def _row(c: sqlite3.Connection, rid: str) -> dict[str, Any]:
    return dict(c.execute("SELECT * FROM source_trades WHERE id=?", (rid,)).fetchone())


# ===========================================================================
# Routing contract
# ===========================================================================


def test_condition_id_uses_condition_route_not_numeric():
    c = _conn()
    cond = "0x" + "a" * 64
    _insert(c, [
        dict(id="r1", source="poly", source_trade_id="s1", market_source_id=cond,
             side="BUY", quantity=10, price=0.4, trader_address=WALLET,
             timestamp="2026-01-01T00:00:00Z", is_sample=0, token_id="win-tok",
             resolution_status="unresolved"),
    ])
    fake = FakeMarketStateProvider(by_condition={cond: _gamma_market(condition_id=cond, resolved=True)})
    resolve_source_trades(c, provider=fake, wallet=WALLET, limit=10, apply=False)
    assert fake.route_log == [(cond, "gamma_condition_id")]


def test_numeric_id_uses_numeric_route():
    c = _conn()
    num = "123456"
    cond = "0x" + "b" * 64
    mkt = _gamma_market(condition_id=cond, resolved=True)
    mkt.source_id = num  # numeric id market carries numeric source id
    _insert(c, [
        dict(id="r1", source="poly", source_trade_id="s1", market_source_id=num,
             side="BUY", quantity=10, price=0.4, trader_address=WALLET,
             timestamp="2026-01-01T00:00:00Z", is_sample=0, token_id="win-tok",
             resolution_status="unresolved"),
    ])
    fake = FakeMarketStateProvider(by_numeric={num: mkt})
    resolve_source_trades(c, provider=fake, wallet=WALLET, limit=10, apply=False)
    assert fake.route_log == [(num, "gamma_numeric_id")]


def test_token_id_only_is_missing_market_identity():
    c = _conn()
    _insert(c, [
        dict(id="r1", source="poly", source_trade_id="s1", market_source_id=None,
             side="BUY", quantity=10, price=0.4, trader_address=WALLET,
             timestamp="2026-01-01T00:00:00Z", is_sample=0, token_id="727532957275666592",
             resolution_status="unresolved"),
    ])
    fake = FakeMarketStateProvider()
    rep = resolve_source_trades(c, provider=fake, wallet=WALLET, limit=10, apply=False)
    assert rep.missing_market_identity == 1
    # Provider was never called with a bare token id.
    assert fake.route_log == []


def test_invalid_route_http_error_is_routing_http_error_not_malformed():
    c = _conn()
    cond = "0x" + "c" * 64
    _insert(c, [
        dict(id="r1", source="poly", source_trade_id="s1", market_source_id=cond,
             side="BUY", quantity=10, price=0.4, trader_address=WALLET,
             timestamp="2026-01-01T00:00:00Z", is_sample=0, token_id="win-tok",
             resolution_status="unresolved"),
    ])
    # Gamma returns 422 for a hex condition id sent to the numeric route, or
    # 404 for an unknown condition id — both are routing errors, NOT malformed
    # market truth.
    fake = FakeMarketStateProvider(by_condition={cond: _HttpError(422)})
    rep = resolve_source_trades(c, provider=fake, wallet=WALLET, limit=10, apply=False)
    assert rep.routing_http_error == 1
    assert rep.malformed_payload == 0
    assert rep.errors[0]["error_type"] == "routing_http_error"
    assert rep.errors[0]["http_status"] == "422"
    assert rep.errors[0]["route_type"] == "gamma_condition_id"


def test_provider_unavailable_distinguished_from_routing_error():
    c = _conn()
    cond = "0x" + "d" * 64
    _insert(c, [
        dict(id="r1", source="poly", source_trade_id="s1", market_source_id=cond,
             side="BUY", quantity=10, price=0.4, trader_address=WALLET,
             timestamp="2026-01-01T00:00:00Z", is_sample=0, token_id="win-tok",
             resolution_status="unresolved"),
    ])
    fake = FakeMarketStateProvider(by_condition={cond: RuntimeError("boom")})
    rep = resolve_source_trades(c, provider=fake, wallet=WALLET, limit=10, apply=False)
    assert rep.provider_unavailable == 1
    assert rep.routing_http_error == 0
    assert rep.malformed_payload == 0
    assert rep.errors[0]["error_type"] == "provider_unavailable"


# ===========================================================================
# Resolution outcomes
# ===========================================================================


def test_valid_unresolved_response_classified_unresolved():
    c = _conn()
    cond = "0x" + "e" * 64
    _insert(c, [
        dict(id="r1", source="poly", source_trade_id="s1", market_source_id=cond,
             side="BUY", quantity=10, price=0.4, trader_address=WALLET,
             timestamp="2026-01-01T00:00:00Z", is_sample=0, token_id="win-tok",
             resolution_status="unresolved"),
    ])
    fake = FakeMarketStateProvider(by_condition={cond: _gamma_market(condition_id=cond, resolved=False)})
    rep = resolve_source_trades(c, provider=fake, wallet=WALLET, limit=10, apply=False)
    assert rep.unresolved == 1
    assert rep.updated == 0


def test_valid_resolved_single_winner_produces_truth_and_pnl():
    c = _conn()
    cond = "0x" + "f" * 64
    _insert(c, [
        dict(id="r1", source="poly", source_trade_id="s1", market_source_id=cond,
             side="BUY", quantity=10, price=0.4, trader_address=WALLET,
             timestamp="2026-01-01T00:00:00Z", is_sample=0, token_id="win-tok",
             resolution_status="unresolved"),
    ])
    fake = FakeMarketStateProvider(by_condition={cond: _gamma_market(condition_id=cond, resolved=True, winner_token="win-tok", loser_token="lose-tok")})
    rep = resolve_source_trades(c, provider=fake, wallet=WALLET, limit=10, apply=True)
    assert rep.updated == 1
    row = _row(c, "r1")
    assert row["resolution_status"] == "won"
    assert row["is_winning_trade"] == 1
    assert row["winning_token_id"] == "win-tok"
    assert abs(row["realized_pnl"] - 6.0) < 1e-9  # frozen helper: (1-0.4)*10


def test_ambiguous_payload_remains_non_writable():
    c = _conn()
    cond = "0x" + "1" * 64
    mkt = Market(
        source_id=cond, question="t",
        outcomes=[
            MarketOutcome(label="Yes", price=0.5, clob_token_id="win-tok"),
            MarketOutcome(label="Yes", price=0.5, clob_token_id="win-tok2"),
        ],
        source="polymarket", active=False, closed=True, resolved=True,
        resolution_outcome="Yes", fetched_at=datetime.now(timezone.utc),
    )
    _insert(c, [
        dict(id="r1", source="poly", source_trade_id="s1", market_source_id=cond,
             side="BUY", quantity=10, price=0.4, trader_address=WALLET,
             timestamp="2026-01-01T00:00:00Z", is_sample=0, token_id="win-tok",
             resolution_status="unresolved"),
    ])
    fake = FakeMarketStateProvider(by_condition={cond: mkt})
    rep = resolve_source_trades(c, provider=fake, wallet=WALLET, limit=10, apply=True)
    assert rep.ambiguous == 1
    assert rep.updated == 0
    assert _row(c, "r1")["resolution_status"] == "unresolved"


def test_same_condition_id_queried_once_across_trades():
    c = _conn()
    cond = "0x" + "2" * 64
    _insert(c, [
        dict(id="r1", source="poly", source_trade_id="s1", market_source_id=cond,
             side="BUY", quantity=10, price=0.4, trader_address=WALLET,
             timestamp="2026-01-01T00:00:00Z", is_sample=0, token_id="win-tok",
             resolution_status="unresolved"),
        dict(id="r2", source="poly", source_trade_id="s2", market_source_id=cond,
             side="BUY", quantity=5, price=0.3, trader_address=WALLET,
             timestamp="2026-01-02T00:00:00Z", is_sample=0, token_id="win-tok",
             resolution_status="unresolved"),
    ])
    fake = FakeMarketStateProvider(by_condition={cond: _gamma_market(condition_id=cond, resolved=True)})
    rep = resolve_source_trades(c, provider=fake, wallet=WALLET, limit=10, apply=False)
    assert rep.unique_markets_checked == 1
    assert rep.provider_calls == 1
    assert fake.route_log.count((cond, "gamma_condition_id")) == 1


# ===========================================================================
# BUY / SELL unchanged
# ===========================================================================


def test_buy_uses_frozen_helper_unchanged():
    c = _conn()
    cond = "0x" + "3" * 64
    _insert(c, [
        dict(id="r1", source="poly", source_trade_id="s1", market_source_id=cond,
             side="BUY", quantity=10, price=0.4, trader_address=WALLET,
             timestamp="2026-01-01T00:00:00Z", is_sample=0, token_id="lose-tok",
             resolution_status="unresolved"),
    ])
    fake = FakeMarketStateProvider(by_condition={cond: _gamma_market(condition_id=cond, resolved=True, winner_token="win-tok", loser_token="lose-tok")})
    rep = resolve_source_trades(c, provider=fake, wallet=WALLET, limit=10, apply=True)
    assert rep.updated == 1
    row = _row(c, "r1")
    assert row["resolution_status"] == "lost"
    assert row["is_winning_trade"] == 0
    assert abs(row["realized_pnl"] - (-4.0)) < 1e-9  # frozen: -0.4*10


def test_sell_remains_documentation_only():
    c = _conn()
    cond = "0x" + "4" * 64
    _insert(c, [
        dict(id="r1", source="poly", source_trade_id="s1", market_source_id=cond,
             side="SELL", quantity=99, price=0.99, trader_address=WALLET,
             timestamp="2026-01-01T00:00:00Z", is_sample=0, token_id="win-tok",
             resolution_status="unresolved"),
    ])
    fake = FakeMarketStateProvider(by_condition={cond: _gamma_market(condition_id=cond, resolved=True)})
    rep = resolve_source_trades(c, provider=fake, wallet=WALLET, limit=10, apply=True)
    assert rep.unsupported_sell_accounting == 1
    assert rep.updated == 0
    row = _row(c, "r1")
    assert row["resolution_status"] == "unresolved"
    assert row["is_winning_trade"] is None
    assert row["realized_pnl"] is None


# ===========================================================================
# Routing helpers + identity
# ===========================================================================


def test_route_type_classification():
    assert _route_type_for("0x" + "a" * 64) == "gamma_condition_id"
    assert _route_type_for("12345") == "gamma_numeric_id"
    assert _route_type_for("sample-market-001") == "unsupported"
    assert _route_type_for("0xSHORT") == "unsupported"


def test_classify_market_identity_uses_condition_or_numeric():
    assert classify_market_identity({"market_source_id": "0x" + "a" * 64}) == "0x" + "a" * 64
    assert classify_market_identity({"market_source_id": "12345"}) == "12345"
    assert classify_market_identity({"market_source_id": None}) is None
    assert classify_market_identity({"market_source_id": "  "}) is None
    # token_id alone is NOT routable
    assert classify_market_identity({"market_source_id": None, "token_id": "727..."}) is None
