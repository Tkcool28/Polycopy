# ruff: noqa: E402
"""PR25A production-write gates + verified online backup tests.

All tests are hermetic: they drive ``scripts/process_approved_wallet_trades.main``
in-process against a TEMP database path, with the bridge call and the online
backup helper monkeypatched. The real production DB (data/polycopy.db) is
NEVER opened, backed up, or modified by these tests.
"""
from __future__ import annotations

import json
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


def _make_temp_db(tmp_path: Path) -> str:
    """Create a minimal writable temp DB so a --write path can open it."""
    p = tmp_path / "bridge_test.db"
    import sqlite3

    con = sqlite3.connect(str(p))
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
    """Control create_verified_backup outcomes."""
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


def test_prod_write_without_allow_live_rejected_before_backup_and_bridge(force_prod, fake_bridge, backup_double, capsys):
    rc = cli.main(["--limit", "3", "--write", "--json", "--db-path", PROD_PATH])
    assert rc == 2, f"expected exit 2, got {rc}"
    assert backup_double["calls"] == [], "backup must not run when gate missing"
    assert fake_bridge == [], "bridge must not run when gate missing"
    err = capsys.readouterr().err
    assert "--allow-live" in err


def test_prod_write_without_confirm_production_db_rejected(force_prod, fake_bridge, backup_double, capsys):
    rc = cli.main(["--limit", "3", "--write", "--allow-live", "--json", "--db-path", PROD_PATH])
    assert rc == 2
    assert backup_double["calls"] == []
    assert fake_bridge == []
    err = capsys.readouterr().err
    assert "--confirm-production-db" in err


def test_prod_write_with_both_gates_runs_backup_then_bridge(force_prod, fake_bridge, backup_double, capsys):
    ok = BackupResult(
        success=True, path="/tmp/backup.db", method="sqlite_online_backup",
        sha256="deadbeef", size=1024, integrity_check="ok",
        foreign_key_violations=0, source_trades_count=35,
    )
    backup_double["result"] = ok
    rc = cli.main([
        "--limit", "3", "--write", "--allow-live", "--confirm-production-db",
        "--json", "--db-path", PROD_PATH,
    ])
    assert rc == 0, f"expected exit 0, got {rc}"
    assert len(backup_double["calls"]) == 1, "exactly one backup must be created"
    assert len(fake_bridge) == 1, "bridge must run after successful backup"
    assert fake_bridge[0]["write_authorization"] is not None
    out = capsys.readouterr().out
    report = json.loads(out)
    assert "backup" in report
    b = report["backup"]
    assert b["backup_path"] == "/tmp/backup.db"
    assert b["backup_sha256"] == "deadbeef"
    assert b["backup_integrity_check"] == "ok"
    assert b["backup_foreign_key_check_count"] == 0
    assert b["backup_size_bytes"] == 1024


def test_prod_write_backup_failure_aborts_before_bridge(force_prod, fake_bridge, backup_double, capsys):
    bad = BackupResult(success=False, error="integrity mismatch", integrity_check="fail",
                       foreign_key_violations=1, size=0, sha256=None)
    backup_double["result"] = bad
    rc = cli.main([
        "--limit", "3", "--write", "--allow-live", "--confirm-production-db",
        "--json", "--db-path", PROD_PATH,
    ])
    assert rc == 1, f"expected exit 1 on backup failure, got {rc}"
    assert len(backup_double["calls"]) == 1, "backup attempt happened"
    assert fake_bridge == [], "bridge must NOT run when backup fails"
    assert "backup failed" in capsys.readouterr().err


def test_dry_run_requires_no_gates_and_creates_no_backup(tmp_path, fake_bridge, backup_double, capsys):
    temp = _make_temp_db(tmp_path)
    rc = cli.main(["--limit", "3", "--json", "--db-path", temp])
    assert rc == 0, f"dry-run should succeed, got {rc}"
    assert backup_double["calls"] == [], "dry-run must not create a backup"
    assert len(fake_bridge) == 1
    assert fake_bridge[0]["write"] is False
    out = capsys.readouterr().out
    report = json.loads(out)
    assert "backup" not in report, "dry-run report must not include backup metadata"


def test_temp_db_write_bypasses_production_gates(tmp_path, fake_bridge, backup_double, capsys):
    """A --write to a NON-production temp DB uses test-safe behavior (no gates,
    no backup) and the bridge proceeds."""
    temp = _make_temp_db(tmp_path)
    assert cli._is_production_db(temp) is False
    rc = cli.main(["--limit", "3", "--write", "--json", "--db-path", temp])
    assert rc == 0, f"temp-db write should succeed, got {rc}"
    assert backup_double["calls"] == [], "no backup for non-prod temp DB"
    assert len(fake_bridge) == 1, "bridge runs for temp-db write"
    assert fake_bridge[0]["write"] is True


def test_production_path_detection(tmp_path):
    assert cli._is_production_db(PROD_PATH) is True
    assert cli._is_production_db(str((REPO / "data" / "polycopy.db"))) is True
    assert cli._is_production_db(str(tmp_path / "other.db")) is False
    assert cli._is_production_db(str(REPO / "data" / "polycopy.db")) is True
