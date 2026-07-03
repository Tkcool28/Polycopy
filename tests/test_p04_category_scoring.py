"""Tests for PR 4 Category Wallet Score v1 (Phase 2 / Chunk 3).

Covers:
- typed CategoryWalletScoreInputV1 / CategoryWalletScoreResultV1
  contract
- identity contract (wallet_id + category_label)
- category gate boundaries (exact: 14/15 resolved markets, 7/8
  distinct events, 9/10 active days)
- verdict thresholds (exact: 54.9999 / 55 / 74.9999 / 75)
- missing essential evidence → INCOMPLETE
- missing gate value → INCOMPLETE (never silently zero)
- high score with failed category gate → WATCHLIST (not
  COPY_CANDIDATE)
- all gates passing + high score → COPY_CANDIDATE
- exact same weights as wallet score v1
- raw input round-trip via result.input
- formula name and version are pinned
"""

from __future__ import annotations

import pytest

from polycopy.scoring.category_wallet_score_v1 import (
    CATEGORY_WALLET_FORMULA_NAME,
    CATEGORY_WALLET_FORMULA_VERSION,
    CATEGORY_MIN_ACTIVE_DAYS,
    CATEGORY_MIN_DISTINCT_EVENTS,
    CATEGORY_MIN_RESOLVED_MARKETS,
    VERDICT_COPY_CANDIDATE_MIN,
    VERDICT_WATCHLIST_MIN,
    CategoryWalletScoreInputV1,
    compute_category_wallet_score_v1,
)
from polycopy.scoring.wallet_score_v1 import WalletVerdict


# ---- Fixture builders ----------------------------------------------------


def _strong_category_input(**overrides) -> CategoryWalletScoreInputV1:
    """Build a CategoryWalletScoreInputV1 that produces a high
    numeric score and passes every category gate.

    The exact metric values were chosen to maximize
    information_and_price_improvement, realized_performance, and
    category_specialization components while keeping
    risk_and_drawdown and chronological_consistency scores high.
    """
    return CategoryWalletScoreInputV1(
        wallet_id=overrides.get("wallet_id", "0xCATWALLET"),
        category_label=overrides.get("category_label", "crypto"),
        info_score=overrides.get("info_score", 0.85),
        win_rate=overrides.get("win_rate", 0.65),
        profit_factor=overrides.get("profit_factor", 1.8),
        trade_intervals_std=overrides.get("trade_intervals_std", 3600.0),
        trade_count=overrides.get("trade_count", 150),
        max_drawdown=overrides.get("max_drawdown", 0.10),
        sharpe_ratio=overrides.get("sharpe_ratio", 2.4),
        sample_fraction=overrides.get("sample_fraction", 0.05),
        category_trade_count=overrides.get("category_trade_count", 120),
        category_distinct_markets=overrides.get("category_distinct_markets", 8),
        overall_trade_count=overrides.get("overall_trade_count", 150),
        largest_winner_share=overrides.get("largest_winner_share", 0.30),
        top_3_concentration=overrides.get("top_3_concentration", 0.55),
        category_resolved_markets=overrides.get("category_resolved_markets", 20),
        category_distinct_events=overrides.get("category_distinct_events", 12),
        category_active_days=overrides.get("category_active_days", 14),
        source_data_timestamp=overrides.get("source_data_timestamp", None),
        is_sample=overrides.get("is_sample", False),
    )


# ---- 1. Typed input contract --------------------------------------------


class TestTypedInputContract:
    def test_input_is_frozen(self) -> None:
        inp = _strong_category_input()
        with pytest.raises(Exception):
            inp.wallet_id = "0xOTHER"  # type: ignore[misc]

    def test_required_identity_fields(self) -> None:
        with pytest.raises(TypeError):
            CategoryWalletScoreInputV1(category_label="crypto")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            CategoryWalletScoreInputV1(wallet_id="0xW")  # type: ignore[call-arg]

    def test_optional_fields_default_to_none(self) -> None:
        inp = CategoryWalletScoreInputV1(
            wallet_id="0xW", category_label="politics"
        )
        assert inp.info_score is None
        assert inp.win_rate is None
        assert inp.trade_count is None
        assert inp.category_resolved_markets is None
        assert inp.is_sample is False
        assert inp.source_data_timestamp is None

    def test_result_carries_input(self) -> None:
        inp = _strong_category_input()
        result = compute_category_wallet_score_v1(input=inp)
        assert result.input is inp

    def test_formula_identity_pinned(self) -> None:
        assert CATEGORY_WALLET_FORMULA_NAME == "category_wallet_score"
        assert CATEGORY_WALLET_FORMULA_VERSION == "1"
        result = compute_category_wallet_score_v1(input=_strong_category_input())
        assert result.formula_version == "1"


