"""Immutability tests for PR 4 decision and research tables.

Proves the following invariants:

  * Identical point-in-time inputs return the same persisted row ID.
  * A changed identity (different inputs) creates a NEW row ID.
  * The existing row's values remain byte-for-byte unchanged when a
    new row is created (insert-only behavior).
  * Persistence modules expose no UPDATE or DELETE operations for
    PR 4 tables.
  * Step 7 (full end-to-end paper signal evaluation) can be rerun
    without mutating prior decision rows.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path

from polycopy.db.database import Database
from polycopy.scoring.score_serialization import (
    persist_paper_signal,
    persist_trade_score_v1,
    persist_wallet_score_v1,
    record_exit_experiments,
)


# ---- Helpers ------------------------------------------------------------


def _fresh_db(tmp_path: Path) -> Database:
    db = Database(db_path=tmp_path / "immut.db").connect()
    return db


def _wallet(db: Database, wid: str = "0xW") -> None:
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) VALUES "
        "(?, ?, 'w', 0, '2026-01-01T00:00:00Z', ?)",
        (wid, wid.lower(), wid.lower()),
    )
    db.conn.commit()


def _source_trade(db: Database, sid: str = "st-1") -> None:
    db.conn.execute(
        "INSERT INTO source_trades (id, source, source_trade_id, "
        "market_source_id, side, outcome, quantity, price, "
        "trader_address, timestamp, is_sample) "
        "VALUES (?, 'polymarket', ?, 'm-src-1', 'BUY', 'YES', 100, 0.5, "
        "'0xt', '2026-07-01T00:00:00Z', 0)",
        (sid, sid),
    )
    db.conn.commit()


def _make_wallet_result():
    from polycopy.scoring.wallet_score_v1 import (
        WalletScoreInputV1,
        compute_wallet_score_v1,
    )
    inp = WalletScoreInputV1(
        wallet_id="0xW",
        info_score=0.8,
        win_rate=0.6,
        profit_factor=1.5,
        trade_intervals_std=10.0,
        trade_count=50,
        max_drawdown=0.2,
        sharpe_ratio=1.2,
        sample_fraction=0.5,
        category_trade_count=30,
        category_distinct_markets=5,
        overall_trade_count=80,
        largest_winner_share=0.4,
        top_3_concentration=0.6,
        resolved_markets=10,
        active_trading_days=30,
        distinct_events=8,
        category_resolved_markets=8,
        category_distinct_events=4,
        category_active_days=20,
    )
    return compute_wallet_score_v1(wallet_id="0xW", input=inp)


def _make_trade_result():
    from polycopy.scoring.trade_score_v1 import (
        TradeCopyabilityInputV1,
        compute_trade_score_v1,
    )
    inp = TradeCopyabilityInputV1(
        wallet_id="0xW",
        source_trade_id="st-1",
        side="BUY",
        price_deterioration_pct=0.01,
        intended_stake=50.0,
        executable_depth=60.0,
        fill_percentage=0.95,
        spread=0.02,
        best_bid_size=100.0,
        best_ask_size=110.0,
        trade_age_seconds=10,
        seconds_to_market_end=1000,
        market_active=True,
        market_closed=False,
        market_resolved=False,
    )
    return compute_trade_score_v1(
        wallet_id="0xW", source_trade_id="st-1", input=inp,
    )


# ---- Idempotency: identical inputs return same row ID -------------------


def test_identical_wallet_score_returns_same_row_id(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    _wallet(db)
    result = _make_wallet_result()
    id1 = persist_wallet_score_v1(
        db, "0xW", result, source_data_timestamp="2026-07-01T00:00:00Z",
    )
    id2 = persist_wallet_score_v1(
        db, "0xW", result, source_data_timestamp="2026-07-01T00:00:00Z",
    )
    assert id1 == id2


def test_identical_trade_score_returns_same_row_id(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    _wallet(db)
    _source_trade(db)
    result = _make_trade_result()
    id1 = persist_trade_score_v1(
        db, "0xW", "st-1", result,
        source_data_timestamp="2026-07-01T00:00:00Z",
    )
    id2 = persist_trade_score_v1(
        db, "0xW", "st-1", result,
        source_data_timestamp="2026-07-01T00:00:00Z",
    )
    assert id1 == id2


# ---- Changed identity creates a new row ID ------------------------------


def test_changed_source_data_timestamp_creates_new_wallet_row(
    tmp_path: Path,
) -> None:
    db = _fresh_db(tmp_path)
    _wallet(db)
    result = _make_wallet_result()
    id1 = persist_wallet_score_v1(
        db, "0xW", result, source_data_timestamp="2026-07-01T00:00:00Z",
    )
    id2 = persist_wallet_score_v1(
        db, "0xW", result, source_data_timestamp="2026-07-02T00:00:00Z",
    )
    assert id1 != id2


def test_changed_source_data_timestamp_creates_new_trade_row(
    tmp_path: Path,
) -> None:
    db = _fresh_db(tmp_path)
    _wallet(db)
    _source_trade(db)
    result = _make_trade_result()
    id1 = persist_trade_score_v1(
        db, "0xW", "st-1", result,
        source_data_timestamp="2026-07-01T00:00:00Z",
    )
    id2 = persist_trade_score_v1(
        db, "0xW", "st-1", result,
        source_data_timestamp="2026-07-02T00:00:00Z",
    )
    assert id1 != id2


# ---- Existing row byte-for-byte unchanged after a new write -------------


def test_existing_row_byte_for_byte_unchanged(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    _wallet(db)
    _source_trade(db)
    result = _make_trade_result()
    id1 = persist_trade_score_v1(
        db, "0xW", "st-1", result,
        source_data_timestamp="2026-07-01T00:00:00Z",
    )
    snap1 = dict(db.conn.execute(
        "SELECT * FROM trade_copyability_decisions WHERE id = ?", (id1,),
    ).fetchone())

    id2 = persist_trade_score_v1(
        db, "0xW", "st-1", result,
        source_data_timestamp="2026-07-02T00:00:00Z",
    )
    assert id2 != id1

    snap1_again = dict(db.conn.execute(
        "SELECT * FROM trade_copyability_decisions WHERE id = ?", (id1,),
    ).fetchone())
    assert snap1 == snap1_again


# ---- No UPDATE/DELETE statements in persistence modules -----------------


def test_persistence_modules_expose_no_update_or_delete() -> None:
    """Static guard against accidentally introducing UPDATE/DELETE."""
    import polycopy.scoring.score_serialization as s_mod
    import polycopy.scoring.paper_signal as p_mod
    forbidden = (
        "UPDATE wallet_score_decisions",
        "UPDATE category_wallet_score_decisions",
        "UPDATE trade_copyability_decisions",
        "UPDATE shadow_decisions",
        "UPDATE paper_signal_decisions",
        "UPDATE exit_experiment_registrations",
        "UPDATE score_component_inputs",
        "DELETE FROM wallet_score_decisions",
        "DELETE FROM category_wallet_score_decisions",
        "DELETE FROM trade_copyability_decisions",
        "DELETE FROM shadow_decisions",
        "DELETE FROM paper_signal_decisions",
        "DELETE FROM exit_experiment_registrations",
        "DELETE FROM score_component_inputs",
    )
    for mod in (s_mod, p_mod):
        src = inspect.getsource(mod)
        for stmt in forbidden:
            assert stmt not in src, (
                f"{mod.__name__} contains forbidden mutation: {stmt!r}"
            )


# ---- Step 7 rerun does not mutate prior paper-signal row --------------


def test_paper_signal_rerun_no_mutation(tmp_path: Path) -> None:
    """Rerun paper-signal persistence with identical typed input and
    prove the row remains unchanged."""
    from polycopy.scoring.paper_signal_input import PaperSignalDecisionInput
    from polycopy.scoring.verdict_generation import SignalVerdict

    db = _fresh_db(tmp_path)
    _wallet(db)
    _source_trade(db)
    db.conn.execute(
        "INSERT INTO copy_candidates (wallet_id, source, source_trade_id, "
        "source_trade_internal_id, side, source_trade_price, "
        "source_trade_quantity, source_trade_timestamp, observed_at, "
        "wallet_score_version, wallet_score, wallet_verdict, status, "
        "created_at, updated_at) "
        "VALUES ('0xW', 'polymarket', 'st-1', 'st-1', 'BUY', 0.5, "
        "100, '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z', "
        "'v1', 80.0, 'copy_candidate', 'pending', "
        "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')",
    )
    db.conn.execute(
        "INSERT INTO candidate_price_snapshots (id, candidate_id, "
        "snapshot_run_id, fetch_status, side, source_trade_price, "
        "source_trade_quantity, source_trade_timestamp, fetched_at, "
        "created_at) "
        "VALUES ('snap-1', 1, 'run-1', 'OK', 'BUY', 0.5, 100, "
        "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z', "
        "'2026-07-01T00:00:00Z')",
    )
    db.conn.commit()

    wallet_result = _make_wallet_result()
    trade_result = _make_trade_result()

    wallet_id_1 = persist_wallet_score_v1(
        db, "0xW", wallet_result,
        source_data_timestamp="2026-07-01T00:00:00Z",
    )
    trade_id_1 = persist_trade_score_v1(
        db, "0xW", "st-1", trade_result,
        source_data_timestamp="2026-07-01T00:00:00Z",
    )

    eval_dt = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    typed = PaperSignalDecisionInput(
        candidate_id=1,
        source_trade_id="st-1",
        wallet_id="0xW",
        wallet_score_decision_id=wallet_id_1,
        category_score_decision_id=None,
        trade_score_decision_id=trade_id_1,
        price_snapshot_id="snap-1",
        intended_stake=50.0,
        category_label="category-1",
        behavior_classification="directional",
        wallet_formula_name="wallet_score",
        wallet_formula_version="1",
        category_formula_name="category_wallet_score",
        category_formula_version="1",
        trade_formula_name="trade_copyability",
        trade_formula_version=trade_result.formula_version,
        evaluation_timestamp=eval_dt,
        final_verdict=SignalVerdict.COPY_CANDIDATE.value,
        final_reason="ok",
        is_approved=0,
        auto_approve_requested=False,
    )

    ps_id1 = persist_paper_signal(
        db, 1, "0xW",
        SignalVerdict.COPY_CANDIDATE.value,
        "ok",
        wallet_result.score,
        trade_result.score,
        0.0,
        None,
        SignalVerdict.COPY_CANDIDATE.value,
        "2026-07-01T00:00:00Z",
        "st-1",
        "snap-1",
        typed_input=typed,
    )
    snap1 = dict(db.conn.execute(
        "SELECT * FROM paper_signal_decisions WHERE id = ?", (ps_id1,),
    ).fetchone())

    ps_id2 = persist_paper_signal(
        db, 1, "0xW",
        SignalVerdict.COPY_CANDIDATE.value,
        "ok",
        wallet_result.score,
        trade_result.score,
        0.0,
        None,
        SignalVerdict.COPY_CANDIDATE.value,
        "2026-07-01T00:00:00Z",
        "st-1",
        "snap-1",
        typed_input=typed,
    )
    assert ps_id1 == ps_id2, "Idempotency violated — expected same id."

    snap1_again = dict(db.conn.execute(
        "SELECT * FROM paper_signal_decisions WHERE id = ?", (ps_id1,),
    ).fetchone())
    assert snap1 == snap1_again, "Row mutated between identical inserts!"


# ---- Exit experiments are also insert-only ------------------------------


def test_exit_experiments_rerun_no_duplicates(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    _wallet(db)
    _source_trade(db)
    db.conn.execute(
        "INSERT INTO copy_candidates (wallet_id, source, source_trade_id, "
        "source_trade_internal_id, side, source_trade_price, "
        "source_trade_quantity, source_trade_timestamp, observed_at, "
        "wallet_score_version, wallet_score, wallet_verdict, status, "
        "created_at, updated_at) "
        "VALUES ('0xW', 'polymarket', 'st-1', 'st-1', 'BUY', 0.5, "
        "100, '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z', "
        "'v1', 80.0, 'copy_candidate', 'pending', "
        "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')",
    )
    db.conn.execute(
        "INSERT INTO paper_signal_decisions (candidate_id, wallet_id, "
        "signal_family, signal_reason, final_verdict, is_approved, "
        "idempotency_key, computed_at, created_at) VALUES "
        "(1, '0xW', 'copy_candidate', 'ok', 'copy_candidate', 0, "
        "'k1', '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')",
    )
    db.conn.commit()

    ts = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    ids1 = record_exit_experiments(db, paper_signal_id=1,
                                    signal_evaluation_timestamp=ts)
    ids2 = record_exit_experiments(db, paper_signal_id=1,
                                    signal_evaluation_timestamp=ts)
    assert ids1 == ids2
    assert len(set(ids1)) == 7