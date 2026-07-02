"""Schema-bridge tests for the v8 → v9 additive migration.

This suite proves the schema-bridge is additive, idempotent, and
preserves all prior-PR invariants. The bridge adds ONE new table
(``candidate_price_snapshots``) and THREE indexes. It does not alter
any existing column, does not introduce a circular FK, and does not
add a ``latest_price_snapshot_id`` pointer.

The bridge test plan (matches the incident disclosure §7):

  1. v8 → v9 migration succeeds.
  2. Existing rows are preserved.
  3. candidate_price_snapshots table exists.
  4. Expected columns are present.
  5. UNIQUE(candidate_id, snapshot_run_id) is enforced.
  6. FK candidate_id → copy_candidates(id) is enforced.
  7. Indexes idx_cps_candidate_fetched, idx_cps_status, idx_cps_run exist.
  8. Migration is idempotent (re-running v9 is a no-op).
  9. PRAGMA foreign_key_check is clean post-migration.
 10. Safety invariants hold (signals/orders/positions/decision_log
     are zero after bridge; the new table is empty).

All tests use disposable DBs (``tmp_path``); production
``/root/Polycopy/data/polycopy.db`` is NEVER touched by this suite.
Read-only production inspection happens in the incident report, not
here.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.db.database import Database  # noqa: E402
from polycopy.db.schema import (  # noqa: E402
    MIGRATIONS,
    SCHEMA_VERSION,
)


# ── 1. v8 → v9 migration succeeds on a v8 DB ───────────────────────────────
def test_v8_to_v9_migration_succeeds_on_v8_db(tmp_path: Path) -> None:
    """A v8 DB (no candidate_price_snapshots) migrates cleanly to v9."""
    db_path = tmp_path / "bridge-s1.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    pre_version = SCHEMA_VERSION - 1  # 8
    for version in range(1, pre_version + 1):
        for stmt in MIGRATIONS[version]:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) "
            "VALUES ('schema_version', ?)",
            (str(version),),
        )
    conn.commit()
    # Sanity: pre-state.
    pre = conn.execute(
        "SELECT value FROM _meta WHERE key='schema_version'"
    ).fetchone()["value"]
    assert pre == str(pre_version)
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name='candidate_price_snapshots'"
    ).fetchone() is None
    conn.close()

    # Open via Database so the migration runner applies v9.
    db = Database(db_path=db_path).connect()
    try:
        post = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
        assert post is not None
        assert int(post["value"]) == SCHEMA_VERSION
    finally:
        db.close()


# ── 2. Existing rows are preserved ─────────────────────────────────────────
def test_existing_rows_preserved_through_bridge(tmp_path: Path) -> None:
    """Wallets / markets / source_trades written at v8 survive v9."""
    db_path = tmp_path / "bridge-s2.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    pre_version = SCHEMA_VERSION - 1
    for version in range(1, pre_version + 1):
        for stmt in MIGRATIONS[version]:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) "
            "VALUES ('schema_version', ?)",
            (str(version),),
        )
    conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) "
        "VALUES ('w1', '0xABC', 'a', 0, '2026-01-01T00:00:00Z')",
    )
    conn.execute(
        "INSERT INTO markets (id, source_id, source, question, fetched_at) "
        "VALUES ('m1', 'cond-1', 'polymarket', 'Q?', '2026-01-01T00:00:00Z')",
    )
    conn.execute(
        "INSERT INTO source_trades (id, source, source_trade_id, "
        "market_source_id, side, outcome, quantity, price, "
        "trader_address, timestamp) VALUES ('t1', 'polymarket_data_api', "
        "'tx-1', 'cond-1', 'BUY', 'Yes', 10.0, 0.5, '0xabc', "
        "'2026-01-01T00:00:00Z')",
    )
    conn.commit()
    conn.close()

    db = Database(db_path=db_path).connect()
    try:
        assert db.fetchone("SELECT id FROM wallets WHERE id='w1'") is not None
        assert db.fetchone("SELECT id FROM markets WHERE id='m1'") is not None
        assert (
            db.fetchone(
                "SELECT id FROM source_trades WHERE source_trade_id='tx-1'"
            )
            is not None
        )
    finally:
        db.close()


# ── 3. candidate_price_snapshots table exists after v9 ─────────────────────
def test_candidate_price_snapshots_table_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "bridge-s3.db"
    db = Database(db_path=db_path).connect()
    try:
        row = db.fetchone(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='candidate_price_snapshots'"
        )
        assert row is not None, "candidate_price_snapshots table missing"
    finally:
        db.close()


# ── 4. Expected columns are present ────────────────────────────────────────
def test_expected_columns_present(tmp_path: Path) -> None:
    db_path = tmp_path / "bridge-s4.db"
    db = Database(db_path=db_path).connect()
    try:
        cols = {
            r["name"]
            for r in db.conn.execute(
                "PRAGMA table_info(candidate_price_snapshots)"
            ).fetchall()
        }
        required = {
            "id", "candidate_id", "snapshot_run_id", "fetch_status",
            "fetch_endpoint", "fetch_http_status", "fetch_latency_ms",
            "request_attempts", "fetch_error_code", "fetch_error_message",
            "token_id", "side", "source_trade_price", "source_trade_quantity",
            "source_trade_timestamp",
            "best_bid", "best_bid_size", "best_ask", "best_ask_size",
            "mid_price", "spread",
            "executable_price", "executable_side_depth", "expected_fill_price",
            "price_deterioration", "price_deterioration_pct",
            "mid_change", "mid_change_pct",
            "trade_age_seconds", "market_end_at", "seconds_to_market_end",
            "market_metadata_fetched_at",
            "market_active_at_fetch", "market_closed_at_fetch",
            "market_resolved_at_fetch",
            "bid_level_count", "ask_level_count",
            "book_summary_json", "book_hash",
            "fetched_at", "created_at",
        }
        missing = required - cols
        assert not missing, f"missing columns: {missing}"
    finally:
        db.close()


# ── 5. UNIQUE(candidate_id, snapshot_run_id) is enforced ──────────────────
def test_unique_candidate_snapshot_run_enforced(tmp_path: Path) -> None:
    """A duplicate (candidate_id, snapshot_run_id) raises IntegrityError."""
    db_path = tmp_path / "bridge-s5.db"
    db = Database(db_path=db_path).connect()
    try:
        # Seed wallet + candidate first (FK requirement).
        wallet_id = str(uuid4())
        db.conn.execute(
            "INSERT INTO wallets (id, address, label, is_sample, "
            "created_at, canonical_address) "
            "VALUES (?, '0xWALLET', 'w', 0, ?, '0xwallet')",
            (wallet_id, "2026-01-01T00:00:00Z"),
        )
        cur = db.conn.execute(
            "INSERT INTO copy_candidates (wallet_id, source, source_trade_id, "
            "side, source_trade_price, source_trade_quantity, "
            "source_trade_timestamp, observed_at, wallet_score_version, "
            "wallet_score, wallet_verdict, status, created_at, updated_at) "
            "VALUES (?, 's', 't1', 'BUY', 0.5, 1.0, "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'v1', 80.0, "
            "'copy_candidate', 'PENDING_PRICE_CHECK', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (wallet_id,),
        )
        candidate_id = int(cur.lastrowid)
        db.conn.commit()

        snap_id_1 = str(uuid4())
        run_id = "run-fixed"
        db.conn.execute(
            "INSERT INTO candidate_price_snapshots (id, candidate_id, "
            "snapshot_run_id, fetch_status, side, source_trade_price, "
            "source_trade_quantity, source_trade_timestamp, fetched_at, "
            "created_at) VALUES (?, ?, ?, 'OK', 'BUY', 0.5, 1.0, "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z')",
            (snap_id_1, candidate_id, run_id),
        )
        db.conn.commit()

        # Duplicate insert: different id, same (candidate_id, run_id).
        snap_id_2 = str(uuid4())
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(
                "INSERT INTO candidate_price_snapshots (id, candidate_id, "
                "snapshot_run_id, fetch_status, side, source_trade_price, "
                "source_trade_quantity, source_trade_timestamp, fetched_at, "
                "created_at) VALUES (?, ?, ?, 'OK', 'BUY', 0.5, 1.0, "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z')",
                (snap_id_2, candidate_id, run_id),
            )
    finally:
        db.close()


# ── 6. FK candidate_id → copy_candidates(id) is enforced ──────────────────
def test_fk_candidate_id_enforced(tmp_path: Path) -> None:
    """An insert referencing a non-existent candidate raises IntegrityError."""
    db_path = tmp_path / "bridge-s6.db"
    db = Database(db_path=db_path).connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(
                "INSERT INTO candidate_price_snapshots (id, candidate_id, "
                "snapshot_run_id, fetch_status, side, source_trade_price, "
                "source_trade_quantity, source_trade_timestamp, fetched_at, "
                "created_at) VALUES (?, 999999, 'r', 'OK', 'BUY', 0.5, "
                "1.0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z')",
                (str(uuid4()),),
            )
    finally:
        db.close()


# ── 7. Indexes exist ───────────────────────────────────────────────────────
def test_indexes_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "bridge-s7.db"
    db = Database(db_path=db_path).connect()
    try:
        indexes = {
            r["name"]
            for r in db.conn.execute(
                "PRAGMA index_list(candidate_price_snapshots)"
            ).fetchall()
        }
        for ix in (
            "idx_cps_candidate_fetched",
            "idx_cps_status",
            "idx_cps_run",
        ):
            assert ix in indexes, f"missing index {ix}; got {sorted(indexes)}"
    finally:
        db.close()


# ── 8. Migration is idempotent (re-opening v9 DB is a no-op) ──────────────
def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Opening a v9 DB a second time does NOT re-apply v9 DDL."""
    db_path = tmp_path / "bridge-s8.db"
    db = Database(db_path=db_path).connect()
    db.close()
    # Re-open.
    db = Database(db_path=db_path).connect()
    try:
        row = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
        assert int(row["value"]) == SCHEMA_VERSION
        # New table is still there.
        assert (
            db.fetchone(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='candidate_price_snapshots'"
            )
            is not None
        )
    finally:
        db.close()


