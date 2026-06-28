"""P14 API integration tests for real paper preview and persistent idempotency."""

from __future__ import annotations

from fastapi.testclient import TestClient

from polycopy.api.app import app


MARKET_ID = "00000000-0000-0000-0000-000000000001"
ORDER_ID = "00000000-0000-0000-0000-000000000099"


def _reset_app_state(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYCOPY_ENABLE_DEMO_DATA", "true")
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p14-paper.sqlite"))

    import polycopy.config.settings as settings_module
    import polycopy.db.database as database_module
    from polycopy.api.app import _bidask_provider, _idempotency_store

    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None
    _idempotency_store._db = None  # noqa: SLF001 - test reset of app singleton
    _idempotency_store._ensured_table = False  # noqa: SLF001
    _bidask_provider.clear()
    return database_module, settings_module, _bidask_provider, _idempotency_store


def test_preview_uses_executable_ask_and_fails_without_bidask(monkeypatch, tmp_path):
    database_module, settings_module, bidask, _ = _reset_app_state(monkeypatch, tmp_path)

    with TestClient(app) as client:
        missing = client.post(
            "/paper/preview",
            json={"market_id": MARKET_ID, "outcome": "Yes", "side": "buy", "quantity": 10, "price": 0.65},
        )
        assert missing.status_code == 422
        assert "No bid/ask snapshot" in missing.json()["detail"]

        bidask.set_snapshot(MARKET_ID, "Yes", bid=0.62, ask=0.68, ask_volume=100.0, bid_volume=50.0)
        preview = client.post(
            "/paper/preview",
            json={"market_id": MARKET_ID, "outcome": "Yes", "side": "buy", "quantity": 10, "price": 0.65},
        )
        assert preview.status_code == 200
        data = preview.json()
        assert data["estimated_fill_price"] == 0.68
        assert data["bid"] == 0.62
        assert data["ask"] == 0.68
        assert data["spread"] == 0.06
        assert data["fill_model_version"] == "polycopy-fill-v1"
        assert data["status"] == "pending"
        assert data["is_sample"] is True

    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None


def test_approve_persists_order_position_decision_and_idempotency_across_restart(monkeypatch, tmp_path):
    database_module, settings_module, _, idempotency = _reset_app_state(monkeypatch, tmp_path)

    with TestClient(app) as client:
        first = client.post("/paper/approve", json={"order_id": ORDER_ID, "notes": "approve sample"})
        assert first.status_code == 200
        first_data = first.json()
        assert first_data["status"] == "filled"

        orders = client.get("/paper/orders").json()
        assert orders["total_count"] == 1
        assert orders["orders"][0]["id"] == first_data["id"]
        positions = client.get("/positions").json()
        assert positions["total_count"] == 1
        decisions = client.get("/decision-log").json()
        assert decisions["total_count"] == 1
        assert decisions["entries"][0]["decision_type"] == "paper_approve"

    # Simulate an API/service restart while keeping the same SQLite file.
    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None
    idempotency._db = None  # noqa: SLF001
    idempotency._ensured_table = False  # noqa: SLF001

    with TestClient(app) as restarted_client:
        replay = restarted_client.post("/paper/approve", json={"order_id": ORDER_ID, "notes": "approve sample"})
        assert replay.status_code == 200
        replay_data = replay.json()
        assert replay_data["id"] == first_data["id"]
        assert replay_data["status"] == first_data["status"]

        orders_after = restarted_client.get("/paper/orders").json()
        positions_after = restarted_client.get("/positions").json()
        decisions_after = restarted_client.get("/decision-log").json()
        assert orders_after["total_count"] == 1
        assert positions_after["total_count"] == 1
        assert decisions_after["total_count"] == 1

    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None


def test_reject_is_persistent_and_idempotent(monkeypatch, tmp_path):
    database_module, settings_module, _, _ = _reset_app_state(monkeypatch, tmp_path)
    payload = {"order_id": "00000000-0000-0000-0000-000000000100", "notes": "operator says no"}

    with TestClient(app) as client:
        first = client.post("/paper/reject", json=payload)
        assert first.status_code == 200
        second = client.post("/paper/reject", json=payload)
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["status"] == "cancelled"

        orders = client.get("/paper/orders").json()
        decisions = client.get("/decision-log").json()
        assert orders["total_count"] == 1
        assert orders["orders"][0]["status"] == "cancelled"
        assert decisions["entries"][0]["decision_type"] == "paper_reject"

    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None
