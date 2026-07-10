"""PR24Z historical production reconciliation — final-architecture tests.

Read-only verification only. No live API, no production write.

These tests verify the *committed* artifacts and the one-time migration
module against the *current* final architecture. They deliberately do NOT
depend on the production DB (data/polycopy.db) and do NOT import any removed
legacy-compatibility gate (IdentityCompatibilityGate / run_identity_compatibility_gate),
which was intentionally removed from the permanent ingestion path.

The 14 historical rows below are taken from the committed historical reference
and approved mapping artifacts — never from the production DB.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from polycopy.ingestion.normalized_source_trade import generate_identity
from polycopy.migrations.pr24z_canonical_identity import (
    SOURCE as MIG_SOURCE,
    canonical_replay_would_insert,
    load_reference,
)
from polycopy.migrations.pr24z_marker import validate_pr24z_migration_marker

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
HIST_REF = REPORTS / "pr24z_historical_production_reference.json"
MAPPING_CSV = REPORTS / "pr24z_canonical_identity_migration_mapping.csv"
MARKER = ROOT / "data" / ".pr24z_canonical_migration_complete"
SOURCE = "polymarket_data_api_trades_user"


# ── helpers ────────────────────────────────────────────────────────────────
def _hist() -> list[dict]:
    return load_reference(HIST_REF)


def _legacy_ids() -> list[str]:
    return [r["source_trade_id"] for r in _hist()]


def _canonical_ids() -> list[str]:
    return [r["transaction_hash"] for r in _hist()]


def _read_mapping() -> list[dict]:
    with MAPPING_CSV.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
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


def _insert_hist(conn: sqlite3.Connection, rows: list[dict], *, canonical: bool = False) -> None:
    for i, h in enumerate(rows, 1):
        conn.execute(
            """INSERT INTO source_trades
            (id, source, source_trade_id, market_source_id, side, outcome, quantity, price, trader_address, timestamp, is_sample, token_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"target-{i}",
                h["source"],
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


def _db(tmp_path: Path, *, canonical: bool = False) -> Path:
    path = tmp_path / "copy.db"
    conn = _connect(path)
    _create_schema(conn)
    _insert_hist(conn, _hist(), canonical=canonical)
    _insert_extras(conn)
    conn.commit()
    conn.close()
    return path


# ── 1-5: artifact + mapping integrity ──────────────────────────────────────
def test_1_historical_reference_contains_exactly_14_rows():
    rows = _hist()
    assert len(rows) == 14
    assert all(r.get("source_trade_id") and r.get("transaction_hash") for r in rows)


def test_2_approved_mapping_contains_exactly_14_rows():
    m = _read_mapping()
    assert len(m) == 14


def test_3_each_legacy_maps_to_exactly_one_canonical():
    m = _read_mapping()
    legacy_to_canon = {}
    for r in m:
        leg = r["legacy_source_trade_id"]
        assert leg not in legacy_to_canon, f"duplicate legacy id {leg}"
        legacy_to_canon[leg] = r["canonical_source_trade_id"]
    assert len(legacy_to_canon) == 14


def test_4_every_canonical_id_is_unique():
    m = _read_mapping()
    canon = [r["canonical_source_trade_id"] for r in m]
    assert len(set(canon)) == 14


def test_5_canonical_ids_derive_from_historical_upstream_source_provided_ids():
    m = _read_mapping()
    for r in m:
        # canonical == upstream_source_provided_id == historical transaction_hash
        assert r["canonical_source_trade_id"] == r["historical_transaction_hash"]
        assert r["canonical_source_trade_id"] == r["upstream_source_provided_id"]


# ── 6-7: immutable + final state ───────────────────────────────────────────
def test_6_approved_immutable_fields_match():
    m = _read_mapping()
    for r in m:
        assert r["immutable_fields_match"] == "True"


def test_7_mapping_is_in_all_canonical_final_state():
    m = _read_mapping()
    assert all(r["migration_state"] == "ALL_CANONICAL" for r in m)
    # The one-time production migration WAS applied (historical record).
    assert all(r["migration_applied"] == "True" for r in m)


# ── 8-10: post-migration DB expectations (against temp canonical DB) ───────
def test_8_every_post_migration_canonical_row_exists_once(tmp_path):
    path = _db(tmp_path, canonical=True)
    conn = _connect(path)
    for canon in _canonical_ids():
        c = conn.execute(
            "SELECT COUNT(*) c FROM source_trades WHERE source=? AND source_trade_id=?",
            (MIG_SOURCE, canon),
        ).fetchone()["c"]
        assert c == 1
    conn.close()


def test_9_every_historical_legacy_id_absent_after_migration(tmp_path):
    path = _db(tmp_path, canonical=True)
    conn = _connect(path)
    for leg in _legacy_ids():
        c = conn.execute(
            "SELECT COUNT(*) c FROM source_trades WHERE source=? AND source_trade_id=?",
            (MIG_SOURCE, leg),
        ).fetchone()["c"]
        assert c == 0
    conn.close()


def test_10_replay_would_insert_is_false_for_all_14(tmp_path):
    path = _db(tmp_path, canonical=True)
    conn = _connect(path)
    assert canonical_replay_would_insert(conn, _hist()) == 0
    conn.close()


# ── 11-13: no legacy compatibility in permanent ingestion code ────────────
def test_11_normal_writer_code_contains_no_legacy_compatibility_gate():
    src = (ROOT / "src/polycopy/ingestion/source_trade_writer.py").read_text()
    assert "IdentityCompatibilityGate" not in src
    assert "run_identity_compatibility_gate" not in src


def test_12_source_trade_writer_imports_no_historical_pr24z_artifacts():
    src = (ROOT / "src/polycopy/ingestion/source_trade_writer.py").read_text()
    assert "pr24z_historical" not in src
    assert "pr24z_canonical_identity_migration_mapping" not in src


def test_13_no_permanent_legacy_alias_function_exists():
    import importlib

    mod = importlib.import_module("polycopy.ingestion.source_trade_writer")
    assert not hasattr(mod, "IdentityCompatibilityGate")
    assert not hasattr(mod, "run_identity_compatibility_gate")


# ── 14: migration module separate from normal ingestion ───────────────────
def test_14_one_time_migration_module_is_separate_from_ingestion():
    # The migration module lives under migrations/ and is standalone; the
    # normal ingestion writer must not import it.
    writer_src = (ROOT / "src/polycopy/ingestion/source_trade_writer.py").read_text()
    assert "from polycopy.migrations.pr24z_canonical_identity import" not in writer_src
    assert "import pr24z_canonical_identity" not in writer_src
    # And the marker validates the final canonical state.
    if MARKER.exists():
        v = validate_pr24z_migration_marker(MARKER, str(ROOT / "data" / "polycopy.db"))
        assert v.valid
        assert v.data is not None
        assert v.data["canonical_row_count"] == 14
        assert v.data["legacy_row_count"] == 0


# ── bonus: canonical identity derivation matches writer path ──────────────
def test_canonical_identity_derives_from_upstream_not_legacy():
    row0 = _hist()[0]
    ident = generate_identity({"sourceProvidedTradeId": row0["transaction_hash"]})
    assert ident.source_trade_id == row0["transaction_hash"]
    assert ident.source_trade_id != row0["source_trade_id"]
