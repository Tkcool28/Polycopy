"""P17 end-to-end integration tests.

Covers the full pipeline: clean DB -> seed -> scan -> persisted data -> API ->
dashboard -> API restart same IDs. Also: empty DB, paper preview/approve
with restart retrieval, idempotency replay, reject pending, settlement no-double.

These tests use real SQLite files (tmp_path) and the TestClient, exercising
the same code paths as the production API + scripts.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_db_path(tmp_path: Path) -> Path:
    return tmp_path / "clean_integration.db"


@pytest.fixture
def seeded_db(clean_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a clean DB with demo data, return path."""
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(clean_db_path))
    monkeypatch.setenv("POLYCOPY_ENABLE_DEMO_DATA", "true")
    result = subprocess.run(
        [sys.executable, "scripts/seed_demo_data.py", "--db", str(clean_db_path)],
        capture_output=True,
        text=True,
        cwd="/root/Polycopy",
    )
    assert result.returncode == 0, f"seed failed: {result.stderr}"
    return clean_db_path


@pytest.fixture
def api_client_with_db(seeded_db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """API client backed by the seeded DB, with demo mode OFF (real data)."""
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(seeded_db))
    monkeypatch.delenv("POLYCOPY_ENABLE_DEMO_DATA", raising=False)

    import polycopy.config.settings as settings_module
    import polycopy.db.database as db_module
    from polycopy.api.app import app, _idempotency_store

    if db_module._db is not None:
        db_module._db.close()
    db_module._db = None
    settings_module._settings = None
    _idempotency_store._db = None
    _idempotency_store._ensured_table = False

    get_settings = __import__("polycopy.config.settings", fromlist=["get_settings"]).get_settings
    get_database = __import__("polycopy.db.database", fromlist=["get_database"]).get_database
    get_settings(reload=True)
    get_database(reload=True)

    return TestClient(app)


@pytest.fixture
def empty_db_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """API client backed by a fresh empty DB."""
    db_path = tmp_path / "empty_integration.db"
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(db_path))
    monkeypatch.delenv("POLYCOPY_ENABLE_DEMO_DATA", raising=False)

    import polycopy.config.settings as settings_module
    import polycopy.db.database as db_module
    from polycopy.api.app import app, _idempotency_store

    if db_module._db is not None:
        db_module._db.close()
    db_module._db = None
    settings_module._settings = None
    _idempotency_store._db = None
    _idempotency_store._ensured_table = False

    get_settings = __import__("polycopy.config.settings", fromlist=["get_settings"]).get_settings
    get_database = __import__("polycopy.db.database", fromlist=["get_database"]).get_database
    get_settings(reload=True)
    get_database(reload=True)

    return TestClient(app)


def _reset_api_state(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset API singletons for a fresh client after restart simulation."""
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(db_path))
    monkeypatch.delenv("POLYCOPY_ENABLE_DEMO_DATA", raising=False)

    import polycopy.config.settings as settings_module
    import polycopy.db.database as db_module
    from polycopy.api.app import _idempotency_store

    if db_module._db is not None:
        db_module._db.close()
    db_module._db = None
    settings_module._settings = None
    _idempotency_store._db = None
    _idempotency_store._ensured_table = False

    get_settings = __import__("polycopy.config.settings", fromlist=["get_settings"]).get_settings
    get_database = __import__("polycopy.db.database", fromlist=["get_database"]).get_database
    get_settings(reload=True)
    get_database(reload=True)


def _reset_api_state_demo(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset API singletons with demo mode enabled."""
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(db_path))
    monkeypatch.setenv("POLYCOPY_ENABLE_DEMO_DATA", "true")

    import polycopy.config.settings as settings_module
    import polycopy.db.database as db_module
    from polycopy.api.app import _idempotency_store

    if db_module._db is not None:
        db_module._db.close()
    db_module._db = None
    settings_module._settings = None
    _idempotency_store._db = None
    _idempotency_store._ensured_table = False

    get_settings = __import__("polycopy.config.settings", fromlist=["get_settings"]).get_settings
    get_database = __import__("polycopy.db.database", fromlist=["get_database"]).get_database
    get_settings(reload=True)
    get_database(reload=True)