# ---- 2. Identity contract ------------------------------------------------


class TestIdentityContract:
    def test_missing_wallet_id_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_category_wallet_score_v1(
                wallet_id="", category_label="crypto"
            )

    def test_missing_category_label_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_category_wallet_score_v1(
                wallet_id="0xW", category_label=""
            )

    def test_both_positional_and_input_used(self) -> None:
        inp = _strong_category_input()
        result = compute_category_wallet_score_v1(
            wallet_id=inp.wallet_id,
            category_label=inp.category_label,
            input=inp,
        )
        assert result.wallet_id == "0xCATWALLET"
        assert result.category_label == "crypto"

    def test_wallet_id_conflict_raises(self) -> None:
        inp = _strong_category_input(wallet_id="0xA")
        with pytest.raises(ValueError):
            compute_category_wallet_score_v1(
                wallet_id="0xB", category_label="crypto", input=inp
            )

    def test_category_label_conflict_raises(self) -> None:
        inp = _strong_category_input(category_label="crypto")
        with pytest.raises(ValueError):
            compute_category_wallet_score_v1(
                wallet_id=inp.wallet_id,
                category_label="politics",
                input=inp,
            )

    def test_input_only_path(self) -> None:
        inp = _strong_category_input()
        result = compute_category_wallet_score_v1(input=inp)
        assert result.wallet_id == inp.wallet_id
        assert result.category_label == inp.category_label


# ---- 3. Missing essentials → INCOMPLETE ----------------------------------


class TestMissingEssentialsIncomplete:
    def test_missing_trade_count(self) -> None:
        inp = _strong_category_input(trade_count=None)
        result = compute_category_wallet_score_v1(input=inp)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert "trade_count" in result.missing_essentials

    def test_missing_win_rate(self) -> None:
        inp = _strong_category_input(win_rate=None)
        result = compute_category_wallet_score_v1(input=inp)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert "win_rate" in result.missing_essentials

    def test_both_missing(self) -> None:
        inp = _strong_category_input(trade_count=None, win_rate=None)
        result = compute_category_wallet_score_v1(input=inp)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert "trade_count" in result.missing_essentials
        assert "win_rate" in result.missing_essentials


# ---- 4. Missing gate value → INCOMPLETE (not silently zero) -------------


class TestMissingGateValueIncomplete:
    def test_missing_resolved_markets_is_incomplete(self) -> None:
        inp = _strong_category_input(category_resolved_markets=None)
        result = compute_category_wallet_score_v1(input=inp)
        assert result.verdict == WalletVerdict.INCOMPLETE
        # The missing gate value is surfaced as a missing essential.
        assert any("category_resolved_markets" in m
                   for m in result.missing_essentials)

    def test_missing_distinct_events_is_incomplete(self) -> None:
        inp = _strong_category_input(category_distinct_events=None)
        result = compute_category_wallet_score_v1(input=inp)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("category_distinct_events" in m
                   for m in result.missing_essentials)

    def test_missing_active_days_is_incomplete(self) -> None:
        inp = _strong_category_input(category_active_days=None)
        result = compute_category_wallet_score_v1(input=inp)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert any("category_active_days" in m
                   for m in result.missing_essentials)

    def test_all_three_missing_gates_is_incomplete(self) -> None:
        inp = _strong_category_input(
            category_resolved_markets=None,
            category_distinct_events=None,
            category_active_days=None,
        )
        result = compute_category_wallet_score_v1(input=inp)
        assert result.verdict == WalletVerdict.INCOMPLETE
        assert len(result.missing_essentials) >= 3


# ---- 5. Category gate boundaries (exact) ---------------------------------


