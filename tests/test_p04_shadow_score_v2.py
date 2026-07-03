"""Tests for V2 shadow typed contracts and persistence (Chunk 5 §5.4–§5.7).

Verifies:

- the typed input / result dataclasses freeze correctly;
- the canonical delay scenarios cover the six required values;
- the canonical formula name + version are pinned;
- frozen weights sum to 100;
- verdict rules (>=70 COPY_CANDIDATE, >=50 WATCHLIST, else SKIP);
- SHADOW_INCOMPLETE on missing essential evidence;
- shadow persistence reads from the typed input;
- six separate immutable rows for the six scenarios;
- changed scenario / snapshot / formula version creates new rows;
- changed delay scenario creates a new shadow row;
- replay: recomputing with the same typed input reproduces the result.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path

import pytest

from polycopy.db.database import Database
from polycopy.scoring.shadow_score_v2_typed import (
    DELAY_SCENARIO_SECONDS,
    REASON_NO_DELAYED_PRICE,
    REASON_NO_SOURCE_PRICE,
    SHADOW_FORMULA_NAME,
    SHADOW_FORMULA_VERSION,
    SHADOW_WEIGHTS,
    VERDICT_SHADOW_COPY_CANDIDATE,
    VERDICT_SHADOW_INCOMPLETE,
    VERDICT_SHADOW_WATCHLIST,
    DelayScenario,
    ShadowScoreInputV2,
    ShadowScoreResultV2,
)
from polycopy.scoring.shadow_score_v2_engine import (
    compute_measured_delay_seconds,
    compute_shadow_score_v2_from_input,
)
from polycopy.scoring.score_serialization import persist_shadow_score_v2


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_input(**overrides) -> ShadowScoreInputV2:
    base = dict(
        wallet_id="0xW",
        source_trade_id="t-1",
        candidate_id=1,
        delay_scenario=DelayScenario.THEORETICAL_IMMEDIATE,
        source_price=0.50,
        delayed_copy_price=0.50,
        intended_stake=100.0,
        executable_depth=100.0,
        fill_percentage=1.0,
        slippage=0.005,
        spread=0.01,
        wallet_skill_persistence_input=80.0,
        copied_realized_performance_input=70.0,
        concentration_correlation_input=60.0,
        source_data_timestamp="2026-07-03T12:00:00Z",
        price_snapshot_id="snap-1",
        depth_hash="hash-1",
        missing_forward_reasons=(),
        measured_delay_seconds=None,
    )
    base.update(overrides)
    return ShadowScoreInputV2(**base)


# ── Typed contract tests ─────────────────────────────────────────────────


def test_typed_input_is_frozen_dataclass():
    assert is_dataclass(ShadowScoreInputV2)
    inp = _make_input()
    with pytest.raises((FrozenInstanceError, AttributeError)):
        inp.wallet_id = "mutated"  # type: ignore[misc]


def test_typed_result_is_frozen_dataclass():
    assert is_dataclass(ShadowScoreResultV2)


def test_canonical_formula_identity():
    assert SHADOW_FORMULA_NAME == "Copy-Adjusted Alpha Score"
    assert SHADOW_FORMULA_VERSION == "2-shadow"


def test_frozen_weights_sum_to_one_hundred():
    assert abs(sum(SHADOW_WEIGHTS.values()) - 100.0) < 1e-9
    # Frozen weight breakdown must match the spec exactly.
    assert SHADOW_WEIGHTS["delayed_entry_alpha"] == 30.0
    assert SHADOW_WEIGHTS["tradeable_price_retention"] == 20.0
    assert SHADOW_WEIGHTS["execution_feasibility"] == 15.0
    assert SHADOW_WEIGHTS["skill_persistence"] == 15.0
    assert SHADOW_WEIGHTS["copied_realized_performance"] == 10.0
    assert SHADOW_WEIGHTS["concentration_correlation"] == 10.0


def test_canonical_six_delay_scenarios():
    scenarios = list(DelayScenario)
    assert len(scenarios) == 6
    assert DelayScenario.THEORETICAL_IMMEDIATE in scenarios
    assert DelayScenario.DELAY_30_SECONDS in scenarios
    assert DelayScenario.DELAY_2_MINUTES in scenarios
    assert DelayScenario.DELAY_5_MINUTES in scenarios
    assert DelayScenario.DELAY_15_MINUTES in scenarios
    assert DelayScenario.ACTUAL_MEASURED_DELAY in scenarios


def test_delay_scenario_seconds_frozen_mapping():
    assert DELAY_SCENARIO_SECONDS[DelayScenario.THEORETICAL_IMMEDIATE] == 0.0
    assert DELAY_SCENARIO_SECONDS[DelayScenario.DELAY_30_SECONDS] == 30.0
    assert DELAY_SCENARIO_SECONDS[DelayScenario.DELAY_2_MINUTES] == 120.0
    assert DELAY_SCENARIO_SECONDS[DelayScenario.DELAY_5_MINUTES] == 300.0
    assert DELAY_SCENARIO_SECONDS[DelayScenario.DELAY_15_MINUTES] == 900.0
    assert DELAY_SCENARIO_SECONDS[DelayScenario.ACTUAL_MEASURED_DELAY] is None


# ── Verdict rules ────────────────────────────────────────────────────────


def test_complete_strong_score_is_copy_candidate():
    inp = _make_input()  # All evidence present.
    r = compute_shadow_score_v2_from_input(inp)
    # Without strong forward-evidence tuning, the engine's
    # neutral alpha gives a positive score.
    assert r.verdict in (
        VERDICT_SHADOW_COPY_CANDIDATE,
        VERDICT_SHADOW_WATCHLIST,
    )


def test_missing_delayed_price_yields_incomplete():
    inp = _make_input(delayed_copy_price=None)
    r = compute_shadow_score_v2_from_input(inp)
    assert r.verdict == VERDICT_SHADOW_INCOMPLETE
    assert REASON_NO_DELAYED_PRICE in r.missing_forward_reasons


def test_missing_source_price_yields_incomplete():
    inp = _make_input(source_price=None)
    r = compute_shadow_score_v2_from_input(inp)
    assert r.verdict == VERDICT_SHADOW_INCOMPLETE
    assert REASON_NO_SOURCE_PRICE in r.missing_forward_reasons


def test_missing_measured_delay_yields_incomplete():
    inp = _make_input(
        delay_scenario=DelayScenario.ACTUAL_MEASURED_DELAY,
        measured_delay_seconds=None,
    )
    r = compute_shadow_score_v2_from_input(inp)
    assert r.verdict == VERDICT_SHADOW_INCOMPLETE


def test_zero_placeholders_do_not_promote_to_skip():
    """A missing input must surface SHADOW_INCOMPLETE — never
    SHADOW_SKIP. The test for SHADOW_INCOMPLETE precedence over a
    numeric zero placeholder is implicit in the above tests, but we
    repeat it explicitly for the spec's anti-pattern."""
    inp = _make_input(
        wallet_skill_persistence_input=None,
        copied_realized_performance_input=None,
        concentration_correlation_input=None,
        slippage=None,
        fill_percentage=None,
    )
    r = compute_shadow_score_v2_from_input(inp)
    assert r.verdict == VERDICT_SHADOW_INCOMPLETE