# ── 9. PRAGMA foreign_key_check is clean post-migration ───────────────────
def test_fk_check_clean_post_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "bridge-s9.db"
    db = Database(db_path=db_path).connect()
    try:
        assert db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        db.close()


# ── 10. Safety invariants: no signals/orders/positions/decision_log rows ─
def test_safety_invariants_hold_after_bridge(tmp_path: Path) -> None:
    """The bridge does not write to any action / decision table."""
    db_path = tmp_path / "bridge-s10.db"
    db = Database(db_path=db_path).connect()
    try:
        for t in (
            "signals", "orders", "positions", "decision_log",
            "copy_candidates", "candidate_price_snapshots",
        ):
            n = db.fetchone(f"SELECT COUNT(*) AS n FROM {t}")["n"]
            assert n == 0, f"{t} should be 0 after fresh bridge, got {n}"
    finally:
        db.close()


# ── No latest_price_snapshot_id column on copy_candidates ─────────────────
def test_no_latest_pointer_on_copy_candidates(tmp_path: Path) -> None:
    """Contract §6.6: no ``latest_price_snapshot_id`` column on copy_candidates."""
    db_path = tmp_path / "bridge-s11.db"
    db = Database(db_path=db_path).connect()
    try:
        cols = {
            r["name"]
            for r in db.conn.execute(
                "PRAGMA table_info(copy_candidates)"
            ).fetchall()
        }
        assert "latest_price_snapshot_id" not in cols
    finally:
        db.close()


# ── No new column on markets ──────────────────────────────────────────────
def test_no_new_market_column_added(tmp_path: Path) -> None:
    """Contract §5: no new column on markets (existing end_date is reused)."""
    db_path = tmp_path / "bridge-s12.db"
    db = Database(db_path=db_path).connect()
    try:
        cols = {
            r["name"]
            for r in db.conn.execute("PRAGMA table_info(markets)").fetchall()
        }
        assert "end_date_iso" not in cols
        # The existing end_date column is still there.
        assert "end_date" in cols
    finally:
        db.close()


# ── Schema version constant is 9 ──────────────────────────────────────────
def test_schema_version_constant_is_9() -> None:
    assert SCHEMA_VERSION == 9
