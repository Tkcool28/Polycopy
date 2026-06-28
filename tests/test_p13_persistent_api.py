"""P13 API persistence tests for dashboard read routes and explicit demo mode."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from polycopy.api.app import app
from polycopy.config.settings import get_settings
from polycopy.db import database as db_module
from polycopy.db.database import get_database

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).isoformat()
WALLET_ID = "11111111-1111-1111-1111-111111111111"
SAMPLE_WALLET_ID = "22222222-2222-2222-2222-222222222222"
MARKET_ID = "33333333-3333-3333-3333-333333333333"
SIGNAL_ID = "44444444-4444-4444-4444-444444444444"
ORDER_ID = "55555555-5555-5555-5555-555555555555"
POSITION_ID = "66666666-6666-6666-6666-666666666666"
DECISION_ID = "77777777-7777-7777-7777-777777777777"
EXPERIMENT_ID = "88888888-8888-8888-8888-888888888888"


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.delenv("POLYCOPY_ENABLE_DEMO_DATA", raising=False)
    get_settings(reload=True)
    get_database(reload=True)
    yield TestClient(app)
    if db_module._db is not None:
        db_module._db.close()
        db_module._db = None
    get_settings(reload=True)


def _enable_demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "demo.db"))
    monkeypatch.setenv("POLYCOPY_ENABLE_DEMO_DATA", "true")
    get_settings(reload=True)
    get_database(reload=True)
    return TestClient(app)


def _seed_persisted_dashboard_data() -> None:
    db = get_database()
    db.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, 0, ?)",
        (WALLET_ID, "0xLIVE", "live-wallet", NOW),
    )
    db.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, 1, ?)",
        (SAMPLE_WALLET_ID, "0xSAMPLE", "sample-wallet", NOW),
    )
    db.execute(
        "INSERT INTO wallet_balances (wallet_id, currency, amount, as_of, is_sample) VALUES (?, ?, ?, ?, 0)",
        (WALLET_ID, "USDC", 12.5, NOW),
    )
    db.execute(
        """
        INSERT INTO markets (id, source_id, source, question, active, closed, resolved,
                             volume_24h, fetched_at, is_sample)
        VALUES (?, 'm1', 'test', 'Will tests pass?', 1, 0, 0, 100.0, ?, 0)
        """,
        (MARKET_ID, NOW),
    )
    db.execute(
        """
        INSERT INTO performance_summaries
            (wallet_id, strategy_label, start_date, end_date, total_pnl, realized_pnl,
             unrealized_pnl, win_rate, sharpe_ratio, max_drawdown, trade_count, is_sample)
        VALUES (?, 'default', ?, ?, 4.0, 3.0, 1.0, 0.6, 1.2, 0.1, 9, 0)
        """,
        (WALLET_ID, NOW, NOW),
    )
    db.execute(
        """
        INSERT INTO signals
            (id, market_id, source, strength, confidence, edge_estimate,
             predicted_prob, market_prob, reasoning, produced_at, is_sample)
        VALUES (?, ?, 'engine', 'buy', 0.8, 0.1, 0.6, 0.5, 'persisted signal', ?, 0)
        """,
        (SIGNAL_ID, MARKET_ID, NOW),
    )
    db.execute(
        """
        INSERT INTO orders
            (id, market_id, wallet_id, side, order_type, outcome, quantity, price,
             status, filled_quantity, created_at, updated_at, is_sample)
        VALUES (?, ?, ?, 'buy', 'limit', 'Yes', 3.0, 0.42, 'pending', 1.0, ?, ?, 0)
        """,
        (ORDER_ID, MARKET_ID, WALLET_ID, NOW, NOW),
    )
    db.execute(
        """
        INSERT INTO positions
            (id, market_id, wallet_id, outcome, quantity, avg_entry_price, current_price,
             realized_pnl, opened_at, updated_at, is_sample)
        VALUES (?, ?, ?, 'Yes', 3.0, 0.4, 0.5, 0.2, ?, ?, 0)
        """,
        (POSITION_ID, MARKET_ID, WALLET_ID, NOW, NOW),
    )
    db.execute(
        """
        INSERT INTO decision_log
            (id, wallet_id, market_id, decision_type, signal_ids, order_id, rationale,
             metrics, created_at, is_sample)
        VALUES (?, ?, ?, 'copy', ?, ?, 'persisted decision', ?, ?, 0)
        """,
        (DECISION_ID, WALLET_ID, MARKET_ID, json.dumps([SIGNAL_ID]), ORDER_ID, json.dumps({"edge": 0.1}), NOW),
    )
    db.execute(
        """
        INSERT INTO experiment_runs
            (id, label, strategy_config, status, started_at, ended_at, result_summary, is_sample)
        VALUES (?, 'experiment-a', ?, 'completed', ?, ?, ?, 0)
        """,
        (EXPERIMENT_ID, json.dumps({"threshold": 70}), NOW, NOW, json.dumps({"pnl": 1.25})),
    )
    db.execute(
        """
        INSERT INTO raw_snapshots
            (id, source, endpoint, query_params, file_path, content_hash, hash_algo,
             content_type, size_bytes, fetched_at, ingested_at, is_sample)
        VALUES ('99999999-9999-9999-9999-999999999999', 'test_source', '/markets', '{}',
                'snapshot.json', 'abc', 'sha256', 'application/json', 3, ?, ?, 0)
        """,
        (NOW, NOW),
    )
    db.conn.commit()


def test_empty_db_returns_empty_without_sample_fallback(api_client: TestClient):
    collection_expectations = {
        "/scans": ("scans", "total_count"),
        "/wallets": ("wallets", "total_count"),
        "/signals": ("signals", "total_count"),
        "/paper/orders": ("orders", "total_count"),
        "/positions": ("positions", "total_count"),
        "/decision-log": ("entries", "total_count"),
        "/experiments": ("experiments", "total_count"),
    }
    for path, (items_key, count_key) in collection_expectations.items():
        data = api_client.get(path).json()
        assert data[items_key] == []
        assert data[count_key] == 0
        assert data.get("is_sample_data") is False

    assert api_client.get(f"/wallets/{WALLET_ID}").status_code == 404
    assert api_client.get(f"/signals/{SIGNAL_ID}").status_code == 404

    data_health = api_client.get("/data/health").json()
    assert data_health["sources"] == []
    assert data_health["snapshot_count"] == 0
    assert data_health["overall_status"] == "unavailable"

    risk = api_client.get("/risk/console").json()
    assert risk["current_exposures"]["global"] == 0
    assert risk["is_sample_data"] is False


def test_persisted_routes_use_sqlite_and_stable_ids(api_client: TestClient):
    _seed_persisted_dashboard_data()

    assert api_client.get("/scans").json()["total_count"] == 2
    wallet_list = api_client.get("/wallets").json()
    assert {w["id"] for w in wallet_list["wallets"]} == {WALLET_ID, SAMPLE_WALLET_ID}
    assert any("SAMPLE DATA" in w["label"] for w in wallet_list["wallets"] if w["id"] == SAMPLE_WALLET_ID)

    wallet = api_client.get(f"/wallets/{WALLET_ID}").json()
    assert wallet["id"] == WALLET_ID
    assert wallet["balances"][0]["amount"] == 12.5

    signal = api_client.get(f"/signals/{SIGNAL_ID}").json()
    assert signal["id"] == SIGNAL_ID
    assert api_client.get("/signals", params={"market_id": MARKET_ID}).json()["signals"][0]["id"] == SIGNAL_ID

    orders = api_client.get("/paper/orders", params={"status": "pending"}).json()
    assert orders["orders"][0]["id"] == ORDER_ID
    assert api_client.get("/positions", params={"wallet_id": WALLET_ID}).json()["positions"][0]["id"] == POSITION_ID
    assert api_client.get("/portfolio/summary").json()["total_positions"] == 1
    assert api_client.get("/decision-log").json()["entries"][0]["id"] == DECISION_ID
    assert api_client.get("/experiments").json()["experiments"][0]["id"] == EXPERIMENT_ID
    assert api_client.get("/data/health").json()["snapshot_count"] == 1
    assert "order_kill_switch" in {g["gate_name"] for g in api_client.get("/risk/console").json()["gates"]}

    exported = api_client.get("/decision-log/export", params={"format": "json"}).json()
    assert exported["entries"][0]["id"] == DECISION_ID
    csv_export = api_client.get("/decision-log/export", params={"format": "csv"})
    assert csv_export.headers["content-type"].startswith("text/csv")
    assert DECISION_ID in csv_export.text


def test_demo_mode_is_explicit_labeled_and_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = _enable_demo(tmp_path, monkeypatch)
    try:
        first = client.get("/signals").json()
        second = client.get("/signals").json()
        assert first["is_sample_data"] is True
        assert first["signals"][0]["id"] == second["signals"][0]["id"]
        assert "SAMPLE DATA" in first["signals"][0]["reasoning"]

        wallet = client.get("/wallets/00000000-0000-0000-0000-000000000001").json()
        assert wallet["is_sample"] is True
        assert "DEMO DATA" in wallet["label"]

        order_ids = [client.get("/paper/orders").json()["orders"][0]["id"] for _ in range(2)]
        assert order_ids[0] == order_ids[1]
        assert client.get("/data/health").json()["sources"][0]["status"] == "ok"
    finally:
        if db_module._db is not None:
            db_module._db.close()
            db_module._db = None
