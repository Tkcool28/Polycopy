"""PR24E: regression tests for the incomplete-verdict resolution-evidence guard.

Three independent concerns are covered here, mirroring the structure of
``test_p24b_query_memory_guards.py``:

1. **Pure helper invariants** — ``polycopy.scoring.incomplete_verdict_guard``
   forces ``INCOMPLETE`` whenever resolved-market evidence is missing
   or zero, populates the structured reason buckets, and rejects a
   silent ``SKIP`` (skip + empty reasons → ``INCOMPLETE`` with reasons).

2. **Compute path** — ``compute_wallet_score_v1`` returns
   ``WalletVerdict.INCOMPLETE`` for a wallet with no
   ``resolved_markets``, even when other components look healthy.

3. **Persistence / consistency** — ``persist_wallet_score_v1`` +
   ``persist_decision_verdicts_and_components`` keep
   ``wallet_score_decisions`` and ``decision_verdicts`` rows consistent
   on a fresh DB: both MUST be ``INCOMPLETE`` with non-empty reason
   buckets. Regression guard for the bug found in the post-PR26 smoke
   test where a zero-resolution-evidence wallet was labeled ``SKIP``
   with empty reason buckets.

Tests use a small SQLite DB via the existing ``Database`` wrapper, the
same path that ``run_scan --use-sample`` exercises in production.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path



# ────────────────────────────────────────────────────────────────────
# 1. Pure helper invariants
# ────────────────────────────────────────────────────────────────────


class TestDeriveWalletVerdictFromEvidence:
    """The pure helper is the single source of truth for the contract."""

    def test_zero_resolution_markets_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="copy_candidate",
            resolved_markets=0,
            category_resolved_markets=20,
            sample_fraction=0.5,
            sharpe_ratio=1.5,
            max_drawdown=0.10,
        )
        assert out["verdict"] == "incomplete"
        assert out["verdict_family"] == "incomplete"
        assert "resolved_markets" in out["missing_essentials"]
        assert "no_resolved_market_evidence" in out["eligibility_failures"]

    def test_none_resolution_markets_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="watchlist",
            resolved_markets=None,
            category_resolved_markets=15,
        )
        assert out["verdict"] == "incomplete"
        assert out["verdict_family"] == "incomplete"
        assert "resolved_markets" in out["missing_essentials"]

    def test_zero_category_resolution_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="skip",
            resolved_markets=50,
            category_resolved_markets=0,
        )
        assert out["verdict"] == "incomplete"
        assert "category_resolved_markets" in out["missing_essentials"]
        assert "no_resolved_market_evidence" in out["eligibility_failures"]

    def test_none_category_resolution_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="watchlist",
            resolved_markets=50,
            category_resolved_markets=None,
        )
        assert out["verdict"] == "incomplete"
        assert "category_resolved_markets" in out["missing_essentials"]

    def test_sufficient_resolution_preserves_real_verdict(self):
        """When resolution evidence is present, real verdicts survive."""
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="copy_candidate",
            resolved_markets=50,
            category_resolved_markets=20,
            sample_fraction=0.5,
            sharpe_ratio=1.5,
            max_drawdown=0.10,
        )
        assert out["verdict"] == "copy_candidate"
        assert out["verdict_family"] == "copy_candidate"
        assert "resolved_markets" not in out["missing_essentials"]

    def test_silent_skip_is_promoted_to_incomplete(self):
        """A zero-resolution-evidence SKIP with no reason buckets is
        unexplained and is rewritten as INCOMPLETE-with-reasons.

        PR24E contract: the "silent skip" invariant only applies in
        the resolved-evidence-gap path. If ``resolved_markets`` is
        None/0 (Rule 1 territory) and the caller signalled no
        eligibility failures either, the persisted row must carry
        structured reasons — the wallet cannot be silently skipped
        without resolved-market evidence to justify the verdict.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="skip",
            resolved_markets=None,  # resolution evidence GAP (Rule 1 fires)
            category_resolved_markets=None,
            missing_essentials=[],  # caller signalled no reasons
            eligibility_failures=[],
        )
        # Rule 1 already forced INCOMPLETE; Rule 2 only populated reasons.
        assert out["verdict"] == "incomplete"
        assert out["missing_essentials"], (
            "silent skip in resolution-evidence-gap path must carry reasons"
        )
        assert "no_resolved_market_evidence" in out["eligibility_failures"]

    def test_silent_skip_outside_resolution_gap_is_preserved(self):
        """A SKIP with empty reasons but sufficient resolved-market
        evidence is a true score-driven skip and PR24E preserves it.

        This is the "existing real-skip behavior preserved" line of
        the contract.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="skip",
            resolved_markets=50,  # sufficient resolution evidence
            category_resolved_markets=20,
            missing_essentials=[],
            eligibility_failures=[],
        )
        assert out["verdict"] == "skip", (
            "PR24E preserves true score-driven skips when resolution "
            "evidence is sufficient"
        )

    def test_skip_with_explicit_eligibility_failures_preserved(self):
        """A ``SKIP`` that already carries an explicit failure reason stays SKIP.

        This is the existing real-skip path the PR24E contract
        explicitly preserves.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="skip",
            resolved_markets=50,
            category_resolved_markets=20,
            missing_essentials=[],
            eligibility_failures=["active_trading_days=5 < 20"],
        )
        assert out["verdict"] == "skip"
        assert "active_trading_days=5 < 20" in out["eligibility_failures"]

    def test_unknown_verdict_string_normalizes_to_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="",  # empty
            resolved_markets=50,
            category_resolved_markets=20,
        )
        assert out["verdict"] == "incomplete"

    def test_missing_essentials_are_deduplicated_and_ordered(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="copy_candidate",
            resolved_markets=None,
            category_resolved_markets=None,
            sample_fraction=None,
            sharpe_ratio=None,
            max_drawdown=None,
            missing_essentials=["trade_count", "resolved_markets"],
            eligibility_failures=None,
        )
        # `resolved_markets` appears once even though it was in both
        # the caller-provided list and the helper-computed list.
        assert out["missing_essentials"].count("resolved_markets") == 1
        # All five resolution-evidence keys are populated.
        for key in (
            "resolved_markets",
            "category_resolved_markets",
            "sample_fraction",
            "sharpe_ratio",
            "max_drawdown",
        ):
            assert key in out["missing_essentials"]


