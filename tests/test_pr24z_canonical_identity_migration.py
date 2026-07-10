from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from polycopy.ingestion.normalized_source_trade import generate_identity
from polycopy.migrations.pr24z_canonical_identity import (
    DEFAULT_REFERENCE_PATH,
    IMMUTABLE_FIELDS,
    SOURCE,
    load_reference,
    migrate,
    trust_gate,
)
from polycopy.migrations.pr24z_marker import validate_pr24z_migration_marker


ROOT = Path(__file__).resolve().parents[1]
REF = DEFAULT_REFERENCE_PATH


def _hist():
    return load_reference(REF)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE source_trades (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            source_trade_id TEXT NOT NULL,
            market_source_id TEXT NOT NULL,
            side TEXT NOT NULL,
            outcome TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            trader_address TEXT,
            timestamp TEXT NOT NULL,
            is_sample INTEGER NOT NULL DEFAULT 0,
            token_id TEXT,
            UNIQUE(source, source_trade_id)
        );
        CREATE TABLE trade_copyability_decisions (id INTEGER PRIMARY KEY, source_trade_id TEXT NOT NULL);
        CREATE TABLE copy_candidates (id INTEGER PRIMARY KEY, source_trade_id TEXT NOT NULL, source_trade_internal_id TEXT REFERENCES source_trades(id));
        CREATE TABLE paper_signal_decisions (id INTEGER PRIMARY KEY, candidate_id INTEGER, source_trade_id TEXT);
        CREATE TABLE candidate_price_snapshots (id TEXT PRIMARY KEY, candidate_id INTEGER NOT NULL);
        CREATE TABLE candidate_price_snapshot_levels (id INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES candidate_price_snapshots(id));
        CREATE TABLE orders (id TEXT PRIMARY KEY, source_order_id TEXT);
        CREATE TABLE positions (id TEXT PRIMARY KEY, wallet_id TEXT);
        CREATE TABLE settlement_accounting_ledger (id TEXT PRIMARY KEY, source_trade_id TEXT NOT NULL REFERENCES source_trades(id));
        CREATE TABLE wallet_score_decisions (id INTEGER PRIMARY KEY, wallet_id TEXT NOT NULL, candidate_id INTEGER);
        """
    )


def _insert_hist(conn: sqlite3.Connection, rows: list[dict], *, canonical: bool = False, limit: int = 14) -> None:
    for i, h in enumerate(rows[:limit], 1):
        conn.execute(
            """INSERT INTO source_trades
            (id, source, source_trade_id, market_source_id, side, outcome, quantity, price, trader_address, timestamp, is_sample, token_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"target-{i}",
                SOURCE,
                h["transaction_hash"] if canonical else h["source_trade_id"],
                h["market_source_id"],
                h["side"],
                h["outcome"],
                h["quantity"],
                h["price"],
                h["trader_address"],
                h["timestamp"],
                h["is_sample"],
                h["token_id"],
            ),
        )


def _insert_extras(conn: sqlite3.Connection) -> None:
    for i in range(5):
        conn.execute(
            """INSERT INTO source_trades
            (id, source, source_trade_id, market_source_id, side, outcome, quantity, price, trader_address, timestamp, is_sample, token_id)
            VALUES (?, ?, ?, ?, 'BUY', 'Yes', 1, 0.5, '0xabc', '2026-01-01T00:00:00+00:00', 0, ?)""",
            (f"extra-{i}", SOURCE, f"polymarket:extra{i}", f"market-{i}", f"token-{i}"),
        )


def _db(tmp_path: Path, *, canonical: bool = False, limit: int = 14, extras: bool = True) -> Path:
    path = tmp_path / "copy.db"
    conn = _connect(path)
    _create_schema(conn)
    rows = _hist()
    _insert_hist(conn, rows, canonical=canonical, limit=limit)
    if extras:
        _insert_extras(conn)
    conn.commit()
    conn.close()
    return path


def _row_ids(path: Path) -> list[tuple[str, str]]:
    conn = _connect(path)
    rows = conn.execute("SELECT id, source_trade_id FROM source_trades WHERE id LIKE 'target-%' ORDER BY id").fetchall()
    conn.close()
    return [(r["id"], r["source_trade_id"]) for r in rows]


def test_01_14_upstream_ids_load_from_historical_transaction_hash():
    rows = _hist()
    assert len(rows) == 14
    assert all(r["transaction_hash"] == r["sourceProvidedTradeId"] for r in rows)


def test_02_immutable_trust_gate_passes_for_matching_rows(tmp_path):
    path = _db(tmp_path)
    conn = _connect(path)
    assert trust_gate(conn, _hist()) == (14, 14, 0)
    conn.close()


