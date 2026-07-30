"""P14 API integration tests for real paper preview and persistent idempotency."""

from __future__ import annotations

from datetime import datetime, timezone

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
    _idempotency_store._db = None
    _idempotency_store._ensured_table = False
    _bidask_provider.clear()
    return database_module, settings_module, _bidask_provider, _idempotency_store


def _insert_pending_order(order_id: str, *, status: str = "pending") -> None:
    from polycopy.db.database import get_database

    db = get_database()
    now = datetime.now(timezone.utc).isoformat()
    wallet_id = "00000000-0000-0000-0000-000000000002"
    db.execute(
        "INSERT OR IGNORE INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, ?, ?)",
        (wallet_id, "0xpaper", "paper", 1, now),
    )
    db.execute(
        "INSERT OR IGNORE INTO markets (id, source_id, source, question, fetched_at, is_sample) VALUES (?, ?, ?, ?, ?, ?)",
        (MARKET_ID, "paper-market", "paper", "Paper market", now, 1),
    )
    db.execute(
        """
        INSERT INTO orders (
            id, market_id, wallet_id, side, order_type, outcome, quantity, price,
            status, filled_quantity, created_at, updated_at, is_sample
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (order_id, MARKET_ID, wallet_id, "buy", "limit", "Yes", 10.0, 0.65, status, 0.0, now, now, 1),
    )
    db.conn.commit()


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


def test_approve_route_retired_returns_404(monkeypatch, tmp_path):
    """POST /paper/approve is retired — framework default 404."""
    database_module, settings_module, _, _ = _reset_app_state(monkeypatch, tmp_path)
    with TestClient(app) as client:
        resp = client.post("/paper/approve", json={"order_id": "00000000-0000-0000-0000-000000000099", "notes": "approve sample"})
        assert resp.status_code == 404

    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None


def test_reject_route_retired_returns_404(monkeypatch, tmp_path):
    """POST /paper/reject is retired — framework default 404."""
    database_module, settings_module, _, _ = _reset_app_state(monkeypatch, tmp_path)
    with TestClient(app) as client:
        resp = client.post("/paper/reject", json={"order_id": "00000000-0000-0000-0000-000000000100", "notes": "operator says no"})
        assert resp.status_code == 404

    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None
