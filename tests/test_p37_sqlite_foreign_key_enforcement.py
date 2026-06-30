"""Tests for SQLite foreign-key enforcement (Track A1).

Verifies that:
1. Database.connect() enables FKs.
2. Invalid child insert fails with sqlite3.IntegrityError.
3. Valid parent-child insert succeeds.
4. Fresh DB migrations succeed with FK enforcement on.
5. V4->V5 migration succeeds with FK enforcement on.
6. Sentinel cleanup preserves referential integrity.
7. PRAGMA foreign_key_check returns no rows.
8. Reopening DB enables FKs again.
9. Test/temp DB helpers do not bypass enforcement.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from polycopy.db.database import Database
from polycopy.db.schema import MIGRATIONS, SCHEMA_VERSION, _V5_DDL

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _init_db_at_version(db_path: Path, target: int) -> sqlite3.Connection:
    """Init a DB and run migrations 1..target with raw sqlite3, FKs ON."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for v in range(1, target + 1):
        for stmt in MIGRATIONS[v]:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(v),),
        )
    conn.commit()
    return conn


def _apply_v5(conn) -> None:
    for stmt in _V5_DDL:
        conn.execute(stmt)
    conn.execute(
        "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _seed_market(conn) -> str:
    market_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO markets (id, source_id, source, question, active, closed, "
        "resolved, volume_24h, fetched_at, is_sample) "
        "VALUES (?, 'mkt-p37', 'polymarket', 'P37?', 1, 0, 0, 1000.0, ?, 0)",
        (market_id, datetime.now(timezone.utc).isoformat()),
    )
    return market_id


def _insert_wallet(conn, address: str) -> str:
    wid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) "
        "VALUES (?, ?, 'p37', 0, ?)",
        (wid, address, datetime.now(timezone.utc).isoformat()),
    )
    return wid


def _insert_order(conn, market_id: str, wallet_id: str) -> str:
    oid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO orders (id, market_id, wallet_id, side, order_type, outcome, "
        "quantity, price, status, created_at, updated_at, is_sample) "
        "VALUES (?, ?, ?, 'buy', 'market', 'Yes', 1.0, 0.5, 'pending', ?, ?, 0)",
        (oid, market_id, wallet_id, now, now),
    )
    return oid


def _insert_decision_log(
    conn, wallet_id: str, market_id: str, order_id: str | None
) -> str:
    dlid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO decision_log (id, wallet_id, market_id, decision_type, "
        "signal_ids, order_id, rationale, metrics, created_at, is_sample) "
        "VALUES (?, ?, ?, 'follow', '[]', ?, 'r', '{}', ?, 0)",
        (dlid, wallet_id, market_id, order_id, datetime.now(timezone.utc).isoformat()),
    )
    return dlid


def _rowcount(conn, table: str, where: str = "1=1", params: tuple = ()) -> int:
    return conn.execute(
        f"SELECT COUNT(*) AS c FROM {table} WHERE {where}", params
    ).fetchone()["c"]


# ─── 1. Database.connect() enables FKs ──────────────────────────────────────


class TestDatabaseConnectEnablesForeignKeys:
    def test_connect_enables_fks(self, tmp_path: Path):
        """Database.connect() must enable PRAGMA foreign_keys = ON."""
        db_path = tmp_path / "fk-test.db"
        with Database(db_path=db_path) as db:
            row = db.fetchone("PRAGMA foreign_keys")
            assert row is not None
            assert row[0] == 1, f"Expected FK enabled (1), got {row[0]}"

    def test_fk_pragma_persists_on_connection(self, tmp_path: Path):
        """After connect, PRAGMA foreign_keys must return 1 on the live connection."""
        db_path = tmp_path / "fk-persist.db"
        db = Database(db_path=db_path)
        db.connect()
        result = db.conn.execute("PRAGMA foreign_keys").fetchone()
        assert result[0] == 1
        db.close()


# ─── 2. Invalid child insert fails ──────────────────────────────────────────


