"""P19 regressions for Codex P1 paper exposure and sell accounting fixes.

The POST /paper/approve and POST /paper/reject routes have been retired.
These tests verify the routes return 404 (framework default).
"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from polycopy.api.app import app


def test_paper_approve_returns_404(monkeypatch, tmp_path):
    """POST /paper/approve is retired — framework default 404."""
    monkeypatch.setenv("POLYCOPY_ENABLE_DEMO_DATA", "false")
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p19.sqlite"))
    monkeypatch.setenv("POLYCOPY_ORDER_KILL_SWITCH", "false")

    import polycopy.config.settings as settings_module
    import polycopy.db.database as database_module
    from polycopy.api.app import _idempotency_store

    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None
    _idempotency_store._db = None
    _idempotency_store._ensured_table = False

    with TestClient(app) as client:
        resp = client.post("/paper/approve", json={"order_id": str(uuid4())})
        assert resp.status_code == 404


def test_paper_reject_returns_404(monkeypatch, tmp_path):
    """POST /paper/reject is retired — framework default 404."""
    monkeypatch.setenv("POLYCOPY_ENABLE_DEMO_DATA", "false")
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p19.sqlite"))
    monkeypatch.setenv("POLYCOPY_ORDER_KILL_SWITCH", "false")

    import polycopy.config.settings as settings_module
    import polycopy.db.database as database_module
    from polycopy.api.app import _idempotency_store

    if database_module._db is not None:
        database_module._db.close()
    database_module._db = None
    settings_module._settings = None
    _idempotency_store._db = None
    _idempotency_store._ensured_table = False

    with TestClient(app) as client:
        resp = client.post("/paper/reject", json={"order_id": str(uuid4())})
        assert resp.status_code == 404
