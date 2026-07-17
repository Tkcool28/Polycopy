"""PR #70 focused review-fix tests — the four review defects.

Bounded, deterministic tests against a temp (v20) DB. No production DB is
touched. Each test exercises one of the four fixes:

  1. Outcome provenance is carried from the source trade (never hardcoded "Yes").
  2. A blocked execution attempt can be retried (multiple immutable risk rows).
  3. Conflicting settlement evidence returns "conflict" (no second settlement).
  4. Exposure queries exclude settled positions via explicit SQL aliases.

Plus a v20 migration-preservation test.
"""
from __future__ import annotations

from pathlib import Path

from polycopy.db.database import Database
from polycopy.execution.specialist_spine import (
    consume_eligible_signal,
    create_execution_authorization,
    settle_specialist_position,
    _current_exposure,
)
from polycopy.engine.approved_specialist_dispatcher import dispatch_one
from tests.fixtures.specialist_paper_fixtures import (
    bridge_dependencies,
    create_approval_for_target,
    ingest_target_trade,
    make_target_trade,
    seed_resolved_evidence,
    paper_runtime,
)

_REPO = Path(__file__).resolve().parents[1]
for _c in (_REPO / "src", _REPO / "scripts"):
    if str(_c) not in __import__("sys").path:
        __import__("sys").path.insert(0, str(_c))


# --------------------------------------------------------------------------- #
# Shared harness                                                               #
# --------------------------------------------------------------------------- #
def _make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "pass4.db").connect()
    seed_resolved_evidence(db)
    db.commit()
    return db


def _full_chain(db: Database, *, outcome: str = "Yes"):
    aid = create_approval_for_target(db)
    # Build the target trade with the EXACT canonical outcome. The bridge now
    # accepts both binary outcomes (Yes/No) through the real dispatch path, so
    # a BUY-No trade hydrates to the No outcome honestly — no post-dispatch
    # outcome mutation is performed or tolerated.
    trade = make_target_trade(outcome=outcome)
    ing = ingest_target_trade(db, trade=trade)
    target_stid = trade["source_trade_id"]
    target_iid = ing["source_trade_internal_id"]
    deps = bridge_dependencies()
    res = dispatch_one(
        db, approval_id=aid, source_trade_internal_id=target_iid,
        gamma_resolver=deps.gamma.get_market, clob_provider=deps.clob, dry_run=False,
    )
    db.commit()
    assert res.status == "execution_pending", (res.status, res.reason_codes)
    return aid, res, target_stid, target_iid


def _authorize(db: Database, aid, disp):
    auth_id = create_execution_authorization(
        db, paper_signal_decision_id=disp.paper_signal_decision_id,
        specialist_approval_id=aid, source_trade_id=disp.source_trade_internal_id,
        candidate_id=disp.candidate_id, authorized_by="op",
        authorization_reason="vetted", policy_version="specialist_paper_execution_v1",
    )
    db.commit()
    return auth_id


def _execute(db: Database, disp, *, kill_switch: bool = False):
    runtime = paper_runtime(allow=True, kill_switch=kill_switch)
    ex = consume_eligible_signal(db, disp.paper_signal_decision_id, runtime)
    db.commit()
    return ex


def _count(db: Database, table: str, where: str = "") -> int:
    sql = f"SELECT COUNT(*) AS c FROM {table}" + (f" WHERE {where}" if where else "")
    return int(db.fetchone(sql)["c"])


def _risk_rows(db: Database, psd: int) -> list[dict]:
    rows = db.fetchall(
        "SELECT * FROM execution_risk_decisions WHERE paper_signal_decision_id=? "
        "ORDER BY attempt_number ASC",
        (psd,),
    )
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Fix 1 — outcome provenance                                                   #
# --------------------------------------------------------------------------- #
def test_no_outcome_lifecycle(tmp_path):
    db = _make_db(tmp_path)
    aid, disp, _stid, _iid = _full_chain(db, outcome="No")
    _authorize(db, aid, disp)
    ex = _execute(db, disp)
    assert ex.status == "executed", ex.rejection_reasons
    psd = disp.paper_signal_decision_id

    order = db.fetchone(
        "SELECT outcome FROM paper_orders WHERE paper_signal_decision_id=?", (psd,))
    pos = db.fetchone(
        "SELECT outcome FROM paper_positions WHERE paper_signal_decision_id=?", (psd,))
    assert order["outcome"] == "No", "order outcome must carry source-trade outcome"
    assert pos["outcome"] == "No", "position outcome must carry source-trade outcome"

    # Resolution NO = win for a No position.
    so = settle_specialist_position(
        db, ex.position_id, resolution_outcome="No",
        evidence_source="authoritative")
    db.commit()
    assert so.status == "settled", so
    assert so.is_winner is True, "No position resolved NO must win"
    assert so.realized_pnl is not None and so.realized_pnl > 0

    # Resolution YES = loss for a No position (re-settle equivalent replay).
    so2 = settle_specialist_position(
        db, ex.position_id, resolution_outcome="Yes",
        evidence_source="authoritative")
    assert so2.status == "conflict", "already-settled No position cannot flip to YES"
    assert so2.realized_pnl == so.realized_pnl


