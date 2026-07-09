"""Tests for PR24P Trade Copyability v1 defensive hardening (BUY-only).

These tests confirm the hardening patch to Trade Copyability Score v1:

  * explicit formula identity constants (name/display/version)
  * weights and thresholds unchanged
  * BUY-only side enforcement (SELL -> SKIP, malformed -> INCOMPLETE)
  * price-deterioration becomes ESSENTIAL (no silent 0 / no silent BUY)
  * explicit-vs-trace mismatch detection within tolerance
  * severe partial fill cannot become copy_candidate
  * hard duration exclusions block copy_candidate
  * snapshot point-in-time validation
  * additive schema v16 trace columns persist + migrate

Run:
  PYTHONPATH=src pytest tests/test_p24p_trade_copyability_v1_hardening.py -q
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from polycopy.scoring import depth_normalization as dn
from polycopy.scoring.trade_score_v1 import (
    WEIGHTS,
    PRICE_DETERIORATION_MISMATCH_TOLERANCE,
    TRADE_COPYABILITY_FORMULA_NAME,
    TRADE_COPYABILITY_FORMULA_DISPLAY_NAME,
    TRADE_COPYABILITY_FORMULA_VERSION,
    TradeCopyabilityInputV1,
    TradeVerdict,
    calculate_buy_price_deterioration_pct,
    calculate_side_aware_price_deterioration_pct,
    compute_trade_score_v1,
    validate_price_snapshot_timing,
)
from polycopy.db.database import Database
from polycopy.db.schema import SCHEMA_VERSION

_REPO_ROOT = Path(__file__).resolve().parent.parent


EXPECTED_WEIGHTS = {
    "copy_price_quality": 30.0,
    "fill_feasibility": 25.0,
    "liquidity_and_spread_quality": 15.0,
    "trade_freshness": 10.0,
    "holding_period_quality": 10.0,
    "market_and_resolution_quality": 5.0,
    "strategy_and_data_quality": 5.0,
}


def _base_complete_input(**overrides) -> TradeCopyabilityInputV1:
    """Strong, complete BUY trade that scores copy_candidate at ~95-100."""
    base: dict = dict(
        wallet_id="w",
        source_trade_id="t",
        side="BUY",
        price_deterioration_pct=0.0,
        intended_stake=100.0,
        executable_depth=200.0,
        fill_percentage=None,
        spread=0.0,
        best_bid_size=1000.0,
        best_ask_size=1000.0,
        trade_age_seconds=0.0,
        seconds_to_market_end=7 * 24 * 3600.0,  # 1 week -> preferred (100)
        market_active=True,
        market_closed=False,
        market_resolved=False,
        has_valid_strategy=True,
        has_complete_data=True,
        market_category=None,
        depth_walk_result=None,
        depth_status_reason=None,
        price_snapshot_id=None,
        depth_hash=None,
    )
    base.update(overrides)
    return TradeCopyabilityInputV1(**base)


def _partial_depth_walk(side: str = "BUY") -> dn.DepthWalkResult:
    return dn.DepthWalkResult(
        side=side,
        intended_notional=Decimal("100"),
        filled_notional=Decimal("50"),
        fill_percentage=Decimal("0.5"),
        contracts_filled=Decimal("50"),
        vwap_fill_price=None,
        slippage=None,
        levels_consumed=1,
        remaining_notional=Decimal("50"),
        is_complete=False,
        insufficient_reason=dn.DEPTH_INSUFFICIENT_FOR_STAKE,
    )


def _fresh_db(tmp_path: Path) -> Database:
    db = Database(db_path=tmp_path / "fresh_p24p.db")
    db.connect()
    return db


# -------------------------------------------------------------------------
# 1. Formula identity constants
# -------------------------------------------------------------------------


def test_formula_identity_constants():
    assert TRADE_COPYABILITY_FORMULA_NAME == "trade_copyability"
    assert TRADE_COPYABILITY_FORMULA_DISPLAY_NAME == "Trade Copyability Score"
    assert TRADE_COPYABILITY_FORMULA_VERSION == "1"


def test_trade_score_result_carries_formula_identity():
    res = compute_trade_score_v1(input=_base_complete_input())
    assert res.formula_name == TRADE_COPYABILITY_FORMULA_NAME
    assert res.formula_version == TRADE_COPYABILITY_FORMULA_VERSION


# -------------------------------------------------------------------------
# 2. Weights and thresholds unchanged
# -------------------------------------------------------------------------


def test_weights_sum_to_100():
    assert sum(WEIGHTS.values()) == 100.0


def test_weights_match_expected():
    assert dict(WEIGHTS) == EXPECTED_WEIGHTS


def test_thresholds_unchanged():
    from polycopy.scoring.trade_score_v1 import (
        VERDICT_COPY_CANDIDATE_MIN,
        VERDICT_WATCHLIST_MIN,
    )
    assert VERDICT_COPY_CANDIDATE_MIN == 70.0
    assert VERDICT_WATCHLIST_MIN == 50.0


# -------------------------------------------------------------------------
# 3. Missing price evidence is incomplete
# -------------------------------------------------------------------------


def test_missing_price_evidence_incomplete():
    inp = _base_complete_input(
        price_deterioration_pct=None,
        source_entry_price=None,
        current_copy_price=None,
        estimated_fill_price=None,
    )
    res = compute_trade_score_v1(input=inp)
    assert res.verdict == TradeVerdict.INCOMPLETE
    assert "price_deterioration_pct" in res.missing_essentials
    assert res.verdict != TradeVerdict.COPY_CANDIDATE


# -------------------------------------------------------------------------
# 4. BUY deterioration helper
# -------------------------------------------------------------------------


def test_buy_deterioration_helper_positive():
    val, reason = calculate_buy_price_deterioration_pct(
        source_entry_price=0.50, estimated_fill_price=0.55)
    assert reason is None
    assert val == pytest.approx(0.10, abs=1e-9)


def test_buy_deterioration_helper_negative():
    val, reason = calculate_buy_price_deterioration_pct(
        source_entry_price=0.50, estimated_fill_price=0.45)
    assert reason is None
    assert val == pytest.approx(-0.10, abs=1e-9)


def test_buy_deterioration_current_price_fallback():
    val, reason = calculate_buy_price_deterioration_pct(
        source_entry_price=0.50, current_copy_price=0.55)
    assert reason is None
    assert val == pytest.approx(0.10, abs=1e-9)


def test_buy_deterioration_estimated_precedence():
    # estimated_fill_price must take precedence over current_copy_price.
    val_est, _ = calculate_buy_price_deterioration_pct(
        source_entry_price=0.50, current_copy_price=0.60,
        estimated_fill_price=0.55)
    val_cur, _ = calculate_buy_price_deterioration_pct(
        source_entry_price=0.50, current_copy_price=0.60)
    assert val_est == pytest.approx(0.10, abs=1e-9)
    assert val_cur == pytest.approx(0.20, abs=1e-9)


def test_buy_deterioration_invalid_source():
    _, reason = calculate_buy_price_deterioration_pct(
        source_entry_price=1.5, estimated_fill_price=0.55)
    assert reason == "invalid_source_entry_price"


def test_buy_deterioration_invalid_copy():
    _, reason = calculate_buy_price_deterioration_pct(
        source_entry_price=0.50, estimated_fill_price=-0.1)
    assert reason == "invalid_copy_price"


def test_buy_deterioration_missing_source():
    _, reason = calculate_buy_price_deterioration_pct(
        source_entry_price=None, estimated_fill_price=0.55)
    assert reason == "missing_source_entry_price"


def test_buy_deterioration_missing_copy():
    _, reason = calculate_buy_price_deterioration_pct(
        source_entry_price=0.50, current_copy_price=None,
        estimated_fill_price=None)
    assert reason == "missing_copy_price"


def test_side_aware_helper_sell_unsupported():
    _, reason = calculate_side_aware_price_deterioration_pct(
        side="SELL", source_entry_price=0.50, estimated_fill_price=0.55)
    assert reason == "sell_side_copyability_not_supported_v1"


def test_side_aware_helper_invalid_side_blank():
    _, reason = calculate_side_aware_price_deterioration_pct(
        side=None, source_entry_price=0.50, estimated_fill_price=0.55)
    assert reason == "invalid_side"


def test_side_aware_helper_invalid_side_garbage():
    _, reason = calculate_side_aware_price_deterioration_pct(
        side="garbage", source_entry_price=0.50, estimated_fill_price=0.55)
    assert reason == "invalid_side"


def test_side_aware_helper_buy_delegates():
    val, reason = calculate_side_aware_price_deterioration_pct(
        side="BUY", source_entry_price=0.50, estimated_fill_price=0.55)
    assert reason is None
    assert val == pytest.approx(0.10, abs=1e-9)


# -------------------------------------------------------------------------
# 5. Exact SELL unsupported
# -------------------------------------------------------------------------


def test_sell_unsupported_skip():
    inp = _base_complete_input(side="SELL")
    res = compute_trade_score_v1(input=inp)
    assert res.verdict == TradeVerdict.SKIP
    assert res.score == 0.0
    assert "sell_side_copyability_not_supported_v1" in res.rejection_reasons
    assert res.verdict != TradeVerdict.WATCHLIST
    assert res.verdict != TradeVerdict.COPY_CANDIDATE


def test_sell_unsupported_not_in_missing_essentials():
    inp = _base_complete_input(side="SELL")
    res = compute_trade_score_v1(input=inp)
    assert "side" not in res.missing_essentials


# -------------------------------------------------------------------------
# 6. Missing side
# -------------------------------------------------------------------------


@pytest.mark.parametrize("side", [None, ""])
def test_missing_side_incomplete(side):
    inp = _base_complete_input(side=side)
    res = compute_trade_score_v1(input=inp)
    assert res.verdict == TradeVerdict.INCOMPLETE
    assert "side" in res.missing_essentials
    assert "missing_side" in res.rejection_reasons
    assert res.verdict != TradeVerdict.WATCHLIST
    assert res.verdict != TradeVerdict.COPY_CANDIDATE


# -------------------------------------------------------------------------
# 7. Malformed / case-mismatched side
# -------------------------------------------------------------------------


@pytest.mark.parametrize("side", [
    "sell", "Sell", "Buy", "BUY ", " Buy", "SEL", "garbage",
])
def test_malformed_side_incomplete_not_skip(side):
    inp = _base_complete_input(side=side)
    res = compute_trade_score_v1(input=inp)
    assert res.verdict == TradeVerdict.INCOMPLETE
    assert "side" in res.missing_essentials
    assert "invalid_side" in res.rejection_reasons
    assert res.verdict != TradeVerdict.WATCHLIST
    assert res.verdict != TradeVerdict.COPY_CANDIDATE


# -------------------------------------------------------------------------
# 8. BUY exact only
# -------------------------------------------------------------------------


def test_buy_only_proceeds_and_can_be_copy_candidate():
    res = compute_trade_score_v1(input=_base_complete_input(side="BUY"))
    assert res.verdict == TradeVerdict.COPY_CANDIDATE
    assert res.score >= 70.0


# -------------------------------------------------------------------------
# 9. Explicit pct and raw trace mismatch blocks
# -------------------------------------------------------------------------


def test_explicit_and_trace_mismatch_blocks():
    inp = _base_complete_input(
        price_deterioration_pct=0.00,
        source_entry_price=0.50,
        estimated_fill_price=0.55,  # derives +0.10
    )
    res = compute_trade_score_v1(input=inp)
    assert res.verdict == TradeVerdict.INCOMPLETE
    assert "price_deterioration_trace_mismatch" in res.missing_essentials
    assert "PRICE_DETERIORATION_TRACE_MISMATCH" in res.rejection_reasons


# -------------------------------------------------------------------------
# 10. Explicit pct and raw trace match passes
# -------------------------------------------------------------------------


def test_explicit_and_trace_match_passes():
    inp = _base_complete_input(
        price_deterioration_pct=0.10,
        source_entry_price=0.50,
        estimated_fill_price=0.55,
    )
    res = compute_trade_score_v1(input=inp)
    assert res.verdict != TradeVerdict.INCOMPLETE
    assert "price_deterioration_trace_mismatch" not in res.missing_essentials


# -------------------------------------------------------------------------
# 11. Tolerance behavior
# -------------------------------------------------------------------------


def test_mismatch_exact_match_passes():
    inp = _base_complete_input(
        price_deterioration_pct=0.10,
        source_entry_price=0.50,
        estimated_fill_price=0.55,
    )
    res = compute_trade_score_v1(input=inp)
    assert "price_deterioration_trace_mismatch" not in res.missing_essentials


def test_mismatch_within_tolerance_passes():
    # derived 0.10, explicit 0.10 + half tolerance
    within = 0.10 + PRICE_DETERIORATION_MISMATCH_TOLERANCE / 2
    inp = _base_complete_input(
        price_deterioration_pct=within,
        source_entry_price=0.50,
        estimated_fill_price=0.55,
    )
    res = compute_trade_score_v1(input=inp)
    assert "price_deterioration_trace_mismatch" not in res.missing_essentials


def test_mismatch_greater_than_tolerance_blocks():
    over = 0.10 + PRICE_DETERIORATION_MISMATCH_TOLERANCE * 2
    inp = _base_complete_input(
        price_deterioration_pct=over,
        source_entry_price=0.50,
        estimated_fill_price=0.55,
    )
    res = compute_trade_score_v1(input=inp)
    assert "price_deterioration_trace_mismatch" in res.missing_essentials


# -------------------------------------------------------------------------
# 12. Existing BUY caller compatibility (explicit pct, no trace)
# -------------------------------------------------------------------------


def test_existing_buy_caller_no_trace_still_works():
    inp = _base_complete_input(
        price_deterioration_pct=0.0,
        source_entry_price=None,
        current_copy_price=None,
        estimated_fill_price=None,
    )
    res = compute_trade_score_v1(input=inp)
    assert res.verdict == TradeVerdict.COPY_CANDIDATE
    assert "price_deterioration_trace_mismatch" not in res.missing_essentials


# -------------------------------------------------------------------------
# 13. Severe partial fill downgrade
# -------------------------------------------------------------------------


def test_severe_partial_fill_not_copy_candidate():
    inp = _base_complete_input(
        price_deterioration_pct=0.0,
        fill_percentage=0.79,
    )
    res = compute_trade_score_v1(input=inp)
    assert res.verdict != TradeVerdict.COPY_CANDIDATE
    assert "partial_fill_below_copy_candidate_threshold" in res.rejection_reasons


def test_partial_fill_boundary_080_can_remain_copy_candidate():
    inp = _base_complete_input(
        price_deterioration_pct=0.0,
        fill_percentage=0.80,
    )
    res = compute_trade_score_v1(input=inp)
    assert res.verdict == TradeVerdict.COPY_CANDIDATE


def test_partial_fill_zero_not_copy_candidate():
    inp = _base_complete_input(
        price_deterioration_pct=0.0,
        fill_percentage=0.0,
    )
    res = compute_trade_score_v1(input=inp)
    assert res.verdict != TradeVerdict.COPY_CANDIDATE


# -------------------------------------------------------------------------
# 14. Duration hard exclusions
# -------------------------------------------------------------------------


def test_duration_14m59s_excluded_short():
    inp = _base_complete_input(
        price_deterioration_pct=0.0,
        seconds_to_market_end=14 * 60 + 59,
    )
    res = compute_trade_score_v1(input=inp)
    assert res.verdict == TradeVerdict.SKIP
    assert res.score == 0.0
    assert "duration_excluded_short" in res.rejection_reasons


def test_duration_45d_plus_1s_excluded_long():
    inp = _base_complete_input(
        price_deterioration_pct=0.0,
        seconds_to_market_end=45 * 24 * 3600 + 1,
    )
    res = compute_trade_score_v1(input=inp)
    assert res.verdict == TradeVerdict.SKIP
    assert res.score == 0.0
    assert "duration_excluded_long" in res.rejection_reasons


def test_duration_15m_boundary_not_hard_excluded():
    from polycopy.scoring.trade_score_v1 import _holding_period_component

    score = _holding_period_component(float(15 * 60))[0]
    assert score == 40.0
    inp = _base_complete_input(
        price_deterioration_pct=0.0,
        seconds_to_market_end=15 * 60,
    )
    res = compute_trade_score_v1(input=inp)
    assert "duration_excluded_short" not in res.rejection_reasons


def test_duration_45d_exact_not_hard_excluded():
    from polycopy.scoring.trade_score_v1 import _holding_period_component

    score = _holding_period_component(float(45 * 24 * 3600))[0]
    assert score == 40.0
    inp = _base_complete_input(
        price_deterioration_pct=0.0,
        seconds_to_market_end=45 * 24 * 3600,
    )
    res = compute_trade_score_v1(input=inp)
    assert "duration_excluded_long" not in res.rejection_reasons


# -------------------------------------------------------------------------
# 15. Snapshot timing validation
# -------------------------------------------------------------------------


def test_snapshot_before_source_trade_incomplete():
    inp = _base_complete_input(
        price_deterioration_pct=0.0,
        source_trade_timestamp="2026-01-02T00:00:00Z",
        price_snapshot_fetched_at="2026-01-01T00:00:00Z",
        evaluation_timestamp="2026-01-03T00:00:00Z",
    )
    res = compute_trade_score_v1(input=inp)
    assert res.verdict == TradeVerdict.INCOMPLETE
    assert "snapshot_before_source_trade" in res.missing_essentials


def test_snapshot_after_evaluation_incomplete():
    inp = _base_complete_input(
        price_deterioration_pct=0.0,
        source_trade_timestamp="2026-01-01T00:00:00Z",
        price_snapshot_fetched_at="2026-01-03T00:00:00Z",
        evaluation_timestamp="2026-01-02T00:00:00Z",
    )
    res = compute_trade_score_v1(input=inp)
    assert res.verdict == TradeVerdict.INCOMPLETE
    assert "snapshot_after_evaluation" in res.missing_essentials


def test_malformed_timestamp_incomplete():
    inp = _base_complete_input(
        price_deterioration_pct=0.0,
        source_trade_timestamp="not-a-date",
        price_snapshot_fetched_at="2026-01-01T00:00:00Z",
        evaluation_timestamp="2026-01-02T00:00:00Z",
    )
    res = compute_trade_score_v1(input=inp)
    assert res.verdict == TradeVerdict.INCOMPLETE
    assert "invalid_price_snapshot_timestamp" in res.missing_essentials


def test_missing_timing_fields_do_not_break_isolated_scoring():
    res = compute_trade_score_v1(
        input=_base_complete_input(
            price_deterioration_pct=0.0,
            source_trade_timestamp=None,
            price_snapshot_fetched_at=None,
            evaluation_timestamp=None,
        )
    )
    assert res.verdict != TradeVerdict.INCOMPLETE
    # helper returns None for fully-missing timing evidence
    assert validate_price_snapshot_timing(
        source_trade_timestamp=None,
        price_snapshot_fetched_at=None,
        evaluation_timestamp=None,
    ) is None


# -------------------------------------------------------------------------
# 16. Depth behavior unchanged
# -------------------------------------------------------------------------


@pytest.mark.parametrize("reason", [
    dn.DEPTH_NOT_CAPTURED,
    dn.DEPTH_LEVELS_MALFORMED,
    dn.DEPTH_SNAPSHOT_MISMATCH,
])
def test_depth_rejection_still_incomplete(reason):
    inp = _base_complete_input(depth_status_reason=reason)
    res = compute_trade_score_v1(input=inp)
    assert res.verdict == TradeVerdict.INCOMPLETE


def test_partial_depth_preserves_insufficient_reason():
    inp = _base_complete_input(
        price_deterioration_pct=0.0,
        depth_walk_result=_partial_depth_walk("BUY"),
    )
    res = compute_trade_score_v1(input=inp)
    assert dn.DEPTH_INSUFFICIENT_FOR_STAKE in res.rejection_reasons


# -------------------------------------------------------------------------
# 17. Schema v16 + persistence of trace fields
# -------------------------------------------------------------------------


def test_fresh_db_at_schema_v16(tmp_path):
    db = _fresh_db(tmp_path)
    row = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
    assert int(row["value"]) == SCHEMA_VERSION
    assert SCHEMA_VERSION == 16
    db.close()


def test_v16_trace_columns_present_on_fresh_db(tmp_path):
    db = _fresh_db(tmp_path)
    cols = {r["name"] for r in db.fetchall(
        "PRAGMA table_info(trade_copyability_decisions)")}
    needed = {
        "source_entry_price", "current_copy_price", "estimated_fill_price",
        "source_trade_timestamp", "price_snapshot_fetched_at",
        "evaluation_timestamp",
    }
    assert needed <= cols, f"missing v16 columns: {needed - cols}"
    db.close()


def test_v16_upgrade_from_v15_adds_trace_columns(tmp_path):
    """Build a fresh DB at v15, then reopen so the runner applies v16."""
    db = _fresh_db(tmp_path)
    # rewind to v15
    db.conn.execute("UPDATE _meta SET value='15' WHERE key='schema_version'")
    db.conn.commit()
    db.close()

    db2 = Database(db_path=tmp_path / "fresh_p24p.db")
    db2.connect()
    assert int(db2.fetchone(
        "SELECT value FROM _meta WHERE key='schema_version'")["value"]) == 16
    cols = {r["name"] for r in db2.fetchall(
        "PRAGMA table_info(trade_copyability_decisions)")}
    needed = {
        "source_entry_price", "current_copy_price", "estimated_fill_price",
        "source_trade_timestamp", "price_snapshot_fetched_at",
        "evaluation_timestamp",
    }
    assert needed <= cols, f"missing v16 columns after upgrade: {needed - cols}"
    db2.close()


def test_persist_trade_score_v1_stores_trace_fields(tmp_path):
    from polycopy.scoring.score_serialization import persist_trade_score_v1

    db = _fresh_db(tmp_path)
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) "
        "VALUES ('w','0x','l',1,'2026-01-01T00:00:00Z')"
    )
    db.conn.commit()

    inp = _base_complete_input(
        price_deterioration_pct=0.0,
        source_entry_price=0.50,
        current_copy_price=0.50,
        estimated_fill_price=0.51,
        source_trade_timestamp="2026-01-01T00:00:00Z",
        price_snapshot_fetched_at="2026-01-01T00:00:05Z",
        evaluation_timestamp="2026-01-01T00:00:10Z",
    )
    res = compute_trade_score_v1(input=inp)
    pid = persist_trade_score_v1(db, "w", "t", res)
    row = db.fetchone(
        "SELECT source_entry_price, current_copy_price, estimated_fill_price, "
        "source_trade_timestamp, price_snapshot_fetched_at, evaluation_timestamp "
        "FROM trade_copyability_decisions WHERE id=?", (pid,)
    )
    assert row["source_entry_price"] == pytest.approx(0.50)
    assert row["current_copy_price"] == pytest.approx(0.50)
    assert row["estimated_fill_price"] == pytest.approx(0.51)
    assert row["source_trade_timestamp"] == "2026-01-01T00:00:00Z"
    assert row["price_snapshot_fetched_at"] == "2026-01-01T00:00:05Z"
    assert row["evaluation_timestamp"] == "2026-01-01T00:00:10Z"
    db.close()


def test_schema_v16_source_has_no_destructive_statements():
    from polycopy.db import schema_v16

    for stmt in schema_v16._V16_DDL:
        upper = stmt.upper()
        assert "ADD COLUMN" in upper, f"v16 stmt not additive: {stmt}"
        assert "DROP" not in upper
        assert "DELETE" not in upper
        assert "RENAME" not in upper
        assert "UPDATE" not in upper


def test_column_count_matches_schema(tmp_path):
    """persist_trade_score_v1 INSERT must list every v16 schema column."""
    from polycopy.scoring.score_serialization import persist_trade_score_v1
    import polycopy.scoring.score_serialization as ser

    db = _fresh_db(tmp_path)
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) "
        "VALUES ('w','0x','l',1,'2026-01-01T00:00:00Z')"
    )
    db.conn.commit()

    inp = _base_complete_input(price_deterioration_pct=0.0)
    res = compute_trade_score_v1(input=inp)

    schema_cols = {
        r["name"] for r in db.fetchall(
            "PRAGMA table_info(trade_copyability_decisions)")
    } - {"id"}

    captured: list[str] = []
    real_execute = ser.Database.execute

    def fake_execute(self_db, sql, params=()):
        captured.append(sql)
        return real_execute(self_db, sql, params)

    try:
        ser.Database.execute = fake_execute  # type: ignore[assignment]
        try:
            persist_trade_score_v1(db, "w", "t", res)
        except Exception:
            pass
    finally:
        ser.Database.execute = real_execute  # type: ignore[assignment]

    insert_sql = next(
        (s for s in captured if "INSERT" in s
         and "trade_copyability_decisions" in s),
        None,
    )
    assert insert_sql is not None
    col_part = insert_sql[
        insert_sql.index("(") + 1:insert_sql.index(")")].split(",")
    n_cols = len([c for c in col_part if c.strip()])
    n_vals = insert_sql.count("?")
    assert n_cols == n_vals, f"cols {n_cols} != placeholders {n_vals}"
    assert n_cols == len(schema_cols), (
        f"INSERT has {n_cols} cols but schema has {len(schema_cols)}")


# -------------------------------------------------------------------------
# Guardrail confirmation: compute_trade_score_v1 performs no DB writes
# -------------------------------------------------------------------------


def test_compute_performs_no_db_write():
    """Hardening must be pure scoring — no DB, no broker, no candidates."""
    import inspect

    src = inspect.getsource(compute_trade_score_v1)
    # Check for real mutating/integration calls only. Docstrings may
    # legitimately mention "settle"/"scan"/"collect"/"specialist" as
    # context (e.g. settlement-accounting exclusion rationale), so we
    # look for execution-level tokens, not bare substrings.
    forbidden = (
        "INSERT ", "UPDATE ", "DELETE ", "DROP TABLE", "ALTER TABLE",
        "CREATE TABLE", "polycopy.db.database", "broker",
        "persist_", "create_candidate", "paper_signal",
    )
    low = src.lower()
    for tok in forbidden:
        assert tok.lower() not in low, f"compute contains forbidden {tok!r}"
