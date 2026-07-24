"""T7 (evaluate/rescore) + T8 (status) tests for PR #71 S6.

Temp/scratch DBs only. Never opens production.

T7: drive scripts/evaluate_specialist_evidence_watchlist.py (frozen
scorer, reused unchanged). Assert mode/selector semantics, atomic
outer-transaction rollback, honest decision reporting, supported-category
authority, forbidden-artifact delta observation, and idempotent replay.

T8: drive scripts/specialist_evidence_status.py (read-only, current evidence).
Assert GREEN requires CURRENT wallet + CURRENT supported-category
copy_candidate, stale historical decisions cannot create GREEN, taxonomy /
resolution conflicts are RED, staleness policy, deterministic best-category
selection, cohort filtering, and read-only purity (zero write SQL).

E2E: canonical watched wallet -> dry-run score -> write -> replay -> read-only
status, with fingerprint agreement and zero research-to-execution artifacts.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.canonical_metadata import build_canonical_metadata  # noqa: E402
from polycopy.scoring.wallet_evidence import (  # noqa: E402
    resolve_wallet_score_v1,
)
from evidence_db import DbConn, FORBIDDEN_EXECUTION_TABLES, open_readonly  # noqa: E402


_LAST_SPY = None  # holds the most recent instrumented DbConn for §7 assertions


def _load(name):
    s = importlib.util.spec_from_file_location(name, ROOT / "scripts" / name)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def _tmp():
    raise RuntimeError("_tmp is provided by the module-owned SQLite fixture")


@pytest.fixture(autouse=True)
def _owned_sqlite_paths(monkeypatch, owned_sqlite):
    """Route this module's disposable SQLite files through pytest ownership."""
    monkeypatch.setitem(globals(), "_tmp", owned_sqlite.new_path)


def _open():
    p = _tmp()
    return Database(p).connect(), p


def _seed_wallet(db, wid, address, is_sample=0):
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (wid, address, "t", is_sample, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()


def _seed_watch(db, wid, wallet_id, status="active", last_collection_at=None):
    db.conn.execute(
        "INSERT INTO specialist_evidence_watchlist("
        "id, wallet_id, status, source, reason, created_by, created_at, "
        "last_collection_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (wid, wallet_id, status, "manual", "seed", "t",
         "2026-01-01T00:00:00Z", last_collection_at),
    )
    db.conn.commit()


GCOND = "0x" + "c" * 64
GTOK = "0x" + "c" * 64
GAMMA = {
    "conditionId": GCOND, "tokenId": GTOK, "category": "Politics",
    "tags": ["election"], "events": [{"id": "e1", "slug": "us"}],
    "series": [], "question": "Q", "slug": "us",
    "outcomes": ["Yes", "No"], "outcomePrices": ["0.4", "0.6"],
}


def _seed_trade(db, tid, cond=GCOND, meta=None, side="BUY",
                resolution_status=None, is_winning_trade=None,
                realized_pnl=None, timestamp=None, trader=None):
    if meta is None:
        meta = build_canonical_metadata({}, GAMMA)
    if trader is None:
        trader = "0xeval00000000000000000000000000000abc"
    db.conn.execute(
        "INSERT INTO source_trades("
        "id, source, source_trade_id, market_source_id, side, outcome, "
        "quantity, price, trader_address, timestamp, is_sample, "
        "metadata_json, resolution_status, is_winning_trade, realized_pnl) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, "polymarket", tid, cond, side, "Yes", 10.0, 0.40,
         trader, timestamp or "2026-02-01T00:00:00Z", 0,
         json.dumps(meta, sort_keys=True),
         resolution_status, is_winning_trade, realized_pnl),
    )
    db.conn.commit()


def _recent_ts(hours_ago=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _seed_green_evidence(db, wid, address, *, n=120, winrate=0.8,
                         ndays=30, nev=25, cond=GCOND, prefix="green"):
    """Seed canonical BUY evidence that scores copy_candidate on both wallet
    and its supported 'politics' category under the FROZEN scorer."""
    import random
    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    rng = random.Random(7)
    for i in range(n):
        day = base + timedelta(days=i % ndays)
        ev = i % nev
        won = rng.random() < winrate
        tid = f"{prefix}{i}"
        db.conn.execute(
            "INSERT INTO source_trades("
            "id, source, source_trade_id, market_source_id, side, outcome, "
            "quantity, price, trader_address, timestamp, is_sample, "
            "metadata_json, resolution_status, is_winning_trade, realized_pnl) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "polymarket", tid, f"m{i}", "BUY", "Yes", 10.0, 0.4,
             address, day.isoformat(), 0,
             json.dumps({"taxonomy": {"raw_category": "Politics"},
                         "event": {"id": f"ev{ev}", "slug": f"ev{ev}"}},
                        sort_keys=True),
             "won" if won else "lost", 1 if won else 0,
             9.0 if won else -1.0),
        )
    db.conn.commit()


WID = "uuid-eval"
ADDR = "0xeval00000000000000000000000000000abc"
WRITE = ["--write", "--allow-live", "--confirm-production-db"]


# ── T7: evaluate / rescore ────────────────────────────────────────────────────

def test_evaluate_dry_run_default_no_write_succeeds():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_trade(db, "polymarket:st1")
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--wallet-id", WID])
    assert rc == 0, rc
    rc2 = ev.main(["--db-path", str(db.db_path), "--wallet-id", WID, "--dry-run"])
    assert rc2 == 0, rc2
    db.close()


def test_evaluate_dry_run_and_write_mutually_exclusive():
    """S6 §1: --write together with --dry-run exits 2."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_trade(db, "polymarket:st1")
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--wallet-id", WID,
                  "--write", "--dry-run"])
    assert rc == 2, rc
    db.close()


def test_evaluate_production_write_refused_before_open():
    """S6 §1/§2: write on recognized production DB missing the full gate set
    is refused (rc 2) BEFORE any writable open / schema read."""
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    prod_path = str(ROOT / "data" / "polycopy.db")
    rc = ev.main(["--db-path", prod_path, "--write", "--wallet-id", WID])
    assert rc == 2, rc


def test_evaluate_nonproduction_write_without_live_gate():
    """S6 §1/§2: non-production write may use --write WITHOUT --allow-live."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_trade(db, "polymarket:st1")
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc == 0, rc
    assert db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0] == 1
    db.close()


def test_evaluate_unknown_wallet_exits_2_before_writable():
    """S6 §1: unknown --wallet-id exits 2; no writable open required."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id",
                  "does-not-exist"])
    assert rc == 2, rc
    db.close()


def test_evaluate_no_active_watch_exits_2():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    # No active watch (never watched at all).
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc == 2, rc
    db.close()


def test_evaluate_sample_wallet_exits_2():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR, is_sample=1)
    _seed_watch(db, "wl-e", WID)
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc == 2, rc
    db.close()


def test_evaluate_duplicate_active_watches_evaluate_once():
    """S6 §1: a wallet with one ACTIVE + one PAUSED watch is evaluated ONCE
    (only the active watch drives scoring; paused is informational)."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-active", WID)       # active
    _seed_watch(db, "wl-paused", WID, status="paused")  # informational only
    _seed_trade(db, "polymarket:st1")
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc == 0, rc
    assert db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0] == 1
    db.close()


