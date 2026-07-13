"""Hermetic tests for scripts/repair_pr25a_paper_provenance.py.

NO real production DB is ever opened. Tests build a tmp DB with a seeded
paper_signal_decisions (NULL provenance) + trade_copyability_decisions, then
call ``repair.repair`` directly with an injected backup helper.

Covers Sections 10 + the post-audit corrections:
  * dry-run zero writes
  * production gates required (all three + exact canonical path)
  * NO silent --write downgrade (incomplete gates -> nonzero, no backup/open)
  * exact resolved-path production detection (no substring heuristic)
  * limit bounds 1..3 (-1/0/4 rejected)
  * all-or-nothing batch (mixed valid/invalid -> 0 updates, rollback)
  * narrow UPDATE + rowcount enforcement
  * per-row before/after column proof (only trade_score_decision_id changes)
  * post-repair integrity_check / foreign_key_check verification
  * backup ordering + backup failure
  * second-run idempotency
  * real production DB hard guard
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

_PROD_PATH = str((REPO / "data" / "polycopy.db").resolve())

_CONNECT_CALLS: list[str] = []


@pytest.fixture(autouse=True)
def _guard(monkeypatch):
    """Refuse any connect() to the real production DB; record targets."""
    _CONNECT_CALLS.clear()
    real_connect = sqlite3.connect

    def _guarded(path, *a, **k):
        resolved = (
            str(Path(str(path).split(":")[1].split("?")[0]).resolve())
            if str(path).startswith("file:") else str(Path(path).resolve())
        )
        _CONNECT_CALLS.append(resolved)
        if resolved == _PROD_PATH:
            raise AssertionError(f"TEST LEAK: opened real production DB: {resolved}")
        return real_connect(path, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", _guarded)
    yield
    assert _PROD_PATH not in _CONNECT_CALLS, "production DB was opened during test"


# Real production paper_signal_decisions columns are discovered dynamically by
# the repair utility (PRAGMA table_info). We build the test DB through the
# canonical Database migration path so the fixture schema is IDENTICAL to
# production -- no synthetic/simplified table that could drift again.


def _mk_db(path: Path, *, rows):
    """Build a tmp DB using the canonical production migrations, then seed rows.

    ``rows`` is a list of dicts:
        {"paper": {real paper columns}, "tc": [(candidate_id, snapshot_id), ...]}
    Only ``candidate_id`` + ``price_snapshot_id`` are required in each paper dict;
    the remaining real columns default to safe bridge-incomplete values.

    Parent rows (wallets, copy_candidates, candidate_price_snapshots,
    trade_copyability_decisions) are seeded so foreign-key constraints hold. To
    stay drift-proof, required NOT NULL columns are discovered from the live
    schema via PRAGMA table_info and filled with safe dummy values -- the
    fixture schema is IDENTICAL to production (built via Database migrations).
    """
    from polycopy.db.database import Database

    # Create the real, migrated schema (identical to production).
    db = Database(path)
    db.connect()
    db.close()

    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")

    def _dummy_for(col_type: str):
        t = (col_type or "TEXT").upper()
        if "INT" in t:
            return 0
        if "REAL" in t or "FLOA" in t or "DOUB" in t:
            return 0.0
        return "x"

    def _seed(table: str, provided: dict):
        """Insert a row into ``table`` supplying required NOT NULL columns."""
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        cols, vals = [], []
        for (_, name, ctype, notnull, default, _) in info:
            if name == "id" and name not in provided:
                continue  # caller supplies id separately if needed
            if name in provided:
                cols.append(name); vals.append(provided[name]); continue
            if notnull and default is None:
                cols.append(name); vals.append(_dummy_for(ctype))
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(cols)
        conn.execute(
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", vals)

    def _ensure_candidate(cid, wid):
        key = ("cand", cid, wid)
        if key in seen:
            return
        _seed("copy_candidates",
              {"id": cid, "wallet_id": wid, "source_trade_id": f"st-{cid}",
               "created_at": T, "updated_at": T})
        seen[key] = True

    def _ensure_snapshot(snap, cid):
        key = ("snap", snap, cid)
        if key in seen:
            return
        _seed("candidate_price_snapshots",
              {"id": snap, "candidate_id": cid, "snapshot_run_id": snap,
               "source_trade_timestamp": T, "fetched_at": T, "created_at": T})
        seen[key] = True

    tc_id = 0
    paper_id = 0
    seen = {}
    T = "2026-01-01T00:00:00Z"
    for item in rows:
        cid = item["paper"].get("candidate_id")
        snap = item["paper"].get("price_snapshot_id")
        wid = item["paper"].get("wallet_id", "w")
        key = ("wallet", wid)
        if key not in seen:
            _seed("wallets", {"id": wid}); seen[key] = True
        key = ("cand", cid, wid)
        if key not in seen:
            _seed("copy_candidates",
                  {"id": cid, "wallet_id": wid,
                   "source_trade_id": f"st-{cid}", "created_at": T,
                   "updated_at": T}); seen[key] = True
        key = ("snap", snap, cid)
        if key not in seen:
            _seed("candidate_price_snapshots",
                  {"id": snap, "candidate_id": cid, "snapshot_run_id": snap,
                   "source_trade_timestamp": T, "fetched_at": T,
                   "created_at": T}); seen[key] = True
        for (tc_cid, tc_snap) in item["tc"]:
            tc_id += 1
            _ensure_candidate(tc_cid, wid)
            _ensure_snapshot(tc_snap, tc_cid)
            _seed("trade_copyability_decisions",
                  {"id": tc_id, "candidate_id": tc_cid, "wallet_id": wid,
                   "price_snapshot_id": tc_snap, "verdict": "skip",
                   "source_trade_id": f"tcst-{tc_id}"})
        paper_id += 1
        p = item["paper"]
        conn.execute(
            """
            INSERT INTO paper_signal_decisions (
                id, candidate_id, wallet_id, signal_family, signal_reason,
                wallet_score, trade_score, shadow_score, shadow_verdict,
                final_verdict, is_approved, source_data_timestamp,
                source_trade_id, price_snapshot_id, idempotency_key,
                decision_input_json, computed_at, created_at,
                trade_score_decision_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                paper_id,
                p.get("candidate_id"),
                wid,
                p.get("signal_family", "incomplete"),
                repair_mod.PR25A_PAPER_REASON,
                p.get("wallet_score", 0.0),
                p.get("trade_score", 0.0),
                p.get("shadow_score", 0.0),
                p.get("shadow_verdict"),
                p.get("final_verdict", "incomplete"),
                p.get("is_approved", 0),
                p.get("source_data_timestamp", T),
                p.get("source_trade_id", f"st-{paper_id}"),
                p.get("price_snapshot_id"),
                p.get("idempotency_key", f"idem-{paper_id}"),
                p.get("decision_input_json"),
                p.get("computed_at", T),
                p.get("created_at", T),
                p.get("trade_score_decision_id"),
            ),
        )
    conn.commit()
    conn.close()


