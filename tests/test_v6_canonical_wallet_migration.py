from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from polycopy.db.database import Database
from polycopy.db.schema import MIGRATIONS, SCHEMA_VERSION, _V6_DDL


def _init_db_at_version(db_path: Path, target: int) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for version in range(1, target + 1):
        for stmt in MIGRATIONS[version]:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(version),),
        )
    conn.commit()
    return conn


def _apply_v6(conn: sqlite3.Connection) -> None:
    for stmt in _V6_DDL:
        conn.execute(stmt)
    conn.execute(
        "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _insert_market(conn: sqlite3.Connection, market_id: str = "m1") -> str:
    conn.execute(
        "INSERT INTO markets (id, source_id, source, question, fetched_at) "
        "VALUES (?, ?, 'polymarket', 'Q?', '2026-01-01T00:00:00Z')",
        (market_id, market_id),
    )
    return market_id


def _insert_wallet(
    conn: sqlite3.Connection,
    wallet_id: str,
    address: str,
    *,
    label: str = "default",
    is_sample: int = 0,
    created_at: str = "2026-01-01T00:00:00Z",
) -> str:
    conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (wallet_id, address, label, is_sample, created_at),
    )
    return wallet_id


def _insert_dependents(conn: sqlite3.Connection, wallet_id: str, market_id: str, suffix: str) -> None:
    conn.execute(
        "INSERT INTO wallet_balances (wallet_id, currency, amount, as_of) "
        "VALUES (?, 'USDC', 1.0, '2026-01-01T00:00:00Z')",
        (wallet_id,),
    )
    conn.execute(
        "INSERT INTO positions (id, market_id, wallet_id, outcome, quantity, "
        "avg_entry_price, current_price, opened_at) "
        "VALUES (?, ?, ?, 'Yes', 1.0, 0.5, 0.5, '2026-01-01T00:00:00Z')",
        (f"p-{suffix}", market_id, wallet_id),
    )
    conn.execute(
        "INSERT INTO orders (id, market_id, wallet_id, side, order_type, outcome, "
        "quantity, price, status, created_at) "
        "VALUES (?, ?, ?, 'buy', 'limit', 'Yes', 1.0, 0.5, 'pending', '2026-01-01T00:00:00Z')",
        (f"o-{suffix}", market_id, wallet_id),
    )
    conn.execute(
        "INSERT INTO decision_log (id, wallet_id, market_id, decision_type, signal_ids, "
        "order_id, created_at) VALUES (?, ?, ?, 'follow', '[]', ?, '2026-01-01T00:00:00Z')",
        (f"d-{suffix}", wallet_id, market_id, f"o-{suffix}"),
    )
    conn.execute(
        "INSERT INTO performance_summaries (wallet_id, start_date, end_date, total_pnl, "
        "realized_pnl, unrealized_pnl, win_rate, max_drawdown, trade_count) "
        "VALUES (?, '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', 0, 0, 0, 0, 0, 0)",
        (wallet_id,),
    )