def test_evaluate_taxonomy_enables_category_aggregation():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_trade(db, "polymarket:st1")
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), *WRITE, "--wallet-id", WID])
    assert rc == 0, rc
    cats = db.conn.execute(
        "SELECT category_label, verdict FROM category_wallet_score_decisions "
        "WHERE wallet_id=?", (WID,)).fetchall()
    assert len(cats) == 1, cats
    assert dict(cats[0])["category_label"] == "politics", cats
    db.close()


def test_evaluate_no_category_row_without_taxonomy():
    """S6 §4: partial/unavailable taxonomy creates no category decision."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_trade(db, "polymarket:st1", meta={"foo": "bar"})  # unavailable
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), *WRITE, "--wallet-id", WID])
    assert rc == 0, rc
    n = db.conn.execute(
        "SELECT COUNT(*) FROM category_wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0]
    assert n == 0, n
    db.close()


def test_evaluate_multiple_usable_categories_all_evaluated():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    # Two usable categories via two distinct trades.
    _seed_trade(db, "t-pol", meta={"taxonomy": {"raw_category": "Politics"},
                                   "event": {"id": "e1", "slug": "us"}})
    _seed_trade(db, "t-spo", meta={"taxonomy": {"raw_category": "Sports"},
                                   "event": {"id": "e2", "slug": "nba"}})
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), *WRITE, "--wallet-id", WID])
    assert rc == 0, rc
    labels = [dict(r)["category_label"] for r in db.conn.execute(
        "SELECT category_label FROM category_wallet_score_decisions "
        "WHERE wallet_id=?", (WID,)).fetchall()]
    assert set(labels) == {"politics", "sports"}, labels
    db.close()


def test_evaluate_dry_run_writes_zero_rows():
    """S6 §3: dry-run must write zero decisions."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_green_evidence(db, WID, ADDR)
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--json", "--wallet-id", WID])
    assert rc == 0, rc
    w = db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0]
    c = db.conn.execute(
        "SELECT COUNT(*) FROM category_wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0]
    assert w == 0 and c == 0, (w, c)
    db.close()


def test_evaluate_dry_run_reports_would_create():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_green_evidence(db, WID, ADDR)
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--json", "--wallet-id", WID])
    assert rc == 0, rc
    import io
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        ev.main(["--db-path", str(db.db_path), "--json", "--wallet-id", WID])
    finally:
        sys.stdout = old
    out = json.loads(buf.getvalue())
    rec = out["wallets"][0]
    assert rec["wallet_decision_intent"] == "would_create", rec
    assert rec["wallet_decision_would_create"] is True
    db.close()


def test_evaluate_write_reports_created_and_commits_once():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_green_evidence(db, WID, ADDR)
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc == 0, rc
    w = db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0]
    assert w == 1, w
    db.close()


def test_evaluate_replay_idempotent_no_duplicates():
    """S6 §3: unchanged evidence replay creates zero new rows, reports reused."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_green_evidence(db, WID, ADDR)
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc == 0, rc
    # Replay (run to the same path the status CLI uses: current evidence).
    rc2 = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc2 == 0, rc2
    w = db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0]
    assert w == 1, w
    db.close()


def test_evaluate_changed_evidence_creates_new_row():
    """S6 §3: changed canonical evidence creates a new auditable row."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_green_evidence(db, WID, ADDR)
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc == 0, rc
    w_before = db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0]
    # Change evidence: add many more resolved trades -> new fingerprint.
    _seed_green_evidence(db, WID, ADDR, n=160, prefix="more")
    rc2 = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc2 == 0, rc2
    w_after = db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0]
    assert w_after == w_before + 1, (w_before, w_after)
    db.close()


# ── Atomicity / forbidden-artifact tests ─────────────────────────────────────

def test_evaluate_forced_persistence_failure_rolls_back():
    """S6 §2: force a commit failure AFTER at least one wallet + one category
    decision are staged. Prove every new decision rolls back (rc 1, 0 rows)."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_green_evidence(db, WID, ADDR)
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    # Inject a commit-failure hook onto the CLI's DbConn class.
    from evidence_db import DbConn as _DC
    _DC._COMMIT_FAIL_HOOK = RuntimeError("forced commit failure")
    try:
        rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    finally:
        _DC._COMMIT_FAIL_HOOK = None
    assert rc == 1, rc  # rollback -> exit 1
    w = db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0]
    c = db.conn.execute(
        "SELECT COUNT(*) FROM category_wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0]
    assert w == 0 and c == 0, (w, c)  # nothing survived rollback
    db.close()


def test_evaluate_forbidden_delta_rollback():
    """S6 §5: a delta in a forbidden execution-artifact table (simulated by
    inflating the AFTER count) is detected and forces rollback (rc 1, 0 rows)."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_green_evidence(db, WID, ADDR)
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    # Make the AFTER count of specialist_approvals read +1 vs the BEFORE count
    # (simulating a phantom approval created during the run). The second call
    # to count_table_optional is the AFTER snapshot.
    orig = DbConn.count_table_optional
    calls = {"n": 0}
    def _inflated(self, table):
        calls["n"] += 1
        base = orig(self, table)
        if table == "specialist_approvals" and calls["n"] % 2 == 0:
            return base + 1  # AFTER sample shows a delta
        return base
    DbConn.count_table_optional = _inflated
    try:
        rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    finally:
        DbConn.count_table_optional = orig
    assert rc == 1, rc  # delta detected -> rollback, exit 1
    # No score decisions survived.
    assert db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0] == 0
    db.close()


def test_evaluate_preexisting_forbidden_rows_unchanged():
    """S6 §5: pre-existing (unchanged) forbidden rows are allowed."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_trade(db, "polymarket:st1")
    db.conn.execute(
        "INSERT INTO specialist_approvals("
        "approval_id, wallet_address, specialist_category, formula_name, "
        "formula_version, reviewer, approved_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("ap1", ADDR, "politics", "wallet_score", "1", "t",
         "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    db.conn.commit()
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc == 0, rc
    assert db.conn.execute(
        "SELECT COUNT(*) FROM specialist_approvals").fetchone()[0] == 1
    db.close()


def test_evaluate_missing_optional_forbidden_table_ok():
    """S6 §5: a genuinely absent optional forbidden table counts as 0 (no error)."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_trade(db, "polymarket:st1")
    # Destroy one optional forbidden table to prove absence is tolerated.
    db.conn.execute("DROP TABLE IF EXISTS paper_position_settlements")
    db.conn.commit()
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc == 0, rc
    db.close()