class _FakeBackup:
    def __init__(self, *, ok=True):
        self.ok = ok

    def __call__(self, db_path, *, backup_path=None):
        from polycopy.ingestion.source_trade_writer import BackupResult
        r = BackupResult(path=backup_path or "x", method="fake")
        if not self.ok:
            r.error = "injected_backup_failure"
            r.integrity_check = "fail"
            r.foreign_key_violations = 1
            return r
        r.success = True
        r.integrity_check = "ok"
        r.foreign_key_violations = 0
        r.sha256 = "deadbeef"
        r.size = 1
        return r


# --------------------------------------------------------------------------
# 1. Dry-run reports repairable rows, zero writes.
# --------------------------------------------------------------------------
def test_dry_run_reports_repairable_rows_and_writes_zero(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"}, "tc": [(1, "S1")]},
        {"paper": {"candidate_id": 2, "price_snapshot_id": "S2"}, "tc": [(2, "S2")]},
        {"paper": {"candidate_id": 3, "price_snapshot_id": "S3"}, "tc": [(3, "S3")]},
    ])
    rep, code = repair_mod.repair(str(dbp), limit=3, json_out=False,
                                  backup_helper=_FakeBackup())
    assert code == 0
    assert rep.dry_run is True
    assert len(rep.updated) == 0
    # Zero writes: NULL columns remain NULL.
    live = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    nulls = live.execute(
        "SELECT COUNT(*) FROM paper_signal_decisions "
        "WHERE trade_score_decision_id IS NULL").fetchone()[0]
    live.close()
    assert nulls == 3


