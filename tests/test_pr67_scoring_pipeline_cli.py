from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from polycopy.db.database import Database
from tests.test_p04_chunk4_runtime_paper_signal import (
    _insert_candidate,
    _insert_depth_levels,
    _insert_snapshot,
    _insert_source_trade,
    _insert_wallet,
    _insert_market,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_wallet_scoring_pipeline.py"
spec = importlib.util.spec_from_file_location("pr67_cli", SCRIPT)
assert spec and spec.loader
cli = importlib.util.module_from_spec(spec)
sys.modules["pr67_cli"] = cli
spec.loader.exec_module(cli)

FORBIDDEN = (
    "source_trades",
    "wallets",
    "markets",
    "market_outcomes",
    "copy_candidates",
    "candidate_price_snapshots",
    "candidate_price_snapshot_levels",
    "shadow_decisions",
    "exit_experiment_registrations",
    "orders",
    "positions",
    "settlement_accounting_ledger",
)
ALLOWED = {
    "wallet_score_decisions",
    "category_wallet_score_decisions",
    "trade_copyability_decisions",
    "paper_signal_decisions",
}


def _hash_table(conn: sqlite3.Connection, table: str) -> str:
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is None
    ):
        return "ABSENT"
    cols = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
    rows = [
        list(row) for row in conn.execute(f'SELECT {",".join(cols)} FROM "{table}" ORDER BY rowid')
    ]
    return hashlib.sha256(json.dumps(rows, default=str, separators=(",", ":")).encode()).hexdigest()


def _args(path: Path, *extra: str):
    return cli.build_parser().parse_args(["--db-path", str(path), *extra])


def _seed(path: Path) -> tuple[Database, list[int]]:
    db = Database(path).connect()
    wallet = _insert_wallet(db, "wallet-a")
    market, outcome = _insert_market(db)
    meta = json.dumps(
        {
            "event": {"id": "e1", "slug": "not-category", "title": "ignored"},
            "taxonomy": {"raw_category": "Politics"},
        }
    )
    ids = []
    for number, (side, status, won, pnl) in enumerate(
        (
            ("BUY", "won", 1, 2.0),
            ("BUY", "lost", 0, -1.0),
            ("BUY", "unresolved", None, None),
            ("SELL", "won", 1, 99.0),
        )
    ):
        tid = _insert_source_trade(
            db,
            trader_address=wallet.lower(),
            side=side,
            timestamp=f"2026-07-0{number + 1}T00:00:00Z",
        )
        db.execute(
            "UPDATE source_trades SET resolution_status=?, is_winning_trade=?, realized_pnl=?, metadata_json=? WHERE id=?",
            (status, won, pnl, meta, tid),
        )
    for number in range(2):
        trade_id = _insert_source_trade(
            db,
            trader_address=wallet.lower(),
            side="BUY",
            timestamp=f"2026-07-0{number + 3}T00:00:00Z",
        )
        db.execute(
            "UPDATE source_trades SET resolution_status='won', is_winning_trade=1, realized_pnl=1.0, metadata_json=? WHERE id=?",
            (meta, trade_id),
        )
        cid = _insert_candidate(
            db,
            wallet_id=wallet,
            source_trade_id=trade_id,
            source_trade_internal_id=trade_id,
            market_id=market,
            market_outcome_id=outcome,
            observed_at=f"2026-07-0{number + 5}T00:00:00Z",
        )
        snap = _insert_snapshot(db, candidate_id=cid, fetched_at=f"2026-07-0{number + 5}T00:00:00Z")
        _insert_depth_levels(db, snapshot_id=snap)
        ids.append(cid)
    db.conn.commit()
    return db, ids


