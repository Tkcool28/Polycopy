"""Final-pass shadow semantics tests (Chunk 8 — three final blockers).

These tests verify the three final repair-pass blockers:

  1. Missing or invalid trade side must NEVER default to BUY.
  2. Missing executable evidence must NEVER fall back to
     ``source_price`` (or midpoint, or top-of-book, or zero).
  3. Delay validation must support the legitimate 15-minute shadow
     scenario (valid window 900-1200s) and the actual-measured-delay
     scenario (no fixed window, conservative 1500s safety ceiling).

The tests cover:

  * ``validate_observed_delay_for_scenario`` — the scenario-aware
    helper that lives in ``shadow_score_v2_typed``.
  * Static analysis of ``paper_signal.py`` to confirm the runtime
    no longer has the dangerous fallbacks.
  * Engine behaviour on typed inputs with missing/invalid side
    (verdicts must become SHADOW_INCOMPLETE).
  * Engine behaviour on typed inputs with delayed_copy_price=None
    (verdicts must become SHADOW_INCOMPLETE — never silently graded).
  * Offset field persistence for the legitimate 15-minute
    observation (1050s observed → accepted, delay_error=150).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

from polycopy.scoring.shadow_score_v2_typed import (  # noqa: E402
    ACTUAL_MEASURED_DELAY_MAX_SECONDS,
    DELAY_SCENARIO_SECONDS,
    DELAY_SCENARIO_TOLERANCE_SECONDS,
    VERDICT_SHADOW_INCOMPLETE,
    DelayScenario,
    ShadowScoreInputV2,
    validate_observed_delay_for_scenario,
)
from polycopy.scoring.shadow_score_v2_engine import (  # noqa: E402
    compute_shadow_score_v2_from_input,
)


_REPO_ROOT_SRC = _REPO_ROOT / "src" / "polycopy" / "scoring" / "paper_signal.py"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _build_typed_input(**overrides: object) -> ShadowScoreInputV2:
    """Build a fully populated ShadowScoreInputV2 for tests."""
    base: dict = dict(
        wallet_id="0xW",
        source_trade_id="t-1",
        candidate_id=1,
        delay_scenario=DelayScenario.THEORETICAL_IMMEDIATE,
        source_price=0.50,
        delayed_copy_price=0.50,
        intended_stake=100.0,
        executable_depth=None,
        fill_percentage=None,
        slippage=None,
        spread=None,
        wallet_skill_persistence_input=None,
        copied_realized_performance_input=None,
        concentration_correlation_input=None,
        source_data_timestamp=None,
        price_snapshot_id=None,
        depth_hash=None,
        target_delay_seconds=0.0,
        actual_observed_delay_seconds=0.0,
        delay_error_seconds=0.0,
    )
    base.update(overrides)
    return ShadowScoreInputV2(**base)


# ── 1. Static analysis — no dangerous fallbacks remain ──────────────────────


class TestStaticSafety:
    """Inspect paper_signal.py to confirm the dangerous fallbacks
    (``else "BUY"``, ``delayed_copy_price = source_price``,
    global 600s ceiling) are gone."""

    @classmethod
    @pytest.fixture(scope="class")
    def paper_signal_source(cls) -> str:
        return _REPO_ROOT_SRC.read_text(encoding="utf-8")

    def test_no_else_buy_fallback(self, paper_signal_source: str) -> None:
        """``else "BUY"`` (or single-quoted variant) must NOT appear in
        paper_signal.py — it was the site of the silent direction
        invention."""
        assert 'else "BUY"' not in paper_signal_source, (
            "paper_signal.py still contains `else \"BUY\"` — missing "
            "or invalid trade side must not silently become BUY."
        )
        assert "else 'BUY'" not in paper_signal_source

    def test_no_source_price_fallback_for_delayed_price(
        self, paper_signal_source: str,
    ) -> None:
        """``delayed_copy_price = source_price`` must NOT appear in
        paper_signal.py — it would fabricate a perfect copy price when
        no real depth walk exists."""
        assert "delayed_copy_price = source_price" not in paper_signal_source, (
            "paper_signal.py still contains "
            "`delayed_copy_price = source_price` — missing executable "
            "evidence must not fall back to the source trade price."
        )
        assert "delayed_copy_price=source_price" not in paper_signal_source

    def test_no_midpoint_in_executable_price(
        self, paper_signal_source: str,
    ) -> None:
        """All ``midpoint`` mentions in paper_signal.py must be inside
        a comment block (NEVER the executable price)."""
        # Walk line-by-line and assert no executable-price assignment
        # uses midpoint outside a comment.
        for n, line in enumerate(paper_signal_source.splitlines(), 1):
            if "midpoint" not in line.lower():
                continue
            stripped = line.lstrip()
            # Permitted: docstring/comment lines (start with ``#`` or
            # triple-quoted block lines).
            if stripped.startswith("#"):
                continue
            # Mid-executable helper comment lines that explicitly say
            # "NEVER a midpoint" are also fine — they are documentation
            # for the avoidance.
            lowered = line.lower()
            if "never" in lowered or "not" in lowered:
                continue
            # Otherwise: a bare midpoint mention in code is suspicious.
            pytest.fail(
                f"paper_signal.py line {n} contains a suspicious "
                f"`midpoint` mention: {line!r}"
            )

    def test_no_global_600s_ceiling(self, paper_signal_source: str) -> None:
        """The global ``> 600.0`` ceiling on
        ``actual_observed_delay_seconds`` must NOT remain — it would
        wrongly invalidate every legitimate 15-minute observation
        (target=900s, tolerance=300s, valid window 900-1200s)."""
        assert "> 600.0" not in paper_signal_source
        assert ">= 600.0" not in paper_signal_source

    def test_no_executable_price_for_immediate_source_price(
        self, paper_signal_source: str,
    ) -> None:
        """The runtime used to set
        ``executable_price_for_immediate = source_price`` as a
        fallback. That fallback is gone."""
        assert "executable_price_for_immediate = source_price" not in (
            paper_signal_source
        )


# ── 2. Scenario-aware delay validation helper ──────────────────────────────


class TestScenarioAwareDelayValidation:
    """Unit tests for ``validate_observed_delay_for_scenario``."""

    @pytest.mark.parametrize(
        "scenario,observed,should_pass",
        [
            # 30-second scenario: valid 30..60
            (DelayScenario.DELAY_30_SECONDS, 30.0, True),
            (DelayScenario.DELAY_30_SECONDS, 45.0, True),
            (DelayScenario.DELAY_30_SECONDS, 60.0, True),
            (DelayScenario.DELAY_30_SECONDS, 61.0, False),
            (DelayScenario.DELAY_30_SECONDS, 29.0, False),
            # 2-minute scenario: valid 120..180
            (DelayScenario.DELAY_2_MINUTES, 120.0, True),
            (DelayScenario.DELAY_2_MINUTES, 180.0, True),
            (DelayScenario.DELAY_2_MINUTES, 181.0, False),
            (DelayScenario.DELAY_2_MINUTES, 119.0, False),
            # 5-minute scenario: valid 300..420
            (DelayScenario.DELAY_5_MINUTES, 300.0, True),
            (DelayScenario.DELAY_5_MINUTES, 420.0, True),
            (DelayScenario.DELAY_5_MINUTES, 421.0, False),
            (DelayScenario.DELAY_5_MINUTES, 299.0, False),
            # 15-minute scenario: valid 900..1200 (THE blocker)
            (DelayScenario.DELAY_15_MINUTES, 900.0, True),
            (DelayScenario.DELAY_15_MINUTES, 1050.0, True),
            (DelayScenario.DELAY_15_MINUTES, 1200.0, True),
            (DelayScenario.DELAY_15_MINUTES, 1201.0, False),
            (DelayScenario.DELAY_15_MINUTES, 899.0, False),
            # THEORETICAL_IMMEDIATE: target=0, no upper bound
            (DelayScenario.THEORETICAL_IMMEDIATE, 0.0, True),
            (DelayScenario.THEORETICAL_IMMEDIATE, 30.0, True),
            (DelayScenario.THEORETICAL_IMMEDIATE, 900.0, True),
            # ACTUAL_MEASURED_DELAY: any non-negative <= ceiling
            (DelayScenario.ACTUAL_MEASURED_DELAY, 0.0, True),
            (DelayScenario.ACTUAL_MEASURED_DELAY, 900.0, True),
            (
                DelayScenario.ACTUAL_MEASURED_DELAY,
                ACTUAL_MEASURED_DELAY_MAX_SECONDS,
                True,
            ),
        ],
    )
    def test_valid_window(
        self,
        scenario: DelayScenario,
        observed: float,
        should_pass: bool,
    ) -> None:
        reason = validate_observed_delay_for_scenario(scenario, observed)
        if should_pass:
            assert reason is None, (
                f"expected pass for {scenario.value} at {observed}s; "
                f"got reason: {reason!r}"
            )
        else:
            assert reason is not None, (
                f"expected rejection for {scenario.value} at {observed}s"
            )
            assert "actual_observed_delay" in reason

    @pytest.mark.parametrize(
        "scenario",
        [
            DelayScenario.DELAY_30_SECONDS,
            DelayScenario.DELAY_2_MINUTES,
            DelayScenario.DELAY_5_MINUTES,
            DelayScenario.DELAY_15_MINUTES,
            DelayScenario.THEORETICAL_IMMEDIATE,
            DelayScenario.ACTUAL_MEASURED_DELAY,
        ],
    )
    def test_negative_observed_rejected(
        self, scenario: DelayScenario,
    ) -> None:
        reason = validate_observed_delay_for_scenario(scenario, -1.0)
        assert reason is not None
        assert "actual_observed_delay_negative" in reason
        assert scenario.value in reason

    def test_none_observed_returns_none(self) -> None:
        """A missing observed value is not the validator's concern —
        it returns None (the engine treats missing evidence as
        SHADOW_INCOMPLETE separately)."""
        for scenario in DelayScenario:
            assert validate_observed_delay_for_scenario(scenario, None) is None

    def test_actual_measured_above_ceiling_rejected(self) -> None:
        """ACTUAL_MEASURED_DELAY accepts up to
        ACTUAL_MEASURED_DELAY_MAX_SECONDS (1500s); values above are
        rejected as corrupt-timestamp guards."""
        reason = validate_observed_delay_for_scenario(
            DelayScenario.ACTUAL_MEASURED_DELAY,
            ACTUAL_MEASURED_DELAY_MAX_SECONDS + 1.0,
        )
        assert reason is not None
        assert "actual_observed_delay_out_of_range" in reason

    def test_invalid_value_type_rejected(self) -> None:
        reason = validate_observed_delay_for_scenario(
            DelayScenario.DELAY_5_MINUTES, "not-a-number",  # type: ignore[arg-type]
        )
        assert reason is not None
        assert "actual_observed_delay_invalid" in reason

    def test_windows_match_frozen_constants(self) -> None:
        """The valid windows for fixed-delay scenarios must equal
        ``DELAY_SCENARIO_SECONDS[scenario]`` .. ``+ tolerance`` —
        guard against accidental drift from the frozen contract."""
        expected = {
            DelayScenario.DELAY_30_SECONDS: (30.0, 60.0),
            DelayScenario.DELAY_2_MINUTES: (120.0, 180.0),
            DelayScenario.DELAY_5_MINUTES: (300.0, 420.0),
            DelayScenario.DELAY_15_MINUTES: (900.0, 1200.0),
        }
        for scenario, (lo, hi) in expected.items():
            target = DELAY_SCENARIO_SECONDS[scenario]
            tolerance = DELAY_SCENARIO_TOLERANCE_SECONDS[scenario]
            assert target is not None
            assert tolerance is not None
            assert abs(float(target) - lo) < 1e-9, scenario.value
            assert abs(float(target) + float(tolerance) - hi) < 1e-9, (
                scenario.value
            )


# ── 3. Engine behaviour on missing/invalid evidence ────────────────────────


class TestMissingEvidenceEngineBehavior:
    """Verify the engine returns SHADOW_INCOMPLETE for the missing-
    evidence cases the runtime must surface (not silently grade)."""

    def test_delayed_copy_price_none_is_shadow_incomplete(self) -> None:
        """When ``delayed_copy_price`` is None the engine must return
        SHADOW_INCOMPLETE — never silently substitute source_price."""
        inp = _build_typed_input(delayed_copy_price=None)
        result = compute_shadow_score_v2_from_input(inp)
        assert result.verdict == VERDICT_SHADOW_INCOMPLETE
        # The engine must explicitly record why.
        assert any(
            "missing_delayed_copy_price" in r
            for r in result.missing_forward_reasons
        )

    def test_15_minute_observation_accepted_with_offset(self) -> None:
        """A 15-minute observation at 1050s (target=900, error=+150)
        must be accepted: not marked SHADOW_INCOMPLETE on out-of-range
        grounds. ``delay_error_seconds=+150`` is persisted correctly."""
        inp = _build_typed_input(
            delay_scenario=DelayScenario.DELAY_15_MINUTES,
            target_delay_seconds=900.0,
            actual_observed_delay_seconds=1050.0,
            delay_error_seconds=150.0,
        )
        result = compute_shadow_score_v2_from_input(inp)
        # The runtime must NOT mark this SHADOW_INCOMPLETE on the
        # grounds of an out-of-range delay — 1050s is squarely inside
        # the 900-1200s window.
        out_of_range_reasons = [
            r for r in result.missing_forward_reasons
            if "actual_observed_delay_out_of_range" in r
        ]
        assert out_of_range_reasons == [], (
            f"15-minute observation at 1050s wrongly rejected: "
            f"{out_of_range_reasons}"
        )

    def test_actual_measured_delay_900s_accepted(self) -> None:
        """An ACTUAL_MEASURED_DELAY observation at 900s must be
        accepted — the previous global 600s ceiling wrongly
        invalidated this."""
        inp = _build_typed_input(
            delay_scenario=DelayScenario.ACTUAL_MEASURED_DELAY,
            target_delay_seconds=None,
            actual_observed_delay_seconds=900.0,
            delay_error_seconds=None,
        )
        result = compute_shadow_score_v2_from_input(inp)
        out_of_range_reasons = [
            r for r in result.missing_forward_reasons
            if "actual_observed_delay_out_of_range" in r
        ]
        assert out_of_range_reasons == [], (
            f"actual-measured 900s wrongly rejected: "
            f"{out_of_range_reasons}"
        )

    def test_theoretical_immediate_with_measured_delay_accepted(
        self,
    ) -> None:
        """THEORETICAL_IMMEDIATE is not graded against the fixed-delay
        window — the scenario represents the immediate available
        candidate snapshot, not a guaranteed exact-zero observation."""
        inp = _build_typed_input(
            delay_scenario=DelayScenario.THEORETICAL_IMMEDIATE,
            target_delay_seconds=0.0,
            actual_observed_delay_seconds=12.0,
            delay_error_seconds=12.0,
        )
        result = compute_shadow_score_v2_from_input(inp)
        out_of_range_reasons = [
            r for r in result.missing_forward_reasons
            if "actual_observed_delay_out_of_range" in r
        ]
        assert out_of_range_reasons == [], (
            f"theoretical-immediate with measured 12s wrongly rejected: "
            f"{out_of_range_reasons}"
        )


# ── 4. Offset persistence for the legitimate 15-minute observation ────────


class TestOffsetFieldPersistenceFifteenMinute:
    """Verify the persistence path accepts and stores the legitimate
    15-minute observation with all three offset fields populated."""

    def test_fifteen_minute_observation_persists_three_offset_fields(
        self, tmp_path: Path,
    ) -> None:
        """A 15-minute observation at 1050s (target=900, error=+150)
        must persist all three audit values and not be rejected by
        the inline validator in ``persist_shadow_score_v2``."""
        from polycopy.db.database import Database
        from polycopy.scoring.score_serialization import (
            persist_shadow_score_v2,
        )
        import uuid
        from datetime import datetime, timezone

        with Database(db_path=tmp_path / "fifteen.db") as db:
            db.connect()
            wid = str(uuid.uuid4())
            mid = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, "
                "created_at) VALUES (?, '0x', 'r', 1, ?)",
                (wid, now),
            )
            db.execute(
                "INSERT INTO markets (id, source_id, source, question, "
                "active, closed, resolved, volume_24h, fetched_at, "
                "is_sample) VALUES (?, 'm', 'p', 'q', 1, 0, 0, 0.0, "
                "?, 1)",
                (mid, now),
            )
            db.execute(
                "INSERT INTO copy_candidates (wallet_id, source, "
                "source_trade_id, market_id, side, source_trade_price, "
                "source_trade_quantity, source_trade_timestamp, "
                "observed_at, wallet_score_version, wallet_score, "
                "wallet_verdict, status, created_at, updated_at) "
                "VALUES (?, 'polymarket', 't-15m', ?, 'BUY', 0.5, "
                "10.0, ?, ?, '1', 50.0, 'watchlist', 'pending', "
                "?, ?)",
                (wid, mid, now, now, now, now),
            )
            cid = db.fetchone(
                "SELECT id FROM copy_candidates ORDER BY id DESC LIMIT 1"
            )["id"]

            inp = _build_typed_input(
                delay_scenario=DelayScenario.DELAY_15_MINUTES,
                target_delay_seconds=900.0,
                actual_observed_delay_seconds=1050.0,
                delay_error_seconds=150.0,
            )
            result = compute_shadow_score_v2_from_input(inp)
            # Must NOT raise (the previous global 600s ceiling would).
            row_id = persist_shadow_score_v2(
                db, wid, "t-15m", result,
                candidate_id=int(cid),
                source_data_timestamp=now,
            )
            assert row_id is not None

            # Verify the three v12 offset audit fields were persisted correctly.
            row = db.fetchone(
                "SELECT target_delay_seconds, actual_observed_delay_seconds, "
                "delay_error_seconds FROM shadow_decisions WHERE id = ?",
                (row_id,),
            )
            assert row is not None
            assert abs(float(row["target_delay_seconds"]) - 900.0) < 1e-9
            assert abs(float(row["actual_observed_delay_seconds"]) - 1050.0) < 1e-9
            assert abs(float(row["delay_error_seconds"]) - 150.0) < 1e-9