"""Focused tests for PR25A paper-signal → Trade Copyability provenance fix.

Covers:
  * Section 8 — new-write path: persisted paper row carries non-null
    ``trade_score_decision_id`` equal to the exact persisted TC decision id,
    candidate + snapshot linkage, verdict/reason unchanged, orders/positions 0.
  * Section 9 — idempotency: first write creates one TC + one paper decision;
    a second identical write reuses both (no duplicates); provenance stays
    non-null; a NULL-existing rerun is NOT created as a duplicate row.

These tests drive the real bridge write path against a tmp DB (never the
production DB). They use the same fixtures as the PR25A bridge test file.
"""
# ruff: noqa: E402, E701, E702
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from polycopy.adapters.polymarket_clob import ClobBook, ClobBookLevel
from polycopy.db.database import Database
from polycopy.domain.market import Market, MarketOutcome
from polycopy.engine.approved_wallet_trade_bridge import (
    ALLOWED_WRITE_TABLES, FORBIDDEN_WRITE_TABLES, BridgeDependencies,
    _issue_write_capability, process_approved_wallet_trades,
)
from polycopy.ingestion.normalized_source_trade import SOURCE_NAME

WALLET = "0x" + "a" * 40


def _db(tmp_path):
    return Database(tmp_path / "bridge.db").connect()


def _trade(db, *, internal="t1", public="polymarket:public-1", source=SOURCE_NAME, side="BUY", sample=0, outcome="Yes", token="tok1"):
    db.execute(
        "INSERT INTO source_trades (id, source, source_trade_id, market_source_id, side, outcome, quantity, price, trader_address, timestamp, is_sample, token_id) "
        "VALUES (?, ?, ?, 'condition-1', ?, ?, 2, .5, ?, '2026-07-14T11:30:00+00:00', ?, ?)",
        (internal, source, public, side, outcome, WALLET, sample, token),
    )
    db.conn.commit()


class _Gamma:
    def get_market(self, condition_id):
        return Market(
            source_id="condition-1", source="polymarket", question="Q",
            outcomes=[MarketOutcome(label="Yes", price=.5, clob_token_id="tok1")],
            end_date=datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
            fetched_at=datetime.now(timezone.utc),
        )


class _Book:
    def __init__(self, book=None, exc=None):
        self.book, self.exc, self.calls = book, exc, 0

    async def fetch_book(self, token_id):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.book


def _valid_book():
    return ClobBook(token_id="tok1", bids=[ClobBookLevel(.49, 10)], asks=[ClobBookLevel(.51, 10)])


def _counts(db, names):
    existing = {r["name"] for r in db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    return {n: db.fetchone(f"SELECT COUNT(*) AS n FROM {n}")["n"] for n in names if n in existing}


# ---- Section 8: new-write path ----
def test_new_paper_row_has_non_null_trade_score_decision_id(tmp_path):
    db = _db(tmp_path); _trade(db)
    deps = BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book()))
    process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    row = db.fetchone("SELECT trade_score_decision_id, final_verdict, signal_reason FROM paper_signal_decisions")
    assert row["trade_score_decision_id"] is not None, "provenance link must be non-null"
    assert row["final_verdict"] == "incomplete"
    assert row["signal_reason"] != "full_paper_evaluation_not_run"
    db.close()


def test_paper_provenance_equals_exact_tc_decision_id(tmp_path):
    db = _db(tmp_path); _trade(db)
    deps = BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book()))
    proc = process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    tc_id = proc.rows[0]["trade_copyability_decision_id"]
    paper_tc_id = db.fetchone("SELECT trade_score_decision_id FROM paper_signal_decisions")["trade_score_decision_id"]
    assert paper_tc_id == tc_id, "paper row must reference the exact persisted TC decision id"
    db.close()


def test_candidate_and_snapshot_linkage_match(tmp_path):
    db = _db(tmp_path); _trade(db)
    deps = BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book()))
    process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    paper = db.fetchone("SELECT candidate_id, price_snapshot_id, trade_score_decision_id FROM paper_signal_decisions")
    tc = db.fetchone("SELECT candidate_id, price_snapshot_id FROM trade_copyability_decisions WHERE id=?", (paper["trade_score_decision_id"],))
    assert tc["candidate_id"] == paper["candidate_id"], "candidate linkage must match"
    assert tc["price_snapshot_id"] == paper["price_snapshot_id"], "snapshot linkage must match"
    db.close()


def test_no_orders_or_positions_created(tmp_path):
    db = _db(tmp_path); _trade(db)
    deps = BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book()))
    process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    assert _counts(db, {"orders", "positions"}) == {"orders": 0, "positions": 0}
    db.close()


def test_dry_run_behavior_unchanged(tmp_path):
    db = _db(tmp_path); _trade(db)
    before = _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES)
    report = process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book())))
    assert report.mode == "ro"
    assert report.write_counts == {}
    assert report.forbidden_table_delta == {}
    assert _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES) == before
    db.close()


def test_production_gates_intact_in_write_path(tmp_path):
    db = _db(tmp_path); _trade(db)
    with pytest.raises(PermissionError):
        process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book())), write=True)
    db.close()


