"""P15 tests: live-read-only Polymarket adapter and data-health correctness."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from polycopy.adapters.polymarket import PolymarketPublicAdapter
from polycopy.api.app import app
from polycopy.config.settings import get_settings
from polycopy.db import database as db_module
from polycopy.db.database import get_database


NOW = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc).isoformat()


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p15.db"))
    monkeypatch.delenv("POLYCOPY_ENABLE_DEMO_DATA", raising=False)
    get_settings(reload=True)
    get_database(reload=True)
    yield TestClient(app)
    if db_module._db is not None:
        db_module._db.close()
        db_module._db = None
    get_settings(reload=True)


# ── PolymarketPublicAdapter unit tests ────────────────────────────────────────


class TestPolymarketPublicAdapterConstruction:
    """Adapter can be constructed with configurable URLs and timeouts."""

    def test_default_construction(self):
        adapter = PolymarketPublicAdapter(
            gamma_base_url="https://gamma-api.polymarket.com",
            clob_base_url="https://clob.polymarket.com",
        )
        assert adapter.gamma_base_url == "https://gamma-api.polymarket.com"
        assert adapter.clob_base_url == "https://clob.polymarket.com"

    def test_trailing_slash_stripped(self):
        adapter = PolymarketPublicAdapter(
            gamma_base_url="https://gamma-api.polymarket.com/",
            clob_base_url="https://clob.polymarket.com/",
        )
        assert not adapter.gamma_base_url.endswith("/")
        assert not adapter.clob_base_url.endswith("/")


class TestPolymarketMarketParsing:
    """Gamma market JSON is parsed into our domain model correctly."""

    def test_parse_standard_market(self):
        data = {
            "conditionId": "0xabc123",
            "question": "Will X happen?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.65", "0.35"]',
            "active": True,
            "closed": False,
            "resolved": False,
            "volume24hr": 1500.5,
        }
        market = PolymarketPublicAdapter._parse_gamma_market(data)
        assert market.source_id == "0xabc123"
        assert market.question == "Will X happen?"
        assert market.source == "polymarket"
        assert market.active is True
        assert market.closed is False
        assert market.resolved is False
        assert market.volume_24h == 1500.5
        assert market.is_sample is False
        assert len(market.outcomes) == 2
        assert market.outcomes[0].label == "Yes"
        assert market.outcomes[0].price == 0.65
        assert market.outcomes[1].label == "No"
        assert market.outcomes[1].price == 0.35

    def test_parse_resolved_market(self):
        data = {
            "conditionId": "0xdef456",
            "question": "Old question?",
            "outcomes": '["A", "B"]',
            "outcomePrices": '["1.0", "0.0"]',
            "active": False,
            "closed": True,
            "resolved": True,
            "resolutionOutcome": "A",
            "volume24hr": 0,
        }
        market = PolymarketPublicAdapter._parse_gamma_market(data)
        assert market.resolved is True
        assert market.resolution_outcome == "A"
        assert market.closed is True

    def test_parse_with_list_outcomes(self):
        """Some Gamma responses have outcomes as actual lists, not JSON strings."""
        data = {
            "conditionId": "0xghi789",
            "question": "List outcomes?",
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.5", "0.5"],
            "active": True,
            "closed": False,
            "volume24hr": 100,
        }
        market = PolymarketPublicAdapter._parse_gamma_market(data)
        assert len(market.outcomes) == 2
        assert market.outcomes[0].price == 0.5

    def test_parse_missing_fields_defaults(self):
        """Minimal data still produces a valid market."""
        data = {
            "conditionId": "0xminimal",
            "question": "Minimal?",
        }
        market = PolymarketPublicAdapter._parse_gamma_market(data)
        assert market.active is False
        assert market.closed is False
        assert market.volume_24h == 0
        assert market.outcomes == []
        assert market.is_sample is False


class TestPolymarketTokenParsing:
    """CLOB token list is parsed into MarketOutcome."""

    def test_parse_clob_tokens(self):
        tokens = [
            {"token_id": "111", "outcome": "Yes", "price": 0.7},
            {"token_id": "222", "outcome": "No", "price": 0.3},
        ]
        outcomes = PolymarketPublicAdapter.parse_clob_tokens(tokens)
        assert len(outcomes) == 2
        assert outcomes[0].label == "Yes"
        assert outcomes[0].price == 0.7
        assert outcomes[1].label == "No"
        assert outcomes[1].price == 0.3

    def test_parse_clob_tokens_clamps_price(self):
        """Prices outside [0, 1] are clamped."""
        tokens = [
            {"token_id": "1", "outcome": "A", "price": 1.5},
            {"token_id": "2", "outcome": "B", "price": -0.2},
        ]
        outcomes = PolymarketPublicAdapter.parse_clob_tokens(tokens)
        assert outcomes[0].price == 1.0
        assert outcomes[1].price == 0.0

    def test_parse_clob_tokens_empty(self):
        assert PolymarketPublicAdapter.parse_clob_tokens([]) == []


# ── Data health endpoint tests ─────────────────────────────────────────────────


class TestDataHealthEmptyDB:
    """Data health returns correct structure when DB has no data."""

    def test_empty_db_returns_unavailable(self, api_client):
        resp = api_client.get("/data/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall_status"] == "unavailable"
        assert data["snapshot_count"] == 0
        assert data["sources"] == []
        assert data["missing_capabilities"] == []

    def test_response_schema_has_new_fields(self, api_client):
        resp = api_client.get("/data/health")
        data = resp.json()
        # Verify new fields exist
        assert "sources" in data
        assert "snapshot_count" in data
        assert "oldest_snapshot" in data
        assert "newest_snapshot" in data
        assert "missing_capabilities" in data
        assert "overall_status" in data


class TestDataHealthWithProviderData:
    """Data health reflects persisted provider_health rows."""

    def _write_provider_health(self, tmp_path: Path) -> None:
        """Seed provider_health table directly in the test DB."""
        db = get_database(reload=True)
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO provider_health (provider, capability, status, last_success, "
            "last_attempt, http_status, error_message) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("polymarket", "gamma_markets", "ok", now, now, 200, ""),
        )
        db.execute(
            "INSERT INTO provider_health (provider, capability, status, last_attempt, "
            "http_status, error_message) VALUES (?, ?, ?, ?, ?, ?)",
            ("polymarket", "clob_trades", "disabled", now, 401, ""),
        )
        db.conn.commit()

    def test_provider_health_shows_per_capability_status(self, api_client, tmp_path):
        self._write_provider_health(tmp_path)
        resp = api_client.get("/data/health")
        assert resp.status_code == 200
        data = resp.json()
        sources = data["sources"]
        assert len(sources) == 2

        # Find clob_trades source
        clob = next(s for s in sources if "clob_trades" in s["source"])
        assert clob["status"] == "disabled"
        assert clob["http_status"] == 401

        # Find gamma source
        gamma = next(s for s in sources if "gamma_markets" in s["source"])
        assert gamma["status"] == "ok"
        assert gamma["http_status"] == 200

    def test_missing_capabilities_populated(self, api_client, tmp_path):
        self._write_provider_health(tmp_path)
        resp = api_client.get("/data/health")
        data = resp.json()
        assert "polymarket.clob_trades" in data["missing_capabilities"]

    def test_overall_status_degraded(self, api_client, tmp_path):
        self._write_provider_health(tmp_path)
        resp = api_client.get("/data/health")
        data = resp.json()
        # One OK + one disabled = degraded
        assert data["overall_status"] == "degraded"


class TestDataHealthFallbackToSnapshots:
    """When provider_health is empty, fall back to raw_snapshots view."""

    def _seed_snapshot_and_market(self, tmp_path: Path) -> None:
        db = get_database(reload=True)
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO raw_snapshots (id, source, endpoint, query_params, file_path, "
            "content_hash, hash_algo, content_type, size_bytes, fetched_at, ingested_at, is_sample) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("snap-1", "polymarket_gamma", "/markets", "{}", "/path/snap.json",
             "abc123", "sha256", "application/json", 512, now, now, 0),
        )
        db.execute(
            "INSERT INTO markets (id, source_id, source, question, active, closed, fetched_at, is_sample) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("m-1", "0xabc", "polymarket", "Test?", 1, 0, now, 0),
        )
        db.conn.commit()

    def test_snapshot_source_appears_in_health(self, api_client, tmp_path):
        self._seed_snapshot_and_market(tmp_path)
        resp = api_client.get("/data/health")
        data = resp.json()
        sources = data["sources"]
        assert len(sources) >= 1
        assert sources[0]["is_sample"] is False
        assert sources[0]["live_count"] == 1


# ── Live read-only safety tests ────────────────────────────────────────────────


class TestReadOnlySafety:
    """Verify adapter never exposes order submission paths."""

    def test_adapter_has_no_place_order(self):
        assert not hasattr(PolymarketPublicAdapter, "place_order")

    def test_adapter_has_no_cancel_order(self):
        assert not hasattr(PolymarketPublicAdapter, "cancel_order")

    def test_adapter_constructor_requires_no_credentials(self):
        # Adapter takes URLs only — no API keys or private keys
        adapter = PolymarketPublicAdapter(
            gamma_base_url="https://gamma-api.polymarket.com",
            clob_base_url="https://clob.polymarket.com",
        )
        assert adapter is not None


class TestProviderHealthTableExists:
    """Schema migration V2 creates provider_health table."""

    def test_provider_health_table_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "mig.db"))
        monkeypatch.delenv("POLYCOPY_ENABLE_DEMO_DATA", raising=False)
        get_settings(reload=True)
        db = get_database(reload=True)
        rows = db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='provider_health'"
        )
        assert len(rows) == 1
        db.close()


class TestFreshnessHelper:
    """seconds_since computes elapsed time correctly."""

    def test_seconds_since_known_time(self):
        from polycopy.risk.freshness import seconds_since
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        elapsed = seconds_since(past)
        assert elapsed > 0
        assert elapsed > 100000  # Many seconds since 2020

    def test_seconds_since_none(self):
        from polycopy.risk.freshness import seconds_since
        assert seconds_since(None) is None

    def test_seconds_since_recent(self):
        from polycopy.risk.freshness import seconds_since
        now = datetime.now(timezone.utc)
        elapsed = seconds_since(now)
        assert elapsed is not None
        assert elapsed < 1.0  # Less than 1 second
