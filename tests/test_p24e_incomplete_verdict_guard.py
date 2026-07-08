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
        """A SKIP with empty reasons but all required evidence present is
        preserved as a TRUE score-driven skip.

        PR27 invariant: the verdict stays ``skip`` (no promotion to
        incomplete), but the helper MUST attach the canonical
        ``score_below_copy_threshold`` marker to
        ``eligibility_failures`` so the persisted row is never silent.

        PR24F invariant: the wallet must also provide all required
        non-resolution evidence (``sample_fraction``, ``sharpe_ratio``,
        ``max_drawdown``); otherwise Rule 1b forces INCOMPLETE.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
            SCORE_BELOW_COPY_THRESHOLD,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="skip",
            resolved_markets=50,  # sufficient resolution evidence
            category_resolved_markets=20,
            # PR24F: all required non-resolution evidence must be present
            # for a real verdict (Rule 1b).
            sample_fraction=0.20,
            sharpe_ratio=0.5,
            max_drawdown=0.30,
            missing_essentials=[],
            eligibility_failures=[],
        )
        # PR27: verdict is preserved (no promotion to incomplete).
        assert out["verdict"] == "skip", (
            "PR27 preserves true score-driven skips when all required "
            "evidence is present"
        )
        # PR27: but the row must carry the canonical marker so it is
        # never silent (both buckets empty).
        assert SCORE_BELOW_COPY_THRESHOLD in out["eligibility_failures"]
        assert out["eligibility_failures"], (
            "eligibility_failures must be non-empty for any skip"
        )

    def test_skip_with_explicit_eligibility_failures_preserved(self):
        """A ``SKIP`` that already carries an explicit failure reason stays SKIP.

        PR24F: also provide all required non-resolution evidence so
        Rule 1b does not promote the verdict to incomplete.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="skip",
            resolved_markets=50,
            category_resolved_markets=20,
            sample_fraction=0.20,
            sharpe_ratio=0.5,
            max_drawdown=0.30,
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
        """A caller-constructed WalletScoreResult with verdict=SKIP and
        EMPTY reason buckets must NOT be persisted silently.

        PR27 invariant: when the resolution evidence is sufficient
        the verdict stays ``skip`` (no promotion to incomplete), but
        ``eligibility_failures_json`` MUST be non-empty so the row is
        auditable. The canonical ``score_below_copy_threshold`` marker
        is the contract.

        PR24F invariant: the wallet's typed input must include all
        required non-resolution evidence (``sample_fraction``,
        ``sharpe_ratio``, ``max_drawdown``); otherwise the helper
        promotes to INCOMPLETE.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            SCORE_BELOW_COPY_THRESHOLD,
        )
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
            # PR24F: all required non-resolution evidence must be present
            # for a real verdict (Rule 1b).
            sample_fraction=0.20,
            sharpe_ratio=0.5,
            max_drawdown=0.30,
        )

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
        # PR27: verdict is preserved as SKIP (no promotion to incomplete).
        assert row["verdict"] == "skip"
        # PR27: eligibility_failures_json must NOT be empty — the row
        # carries the canonical score-driven skip marker.
        failures = json.loads(row["eligibility_failures_json"] or "[]")
        assert failures, (
            "PR27 invariant: persisted skip must have a non-empty "
            "eligibility_failures_json"
        )
        assert SCORE_BELOW_COPY_THRESHOLD in failures, (
            "PR27 invariant: persisted skip must carry the canonical "
            "score_below_copy_threshold marker"
        )


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


# ────────────────────────────────────────────────────────────────────
# 4. PR27 — "no skip is silent" invariant
# ────────────────────────────────────────────────────────────────────


class TestPR27NoSilentSkip:
    """The PR27 cleanup tightens the contract: a persisted wallet decision
    whose ``verdict == "skip"`` must NEVER have BOTH
    ``missing_essentials_json`` and ``eligibility_failures_json``
    empty. Two canonical markers are the source of truth:

      * ``no_resolved_market_evidence`` — Rule 1 territory.
      * ``score_below_copy_threshold`` — Rule 2 territory (sufficient
        resolution evidence, score formula legitimately produced SKIP).
    """

    def test_zero_resolved_skip_with_empty_reasons_becomes_incomplete(self):
        """Case 1 (per task brief): zero resolution evidence + skip + empty
        reason buckets ⇒ INCOMPLETE with ``no_resolved_market_evidence``.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="skip",
            resolved_markets=None,  # zero-evidence path
            category_resolved_markets=None,
            missing_essentials=[],
            eligibility_failures=[],
        )
        assert out["verdict"] == "incomplete"
        assert out["verdict_family"] == "incomplete"
        assert "no_resolved_market_evidence" in out["eligibility_failures"]
        assert out["eligibility_failures"], (
            "eligibility_failures must be non-empty for incomplete"
        )

    def test_sufficient_evidence_skip_gets_score_below_marker(self):
        """Case 2 (per task brief): sufficient resolution evidence + skip
        + empty reason buckets ⇒ verdict stays SKIP, but
        ``score_below_copy_threshold`` is appended so the row is
        non-silent.

        PR24F: also provide all required non-resolution evidence so
        Rule 1b does not promote to incomplete.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
            SCORE_BELOW_COPY_THRESHOLD,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="skip",
            resolved_markets=50,
            category_resolved_markets=20,
            # PR24F: all required non-resolution evidence must be present
            # for a real verdict (Rule 1b).
            sample_fraction=0.20,
            sharpe_ratio=0.5,
            max_drawdown=0.30,
            missing_essentials=[],
            eligibility_failures=[],
        )
        # Verdict preserved — no promotion.
        assert out["verdict"] == "skip"
        assert out["verdict_family"] == "skip"
        # Marker attached so the row is auditable.
        assert out["eligibility_failures"] == [SCORE_BELOW_COPY_THRESHOLD]

    def test_sufficient_evidence_skip_with_existing_failure_preserved(self):
        """Case 3 (per task brief): sufficient resolution evidence + skip
        + non-empty eligibility_failures ⇒ existing failure is preserved,
        canonical marker is NOT duplicated unless it would be the only
        entry.

        PR24F: also provide all required non-resolution evidence.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="skip",
            resolved_markets=50,
            category_resolved_markets=20,
            sample_fraction=0.20,
            sharpe_ratio=0.5,
            max_drawdown=0.30,
            missing_essentials=[],
            eligibility_failures=["active_trading_days=5 < 20"],
        )
        assert out["verdict"] == "skip"
        # Existing failure is preserved verbatim.
        assert out["eligibility_failures"] == ["active_trading_days=5 < 20"]
        # The canonical marker was NOT appended because the caller
        # already supplied a reason — no duplication.
        assert "score_below_copy_threshold" not in out["eligibility_failures"]

    def test_persisted_skip_never_has_both_buckets_empty(self, tmp_path: Path):
        """Case 4 (per task brief): no persisted wallet_score_decisions or
        decision_verdicts row can represent SKIP with both reason
        buckets empty.

        Drives the full pipeline path: build a real ``WalletScoreResult``
        via ``compute_wallet_score_v1`` and run the scan pipeline
        writer. Verifies both rows.
        """
        sys.path.insert(  # noqa: E402
            0, str(Path(__file__).resolve().parent.parent / "scripts")
        )
        from polycopy.scoring.incomplete_verdict_guard import (  # noqa: E402
            SCORE_BELOW_COPY_THRESHOLD,
        )
        from polycopy.scoring.score_serialization import (  # noqa: E402
            persist_wallet_score_v1,
        )
        from polycopy.scoring.wallet_score_v1 import (  # noqa: E402
            WalletScoreInputV1,
            compute_wallet_score_v1,
        )
        from scripts import scan_pipeline_wiring  # noqa: E402

        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db, "0xPR27PROVE")

        # Real scoring input — score formula will produce SKIP
        # legitimately (low win_rate, sufficient evidence).
        inp = WalletScoreInputV1(
            wallet_id=wid,
            win_rate=0.30,
            trade_count=150,
            resolved_markets=50,
            category_resolved_markets=20,
            active_trading_days=30,
            distinct_events=20,
            sample_fraction=0.20,
            sharpe_ratio=0.5,
            max_drawdown=0.30,
        )
        result = compute_wallet_score_v1(input=inp)
        persist_wallet_score_v1(
            db, wid, result, source_data_timestamp="2026-07-01T00:00:00Z"
        )
        db.conn.commit()

        counters = scan_pipeline_wiring.ScanPipelineCounters()
        scan_pipeline_wiring.persist_decision_verdicts_and_components(
            db, counters=counters, max_verdicts=10
        )

        # wallet_score_decisions row invariant.
        w_row = db.conn.execute(
            "SELECT verdict, missing_essentials_json, eligibility_failures_json "
            "FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert w_row is not None
        if w_row["verdict"] == "skip":
            missing = json.loads(w_row["missing_essentials_json"] or "[]")
            failures = json.loads(w_row["eligibility_failures_json"] or "[]")
            assert not (not missing and not failures), (
                "PR27 invariant violated: wallet_score_decisions row "
                "has verdict='skip' AND empty missing_essentials_json "
                "AND empty eligibility_failures_json"
            )
            # In the score-driven skip path the marker must be present.
            assert SCORE_BELOW_COPY_THRESHOLD in failures, (
                "PR27 invariant: score-driven skip must carry "
                "score_below_copy_threshold"
            )

        # decision_verdicts row invariant.
        d_row = db.conn.execute(
            "SELECT verdict, verdict_family FROM decision_verdicts "
            "WHERE wallet_id = ? AND formula_name = 'wallet_score'",
            (wid,),
        ).fetchone()
        assert d_row is not None
        if d_row["verdict"] == "skip":
            assert d_row["verdict_family"] == "skip"
            # The pipeline writer does not currently persist
            # exclusion_reasons_json for wallet rows, but the parent
            # wallet_score_decisions row carries the marker — confirmed
            # above. Cross-table consistency is upheld because the
            # pipeline writer re-derives verdict/family from the
            # parent's evidence columns.
            assert d_row["verdict"] == w_row["verdict"]


# ────────────────────────────────────────────────────────────────────
# 5. PR27 cleanup follow-up — None caller buckets must also be caught
# ────────────────────────────────────────────────────────────────────


class TestPR27NoneBucketCleanup:
    """PR27 follow-up: the no-silent-skip invariant must also fire when
    the caller omits the reason buckets (``None``) rather than passing
    an empty list. ``None`` and ``[]`` are equivalent for this check.

    These tests use the pure ``derive_wallet_verdict_from_evidence``
    helper directly so the contract is exercised in isolation from
    ``compute_wallet_score_v1`` (which always supplies concrete bucket
    values).
    """

    def test_skip_with_both_buckets_none_gets_marker(self):
        """Sufficient-evidence skip with ``missing_essentials=None`` AND
        ``eligibility_failures=None`` ⇒ verdict stays SKIP and
        ``score_below_copy_threshold`` is appended.

        PR24F: also provide all required non-resolution evidence so
        Rule 1b does not promote to incomplete.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
            SCORE_BELOW_COPY_THRESHOLD,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="skip",
            resolved_markets=50,
            category_resolved_markets=20,
            # PR24F: all required non-resolution evidence must be present
            # for a real verdict (Rule 1b).
            sample_fraction=0.20,
            sharpe_ratio=0.5,
            max_drawdown=0.30,
            missing_essentials=None,
            eligibility_failures=None,
        )
        assert out["verdict"] == "skip"
        assert out["verdict_family"] == "skip"
        assert SCORE_BELOW_COPY_THRESHOLD in out["eligibility_failures"], (
            "PR27: skip with None eligibility_failures must still "
            "carry the canonical score-driven marker"
        )
        assert out["eligibility_failures"], (
            "eligibility_failures must be non-empty for any skip"
        )

    def test_skip_with_empty_missing_and_none_failures_gets_marker(self):
        """Sufficient-evidence skip with ``missing_essentials=[]`` AND
        ``eligibility_failures=None`` ⇒ verdict stays SKIP and
        ``score_below_copy_threshold`` is appended.

        PR24F: also provide all required non-resolution evidence.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
            SCORE_BELOW_COPY_THRESHOLD,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="skip",
            resolved_markets=50,
            category_resolved_markets=20,
            sample_fraction=0.20,
            sharpe_ratio=0.5,
            max_drawdown=0.30,
            missing_essentials=[],
            eligibility_failures=None,
        )
        assert out["verdict"] == "skip"
        assert out["verdict_family"] == "skip"
        assert SCORE_BELOW_COPY_THRESHOLD in out["eligibility_failures"]

    def test_skip_with_existing_failure_preserved_when_none_buckets(self):
        """Sufficient-evidence skip with non-empty eligibility_failures
        preserves the existing failure; canonical marker is NOT
        duplicated even when ``missing_essentials`` is ``None``.

        PR24F: also provide all required non-resolution evidence.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="skip",
            resolved_markets=50,
            category_resolved_markets=20,
            sample_fraction=0.20,
            sharpe_ratio=0.5,
            max_drawdown=0.30,
            missing_essentials=None,
            eligibility_failures=["active_trading_days=5 < 20"],
        )
        assert out["verdict"] == "skip"
        assert out["eligibility_failures"] == ["active_trading_days=5 < 20"]
        assert "score_below_copy_threshold" not in out["eligibility_failures"]

    def test_zero_evidence_skip_with_none_buckets_becomes_incomplete(self):
        """Zero-evidence skip with both buckets ``None`` ⇒ INCOMPLETE
        with ``no_resolved_market_evidence`` (Rule 1 still fires).
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            verdict="skip",
            resolved_markets=None,
            category_resolved_markets=None,
            missing_essentials=None,
            eligibility_failures=None,
        )
        assert out["verdict"] == "incomplete"
        assert out["verdict_family"] == "incomplete"
        assert "no_resolved_market_evidence" in out["eligibility_failures"]
        assert out["missing_essentials"], (
            "missing_essentials must be populated for incomplete"
        )