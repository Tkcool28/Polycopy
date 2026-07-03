"""Tests for PR 4 paper-signal category + behavior integration (Task 3.7).

Covers:
- missing category label => INCOMPLETE
- no category decision => INCOMPLETE
- category INCOMPLETE => INCOMPLETE
- category WATCHLIST blocks COPY_CANDIDATE
- category SKIP blocks COPY_CANDIDATE
- category COPY_CANDIDATE + DIRECTIONAL can advance
- UNKNOWN behavior caps WATCHLIST
- MARKET_MAKER_LP => SKIP
- HIGH_FREQUENCY_BOT => SKIP
- ARBITRAGE_MULTI_LEG => SKIP
- shadow score/verdict does not change the final verdict
- shadow cannot fill a missing category verdict
- category_label participates in identity (no cross-category fallback)
- load_persisted_category_decision returns None on empty/missing
- resolve_category_label honors snapshot book_summary_json
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Optional
from uuid import uuid4

import pytest

from polycopy.scoring.behavior_classification import (
    BehaviorClassification,
    BehaviorClassificationResult,
    BehaviorEvidence,
)
from polycopy.scoring.category_wallet_score_v1 import (
    CategoryWalletScoreInputV1,
    compute_category_wallet_score_v1,
)
from polycopy.scoring.paper_signal import (
    CATEGORY_FORMULA_VERSION,
    PersistedCategoryDecision,
    _build_category_inputs,
    generate_paper_signal_decision,
    load_persisted_category_decision,
    resolve_category_label,
)
from polycopy.scoring.score_serialization import (
    persist_category_score_v1,
)
from polycopy.scoring.trade_score_v1 import (
    TradeCopyabilityInputV1,
    TradeScoreResult,
    TradeVerdict,
    compute_trade_score_v1,
)
from polycopy.scoring.verdict_generation import (
    SignalVerdict,
    WalletVerdict,
)
from polycopy.scoring.wallet_score_v1 import (
    WalletScoreInputV1,
    WalletScoreResult,
    compute_wallet_score_v1,
)


# ---- Helpers ------------------------------------------------------------


def _make_db(tmp_path: Path):
    from polycopy.db.database import Database

    db = Database(db_path=tmp_path / "ps.db").connect()
    wallet_id = "0xW_" + uuid4().hex[:10]
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, "
        "created_at, canonical_address) VALUES "
        "(?, ?, 'w', 0, ?, ?)",
        (wallet_id, wallet_id.lower(),
         "2026-01-01T00:00:00Z", wallet_id.lower()),
    )
    db.conn.commit()
    return db, wallet_id


def _strong_wallet_result(wallet_id: str) -> WalletScoreResult:
    inp = WalletScoreInputV1(
        wallet_id=wallet_id,
        info_score=0.85,
        win_rate=0.65,
        profit_factor=1.8,
        trade_intervals_std=3600.0,
        trade_count=150,
        max_drawdown=0.10,
        sharpe_ratio=2.4,
        sample_fraction=0.05,
        category_trade_count=120,
        category_distinct_markets=8,
        overall_trade_count=150,
        largest_winner_share=0.30,
        top_3_concentration=0.55,
        resolved_markets=40,
        active_trading_days=30,
        distinct_events=20,
    )
    return compute_wallet_score_v1(input=inp)


def _strong_trade_result(wallet_id: str) -> TradeScoreResult:
    inp = TradeCopyabilityInputV1(
        wallet_id=wallet_id,
        source_trade_id="src-1",
        side="BUY",
        price_deterioration_pct=0.02,
        intended_stake=100.0,
        executable_depth=100.0,
        spread=0.01,
        best_bid_size=200.0,
        best_ask_size=200.0,
        trade_age_seconds=30.0,
        seconds_to_market_end=2 * 24 * 3600,  # 2 days
        market_active=True,
        market_closed=False,
        market_resolved=False,
        has_valid_strategy=True,
        has_complete_data=True,
        market_category="crypto",
    )
    return compute_trade_score_v1(input=inp)


def _directional_behavior() -> BehaviorClassificationResult:
    return BehaviorClassificationResult(
        classification=BehaviorClassification.DIRECTIONAL,
        reasons=["directional_test"],
        is_eligible_for_copy=True,
        is_watchlist_cap=False,
        is_skip=False,
    )


def _watchlist_behavior() -> BehaviorClassificationResult:
    return BehaviorClassificationResult(
        classification=BehaviorClassification.UNKNOWN,
        reasons=["unknown_test"],
        is_eligible_for_copy=False,
        is_watchlist_cap=True,
        is_skip=False,
    )


def _mm_behavior() -> BehaviorClassificationResult:
    return BehaviorClassificationResult(
        classification=BehaviorClassification.MARKET_MAKER_LP,
        reasons=["mm_test"],
        is_eligible_for_copy=False,
        is_watchlist_cap=False,
        is_skip=True,
    )


def _hft_behavior() -> BehaviorClassificationResult:
    return BehaviorClassificationResult(
        classification=BehaviorClassification.HIGH_FREQUENCY_BOT,
        reasons=["hft_test"],
        is_eligible_for_copy=False,
        is_watchlist_cap=False,
        is_skip=True,
    )


def _arb_behavior() -> BehaviorClassificationResult:
    return BehaviorClassificationResult(
        classification=BehaviorClassification.ARBITRAGE_MULTI_LEG,
        reasons=["arb_test"],
        is_eligible_for_copy=False,
        is_watchlist_cap=False,
        is_skip=True,
    )


def _persist_category(
    db, wallet_id: str, category: str, verdict_str: str,
    source_data_timestamp: Optional[str] = None,
) -> int:
    """Compute and persist a category score that produces the
    requested verdict. Strong inputs + passed gates yield
    COPY_CANDIDATE; deliberately failed gates yield WATCHLIST;
    missing essentials yield INCOMPLETE; etc.
    """
    # Build inputs that produce the requested verdict.
    if verdict_str == "copy_candidate":
        inp = CategoryWalletScoreInputV1(
            wallet_id=wallet_id,
            category_label=category,
            info_score=0.85, win_rate=0.65, profit_factor=1.8,
            trade_intervals_std=3600.0, trade_count=150,
            max_drawdown=0.10, sharpe_ratio=2.4,
            sample_fraction=0.05,
            category_trade_count=120, category_distinct_markets=8,
            overall_trade_count=150,
            largest_winner_share=0.30, top_3_concentration=0.55,
            category_resolved_markets=20,
            category_distinct_events=12,
            category_active_days=14,
            source_data_timestamp=source_data_timestamp,
        )
    elif verdict_str == "watchlist":
        # Strong score but failed category gate.
        inp = CategoryWalletScoreInputV1(
            wallet_id=wallet_id,
            category_label=category,
            info_score=0.95, win_rate=0.85, profit_factor=2.2,
            trade_intervals_std=600.0, trade_count=300,
            max_drawdown=0.05, sharpe_ratio=2.9,
            sample_fraction=0.0,
            category_trade_count=280, category_distinct_markets=20,
            overall_trade_count=300,
            largest_winner_share=0.10, top_3_concentration=0.30,
            category_resolved_markets=10,  # < 15
            category_distinct_events=12,
            category_active_days=14,
            source_data_timestamp=source_data_timestamp,
        )
    elif verdict_str == "incomplete":
        inp = CategoryWalletScoreInputV1(
            wallet_id=wallet_id,
            category_label=category,
            # Missing essential metrics → INCOMPLETE
            win_rate=None, trade_count=None,
            source_data_timestamp=source_data_timestamp,
        )
    else:  # skip
        inp = CategoryWalletScoreInputV1(
            wallet_id=wallet_id,
            category_label=category,
            # Low score → SKIP
            info_score=0.05, win_rate=0.05, profit_factor=0.5,
            trade_intervals_std=24 * 3600.0, trade_count=20,
            max_drawdown=0.50, sharpe_ratio=0.1,
            sample_fraction=0.80,
            category_trade_count=15, category_distinct_markets=2,
            overall_trade_count=100,
            largest_winner_share=0.20, top_3_concentration=0.60,
            category_resolved_markets=20,
            category_distinct_events=12,
            category_active_days=14,
            source_data_timestamp=source_data_timestamp,
        )
    result = compute_category_wallet_score_v1(input=inp)
    return persist_category_score_v1(
        db, wallet_id, category, result,
        source_data_timestamp=source_data_timestamp,
    )


# ---- 1. Loader tests ---------------------------------------------------


class TestLoadPersistedCategoryDecision:
    def test_returns_none_on_empty_label(self, tmp_path: Path) -> None:
        db, w = _make_db(tmp_path)
        try:
            assert load_persisted_category_decision(db, w, "") is None
            assert load_persisted_category_decision(db, w, None) is None
            assert load_persisted_category_decision(db, w, "   ") is None
        finally:
            db.close()

    def test_returns_none_when_no_decision(self, tmp_path: Path) -> None:
        db, w = _make_db(tmp_path)
        try:
            assert load_persisted_category_decision(
                db, w, "crypto"
            ) is None
        finally:
            db.close()

    def test_filters_by_category_label(self, tmp_path: Path) -> None:
        """A category_label mismatch MUST return None — the loader
        cannot fall back to another category."""
        db, w = _make_db(tmp_path)
        try:
            _persist_category(db, w, "crypto", "copy_candidate")
            assert load_persisted_category_decision(
                db, w, "crypto"
            ) is not None
            assert load_persisted_category_decision(
                db, w, "politics"
            ) is None
        finally:
            db.close()

    def test_returns_typed_persisted_decision(
        self, tmp_path: Path
    ) -> None:
        db, w = _make_db(tmp_path)
        try:
            _persist_category(db, w, "crypto", "copy_candidate")
            decision = load_persisted_category_decision(db, w, "crypto")
            assert isinstance(decision, PersistedCategoryDecision)
            assert decision.wallet_id == w
            assert decision.category_label == "crypto"
            assert decision.verdict == "copy_candidate"
            assert decision.score >= 75.0
        finally:
            db.close()


# ---- 2. Category label resolution --------------------------------------


class TestResolveCategoryLabel:
    def test_returns_none_when_no_snapshot_no_outcome(
        self, tmp_path: Path
    ) -> None:
        db, w = _make_db(tmp_path)
        try:
            assert resolve_category_label(
                db, {"wallet_id": w, "market_outcome_id": None}, None
            ) is None
        finally:
            db.close()

    def test_resolves_from_snapshot_book_summary(
        self, tmp_path: Path
    ) -> None:
        db, w = _make_db(tmp_path)
        try:
            import json as _json
            snapshot = {
                "book_summary_json": _json.dumps(
                    {"category_label": "crypto"}
                )
            }
            label = resolve_category_label(
                db, {"wallet_id": w, "market_outcome_id": None},
                snapshot,
            )
            assert label == "crypto"
        finally:
            db.close()

    def test_falls_back_to_market_id(self, tmp_path: Path) -> None:
        db, w = _make_db(tmp_path)
        try:
            # Seed a market + outcome, then resolve.
            market_id = "mkt-" + uuid4().hex[:8]
            db.conn.execute(
                "INSERT INTO markets (id, source_id, source, question, "
                "fetched_at) VALUES (?, 's1', 'polymarket', 'Q', "
                "'2026-01-01T00:00:00Z')",
                (market_id,),
            )
            cur = db.conn.execute(
                "INSERT INTO market_outcomes (market_id, label, price, "
                "volume) VALUES (?, 'Yes', 0.5, 0)",
                (market_id,),
            )
            outcome_id = int(cur.lastrowid)
            db.conn.commit()
            label = resolve_category_label(
                db,
                {"wallet_id": w, "market_outcome_id": outcome_id},
                None,
            )
            assert label == f"market:{market_id}"
        finally:
            db.close()

    def test_malformed_book_summary_ignored(self, tmp_path: Path) -> None:
        db, w = _make_db(tmp_path)
        try:
            snapshot = {"book_summary_json": "not-json{"}
            assert resolve_category_label(
                db, {"wallet_id": w, "market_outcome_id": None},
                snapshot,
            ) is None
        finally:
            db.close()


# ---- 3. Decision integration: category gates ---------------------------


class TestCategoryVerdictGates:
    def test_missing_category_label_yields_incomplete(
        self, tmp_path: Path
    ) -> None:
        db, w = _make_db(tmp_path)
        try:
            score, verdict = _build_category_inputs(
                db, {"wallet_id": w, "market_outcome_id": None}, None
            )
            assert score is None
            assert verdict is None
            wsr = _strong_wallet_result(w)
            tsr = _strong_trade_result(w)
            v = generate_paper_signal_decision(
                wallet_score_result=wsr,
                trade_score_result=tsr,
                behavior_result=_directional_behavior(),
                category_score=score,
                category_verdict=verdict,
                shadow_result=None,
            )
            assert v == SignalVerdict.INCOMPLETE
        finally:
            db.close()

    def test_no_category_decision_yields_incomplete(
        self, tmp_path: Path
    ) -> None:
        db, w = _make_db(tmp_path)
        try:
            # Seed a market + outcome so label resolves, but
            # don't persist a category decision.
            market_id = "mkt-" + uuid4().hex[:8]
            db.conn.execute(
                "INSERT INTO markets (id, source_id, source, question, "
                "fetched_at) VALUES (?, 's1', 'polymarket', 'Q', "
                "'2026-01-01T00:00:00Z')",
                (market_id,),
            )
            cur = db.conn.execute(
                "INSERT INTO market_outcomes (market_id, label, price, "
                "volume) VALUES (?, 'Yes', 0.5, 0)",
                (market_id,),
            )
            outcome_id = int(cur.lastrowid)
            db.conn.commit()
            score, verdict = _build_category_inputs(
                db,
                {"wallet_id": w, "market_outcome_id": outcome_id},
                None,
            )
            assert score is None
            assert verdict is None
            wsr = _strong_wallet_result(w)
            tsr = _strong_trade_result(w)
            v = generate_paper_signal_decision(
                wallet_score_result=wsr,
                trade_score_result=tsr,
                behavior_result=_directional_behavior(),
                category_score=score,
                category_verdict=verdict,
                shadow_result=None,
            )
            assert v == SignalVerdict.INCOMPLETE
        finally:
            db.close()

    def test_category_incomplete_yields_incomplete(
        self, tmp_path: Path
    ) -> None:
        db, w = _make_db(tmp_path)
        try:
            _persist_category(db, w, "crypto", "incomplete")
            score, verdict = _build_category_inputs(
                db, {"wallet_id": w, "market_outcome_id": None},
                {"book_summary_json": '{"category_label": "crypto"}'},
            )
            assert score is not None
            assert verdict == "incomplete"
            wsr = _strong_wallet_result(w)
            tsr = _strong_trade_result(w)
            v = generate_paper_signal_decision(
                wallet_score_result=wsr,
                trade_score_result=tsr,
                behavior_result=_directional_behavior(),
                category_score=score,
                category_verdict=verdict,
                shadow_result=None,
            )
            assert v == SignalVerdict.INCOMPLETE
        finally:
            db.close()

    def test_category_watchlist_blocks_copy_candidate(
        self, tmp_path: Path
    ) -> None:
        db, w = _make_db(tmp_path)
        try:
            _persist_category(db, w, "crypto", "watchlist")
            score, verdict = _build_category_inputs(
                db, {"wallet_id": w, "market_outcome_id": None},
                {"book_summary_json": '{"category_label": "crypto"}'},
            )
            assert verdict == "watchlist"
            wsr = _strong_wallet_result(w)
            tsr = _strong_trade_result(w)
            v = generate_paper_signal_decision(
                wallet_score_result=wsr,
                trade_score_result=tsr,
                behavior_result=_directional_behavior(),
                category_score=score,
                category_verdict=verdict,
                shadow_result=None,
            )
            # WATCHLIST — never COPY_CANDIDATE.
            assert v == SignalVerdict.WATCHLIST
        finally:
            db.close()

    def test_category_skip_blocks_copy_candidate(
        self, tmp_path: Path
    ) -> None:
        db, w = _make_db(tmp_path)
        try:
            _persist_category(db, w, "crypto", "skip")
            score, verdict = _build_category_inputs(
                db, {"wallet_id": w, "market_outcome_id": None},
                {"book_summary_json": '{"category_label": "crypto"}'},
            )
            assert verdict == "skip"
            wsr = _strong_wallet_result(w)
            tsr = _strong_trade_result(w)
            v = generate_paper_signal_decision(
                wallet_score_result=wsr,
                trade_score_result=tsr,
                behavior_result=_directional_behavior(),
                category_score=score,
                category_verdict=verdict,
                shadow_result=None,
            )
            # Category SKIP → category verdict != copy_candidate
            # → WATCHLIST (Phase 3 decision engine rule). The
            # important contract is that the final verdict is
            # never COPY_CANDIDATE — the existing
            # test_16_category_skip_blocks_copy documents the
            # same behavior.
            assert v == SignalVerdict.WATCHLIST
            assert v != SignalVerdict.COPY_CANDIDATE
        finally:
            db.close()

    def test_category_copy_candidate_plus_directional_advances(
        self, tmp_path: Path
    ) -> None:
        db, w = _make_db(tmp_path)
        try:
            _persist_category(db, w, "crypto", "copy_candidate")
            score, verdict = _build_category_inputs(
                db, {"wallet_id": w, "market_outcome_id": None},
                {"book_summary_json": '{"category_label": "crypto"}'},
            )
            assert verdict == "copy_candidate"
            wsr = _strong_wallet_result(w)
            tsr = _strong_trade_result(w)
            v = generate_paper_signal_decision(
                wallet_score_result=wsr,
                trade_score_result=tsr,
                behavior_result=_directional_behavior(),
                category_score=score,
                category_verdict=verdict,
                shadow_result=None,
            )
            assert v == SignalVerdict.COPY_CANDIDATE
        finally:
            db.close()


# ---- 4. Behavior cap integration ---------------------------------------


class TestBehaviorCapIntegration:
    def test_unknown_behavior_caps_watchlist(self, tmp_path: Path) -> None:
        db, w = _make_db(tmp_path)
        try:
            _persist_category(db, w, "crypto", "copy_candidate")
            score, verdict = _build_category_inputs(
                db, {"wallet_id": w, "market_outcome_id": None},
                {"book_summary_json": '{"category_label": "crypto"}'},
            )
            wsr = _strong_wallet_result(w)
            tsr = _strong_trade_result(w)
            v = generate_paper_signal_decision(
                wallet_score_result=wsr,
                trade_score_result=tsr,
                behavior_result=_watchlist_behavior(),
                category_score=score,
                category_verdict=verdict,
                shadow_result=None,
            )
            # UNKNOWN behavior caps at WATCHLIST — cannot be
            # COPY_CANDIDATE.
            assert v == SignalVerdict.WATCHLIST
        finally:
            db.close()

    def test_market_maker_yields_skip(self, tmp_path: Path) -> None:
        db, w = _make_db(tmp_path)
        try:
            _persist_category(db, w, "crypto", "copy_candidate")
            score, verdict = _build_category_inputs(
                db, {"wallet_id": w, "market_outcome_id": None},
                {"book_summary_json": '{"category_label": "crypto"}'},
            )
            wsr = _strong_wallet_result(w)
            tsr = _strong_trade_result(w)
            v = generate_paper_signal_decision(
                wallet_score_result=wsr,
                trade_score_result=tsr,
                behavior_result=_mm_behavior(),
                category_score=score,
                category_verdict=verdict,
                shadow_result=None,
            )
            assert v == SignalVerdict.SKIP
        finally:
            db.close()

    def test_hft_yields_skip(self, tmp_path: Path) -> None:
        db, w = _make_db(tmp_path)
        try:
            _persist_category(db, w, "crypto", "copy_candidate")
            score, verdict = _build_category_inputs(
                db, {"wallet_id": w, "market_outcome_id": None},
                {"book_summary_json": '{"category_label": "crypto"}'},
            )
            wsr = _strong_wallet_result(w)
            tsr = _strong_trade_result(w)
            v = generate_paper_signal_decision(
                wallet_score_result=wsr,
                trade_score_result=tsr,
                behavior_result=_hft_behavior(),
                category_score=score,
                category_verdict=verdict,
                shadow_result=None,
            )
            assert v == SignalVerdict.SKIP
        finally:
            db.close()

    def test_arbitrage_yields_skip(self, tmp_path: Path) -> None:
        db, w = _make_db(tmp_path)
        try:
            _persist_category(db, w, "crypto", "copy_candidate")
            score, verdict = _build_category_inputs(
                db, {"wallet_id": w, "market_outcome_id": None},
                {"book_summary_json": '{"category_label": "crypto"}'},
            )
            wsr = _strong_wallet_result(w)
            tsr = _strong_trade_result(w)
            v = generate_paper_signal_decision(
                wallet_score_result=wsr,
                trade_score_result=tsr,
                behavior_result=_arb_behavior(),
                category_score=score,
                category_verdict=verdict,
                shadow_result=None,
            )
            assert v == SignalVerdict.SKIP
        finally:
            db.close()


# ---- 5. Shadow isolation -----------------------------------------------


class TestShadowIsolation:
    def test_shadow_verdict_does_not_override_skip(
        self, tmp_path: Path
    ) -> None:
        """A COPY_CANDIDATE shadow verdict cannot lift a SKIP
        produced by behavior MM. Shadow is non-controlling
        (Phase 15)."""
        from polycopy.scoring.shadow_score_v2 import (
            ShadowVerdict,
            ShadowScoreResult,
        )
        db, w = _make_db(tmp_path)
        try:
            _persist_category(db, w, "crypto", "copy_candidate")
            score, verdict = _build_category_inputs(
                db, {"wallet_id": w, "market_outcome_id": None},
                {"book_summary_json": '{"category_label": "crypto"}'},
            )
            wsr = _strong_wallet_result(w)
            tsr = _strong_trade_result(w)
            shadow = ShadowScoreResult(
                wallet_id=w,
                source_trade_id="src-1",
                score=99.0,
                verdict=ShadowVerdict.SHADOW_COPY_CANDIDATE,
            )
            v = generate_paper_signal_decision(
                wallet_score_result=wsr,
                trade_score_result=tsr,
                behavior_result=_mm_behavior(),
                category_score=score,
                category_verdict=verdict,
                shadow_result=shadow,
            )
            # Shadow is ignored; MM forces SKIP.
            assert v == SignalVerdict.SKIP
        finally:
            db.close()

    def test_shadow_cannot_fill_missing_category_verdict(
        self, tmp_path: Path
    ) -> None:
        """A COPY_CANDIDATE shadow verdict MUST NOT be used to
        fill a missing category verdict. The signal must remain
        INCOMPLETE."""
        from polycopy.scoring.shadow_score_v2 import (
            ShadowVerdict,
            ShadowScoreResult,
        )
        db, w = _make_db(tmp_path)
        try:
            wsr = _strong_wallet_result(w)
            tsr = _strong_trade_result(w)
            shadow = ShadowScoreResult(
                wallet_id=w,
                source_trade_id="src-1",
                score=99.0,
                verdict=ShadowVerdict.SHADOW_COPY_CANDIDATE,
            )
            v = generate_paper_signal_decision(
                wallet_score_result=wsr,
                trade_score_result=tsr,
                behavior_result=_directional_behavior(),
                category_score=None,
                category_verdict=None,
                shadow_result=shadow,
            )
            # Shadow is non-controlling. Missing category →
            # INCOMPLETE.
            assert v == SignalVerdict.INCOMPLETE
        finally:
            db.close()

    def test_shadow_none_is_handled(self, tmp_path: Path) -> None:
        db, w = _make_db(tmp_path)
        try:
            _persist_category(db, w, "crypto", "copy_candidate")
            score, verdict = _build_category_inputs(
                db, {"wallet_id": w, "market_outcome_id": None},
                {"book_summary_json": '{"category_label": "crypto"}'},
            )
            wsr = _strong_wallet_result(w)
            tsr = _strong_trade_result(w)
            v = generate_paper_signal_decision(
                wallet_score_result=wsr,
                trade_score_result=tsr,
                behavior_result=_directional_behavior(),
                category_score=score,
                category_verdict=verdict,
                shadow_result=None,
            )
            assert v == SignalVerdict.COPY_CANDIDATE
        finally:
            db.close()


# ---- 6. No cross-category fallback ------------------------------------


class TestNoCategoryFallback:
    def test_different_category_label_does_not_fallback(
        self, tmp_path: Path
    ) -> None:
        """A persisted decision for ``crypto`` MUST NOT be
        returned for the label ``politics``. This is the spec
        rule against silent cross-category fallback."""
        db, w = _make_db(tmp_path)
        try:
            _persist_category(db, w, "crypto", "copy_candidate")
            # Loader for "politics" must return None.
            assert load_persisted_category_decision(
                db, w, "politics"
            ) is None
            # And the decision engine must yield INCOMPLETE
            # when the label doesn't match.
            wsr = _strong_wallet_result(w)
            tsr = _strong_trade_result(w)
            v = generate_paper_signal_decision(
                wallet_score_result=wsr,
                trade_score_result=tsr,
                behavior_result=_directional_behavior(),
                category_score=None,
                category_verdict=None,
                shadow_result=None,
            )
            assert v == SignalVerdict.INCOMPLETE
        finally:
            db.close()


# ---- 7. Formula version constant ---------------------------------------


class TestCategoryFormulaVersion:
    def test_formula_version_pinned(self) -> None:
        assert CATEGORY_FORMULA_VERSION == "1"