def test_evaluate_present_table_count_failure_propagates():
    """S6 §5: a present forbidden table whose COUNT raises propagates (rc 1)."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_green_evidence(db, WID, ADDR)
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    # Poison the count path for a present table (specialist_approvals exists).
    # The rescore CLI opens a DbConn, so patch DbConn.count_table.
    orig = DbConn.count_table_optional
    def _boom(self, table):
        if table == "specialist_approvals":
            raise sqlite3.OperationalError("injected count failure")
        return orig(self, table)
    DbConn.count_table_optional = _boom
    try:
        rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    finally:
        DbConn.count_table_optional = orig
    assert rc == 1, rc
    db.close()


def test_evaluate_source_trades_unchanged():
    """S6 §15.15: rescoring never mutates source_trades evidence."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_green_evidence(db, WID, ADDR)
    before = db.conn.execute(
        "SELECT COUNT(*), SUM(quantity) FROM source_trades").fetchone()
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc == 0, rc
    after = db.conn.execute(
        "SELECT COUNT(*), SUM(quantity) FROM source_trades").fetchone()
    assert before == after, (before, after)
    db.close()


def test_evaluate_no_auto_approval_or_execution():
    """S6 §15.34: no approval/dispatch/candidate/execution artifact created."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_green_evidence(db, WID, ADDR)
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc == 0, rc
    for t in FORBIDDEN_EXECUTION_TABLES:
        n = db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        assert n == 0, f"unexpected artifact in {t}: {n}"
    db.close()


# ── T8: status (read-only, current evidence) ────────────────────────────────
def _status_json(st, db, *, wallet_id=None):
    old = sys.stdout
    buf = io.StringIO()
    sys.stdout = buf
    try:
        args = ["--db-path", str(db.db_path), "--json"]
        if wallet_id is not None:
            args += ["--wallet-id", wallet_id]
        st.main(args)
    finally:
        sys.stdout = old
    return json.loads(buf.getvalue())


def test_status_current_green_on_copy_candidate():
    """S6 §7/§16.16: current wallet + current supported category candidate -> GREEN."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_green_evidence(db, WID, ADDR)
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    assert out["overall_state"] == "GREEN", out
    w = out["wallets"][0]
    assert w["ready_for_human_review"] is True
    assert w["approval_created"] is False
    assert w["dispatch_created"] is False
    assert w["execution_authorized"] is False
    db.close()


def test_status_stale_historical_wallet_cannot_green():
    """S6 §16.17: a stale HISTORICAL wallet copy_candidate (current evidence
    no longer qualifies) cannot create GREEN."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    # Only a single unresolved trade: current evidence -> incomplete.
    _seed_trade(db, "polymarket:st1")
    now = _recent_ts(0)
    # Historical wallet copy_candidate decision exists but is stale.
    db.conn.execute(
        "INSERT INTO wallet_score_decisions("
        "wallet_id, formula_name, formula_version, idempotency_key, "
        "final_score, verdict, computed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (WID, "wallet_score_v1", "1", "k1", 80.0, "copy_candidate", now, now))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    assert out["overall_state"] != "GREEN", out
    db.close()


def test_status_stale_historical_category_cannot_green():
    """S6 §16.18: stale HISTORICAL category copy_candidate cannot create GREEN."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "polymarket:st1")
    now = _recent_ts(0)
    db.conn.execute(
        "INSERT INTO category_wallet_score_decisions("
        "wallet_id, category_label, formula_name, formula_version, "
        "idempotency_key, final_score, verdict, computed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (WID, "politics", "category_wallet_score_v1", "1", "ck1", 80.0,
         "copy_candidate", now, now))
    # Current wallet resolves copy_candidate too (give it enough evidence).
    _seed_green_evidence(db, WID, ADDR)
    # But the single 'polymarket:st1' trade already seeded makes wallet
    # incomplete-> we need the wallet to be copy_candidate currently.
    # Re-seed green evidence supersedes the single trade.
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    # Even if wallet is green, the historical category is ignored: status
    # re-derives current category resolution -> not copy_candidate on 1 trade.
    assert out["overall_state"] != "GREEN", out
    db.close()


def test_status_unsupported_historical_category_cannot_green():
    """S6 §16.19: an UNSUPPORTED historical category label cannot create GREEN."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    # Current evidence is INCOMPLETE (single trade) so it does NOT qualify.
    _seed_trade(db, "polymarket:st1", meta={"taxonomy": {"raw_category": "Politics"},
                                            "event": {"id": "e1", "slug": "us"}})
    now = _recent_ts(0)
    # Historical wallet copy_candidate + category copy_candidate on an
    # UNSUPPORTED label ('sports' is not a supported label for this wallet).
    db.conn.execute(
        "INSERT INTO wallet_score_decisions("
        "wallet_id, formula_name, formula_version, idempotency_key, "
        "final_score, verdict, computed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (WID, "wallet_score_v1", "1", "k1", 80.0, "copy_candidate", now, now))
    db.conn.execute(
        "INSERT INTO category_wallet_score_decisions("
        "wallet_id, category_label, formula_name, formula_version, "
        "idempotency_key, final_score, verdict, computed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (WID, "sports", "category_wallet_score_v1", "1", "ck1", 80.0,
         "copy_candidate", now, now))
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    # 'sports' is not a supported label for this wallet -> NOT green.
    assert out["overall_state"] != "GREEN", out
    db.close()


def test_status_best_current_category_deterministic():
    """S6 §8/§16.20: best current category selected deterministically."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    # Two supported categories; both incomplete (few trades) -> best by label.
    _seed_trade(db, "t-pol", meta={"taxonomy": {"raw_category": "Politics"},
                                   "event": {"id": "e1", "slug": "us"}})
    _seed_trade(db, "t-spo", meta={"taxonomy": {"raw_category": "Sports"},
                                   "event": {"id": "e2", "slug": "nba"}})
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    cats = out["wallets"][0]["current_category_results"]
    assert {c["category_label"] for c in cats} == {"politics", "sports"}, cats
    best = out["wallets"][0]["selected_best_category"]
    # Deterministic: lowest label wins when verdicts + scores tie.
    assert best["category_label"] == "politics", best
    db.close()


def test_status_partial_taxonomy_yellow():
    """S6 §11/§16.21: ordinary partial taxonomy -> YELLOW (not conflict/RED)."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    # Partial taxonomy (tags only, no raw_category).
    _seed_trade(db, "t-partial",
                meta={"taxonomy": {"tags": ["election"]}})
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert w["state"] == "YELLOW", out
    assert "taxonomy_partial" in w["yellow_reasons"], w
    db.close()


def test_status_explicit_taxonomy_conflict_red():
    """S6 §11/§16.22: explicit wallet-scoped taxonomy conflict -> RED."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    # Trade carries usable taxonomy AND an enrichment conflict row.
    _seed_trade(db, "t-pol", meta={"taxonomy": {"raw_category": "Politics"},
                                   "event": {"id": "e1", "slug": "us"}})
    db.conn.execute(
        "INSERT INTO source_trade_enrichments("
        "enrichment_id, source_trade_internal_id, status, reason_codes_json, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?)",
        ("en1", "t-pol", "conflict",
         json.dumps(["taxonomy_conflict"]), "2026-01-01T00:00:00Z",
         "2026-01-01T00:00:00Z"))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert w["state"] == "RED", out
    assert any("taxonomy_conflict" in r for r in w["red_reasons"]), w
    db.close()


