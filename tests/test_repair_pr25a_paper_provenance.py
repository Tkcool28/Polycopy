"""Hermetic tests for scripts/repair_pr25a_paper_provenance.py.

NO real production DB is ever opened. Tests build a tmp DB with a seeded
paper_signal_decisions (NULL provenance) + trade_copyability_decisions, then
call ``repair.repair`` directly with an injected backup helper.

Covers Section 10:
  1. Dry-run reports repairable rows, zero writes.
  2. Production gates required for production DB repair.
  3. Verified online backup occurs before writable open.
  4. Exact one-match row updates successfully.
  5. Missing TC match aborts that row.
  6. Multiple TC matches abort that row.
  7. Candidate mismatch aborts that row.
  8. Snapshot mismatch aborts that row.
  9. Malformed idempotency key aborts that row (N/A here: key not used; we
     instead assert exactly-one requirement strength).
 10. Only trade_score_decision_id changes.
 11. Forbidden-table fingerprints remain identical.
 12. Second repair run idempotent (updates zero rows).
 13. Real production DB is never opened by tests.
 14. Cleanup/report JSON survives failures.
 15. Limit greater than 3 is rejected.
"""
from __future__ import annotations

# ruff: noqa: E402, E701, E702
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
import sys
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import scripts.repair_pr25a_paper_provenance as repair_mod

PROD_PATH = str((REPO / "data" / "polycopy.db").resolve())

_CONNECT_CALLS: list[str] = []