def test_yes_outcome_still_works(tmp_path):
    db = _make_db(tmp_path)
    aid, disp, _stid, _iid = _full_chain(db, outcome="Yes")
    _authorize(db, aid, disp)
    ex = _execute(db, disp)
    assert ex.status == "executed", ex.rejection_reasons
    psd = disp.paper_signal_decision_id

    order = db.fetchone(
        "SELECT outcome FROM paper_orders WHERE paper_signal_decision_id=?", (psd,))
    pos = db.fetchone(
        "SELECT outcome FROM paper_positions WHERE paper_signal_decision_id=?", (psd,))
    assert order["outcome"] == "Yes"
    assert pos["outcome"] == "Yes"

    so = settle_specialist_position(
        db, ex.position_id, resolution_outcome="Yes", evidence_source="authoritative")
    db.commit()
    assert so.status == "settled" and so.is_winner is True


def test_buy_no_true_end_to_end(tmp_path):
    """Real full-pipeline BUY-No: approved specialist -> canonical source trade
    side=BUY outcome=No -> enrichment -> durable dispatch -> bridge candidate
    -> wallet/category/copyability decisions -> paper signal -> execution auth
    -> paper order outcome=No -> paper position outcome=No -> settlement NO=win.

    No post-dispatch outcome mutation. Asserts the exact artifact counts.
    """
    db = _make_db(tmp_path)
    aid, disp, _stid, _iid = _full_chain(db, outcome="No")
    assert disp.paper_signal_verdict == "copy_candidate"
    psd = disp.paper_signal_decision_id

    # ── Required counts after dispatch + bridge (target trade scoped) ──
    assert _count(db, "source_trades", f"id='{_iid}'") == 1
    assert _count(db, "source_trade_enrichments") == 1
    assert _count(db, "approved_specialist_trade_dispatches") == 1
    assert _count(db, "copy_candidates") == 1
    assert _count(db, "candidate_price_snapshots") == 1
    assert _count(db, "wallet_score_decisions") == 1
    assert _count(db, "category_wallet_score_decisions") == 1
    assert _count(db, "trade_copyability_decisions") == 1
    assert _count(db, "paper_signal_decisions") == 1

    auth_id = _authorize(db, aid, disp)
    assert auth_id is not None

    ex = _execute(db, disp)
    assert ex.status == "executed", ex.rejection_reasons
    assert ex.position_id is not None
    psd = disp.paper_signal_decision_id
    # The authorization's pre-execution risk decision is created at execution.
    assert _count(db, "execution_risk_decisions",
                  f"paper_signal_decision_id={psd}") == 1
    assert _count(db, "paper_fills") == 1
    assert _count(db, "paper_positions", f"paper_signal_decision_id={psd}") == 1
    assert _count(db, "paper_position_lots",
                  f"position_id='{ex.position_id}'") == 1

    # ── Outcome provenance carried honestly through the spine ──
    order = db.fetchone(
        "SELECT outcome FROM paper_orders WHERE paper_signal_decision_id=?", (psd,))
    pos = db.fetchone(
        "SELECT outcome FROM paper_positions WHERE paper_signal_decision_id=?", (psd,))
    assert order["outcome"] == "No", "paper order must carry source-trade No"
    assert pos["outcome"] == "No", "paper position must carry source-trade No"

    # ── Settlement NO = win for a No position ──
    so = settle_specialist_position(
        db, ex.position_id, resolution_outcome="No", evidence_source="authoritative")
    db.commit()
    assert so.status == "settled", so
    assert so.is_winner is True, "No position resolved NO must win"
    assert so.realized_pnl is not None and so.realized_pnl > 0
    assert _count(db, "paper_position_settlements",
                  f"position_id='{ex.position_id}'") == 1

    # ── Settlement YES = LOSS for a No position (no second row) ──
    so2 = settle_specialist_position(
        db, ex.position_id, resolution_outcome="Yes", evidence_source="authoritative")
    assert so2.status == "conflict", "No position cannot flip to YES"
    assert so2.realized_pnl == so.realized_pnl, "conflict must preserve original P&L"
    assert _count(db, "paper_position_settlements",
                  f"position_id='{ex.position_id}'") == 1