def test_status_resolution_conflict_red():
    """S6 §11/§16.23: wallet-scoped resolution conflict -> RED."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "t-pol", meta={"taxonomy": {"raw_category": "Politics"},
                                   "event": {"id": "e1", "slug": "us"}},
                cond="m1")
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, last_error, "
        "attempt_count) VALUES (?,?,?,?,?)",
        ("m1", _recent_ts(0), "conflict", "resolution mismatch", 3))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert w["state"] == "RED", out
    assert any("resolution_conflict" in r for r in w["red_reasons"]), w
    db.close()


def test_status_unrelated_conflict_not_red():
    """S6 §11/§16.24: another wallet's market failure must NOT make this RED."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_wallet(db, "uuid-other", "0x" + "b" * 40)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "t-pol", meta={"taxonomy": {"raw_category": "Politics"},
                              "event": {"id": "e1", "slug": "us"}},
                cond="m1")
    # A conflicting refresh row for a market NOT owned by this wallet.
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, last_error, "
        "attempt_count) VALUES (?,?,?,?,?)",
        ("OTHERMARKET", _recent_ts(0), "conflict", "x", 3))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert w["state"] != "RED", out
    db.close()


def test_status_new_never_collected_watch_yellow():
    """S6 §10/§16.25: a new never-collected watch -> YELLOW (not RED)."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=None)  # never collected
    _seed_trade(db, "t-pol", meta={"taxonomy": {"raw_category": "Politics"},
                                   "event": {"id": "e1", "slug": "us"}})
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert w["state"] == "YELLOW", out
    assert "collector_not_yet_collected" in w["yellow_reasons"], w
    db.close()


def test_status_overdue_collector_red():
    """S6 §10/§16.26: truly overdue collector -> RED."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    # Collection timestamp far older than default 3h policy.
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(72))
    _seed_trade(db, "t-pol", meta={"taxonomy": {"raw_category": "Politics"},
                                   "event": {"id": "e1", "slug": "us"}})
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert w["state"] == "RED", out
    assert "collector_stale" in w["red_reasons"], w
    db.close()


def test_status_recovered_refresh_not_stale():
    """S6 §10/§16.27: a recovered (later successful) refresh row -> not stale."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "t-pol", meta={"taxonomy": {"raw_category": "Politics"},
                              "event": {"id": "e1", "slug": "us"}}, cond="m1")
    # Old error but a current successful status -> recovered, not stale.
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, last_error, "
        "attempt_count, resolved_at) VALUES (?,?,?,?,?,?)",
        ("m1", _recent_ts(0), "resolved", None, 3, _recent_ts(0)))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert "refresh_overdue" not in w["red_reasons"], w
    db.close()


def test_status_current_failed_refresh_red():
    """S6 §10/§16.28: current failed/overdue refresh -> RED."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "t-pol", meta={"taxonomy": {"raw_category": "Politics"},
                              "event": {"id": "e1", "slug": "us"}}, cond="m1")
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, last_error, "
        "attempt_count) VALUES (?,?,?,?,?)",
        ("m1", _recent_ts(72), "failed", "boom", 5))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert w["state"] == "RED", out
    assert any("refresh" in r for r in w["red_reasons"]), w
    db.close()


def test_status_missing_or_sample_active_wallet_red():
    """S6 §9/§16.29: a SAMPLE wallet behind an active watch -> RED (never GREEN).

    NOTE: a truly orphan watch (wallet_id with no wallets row) is prevented by
    the schema FK, so `missing_wallet_record` is defensive only; the sample
    path is the realistic RED driver and is asserted here.
    """
    db, _ = _open()
    _seed_wallet(db, "uuid-sample", "0x" + "s" * 40, is_sample=1)
    _seed_watch(db, "wl-s", "uuid-sample")
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    assert out["overall_state"] == "RED", out
    db.close()


def test_status_wallet_id_recomputes_overall():
    """S6 §9/§16.30: --wallet-id filters, recomputes overall + counts."""
    db, _ = _open()
    _seed_wallet(db, "uuid-a", "0x" + "a" * 40)
    _seed_wallet(db, "uuid-b", "0x" + "b" * 40)
    _seed_watch(db, "wl-a", "uuid-a", last_collection_at=_recent_ts(0))
    _seed_watch(db, "wl-b", "uuid-b", last_collection_at=_recent_ts(0))
    _seed_green_evidence(db, "uuid-a", "0x" + "a" * 40)
    _seed_trade(db, "st-b", meta={"taxonomy": {"raw_category": "Politics"},
                                  "event": {"id": "e1", "slug": "us"}})
    st = _load("specialist_evidence_status.py")
    # Full report: GREEN (uuid-a qualifies) + incomplete (uuid-b).
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    full = _status_json(st, db)
    assert full["watched_count"] == 2, full
    # Filtered report: only uuid-a.
    rc2 = st.main(["--db-path", str(db.db_path), "--wallet-id", "uuid-a"])
    assert rc2 == 0, rc2
    filt = _status_json(st, db, wallet_id="uuid-a")
    assert filt["watched_count"] == 1, filt
    assert filt["wallets"][0]["wallet_id"] == "uuid-a", filt
    db.close()


def test_status_invalid_selector_exits_2():
    """S6 §9/§16.31: invalid status selector (unknown wallet) exits 2."""
    db, _ = _open()
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path), "--wallet-id", "ghost"])
    assert rc == 2, rc
    db.close()


def test_status_present_table_count_error_not_zero():
    """S6 §16.32: a present-table SQL error does NOT report zero (propagates)."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "t-pol", meta={"taxonomy": {"raw_category": "Politics"},
                                   "event": {"id": "e1", "slug": "us"}})
    st = _load("specialist_evidence_status.py")
    orig = DbConn.count_table_optional
    def _boom(self, table):
        if table == "specialist_approvals":
            raise sqlite3.OperationalError("injected")
        return orig(self, table)
    DbConn.count_table_optional = _boom
    try:
        rc = st.main(["--db-path", str(db.db_path)])
    finally:
        DbConn.count_table_optional = orig
    # Count-error path fails the report closed -> not a silent zero. The CLI
    # returns RC 1 (untrustworthy report) rather than fabricating GREEN.
    assert rc == 1, rc
    db.close()


def test_status_green_emits_zero_write_sql():
    """S6 §14/§16.33: a complete GREEN report executes zero write SQL."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_green_evidence(db, WID, ADDR)
    st = _load("specialist_evidence_status.py")
    writes = []

    def _trace(conn, sql):
        s = sql.strip().upper()
        if s.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            writes.append(sql)

    raw = db.conn
    raw.set_trace_callback(_trace)
    try:
        rc = st.main(["--db-path", str(db.db_path)])
    finally:
        raw.set_trace_callback(None)
    assert rc == 0, rc
    assert writes == [], writes
    db.close()


