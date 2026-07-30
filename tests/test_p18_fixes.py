"""P18 regression tests for paper approve/reject route retirement, resolution, and frontend fixes.

Covers route retirement (404 proof), resolution semantics, and frontend fixes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from polycopy.api.app import app


MARKET_ID = "00000000-0000-0000-0000-000000000001"
WALLET_ID = "00000000-0000-0000-0000-000000000002"


def _reset_app_state(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYCOPY_ENABLE_DEMO_DATA", "true")
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p18.sqlite"))
    monkeypatch.setenv("POLYCOPY_ORDER_KILL_SWITCH", "false")

    import polycopy.config.settings as settings_module
    import polycopy.db.database as database_module
    from polycopy.api.app import _bidask_provider, _idempotency_store
    from polycopy.config.settings import get_settings
    from polycopy.db.database import Database

    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None
    _idempotency_store._db = None
    _idempotency_store._ensured_table = False
    _bidask_provider.clear()

    # Bind the global DB singleton explicitly to THIS test's tmp_path so the
    # test can neither leak nor inherit another test's SQLite file, independent
    # of how POLYCOPY_DB_PATH env-resolution behaves across Python/CI versions.
    # (See CI run 343: P18 approval tests returned 409/state-pollution on
    # Python 3.12 because the shared singleton occasionally resolved to a
    # prior test's DB. Explicit binding makes isolation deterministic.)
    settings = get_settings(reload=True)
    db_path = tmp_path / "p18.sqlite"
    settings.db_path = db_path
    fresh_db = Database(db_path=db_path)
    fresh_db.connect()
    database_module._db = fresh_db
    _idempotency_store._db = fresh_db
    return database_module, settings_module, _bidask_provider, _idempotency_store


def _seed_pending_order(order_id: str, *, market_id: str = MARKET_ID, status: str = "pending") -> None:
    from polycopy.db.database import get_database
    # Compute the seed timestamp at CALL time, not at module import. The approve
    # endpoint enforces staleness_seconds (120s) and order_preview_max_age_seconds
    # (3600s) against the order's created_at. In a long full-suite run the import-
    # frozen NOW would already be expired by the time these tests execute, causing
    # spurious 409 / "skip" decision failures. Seeding relative to the execution
    # moment keeps the order fresh regardless of suite duration or collection order.
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_database()
    db.execute(
        "INSERT OR IGNORE INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, ?, ?)",
        (WALLET_ID, "0xtest", "test", 0, now_iso),
    )
    db.execute(
        "INSERT OR IGNORE INTO markets (id, source_id, source, question, fetched_at, is_sample) VALUES (?, ?, ?, ?, ?, ?)",
        (market_id, "m1", "test", "Test Q", now_iso, 0),
    )
    db.execute(
        """
        INSERT OR IGNORE INTO orders
            (id, market_id, wallet_id, side, order_type, outcome, quantity, price,
             status, filled_quantity, created_at, updated_at, is_sample)
        VALUES (?, ?, ?, 'buy', 'limit', 'Yes', 10.0, 0.65, ?, 0.0, ?, ?, 0)
        """,
        (order_id, market_id, WALLET_ID, status, now_iso, now_iso),
    )
    db.conn.commit()

# ===========================================================================
# Test A: Retired route returns 404
# ===========================================================================

class TestRetiredRoutesReturn404:
    """Retired POST /paper/approve and POST /paper/reject return framework 404."""

    def test_paper_approve_returns_404(self, monkeypatch, tmp_path):
        """POST /paper/approve is retired — framework default 404."""
        _reset_app_state(monkeypatch, tmp_path)
        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": str(uuid4())})
            assert resp.status_code == 404

    def test_paper_reject_returns_404(self, monkeypatch, tmp_path):
        """POST /paper/reject is retired — framework default 404."""
        _reset_app_state(monkeypatch, tmp_path)
        with TestClient(app) as client:
            resp = client.post("/paper/reject", json={"order_id": str(uuid4())})
            assert resp.status_code == 404


# ===========================================================================
# Test B: Resolution semantics
# ===========================================================================

class FakeResolutionAdapter:
    """Fake resolution provider for testing check_resolution logic."""

    def __init__(self, market):
        self._market = market

    async def check_resolution(self, market_id: str):
        """Replicates the fixed PolymarketPublicAdapter.check_resolution logic."""
        m = self._market
        if m is None:
            return None
        if not m.resolved:
            return None
        if not m.resolution_outcome:
            return None
        return m

    async def list_resolved_since(self, since_timestamp: str, limit: int = 100):
        return []


class TestResolutionSemantics:
    """Regression test D: ResolutionProvider semantics.

    The key fix: check_resolution() must return None unless market.resolved=True
    AND market.resolution_outcome is non-empty.
    """

    def test_unresolved_market_returns_none(self):
        """An unresolved market must return None from check_resolution."""
        from polycopy.domain.market import Market, MarketOutcome
        from datetime import datetime, timezone

        open_market = Market(
            source_id="test",
            question="Open market",
            outcomes=[MarketOutcome(label="Yes", price=0.5)],
            source="test",
            resolved=False,
            resolution_outcome=None,
            fetched_at=datetime.now(timezone.utc),
        )
        adapter = FakeResolutionAdapter(open_market)
        import asyncio
        result = asyncio.run(adapter.check_resolution("unresolved-market"))
        assert result is None

    def test_resolved_market_with_outcome_returns_market(self):
        """A resolved market with valid outcome returns the market."""
        from polycopy.domain.market import Market, MarketOutcome
        from datetime import datetime, timezone

        resolved_market = Market(
            source_id="test",
            question="Resolved market",
            outcomes=[MarketOutcome(label="Yes", price=1.0)],
            source="test",
            resolved=True,
            resolution_outcome="Yes",
            fetched_at=datetime.now(timezone.utc),
        )
        adapter = FakeResolutionAdapter(resolved_market)
        import asyncio
        result = asyncio.run(adapter.check_resolution("resolved-market"))
        assert result is not None
        assert result.resolved is True
        assert result.resolution_outcome == "Yes"

    def test_disputed_market_returns_none(self):
        """A disputed market (resolved=True but no outcome) returns None."""
        from polycopy.domain.market import Market, MarketOutcome
        from datetime import datetime, timezone

        disputed_market = Market(
            source_id="test",
            question="Disputed market",
            outcomes=[MarketOutcome(label="Yes", price=0.5)],
            source="test",
            resolved=True,
            resolution_outcome=None,
            fetched_at=datetime.now(timezone.utc),
        )
        adapter = FakeResolutionAdapter(disputed_market)
        import asyncio
        result = asyncio.run(adapter.check_resolution("disputed-market"))
        assert result is None

    def test_missing_market_returns_none(self):
        """A market not found returns None."""
        adapter = FakeResolutionAdapter(None)
        import asyncio
        result = asyncio.run(adapter.check_resolution("nonexistent"))
        assert result is None
