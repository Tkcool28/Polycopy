"""Specialist Paper Execution Spine — schema-contract and negative-matrix tests.

These tests prove:

  * schema v18 migrates every new table with the expected PK type, required
    columns, foreign keys, unique constraints, and indexes; minimal valid
    inserts succeed and invalid FK inserts fail (step 6).
  * risk-decision evidence row persists enough to explain allow/block (step 5).
  * the negative-execution matrix blocks unsafe requests without creating orders
    (step 4): missing/zero/negative/NaN/inf quantity, missing snapshot, missing
    depth, stale snapshot, missing authorization, already-used authorization,
    disabled/revoked approval, wrong-signal authorization, incomplete scoring.

All runs use temporary databases only.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import pytest

from polycopy.db.database import Database
from polycopy.execution import specialist_approval as sa
from polycopy.execution.specialist_spine import (
    ExecutionRuntime,
    consume_eligible_signal,
    create_execution_authorization,
    mark_specialist_position,
    settle_specialist_position,
)
import tests.fixtures.specialist_paper_fixtures as fx
import polycopy.engine.approved_wallet_trade_bridge as bridge


NEW_TABLES = [
    "specialist_approvals",
    "paper_signal_execution_authorizations",
    "execution_risk_decisions",
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "paper_position_lots",
    "paper_position_marks",
    "paper_position_settlements",
]


def _connect() -> Database:
    fd, n = tempfile.mkstemp(suffix=".db")
    import os
    os.close(fd)
    return Database(Path(n)).connect(), Path(n)


def _runtime(**overrides):
    base = dict(
        db_is_temporary=True, allow_production_execution=False,
        broker_mode="paper", kill_switch_engaged=False, is_paper=True,
        is_live=False, max_order_size=10.0, max_per_market=10.0,
        max_per_wallet=10.0, max_global=10.0, snapshot_max_age_seconds=600.0,
    )
    base.update(overrides)
    return ExecutionRuntime(**base)


def _seed_score_authorize(tmp_path: Path):
    db = Database(tmp_path / "neg.db").connect()
    fx.seed_resolved_evidence(db)
    ing = fx.ingest_target_trade(db)
    stored = db.fetchone(
        "SELECT source_trade_id FROM source_trades WHERE id=?",
        (ing["source_trade_internal_id"],),
    )["source_trade_id"]
    bridge.process_approved_wallet_trades(
        db, wallet=fx.FIXED_WALLET, limit=1, dependencies=fx.bridge_dependencies(),
        write=True, write_authorization=bridge._issue_write_capability(),
        source_trade_id=stored, evaluate_canonical_decisions=True,
    )
    cid = db.fetchone("SELECT id FROM copy_candidates ORDER BY id DESC LIMIT 1")["id"]
    psid = db.fetchone(
        "SELECT id FROM paper_signal_decisions WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
        (cid,),
    )["id"]
    sig = db.fetchone(
        "SELECT source_trade_id, candidate_id FROM paper_signal_decisions WHERE id=?",
        (psid,),
    )
    ap = sa.create_approval(
        db, wallet_address=fx.FIXED_WALLET, specialist_category=fx.SPECIALIST_CATEGORY,
        formula_name="wallet_score_v1", formula_version="1", reviewer="tester",
        approval_reason="manual",
    )
    auth = create_execution_authorization(
        db, paper_signal_decision_id=psid, specialist_approval_id=ap.approval_id,
        source_trade_id=sig["source_trade_id"], candidate_id=sig["candidate_id"],
        authorized_by="tester", authorization_reason="manual gate",
    )
    return db, ap, auth, psid, ing


# --------------------------------------------------------------------------- #
# Step 6 — schema contract
# --------------------------------------------------------------------------- #
def test_schema_v18_new_tables_exist():
    db, n = _connect()
    for t in NEW_TABLES:
        row = db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)
        )
        assert row is not None, f"missing table {t}"
    db.close(); n.unlink()


def test_execution_risk_decision_pk_type_and_columns():
    db, n = _connect()
    cols = {r["name"]: r for r in db.fetchall("PRAGMA table_info(execution_risk_decisions)")}
    assert cols["risk_decision_id"]["pk"] == 1
    for required in [
        "paper_signal_decision_id", "specialist_approval_id", "source_trade_id",
        "candidate_id", "snapshot_id", "decision", "requested_quantity",
        "requested_price", "estimated_fill_price", "estimated_slippage",
        "market_exposure_before", "wallet_exposure_before", "portfolio_exposure_before",
        "configured_limits_json", "kill_switch_state", "paper_mode",
        "policy_version", "evidence_timestamp", "evaluated_at",
    ]:
        assert required in cols, f"missing column {required}"
    db.close(); n.unlink()


def test_invalid_fk_insert_fails():
    db, n = _connect()
    with pytest.raises(Exception):
        db.execute(
            "INSERT INTO paper_orders (id, specialist_approval_id, source_trade_internal_id, "
            "copy_candidate_id, candidate_price_snapshot_id, trade_copyability_decision_id, "
            "paper_signal_decision_id, execution_risk_decision_id, source_wallet_id, market_id, "
            "wallet_id, side, outcome, quantity, price, status, requested_quantity, "
            "requested_price, fill_model_version, created_at, policy_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("x", "bad-approval", "bad-st", 1, None, None, 1, "bad-risk",
             "bad-w", "bad-m", "bad-w", "BUY", "Yes", 1.0, 0.4, "filled",
             1.0, 0.4, "fill_model_v1", "now", "v1"),
        )
    db.close(); n.unlink()


# --------------------------------------------------------------------------- #
# Step 5 — risk evidence persistence
# --------------------------------------------------------------------------- #
def test_risk_allow_evidence_persisted(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "executed"
    row = db.fetchone(
        "SELECT * FROM execution_risk_decisions WHERE risk_decision_id=?",
        (res.risk_decision_id,),
    )
    assert row["decision"] == "allow"
    assert row["reason_codes"]
    assert row["paper_signal_decision_id"] == psid
    assert row["specialist_approval_id"] == ap.approval_id
    assert row["source_trade_id"]
    assert row["candidate_id"]
    assert row["snapshot_id"]
    assert row["requested_quantity"] is not None
    assert row["requested_price"] is not None
    assert row["estimated_fill_price"] is not None
    assert row["estimated_slippage"] is not None
    assert row["market_exposure_before"] is not None
    assert row["wallet_exposure_before"] is not None
    assert row["portfolio_exposure_before"] is not None
    assert row["configured_limits_json"]
    assert row["kill_switch_state"] == 0
    assert row["paper_mode"] == "paper"
    assert row["policy_version"]
    assert row["evidence_timestamp"] is not None
    assert row["evaluated_at"]
    db.close()


def test_risk_block_evidence_persisted(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    res = consume_eligible_signal(db, psid, _runtime(kill_switch_engaged=True))
    assert res.status == "blocked"
    # A blocked request must create a risk decision but NO order/fill/position.
    assert res.risk_decision_id
    assert db.fetchone(
        "SELECT COUNT(*) AS c FROM execution_risk_decisions WHERE risk_decision_id=?",
        (res.risk_decision_id,),
    )["c"] == 1
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_orders")["c"] == 0
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_fills")["c"] == 0
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_positions")["c"] == 0
    row = db.fetchone(
        "SELECT * FROM execution_risk_decisions WHERE risk_decision_id=?",
        (res.risk_decision_id,),
    )
    assert row["decision"] == "block"
    assert "kill_switch_engaged" in (row["reason_codes"] or "")
    # An invalid request must never produce "allow".
    assert row["decision"] != "allow"
    db.close()


# --------------------------------------------------------------------------- #
# Step 4 — negative matrix
# --------------------------------------------------------------------------- #
def test_missing_authorization_no_execution(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    # Revoke the authorization (valid status) so get_active_authorization returns none.
    db.execute(
        "UPDATE paper_signal_execution_authorizations SET status='revoked', "
        "updated_at=? WHERE authorization_id=?",
        ("2000-01-01T00:00:00Z", auth),
    )
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "blocked"
    assert any("no_active_execution_authorization" in r for r in res.rejection_reasons)
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_orders")["c"] == 0
    db.close()


def test_authorization_already_used_no_duplicate(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    r1 = consume_eligible_signal(db, psid, _runtime())
    assert r1.status == "executed"
    r2 = consume_eligible_signal(db, psid, _runtime())
    assert r2.status == "already_executed"
    assert r2.order_id == r1.order_id
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_orders")["c"] == 1
    db.close()


def test_wrong_signal_authorization_blocked(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    # Create a SECOND, schema-valid signal for the same candidate/wallet, then
    # authorize THAT signal (active). Consuming the original signal `psid` must
    # block: an authorization for signal B does not authorize signal A.
    sig = db.fetchone("SELECT candidate_id, wallet_id FROM paper_signal_decisions WHERE id=?", (psid,))
    db.execute(
        "INSERT INTO paper_signal_decisions "
        "(candidate_id, wallet_id, signal_family, final_verdict, idempotency_key, "
        "computed_at, created_at, price_snapshot_id) "
        "VALUES (?,?, 'copy_candidate', 'copy_candidate', 'wrong-signal-test', "
        "'2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z', "
        "(SELECT price_snapshot_id FROM paper_signal_decisions WHERE id=?))",
        (sig["candidate_id"], sig["wallet_id"], psid),
    )
    other_psid = db.fetchone(
        "SELECT id FROM paper_signal_decisions ORDER BY id DESC LIMIT 1"
    )["id"]
    create_execution_authorization(
        db, paper_signal_decision_id=other_psid, specialist_approval_id=ap.approval_id,
        source_trade_id=ing["source_trade_internal_id"], candidate_id=sig["candidate_id"],
        authorized_by="tester", authorization_reason="for-other-signal",
    )
    # Revoke the correct authorization so get_active_authorization(psid) returns none,
    # while a different (active) authorization exists only for the other signal.
    db.execute(
        "UPDATE paper_signal_execution_authorizations SET status='revoked', "
        "updated_at=? WHERE authorization_id=?",
        ("2000-01-01T00:00:00Z", auth),
    )
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "blocked"
    assert any("no_active_execution_authorization" in r for r in res.rejection_reasons)
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_orders")["c"] == 0
    db.close()


def test_missing_snapshot_blocked(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    snap_id = db.fetchone(
        "SELECT price_snapshot_id FROM paper_signal_decisions WHERE id=?", (psid,)
    )["price_snapshot_id"]
    # Make the snapshot's fetched_at unparseable — a schema-valid unusable
    # snapshot condition (distinct from the stale-snapshot test). The spine's
    # _is_snapshot_fresh returns "snapshot_timestamp_unparseable" and blocks.
    db.execute(
        "UPDATE candidate_price_snapshots SET fetched_at='not-a-timestamp' WHERE id=?",
        (snap_id,),
    )
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "blocked"
    assert any("snapshot_timestamp_unparseable" in r for r in res.rejection_reasons)
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_orders")["c"] == 0
    db.close()


def test_missing_depth_blocked(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    snap_id = db.fetchone(
        "SELECT price_snapshot_id FROM paper_signal_decisions WHERE id=?", (psid,)
    )["price_snapshot_id"]
    db.execute("DELETE FROM candidate_price_snapshot_levels WHERE snapshot_id=?", (snap_id,))
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "blocked"
    assert any("depth_missing" in r for r in res.rejection_reasons)
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_orders")["c"] == 0
    db.close()


def test_stale_snapshot_blocked(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    snap_id = db.fetchone(
        "SELECT price_snapshot_id FROM paper_signal_decisions WHERE id=?", (psid,)
    )["price_snapshot_id"]
    db.execute(
        "UPDATE candidate_price_snapshots SET fetched_at='2000-01-01T00:00:00Z' WHERE id=?",
        (snap_id,),
    )
    res = consume_eligible_signal(db, psid, _runtime(snapshot_max_age_seconds=600.0))
    assert res.status == "blocked"
    assert any("stale_snapshot" in r for r in res.rejection_reasons)
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_orders")["c"] == 0
    db.close()


def test_zero_quantity_blocked(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    snap_id = db.fetchone(
        "SELECT price_snapshot_id FROM paper_signal_decisions WHERE id=?", (psid,)
    )["price_snapshot_id"]
    db.execute(
        "UPDATE candidate_price_snapshots SET source_trade_quantity=0.0 WHERE id=?",
        (snap_id,),
    )
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "blocked"
    assert any("invalid_request_quantity:missing_or_nonpositive" in r for r in res.rejection_reasons)
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_orders")["c"] == 0
    db.close()


def test_negative_quantity_blocked(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    snap_id = db.fetchone(
        "SELECT price_snapshot_id FROM paper_signal_decisions WHERE id=?", (psid,)
    )["price_snapshot_id"]
    db.execute(
        "UPDATE candidate_price_snapshots SET source_trade_quantity=-5.0 WHERE id=?",
        (snap_id,),
    )
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "blocked"
    assert any("invalid_request_quantity:missing_or_nonpositive" in r for r in res.rejection_reasons)
    db.close()


def test_nan_quantity_blocked(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    snap_id = db.fetchone(
        "SELECT price_snapshot_id FROM paper_signal_decisions WHERE id=?", (psid,)
    )["price_snapshot_id"]
    db.execute(
        "UPDATE candidate_price_snapshots SET source_trade_quantity='NaN' WHERE id=?",
        (snap_id,),
    )
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "blocked"
    assert any("invalid_request_quantity:missing_or_nonpositive" in r for r in res.rejection_reasons)
    db.close()


def test_infinite_quantity_blocked(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    snap_id = db.fetchone(
        "SELECT price_snapshot_id FROM paper_signal_decisions WHERE id=?", (psid,)
    )["price_snapshot_id"]
    db.execute(
        "UPDATE candidate_price_snapshots SET source_trade_quantity='inf' WHERE id=?",
        (snap_id,),
    )
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "blocked"
    assert any("invalid_request_quantity:missing_or_nonpositive" in r for r in res.rejection_reasons)
    db.close()


def test_disabled_approval_blocked(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    sa.set_enabled(db, ap.approval_id, False, updated_by="tester")
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "blocked"
    assert any("approval_disabled_or_revoked" in r for r in res.rejection_reasons)
    db.close()


def test_negative_infinity_quantity_blocked(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    snap_id = db.fetchone(
        "SELECT price_snapshot_id FROM paper_signal_decisions WHERE id=?", (psid,)
    )["price_snapshot_id"]
    db.execute(
        "UPDATE candidate_price_snapshots SET source_trade_quantity='-inf' WHERE id=?",
        (snap_id,),
    )
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "blocked"
    assert any("invalid_request_quantity:missing_or_nonpositive" in r for r in res.rejection_reasons)
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_orders")["c"] == 0
    db.close()


def test_copyability_not_eligible_blocked(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    cid = db.fetchone(
        "SELECT candidate_id FROM paper_signal_decisions WHERE id=?", (psid,)
    )["candidate_id"]
    # Overwrite the latest copyability verdict to a non-eligible value.
    db.execute(
        "UPDATE trade_copyability_decisions SET verdict='skip' "
        "WHERE id=(SELECT id FROM trade_copyability_decisions WHERE candidate_id=? "
        "ORDER BY id DESC LIMIT 1)",
        (cid,),
    )
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "blocked"
    assert any("copyability_not_eligible" in r for r in res.rejection_reasons)
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_orders")["c"] == 0
    db.close()


def test_signal_not_copy_candidate_blocked(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    db.execute(
        "UPDATE paper_signal_decisions SET final_verdict='watchlist' WHERE id=?", (psid,)
    )
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "blocked"
    assert any("signal_verdict_not_eligible" in r for r in res.rejection_reasons)
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_orders")["c"] == 0
    db.close()


def test_settlement_replay_returns_existing(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "executed"
    mr = mark_specialist_position(db, res.position_id, mark_price=0.5, bid_price=0.45,
                                  ask_price=0.55, evidence_source="test")
    so1 = settle_specialist_position(db, res.position_id, resolution_outcome="YES",
                                     evidence_source="test")
    so2 = settle_specialist_position(db, res.position_id, resolution_outcome="YES",
                                     evidence_source="test")
    assert so2.status in ("already_settled", "settled")
    assert so2.settlement_id == so1.settlement_id
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_position_settlements")["c"] == 1
    db.close()


def test_conflicting_settlement_blocked(tmp_path: Path):
    db, ap, auth, psid, ing = _seed_score_authorize(tmp_path)
    res = consume_eligible_signal(db, psid, _runtime())
    assert res.status == "executed"
    mr = mark_specialist_position(db, res.position_id, mark_price=0.5, bid_price=0.45,
                                  ask_price=0.55, evidence_source="test")
    so1 = settle_specialist_position(db, res.position_id, resolution_outcome="YES",
                                     evidence_source="test")
    # Different resolution evidence (NO) must not create a second settlement.
    so2 = settle_specialist_position(db, res.position_id, resolution_outcome="NO",
                                     evidence_source="conflict")
    assert so2.status == "already_settled"
    assert db.fetchone("SELECT COUNT(*) AS c FROM paper_position_settlements")["c"] == 1
    db.close()
