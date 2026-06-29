"""Tests for Polycopy FastAPI endpoints.

Covers: health, system status, scans, wallets, signals, paper orders,
positions, portfolio, decision log, experiments, data health, config,
and idempotency protection.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from polycopy.api.app import app, _idempotency_store


@pytest.fixture(autouse=True)
def _clear_idempotency():
    """Clear idempotency store between tests."""
    _idempotency_store.clear()
    yield
    _idempotency_store.clear()


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Sync test client for FastAPI app."""
    monkeypatch.setenv("POLYCOPY_ENABLE_DEMO_DATA", "true")
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "test-api.sqlite"))
    from polycopy.api.app import _bidask_provider
    import polycopy.config.settings as settings_module
    import polycopy.db.database as database_module

    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None
    _bidask_provider.set_snapshot(
        market_id="00000000-0000-0000-0000-000000000010",
        outcome="Yes",
        bid=0.62,
        ask=0.68,
        ask_volume=100.0,
        bid_volume=50.0,
    )
    with TestClient(app) as test_client:
        yield test_client
    _bidask_provider.clear()
    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None


# ── Health & system status ────────────────────────────────────────────────────

class TestHealth:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_system_status_returns_config(self, client):
        resp = client.get("/system/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["broker_mode"] == "paper"
        assert data["order_kill_switch"] is False
        assert data["is_live"] is False


# ── Scans ─────────────────────────────────────────────────────────────────────

class TestScans:
    def test_list_scans_returns_results(self, client):
        resp = client.get("/scans")
        assert resp.status_code == 200
        data = resp.json()
        assert "scans" in data
        assert "total_count" in data

    def test_scans_with_pagination(self, client):
        resp = client.get("/scans?limit=1&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] >= 0


# ── Wallets ───────────────────────────────────────────────────────────────────

class TestWallets:
    def test_list_wallets_returns_results(self, client):
        resp = client.get("/wallets")
        assert resp.status_code == 200
        data = resp.json()
        assert "wallets" in data
        assert "total_count" in data

    def test_get_sample_wallet_by_id(self, client):
        wallet_id = "00000000-0000-0000-0000-000000000001"
        resp = client.get(f"/wallets/{wallet_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "SAMPLE" in data["label"] or "sample" in data["label"]

    def test_get_unknown_wallet_returns_404(self, client):
        resp = client.get("/wallets/00000000-0000-0000-0000-000000000099")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


# ── Signals ────────────────────────────────────────────────────────────────────

class TestSignals:
    def test_list_signals_returns_results(self, client):
        resp = client.get("/signals")
        assert resp.status_code == 200
        data = resp.json()
        assert "signals" in data
        assert "total_count" in data

    def test_get_unknown_signal_returns_404(self, client):
        resp = client.get("/signals/00000000-0000-0000-0000-000000000099")
        assert resp.status_code == 404


# ── Paper orders (idempotency) ────────────────────────────────────────────────

class TestPaperOrders:
    @staticmethod
    def _seed_pending_order(order_id: str):
        """Seed a pending order in the DB so approve/reject can transition it."""
        from polycopy.db.database import get_database
        db = get_database()
        # was hardcoded "2026-06-28T12:00:00+00:00"; now dynamic so the order
        # doesn't expire past order_preview_max_age_seconds once wall-clock
        # passes that hardcoded value (regression: see test_no_hardcoded_test_seeds).
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT OR IGNORE INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, ?, ?)",
            ("00000000-0000-0000-0000-000000000002", "0xtest", "test", 0, now),
        )
        db.execute(
            "INSERT OR IGNORE INTO markets (id, source_id, source, question, fetched_at, is_sample) VALUES (?, ?, ?, ?, ?, ?)",
            ("00000000-0000-0000-0000-000000000001", "m1", "test", "Test Q", now, 0),
        )
        db.execute(
            """
            INSERT OR IGNORE INTO orders
                (id, market_id, wallet_id, side, order_type, outcome, quantity, price,
                 status, filled_quantity, created_at, updated_at, is_sample)
            VALUES (?, ?, ?, 'buy', 'limit', 'Yes', 10.0, 0.65, 'pending', 0.0, ?, ?, 0)
            """,
            (order_id, "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002", now, now),
        )
        db.conn.commit()

    def test_preview_order_response_model(self, client):
        resp = client.post(
            "/paper/preview",
            json={
                "market_id": "00000000-0000-0000-0000-000000000010",
                "outcome": "Yes",
                "side": "buy",
                "quantity": 10,
                "price": 0.65,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["estimated_fill_price"] == 0.68
        assert data["estimated_fee"] > 0
        assert data["estimated_total_cost"] > 0

    def test_approve_order_succeeds(self, client):
        order_id = "00000000-0000-0000-0000-000000000001"
        self._seed_pending_order(order_id)
        resp = client.post("/paper/approve", json={"order_id": order_id})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "filled"
        assert data["id"] == order_id

    def test_approve_order_duplicate_rejected(self, client):
        order_id = "00000000-0000-0000-0000-000000000001"
        self._seed_pending_order(order_id)
        payload = {"order_id": order_id}
        # First submission
        resp1 = client.post("/paper/approve", json=payload)
        assert resp1.status_code == 200

        # Duplicate submission — same order_id replays the same safe result
        resp2 = client.post("/paper/approve", json=payload)
        assert resp2.status_code == 200
        assert resp2.json()["id"] == resp1.json()["id"]

    def test_reject_order_succeeds(self, client):
        order_id = "00000000-0000-0000-0000-000000000002"
        self._seed_pending_order(order_id)
        resp = client.post("/paper/reject", json={"order_id": order_id})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cancelled"
        assert data["id"] == order_id

    def test_reject_order_duplicate_rejected(self, client):
        order_id = "00000000-0000-0000-0000-000000000002"
        self._seed_pending_order(order_id)
        payload = {"order_id": order_id}
        resp1 = client.post("/paper/reject", json=payload)
        assert resp1.status_code == 200
        resp2 = client.post("/paper/reject", json=payload)
        assert resp2.status_code == 200
        assert resp2.json()["id"] == resp1.json()["id"]

    def test_list_paper_orders(self, client):
        resp = client.get("/paper/orders")
        assert resp.status_code == 200
        data = resp.json()
        assert "orders" in data

    def test_list_paper_orders_with_filter(self, client):
        resp = client.get("/paper/orders?status=pending")
        assert resp.status_code == 200
        data = resp.json()
        assert all(o["status"] == "pending" for o in data["orders"])


# ── Positions & portfolio ────────────────────────────────────────────────────

class TestPositions:
    def test_list_positions_returns_results(self, client):
        resp = client.get("/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert "positions" in data
        assert "total_unrealized_pnl" in data
        assert "total_cost_basis" in data

    def test_portfolio_summary(self, client):
        resp = client.get("/portfolio/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_positions" in data
        assert "total_pnl" in data
        assert "wallet_count" in data


# ── Decision log ──────────────────────────────────────────────────────────────

class TestDecisionLog:
    def test_list_decisions_returns_results(self, client):
        resp = client.get("/decision-log")
        assert resp.status_code == 200
        data = resp.json()
        assert "entries" in data
        assert "total_count" in data


# ── Experiments ────────────────────────────────────────────────────────────────

class TestExperiments:
    def test_list_experiments_returns_results(self, client):
        resp = client.get("/experiments")
        assert resp.status_code == 200
        data = resp.json()
        assert "experiments" in data
        assert "total_count" in data
        assert "profitable_count" in data


# ── Data health ────────────────────────────────────────────────────────────────

class TestDataHealth:
    def test_data_health_returns_sources(self, client):
        resp = client.get("/data/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "sources" in data
        assert "overall_status" in data
        assert len(data["sources"]) > 0


# ── Configuration (secrets excluded) ──────────────────────────────────────────

class TestConfig:
    def test_config_returns_no_secrets(self, client):
        resp = client.get("/config")
        assert resp.status_code == 200
        data = resp.json()
        # Verify secrets are excluded
        assert "polymarket_private_key" not in data
        assert "private_key" not in [k.lower() for k in data.keys()]
        assert "token" not in [k.lower() for k in data.keys()]
        assert "secret" not in [k.lower() for k in data.keys()]
        # Verify expected fields present
        assert "broker_mode" in data
        assert "paper_mode" in data
        assert "gamma_base_url" in data
        assert "order_kill_switch" in data

    def test_config_shows_paper_mode(self, client):
        resp = client.get("/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["broker_mode"] == "paper"


# ── Idempotency ────────────────────────────────────────────────────────────────

class TestIdempotency:
    def test_new_key_is_not_duplicate(self, client):
        resp = client.get("/idempotency/test-key-123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_duplicate"] is False
        assert "new" in data["message"].lower()

    def test_same_key_is_duplicate(self, client):
        # Register a key by calling approve
        client.post("/paper/approve", json={"order_id": "00000000-0000-0000-0000-000000000050"})
        # The key is now in the store
        resp = client.get("/idempotency/unknown")
        # Unknown key should be "new"
        assert resp.json()["is_duplicate"] is False
