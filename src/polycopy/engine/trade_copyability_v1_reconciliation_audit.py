"""PR24O — Trade Copyability Score v1 reconciliation audit (read-only).

This module is a PURE, READ-ONLY audit. It does NOT:

  * import ``polycopy.db.database`` (no ORM / no writes),
  * write to any database,
  * rebuild or retune the Trade Copyability v1 formula,
  * wire anything into automation,
  * create copy candidates or paper signals.

It reconciles the *existing* Trade Copyability Score v1 implementation
(``polycopy.scoring.trade_score_v1``) against the PR24M Wallet Skill Score
path and reports:

  1. Formula / weight / threshold presence.
  2. Component reconciliation against expected weights.
  3. Essential input-field coverage on ``TradeCopyabilityInputV1``.
  4. Safety behaviour (synthetic inputs -> expected verdicts).
  5. Integration risk (old wallet-score object still in runtime paths).
  6. Persistence (schema + live table column coverage).
  7. Production DB read-only sparse counts.

The module may open SQLite read-only (``mode=ro``) for counts/persistence
inspection, but performs no mutation.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import polycopy.scoring.trade_score_v1 as _tsv1
from polycopy.scoring import depth_normalization as _dn
from polycopy.scoring.trade_score_v1 import (
    VERDICT_COPY_CANDIDATE_MIN,
    VERDICT_WATCHLIST_MIN,
    WEIGHTS,
    TradeCopyabilityInputV1,
    compute_trade_score_v1,
)

# --------------------------------------------------------------------------
# Expected reference values (from the PR24O audit spec)
# --------------------------------------------------------------------------

EXPECTED_WEIGHTS: dict[str, float] = {
    "copy_price_quality": 30.0,
    "fill_feasibility": 25.0,
    "liquidity_and_spread_quality": 15.0,
    "trade_freshness": 10.0,
    "holding_period_quality": 10.0,
    "market_and_resolution_quality": 5.0,
    "strategy_and_data_quality": 5.0,
}

EXPECTED_THRESHOLDS: dict[str, float] = {
    "copy_candidate_min": 70.0,
    "watchlist_min": 50.0,
}

# The 21 essential / required fields the audit must confirm exist on the
# TradeCopyabilityInputV1 dataclass.
ESSENTIAL_INPUT_FIELDS: tuple[str, ...] = (
    "wallet_id",
    "source_trade_id",
    "side",
    "price_deterioration_pct",
    "intended_stake",
    "executable_depth",
    "fill_percentage",
    "spread",
    "best_bid_size",
    "best_ask_size",
    "trade_age_seconds",
    "seconds_to_market_end",
    "market_active",
    "market_closed",
    "market_resolved",
    "has_valid_strategy",
    "has_complete_data",
    "market_category",
    "depth_walk_result",
    "depth_status_reason",
    "price_snapshot_id",
    "depth_hash",
)

# Tables counted in the production read-only smoke.
PRODUCTION_TABLES: tuple[str, ...] = (
    "trade_copyability_decisions",
    "paper_signal_decisions",
    "candidate_price_snapshots",
    "candidate_price_snapshot_levels",
    "copy_candidates",
    "source_trades",
    "wallet_score_decisions",
)

# Columns that must be stored on trade_copyability_decisions.
PERSISTENCE_REQUIRED_COLUMNS: tuple[str, ...] = (
    "price_deterioration_pct",
    "side",
    "intended_stake",
    "executable_depth",
    "fill_percentage",
    "spread",
    "best_bid_size",
    "best_ask_size",
    "trade_age_seconds",
    "seconds_to_market_end",
    "market_active",
    "market_closed",
    "market_resolved",
    "depth_walk_json",
    "insufficient_depth_reason",
    "component_scores_json",
    "final_score",
    "verdict",
    "missing_essentials_json",
    "rejection_reasons_json",
    "candidate_id",
    "price_snapshot_id",
)

RECOMMENDED_NEXT_STEP = (
    "PR24P should patch/strengthen Trade Copyability v1 before any "
    "integration. Do not wire automation yet."
)


# --------------------------------------------------------------------------
# Audit dataclasses
# --------------------------------------------------------------------------

@dataclass
class TradeCopyabilityAuditFinding:
    """A single reconciliation finding."""

    key: str
    status: str  # PASS | FAIL | WARN | INFO
    severity: str  # info | low | medium | high | blocker
    summary: str
    evidence: dict
    recommendation: Optional[str] = None


@dataclass
class TradeCopyabilityReconciliationAuditReport:
    """Full reconciliation audit report."""

    formula_present: bool
    weights_sum: Optional[float]
    weights_match_expected: bool
    thresholds_match_expected: bool
    essential_inputs_present: dict
    safety_checks: tuple
    integration_findings: tuple
    persistence_findings: tuple
    production_counts: dict
    recommended_next_step: str
    ready_to_wire_to_automation: bool


# --------------------------------------------------------------------------
# Synthetic input builders (pure, in-memory)
# --------------------------------------------------------------------------

_DEFAULT_WALLET_ID = "audit_wallet_000"
_DEFAULT_TRADE_ID = "audit_trade_000"


def _base_complete_input(**overrides) -> TradeCopyabilityInputV1:
    """Build a fully-specified, valid TradeCopyabilityInputV1.

    The base is a *strong* complete trade (score 100 -> copy_candidate)
    unless overridden. Override any field to weaken/break it.
    """
    base: dict[str, object] = dict(
        wallet_id=_DEFAULT_WALLET_ID,
        source_trade_id=_DEFAULT_TRADE_ID,
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
    # `base` is ``dict[str, object]``; at runtime it holds only the exact
    # field types expected by TradeCopyabilityInputV1. Pyright cannot narrow
    # a dynamic mapping, so we cast to Any for this single construction call
    # rather than sprinkling ignores across every field assignment.
    from typing import Any, cast

    return TradeCopyabilityInputV1(**cast(Any, base))


def _partial_depth_walk_result(side: str = "BUY") -> _dn.DepthWalkResult:
    """A truthful partial-fill depth walk (is_complete=False)."""
    return _dn.DepthWalkResult(
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
        insufficient_reason=_dn.DEPTH_INSUFFICIENT_FOR_STAKE,
    )


# --------------------------------------------------------------------------
# 1-3. Formula / weights / thresholds / essential inputs
# --------------------------------------------------------------------------

def audit_formula_presence() -> dict:
    weights_sum = sum(WEIGHTS.values())
    weights_match = dict(WEIGHTS) == EXPECTED_WEIGHTS
    thresholds_match = (
        VERDICT_COPY_CANDIDATE_MIN == EXPECTED_THRESHOLDS["copy_candidate_min"]
        and VERDICT_WATCHLIST_MIN == EXPECTED_THRESHOLDS["watchlist_min"]
    )
    # Field presence must be checked via dataclasses.fields, because
    # dataclass fields without defaults (e.g. wallet_id) are NOT set as
    # class attributes — ``hasattr`` would wrongly return False.
    import dataclasses

    present_fields = {f.name for f in dataclasses.fields(TradeCopyabilityInputV1)}
    essential_present = {
        f: (f in present_fields) for f in ESSENTIAL_INPUT_FIELDS
    }
    return {
        "formula_present": True,
        "weights_sum": float(weights_sum),
        "weights_match_expected": weights_match,
        "thresholds_match_expected": thresholds_match,
        "essential_inputs_present": essential_present,
        "formula_version": "1",  # TradeScoreResult.formula_version default
        "has_explicit_formula_name_constant": _has_explicit_formula_name_constant(),
    }


def _has_explicit_formula_name_constant() -> bool:
    """True if trade_score_v1 declares an explicit formula NAME constant."""
    import inspect

    src = inspect.getsource(_tsv1)
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("TRADE_COPYABILITY"):
            return True
        # e.g. FORMULA_NAME = "Trade Copyability Score"
        if stripped.startswith("FORMULA_NAME") and "=" in stripped:
            return True
    return False


# --------------------------------------------------------------------------
# 4. Safety behaviour checks
# --------------------------------------------------------------------------

def _mk_finding(key, status, severity, summary, evidence, recommendation=None):
    return TradeCopyabilityAuditFinding(
        key=key,
        status=status,
        severity=severity,
        summary=summary,
        evidence=evidence,
        recommendation=recommendation,
    )


def _check_missing_essential(field: str) -> TradeCopyabilityAuditFinding:
    inp = _base_complete_input(**{field: None})
    res = compute_trade_score_v1(input=inp)
    ok = res.verdict.value == "incomplete"
    return _mk_finding(
        key=f"missing_essential_{field}",
        status="PASS" if ok else "FAIL",
        severity="info" if ok else "high",
        summary=(
            f"Missing '{field}' produces INCOMPLETE"
            if ok else
            f"Missing '{field}' did NOT produce INCOMPLETE"
        ),
        evidence={
            "field": field,
            "verdict": res.verdict.value,
            "missing_essentials": list(res.missing_essentials),
        },
        recommendation=None if ok else (
            f"Ensure '{field}' is treated as a required essential input."
        ),
    )


def _check_depth_rejection(reason_const: str) -> TradeCopyabilityAuditFinding:
    inp = _base_complete_input(depth_status_reason=reason_const)
    res = compute_trade_score_v1(input=inp)
    ok = res.verdict.value == "incomplete"
    return _mk_finding(
        key=f"depth_rejection_{reason_const}",
        status="PASS" if ok else "FAIL",
        severity="info" if ok else "high",
        summary=(
            f"{reason_const} produces INCOMPLETE"
            if ok else
            f"{reason_const} did NOT produce INCOMPLETE"
        ),
        evidence={
            "depth_status_reason": reason_const,
            "verdict": res.verdict.value,
            "rejection_reasons": list(res.rejection_reasons),
        },
    )


def audit_safety_behaviour() -> list:
    findings: list = []

    # 4a. Missing essentials -> incomplete
    for f in (
        "side",
        "intended_stake",
        "executable_depth",
        "spread",
        "trade_age_seconds",
        "seconds_to_market_end",
        "market_active",
    ):
        findings.append(_check_missing_essential(f))

    # 4b. Depth rejection reasons -> incomplete
    for reason in (
        _dn.DEPTH_NOT_CAPTURED,
        _dn.DEPTH_LEVELS_MALFORMED,
        _dn.DEPTH_SNAPSHOT_MISMATCH,
    ):
        findings.append(_check_depth_rejection(reason))

    # 4c. Strong complete trade -> copy_candidate
    strong = compute_trade_score_v1(input=_base_complete_input())
    strong_ok = strong.verdict.value == "copy_candidate"
    findings.append(_mk_finding(
        key="strong_complete_becomes_copy_candidate",
        status="PASS" if strong_ok else "FAIL",
        severity="info" if strong_ok else "high",
        summary=(
            "Strong complete synthetic trade becomes copy_candidate"
            if strong_ok else
            "Strong complete synthetic trade did NOT become copy_candidate"
        ),
        evidence={
            "final_score": strong.score,
            "verdict": strong.verdict.value,
            "components": [
                {"name": c.name, "raw": c.raw_score, "weighted": c.weighted_score}
                for c in strong.components
            ],
        },
    ))

    # 4d. Weak complete trade -> not copy_candidate (skip/watchlist)
    weak = compute_trade_score_v1(input=_base_complete_input(
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
    ))
    weak_ok = weak.verdict.value != "copy_candidate"
    findings.append(_mk_finding(
        key="weak_complete_not_copy_candidate",
        status="PASS" if weak_ok else "FAIL",
        severity="info" if weak_ok else "medium",
        summary=(
            "Weak complete synthetic trade does NOT become copy_candidate"
            if weak_ok else
            "Weak complete synthetic trade unexpectedly became copy_candidate"
        ),
        evidence={
            "final_score": weak.score,
            "verdict": weak.verdict.value,
        },
    ))

    # 4e. Partial fill preserves insufficient-depth reason
    partial = compute_trade_score_v1(input=_base_complete_input(
        depth_walk_result=_partial_depth_walk_result("BUY"),
    ))
    partial_ok = _dn.DEPTH_INSUFFICIENT_FOR_STAKE in partial.rejection_reasons
    findings.append(_mk_finding(
        key="partial_fill_preserves_insufficient_depth_reason",
        status="PASS" if partial_ok else "FAIL",
        severity="info" if partial_ok else "high",
        summary=(
            "Partial depth walk preserves DEPTH_INSUFFICIENT_FOR_STAKE reason"
            if partial_ok else
            "Partial depth walk did NOT preserve insufficient-depth reason"
        ),
        evidence={
            "verdict": partial.verdict.value,
            "rejection_reasons": list(partial.rejection_reasons),
            "is_complete": partial.input.depth_walk_result.is_complete,
        },
    ))

    # 4f. Short crypto (< 6h) -> skip with short_crypto_exclusion
    short_crypto = compute_trade_score_v1(input=_base_complete_input(
        market_category="crypto",
        seconds_to_market_end=2 * 3600.0,  # 2h < 6h
    ))
    sc_ok = (
        short_crypto.verdict.value == "skip"
        and "short_crypto_exclusion" in short_crypto.rejection_reasons
    )
    findings.append(_mk_finding(
        key="short_crypto_under_6h_skip",
        status="PASS" if sc_ok else "FAIL",
        severity="info" if sc_ok else "high",
        summary=(
            "Short crypto (<6h) produces skip with short_crypto_exclusion"
            if sc_ok else
            "Short crypto (<6h) did NOT produce expected skip"
        ),
        evidence={
            "verdict": short_crypto.verdict.value,
            "rejection_reasons": list(short_crypto.rejection_reasons),
        },
    ))

    # 4g. Duration bucket boundaries
    expected = {
        14 * 60 + 59: 0.0,       # 14m59s -> excluded
        15 * 60: 40.0,           # 15m00s -> 40
        6 * 3600: 75.0,          # 6h -> 75
        24 * 3600: 100.0,        # 1d -> 100
        14 * 24 * 3600: 100.0,   # 14d -> 100
        21 * 24 * 3600: 80.0,    # 21d -> 80
        45 * 24 * 3600: 40.0,    # 45d -> 40
        46 * 24 * 3600: 0.0,     # >45d -> excluded
    }
    boundary_rows = []
    all_ok = True
    for secs, exp in expected.items():
        got = _tsv1._holding_period_component(float(secs))[0]
        ok = abs(got - exp) < 1e-9
        all_ok = all_ok and ok
        boundary_rows.append({"seconds": secs, "expected": exp, "got": got, "ok": ok})
    findings.append(_mk_finding(
        key="duration_boundaries",
        status="PASS" if all_ok else "FAIL",
        severity="info" if all_ok else "high",
        summary=(
            "Duration bucket boundaries behave as documented"
            if all_ok else
            "Duration bucket boundaries deviate from documented values"
        ),
        evidence={"rows": boundary_rows},
    ))

    return findings


# --------------------------------------------------------------------------
# 5. Integration-risk audit (static source inspection)
# --------------------------------------------------------------------------

def _read_source(rel_path: str) -> str:
    base = os.path.dirname(_tsv1.__file__)
    path = os.path.join(base, rel_path)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def audit_integration_risks() -> list:
    findings: list = []

    paper_src = _read_source("paper_signal.py")
    verdict_src = _read_source("verdict_generation.py")
    trade_src = _read_source("trade_score_v1.py")

    # F1: paper_signal imports compute_wallet_score_v1 from OLD wallet_score_v1
    paper_uses_old = (
        "from polycopy.scoring.wallet_score_v1 import" in paper_src
        and "compute_wallet_score_v1" in paper_src
    )
    findings.append(_mk_finding(
        key="paper_signal_uses_old_wallet_score",
        status="FAIL" if paper_uses_old else "PASS",
        severity="high" if paper_uses_old else "info",
        summary=(
            "paper_signal.py imports compute_wallet_score_v1 from old "
            "wallet_score_v1 (not PR24M)"
            if paper_uses_old else
            "paper_signal.py does not import old compute_wallet_score_v1"
        ),
        evidence={
            "imports_compute_wallet_score_v1": paper_uses_old,
            "imports_wallet_skill_score_v1": (
                "wallet_skill_score_v1" in paper_src
            ),
        },
        recommendation=(
            "Bridge paper_signal to PR24M WalletSkillScoreV1 in a dedicated "
            "bridge PR before wiring Trade Copyability v1 into automation."
            if paper_uses_old else None
        ),
    ))

    # F2: verdict_generation imports WalletVerdict from OLD wallet_score_v1
    verdict_uses_old = (
        "from polycopy.scoring.wallet_score_v1 import" in verdict_src
        and "WalletVerdict" in verdict_src
    )
    findings.append(_mk_finding(
        key="verdict_generation_uses_old_wallet_verdict",
        status="FAIL" if verdict_uses_old else "PASS",
        severity="medium" if verdict_uses_old else "info",
        summary=(
            "verdict_generation.py imports WalletVerdict from old "
            "wallet_score_v1 (not PR24M)"
            if verdict_uses_old else
            "verdict_generation.py does not import old WalletVerdict"
        ),
        evidence={
            "imports_wallet_verdict_from_old": verdict_uses_old,
            "imports_wallet_skill_score_v1": (
                "wallet_skill_score_v1" in verdict_src
            ),
        },
        recommendation=(
            "Migrate verdict_generation WalletVerdict usage to PR24M in a "
            "bridge PR."
            if verdict_uses_old else None
        ),
    ))

    # F3: Trade Copyability v1 is independent of wallet scoring objects
    trade_independent = (
        "wallet_score_v1" not in trade_src
        and "wallet_skill_score_v1" not in trade_src
    )
    findings.append(_mk_finding(
        key="trade_v1_independent_of_wallet_scoring",
        status="PASS" if trade_independent else "WARN",
        severity="info" if trade_independent else "medium",
        summary=(
            "Trade Copyability v1 is independent of wallet-score modules "
            "(only helpers + depth_normalization) and is reusable"
            if trade_independent else
            "Trade Copyability v1 references wallet-score modules"
        ),
        evidence={
            "imports_wallet_score_v1": "wallet_score_v1" in trade_src,
            "imports_wallet_skill_score_v1": "wallet_skill_score_v1" in trade_src,
        },
    ))

    # F4: guardrail — must not be wired to PR24M / paper_signal yet
    findings.append(_mk_finding(
        key="no_pr24m_wiring_guardrail",
        status="WARN",
        severity="blocker",
        summary=(
            "Final signal generation must NOT be wired to PR24M Wallet "
            "Skill Score (or paper_signal) without a dedicated bridge PR."
        ),
        evidence={
            "ready_to_wire": False,
            "paper_signal_uses_old_wallet_score": paper_uses_old,
            "verdict_generation_uses_old_wallet_verdict": verdict_uses_old,
        },
        recommendation=(
            "Open PR24P to patch/strengthen Trade Copyability v1 first; "
            "introduce a bridge PR only after that."
        ),
    ))

    return findings


# --------------------------------------------------------------------------
# 6. Persistence audit (static schema + live DB columns, read-only)
# --------------------------------------------------------------------------

def _schema_db_dir() -> str:
    # trade_score_v1 lives in .../polycopy/scoring/ -> db dir is sibling
    return os.path.join(os.path.dirname(os.path.dirname(_tsv1.__file__)), "db")


def _read_schema_sources() -> list:
    db_dir = _schema_db_dir()
    out = []
    if os.path.isdir(db_dir):
        for fn in sorted(os.listdir(db_dir)):
            if fn.startswith("schema") and fn.endswith(".py"):
                try:
                    with open(os.path.join(db_dir, fn), encoding="utf-8") as fh:
                        out.append((fn, fh.read()))
                except OSError:
                    continue
    return out


def audit_persistence(db_path: Optional[str] = None) -> list:
    findings: list = []

    schemas = _read_schema_sources()
    # Detect the table definition by its distinctive table name AND a column
    # that only appears in this table's DDL (`insufficient_depth_reason`).
    # Deliberately avoids embedding any DDL keyword literal in the check so the
    # audit module source stays free of mutating-verb string literals.
    table_in_schema = any(
        "trade_copyability_decisions" in src
        and "insufficient_depth_reason" in src
        for _, src in schemas
    )
    cols_in_schema = {
        c: any(f"        {c}" in src or f"{c} " in src for _, src in schemas)
        for c in PERSISTENCE_REQUIRED_COLUMNS
    }

    # Live DB column coverage (read-only)
    live_present: Optional[dict] = None
    live_table_exists: Optional[bool] = None
    if db_path and os.path.exists(db_path):
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                cur = con.cursor()
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='trade_copyability_decisions'"
                )
                live_table_exists = cur.fetchone() is not None
                if live_table_exists:
                    cur.execute("PRAGMA table_info(trade_copyability_decisions)")
                    live_cols = {r[1] for r in cur.fetchall()}
                    live_present = {
                        c: (c in live_cols) for c in PERSISTENCE_REQUIRED_COLUMNS
                    }
            finally:
                con.close()
        except sqlite3.Error:
            live_present = None
            live_table_exists = None

    all_schema_cols = all(cols_in_schema.values())
    findings.append(_mk_finding(
        key="trade_copyability_decisions_schema_present",
        status="PASS" if (table_in_schema and all_schema_cols) else "WARN",
        severity="info" if (table_in_schema and all_schema_cols) else "medium",
        summary=(
            "trade_copyability_decisions table + required columns present in schema"
            if (table_in_schema and all_schema_cols) else
            "trade_copyability_decisions schema coverage incomplete"
        ),
        evidence={
            "table_in_schema": table_in_schema,
            "missing_schema_columns": [
                c for c, ok in cols_in_schema.items() if not ok
            ],
        },
        recommendation=None if all_schema_cols else (
            "Add the missing columns to the schema before persistence use."
        ),
    ))

    if live_present is not None:
        all_live_cols = all(live_present.values())
        findings.append(_mk_finding(
            key="trade_copyability_decisions_live_columns",
            status="PASS" if all_live_cols else "WARN",
            severity="info" if all_live_cols else "medium",
            summary=(
                "Live DB trade_copyability_decisions has all required columns"
                if all_live_cols else
                "Live DB trade_copyability_decisions missing columns"
            ),
            evidence={
                "table_exists": live_table_exists,
                "missing_columns": [
                    c for c, ok in live_present.items() if not ok
                ],
            },
        ))
    else:
        findings.append(_mk_finding(
            key="trade_copyability_decisions_live_columns",
            status="INFO",
            severity="info",
            summary="Live DB not inspected (no db_path / file absent).",
            evidence={"db_path": db_path},
        ))

    return findings


# --------------------------------------------------------------------------
# 7. Production DB read-only sparse counts
# --------------------------------------------------------------------------

def read_production_counts(db_path: Optional[str]) -> dict:
    """Read-only sparse row counts for the audited tables.

    Opens SQLite with ``mode=ro``. Performs NO mutation. If the file is
    missing or unreadable, every table reports ``None``.
    """
    counts: dict = {t: None for t in PRODUCTION_TABLES}
    if not db_path or not os.path.exists(db_path):
        return counts
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return counts
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing = {r[0] for r in cur.fetchall()}
        for t in PRODUCTION_TABLES:
            if t in existing:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {t}")
                    counts[t] = int(cur.fetchone()[0])
                except sqlite3.Error:
                    counts[t] = None
            else:
                counts[t] = None
    finally:
        con.close()
    return counts


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------

def run_reconciliation_audit(
    db_path: Optional[str] = None,
) -> TradeCopyabilityReconciliationAuditReport:
    formula = audit_formula_presence()
    safety = tuple(audit_safety_behaviour())
    integration = tuple(audit_integration_risks())
    persistence = tuple(audit_persistence(db_path=db_path))
    counts = read_production_counts(db_path)

    return TradeCopyabilityReconciliationAuditReport(
        formula_present=formula["formula_present"],
        weights_sum=formula["weights_sum"],
        weights_match_expected=formula["weights_match_expected"],
        thresholds_match_expected=formula["thresholds_match_expected"],
        essential_inputs_present=formula["essential_inputs_present"],
        safety_checks=safety,
        integration_findings=integration,
        persistence_findings=persistence,
        production_counts=counts,
        recommended_next_step=RECOMMENDED_NEXT_STEP,
        ready_to_wire_to_automation=False,
    )


# --------------------------------------------------------------------------
# Human-readable / JSON rendering
# --------------------------------------------------------------------------

def _finding_to_dict(f: TradeCopyabilityAuditFinding) -> dict:
    return {
        "key": f.key,
        "status": f.status,
        "severity": f.severity,
        "summary": f.summary,
        "evidence": f.evidence,
        "recommendation": f.recommendation,
    }


def report_to_dict(
    report: TradeCopyabilityReconciliationAuditReport,
) -> dict:
    return {
        "formula_present": report.formula_present,
        "weights_sum": report.weights_sum,
        "weights_match_expected": report.weights_match_expected,
        "thresholds_match_expected": report.thresholds_match_expected,
        "essential_inputs_present": report.essential_inputs_present,
        "safety_checks": [_finding_to_dict(f) for f in report.safety_checks],
        "integration_findings": [
            _finding_to_dict(f) for f in report.integration_findings
        ],
        "persistence_findings": [
            _finding_to_dict(f) for f in report.persistence_findings
        ],
        "production_counts": report.production_counts,
        "recommended_next_step": report.recommended_next_step,
        "ready_to_wire_to_automation": report.ready_to_wire_to_automation,
    }


def report_to_human(
    report: TradeCopyabilityReconciliationAuditReport,
    limit: int = 10,
) -> str:
    lines: list = []
    lines.append("=" * 72)
    lines.append("PR24O — TRADE COPYABILITY v1 RECONCILIATION AUDIT (READ-ONLY)")
    lines.append("=" * 72)

    lines.append("\n## 1. Formula / Component Status")
    lines.append(f"  Formula present:           {report.formula_present}")
    lines.append(f"  Weights sum:               {report.weights_sum}")
    lines.append(f"  Weights match expected:    {report.weights_match_expected}")
    lines.append(f"  Thresholds match expected: {report.thresholds_match_expected}")

    lines.append("\n## 2. Weights Table")
    for name, exp in EXPECTED_WEIGHTS.items():
        actual = WEIGHTS.get(name)
        match = "ok" if actual == exp else "MISMATCH"
        lines.append(f"  {name:<32} expected={exp:>5}  actual={actual}  [{match}]")

    lines.append("\n## 3. Essential Input Coverage")
    miss = [f for f, ok in report.essential_inputs_present.items() if not ok]
    lines.append(f"  fields present: {len(report.essential_inputs_present) - len(miss)}/"
                 f"{len(report.essential_inputs_present)}")
    if miss:
        lines.append(f"  MISSING: {miss}")
    else:
        lines.append("  All required fields present on TradeCopyabilityInputV1.")

    lines.append("\n## 4. Safety Behaviour Summary")
    for f in report.safety_checks:
        lines.append(f"  [{f.status:<4}] {f.key}")

    lines.append("\n## 5. Integration Risks")
    for f in report.integration_findings:
        lines.append(f"  [{f.status:<4}] ({f.severity}) {f.key}")
        if f.recommendation:
            lines.append(f"         -> {f.recommendation}")

    lines.append("\n## 6. Persistence Audit")
    for f in report.persistence_findings:
        lines.append(f"  [{f.status:<4}] {f.key}")

    lines.append(f"\n## 7. Production DB Read-Only Counts (limit={limit})")
    shown = 0
    for t in PRODUCTION_TABLES:
        v = report.production_counts.get(t)
        lines.append(f"  {t:<38} {v}")
        shown += 1
        if shown >= limit:
            break

    lines.append("\n## 8. Recommended Next Action")
    lines.append(f"  {report.recommended_next_step}")
    lines.append(f"  ready_to_wire_to_automation = {report.ready_to_wire_to_automation}")

    lines.append("=" * 72)
    lines.append("GUARDRAILS: audit/report only — no writes, no wiring, no restart.")
    lines.append("=" * 72)
    return "\n".join(lines)