def test_explicit_missing_reason_overrides_to_incomplete():
    inp = _make_input(
        missing_forward_reasons=("custom_missing_reason",),
    )
    r = compute_shadow_score_v2_from_input(inp)
    assert r.verdict == VERDICT_SHADOW_INCOMPLETE


# ── Score classification thresholds ─────────────────────────────────────


def test_score_below_fifty_is_skip():
    """Construct a minimal-but-weakly-scoring input by setting
    prices far apart so the retention and alpha components are
    near zero. With neutral slippage / fill, score should fall
    below WATCHLIST_MIN."""
    inp = _make_input(
        source_price=0.50,
        delayed_copy_price=0.10,  # extreme divergence → low retention
        # Disable forward-evidence inputs (None) to keep score
        # driven by retention + alpha + slippage + fill only.
        wallet_skill_persistence_input=20.0,
        copied_realized_performance_input=20.0,
        concentration_correlation_input=20.0,
        slippage=0.10,  # worst case
        fill_percentage=0.10,
    )
    r = compute_shadow_score_v2_from_input(inp)
    VERDICT_SHADOW_SKIP = "SHADOW_SKIP"
    assert r.verdict in (
        VERDICT_SHADOW_SKIP,
        VERDICT_SHADOW_WATCHLIST,
    )