class TestInvalidChildInsertFails:
    def test_order_without_wallet_fails(self, tmp_path: Path):
        """Inserting an order referencing a non-existent wallet must fail."""
        db_path = tmp_path / "fk-order.db"
        with Database(db_path=db_path) as db:
            db.execute(
                "INSERT INTO markets (id, source_id, source, question, fetched_at) "
                "VALUES (?, 'm1', 'polymarket', 'Q?', ?)",
                ("mkt-1", datetime.now(timezone.utc).isoformat()),
            )
            db.conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO orders (id, market_id, wallet_id, side, order_type, "
                    "outcome, quantity, price, status, created_at) "
                    "VALUES (?, ?, ?, 'buy', 'limit', 'Yes', 1.0, 0.5, 'pending', ?)",
                    ("ord-1", "mkt-1", "nonexistent-wallet", datetime.now(timezone.utc).isoformat()),
                )

    def test_position_without_market_fails(self, tmp_path: Path):
        """Inserting a position referencing a non-existent market must fail."""
        db_path = tmp_path / "fk-position.db"
        with Database(db_path=db_path) as db:
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                "VALUES (?, ?, 'test', 0, ?)",
                ("w1", "0xABC", datetime.now(timezone.utc).isoformat()),
            )
            db.conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO positions (id, market_id, wallet_id, outcome, quantity, "
                    "avg_entry_price, current_price, opened_at, is_sample) "
                    "VALUES (?, ?, ?, 'Yes', 1.0, 0.5, 0.6, ?, 0)",
                    ("pos-1", "nonexistent-market", "w1", datetime.now(timezone.utc).isoformat()),
                )

    def test_decision_log_without_wallet_fails(self, tmp_path: Path):
        """Inserting a decision_log referencing a non-existent wallet must fail."""
        db_path = tmp_path / "fk-decision.db"
        with Database(db_path=db_path) as db:
            db.execute(
                "INSERT INTO markets (id, source_id, source, question, fetched_at) "
                "VALUES (?, 'm1', 'polymarket', 'Q?', ?)",
                ("mkt-1", datetime.now(timezone.utc).isoformat()),
            )
            db.conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO decision_log (id, wallet_id, market_id, decision_type, "
                    "signal_ids, created_at) "
                    "VALUES (?, ?, ?, 'follow', '[]', ?)",
                    ("dl-1", "nonexistent-wallet", "mkt-1", datetime.now(timezone.utc).isoformat()),
                )


# ─── 3. Valid parent-child insert succeeds ──────────────────────────────────


class TestValidParentChildInsertSucceeds:
    def test_wallet_order_position_chain(self, tmp_path: Path):
        """Full parent-child insert chain succeeds with FKs enabled."""
        db_path = tmp_path / "fk-valid-chain.db"
        with Database(db_path=db_path) as db:
            market_id = _seed_market(db.conn)
            wallet_id = _insert_wallet(db.conn, "0xVALID")
            order_id = _insert_order(db.conn, market_id, wallet_id)
            _insert_decision_log(db.conn, wallet_id, market_id, order_id)
            db.conn.commit()

            assert _rowcount(db.conn, "wallets") == 1
            assert _rowcount(db.conn, "orders") == 1
            assert _rowcount(db.conn, "decision_log") == 1

    def test_wallet_with_balances(self, tmp_path: Path):
        """Wallet with balance rows inserts cleanly."""
        db_path = tmp_path / "fk-balances.db"
        with Database(db_path=db_path) as db:
            wallet_id = _insert_wallet(db.conn, "0xBAL")
            db.execute(
                "INSERT INTO wallet_balances (wallet_id, currency, amount, as_of, is_sample) "
                "VALUES (?, 'USDC', 100.0, ?, 0)",
                (wallet_id, datetime.now(timezone.utc).isoformat()),
            )
            db.conn.commit()
            assert _rowcount(db.conn, "wallet_balances") == 1


# ─── 4. Fresh DB migrations succeed with FK enforcement on ──────────────────


class TestFreshMigrationsWithFKs:
    def test_fresh_db_migrations_with_fks_on(self, tmp_path: Path):
        """A fresh DB goes through all migrations with FKs enabled throughout."""
        db_path = tmp_path / "fk-fresh.db"
        # Use raw sqlite3 with FKs ON to simulate production path.
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        for v in range(1, SCHEMA_VERSION + 1):
            for stmt in MIGRATIONS[v]:
                conn.execute(stmt)
            conn.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
                (str(v),),
            )
        conn.commit()

        # FKs still on.
        result = conn.execute("PRAGMA foreign_keys").fetchone()
        assert result[0] == 1

        # All tables present.
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "wallets" in tables
        assert "orders" in tables
        assert "source_trades" in tables

        # No FK violations.
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk == []
        conn.close()

    def test_database_class_fresh_db(self, tmp_path: Path):
        """Database() class creates a fresh DB with FKs enabled."""
        db_path = tmp_path / "fk-fresh-class.db"
        with Database(db_path=db_path) as db:
            row = db.fetchone("PRAGMA foreign_keys")
            assert row[0] == 1
            version = db.fetchone("SELECT value FROM _meta WHERE key = 'schema_version'")
            assert int(version["value"]) == SCHEMA_VERSION