def test_status_yellow_red_emit_zero_write_sql():
    """S6 §14/§16.33: YELLOW and RED reports also execute zero write SQL."""
    # YELLOW
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "t-pol", meta={"taxonomy": {"raw_category": "Politics"},
                                   "event": {"id": "e1", "slug": "us"}})
    st = _load("specialist_evidence_status.py")
    writes = []
    db.conn.set_trace_callback(
        lambda sql: writes.append(sql) if sql.strip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE", "REPLACE")) else None)
    rc = st.main(["--db-path", str(db.db_path)])
    db.conn.set_trace_callback(None)
    assert rc == 0, rc
    assert writes == [], writes
    db.close()
    # RED (sample wallet)
    db, _ = _open()
    _seed_wallet(db, "uuid-sample", "0x" + "s" * 40, is_sample=1)
    _seed_watch(db, "wl-s", "uuid-sample")
    st = _load("specialist_evidence_status.py")
    writes = []
    db.conn.set_trace_callback(
        lambda sql: writes.append(sql) if sql.strip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE", "REPLACE")) else None)
    rc = st.main(["--db-path", str(db.db_path)])
    db.conn.set_trace_callback(None)
    assert rc == 0, rc
    assert writes == [], writes
    db.close()


def test_status_no_execution_artifact_created():
    """S6 §16.34: no approval/dispatch/candidate/execution artifact created."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_green_evidence(db, WID, ADDR)
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    for t in FORBIDDEN_EXECUTION_TABLES:
        n = db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        assert n == 0, f"unexpected artifact in {t}: {n}"
    db.close()


# ── E2E (S6 §15.35) ─────────────────────────────────────────────────────────

def test_e2e_canonical_dry_run_write_replay_status():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_green_evidence(db, WID, ADDR)
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    st = _load("specialist_evidence_status.py")

    # 1) Dry-run score (writes zero rows, reports would_create).
    rc = ev.main(["--db-path", str(db.db_path), "--json", "--wallet-id", WID])
    assert rc == 0, rc
    assert db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0] == 0

    # 2) Write score (commits once).
    rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc == 0, rc
    w_id = db.conn.execute(
        "SELECT id FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()
    assert w_id is not None

    # 3) Replay (unchanged evidence -> reuse, no new row).
    rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    assert rc == 0, rc
    assert db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0] == 1

    # 4) Read-only status uses CURRENT evidence; fingerprints agree.
    # The decision tables do NOT persist evidence_fingerprint, but the resolver
    # derives it deterministically from the SAME canonical evidence, so we
    # recompute the resolver fingerprint and compare against the status report.
    import io
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        st.main(["--db-path", str(db.db_path), "--json", "--wallet-id", WID])
    finally:
        sys.stdout = old
    status = json.loads(buf.getvalue())
    wrec = status["wallets"][0]
    cur_fp = wrec["current_wallet_resolution"]["evidence_fingerprint"]
    # Recompute independently via the frozen resolver over the same evidence.
    recomputed = resolve_wallet_score_v1(
        db, WID, cutoff_timestamp=None, persist=False, now=None)
    assert recomputed.evidence_fingerprint == cur_fp, (
        recomputed.evidence_fingerprint, cur_fp)
    # Readiness is based on current evidence (this wallet is copy_candidate).
    assert wrec["ready_for_human_review"] is True

    # 5) Zero research-to-execution artifacts created.
    for t in FORBIDDEN_EXECUTION_TABLES:
        n = db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        assert n == 0, f"unexpected artifact in {t}: {n}"
    db.close()


# ── T9: S6 focused correctness corrections (§2–§7) ───────────────────────────

def test_status_ready_flag_requires_green_and_red_downgrades():
    """S6 §2: score_pair_candidate copy_candidate but a RED reason -> RED,
    ready_for_human_review False, top-level ready count 0."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_green_evidence(db, WID, ADDR)  # both wallet + category copy_candidate
    # Inject a RED reason via a current failed refresh on one of the wallet's
    # canonical markets (green seed uses market_source_id='m0').
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, last_error, "
        "attempt_count) VALUES (?,?,?,?,?)",
        ("m0", _recent_ts(0), "failed", "boom", 5))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert w["state"] == "RED", out
    assert w["ready_for_human_review"] is False, w
    assert out["ready_for_human_review_count"] == 0, out
    db.close()


def test_status_recovered_success_retains_old_error():
    """S6 §3: last_status='resolved' with a stale last_error -> not RED."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "st1", meta={"taxonomy": {"raw_category": "Politics"},
                                  "event": {"id": "e1", "slug": "us"}}, cond="st1")
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, last_error, "
        "attempt_count, resolved_at) VALUES (?,?,?,?,?,?)",
        ("st1", _recent_ts(0), "resolved", "ancient conflict", 3, _recent_ts(0)))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert "refresh_current_failed" not in w["red_reasons"], w
    assert w["state"] != "RED", out
    db.close()


def test_status_recent_unresolved_healthy():
    """S6 §3: recent unresolved market is informational, not an error."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "st1", meta={"taxonomy": {"raw_category": "Politics"},
                                  "event": {"id": "e1", "slug": "us"}}, cond="st1")
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, attempt_count) "
        "VALUES (?,?,?,?)",
        ("st1", _recent_ts(0), "unresolved", 1))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert "refresh_overdue" not in w["red_reasons"], w
    assert "refresh_current_failed" not in w["red_reasons"], w
    db.close()


def test_status_overdue_unresolved_red():
    """S6 §3: unresolved whose last_checked_at is overdue beyond policy -> RED."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "st1", meta={"taxonomy": {"raw_category": "Politics"},
                                  "event": {"id": "e1", "slug": "us"}}, cond="st1")
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, attempt_count) "
        "VALUES (?,?,?,?)",
        ("st1", _recent_ts(72), "unresolved", 1))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert any("refresh_overdue" in r for r in w["red_reasons"]), w
    db.close()


def test_status_malformed_last_checked_at_red():
    """S6 §3: present but malformed last_checked_at -> explicit RED reason."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "st1", meta={"taxonomy": {"raw_category": "Politics"},
                                  "event": {"id": "e1", "slug": "us"}}, cond="st1")
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, attempt_count) "
        "VALUES (?,?,?,?)",
        ("st1", "not-a-timestamp", "ok", 1))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert any("malformed_timestamp" in r for r in w["red_reasons"]), w
    db.close()


