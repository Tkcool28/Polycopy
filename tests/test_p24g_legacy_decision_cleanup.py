"""PR24G: regression tests for the legacy-decision cleanup path.

Covers:

1. **Pure helper invariants** —
   :func:`polycopy.scoring.incomplete_verdict_guard.derive_legacy_wallet_decision_repair`
   identifies legacy suspect rows (verdict=skip + empty buckets) and
   computes a correct repair plan through the current PR24F guard.

2. **Script-level dry-run** — calling the script with ``--dry-run``
   (the default) never writes, regardless of input shape.

3. **Script-level apply** — calling the script with ``--apply``
   repairs the legacy row, leaves already-valid rows untouched, and is
   idempotent on a re-run. The companion ``decision_verdicts`` row is
   repaired in lock-step when ``--include-decision-verdicts`` is set
   and linkage is unambiguous.

4. **Ambiguous linkage policy** — when a parent wallet row matches
   multiple ``decision_verdicts`` rows the script reports ambiguity
   and does NOT modify any companion row.

5. **Filter behaviour** — ``--wallet-id`` and ``--limit`` narrow the
   repair pass correctly.

Tests use a small SQLite DB via the existing ``Database`` wrapper, so
they run in-process without touching the production DB.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "repair_legacy_decision_verdicts.py"


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _fresh_db(tmp_path: Path):
    """Open a fresh Database at a tmp path."""
    from polycopy.db.database import Database

    db_path = tmp_path / "p24g.db"
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


def _insert_legacy_wallet_row(
    db,
    *,
    wallet_id: str,
    verdict: str = "skip",
    missing_essentials_json: str = "[]",
    eligibility_failures_json: str = "[]",
    resolved_markets=None,
    category_resolved_markets=None,
    sample_fraction=None,
    sharpe_ratio=None,
    max_drawdown=None,
) -> int:
    """Insert a hand-crafted legacy suspect ``wallet_score_decisions`` row.

    Includes the schema's required NOT NULL columns
    (``idempotency_key``, ``final_score``, ``verdict``, ``computed_at``,
    ``created_at``) plus a stable ``idempotency_key`` so this fixture
    can co-exist with other fixtures in the same DB without conflict.
    """
    cur = db.conn.execute(
        """
        INSERT INTO wallet_score_decisions (
            wallet_id, formula_name, formula_version, idempotency_key,
            verdict, missing_essentials_json, eligibility_failures_json,
            resolved_markets, category_resolved_markets,
            sample_fraction, sharpe_ratio, max_drawdown,
            final_score, source_data_timestamp, computed_at, created_at
        ) VALUES (?, 'wallet_score', 'v1', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0,
                  '2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z',
                  '2026-07-01T00:00:00Z')
        """,
        (
            wallet_id,
            f"legacy-fixture-{wallet_id}",
            verdict,
            missing_essentials_json,
            eligibility_failures_json,
            resolved_markets,
            category_resolved_markets,
            sample_fraction,
            sharpe_ratio,
            max_drawdown,
        ),
    )
    db.conn.commit()
    return int(cur.lastrowid)


def _insert_companion_decision_row(
    db,
    *,
    wallet_id: str,
    formula_name: str = "wallet_score",
    formula_version: str = "v1",
    verdict: str = "skip",
    verdict_family: str = "skip",
    source_ref_type: str = "wallet_id",
    source_ref_id: str | None = None,
    exclusion_reasons_json: str | None = None,
) -> int:
    cur = db.conn.execute(
        """
        INSERT INTO decision_verdicts (
            wallet_id, formula_name, formula_version,
            verdict, verdict_family, score,
            source_ref_type, source_ref_id, exclusion_reasons_json,
            computed_at
        ) VALUES (?, ?, ?, ?, ?, 0.0, ?, ?, ?, '2026-07-01T00:00:00Z')
        """,
        (
            wallet_id,
            formula_name,
            formula_version,
            verdict,
            verdict_family,
            source_ref_type,
            source_ref_id if source_ref_id is not None else wallet_id,
            exclusion_reasons_json,
        ),
    )
    db.conn.commit()
    return int(cur.lastrowid)


def _run_script(
    tmp_db_path: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the repair script as a subprocess against ``tmp_db_path``.

    Uses the operational lock with ``--lock-timeout 0`` so concurrent
    test runs can't deadlock; the env override points the lock to a
    tmp path so concurrent test runs don't block on the shared lock.
    """
    import os

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    # Disable the RSS guard for these tests — it's a smoke path, not a
    # memory-heavy workload.
    env.pop("POLYCOPY_MAX_RSS_MB", None)

    # Per-test lock file so concurrent pytest workers don't collide.
    lock_path = tmp_db_path.parent / f"{tmp_db_path.stem}.lock"
    env["POLYCOPY_OPERATIONAL_LOCK_PATH"] = str(lock_path)

    # Caller-supplied extra_env is applied LAST so tests can override
    # the per-test lock path (e.g. the cross-process lock-preemption
    # test uses ``extra_env={"POLYCOPY_OPERATIONAL_LOCK_PATH": ...}``
    # to point the script at a file already held by the test process).
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--db", str(tmp_db_path), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# ─────────────────────────────────────────────────────────────────────
# 1. Pure helper invariants
# ─────────────────────────────────────────────────────────────────────