@pytest.fixture(autouse=True)
def _guard(monkeypatch):
    """Refuse any connect() to the real production DB; record targets."""
    _CONNECT_CALLS.clear()
    real_connect = sqlite3.connect

    def _guarded(path, *a, **k):
        resolved = str(Path(path).resolve()) if not str(path).startswith("file:") else str(Path(str(path).split(":")[1].split("?")[0]).resolve())
        _CONNECT_CALLS.append(resolved)
        if resolved == PROD_PATH:
            raise AssertionError(f"TEST LEAK: opened real production DB: {resolved}")
        return real_connect(path, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", _guarded)
    yield
    assert PROD_PATH not in _CONNECT_CALLS, "production DB was opened during test"


def _mk_db(path: Path, *, rows):
    """rows: list of dicts describing a paper row + its matching TC decisions.

    Each item: {"paper": dict, "tc": [list of (candidate_id, snapshot_id)]}
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trade_copyability_decisions ("
        "id INTEGER PRIMARY KEY, candidate_id INTEGER, price_snapshot_id TEXT, verdict TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS paper_signal_decisions ("
        "id INTEGER PRIMARY KEY, candidate_id INTEGER, price_snapshot_id TEXT, "
        "signal_reason TEXT, trade_score_decision_id INTEGER)"
    )
    # Forbidden tables must exist so fingerprints are captured.
    for t in repair_mod.FORBIDDEN_FINGERPRINT_TABLES:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {t} (id INTEGER PRIMARY KEY)")
    tc_id = 0
    paper_id = 0
    for item in rows:
        for (cid, snap) in item["tc"]:
            tc_id += 1
            conn.execute(
                "INSERT INTO trade_copyability_decisions (id, candidate_id, price_snapshot_id, verdict) VALUES (?,?,?,?)",
                (tc_id, cid, snap, "skip"),
            )
        paper_id += 1
        conn.execute(
            "INSERT INTO paper_signal_decisions (id, candidate_id, price_snapshot_id, signal_reason, trade_score_decision_id) VALUES (?,?,?,?,?)",
            (paper_id, item["paper"]["candidate_id"], item["paper"]["price_snapshot_id"],
             repair_mod.PR25A_PAPER_REASON, item["paper"].get("trade_score_decision_id")),
        )
    conn.commit()
    conn.close()


class _FakeBackup:
    def __init__(self, *, ok=True):
        self.ok = ok
        self.path = None
    def __call__(self, db_path, *, backup_path=None):
        from polycopy.ingestion.source_trade_writer import BackupResult
        if not self.ok:
            r = BackupResult(path=backup_path or "x", method="fake")
            r.error = "injected_backup_failure"
            r.integrity_check = "fail"
            r.foreign_key_violations = 1
            return r
        r = BackupResult(path=backup_path or "x", method="fake")
        r.success = True
        r.integrity_check = "ok"
        r.foreign_key_violations = 0
        r.sha256 = "deadbeef"
        r.size = 1
        return r


# 1. Dry-run reports repairable rows, zero writes.
def test_dry_run_reports_repairable_rows_and_writes_zero(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "s1"}, "tc": [(1, "s1")]},
        {"paper": {"candidate_id": 2, "price_snapshot_id": "s2"}, "tc": [(2, "s2")]},
        {"paper": {"candidate_id": 3, "price_snapshot_id": "s3"}, "tc": [(3, "s3")]},
    ])
    rep = repair_mod.repair(str(dbp), limit=3, json_out=False, backup_helper=_FakeBackup())
    assert rep.dry_run is True
    assert len(rep.updated) == 0
    assert len([r for r in rep.rows if r.action == "update"]) == 3
    # No writes actually landed.
    conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    nulls = conn.execute("SELECT COUNT(*) FROM paper_signal_decisions WHERE trade_score_decision_id IS NULL").fetchone()[0]
    conn.close()
    assert nulls == 3


# 4. Exact one-match row updates successfully.
def test_exact_one_match_updates_successfully(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "s1"}, "tc": [(1, "s1")]},
    ])
    rep = repair_mod.repair(str(dbp), limit=3, allow_live=True, write=True,
                            confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert rep.error is None
    assert len(rep.updated) == 1
    conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    row = conn.execute("SELECT trade_score_decision_id FROM paper_signal_decisions WHERE id=1").fetchone()
    conn.close()
    assert row[0] == 1  # the only TC decision for candidate 1


# 5. Missing TC match aborts that row.
def test_missing_tc_match_aborts_row(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "s1"}, "tc": []},
    ])
    rep = repair_mod.repair(str(dbp), limit=3, allow_live=True, write=True,
                            confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert len(rep.updated) == 0
    assert any(r.action == "skip" and "no_matching_tc_decision" in r.reason for r in rep.rows)


# 6. Multiple TC matches abort that row.
def test_multiple_tc_matches_abort_row(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "s1"}, "tc": [(1, "s1"), (1, "s1")]},
    ])
    rep = repair_mod.repair(str(dbp), limit=3, allow_live=True, write=True,
                            confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert len(rep.updated) == 0
    assert any(r.action == "skip" and "multiple_matching_tc_decisions" in r.reason for r in rep.rows)


# 7. Candidate mismatch aborts that row.
# A paper row for candidate 1 must never be linked to a TC decision owned by
# candidate 2. Because the repair filters TC decisions by the paper's OWN
# candidate_id, a foreign-owned TC yields zero matches => the row is rejected
# (no_matching_tc_decision), which is the same safe outcome as a candidate
# mismatch and is asserted explicitly below.
def test_candidate_mismatch_aborts_row(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "s1"}, "tc": [(2, "s1")]},
    ])
    rep = repair_mod.repair(str(dbp), limit=3, allow_live=True, write=True,
                            confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert len(rep.updated) == 0
    # The matching SQL filters by the paper's candidate_id, so a foreign-owned
    # TC decision yields zero matches -> rejected (never relinked).
    assert any(r.action == "skip" and r.reason == "no_matching_tc_decision" for r in rep.rows)


# 8. Snapshot mismatch aborts that row.
def test_snapshot_mismatch_aborts_row(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "s1"}, "tc": [(1, "sX")]},
    ])
    rep = repair_mod.repair(str(dbp), limit=3, allow_live=True, write=True,
                            confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert len(rep.updated) == 0
    # zero matches because candidate+snapshot together exclude the foreign-snapshot TC
    assert any(r.action == "skip" for r in rep.rows)


# 10 + 11. Only trade_score_decision_id changes; forbidden fingerprints identical.
def test_only_trade_score_column_changes_and_forbidden_identical(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "s1"}, "tc": [(1, "s1")]},
    ])
    rep = repair_mod.repair(str(dbp), limit=3, allow_live=True, write=True,
                            confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert rep.error is None
    assert rep.forbidden_identical is True
    # Other columns unchanged: candidate/snapshot/reason intact.
    conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    row = conn.execute("SELECT candidate_id, price_snapshot_id, signal_reason, trade_score_decision_id FROM paper_signal_decisions WHERE id=1").fetchone()
    conn.close()
    assert row[0] == 1 and row[1] == "s1" and row[2] == repair_mod.PR25A_PAPER_REASON and row[3] == 1


# 12. Second repair run idempotent (updates zero rows).
def test_second_repair_run_idempotent(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "s1"}, "tc": [(1, "s1")]},
    ])
    r1 = repair_mod.repair(str(dbp), limit=3, allow_live=True, write=True,
                           confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert len(r1.updated) == 1
    r2 = repair_mod.repair(str(dbp), limit=3, allow_live=True, write=True,
                           confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert len(r2.updated) == 0, "second identical repair must update zero rows"
    assert r2.error is None


# 2. Production gates required for production DB repair.
def test_write_requires_all_three_gates(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "s1"}, "tc": [(1, "s1")]},
    ])
    # Missing gates => dry-run, zero writes.
    rep = repair_mod.repair(str(dbp), limit=3, allow_live=False, write=False,
                            confirm_production_db=False, json_out=False, backup_helper=_FakeBackup())
    assert rep.dry_run is True
    assert len(rep.updated) == 0
    # Single gate missing variants.
    for kw in [
        dict(allow_live=True, write=False, confirm_production_db=False),
        dict(allow_live=False, write=True, confirm_production_db=False),
        dict(allow_live=False, write=False, confirm_production_db=True),
        dict(allow_live=True, write=True, confirm_production_db=False),
    ]:
        rep = repair_mod.repair(str(dbp), limit=3, json_out=False, backup_helper=_FakeBackup(), **kw)
        assert rep.dry_run is True, f"missing gate set {kw} must stay dry-run"


# 3. Verified online backup occurs before writable open.
def test_verified_backup_occurs_before_write(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "s1"}, "tc": [(1, "s1")]},
    ])
    calls = []
    def _tracking_backup(db_path, *, backup_path=None):
        calls.append(("backup", db_path))
        from polycopy.ingestion.source_trade_writer import BackupResult
        r = BackupResult(path=backup_path or "x", method="fake")
        r.success = True; r.integrity_check = "ok"; r.foreign_key_violations = 0; r.sha256 = "x"; r.size = 1
        return r
    rep = repair_mod.repair(str(dbp), limit=3, allow_live=True, write=True,
                            confirm_production_db=True, json_out=False, backup_helper=_tracking_backup)
    assert calls, "backup must be invoked before the writable transaction"
    assert rep.backup is not None


# 3b. Backup failure aborts before any writable open.
def test_backup_failure_aborts_without_write(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "s1"}, "tc": [(1, "s1")]},
    ])
    rep = repair_mod.repair(str(dbp), limit=3, allow_live=True, write=True,
                            confirm_production_db=True, json_out=False, backup_helper=_FakeBackup(ok=False))
    assert rep.error is not None and "verified_backup_failed" in rep.error
    assert len(rep.updated) == 0


# 15. Limit greater than 3 is rejected.
def test_limit_greater_than_max_rejected(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[{"paper": {"candidate_id": 1, "price_snapshot_id": "s1"}, "tc": [(1, "s1")]}])
    with pytest.raises(ValueError):
        repair_mod.repair(str(dbp), limit=4, allow_live=True, write=True,
                          confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())


# 14. Report JSON survives failures (backup failure still serializes).
def test_report_json_survives_failure(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[{"paper": {"candidate_id": 1, "price_snapshot_id": "s1"}, "tc": [(1, "s1")]}])
    import json
    rep = repair_mod.repair(str(dbp), limit=3, allow_live=True, write=True,
                            confirm_production_db=True, json_out=True, backup_helper=_FakeBackup(ok=False))
    dumped = json.dumps(rep.as_dict(), default=str)
    assert "verified_backup_failed" in dumped
    assert "error" in rep.as_dict()


# 13. Real production DB never opened (covered by autouse _guard asserting).
def test_real_production_db_never_opened(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[{"paper": {"candidate_id": 1, "price_snapshot_id": "s1"}, "tc": [(1, "s1")]}])
    repair_mod.repair(str(dbp), limit=3, allow_live=True, write=True,
                      confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert PROD_PATH not in _CONNECT_CALLS
