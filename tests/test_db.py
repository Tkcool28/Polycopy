"""Tests for SQLite schema init and migrations."""

from pathlib import Path

import pytest

from polycopy.db.database import Database
from polycopy.db.schema import SCHEMA_VERSION


class TestDatabase:
    def test_init_creates_schema(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            # Verify _meta table exists and has correct version
            row = db.fetchone("SELECT value FROM _meta WHERE key = 'schema_version'")
            assert row is not None
            assert int(row["value"]) == SCHEMA_VERSION

    def test_all_tables_created(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            tables = {row["name"] for row in db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )}
        expected = {
            "_meta", "wallets", "wallet_balances", "markets", "market_outcomes",
            "signals", "orders", "positions", "source_trades", "decision_log",
            "experiment_runs", "raw_snapshots", "performance_summaries",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    def test_idempotent_connect(self, tmp_path: Path):
        """Connecting twice should not error and should keep the same version."""
        db_path = tmp_path / "test.db"
        db = Database(db_path=db_path)
        db.connect()
        db.close()
        db.connect()
        row = db.fetchone("SELECT value FROM _meta WHERE key = 'schema_version'")
        assert row is not None
        assert int(row["value"]) == SCHEMA_VERSION
        db.close()

    def test_newer_db_raises(self, tmp_path: Path):
        """If DB schema is newer than code, raise RuntimeError."""
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            # Manually bump version ahead of code
            db.execute("INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', '999')")
            db.conn.commit()

        with pytest.raises(RuntimeError, match="newer than code"):
            db2 = Database(db_path=db_path)
            db2.connect()

    def test_can_insert_wallet(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, ?, ?)",
                ("test-id", "0xABC", "test-wallet", 0, "2026-01-01T00:00:00Z"),
            )
            db.conn.commit()
            row = db.fetchone("SELECT address FROM wallets WHERE id = ?", ("test-id",))
            assert row is not None
            assert row["address"] == "0xABC"

    def test_can_insert_market_and_outcomes(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            db.execute(
                "INSERT INTO markets (id, source_id, source, question, fetched_at) VALUES (?, ?, ?, ?, ?)",
                ("m1", "cond-1", "polymarket", "Will X?", "2026-01-01T00:00:00Z"),
            )
            db.execute(
                "INSERT INTO market_outcomes (market_id, label, price, volume) VALUES (?, ?, ?, ?)",
                ("m1", "Yes", 0.7, 1000.0),
            )
            db.conn.commit()
            outcomes = db.fetchall("SELECT * FROM market_outcomes WHERE market_id = ?", ("m1",))
            assert len(outcomes) == 1
            assert float(outcomes[0]["price"]) == pytest.approx(0.7)

    def test_can_insert_order(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, ?, ?)",
                ("w1", "0xW1", "wallet1", 0, "2026-01-01T00:00:00Z"),
            )
            db.execute(
                "INSERT INTO markets (id, source_id, source, question, fetched_at) VALUES (?, ?, ?, ?, ?)",
                ("m1", "cond-1", "polymarket", "Will X?", "2026-01-01T00:00:00Z"),
            )
            db.execute(
                """INSERT INTO orders (id, market_id, wallet_id, side, order_type, outcome,
                   quantity, price, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("o1", "m1", "w1", "buy", "limit", "Yes", 10.0, 0.65, "pending", "2026-01-01T00:00:00Z"),
            )
            db.conn.commit()
            row = db.fetchone("SELECT * FROM orders WHERE id = ?", ("o1",))
            assert row is not None
            assert row["side"] == "buy"
            assert float(row["quantity"]) == pytest.approx(10.0)

    def test_raw_snapshot_table(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        with Database(db_path=db_path) as db:
            db.execute(
                """INSERT INTO raw_snapshots (id, source, endpoint, file_path, content_hash,
                   size_bytes, fetched_at, ingested_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("s1", "polymarket_gamma", "/markets", "snap.json", "abc123", 100, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
            db.conn.commit()
            row = db.fetchone("SELECT * FROM raw_snapshots WHERE id = ?", ("s1",))
            assert row is not None
            assert row["content_hash"] == "abc123"
