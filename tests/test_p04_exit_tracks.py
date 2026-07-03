"""Tests for canonical exit-experiment research tracks (Chunk 5 §5.3).

Verifies:

- exactly seven canonical identifiers;
- EXIT_24H scheduled at evaluation_timestamp + 24h;
- EXIT_72H scheduled at evaluation_timestamp + 72h;
- the other five tracks have NULL scheduled_at;
- rerun is idempotent (no duplicates);
- different paper signals receive their own seven tracks;
- no orders are created;
- no positions are created.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


from polycopy.db.database import Database
from polycopy.scoring.exit_tracks import (
    CANONICAL_EXIT_TRACKS,
    ExitTrack,
    compute_scheduled_at,
)
from polycopy.scoring.score_serialization import record_exit_experiments


CANONICAL_EXPECTED = {
    "HOLD_TO_RESOLUTION",
    "EXIT_24H",
    "EXIT_72H",
    "FAVORABLE_MOVE_005",
    "FAVORABLE_MOVE_010",
    "FAVORABLE_MOVE_015",
    "THESIS_OR_LIQUIDITY_FAILURE",
}


def _make_db(tmp_path: Path) -> Database:
    db = Database(db_path=tmp_path / "exit_tracks.db")
    db.connect()
    # Create a wallet + candidate so the FK on
    # paper_signal_decisions is satisfied.
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
    return db


# ---- Canonical identifier tests ------------------------------------------


def test_canonical_exit_tracks_exactly_seven():
    assert len(CANONICAL_EXIT_TRACKS) == 7


def test_canonical_exit_tracks_exact_names():
    actual = {t.value for t in CANONICAL_EXIT_TRACKS}
    assert actual == CANONICAL_EXPECTED


def test_no_legacy_aliases_in_canonical():
    """Canonical set must NOT include legacy lowercase names."""
    legacy = {"hold_to_resolution", "exit_24h", "exit_72h", "thesis_failure"}
    actual = {t.value for t in CANONICAL_EXIT_TRACKS}
    assert actual.isdisjoint(legacy)


# ---- compute_scheduled_at ------------------------------------------------


def test_exit_24h_scheduled_24h_after_evaluation():
    ts = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
    out = compute_scheduled_at(ExitTrack.EXIT_24H, signal_evaluation_timestamp=ts)
    assert out is not None
    delta = out - ts
    assert delta.total_seconds() == 24 * 3600


def test_exit_72h_scheduled_72h_after_evaluation():
    ts = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
    out = compute_scheduled_at(ExitTrack.EXIT_72H, signal_evaluation_timestamp=ts)
    assert out is not None
    delta = out - ts
    assert delta.total_seconds() == 72 * 3600


def test_hold_to_resolution_scheduled_at_none():
    ts = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
    out = compute_scheduled_at(ExitTrack.HOLD_TO_RESOLUTION, signal_evaluation_timestamp=ts)
    assert out is None


def test_favorable_move_tracks_scheduled_at_none():
    ts = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
    for t in (
        ExitTrack.FAVORABLE_MOVE_005,
        ExitTrack.FAVORABLE_MOVE_010,
        ExitTrack.FAVORABLE_MOVE_015,
        ExitTrack.THESIS_OR_LIQUIDITY_FAILURE,
    ):
        assert compute_scheduled_at(t, signal_evaluation_timestamp=ts) is None


def test_compute_scheduled_at_naive_treated_as_utc():
    ts = datetime(2026, 7, 3, 12, 0, 0)  # naive
    out = compute_scheduled_at(ExitTrack.EXIT_24H, signal_evaluation_timestamp=ts)
    assert out is not None
    delta = out - ts.replace(tzinfo=timezone.utc)
    assert delta.total_seconds() == 24 * 3600


# ---- record_exit_experiments integration ---------------------------------


def test_record_creates_exactly_seven_rows(tmp_path: Path):
    db = _make_db(tmp_path)
    # Stub a paper_signal_decisions row with id 1.
    db.conn.execute(
        "INSERT INTO paper_signal_decisions "
        "(candidate_id, wallet_id, signal_family, final_verdict, "
        " is_approved, idempotency_key, computed_at, created_at) "
        "VALUES (1, '0xW', 'copy_candidate', 'copy_candidate', 0, 'k1', "
        " '2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z')"
    )
    db.conn.commit()

    eval_ts = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
    ids = record_exit_experiments(
        db, paper_signal_id=1, signal_evaluation_timestamp=eval_ts,
    )
    assert len(ids) == 7

    rows = db.fetchall(
        "SELECT experiment_type, scheduled_at FROM exit_experiment_registrations "
        "WHERE paper_signal_id = 1 ORDER BY experiment_type"
    )
    assert len(rows) == 7
    types = {r["experiment_type"] for r in rows}
    assert types == CANONICAL_EXPECTED

    # EXIT_24H scheduled exactly 24h after eval.
    e24 = [r for r in rows if r["experiment_type"] == "EXIT_24H"][0]
    assert e24["scheduled_at"] is not None
    sched_dt = datetime.fromisoformat(e24["scheduled_at"].replace("Z", "+00:00"))
    delta = sched_dt - eval_ts
    assert delta.total_seconds() == 24 * 3600

    # EXIT_72H scheduled exactly 72h after eval.
    e72 = [r for r in rows if r["experiment_type"] == "EXIT_72H"][0]
    sched_dt = datetime.fromisoformat(e72["scheduled_at"].replace("Z", "+00:00"))
    delta = sched_dt - eval_ts
    assert delta.total_seconds() == 72 * 3600

    # Other five have NULL scheduled_at.
    null_tracks = {
        "HOLD_TO_RESOLUTION",
        "FAVORABLE_MOVE_005",
        "FAVORABLE_MOVE_010",
        "FAVORABLE_MOVE_015",
        "THESIS_OR_LIQUIDITY_FAILURE",
    }
    for r in rows:
        if r["experiment_type"] in null_tracks:
            assert r["scheduled_at"] is None, (
                f"{r['experiment_type']} should have NULL scheduled_at"
            )


def test_record_exit_experiments_rerun_no_duplicates(tmp_path: Path):
    db = _make_db(tmp_path)
    db.conn.execute(
        "INSERT INTO paper_signal_decisions "
        "(candidate_id, wallet_id, signal_family, final_verdict, "
        " is_approved, idempotency_key, computed_at, created_at) "
        "VALUES (1, '0xW', 'copy_candidate', 'copy_candidate', 0, 'k1', "
        " '2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z')"
    )
    db.conn.commit()

    eval_ts = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
    record_exit_experiments(db, paper_signal_id=1, signal_evaluation_timestamp=eval_ts)
    record_exit_experiments(db, paper_signal_id=1, signal_evaluation_timestamp=eval_ts)
    n = db.fetchone(
        "SELECT COUNT(*) AS n FROM exit_experiment_registrations WHERE paper_signal_id=1"
    )
    assert int(n["n"]) == 7


def test_no_orders_or_positions_created_empty(tmp_path: Path):
    """Exit experiment registration must NEVER insert rows into
    ``orders`` or ``positions``. We confirm both tables stay empty
    after registration."""
    db = _make_db(tmp_path)
    db.conn.execute(
        "INSERT INTO paper_signal_decisions "
        "(candidate_id, wallet_id, signal_family, final_verdict, "
        " is_approved, idempotency_key, computed_at, created_at) "
        "VALUES (1, '0xW', 'copy_candidate', 'copy_candidate', 0, 'k1', "
        " '2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z')"
    )
    db.conn.commit()

    eval_ts = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
    record_exit_experiments(db, paper_signal_id=1, signal_evaluation_timestamp=eval_ts)

    orders_count = db.fetchone("SELECT COUNT(*) AS n FROM orders")
    positions_count = db.fetchone("SELECT COUNT(*) AS n FROM positions")
    assert int(orders_count["n"]) == 0
    assert int(positions_count["n"]) == 0


def test_different_signal_ids_get_own_seven(tmp_path: Path):
    db = _make_db(tmp_path)
    # Add a second candidate to satisfy the FK for a second paper_signal.
    db.conn.execute(
        "INSERT INTO copy_candidates ("
        "wallet_id, source, source_trade_id, side, "
        "source_trade_price, source_trade_quantity, "
        "source_trade_timestamp, observed_at, "
        "wallet_score_version, wallet_score, wallet_verdict, status, "
        "created_at, updated_at) "
        "VALUES ('0xW', 'test', 't-2', 'BUY', 0.5, 1.0, "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z', "
        "'1', 0.0, 'incomplete', 'PENDING_PRICE_CHECK', "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z')"
    )
    db.conn.execute(
        "INSERT INTO paper_signal_decisions "
        "(candidate_id, wallet_id, signal_family, final_verdict, "
        " is_approved, idempotency_key, computed_at, created_at) "
        "VALUES (1, '0xW', 'copy_candidate', 'copy_candidate', 0, 'k1', "
        " '2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z')"
    )
    db.conn.execute(
        "INSERT INTO paper_signal_decisions "
        "(candidate_id, wallet_id, signal_family, final_verdict, "
        " is_approved, idempotency_key, computed_at, created_at) "
        "VALUES (2, '0xW', 'copy_candidate', 'copy_candidate', 0, 'k2', "
        " '2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z')"
    )
    db.conn.commit()

    eval_ts = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
    record_exit_experiments(db, paper_signal_id=1, signal_evaluation_timestamp=eval_ts)
    record_exit_experiments(db, paper_signal_id=2, signal_evaluation_timestamp=eval_ts)
    n = db.fetchone("SELECT COUNT(*) AS n FROM exit_experiment_registrations")
    assert int(n["n"]) == 14


def test_missing_evaluation_timestamp_falls_back_to_null(tmp_path: Path):
    """When no evaluation timestamp is supplied, the EXIT_24H /
    EXIT_72H scheduled_at is NULL — never silently a fabricated
    timestamp from wall-clock now()."""
    db = _make_db(tmp_path)
    db.conn.execute(
        "INSERT INTO paper_signal_decisions "
        "(candidate_id, wallet_id, signal_family, final_verdict, "
        " is_approved, idempotency_key, computed_at, created_at) "
        "VALUES (1, '0xW', 'copy_candidate', 'copy_candidate', 0, 'k1', "
        " '2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z')"
    )
    db.conn.commit()
    record_exit_experiments(db, paper_signal_id=1)  # no timestamp
    rows = db.fetchall(
        "SELECT experiment_type, scheduled_at FROM exit_experiment_registrations"
    )
    for r in rows:
        assert r["scheduled_at"] is None, (
            f"Without an evaluation timestamp, {r['experiment_type']} "
            "must have NULL scheduled_at"
        )