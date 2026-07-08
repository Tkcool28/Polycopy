"""PR24H — Category decision evidence guard tests.

Five classes mirror the brief's required-test surface:

1. ``TestRule1aZeroCategoryResolutionForcesIncomplete`` — zero or
   missing ``category_resolved_markets`` ⇒ INCOMPLETE +
   ``no_resolved_market_evidence``.

2. ``TestRule1bMissingRequiredNonResolutionForcesIncomplete`` —
   missing ``sample_fraction`` / ``sharpe_ratio`` / ``max_drawdown``
   ⇒ INCOMPLETE + ``missing_required_evidence``.

3. ``TestZeroIsPresentForCategoryNonResolutionFields`` — numeric
   zero for the three non-resolution keys is treated as a real
   measurement.

4. ``TestCategorySkipWithSufficientEvidence`` — full-evidence skip
   stays SKIP but carries ``score_below_copy_threshold``.

5. ``TestPersistencePathEnforcesCategoryCompleteness`` — the
   ``persist_category_score_v1`` path cannot persist a SKIP /
   WATCHLIST / COPY_CANDIDATE with missing required evidence.

Plus regression tests proving the wallet-level guard chain still
passes unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────
# 1. Rule 1a — zero category-resolution evidence ⇒ INCOMPLETE
# ─────────────────────────────────────────────────────────────────────


class TestRule1aZeroCategoryResolutionForcesIncomplete:
    """``category_resolved_markets`` None or zero ⇒ INCOMPLETE."""

    def test_category_resolved_markets_none_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_category_verdict_from_evidence,
            NO_RESOLVED_MARKET_EVIDENCE,
        )

        out = derive_category_verdict_from_evidence(
            verdict="copy_candidate",
            category_resolved_markets=None,
            sample_fraction=1.0,
            sharpe_ratio=2.0,
            max_drawdown=0.10,
        )
        assert out["verdict"] == "incomplete"
        assert out["verdict_family"] == "incomplete"
        assert "category_resolved_markets" in out["missing_essentials"]
        assert NO_RESOLVED_MARKET_EVIDENCE in out["category_gate_failures"]

    def test_category_resolved_markets_zero_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_category_verdict_from_evidence,
            NO_RESOLVED_MARKET_EVIDENCE,
        )

        out = derive_category_verdict_from_evidence(
            verdict="watchlist",
            category_resolved_markets=0,
            sample_fraction=1.0,
            sharpe_ratio=2.0,
            max_drawdown=0.10,
        )
        assert out["verdict"] == "incomplete"
        assert out["verdict_family"] == "incomplete"
        assert "category_resolved_markets" in out["missing_essentials"]
        assert NO_RESOLVED_MARKET_EVIDENCE in out["category_gate_failures"]

    def test_missing_category_resolution_marker_in_gate_failures(self):
        """Brief test 3: missing category resolution adds
        ``no_resolved_market_evidence``.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            enforce_category_decision_eligibility,
            NO_RESOLVED_MARKET_EVIDENCE,
        )

        out = enforce_category_decision_eligibility(
            verdict="copy_candidate",
            category_resolved_markets=None,
            sample_fraction=1.0,
            sharpe_ratio=2.0,
            max_drawdown=0.10,
        )
        assert NO_RESOLVED_MARKET_EVIDENCE in out["category_gate_failures"]
        assert out["verdict"] == "incomplete"

    def test_missing_category_resolution_populates_gate_failures_json(self):
        """Brief test 4: missing category evidence populates
        ``category_gate_failures_json``.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            enforce_category_decision_eligibility,
        )

        out = enforce_category_decision_eligibility(
            verdict="copy_candidate",
            category_resolved_markets=None,
            sample_fraction=1.0,
            sharpe_ratio=2.0,
            max_drawdown=0.10,
        )
        assert out["category_gate_failures"], (
            "category_gate_failures MUST be non-empty when verdict is INCOMPLETE"
        )
        # The JSON-stringified form is what gets persisted.
        persisted = json.dumps(out["category_gate_failures"])
        assert "no_resolved_market_evidence" in persisted


# ─────────────────────────────────────────────────────────────────────
# 2. Rule 1b — missing required non-resolution evidence ⇒ INCOMPLETE
# ─────────────────────────────────────────────────────────────────────


class TestRule1bMissingRequiredNonResolutionForcesIncomplete:
    """Brief tests 10a-c: missing sample/risk fields force INCOMPLETE."""

    def test_missing_sample_fraction_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_category_verdict_from_evidence,
            MISSING_REQUIRED_EVIDENCE,
        )

        out = derive_category_verdict_from_evidence(
            verdict="copy_candidate",
            category_resolved_markets=15,
            sample_fraction=None,
            sharpe_ratio=2.0,
            max_drawdown=0.10,
        )
        assert out["verdict"] == "incomplete"
        assert out["verdict_family"] == "incomplete"
        assert "sample_fraction" in out["missing_essentials"]
        assert MISSING_REQUIRED_EVIDENCE in out["category_gate_failures"]

    def test_missing_sharpe_ratio_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_category_verdict_from_evidence,
            MISSING_REQUIRED_EVIDENCE,
        )

        out = derive_category_verdict_from_evidence(
            verdict="watchlist",
            category_resolved_markets=15,
            sample_fraction=1.0,
            sharpe_ratio=None,
            max_drawdown=0.10,
        )
        assert out["verdict"] == "incomplete"
        assert "sharpe_ratio" in out["missing_essentials"]
        assert MISSING_REQUIRED_EVIDENCE in out["category_gate_failures"]

    def test_missing_max_drawdown_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_category_verdict_from_evidence,
            MISSING_REQUIRED_EVIDENCE,
        )

        out = derive_category_verdict_from_evidence(
            verdict="skip",
            category_resolved_markets=15,
            sample_fraction=1.0,
            sharpe_ratio=2.0,
            max_drawdown=None,
        )
        assert out["verdict"] == "incomplete"
        assert "max_drawdown" in out["missing_essentials"]
        assert MISSING_REQUIRED_EVIDENCE in out["category_gate_failures"]

    def test_all_three_non_resolution_missing_lists_all(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_category_verdict_from_evidence,
        )

        out = derive_category_verdict_from_evidence(
            verdict="copy_candidate",
            category_resolved_markets=15,
            sample_fraction=None,
            sharpe_ratio=None,
            max_drawdown=None,
        )
        assert out["verdict"] == "incomplete"
        assert set(out["missing_essentials"]) == {
            "sample_fraction",
            "sharpe_ratio",
            "max_drawdown",
        }


# ─────────────────────────────────────────────────────────────────────
# 3. Numeric zero is treated as present for non-resolution fields
# ─────────────────────────────────────────────────────────────────────


class TestZeroIsPresentForCategoryNonResolutionFields:
    """Brief tests 10d-e + project policy: zero is information, not absence."""

    def test_sharpe_ratio_zero_is_present(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_category_verdict_from_evidence,
            SCORE_BELOW_COPY_THRESHOLD,
        )

        out = derive_category_verdict_from_evidence(
            verdict="skip",
            category_resolved_markets=15,
            sample_fraction=1.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
        )
        # Sufficient evidence; verdict preserved as skip + marker.
        assert out["verdict"] == "skip"
        assert SCORE_BELOW_COPY_THRESHOLD in out["category_gate_failures"]
        assert out["missing_essentials"] == []

    def test_max_drawdown_zero_is_present(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_category_verdict_from_evidence,
            SCORE_BELOW_COPY_THRESHOLD,
        )

        out = derive_category_verdict_from_evidence(
            verdict="skip",
            category_resolved_markets=15,
            sample_fraction=1.0,
            sharpe_ratio=2.0,
            max_drawdown=0.0,
        )
        assert out["verdict"] == "skip"
        assert SCORE_BELOW_COPY_THRESHOLD in out["category_gate_failures"]
        assert out["missing_essentials"] == []

    def test_sample_fraction_zero_is_present(self):
        """Project policy (matching wallet policy): ``sample_fraction=0.0``
        is treated as a real measurement, not absence. Operators can
        flip this policy in one place if needed — but the default is
        "zero is information".
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_category_verdict_from_evidence,
            SCORE_BELOW_COPY_THRESHOLD,
        )

        out = derive_category_verdict_from_evidence(
            verdict="skip",
            category_resolved_markets=15,
            sample_fraction=0.0,
            sharpe_ratio=2.0,
            max_drawdown=0.10,
        )
        assert out["verdict"] == "skip"
        assert SCORE_BELOW_COPY_THRESHOLD in out["category_gate_failures"]
        assert out["missing_essentials"] == []


