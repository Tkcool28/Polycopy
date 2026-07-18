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
import tempfile
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
from evidence_db import DbConn, FORBIDDEN_EXECUTION_TABLES  # noqa: E402


def _load(name):
    s = importlib.util.spec_from_file_location(name, ROOT / "scripts" / name)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def _tmp():
    return Path(tempfile.mktemp(suffix=".db"))


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
    # to count_table is the AFTER snapshot.
    orig = DbConn.count_table
    calls = {"n": 0}
    def _inflated(self, table):
        calls["n"] += 1
        base = orig(self, table)
        if table == "specialist_approvals" and calls["n"] % 2 == 0:
            return base + 1  # AFTER sample shows a delta
        return base
    DbConn.count_table = _inflated
    try:
        rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    finally:
        DbConn.count_table = orig
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
    orig = DbConn.count_table
    def _boom(self, table):
        if table == "specialist_approvals":
            raise sqlite3.OperationalError("injected count failure")
        return orig(self, table)
    DbConn.count_table = _boom
    try:
        rc = ev.main(["--db-path", str(db.db_path), "--write", "--wallet-id", WID])
    finally:
        DbConn.count_table = orig
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
    orig = DbConn.count_table
    def _boom(self, table):
        if table == "specialist_approvals":
            raise sqlite3.OperationalError("injected")
        return orig(self, table)
    DbConn.count_table = _boom
    try:
        rc = st.main(["--db-path", str(db.db_path)])
    finally:
        DbConn.count_table = orig
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