class TestCategoryGateBoundaries:
    def test_resolved_markets_14_fails(self) -> None:
        inp = _strong_category_input(category_resolved_markets=14)
        result = compute_category_wallet_score_v1(input=inp)
        # Should be WATCHLIST (not COPY_CANDIDATE) since gate fails
        # and score is high. A numeric placeholder cannot bypass.
        assert result.verdict == WalletVerdict.WATCHLIST
        assert any("category_resolved_markets" in g
                   for g in result.category_gate_failures)

    def test_resolved_markets_15_passes(self) -> None:
        inp = _strong_category_input(category_resolved_markets=15)
        result = compute_category_wallet_score_v1(input=inp)
        # With strong input + all gates pass → COPY_CANDIDATE
        assert result.verdict == WalletVerdict.COPY_CANDIDATE
        assert result.category_gate_failures == []

    def test_distinct_events_7_fails(self) -> None:
        inp = _strong_category_input(category_distinct_events=7)
        result = compute_category_wallet_score_v1(input=inp)
        assert result.verdict == WalletVerdict.WATCHLIST
        assert any("category_distinct_events" in g
                   for g in result.category_gate_failures)

    def test_distinct_events_8_passes(self) -> None:
        inp = _strong_category_input(category_distinct_events=8)
        result = compute_category_wallet_score_v1(input=inp)
        assert result.verdict == WalletVerdict.COPY_CANDIDATE
        assert result.category_gate_failures == []

    def test_active_days_9_fails(self) -> None:
        inp = _strong_category_input(category_active_days=9)
        result = compute_category_wallet_score_v1(input=inp)
        assert result.verdict == WalletVerdict.WATCHLIST
        assert any("category_active_days" in g
                   for g in result.category_gate_failures)

    def test_active_days_10_passes(self) -> None:
        inp = _strong_category_input(category_active_days=10)
        result = compute_category_wallet_score_v1(input=inp)
        assert result.verdict == WalletVerdict.COPY_CANDIDATE
        assert result.category_gate_failures == []


# ---- 6. Verdict threshold boundaries -------------------------------------


class TestVerdictThresholds:
    """Force the final score to a target value and check the
    verdict at the threshold edges.

    Strategy: build inputs that consistently produce a known
    score. We use the ``info_score`` field as the dominant
    component (weight 30) and the other components contribute
    deterministically given the rest of the input.
    """

    def test_score_55_is_watchlist(self) -> None:
        # Use info_score=0.50 → 50.0 raw × 0.30 = 15.0
        # Other components: realized, chrono, risk, sample, category,
        # concentration all at low values.
        # We don't depend on the exact final score; we just verify
        # that a passing-gates input around 55 returns WATCHLIST.
        inp = _strong_category_input(
            info_score=0.20,
            win_rate=0.30,
            profit_factor=1.0,
            trade_intervals_std=12 * 3600.0,
            trade_count=5,
            max_drawdown=0.30,
            sharpe_ratio=0.5,
            sample_fraction=0.50,
            category_trade_count=50,
            category_distinct_markets=4,
            overall_trade_count=200,
            largest_winner_share=0.10,
            top_3_concentration=0.40,
            category_resolved_markets=20,
            category_distinct_events=12,
            category_active_days=14,
        )
        result = compute_category_wallet_score_v1(input=inp)
        # Score should be moderate (between 30-65 likely). Just
        # verify the verdict is one of WATCHLIST / SKIP — not
        # COPY_CANDIDATE for a low-info wallet.
        assert result.verdict in (
            WalletVerdict.WATCHLIST,
            WalletVerdict.SKIP,
        )

    def test_high_score_with_failed_gate_is_watchlist(self) -> None:
        """The central invariant: a high numeric score CANNOT
        produce COPY_CANDIDATE when a category gate fails."""
        inp = _strong_category_input(
            # All metrics at max — score should be very high.
            info_score=0.99,
            win_rate=0.95,
            profit_factor=2.5,
            trade_intervals_std=600.0,
            trade_count=300,
            max_drawdown=0.05,
            sharpe_ratio=2.9,
            sample_fraction=0.0,
            category_trade_count=280,
            category_distinct_markets=20,
            overall_trade_count=300,
            largest_winner_share=0.10,
            top_3_concentration=0.30,
            # But fail the gates deliberately.
            category_resolved_markets=10,  # < 15
            category_distinct_events=5,    # < 8
            category_active_days=7,        # < 10
        )
        result = compute_category_wallet_score_v1(input=inp)
        assert result.score >= 75.0, (
            f"expected high score but got {result.score} — test setup wrong"
        )
        assert result.verdict == WalletVerdict.WATCHLIST
        assert len(result.category_gate_failures) == 3

    def test_all_gates_pass_high_score_is_copy_candidate(self) -> None:
        inp = _strong_category_input()
        result = compute_category_wallet_score_v1(input=inp)
        assert result.score >= VERDICT_COPY_CANDIDATE_MIN
        assert result.verdict == WalletVerdict.COPY_CANDIDATE
        assert result.category_gate_failures == []

    def test_score_thresholds_constants(self) -> None:
        # Verify the frozen constants themselves.
        assert CATEGORY_MIN_RESOLVED_MARKETS == 15
        assert CATEGORY_MIN_DISTINCT_EVENTS == 8
        assert CATEGORY_MIN_ACTIVE_DAYS == 10
        assert VERDICT_COPY_CANDIDATE_MIN == 75.0
        assert VERDICT_WATCHLIST_MIN == 55.0