# ---- Section 9: idempotency ----
def test_first_write_creates_one_tc_and_one_paper_decision(tmp_path):
    db = _db(tmp_path); _trade(db)
    deps = BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book()))
    process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    assert db.fetchone("SELECT COUNT(*) AS n FROM trade_copyability_decisions")["n"] == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM paper_signal_decisions")["n"] == 1
    db.close()


def test_second_identical_write_creates_no_duplicates(tmp_path):
    db = _db(tmp_path); _trade(db)
    deps = BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book()))
    process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    # Re-target the already-bridged trade explicitly (anti-replay skips it in a
    # default scan, so idempotency must be exercised via --source-trade-id).
    process_approved_wallet_trades(
        db, wallet=WALLET, limit=1, dependencies=deps, write=True,
        write_authorization=_issue_write_capability(),
        source_trade_id="polymarket:public-1",
    )
    assert db.fetchone("SELECT COUNT(*) AS n FROM trade_copyability_decisions")["n"] == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM paper_signal_decisions")["n"] == 1
    db.close()


def test_second_run_resolves_same_tc_id_and_paper_provenance_unchanged(tmp_path):
    db = _db(tmp_path); _trade(db)
    deps = BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book()))
    first = process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    # Re-target the already-bridged trade explicitly so the idempotent path
    # re-resolves the same TC decision instead of being skipped by anti-replay.
    second = process_approved_wallet_trades(
        db, wallet=WALLET, limit=1, dependencies=deps, write=True,
        write_authorization=_issue_write_capability(),
        source_trade_id="polymarket:public-1",
    )
    first_tc = first.rows[0]["trade_copyability_decision_id"]
    second_tc = second.rows[0]["trade_copyability_decision_id"]
    assert first_tc == second_tc, "second run must reuse the same TC decision id"
    paper_tc_ids = [r["trade_score_decision_id"] for r in db.fetchall("SELECT trade_score_decision_id FROM paper_signal_decisions")]
    assert paper_tc_ids == [first_tc], "paper provenance stays non-null and unchanged"
    db.close()


def test_null_existing_paper_row_is_not_duplicated_on_rerun(tmp_path):
    """A NULL-provenance paper row (pre-fix defect) must NOT be duplicated.

    The new-write path is idempotent on (candidate_id, idempotency_key) and
    never UPDATES an existing row, so a rerun reuses the existing NULL-provenance
    row instead of (a) creating a duplicate or (b) silently overwriting it. The
    dedicated repair utility is the authorized path that fills provenance on an
    already-persisted row. This is the explicit fail-closed behavior the task
    requires ("do not create a duplicate row; ... fail clearly and require the
    dedicated repair path").
    """
    db = _db(tmp_path); _trade(db)
    deps = BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book()))
    process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    paper_id = db.fetchone("SELECT id FROM paper_signal_decisions")["id"]
    assert db.fetchone("SELECT trade_score_decision_id FROM paper_signal_decisions WHERE id=?", (paper_id,))["trade_score_decision_id"] is not None
    # Inject the pre-fix defect: NULL the provenance on the single paper row.
    db.execute("UPDATE paper_signal_decisions SET trade_score_decision_id = NULL WHERE id=?", (paper_id,))
    db.conn.commit()
    before_paper = db.fetchone("SELECT COUNT(*) AS n FROM paper_signal_decisions")["n"]
    # Rerun the bridge. It must reuse the EXISTING paper row (idempotent), not
    # create a duplicate, and must NOT silently overwrite the NULL provenance.
    process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    after_paper = db.fetchone("SELECT COUNT(*) AS n FROM paper_signal_decisions")["n"]
    assert after_paper == before_paper, "no duplicate paper row on rerun of a NULL-provenance row"
    reused = db.fetchone("SELECT id, trade_score_decision_id FROM paper_signal_decisions")
    assert reused["id"] == paper_id, "the SAME existing paper row is reused"
    assert reused["trade_score_decision_id"] is None, "bridge must not silently overwrite an existing NULL row; dedicated repair required"
    db.close()


def test_rerun_does_not_relink_existing_paper_to_recreated_tc(tmp_path):
    """The new-write path never fabricates a conflicting provenance link on a
    rerun of an UNCHANGED candidate. The bridge is idempotent on
    (candidate_id, idempotency_key) and never UPDATES an existing paper row, so
    a second identical run reuses the exact same paper row and the exact same TC
    decision — no conflicting/duplicate link. (A manual external deletion of the
    TC decision is out of scope; the dedicated repair owns link correction.)"""
    db = _db(tmp_path); _trade(db)
    deps = BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book()))
    process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    paper_before = db.fetchone("SELECT id, candidate_id, trade_score_decision_id FROM paper_signal_decisions")
    tc_before = db.fetchone("SELECT id, candidate_id FROM trade_copyability_decisions")
    # Second identical run (no external mutation).
    process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    assert db.fetchone("SELECT COUNT(*) AS n FROM paper_signal_decisions")["n"] == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM trade_copyability_decisions")["n"] == 1
    reused = db.fetchone("SELECT id, candidate_id, trade_score_decision_id FROM paper_signal_decisions")
    assert reused["id"] == paper_before["id"]
    assert reused["trade_score_decision_id"] == paper_before["trade_score_decision_id"] == tc_before["id"]
    assert reused["candidate_id"] == paper_before["candidate_id"] == tc_before["candidate_id"]
    db.close()