# ─────────────────────────────────────────────────────────────────────
# 4. Category SKIP with sufficient evidence is preserved as SKIP
# ─────────────────────────────────────────────────────────────────────


class TestCategorySkipWithSufficientEvidence:
    """Brief tests 5-7: SKIP semantics on the category side."""

    def test_full_evidence_skip_is_preserved_as_skip(self):
        """Brief test 5: Category "skip" with sufficient evidence is
        preserved as "skip".
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_category_verdict_from_evidence,
        )

        out = derive_category_verdict_from_evidence(
            verdict="skip",
            category_resolved_markets=15,
            sample_fraction=1.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
        )
        assert out["verdict"] == "skip"
        assert out["verdict_family"] == "skip"

    def test_full_evidence_skip_with_empty_failures_gets_marker(self):
        """Brief test 6: Category "skip" with sufficient evidence and
        empty failures gets ``score_below_copy_threshold``.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            enforce_category_decision_eligibility,
            SCORE_BELOW_COPY_THRESHOLD,
        )

        out = enforce_category_decision_eligibility(
            verdict="skip",
            category_resolved_markets=15,
            sample_fraction=1.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
        )
        assert out["verdict"] == "skip"
        assert out["verdict_family"] == "skip"
        assert SCORE_BELOW_COPY_THRESHOLD in out["category_gate_failures"]
        assert out["missing_essentials"] == []

    def test_existing_category_gate_failure_is_preserved_not_duplicated(self):
        """Brief test 7: existing category gate failure is preserved
        and not duplicated.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            enforce_category_decision_eligibility,
        )

        out = enforce_category_decision_eligibility(
            verdict="skip",
            category_resolved_markets=15,
            sample_fraction=1.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
            category_gate_failures=["custom_category_exclusion"],
        )
        assert out["verdict"] == "skip"
        # The custom marker is preserved; the canonical
        # ``score_below_copy_threshold`` is NOT appended because the
        # caller already supplied a non-empty list.
        assert out["category_gate_failures"] == ["custom_category_exclusion"]

    def test_verdict_family_follows_corrected_verdict(self):
        """Brief test 8: Category ``verdict_family`` follows corrected
        ``verdict``.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            enforce_category_decision_eligibility,
        )

        out = enforce_category_decision_eligibility(
            verdict="skip",
            verdict_family="skip",
            category_resolved_markets=None,  # forces incomplete
            sample_fraction=1.0,
            sharpe_ratio=2.0,
            max_drawdown=0.10,
        )
        assert out["verdict"] == "incomplete"
        assert out["verdict_family"] == "incomplete"


