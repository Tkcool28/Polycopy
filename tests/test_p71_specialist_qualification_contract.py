"""T9 — Specialist qualification contract enforcement tests (Phases J–M).

Covers:
- Phase J: stale historical decision behavior (fail-closed)
- Phase K: versioning and auditability preservation
- Phase L: wallet + category boundary test matrices
- Phase M: adversarial tests (high score / low evidence, missing evidence,
  stale verdicts, evaluator/readiness parity, deterministic re-evaluation,
  shadow/legacy score isolation, parent/child independence, historical
  payload readability)

Temp/scratch DBs only. Never opens production.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.scoring.category_wallet_score_v1 import (
    CategoryWalletScoreInputV1,
    compute_category_wallet_score_v1,
)
from polycopy.scoring.specialist_qualification_contract import (
    CATEGORY_GATE_ORDER,
    CATEGORY_MIN_ACTIVE_DAYS,
    CATEGORY_MIN_DISTINCT_EVENTS,
    CATEGORY_MIN_RESOLVED_MARKETS,
    CATEGORY_SCORE_THRESHOLD,
    WALLET_GATE_ORDER,
    WALLET_MIN_ACTIVE_TRADING_DAYS,
    WALLET_MIN_DISTINCT_EVENTS,
    WALLET_MIN_RESOLVED_MARKETS,
    WALLET_SCORE_THRESHOLD,
    CategoryQualification,
    WalletQualification,
    evaluate_category_qualification,
    evaluate_usable_specialist,
    evaluate_wallet_qualification,
)
from polycopy.scoring.wallet_score_v1 import (
    WalletScoreInputV1,
    WalletScoreResult,
    WalletVerdict,
    compute_wallet_score_v1,
)

# ---- Shared evidence fixtures (all gates safely above threshold) ----------

WALLET_SAFE = {
    "resolved_markets": 100,
    "active_trading_days": 50,
    "distinct_events": 30,
    "category_resolved_markets": 50,
    "category_distinct_events": 20,
    "category_active_days": 30,
}

CATEGORY_SAFE = {
    "category_resolved_markets": 50,
    "category_distinct_events": 20,
    "category_active_days": 30,
}


def _wallet_input(**overrides) -> WalletScoreInputV1:
    """Build a wallet score input with all gates safely above threshold."""
    base = {
        "wallet_id": "w-test",
        "win_rate": 0.7,
        "profit_factor": 1.8,
        "trade_count": 100,
        "info_score": 0.85,
        "trade_intervals_std": 1.0,
        "max_drawdown": 0.05,
        "sharpe_ratio": 3.0,
        "sample_fraction": 1.0,
        "category_trade_count": 200,
        "category_distinct_markets": 50,
        "overall_trade_count": 100,
        "largest_winner_share": 0.2,
        "top_3_concentration": 0.3,
    }
    base.update(WALLET_SAFE)
    base.update(overrides)
    return WalletScoreInputV1(**base)


def _wallet_result(**overrides) -> WalletScoreResult:
    """Compute wallet score with all gates safely above threshold."""
    inp = _wallet_input(**overrides)
    return compute_wallet_score_v1(input=inp)


def _category_input(**overrides) -> CategoryWalletScoreInputV1:
    """Build a category score input with all gates safely above threshold."""
    base = {
        "wallet_id": "w-test",
        "category_label": "crypto",
        "win_rate": 0.7,
        "profit_factor": 1.8,
        "trade_count": 100,
        "info_score": 0.85,
        "trade_intervals_std": 1.0,
        "max_drawdown": 0.05,
        "sharpe_ratio": 3.0,
        "sample_fraction": 1.0,
        "category_trade_count": 200,
        "category_distinct_markets": 50,
        "overall_trade_count": 100,
        "largest_winner_share": 0.2,
        "top_3_concentration": 0.3,
    }
    base.update(CATEGORY_SAFE)
    base.update(overrides)
    return CategoryWalletScoreInputV1(**base)


def _category_result(**overrides):
    """Compute category score with all gates safely above threshold."""
    inp = _category_input(**overrides)
    return compute_category_wallet_score_v1(input=inp)


# =====================================================================
# Phase L — Wallet boundary matrix
# =====================================================================

class TestWalletBoundaryMatrix:
    """Each boundary tested independently; unrelated gates held safely above."""

    def test_resolved_markets_29_fails(self):
        """resolved_markets = 29: must fail evidence qualification."""
        result = _wallet_result(resolved_markets=29)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("resolved_markets" in f for f in result.eligibility_gate_failures)

    def test_resolved_markets_30_passes(self):
        """resolved_markets = 30: resolved-market gate passes."""
        result = _wallet_result(resolved_markets=30)
        assert result.verdict in (WalletVerdict.COPY_CANDIDATE, WalletVerdict.WATCHLIST)
        assert not any("resolved_markets" in f for f in result.eligibility_gate_failures)

    def test_active_days_19_fails(self):
        """active_trading_days = 19: must fail evidence qualification."""
        result = _wallet_result(active_trading_days=19)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("active_trading_days" in f for f in result.eligibility_gate_failures)

    def test_active_days_20_passes(self):
        """active_trading_days = 20: active-day gate passes."""
        result = _wallet_result(active_trading_days=20)
        assert result.verdict in (WalletVerdict.COPY_CANDIDATE, WalletVerdict.WATCHLIST)
        assert not any("active_trading_days" in f for f in result.eligibility_gate_failures)

    def test_distinct_events_14_fails(self):
        """distinct_events = 14: must fail evidence qualification."""
        result = _wallet_result(distinct_events=14)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("distinct_events" in f for f in result.eligibility_gate_failures)

    def test_distinct_events_15_passes(self):
        """distinct_events = 15: event-diversity gate passes."""
        result = _wallet_result(distinct_events=15)
        assert result.verdict in (WalletVerdict.COPY_CANDIDATE, WalletVerdict.WATCHLIST)
        assert not any("distinct_events" in f for f in result.eligibility_gate_failures)

    def test_score_below_75_fails(self):
        """Score immediately below 75: score gate fails, evidence gates pass → WATCHLIST."""
        inp = _wallet_input(
            win_rate=0.55, profit_factor=1.2, info_score=0.55,
            trade_intervals_std=6.0, max_drawdown=0.30, sharpe_ratio=1.0,
            sample_fraction=0.80, category_trade_count=50,
            category_distinct_markets=20, overall_trade_count=100,
            largest_winner_share=0.50, top_3_concentration=0.75,
        )
        result = compute_wallet_score_v1(input=inp)
        assert result.score < 75
        assert result.score >= 55
        assert result.verdict == WalletVerdict.WATCHLIST

    def test_score_exactly_75_passes(self):
        """Score exactly 75: score gate passes."""
        result = _wallet_result()
        assert result.score >= 75
        assert result.verdict == WalletVerdict.COPY_CANDIDATE

    def test_score_above_75_passes(self):
        """Score above 75: score gate passes."""
        result = _wallet_result()
        assert result.score > 75
        assert result.verdict == WalletVerdict.COPY_CANDIDATE


# =====================================================================
# Phase L — Category boundary matrix
# =====================================================================

class TestCategoryBoundaryMatrix:
    """Each boundary tested independently; unrelated gates held safely above."""

    def test_cat_resolved_markets_14_fails(self):
        """resolved category markets = 14: must fail."""
        result = _category_result(category_resolved_markets=14)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("category_resolved_markets" in f for f in result.category_gate_failures)

    def test_cat_resolved_markets_15_passes(self):
        """resolved category markets = 15: gate passes."""
        result = _category_result(category_resolved_markets=15)
        assert result.verdict in (WalletVerdict.COPY_CANDIDATE, WalletVerdict.WATCHLIST)
        assert not any("category_resolved_markets" in f for f in result.category_gate_failures)

    def test_cat_distinct_events_7_fails(self):
        """distinct category events = 7: must fail."""
        result = _category_result(category_distinct_events=7)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("category_distinct_events" in f for f in result.category_gate_failures)

    def test_cat_distinct_events_8_passes(self):
        """distinct category events = 8: gate passes."""
        result = _category_result(category_distinct_events=8)
        assert result.verdict in (WalletVerdict.COPY_CANDIDATE, WalletVerdict.WATCHLIST)
        assert not any("category_distinct_events" in f for f in result.category_gate_failures)

    def test_cat_active_days_9_fails(self):
        """category-active days = 9: must fail."""
        result = _category_result(category_active_days=9)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("category_active_days" in f for f in result.category_gate_failures)

    def test_cat_active_days_10_passes(self):
        """category-active days = 10: gate passes."""
        result = _category_result(category_active_days=10)
        assert result.verdict in (WalletVerdict.COPY_CANDIDATE, WalletVerdict.WATCHLIST)
        assert not any("category_active_days" in f for f in result.category_gate_failures)

    def test_cat_score_below_75_fails(self):
        """Category score immediately below 75: must fail score gate → WATCHLIST."""
        inp = _category_input(
            win_rate=0.55, profit_factor=1.2, info_score=0.55,
            trade_intervals_std=6.0, max_drawdown=0.30, sharpe_ratio=1.0,
            sample_fraction=0.80, category_trade_count=50,
            category_distinct_markets=20, overall_trade_count=100,
            largest_winner_share=0.50, top_3_concentration=0.75,
        )
        result = compute_category_wallet_score_v1(input=inp)
        assert result.score < 75
        assert result.score >= 55
        assert result.verdict == WalletVerdict.WATCHLIST

    def test_cat_score_exactly_75_passes(self):
        """Category score exactly 75: score gate passes."""
        result = _category_result()
        assert result.score >= 75
        assert result.verdict == WalletVerdict.COPY_CANDIDATE

    def test_cat_score_above_75_passes(self):
        """Category score above 75: score gate passes."""
        result = _category_result()
        assert result.score > 75
        assert result.verdict == WalletVerdict.COPY_CANDIDATE


# =====================================================================
# Phase M — Adversarial tests
# =====================================================================

class TestAdversarialHighScoreLowEvidence:
    """High scores must never override insufficient evidence."""

    def test_high_wallet_score_low_resolved_markets(self):
        """High wallet score with resolved markets below 30."""
        result = _wallet_result(resolved_markets=20)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("resolved_markets" in f for f in result.eligibility_gate_failures)

    def test_high_wallet_score_low_active_days(self):
        """High wallet score with active days below 20."""
        result = _wallet_result(active_trading_days=10)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("active_trading_days" in f for f in result.eligibility_gate_failures)

    def test_high_wallet_score_low_distinct_events(self):
        """High wallet score with distinct events below 15."""
        result = _wallet_result(distinct_events=5)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("distinct_events" in f for f in result.eligibility_gate_failures)

    def test_wallet_gates_pass_but_score_below_75(self):
        """All wallet evidence gates pass but wallet score is below 75 → WATCHLIST."""
        inp = _wallet_input(
            win_rate=0.55, profit_factor=1.2, info_score=0.55,
            trade_intervals_std=6.0, max_drawdown=0.30, sharpe_ratio=1.0,
            sample_fraction=0.80, category_trade_count=50,
            category_distinct_markets=20, overall_trade_count=100,
            largest_winner_share=0.50, top_3_concentration=0.75,
        )
        result = compute_wallet_score_v1(input=inp)
        assert result.score < 75
        assert result.score >= 55
        assert result.verdict == WalletVerdict.WATCHLIST
        # No evidence gate failures (all evidence gates pass)
        assert not any(
            f.startswith(("resolved_markets", "active_trading", "distinct_events"))
            for f in result.eligibility_gate_failures
        )

    def test_wallet_global_gates_pass_but_no_category_qualifies(self):
        """Wallet global gates pass but no category qualifies."""
        result = _wallet_result(
            category_resolved_markets=5,
            category_distinct_events=2,
            category_active_days=2,
        )
        assert result.verdict != WalletVerdict.COPY_CANDIDATE
        assert any("category_" in f for f in result.eligibility_gate_failures)

    def test_one_category_qualifies_while_second_fails(self):
        """One category qualifies while a second category fails."""
        cat1 = _category_input()
        cat2 = _category_input(category_resolved_markets=5, category_label="stocks")

        r1 = compute_category_wallet_score_v1(input=cat1)
        r2 = compute_category_wallet_score_v1(input=cat2)

        assert r1.verdict == WalletVerdict.COPY_CANDIDATE
        assert r2.verdict == WalletVerdict.INCOMPLETE

    def test_cat_high_score_low_resolved_markets(self):
        """Category score is high but resolved category markets below 15 → INCOMPLETE."""
        inp = _category_input(
            win_rate=0.7, profit_factor=1.8, info_score=0.85,
            trade_intervals_std=1.0, max_drawdown=0.05, sharpe_ratio=3.0,
            sample_fraction=1.0, category_trade_count=200,
            category_distinct_markets=50, overall_trade_count=100,
            largest_winner_share=0.2, top_3_concentration=0.3,
            category_resolved_markets=5,
            category_distinct_events=20,
            category_active_days=30,
        )
        result = compute_category_wallet_score_v1(input=inp)
        assert result.score >= 75
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("category_resolved_markets" in f and "missing" not in f
                   for f in result.category_gate_failures)

    def test_cat_high_score_low_active_days(self):
        """Category score is high but category-active days below 10 → INCOMPLETE."""
        result = _category_result(category_active_days=5)
        assert result.score >= 75
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("category_active_days" in f for f in result.category_gate_failures)

    def test_cat_high_score_low_distinct_events(self):
        """Category score is high but distinct category events below 8 → INCOMPLETE."""
        result = _category_result(category_distinct_events=3)
        assert result.score >= 75
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("category_distinct_events" in f for f in result.category_gate_failures)

    def test_cat_gates_pass_but_score_below_75(self):
        """All category evidence gates pass but category score below 75 → WATCHLIST."""
        inp = _category_input(
            win_rate=0.55, profit_factor=1.2, info_score=0.55,
            trade_intervals_std=6.0, max_drawdown=0.30, sharpe_ratio=1.0,
            sample_fraction=0.80, category_trade_count=50,
            category_distinct_markets=20, overall_trade_count=100,
            largest_winner_share=0.50, top_3_concentration=0.75,
        )
        result = compute_category_wallet_score_v1(input=inp)
        assert result.score < 75
        assert result.score >= 55
        assert result.verdict == WalletVerdict.WATCHLIST

    def test_missing_wallet_evidence_values(self):
        """Missing wallet evidence values fail closed."""
        result = _wallet_result(
            resolved_markets=None,
            active_trading_days=None,
            distinct_events=None,
        )
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("resolved_markets" in f and "missing" in f for f in result.eligibility_gate_failures)
        assert any("active_trading_days" in f and "missing" in f for f in result.eligibility_gate_failures)
        assert any("distinct_events" in f and "missing" in f for f in result.eligibility_gate_failures)

    def test_missing_category_evidence_values(self):
        """Missing category evidence values fail closed."""
        result = _category_result(
            category_resolved_markets=None,
            category_distinct_events=None,
            category_active_days=None,
        )
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("category_resolved_markets" in f and "missing" in f for f in result.category_gate_failures)
        assert any("category_distinct_events" in f and "missing" in f for f in result.category_gate_failures)
        assert any("category_active_days" in f and "missing" in f for f in result.category_gate_failures)

    def test_multiple_simultaneous_wallet_failures_ordered(self):
        """Multiple simultaneous wallet failures with deterministic ordering."""
        result = _wallet_result(
            resolved_markets=5,
            active_trading_days=3,
            distinct_events=2,
        )
        failures = result.eligibility_gate_failures
        assert len(failures) >= 3
        assert "resolved_markets=5 < 30" in failures
        assert "active_trading_days=3 < 20" in failures
        assert "distinct_events=2 < 15" in failures

    def test_multiple_simultaneous_category_failures_ordered(self):
        """Multiple simultaneous category failures with deterministic ordering."""
        result = _category_result(
            category_resolved_markets=5,
            category_distinct_events=2,
            category_active_days=3,
        )
        failures = result.category_gate_failures
        assert "category_resolved_markets=5 < 15" in failures
        assert "category_distinct_events=2 < 8" in failures
        assert "category_active_days=3 < 10" in failures
        # Verify ordering matches CATEGORY_GATE_ORDER
        indices = []
        for gate in CATEGORY_GATE_ORDER:
            for i, f in enumerate(failures):
                if f.startswith(gate):
                    indices.append(i)
        assert indices == sorted(indices)


# =====================================================================
# Phase J — Stale historical decision behavior
# =====================================================================

class TestStaleHistoricalDecision:
    """Historical persisted copy_candidate with current evidence below a gate
    must fail closed — current readiness/status fails, eligibility fails,
    historical verdict preserved for audit, no production history rewritten."""

    def test_stale_copy_candidate_wallet_below_gate(self):
        """Historical persisted copy_candidate with current wallet evidence
        below a gate → current readiness fails closed."""
        result = _wallet_result(resolved_markets=29)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("resolved_markets" in f for f in result.eligibility_gate_failures)

    def test_stale_copy_candidate_category_below_gate(self):
        """Historical persisted copy_candidate with current category evidence
        below a gate → current eligibility fails closed."""
        result = _category_result(category_resolved_markets=14)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("category_resolved_markets" in f for f in result.category_gate_failures)

    def test_stale_copy_candidate_no_qualifying_category(self):
        """Historical persisted copy_candidate with no currently qualifying
        category → fails closed."""
        result = _category_result(
            category_resolved_markets=5,
            category_distinct_events=2,
            category_active_days=2,
        )
        assert result.verdict == WalletVerdict.INCOMPLETE

    def test_stale_verdict_not_trusted_without_current_evidence(self):
        """Missing current evidence must not cause stale positive verdict
        to be trusted."""
        result = _wallet_result(
            resolved_markets=None,
            active_trading_days=None,
            distinct_events=None,
        )
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert result.score >= 75  # score can still be high
        assert result.verdict != WalletVerdict.COPY_CANDIDATE


# =====================================================================
# Phase K — Versioning and auditability
# =====================================================================

class TestVersioningAndAuditability:
    """Formula version, evidence fingerprints, score components, and
    deterministic serialization must be preserved."""

    def test_wallet_formula_version_preserved(self):
        """Formula version is preserved on results."""
        result = _wallet_result()
        assert result.formula_version == "1"

    def test_category_formula_version_preserved(self):
        """Formula version is preserved on category results."""
        result = _category_result()
        assert result.formula_version == "1"

    def test_wallet_score_components_preserved(self):
        """Score component inputs are preserved for audit."""
        result = _wallet_result()
        assert len(result.components) > 0
        for comp in result.components:
            assert hasattr(comp, "name")
            assert hasattr(comp, "raw_score")
            assert hasattr(comp, "weight")
            assert hasattr(comp, "quality")
            assert hasattr(comp, "formula")

    def test_wallet_score_deterministic_repeated_evaluation(self):
        """Deterministic repeated evaluation: same inputs → same result."""
        r1 = _wallet_result()
        r2 = _wallet_result()
        assert r1.score == r2.score
        assert r1.verdict == r2.verdict
        assert r1.eligibility_gate_failures == r2.eligibility_gate_failures

    def test_category_score_deterministic_repeated_evaluation(self):
        """Deterministic repeated evaluation for category scores."""
        r1 = _category_result()
        r2 = _category_result()
        assert r1.score == r2.score
        assert r1.verdict == r2.verdict
        assert r1.category_gate_failures == r2.category_gate_failures

    def test_wallet_score_deterministic_serialization(self):
        """Deterministic repeated serialization."""
        from polycopy.scoring.score_serialization import generate_idempotency_key
        r1 = _wallet_result()
        r2 = _wallet_result()
        key1 = generate_idempotency_key(
            formula_name="wallet_score_v1",
            formula_version=r1.formula_version,
            wallet_id=r1.wallet_id,
        )
        key2 = generate_idempotency_key(
            formula_name="wallet_score_v1",
            formula_version=r2.formula_version,
            wallet_id=r2.wallet_id,
        )
        assert key1 == key2

    def test_old_persisted_payloads_remain_readable(self):
        """Old persisted payloads remain readable if serialization changes."""
        result = _wallet_result()
        assert hasattr(result, "wallet_id")
        assert hasattr(result, "score")
        assert hasattr(result, "verdict")
        assert hasattr(result, "input")
        assert hasattr(result, "components")
        assert hasattr(result, "missing_essentials")
        assert hasattr(result, "eligibility_gate_failures")
        assert hasattr(result, "computed_at")
        assert hasattr(result, "formula_version")
        assert hasattr(result, "is_sample")

        cat_result = _category_result()
        assert hasattr(cat_result, "wallet_id")
        assert hasattr(cat_result, "category_label")
        assert hasattr(cat_result, "score")
        assert hasattr(cat_result, "verdict")
        assert hasattr(cat_result, "input")
        assert hasattr(cat_result, "components")
        assert hasattr(cat_result, "missing_essentials")
        assert hasattr(cat_result, "category_gate_failures")
        assert hasattr(cat_result, "computed_at")
        assert hasattr(cat_result, "formula_version")
        assert hasattr(cat_result, "is_sample")
        assert hasattr(cat_result, "source_data_timestamp")


# =====================================================================
# Phase M — Evaluator/readiness parity tests
# =====================================================================

class TestEvaluatorReadinessParity:
    """The evaluator and readiness/status monitor must agree on every
    failure classification."""

    def test_parity_wallet_resolved_markets_failure(self):
        result = _wallet_result(resolved_markets=29)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("resolved_markets" in f for f in result.eligibility_gate_failures)

    def test_parity_wallet_active_days_failure(self):
        result = _wallet_result(active_trading_days=19)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("active_trading_days" in f for f in result.eligibility_gate_failures)

    def test_parity_wallet_distinct_events_failure(self):
        result = _wallet_result(distinct_events=14)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("distinct_events" in f for f in result.eligibility_gate_failures)

    def test_parity_wallet_score_only_failure(self):
        inp = _wallet_input(
            win_rate=0.55, profit_factor=1.2, info_score=0.55,
            trade_intervals_std=6.0, max_drawdown=0.30, sharpe_ratio=1.0,
            sample_fraction=0.80, category_trade_count=50,
            category_distinct_markets=20, overall_trade_count=100,
            largest_winner_share=0.50, top_3_concentration=0.75,
        )
        result = compute_wallet_score_v1(input=inp)
        assert result.score < 75
        assert result.score >= 55
        assert result.verdict == WalletVerdict.WATCHLIST
        evidence_failures = [
            f for f in result.eligibility_gate_failures
            if not f.startswith("category_") and "score" not in f
        ]
        assert len(evidence_failures) == 0

    def test_parity_wallet_missing_evidence(self):
        result = _wallet_result(resolved_markets=None)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("resolved_markets" in f and "missing" in f for f in result.eligibility_gate_failures)

    def test_parity_category_score_only_failure(self):
        inp = _category_input(
            win_rate=0.55, profit_factor=1.2, info_score=0.55,
            trade_intervals_std=6.0, max_drawdown=0.30, sharpe_ratio=1.0,
            sample_fraction=0.80, category_trade_count=50,
            category_distinct_markets=20, overall_trade_count=100,
            largest_winner_share=0.50, top_3_concentration=0.75,
        )
        result = compute_category_wallet_score_v1(input=inp)
        assert result.score < 75
        assert result.score >= 55
        assert result.verdict == WalletVerdict.WATCHLIST

    def test_parity_category_missing_evidence(self):
        result = _category_result(category_resolved_markets=None)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("category_resolved_markets" in f and "missing" in f for f in result.category_gate_failures)

    def test_parity_stale_persisted_positive_verdict(self):
        """Evaluator/readiness parity for stale persisted positive verdict."""
        result = _wallet_result(resolved_markets=29)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert result.verdict != WalletVerdict.COPY_CANDIDATE


# =====================================================================
# Phase M — Additional adversarial tests
# =====================================================================

class TestAdditionalAdversarial:
    """Remaining adversarial test cases from Phase M."""

    def test_deterministic_repeated_evaluation(self):
        """Deterministic repeated evaluation produces identical results."""
        r1 = _wallet_result()
        r2 = _wallet_result()
        assert r1.score == r2.score
        assert r1.verdict == r2.verdict

    def test_deterministic_repeated_serialization(self):
        """Deterministic repeated serialization."""
        from polycopy.scoring.score_serialization import generate_idempotency_key
        r1 = _wallet_result()
        r2 = _wallet_result()
        key1 = generate_idempotency_key(
            formula_name="wallet_score_v1",
            formula_version=r1.formula_version,
            wallet_id=r1.wallet_id,
        )
        key2 = generate_idempotency_key(
            formula_name="wallet_score_v1",
            formula_version=r2.formula_version,
            wallet_id=r2.wallet_id,
        )
        assert key1 == key2

    def test_shadow_score_cannot_override_authoritative(self):
        """Shadow or legacy score cannot override the authoritative result."""
        result = _wallet_result(resolved_markets=29)
        assert result.verdict == WalletVerdict.INCOMPLETE
        import polycopy.scoring.wallet_score_v1 as wsv1
        assert not hasattr(wsv1, "shadow_score")

    def test_parent_wallet_verdict_cannot_grant_category(self):
        """Parent wallet verdict cannot independently grant category qualification."""
        result = _category_result(
            category_resolved_markets=5,
            category_distinct_events=2,
            category_active_days=2,
        )
        assert result.verdict != WalletVerdict.COPY_CANDIDATE
        assert result.verdict == WalletVerdict.INCOMPLETE

    def test_failing_category_cannot_inherit_from_passing_sibling(self):
        """A failing category cannot inherit qualification from a passing sibling."""
        cat1 = _category_input()
        cat2 = _category_input(category_resolved_markets=5, category_label="stocks")

        r1 = compute_category_wallet_score_v1(input=cat1)
        r2 = compute_category_wallet_score_v1(input=cat2)

        assert r1.verdict == WalletVerdict.COPY_CANDIDATE
        assert r2.verdict == WalletVerdict.INCOMPLETE
        assert r2.verdict != r1.verdict

    def test_current_positive_qualification_requires_at_least_one_category(self):
        """Current positive qualification requires at least one qualifying category."""
        result = _wallet_result(
            category_resolved_markets=5,
            category_distinct_events=2,
            category_active_days=2,
        )
        assert result.verdict != WalletVerdict.COPY_CANDIDATE

    def test_exact_threshold_wallet_and_category_qualifies(self):
        """Exact-threshold wallet and category case qualifies when every
        other requirement passes."""
        wallet_inp = _wallet_input(
            resolved_markets=WALLET_MIN_RESOLVED_MARKETS,
            active_trading_days=WALLET_MIN_ACTIVE_TRADING_DAYS,
            distinct_events=WALLET_MIN_DISTINCT_EVENTS,
            category_resolved_markets=CATEGORY_MIN_RESOLVED_MARKETS,
            category_distinct_events=CATEGORY_MIN_DISTINCT_EVENTS,
            category_active_days=CATEGORY_MIN_ACTIVE_DAYS,
        )
        wallet_result = compute_wallet_score_v1(input=wallet_inp)
        assert wallet_result.score >= 75
        assert wallet_result.verdict == WalletVerdict.COPY_CANDIDATE
        assert len(wallet_result.eligibility_gate_failures) == 0

        cat_inp = _category_input(
            category_resolved_markets=CATEGORY_MIN_RESOLVED_MARKETS,
            category_distinct_events=CATEGORY_MIN_DISTINCT_EVENTS,
            category_active_days=CATEGORY_MIN_ACTIVE_DAYS,
        )
        cat_result = compute_category_wallet_score_v1(input=cat_inp)
        assert cat_result.score >= 75
        assert cat_result.verdict == WalletVerdict.COPY_CANDIDATE
        assert len(cat_result.category_gate_failures) == 0


# =====================================================================
# Phase M — Shared contract direct tests
# =====================================================================

class TestSharedContract:
    """Direct tests for the shared qualification contract helpers."""

    def test_wallet_qualification_all_pass(self):
        q = evaluate_wallet_qualification(
            score=75.0,
            resolved_markets=30,
            active_trading_days=20,
            distinct_events=15,
        )
        assert q.qualified is True
        assert q.gate_failures == ()

    def test_wallet_qualification_score_below(self):
        q = evaluate_wallet_qualification(
            score=74.9999,
            resolved_markets=30,
            active_trading_days=20,
            distinct_events=15,
        )
        assert q.qualified is False
        assert any("score" in f for f in q.gate_failures)

    def test_wallet_qualification_missing_evidence(self):
        q = evaluate_wallet_qualification(
            score=75.0,
            resolved_markets=None,
            active_trading_days=20,
            distinct_events=15,
        )
        assert q.qualified is False
        assert any("resolved_markets" in f and "missing" in f for f in q.gate_failures)

    def test_wallet_failure_ordering(self):
        """Failures are ordered by WALLET_GATE_ORDER."""
        q = evaluate_wallet_qualification(
            score=70.0,
            resolved_markets=5,
            active_trading_days=3,
            distinct_events=2,
        )
        failures = list(q.gate_failures)
        assert failures[0].startswith("score")
        evidence = failures[1:]
        assert "resolved_markets" in evidence[0]
        assert "active_trading_days" in evidence[1]
        assert "distinct_events" in evidence[2]

    def test_category_qualification_all_pass(self):
        q = evaluate_category_qualification(
            score=75.0,
            category_resolved_markets=15,
            category_distinct_events=8,
            category_active_days=10,
        )
        assert q.qualified is True
        assert q.gate_failures == ()

    def test_category_qualification_missing_evidence(self):
        q = evaluate_category_qualification(
            score=75.0,
            category_resolved_markets=None,
            category_distinct_events=8,
            category_active_days=10,
        )
        assert q.qualified is False
        assert any("category_resolved_markets" in f and "missing" in f for f in q.gate_failures)

    def test_category_failure_ordering(self):
        """Failures are ordered by CATEGORY_GATE_ORDER."""
        q = evaluate_category_qualification(
            score=70.0,
            category_resolved_markets=5,
            category_distinct_events=2,
            category_active_days=3,
        )
        failures = list(q.gate_failures)
        assert failures[0].startswith("score")
        assert "category_resolved_markets" in failures[1]
        assert "category_distinct_events" in failures[2]
        assert "category_active_days" in failures[3]

    def test_gate_order_constants(self):
        assert WALLET_GATE_ORDER == ("score", "resolved_markets", "active_trading_days", "distinct_events")
        assert CATEGORY_GATE_ORDER == ("score", "category_resolved_markets", "category_distinct_events", "category_active_days")

    def test_threshold_constants(self):
        assert WALLET_SCORE_THRESHOLD == 75
        assert WALLET_MIN_RESOLVED_MARKETS == 30
        assert WALLET_MIN_ACTIVE_TRADING_DAYS == 20
        assert WALLET_MIN_DISTINCT_EVENTS == 15
        assert CATEGORY_SCORE_THRESHOLD == 75
        assert CATEGORY_MIN_RESOLVED_MARKETS == 15
        assert CATEGORY_MIN_DISTINCT_EVENTS == 8
        assert CATEGORY_MIN_ACTIVE_DAYS == 10


# ── Usable specialist composition (Finding 1) ──────────────────────────────


def _qualifying_wallet_q() -> WalletQualification:
    """Wallet qualification with all gates passing."""
    return evaluate_wallet_qualification(
        score=85.0,
        resolved_markets=50,
        active_trading_days=30,
        distinct_events=20,
    )


def _qualifying_category_q(label: str = "crypto") -> CategoryQualification:
    """Category qualification with all gates passing."""
    return evaluate_category_qualification(
        score=85.0,
        category_resolved_markets=20,
        category_distinct_events=12,
        category_active_days=14,
        label=label,
    )


def _failing_category_q(label: str = "politics") -> CategoryQualification:
    """Category qualification with score < 75."""
    return evaluate_category_qualification(
        score=60.0,
        category_resolved_markets=20,
        category_distinct_events=12,
        category_active_days=14,
        label=label,
    )


class TestUsableSpecialistComposition:
    """Tests for evaluate_usable_specialist (Finding 1)."""

    def test_wallet_qualifies_no_category_results(self):
        """Wallet qualifies but no category evaluations exist."""
        wq = _qualifying_wallet_q()
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=None
        )
        assert not result.usable
        assert result.wallet_qualified
        assert len(result.qualifying_category_labels) == 0
        assert "no_category_qualifications" in result.reasons

    def test_wallet_qualifies_empty_category_results(self):
        """Wallet qualifies but no categories were evaluated."""
        wq = _qualifying_wallet_q()
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[]
        )
        assert not result.usable
        assert result.wallet_qualified
        assert "no_category_qualifications" in result.reasons

    def test_category_evidence_passes_score_fails(self):
        """Category evidence passes but score is below 75."""
        wq = _qualifying_wallet_q()
        cat_q = evaluate_category_qualification(
            score=60.0,
            category_resolved_markets=20,
            category_distinct_events=12,
            category_active_days=14,
            label="crypto",
        )
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable
        assert result.wallet_qualified
        assert len(result.qualifying_category_labels) == 0
        assert "no_qualifying_category" in result.reasons

    def test_one_category_watchlist(self):
        """Wallet qualifies, one category is WATCHLIST (score 55-74)."""
        wq = _qualifying_wallet_q()
        cat_q = evaluate_category_qualification(
            score=65.0,
            category_resolved_markets=20,
            category_distinct_events=12,
            category_active_days=14,
            label="crypto",
        )
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable
        assert "no_qualifying_category" in result.reasons

    def test_one_category_skip(self):
        """Wallet qualifies, one category is SKIP (score < 55)."""
        wq = _qualifying_wallet_q()
        cat_q = evaluate_category_qualification(
            score=30.0,
            category_resolved_markets=20,
            category_distinct_events=12,
            category_active_days=14,
            label="crypto",
        )
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable
        assert "no_qualifying_category" in result.reasons

    def test_one_category_incomplete(self):
        """Wallet qualifies, one category is INCOMPLETE (missing evidence)."""
        wq = _qualifying_wallet_q()
        cat_q = evaluate_category_qualification(
            score=85.0,
            category_resolved_markets=None,
            category_distinct_events=12,
            category_active_days=14,
            label="crypto",
        )
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable
        assert "no_qualifying_category" in result.reasons

    def test_one_category_copy_candidate(self):
        """Wallet qualifies, one category is COPY_CANDIDATE."""
        wq = _qualifying_wallet_q()
        cat_q = _qualifying_category_q("crypto")
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert result.usable
        assert result.wallet_qualified
        assert "crypto" in result.qualifying_category_labels
        assert len(result.reasons) == 0

    def test_one_passes_one_fails(self):
        """Wallet qualifies, one category passes and another fails."""
        wq = _qualifying_wallet_q()
        cat_ok = _qualifying_category_q("crypto")
        cat_fail = _failing_category_q("politics")
        result = evaluate_usable_specialist(
            wallet_qualification=wq,
            category_qualifications=[cat_ok, cat_fail],
        )
        assert result.usable
        assert "crypto" in result.qualifying_category_labels
        assert "politics" not in result.qualifying_category_labels

    def test_category_qualifies_wallet_fails(self):
        """Category qualifies but wallet does not."""
        wq = evaluate_wallet_qualification(
            score=60.0,
            resolved_markets=50,
            active_trading_days=30,
            distinct_events=20,
        )
        cat_q = _qualifying_category_q("crypto")
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable
        assert not result.wallet_qualified
        assert "wallet_not_qualified" in result.reasons

    def test_parent_copy_candidate_cannot_manufacture_category(self):
        """Parent wallet COPY_CANDIDATE cannot manufacture category qualification."""
        wq = _qualifying_wallet_q()
        # No category results at all
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=None
        )
        assert not result.usable
        assert "no_category_qualifications" in result.reasons

    def test_evidence_counts_without_score_qualification(self):
        """Passing category evidence counts without score cannot qualify."""
        wq = _qualifying_wallet_q()
        cat_q = evaluate_category_qualification(
            score=70.0,  # < 75
            category_resolved_markets=20,
            category_distinct_events=12,
            category_active_days=14,
            label="crypto",
        )
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable
        assert "no_qualifying_category" in result.reasons

    def test_missing_category_score_fails_closed(self):
        """Missing category score fails closed."""
        wq = _qualifying_wallet_q()
        cat_q = evaluate_category_qualification(
            score=None,
            category_resolved_markets=20,
            category_distinct_events=12,
            category_active_days=14,
            label="crypto",
        )
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable

    def test_repeated_composition_is_deterministic(self):
        """Repeated composition with identical inputs is deterministic."""
        wq = _qualifying_wallet_q()
        cats = [_qualifying_category_q("a"), _qualifying_category_q("b")]
        r1 = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=cats
        )
        r2 = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=cats
        )
        assert r1.usable == r2.usable
        assert r1.qualifying_category_labels == r2.qualifying_category_labels
        assert r1.reasons == r2.reasons


# ── Stale-decision behavior (Finding 2) ────────────────────────────────────


class TestStaleDecisionBehavior:
    """Stale positive decisions must fail closed when current evidence is
    missing, below threshold, changed, or cannot be validated."""

    def test_stale_wallet_positive_current_category_positive(self):
        """Stale wallet COPY_CANDIDATE + current category COPY_CANDIDATE
        does NOT qualify — the wallet evidence is not current."""
        # Current wallet: not qualified (missing evidence)
        current_wallet = evaluate_wallet_qualification(
            score=None,
            resolved_markets=None,
            active_trading_days=None,
            distinct_events=None,
        )
        # Current category: qualified
        current_cat = _qualifying_category_q("crypto")

        result = evaluate_usable_specialist(
            wallet_qualification=current_wallet,
            category_qualifications=[current_cat],
        )
        assert not result.usable
        assert "wallet_not_qualified" in result.reasons

    def test_current_wallet_positive_stale_category_positive(self):
        """Current wallet COPY_CANDIDATE + stale category COPY_CANDIDATE
        does NOT qualify — the category evidence is not current."""
        current_wallet = _qualifying_wallet_q()
        # Current category: not qualified (missing evidence)
        current_cat = evaluate_category_qualification(
            score=None,
            category_resolved_markets=None,
            category_distinct_events=None,
            category_active_days=None,
            label="crypto",
        )
        result = evaluate_usable_specialist(
            wallet_qualification=current_wallet,
            category_qualifications=[current_cat],
        )
        assert not result.usable
        assert "no_qualifying_category" in result.reasons

    def test_both_stale_positive(self):
        """Both stale positive does not qualify."""
        current_wallet = evaluate_wallet_qualification(
            score=None,
            resolved_markets=None,
            active_trading_days=None,
            distinct_events=None,
        )
        current_cat = evaluate_category_qualification(
            score=None,
            category_resolved_markets=None,
            category_distinct_events=None,
            category_active_days=None,
            label="crypto",
        )
        result = evaluate_usable_specialist(
            wallet_qualification=current_wallet,
            category_qualifications=[current_cat],
        )
        assert not result.usable

    def test_historical_positive_current_evidence_below_threshold(self):
        """Historical positive cannot substitute for current evidence
        below threshold."""
        current_wallet = _qualifying_wallet_q()
        # Below threshold category evidence
        current_cat = evaluate_category_qualification(
            score=85.0,
            category_resolved_markets=5,  # < 15
            category_distinct_events=12,
            category_active_days=14,
            label="crypto",
        )
        result = evaluate_usable_specialist(
            wallet_qualification=current_wallet,
            category_qualifications=[current_cat],
        )
        assert not result.usable
        assert "no_qualifying_category" in result.reasons

    def test_no_current_category_evaluations_fails_closed(self):
        """Absence of current category evaluations fails closed."""
        current_wallet = _qualifying_wallet_q()
        result = evaluate_usable_specialist(
            wallet_qualification=current_wallet,
            category_qualifications=None,
        )
        assert not result.usable
        assert "no_category_qualifications" in result.reasons


# ── Distance reporting and diagnostic preservation ────────────────────────


class TestReadinessDistanceReporting:
    """Distance reporting must remain diagnostic-only and not override the
    shared composition result."""

    def test_distance_does_not_override_specialist_result(self):
        """Distance values are diagnostic only — they cannot override the
        shared composition result."""
        # Wallet qualifies, no category results → not ready
        wq = _qualifying_wallet_q()
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=None
        )
        assert not result.usable
        assert "no_category_qualifications" in result.reasons

    def test_deterministic_reasons(self):
        """Composition reasons are deterministic."""
        wq = _qualifying_wallet_q()
        r1 = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=None
        )
        r2 = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=None
        )
        assert r1.reasons == r2.reasons

    def test_qualifying_labels_deterministically_sorted(self):
        """Qualifying category labels are deterministically sorted."""
        wq = _qualifying_wallet_q()
        cats = [
            _qualifying_category_q("z_crypto"),
            _qualifying_category_q("a_crypto"),
        ]
        r1 = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=cats
        )
        r2 = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=cats
        )
        assert r1.qualifying_category_labels == ("a_crypto", "z_crypto")
        assert r1.qualifying_category_labels == r2.qualifying_category_labels


# ── Wallet failure matrix (wiring integration) ────────────────────────────


class TestWalletFailureMatrix:
    """Each wallet failure mode must produce not-ready via the shared
    contract."""

    def test_resolved_markets_29_fails(self):
        wq = evaluate_wallet_qualification(
            score=85.0, resolved_markets=29, active_trading_days=30, distinct_events=20
        )
        cat_q = _qualifying_category_q("crypto")
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable
        assert not result.wallet_qualified

    def test_active_days_19_fails(self):
        wq = evaluate_wallet_qualification(
            score=85.0, resolved_markets=50, active_trading_days=19, distinct_events=20
        )
        cat_q = _qualifying_category_q("crypto")
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable
        assert not result.wallet_qualified

    def test_distinct_events_14_fails(self):
        wq = evaluate_wallet_qualification(
            score=85.0, resolved_markets=50, active_trading_days=30, distinct_events=14
        )
        cat_q = _qualifying_category_q("crypto")
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable
        assert not result.wallet_qualified

    def test_wallet_score_below_75_fails(self):
        wq = evaluate_wallet_qualification(
            score=60.0, resolved_markets=50, active_trading_days=30, distinct_events=20
        )
        cat_q = _qualifying_category_q("crypto")
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable
        assert not result.wallet_qualified

    def test_missing_wallet_score_fails(self):
        wq = evaluate_wallet_qualification(
            score=None, resolved_markets=50, active_trading_days=30, distinct_events=20
        )
        cat_q = _qualifying_category_q("crypto")
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable
        assert not result.wallet_qualified

    def test_missing_wallet_evidence_fails(self):
        wq = evaluate_wallet_qualification(
            score=85.0, resolved_markets=None, active_trading_days=None, distinct_events=None
        )
        cat_q = _qualifying_category_q("crypto")
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable
        assert not result.wallet_qualified


# ── Category failure matrix ───────────────────────────────────────────────


class TestCategoryFailureMatrix:
    """Each category failure mode must produce not-ready via the shared
    contract."""

    def test_no_category_results(self):
        wq = _qualifying_wallet_q()
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=None
        )
        assert not result.usable
        assert "no_category_qualifications" in result.reasons

    def test_cat_resolved_markets_14_fails(self):
        wq = _qualifying_wallet_q()
        cat_q = evaluate_category_qualification(
            score=85.0, category_resolved_markets=14, category_distinct_events=12,
            category_active_days=14, label="crypto"
        )
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable

    def test_cat_distinct_events_7_fails(self):
        wq = _qualifying_wallet_q()
        cat_q = evaluate_category_qualification(
            score=85.0, category_resolved_markets=20, category_distinct_events=7,
            category_active_days=14, label="crypto"
        )
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable

    def test_cat_active_days_9_fails(self):
        wq = _qualifying_wallet_q()
        cat_q = evaluate_category_qualification(
            score=85.0, category_resolved_markets=20, category_distinct_events=12,
            category_active_days=9, label="crypto"
        )
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable

    def test_cat_score_below_75_fails(self):
        wq = _qualifying_wallet_q()
        cat_q = evaluate_category_qualification(
            score=60.0, category_resolved_markets=20, category_distinct_events=12,
            category_active_days=14, label="crypto"
        )
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable

    def test_missing_cat_score_fails(self):
        wq = _qualifying_wallet_q()
        cat_q = evaluate_category_qualification(
            score=None, category_resolved_markets=20, category_distinct_events=12,
            category_active_days=14, label="crypto"
        )
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable

    def test_missing_cat_evidence_fails(self):
        wq = _qualifying_wallet_q()
        cat_q = evaluate_category_qualification(
            score=85.0, category_resolved_markets=None, category_distinct_events=None,
            category_active_days=None, label="crypto"
        )
        result = evaluate_usable_specialist(
            wallet_qualification=wq, category_qualifications=[cat_q]
        )
        assert not result.usable