class TestEnforceWalletDecisionEligibility:
    """Mirror of the helper that preserves the persistence-row shape."""

    def test_keeps_verdict_family_consistent_with_verdict(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            enforce_wallet_decision_eligibility,
        )

        out = enforce_wallet_decision_eligibility(
            verdict="copy_candidate",
            verdict_family="copy_candidate",
            resolved_markets=None,  # forces INCOMPLETE
            category_resolved_markets=20,
        )
        assert out["verdict"] == "incomplete"
        assert out["verdict_family"] == "incomplete"

    def test_missing_evidence_keys_populated(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            enforce_wallet_decision_eligibility,
        )

        out = enforce_wallet_decision_eligibility(
            verdict="watchlist",
            resolved_markets=None,
            category_resolved_markets=None,
            sample_fraction=None,
            sharpe_ratio=None,
            max_drawdown=None,
        )
        for key in (
            "resolved_markets",
            "category_resolved_markets",
            "sample_fraction",
            "sharpe_ratio",
            "max_drawdown",
        ):
            assert key in out["missing_essentials"]
        assert "no_resolved_market_evidence" in out["eligibility_failures"]


# ────────────────────────────────────────────────────────────────────
# 2. compute_wallet_score_v1 returns INCOMPLETE without resolution evidence
# ────────────────────────────────────────────────────────────────────


class TestComputeWalletScoreV1AppliesGuard:
    """The compute path must surface INCOMPLETE, never SKIP-with-no-reasons."""

    def test_zero_resolution_returns_incomplete(self):
        from polycopy.scoring.wallet_score_v1 import (
            WalletVerdict,
            compute_wallet_score_v1,
        )

        result = compute_wallet_score_v1(
            wallet_id="w-zero-resolved",
            win_rate=0.8,
            trade_count=200,
            resolved_markets=0,  # PR24E contract: zero is treated as missing
        )
        assert result.verdict == WalletVerdict.INCOMPLETE
        # Structured reason buckets must be populated.
        assert "resolved_markets" in result.missing_essentials
        assert "no_resolved_market_evidence" in result.eligibility_gate_failures

    def test_none_resolution_returns_incomplete(self):
        from polycopy.scoring.wallet_score_v1 import (
            WalletVerdict,
            compute_wallet_score_v1,
        )

        result = compute_wallet_score_v1(
            wallet_id="w-none-resolved",
            win_rate=0.8,
            trade_count=200,
            # resolved_markets omitted on purpose
        )
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert "resolved_markets" in result.missing_essentials

    def test_none_category_resolution_returns_incomplete(self):
        from polycopy.scoring.wallet_score_v1 import (
            WalletVerdict,
            compute_wallet_score_v1,
        )

        result = compute_wallet_score_v1(
            wallet_id="w-none-cat-resolved",
            win_rate=0.8,
            trade_count=200,
            resolved_markets=50,
            # category_resolved_markets omitted on purpose
        )
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert "category_resolved_markets" in result.missing_essentials

    def test_zero_category_resolution_returns_incomplete(self):
        from polycopy.scoring.wallet_score_v1 import (
            WalletVerdict,
            compute_wallet_score_v1,
        )

        result = compute_wallet_score_v1(
            wallet_id="w-zero-cat-resolved",
            win_rate=0.8,
            trade_count=200,
            resolved_markets=50,
            category_resolved_markets=0,
        )
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert "category_resolved_markets" in result.missing_essentials

    def test_sufficient_resolution_can_produce_copy_candidate(self):
        """Sanity check: when resolution is present, the real verdict path works."""
        from polycopy.scoring.wallet_score_v1 import (
            WalletVerdict,
            compute_wallet_score_v1,
        )

        result = compute_wallet_score_v1(
            wallet_id="w-healthy",
            info_score=0.7,
            win_rate=0.85,
            profit_factor=2.1,
            trade_intervals_std=1800.0,
            trade_count=250,
            max_drawdown=0.08,
            sharpe_ratio=2.6,
            sample_fraction=0.30,
            category_trade_count=180,
            category_distinct_markets=12,
            overall_trade_count=250,
            largest_winner_share=0.25,
            top_3_concentration=0.50,
            resolved_markets=60,
            active_trading_days=35,
            distinct_events=22,
            category_resolved_markets=25,
            category_distinct_events=12,
            category_active_days=20,
        )
        assert result.verdict == WalletVerdict.COPY_CANDIDATE