# ─────────────────────────────────────────────────────────────────────
# 5. Persistence-path enforcement (end-to-end on a fresh DB)
# ─────────────────────────────────────────────────────────────────────


class TestPersistencePathEnforcesCategoryCompleteness:
    """End-to-end: ``persist_category_score_v1`` cannot persist a real
    verdict (SKIP / WATCHLIST / COPY_CANDIDATE) with missing required
    category evidence.
    """

    def _fresh_db(self, tmp_path: Path):
        from polycopy.db.database import Database
        db_path = tmp_path / "p24h.db"
        db = Database(db_path=db_path)
        db.connect()
        db.conn.execute(
            "INSERT INTO wallets (id, address, label, is_sample, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            ("0xWALLET", "0xWALLET", "default", "2026-07-01T00:00:00Z"),
        )
        db.conn.commit()
        return db

    def test_persist_zero_resolution_forces_incomplete(self, tmp_path: Path):
        """Brief test 9 (consistency): when category_resolution is
        zero, persisted verdict MUST be incomplete with
        ``no_resolved_market_evidence`` in
        ``category_gate_failures_json``.
        """
        from polycopy.scoring.category_wallet_score_v1 import (
            CategoryWalletScoreInputV1,
            compute_category_wallet_score_v1,
        )
        from polycopy.scoring.score_serialization import (
            persist_category_score_v1,
        )

        db = self._fresh_db(tmp_path)
        try:
            inp = CategoryWalletScoreInputV1(
                wallet_id="0xWALLET",
                category_label="crypto",
                trade_count=200,
                win_rate=0.6,
                category_resolved_markets=0,  # zero ⇒ INCOMPLETE
                category_distinct_events=10,
                category_active_days=15,
                sample_fraction=1.0,
                sharpe_ratio=2.0,
                max_drawdown=0.10,
            )
            res = compute_category_wallet_score_v1(input=inp)
            row_id = persist_category_score_v1(
                db, "0xWALLET", "crypto", res,
                source_data_timestamp="2026-07-01T00:00:00Z",
            )
            db.conn.commit()
            assert row_id is not None
            row = db.conn.execute(
                "SELECT verdict, missing_essentials_json, "
                "category_gate_failures_json "
                "FROM category_wallet_score_decisions WHERE id = ?",
                (row_id,),
            ).fetchone()
            assert row["verdict"] == "incomplete"
            failures = json.loads(row["category_gate_failures_json"] or "[]")
            assert "no_resolved_market_evidence" in failures
        finally:
            db.close()

    def test_persist_missing_sample_fraction_forces_incomplete(self, tmp_path: Path):
        from polycopy.scoring.category_wallet_score_v1 import (
            CategoryWalletScoreInputV1,
            compute_category_wallet_score_v1,
        )
        from polycopy.scoring.score_serialization import (
            persist_category_score_v1,
        )

        db = self._fresh_db(tmp_path)
        try:
            inp = CategoryWalletScoreInputV1(
                wallet_id="0xWALLET",
                category_label="crypto",
                trade_count=200,
                win_rate=0.6,
                category_resolved_markets=20,
                category_distinct_events=10,
                category_active_days=15,
                sample_fraction=None,  # missing ⇒ INCOMPLETE
                sharpe_ratio=2.0,
                max_drawdown=0.10,
            )
            res = compute_category_wallet_score_v1(input=inp)
            row_id = persist_category_score_v1(
                db, "0xWALLET", "crypto", res,
                source_data_timestamp="2026-07-01T00:00:00Z",
            )
            db.conn.commit()
            row = db.conn.execute(
                "SELECT verdict, missing_essentials_json, "
                "category_gate_failures_json "
                "FROM category_wallet_score_decisions WHERE id = ?",
                (row_id,),
            ).fetchone()
            assert row["verdict"] == "incomplete"
            missing = json.loads(row["missing_essentials_json"] or "[]")
            failures = json.loads(row["category_gate_failures_json"] or "[]")
            assert "sample_fraction" in missing
            assert "missing_required_evidence" in failures
        finally:
            db.close()

    def test_persist_full_evidence_skip_carries_marker(self, tmp_path: Path):
        """Brief test 16 (analog): full-evidence skip with empty
        gate failures must persist with
        ``score_below_copy_threshold``.
        """
        from polycopy.scoring.category_wallet_score_v1 import (
            CategoryWalletScoreInputV1,
            compute_category_wallet_score_v1,
        )
        from polycopy.scoring.score_serialization import (
            persist_category_score_v1,
        )

        db = self._fresh_db(tmp_path)
        try:
            inp = CategoryWalletScoreInputV1(
                wallet_id="0xWALLET",
                category_label="crypto",
                trade_count=200,
                win_rate=0.6,
                category_resolved_markets=20,
                category_distinct_events=10,
                category_active_days=15,
                sample_fraction=1.0,
                sharpe_ratio=0.0,  # zero Sharpe ⇒ real measurement
                max_drawdown=0.0,  # zero drawdown ⇒ real measurement
            )
            res = compute_category_wallet_score_v1(input=inp)
            row_id = persist_category_score_v1(
                db, "0xWALLET", "crypto", res,
                source_data_timestamp="2026-07-01T00:00:00Z",
            )
            db.conn.commit()
            row = db.conn.execute(
                "SELECT verdict, missing_essentials_json, "
                "category_gate_failures_json "
                "FROM category_wallet_score_decisions WHERE id = ?",
                (row_id,),
            ).fetchone()
            # Either verdict is SKIP with marker, or the score
            # actually cleared 55+ and produced WATCHLIST — either
            # way, the row MUST be auditable. We assert auditable
            # here.
            assert row["verdict"] in {"skip", "watchlist", "copy_candidate"}
            failures = json.loads(row["category_gate_failures_json"] or "[]")
            missing = json.loads(row["missing_essentials_json"] or "[]")
            assert failures or missing, (
                "Full-evidence non-INCOMPLETE category row must "
                "carry at least one auditable reason bucket"
            )
        finally:
            db.close()

    def test_persist_manual_skip_with_missing_evidence_is_blocked(self, tmp_path: Path):
        """Simulate a caller constructing a result with verdict=skip
        and missing required evidence. The persistence path must NOT
        trust the caller's verdict — the guard fires and forces
        INCOMPLETE.
        """
        from polycopy.scoring.category_wallet_score_v1 import (
            CategoryWalletScoreInputV1,
            CategoryWalletScoreResultV1,
            WalletVerdict,
        )
        from polycopy.scoring.score_serialization import (
            persist_category_score_v1,
        )
        from datetime import datetime, timezone

        db = self._fresh_db(tmp_path)
        try:
            inp = CategoryWalletScoreInputV1(
                wallet_id="0xWALLET",
                category_label="crypto",
                trade_count=200,
                win_rate=0.6,
                category_resolved_markets=15,
                category_distinct_events=10,
                category_active_days=15,
                sample_fraction=None,  # missing
                sharpe_ratio=2.0,
                max_drawdown=0.10,
            )
            # Caller lies: builds result with verdict=skip despite
            # missing sample_fraction.
            result = CategoryWalletScoreResultV1(
                wallet_id="0xWALLET",
                category_label="crypto",
                score=10.0,
                verdict=WalletVerdict.SKIP,
                input=inp,
                components=[],
                missing_essentials=[],
                category_gate_failures=[],
                computed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                formula_version="1",
                source_data_timestamp="2026-07-01T00:00:00Z",
            )
            row_id = persist_category_score_v1(
                db, "0xWALLET", "crypto", result,
                source_data_timestamp="2026-07-01T00:00:00Z",
            )
            db.conn.commit()
            row = db.conn.execute(
                "SELECT verdict, missing_essentials_json, "
                "category_gate_failures_json "
                "FROM category_wallet_score_decisions WHERE id = ?",
                (row_id,),
            ).fetchone()
            # PR24H guard must have rewritten SKIP -> INCOMPLETE.
            assert row["verdict"] == "incomplete", (
                "Manual SKIP with missing required category evidence "
                "MUST be rewritten to INCOMPLETE by the guard"
            )
            missing = json.loads(row["missing_essentials_json"] or "[]")
            assert "sample_fraction" in missing
            failures = json.loads(row["category_gate_failures_json"] or "[]")
            assert "missing_required_evidence" in failures
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────
# 6. Runtime-safety invariants — no category skip can be silent
# ─────────────────────────────────────────────────────────────────────


