"""PR24F: regression tests for the required-evidence completeness guard.

Five evidence keys gate every wallet verdict in production:

  * ``resolved_markets``            — required, None-or-zero ⇒ missing
  * ``category_resolved_markets``   — required, None-or-zero ⇒ missing
  * ``sample_fraction``             — required, None ⇒ missing
  * ``sharpe_ratio``                — required, None ⇒ missing
  * ``max_drawdown``                — required, None ⇒ missing

A wallet decision may produce a real verdict (``skip``, ``watchlist``,
``copy_candidate``) ONLY if all five keys are present and usable.
Zero-resolution evidence is one failure mode; missing risk/sample
evidence is another. Both force ``verdict = "incomplete"`` with
distinct canonical markers:

  * ``no_resolved_market_evidence`` — Rule 1a (zero-resolution)
  * ``missing_required_evidence``    — Rule 1b (missing risk/sample)

PR24F is a strict superset of PR24E + PR27: the existing zero-resolution
and no-silent-skip invariants are preserved. This test file proves
the new completeness contract holds.

Tests cover three layers:

  1. **Pure helper invariants** — ``derive_wallet_verdict_from_evidence``
     and ``enforce_wallet_decision_eligibility`` produce the right
     payloads for every documented case.
  2. **Compute path** — ``compute_wallet_score_v1`` returns
     ``WalletVerdict.INCOMPLETE`` when any required evidence is
     missing, even when other inputs look healthy.
  3. **Persistence path** — the full pipeline cannot produce a
     ``skip`` row with missing required evidence; a real skip must
     always carry the canonical score-driven marker.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path



# ────────────────────────────────────────────────────────────────────
# Constants — the canonical marker strings this PR introduces.
# ────────────────────────────────────────────────────────────────────


REQUIRED_EVIDENCE = {
    "resolved_markets",
    "category_resolved_markets",
    "sample_fraction",
    "sharpe_ratio",
    "max_drawdown",
}


def _full_evidence_kwargs(**overrides):
    """Return a kwargs dict for ``derive_wallet_verdict_from_evidence``
    that supplies every required evidence field with a non-None value.

    Used to make sure tests that want to drive a non-INCOMPLETE verdict
    always provide the full evidence bundle, isolating the behaviour
    under test.
    """
    base = {
        "verdict": "skip",
        "resolved_markets": 50,
        "category_resolved_markets": 20,
        "sample_fraction": 0.20,
        "sharpe_ratio": 0.5,
        "max_drawdown": 0.30,
        "missing_essentials": None,
        "eligibility_failures": None,
    }
    base.update(overrides)
    return base


# ────────────────────────────────────────────────────────────────────
# 1. Pure helper invariants — Rule 1a (zero-resolution evidence)
# ────────────────────────────────────────────────────────────────────


class TestRule1aZeroResolutionForcesIncomplete:
    """Cases 1–2 from the task brief.

    Zero-resolution evidence is the strongest signal that a wallet
    cannot be evaluated. The verdict must be forced to ``incomplete``
    and ``eligibility_failures`` must include
    ``no_resolved_market_evidence``.
    """

    def test_resolved_markets_none_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
            MISSING_REQUIRED_EVIDENCE,
            NO_RESOLVED_MARKET_EVIDENCE,
        )

        out = derive_wallet_verdict_from_evidence(
            **_full_evidence_kwargs(resolved_markets=None)
        )
        assert out["verdict"] == "incomplete"
        assert out["verdict_family"] == "incomplete"
        assert "resolved_markets" in out["missing_essentials"]
        assert NO_RESOLVED_MARKET_EVIDENCE in out["eligibility_failures"]
        # Rule 1a is the strongest trigger; Rule 1b's marker MUST NOT
        # be added on top because Rule 1b is exclusive with Rule 1a.
        assert MISSING_REQUIRED_EVIDENCE not in out["eligibility_failures"]

    def test_resolved_markets_zero_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
            NO_RESOLVED_MARKET_EVIDENCE,
        )

        out = derive_wallet_verdict_from_evidence(
            **_full_evidence_kwargs(resolved_markets=0)
        )
        assert out["verdict"] == "incomplete"
        assert "resolved_markets" in out["missing_essentials"]
        assert NO_RESOLVED_MARKET_EVIDENCE in out["eligibility_failures"]

    def test_category_resolved_markets_none_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
            NO_RESOLVED_MARKET_EVIDENCE,
        )

        out = derive_wallet_verdict_from_evidence(
            **_full_evidence_kwargs(category_resolved_markets=None)
        )
        assert out["verdict"] == "incomplete"
        assert "category_resolved_markets" in out["missing_essentials"]
        assert NO_RESOLVED_MARKET_EVIDENCE in out["eligibility_failures"]

    def test_category_resolved_markets_zero_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
            NO_RESOLVED_MARKET_EVIDENCE,
        )

        out = derive_wallet_verdict_from_evidence(
            **_full_evidence_kwargs(category_resolved_markets=0)
        )
        assert out["verdict"] == "incomplete"
        assert "category_resolved_markets" in out["missing_essentials"]
        assert NO_RESOLVED_MARKET_EVIDENCE in out["eligibility_failures"]


# ────────────────────────────────────────────────────────────────────
# 2. Pure helper invariants — Rule 1b (missing required evidence)
# ────────────────────────────────────────────────────────────────────


class TestRule1bMissingRequiredEvidenceForcesIncomplete:
    """Cases 3–5 from the task brief.

    Even with sufficient resolution evidence, a missing
    ``sample_fraction``, ``sharpe_ratio``, or ``max_drawdown`` is enough
    to disqualify a wallet from a real verdict. The verdict must be
    forced to ``incomplete`` and ``eligibility_failures`` must include
    the canonical ``missing_required_evidence`` marker.
    """

    def test_sample_fraction_none_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
            MISSING_REQUIRED_EVIDENCE,
            NO_RESOLVED_MARKET_EVIDENCE,
        )

        out = derive_wallet_verdict_from_evidence(
            **_full_evidence_kwargs(sample_fraction=None)
        )
        assert out["verdict"] == "incomplete"
        assert "sample_fraction" in out["missing_essentials"]
        assert MISSING_REQUIRED_EVIDENCE in out["eligibility_failures"]
        # Rule 1a did not fire, so the resolution marker MUST NOT be
        # present.
        assert NO_RESOLVED_MARKET_EVIDENCE not in out["eligibility_failures"]

    def test_sharpe_ratio_none_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
            MISSING_REQUIRED_EVIDENCE,
        )

        out = derive_wallet_verdict_from_evidence(
            **_full_evidence_kwargs(sharpe_ratio=None)
        )
        assert out["verdict"] == "incomplete"
        assert "sharpe_ratio" in out["missing_essentials"]
        assert MISSING_REQUIRED_EVIDENCE in out["eligibility_failures"]

    def test_max_drawdown_none_forces_incomplete(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
            MISSING_REQUIRED_EVIDENCE,
        )

        out = derive_wallet_verdict_from_evidence(
            **_full_evidence_kwargs(max_drawdown=None)
        )
        assert out["verdict"] == "incomplete"
        assert "max_drawdown" in out["missing_essentials"]
        assert MISSING_REQUIRED_EVIDENCE in out["eligibility_failures"]

    def test_all_three_non_resolution_missing_lists_all_keys(self):
        """Case 8 from the task brief: every non-resolution field missing
        ⇒ INCOMPLETE and all three names appear in ``missing_essentials``.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
            MISSING_REQUIRED_EVIDENCE,
        )

        out = derive_wallet_verdict_from_evidence(
            **_full_evidence_kwargs(
                sample_fraction=None,
                sharpe_ratio=None,
                max_drawdown=None,
            )
        )
        assert out["verdict"] == "incomplete"
        assert "sample_fraction" in out["missing_essentials"]
        assert "sharpe_ratio" in out["missing_essentials"]
        assert "max_drawdown" in out["missing_essentials"]
        assert MISSING_REQUIRED_EVIDENCE in out["eligibility_failures"]