def test_buy_no_replay_no_duplicates(tmp_path):
    """Replaying the same BUY-No source trade must not duplicate any artifact."""
    db = _make_db(tmp_path)
    aid, disp, _stid, _iid = _full_chain(db, outcome="No")
    _authorize(db, aid, disp)
    first = _execute(db, disp)
    assert first.status == "executed", first.rejection_reasons
    psd = disp.paper_signal_decision_id

    # Replay the SAME BUY-No source trade -> idempotent, no new artifacts.
    deps = bridge_dependencies()
    replay_disp = dispatch_one(
        db, approval_id=aid, source_trade_internal_id=_iid,
        gamma_resolver=deps.gamma.get_market, clob_provider=deps.clob, dry_run=False,
    )
    assert replay_disp.status == "execution_pending"
    replay = _execute(db, replay_disp)
    assert replay.status == "already_executed"
    assert _count(db, "paper_orders", f"paper_signal_decision_id={psd}") == 1
    assert _count(db, "paper_positions", f"paper_signal_decision_id={psd}") == 1
    assert _count(db, "paper_fills") == 1
    assert _count(db, "copy_candidates") == 1
    assert _count(db, "source_trades", f"id='{_iid}'") == 1


# --------------------------------------------------------------------------- #
# Fix 2 — retryable blocked execution                                          #
# --------------------------------------------------------------------------- #
def test_blocked_risk_retry_succeeds(tmp_path):
    db = _make_db(tmp_path)
    aid, disp, _stid, _iid = _full_chain(db)
    auth_id = _authorize(db, aid, disp)
    psd = disp.paper_signal_decision_id

    # First attempt: kill switch ON -> blocked, one risk row, no order.
    blocked = _execute(db, disp, kill_switch=True)
    assert blocked.status == "blocked"
    assert _count(db, "paper_orders", f"paper_signal_decision_id={psd}") == 0
    assert _count(db, "execution_risk_decisions", f"paper_signal_decision_id={psd}") == 1
    first = _risk_rows(db, psd)[0]
    assert first["decision"] == "block"
    assert first["authorization_id"] == auth_id

    # Second attempt: kill switch OFF -> retry same active authorization.
    allowed = _execute(db, disp, kill_switch=False)
    assert allowed.status == "executed", allowed.rejection_reasons
    assert _count(db, "execution_risk_decisions", f"paper_signal_decision_id={psd}") == 2
    assert _count(db, "paper_orders", f"paper_signal_decision_id={psd}") == 1
    assert _count(db, "paper_fills") == 1
    assert _count(db, "paper_positions", f"paper_signal_decision_id={psd}") == 1
    rows = _risk_rows(db, psd)
    assert rows[1]["decision"] == "allow"
    assert rows[1]["authorization_id"] == auth_id
    assert rows[1]["attempt_number"] == 2
    # Exactly-once: a third execute does not create another order.
    third = _execute(db, disp, kill_switch=False)
    assert third.status == "already_executed"
    assert _count(db, "paper_orders", f"paper_signal_decision_id={psd}") == 1


