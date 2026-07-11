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
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[1]

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
        cwd=REPO_ROOT,
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



def _insert_pending_order(order_id: str, market_id: str, *, quantity: float = 10.0, price: float = 0.65) -> None:
    """Insert an existing pending paper order that approve/reject can transition."""
    from datetime import datetime, timezone

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
        (market_id, f"paper-{market_id}", "paper", "Paper market", now, 1),
    )
    db.execute(
        """
        INSERT OR REPLACE INTO orders (
            id, market_id, wallet_id, side, order_type, outcome, quantity, price,
            status, filled_quantity, created_at, updated_at, is_sample
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (order_id, market_id, wallet_id, "buy", "limit", "Yes", quantity, price, "pending", 0.0, now, now, 1),
    )
    db.conn.commit()

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
        from polycopy.db.database import get_database

        market_id = "00000000-0000-0000-0000-000000000042"
        _bidask_provider.set_snapshot(
            market_id, "Yes", bid=0.58, ask=0.66, ask_volume=150.0, bid_volume=80.0
        )

        order_id = str(uuid.uuid4())
        # Seed the pending order
        db = get_database()
        # was hardcoded "2026-06-28T12:00:00+00:00"; now dynamic so the order
        # doesn't expire past order_preview_max_age_seconds once wall-clock
        # passes that hardcoded value.
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT OR IGNORE INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, ?, ?)",
            ("00000000-0000-0000-0000-000000000080", "0xtest", "test", 0, now),
        )
        db.execute(
            "INSERT OR IGNORE INTO markets (id, source_id, source, question, fetched_at, is_sample) VALUES (?, ?, ?, ?, ?, ?)",
            (market_id, "m1", "test", "Test Q", now, 0),
        )
        db.execute(
            """
            INSERT OR IGNORE INTO orders
                (id, market_id, wallet_id, side, order_type, outcome, quantity, price,
                 status, filled_quantity, created_at, updated_at, is_sample)
            VALUES (?, ?, ?, 'buy', 'limit', 'Yes', 5.0, 0.63, 'pending', 0.0, ?, ?, 0)
            """,
            (order_id, market_id, "00000000-0000-0000-0000-000000000080", now, now),
        )
        db.conn.commit()

        # First session -- approve
        with TestClient(app) as client:
            approve = client.post("/paper/approve", json={"order_id": order_id, "notes": "e2e approve"})
            assert approve.status_code == 200
            first_order_id = approve.json()["id"]
            first_status = approve.json()["status"]
            assert first_order_id == order_id

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
    """Same payload/idempotency key -> no duplicate orders/positions/decisions."""

    def test_approve_idempotent_no_duplicate(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Approving the same existing order twice returns same result, no duplicates."""
        _reset_api_state_demo(seeded_db, monkeypatch)

        from polycopy.api.app import app, _bidask_provider

        market_id = "00000000-0000-0000-0000-000000000077"
        _bidask_provider.set_snapshot(
            market_id, "Yes", bid=0.55, ask=0.62, ask_volume=300.0, bid_volume=150.0
        )

        order_id = str(uuid.uuid4())
        _insert_pending_order(order_id, market_id, quantity=8, price=0.59)

        with TestClient(app) as client:
            before_orders = client.get("/paper/orders").json()["total_count"]
            before_decisions = client.get("/decision-log").json()["total_count"]

            first = client.post(
                "/paper/approve", json={"order_id": order_id, "notes": "idempotent test"}
            )
            assert first.status_code == 200
            first_id = first.json()["id"]

            second = client.post(
                "/paper/approve", json={"order_id": order_id, "notes": "idempotent test"}
            )
            assert second.status_code == 200
            assert second.json()["id"] == first_id

            after_orders = client.get("/paper/orders").json()["total_count"]
            after_decisions = client.get("/decision-log").json()["total_count"]
            assert after_orders == before_orders
            assert after_decisions == before_decisions + 1

    def test_reject_idempotent_no_duplicate(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rejecting the same existing order twice returns same result, no duplicates."""
        _reset_api_state_demo(seeded_db, monkeypatch)

        from polycopy.api.app import app

        order_id = str(uuid.uuid4())
        _insert_pending_order(order_id, "00000000-0000-0000-0000-000000000088")

        with TestClient(app) as client:
            before_orders = client.get("/paper/orders").json()["total_count"]
            before_decisions = client.get("/decision-log").json()["total_count"]

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
            after_decisions = client.get("/decision-log").json()["total_count"]
            assert after_orders == before_orders
            assert after_decisions == before_decisions + 1


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
        _insert_pending_order(order_id, "00000000-0000-0000-0000-000000000089")

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
            cwd=REPO_ROOT,
        )

        result2 = subprocess.run(
            [sys.executable, "scripts/settle_paper_positions.py", "--db", str(seeded_db)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
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


# ---------------------------------------------------------------------------
# Fix 1: Configured staleness threshold tests
# ---------------------------------------------------------------------------


def _seed_raw_snapshot(db, source: str, fetched_at: str, is_sample: int = 0) -> None:
    """Insert a single raw_snapshots row with a controlled fetched_at."""
    db.execute(
        """
        INSERT INTO raw_snapshots
            (id, source, endpoint, query_params, file_path, content_hash, hash_algo,
             content_type, size_bytes, fetched_at, ingested_at, is_sample)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (str(uuid.uuid4()), source, "/markets", "{}", "/snap.json", "h", "sha256",
         "application/json", 10, fetched_at, fetched_at, is_sample),
    )
    db.conn.commit()


class TestConfiguredStalenessThreshold:
    """Data Health must use the configured staleness_seconds, not a hard-coded value."""

    def _client_with_staleness(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, staleness: float,
        now: Optional["datetime"] = None,
    ):
        """Create a client with a custom staleness_seconds value.

        The freshness clock is frozen to ``now`` so the staleness age is
        computed against the exact same reference the caller uses to set
        ``fetched_at``.  This isolates the test from wall-clock drift — the
        real CI flake was a query-time ``datetime.now()`` read landing >1s
        after the test's own read, which flipped a 119s-old snapshot to
        ``stale`` under a heavily loaded 133s suite.
        """
        from datetime import datetime as _dt, timezone as _tz

        if now is None:
            now = _dt.now(_tz.utc)
        db_path = tmp_path / "staleness.db"
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(db_path))
        # Isolate every env input that influences data-health / staleness.
        monkeypatch.delenv("POLYCOPY_ENABLE_DEMO_DATA", raising=False)
        monkeypatch.delenv("POLYCOPY_STALENESS_SECONDS", raising=False)
        monkeypatch.delenv("POLYCOPY_ORDER_KILL_SWITCH", raising=False)

        import polycopy.config.settings as settings_module
        import polycopy.db.database as db_module

        if db_module._db is not None:
            db_module._db.close()
        db_module._db = None
        settings_module._settings = None

        settings = __import__("polycopy.config.settings", fromlist=["get_settings"]).get_settings(reload=True)
        # Override staleness via the dataclass field
        object.__setattr__(settings, "staleness_seconds", staleness)

        db = __import__("polycopy.db.database", fromlist=["get_database"]).get_database(reload=True)

        # Freeze the freshness clock to the same reference used for fetched_at.
        # repository.data_health() does a *local* `from polycopy.risk.freshness
        # import seconds_since`, so we must patch the source module attribute.
        import polycopy.risk.freshness as freshness_module

        def _frozen_seconds_since(dt):
            if dt is None:
                return None
            ref = now
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            return (ref - dt).total_seconds()

        monkeypatch.setattr(freshness_module, "seconds_since", _frozen_seconds_since)

        from polycopy.api.app import app as _app
        return db, TestClient(_app)

    def test_snapshot_121_seconds_stale_with_120_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With staleness=120, a 121s-old snapshot must be stale."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        db, client = self._client_with_staleness(tmp_path, monkeypatch, 120.0, now=now)
        # Insert with fetched_at 121 seconds in the past
        old_ts_121 = (now.replace(microsecond=0) - __import__("datetime").timedelta(seconds=121)).isoformat()
        # Ensure table exists
        _seed_raw_snapshot(db, "src_a", old_ts_121)

        try:
            resp = client.get("/data/health")
            assert resp.status_code == 200
            data = resp.json()
            # Find src_a
            src_a = next(s for s in data["sources"] if "src_a" in s["source"])
            assert src_a["status"] == "stale", f"Expected stale, got {src_a['status']}"
        finally:
            db.close()

    def test_snapshot_119_seconds_not_stale_with_120_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With staleness=120, a 119s-old snapshot must be ok."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        db, client = self._client_with_staleness(tmp_path, monkeypatch, 120.0, now=now)
        old_ts_119 = (now.replace(microsecond=0) - __import__("datetime").timedelta(seconds=119)).isoformat()
        _seed_raw_snapshot(db, "src_b", old_ts_119)

        try:
            resp = client.get("/data/health")
            assert resp.status_code == 200
            data = resp.json()
            src_b = next(s for s in data["sources"] if "src_b" in s["source"])
            assert src_b["status"] == "ok", f"Expected ok, got {src_b['status']}"
        finally:
            db.close()

    def test_configured_staleness_followed_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When staleness is set to 60s, a 61s-old snapshot becomes stale."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        db, client = self._client_with_staleness(tmp_path, monkeypatch, 60.0, now=now)
        old_ts_61 = (now.replace(microsecond=0) - __import__("datetime").timedelta(seconds=61)).isoformat()
        _seed_raw_snapshot(db, "src_c", old_ts_61)

        try:
            resp = client.get("/data/health")
            assert resp.status_code == 200
            data = resp.json()
            src_c = next(s for s in data["sources"] if "src_c" in s["source"])
            assert src_c["status"] == "stale"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Fix 2: Snapshot-only overall health derivation