# ────────────────────────────────────────────────────────────────────
# 3. Zero is NOT missing for non-resolution fields
# ────────────────────────────────────────────────────────────────────


class TestZeroIsPresentForNonResolutionFields:
    """Cases 12–14 from the task brief.

    The PR24F semantics distinguish two classes of "missing":

      * Resolution counts (``resolved_markets``, ``category_resolved_markets``):
        ``None`` or zero ⇒ missing. Zero means "no resolved markets
        exist" and is treated as insufficient evidence.

      * Risk / sample fields (``sample_fraction``, ``sharpe_ratio``,
        ``max_drawdown``): only ``None`` ⇒ missing. A numeric zero is
        a real measured value (no excess return per unit risk; no
        observed drawdown; no observed sample). Those are signals, not
        absence.
    """

    def test_sharpe_ratio_zero_is_present(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            SCORE_BELOW_COPY_THRESHOLD,
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            **_full_evidence_kwargs(sharpe_ratio=0.0, verdict="skip")
        )
        # Verdict preserved as SKIP — zero Sharpe is a real measurement.
        assert out["verdict"] == "skip"
        assert out["verdict_family"] == "skip"
        assert SCORE_BELOW_COPY_THRESHOLD in out["eligibility_failures"]
        assert "sharpe_ratio" not in out["missing_essentials"]

    def test_max_drawdown_zero_is_present(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            **_full_evidence_kwargs(max_drawdown=0.0, verdict="skip")
        )
        assert out["verdict"] == "skip"
        assert "max_drawdown" not in out["missing_essentials"]

    def test_sample_fraction_zero_is_present(self):
        """``sample_fraction=0.0`` is treated as a real measured value
        (the wallet was observed over 0.0 of the resolved-market
        universe). It is NOT missing.

        Decision documented in PR24F: zero is information, not
        absence. This matches the project policy for ``sharpe_ratio``
        and ``max_drawdown`` and keeps the missing-required-evidence
        semantics focused on the truly-unmeasured case (``None``).
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            SCORE_BELOW_COPY_THRESHOLD,
            derive_wallet_verdict_from_evidence,
        )

        out = derive_wallet_verdict_from_evidence(
            **_full_evidence_kwargs(sample_fraction=0.0, verdict="skip")
        )
        assert out["verdict"] == "skip"
        assert "sample_fraction" not in out["missing_essentials"]
        # And the SKIP marker is still attached (PR27 invariant).
        assert SCORE_BELOW_COPY_THRESHOLD in out["eligibility_failures"]


# ────────────────────────────────────────────────────────────────────
# 4. SKIP behaviour when all evidence is present
# ────────────────────────────────────────────────────────────────────


class TestSkipWithAllRequiredEvidence:
    """Cases 6, 9, 10, 11 from the task brief.

    A SKIP can only survive the guard when all five required evidence
    keys are present. The PR27 no-silent-skip rule still fires — every
    SKIP must carry at least one eligibility failure.
    """

    def test_skip_with_full_evidence_and_empty_failures_gets_marker(self):
        """Case 9 + Case 10: real skip with all evidence present + empty
        eligibility_failures ⇒ ``score_below_copy_threshold`` is appended.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            SCORE_BELOW_COPY_THRESHOLD,
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
            eligibility_failures=None,
        )
        assert out["verdict"] == "skip"
        assert out["verdict_family"] == "skip"
        assert out["eligibility_failures"] == [SCORE_BELOW_COPY_THRESHOLD]

    def test_skip_with_full_evidence_and_existing_failure_preserved(self):
        """Case 11: a SKIP that already carries an explicit eligibility
        failure preserves the existing failure; the canonical
        ``score_below_copy_threshold`` marker is NOT duplicated.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            SCORE_BELOW_COPY_THRESHOLD,
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
        assert SCORE_BELOW_COPY_THRESHOLD not in out["eligibility_failures"]


# ────────────────────────────────────────────────────────────────────
# 5. enforce_wallet_decision_eligibility wrapper parity
# ────────────────────────────────────────────────────────────────────


class TestEnforceWrapperAppliesBothRules:
    """The ``enforce_wallet_decision_eligibility`` wrapper must produce
    the same decisions as ``derive_wallet_verdict_from_evidence``.
    """

    def test_zero_resolution_triggers_rule_1a(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            enforce_wallet_decision_eligibility,
            NO_RESOLVED_MARKET_EVIDENCE,
        )

        out = enforce_wallet_decision_eligibility(
            verdict="skip",
            resolved_markets=None,
            category_resolved_markets=None,
            sample_fraction=0.5,
            sharpe_ratio=1.0,
            max_drawdown=0.1,
        )
        assert out["verdict"] == "incomplete"
        assert out["verdict_family"] == "incomplete"
        assert NO_RESOLVED_MARKET_EVIDENCE in out["eligibility_failures"]

    def test_missing_required_triggers_rule_1b(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            enforce_wallet_decision_eligibility,
            MISSING_REQUIRED_EVIDENCE,
        )

        out = enforce_wallet_decision_eligibility(
            verdict="skip",
            resolved_markets=50,
            category_resolved_markets=20,
            sample_fraction=None,  # missing
            sharpe_ratio=1.0,
            max_drawdown=0.1,
        )
        assert out["verdict"] == "incomplete"
        assert "sample_fraction" in out["missing_essentials"]
        assert MISSING_REQUIRED_EVIDENCE in out["eligibility_failures"]


# ────────────────────────────────────────────────────────────────────
# 6. Compute path — compute_wallet_score_v1 returns INCOMPLETE
# ────────────────────────────────────────────────────────────────────


class TestComputeWalletScoreAppliesRule1b:
    """``compute_wallet_score_v1`` should return
    ``WalletVerdict.INCOMPLETE`` whenever any required evidence is
    missing, even when other inputs look healthy.
    """

    def test_compute_returns_incomplete_when_sample_fraction_missing(self):
        from polycopy.scoring.wallet_score_v1 import (
            WalletVerdict,
            compute_wallet_score_v1,
        )

        result = compute_wallet_score_v1(
            wallet_id="p24f-sample-missing",
            win_rate=0.85,
            profit_factor=2.1,
            trade_count=250,
            # Resolution evidence is sufficient.
            resolved_markets=60,
            category_resolved_markets=25,
            # Risk/sample evidence is intentionally missing.
            sharpe_ratio=2.6,
            max_drawdown=0.08,
            # sample_fraction omitted (None)
        )
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert "sample_fraction" in result.missing_essentials

    def test_compute_returns_incomplete_when_sharpe_ratio_missing(self):
        from polycopy.scoring.wallet_score_v1 import (
            WalletVerdict,
            compute_wallet_score_v1,
        )

        result = compute_wallet_score_v1(
            wallet_id="p24f-sharpe-missing",
            win_rate=0.85,
            profit_factor=2.1,
            trade_count=250,
            resolved_markets=60,
            category_resolved_markets=25,
            sample_fraction=0.30,
            # sharpe_ratio omitted
            max_drawdown=0.08,
        )
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert "sharpe_ratio" in result.missing_essentials

    def test_compute_returns_incomplete_when_max_drawdown_missing(self):
        from polycopy.scoring.wallet_score_v1 import (
            WalletVerdict,
            compute_wallet_score_v1,
        )

        result = compute_wallet_score_v1(
            wallet_id="p24f-drawdown-missing",
            win_rate=0.85,
            profit_factor=2.1,
            trade_count=250,
            resolved_markets=60,
            category_resolved_markets=25,
            sample_fraction=0.30,
            sharpe_ratio=2.6,
            # max_drawdown omitted
        )
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert "max_drawdown" in result.missing_essentials


# ────────────────────────────────────────────────────────────────────
# 7. Persistence path — no silent skip, no silent real verdict
# ────────────────────────────────────────────────────────────────────


def _fresh_db(tmp_path: Path):
    from polycopy.db.database import Database

    db_path = tmp_path / "p24f.db"
    db = Database(db_path=db_path)
    db.connect()
    return db


def _insert_wallet(db, wid: str = "0xP24F") -> str:
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (wid, wid, "default", "2026-07-01T00:00:00Z"),
    )
    db.conn.commit()
    return wid


class TestPersistencePathEnforcesCompleteness:
    """Cases 15 + 16 from the task brief.

    On the persistence path:

      * A real verdict (``skip``, ``watchlist``, ``copy_candidate``)
        with missing required evidence MUST be promoted to
        ``incomplete`` before INSERT.
      * A real verdict with all required evidence present MUST carry
        at least one non-empty eligibility failure (PR27 invariant).
      * No persisted row can represent ``skip`` with both
        ``missing_essentials_json`` and ``eligibility_failures_json``
        empty.
    """

    def test_persisted_skip_with_missing_required_evidence_becomes_incomplete(
        self, tmp_path: Path
    ):
        from polycopy.scoring.score_serialization import persist_wallet_score_v1
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1,
            WalletScoreResult,
            WalletVerdict,
        )

        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db, "0xPERSISTMISSING")

        # Real scoring input — sufficient resolution, but ``sharpe_ratio``
        # is missing. compute would naturally produce INCOMPLETE, but we
        # bypass it to prove the persistence guard is also active.
        inp = WalletScoreInputV1(
            wallet_id=wid,
            win_rate=0.65,
            trade_count=150,
            resolved_markets=50,
            category_resolved_markets=20,
            sample_fraction=0.20,
            max_drawdown=0.30,
            # sharpe_ratio missing
        )

        # Caller-supplied SKIP — should be promoted to INCOMPLETE by the
        # persistence guard.
        sneaky_skip = WalletScoreResult(
            wallet_id=wid,
            score=10.0,
            verdict=WalletVerdict.SKIP,
            input=inp,
            missing_essentials=[],
            eligibility_gate_failures=[],
        )

        persist_wallet_score_v1(
            db, wid, sneaky_skip, source_data_timestamp="2026-07-01T00:00:00Z"
        )
        db.conn.commit()

        row = db.conn.execute(
            "SELECT verdict, missing_essentials_json, eligibility_failures_json "
            "FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert row is not None
        # PR24F invariant: SKIP with missing required evidence MUST be
        # promoted to INCOMPLETE before persistence.
        assert row["verdict"] == "incomplete", (
            "PR24F: persisted skip with missing required evidence must "
            "be promoted to incomplete"
        )
        missing = json.loads(row["missing_essentials_json"] or "[]")
        failures = json.loads(row["eligibility_failures_json"] or "[]")
        assert "sharpe_ratio" in missing
        assert "missing_required_evidence" in failures, (
            "PR24F invariant: incomplete-from-missing-required-evidence "
            "must carry the missing_required_evidence marker"
        )

    def test_persisted_skip_with_full_evidence_still_carries_marker(
        self, tmp_path: Path
    ):
        """Case 15: with all required evidence present, a SKIP is
        persisted as SKIP and carries ``score_below_copy_threshold``.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            SCORE_BELOW_COPY_THRESHOLD,
        )
        from polycopy.scoring.score_serialization import persist_wallet_score_v1
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1,
            WalletScoreResult,
            WalletVerdict,
        )

        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db, "0xPERSISTFULL")

        inp = WalletScoreInputV1(
            wallet_id=wid,
            win_rate=0.65,
            trade_count=150,
            resolved_markets=50,
            category_resolved_markets=20,
            sample_fraction=0.20,
            sharpe_ratio=0.5,
            max_drawdown=0.30,
        )

        legit_skip = WalletScoreResult(
            wallet_id=wid,
            score=10.0,
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
        assert row["verdict"] == "skip"
        missing = json.loads(row["missing_essentials_json"] or "[]")
        failures = json.loads(row["eligibility_failures_json"] or "[]")
        # PR24F invariant: a real skip cannot be silent.
        assert not (not missing and not failures), (
            "PR24F invariant: persisted skip must have at least one "
            "non-empty reason bucket"
        )
        assert SCORE_BELOW_COPY_THRESHOLD in failures, (
            "PR24F invariant: persisted skip must carry "
            "score_below_copy_threshold"
        )

    def test_persisted_skip_with_existing_failure_preserved(self, tmp_path: Path):
        """Case 11 mirrored at the persistence layer: a SKIP that already
        has an explicit eligibility failure preserves it; the canonical
        marker is NOT duplicated.
        """
        from polycopy.scoring.score_serialization import persist_wallet_score_v1
        from polycopy.scoring.wallet_score_v1 import (
            WalletScoreInputV1,
            WalletScoreResult,
            WalletVerdict,
        )

        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db, "0xPERSISTEXISTING")

        inp = WalletScoreInputV1(
            wallet_id=wid,
            win_rate=0.65,
            trade_count=150,
            resolved_markets=50,
            category_resolved_markets=20,
            sample_fraction=0.20,
            sharpe_ratio=0.5,
            max_drawdown=0.30,
        )

        skip_with_existing = WalletScoreResult(
            wallet_id=wid,
            score=10.0,
            verdict=WalletVerdict.SKIP,
            input=inp,
            missing_essentials=[],
            eligibility_gate_failures=["active_trading_days=5 < 20"],
        )

        persist_wallet_score_v1(
            db, wid, skip_with_existing,
            source_data_timestamp="2026-07-01T00:00:00Z",
        )
        db.conn.commit()

        row = db.conn.execute(
            "SELECT verdict, eligibility_failures_json "
            "FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert row is not None
        assert row["verdict"] == "skip"
        failures = json.loads(row["eligibility_failures_json"] or "[]")
        assert "active_trading_days=5 < 20" in failures
        assert "score_below_copy_threshold" not in failures


# ────────────────────────────────────────────────────────────────────
# 8. decision_verdicts cross-table consistency
# ────────────────────────────────────────────────────────────────────


class TestDecisionVerdictsRowMirrorsParentEvidence:
    """The ``decision_verdicts`` companion row MUST agree with the parent
    ``wallet_score_decisions`` row's verdict when the underlying
    evidence is missing.
    """

    def test_decision_verdicts_row_is_incomplete_when_evidence_missing(
        self, tmp_path: Path
    ):
        sys.path.insert(  # noqa: E402
            0, str(Path(__file__).resolve().parent.parent / "scripts")
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
        wid = _insert_wallet(db, "0xDVCMISSING")

        inp = WalletScoreInputV1(
            wallet_id=wid,
            win_rate=0.65,
            trade_count=150,
            resolved_markets=50,
            category_resolved_markets=20,
            sample_fraction=0.20,
            sharpe_ratio=0.5,
            # max_drawdown missing — should force INCOMPLETE
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

        verdict_row = db.conn.execute(
            "SELECT verdict, verdict_family FROM decision_verdicts "
            "WHERE wallet_id = ? AND formula_name = 'wallet_score'",
            (wid,),
        ).fetchone()
        assert verdict_row is not None
        assert verdict_row["verdict"] == "incomplete"
        assert verdict_row["verdict_family"] == "incomplete"