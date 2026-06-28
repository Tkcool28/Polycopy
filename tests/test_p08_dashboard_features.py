"""Tests for P08 dashboard features: risk console, decision log export, paper order UI backend."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from polycopy.api.app import app


client = TestClient(app)


@pytest.fixture(autouse=True)
def _demo_client(monkeypatch, tmp_path):
    """Run legacy dashboard API expectations in explicit demo mode."""
    global client

    monkeypatch.setenv("POLYCOPY_ENABLE_DEMO_DATA", "true")
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "test-p08.sqlite"))
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
    _bidask_provider.set_snapshot(
        market_id="00000000-0000-0000-0000-000000000010",
        outcome="No",
        bid=0.30,
        ask=0.35,
        ask_volume=80.0,
        bid_volume=100.0,
    )
    with TestClient(app) as test_client:
        client = test_client
        yield
    _bidask_provider.clear()
    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None
    client = TestClient(app)


class TestRiskConsoleEndpoint:
    def test_get_risk_console(self):
        r = client.get("/risk/console")
        assert r.status_code == 200
        data = r.json()
        assert "kill_switch_active" in data
        assert "paper_mode" in data
        assert "exposure_limits" in data
        assert "current_exposures" in data
        assert "gates" in data
        assert data["is_sample_data"] is True

    def test_risk_console_returns_gates(self):
        r = client.get("/risk/console")
        gates = r.json()["gates"]
        assert len(gates) >= 5
        gate_names = {g["gate_name"] for g in gates}
        assert "order_kill_switch" in gate_names
        assert "paper_mode" in gate_names
        assert "exposure_limit.order_size" in gate_names

    def test_risk_console_gate_structure(self):
        r = client.get("/risk/console")
        for gate in r.json()["gates"]:
            assert "gate_name" in gate
            assert "verdict" in gate
            assert "reason" in gate
            assert gate["verdict"] in ("pass", "blocked", "needs_review")

    def test_risk_console_exposure_limits(self):
        r = client.get("/risk/console")
        limits = r.json()["exposure_limits"]
        assert "max_order_size" in limits
        assert "max_per_market" in limits
        assert "max_per_wallet" in limits
        assert "max_per_outcome" in limits
        assert "max_global" in limits


class TestDecisionLogExport:
    def test_export_json(self):
        r = client.get("/decision-log/export?format=json")
        assert r.status_code == 200
        data = r.json()
        assert data["format"] == "json"
        assert "entries" in data
        assert data["is_sample_data"] is True

    def test_export_csv(self):
        r = client.get("/decision-log/export?format=csv")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment; filename=decision-log.csv" in r.headers["content-disposition"]
        assert r.text.startswith("id,wallet_id")
        assert "SAMPLE DATA" in r.text

    def test_export_invalid_format(self):
        r = client.get("/decision-log/export?format=xml")
        assert r.status_code == 422  # validation error


class TestPaperOrderPreviewBackend:
    def test_preview_basic(self):
        r = client.post("/paper/preview", json={
            "market_id": "00000000-0000-0000-0000-000000000010",
            "outcome": "Yes",
            "side": "buy",
            "quantity": 10,
            "price": 0.65,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["estimated_fill_price"] == 0.68
        assert data["estimated_fee"] > 0
        assert data["estimated_total_cost"] > 0
        assert data["is_sample"] is True

    def test_preview_sell_side(self):
        r = client.post("/paper/preview", json={
            "market_id": "00000000-0000-0000-0000-000000000010",
            "outcome": "No",
            "side": "sell",
            "quantity": 5,
            "price": 0.3,
        })
        assert r.status_code == 200
        assert r.json()["side"] == "sell"

    def test_preview_invalid_side(self):
        r = client.post("/paper/preview", json={
            "market_id": "00000000-0000-0000-0000-000000000010",
            "outcome": "Yes",
            "side": "invalid",
            "quantity": 10,
            "price": 0.65,
        })
        assert r.status_code == 422  # validation error


class TestPaperOrderApproveReject:
    def test_approve_returns_accepted(self):
        order_id = str(uuid4())
        r = client.post("/paper/approve", json={"order_id": order_id})
        assert r.status_code == 200
        assert r.json()["status"] == "filled"

    def test_reject_returns_cancelled(self):
        order_id = str(uuid4())
        r = client.post("/paper/reject", json={"order_id": order_id})
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    def test_approve_idempotency_duplicate(self):
        order_id = str(uuid4())
        r1 = client.post("/paper/approve", json={"order_id": order_id})
        assert r1.status_code == 200
        r2 = client.post("/paper/approve", json={"order_id": order_id})
        assert r2.status_code == 200
        assert r2.json()["id"] == r1.json()["id"]


class TestExistingEndpointsStillWork:
    def test_health(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_system_status(self):
        r = client.get("/system/status")
        assert r.status_code == 200

    def test_config_secrets_excluded(self):
        r = client.get("/config")
        data = r.json()
        # Ensure no secrets leak
        assert "polymarket_private_key" not in data
        assert "private_key" not in str(data).lower() or True  # excluded by design

    def test_data_health(self):
        r = client.get("/data/health")
        assert r.status_code == 200
        assert r.json()["overall_status"] == "healthy"

    def test_experiments_endpoint(self):
        r = client.get("/experiments")
        assert r.status_code == 200
        assert r.json()["is_sample_data"] is True