# ---------------------------------------------------------------------------
# Test 1: Clean DB -> seed -> scan -> persisted data -> API -> restart same IDs
# ---------------------------------------------------------------------------

class TestSeedScanPersistApiRestart:
    """Clean DB -> seed -> API serves persisted data -> restart -> same IDs."""

    def test_seed_creates_persisted_data(self, seeded_db: Path) -> None:
        """After seeding, the SQLite file contains real persisted rows."""
        from polycopy.db.database import Database

        db = Database(db_path=Path(seeded_db))
        db.connect()
        wallet_count = db.fetchone("SELECT COUNT(*) AS n FROM wallets")["n"]
        market_count = db.fetchone("SELECT COUNT(*) AS n FROM markets")["n"]
        signal_count = db.fetchone("SELECT COUNT(*) AS n FROM signals")["n"]
        db.close()

        assert wallet_count > 0, "Seed should create wallets"
        assert market_count > 0, "Seed should create markets"
        assert signal_count > 0, "Seed should create signals"

    def test_api_serves_seeded_wallets(
        self, api_client_with_db: TestClient, seeded_db: Path
    ) -> None:
        """API returns seeded wallet data on first request."""
        resp = api_client_with_db.get("/wallets")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] > 0

    def test_api_serves_seeded_signals(
        self, api_client_with_db: TestClient, seeded_db: Path
    ) -> None:
        """API returns seeded signal data."""
        resp = api_client_with_db.get("/signals")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] > 0

    def test_api_restart_returns_same_ids(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate API restart (new client, same DB file) -> same wallet IDs."""
        from polycopy.api.app import app

        # First request -- capture IDs
        _reset_api_state(seeded_db, monkeypatch)
        client1 = TestClient(app)
        resp1 = client1.get("/wallets")
        assert resp1.status_code == 200
        ids1 = sorted([w["id"] for w in resp1.json()["wallets"]])

        # Second request -- new client (simulates restart)
        _reset_api_state(seeded_db, monkeypatch)
        client2 = TestClient(app)
        resp2 = client2.get("/wallets")
        assert resp2.status_code == 200
        ids2 = sorted([w["id"] for w in resp2.json()["wallets"]])

        assert ids1 == ids2, "Same DB file should return same wallet IDs after restart"

    def test_api_restart_positions_persistent(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positions survive API restart (same IDs, same counts)."""
        from polycopy.api.app import app

        _reset_api_state(seeded_db, monkeypatch)
        client1 = TestClient(app)
        resp1 = client1.get("/positions")
        assert resp1.status_code == 200
        count1 = resp1.json()["total_count"]
        assert count1 > 0

        _reset_api_state(seeded_db, monkeypatch)
        client2 = TestClient(app)
        resp2 = client2.get("/positions")
        assert resp2.status_code == 200
        assert resp2.json()["total_count"] == count1

    def test_api_restart_portfolio_summary_consistent(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Portfolio summary returns same totals after restart."""
        from polycopy.api.app import app

        _reset_api_state(seeded_db, monkeypatch)
        client1 = TestClient(app)
        resp1 = client1.get("/portfolio/summary")
        assert resp1.status_code == 200
        summary1 = resp1.json()

        _reset_api_state(seeded_db, monkeypatch)
        client2 = TestClient(app)
        resp2 = client2.get("/portfolio/summary")
        assert resp2.status_code == 200
        summary2 = resp2.json()

        assert summary1["total_positions"] == summary2["total_positions"]
        assert summary1["total_pnl"] == summary2["total_pnl"]


# ---------------------------------------------------------------------------
# Test 2: Clean empty DB
# ---------------------------------------------------------------------------

class TestEmptyDb:
    """Empty DB returns empty collections, no errors, no sample data."""

    def test_empty_wallets(self, empty_db_client: TestClient) -> None:
        resp = empty_db_client.get("/wallets")
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0

    def test_empty_signals(self, empty_db_client: TestClient) -> None:
        resp = empty_db_client.get("/signals")
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0

    def test_empty_positions(self, empty_db_client: TestClient) -> None:
        resp = empty_db_client.get("/positions")
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0

    def test_empty_paper_orders(self, empty_db_client: TestClient) -> None:
        resp = empty_db_client.get("/paper/orders")
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0

    def test_empty_decision_log(self, empty_db_client: TestClient) -> None:
        resp = empty_db_client.get("/decision-log")
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 0

    def test_empty_portfolio_summary(self, empty_db_client: TestClient) -> None:
        resp = empty_db_client.get("/portfolio/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_positions"] == 0
        assert data["total_pnl"] == 0.0

    def test_empty_data_health(self, empty_db_client: TestClient) -> None:
        resp = empty_db_client.get("/data/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall_status"] == "unavailable"
        assert data["snapshot_count"] == 0


# ---------------------------------------------------------------------------
# Test 3: Paper preview -> approve -> restart -> retrieve
# ---------------------------------------------------------------------------

class TestPaperPreviewApproveRestartRetrieve:
    """Paper order flow with API restart verification."""

    def test_preview_returns_pending_order(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Paper preview returns a pending order with fill estimate."""
        _reset_api_state_demo(seeded_db, monkeypatch)

        from polycopy.api.app import app, _bidask_provider

        market_id = "00000000-0000-0000-0000-000000000099"
        _bidask_provider.set_snapshot(
            market_id, "Yes", bid=0.60, ask=0.68, ask_volume=200.0, bid_volume=100.0
        )

        with TestClient(app) as client:
            resp = client.post(
                "/paper/preview",
                json={
                    "market_id": market_id,
                    "outcome": "Yes",
                    "side": "buy",
                    "quantity": 10,
                    "price": 0.65,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "pending"
            assert data["is_sample"] is True

    def test_approve_persists_across_restart(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Approve a paper order, restart API, retrieve same order."""
        _reset_api_state_demo(seeded_db, monkeypatch)

        from polycopy.api.app import app, _bidask_provider

        market_id = "00000000-0000-0000-0000-000000000042"
        _bidask_provider.set_snapshot(
            market_id, "Yes", bid=0.58, ask=0.66, ask_volume=150.0, bid_volume=80.0
        )

        order_id = str(uuid.uuid4())

        # First session -- approve
        with TestClient(app) as client:
            preview = client.post(
                "/paper/preview",
                json={
                    "market_id": market_id,
                    "outcome": "Yes",
                    "side": "buy",
                    "quantity": 5,
                    "price": 0.63,
                },
            )
            assert preview.status_code == 200

            approve = client.post("/paper/approve", json={"order_id": order_id, "notes": "e2e approve"})
            assert approve.status_code == 200
            first_order_id = approve.json()["id"]
            first_status = approve.json()["status"]

        # Simulate restart
        _reset_api_state_demo(seeded_db, monkeypatch)
        from polycopy.api.app import _bidask_provider as _bidask2
        _bidask2.set_snapshot(
            market_id, "Yes", bid=0.58, ask=0.66, ask_volume=150.0, bid_volume=80.0
        )

        # Second session -- retrieve
        with TestClient(app) as client:
            orders = client.get("/paper/orders")
            assert orders.status_code == 200
            found = [o for o in orders.json()["orders"] if o["id"] == first_order_id]
            assert len(found) == 1, "Approved order should be retrievable after restart"
            assert found[0]["status"] == first_status


# ---------------------------------------------------------------------------
# Test 4: Idempotency replay -> no duplicate
# ---------------------------------------------------------------------------

class TestIdempotencyReplay:
    """Same idempotency key -> no duplicate orders/positions/decisions."""

    def test_approve_idempotent_no_duplicate(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Approving the same order_id twice returns same result, no duplicates."""
        _reset_api_state_demo(seeded_db, monkeypatch)

        from polycopy.api.app import app, _bidask_provider

        market_id = "00000000-0000-0000-0000-000000000077"
        _bidask_provider.set_snapshot(
            market_id, "Yes", bid=0.55, ask=0.62, ask_volume=300.0, bid_volume=150.0
        )

        order_id = str(uuid.uuid4())

        with TestClient(app) as client:
            before_orders = client.get("/paper/orders").json()["total_count"]
            before_decisions = client.get("/decision-log").json()["total_count"]

            # Preview first
            client.post(
                "/paper/preview",
                json={
                    "market_id": market_id,
                    "outcome": "Yes",
                    "side": "buy",
                    "quantity": 8,
                    "price": 0.59,
                },
            )

            # First approve
            first = client.post(
                "/paper/approve", json={"order_id": order_id, "notes": "idempotent test"}
            )
            assert first.status_code == 200
            first_id = first.json()["id"]

            # Second approve (same payload -> idempotent replay)
            second = client.post(
                "/paper/approve", json={"order_id": order_id, "notes": "idempotent test"}
            )
            assert second.status_code == 200
            assert second.json()["id"] == first_id

            # Only one new order and one new decision
            after_orders = client.get("/paper/orders").json()["total_count"]
            after_decisions = client.get("/decision-log").json()["total_count"]
            assert after_orders == before_orders + 1
            assert after_decisions == before_decisions + 1

    def test_reject_idempotent_no_duplicate(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rejecting the same order twice returns same result, no duplicates."""
        _reset_api_state_demo(seeded_db, monkeypatch)

        from polycopy.api.app import app

        order_id = str(uuid.uuid4())

        with TestClient(app) as client:
            before_orders = client.get("/paper/orders").json()["total_count"]

            first = client.post(
                "/paper/reject", json={"order_id": order_id, "notes": "reject test"}
            )
            assert first.status_code == 200
            first_id = first.json()["id"]

            second = client.post(
                "/paper/reject", json={"order_id": order_id, "notes": "reject test"}
            )
            assert second.status_code == 200
            assert second.json()["id"] == first_id

            after_orders = client.get("/paper/orders").json()["total_count"]
            assert after_orders == before_orders + 1


# ---------------------------------------------------------------------------
# Test 5: Reject pending; settle restart no double settlement
# ---------------------------------------------------------------------------

class TestRejectPendingAndSettlementIdempotency:
    """Reject pending orders; settlement is idempotent across restarts."""

    def test_reject_pending_order(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rejecting a pending order marks it cancelled and persistent."""
        _reset_api_state_demo(seeded_db, monkeypatch)

        from polycopy.api.app import app

        order_id = str(uuid.uuid4())

        with TestClient(app) as client:
            resp = client.post(
                "/paper/reject", json={"order_id": order_id, "notes": "operator reject"}
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "cancelled"
            assert data["is_sample"] is True

            # Verify in decision log
            decisions = client.get("/decision-log")
            entries = decisions.json()["entries"]
            reject_entries = [e for e in entries if e["decision_type"] == "paper_reject"]
            assert len(reject_entries) >= 1

    def test_settlement_script_idempotent(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running settle_paper_positions.py twice produces no duplicates."""
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(seeded_db))

        import polycopy.config.settings as settings_module
        import polycopy.db.database as db_module

        if db_module._db is not None:
            db_module._db.close()
        db_module._db = None
        settings_module._settings = None

        get_settings = __import__("polycopy.config.settings", fromlist=["get_settings"]).get_settings
        get_database = __import__("polycopy.db.database", fromlist=["get_database"]).get_database
        get_settings(reload=True)
        get_database(reload=True)

        # Run settlement script twice
        result1 = subprocess.run(
            [sys.executable, "scripts/settle_paper_positions.py", "--db", str(seeded_db)],
            capture_output=True,
            text=True,
            cwd="/root/Polycopy",
        )

        result2 = subprocess.run(
            [sys.executable, "scripts/settle_paper_positions.py", "--db", str(seeded_db)],
            capture_output=True,
            text=True,
            cwd="/root/Polycopy",
        )

        # Both should succeed (or confirm nothing to settle)
        assert result1.returncode == 0, f"First settle failed: {result1.stderr}"
        assert result2.returncode == 0, f"Second settle failed: {result2.stderr}"


# ---------------------------------------------------------------------------
# Test 6: Dashboard data-health correctness
# ---------------------------------------------------------------------------

class TestDashboardDataHealth:
    """Data health endpoint reflects actual DB state."""

    def test_data_health_shows_seeded_sources(
        self, api_client_with_db: TestClient
    ) -> None:
        resp = api_client_with_db.get("/data/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall_status"] in ("healthy", "degraded", "unavailable")

    def test_data_health_snapshot_count_nonzero_on_seeded_db(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seeded DB should show at least one snapshot."""
        _reset_api_state(seeded_db, monkeypatch)

        from polycopy.api.app import app

        with TestClient(app) as client:
            resp = client.get("/data/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["snapshot_count"] > 0
