"""Tests for PR24O Trade Copyability v1 reconciliation audit (read-only).

These tests confirm the audit can RECONCILE the existing Trade Copyability
Score v1 without rebuilding it, and that the audit module/CLI are pure
(read-only) and refuse to wire automation.

Run:
  PYTHONPATH=src pytest tests/test_p24o_trade_copyability_v1_reconciliation_audit.py -q
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from polycopy.engine import (
    trade_copyability_v1_reconciliation_audit as audit,
)
from polycopy.scoring import depth_normalization as dn
from polycopy.scoring.trade_score_v1 import (
    WEIGHTS,
    TradeCopyabilityInputV1,
    compute_trade_score_v1,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CLI = _REPO_ROOT / "scripts" / "report_trade_copyability_v1_reconciliation.py"
_PROD_DB = os.environ.get(
    "POLYCOPY_DB",
    str(_REPO_ROOT / "data" / "polycopy.db"),
)

EXPECTED_WEIGHTS = {
    "copy_price_quality": 30.0,
    "fill_feasibility": 25.0,
    "liquidity_and_spread_quality": 15.0,
    "trade_freshness": 10.0,
    "holding_period_quality": 10.0,
    "market_and_resolution_quality": 5.0,
    "strategy_and_data_quality": 5.0,
}


# --------------------------------------------------------------------------
# 1. Weights
# --------------------------------------------------------------------------

def test_weights_sum_to_100():
    assert sum(WEIGHTS.values()) == 100.0


def test_weights_match_expected():
    assert dict(WEIGHTS) == EXPECTED_WEIGHTS


def test_audit_report_weights_match():
    report = audit.run_reconciliation_audit(db_path=_PROD_DB)
    assert report.weights_sum == 100.0
    assert report.weights_match_expected is True
    assert report.thresholds_match_expected is True
    assert report.formula_present is True


# --------------------------------------------------------------------------
# 2. Expected input fields present
# --------------------------------------------------------------------------

def test_expected_input_fields_present():
    import dataclasses

    expected = (\
        "wallet_id", "source_trade_id", "side", "price_deterioration_pct",
        "intended_stake", "executable_depth", "fill_percentage", "spread",
        "best_bid_size", "best_ask_size", "trade_age_seconds",
        "seconds_to_market_end", "market_active", "market_closed",
        "market_resolved", "has_valid_strategy", "has_complete_data",
        "market_category", "depth_walk_result", "depth_status_reason",
        "price_snapshot_id", "depth_hash",
    )
    present = {f.name for f in dataclasses.fields(TradeCopyabilityInputV1)}
    for f in expected:
        assert f in present, f"missing field {f}"

    report = audit.run_reconciliation_audit(db_path=_PROD_DB)
    for f in expected:
        assert report.essential_inputs_present.get(f) is True, f


# --------------------------------------------------------------------------
# 3. Missing essentials -> incomplete
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field", [
    "side", "intended_stake", "executable_depth", "spread",
    "trade_age_seconds", "seconds_to_market_end", "market_active",
])
def test_missing_essential_produces_incomplete(field):
    inp = audit._base_complete_input(**{field: None})
    res = compute_trade_score_v1(input=inp)
    assert res.verdict.value == "incomplete"
    assert field in res.missing_essentials or res.missing_essentials


# --------------------------------------------------------------------------
# 4. Depth rejection reasons -> incomplete
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [
    dn.DEPTH_NOT_CAPTURED,
    dn.DEPTH_LEVELS_MALFORMED,
    dn.DEPTH_SNAPSHOT_MISMATCH,
])
def test_depth_rejection_produces_incomplete(reason):
    inp = audit._base_complete_input(depth_status_reason=reason)
    res = compute_trade_score_v1(input=inp)
    assert res.verdict.value == "incomplete"


# --------------------------------------------------------------------------
# 5. Strong complete synthetic trade -> copy_candidate
# --------------------------------------------------------------------------

def test_strong_complete_becomes_copy_candidate():
    res = compute_trade_score_v1(input=audit._base_complete_input())
    assert res.verdict.value == "copy_candidate"
    assert res.score >= 70.0


# --------------------------------------------------------------------------
# 6. Weak synthetic trade does NOT become copy_candidate
# --------------------------------------------------------------------------

def test_weak_complete_not_copy_candidate():
    inp = audit._base_complete_input(
        price_deterioration_pct=0.5,
        intended_stake=100.0,
        executable_depth=10.0,
        spread=0.2,
        best_bid_size=1000.0,
        best_ask_size=1000.0,
        trade_age_seconds=3600.0,
        seconds_to_market_end=7 * 24 * 3600.0,
        market_active=False,
        has_valid_strategy=False,
        has_complete_data=False,
    )
    res = compute_trade_score_v1(input=inp)
    assert res.verdict.value != "copy_candidate"


# --------------------------------------------------------------------------
# 7. Partial depth walk preserves insufficient depth reason
# --------------------------------------------------------------------------

def test_partial_depth_preserves_insufficient_reason():
    inp = audit._base_complete_input(
        depth_walk_result=audit._partial_depth_walk_result("BUY"),
    )
    res = compute_trade_score_v1(input=inp)
    assert dn.DEPTH_INSUFFICIENT_FOR_STAKE in res.rejection_reasons
    assert res.input is not None
    assert res.input.depth_walk_result.is_complete is False


# --------------------------------------------------------------------------
# 8. Short crypto under 6h -> skip with short_crypto_exclusion
# --------------------------------------------------------------------------

def test_short_crypto_under_6h_skip():
    inp = audit._base_complete_input(
        market_category="crypto",
        seconds_to_market_end=2 * 3600.0,  # 2h < 6h
    )
    res = compute_trade_score_v1(input=inp)
    assert res.verdict.value == "skip"
    assert "short_crypto_exclusion" in res.rejection_reasons


# --------------------------------------------------------------------------
# 9. Duration bucket boundaries
# --------------------------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (14 * 60 + 59, 0.0),     # 14m59s -> excluded
    (15 * 60, 40.0),         # 15m00s -> 40
    (6 * 3600, 75.0),        # 6h -> 75
    (24 * 3600, 100.0),      # 1d -> 100
    (14 * 24 * 3600, 100.0), # 14d -> 100
    (21 * 24 * 3600, 80.0),  # 21d -> 80
    (45 * 24 * 3600, 40.0),  # 45d -> 40
    (46 * 24 * 3600, 0.0),   # >45d -> excluded
])
def test_duration_boundaries(seconds, expected):
    from polycopy.scoring.trade_score_v1 import _holding_period_component
    got = _holding_period_component(float(seconds))[0]
    assert abs(got - expected) < 1e-9


# --------------------------------------------------------------------------
# 10. Report says ready_to_wire_to_automation is False
# --------------------------------------------------------------------------

def test_report_ready_to_wire_false():
    report = audit.run_reconciliation_audit(db_path=_PROD_DB)
    assert report.ready_to_wire_to_automation is False


# --------------------------------------------------------------------------
# 11. CLI JSON is valid
# --------------------------------------------------------------------------

def test_cli_json_valid():
    proc = subprocess.run(
        [sys.executable, str(_CLI), "--json", "--db-path", _PROD_DB],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT / "src")},
        cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert isinstance(payload, dict)
    assert payload["ready_to_wire_to_automation"] is False
    assert payload["formula_present"] is True


# --------------------------------------------------------------------------
# 12. CLI production DB smoke opens read-only and exits 0
# --------------------------------------------------------------------------

def test_cli_production_db_readonly_smoke():
    proc = subprocess.run(
        [sys.executable, str(_CLI), "--limit", "10", "--db-path", _PROD_DB],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT / "src")},
        cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "PR24O" in proc.stdout


# --------------------------------------------------------------------------
# 13. No DB mutation purity
# --------------------------------------------------------------------------

def test_module_does_not_import_database_for_writes():
    import inspect

    src = inspect.getsource(audit)
    # The pure audit module must NOT import the write-capable Database class.
    assert "from polycopy.db.database import Database" not in src
    assert "import polycopy.db.database" not in src


def test_cli_uses_readonly_sqlite():
    cli_src = _CLI.read_text(encoding="utf-8")
    # Must open read-only.
    assert 'mode=ro' in cli_src
    # Must never EXECUTE a mutating statement. Docstrings may mention the
    # words, so we look for execution calls specifically.
    forbidden_exec = (
        ".execute(", "executescript(", ".commit(", "INSERT ", "UPDATE ",
        "DELETE ", "DROP TABLE", "ALTER TABLE", "CREATE TABLE",
    )
    for token in forbidden_exec:
        assert token not in cli_src, f"CLI contains forbidden {token!r}"


def test_audit_module_no_mutation_statements():
    import inspect

    src = inspect.getsource(audit)
    # The audit module opens SQLite with mode=ro and performs only SELECT /
    # PRAGMA reads. It must never issue a mutating SQL verb. We assert the
    # absence of DML/DDL verbs (case-insensitive) rather than .execute(,
    # because read-only SELECT/PRAGMA execution is permitted and required.
    forbidden_verbs = (
        "insert into", "update ", "delete from", "delete ",
        "drop table", "alter table", "create table", "create index",
        "commit;", ".commit(", "executescript(",
    )
    low = src.lower()
    for token in forbidden_verbs:
        assert token not in low, f"audit module contains forbidden verb {token!r}"
    # Nor must it import the write-capable Database class.
    assert "from polycopy.db.database import Database" not in src
    assert "import polycopy.db.database" not in src


def test_production_db_unchanged_around_cli(tmp_path):
    """Run CLI against a copy of the production DB and confirm no writes."""
    if not os.path.exists(_PROD_DB):
        pytest.skip("production DB not present")
    import shutil

    copy = tmp_path / "polycopy_copy.db"
    shutil.copyfile(_PROD_DB, copy)

    before_size = copy.stat().st_size
    before_mtime = copy.stat().st_mtime

    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, str(_CLI), "--db-path", str(copy), "--limit", "10"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT / "src")},
            cwd=str(_REPO_ROOT),
        )
        assert proc.returncode == 0, proc.stderr

    # Read-only open of mode=ro must never change size/mtime.
    after_size = copy.stat().st_size
    after_mtime = copy.stat().st_mtime
    assert after_size == before_size, "DB size changed after read-only audit"
    assert after_mtime == before_mtime, "DB mtime changed after read-only audit"

    # Also confirm read counts are non-negative when tables exist.
    con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM trade_copyability_decisions"
        )
        assert cur.fetchone()[0] >= 0
    finally:
        con.close()