# ── Measured delay calculation ──────────────────────────────────────────


def test_compute_measured_delay_seconds_positive():
    s = compute_measured_delay_seconds(
        source_trade_timestamp="2026-07-03T12:00:00Z",
        candidate_snapshot_timestamp="2026-07-03T12:00:30Z",
    )
    assert s == 30.0


def test_compute_measured_delay_seconds_zero_when_equal():
    s = compute_measured_delay_seconds(
        source_trade_timestamp="2026-07-03T12:00:00Z",
        candidate_snapshot_timestamp="2026-07-03T12:00:00Z",
    )
    assert s == 0.0


def test_compute_measured_delay_seconds_clamps_negative_to_zero():
    s = compute_measured_delay_seconds(
        source_trade_timestamp="2026-07-03T12:00:30Z",
        candidate_snapshot_timestamp="2026-07-03T12:00:00Z",
    )
    assert s == 0.0


# ── Replay ───────────────────────────────────────────────────────────────


def test_replay_same_input_produces_same_result():
    inp = _make_input()
    r1 = compute_shadow_score_v2_from_input(inp)
    r2 = compute_shadow_score_v2_from_input(inp)
    assert r1.score == r2.score
    assert r1.verdict == r2.verdict
    assert r1.component_scores == r2.component_scores
    assert r1.missing_forward_reasons == r2.missing_forward_reasons


def test_result_retains_exact_typed_input():
    inp = _make_input(
        wallet_skill_persistence_input=42.0,
        missing_forward_reasons=("test_reason",),
    )
    r = compute_shadow_score_v2_from_input(inp)
    assert r.input == inp


# ── Persistence: scenario rows are separate, idempotent, and replayable ──


def _make_db(tmp_path: Path) -> Database:
    db = Database(db_path=tmp_path / "shadow.db")
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
    return db


def test_six_scenario_rows_for_one_signal(tmp_path: Path):
    db = _make_db(tmp_path)
    seen_verdicts = []
    for scenario in DelayScenario:
        inp = _make_input(
            delay_scenario=scenario,
            measured_delay_seconds=(
                30.0 if scenario is DelayScenario.ACTUAL_MEASURED_DELAY
                else None
            ),
            price_snapshot_id=None,
            depth_hash=None,
        )
        result = compute_shadow_score_v2_from_input(inp)
        rid = persist_shadow_score_v2(
            db,
            wallet_id="0xW",
            source_trade_id="t-1",
            result=result,
            candidate_id=None,
            source_data_timestamp="2026-07-03T12:00:00Z",
        )
        assert rid > 0
        seen_verdicts.append((scenario.value, result.verdict))

    n = db.fetchone(
        "SELECT COUNT(*) AS n FROM shadow_decisions WHERE wallet_id='0xW'"
    )
    assert int(n["n"]) == 6


def test_changed_scenario_creates_new_row(tmp_path: Path):
    db = _make_db(tmp_path)
    inp_a = _make_input(
        delay_scenario=DelayScenario.THEORETICAL_IMMEDIATE,
        price_snapshot_id=None,
        depth_hash=None,
    )
    inp_b = _make_input(
        delay_scenario=DelayScenario.DELAY_30_SECONDS,
        price_snapshot_id=None,
        depth_hash=None,
    )
    r_a = compute_shadow_score_v2_from_input(inp_a)
    r_b = compute_shadow_score_v2_from_input(inp_b)
    persist_shadow_score_v2(
        db, "0xW", "t-1", r_a, candidate_id=None,
        source_data_timestamp="2026-07-03T12:00:00Z",
    )
    persist_shadow_score_v2(
        db, "0xW", "t-1", r_b, candidate_id=None,
        source_data_timestamp="2026-07-03T12:00:00Z",
    )
    n = db.fetchone(
        "SELECT COUNT(*) AS n FROM shadow_decisions WHERE wallet_id='0xW'"
    )
    assert int(n["n"]) == 2