def test_stale_snapshot_retry_succeeds(tmp_path):
    db = _make_db(tmp_path)
    aid, disp, _stid, _iid = _full_chain(db)
    _authorize(db, aid, disp)
    psd = disp.paper_signal_decision_id
    snap_id = db.fetchone(
        "SELECT price_snapshot_id FROM paper_signal_decisions WHERE id=?", (psd,)
    )["price_snapshot_id"]

    # Stale snapshot -> blocked.
    db.execute(
        "UPDATE candidate_price_snapshots SET fetched_at='2000-01-01T00:00:00Z' "
        "WHERE id=?", (snap_id,))
    db.commit()
    blocked = _execute(db, disp)
    assert blocked.status == "blocked"
    assert _count(db, "paper_orders", f"paper_signal_decision_id={psd}") == 0

    # Refresh snapshot -> retry -> execute.
    db.execute(
        "UPDATE candidate_price_snapshots SET fetched_at="
        "(SELECT created_at FROM candidate_price_snapshots WHERE id=?) WHERE id=?",
        (snap_id, snap_id))
    db.commit()
    allowed = _execute(db, disp)
    assert allowed.status == "executed", allowed.rejection_reasons
    assert _count(db, "execution_risk_decisions", f"paper_signal_decision_id={psd}") == 2
    assert _count(db, "paper_orders", f"paper_signal_decision_id={psd}") == 1
    assert _count(db, "paper_fills") == 1
    assert _count(db, "paper_positions", f"paper_signal_decision_id={psd}") == 1


# --------------------------------------------------------------------------- #
# Fix 3 — settlement conflict detection                                        #
# --------------------------------------------------------------------------- #
def _run_lifecycle_to_position(db, outcome: str = "Yes"):
    aid, disp, _stid, _iid = _full_chain(db, outcome=outcome)
    _authorize(db, aid, disp)
    ex = _execute(db, disp)
    assert ex.status == "executed", ex.rejection_reasons
    return disp, ex


def test_settlement_conflict_yes_position(tmp_path):
    db = _make_db(tmp_path)
    disp, ex = _run_lifecycle_to_position(db, outcome="Yes")
    # First settlement: YES -> settled.
    so1 = settle_specialist_position(
        db, ex.position_id, resolution_outcome="Yes", evidence_source="authoritative")
    db.commit()
    assert so1.status == "settled"
    # Conflicting: YES then NO -> conflict, one settlement row, P&L unchanged.
    so2 = settle_specialist_position(
        db, ex.position_id, resolution_outcome="No", evidence_source="authoritative")
    assert so2.status == "conflict"
    assert so2.realized_pnl == so1.realized_pnl
    assert _count(db, "paper_position_settlements",
                  f"position_id='{ex.position_id}'") == 1
    # Equivalent replay: YES again -> already_settled.
    so3 = settle_specialist_position(
        db, ex.position_id, resolution_outcome="yes", evidence_source="another-source")
    assert so3.status == "already_settled"
    assert so3.realized_pnl == so1.realized_pnl


def test_settlement_conflict_no_position(tmp_path):
    db = _make_db(tmp_path)
    disp, ex = _run_lifecycle_to_position(db, outcome="No")
    so1 = settle_specialist_position(
        db, ex.position_id, resolution_outcome="No", evidence_source="authoritative")
    db.commit()
    assert so1.status == "settled"
    so2 = settle_specialist_position(
        db, ex.position_id, resolution_outcome="Yes", evidence_source="authoritative")
    assert so2.status == "conflict"
    assert _count(db, "paper_position_settlements",
                  f"position_id='{ex.position_id}'") == 1
    so3 = settle_specialist_position(
        db, ex.position_id, resolution_outcome="NO", evidence_source="authoritative")
    assert so3.status == "already_settled"


# --------------------------------------------------------------------------- #
# Fix 4 — exposure excludes settled positions                                  #
# --------------------------------------------------------------------------- #
def test_exposure_excludes_settled_positions(tmp_path):
    db = _make_db(tmp_path)
    disp, ex = _run_lifecycle_to_position(db, outcome="Yes")
    pid = ex.position_id
    pos = db.fetchone(
        "SELECT quantity, avg_entry_price, wallet_id, market_id FROM paper_positions "
        "WHERE id=?", (pid,))
    notional = float(pos["quantity"]) * float(pos["avg_entry_price"])
    wallet = pos["wallet_id"]
    market = pos["market_id"]

    # OPEN position is counted in exposure.
    exp_open = _current_exposure(db, wallet, market)
    assert exp_open["per_wallet"] == notional, exp_open
    assert exp_open["per_market"] == notional, exp_open
    assert exp_open["global"] == notional, exp_open

    # Settle the position -> it must be EXCLUDED from exposure.
    settle_specialist_position(
        db, pid, resolution_outcome="Yes", evidence_source="authoritative")
    db.commit()
    exp_settled = _current_exposure(db, wallet, market)
    assert exp_settled["per_wallet"] == 0.0, exp_settled
    assert exp_settled["per_market"] == 0.0, exp_settled
    assert exp_settled["global"] == 0.0, exp_settled