# --------------------------------------------------------------------------
# 2-4. Production gates required + no silent downgrade.
# --------------------------------------------------------------------------
def test_exact_one_match_updates_successfully(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"}, "tc": [(1, "S1")]},
    ])
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 0
    assert rep.committed is True
    assert len(rep.updated) == 1
    assert rep.updated[0]["trade_score_decision_id"] == 1


# Every incomplete --write gate combination must exit nonzero BEFORE backup/open.
@pytest.mark.parametrize("kw", [
    {"write": True},
    {"write": True, "allow_live": True},
    {"write": True, "confirm_production_db": True},
    {"write": True, "allow_live": True, "confirm_production_db": True,
     "prod_override": False},
])
def test_write_with_incomplete_gates_exits_nonzero(tmp_path, monkeypatch, kw):
    # prod_override=False => keep real _is_production_db (tmp path -> not prod).
    if kw.pop("prod_override", True):
        monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"}, "tc": [(1, "S1")]},
    ])
    rep, code = repair_mod.repair(str(dbp), limit=3, json_out=False,
                                  backup_helper=_FakeBackup(), **kw)
    assert code == 2, f"expected nonzero exit for incomplete gates {kw}"
    assert rep.gates_complete is False
    assert rep.error is not None
    assert rep.backup is None, "no backup must be created on incomplete gates"
    assert len(rep.updated) == 0
    # No writable connection opened for a tmp (non-prod) path when gates missing.
    assert _PROD_PATH not in _CONNECT_CALLS


def test_backup_occurs_before_writable_open(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"}, "tc": [(1, "S1")]},
    ])
    calls = []

    def _tracked_backup(db_path, *, backup_path=None):
        calls.append("backup")
        return _FakeBackup()(db_path, backup_path=backup_path)

    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_tracked_backup)
    assert code == 0
    assert calls == ["backup"]
    assert rep.backup is not None


def test_backup_failure_aborts_without_write(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"}, "tc": [(1, "S1")]},
    ])
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup(ok=False))
    assert code == 1
    assert rep.committed is False
    assert len(rep.updated) == 0


# --------------------------------------------------------------------------
# 5-8. Reject rows: missing / multiple / candidate / snapshot mismatch.
# --------------------------------------------------------------------------
def test_missing_tc_match_aborts_row(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"}, "tc": []},
    ])
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    # All-or-nothing: 1 selected, 0 valid -> rollback/zero updates.
    assert code == 1
    assert rep.status == "batch_validation_failed"
    assert len(rep.updated) == 0


def test_multiple_tc_matches_abort_row(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"},
         "tc": [(1, "S1"), (1, "S1")]},
    ])
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 1
    assert len(rep.updated) == 0


def test_candidate_mismatch_aborts_row(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    # Paper for candidate 1; only TC for candidate 2 -> zero matches for cand 1.
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"},
         "tc": [(2, "S2")]},
    ])
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 1
    assert len(rep.updated) == 0


def test_snapshot_mismatch_aborts_row(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    # Paper cand 1 / S1; only TC cand 1 / S2 -> zero matches for (1, S1).
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"},
         "tc": [(1, "S2")]},
    ])
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 1
    assert len(rep.updated) == 0