def test_status_malformed_next_check_after_red():
    """S6 §3: present but malformed next_check_after -> explicit RED reason."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "st1", meta={"taxonomy": {"raw_category": "Politics"},
                                  "event": {"id": "e1", "slug": "us"}}, cond="st1")
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, next_check_after, "
        "attempt_count) VALUES (?,?,?,?,?)",
        ("st1", _recent_ts(0), "ok", "malformed", 1))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert any("malformed_timestamp" in r for r in w["red_reasons"]), w
    db.close()


def test_status_current_failed_error_conflict_red():
    """S6 §3: current failed/error/conflict status -> RED (no last_error needed)."""
    for status in ("failed", "error", "conflict"):
        db, _ = _open()
        _seed_wallet(db, WID, ADDR)
        _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
        _seed_trade(db, "st1", meta={"taxonomy": {"raw_category": "Politics"},
                                      "event": {"id": "e1", "slug": "us"}}, cond="st1")
        db.conn.execute(
            "INSERT INTO specialist_market_refresh_state("
            "market_source_id, last_checked_at, last_status, attempt_count) "
            "VALUES (?,?,?,?)",
            ("st1", _recent_ts(0), status, 5))
        db.conn.commit()
        st = _load("specialist_evidence_status.py")
        out = _status_json(st, db)
        w = out["wallets"][0]
        assert any("refresh_current_failed" in r for r in w["red_reasons"]), (status, w)
        db.close()


def test_status_terminal_resolved_aged_not_red():
    """S6 §2: a resolved market checked 72+ h ago stays non-RED (terminal)."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "st1", meta={"taxonomy": {"raw_category": "Politics"},
                                  "event": {"id": "e1", "slug": "us"}}, cond="st1")
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, resolved_at, "
        "attempt_count) VALUES (?,?,?,?,?)",
        ("st1", _recent_ts(72), "resolved", _recent_ts(72), 1))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert not any(r.startswith("refresh") for r in w["red_reasons"]), w
    db.close()


def test_status_terminal_resolved_with_old_error_not_red():
    """S6 §2: resolved market retains old last_error and stays non-RED."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "st1", meta={"taxonomy": {"raw_category": "Politics"},
                                  "event": {"id": "e1", "slug": "us"}}, cond="st1")
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, last_error, "
        "resolved_at, attempt_count) VALUES (?,?,?,?,?,?)",
        ("st1", _recent_ts(0), "resolved", "ancient conflict", _recent_ts(0), 3))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert not any(r.startswith("refresh") for r in w["red_reasons"]), w
    db.close()


def test_status_resolved_without_resolved_at_aged_red():
    """S6 §2: resolved status BUT missing resolved_at, aged -> falls through
    to staleness and is RED (terminal bypass requires the timestamp)."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "st1", meta={"taxonomy": {"raw_category": "Politics"},
                                  "event": {"id": "e1", "slug": "us"}}, cond="st1")
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, attempt_count) "
        "VALUES (?,?,?,?)",
        ("st1", _recent_ts(72), "resolved", 1))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert any("refresh_overdue" in r for r in w["red_reasons"]), w
    db.close()


def test_status_malformed_resolved_at_red():
    """S6 §2: present malformed resolved_at -> explicit RED (terminal
    authority unreadable)."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "st1", meta={"taxonomy": {"raw_category": "Politics"},
                                  "event": {"id": "e1", "slug": "us"}}, cond="st1")
    db.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, resolved_at, "
        "attempt_count) VALUES (?,?,?,?,?)",
        ("st1", _recent_ts(0), "resolved", "not-a-timestamp", 1))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    out = _status_json(st, db)
    w = out["wallets"][0]
    assert any("malformed_timestamp" in r for r in w["red_reasons"]), w
    db.close()


def test_status_integrity_finding_red_exit0():
    """S6 §3: an actual integrity_check / FK finding -> ordinary RED, exit 0."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    st = _load("specialist_evidence_status.py")
    DbConn = st.DbConn
    orig_fetchone = DbConn.fetchone
    def _trap(self, sql, params=None):
        if "integrity_check" in str(sql):
            # Return a non-ok row -> ordinary RED finding.
            class _R:
                def __getitem__(self, i):
                    return "not_ok"
            return _R()
        return orig_fetchone(self, sql, params)
    DbConn.fetchone = _trap
    try:
        rc = st.main(["--db-path", str(db.db_path)])
    finally:
        DbConn.fetchone = orig_fetchone
    assert rc == 0, rc
    out = st._LAST_REPORT  # exact (patched) run; do not re-run main
    assert out["overall_state"] == "RED", out
    assert ("integrity_check_not_ok" in out["global_integrity"]["reasons"]
            or any("integrity_check_not_ok" in r
                   for w in out["wallets"] for r in w["red_reasons"])), out
    db.close()


def test_status_integrity_query_error_exit1():
    """S6 §3: integrity_check QUERY raising -> fail_closed, exit 1."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    st = _load("specialist_evidence_status.py")
    DbConn = st.DbConn
    orig_fetchone = DbConn.fetchone
    def _trap(self, sql, params=None):
        if "integrity_check" in str(sql):
            raise sqlite3.OperationalError("injected integrity failure")
        return orig_fetchone(self, sql, params)
    DbConn.fetchone = _trap
    try:
        rc = st.main(["--db-path", str(db.db_path)])
    finally:
        DbConn.fetchone = orig_fetchone
    assert rc == 1, rc
    db.close()


def test_status_schema_version_mismatch_exit1():
    """S6 §3: schema_version != 21 in _meta -> fail_closed, exit 1."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    st = _load("specialist_evidence_status.py")
    orig = st._read_meta_schema_version
    st._read_meta_schema_version = lambda db: 20
    try:
        rc = st.main(["--db-path", str(db.db_path)])
    finally:
        st._read_meta_schema_version = orig
    assert rc == 1, rc
    db.close()