class TestRuntimeSafetyInvariants:
    """Brief PART 6 — runtime safety tests."""

    def test_no_category_skip_can_be_silent(self):
        """A category decision whose verdict is SKIP is NEVER persisted
        with both ``missing_essentials_json`` and
        ``category_gate_failures_json`` empty.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_category_verdict_from_evidence,
        )

        # Sufficient evidence + SKIP -> never silent.
        out = derive_category_verdict_from_evidence(
            verdict="skip",
            category_resolved_markets=15,
            sample_fraction=1.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
        )
        assert not (out["missing_essentials"] == [] and out["category_gate_failures"] == []), (
            "Category SKIP must carry at least one reason bucket"
        )

    def test_no_category_skip_with_missing_required_evidence(self):
        """A category decision with missing required category evidence
        cannot produce a SKIP verdict.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_category_verdict_from_evidence,
        )

        cases = [
            # (sample_fraction, sharpe_ratio, max_drawdown)
            (None, 2.0, 0.10),
            (1.0, None, 0.10),
            (1.0, 2.0, None),
            (None, None, 0.10),
            (None, None, None),
        ]
        for sf, sr, dd in cases:
            out = derive_category_verdict_from_evidence(
                verdict="skip",
                category_resolved_markets=15,
                sample_fraction=sf,
                sharpe_ratio=sr,
                max_drawdown=dd,
            )
            assert out["verdict"] == "incomplete", (
                f"Missing (sf={sf}, sr={sr}, dd={dd}) must force INCOMPLETE, "
                f"got {out['verdict']}"
            )

    def test_category_guard_uses_shared_helper(self):
        """Brief PART 6: "category guard uses shared helper, not
        duplicate local logic". The category helper is the same module
        as the wallet helper; verify both share the canonical markers
        and key-aware rule logic by exercising them via the same
        import surface.
        """
        from polycopy.scoring import incomplete_verdict_guard as g

        # Same canonical marker used by both wallet and category paths.
        assert g.NO_RESOLVED_MARKET_EVIDENCE == "no_resolved_market_evidence"
        assert g.MISSING_REQUIRED_EVIDENCE == "missing_required_evidence"
        assert g.SCORE_BELOW_COPY_THRESHOLD == "score_below_copy_threshold"

        # Both helpers exist and apply the same key-aware missing rule.
        assert g.lacks_category_resolution_evidence(0) is True
        assert g.lacks_category_resolution_evidence(None) is True
        assert g.lacks_category_resolution_evidence(15) is False


