"""Extended migration matrix tests (Chunk 6).

Builds on test_p04_schema_v11.py. Adds:

- Fresh DB: foreign_key_check clean.
- Fresh DB: expected PR 4 tables exist.
- Fresh DB: expected PR 4 indexes exist.
- Fresh DB: no scoring rows invented by initialization.
- Fresh DB: no paper signals invented.
- Fresh DB: no exit tracks invented.
- Fresh DB: candidate_price_snapshots without levels remain valid.
- Fresh DB: schema_v10 CHECK constraints reject invalid rows.
- Migration is idempotent on reconnect.
- Migration source contains no destructive statements.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from polycopy.db import schema_v10
from polycopy.db import schema_v11
from polycopy.db.database import Database
from polycopy.db.schema import SCHEMA_VERSION


def _fresh_db(tmp_path: Path) -> Database:
    return Database(db_path=tmp_path / "matrix.db").connect()


def _read_schema_version(db: Database) -> int:
    row = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
    assert row is not None
    return int(row["value"])


def _table_names(db: Database) -> set[str]:
    rows = db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    return {str(r["name"]) for r in rows}


def _index_names(db: Database) -> set[str]:
    rows = db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )
    return {str(r["name"]) for r in rows}


def _row_count(db: Database, table: str) -> int:
    row = db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")
    assert row is not None
    return int(row["n"])


# ---- Fresh DB: foreign_key_check clean --------------------------------


def test_fresh_db_foreign_key_check_returns_zero_rows(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    rows = db.conn.execute("PRAGMA foreign_key_check").fetchall()
    assert rows == [], f"FK violations on fresh DB: {rows}"


# ---- Fresh DB: expected PR 4 tables exist -----------------------------


def test_fresh_db_has_all_pr4_tables(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    tables = _table_names(db)
    expected = {
        "wallet_score_decisions",
        "category_wallet_score_decisions",
        "trade_copyability_decisions",
        "shadow_decisions",
        "decision_verdicts",
        "paper_signal_decisions",
        "exit_experiment_registrations",
        "score_component_inputs",
        "candidate_price_snapshot_levels",
        "candidate_price_snapshots",
        "copy_candidates",
        "wallets",
        "source_trades",
    }
    missing = expected - tables
    assert not missing, f"Missing tables: {missing}"


# ---- Fresh DB: expected indexes exist ---------------------------------


def test_fresh_db_has_pr4_indexes(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    indexes = _index_names(db)
    expected = {
        "idx_wallet_score_wallet",
        "idx_category_score_wallet",
        "idx_trade_score_wallet",
        "idx_trade_score_verdict",
        "idx_shadow_decision_wallet",
        "idx_shadow_decision_verdict",
        "idx_paper_signal_candidate",
        "idx_paper_signal_approved",
        "idx_paper_signal_wallet",
        "idx_exit_experiment_signal",
        "idx_score_inputs_decision",
        "idx_cpsl_snapshot",
        "idx_cpsl_snapshot_side",
        "idx_cpsl_snapshot_side_level",
    }
    missing = expected - indexes
    assert not missing, f"Missing indexes: {missing}"


# ---- Fresh DB: no rows invented by migration --------------------------


def test_fresh_db_no_scoring_rows_invented(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    assert _row_count(db, "wallet_score_decisions") == 0
    assert _row_count(db, "category_wallet_score_decisions") == 0
    assert _row_count(db, "trade_copyability_decisions") == 0


def test_fresh_db_no_paper_signals_invented(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    assert _row_count(db, "paper_signal_decisions") == 0


def test_fresh_db_no_exit_tracks_invented(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    assert _row_count(db, "exit_experiment_registrations") == 0


def test_fresh_db_no_shadow_rows_invented(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    assert _row_count(db, "shadow_decisions") == 0


# ---- Old snapshots without levels remain valid -----------------------


def test_snapshot_without_levels_is_valid(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) VALUES "
        "('0xW', '0xw', 'w', 0, '2026-01-01T00:00:00Z', '0xw')",
    )
    db.conn.execute(
        "INSERT INTO source_trades (id, source, source_trade_id, "
        "market_source_id, side, outcome, quantity, price, "
        "trader_address, timestamp, is_sample) "
        "VALUES ('st-1', 'polymarket', 'st-1', 'm-src-1', 'BUY', 'YES', "
        "100, 0.5, '0xt', '2026-07-01T00:00:00Z', 0)",
    )
    db.conn.execute(
        "INSERT INTO copy_candidates (wallet_id, source, source_trade_id, "
        "source_trade_internal_id, side, source_trade_price, "
        "source_trade_quantity, source_trade_timestamp, observed_at, "
        "wallet_score_version, wallet_score, wallet_verdict, status, "
        "created_at, updated_at) "
        "VALUES ('0xW', 'polymarket', 'st-1', 'st-1', 'BUY', 0.5, "
        "100, '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z', "
        "'v1', 80.0, 'copy_candidate', 'pending', "
        "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')",
    )
    db.conn.execute(
        "INSERT INTO candidate_price_snapshots (id, candidate_id, "
        "snapshot_run_id, fetch_status, side, source_trade_price, "
        "source_trade_quantity, source_trade_timestamp, fetched_at, "
        "created_at) "
        "VALUES ('snap-1', 1, 'run-1', 'OK', 'BUY', 0.5, 100, "
        "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z', "
        "'2026-07-01T00:00:00Z')",
    )
    db.conn.commit()
    n = _row_count(db, "candidate_price_snapshot_levels")
    assert n == 0
    rows = db.conn.execute("PRAGMA foreign_key_check").fetchall()
    assert rows == []


# ---- Fresh DB CHECK constraints are enforced --------------------------


def _seed_fk_chain(db: Database) -> None:
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) VALUES "
        "('0xW', '0xw', 'w', 0, '2026-01-01T00:00:00Z', '0xw')",
    )
    db.conn.execute(
        "INSERT INTO source_trades (id, source, source_trade_id, "
        "market_source_id, side, outcome, quantity, price, "
        "trader_address, timestamp, is_sample) "
        "VALUES ('st-1', 'polymarket', 'st-1', 'm-src-1', 'BUY', 'YES', "
        "100, 0.5, '0xt', '2026-07-01T00:00:00Z', 0)",
    )
    db.conn.execute(
        "INSERT INTO copy_candidates (wallet_id, source, source_trade_id, "
        "source_trade_internal_id, side, source_trade_price, "
        "source_trade_quantity, source_trade_timestamp, observed_at, "
        "wallet_score_version, wallet_score, wallet_verdict, status, "
        "created_at, updated_at) "
        "VALUES ('0xW', 'polymarket', 'st-1', 'st-1', 'BUY', 0.5, "
        "100, '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z', "
        "'v1', 80.0, 'copy_candidate', 'pending', "
        "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')",
    )
    db.conn.execute(
        "INSERT INTO candidate_price_snapshots (id, candidate_id, "
        "snapshot_run_id, fetch_status, side, source_trade_price, "
        "source_trade_quantity, source_trade_timestamp, fetched_at, "
        "created_at) "
        "VALUES ('snap-1', 1, 'run-1', 'OK', 'BUY', 0.5, 100, "
        "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z', "
        "'2026-07-01T00:00:00Z')",
    )
    db.conn.execute(
        "INSERT INTO paper_signal_decisions (candidate_id, wallet_id, "
        "signal_family, signal_reason, final_verdict, is_approved, "
        "idempotency_key, computed_at, created_at) "
        "VALUES (1, '0xW', 'copy_candidate', 'ok', 'copy_candidate', "
        "0, 'k1', '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')",
    )
    db.conn.commit()


def test_fresh_db_check_rejects_negative_count(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) VALUES "
        "('0xW', '0xw', 'w', 0, '2026-01-01T00:00:00Z', '0xw')",
    )
    db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO wallet_score_decisions "
            "(wallet_id, formula_name, formula_version, idempotency_key, "
            " final_score, verdict, trade_count, computed_at, created_at) "
            "VALUES ('0xW', 'wallet_score', '1', 'k1', 80.0, "
            "'copy_candidate', -1, '2026-07-01T00:00:00Z', "
            "'2026-07-01T00:00:00Z')",
        )


def test_fresh_db_check_rejects_size_zero_in_levels(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    _seed_fk_chain(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO candidate_price_snapshot_levels "
            "(snapshot_id, side, level_index, price, size, "
            " cumulative_size, cumulative_notional, created_at) "
            "VALUES ('snap-1', 'BID', 0, 0.5, 0, 0, 0, "
            "'2026-07-01T00:00:00Z')",
        )


def test_fresh_db_check_rejects_lowercase_legacy_exit_track(
    tmp_path: Path,
) -> None:
    db = _fresh_db(tmp_path)
    _seed_fk_chain(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO exit_experiment_registrations "
            "(paper_signal_id, experiment_type, status, registered_at) "
            "VALUES (1, 'exit_24h', 'registered', '2026-07-01T00:00:00Z')",
        )


# ---- Migration is idempotent on reconnect -----------------------------


def test_reconnect_preserves_state(tmp_path: Path) -> None:
    path = tmp_path / "reconnect.db"
    db1 = Database(db_path=path).connect()
    assert _read_schema_version(db1) == SCHEMA_VERSION
    db1.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) VALUES "
        "('0xW', '0xw', 'w', 0, '2026-01-01T00:00:00Z', '0xw')",
    )
    db1.conn.commit()
    db1.close()

    db2 = Database(db_path=path).connect()
    assert _read_schema_version(db2) == SCHEMA_VERSION
    n = _row_count(db2, "wallets")
    assert n == 1


# ---- Migration source contains no destructive statements --------------


def test_schema_v10_source_has_no_destructive_statements() -> None:
    src = inspect.getsource(schema_v10)
    for tok in ("DROP TABLE", "DROP INDEX", "DROP VIEW", "RENAME TO",
                "TRUNCATE", "UPDATE ", "DELETE FROM", "INSERT INTO"):
        assert tok not in src, (
            f"schema_v10.py contains forbidden token: {tok!r}"
        )


def test_schema_v11_source_has_no_destructive_statements() -> None:
    """The v11 migration is additive ALTER TABLE ADD COLUMN only."""
    src = inspect.getsource(schema_v11)
    for tok in ("DROP TABLE", "DROP INDEX", "DROP VIEW", "RENAME TO",
                "TRUNCATE", "UPDATE ", "DELETE FROM", "INSERT INTO"):
        assert tok not in src, (
            f"schema_v11.py contains forbidden token: {tok!r}"
        )