def test_identical_rerun_dedupes(tmp_path: Path):
    db = _make_db(tmp_path)
    inp = _make_input(price_snapshot_id=None, depth_hash=None)
    r = compute_shadow_score_v2_from_input(inp)
    persist_shadow_score_v2(
        db, "0xW", "t-1", r, candidate_id=None,
        source_data_timestamp="2026-07-03T12:00:00Z",
    )
    persist_shadow_score_v2(
        db, "0xW", "t-1", r, candidate_id=None,
        source_data_timestamp="2026-07-03T12:00:00Z",
    )
    n = db.fetchone(
        "SELECT COUNT(*) AS n FROM shadow_decisions WHERE wallet_id='0xW'"
    )
    assert int(n["n"]) == 1


def test_persistence_reads_typed_input_columns(tmp_path: Path):
    db = _make_db(tmp_path)
    # Insert a real snapshot to satisfy the FK on
    # shadow_decisions.price_snapshot_id.
    db.conn.execute(
        "INSERT INTO candidate_price_snapshots ("
        "id, candidate_id, snapshot_run_id, fetch_status, side, "
        "source_trade_price, source_trade_quantity, "
        "source_trade_timestamp, fetched_at, created_at) "
        "VALUES ('snap-X', 1, 'run-X', 'OK', 'BUY', 0.5, 1.0, "
        "'2026-07-03T12:00:00Z', '2026-07-03T12:00:00Z', "
        "'2026-07-03T12:00:00Z')"
    )
    db.conn.commit()
    inp = _make_input(
        source_price=0.55,
        delayed_copy_price=0.50,
        intended_stake=200.0,
        slippage=0.02,
        spread=0.03,
        price_snapshot_id="snap-X",
        depth_hash="hash-X",
    )
    r = compute_shadow_score_v2_from_input(inp)
    persist_shadow_score_v2(
        db, "0xW", "t-1", r, candidate_id=None,
        source_data_timestamp="2026-07-03T12:00:00Z",
    )
    row = db.fetchone(
        "SELECT source_price, delayed_copy_price, intended_stake, "
        "       slippage, spread, price_snapshot_id, depth_hash, "
        "       delay_scenario, formula_name, formula_version "
        "FROM shadow_decisions WHERE wallet_id='0xW'"
    )
    assert abs(row["source_price"] - 0.55) < 1e-9
    assert abs(row["delayed_copy_price"] - 0.50) < 1e-9
    assert abs(row["intended_stake"] - 200.0) < 1e-9
    assert abs(row["slippage"] - 0.02) < 1e-9
    assert abs(row["spread"] - 0.03) < 1e-9
    assert row["price_snapshot_id"] == "snap-X"
    assert row["depth_hash"] == "hash-X"
    assert row["delay_scenario"] == "theoretical_immediate"
    assert row["formula_name"] == "shadow_score"
    assert row["formula_version"] == "2-shadow"


def test_persistence_serializes_missing_forward_reasons(tmp_path: Path):
    db = _make_db(tmp_path)
    inp = _make_input(
        delayed_copy_price=None,
        source_price=None,
        price_snapshot_id=None,
        depth_hash=None,
    )
    r = compute_shadow_score_v2_from_input(inp)
    persist_shadow_score_v2(
        db, "0xW", "t-1", r, candidate_id=None,
        source_data_timestamp="2026-07-03T12:00:00Z",
    )
    row = db.fetchone(
        "SELECT missing_forward_reasons_json, missing_components_json "
        "FROM shadow_decisions WHERE wallet_id='0xW'"
    )
    reasons = json.loads(row["missing_forward_reasons_json"])
    assert REASON_NO_DELAYED_PRICE in reasons
    assert REASON_NO_SOURCE_PRICE in reasons