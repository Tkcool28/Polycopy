"""PR66 checkpoint 1: additive schema v17 and source-trade metadata contract."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import cast
from pathlib import Path

import pytest

import polycopy.db.database as database_module
from polycopy.db.database import Database
from polycopy.db.schema import SCHEMA_VERSION
from polycopy.ingestion.normalized_source_trade import normalize_source_trade
from polycopy.ingestion.source_trade_metadata import (
    normalize_source_trade_metadata,
    serialize_source_trade_metadata,
)
from polycopy.ingestion.source_trade_writer import write_valid_rows

WALLET = "0x" + "a" * 40
TOKEN = "0x" + "b" * 64
MARKET = "0x" + "c" * 64


def _raw(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "sourceProvidedTradeId": "fill-1",
        "proxyWallet": WALLET,
        "asset": TOKEN,
        "conditionId": MARKET,
        "side": "BUY",
        "price": "0.40",
        "size": "2",
        "timestamp": 1700000000,
    }
    raw.update(overrides)
    return raw


def _source_shape(conn: sqlite3.Connection) -> dict[str, object]:
    return {
        "columns": [tuple(row) for row in conn.execute("PRAGMA table_info(source_trades)")],
        "indexes": sorted(
            (row[1], row[2], tuple(item[2] for item in conn.execute(f"PRAGMA index_info({row[1]})")))
            for row in conn.execute("PRAGMA index_list(source_trades)")
        ),
        "foreign_keys": [tuple(row) for row in conn.execute("PRAGMA foreign_key_list(source_trades)")],
        "table_sql": conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_trades'"
        ).fetchone()[0],
    }


def test_fresh_db_has_v17_metadata_column_and_wallet_history_index(tmp_path: Path) -> None:
    db = Database(tmp_path / "fresh.db").connect()
    try:
        assert db.conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        columns = {row[1] for row in db.conn.execute("PRAGMA table_info(source_trades)")}
        assert "metadata_json" in columns
        index_rows = {
            row[1]: row for row in db.conn.execute("PRAGMA index_list(source_trades)")
        }
        assert "idx_source_trades_wallet_timestamp" in index_rows
        assert index_rows["idx_source_trades_wallet_timestamp"][2] == 0
        assert [row[2] for row in db.conn.execute(
            "PRAGMA index_info(idx_source_trades_wallet_timestamp)"
        )] == ["trader_address", "timestamp"]
        assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert list(db.conn.execute("PRAGMA foreign_key_check")) == []
    finally:
        db.close()


def test_genuine_v16_upgrade_preserves_rows_and_matches_fresh_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_path = tmp_path / "legacy-v16.db"
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 16)
    legacy = Database(legacy_path).connect()
    try:
        assert legacy.conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()[0] == "16"
        assert "metadata_json" not in {
            row[1] for row in legacy.conn.execute("PRAGMA table_info(source_trades)")
        }
        legacy.conn.execute(
            """INSERT INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample, token_id)
               VALUES ('legacy-id', 'test', 'legacy-fill', ?, 'BUY', 'Yes',
                       2, .4, ?, '2024-01-01T00:00:00+00:00', 0, ?)""",
            (MARKET, WALLET, TOKEN),
        )
        legacy.conn.commit()
    finally:
        legacy.close()

    monkeypatch.setattr(database_module, "SCHEMA_VERSION", SCHEMA_VERSION)
    upgraded = Database(legacy_path).connect()
    fresh = Database(tmp_path / "fresh.db").connect()
    try:
        assert upgraded.conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        assert upgraded.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0] == 1
        assert upgraded.conn.execute(
            "SELECT source_trade_id FROM source_trades WHERE id='legacy-id'"
        ).fetchone()[0] == "legacy-fill"
        assert upgraded.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert list(upgraded.conn.execute("PRAGMA foreign_key_check")) == []
        assert _source_shape(upgraded.conn) == _source_shape(fresh.conn)
    finally:
        upgraded.close()
        fresh.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {
                "event": {"id": "event-1", "slug": "event-slug", "title": "Event"},
                "category": "Politics",
                "tags": ["z", "a", "z", " ", 7, {"bad": True}],
                "series": {"id": "series-1", "slug": "series-slug", "title": "Series", "ticker": "SER"},
                "ignored": "must-not-persist",
            },
            {
                "metadata_version": "1",
                "event": {"id": "event-1", "slug": "event-slug", "title": "Event"},
                "taxonomy": {"raw_category": "Politics", "tags": ["7", "a", "z"]},
                "series": {"id": "series-1", "slug": "series-slug", "title": "Series", "ticker": "SER"},
            },
        ),
        (
            {"event": "malformed", "taxonomy": "malformed", "series": [], "tags": "not-a-list"},
            {
                "metadata_version": "1",
                "event": {"id": None, "slug": None, "title": None},
                "taxonomy": {"raw_category": None, "tags": []},
                "series": {"id": None, "slug": None, "title": None, "ticker": None},
            },
        ),
        (
            {},
            {
                "metadata_version": "1",
                "event": {"id": None, "slug": None, "title": None},
                "taxonomy": {"raw_category": None, "tags": []},
                "series": {"id": None, "slug": None, "title": None, "ticker": None},
            },
        ),
    ],
)
def test_canonical_metadata_contract(raw: dict[str, object], expected: dict[str, object]) -> None:
    assert normalize_source_trade_metadata(raw) == expected
    assert json.loads(serialize_source_trade_metadata(raw)) == expected


def test_metadata_serialization_is_deterministic_and_isolated_from_identity() -> None:
    first = _raw(event={"slug": "first"}, tags=["b", "a"])
    equivalent = _raw(event={"slug": "first"}, tags=("a", "b"))
    richer = _raw(event={"slug": "second", "title": "richer"}, category="Politics")

    assert serialize_source_trade_metadata(first) == serialize_source_trade_metadata(equivalent)
    assert normalize_source_trade(first).source_trade_id == normalize_source_trade(richer).source_trade_id


def test_writer_round_trip_is_canonical_first_insert_wins_and_slug_is_not_category(tmp_path: Path) -> None:
    db = Database(tmp_path / "writer.db").connect()
    try:
        first = normalize_source_trade(_raw(event={"slug": "election-2026"}, category="Politics"))
        replay = normalize_source_trade(_raw(event={"slug": "different-event"}, category="Different"))
        assert first.validation_status == replay.validation_status == "valid"
        assert write_valid_rows(db, [first], dry_run=False).inserted == 1
        assert write_valid_rows(db, [replay], dry_run=False).deduplicated == 1
        stored = db.conn.execute(
            "SELECT metadata_json FROM source_trades WHERE source_trade_id=?", (first.source_trade_id,)
        ).fetchone()[0]
        assert stored == serialize_source_trade_metadata(first.metadata)
        payload = json.loads(stored)
        assert payload["event"]["slug"] == "election-2026"
        assert payload["taxonomy"]["raw_category"] == "Politics"
        assert "category_label" not in payload
    finally:
        db.close()


def test_writer_rejects_a_latest_schema_without_metadata_column() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO _meta VALUES ('schema_version', '17')")
    conn.execute(
        """CREATE TABLE source_trades (
               id TEXT PRIMARY KEY, source TEXT NOT NULL, source_trade_id TEXT NOT NULL,
               market_source_id TEXT NOT NULL, side TEXT NOT NULL, outcome TEXT NOT NULL,
               quantity REAL NOT NULL, price REAL NOT NULL, trader_address TEXT,
               timestamp TEXT NOT NULL, is_sample INTEGER NOT NULL, token_id TEXT,
               UNIQUE(source, source_trade_id))"""
    )
    candidate = normalize_source_trade(_raw())
    with pytest.raises(RuntimeError, match="metadata_json"):
        write_valid_rows(cast(Database, SimpleNamespace(conn=conn)), [candidate], dry_run=False)


def test_existing_buy_only_normalization_is_unchanged_when_metadata_is_absent() -> None:
    candidate = normalize_source_trade(_raw(side="SELL"))
    assert candidate.side == "SELL"
    assert candidate.validation_status == "rejected"
    assert "unsupported_side" in candidate.validation_reasons
    assert candidate.metadata == normalize_source_trade_metadata({})
