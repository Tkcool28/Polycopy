"""T7 (evaluate) + T8 (status) tests (plan Tasks 10/11).

Temp/scratch DBs only. Never opens production.

T7: drive scripts/evaluate_specialist_evidence_watchlist.py (frozen
scorer, reused unchanged). Assert taxonomy enables category aggregation,
unresolved evidence stays 'incomplete', frozen thresholds unchanged,
no auto-approval, idempotent replay, no category row without a
supported taxonomy label.

T8: drive scripts/specialist_evidence_status.py (read-only). Assert
RED on sample wallet in cohort, YELLOW on accumulating evidence,
GREEN when matching copy_candidate decisions exist, RED on an
unexpected execution artifact.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.canonical_metadata import (  # noqa: E402
    build_canonical_metadata,
)
from polycopy.scoring.wallet_evidence import (  # noqa: E402
    resolve_wallet_score_v1,
)


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


def _seed_watch(db, wid, wallet_id, status="active", is_sample=0):
    db.conn.execute(
        "INSERT INTO specialist_evidence_watchlist("
        "id, wallet_id, status, source, reason, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (wid, wallet_id, status, "manual", "seed", "t",
         "2026-01-01T00:00:00Z"),
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


def _seed_trade(db, tid, cond=GCOND, meta=None, side="BUY"):
    if meta is None:
        meta = build_canonical_metadata({}, GAMMA)
    db.conn.execute(
        "INSERT INTO source_trades("
        "id, source, source_trade_id, market_source_id, side, outcome, "
        "quantity, price, trader_address, timestamp, is_sample, "
        "metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, "polymarket", tid, cond, side, "Yes", 10.0, 0.40,
         "0xeval00000000000000000000000000000abc",
         "2026-02-01T00:00:00Z", 0,
         json.dumps(meta, sort_keys=True)),
    )
    db.conn.commit()


WID = "uuid-eval"
ADDR = "0xeval00000000000000000000000000000abc"
WRITE = ["--write", "--allow-live", "--confirm-production-db"]


# ── T7: evaluate ────────────────────────────────────────────────────────────
def test_evaluate_taxonomy_enables_category_aggregation():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_trade(db, "polymarket:st1")  # has taxonomy via GAMMA
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), *WRITE, "--wallet-id", WID])
    assert rc == 0, rc
    cats = db.conn.execute(
        "SELECT category_label, verdict FROM category_wallet_score_decisions "
        "WHERE wallet_id=?", (WID,)
    ).fetchall()
    assert len(cats) == 1, cats
    assert dict(cats[0])["category_label"] == "politics", cats
    db.close()


def test_evaluate_unresolved_stays_incomplete():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_trade(db, "polymarket:st1")  # 0 resolved markets
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), *WRITE, "--wallet-id", WID])
    assert rc == 0, rc
    row = db.conn.execute(
        "SELECT verdict FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,),
    ).fetchone()
    assert dict(row)["verdict"] == "incomplete", row
    db.close()


def test_evaluate_no_auto_approval():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_trade(db, "polymarket:st1")
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), *WRITE, "--wallet-id", WID])
    assert rc == 0, rc
    assert db.conn.execute(
        "SELECT COUNT(*) FROM specialist_approvals").fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM approved_specialist_trade_dispatches"
    ).fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0
    db.close()


def test_evaluate_replay_idempotent():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_trade(db, "polymarket:st1")
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    for _ in range(2):
        rc = ev.main(
            ["--db-path", str(db.db_path), *WRITE, "--wallet-id", WID])
        assert rc == 0, rc
    assert db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0] == 1
    db.close()


def test_evaluate_frozen_threshold_unchanged():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_trade(db, "polymarket:st1")
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), *WRITE, "--wallet-id", WID])
    assert rc == 0, rc
    # Direct scorer call must match the persisted decision's score.
    direct = resolve_wallet_score_v1(
        db, WID, cutoff_timestamp=None, persist=False, now=None)
    row = db.conn.execute(
        "SELECT final_score FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()
    assert abs(dict(row)["final_score"] - direct.result.score) < 1e-6
    db.close()


def test_evaluate_no_category_row_without_taxonomy():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    # Trade with NO taxonomy (unavailable) -> no category decision.
    _seed_trade(db, "polymarket:st1", meta={"foo": "bar"})
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    rc = ev.main(["--db-path", str(db.db_path), *WRITE, "--wallet-id", WID])
    assert rc == 0, rc
    n = db.conn.execute(
        "SELECT COUNT(*) FROM category_wallet_score_decisions "
        "WHERE wallet_id=?", (WID,)).fetchone()[0]
    assert n == 0, n
    db.close()


# ── T8: status ──────────────────────────────────────────────────────────────
def test_status_red_on_sample_wallet_in_cohort():
    db, _ = _open()
    _seed_wallet(db, "uuid-sample", "0xsample00000000000000000000000000000abc",
                is_sample=1)
    _seed_watch(db, "wl-s", "uuid-sample")
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    assert out["overall_state"] == "RED", out
    db.close()


def test_status_yellow_accumulating():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_trade(db, "polymarket:st1")
    # Recent collection -> not stale.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute(
        "UPDATE specialist_evidence_watchlist SET last_collection_at=? "
        "WHERE id='wl-e'", (now,))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    # No red reasons, not green -> yellow.
    assert out["overall_state"] == "YELLOW", out
    assert not out["wallets"][0].get("red_reasons"), out
    db.close()

def test_status_green_on_matching_copy_candidate():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    _seed_trade(db, "polymarket:st1")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute(
        "UPDATE specialist_evidence_watchlist SET last_collection_at=? "
        "WHERE id='wl-e'", (now,))
    # Simulate a prior evaluate producing copy_candidate decisions.
    db.conn.execute(
        "INSERT INTO wallet_score_decisions("
        "wallet_id, formula_name, formula_version, idempotency_key, "
        "final_score, verdict, computed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (WID, "wallet_score_v1", "1", "k1", 80.0, "copy_candidate",
         now, now))
    db.conn.execute(
        "INSERT INTO category_wallet_score_decisions("
        "wallet_id, category_label, formula_name, formula_version, "
        "idempotency_key, final_score, verdict, computed_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (WID, "politics", "category_wallet_score_v1", "1", "ck1", 80.0,
         "copy_candidate", now, now))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    assert out["overall_state"] == "GREEN", out
    db.close()


def test_status_red_on_execution_artifact():
    db, _ = _open()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-e", WID)
    # Unexpected execution-plane artifact -> RED.
    db.conn.execute(
        "INSERT INTO specialist_approvals("
        "approval_id, wallet_address, specialist_category, formula_name, "
        "formula_version, reviewer, approved_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("ap1", ADDR, "politics", "wallet_score", "1", "t",
         "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
         "2026-01-01T00:00:00Z"))
    db.conn.commit()
    st = _load("specialist_evidence_status.py")
    rc = st.main(["--db-path", str(db.db_path)])
    assert rc == 0, rc
    out = _status_json(st, db)
    assert out["overall_state"] == "RED", out
    db.close()


def _status_json(st, db):
    import io
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        st.main(["--db-path", str(db.db_path), "--json"])
    finally:
        sys.stdout = old
    return json.loads(buf.getvalue())