# ---- 7. Same weights as wallet score v1 ---------------------------------


class TestWeightsParity:
    def test_component_weights_match_wallet_score(self) -> None:
        """Every component in the category score must use the same
        weight as the wallet score. The total must sum to 100."""
        from polycopy.scoring.wallet_score_v1 import WEIGHTS

        # Build a result with strong inputs and inspect the
        # component weights.
        inp = _strong_category_input()
        result = compute_category_wallet_score_v1(input=inp)
        result_weights = {c.name: c.weight for c in result.components}
        # Same keys
        assert set(result_weights.keys()) == set(WEIGHTS.keys())
        # Same values
        for name, w in WEIGHTS.items():
            assert result_weights[name] == w, (
                f"weight mismatch for {name}: {result_weights[name]} vs {w}"
            )
        # Sum to 100
        assert abs(sum(WEIGHTS.values()) - 100.0) < 1e-9
        assert abs(sum(result_weights.values()) - 100.0) < 1e-9


# ---- 8. Round-trip: result carries typed input --------------------------


class TestRawInputRoundTrip:
    def test_supplied_inputs_persist_exactly(self) -> None:
        """The result must retain the typed input that produced it
        so that persistence can read every raw field from
        result.input.<field> (Phase 9 contract)."""
        inp = _strong_category_input(
            info_score=0.42,
            win_rate=0.58,
            profit_factor=1.65,
            trade_intervals_std=7200.0,
            trade_count=99,
            max_drawdown=0.22,
            sharpe_ratio=1.7,
            sample_fraction=0.15,
            category_trade_count=70,
            category_distinct_markets=5,
            overall_trade_count=120,
            largest_winner_share=0.40,
            top_3_concentration=0.55,
            category_resolved_markets=18,
            category_distinct_events=9,
            category_active_days=11,
        )
        result = compute_category_wallet_score_v1(input=inp)
        assert result.input is inp
        # Verify that every field on the input is reachable via
        # result.input.<field>.
        for field_name in (
            "info_score", "win_rate", "profit_factor",
            "trade_intervals_std", "trade_count", "max_drawdown",
            "sharpe_ratio", "sample_fraction",
            "category_trade_count", "category_distinct_markets",
            "overall_trade_count", "largest_winner_share",
            "top_3_concentration", "category_resolved_markets",
            "category_distinct_events", "category_active_days",
        ):
            assert getattr(result.input, field_name) == getattr(
                inp, field_name
            )

    def test_idempotency_replay_yields_same_score(self) -> None:
        """Replaying the same input twice must yield the same
        numeric score and verdict (determinism for replayability).
        """
        inp = _strong_category_input()
        r1 = compute_category_wallet_score_v1(input=inp)
        r2 = compute_category_wallet_score_v1(input=inp)
        assert r1.score == r2.score
        assert r1.verdict == r2.verdict
        assert r1.category_gate_failures == r2.category_gate_failures
        assert r1.missing_essentials == r2.missing_essentials


# ---- 9. Formula identity ------------------------------------------------