def test_03_immutable_mismatch_blocks_migration(tmp_path):
    path = _db(tmp_path)
    conn = _connect(path)
    conn.execute("UPDATE source_trades SET price=0.123 WHERE id='target-1'")
    conn.commit()
    conn.close()
    res = migrate(path, apply=True, marker_path=tmp_path / "marker.json", reports_dir=tmp_path / "reports")
    assert not res.ok
    assert res.error is not None and "trust gate" in res.error


def test_04_row_1_canonical_id_derives_from_e0c9_not_11ae():
    row = _hist()[0]
    ident = generate_identity({"sourceProvidedTradeId": row["transaction_hash"]})
    assert ident.source_trade_id is not None
    assert ident.source_trade_id.startswith("polymarket:e0c9d495")
    assert not ident.source_trade_id.startswith("polymarket:11ae80be")


def test_05_row_2_canonical_id_derives_from_9b811_not_9b9b():
    row = _hist()[1]
    ident = generate_identity({"sourceProvidedTradeId": row["transaction_hash"]})
    assert ident.source_trade_id is not None
    assert ident.source_trade_id.startswith("polymarket:9b811fe6")
    assert not ident.source_trade_id.startswith("polymarket:9b9b74c3")


def test_06_all_legacy_updates_exactly_14(tmp_path):
    path = _db(tmp_path)
    res = migrate(path, apply=True, marker_path=tmp_path / "marker.json", reports_dir=tmp_path / "reports")
    assert res.ok and res.rows_updated == 14 and res.canonical_row_count == 14 and res.legacy_row_count == 0


def test_07_all_canonical_noops_successfully(tmp_path):
    path = _db(tmp_path, canonical=True)
    res = migrate(path, apply=True, marker_path=tmp_path / "marker.json", reports_dir=tmp_path / "reports")
    assert res.ok and res.already_migrated and res.rows_updated == 0


def test_08_second_run_is_idempotent(tmp_path):
    path = _db(tmp_path)
    first = migrate(path, apply=True, marker_path=tmp_path / "marker.json", reports_dir=tmp_path / "r1")
    before = _row_ids(path)
    second = migrate(path, apply=True, marker_path=tmp_path / "marker2.json", reports_dir=tmp_path / "r2")
    assert first.ok and second.ok and second.already_migrated and _row_ids(path) == before


def test_09_mixed_fails_closed(tmp_path):
    path = _db(tmp_path)
    h = _hist()[0]
    conn = _connect(path)
    conn.execute("UPDATE source_trades SET source_trade_id=? WHERE id='target-1'", (h["transaction_hash"],))
    conn.commit()
    conn.close()
    res = migrate(path, apply=True, marker_path=tmp_path / "marker.json", reports_dir=tmp_path / "reports")
    assert not res.ok and res.state == "MIXED" and res.rows_updated == 0


def test_10_missing_fails_closed(tmp_path):
    path = _db(tmp_path, limit=13)
    res = migrate(path, apply=True, marker_path=tmp_path / "marker.json", reports_dir=tmp_path / "reports")
    assert not res.ok and res.state == "MISSING"


def test_11_duplicate_fails_closed(tmp_path):
    path = tmp_path / "dup.db"
    conn = _connect(path)
    _create_schema(conn)
    conn.execute("DROP INDEX sqlite_autoindex_source_trades_2") if False else None
    # Recreate without the unique target constraint to simulate a corrupted DB duplicate.
    conn.executescript("DROP TABLE source_trades; CREATE TABLE source_trades (id TEXT PRIMARY KEY, source TEXT NOT NULL, source_trade_id TEXT NOT NULL, market_source_id TEXT NOT NULL, side TEXT NOT NULL, outcome TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL, trader_address TEXT, timestamp TEXT NOT NULL, is_sample INTEGER NOT NULL DEFAULT 0, token_id TEXT);")
    _insert_hist(conn, _hist())
    conn.execute("INSERT INTO source_trades SELECT 'dupe-target', source, source_trade_id, market_source_id, side, outcome, quantity, price, trader_address, timestamp, is_sample, token_id FROM source_trades WHERE id='target-1'")
    _insert_extras(conn)
    conn.commit()
    conn.close()
    res = migrate(path, apply=True, marker_path=tmp_path / "marker.json", reports_dir=tmp_path / "reports")
    assert not res.ok and res.state == "DUPLICATE"


def test_12_collision_fails_closed(tmp_path):
    path = _db(tmp_path)
    h = _hist()[0]
    conn = _connect(path)
    conn.execute("UPDATE source_trades SET source_trade_id=? WHERE id='extra-0'", (h["transaction_hash"],))
    conn.commit()
    conn.close()
    res = migrate(path, apply=True, marker_path=tmp_path / "marker.json", reports_dir=tmp_path / "reports")
    assert not res.ok and res.state == "COLLISION"