# --------------------------------------------------------------------------
# 10. Only trade_score_decision_id changes (per-row before/after proof).
# --------------------------------------------------------------------------
def test_only_trade_score_column_changes_and_forbidden_identical(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1",
                   "final_verdict": "incomplete", "trade_score": 0.5,
                   "idempotency_key": "idem-1", "decision_input_json": "orig"},
         "tc": [(1, "S1")]},
    ])
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 0
    assert len(rep.proofs) == 1
    assert rep.proofs[0]["changed_columns"] == ["trade_score_decision_id"]
    assert rep.changed_columns_valid is True
    assert rep.forbidden_identical is True
    # Other real columns untouched in the live DB.
    live = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    row = live.execute(
        "SELECT final_verdict, signal_reason, trade_score, decision_input_json "
        "FROM paper_signal_decisions WHERE id=1").fetchone()
    live.close()
    assert row == ("incomplete", repair_mod.PR25A_PAPER_REASON, 0.5, "orig")


# --------------------------------------------------------------------------
# 12. Second repair run idempotent (updates zero rows).
# --------------------------------------------------------------------------
def test_second_repair_run_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"}, "tc": [(1, "S1")]},
    ])
    r1, _ = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert len(r1.updated) == 1
    r2, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 0
    assert len(r2.updated) == 0, "second identical repair must update zero rows"
    assert r2.committed is True  # transaction still commits (nothing to do)


# --------------------------------------------------------------------------
# 15. Limit greater than 3 rejected.
# --------------------------------------------------------------------------
def test_limit_greater_than_max_rejected(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[])
    rep, code = repair_mod.repair(str(dbp), limit=4, json_out=False,
                                  backup_helper=_FakeBackup())
    assert code == 2
    assert rep.status == "invalid_limit"


# --------------------------------------------------------------------------
# 14. Cleanup/report JSON survives failures.
# --------------------------------------------------------------------------
def test_report_json_survives_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"}, "tc": []},
    ])
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 1
    assert rep.error is not None
    assert rep.finished_at is not None
    d = rep.as_dict()
    assert "error" in d and "status" in d


# --------------------------------------------------------------------------
# 13. Real production DB hard guard (autouse fixture handles it).
# --------------------------------------------------------------------------
def test_real_production_db_never_opened(tmp_path, monkeypatch):
    # The canonical production path is an exact resolved-path equality.
    # Make the repo-local data/polycopy.db the "production" target so the test
    # is location-independent (the VPS path /root/Polycopy does not exist on
    # clean runners). Assert the guard key resolves to production and that no
    # connection to it is ever opened by this test.
    canonical = Path(__file__).resolve().parents[1] / "data" / "polycopy.db"
    monkeypatch.setattr(repair_mod, "CANONICAL_PRODUCTION_DB", canonical.resolve())
    assert repair_mod._is_production_db(str(canonical)) is True
    assert str(canonical.resolve()) not in _CONNECT_CALLS


# ==========================================================================
# POST-AUDIT CORRECTIONS
# ==========================================================================

# --- Section 3: exact resolved-path production detection -------------------
def test_production_path_detection_exact_equality(monkeypatch):
    # Location-independent: point CANONICAL_PRODUCTION_DB at a temp file and
    # verify exact resolved-path equality (no substring/suffix heuristic) for
    # the canonical path, a relative path resolving to it, and a symlink to it.
    # Look-alike paths must NOT be classified as production.
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        canon = Path(td) / "data" / "polycopy.db"
        canon.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            repair_mod, "CANONICAL_PRODUCTION_DB", canon.resolve())
        # Exact canonical path -> production.
        assert repair_mod._is_production_db(str(canon)) is True
        # Relative path resolving to canonical (chdir into the temp dir).
        cwd = os.getcwd()
        try:
            os.chdir(str(canon.parent))
            assert repair_mod._is_production_db("polycopy.db") is True
        finally:
            os.chdir(cwd)
        # Symlink resolving to canonical.
        link = Path(td) / "polycopy_link.db"
        link.symlink_to(canon)
        assert repair_mod._is_production_db(str(link)) is True
        # Look-alikes must NOT be classified as production.
        assert repair_mod._is_production_db(
            str(Path(td) / "other" / "polycopy.db")) is False
        assert repair_mod._is_production_db(
            str(Path(td) / "data" / "polycopy.db.bak")) is False
        assert repair_mod._is_production_db(
            str(Path(td) / "otherdir" / "polycopy.db")) is False


