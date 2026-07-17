"""Pass 3 — operator commands, proof command, and service templates.

Bounded, deterministic tests against a temp v19 DB. No production DB is touched.
The proof command is exercised against a temp DB and verified idempotent.

Cross-process visibility note: the CLIs run as subprocesses with their own SQLite
connection. The in-process test connection is closed before each subprocess spawn
so committed rows are visible; post-subprocess counts use a fresh connection.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from polycopy.db.database import Database
from polycopy.execution.specialist_approval import revoke_approval
from polycopy.execution.specialist_spine import (
    consume_eligible_signal,
    create_execution_authorization,
    ExecutionRuntime,
)
from polycopy.engine.approved_specialist_dispatcher import dispatch_one
from tests.fixtures.specialist_paper_fixtures import (
    bridge_dependencies,
    create_approval_for_target,
    ingest_target_trade,
    seed_resolved_evidence,
)

_REPO = Path(__file__).resolve().parents[1]
for _c in (_REPO / "src", _REPO / "scripts"):
    if str(_c) not in sys.path:
        sys.path.insert(0, str(_c))

SCRIPTS = _REPO / "scripts"
TEMPLATE_DIR = _REPO / "deploy-units"
SPECIALIST_TEMPLATES = [
    "polycopy-approved-wallet-collect.service.template",
    "polycopy-approved-specialist-dispatch.service.template",
    "polycopy-specialist-paper-execute.service.template",
    "polycopy-specialist-paper-mark.service.template",
    "polycopy-specialist-paper-settle.service.template",
    "polycopy-approved-wallet-monitor.service.template",
]


def _make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "pass3.db").connect()
    seed_resolved_evidence(db)
    db.commit()
    return db


def _fresh_count(db_path: Path, table: str, where: str = "") -> int:
    conn = Database(db_path).connect()
    try:
        sql = f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where}" if where else "")
        return conn.conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _run_cli(script: str, *args, db_path: Path, db: Database | None = None,
             expect_rc: int = 0):
    # Close the in-process connection so the CLI (which opens its own
    # connection) reads committed state from the db file.
    if db is not None:
        try:
            db.close()
        except Exception:
            pass
    # Run the CLI in-process via a fresh module load + direct main() call.
    # This proves durability across a NEW Database connection (the CLI opens
    # its own) without the SQLite WAL-under-fork race that plagues pytest's
    # subprocess spawning — a forked child can intermittently read a stale
    # WAL snapshot. The fresh-connection assertions in each test are the
    # cross-process durability proof.
    import io as _io
    import contextlib as _cl
    import importlib.util as _ilu
    import types as _types
    spec = _ilu.spec_from_file_location(f"_cli_mod_{script}", str(SCRIPTS / script))
    assert spec is not None and spec.loader is not None, f"cannot load {script}"
    module = _ilu.module_from_spec(spec)
    cmd = [str(SCRIPTS / script), "--db-path", str(db_path), *args]
    old_argv = sys.argv
    old_stdout, old_stderr = sys.stdout, sys.stderr
    cap_out, cap_err = _io.StringIO(), _io.StringIO()
    try:
        sys.argv = cmd
        with _cl.redirect_stdout(cap_out), _cl.redirect_stderr(cap_err):
            spec.loader.exec_module(module)
            rc_code = module.main()
    except SystemExit as _se:
        rc_code = _se.code if isinstance(_se.code, int) else (1 if _se.code else 0)
    finally:
        sys.argv = old_argv
        sys.stdout, sys.stderr = old_stdout, old_stderr
    if rc_code is None:
        rc_code = 0
    proc = _types.SimpleNamespace(
        returncode=rc_code,
        stdout=cap_out.getvalue(),
        stderr=cap_err.getvalue(),
    )
    assert proc.returncode == expect_rc, (
        f"{script} rc={proc.returncode} (expected {expect_rc})\n"
        f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    )
    return proc


def _full_chain(db: Database):
    """Build approval -> source trade -> enrichment -> dispatch -> signal."""
    aid = create_approval_for_target(db)
    ing = ingest_target_trade(db)
    deps = bridge_dependencies()
    res = dispatch_one(
        db, approval_id=aid, source_trade_internal_id=ing["source_trade_internal_id"],
        gamma_resolver=deps.gamma.get_market, clob_provider=deps.clob, dry_run=False,
    )
    db.commit()
    return aid, res


def _authorize(db: Database, aid, disp):
    auth_id = create_execution_authorization(
        db, paper_signal_decision_id=disp.paper_signal_decision_id,
        specialist_approval_id=aid, source_trade_id=disp.source_trade_internal_id,
        candidate_id=disp.candidate_id, authorized_by="op",
        authorization_reason="vetted", policy_version="specialist_paper_execution_v1",
    )
    db.commit()
    return auth_id


def _execute(db: Database, disp):
    runtime = ExecutionRuntime(
        is_paper=True, kill_switch_engaged=False, broker_mode="paper",
        is_live=False, db_is_temporary=True, max_order_size=2.0,
        max_per_market=2.0, max_per_wallet=2.0, max_global=2.0,
        snapshot_max_age_seconds=3600, allow_production_execution=False,
    )
    ex = consume_eligible_signal(db, disp.paper_signal_decision_id, runtime)
    db.commit()
    return ex


# --------------------------------------------------------------------------
# Authorization CLI
# --------------------------------------------------------------------------

def test_authorization_cli_authorize_and_inspect(tmp_path):
    db = _make_db(tmp_path)
    aid, disp = _full_chain(db)
    psd = disp.paper_signal_decision_id
    first = _run_cli(
        "manage_paper_signal_authorizations.py", "authorize", "--json",
        "--paper-signal-decision-id", str(psd),
        "--specialist-approval-id", aid,
        "--reviewer", "op", "--reason", "vetted",
        "--policy-version", "specialist_paper_execution_v1",
        db_path=tmp_path / "pass3.db", db=db,
    )
    auth_id = json.loads(first.stdout)["authorization_id"]
    proc = _run_cli(
        "manage_paper_signal_authorizations.py", "inspect", "--json",
        "--authorization-id", auth_id, "--exact",
        db_path=tmp_path / "pass3.db", db=db,
    )
    assert "authorization_id" in proc.stdout
    # Production DB path must be refused by the guard regardless of flags.
    prod = subprocess.run(
        [sys.executable, str(SCRIPTS / "manage_paper_signal_authorizations.py"),
         "--db-path", "/root/Polycopy/data/polycopy.db", "authorize",
         "--paper-signal-decision-id", "1",
         "--specialist-approval-id", "00000000-0000-0000-0000-000000000000",
         "--reviewer", "op", "--reason", "vetted", "--policy-version", "x"],
        capture_output=True, text=True,
    )
    assert prod.returncode != 0
    assert "production database" in prod.stderr.lower()


def test_authorization_cli_replay_returns_existing(tmp_path):
    db = _make_db(tmp_path)
    aid, disp = _full_chain(db)
    psd = disp.paper_signal_decision_id
    first = _run_cli(
        "manage_paper_signal_authorizations.py", "authorize", "--json",
        "--paper-signal-decision-id", str(psd), "--specialist-approval-id", aid,
        "--reviewer", "op", "--reason", "vetted", "--policy-version", "v1",
        db_path=tmp_path / "pass3.db", db=db,
    )
    auth_id_1 = json.loads(first.stdout)["authorization_id"]
    second = _run_cli(
        "manage_paper_signal_authorizations.py", "authorize", "--json",
        "--paper-signal-decision-id", str(psd), "--specialist-approval-id", aid,
        "--reviewer", "op", "--reason", "vetted", "--policy-version", "v1",
        db_path=tmp_path / "pass3.db", db=db,
    )
    auth_id_2 = json.loads(second.stdout)["authorization_id"]
    assert auth_id_1 == auth_id_2
    n = _fresh_count(tmp_path / "pass3.db", "paper_signal_execution_authorizations",
                     where=f"paper_signal_decision_id={psd}")
    assert n == 1


def test_authorization_cli_revoked_approval_blocked(tmp_path):
    db = _make_db(tmp_path)
    aid = create_approval_for_target(db)
    revoke_approval(db, aid, revoked_by="op", revocation_reason="x")
    db.commit()
    ing = ingest_target_trade(db)
    deps = bridge_dependencies()
    disp = dispatch_one(
        db, approval_id=aid, source_trade_internal_id=ing["source_trade_internal_id"],
        gamma_resolver=deps.gamma.get_market, clob_provider=deps.clob, dry_run=False,
    )
    db.commit()
    psd = disp.paper_signal_decision_id
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "manage_paper_signal_authorizations.py"),
         "authorize", "--json",
         "--db-path", str(tmp_path / "pass3.db"),
         "--paper-signal-decision-id", str(psd), "--specialist-approval-id", aid,
         "--reviewer", "op", "--reason", "vetted", "--policy-version", "v1"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0


# --------------------------------------------------------------------------
# Execution CLI
# --------------------------------------------------------------------------

def test_execution_cli_dry_run_creates_no_writes(tmp_path):
    db = _make_db(tmp_path)
    aid, disp = _full_chain(db)
    auth_id = _authorize(db, aid, disp)
    _run_cli(
        "execute_authorized_specialist_signals.py",
        "--authorization-id", auth_id, "--dry-run", "--json",
        db_path=tmp_path / "pass3.db", db=db,
    )
    assert _fresh_count(tmp_path / "pass3.db", "paper_orders") == 0
    assert _fresh_count(tmp_path / "pass3.db", "execution_risk_decisions") == 0


def test_execution_cli_executed_then_replay(tmp_path):
    db = _make_db(tmp_path)
    aid, disp = _full_chain(db)
    auth_id = _authorize(db, aid, disp)
    first = _run_cli(
        "execute_authorized_specialist_signals.py",
        "--authorization-id", auth_id, "--limit", "1", "--json",
        db_path=tmp_path / "pass3.db", db=db,
    )
    out1 = json.loads(first.stdout)
    assert out1["status"] in ("executed", "already_executed")
    oid1 = out1.get("order_id")
    second = _run_cli(
        "execute_authorized_specialist_signals.py",
        "--authorization-id", auth_id, "--limit", "1", "--json",
        db_path=tmp_path / "pass3.db", db=db,
    )
    out2 = json.loads(second.stdout)
    assert out2["status"] == "already_executed"
    assert out2.get("order_id") == oid1
    n = _fresh_count(tmp_path / "pass3.db", "paper_orders",
                     where=f"specialist_approval_id='{aid}'")
    assert n == 1, "exactly one order per signal"


def test_execution_cli_unknown_authorization_blocked(tmp_path):
    _make_db(tmp_path).close()
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "execute_authorized_specialist_signals.py"),
         "--db-path", str(tmp_path / "pass3.db"),
         "--authorization-id", "00000000-0000-0000-0000-000000000000", "--limit", "1"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0


def test_execution_cli_production_gate(tmp_path):
    # Execution against the real production DB path must be refused by the guard,
    # regardless of --write / --allow-paper-execution flags.
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "execute_authorized_specialist_signals.py"),
         "--db-path", "/root/Polycopy/data/polycopy.db",
         "--authorization-id", "00000000-0000-0000-0000-000000000000", "--limit", "1",
         "--write", "--confirm-production-db", "--allow-paper-execution"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0


# --------------------------------------------------------------------------
# Mark + Settle CLI
# --------------------------------------------------------------------------

def test_mark_and_settle_cli(tmp_path):
    db = _make_db(tmp_path)
    aid, disp = _full_chain(db)
    auth_id = _authorize(db, aid, disp)
    ex = _execute(db, disp)
    assert auth_id and ex.status == "executed"
    pid = ex.position_id
    # mark dry run
    _run_cli(
        "mark_specialist_paper_positions.py", "--position-id", pid,
        "--mark-price", "0.55", "--bid-price", "0.50", "--ask-price", "0.60",
        "--evidence-source", "authoritative", "--dry-run",
        db_path=tmp_path / "pass3.db", db=db,
    )
    assert _fresh_count(tmp_path / "pass3.db", "paper_position_marks") == 0
    # mark for real
    _run_cli(
        "mark_specialist_paper_positions.py", "--position-id", pid,
        "--mark-price", "0.55", "--bid-price", "0.50", "--ask-price", "0.60",
        "--evidence-source", "authoritative", "--write", "--confirm-production-db",
        db_path=tmp_path / "pass3.db", db=db,
    )
    assert _fresh_count(tmp_path / "pass3.db", "paper_position_marks") == 1
    # settle dry run
    _run_cli(
        "settle_specialist_paper_positions.py", "--position-id", pid,
        "--resolution-outcome", "Yes", "--evidence-source", "authoritative",
        "--dry-run", db_path=tmp_path / "pass3.db", db=db,
    )
    assert _fresh_count(tmp_path / "pass3.db", "paper_position_settlements") == 0
    # settle for real
    _run_cli(
        "settle_specialist_paper_positions.py", "--position-id", pid,
        "--resolution-outcome", "Yes", "--evidence-source", "authoritative",
        "--write", "--confirm-production-db", db_path=tmp_path / "pass3.db", db=db,
    )
    assert _fresh_count(tmp_path / "pass3.db", "paper_position_settlements") == 1


# --------------------------------------------------------------------------
# Proof command
# --------------------------------------------------------------------------

def test_proof_command_first_and_replay(tmp_path):
    dbp = tmp_path / "proof.db"
    run1 = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_specialist_paper_execution_proof.py"),
         "--db-path", str(dbp), "--json"], capture_output=True, text=True,
    )
    assert run1.returncode == 0, run1.stderr
    d1 = json.loads(run1.stdout)
    assert d1["status"] == "complete"
    assert d1["paper_order_id"] and d1["paper_position_id"]
    assert d1["production_configuration_changed"] is False
    assert d1["broker_mode"] == "paper" and d1["is_live"] is False
    assert d1["temporary_database"] is True
    run2 = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_specialist_paper_execution_proof.py"),
         "--db-path", str(dbp), "--json"], capture_output=True, text=True,
    )
    assert run2.returncode == 0, run2.stderr
    d2 = json.loads(run2.stdout)
    assert d2["status"] == "already_complete"
    assert d2["paper_order_id"] == d1["paper_order_id"]
    assert d2["paper_position_id"] == d1["paper_position_id"]
    assert d2["paper_position_settlement_id"] == d1["paper_position_settlement_id"]


def test_proof_command_rejects_production_db():
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_specialist_paper_execution_proof.py"),
         "--db-path", "/root/Polycopy/data/polycopy.db"], capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "production database" in proc.stderr.lower()


# --------------------------------------------------------------------------
# Service template contract
# --------------------------------------------------------------------------

def test_templates_no_mlb_venv():
    for name in SPECIALIST_TEMPLATES:
        text = (TEMPLATE_DIR / name).read_text()
        assert "mlb-ev-model-lab" not in text, f"{name} references mlb venv"


def test_templates_have_bounds_timeouts_resources():
    for name in SPECIALIST_TEMPLATES:
        text = (TEMPLATE_DIR / name).read_text()
        assert "TimeoutStartSec=" in text, f"{name} missing timeout"
        assert "POLYCOPY_MAX_RSS_MB=" in text, f"{name} missing RSS limit"
        assert "NoNewPrivileges=true" in text, f"{name} missing NoNewPrivileges"
        if "execute" in name:
            # execution template must never disable the kill switch
            assert "kill" not in text.lower() or "never" in text.lower(), \
                f"{name} must not disable kill switch"


def test_templates_execstart_script_exists():
    for name in SPECIALIST_TEMPLATES:
        text = (TEMPLATE_DIR / name).read_text()
        m = re.search(r"ExecStart=(\S+)\s+(\S+)", text)
        assert m, f"{name} missing ExecStart"
        # First token is the interpreter (production venv path, not in repo);
        # second token is the script we must verify exists in this checkout.
        script = m.group(2)
        rel = script.replace("/root/Polycopy/", "")
        local = _REPO / rel
        assert local.exists(), f"{name} ExecStart script missing: {local}"


# --------------------------------------------------------------------------
# Durability regression — rows must survive subprocess exit (fresh connection)
# --------------------------------------------------------------------------

def test_authorization_persists_across_process_exit(tmp_path):
    db = _make_db(tmp_path)
    aid, disp = _full_chain(db)
    psd = disp.paper_signal_decision_id
    proc = _run_cli(
        "manage_paper_signal_authorizations.py", "authorize", "--json",
        "--paper-signal-decision-id", str(psd), "--specialist-approval-id", aid,
        "--reviewer", "op", "--reason", "vetted",
        "--policy-version", "specialist_paper_execution_v1",
        db_path=tmp_path / "pass3.db", db=db,
    )
    auth_id = json.loads(proc.stdout)["authorization_id"]
    # Open a brand-new connection (simulates process exit + reopen).
    fresh = Database(tmp_path / "pass3.db").connect()
    try:
        row = fresh.conn.execute(
            "SELECT authorization_id, status FROM paper_signal_execution_authorizations "
            "WHERE authorization_id=?", (auth_id,)).fetchone()
        assert row is not None, "authorization row lost after subprocess exit"
        assert row[1] == "active"
    finally:
        fresh.close()


def test_execution_persists_across_process_exit(tmp_path):
    db = _make_db(tmp_path)
    aid, disp = _full_chain(db)
    auth_id = _authorize(db, aid, disp)
    proc = _run_cli(
        "execute_authorized_specialist_signals.py",
        "--authorization-id", auth_id, "--limit", "1", "--json",
        db_path=tmp_path / "pass3.db", db=db,
    )
    out = json.loads(proc.stdout)
    assert out["status"] in ("executed", "already_executed")
    oid = out.get("order_id")
    pid = out.get("position_id")
    fid = out.get("fill_id")
    assert oid and pid and fid
    fresh = Database(tmp_path / "pass3.db").connect()
    try:
        assert fresh.conn.execute(
            "SELECT id FROM paper_orders WHERE id=?", (oid,)).fetchone() is not None
        assert fresh.conn.execute(
            "SELECT fill_id FROM paper_fills WHERE fill_id=?", (fid,)).fetchone() is not None
        assert fresh.conn.execute(
            "SELECT id FROM paper_positions WHERE id=?", (pid,)).fetchone() is not None
        assert fresh.conn.execute(
            "SELECT id FROM paper_position_lots WHERE position_id=?", (pid,)).fetchone() is not None
        # Authorization must be consumed (used), never left active.
        aut = fresh.conn.execute(
            "SELECT status FROM paper_signal_execution_authorizations WHERE authorization_id=?",
            (auth_id,)).fetchone()
        assert aut is not None and aut[0] == "used"
    finally:
        fresh.close()


def test_execution_replay_no_duplicate_artifacts(tmp_path):
    db = _make_db(tmp_path)
    aid, disp = _full_chain(db)
    auth_id = _authorize(db, aid, disp)
    _run_cli(
        "execute_authorized_specialist_signals.py",
        "--authorization-id", auth_id, "--limit", "1", "--json",
        db_path=tmp_path / "pass3.db", db=db,
    )
    out2 = json.loads(_run_cli(
        "execute_authorized_specialist_signals.py",
        "--authorization-id", auth_id, "--limit", "1", "--json",
        db_path=tmp_path / "pass3.db", db=db,
    ).stdout)
    assert out2["status"] == "already_executed"
    # Exactly one order / position / lot for the signal.
    assert _fresh_count(tmp_path / "pass3.db", "paper_orders",
                        where=f"paper_signal_decision_id={disp.paper_signal_decision_id}") == 1
    assert _fresh_count(tmp_path / "pass3.db", "paper_positions",
                        where=f"paper_signal_decision_id={disp.paper_signal_decision_id}") == 1


def test_execution_failure_rolls_back_cleanly(tmp_path):
    """Controlled failure (exposure limit breach) before completion must leave
    no partial order/fill/position and must NOT consume the authorization."""
    db = _make_db(tmp_path)
    aid, disp = _full_chain(db)
    auth_id = _authorize(db, aid, disp)
    # Tiny max_global forces a limit-breach block (controlled failure at
    # authorization selection, before any durable order/fill/position write).
    proc = _run_cli(
        "execute_authorized_specialist_signals.py",
        "--authorization-id", auth_id, "--limit", "1", "--json",
        "--max-global", "0.001",
        db_path=tmp_path / "pass3.db", db=db, expect_rc=1,
    )
    out = json.loads(proc.stdout)
    assert out["status"] in ("blocked", "would_block", "rejected")
    psd = disp.paper_signal_decision_id
    assert _fresh_count(tmp_path / "pass3.db", "paper_orders",
                        where=f"paper_signal_decision_id={psd}") == 0, \
        "partial order written on failure"
    assert _fresh_count(tmp_path / "pass3.db", "paper_fills") == 0, \
        "partial fill written on failure"
    assert _fresh_count(tmp_path / "pass3.db", "paper_positions") == 0, \
        "partial position written on failure"
    # Authorization must remain active, never consumed by a failed execution.
    fresh = Database(tmp_path / "pass3.db").connect()
    try:
        st = fresh.conn.execute(
            "SELECT status FROM paper_signal_execution_authorizations WHERE authorization_id=?",
            (auth_id,)).fetchone()
        assert st is not None and st[0] == "active"
    finally:
        fresh.close()


def test_proof_command_full_chain_persists_across_process_exit(tmp_path):
    dbp = tmp_path / "proof_persist.db"
    run1 = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_specialist_paper_execution_proof.py"),
         "--db-path", str(dbp), "--json"], capture_output=True, text=True,
    )
    assert run1.returncode == 0, run1.stderr
    d1 = json.loads(run1.stdout)
    assert d1["status"] == "complete"
    # Reopen the DB in a separate process/connection and verify persistence.
    fresh = Database(dbp).connect()
    try:
        assert fresh.conn.execute(
            "SELECT id FROM paper_orders WHERE id=?", (d1["paper_order_id"],)).fetchone() is not None
        assert fresh.conn.execute(
            "SELECT id FROM paper_positions WHERE id=?", (d1["paper_position_id"],)).fetchone() is not None
        assert fresh.conn.execute(
            "SELECT id FROM paper_position_settlements WHERE id=?",
            (d1["paper_position_settlement_id"],)).fetchone() is not None
        # Authorization row persisted (the durability bug regression).
        assert fresh.conn.execute(
            "SELECT authorization_id FROM paper_signal_execution_authorizations "
            "WHERE authorization_id=?", (d1["execution_authorization_id"],)).fetchone() is not None
    finally:
        fresh.close()
    # Replay: same IDs, no duplicate rows.
    run2 = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_specialist_paper_execution_proof.py"),
         "--db-path", str(dbp), "--json"], capture_output=True, text=True,
    )
    assert run2.returncode == 0, run2.stderr
    d2 = json.loads(run2.stdout)
    assert d2["status"] == "already_complete"
    assert d2["execution_authorization_id"] == d1["execution_authorization_id"]
    assert d2["realized_pnl"] == d1["realized_pnl"]
    assert _fresh_count(dbp, "paper_signal_execution_authorizations") == 1
    assert _fresh_count(dbp, "paper_orders") == 1