# ─── 5. V4->V5 migration succeeds with FK enforcement on ────────────────────


class TestV4ToV5MigrationWithFKs:
    def test_v4_to_v5_migration_with_fks_on(self, tmp_path: Path):
        """V4 -> V5 migration must succeed with PRAGMA foreign_keys = ON."""
        db_path = tmp_path / "fk-v4tov5.db"
        conn = _init_db_at_version(db_path, 4)
        market_id = _seed_market(conn)

        # Seed sentinel wallet + order + cross-reference decision_log.
        sentinel_wallet = _insert_wallet(conn, "unknown")
        sentinel_order = _insert_order(conn, market_id, sentinel_wallet)
        real_wallet = _insert_wallet(conn, "0xREAL_KEEP")
        _insert_decision_log(conn, real_wallet, market_id, sentinel_order)
        conn.commit()

        # Apply v5 with FKs ON.
        conn.execute("PRAGMA foreign_keys = ON")
        _apply_v5(conn)

        # Integrity check.
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk == [], f"FK violations after V4->V5: {fk}"

        # Sentinel gone, real kept.
        assert _rowcount(conn, "wallets", "address = 'unknown'") == 0
        assert _rowcount(conn, "wallets", "address = '0xREAL_KEEP'") == 1
        conn.close()

    def test_v4_to_v5_heavy_cross_references(self, tmp_path: Path):
        """V4 -> V5 with heavy cross-reference graph succeeds under FK ON."""
        db_path = tmp_path / "fk-v4tov5-heavy.db"
        conn = _init_db_at_version(db_path, 4)
        market_id = _seed_market(conn)

        sentinel_wallet = _insert_wallet(conn, "missing")
        sentinel_order = _insert_order(conn, market_id, sentinel_wallet)
        real_wallet = _insert_wallet(conn, "0xREAL")
        for _ in range(5):
            _insert_decision_log(conn, real_wallet, market_id, sentinel_order)
        _insert_decision_log(conn, sentinel_wallet, market_id, sentinel_order)
        conn.commit()

        conn.execute("PRAGMA foreign_keys = ON")
        _apply_v5(conn)

        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk == []
        assert _rowcount(conn, "wallets", "address = 'missing'") == 0
        assert _rowcount(conn, "wallets", "address = '0xREAL'") == 1
        conn.close()


# ─── 6. Sentinel cleanup preserves referential integrity ─────────────────────


class TestSentinelCleanupIntegrity:
    def test_sentinel_cleanup_no_orphans(self, tmp_path: Path):
        """After V5 migration, no orphan rows reference deleted sentinels."""
        db_path = tmp_path / "fk-sentinel-clean.db"
        conn = _init_db_at_version(db_path, 4)
        market_id = _seed_market(conn)

        # Multiple sentinel wallets with dependents.
        for addr in ("unknown", "anonymous", "missing", "0x", "0x0"):
            wid = _insert_wallet(conn, addr)
            _insert_order(conn, market_id, wid)
            conn.execute(
                "INSERT INTO wallet_balances (wallet_id, currency, amount, as_of, is_sample) "
                "VALUES (?, 'USDC', 10.0, ?, 0)",
                (wid, datetime.now(timezone.utc).isoformat()),
            )
            conn.execute(
                "INSERT INTO positions (id, market_id, wallet_id, outcome, quantity, "
                "avg_entry_price, current_price, opened_at, is_sample) "
                "VALUES (?, ?, ?, 'Yes', 1.0, 0.5, 0.6, ?, 0)",
                (str(uuid.uuid4()), market_id, wid, datetime.now(timezone.utc).isoformat()),
            )

        # Real wallet kept.
        real_wallet = _insert_wallet(conn, "0xREAL_KEEP")
        _insert_order(conn, market_id, real_wallet)
        conn.commit()

        conn.execute("PRAGMA foreign_keys = ON")
        _apply_v5(conn)

        # No FK violations.
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk == []

        # All sentinel rows gone.
        for addr in ("unknown", "anonymous", "missing", "0x", "0x0"):
            assert _rowcount(conn, "wallets", "address = ?", (addr,)) == 0

        # Real wallet and its order survive.
        assert _rowcount(conn, "wallets", "address = '0xREAL_KEEP'") == 1
        assert _rowcount(conn, "orders", "wallet_id = ?", (real_wallet,)) == 1
        conn.close()


# ─── 7. PRAGMA foreign_key_check returns no rows ────────────────────────────


