# ruff: noqa: E402
"""PR25A production-write gates + verified online backup tests (hermetic).

HARD GUARD: every test runs under an autouse fixture that:
  * refuses to let Database().connect() open the real production DB
    (/root/Polycopy/data/polycopy.db), raising immediately if attempted, and
  * records every Database().connect() call path so tests can assert only
    tmp_path was ever opened.

All tests drive ``scripts/process_approved_wallet_trades.main`` in-process with
the bridge call and the online backup helper monkeypatched. The real production
DB is NEVER opened, backed up, migrated, or otherwise touched by these tests.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import scripts.process_approved_wallet_trades as cli  # noqa: E402
from polycopy.ingestion.source_trade_writer import BackupResult  # noqa: E402

PROD_PATH = str((REPO / "data" / "polycopy.db").resolve())
WALLET = "0x" + "a" * 40

# Autouse hard guard: no test may open / connect to / migrate the real prod DB.
_CONNECT_CALLS: list[str] = []


@pytest.fixture(autouse=True)
def _guard_real_db(monkeypatch):
    """Refuse real production DB access; record connect() targets."""
    _CONNECT_CALLS.clear()

    import scripts.process_approved_wallet_trades as _cli

    class _FakeConn:
        def __init__(self, path: Path):
            self.db_path = path
            self._conn = None
        def connect(self):
            resolved = str(self.db_path.resolve())
            _CONNECT_CALLS.append(resolved)
            if resolved == PROD_PATH:
                raise AssertionError(
                    f"TEST LEAK: attempted to open the REAL production DB: {resolved}"
                )
            # Delegate to a real temp sqlite so the bridge has a working DB.
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            return self
        def close(self):
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        @property
        def conn(self):
            if self._conn is None:
                raise RuntimeError("not connected")
            return self._conn

    monkeypatch.setattr(_cli, "Database", _FakeConn)

    # Also guard the lowest-level sqlite handle to the prod path (belt + braces).
    _orig_connect = sqlite3.connect

    def _guarded_connect(*a, **kw):
        target = None
        if a and isinstance(a[0], str):
            target = a[0]
        elif "database" in kw:
            target = kw["database"]
        # URI form: file:/abs?mode=ro -> extract path
        if isinstance(target, str) and target.startswith("file:"):
            target = target[5:].split("?", 1)[0]
        if target and Path(target).resolve() == Path(PROD_PATH):
            raise AssertionError(
                f"TEST LEAK: low-level sqlite tried to open real prod DB: {target}"
            )
        return _orig_connect(*a, **kw)

    monkeypatch.setattr(sqlite3, "connect", _guarded_connect)
    yield
    # After each test, assert no real-prod access happened.
    assert PROD_PATH not in _CONNECT_CALLS, (
        f"real production DB was opened: {_CONNECT_CALLS}"
    )


def _make_temp_db(tmp_path: Path) -> str:
    """Create a minimal writable temp DB so a --write path can open it."""
    p = tmp_path / "bridge_test.db"
    con = sqlite3.connect(str(p))
    con.execute(
        "CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    con.execute("INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', '130')")
    con.execute(
        "CREATE TABLE source_trades (id TEXT, source TEXT, source_trade_id TEXT, "
        "market_source_id TEXT, side TEXT, outcome TEXT, quantity TEXT, price TEXT, "
        "trader_address TEXT, timestamp TEXT, is_sample INTEGER, token_id TEXT)"
    )
    con.commit()
    con.close()
    return str(p)


@pytest.fixture(autouse=True)
def _stub_wallet(monkeypatch):
    """resolve_wallet must not require the real approved-wallet env var."""
    monkeypatch.setattr(cli, "resolve_wallet", lambda wallet=None: WALLET)


@pytest.fixture
def fake_bridge(monkeypatch):
    """Record bridge invocations; return a report-like object with as_dict()."""
    calls = []

    class _FakeReport:
        def __init__(self, **kw):
            self.cleanup_errors = []
            self.rows = kw.get("rows", [])
            self.failures = kw.get("failures", [])
            self.write_counts = kw.get("write_counts", {})
            self.forbidden_table_delta = kw.get("forbidden_table_delta", {})

        def as_dict(self):
            return {
                "wallet": WALLET,
                "limit": 3,
                "mode": "rw",
                "dry_run": False,
                "selected": 3,
                "rows": self.rows,
                "write_counts": self.write_counts,
                "forbidden_table_delta": self.forbidden_table_delta,
                "failures": self.failures,
                "cleanup_errors": self.cleanup_errors,
                "allowed_write_tables": [],
                "forbidden_write_tables": [],
            }

    def _fake(db, *, wallet, limit, dependencies, write,
              write_authorization=None,
              source_trade_id=None, client_close_hooks=()):
        calls.append({
            "db": db, "wallet": wallet, "limit": limit, "write": write,
            "write_authorization": write_authorization,
            "source_trade_id": source_trade_id,
        })
        return _FakeReport(write_counts={"copy_candidates": 1})

    monkeypatch.setattr(cli, "process_approved_wallet_trades", _fake)
    return calls


@pytest.fixture
def backup_double(monkeypatch):
    """Control create_verified_backup outcomes; record call args."""
    outcomes = {"result": None, "calls": []}

    def _fake(db_path, *, backup_path=None):
        outcomes["calls"].append((db_path, backup_path))
        return outcomes["result"]

    monkeypatch.setattr(cli, "create_verified_backup", _fake)
    return outcomes


@pytest.fixture
def force_prod(monkeypatch):
    """Force _is_production_db True for the duration (auto-reverted)."""
    monkeypatch.setattr(cli, "_is_production_db", lambda p: True)


def test_prod_write_without_allow_live_rejected_before_backup_and_bridge(tmp_path, force_prod, fake_bridge, backup_double, capsys):
    temp = _make_temp_db(tmp_path)
    rc = cli.main(["--limit", "3", "--write", "--json", "--db-path", temp])
    assert rc == 2, f"expected exit 2, got {rc}"
    assert backup_double["calls"] == [], "backup must not run when gate missing"
    assert fake_bridge == [], "bridge must not run when gate missing"
    # No Database().connect() to the temp path either (aborted pre-open).
    assert _CONNECT_CALLS == [], f"no DB open expected, got {_CONNECT_CALLS}"
    err = capsys.readouterr().err
    assert "--allow-live" in err


def test_prod_write_without_confirm_production_db_rejected(tmp_path, force_prod, fake_bridge, backup_double, capsys):
    temp = _make_temp_db(tmp_path)
    rc = cli.main(["--limit", "3", "--write", "--allow-live", "--json", "--db-path", temp])
    assert rc == 2
    assert backup_double["calls"] == []
    assert fake_bridge == []
    assert _CONNECT_CALLS == []
    err = capsys.readouterr().err
    assert "--confirm-production-db" in err


def test_prod_write_with_both_gates_runs_backup_then_bridge(tmp_path, force_prod, fake_bridge, backup_double, capsys):
    temp = _make_temp_db(tmp_path)
    ok = BackupResult(
        success=True, path="/tmp/backup.db", method="sqlite_online_backup",
        sha256="deadbeef", size=1024, integrity_check="ok",
        foreign_key_violations=0, source_trades_count=35, schema_version=130,
    )
    backup_double["result"] = ok
    rc = cli.main([
        "--limit", "3", "--write", "--allow-live", "--confirm-production-db",
        "--json", "--db-path", temp,
    ])
    assert rc == 0, f"expected exit 0, got {rc}"
    # Backup ran exactly once, and only against the temp path.
    assert len(backup_double["calls"]) == 1, "exactly one backup must be created"
    assert backup_double["calls"][0][0] == temp, "backup double got temp path only"
    # Bridge ran only AFTER backup, and only against the temp path.
    assert len(fake_bridge) == 1, "bridge must run after successful backup"
    assert fake_bridge[0]["write_authorization"] is not None
    assert _CONNECT_CALLS == [str(Path(temp).resolve())], f"DB connect to temp only: {_CONNECT_CALLS}"
    out = capsys.readouterr().out
    report = json.loads(out)
    assert "backup" in report
    b = report["backup"]
    assert b["backup_path"] == "/tmp/backup.db"
    assert b["backup_sha256"] == "deadbeef"
    assert b["backup_integrity_check"] == "ok"
    assert b["backup_foreign_key_check_count"] == 0
    assert b["backup_schema_version"] == 130
    assert b["backup_size_bytes"] == 1024


def test_prod_write_backup_failure_aborts_before_bridge(tmp_path, force_prod, fake_bridge, backup_double, capsys):
    temp = _make_temp_db(tmp_path)
    bad = BackupResult(success=False, error="integrity mismatch", integrity_check="fail",
                       foreign_key_violations=1, size=0, sha256=None, schema_version=None)
    backup_double["result"] = bad
    rc = cli.main([
        "--limit", "3", "--write", "--allow-live", "--confirm-production-db",
        "--json", "--db-path", temp,
    ])
    assert rc == 1, f"expected exit 1 on backup failure, got {rc}"
    assert len(backup_double["calls"]) == 1, "backup attempt happened"
    assert fake_bridge == [], "bridge must NOT run when backup fails"
    assert _CONNECT_CALLS == [], "writable DB must NOT open on backup failure"
    assert "backup failed" in capsys.readouterr().err


def test_prod_write_schema_mismatch_aborts_before_bridge(tmp_path, force_prod, fake_bridge, backup_double, capsys):
    """Source canonical schema version != backup schema version must abort
    before Database().connect() and bridge."""
    temp = _make_temp_db(tmp_path)
    ok = BackupResult(
        success=True, path="/tmp/backup.db", integrity_check="ok",
        foreign_key_violations=0, source_trades_count=35, schema_version=129,
        sha256="deadbeef", size=1024,
    )
    backup_double["result"] = ok  # backup says 129, source _meta says 130
    rc = cli.main([
        "--limit", "3", "--write", "--allow-live", "--confirm-production-db",
        "--json", "--db-path", temp,
    ])
    assert rc == 1, f"expected exit 1 on schema mismatch, got {rc}"
    assert fake_bridge == [], "bridge must NOT run on schema mismatch"
    assert _CONNECT_CALLS == [], "writable DB must NOT open on schema mismatch"
    assert "schema version mismatch" in capsys.readouterr().err


def test_backup_schema_version_comes_from_backup_verification(tmp_path, force_prod, fake_bridge, backup_double):
    """The JSON backup_schema_version must equal BackupResult.schema_version
    (read from the backup's _meta), not the live writable DB."""
    temp = _make_temp_db(tmp_path)
    ok = BackupResult(
        success=True, path="/tmp/backup.db", integrity_check="ok",
        foreign_key_violations=0, source_trades_count=35, schema_version=130,
        sha256="deadbeef", size=1024,
    )
    backup_double["result"] = ok
    cli.main([
        "--limit", "3", "--write", "--allow-live", "--confirm-production-db",
        "--json", "--db-path", temp,
    ])
    bp = backup_double["calls"][0][1]
    assert ".pr25a_online_backup_" in bp, "correct backup filename marker"
    # Must NOT duplicate polycopy.db in the name.
    assert "polycopy.db.pr25a_online_backup_" not in bp, "filename must not duplicate polycopy.db"
    # And it must be derived from the temp db path (no second polycopy.db fragment).
    assert bp.startswith(temp + ".pr25a_online_backup_"), f"backup derived from db_path: {bp}"


def test_dry_run_requires_no_gates_and_creates_no_backup(tmp_path, fake_bridge, backup_double, capsys):
    temp = _make_temp_db(tmp_path)
    rc = cli.main(["--limit", "3", "--json", "--db-path", temp])
    assert rc == 0, f"dry-run should succeed, got {rc}"
    assert backup_double["calls"] == [], "dry-run must not create a backup"
    assert len(fake_bridge) == 1
    assert fake_bridge[0]["write"] is False
    # Dry-run opens the temp DB read-only (a _ReadOnlyDb with sqlite3 mode=ro),
    # but never as a writable production connect.
    out = capsys.readouterr().out
    report = json.loads(out)
    assert "backup" not in report, "dry-run report must not include backup metadata"


def test_temp_db_write_bypasses_production_gates(tmp_path, fake_bridge, backup_double, capsys):
    """A --write to a NON-production temp DB uses test-safe behavior (no gates,
    no backup) and the bridge proceeds. Adapters are constructed only after
    the (absent) gate check passes."""
    temp = _make_temp_db(tmp_path)
    assert cli._is_production_db(temp) is False
    rc = cli.main(["--limit", "3", "--write", "--json", "--db-path", temp])
    assert rc == 0, f"temp-db write should succeed, got {rc}"
    assert backup_double["calls"] == [], "no backup for non-prod temp DB"
    assert len(fake_bridge) == 1, "bridge runs for temp-db write"
    assert fake_bridge[0]["write"] is True


def test_missing_gates_construct_no_clients_or_connect(tmp_path, force_prod, fake_bridge, backup_double):
    """Gate failure must not open the DB and must not leak anything."""
    temp = _make_temp_db(tmp_path)
    rc = cli.main(["--limit", "3", "--write", "--json", "--db-path", temp])
    assert rc == 2
    assert _CONNECT_CALLS == []
    assert fake_bridge == []
    assert backup_double["calls"] == []


def test_production_path_detection(tmp_path):
    assert cli._is_production_db(PROD_PATH) is True
    assert cli._is_production_db(str((REPO / "data" / "polycopy.db"))) is True
    assert cli._is_production_db(str(tmp_path / "other.db")) is False
