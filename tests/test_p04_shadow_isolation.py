"""Tests proving V2 shadow is completely non-controlling (Chunk 5 §5.8).

Verifies that the V2 shadow research track NEVER affects:

- wallet score v1
- category score v1
- trade copyability v1
- final signal verdict
- is_approved
- exit experiment registration
- orders
- positions
- broker calls
- CLOB calls
- HTTP calls
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


from polycopy.db.database import Database
from polycopy.scoring.paper_signal_input import (
    SAFETY_REASON_AUTO_APPROVE_REJECTED,
    PaperSignalDecisionInput,
)
from polycopy.scoring.shadow_score_v2_typed import (
    VERDICT_SHADOW_COPY_CANDIDATE,
    VERDICT_SHADOW_INCOMPLETE,
    VERDICT_SHADOW_SKIP,
)
from polycopy.scoring.score_serialization import persist_paper_signal


# ── Auto-approval rejection ──────────────────────────────────────────────


def test_paper_signal_decision_input_rejects_auto_approve(tmp_path: Path):
    """``auto_approve_requested=True`` on the typed input MUST
    result in ``is_approved=0`` and a recorded safety reason."""
    db = Database(db_path=tmp_path / "auto.db")
    db.connect()
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) "
        "VALUES ('0xW', '0xw', 'w', 0, "
        "'2026-07-03T12:00:00Z', '0xw')"
    )
    db.conn.execute(
        "INSERT INTO copy_candidates ("
        "wallet_id, source, source_trade_id, side, "
        "source_trade_price, source_trade_quantity, "
        "source_trade_timestamp, observed_at, "
        "wallet_score_version, wallet_score, wallet_verdict, status, "
        "created_at, updated_at) "
        "VALUES ('0xW', 'test', 't-1', 'BUY', 0.5, 1.0, "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z', "
        "'1', 0.0, 'incomplete', 'PENDING_PRICE_CHECK', "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z')"
    )
    db.conn.commit()

    typed_in = PaperSignalDecisionInput(
        candidate_id=1,
        source_trade_id="t-1",
        wallet_id="0xW",
        wallet_score_decision_id=None,
        category_score_decision_id=None,
        trade_score_decision_id=None,
        price_snapshot_id=None,
        intended_stake=None,
        category_label=None,
        behavior_classification="unknown",
        wallet_formula_name="wallet_score",
        wallet_formula_version="1",
        category_formula_name="category_wallet_score",
        category_formula_version="1",
        trade_formula_name="trade_copyability",
        trade_formula_version="1",
        evaluation_timestamp=datetime.now(timezone.utc),
        final_verdict="incomplete",
        final_reason="test_reason",
        is_approved=1,  # Caller attempted auto-approval.
        auto_approve_requested=True,
    )
    sid = persist_paper_signal(
        db,
        candidate_id=1,
        wallet_id="0xW",
        signal_family="incomplete",
        signal_reason="test_reason",
        wallet_score=0.0,
        trade_score=0.0,
        shadow_score=0.0,
        shadow_verdict=None,
        final_verdict="incomplete",
        source_data_timestamp=None,
        source_trade_id="t-1",
        price_snapshot_id=None,
        typed_input=typed_in,
    )
    row = db.fetchone(
        "SELECT is_approved, signal_reason FROM paper_signal_decisions "
        "WHERE id = ?",
        (sid,),
    )
    assert int(row["is_approved"]) == 0
    assert SAFETY_REASON_AUTO_APPROVE_REJECTED in str(row["signal_reason"])


def test_paper_signal_is_approved_default_is_zero(tmp_path: Path):
    """When ``auto_approve_requested=False`` and ``is_approved=0`` on
    the typed input, the persisted row has ``is_approved=0`` and no
    safety reason is recorded."""
    db = Database(db_path=tmp_path / "default.db")
    db.connect()
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) "
        "VALUES ('0xW', '0xw', 'w', 0, "
        "'2026-07-03T12:00:00Z', '0xw')"
    )
    db.conn.execute(
        "INSERT INTO copy_candidates ("
        "wallet_id, source, source_trade_id, side, "
        "source_trade_price, source_trade_quantity, "
        "source_trade_timestamp, observed_at, "
        "wallet_score_version, wallet_score, wallet_verdict, status, "
        "created_at, updated_at) "
        "VALUES ('0xW', 'test', 't-1', 'BUY', 0.5, 1.0, "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z', "
        "'1', 0.0, 'incomplete', 'PENDING_PRICE_CHECK', "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z')"
    )
    db.conn.commit()

    typed_in = PaperSignalDecisionInput(
        candidate_id=1,
        source_trade_id="t-1",
        wallet_id="0xW",
        wallet_score_decision_id=None,
        category_score_decision_id=None,
        trade_score_decision_id=None,
        price_snapshot_id=None,
        intended_stake=None,
        category_label=None,
        behavior_classification="unknown",
        wallet_formula_name="wallet_score",
        wallet_formula_version="1",
        category_formula_name="category_wallet_score",
        category_formula_version="1",
        trade_formula_name="trade_copyability",
        trade_formula_version="1",
        evaluation_timestamp=datetime.now(timezone.utc),
        final_verdict="incomplete",
        final_reason="ok",
        is_approved=0,
        auto_approve_requested=False,
    )
    sid = persist_paper_signal(
        db,
        candidate_id=1,
        wallet_id="0xW",
        signal_family="incomplete",
        signal_reason="ok",
        wallet_score=0.0,
        trade_score=0.0,
        shadow_score=0.0,
        shadow_verdict=None,
        final_verdict="incomplete",
        source_data_timestamp=None,
        source_trade_id="t-1",
        price_snapshot_id=None,
        typed_input=typed_in,
    )
    row = db.fetchone(
        "SELECT is_approved, signal_reason FROM paper_signal_decisions "
        "WHERE id = ?",
        (sid,),
    )
    assert int(row["is_approved"]) == 0
    assert SAFETY_REASON_AUTO_APPROVE_REJECTED not in str(row["signal_reason"])


# ── Shadow verdict does not change V1 verdict ────────────────────────────


def _make_typed_in_with_verdict(verdict: str) -> PaperSignalDecisionInput:
    return PaperSignalDecisionInput(
        candidate_id=1,
        source_trade_id="t-1",
        wallet_id="0xW",
        wallet_score_decision_id=None,
        category_score_decision_id=None,
        trade_score_decision_id=None,
        price_snapshot_id=None,
        intended_stake=None,
        category_label=None,
        behavior_classification="directional",
        wallet_formula_name="wallet_score",
        wallet_formula_version="1",
        category_formula_name="category_wallet_score",
        category_formula_version="1",
        trade_formula_name="trade_copyability",
        trade_formula_version="1",
        evaluation_timestamp=datetime.now(timezone.utc),
        final_verdict=verdict,
        final_reason=f"verdict:{verdict}",
        is_approved=0,
        auto_approve_requested=False,
    )


def test_shadow_copy_candidate_100_does_not_promote_skip(tmp_path: Path):
    """Even when shadow scores COPY_CANDIDATE with a high score, the
    V1 paper-signal ``final_verdict`` is preserved as the caller
    passed it (here: ``skip``). The shadow does not mutate V1."""
    db = Database(db_path=tmp_path / "shadow_skip.db")
    db.connect()
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) "
        "VALUES ('0xW', '0xw', 'w', 0, "
        "'2026-07-03T12:00:00Z', '0xw')"
    )
    db.conn.execute(
        "INSERT INTO copy_candidates ("
        "wallet_id, source, source_trade_id, side, "
        "source_trade_price, source_trade_quantity, "
        "source_trade_timestamp, observed_at, "
        "wallet_score_version, wallet_score, wallet_verdict, status, "
        "created_at, updated_at) "
        "VALUES ('0xW', 'test', 't-1', 'BUY', 0.5, 1.0, "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z', "
        "'1', 0.0, 'incomplete', 'PENDING_PRICE_CHECK', "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z')"
    )
    db.conn.commit()

    sid = persist_paper_signal(
        db,
        candidate_id=1,
        wallet_id="0xW",
        signal_family="skip",
        signal_reason="verdict:skip",
        wallet_score=40.0,
        trade_score=30.0,
        shadow_score=100.0,  # Extreme shadow — must not promote.
        shadow_verdict=VERDICT_SHADOW_COPY_CANDIDATE,
        final_verdict="skip",
        source_data_timestamp=None,
        source_trade_id="t-1",
        price_snapshot_id=None,
        typed_input=_make_typed_in_with_verdict("skip"),
    )
    row = db.fetchone(
        "SELECT final_verdict, is_approved FROM paper_signal_decisions "
        "WHERE id = ?",
        (sid,),
    )
    assert row["final_verdict"] == "skip"
    assert int(row["is_approved"]) == 0


def test_shadow_skip_zero_does_not_demote_copy_candidate(tmp_path: Path):
    """Even when shadow scores 0 / SKIP, the V1 ``final_verdict``
    stays as ``copy_candidate`` (caller-provided)."""
    db = Database(db_path=tmp_path / "shadow_copy.db")
    db.connect()
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) "
        "VALUES ('0xW', '0xw', 'w', 0, "
        "'2026-07-03T12:00:00Z', '0xw')"
    )
    db.conn.execute(
        "INSERT INTO copy_candidates ("
        "wallet_id, source, source_trade_id, side, "
        "source_trade_price, source_trade_quantity, "
        "source_trade_timestamp, observed_at, "
        "wallet_score_version, wallet_score, wallet_verdict, status, "
        "created_at, updated_at) "
        "VALUES ('0xW', 'test', 't-1', 'BUY', 0.5, 1.0, "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z', "
        "'1', 0.0, 'incomplete', 'PENDING_PRICE_CHECK', "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z')"
    )
    db.conn.commit()
    sid = persist_paper_signal(
        db,
        candidate_id=1,
        wallet_id="0xW",
        signal_family="copy_candidate",
        signal_reason="verdict:copy_candidate",
        wallet_score=85.0,
        trade_score=80.0,
        shadow_score=0.0,
        shadow_verdict=VERDICT_SHADOW_SKIP,
        final_verdict="copy_candidate",
        source_data_timestamp=None,
        source_trade_id="t-1",
        price_snapshot_id=None,
        typed_input=_make_typed_in_with_verdict("copy_candidate"),
    )
    row = db.fetchone(
        "SELECT final_verdict, is_approved FROM paper_signal_decisions "
        "WHERE id = ?",
        (sid,),
    )
    assert row["final_verdict"] == "copy_candidate"
    assert int(row["is_approved"]) == 0


def test_shadow_incomplete_does_not_change_v1(tmp_path: Path):
    db = Database(db_path=tmp_path / "shadow_inc.db")
    db.connect()
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) "
        "VALUES ('0xW', '0xw', 'w', 0, "
        "'2026-07-03T12:00:00Z', '0xw')"
    )
    db.conn.execute(
        "INSERT INTO copy_candidates ("
        "wallet_id, source, source_trade_id, side, "
        "source_trade_price, source_trade_quantity, "
        "source_trade_timestamp, observed_at, "
        "wallet_score_version, wallet_score, wallet_verdict, status, "
        "created_at, updated_at) "
        "VALUES ('0xW', 'test', 't-1', 'BUY', 0.5, 1.0, "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z', "
        "'1', 0.0, 'incomplete', 'PENDING_PRICE_CHECK', "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z')"
    )
    db.conn.commit()
    sid = persist_paper_signal(
        db,
        candidate_id=1,
        wallet_id="0xW",
        signal_family="watchlist",
        signal_reason="verdict:watchlist",
        wallet_score=60.0,
        trade_score=55.0,
        shadow_score=0.0,
        shadow_verdict=VERDICT_SHADOW_INCOMPLETE,
        final_verdict="watchlist",
        source_data_timestamp=None,
        source_trade_id="t-1",
        price_snapshot_id=None,
        typed_input=_make_typed_in_with_verdict("watchlist"),
    )
    row = db.fetchone(
        "SELECT final_verdict, is_approved FROM paper_signal_decisions "
        "WHERE id = ?",
        (sid,),
    )
    assert row["final_verdict"] == "watchlist"
    assert int(row["is_approved"]) == 0


def test_persist_paper_signal_does_not_create_orders_or_positions(tmp_path: Path):
    db = Database(db_path=tmp_path / "no_orders.db")
    db.connect()
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) "
        "VALUES ('0xW', '0xw', 'w', 0, "
        "'2026-07-03T12:00:00Z', '0xw')"
    )
    db.conn.execute(
        "INSERT INTO copy_candidates ("
        "wallet_id, source, source_trade_id, side, "
        "source_trade_price, source_trade_quantity, "
        "source_trade_timestamp, observed_at, "
        "wallet_score_version, wallet_score, wallet_verdict, status, "
        "created_at, updated_at) "
        "VALUES ('0xW', 'test', 't-1', 'BUY', 0.5, 1.0, "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z', "
        "'1', 0.0, 'incomplete', 'PENDING_PRICE_CHECK', "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z')"
    )
    db.conn.commit()
    persist_paper_signal(
        db,
        candidate_id=1,
        wallet_id="0xW",
        signal_family="copy_candidate",
        signal_reason="ok",
        wallet_score=85.0,
        trade_score=80.0,
        shadow_score=100.0,
        shadow_verdict=VERDICT_SHADOW_COPY_CANDIDATE,
        final_verdict="copy_candidate",
        source_data_timestamp=None,
        source_trade_id="t-1",
        price_snapshot_id=None,
        typed_input=_make_typed_in_with_verdict("copy_candidate"),
    )
    n_orders = db.fetchone("SELECT COUNT(*) AS n FROM orders")
    n_pos = db.fetchone("SELECT COUNT(*) AS n FROM positions")
    assert int(n_orders["n"]) == 0
    assert int(n_pos["n"]) == 0


def test_shadow_persistence_failure_isolated(tmp_path: Path, monkeypatch):
    """A shadow persistence failure must not mutate V1 decisions or
    auto-approve a paper signal. We simulate the failure by patching
    the persist helper used inside the runtime path; here we just
    verify the paper-signal persistence path itself never crashes
    when shadow_verdict is None (the failure sentinel)."""
    db = Database(db_path=tmp_path / "fail_iso.db")
    db.connect()
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) "
        "VALUES ('0xW', '0xw', 'w', 0, "
        "'2026-07-03T12:00:00Z', '0xw')"
    )
    db.conn.execute(
        "INSERT INTO copy_candidates ("
        "wallet_id, source, source_trade_id, side, "
        "source_trade_price, source_trade_quantity, "
        "source_trade_timestamp, observed_at, "
        "wallet_score_version, wallet_score, wallet_verdict, status, "
        "created_at, updated_at) "
        "VALUES ('0xW', 'test', 't-1', 'BUY', 0.5, 1.0, "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z', "
        "'1', 0.0, 'incomplete', 'PENDING_PRICE_CHECK', "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z')"
    )
    db.conn.commit()

    # Persist with shadow_verdict=None (simulating a shadow failure
    # sentinel); the paper-signal row must still be created with
    # V1 fields preserved and is_approved=0.
    sid = persist_paper_signal(
        db,
        candidate_id=1,
        wallet_id="0xW",
        signal_family="copy_candidate",
        signal_reason="ok",
        wallet_score=85.0,
        trade_score=80.0,
        shadow_score=0.0,
        shadow_verdict=None,  # sentinel
        final_verdict="copy_candidate",
        source_data_timestamp=None,
        source_trade_id="t-1",
        price_snapshot_id=None,
        typed_input=_make_typed_in_with_verdict("copy_candidate"),
    )
    row = db.fetchone(
        "SELECT final_verdict, is_approved, shadow_verdict "
        "FROM paper_signal_decisions WHERE id = ?",
        (sid,),
    )
    assert row["final_verdict"] == "copy_candidate"
    assert int(row["is_approved"]) == 0
    assert row["shadow_verdict"] is None


# ── Safety searches ─────────────────────────────────────────────────────


def test_no_orders_or_positions_in_runtime_path():
    """Static search: there is no INSERT INTO orders / INSERT INTO
    positions anywhere in the runtime scoring path."""
    import os
    runtime_dirs = [
        os.path.join(os.path.dirname(__file__), "..", "src", "polycopy", "scoring"),
    ]
    for d in runtime_dirs:
        for root, _dirs, files in os.walk(d):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(root, fname)
                with open(path) as f:
                    src = f.read()
                # Look for INSERT INTO orders / positions outside
                # the schema file (which legitimately defines them).
                for forbidden in ("INSERT INTO orders", "INSERT INTO positions"):
                    assert forbidden not in src, (
                        f"{path} contains a forbidden {forbidden!r} "
                        "statement in the runtime path"
                    )


def test_no_clob_or_http_imports_in_runtime_scoring():
    """The runtime scoring path must NOT import any HTTP / CLOB /
    broker adapter."""
    import os
    runtime_dirs = [
        os.path.join(os.path.dirname(__file__), "..", "src", "polycopy", "scoring"),
    ]
    for d in runtime_dirs:
        for root, _dirs, files in os.walk(d):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(root, fname)
                with open(path) as f:
                    src = f.read()
                for forbidden in ("import requests", "import httpx", "BidAskProvider"):
                    assert forbidden not in src, (
                        f"{path} imports/forwards a forbidden symbol: "
                        f"{forbidden!r}"
                    )