def test_fresh_database_creates_canonical_column_unique_index_and_triggers(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh-v6.db"
    with Database(db_path=db_path) as db:
        version = db.fetchone("SELECT value FROM _meta WHERE key = 'schema_version'")
        assert version is not None
        assert int(version["value"]) == 6

        columns = {row["name"] for row in db.fetchall("PRAGMA table_info(wallets)")}
        assert "canonical_address" in columns

        indexes = {row["name"] for row in db.fetchall("PRAGMA index_list(wallets)")}
        assert "ux_wallets_canonical_address" in indexes

        triggers = {row["name"] for row in db.fetchall("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert "trg_wallets_canonical_address_ai" in triggers
        assert "trg_wallets_canonical_address_au" in triggers
        assert db.fetchall("PRAGMA foreign_key_check") == []


def test_v6_migration_collapses_duplicate_wallets_and_rehomes_dependents(tmp_path: Path) -> None:
    conn = _init_db_at_version(tmp_path / "collapse.db", 5)
    market_id = _insert_market(conn)
    survivor = _insert_wallet(
        conn,
        "w1",
        "  0xAbC  ",
        label="keeper",
        is_sample=1,
        created_at="2026-01-01T00:00:00Z",
    )
    duplicate = _insert_wallet(
        conn,
        "w2",
        "0xabc",
        label="duplicate",
        is_sample=0,
        created_at="2026-01-02T00:00:00Z",
    )
    _insert_dependents(conn, survivor, market_id, "a")
    _insert_dependents(conn, duplicate, market_id, "b")
    conn.commit()

    _apply_v6(conn)

    wallets = conn.execute("SELECT id, address, label, is_sample, canonical_address FROM wallets").fetchall()
    assert len(wallets) == 1
    assert wallets[0]["id"] == survivor
    assert wallets[0]["canonical_address"] == "0xabc"
    assert wallets[0]["label"] == "keeper"
    assert wallets[0]["is_sample"] == 0

    for table in ["wallet_balances", "positions", "orders", "decision_log", "performance_summaries"]:
        ids = {row["wallet_id"] for row in conn.execute(f"SELECT wallet_id FROM {table}").fetchall()}
        assert ids == {survivor}, table
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_v6_survivor_tie_breaks_on_lowest_id(tmp_path: Path) -> None:
    conn = _init_db_at_version(tmp_path / "tie.db", 5)
    _insert_wallet(conn, "b-wallet", "0xDEF", created_at="2026-01-01T00:00:00Z")
    _insert_wallet(conn, "a-wallet", "0xdef", created_at="2026-01-01T00:00:00Z")
    conn.commit()

    _apply_v6(conn)

    row = conn.execute("SELECT id, canonical_address FROM wallets").fetchone()
    assert row["id"] == "a-wallet"
    assert row["canonical_address"] == "0xdef"
    conn.close()


def test_v6_cleans_invalid_wallet_rows_child_before_parent(tmp_path: Path) -> None:
    conn = _init_db_at_version(tmp_path / "invalid.db", 5)
    market_id = _insert_market(conn)
    _insert_wallet(conn, "real", "0xREAL")
    _insert_wallet(conn, "bad", "unknown")
    _insert_dependents(conn, "bad", market_id, "bad")
    conn.execute(
        "INSERT INTO decision_log (id, wallet_id, market_id, decision_type, signal_ids, "
        "order_id, created_at) VALUES ('d-cross', 'real', ?, 'follow', '[]', 'o-bad', '2026-01-01T00:00:00Z')",
        (market_id,),
    )
    conn.commit()

    _apply_v6(conn)

    remaining = conn.execute("SELECT id, canonical_address FROM wallets").fetchall()
    assert [(row["id"], row["canonical_address"]) for row in remaining] == [("real", "0xreal")]
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE wallet_id = 'bad'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM decision_log WHERE order_id = 'o-bad'").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_database_triggers_canonicalize_and_reject_duplicate_or_sentinel_inserts(tmp_path: Path) -> None:
    db_path = tmp_path / "trigger.db"
    with Database(db_path=db_path) as db:
        db.execute(
            "INSERT INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, 'a', 0, ?)",
            ("w1", "  0xABC  ", "2026-01-01T00:00:00Z"),
        )
        db.conn.commit()
        row = db.fetchone("SELECT canonical_address FROM wallets WHERE id = 'w1'")
        assert row is not None
        assert row["canonical_address"] == "0xabc"

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, 'b', 0, ?)",
                ("w2", "0xabc", "2026-01-01T00:00:00Z"),
            )
        db.conn.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, 'bad', 0, ?)",
                ("bad", "unknown", "2026-01-01T00:00:00Z"),
            )
        db.conn.rollback()


def test_reopening_v6_database_is_noop_and_fk_clean(tmp_path: Path) -> None:
    db_path = tmp_path / "reopen.db"
    with Database(db_path=db_path) as db:
        db.execute(
            "INSERT INTO wallets (id, address, label, is_sample, created_at) VALUES ('w1', '0xAAA', 'a', 0, '2026-01-01T00:00:00Z')"
        )
        db.conn.commit()

    with Database(db_path=db_path) as db:
        wallets = db.fetchall("SELECT id, canonical_address FROM wallets")
        assert [(row["id"], row["canonical_address"]) for row in wallets] == [("w1", "0xaaa")]
        assert db.fetchall("PRAGMA foreign_key_check") == []
