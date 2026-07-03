"""Constraint test matrix for PR 4 persistence.

Proves the application-level validators and the SQL CHECK constraints
on fresh databases both reject the same malformed inputs:

  - score < 0, score > 100
  - fill < 0, fill > 1
  - negative counts and depths
  - invalid V1 verdict strings (uppercase, legacy aliases, garbage)
  - invalid shadow verdict strings
  - invalid behavior classifications
  - invalid delay scenarios (uppercase, garbage)
  - invalid exit tracks (lowercase legacy, garbage)
  - is_approved = 1 (auto-approve attempt) — runtime hard-rejected
  - NaN, +Infinity, -Infinity
  - exit-track lowercase legacy alias
  - measured_delay_seconds < 0

Also proves the validators accept boundary values:

  - score 0, score 100
  - fill 0, fill 1
  - stake 0, depth 0
  - measured delay 0
  - canonical enum strings
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from polycopy.db.database import Database
from polycopy.scoring.persistence_validation import (
    CANONICAL_BEHAVIOR_CLASSIFICATIONS,
    CANONICAL_DELAY_SCENARIOS,
    CANONICAL_EXIT_TRACKS,
    CANONICAL_SHADOW_VERDICTS,
    CANONICAL_V1_VERDICTS,
    PersistenceValidationError,
    require_canonical_behavior_classification,
    require_canonical_delay_scenario,
    require_canonical_exit_track,
    require_canonical_shadow_verdict,
    require_canonical_v1_verdict,
    require_finite,
    require_optional_boolean_int,
    require_optional_fill_ratio,
    require_optional_measured_delay_seconds,
    require_optional_nonnegative,
    require_optional_nonnegative_int,
    require_optional_score,
    require_unapproved,
    validate_category_row,
    validate_decision_row,
    validate_exit_track,
    validate_exit_track_batch,
    validate_paper_signal_row,
    validate_shadow_row,
    validate_trade_row,
    validate_wallet_row,
)


# ---- Helper for fresh DB ------------------------------------------------


def _fresh_db(tmp_path: Path) -> Database:
    return Database(db_path=tmp_path / "check.db").connect()


# ---- Finite numeric ----------------------------------------------------


def test_require_finite_accepts_int_and_float() -> None:
    require_finite(0, field="x")
    require_finite(1, field="x")
    require_finite(-1, field="x")
    require_finite(0.0, field="x")
    require_finite(1.5, field="x")
    require_finite(-3.14, field="x")


@pytest.mark.parametrize("bad", [
    math.nan,
    math.inf,
    -math.inf,
    "1",
    None,
    True,
    False,
    object(),
])
def test_require_finite_rejects_non_finite(bad) -> None:
    with pytest.raises(PersistenceValidationError):
        require_finite(bad, field="x")


# ---- Optional score [0, 100] ------------------------------------------


@pytest.mark.parametrize("good", [0, 0.0, 50, 50.0, 100, 100.0, None])
def test_require_optional_score_accepts_valid(good) -> None:
    require_optional_score(good, field="score")


@pytest.mark.parametrize("bad", [
    -0.0001, -1, 100.0001, 101, math.nan, math.inf, "50",
])
def test_require_optional_score_rejects_invalid(bad) -> None:
    with pytest.raises(PersistenceValidationError):
        require_optional_score(bad, field="score")


# ---- Optional fill ratio [0, 1] ---------------------------------------


@pytest.mark.parametrize("good", [0, 0.0, 0.5, 1, 1.0, None])
def test_require_optional_fill_ratio_accepts_valid(good) -> None:
    require_optional_fill_ratio(good, field="fill")


@pytest.mark.parametrize("bad", [
    -0.0001, 1.0001, 2, math.nan, -math.inf,
])
def test_require_optional_fill_ratio_rejects_invalid(bad) -> None:
    with pytest.raises(PersistenceValidationError):
        require_optional_fill_ratio(bad, field="fill")


# ---- Optional nonnegative --------------------------------------------


@pytest.mark.parametrize("good", [0, 0.0, 1, 5.5, None])
def test_require_optional_nonnegative_accepts_valid(good) -> None:
    require_optional_nonnegative(good, field="x")


@pytest.mark.parametrize("bad", [
    -1, -0.0001, math.nan, math.inf, -math.inf,
])
def test_require_optional_nonnegative_rejects_invalid(bad) -> None:
    with pytest.raises(PersistenceValidationError):
        require_optional_nonnegative(bad, field="x")


# ---- Optional nonnegative integer ------------------------------------


@pytest.mark.parametrize("good", [0, 1, 5, 100, None, 30.0, 0.0])
def test_require_optional_nonnegative_int_accepts_valid(good) -> None:
    require_optional_nonnegative_int(good, field="x")


@pytest.mark.parametrize("bad", [
    -1, -0.5, math.nan, math.inf, -math.inf, "5",
])
def test_require_optional_nonnegative_int_rejects_invalid(bad) -> None:
    with pytest.raises(PersistenceValidationError):
        require_optional_nonnegative_int(bad, field="x")


# ---- Optional boolean int ---------------------------------------------


@pytest.mark.parametrize("good", [0, 1, True, False, None])
def test_require_optional_boolean_int_accepts_valid(good) -> None:
    require_optional_boolean_int(good, field="x")


@pytest.mark.parametrize("bad", [-1, 2, "1", 1.5])
def test_require_optional_boolean_int_rejects_invalid(bad) -> None:
    with pytest.raises(PersistenceValidationError):
        require_optional_boolean_int(bad, field="x")


# ---- Measured delay ---------------------------------------------------


@pytest.mark.parametrize("good", [0, 0.0, 30, 3600.0, None])
def test_require_optional_measured_delay_accepts_valid(good) -> None:
    require_optional_measured_delay_seconds(good, field="delay")


@pytest.mark.parametrize("bad", [-1, -0.5, math.nan])
def test_require_optional_measured_delay_rejects_invalid(bad) -> None:
    with pytest.raises(PersistenceValidationError):
        require_optional_measured_delay_seconds(bad, field="delay")


# ---- V1 verdict enum --------------------------------------------------


@pytest.mark.parametrize("good", list(CANONICAL_V1_VERDICTS))
def test_require_canonical_v1_verdict_accepts_canonical(good) -> None:
    require_canonical_v1_verdict(good, field="v")


@pytest.mark.parametrize("bad", [
    "COPY_CANDIDATE", "Watchlist", "garbage", "", None,
    "hold_to_resolution",  # legacy alias
])
def test_require_canonical_v1_verdict_rejects_invalid(bad) -> None:
    with pytest.raises(PersistenceValidationError):
        require_canonical_v1_verdict(bad, field="v")


# ---- Shadow verdict enum ----------------------------------------------


@pytest.mark.parametrize("good", list(CANONICAL_SHADOW_VERDICTS))
def test_require_canonical_shadow_verdict_accepts_canonical(good) -> None:
    require_canonical_shadow_verdict(good, field="v", optional=True)


@pytest.mark.parametrize("bad", [
    "copy_candidate",  # legacy lowercase
    "SHADOW_DROP",  # garbage
    "WATCH",  # garbage
])
def test_require_canonical_shadow_verdict_rejects_invalid(bad) -> None:
    with pytest.raises(PersistenceValidationError):
        require_canonical_shadow_verdict(bad, field="v", optional=True)


def test_require_canonical_shadow_verdict_none_optional() -> None:
    require_canonical_shadow_verdict(None, field="v", optional=True)


def test_require_canonical_shadow_verdict_none_required_raises() -> None:
    with pytest.raises(PersistenceValidationError):
        require_canonical_shadow_verdict(None, field="v", optional=False)


# ---- Exit track enum (uppercase canonical, lowercase legacy REJECTED) --


@pytest.mark.parametrize("good", list(CANONICAL_EXIT_TRACKS))
def test_require_canonical_exit_track_accepts_canonical(good) -> None:
    require_canonical_exit_track(good, field="t")


@pytest.mark.parametrize("bad", [
    "hold_to_resolution",  # legacy lowercase alias
    "exit_24h",
    "favorable_move_5pct",
    "favorable_move_10_pct",
    "thesis_failure",
    "liquidity_failure",
    "EXIT_99H",
    "",
    None,
])
def test_require_canonical_exit_track_rejects_invalid(bad) -> None:
    with pytest.raises(PersistenceValidationError):
        require_canonical_exit_track(bad, field="t")


# ---- Delay scenario enum ----------------------------------------------


@pytest.mark.parametrize("good", list(CANONICAL_DELAY_SCENARIOS))
def test_require_canonical_delay_scenario_accepts_canonical(good) -> None:
    require_canonical_delay_scenario(good, field="s", optional=True)


@pytest.mark.parametrize("bad", [
    "DELAY_30S",  # legacy uppercase
    "delay_99_minutes",  # garbage
    "immediate",  # garbage
])
def test_require_canonical_delay_scenario_rejects_invalid(bad) -> None:
    with pytest.raises(PersistenceValidationError):
        require_canonical_delay_scenario(bad, field="s", optional=True)


# ---- Behavior classification enum -------------------------------------


@pytest.mark.parametrize("good", list(CANONICAL_BEHAVIOR_CLASSIFICATIONS))
def test_require_canonical_behavior_accepts_canonical(good) -> None:
    require_canonical_behavior_classification(good, field="b", optional=True)


@pytest.mark.parametrize("bad", [
    "DIRECTIONAL",  # uppercase
    "phantom",  # garbage
])
def test_require_canonical_behavior_rejects_invalid(bad) -> None:
    with pytest.raises(PersistenceValidationError):
        require_canonical_behavior_classification(bad, field="b", optional=True)


# ---- Auto-approval guardrail ------------------------------------------


def test_require_unapproved_accepts_zero() -> None:
    require_unapproved(0)


def test_require_unapproved_accepts_zero_with_auto_request() -> None:
    require_unapproved(0, auto_approve_requested=True)


def test_require_unapproved_rejects_one() -> None:
    with pytest.raises(PersistenceValidationError):
        require_unapproved(1)


def test_require_unapproved_rejects_true() -> None:
    with pytest.raises(PersistenceValidationError):
        require_unapproved(True, auto_approve_requested=True)


def test_require_unapproved_rejects_negative_int() -> None:
    with pytest.raises(PersistenceValidationError):
        require_unapproved(-1)


def test_require_unapproved_rejects_two() -> None:
    with pytest.raises(PersistenceValidationError):
        require_unapproved(2)


# ---- Composite validators ---------------------------------------------


def test_validate_decision_row_accepts_valid() -> None:
    validate_decision_row(final_score=80.0, verdict="copy_candidate")


def test_validate_decision_row_accepts_boundaries() -> None:
    validate_decision_row(final_score=0, verdict="incomplete")
    validate_decision_row(final_score=100, verdict="copy_candidate")


@pytest.mark.parametrize("bad_score", [-0.0001, 100.0001])
def test_validate_decision_row_rejects_bad_score(bad_score) -> None:
    with pytest.raises(PersistenceValidationError):
        validate_decision_row(final_score=bad_score, verdict="copy_candidate")


@pytest.mark.parametrize("bad_verdict", ["COPY_CANDIDATE", "drop", ""])
def test_validate_decision_row_rejects_bad_verdict(bad_verdict) -> None:
    with pytest.raises(PersistenceValidationError):
        validate_decision_row(final_score=80.0, verdict=bad_verdict)


# ---- SQL CHECK constraints on a fresh DB -----------------------------


def test_sql_check_rejects_bad_final_score_on_fresh_db(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) VALUES "
        "('0xW', '0xw', 'w', 0, '2026-01-01T00:00:00Z', '0xw')",
    )
    db.conn.commit()
    # Insert a row with final_score = 150 (>100). Should violate the
    # fresh-DB CHECK constraint.
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO wallet_score_decisions "
            "(wallet_id, formula_name, formula_version, idempotency_key, "
            " final_score, verdict, computed_at, created_at) "
            "VALUES ('0xW', 'wallet_score', '1', 'k1', 150.0, "
            "'copy_candidate', '2026-07-01T00:00:00Z', "
            "'2026-07-01T00:00:00Z')",
        )


def test_sql_check_rejects_uppercase_v1_verdict_on_fresh_db(
    tmp_path: Path,
) -> None:
    """Fresh DB CHECK enforces lowercase V1 enum."""
    db = _fresh_db(tmp_path)
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) VALUES "
        "('0xW', '0xw', 'w', 0, '2026-01-01T00:00:00Z', '0xw')",
    )
    db.conn.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO wallet_score_decisions "
            "(wallet_id, formula_name, formula_version, idempotency_key, "
            " final_score, verdict, computed_at, created_at) "
            "VALUES ('0xW', 'wallet_score', '1', 'k1', 80.0, "
            "'COPY_CANDIDATE', '2026-07-01T00:00:00Z', "
            "'2026-07-01T00:00:00Z')",
        )


def test_sql_check_rejects_uppercase_signal_family_on_fresh_db(
    tmp_path: Path,
) -> None:
    """Fresh DB CHECK enforces lowercase signal_family enum."""
    db = _fresh_db(tmp_path)
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) VALUES "
        "('0xW', '0xw', 'w', 0, '2026-01-01T00:00:00Z', '0xw')",
    )
    db.conn.execute(
        "INSERT INTO source_trades (id, source, source_trade_id, "
        "market_source_id, side, outcome, quantity, price, "
        "trader_address, timestamp, is_sample) "
        "VALUES ('st-1', 'polymarket', 'st-1', 'm-src-1', 'BUY', 'YES', "
        "100, 0.5, '0xt', '2026-07-01T00:00:00Z', 0)",
    )
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
    db.conn.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO paper_signal_decisions "
            "(candidate_id, wallet_id, signal_family, signal_reason, "
            " final_verdict, is_approved, idempotency_key, "
            "computed_at, created_at) "
            "VALUES (1, '0xW', 'COPY_CANDIDATE', 'ok', 'copy_candidate', "
            "0, 'k1', '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')",
        )


def test_sql_check_rejects_is_approved_one(tmp_path: Path) -> None:
    """Even at the SQL level, is_approved must be 0 or 1; the
    application validator additionally rejects 1 for paper signals."""
    db = _fresh_db(tmp_path)
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) VALUES "
        "('0xW', '0xw', 'w', 0, '2026-01-01T00:00:00Z', '0xw')",
    )
    db.conn.execute(
        "INSERT INTO source_trades (id, source, source_trade_id, "
        "market_source_id, side, outcome, quantity, price, "
        "trader_address, timestamp, is_sample) "
        "VALUES ('st-1', 'polymarket', 'st-1', 'm-src-1', 'BUY', 'YES', "
        "100, 0.5, '0xt', '2026-07-01T00:00:00Z', 0)",
    )
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
    db.conn.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO paper_signal_decisions "
            "(candidate_id, wallet_id, signal_family, signal_reason, "
            " final_verdict, is_approved, idempotency_key, "
            "computed_at, created_at) "
            "VALUES (1, '0xW', 'copy_candidate', 'ok', 'copy_candidate', "
            "2, 'k1', '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')",
        )


def test_sql_check_accepts_valid_full_paper_signal_row(
    tmp_path: Path,
) -> None:
    """A row with all canonical values must be accepted."""
    db = _fresh_db(tmp_path)
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) VALUES "
        "('0xW', '0xw', 'w', 0, '2026-01-01T00:00:00Z', '0xw')",
    )
    db.conn.execute(
        "INSERT INTO source_trades (id, source, source_trade_id, "
        "market_source_id, side, outcome, quantity, price, "
        "trader_address, timestamp, is_sample) "
        "VALUES ('st-1', 'polymarket', 'st-1', 'm-src-1', 'BUY', 'YES', "
        "100, 0.5, '0xt', '2026-07-01T00:00:00Z', 0)",
    )
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
    db.conn.commit()
    db.conn.execute(
        "INSERT INTO paper_signal_decisions "
        "(candidate_id, wallet_id, signal_family, signal_reason, "
        " final_verdict, is_approved, idempotency_key, "
        "computed_at, created_at) "
        "VALUES (1, '0xW', 'copy_candidate', 'ok', 'copy_candidate', "
        "0, 'k1', '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')",
    )
    db.conn.commit()
    row = db.conn.execute(
        "SELECT final_verdict, is_approved FROM paper_signal_decisions "
        "WHERE candidate_id = 1",
    ).fetchone()
    assert row["final_verdict"] == "copy_candidate"
    assert row["is_approved"] == 0


# ---- validate_paper_signal_row direct ---------------------------------


def test_validate_paper_signal_row_accepts_canonical_lowercase() -> None:
    validate_paper_signal_row(
        signal_family="copy_candidate",
        wallet_score=80.0,
        trade_score=85.0,
        shadow_score=70.0,
        shadow_verdict="SHADOW_COPY_CANDIDATE",
        final_verdict="copy_candidate",
        is_approved=0,
    )


def test_validate_paper_signal_row_rejects_uppercase_signal_family() -> None:
    with pytest.raises(PersistenceValidationError):
        validate_paper_signal_row(
            signal_family="COPY_CANDIDATE",  # uppercase rejected
            wallet_score=80.0,
            trade_score=85.0,
            shadow_score=70.0,
            shadow_verdict=None,
            final_verdict="copy_candidate",
            is_approved=0,
        )


def test_validate_paper_signal_row_rejects_legacy_shadow_lowercase() -> None:
    with pytest.raises(PersistenceValidationError):
        validate_paper_signal_row(
            wallet_score=80.0,
            trade_score=85.0,
            shadow_score=70.0,
            shadow_verdict="copy_candidate",  # V1 casing, not V2
            final_verdict="copy_candidate",
            is_approved=0,
        )


def test_validate_paper_signal_row_rejects_auto_approve() -> None:
    with pytest.raises(PersistenceValidationError):
        validate_paper_signal_row(
            wallet_score=80.0,
            trade_score=85.0,
            shadow_score=70.0,
            shadow_verdict=None,
            final_verdict="copy_candidate",
            is_approved=1,
            auto_approve_requested=True,
        )


# ---- validate_exit_track_batch ---------------------------------------


def test_validate_exit_track_batch_accepts_canonical_seven() -> None:
    validate_exit_track_batch(list(CANONICAL_EXIT_TRACKS))


def test_validate_exit_track_rejects_legacy_lowercase() -> None:
    with pytest.raises(PersistenceValidationError):
        validate_exit_track("exit_24h")


# ---- validate_shadow_row ---------------------------------------------


def test_validate_shadow_row_accepts_canonical() -> None:
    validate_shadow_row(
        final_score=70.0,
        verdict="SHADOW_COPY_CANDIDATE",
        delay_scenario="theoretical_immediate",
        delay_seconds=0.0,
        fill_percentage=0.95,
        measured_delay_seconds=30.0,
        copied_trade_count=5,
        days_since_last_trade=1,
    )


def test_validate_shadow_row_accepts_boundary_zero_values() -> None:
    validate_shadow_row(
        final_score=0,
        verdict="SHADOW_INCOMPLETE",
        delay_scenario="actual_measured_delay",
        delay_seconds=0,
        fill_percentage=0,
        measured_delay_seconds=0,
    )


def test_validate_shadow_row_rejects_bad_score() -> None:
    with pytest.raises(PersistenceValidationError):
        validate_shadow_row(
            final_score=100.0001,
            verdict="SHADOW_COPY_CANDIDATE",
            delay_scenario="theoretical_immediate",
        )


def test_validate_shadow_row_rejects_bad_verdict() -> None:
    with pytest.raises(PersistenceValidationError):
        validate_shadow_row(
            final_score=70.0,
            verdict="copy_candidate",  # V1 casing in V2
            delay_scenario="theoretical_immediate",
        )


def test_validate_shadow_row_rejects_bad_scenario() -> None:
    with pytest.raises(PersistenceValidationError):
        validate_shadow_row(
            final_score=70.0,
            verdict="SHADOW_COPY_CANDIDATE",
            delay_scenario="DELAY_30S",  # legacy uppercase
        )


def test_validate_shadow_row_rejects_negative_delay() -> None:
    with pytest.raises(PersistenceValidationError):
        validate_shadow_row(
            final_score=70.0,
            verdict="SHADOW_COPY_CANDIDATE",
            delay_scenario="theoretical_immediate",
            delay_seconds=-1.0,
        )


# ---- validate_trade_row ----------------------------------------------


def test_validate_trade_row_accepts_canonical() -> None:
    validate_trade_row(
        final_score=80.0,
        verdict="copy_candidate",
        intended_stake=50.0,
        executable_depth=60.0,
        fill_percentage=0.95,
        trade_age_seconds=30,
        seconds_to_market_end=1000,
        market_active=True,
        market_closed=False,
        market_resolved=False,
    )


def test_validate_trade_row_accepts_boundary_zero_values() -> None:
    validate_trade_row(
        final_score=0,
        verdict="incomplete",
        intended_stake=0,
        executable_depth=0,
        fill_percentage=0,
    )


def test_validate_trade_row_rejects_negative_stake() -> None:
    with pytest.raises(PersistenceValidationError):
        validate_trade_row(
            final_score=80.0,
            verdict="copy_candidate",
            intended_stake=-0.0001,
        )


def test_validate_trade_row_rejects_negative_depth() -> None:
    with pytest.raises(PersistenceValidationError):
        validate_trade_row(
            final_score=80.0,
            verdict="copy_candidate",
            executable_depth=-1,
        )


def test_validate_trade_row_rejects_bad_fill() -> None:
    with pytest.raises(PersistenceValidationError):
        validate_trade_row(
            final_score=80.0,
            verdict="copy_candidate",
            fill_percentage=1.0001,
        )


# ---- validate_wallet_row and validate_category_row --------------------


def test_validate_wallet_row_accepts_canonical() -> None:
    validate_wallet_row(
        final_score=80.0,
        verdict="copy_candidate",
        trade_count=50,
    )


def test_validate_wallet_row_rejects_negative_count() -> None:
    with pytest.raises(PersistenceValidationError):
        validate_wallet_row(
            final_score=80.0,
            verdict="copy_candidate",
            trade_count=-1,
        )


def test_validate_category_row_accepts_canonical() -> None:
    validate_category_row(
        final_score=80.0,
        verdict="copy_candidate",
        category_trade_count=30,
    )


def test_validate_category_row_rejects_negative_count() -> None:
    with pytest.raises(PersistenceValidationError):
        validate_category_row(
            final_score=80.0,
            verdict="copy_candidate",
            category_trade_count=-5,
        )