def test_status_schema_version_missing_exit1():
    """S6 §3: missing schema_version row -> fail_closed, exit 1."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    st = _load("specialist_evidence_status.py")
    orig = st._read_meta_schema_version
    st._read_meta_schema_version = lambda db: (_ for _ in ()).throw(
        RuntimeError("schema_version row missing from _meta"))
    try:
        rc = st.main(["--db-path", str(db.db_path)])
    finally:
        st._read_meta_schema_version = orig
    assert rc == 1, rc
    db.close()


def test_status_stable_preexisting_artifact_baseline_not_red():
    """S6 §4: stable preexisting copy_candidate rows do NOT force RED; counts
    stay visible; delta is zero; report still executes zero writes."""
    import importlib
    st = importlib.import_module("specialist_evidence_status")
    db, _ = _open()
    _seed_wallet(db, "uuid-g", "0x" + "g" * 40)
    _seed_watch(db, "wl-g", "uuid-g", last_collection_at=_recent_ts(0))
    _seed_green_evidence(db, "uuid-g", "0x" + "g" * 40)
    # Preexisting legitimate execution-plane rows (NOT created by S6).
    db.conn.execute(
        "INSERT INTO specialist_approvals("
        "approval_id, wallet_address, specialist_category, formula_name, "
        "formula_version, reviewer, approved_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("ap1", "0x" + "g" * 40, "politics", "f1", "v1", "t",
         "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z"))
    db.conn.commit()
    orig = st.open_readonly
    st.open_readonly = _instrumented_readonly
    try:
        rc = st.main(["--db-path", str(db.db_path)])
    finally:
        st.open_readonly = orig
    assert rc == 0, rc
    out = st._LAST_REPORT  # exact (patched) run; do not re-run main
    assert out["execution_artifact_baseline_counts"]["specialist_approvals"] == 1, out
    assert out["execution_artifact_counts"]["specialist_approvals"] == 1, out
    assert out["execution_artifact_delta"] == {}, out
    assert out["wallets"][0]["state"] == "GREEN", out
    assert _LAST_SPY._writes == [], _LAST_SPY._writes
    db.close()


def test_status_execution_delta_produces_red():
    """S6 §4: an injected count delta during the run -> RED with exact table."""
    import importlib
    st = importlib.import_module("specialist_evidence_status")
    db, _ = _open()
    _seed_wallet(db, "uuid-g", "0x" + "g" * 40)
    _seed_watch(db, "wl-g", "uuid-g", last_collection_at=_recent_ts(0))
    _seed_green_evidence(db, "uuid-g", "0x" + "g" * 40)
    # Baseline 0; after-eval capture sees 1 -> delta on copy_candidates.
    orig = st._global_execution_counts
    state = {"n": 0}
    def _two_phase(db):
        state["n"] += 1
        if state["n"] == 1:
            return ({k: 0 for k in FORBIDDEN_EXECUTION_TABLES}, {}, {})
        return ({k: (1 if k == "copy_candidates" else 0)
                 for k in FORBIDDEN_EXECUTION_TABLES}, {}, {})
    st._global_execution_counts = _two_phase
    try:
        rc = st.main(["--db-path", str(db.db_path)])
    finally:
        st._global_execution_counts = orig
    assert rc == 0, rc
    out = st._LAST_REPORT  # exact (patched) run; do not re-run main
    assert out["overall_state"] == "RED", out
    assert out["execution_artifact_delta"].get("copy_candidates") == 1, out
    assert any("execution_artifact_delta:copy_candidates" in r
               for w in out["wallets"] for r in w["red_reasons"]), out
    db.close()


def test_status_explicit_sample_selector_exits_2():
    """S6 §4: explicit --wallet-id on a SAMPLE wallet -> exit 2."""
    db, _ = _open()
    _seed_wallet(db, "uuid-sample", "0x" + "s" * 40, is_sample=1)
    _seed_watch(db, "wl-s", "uuid-sample")
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path), "--wallet-id", "uuid-sample"])
    assert rc == 2, rc
    db.close()


def test_status_duplicate_active_watch_dedup_lowest_id():
    """S6 §1B: one wallet with two synthetic ACTIVE rows -> evaluate once,
    lowest active watch id selected, watched_count==1, ready count not dup'd.

    The schema normally blocks duplicate active rows via a partial unique
    index, so we drop it in a disposable test DB to exercise the pure dedup.
    """
    import importlib
    st = importlib.import_module("specialist_evidence_status")
    db, _ = _open()
    # Drop the partial unique index that enforces one-active-per-wallet.
    db.conn.execute("DROP INDEX IF EXISTS ux_evidence_watchlist_active")
    db.conn.commit()
    _seed_wallet(db, WID, ADDR)
    # Two synthetic active rows; lower id should win.
    _seed_watch(db, "wl-bbb", WID)  # higher id
    _seed_watch(db, "wl-aaa", WID)  # lower id -> chosen
    _seed_green_evidence(db, WID, ADDR)
    out = _status_json(st, db)
    # Exactly one wallet record (deduplicated), chosen lowest active id.
    assert len(out["wallets"]) == 1, out
    assert out["wallets"][0]["watch_id"] == "wl-aaa", out
    assert out["watched_count"] == 1, out
    # Ready count is per-wallet, not per-raw-active-row.
    assert out["ready_for_human_review_count"] <= 1, out
    db.close()


def test_status_paused_row_lower_id_active_chosen():
    """S6 §1A: one wallet with a paused row ID lower than its active row.

    Selector must still succeed and choose the ACTIVE row (never matched[0]
    which could be the paused row sorting first)."""
    import importlib
    st = importlib.import_module("specialist_evidence_status")
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    # Paused row with LOWER id than the active row.
    _seed_watch(db, "wl-paused-lower", WID, status="paused")
    _seed_watch(db, "wl-active-higher", WID, status="active",
                last_collection_at=_recent_ts(0))
    _seed_green_evidence(db, WID, ADDR)
    out = _status_json(st, db)
    assert len(out["wallets"]) == 1, out
    assert out["wallets"][0]["watch_id"] == "wl-active-higher", out
    db.close()


def test_status_missing_wallet_record_red():
    """S6 §4: FK disabled seed with an orphan active watch -> missing_wallet_record RED."""
    db, _ = _open()
    # Disable FK so we can insert an orphan watch (no wallets row).
    db.conn.execute("PRAGMA foreign_keys = OFF")
    db.conn.execute(
        "INSERT INTO specialist_evidence_watchlist(wallet_id, status, source, "
        "reason, created_by, created_at, last_collection_at) "
        "VALUES (?, 'active', 'manual', 'seed', 't', '2026-01-01T00:00:00Z', ?)",
        ("uuid-ghost", _recent_ts(0)))
    db.conn.commit()
    db.conn.execute("PRAGMA foreign_keys = ON")
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    # The monitor must still report the damaged row honestly as RED.
    assert any("missing_wallet_record" in w["red_reasons"] for w in out["wallets"]), out
    db.close()


def test_evidence_db_absent_optional_table_returns_zero():
    """S6 §5: a genuinely absent optional table returns 0 via count_table_optional."""
    db, _ = _open()
    conn = DbConn(db.conn)
    # 'nonexistent_table' is not in sqlite_master -> 0.
    assert conn.count_table_optional("nonexistent_table") == 0
    db.close()


def test_evidence_db_present_bogus_name_propagates():
    """S6 §5: a present table whose COUNT raises the DB's internal
    'no such table: ...' propagates rather than returning zero.

    We create a REAL view (so it appears in sqlite_master) that references a
    non-existent base table; SELECT COUNT(*) over it raises an OperationalError
    containing 'no such table'. count_table (strict) must propagate it.
    """
    db, _ = _open()
    conn = DbConn(db.conn)
    conn.conn.execute(
        "CREATE VIEW bogus_internal_name AS SELECT 1 AS x FROM nonexistent_base"
    )
    conn.conn.commit()
    with pytest.raises(sqlite3.OperationalError) as exc:
        conn.count_table("bogus_internal_name")
    assert "no such table" in str(exc.value).lower(), exc.value
    db.close()


def test_status_schema_version_reported():
    """S6 §6: top-level schema_version populated from the _meta row (==21)."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    st = _load("specialist_evidence_status.py")
    out = _status_json(st, db)
    assert out["schema_version"] == 21, out
    db.close()


def test_status_nonpositive_stale_hours_exits_2():
    """S6 §6: non-positive stale-hour arguments -> exit 2."""
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID, last_collection_at=_recent_ts(0))
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path),
                  "--collector-stale-after-hours", "0"])
    assert rc == 2, rc
    rc = st.main(["--db-path", str(db.db_path),
                  "--refresh-stale-after-hours", "-1"])
    assert rc == 2, rc
    db.close()


def _instrumented_readonly(db_path):
    """S6 §7: return an instrumented DbConn whose execute records writes.
    The most recent spy is stashed in `_LAST_SPY` for assertions."""
    class _WriteSpyDbConn(DbConn):
        _writes = []

        def execute(self, sql, params=None):
            s = str(sql).strip().upper()
            if s.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
                self._writes.append(sql)
            return super().execute(sql, params)
    real = open_readonly(db_path)
    spy = _WriteSpyDbConn(real.conn)
    global _LAST_SPY
    _LAST_SPY = spy
    return spy


