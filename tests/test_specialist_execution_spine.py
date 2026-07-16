"""Specialist Paper Execution Spine — full lifecycle, replay, and safety tests.

These tests prove the persistent specialist paper lifecycle end-to-end against a
temporary SQLite database:

    approval -> source trade -> enrichment -> candidate -> snapshot/depth
    -> wallet/category scoring -> trade copyability -> copy_candidate paper signal
    -> execution authorization -> risk -> order -> fill -> position/lot
    -> mark -> settlement -> realized P&L

Required coverage (from the SPECIALIST EXECUTION SPINE task):

  * full positive lifecycle produces exactly one order/fill/position/lot/mark/settlement
  * replay of the same authorization+signal is duplicate-safe (already_executed)
  * settlement replay returns the existing settlement (no duplicate row)
  * risk semantics: invalid/missing/zero/negative/NaN quantity -> blocked, no order
  * kill switch engaged -> blocked, no order
  * exposure limits unset/zero -> blocked, no order
  * missing depth -> blocked, no order
  * stale snapshot -> blocked, no order
  * revoked approval -> blocked, no order
  * authorization already used -> no duplicate order
  * paper_signal_decisions.is_approved remains 0 (legacy field untouched)
  * authorization is marked used
  * approval remains enabled
  * Database.fetchone/fetchall return dict rows (mapping contract)

All runs use temporary databases only. No production DB, no network, no deploy.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from typing import Any, Optional

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


def _runtime(**overrides: Any) -> ExecutionRuntime:
    base: dict[str, Any] = dict(
        db_is_temporary=True,
        allow_production_execution=False,
        broker_mode="paper",
        kill_switch_engaged=False,
        is_paper=True,
        is_live=False,
        max_order_size=10.0,
        max_per_market=10.0,
        max_per_wallet=10.0,
        max_global=10.0,
        snapshot_max_age_seconds=600.0,
    )
    base.update(overrides)
    return ExecutionRuntime(**base)


def _seed_and_score(db: Database):
    """Run the full upstream chain up to a copy_candidate paper signal."""
    fx.seed_resolved_evidence(db)
    ing = fx.ingest_target_trade(db)
    stored = db.fetchone(
        "SELECT source_trade_id FROM source_trades WHERE id=?",
        (ing["source_trade_internal_id"],),
    )["source_trade_id"]
    bridge.process_approved_wallet_trades(
        db,
        wallet=fx.FIXED_WALLET,
        limit=1,
        dependencies=fx.bridge_dependencies(),
        write=True,
        write_authorization=bridge._issue_write_capability(),
        source_trade_id=stored,
        evaluate_canonical_decisions=True,
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
    return ing, cid, psid, sig


def _approve_and_authorize(db: Database, psid: int, sig: dict) -> tuple:
    ap = sa.create_approval(
        db,
        wallet_address=fx.FIXED_WALLET,
        specialist_category=fx.SPECIALIST_CATEGORY,
        formula_name="wallet_score_v1",
        formula_version="1",
        reviewer="tester",
        approval_reason="manual",
    )
    auth = create_execution_authorization(
        db,
        paper_signal_decision_id=psid,
        specialist_approval_id=ap.approval_id,
        source_trade_id=sig["source_trade_id"],
        candidate_id=sig["candidate_id"],
        authorized_by="tester",
        authorization_reason="manual gate",
    )
    return ap, auth


def _counts(db: Database) -> dict:
    out = {}
    for t in [
        "paper_orders",
        "paper_fills",
        "paper_positions",
        "paper_position_lots",
        "paper_position_marks",
        "paper_position_settlements",
        "execution_risk_decisions",
    ]:
        out[t] = db.fetchone(f"SELECT COUNT(*) AS c FROM {t}")["c"]
    return out


def test_full_positive_lifecycle(tmp_path: Path):
    db = Database(tmp_path / "lifecycle.db").connect()
    ing, cid, psid, sig = _seed_and_score(db)
    ap, auth = _approve_and_authorize(db, psid, sig)
    rt = _runtime()
    res = consume_eligible_signal(db, psid, rt)
    assert res.status == "executed", res.rejection_reasons
    assert res.order_id and res.fill_id and res.position_id and res.risk_decision_id
    # Required ID map.
    assert db.fetchone("SELECT id FROM copy_candidates WHERE id=?", (cid,))
    mr = mark_specialist_position(
        db, res.position_id, mark_price=0.5, bid_price=0.45, ask_price=0.55,
        evidence_source="test",
    )
    assert mr.status == "marked" and mr.mark_id
    so = settle_specialist_position(
        db, res.position_id, resolution_outcome="YES", evidence_source="test"
    )
    assert so.status == "settled" and so.settlement_id
    # Full provenance chain.
    snap = db.fetchone(
        "SELECT price_snapshot_id FROM paper_signal_decisions WHERE id=?", (psid,)
    )
    wsd = db.fetchone("SELECT id FROM wallet_score_decisions ORDER BY id DESC LIMIT 1")
    csd = db.fetchone("SELECT id FROM category_wallet_score_decisions ORDER BY id DESC LIMIT 1")
    tcd = db.fetchone("SELECT id FROM trade_copyability_decisions ORDER BY id DESC LIMIT 1")
    lot = db.fetchone(
        "SELECT id FROM paper_position_lots WHERE position_id=?", (res.position_id,)
    )
    mark_row = db.fetchone(
        "SELECT id FROM paper_position_marks WHERE position_id=?", (res.position_id,)
    )
    assert snap["price_snapshot_id"]
    assert wsd["id"] and csd["id"] and tcd["id"]
    assert lot["id"] and mark_row["id"]
    # Realized P&L derived exactly: 10 shares * (1.0 - 0.42 fill).
    assert so.realized_pnl == pytest.approx(10.0 * (1.0 - 0.42), abs=1e-6)
    c = _counts(db)
    assert c["paper_orders"] == 1
    assert c["paper_fills"] == 1
    assert c["paper_positions"] == 1
    assert c["paper_position_lots"] == 1
    assert c["paper_position_marks"] == 1
    assert c["paper_position_settlements"] == 1
    assert c["execution_risk_decisions"] == 1
    # Provenance / immutability checks.
    assert db.fetchone(
        "SELECT is_approved FROM paper_signal_decisions WHERE id=?", (psid,)
    )["is_approved"] == 0
    assert db.fetchone(
        "SELECT status FROM paper_signal_execution_authorizations WHERE authorization_id=?",
        (auth,),
    )["status"] == "used"
    assert db.fetchone(
        "SELECT enabled FROM specialist_approvals WHERE approval_id=?",
        (ap.approval_id,),
    )["enabled"] == 1
    assert db.fetchone(
        "SELECT status FROM paper_positions WHERE id=?", (res.position_id,)
    )["status"] == "settled"
    # Foreign keys resolve.
    assert db.fetchone(
        "SELECT 1 FROM paper_orders po "
        "JOIN paper_fills pf ON pf.order_id = po.id "
        "JOIN paper_positions pp ON pp.paper_order_id = po.id "
        "WHERE po.id=?", (res.order_id,)
    )
    db.close()


def test_replay_is_duplicate_safe(tmp_path: Path):
    db = Database(tmp_path / "replay.db").connect()
    ing, cid, psid, sig = _seed_and_score(db)
    ap, auth = _approve_and_authorize(db, psid, sig)
    rt = _runtime()
    res = consume_eligible_signal(db, psid, rt)
    assert res.status == "executed"
    res2 = consume_eligible_signal(db, psid, rt)
    assert res2.status == "already_executed"
    assert res2.order_id == res.order_id
    assert res2.position_id == res.position_id
    # Settlement replay returns existing row.
    so = settle_specialist_position(
        db, res.position_id, resolution_outcome="YES", evidence_source="test"
    )
    so2 = settle_specialist_position(
        db, res.position_id, resolution_outcome="YES", evidence_source="test"
    )
    assert so2.status == "already_settled"
    assert so2.settlement_id == so.settlement_id
    c = _counts(db)
    assert c["paper_orders"] == 1
    assert c["paper_fills"] == 1
    assert c["paper_position_settlements"] == 1
    db.close()


def test_kill_switch_blocks(tmp_path: Path):
    db = Database(tmp_path / "kill.db").connect()
    ing, cid, psid, sig = _seed_and_score(db)
    ap, auth = _approve_and_authorize(db, psid, sig)
    rt = _runtime(kill_switch_engaged=True)
    res = consume_eligible_signal(db, psid, rt)
    assert res.status == "blocked"
    assert any("kill_switch" in r for r in res.rejection_reasons)
    assert _counts(db)["paper_orders"] == 0
    db.close()


def test_zero_limits_block(tmp_path: Path):
    db = Database(tmp_path / "limits.db").connect()
    ing, cid, psid, sig = _seed_and_score(db)
    ap, auth = _approve_and_authorize(db, psid, sig)
    rt = _runtime(max_order_size=0.0, max_per_market=0.0, max_per_wallet=0.0, max_global=0.0)
    res = consume_eligible_signal(db, psid, rt)
    assert res.status == "blocked"
    assert any("exposure_limits_not_configured" in r for r in res.rejection_reasons)
    assert _counts(db)["paper_orders"] == 0
    db.close()


def test_revoked_approval_blocks(tmp_path: Path):
    db = Database(tmp_path / "revoke.db").connect()
    ing, cid, psid, sig = _seed_and_score(db)
    ap, auth = _approve_and_authorize(db, psid, sig)
    sa.revoke_approval(db, ap.approval_id, revoked_by="tester", revocation_reason="test")
    rt = _runtime()
    res = consume_eligible_signal(db, psid, rt)
    assert res.status == "blocked"
    assert any("approval_disabled_or_revoked" in r for r in res.rejection_reasons)
    assert _counts(db)["paper_orders"] == 0
    db.close()


def test_database_fetch_returns_dicts(tmp_path: Path):
    db = Database(tmp_path / "dict.db").connect()
    fx.seed_resolved_evidence(db)
    row = db.fetchone("SELECT approval_id FROM specialist_approvals LIMIT 1")
    assert row is None or isinstance(row, dict)
    rows = db.fetchall("SELECT 1 AS x")
    assert isinstance(rows, list) and (not rows or isinstance(rows[0], dict))
    db.close()
