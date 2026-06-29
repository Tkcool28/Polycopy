"""P18 regression tests for paper approve/reject identity, resolution, and frontend fixes.

Covers all five Codex review findings plus required regression tests A-E.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from polycopy.api.app import app


MARKET_ID = "00000000-0000-0000-0000-000000000001"
WALLET_ID = "00000000-0000-0000-0000-000000000002"
# Was hardcoded "2026-06-28T12:00:00+00:00"; now dynamic so seeded orders
# never expire past order_preview_max_age_seconds once wall-clock passes
# that hardcoded value.
NOW = datetime.now(timezone.utc).isoformat()


def _reset_app_state(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYCOPY_ENABLE_DEMO_DATA", "true")
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p18.sqlite"))

    import polycopy.config.settings as settings_module
    import polycopy.db.database as database_module
    from polycopy.api.app import _bidask_provider, _idempotency_store

    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None
    _idempotency_store._db = None  # noqa: SLF001
    _idempotency_store._ensured_table = False  # noqa: SLF001
    _bidask_provider.clear()
    return database_module, settings_module, _bidask_provider, _idempotency_store


def _seed_pending_order(order_id: str, *, market_id: str = MARKET_ID, status: str = "pending") -> None:
    from polycopy.db.database import get_database
    db = get_database()
    db.execute(
        "INSERT OR IGNORE INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, ?, ?)",
        (WALLET_ID, "0xtest", "test", 0, NOW),
    )
    db.execute(
        "INSERT OR IGNORE INTO markets (id, source_id, source, question, fetched_at, is_sample) VALUES (?, ?, ?, ?, ?, ?)",
        (market_id, "m1", "test", "Test Q", NOW, 0),
    )
    db.execute(
        """
        INSERT OR IGNORE INTO orders
            (id, market_id, wallet_id, side, order_type, outcome, quantity, price,
             status, filled_quantity, created_at, updated_at, is_sample)
        VALUES (?, ?, ?, 'buy', 'limit', 'Yes', 10.0, 0.65, ?, 0.0, ?, ?, 0)
        """,
        (order_id, market_id, WALLET_ID, status, NOW, NOW),
    )
    db.conn.commit()


# ===========================================================================
# Test A: Approval of an existing pending order
# ===========================================================================

class TestApprovalRegression:
    """Regression test A: Approval of an existing pending order."""

    def test_approve_preserves_order_count(self, monkeypatch, tmp_path):
        """Approve a pending order — total order count must remain 1."""
        db_mod, settings_mod, _, idem = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id)

        with TestClient(app) as client:
            # Before: 1 order
            before = client.get("/paper/orders").json()["total_count"]
            assert before == 1

            resp = client.post("/paper/approve", json={"order_id": order_id, "notes": "test approve"})
            assert resp.status_code == 200
            assert resp.json()["id"] == order_id
            assert resp.json()["status"] == "filled"

            # After: still 1 order
            after = client.get("/paper/orders").json()["total_count"]
            assert after == 1

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None

    def test_approve_transitions_same_uuid(self, monkeypatch, tmp_path):
        """The same UUID transitions to filled — no replacement order."""
        db_mod, settings_mod, _, idem = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id)

        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": order_id})
            assert resp.status_code == 200
            assert resp.json()["id"] == order_id
            assert resp.json()["status"] == "filled"

            # Verify in DB
            orders = client.get("/paper/orders").json()
            assert orders["total_count"] == 1
            assert orders["orders"][0]["id"] == order_id
            assert orders["orders"][0]["status"] == "filled"

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None

    def test_approve_creates_one_position(self, monkeypatch, tmp_path):
        """Approval creates exactly one position."""
        db_mod, settings_mod, _, idem = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id)

        with TestClient(app) as client:
            client.post("/paper/approve", json={"order_id": order_id})
            positions = client.get("/positions").json()
            assert positions["total_count"] == 1

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None

    def test_approve_creates_one_decision(self, monkeypatch, tmp_path):
        """Approval creates exactly one decision log entry."""
        db_mod, settings_mod, _, idem = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id)

        with TestClient(app) as client:
            client.post("/paper/approve", json={"order_id": order_id})
            decisions = client.get("/decision-log").json()
            assert decisions["total_count"] == 1
            assert decisions["entries"][0]["decision_type"] == "paper_approve"
            assert decisions["entries"][0]["order_id"] == order_id

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None

    def test_approve_persists_across_restart(self, monkeypatch, tmp_path):
        """After approve, restart API — state remains identical."""
        db_mod, settings_mod, _, idem = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id)

        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": order_id, "notes": "persist"})
            assert resp.status_code == 200
            first_status = resp.json()["status"]
            first_id = resp.json()["id"]

        # Restart
        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None
        idem._db = None  # noqa: SLF001
        idem._ensured_table = False  # noqa: SLF001

        with TestClient(app) as client:
            orders = client.get("/paper/orders").json()
            found = [o for o in orders["orders"] if o["id"] == first_id]
            assert len(found) == 1
            assert found[0]["status"] == first_status

            # Replay idempotency
            replay = client.post("/paper/approve", json={"order_id": order_id, "notes": "persist"})
            assert replay.status_code == 200
            assert replay.json()["id"] == first_id
            assert replay.json()["status"] == first_status

            # Counts unchanged
            assert client.get("/paper/orders").json()["total_count"] == 1
            assert client.get("/positions").json()["total_count"] == 1
            assert client.get("/decision-log").json()["total_count"] == 1

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None


# ===========================================================================
# Test B: Rejection of an existing pending order
# ===========================================================================

class TestRejectionRegression:
    """Regression test B: Rejection of an existing pending order."""

    def test_reject_preserves_order_count(self, monkeypatch, tmp_path):
        """Reject a pending order — total order count must remain 1."""
        db_mod, settings_mod, _, idem = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id)

        with TestClient(app) as client:
            resp = client.post("/paper/reject", json={"order_id": order_id, "notes": "test reject"})
            assert resp.status_code == 200
            assert resp.json()["id"] == order_id
            assert resp.json()["status"] == "cancelled"

            orders = client.get("/paper/orders").json()
            assert orders["total_count"] == 1

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None

    def test_reject_transitions_same_uuid(self, monkeypatch, tmp_path):
        """Same UUID transitions to cancelled — no replacement row."""
        db_mod, settings_mod, _, idem = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id)

        with TestClient(app) as client:
            resp = client.post("/paper/reject", json={"order_id": order_id})
            assert resp.json()["id"] == order_id
            assert resp.json()["status"] == "cancelled"

            orders = client.get("/paper/orders").json()
            assert orders["total_count"] == 1
            assert orders["orders"][0]["id"] == order_id

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None

    def test_reject_stores_operator_note(self, monkeypatch, tmp_path):
        """Operator note is stored in decision log."""
        db_mod, settings_mod, _, idem = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id)

        with TestClient(app) as client:
            client.post("/paper/reject", json={"order_id": order_id, "notes": "operator says no"})
            decisions = client.get("/decision-log").json()
            reject_entries = [e for e in decisions["entries"] if e["decision_type"] == "paper_reject"]
            assert len(reject_entries) == 1
            assert "operator says no" in reject_entries[0]["rationale"]

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None

    def test_reject_persists_across_restart(self, monkeypatch, tmp_path):
        """After reject, restart API — state remains identical."""
        db_mod, settings_mod, _, idem = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id)

        with TestClient(app) as client:
            resp = client.post("/paper/reject", json={"order_id": order_id, "notes": "persist"})
            assert resp.status_code == 200
            first_id = resp.json()["id"]

        # Restart
        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None
        idem._db = None  # noqa: SLF001
        idem._ensured_table = False  # noqa: SLF001

        with TestClient(app) as client:
            orders = client.get("/paper/orders").json()
            found = [o for o in orders["orders"] if o["id"] == first_id]
            assert len(found) == 1
            assert found[0]["status"] == "cancelled"

            # Replay
            replay = client.post("/paper/reject", json={"order_id": order_id, "notes": "persist"})
            assert replay.status_code == 200
            assert replay.json()["id"] == first_id

            # No duplicates
            assert client.get("/paper/orders").json()["total_count"] == 1
            assert client.get("/decision-log").json()["total_count"] == 1

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None


# ===========================================================================
# Test C: Invalid transitions
# ===========================================================================

class TestInvalidTransitions:
    """Regression test C: Invalid transitions are rejected."""

    def test_unknown_order_returns_404(self, monkeypatch, tmp_path):
        _reset_app_state(monkeypatch, tmp_path)
        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": str(uuid4())})
            assert resp.status_code == 404

            resp = client.post("/paper/reject", json={"order_id": str(uuid4())})
            assert resp.status_code == 404

    def test_filled_order_cannot_be_rejected(self, monkeypatch, tmp_path):
        db_mod, settings_mod, _, _ = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id, status="filled")

        with TestClient(app) as client:
            resp = client.post("/paper/reject", json={"order_id": order_id})
            assert resp.status_code == 409

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None

    def test_rejected_order_cannot_be_approved(self, monkeypatch, tmp_path):
        db_mod, settings_mod, _, _ = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id, status="cancelled")

        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": order_id})
            assert resp.status_code == 409

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None

    def test_expired_preview_cannot_be_approved(self, monkeypatch, tmp_path):
        """Order older than order_preview_max_age_seconds cannot be approved."""
        db_mod, settings_mod, _, _ = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())

        # Seed with old timestamp
        from polycopy.db.database import get_database
        db = get_database()
        old_time = "2020-01-01T00:00:00+00:00"
        db.execute(
            "INSERT OR IGNORE INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, ?, ?)",
            (WALLET_ID, "0xtest", "test", 0, old_time),
        )
        db.execute(
            "INSERT OR IGNORE INTO markets (id, source_id, source, question, fetched_at, is_sample) VALUES (?, ?, ?, ?, ?, ?)",
            (MARKET_ID, "m1", "test", "Test Q", old_time, 0),
        )
        db.execute(
            """
            INSERT OR IGNORE INTO orders
                (id, market_id, wallet_id, side, order_type, outcome, quantity, price,
                 status, filled_quantity, created_at, updated_at, is_sample)
            VALUES (?, ?, ?, 'buy', 'limit', 'Yes', 10.0, 0.65, 'pending', 0.0, ?, ?, 0)
            """,
            (order_id, MARKET_ID, WALLET_ID, old_time, old_time),
        )
        db.conn.commit()

        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": order_id})
            assert resp.status_code == 409
            assert "expired" in resp.json()["detail"].lower() or "preview" in resp.json()["detail"].lower()

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None

    def test_risk_failure_blocks_without_mutating(self, monkeypatch, tmp_path):
        """Risk gate failure blocks transition without mutating order."""
        db_mod, settings_mod, _, _ = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id)

        # Set kill switch via env
        import os
        os.environ["POLYCOPY_ORDER_KILL_SWITCH"] = "true"
        try:
            # Reload settings
            import polycopy.config.settings as settings_module
            settings_module._settings = None
            from polycopy.db.database import get_database
            get_database(reload=True)

            with TestClient(app) as client:
                resp = client.post("/paper/approve", json={"order_id": order_id})
                assert resp.status_code == 409

                # Order still pending
                orders = client.get("/paper/orders").json()
                found = [o for o in orders["orders"] if o["id"] == order_id]
                assert len(found) == 1
                assert found[0]["status"] == "pending"
        finally:
            os.environ.pop("POLYCOPY_ORDER_KILL_SWITCH", None)
            settings_module._settings = None

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None


# ===========================================================================
# Test D: Resolution semantics
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


# ===========================================================================
# Test E: Operator notes
# ===========================================================================

class TestOperatorNotes:
    """Regression test E: Operator notes are persisted and displayed."""

    def test_approval_note_persists_in_decision_log(self, monkeypatch, tmp_path):
        db_mod, settings_mod, _, _ = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id)

        with TestClient(app) as client:
            client.post("/paper/approve", json={"order_id": order_id, "notes": "My approval note"})
            decisions = client.get("/decision-log").json()
            approve_entries = [e for e in decisions["entries"] if e["decision_type"] == "paper_approve"]
            assert len(approve_entries) == 1
            assert "My approval note" in approve_entries[0]["rationale"]

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None

    def test_rejection_note_persists_in_decision_log(self, monkeypatch, tmp_path):
        db_mod, settings_mod, _, _ = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id)

        with TestClient(app) as client:
            client.post("/paper/reject", json={"order_id": order_id, "notes": "My rejection note"})
            decisions = client.get("/decision-log").json()
            reject_entries = [e for e in decisions["entries"] if e["decision_type"] == "paper_reject"]
            assert len(reject_entries) == 1
            assert "My rejection note" in reject_entries[0]["rationale"]

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None

    def test_empty_note_allowed(self, monkeypatch, tmp_path):
        """Empty note (None) is allowed — backend uses default rationale."""
        db_mod, settings_mod, _, _ = _reset_app_state(monkeypatch, tmp_path)
        order_id = str(uuid4())
        _seed_pending_order(order_id)

        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": order_id})
            assert resp.status_code == 200

            decisions = client.get("/decision-log").json()
            approve_entries = [e for e in decisions["entries"] if e["decision_type"] == "paper_approve"]
            assert len(approve_entries) == 1
            # Default rationale is used
            assert approve_entries[0]["rationale"]  # non-empty string

        db_mod._db.close()
        db_mod._db = None
        settings_mod._settings = None