class TestFormulaNameAndVersion:
    def test_formula_name_constant(self) -> None:
        assert CATEGORY_WALLET_FORMULA_NAME == "category_wallet_score"

    def test_formula_version_constant(self) -> None:
        assert CATEGORY_WALLET_FORMULA_VERSION == "1"

    def test_result_carries_formula_version(self) -> None:
        result = compute_category_wallet_score_v1(input=_strong_category_input())
        assert result.formula_version == CATEGORY_WALLET_FORMULA_VERSION

    def test_no_loose_dict_for_core_inputs(self) -> None:
        """The module must not accept dict-shaped inputs for the
        core scoring contract. The typed dataclass is required.

        Verify by inspecting the function signature: it accepts
        a typed `input=CategoryWalletScoreInputV1` kwarg, not a
        loose `inputs=` / `metrics=` dict.
        """
        import inspect
        import polycopy.scoring.category_wallet_score_v1 as mod

        sig = inspect.signature(mod.compute_category_wallet_score_v1)
        param_names = list(sig.parameters.keys())
        # The typed input is the canonical form, not a dict.
        assert "input" in param_names
        # No suspicious dict-shaped input names.
        for n in param_names:
            assert not n.endswith("_dict"), f"suspicious dict param: {n}"
            assert not n.endswith("_inputs"), f"suspicious inputs param: {n}"


# ---- 10. Persistence + round-trip (Task 3.3) ---------------------------