# --- Section 4: limit bounds ----------------------------------------------
@pytest.mark.parametrize("lim,accepted", [
    (-1, False), (0, False), (1, True), (2, True), (3, True), (4, False),
])
def test_limit_bounds(tmp_path, lim, accepted):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[])
    rep, code = repair_mod.repair(str(dbp), limit=lim, json_out=False,
                                  backup_helper=_FakeBackup())
    if accepted:
        assert code == 0
        assert rep.status != "invalid_limit"
    else:
        assert code == 2
        assert rep.status == "invalid_limit"


def test_limit_negative_never_reaches_sqlite(tmp_path):
    # Prove the negative limit is rejected at argument validation, so SQLite
    # never sees LIMIT -1 (which would mean "no limit" / unbounded).
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"}, "tc": [(1, "S1")]},
    ])
    rep, code = repair_mod.repair(str(dbp), limit=-1, json_out=False,
                                  backup_helper=_FakeBackup())
    assert code == 2
    assert rep.status == "invalid_limit"
    # No candidate scan / backup / writable open occurred.
    assert len(rep.rows) == 0
    assert rep.backup is None


def test_limit_malformed_cli_rejected():
    with pytest.raises(SystemExit):
        repair_mod.main(["--limit", "abc", "--json"])


# --- Section 5: all-or-nothing mixed batches ------------------------------
def _mk_three(dbp, *, third="valid"):
    base = [
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"}, "tc": [(1, "S1")]},
        {"paper": {"candidate_id": 2, "price_snapshot_id": "S2"}, "tc": [(2, "S2")]},
    ]
    if third == "valid":
        base.append({"paper": {"candidate_id": 3, "price_snapshot_id": "S3"},
                     "tc": [(3, "S3")]})
    elif third == "missing":
        base.append({"paper": {"candidate_id": 3, "price_snapshot_id": "S3"},
                     "tc": []})
    elif third == "ambiguous":
        base.append({"paper": {"candidate_id": 3, "price_snapshot_id": "S3"},
                     "tc": [(3, "S3"), (3, "S3")]})
    elif third == "candidate_mismatch":
        base.append({"paper": {"candidate_id": 3, "price_snapshot_id": "S3"},
                     "tc": [(9, "S9")]})
    elif third == "snapshot_mismatch":
        base.append({"paper": {"candidate_id": 3, "price_snapshot_id": "S3"},
                     "tc": [(3, "OTHER")]})
    _mk_db(dbp, rows=base)


@pytest.mark.parametrize("third,desc", [
    ("missing", "2 valid + 1 missing TC"),
    ("ambiguous", "2 valid + 1 ambiguous TC"),
    ("candidate_mismatch", "2 valid + 1 candidate mismatch"),
    ("snapshot_mismatch", "2 valid + 1 snapshot mismatch"),
])
def test_all_or_nothing_mixed_batch_zero_updates(tmp_path, monkeypatch, third, desc):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_three(dbp, third=third)
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 1, f"mixed batch ({desc}) must abort nonzero"
    assert rep.status == "batch_validation_failed"
    assert len(rep.updated) == 0, f"mixed batch ({desc}) must commit zero updates"
    # Live DB: all three still NULL.
    live = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    nulls = live.execute(
        "SELECT COUNT(*) FROM paper_signal_decisions "
        "WHERE trade_score_decision_id IS NULL").fetchone()[0]
    live.close()
    assert nulls == 3


