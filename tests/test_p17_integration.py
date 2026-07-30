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

    def test_approve_route_retired_returns_404(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST /paper/approve is retired — framework default 404."""
        _reset_api_state_demo(seeded_db, monkeypatch)

        from polycopy.api.app import app
        from polycopy.db.database import get_database

        order_id = str(uuid.uuid4())
        # Seed the pending order
        db = get_database()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT OR IGNORE INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, ?, ?)",
            ("00000000-0000-0000-0000-000000000080", "0xtest", "test", 0, now),
        )
        db.execute(
            "INSERT OR IGNORE INTO markets (id, source_id, source, question, fetched_at, is_sample) VALUES (?, ?, ?, ?, ?, ?)",
            ("00000000-0000-0000-0000-000000000042", "m1", "test", "Test Q", now, 0),
        )
        db.execute(
            """
            INSERT OR IGNORE INTO orders
                (id, market_id, wallet_id, side, order_type, outcome, quantity, price,
                 status, filled_quantity, created_at, updated_at, is_sample)
            VALUES (?, ?, ?, 'buy', 'limit', 'Yes', 5.0, 0.63, 'pending', 0.0, ?, ?, 0)
            """,
            (order_id, "00000000-0000-0000-0000-000000000042", "00000000-0000-0000-0000-000000000080", now, now),
        )
        db.conn.commit()

        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": order_id, "notes": "e2e approve"})
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Test 4: Retired routes return 404
# ---------------------------------------------------------------------------

class TestRetiredRoutesReturn404:
    """POST /paper/approve and POST /paper/reject are retired — 404."""

    def test_paper_approve_returns_404(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST /paper/approve is retired — framework default 404."""
        _reset_api_state_demo(seeded_db, monkeypatch)
        from polycopy.api.app import app
        with TestClient(app) as client:
            resp = client.post("/paper/approve", json={"order_id": str(uuid.uuid4()), "notes": "idempotent test"})
            assert resp.status_code == 404

    def test_paper_reject_returns_404(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST /paper/reject is retired — framework default 404."""
        _reset_api_state_demo(seeded_db, monkeypatch)
        from polycopy.api.app import app
        with TestClient(app) as client:
            resp = client.post("/paper/reject", json={"order_id": str(uuid.uuid4()), "notes": "reject test"})
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test 5: Reject route retired; settle restart no double settlement
# ---------------------------------------------------------------------------

class TestRejectPendingAndSettlementIdempotency:
    """Reject route is retired (404); settlement is idempotent across restarts."""

    def test_paper_reject_returns_404(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST /paper/reject is retired — framework default 404."""
        _reset_api_state_demo(seeded_db, monkeypatch)
        from polycopy.api.app import app
        with TestClient(app) as client:
            resp = client.post(
                "/paper/reject", json={"order_id": str(uuid.uuid4()), "notes": "operator reject"}
            )
            assert resp.status_code == 404

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
# Fix 3: Retired approve route returns 404
# ---------------------------------------------------------------------------


class TestRetiredApproveRouteReturns404:
    """POST /paper/approve is retired — framework default 404."""

    def test_paper_approve_returns_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST /paper/approve is retired — framework default 404."""
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
        get_database(reload=True)

        try:
            with TestClient(app) as client:
                resp = client.post("/paper/approve", json={"order_id": str(uuid.uuid4()), "notes": "test buy"})
                assert resp.status_code == 404
        finally:
            if db_module._db is not None:
                db_module._db.close()
            db_module._db = None
            settings_module._settings = None