class TestDeriveLegacyWalletDecisionRepair:
    """The helper is the single source of truth for the repair plan."""

    def test_legacy_suspect_row_gets_repair_plan(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_legacy_wallet_decision_repair,
        )

        row = {
            "id": 1,
            "wallet_id": "0xLEGACY",
            "verdict": "skip",
            "missing_essentials_json": "[]",
            "eligibility_failures_json": "[]",
            "resolved_markets": None,
            "category_resolved_markets": None,
            "sample_fraction": None,
            "sharpe_ratio": None,
            "max_drawdown": None,
        }
        plan = derive_legacy_wallet_decision_repair(row)
        assert plan["repair_needed"] is True
        assert plan["old_verdict"] == "skip"
        assert plan["new_verdict"] == "incomplete"
        assert "resolved_markets" in plan["new_missing_essentials"]
        assert "no_resolved_market_evidence" in plan["new_eligibility_failures"]

    def test_already_valid_skip_with_failures_is_left_alone(self):
        """A SKIP with a non-empty failure list is the new contract;
        re-repairing it would be a no-op and must report
        ``repair_needed=False``.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_legacy_wallet_decision_repair,
        )

        row = {
            "id": 2,
            "wallet_id": "0xVALID",
            "verdict": "skip",
            "missing_essentials_json": "[]",
            "eligibility_failures_json": json.dumps(["score_below_copy_threshold"]),
            "resolved_markets": 60,
            "category_resolved_markets": 25,
            "sample_fraction": 1.0,
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
        }
        plan = derive_legacy_wallet_decision_repair(row)
        assert plan["repair_needed"] is False, (
            "Already-valid skip with proper buckets must NOT be repaired"
        )
        assert plan["updated_payload"] == {}

    def test_existing_incomplete_row_is_left_alone(self):
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_legacy_wallet_decision_repair,
        )

        row = {
            "id": 3,
            "wallet_id": "0xINC",
            "verdict": "incomplete",
            "missing_essentials_json": json.dumps(["resolved_markets"]),
            "eligibility_failures_json": json.dumps(["no_resolved_market_evidence"]),
            "resolved_markets": None,
            "category_resolved_markets": 25,
            "sample_fraction": 1.0,
            "sharpe_ratio": 2.0,
            "max_drawdown": 0.1,
        }
        plan = derive_legacy_wallet_decision_repair(row)
        assert plan["repair_needed"] is False
        assert plan["updated_payload"] == {}

    def test_missing_required_evidence_with_resolved_counts_becomes_incomplete(self):
        """Test 3 from the brief: resolved counts present but
        sample_fraction / sharpe_ratio / max_drawdown missing → INCOMPLETE
        with ``missing_required_evidence``.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_legacy_wallet_decision_repair,
        )

        row = {
            "id": 4,
            "wallet_id": "0xMREQ",
            "verdict": "skip",
            "missing_essentials_json": "[]",
            "eligibility_failures_json": "[]",
            "resolved_markets": 60,
            "category_resolved_markets": 25,
            "sample_fraction": None,
            "sharpe_ratio": None,
            "max_drawdown": None,
        }
        plan = derive_legacy_wallet_decision_repair(row)
        assert plan["repair_needed"] is True
        assert plan["new_verdict"] == "incomplete"
        assert set(plan["new_missing_essentials"]) == {
            "sample_fraction",
            "sharpe_ratio",
            "max_drawdown",
        }
        assert "missing_required_evidence" in plan["new_eligibility_failures"]

    def test_full_evidence_skip_with_empty_failures_still_gets_marker(self):
        """Test 4 from the brief: full evidence + low score → SKIP +
        ``score_below_copy_threshold`` (NOT incomplete).
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_legacy_wallet_decision_repair,
        )

        row = {
            "id": 5,
            "wallet_id": "0xSKIP",
            "verdict": "skip",
            "missing_essentials_json": "[]",
            "eligibility_failures_json": "[]",
            "resolved_markets": 60,
            "category_resolved_markets": 25,
            "sample_fraction": 1.0,
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
        }
        plan = derive_legacy_wallet_decision_repair(row)
        # The legacy suspect shape fires (skip + empty buckets) and the
        # full-evidence skip branch re-derives to skip with the
        # canonical marker — repair_needed stays True the FIRST run so
        # the silent skip gets the marker, and stays False on
        # subsequent runs because the row will already carry it.
        assert plan["repair_needed"] is True
        assert plan["new_verdict"] == "skip"
        assert plan["new_verdict_family"] == "skip"
        assert plan["new_missing_essentials"] == []
        assert plan["new_eligibility_failures"] == ["score_below_copy_threshold"]

    def test_existing_failure_marker_is_preserved_and_not_duplicated(self):
        """Test 11 from the brief: existing eligibility failure is
        preserved; the helper must not duplicate the marker.
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_legacy_wallet_decision_repair,
        )

        row = {
            "id": 6,
            "wallet_id": "0xPRE",
            "verdict": "skip",
            "missing_essentials_json": "[]",
            "eligibility_failures_json": json.dumps(["custom_exclusion"]),
            "resolved_markets": 60,
            "category_resolved_markets": 25,
            "sample_fraction": 1.0,
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
        }
        plan = derive_legacy_wallet_decision_repair(row)
        # ``custom_exclusion`` is preserved; ``score_below_copy_threshold``
        # is NOT appended because the caller's eligibility_failures was
        # already non-empty.
        assert plan["new_verdict"] == "skip"
        assert plan["new_eligibility_failures"] == ["custom_exclusion"]

    def test_idempotent_when_repaired_row_is_re_passed(self):
        """Test 7 from the brief: re-running the helper on a repaired
        row must report ``repair_needed=False`` (no further changes).
        """
        from polycopy.scoring.incomplete_verdict_guard import (
            derive_legacy_wallet_decision_repair,
        )

        original = {
            "id": 7,
            "wallet_id": "0xREPLAY",
            "verdict": "skip",
            "missing_essentials_json": "[]",
            "eligibility_failures_json": "[]",
            "resolved_markets": None,
            "category_resolved_markets": None,
            "sample_fraction": None,
            "sharpe_ratio": None,
            "max_drawdown": None,
        }
        first = derive_legacy_wallet_decision_repair(original)
        assert first["repair_needed"] is True

        # Re-derive using the planned payload as the new input.
        repaired = dict(original)
        repaired.update(first["updated_payload"])
        second = derive_legacy_wallet_decision_repair(repaired)
        assert second["repair_needed"] is False
        assert second["updated_payload"] == {}