def test_three_valid_rows_all_updated(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_three(dbp, third="valid")
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 0
    assert len(rep.updated) == 3
    assert rep.updated[0]["trade_score_decision_id"] == 1
    assert rep.updated[1]["trade_score_decision_id"] == 2
    assert rep.updated[2]["trade_score_decision_id"] == 3


def test_mid_transaction_exception_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_three(dbp, third="valid")
    # Corrupt the AFTER proof for the 2nd updated row so changed_columns check
    # fails -> raises -> rollback of the entire batch.
    orig_read = repair_mod._read_paper_row
    calls = {"n": 0}

    def _corrupt_after(conn, paper_id, cols):
        row = orig_read(conn, paper_id, cols)
        calls["n"] += 1
        if calls["n"] >= 4:  # 2nd row's AFTER read
            row = dict(row)
            row["final_verdict"] = "TAMPERED"
        return row

    monkeypatch.setattr(repair_mod, "_read_paper_row", _corrupt_after)
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 1
    assert rep.rolled_back is True
    assert rep.committed is False
    assert len(rep.updated) == 0
    # Live DB: all three still NULL (fully rolled back).
    live = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    nulls = live.execute(
        "SELECT COUNT(*) FROM paper_signal_decisions "
        "WHERE trade_score_decision_id IS NULL").fetchone()[0]
    live.close()
    assert nulls == 3


# --- Section 6/10: rowcount enforcement + before/after proof --------------
def test_rowcount_mismatch_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"}, "tc": [(1, "S1")]},
    ])
    # Simulate a concurrent modification: the pre-update read reports a
    # candidate_id that no longer matches the live row, so the narrow UPDATE
    # WHERE (candidate_id = <tampered>) hits 0 rows -> rowcount guard -> rollback.
    orig_read = repair_mod._read_paper_row
    state = {"before": True}

    def _tamper(conn, paper_id, cols):
        row = orig_read(conn, paper_id, cols)
        if state["before"]:
            state["before"] = False
            row = dict(row)
            row["candidate_id"] = 999  # stale/corrupt pre-read
        return row

    monkeypatch.setattr(repair_mod, "_read_paper_row", _tamper)
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 1
    assert rep.rolled_back is True
    assert len(rep.updated) == 0


def test_second_paper_column_change_fails_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"}, "tc": [(1, "S1")]},
    ])
    orig_read = repair_mod._read_paper_row
    calls = {"n": 0}

    def _tamper_after(conn, paper_id, cols):
        row = orig_read(conn, paper_id, cols)
        calls["n"] += 1
        if calls["n"] >= 2:  # AFTER read
            row = dict(row)
            row["decision_input_json"] = "CHANGED"
        return row

    monkeypatch.setattr(repair_mod, "_read_paper_row", _tamper_after)
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 1
    assert rep.changed_columns_valid is False or rep.rolled_back is True
    assert len(rep.updated) == 0


# --- Section 8: post-repair integrity/FK verification ----------------------
def test_post_repair_integrity_reported_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_three(dbp, third="valid")
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 0
    assert rep.integrity_check == "ok"
    assert rep.foreign_key_check_count == 0


def test_post_repair_integrity_failure_returns_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_three(dbp, third="valid")

    real_connect = sqlite3.connect

    class _Wrap:
        def __init__(self, path, *a, **k):
            object.__setattr__(self, "_c", real_connect(path, *a, **k))
            self._c.row_factory = sqlite3.Row
        def __getattr__(self, name):
            return getattr(self._c, name)
        def __setattr__(self, name, val):
            if name == "_c":
                object.__setattr__(self, name, val)
            else:
                setattr(self._c, name, val)
        def execute(self, sql, *args):
            if str(sql).strip().upper().startswith("PRAGMA INTEGRITY_CHECK"):
                fake = [("not ok",)]
                return type("_C", (), {"fetchall": lambda self: fake})()
            return self._c.execute(sql, *args)

    monkeypatch.setattr(repair_mod, "_ro_reopen", lambda p: _Wrap(p))
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 1
    assert rep.status == "post_repair_verification_failed"
    assert rep.integrity_check != "ok"
    # Committed but flagged; recovery via backup path.
    assert rep.backup is not None