def test_13_partial_update_error_rolls_back_all_changes(tmp_path):
    path = _db(tmp_path)
    blocked_canonical = _hist()[7]["transaction_hash"]
    conn = _connect(path)
    conn.execute(
        """
        CREATE TRIGGER fail_mid_migration
        BEFORE UPDATE OF source_trade_id ON source_trades
        WHEN NEW.source_trade_id = '%s'
        BEGIN
          SELECT RAISE(ABORT, 'forced mid-migration failure');
        END;
        """ % blocked_canonical
    )
    conn.commit()
    conn.close()
    res = migrate(path, apply=True, marker_path=tmp_path / "marker.json", reports_dir=tmp_path / "reports")
    assert not res.ok
    assert all(sid == _hist()[int(i.split('-')[1])-1]["source_trade_id"] for i, sid in _row_ids(path))


def test_14_source_trades_id_remains_unchanged(tmp_path):
    path = _db(tmp_path)
    before = [i for i, _ in _row_ids(path)]
    assert migrate(path, apply=True, marker_path=tmp_path / "marker.json", reports_dir=tmp_path / "reports").ok
    assert [i for i, _ in _row_ids(path)] == before


def test_15_immutable_fields_remain_unchanged(tmp_path):
    path = _db(tmp_path)
    rows = _hist()
    assert migrate(path, apply=True, marker_path=tmp_path / "marker.json", reports_dir=tmp_path / "reports").ok
    conn = _connect(path)
    for idx, h in enumerate(rows, 1):
        r = conn.execute("SELECT * FROM source_trades WHERE id=?", (f"target-{idx}",)).fetchone()
        assert all((float(r[f]) == float(h[f]) if f in {"price", "quantity"} else r[f] == h[f]) for f in IMMUTABLE_FIELDS)
    conn.close()


def test_16_source_trades_count_remains_unchanged(tmp_path):
    path = _db(tmp_path)
    assert migrate(path, apply=True, marker_path=tmp_path / "marker.json", reports_dir=tmp_path / "reports").source_trades_count == 19


def test_17_wallet_score_decisions_remains_unchanged_and_valid(tmp_path):
    path = _db(tmp_path)
    conn = _connect(path)
    conn.execute("INSERT INTO wallet_score_decisions (wallet_id) VALUES ('wallet-1')")
    conn.commit()
    conn.close()
    res = migrate(path, apply=True, marker_path=tmp_path / "marker.json", reports_dir=tmp_path / "reports")
    assert res.ok
    assert res.dependency_audit["tables"]["wallet_score_decisions"]["wallet_score_decisions_linkage"] == "otherwise trade-linked"
    conn = _connect(path)
    assert conn.execute("SELECT COUNT(*) FROM wallet_score_decisions").fetchone()[0] == 1
    conn.close()


def test_18_canonical_replay_yields_0_would_insert_rows(tmp_path):
    path = _db(tmp_path)
    res = migrate(path, apply=True, marker_path=tmp_path / "marker.json", reports_dir=tmp_path / "reports")
    assert res.ok and all(not m.replay_would_insert for m in res.mapping)


def test_19_marker_is_not_created_on_failure(tmp_path):
    path = _db(tmp_path, limit=13)
    marker = tmp_path / "marker.json"
    res = migrate(path, apply=True, marker_path=marker, reports_dir=tmp_path / "reports")
    assert not res.ok and not marker.exists()


def test_20_marker_created_only_after_complete_verification(tmp_path):
    path = _db(tmp_path)
    marker = tmp_path / "marker.json"
    res = migrate(path, apply=True, marker_path=marker, reports_dir=tmp_path / "reports")
    data = json.loads(marker.read_text())
    assert res.ok and marker.exists() and data["canonical_row_count"] == 14 and data["legacy_row_count"] == 0 and data["integrity_result"] == "ok"


def test_20b_migration_marker_conforms_to_shared_validator(tmp_path):
    path = _db(tmp_path)
    marker = tmp_path / "marker.json"
    res = migrate(path, apply=True, marker_path=marker, reports_dir=tmp_path / "reports")
    validation = validate_pr24z_migration_marker(marker, path)
    assert res.ok and validation.valid


def test_21_normal_ingestion_still_contains_no_permanent_legacy_alias_behavior():
    src = (ROOT / "src/polycopy/ingestion/source_trade_writer.py").read_text()
    assert "IdentityCompatibilityGate" not in src
    assert "run_identity_compatibility_gate" not in src


def test_22_no_test_opens_production_db_for_writing():
    # This test module constructs temp DBs only; production DB is never passed with apply=True.
    forbidden = "data" + "/" + "polycopy.db"
    assert forbidden not in Path(__file__).read_text()
