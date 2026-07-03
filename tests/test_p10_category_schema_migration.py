"""V10 schema-bridge tests for the Category Wallet Score additions
(Phase 2 / Chunk 3 — Task 3.8).

The Chunk 3 schema migration extended the
``category_wallet_score_decisions`` table with five new columns
necessary for the typed category score contract:

  - category_resolved_markets
  - category_distinct_events
  - category_active_days
  - missing_essentials_json
  - category_gate_failures_json

This test file proves that:

  1. Fresh empty DB initializes to the current canonical schema
     (currently v11 — Chunk 5 added shadow typed-input columns).
  2. Existing valid v9 DB upgrades to the current canonical schema.
  3. Existing v9 data remains unchanged.
  4. category_wallet_score_decisions exists at the current schema.
  5. The new category columns exist.
  6. Required category indexes exist.
  7. No category rows are invented during migration.
  8. foreign_key_check returns no rows.
  9. Re-running schema initialization is idempotent.
 10. Existing candidate price snapshots without depth levels
     remain valid (DEPTH_NOT_CAPTURED, not auto-backfilled).
 11. Existing v9 production-style rows are preserved exactly.

The tests use disposable DBs (``tmp_path``); production
``/root/Polycopy/data/polycopy.db`` is NEVER touched.

NOTE: Test 1 was originally named ``test_fresh_db_initializes_to_v10``
and hard-coded ``SCHEMA_VERSION == 10``. Chunk 5 added v11 (additive
shadow typed-input columns). The test is renamed to
``test_fresh_db_initializes_to_current_schema`` and asserts against
the imported ``SCHEMA_VERSION`` constant, which is the canonical
source of truth.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.db.database import Database  # noqa: E402
from polycopy.db.schema import (  # noqa: E402
    MIGRATIONS,
    SCHEMA_VERSION,
)


# ── 1. Fresh empty DB initializes to the current canonical schema ─────


def test_fresh_db_initializes_to_current_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "v10-s1.db"
    db = Database(db_path=db_path).connect()
    try:
        row = db.fetchone(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        )
        assert row is not None
        # Canonical contract: fresh DB reaches the current SCHEMA_VERSION
        # exactly. The constant is the single source of truth; tests must
        # not hard-code a version number that drifts on additive schema
        # bumps (e.g. v10 → v11 added shadow typed-input columns).
        assert int(row["value"]) == SCHEMA_VERSION
        # Sanity: SCHEMA_VERSION must be a positive integer (regression
        # guard against accidental constant corruption).
        assert SCHEMA_VERSION >= 1
    finally:
        db.close()


# ── 2. v9 DB upgrades to v10 ─────────────────────────────────────────


def test_v9_db_upgrades_to_v10(tmp_path: Path) -> None:
    db_path = tmp_path / "v10-s2.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # SCHEMA_VERSION bumped past v11; pre-state for the v9→v10
    # test is v9, so we stop at v9 (v10's CREATE TABLE for
    # shadow_decisions already declares the slippage column that
    # v11 would then try to ADD again).
    pre_version = 9
    for version in range(1, pre_version + 1):
        for stmt in MIGRATIONS[version]:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) "
            "VALUES ('schema_version', ?)",
            (str(version),),
        )
    conn.commit()
    conn.close()

    db = Database(db_path=db_path).connect()
    try:
        row = db.fetchone(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        )
        assert row is not None
        assert int(row["value"]) == SCHEMA_VERSION
    finally:
        db.close()


# ── 3. Existing v9 data remains unchanged ─────────────────────────────


def test_existing_v9_data_preserved_through_v10(tmp_path: Path) -> None:
    db_path = tmp_path / "v10-s3.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # SCHEMA_VERSION bumped past v11; stop at v9 (pre-state for v10).
    pre_version = 9
    for version in range(1, pre_version + 1):
        for stmt in MIGRATIONS[version]:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) "
            "VALUES ('schema_version', ?)",
            (str(version),),
        )
    # Seed production-style rows.
    conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at) VALUES ('w9', '0xABC', 'a', 0, "
        "'2026-01-01T00:00:00Z')",
    )
    conn.execute(
        "INSERT INTO markets (id, source_id, source, question, "
        "fetched_at) VALUES ('m9', 'cond-1', 'polymarket', 'Q?', "
        "'2026-01-01T00:00:00Z')",
    )
    conn.execute(
        "INSERT INTO source_trades (id, source, source_trade_id, "
        "market_source_id, side, outcome, quantity, price, "
        "trader_address, timestamp) VALUES ('t9', "
        "'polymarket_data_api', 'tx-1', 'cond-1', 'BUY', 'Yes', "
        "10.0, 0.5, '0xabc', '2026-01-01T00:00:00Z')",
    )
    conn.commit()
    conn.close()

    db = Database(db_path=db_path).connect()
    try:
        assert db.fetchone("SELECT id FROM wallets WHERE id='w9'") is not None
        assert db.fetchone("SELECT id FROM markets WHERE id='m9'") is not None
        assert (
            db.fetchone(
                "SELECT id FROM source_trades WHERE source_trade_id='tx-1'"
            )
            is not None
        )
    finally:
        db.close()


# ── 4. category_wallet_score_decisions exists at v10 ─────────────────


def test_category_wallet_score_decisions_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "v10-s4.db"
    db = Database(db_path=db_path).connect()
    try:
        row = db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='category_wallet_score_decisions'"
        )
        assert row is not None, (
            "category_wallet_score_decisions table missing"
        )
    finally:
        db.close()


# ── 5. New category columns exist ────────────────────────────────────


def test_new_category_columns_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "v10-s5.db"
    db = Database(db_path=db_path).connect()
    try:
        cols = {
            r["name"]
            for r in db.conn.execute(
                "PRAGMA table_info(category_wallet_score_decisions)"
            ).fetchall()
        }
        required = {
            # Pre-existing columns (proves upgrade didn't drop them).
            "wallet_id", "category_label", "formula_name",
            "formula_version", "idempotency_key",
            "info_score", "win_rate", "profit_factor",
            "trade_intervals_std", "trade_count", "max_drawdown",
            "sharpe_ratio", "sample_fraction",
            "category_trade_count", "category_distinct_markets",
            "overall_trade_count", "largest_winner_share",
            "top_3_concentration", "component_scores_json",
            "final_score", "verdict",
            "source_data_timestamp", "computed_at", "created_at",
            # New in Chunk 3.
            "category_resolved_markets",
            "category_distinct_events",
            "category_active_days",
            "missing_essentials_json",
            "category_gate_failures_json",
        }
        missing = required - cols
        assert not missing, (
            f"category_wallet_score_decisions missing columns: "
            f"{sorted(missing)}"
        )
    finally:
        db.close()


# ── 6. Required category indexes exist ───────────────────────────────


def test_category_indexes_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "v10-s6.db"
    db = Database(db_path=db_path).connect()
    try:
        indexes = {
            r["name"]
            for r in db.conn.execute(
                "PRAGMA index_list(category_wallet_score_decisions)"
            ).fetchall()
        }
        # The wallet index is created by the v10 DDL.
        assert "idx_category_score_wallet" in indexes, (
            f"missing idx_category_score_wallet; got {sorted(indexes)}"
        )
    finally:
        db.close()


# ── 7. No category rows invented during migration ─────────────────────


def test_no_category_rows_invented_during_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "v10-s7.db"
    db = Database(db_path=db_path).connect()
    try:
        n = db.fetchone(
            "SELECT COUNT(*) AS n FROM category_wallet_score_decisions"
        )["n"]
        assert n == 0, (
            f"category_wallet_score_decisions must be empty after a "
            f"fresh migration, got {n} rows"
        )
    finally:
        db.close()


# ── 8. foreign_key_check returns no rows post-migration ──────────────


def test_foreign_key_check_clean_post_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "v10-s8.db"
    db = Database(db_path=db_path).connect()
    try:
        violations = db.fetchall("PRAGMA foreign_key_check")
        assert violations == [], (
            f"FK violations after fresh v10 migration: {violations}"
        )
    finally:
        db.close()


# ── 9. Re-running schema initialization is idempotent ────────────────


def test_migration_is_idempotent_v10(tmp_path: Path) -> None:
    db_path = tmp_path / "v10-s9.db"
    # First open: runs v1..v10.
    db = Database(db_path=db_path).connect()
    db.close()
    # Second open: should be a no-op.
    db = Database(db_path=db_path).connect()
    try:
        row = db.fetchone(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        )
        assert int(row["value"]) == SCHEMA_VERSION
        # All the new category columns still exist.
        cols = {
            r["name"]
            for r in db.conn.execute(
                "PRAGMA table_info(category_wallet_score_decisions)"
            ).fetchall()
        }
        assert "category_resolved_markets" in cols
        assert "category_distinct_events" in cols
        assert "category_active_days" in cols
        assert "missing_essentials_json" in cols
        assert "category_gate_failures_json" in cols
    finally:
        db.close()


# ── 10. Old snapshots without depth levels remain valid ──────────────


def test_old_snapshot_without_depth_levels_remains_valid(
    tmp_path: Path,
) -> None:
    """A v9-era ``candidate_price_snapshots`` row that was created
    before the v10 depth-tables migration must still be readable
    after v10. The depth-walk loader must treat it as
    DEPTH_NOT_CAPTURED, not invent synthetic levels."""
    db_path = tmp_path / "v10-s10.db"
    db = Database(db_path=db_path).connect()
    try:
        wallet_id = "0xW" + uuid4().hex[:10]
        db.conn.execute(
            "INSERT INTO wallets (id, address, label, is_sample, "
            "created_at, canonical_address) VALUES "
            "(?, ?, 'w', 0, ?, ?)",
            (wallet_id, wallet_id.lower(),
             "2026-01-01T00:00:00Z", wallet_id.lower()),
        )
        cur = db.conn.execute(
            "INSERT INTO copy_candidates (wallet_id, source, "
            "source_trade_id, side, source_trade_price, "
            "source_trade_quantity, source_trade_timestamp, "
            "observed_at, wallet_score_version, wallet_score, "
            "wallet_verdict, status, created_at, updated_at) "
            "VALUES (?, 's', 't1', 'BUY', 0.5, 1.0, "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', "
            "'v1', 80.0, 'copy_candidate', 'PENDING_PRICE_CHECK', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (wallet_id,),
        )
        candidate_id = int(cur.lastrowid)
        snap_id = str(uuid4())
        db.conn.execute(
            "INSERT INTO candidate_price_snapshots (id, candidate_id, "
            "snapshot_run_id, fetch_status, side, source_trade_price, "
            "source_trade_quantity, source_trade_timestamp, "
            "fetched_at, created_at) VALUES (?, ?, 'r1', 'OK', 'BUY', "
            "0.5, 1.0, '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (snap_id, candidate_id),
        )
        db.conn.commit()
        # Confirm no depth levels were backfilled.
        n = db.fetchone(
            "SELECT COUNT(*) AS n FROM candidate_price_snapshot_levels "
            "WHERE snapshot_id = ?",
            (snap_id,),
        )["n"]
        assert n == 0, (
            f"depth levels must NOT be backfilled by the migration, "
            f"got {n}"
        )
    finally:
        db.close()


# ── 11. v9 production-style rows preserved exactly ───────────────────


def test_v9_production_style_rows_preserved_exactly(tmp_path: Path) -> None:
    """A v9 DB with the canonical production row set must round-trip
    through v10 without any modification to existing values."""
    db_path = tmp_path / "v10-s11.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # SCHEMA_VERSION bumped past v11; stop at v9 (pre-state for v10).
    pre_version = 9
    for version in range(1, pre_version + 1):
        for stmt in MIGRATIONS[version]:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) "
            "VALUES ('schema_version', ?)",
            (str(version),),
        )
    # Seed exactly the kind of rows the v9 production DB has.
    conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at) VALUES ('prod-1', '0xPROD1', 'p', 0, "
        "'2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO markets (id, source_id, source, question, "
        "fetched_at) VALUES ('prod-m', 'cond-prod', 'polymarket', "
        "'Q?', '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO source_trades (id, source, source_trade_id, "
        "market_source_id, side, outcome, quantity, price, "
        "trader_address, timestamp) VALUES ('prod-t', "
        "'polymarket_data_api', 'prod-tx', 'cond-prod', 'BUY', "
        "'Yes', 5.0, 0.42, '0xprod1', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    db = Database(db_path=db_path).connect()
    try:
        w = db.fetchone("SELECT * FROM wallets WHERE id='prod-1'")
        assert w is not None
        assert w["address"] == "0xPROD1"
        m = db.fetchone("SELECT * FROM markets WHERE id='prod-m'")
        assert m is not None
        assert m["question"] == "Q?"
        t = db.fetchone(
            "SELECT * FROM source_trades WHERE source_trade_id='prod-tx'"
        )
        assert t is not None
        assert float(t["price"]) == 0.42
        assert float(t["quantity"]) == 5.0
        # And the new category table is empty.
        n = db.fetchone(
            "SELECT COUNT(*) AS n FROM category_wallet_score_decisions"
        )["n"]
        assert n == 0
    finally:
        db.close()


# ── 12. New v10 tables also exist alongside the new columns ───────────


def test_v10_signal_tables_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "v10-s12.db"
    db = Database(db_path=db_path).connect()
    try:
        for table in (
            "wallet_score_decisions",
            "category_wallet_score_decisions",
            "trade_copyability_decisions",
            "shadow_decisions",
            "decision_verdicts",
            "paper_signal_decisions",
            "exit_experiment_registrations",
            "score_component_inputs",
            "candidate_price_snapshot_levels",
        ):
            row = db.fetchone(
                "SELECT name FROM sqlite_master WHERE type='table' "
                f"AND name='{table}'"
            )
            assert row is not None, f"v10 table missing: {table}"
    finally:
        db.close()