class TestForeignKeyCheckEmpty:
    def test_fk_check_empty_on_fresh_db(self, tmp_path: Path):
        """PRAGMA foreign_key_check returns no rows on a fresh DB."""
        db_path = tmp_path / "fk-check.db"
        with Database(db_path=db_path) as db:
            fk = db.fetchall("PRAGMA foreign_key_check")
            assert fk == []

    def test_fk_check_empty_after_valid_inserts(self, tmp_path: Path):
        """PRAGMA foreign_key_check returns no rows after valid inserts."""
        db_path = tmp_path / "fk-check-inserts.db"
        with Database(db_path=db_path) as db:
            market_id = _seed_market(db.conn)
            wallet_id = _insert_wallet(db.conn, "0xCHECK")
            _insert_order(db.conn, market_id, wallet_id)
            _insert_decision_log(db.conn, wallet_id, market_id, None)
            db.conn.commit()

            fk = db.fetchall("PRAGMA foreign_key_check")
            assert fk == []


# ─── 8. Reopening DB enables FKs again ──────────────────────────────────────


class TestReopenEnablesFKs:
    def test_reopen_enables_fks(self, tmp_path: Path):
        """Reopening an existing DB must re-enable FKs."""
        db_path = tmp_path / "fk-reopen.db"
        # First connect.
        db = Database(db_path=db_path)
        db.connect()
        row = db.fetchone("PRAGMA foreign_keys")
        assert row[0] == 1
        db.close()

        # Second connect (reopen).
        db2 = Database(db_path=db_path)
        db2.connect()
        row2 = db2.fetchone("PRAGMA foreign_keys")
        assert row2[0] == 1, "FKs must be re-enabled on reopen"
        db2.close()

    def test_get_database_reload_enables_fks(self, tmp_path: Path, monkeypatch):
        """get_database(reload=True) must produce a connection with FKs ON."""
        from polycopy.db import database as db_module

        db_path = tmp_path / "fk-reload.db"
        monkeypatch.setattr(
            "polycopy.config.settings.get_settings",
            lambda: type("S", (), {"db_path": db_path, "db_echo": False})(),
        )
        db_module._db = None
        db1 = db_module.get_database()
        row = db1.fetchone("PRAGMA foreign_keys")
        assert row[0] == 1

        db2 = db_module.get_database(reload=True)
        row2 = db2.fetchone("PRAGMA foreign_keys")
        assert row2[0] == 1


# ─── 9. Test/temp DB helpers do not bypass enforcement ─────────────────────


class TestTestHelpersEnforceFKs:
    def test_raw_sqlite_connect_in_test_helps_enforcement(self, tmp_path: Path):
        """A raw sqlite3.connect (as used in test helpers) must also have
        FKs enabled when going through the same code path."""
        db_path = tmp_path / "fk-raw-helper.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")

        # Create schema V1 tables.
        for stmt in MIGRATIONS[1]:
            conn.execute(stmt)
        conn.commit()

        # Insert parent wallet.
        conn.execute(
            "INSERT INTO wallets (id, address, label, is_sample, created_at) "
            "VALUES (?, '0xTEST', 'test', 0, ?)",
            ("w1", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

        # Invalid child insert must fail.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO wallet_balances (wallet_id, currency, amount, as_of, is_sample) "
                "VALUES ('nonexistent', 'USDC', 10.0, ?, 0)",
                (datetime.now(timezone.utc).isoformat(),),
            )
        conn.close()

    def test_database_class_does_not_bypass_fks(self, tmp_path: Path):
        """Database() must not bypass or override FK enforcement."""
        db_path = tmp_path / "fk-nobypass.db"
        with Database(db_path=db_path) as db:
            # Verify FK is on BEFORE any data insertion.
            row = db.fetchone("PRAGMA foreign_keys")
            assert row[0] == 1

            # Insert parent.
            db.execute(
                "INSERT INTO markets (id, source_id, source, question, fetched_at) "
                "VALUES (?, 'm1', 'polymarket', 'Q?', ?)",
                ("m1", datetime.now(timezone.utc).isoformat()),
            )
            db.conn.commit()

            # Child without parent must fail even on Database() connection.
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO orders (id, market_id, wallet_id, side, order_type, "
                    "outcome, quantity, price, status, created_at) "
                    "VALUES (?, ?, ?, 'buy', 'limit', 'Yes', 1.0, 0.5, 'pending', ?)",
                    ("o1", "m1", "no-such-wallet", datetime.now(timezone.utc).isoformat()),
                )