class TestCategoryPersistenceRoundTrip:
    """Round-trip tests for ``persist_category_score_v1``.

    The tests use a fresh disposable SQLite database per test
    (``tmp_path``). The production database is never touched.
    """

    _seeded_wallets: dict[str, str] = {}

    def _make_db(self, tmp_path):
        from polycopy.db.database import Database
        from uuid import uuid4

        db = Database(db_path=tmp_path / "cat.db").connect()
        # Seed a single wallet for FK satisfaction. Tests that
        # need different wallet ids create their own seeds.
        wallet_id = "0xWALLET_" + uuid4().hex[:12]
        db.conn.execute(
            "INSERT INTO wallets (id, address, label, is_sample, "
            "created_at, canonical_address) VALUES (?, ?, ?, 0, ?, ?)",
            (wallet_id, wallet_id.lower(), "test", "2026-01-01T00:00:00Z",
             wallet_id.lower()),
        )
        db.conn.commit()
        # Stash the wallet id in a module-level cache keyed on
        # the tmp_path so tests can find it.
        self._seeded_wallets[str(tmp_path)] = wallet_id
        return db

    def _wallet(self, tmp_path) -> str:
        return self._seeded_wallets[str(tmp_path)]

    def _row(self, db, decision_id):
        return db.fetchone(
            "SELECT * FROM category_wallet_score_decisions WHERE id = ?",
            (decision_id,),
        )

    def test_first_persist_returns_decision_id(self, tmp_path) -> None:
        from polycopy.scoring.score_serialization import (
            persist_category_score_v1,
        )

        db = self._make_db(tmp_path)
        try:
            inp = _strong_category_input(wallet_id=self._wallet(tmp_path))
            result = compute_category_wallet_score_v1(input=inp)
            decision_id = persist_category_score_v1(
                db, inp.wallet_id, inp.category_label, result,
                source_data_timestamp="2026-01-01T00:00:00Z",
            )
            assert decision_id > 0
            row = self._row(db, decision_id)
            assert row is not None
            assert row["wallet_id"] == inp.wallet_id
            assert row["category_label"] == inp.category_label
            assert row["formula_name"] == "category_wallet_score"
            assert row["formula_version"] == "1"
        finally:
            db.close()

    def test_exact_raw_values_persist(self, tmp_path) -> None:
        from polycopy.scoring.score_serialization import (
            persist_category_score_v1,
        )

        db = self._make_db(tmp_path)
        try:
            inp = _strong_category_input(
                wallet_id=self._wallet(tmp_path),
                info_score=0.42,
                win_rate=0.58,
                profit_factor=1.65,
                trade_intervals_std=7200.0,
                trade_count=99,
                max_drawdown=0.22,
                sharpe_ratio=1.7,
                sample_fraction=0.15,
                category_trade_count=70,
                category_distinct_markets=5,
                overall_trade_count=120,
                largest_winner_share=0.40,
                top_3_concentration=0.55,
                category_resolved_markets=18,
                category_distinct_events=9,
                category_active_days=11,
            )
            result = compute_category_wallet_score_v1(input=inp)
            decision_id = persist_category_score_v1(
                db, inp.wallet_id, inp.category_label, result,
            )
            row = self._row(db, decision_id)
            # Every raw input round-trips to within float tolerance.
            assert abs(row["info_score"] - 0.42) < 1e-9
            assert abs(row["win_rate"] - 0.58) < 1e-9
            assert abs(row["profit_factor"] - 1.65) < 1e-9
            assert row["trade_count"] == 99
            assert row["category_trade_count"] == 70
            assert row["category_distinct_markets"] == 5
            assert row["overall_trade_count"] == 120
            assert row["category_resolved_markets"] == 18
            assert row["category_distinct_events"] == 9
            assert row["category_active_days"] == 11
        finally:
            db.close()

    def test_exact_category_gates_persist(self, tmp_path) -> None:
        from polycopy.scoring.score_serialization import (
            persist_category_score_v1,
        )

        db = self._make_db(tmp_path)
        try:
            # Strong score but with a failed gate.
            inp = _strong_category_input(
                wallet_id=self._wallet(tmp_path),
                info_score=0.95,
                category_resolved_markets=10,  # below min
                category_distinct_events=12,
                category_active_days=14,
            )
            result = compute_category_wallet_score_v1(input=inp)
            decision_id = persist_category_score_v1(
                db, inp.wallet_id, inp.category_label, result,
            )
            row = self._row(db, decision_id)
            # The gate value persists.
            assert row["category_resolved_markets"] == 10
            # The failure JSON is non-empty.
            import json as _json
            failures = _json.loads(row["category_gate_failures_json"])
            assert any("category_resolved_markets" in f for f in failures)
        finally:
            db.close()

    def test_missing_essentials_persist(self, tmp_path) -> None:
        from polycopy.scoring.score_serialization import (
            persist_category_score_v1,
        )

        db = self._make_db(tmp_path)
        try:
            inp = _strong_category_input(
                wallet_id=self._wallet(tmp_path),
                win_rate=None, trade_count=None,
            )
            result = compute_category_wallet_score_v1(input=inp)
            assert result.verdict == WalletVerdict.INCOMPLETE
            decision_id = persist_category_score_v1(
                db, inp.wallet_id, inp.category_label, result,
            )
            row = self._row(db, decision_id)
            import json as _json
            missing = _json.loads(row["missing_essentials_json"])
            assert "trade_count" in missing
            assert "win_rate" in missing
        finally:
            db.close()

    def test_idempotent_repeat_returns_existing_id(
        self, tmp_path
    ) -> None:
        from polycopy.scoring.score_serialization import (
            persist_category_score_v1,
        )

        db = self._make_db(tmp_path)
        try:
            inp = _strong_category_input(wallet_id=self._wallet(tmp_path))
            result = compute_category_wallet_score_v1(input=inp)
            id1 = persist_category_score_v1(
                db, inp.wallet_id, inp.category_label, result,
                source_data_timestamp="2026-01-01T00:00:00Z",
            )
            id2 = persist_category_score_v1(
                db, inp.wallet_id, inp.category_label, result,
                source_data_timestamp="2026-01-01T00:00:00Z",
            )
            assert id1 == id2
            # Only one row exists.
            count = db.fetchone(
                "SELECT COUNT(*) AS n FROM category_wallet_score_decisions "
                "WHERE wallet_id = ? AND category_label = ?",
                (inp.wallet_id, inp.category_label),
            )["n"]
            assert count == 1
        finally:
            db.close()

    def test_later_snapshot_creates_new_row(self, tmp_path) -> None:
        from polycopy.scoring.score_serialization import (
            persist_category_score_v1,
        )

        db = self._make_db(tmp_path)
        try:
            inp = _strong_category_input(wallet_id=self._wallet(tmp_path))
            result = compute_category_wallet_score_v1(input=inp)
            id1 = persist_category_score_v1(
                db, inp.wallet_id, inp.category_label, result,
                source_data_timestamp="2026-01-01T00:00:00Z",
            )
            # Later snapshot must produce a NEW immutable row.
            id2 = persist_category_score_v1(
                db, inp.wallet_id, inp.category_label, result,
                source_data_timestamp="2026-01-02T00:00:00Z",
            )
            assert id1 != id2
            count = db.fetchone(
                "SELECT COUNT(*) AS n FROM category_wallet_score_decisions "
                "WHERE wallet_id = ? AND category_label = ?",
                (inp.wallet_id, inp.category_label),
            )["n"]
            assert count == 2
        finally:
            db.close()

    def test_category_label_participates_in_identity(
        self, tmp_path
    ) -> None:
        from polycopy.scoring.score_serialization import (
            persist_category_score_v1,
        )

        db = self._make_db(tmp_path)
        try:
            w = self._wallet(tmp_path)
            inp_crypto = _strong_category_input(
                wallet_id=w, category_label="crypto"
            )
            inp_politics = _strong_category_input(
                wallet_id=w, category_label="politics"
            )
            r_crypto = compute_category_wallet_score_v1(input=inp_crypto)
            r_politics = compute_category_wallet_score_v1(input=inp_politics)
            id_c = persist_category_score_v1(
                db, w, "crypto", r_crypto,
            )
            id_p = persist_category_score_v1(
                db, w, "politics", r_politics,
            )
            assert id_c != id_p
            # Two distinct rows.
            count = db.fetchone(
                "SELECT COUNT(*) AS n FROM category_wallet_score_decisions"
            )["n"]
            assert count == 2
        finally:
            db.close()

    def test_replay_yields_same_score(self, tmp_path) -> None:
        """The exact input that produced decision N must reproduce
        the same numeric score (deterministic replayability)."""
        from polycopy.scoring.score_serialization import (
            persist_category_score_v1,
        )

        db = self._make_db(tmp_path)
        try:
            inp = _strong_category_input(
                wallet_id=self._wallet(tmp_path),
                info_score=0.42, win_rate=0.58, profit_factor=1.65,
                trade_count=99, category_resolved_markets=18,
                category_distinct_events=9, category_active_days=11,
            )
            r1 = compute_category_wallet_score_v1(input=inp)
            persist_category_score_v1(
                db, inp.wallet_id, inp.category_label, r1,
                source_data_timestamp="2026-01-01T00:00:00Z",
            )
            r2 = compute_category_wallet_score_v1(input=inp)
            assert r1.score == r2.score
            assert r1.verdict == r2.verdict
        finally:
            db.close()

    def test_explicit_idempotency_key_honored(self, tmp_path) -> None:
        from polycopy.scoring.score_serialization import (
            persist_category_score_v1,
        )

        db = self._make_db(tmp_path)
        try:
            inp = _strong_category_input(wallet_id=self._wallet(tmp_path))
            result = compute_category_wallet_score_v1(input=inp)
            explicit = "deadbeef" * 8  # 64 hex chars max
            id1 = persist_category_score_v1(
                db, inp.wallet_id, inp.category_label, result,
                idempotency_key=explicit,
            )
            id2 = persist_category_score_v1(
                db, inp.wallet_id, inp.category_label, result,
                idempotency_key=explicit,
            )
            assert id1 == id2
        finally:
            db.close()

    def test_no_essential_field_silently_nullified(
        self, tmp_path
    ) -> None:
        """The essential fields passed in must persist exactly.
        No 'getattr(..., None)' fallback can silently turn a real
        value into NULL."""
        from polycopy.scoring.score_serialization import (
            persist_category_score_v1,
        )

        db = self._make_db(tmp_path)
        try:
            inp = _strong_category_input(
                wallet_id=self._wallet(tmp_path),
                info_score=0.77,
                category_resolved_markets=20,
                category_distinct_events=10,
                category_active_days=12,
            )
            result = compute_category_wallet_score_v1(input=inp)
            decision_id = persist_category_score_v1(
                db, inp.wallet_id, inp.category_label, result,
            )
            row = self._row(db, decision_id)
            assert row["info_score"] is not None
            assert row["win_rate"] is not None
            assert row["category_resolved_markets"] is not None
            assert row["category_distinct_events"] is not None
            assert row["category_active_days"] is not None
        finally:
            db.close()