def test_default_dry_run_is_read_only_and_json_and_human_output(tmp_path, capsys):
    path = tmp_path / "fixture.db"
    db, ids = _seed(path)
    db.close()
    before_file = hashlib.sha256(path.read_bytes()).hexdigest()
    conn = sqlite3.connect(path)
    before = {table: _hash_table(conn, table) for table in FORBIDDEN + tuple(ALLOWED)}
    conn.close()

    assert cli.main(["--db-path", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["aggregate"]["dry_run"] is True
    assert payload["aggregate"]["decisions_would_create"] > 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_file

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    assert {table: _hash_table(conn, table) for table in FORBIDDEN + tuple(ALLOWED)} == before
    conn.close()
    assert cli.main(["--db-path", str(path), "--include-details"]) == 0
    assert "mode=dry_run" in capsys.readouterr().out


def test_apply_only_decision_tables_replays_and_no_orders_positions(tmp_path, monkeypatch):
    path = tmp_path / "fixture.db"
    db, ids = _seed(path)
    db.close()
    writes: list[str] = []
    real_open = cli.open_connection

    def traced(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        conn.set_trace_callback(lambda sql: writes.append(sql))
        return conn

    monkeypatch.setattr(cli, "open_connection", traced)
    first = cli.run(_args(path, "--apply", "--limit", "2"))
    second = cli.run(_args(path, "--apply", "--limit", "2"))
    assert first["aggregate"]["decisions_inserted"] > 0
    assert second["aggregate"]["decisions_inserted"] == 0
    targets = set()
    for sql in writes:
        words = " ".join(sql.split()).upper()
        if words.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            assert not words.startswith(("UPDATE", "DELETE", "REPLACE"))
            for table in ALLOWED | set(FORBIDDEN):
                if f" {table.upper()}" in words:
                    targets.add(table)
    assert targets <= ALLOWED
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    conn.close()


def test_selection_wallet_bounds_snapshot_and_fail_fast(tmp_path):
    path = tmp_path / "fixture.db"
    db, ids = _seed(path)
    second_wallet = _insert_wallet(db, "wallet-b")
    db.execute("UPDATE wallets SET canonical_address='0xabc111' WHERE id='wallet-a'")
    db.execute("UPDATE wallets SET canonical_address='0xabc222' WHERE id=?", (second_wallet,))
    db.conn.commit()
    db.close()
    assert [
        item["candidate_id"]
        for item in cli.run(_args(path, "--candidate-id", str(ids[0]), "--include-details"))["candidates"]
    ] == [ids[0]]
    assert cli.run(_args(path, "--wallet", "0xabc111"))["aggregate"]["examined"] == 2
    with pytest.raises(cli.SafetyError, match="ambiguous"):
        cli.run(_args(path, "--wallet", "0xabc"))
    assert [
        item["candidate_id"]
        for item in cli.run(_args(path, "--limit", "1", "--include-details"))["candidates"]
    ] == [ids[0]]
    assert [
        item["candidate_id"]
        for item in cli.run(_args(path, "--limit", "1", "--offset", "1", "--include-details"))[
            "candidates"
        ]
    ] == [ids[1]]
    for argv in (
        ("--limit", "51"),
        ("--offset", "-1"),
        ("--candidate-id", str(ids[0]), "--offset", "1"),
    ):
        with pytest.raises(cli.SafetyError):
            cli.run(_args(path, *argv))
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM candidate_price_snapshots WHERE candidate_id=?", (ids[1],))
    conn.commit()
    conn.close()
    result = cli.run(_args(path, "--candidate-id", str(ids[1]), "--include-details"))
    assert result["candidates"][0]["paper_verdict"] == "INCOMPLETE"


def test_network_isolation_and_candidate_failure_reporting(tmp_path, monkeypatch, capsys):
    path = tmp_path / "fixture.db"
    db, ids = _seed(path)
    db.close()
    calls: list[int] = []

    def failed(*args, **kwargs):
        calls.append(int(args[1]))
        return {
            "candidate_id": ids[0],
            "outcome_kind": "failed",
            "verdict": "INCOMPLETE",
            "reason": "synthetic",
            "is_approved": 0,
            "paper_signal_id": None,
        }

    monkeypatch.setattr(cli, "evaluate_paper_signals_for_candidate", failed)
    assert cli.main(["--db-path", str(path), "--json", "--fail-fast"]) == 1
    assert calls == [ids[0]]
    assert json.loads(capsys.readouterr().out)["aggregate"]["failed"] == 1
    with pytest.raises(cli.SafetyError, match="unresolvable"):
        cli.run(_args(tmp_path / "absent.db"))


def test_production_gates_symlink_and_apply_authorization(tmp_path, monkeypatch):
    path = tmp_path / "fixture.db"
    db, ids = _seed(path)
    db.close()
    monkeypatch.setattr(cli, "PRODUCTION_DB", path.resolve())
    assert cli.run(_args(path))["aggregate"]["production_db"] is True
    with pytest.raises(cli.SafetyError, match="confirm"):
        cli.run(_args(path, "--apply"))
    with pytest.raises(cli.SafetyError, match="requires --apply"):
        cli.run(_args(path, "--confirm-production-db"))
    called = []
    monkeypatch.setattr(
        cli, "operational_job_lock", lambda *a, **k: called.append((a, k)) or nullcontext()
    )
    cli.run(_args(path, "--apply", "--confirm-production-db", "--candidate-id", str(ids[0])))
    assert called and called[0][1]["timeout"] == 0.0
    alias = tmp_path / "alias.db"
    alias.symlink_to(path)
    assert cli.is_production_db(cli.resolve_db_path(str(alias)))


def test_no_network_imports_or_forbidden_dml_in_cli_source():
    source = SCRIPT.read_text()
    for forbidden in (
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "Database.connect",
        "create_order",
        "submit_order",
        "systemctl",
        "timer",
    ):
        assert forbidden not in source


def test_read_only_uri_wal_visibility_write_rejection_and_schema_validation(tmp_path):
    path = tmp_path / "wal.db"
    writer = sqlite3.connect(path)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    writer.execute("CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    writer.execute("INSERT INTO _meta VALUES ('schema_version', '17')")
    writer.execute("CREATE TABLE committed_wal_data (value TEXT)")
    writer.execute("INSERT INTO committed_wal_data VALUES ('visible')")
    writer.commit()
    assert path.with_name(path.name + "-wal").exists()
    reader = cli.open_connection(path.resolve(), readonly=True)
    assert reader.execute("SELECT value FROM committed_wal_data").fetchone()[0] == "visible"
    assert cli.read_schema_version(reader) == 17
    with pytest.raises(sqlite3.OperationalError):
        reader.execute("INSERT INTO committed_wal_data VALUES ('blocked')")
    reader.close()
    writer.close()
    uri_source = SCRIPT.read_text()
    assert '?mode=ro"' in uri_source
    assert "mode=ro&immutable=1" not in uri_source


def test_schema_metadata_missing_zero_and_invalid_are_explicit(tmp_path):
    path = tmp_path / "meta.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    reader = cli.open_connection(path.resolve(), readonly=True)
    with pytest.raises(cli.SafetyError, match="missing"):
        cli.read_schema_version(reader)
    reader.close()
    conn.execute("INSERT INTO _meta VALUES ('schema_version', '0')")
    conn.commit()
    reader = cli.open_connection(path.resolve(), readonly=True)
    with pytest.raises(cli.SafetyError, match="invalid"):
        cli.read_schema_version(reader)
    reader.close()
    conn.execute("UPDATE _meta SET value='not-a-number'")
    conn.commit()
    reader = cli.open_connection(path.resolve(), readonly=True)
    with pytest.raises(cli.SafetyError, match="invalid"):
        cli.read_schema_version(reader)
    reader.close()
    conn.close()