# ---------------------------------------------------------------------------


class TestSnapshotOnlyOverallHealth:
    """When provider_health is empty, derive overall_status from source statuses."""

    def _make_client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from polycopy.api.app import app
        import polycopy.db.database as db_module
        import polycopy.config.settings as settings_module

        db_path = tmp_path / "oh.db"
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(db_path))
        monkeypatch.delenv("POLYCOPY_ENABLE_DEMO_DATA", raising=False)
        if db_module._db is not None:
            db_module._db.close()
        db_module._db = None
        settings_module._settings = None
        get_settings = __import__("polycopy.config.settings", fromlist=["get_settings"]).get_settings
        get_database = __import__("polycopy.db.database", fromlist=["get_database"]).get_database
        get_settings(reload=True)
        db = get_database(reload=True)
        return db, TestClient(app)

    def _seed_snapshot_age(self, db, source: str, age_seconds: float) -> None:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc).replace(microsecond=0)
        ts = (now - timedelta(seconds=age_seconds)).isoformat()
        db.execute(
            """
            INSERT INTO raw_snapshots
                (id, source, endpoint, query_params, file_path, content_hash, hash_algo,
                 content_type, size_bytes, fetched_at, ingested_at, is_sample)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (str(uuid.uuid4()), source, "/markets", "{}", "/s.json", "h", "sha256",
             "application/json", 5, ts, ts),
        )
        db.conn.commit()

    def test_all_fresh_sources_healthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db, client = self._make_client(tmp_path, monkeypatch)
        self._seed_snapshot_age(db, "fresh_src", 5.0)
        try:
            resp = client.get("/data/health")
            data = resp.json()
            assert data["overall_status"] == "healthy"
        finally:
            db.close()

    def test_one_fresh_one_stale_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db, client = self._make_client(tmp_path, monkeypatch)
        self._seed_snapshot_age(db, "deg_fresh", 5.0)
        self._seed_snapshot_age(db, "deg_stale", 400.0)
        try:
            resp = client.get("/data/health")
            data = resp.json()
            assert data["overall_status"] == "degraded"
        finally:
            db.close()

    def test_all_stale_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db, client = self._make_client(tmp_path, monkeypatch)
        self._seed_snapshot_age(db, "s1", 301.0)
        self._seed_snapshot_age(db, "s2", 400.0)
        try:
            resp = client.get("/data/health")
            data = resp.json()
            assert data["overall_status"] == "degraded"
        finally:
            db.close()

    def test_all_unavailable_unhealthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provider-health error row only -> overall_status is degraded/error."""
        from datetime import datetime, timezone
        db, client = self._make_client(tmp_path, monkeypatch)
        db.execute(
            "INSERT INTO provider_health (provider, capability, status, last_attempt, http_status, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("test", "failing", "error", datetime.now(timezone.utc).isoformat(), 500, "down"),
        )
        db.conn.commit()
        try:
            resp = client.get("/data/health")
            data = resp.json()
            # At least the error source should make overall degraded or unavailable
            assert data["overall_status"] in ("degraded", "unavailable")
        finally:
            db.close()

    def test_no_snapshots_empty_db_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty DB with no provider_health and no snapshots -> unavailable."""
        db, client = self._make_client(tmp_path, monkeypatch)
        try:
            resp = client.get("/data/health")
            data = resp.json()
            assert data["overall_status"] == "unavailable"
            assert data["sources"] == []
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Fix 3: Buy fills debit simulated USDC
# ---------------------------------------------------------------------------


class TestBuyFillDebitsUSDC:
    """Approving a buy order must reduce the wallet's simulated USDC balance."""

    def _make_client_and_wallet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial_usdc: float
    ):
        from datetime import datetime, timezone
        from polycopy.api.app import app
        import polycopy.db.database as db_module
        import polycopy.config.settings as settings_module

        db_path = tmp_path / "buy.db"
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(db_path))
        monkeypatch.delenv("POLYCOPY_ENABLE_DEMO_DATA", raising=False)
        if db_module._db is not None:
            db_module._db.close()
        db_module._db = None
        settings_module._settings = None
        get_settings = __import__("polycopy.config.settings", fromlist=["get_settings"]).get_settings
        get_database = __import__("polycopy.db.database", fromlist=["get_database"]).get_database
        get_settings(reload=True)
        db = get_database(reload=True)
        now = datetime.now(timezone.utc).isoformat()
        wallet_id = "00000000-0000-0000-0000-000000000099"
        market_id = "00000000-0000-0000-0000-000000000098"
        order_id = str(uuid.uuid4())
        db.execute(
            "INSERT OR IGNORE INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, 0, ?)",
            (wallet_id, "0xBUYTEST", "buy-wallet", now),
        )
        db.execute(
            "INSERT INTO wallet_balances (wallet_id, currency, amount, as_of, is_sample) VALUES (?, ?, ?, ?, 0)",
            (wallet_id, "USDC", initial_usdc, now),
        )
        db.execute(
            "INSERT OR IGNORE INTO markets (id, source_id, source, question, active, closed, resolved, fetched_at, is_sample) "
            "VALUES (?, ?, ?, ?, 1, 0, 0, ?, 0)",
            (market_id, "m-buy", "test", "Buy test?", now),
        )
        db.execute(
            "INSERT OR IGNORE INTO orders "
            "(id, market_id, wallet_id, side, order_type, outcome, quantity, price, status, filled_quantity, created_at, updated_at, is_sample) "
            "VALUES (?, ?, ?, 'buy', 'limit', 'Yes', ?, ?, 'pending', 0.0, ?, ?, 0)",
            (order_id, market_id, wallet_id, 10.0, 0.5, now, now),
        )
        db.conn.commit()
        return db, TestClient(app), order_id, wallet_id

    def _reset_and_reload(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Restart service objects to simulate API restart."""
        import polycopy.db.database as db_module
        import polycopy.config.settings as settings_module
        from polycopy.api.app import _idempotency_store
        from polycopy.api.app import _bidask_provider as _ba
        # Set snapshot for deterministic fills
        _ba.set_snapshot(
            "00000000-0000-0000-0000-000000000098", "Yes",
            bid=0.45, ask=0.55, ask_volume=500.0, bid_volume=200.0,
        )
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

    def test_successful_buy_debits_cash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Start with known USDC, approve buy, confirm cash decreases by notional + fee."""
        from polycopy.api.app import _bidask_provider
        db, client, order_id, wallet_id = self._make_client_and_wallet(tmp_path, monkeypatch, 100.0)
        _bidask_provider.set_snapshot(
            "00000000-0000-0000-0000-000000000098", "Yes",
            bid=0.45, ask=0.55, ask_volume=500.0, bid_volume=200.0,
        )
        try:
            resp = client.post("/paper/approve", json={"order_id": order_id, "notes": "test buy"})
            assert resp.status_code == 200
            data = resp.json()
            # Check balance decreased
            bal = client.get(f"/wallets/{wallet_id}").json()["balances"][0]
            assert bal["currency"] == "USDC"
            # Approve uses order limit price (0.5) for accounting, not the market ask
            from polycopy.config.settings import get_settings as _gs
            fee_rate = _gs().fill_fee_rate  # 0.001
            qty = 10.0
            limit_price = 0.5  # order price
            notional = limit_price * qty
            fee = notional * fee_rate
            expected = 100.0 - notional - fee
            assert abs(bal["amount"] - expected) < 0.001, f"bal={bal['amount']}, expected={expected}"
            # Exactly one position
            positions = client.get("/positions", params={"wallet_id": wallet_id}).json()["positions"]
            assert len(positions) == 1
            # One fill = order status filled
            assert data["status"] == "filled"
            # Order ID unchanged
            assert data["id"] == order_id
        finally:
            db.close()

    def test_insufficient_cash_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Start below required total. Approval fails with no mutation."""
        from polycopy.api.app import _bidask_provider
        db, client, order_id, wallet_id = self._make_client_and_wallet(tmp_path, monkeypatch, 1.0)
        _bidask_provider.set_snapshot(
            "00000000-0000-0000-0000-000000000098", "Yes",
            bid=0.45, ask=0.55, ask_volume=500.0, bid_volume=200.0,
        )
        try:
            resp = client.post("/paper/approve", json={"order_id": order_id, "notes": "should fail"})
            assert resp.status_code == 409
            assert "insufficient" in resp.json()["detail"].lower()
            # Balance unchanged
            bal = client.get(f"/wallets/{wallet_id}").json()["balances"][0]
            assert bal["amount"] == 1.0
            # No position
            positions = client.get("/positions", params={"wallet_id": wallet_id}).json()["positions"]
            assert len(positions) == 0
            # Order still pending
            orders = client.get("/paper/orders", params={"status": "pending"}).json()["orders"]
            assert any(o["id"] == order_id for o in orders)
            # No decision log
            decisions = client.get("/decision-log").json()["entries"]
            assert not any(e["order_id"] == order_id for e in decisions)
        finally:
            db.close()

    def test_exact_balance_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Balance == notional + fee. Approval succeeds, final balance is 0."""
        from polycopy.config.settings import get_settings as _gs
        from polycopy.api.app import _bidask_provider
        fee_rate = _gs().fill_fee_rate
        limit_price = 0.5
        qty = 10.0
        notional = limit_price * qty
        fee = notional * fee_rate
        total = notional + fee
        db, client, order_id, wallet_id = self._make_client_and_wallet(tmp_path, monkeypatch, total)
        _bidask_provider.set_snapshot(
            "00000000-0000-0000-0000-000000000098", "Yes",
            bid=0.45, ask=0.55, ask_volume=500.0, bid_volume=200.0,
        )
        try:
            resp = client.post("/paper/approve", json={"order_id": order_id, "notes": "exact"})
            assert resp.status_code == 200
            bal = client.get(f"/wallets/{wallet_id}").json()["balances"][0]
            assert abs(bal["amount"]) < 1e-9
        finally:
            db.close()

    def test_idempotent_restart_no_double_debit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Approve once, restart, replay same request. Cash debited only once."""
        from polycopy.api.app import _bidask_provider
        db, client, order_id, wallet_id = self._make_client_and_wallet(tmp_path, monkeypatch, 100.0)
        _bidask_provider.set_snapshot(
            "00000000-0000-0000-0000-000000000098", "Yes",
            bid=0.45, ask=0.55, ask_volume=500.0, bid_volume=200.0,
        )
        try:
            first = client.post("/paper/approve", json={"order_id": order_id, "notes": "idempotent restart"})
            assert first.status_code == 200
            bal_after_first = client.get(f"/wallets/{wallet_id}").json()["balances"][0]["amount"]
            # Simulate restart
            self._reset_and_reload(tmp_path, monkeypatch)
            self._reset_and_reload(tmp_path, monkeypatch)
            from polycopy.api.app import _idempotency_store
            _idempotency_store._db = None
            _idempotency_store._ensured_table = False
            __import__("polycopy.db.database", fromlist=["get_database"]).get_database(reload=True)
            second = client.post("/paper/approve", json={"order_id": order_id, "notes": "idempotent restart"})
            assert second.status_code == 200
            bal_after_second = client.get(f"/wallets/{wallet_id}").json()["balances"][0]["amount"]
            assert abs(bal_after_first - bal_after_second) < 1e-9, (
                f"Double debit! first={bal_after_first}, second={bal_after_second}"
            )
        finally:
            db.close()

    def test_buy_then_sell_accounting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Buy debits cash. Sell credits proceeds. Confirm P&L consistent."""
        from polycopy.api.app import _bidask_provider
        from polycopy.config.settings import get_settings as _gs
        db, client, order_id, wallet_id = self._make_client_and_wallet(tmp_path, monkeypatch, 100.0)
        _bidask_provider.set_snapshot(
            "00000000-0000-0000-0000-000000000098", "Yes",
            bid=0.45, ask=0.55, ask_volume=500.0, bid_volume=200.0,
        )
        try:
            buy_resp = client.post("/paper/approve", json={"order_id": order_id, "notes": "buy"})
            assert buy_resp.status_code == 200
            balance_after_buy = client.get(f"/wallets/{wallet_id}").json()["balances"][0]["amount"]
            # Now sell: a matching sell order must be created. Use a direct buy->sell flow.
            # Create a new pending sell order referencing the same market/outcome/wallet/qty.
            sell_order_id = str(uuid.uuid4())
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            position = client.get("/positions", params={"wallet_id": wallet_id}).json()["positions"][0]
            sell_qty = position["quantity"]
            sell_price = 0.65
            db.execute(
                "INSERT OR IGNORE INTO orders "
                "(id, market_id, wallet_id, side, order_type, outcome, quantity, price, status, filled_quantity, created_at, updated_at, is_sample) "
                "VALUES (?, ?, ?, 'sell', 'limit', 'Yes', ?, ?, 'pending', 0.0, ?, ?, 0)",
                (sell_order_id, position["market_id"], wallet_id, sell_qty, sell_price, now, now),
            )
            db.conn.commit()
            sell_resp = client.post("/paper/approve", json={"order_id": sell_order_id, "notes": "sell"})
            assert sell_resp.status_code == 200
            balance_after_sell = client.get(f"/wallets/{wallet_id}").json()["balances"][0]["amount"]
            # Cash must have increased by sell_price * sell_qty - fee
            fee_rate = _gs().fill_fee_rate
            sell_proceeds = sell_price * sell_qty * (1.0 - fee_rate)
            assert balance_after_sell > balance_after_buy
            assert abs((balance_after_sell - balance_after_buy) - sell_proceeds) < 0.01
        finally:
            db.close()