# --------------------------------------------------------------------------- #
# Migration — v20 preserves existing risk decisions                            #
# --------------------------------------------------------------------------- #
def test_v20_migration_preserves_risk_decisions(tmp_path):
    # Build a REAL chain on a single DB file so the v19 risk row references valid
    # parent rows (paper_signal_decisions, copy_candidates, specialist_approvals),
    # then rebuild the table to the v19 (physical) shape and force _meta=19 so the
    # runner genuinely replays the v20 migration (which rebuilds the table).
    db_path = tmp_path / "mig.db"
    db = Database(db_path).connect()
    seed_resolved_evidence(db)
    aid, disp, _stid, _iid = _full_chain(db)
    _authorize(db, aid, disp)
    psd = disp.paper_signal_decision_id
    cand_id = disp.candidate_id
    # Capture a real source trade id for the risk row.
    st_id = db.fetchone(
        "SELECT source_trade_id FROM paper_signal_decisions WHERE id=?",
        (psd,))["source_trade_id"]
    snapshot_id = db.fetchone(
        "SELECT price_snapshot_id FROM paper_signal_decisions WHERE id=?",
        (psd,))["price_snapshot_id"]

    # Drop the v20-shaped table + its indexes, recreate the v19 (v18) shape.
    db.execute("DROP TABLE IF EXISTS execution_risk_decisions")
    db.execute("DROP INDEX IF EXISTS idx_execution_risk_authz")
    db.execute("DROP INDEX IF EXISTS idx_execution_risk_attempt")
    db.execute(
        """CREATE TABLE execution_risk_decisions (
               risk_decision_id TEXT PRIMARY KEY,
               paper_signal_decision_id INTEGER NOT NULL,
               specialist_approval_id TEXT,
               source_trade_id TEXT NOT NULL,
               candidate_id INTEGER NOT NULL,
               snapshot_id TEXT,
               decision TEXT NOT NULL,
               reason_codes TEXT,
               requested_quantity REAL, requested_price REAL,
               estimated_fill_price REAL, estimated_slippage REAL,
               market_exposure_before REAL NOT NULL DEFAULT 0,
               wallet_exposure_before REAL NOT NULL DEFAULT 0,
               portfolio_exposure_before REAL NOT NULL DEFAULT 0,
               configured_limits_json TEXT, kill_switch_state INTEGER NOT NULL,
               paper_mode TEXT NOT NULL, evidence_timestamp TEXT,
               evaluated_at TEXT NOT NULL, policy_version TEXT NOT NULL,
               UNIQUE(paper_signal_decision_id));""")
    import uuid as _uuid
    rid = str(_uuid.uuid4())
    db.execute(
        "INSERT INTO execution_risk_decisions (risk_decision_id, paper_signal_decision_id, "
        "specialist_approval_id, source_trade_id, candidate_id, snapshot_id, decision, "
        "reason_codes, market_exposure_before, wallet_exposure_before, "
        "portfolio_exposure_before, kill_switch_state, paper_mode, evaluated_at, "
        "policy_version) "
        "VALUES (?,?,?,?,?,?,?,?,0,0,0,0,'paper','2026-01-01T00:00:00Z','v1')",
        (rid, psd, aid, st_id, cand_id, snapshot_id, "allow", "[]"))
    db.execute("UPDATE _meta SET value='19' WHERE key='schema_version'")
    db.commit()
    db.close()

    # Reopen -> migrate v19 -> v20.
    db2 = Database(tmp_path / "mig.db").connect()
    version = db2.fetchone("SELECT value FROM _meta WHERE key='schema_version'")["value"]
    assert int(version) == 20, f"expected schema 20 after migration, got {version}"

    # Risk row preserved, with new attempt identity columns populated.
    row = db2.fetchone("SELECT * FROM execution_risk_decisions WHERE risk_decision_id=?", (rid,))
    assert row is not None
    assert row["execution_attempt_id"] == rid, "migrated attempt PK reuses original risk id"
    assert row["authorization_id"] is None
    assert int(row["attempt_number"]) == 1
    # No FK violations after rebuild (the order->risk FK relationship is proven
    # by the lifecycle tests, which create paper_orders against v20 risk rows).
    fk = list(db2.conn.execute("PRAGMA foreign_key_check"))
    assert len(fk) == 0, f"foreign_key_check reported {len(fk)} violations: {fk}"
    db2.close()