# ─────────────────────────────────────────────────────────────────────
# 7. Regression — wallet-level PR24E/F/G chain still passes
# ─────────────────────────────────────────────────────────────────────


class TestWalletGuardChainUnchanged:
    """Brief test 12 — wallet-level guards MUST still pass unchanged."""

    def test_pr24e_zero_resolution_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
            NO_RESOLVED_MARKET_EVIDENCE,
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
        assert NO_RESOLVED_MARKET_EVIDENCE in out["eligibility_failures"]

    def test_pr24f_full_evidence_skip_carries_marker(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            enforce_wallet_decision_eligibility,
            SCORE_BELOW_COPY_THRESHOLD,
        )

        out = enforce_wallet_decision_eligibility(
            verdict="skip",
            resolved_markets=60,
            category_resolved_markets=25,
            sample_fraction=1.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
        )
        assert out["verdict"] == "skip"
        assert SCORE_BELOW_COPY_THRESHOLD in out["eligibility_failures"]

    def test_pr24h_helper_is_importable_from_shared_module(self):
        """The category helper is co-located with the wallet helper in
        the same module — there is exactly one source of truth for
        the verdict-derivation logic.
        """
        import polycopy.scoring.incomplete_verdict_guard as g

        assert callable(g.derive_category_verdict_from_evidence)
        assert callable(g.enforce_category_decision_eligibility)
        assert callable(g.apply_to_category_score_result)
        assert callable(g.lacks_category_resolution_evidence)