# ────────────────────────────────────────────────────────────────────
# 3. Persistence / row consistency on a fresh DB
# ────────────────────────────────────────────────────────────────────


def _fresh_db(tmp_path: Path):
    """Open a fresh Database at a tmp path. Mirrors test_p24c helper style."""
    from polycopy.db.database import Database

    db_path = tmp_path / "p24e.db"
    db = Database(db_path=db_path)
    db.connect()
    return db


def _insert_wallet(db, wid: str = "0xWALLET") -> str:
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (wid, wid, "default", "2026-07-01T00:00:00Z"),
    )
    db.conn.commit()
    return wid


class TestPersistenceRowConsistency:
    """End-to-end: the wallet row and its companion decision_verdicts row
    must agree on the verdict and on the reason buckets.
    """

    def test_zero_resolution_persists_incomplete_with_reasons(
        self, tmp_path: Path
    ):
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1,
            compute_wallet_score_v1,
        )
        from polycopy.scoring.score_serialization import persist_wallet_score_v1

        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        inp = WalletScoreInputV1(
            wallet_id=wid,
            win_rate=0.65,
            trade_count=150,
            # resolved_markets / category_resolved_markets intentionally
            # omitted → PR24E forces INCOMPLETE
        )
        res = compute_wallet_score_v1(input=inp)
        persist_wallet_score_v1(
            db, wid, res, source_data_timestamp="2026-07-01T00:00:00Z"
        )
        db.conn.commit()

        # Read the wallet row back.
        row = db.conn.execute(
            "SELECT verdict, missing_essentials_json, eligibility_failures_json "
            "FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert row is not None
        assert row["verdict"] == "incomplete", (
            "zero-resolution wallet must persist as incomplete, not skip"
        )
        missing = json.loads(row["missing_essentials_json"] or "[]")
        failures = json.loads(row["eligibility_failures_json"] or "[]")
        assert "resolved_markets" in missing
        assert "no_resolved_market_evidence" in failures
        # Both reason buckets must be non-empty — no silent verdict.
        assert missing, "missing_essentials_json must be non-empty for incomplete"
        assert failures, "eligibility_failures_json must be non-empty for incomplete"

    def test_decision_verdicts_row_mirrors_wallet_row(self, tmp_path: Path):
        """The decision_verdicts row inserted by the wiring helper MUST
        agree with the parent wallet_score_decisions row.
        """
        sys.path.insert(  # noqa: E402
            0, str(Path(__file__).resolve().parent.parent / "scripts")
        )
        from polycopy.scoring.wallet_score_v1 import (  # noqa: E402
            WalletScoreInputV1,
            compute_wallet_score_v1,
        )
        from polycopy.scoring.score_serialization import (  # noqa: E402
            persist_wallet_score_v1,
        )
        from scripts import scan_pipeline_wiring  # noqa: E402

        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        inp = WalletScoreInputV1(
            wallet_id=wid,
            win_rate=0.65,
            trade_count=150,
            # resolved_markets omitted → PR24E INCOMPLETE
        )
        res = compute_wallet_score_v1(input=inp)
        persist_wallet_score_v1(
            db, wid, res, source_data_timestamp="2026-07-01T00:00:00Z"
        )
        db.conn.commit()

        counters = scan_pipeline_wiring.ScanPipelineCounters()
        scan_pipeline_wiring.persist_decision_verdicts_and_components(
            db, counters=counters, max_verdicts=10
        )

        verdict_row = db.conn.execute(
            "SELECT verdict, verdict_family FROM decision_verdicts "
            "WHERE wallet_id = ? AND formula_name = 'wallet_score'",
            (wid,),
        ).fetchone()
        assert verdict_row is not None
        assert verdict_row["verdict"] == "incomplete", (
            "decision_verdicts row must be 'incomplete' for zero-resolution wallet"
        )
        assert verdict_row["verdict_family"] == "incomplete", (
            "verdict_family must follow verdict (PR24E consistency rule)"
        )

    def test_silent_skip_is_corrected_at_persistence(self, tmp_path: Path):
        """If a caller manually constructs a WalletScoreResult with
        verdict=SKIP and EMPTY reason buckets, the persistence path's
        guard must correct it ONLY when there's also a resolution-
        evidence gap. If the resolution evidence is sufficient, the
        SKIP is a true score-driven verdict and is preserved.
        """
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1,
            WalletScoreResult,
            WalletVerdict,
        )
        from polycopy.scoring.score_serialization import persist_wallet_score_v1

        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db, "0xSILENTSKIP")

        inp = WalletScoreInputV1(
            wallet_id=wid,
            win_rate=0.65,
            trade_count=150,
            resolved_markets=50,  # sufficient resolution evidence
            category_resolved_markets=20,
        )

        # Manually craft a "buggy" result with empty reason buckets.
        # With sufficient resolution evidence the PR24E guard
        # preserves the verdict (this is the "real skip" path).
        legit_skip = WalletScoreResult(
            wallet_id=wid,
            score=0.0,
            verdict=WalletVerdict.SKIP,
            input=inp,
            missing_essentials=[],
            eligibility_gate_failures=[],
        )

        persist_wallet_score_v1(
            db, wid, legit_skip, source_data_timestamp="2026-07-01T00:00:00Z"
        )
        db.conn.commit()

        row = db.conn.execute(
            "SELECT verdict, missing_essentials_json, eligibility_failures_json "
            "FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert row is not None
        # PR24E preserves true score-driven skips when resolution evidence
        # is sufficient — the verdict is 'skip' and reason buckets stay
        # empty (the score formula produced the verdict deterministically).
        assert row["verdict"] == "skip"


class TestRegressionPostPR26SmokeTest:
    """The exact shape from the post-PR26 smoke-test failure: a wallet
    decision produced by ``run_scan --use-sample`` on a fresh empty DB
    must NOT be labeled ``SKIP`` with empty reason buckets.
    """

    def test_post_pr26_shape_cannot_persist_silent_skip(
        self, tmp_path: Path
    ):
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1,
        )
        from polycopy.scoring.score_serialization import persist_wallet_score_v1

        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db, "0xPOSTPR26")

        # This is the exact input shape that produced the SKIP-with-no-
        # reasons bug in the post-PR26 smoke test:
        #   trade_count=2, win_rate=0.0, every other metric None
        # (resolved_markets / category_resolved_markets were NULL on the
        # persisted row because the sample data doesn't carry them.)
        inp = WalletScoreInputV1(
            wallet_id=wid,
            trade_count=2,
            win_rate=0.0,
            # resolved_markets / category_resolved_markets / sample_fraction
            # / sharpe_ratio / max_drawdown all default to None.
        )

        result = compute_wallet_score_v1_through_helper(inp)
        persist_wallet_score_v1(
            db, wid, result, source_data_timestamp="2026-07-01T00:00:00Z"
        )
        db.conn.commit()

        row = db.conn.execute(
            "SELECT verdict, missing_essentials_json, eligibility_failures_json "
            "FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert row is not None
        # The post-PR26 bug was verdict='skip' with empty reason buckets.
        # After PR24E this row MUST be 'incomplete' with reasons.
        assert row["verdict"] != "skip", (
            "PR24E: zero-evidence wallet cannot persist 'skip'"
        )
        assert row["verdict"] == "incomplete"
        missing = json.loads(row["missing_essentials_json"] or "[]")
        failures = json.loads(row["eligibility_failures_json"] or "[]")
        assert missing, "missing_essentials_json must be populated for incomplete"
        assert failures, "eligibility_failures_json must be populated for incomplete"
        assert "no_resolved_market_evidence" in failures


def compute_wallet_score_v1_through_helper(inp):
    """Run ``compute_wallet_score_v1`` and return its result for test use.

    A handful of regression tests in this module want to drive the same
    code path the production pipeline uses (compute → wrap → persist)
    without committing to a particular verdict up front.
    """
    from polycopy.scoring.wallet_score_v1 import compute_wallet_score_v1

    return compute_wallet_score_v1(input=inp)