def test_post_repair_fk_failure_returns_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_three(dbp, third="valid")

    real_connect = sqlite3.connect

    class _Wrap:
        def __init__(self, path, *a, **k):
            object.__setattr__(self, "_c", real_connect(path, *a, **k))
            self._c.row_factory = sqlite3.Row
        def __getattr__(self, name):
            return getattr(self._c, name)
        def __setattr__(self, name, val):
            if name == "_c":
                object.__setattr__(self, name, val)
            else:
                setattr(self._c, name, val)
        def execute(self, sql, *args):
            if str(sql).strip().upper().startswith("PRAGMA FOREIGN_KEY_CHECK"):
                fake = [("paper_signal_decisions", "trade_score_decision_id", "1", "1")]
                return type("_C", (), {"fetchall": lambda self: fake})()
            return self._c.execute(sql, *args)

    monkeypatch.setattr(repair_mod, "_ro_reopen", lambda p: _Wrap(p))
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 1
    assert rep.foreign_key_check_count != 0


# ==========================================================================
# SCHEMA-CONTRACT TESTS (close the real-schema drift class of defect)
# ==========================================================================
def test_discovers_real_paper_columns(tmp_path):
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"}, "tc": [(1, "S1")]},
    ])
    conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    cols = repair_mod._discover_paper_columns(conn)
    conn.close()
    real = {
        "id", "candidate_id", "wallet_id", "signal_family", "signal_reason",
        "wallet_score", "trade_score", "shadow_score", "shadow_verdict",
        "final_verdict", "is_approved", "source_data_timestamp",
        "source_trade_id", "price_snapshot_id", "idempotency_key",
        "computed_at", "created_at", "decision_input_json",
        "wallet_score_decision_id", "category_score_decision_id",
        "trade_score_decision_id",
    }
    assert set(cols) == real
    for bad in ("verdict", "score", "score_inputs", "metadata", "payload", "notes"):
        assert bad not in cols
    for req in ("id", "candidate_id", "price_snapshot_id", "trade_score_decision_id"):
        assert req in cols


def test_missing_required_column_fails_closed(tmp_path):
    dbp = tmp_path / "repair.db"
    conn = sqlite3.connect(str(dbp))
    conn.execute(
        "CREATE TABLE paper_signal_decisions ("
        "id INTEGER PRIMARY KEY, candidate_id INTEGER)")
    conn.commit(); conn.close()
    c2 = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.Error):
            repair_mod._discover_paper_columns(c2)
    finally:
        c2.close()


def test_full_row_proof_detects_any_extra_change(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_db(dbp, rows=[
        {"paper": {"candidate_id": 1, "price_snapshot_id": "S1"}, "tc": [(1, "S1")]},
    ])
    orig_read = repair_mod._read_paper_row
    calls = {"n": 0}

    def _flip(conn, paper_id, cols):
        row = orig_read(conn, paper_id, cols)
        calls["n"] += 1
        if calls["n"] >= 2:  # AFTER read
            row = dict(row)
            row["wallet_score"] = 99.0  # an unrelated real column changed
        return row

    monkeypatch.setattr(repair_mod, "_read_paper_row", _flip)
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 1
    assert rep.rolled_back is True
    assert len(rep.updated) == 0


def test_real_schema_end_to_end_update(tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "_is_production_db", lambda p: True)
    dbp = tmp_path / "repair.db"
    _mk_three(dbp, third="valid")
    rep, code = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code == 0
    assert rep.selected_count == 3
    assert rep.validated_count == 3
    assert rep.updated_count == 3
    assert rep.committed is True
    assert rep.changed_columns_valid is True
    for p in rep.proofs:
        assert p["changed_columns"] == ["trade_score_decision_id"]
    live = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    rows = live.execute(
        "SELECT id, trade_score_decision_id, final_verdict, trade_score, "
        "decision_input_json FROM paper_signal_decisions ORDER BY id").fetchall()
    live.close()
    assert rows[0] == (1, 1, "incomplete", 0.0, None)
    assert rows[1] == (2, 2, "incomplete", 0.0, None)
    assert rows[2] == (3, 3, "incomplete", 0.0, None)
    r2, code2 = repair_mod.repair(
        str(dbp), limit=3, allow_live=True, write=True,
        confirm_production_db=True, json_out=False, backup_helper=_FakeBackup())
    assert code2 == 0
    assert len(r2.updated) == 0