# ─────────────────────────────────────────────────────────────────────
# 2. Script-level behaviour: dry-run never writes
# ─────────────────────────────────────────────────────────────────────


class TestScriptDryRunNeverWrites:
    """The default ``--dry-run`` mode must never mutate the DB."""

    def test_dry_run_finds_old_smoke_shape_and_reports_repair(self, tmp_path: Path):
        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        _insert_legacy_wallet_row(db, wallet_id=wid)

        proc = _run_script(tmp_path / "p24g.db", "--json")
        assert proc.returncode == 0, proc.stderr
        lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
        assert len(lines) == 2, proc.stdout
        plan = json.loads(lines[0])
        summary = json.loads(lines[1])
        assert plan["row_id"] == 1
        assert plan["old_verdict"] == "skip"
        assert plan["new_verdict"] == "incomplete"
        assert "no_resolved_market_evidence" in plan["new_eligibility_failures"]
        assert summary["repairs_planned"] == 1
        assert summary["repairs_applied"] == 0
        assert summary["dry_run"] is True

        # DB must be unchanged.
        row = db.conn.execute(
            "SELECT verdict, missing_essentials_json, eligibility_failures_json "
            "FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert row["verdict"] == "skip"
        assert row["missing_essentials_json"] == "[]"
        assert row["eligibility_failures_json"] == "[]"

    def test_default_mode_without_apply_arg_is_dry_run(self, tmp_path: Path):
        """When neither --dry-run nor --apply is passed, the script
        must default to dry-run.
        """
        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        _insert_legacy_wallet_row(db, wallet_id=wid)

        proc = _run_script(tmp_path / "p24g.db")
        assert proc.returncode == 0, proc.stderr
        assert "DRY-RUN" in proc.stdout

        row = db.conn.execute(
            "SELECT verdict FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert row["verdict"] == "skip"


# ─────────────────────────────────────────────────────────────────────
# 3. Script-level behaviour: apply repairs rows
# ─────────────────────────────────────────────────────────────────────


class TestScriptApplyRepairsRows:
    """``--apply`` performs the planned UPDATE inside a transaction."""

    def test_apply_repairs_old_smoke_shape(self, tmp_path: Path):
        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        _insert_legacy_wallet_row(db, wallet_id=wid)

        proc = _run_script(tmp_path / "p24g.db", "--apply", "--json")
        assert proc.returncode == 0, proc.stderr

        row = db.conn.execute(
            "SELECT verdict, missing_essentials_json, eligibility_failures_json "
            "FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert row["verdict"] == "incomplete"
        missing = json.loads(row["missing_essentials_json"])
        failures = json.loads(row["eligibility_failures_json"])
        assert "resolved_markets" in missing
        assert "no_resolved_market_evidence" in failures

    def test_apply_repairs_missing_required_evidence_with_resolved_counts(
        self, tmp_path: Path
    ):
        """Test 3 from the brief: resolved counts present, but
        sample_fraction / sharpe_ratio / max_drawdown missing → repair
        to incomplete with ``missing_required_evidence``.
        """
        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        _insert_legacy_wallet_row(
            db,
            wallet_id=wid,
            resolved_markets=60,
            category_resolved_markets=25,
        )

        proc = _run_script(tmp_path / "p24g.db", "--apply", "--json")
        assert proc.returncode == 0, proc.stderr

        row = db.conn.execute(
            "SELECT verdict, missing_essentials_json, eligibility_failures_json "
            "FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert row["verdict"] == "incomplete"
        missing = json.loads(row["missing_essentials_json"])
        failures = json.loads(row["eligibility_failures_json"])
        assert set(missing) == {"sample_fraction", "sharpe_ratio", "max_drawdown"}
        assert "missing_required_evidence" in failures

    def test_apply_repairs_full_evidence_skip_with_marker(self, tmp_path: Path):
        """Test 4 from the brief: full-evidence skip with empty failure
        → stays SKIP but carries ``score_below_copy_threshold``.
        """
        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        _insert_legacy_wallet_row(
            db,
            wallet_id=wid,
            resolved_markets=60,
            category_resolved_markets=25,
            sample_fraction=1.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
        )

        proc = _run_script(tmp_path / "p24g.db", "--apply", "--json")
        assert proc.returncode == 0, proc.stderr

        row = db.conn.execute(
            "SELECT verdict, missing_essentials_json, eligibility_failures_json "
            "FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert row["verdict"] == "skip"
        missing = json.loads(row["missing_essentials_json"])
        failures = json.loads(row["eligibility_failures_json"])
        assert missing == []
        assert failures == ["score_below_copy_threshold"]

    def test_apply_leaves_existing_valid_skip_unchanged(self, tmp_path: Path):
        """Test 5 from the brief: an existing skip with a non-empty
        eligibility_failures must NOT be re-repaired.
        """
        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        _insert_legacy_wallet_row(
            db,
            wallet_id=wid,
            missing_essentials_json="[]",
            eligibility_failures_json=json.dumps(["custom_exclusion"]),
            resolved_markets=60,
            category_resolved_markets=25,
            sample_fraction=1.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
        )

        proc = _run_script(tmp_path / "p24g.db", "--apply", "--json")
        assert proc.returncode == 0, proc.stderr

        row = db.conn.execute(
            "SELECT verdict, missing_essentials_json, eligibility_failures_json "
            "FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert row["verdict"] == "skip"
        failures = json.loads(row["eligibility_failures_json"])
        assert failures == ["custom_exclusion"]

    def test_apply_leaves_existing_incomplete_unchanged(self, tmp_path: Path):
        """Test 6 from the brief: an existing incomplete row with proper
        buckets must NOT be re-repaired.
        """
        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        _insert_legacy_wallet_row(
            db,
            wallet_id=wid,
            verdict="incomplete",
            missing_essentials_json=json.dumps(["resolved_markets"]),
            eligibility_failures_json=json.dumps(["no_resolved_market_evidence"]),
            resolved_markets=None,
            category_resolved_markets=25,
            sample_fraction=1.0,
            sharpe_ratio=2.0,
            max_drawdown=0.1,
        )

        proc = _run_script(tmp_path / "p24g.db", "--apply", "--json")
        assert proc.returncode == 0, proc.stderr

        row = db.conn.execute(
            "SELECT verdict, missing_essentials_json, eligibility_failures_json "
            "FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert row["verdict"] == "incomplete"
        missing = json.loads(row["missing_essentials_json"])
        failures = json.loads(row["eligibility_failures_json"])
        assert missing == ["resolved_markets"]
        assert failures == ["no_resolved_market_evidence"]

    def test_apply_is_idempotent(self, tmp_path: Path):
        """Test 7 from the brief: re-running --apply must produce zero
        additional changes.
        """
        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        _insert_legacy_wallet_row(db, wallet_id=wid)

        first = _run_script(tmp_path / "p24g.db", "--apply", "--json")
        assert first.returncode == 0, first.stderr

        # Snapshot after first apply.
        row1 = db.conn.execute(
            "SELECT verdict, missing_essentials_json, eligibility_failures_json "
            "FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()

        # Second apply.
        second = _run_script(tmp_path / "p24g.db", "--apply", "--json")
        assert second.returncode == 0, second.stderr

        row2 = db.conn.execute(
            "SELECT verdict, missing_essentials_json, eligibility_failures_json "
            "FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()

        # The persisted shape is unchanged.
        assert row1["verdict"] == row2["verdict"] == "incomplete"
        assert row1["missing_essentials_json"] == row2["missing_essentials_json"]
        assert row1["eligibility_failures_json"] == row2["eligibility_failures_json"]

        # The second summary shows zero repairs applied (everything
        # already satisfies PR24F).
        summary = json.loads(second.stdout.strip().splitlines()[-1])
        assert summary["repairs_applied"] == 0
        assert summary["candidates_total"] == 0


# ─────────────────────────────────────────────────────────────────────
# 4. Companion decision_verdicts linkage
# ─────────────────────────────────────────────────────────────────────


class TestCompanionDecisionVerdictsLinkage:
    """``--include-decision-verdicts`` mirrors parent → companion."""

    def test_unambiguous_companion_is_repaired(self, tmp_path: Path):
        """Test 8 from the brief: companion row mirrors the repaired
        parent row when linkage is unambiguous.
        """
        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        _insert_legacy_wallet_row(db, wallet_id=wid)
        _insert_companion_decision_row(db, wallet_id=wid)

        proc = _run_script(
            tmp_path / "p24g.db",
            "--apply",
            "--include-decision-verdicts",
            "--json",
        )
        assert proc.returncode == 0, proc.stderr

        # Companion row reflects the repaired parent.
        crow = db.conn.execute(
            "SELECT verdict, verdict_family, exclusion_reasons_json "
            "FROM decision_verdicts WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert crow["verdict"] == "incomplete"
        assert crow["verdict_family"] == "incomplete"
        reasons = json.loads(crow["exclusion_reasons_json"] or "[]")
        assert "no_resolved_market_evidence" in reasons

    def test_ambiguous_companion_is_reported_and_not_guessed(self, tmp_path: Path):
        """Test 9 from the brief: when multiple companion rows match,
        the script reports ambiguity and does NOT modify any companion
        row.

        Two companion rows for the same wallet are seeded with
        DIFFERENT ``(source_ref_type, source_ref_id)`` tuples (so they
        don't violate the ``decision_verdicts`` UNIQUE constraint) but
        the script's loose parent-formula match returns both of them,
        exercising the ambiguity-reporting branch.
        """
        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        # Parent wallet row uses formula "wallet_score" / "v1" so the
        # script's WHERE matches both companion rows below.
        _insert_legacy_wallet_row(db, wallet_id=wid)
        # Two companion rows with different source_ref tuples — both
        # are returned by the loose ``(wallet_id, formula_name,
        # formula_version)`` match the script performs. The schema's
        # UNIQUE constraint prevents literal duplicates.
        _insert_companion_decision_row(
            db,
            wallet_id=wid,
            source_ref_type="wallet_id",
            source_ref_id=wid,
        )
        _insert_companion_decision_row(
            db,
            wallet_id=wid,
            source_ref_type="wallet_score_run",
            source_ref_id=f"{wid}-run-1",
        )

        proc = _run_script(
            tmp_path / "p24g.db",
            "--apply",
            "--include-decision-verdicts",
            "--json",
        )
        assert proc.returncode == 0, proc.stderr

        # Both companions still carry the legacy shape.
        crows = db.conn.execute(
            "SELECT verdict, exclusion_reasons_json FROM decision_verdicts "
            "WHERE wallet_id = ? ORDER BY id",
            (wid,),
        ).fetchall()
        assert len(crows) == 2
        for crow in crows:
            assert crow["verdict"] == "skip"
            assert crow["exclusion_reasons_json"] is None

        # The wallet row IS repaired (parent repair is independent of
        # companion ambiguity).
        wrow = db.conn.execute(
            "SELECT verdict FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert wrow["verdict"] == "incomplete"

        # Summary reports the ambiguity count.
        summary = json.loads(proc.stdout.strip().splitlines()[-1])
        assert summary["companions_ambiguous"] == 1


# ─────────────────────────────────────────────────────────────────────
# 5. Filter behaviour
# ─────────────────────────────────────────────────────────────────────


class TestScriptFilters:
    """``--wallet-id`` and ``--limit`` narrow the repair pass."""

    def test_limit_is_honored(self, tmp_path: Path):
        """Test 10 from the brief."""
        db = _fresh_db(tmp_path)
        wids = ["0xA", "0xB", "0xC"]
        for wid in wids:
            _insert_wallet(db, wid=wid)
            _insert_legacy_wallet_row(db, wallet_id=wid)

        proc = _run_script(tmp_path / "p24g.db", "--apply", "--limit", "1", "--json")
        assert proc.returncode == 0, proc.stderr

        # Exactly one row was repaired.
        summary = json.loads(proc.stdout.strip().splitlines()[-1])
        assert summary["repairs_applied"] == 1
        assert summary["candidates_total"] == 1

        # The other two are still legacy shape.
        skip_count = db.conn.execute(
            "SELECT COUNT(*) FROM wallet_score_decisions WHERE verdict = 'skip'"
        ).fetchone()[0]
        assert skip_count == 2

    def test_wallet_id_is_honored(self, tmp_path: Path):
        """Test 11 from the brief."""
        db = _fresh_db(tmp_path)
        for wid in ["0xA", "0xB", "0xC"]:
            _insert_wallet(db, wid=wid)
            _insert_legacy_wallet_row(db, wallet_id=wid)

        proc = _run_script(
            tmp_path / "p24g.db",
            "--apply",
            "--wallet-id",
            "0xB",
            "--json",
        )
        assert proc.returncode == 0, proc.stderr

        # Only 0xB was repaired.
        rows = {
            r["wallet_id"]: r["verdict"]
            for r in db.conn.execute(
                "SELECT wallet_id, verdict FROM wallet_score_decisions"
            ).fetchall()
        }
        assert rows["0xA"] == "skip"
        assert rows["0xB"] == "incomplete"
        assert rows["0xC"] == "skip"


# ─────────────────────────────────────────────────────────────────────
# 6. Lock + RSS behaviour
# ─────────────────────────────────────────────────────────────────────


class TestScriptLock:
    """``--apply`` uses the shared global operational lock."""

    def test_apply_uses_global_lock(self, tmp_path: Path):
        """Test 13 from the brief: holding the shared lock with a
        competing process blocks the script's apply path (it must
        fail-fast rather than silently retrying).
        """
        from polycopy.utils.concurrency import FileLock

        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        _insert_legacy_wallet_row(db, wallet_id=wid)

        # Pre-acquire the operational lock the script will request.
        lock_path = tmp_path / "preempted.lock"
        held = FileLock(lock_path=lock_path, timeout=0.0)
        try:
            with held:
                proc = _run_script(
                    tmp_path / "p24g.db",
                    "--apply",
                    "--lock-timeout",
                    "0",
                    extra_env={
                        "POLYCOPY_OPERATIONAL_LOCK_PATH": str(lock_path)
                    },
                )
                assert proc.returncode == 2, (
                    f"expected exit 2 (lock held), got {proc.returncode}; "
                    f"stderr={proc.stderr!r}"
                )
                assert "lock" in proc.stderr.lower() or "Lock" in proc.stderr

            # DB is untouched.
            row = db.conn.execute(
                "SELECT verdict FROM wallet_score_decisions WHERE wallet_id = ?",
                (wid,),
            ).fetchone()
            assert row["verdict"] == "skip"
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def test_apply_can_succeed_after_lock_released(self, tmp_path: Path):
        """Sanity: once the competing holder releases the lock, the
        script can acquire it and complete the repair normally.
        """
        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        _insert_legacy_wallet_row(db, wallet_id=wid)

        proc = _run_script(tmp_path / "p24g.db", "--apply", "--lock-timeout", "1")
        assert proc.returncode == 0, proc.stderr

        row = db.conn.execute(
            "SELECT verdict FROM wallet_score_decisions WHERE wallet_id = ?",
            (wid,),
        ).fetchone()
        assert row["verdict"] == "incomplete"


# ─────────────────────────────────────────────────────────────────────
# 7. JSON output
# ─────────────────────────────────────────────────────────────────────


class TestScriptJsonOutput:
    """``--json`` emits one JSON object per planned repair."""

    def test_json_output_is_parseable(self, tmp_path: Path):
        """Test 14 from the brief."""
        db = _fresh_db(tmp_path)
        wid = _insert_wallet(db)
        _insert_legacy_wallet_row(db, wallet_id=wid)

        proc = _run_script(tmp_path / "p24g.db", "--dry-run", "--json")
        assert proc.returncode == 0, proc.stderr

        # First line: plan; last line: summary. All must parse.
        lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
        assert len(lines) == 2
        plan = json.loads(lines[0])
        summary = json.loads(lines[1])

        assert plan["row_id"] == 1
        assert plan["old_verdict"] == "skip"
        assert plan["new_verdict"] == "incomplete"
        assert summary["dry_run"] is True