def test_status_zero_write_proof_all_states():
    """S6 §7: a complete GREEN, YELLOW, and RED report executes ZERO writes on
    the connection actually used by the status code."""
    import importlib
    st = importlib.import_module("specialist_evidence_status")
    # GREEN wallet.
    dbg, _ = _open()
    _seed_wallet(dbg, "uuid-g", "0x" + "g" * 40)
    _seed_watch(dbg, "wl-g", "uuid-g", last_collection_at=_recent_ts(0))
    _seed_green_evidence(dbg, "uuid-g", "0x" + "g" * 40)
    # YELLOW wallet (never collected).
    _seed_wallet(dbg, "uuid-y", "0x" + "y" * 40)
    _seed_watch(dbg, "wl-y", "uuid-y", last_collection_at=None)
    # RED wallet (current failed refresh).
    _seed_wallet(dbg, "uuid-r", "0x" + "r" * 40)
    _seed_watch(dbg, "wl-r", "uuid-r", last_collection_at=_recent_ts(0))
    _seed_trade(dbg, "str", meta={"taxonomy": {"raw_category": "Politics"},
                                   "event": {"id": "e1", "slug": "us"}}, cond="str")
    dbg.conn.execute(
        "INSERT INTO specialist_market_refresh_state("
        "market_source_id, last_checked_at, last_status, attempt_count) "
        "VALUES (?,?,?,?)",
        ("str", _recent_ts(0), "failed", 5))
    dbg.conn.commit()
    # Patch the CLI's open_readonly to return our instrumented connection.
    orig = st.open_readonly
    st.open_readonly = _instrumented_readonly
    try:
        rc = st.main(["--db-path", str(dbg.db_path)])
    finally:
        st.open_readonly = orig
    assert rc == 0, rc
    assert _LAST_SPY._writes == [], _LAST_SPY._writes
    dbg.close()


def test_status_uses_readonly_open():
    """S6 §7: the monitor opens read-only (mode=ro) for the report."""
    import importlib
    st = importlib.import_module("specialist_evidence_status")
    dbg, _ = _open()
    _seed_wallet(dbg, WID, ADDR)
    _seed_watch(dbg, "wl-e", WID, last_collection_at=_recent_ts(0))
    seen = {}
    orig = st.open_readonly
    def _spy(p):
        conn = orig(p)
        seen["called"] = True
        # Prove the underlying connection is read-only: an attempt to modify the
        # DB through this connection must be rejected.
        try:
            conn.conn.execute("CREATE TABLE _ro_probe(x INTEGER)")
            seen["readonly"] = False
        except sqlite3.OperationalError:
            seen["readonly"] = True
        return conn
    st.open_readonly = _spy
    try:
        st.main(["--db-path", str(dbg.db_path)])
    finally:
        st.open_readonly = orig
    assert seen.get("called") is True, seen
    assert seen.get("readonly") is True, seen
    dbg.close()


def test_status_delta_applied_to_final_wallet_state():
    """S6 EXEC-DELTA correction §1: a wallet that initially resolves GREEN
    but whose post-evaluation artifact count changes must be flipped to RED with
    ready_for_human_review=False, and the count recomputed AFTER the delta so
    ready_for_human_review_count==0. Baseline/count/delta stay accurate."""
    import importlib
    st = importlib.import_module("specialist_evidence_status")
    db, _ = _open()
    _seed_wallet(db, "uuid-g", "0x" + "g" * 40)
    _seed_watch(db, "wl-g", "uuid-g", last_collection_at=_recent_ts(0))
    _seed_green_evidence(db, "uuid-g", "0x" + "g" * 40)
    # Baseline 0; after-eval capture sees 1 -> delta on copy_candidates.
    orig = st._global_execution_counts
    state = {"n": 0}
    def _two_phase(d):
        state["n"] += 1
        if state["n"] == 1:
            return ({k: 0 for k in FORBIDDEN_EXECUTION_TABLES}, {}, {})
        return ({k: (1 if k == "copy_candidates" else 0)
                 for k in FORBIDDEN_EXECUTION_TABLES}, {}, {})
    st._global_execution_counts = _two_phase
    try:
        rc = st.main(["--db-path", str(db.db_path)])
    finally:
        st._global_execution_counts = orig
    assert rc == 0, rc
    out = st._LAST_REPORT  # exact (patched) run; do not re-run main
    # §1 proofs
    assert out["overall_state"] == "RED", out
    w = out["wallets"][0]
    assert w["state"] == "RED", w
    assert w["ready_for_human_review"] is False, w
    assert out["ready_for_human_review_count"] == 0, out
    assert any("execution_artifact_delta:copy_candidates" in r
               for r in w["red_reasons"]), w
    # Baseline / count / delta fields remain accurate.
    assert out["execution_artifact_baseline_counts"]["copy_candidates"] == 0, out
    assert out["execution_artifact_counts"]["copy_candidates"] == 1, out
    assert out["execution_artifact_delta"].get("copy_candidates") == 1, out
    db.close()


def test_status_second_count_failure_exits_1():
    """S6 EXEC-DELTA correction §2: baseline count succeeds but the same
    table's SECOND count raises -> fail closed, CLI exit 1, no normal report,
    zero SQL writes."""
    import importlib
    st = importlib.import_module("specialist_evidence_status")
    db, _ = _open()
    _seed_wallet(db, "uuid-g", "0x" + "g" * 40)
    _seed_watch(db, "wl-g", "uuid-g", last_collection_at=_recent_ts(0))
    _seed_green_evidence(db, "uuid-g", "0x" + "g" * 40)
    # Count using a writable-but-instrumented connection to assert zero writes.
    orig_open = st.open_readonly
    spy = {}
    def _spy_open(p):
        real = orig_open(p)
        class _Spy(DbConn):
            _writes = []
            def execute(self, sql, params=None):
                if str(sql).strip().upper().startswith(
                        ("INSERT", "UPDATE", "DELETE", "REPLACE")):
                    self._writes.append(sql)
                return super().execute(sql, params)
        s = _Spy(real.conn)
        spy["conn"] = s
        return s
    orig = st._global_execution_counts
    calls = {"n": 0}
    def _fail_second(d):
        calls["n"] += 1
        if calls["n"] == 1:
            return ({k: 0 for k in FORBIDDEN_EXECUTION_TABLES}, {}, {})
        # second call: this table's count raises
        raise RuntimeError("injected second count failure: copy_candidates")
    st._global_execution_counts = _fail_second
    st.open_readonly = _spy_open
    st._LAST_REPORT = None  # clear any prior-test report
    try:
        rc = st.main(["--db-path", str(db.db_path)])
    finally:
        st._global_execution_counts = orig
        st.open_readonly = orig_open
    assert rc == 1, rc  # fail closed
    # No normal report emitted (exception propagated before return).
    assert st._LAST_REPORT is None, \
        "expected no normal report after second-count failure"
    # Zero writes on the actual connection used.
    assert spy["conn"]._writes == [], spy["conn"]._writes
    db.close()
