"""Section F — scorer reuse proof.

For a shared canonical evidence fixture, the production-style typed input
that PR67's :func:`build_wallet_score_input_v1` constructs from
:class:`WalletEvidence`, and the discovery-style typed input our new
``build_wallet_score_input_v1`` constructs from
:class:`WalletCategoryEvidence`, MUST yield the SAME WalletScoreResult /
CategoryWalletScoreResult fields when fed into the frozen scorers.

The fixture below uses common metrics (resolved_markets=15, etc.) that
both paths can construct.
"""
from __future__ import annotations

from datetime import datetime, timezone


from polycopy.discovery.short_horizon_specialists import (
    STATUS_TAXONOMY_INCOMPLETE,
    STATUS_LONG_HORIZON_HEAVY,
    STATUS_SOURCE_INCOMPLETE,
    STATUS_CONFLICT,
    discover_short_horizon_specialists,
)
from polycopy.discovery.wallet_evidence import (
    build_wallet_score_input_v1,
    evidence_from_history,
)
from polycopy.discovery.wallet_history import (
    IncompleteEvidence,
    SettledEvidence,
    WalletHistoryRecord,
)
from polycopy.scoring.category_wallet_score_v1 import (
    CATEGORY_WALLET_FORMULA_VERSION,
    compute_category_wallet_score_v1,
)
from polycopy.scoring.wallet_evidence import (
    WalletEvidence,
    build_wallet_score_input_v1 as pr67_build_wallet_input,
    build_category_score_input_v1 as pr67_build_category_input,
)
from polycopy.scoring.wallet_score_v1 import (
    GLOBAL_MIN_ACTIVE_TRADING_DAYS,
    GLOBAL_MIN_DISTINCT_EVENTS,
    GLOBAL_MIN_RESOLVED_MARKETS,
    CATEGORY_MIN_RESOLVED_MARKETS,
    CATEGORY_MIN_DISTINCT_EVENTS,
    compute_wallet_score_v1,
)


def _now() -> datetime:
    return datetime(2026, 7, 14, 12, tzinfo=timezone.utc)


def _make_pr67_evidence() -> WalletEvidence:
    """A canonical fixture: 36 settled, 22 wins, realized pnl + pf + win rate.

    These numbers are chosen so the scorer classifies the wallet normally
    AND so the discovery-side fixture yields the same verdict through the
    shared scorers."""
    wins = 22
    losses = 14
    return WalletEvidence(
        wallet_id="0xfixture",
        category_label=None,
        total_buy_trades=36,
        resolved_buy_trades=36,
        resolved_markets=36,
        winning_buy_trades=wins,
        losing_buy_trades=losses,
        realized_pnl=37.0,
        win_rate=wins / (wins + losses),
        profit_factor=3.142857,  # gross_gain / gross_loss = 44 / 14
        active_trading_days=24,
        distinct_events=36,
        distinct_markets=36,
        unresolved_buy_trades=0,
        missing_event_identity_count=0,
        evidence_start_timestamp="2025-07-01T00:00:00+00:00",
        source_data_timestamp="2026-07-13T00:00:00+00:00",
        evidence_fingerprint="fixture-fingerprint",
        included_source_trade_ids=(),
        missing_reasons=(),
    )


def _make_pr69_history_record() -> WalletHistoryRecord:
    n_settled = 36
    pnls = [2.0] * 22 + [-0.5] * 14  # 22 winners + 14 losers
    return WalletHistoryRecord(
        wallet_address="0xfixture",
        settled=tuple(
            SettledEvidence(
                wallet_address="0xfixture",
                market_condition_id=f"0x{i:064x}",
                identity_hash=f"id{i}",
                side="BUY",
                price=0.5, size=1.0,
                # spread across enough distinct dates (≥ 25) to clear GLOBAL_MIN_ACTIVE_TRADING_DAYS
                timestamp=f"2026-0{(i // 25) + 1}-{(i % 25) + 1:02d}T00:00:00+00:00",
                category_label="sports",
                winning_outcome=(i < 22),
                settled_realized_pnl=pnls[i],
                redeemed=True,
                proof_source="r",
                horizon_status="HORIZON_PREFERRED",
            )
            for i in range(n_settled)
        ),
        early_exit=tuple(),
        unresolved=tuple(),
        incomplete=tuple(),
        first_qualifying_trade="2026-01-01T00:00:00+00:00",
        last_qualifying_trade="2026-07-14T00:00:00+00:00",
        active_trading_days=24,
        distinct_events=tuple(f"0x{i:064x}" for i in range(n_settled)),
        buy_count=36, sell_count=0,
        two_sided_churn=False,
        market_concentration={}, event_concentration={},
        largest_market_pnl_share=None, largest_event_pnl_share=None,
        long_horizon_excluded=0, taxonomy_excluded=0, source_incomplete=0,
        evidence_completeness=1.0,
    )


# --- F.1 production and discovery wallet input share fields -------------------


def test_both_paths_pass_canonical_inputs_through_same_wallet_score_result() -> None:
    """Shared scorer verdict / missing-essentials / gate-failures MUST be
    identical across both builders for the canonical fixture.

    The discovery-side builder and the production-side builder both feed
    :class:`WalletScoreInputV1` into the frozen scorer. With an identical
    set of metric fields, the verdict, the missing-essentials envelope,
    and the gate-failure list MUST agree exactly — that is the only
    property an operator can rely on to know the audit pipeline cannot
    drift from production.
    """
    ev67 = _make_pr67_evidence()
    inp67 = pr67_build_wallet_input(ev67)
    res67 = compute_wallet_score_v1(input=inp67, now=_now())

    rec = _make_pr69_history_record()
    all_ev = [e for e in evidence_from_history(rec) if e.category_label == "__all__"][0]
    inp69 = build_wallet_score_input_v1(all_ev)
    res69 = compute_wallet_score_v1(input=inp69, now=_now())

    # 1) Identity dataclass type — both MUST construct the same
    #    WalletScoreInputV1 (this is what "shared input builder" means).
    assert type(inp67) is type(inp69)
    assert inp67.wallet_id == inp69.wallet_id == "0xfixture"

    # 2) The fields BOTH builders populate identically from canonical
    #    metrics MUST agree exactly.
    assert inp67.win_rate == inp69.win_rate
    assert inp67.trade_count == inp69.trade_count
    assert inp67.resolved_markets == inp69.resolved_markets
    assert inp67.distinct_events == inp69.distinct_events

    # 3) The frozen scorer's verdict, missing-essentials and gate-failure
    #    sets MUST be byte-identical — that is the rejection-on-drift
    #    contract.
    assert res67.verdict == res69.verdict
    assert set(res67.missing_essentials) == set(res69.missing_essentials)
    assert set(res67.eligibility_gate_failures) == set(res69.eligibility_gate_failures)
    assert res67.score == res69.score


# --- F.2 unchanged formula versions ------------------------------------------


def test_discovered_wallet_score_uses_frozen_v1_formula_version() -> None:
    ev67 = _make_pr67_evidence()
    inp67 = pr67_build_wallet_input(ev67)
    res = compute_wallet_score_v1(input=inp67, now=_now())
    # The frozen v1 formula version is exactly "1"; reassigning it would
    # break every audit harness that relies on this constant.
    assert res.formula_version == "1"


def test_category_score_formula_version_is_frozen() -> None:
    ev67 = _make_pr67_evidence()
    inp67 = pr67_build_category_input(ev67, "sports", overall_trade_count=18)
    res = compute_category_wallet_score_v1(input=inp67, now=_now())
    assert res.formula_version == CATEGORY_WALLET_FORMULA_VERSION


# --- F.3 long-horizon evidence absent from settled -------------------------


def test_long_horizon_excluded_evidence_does_not_reach_scorer() -> None:
    rec = WalletHistoryRecord(
        wallet_address="0xfixture",
        settled=tuple(),
        early_exit=tuple(),
        unresolved=tuple(),
        incomplete=tuple(),
        first_qualifying_trade=None, last_qualifying_trade=None,
        active_trading_days=0, distinct_events=(),
        buy_count=0, sell_count=0, two_sided_churn=False,
        market_concentration={}, event_concentration={},
        largest_market_pnl_share=None, largest_event_pnl_share=None,
        long_horizon_excluded=2,
        taxonomy_excluded=0,
        source_incomplete=0,
        evidence_completeness=0.0,
    )
    evs = evidence_from_history(rec)
    all_ev = [e for e in evs if e.category_label == "__all__"][0]
    inp = build_wallet_score_input_v1(all_ev)
    # Long-horizon excluded doesn't contribute to qualifying_trades.
    assert all_ev.qualifying_trades == 0
    assert inp.trade_count is None or inp.trade_count == 0


# --- F.4 incomplete stays incomplete ---------------------------------------


def test_incomplete_stays_incomplete_in_status() -> None:
    rec = WalletHistoryRecord(
        wallet_address="0xfixture",
        settled=(),
        early_exit=(),
        unresolved=(),
        incomplete=(),
        first_qualifying_trade=None, last_qualifying_trade=None,
        active_trading_days=0, distinct_events=(),
        buy_count=0, sell_count=0, two_sided_churn=False,
        market_concentration={}, event_concentration={},
        largest_market_pnl_share=None, largest_event_pnl_share=None,
        long_horizon_excluded=0, taxonomy_excluded=0,
        source_incomplete=999,
        evidence_completeness=0.0,
    )
    report = discover_short_horizon_specialists(history_records=(rec,), now=_now())
    assert len(report.candidates) == 1
    assert report.candidates[0]["overall_status"] == STATUS_SOURCE_INCOMPLETE



def test_long_horizon_heavy_status() -> None:
    rec = WalletHistoryRecord(
        wallet_address="0xfixture",
        settled=(),
        early_exit=(),
        unresolved=(),
        incomplete=(),
        first_qualifying_trade=None, last_qualifying_trade=None,
        active_trading_days=0, distinct_events=(),
        buy_count=0, sell_count=0, two_sided_churn=False,
        market_concentration={}, event_concentration={},
        largest_market_pnl_share=None, largest_event_pnl_share=None,
        long_horizon_excluded=5,
        taxonomy_excluded=0,
        source_incomplete=0,
        evidence_completeness=0.0,
    )
    report = discover_short_horizon_specialists(history_records=(rec,), now=_now())
    assert report.candidates[0]["overall_status"] == STATUS_LONG_HORIZON_HEAVY


def test_taxonomy_incomplete_status() -> None:
    rec = WalletHistoryRecord(
        wallet_address="0xfixture",
        settled=(),
        early_exit=(),
        unresolved=(),
        incomplete=(),
        first_qualifying_trade=None, last_qualifying_trade=None,
        active_trading_days=0, distinct_events=(),
        buy_count=0, sell_count=0, two_sided_churn=False,
        market_concentration={}, event_concentration={},
        largest_market_pnl_share=None, largest_event_pnl_share=None,
        long_horizon_excluded=0,
        taxonomy_excluded=10,
        source_incomplete=0,
        evidence_completeness=0.0,
    )
    report = discover_short_horizon_specialists(history_records=(rec,), now=_now())
    assert report.candidates[0]["overall_status"] == STATUS_TAXONOMY_INCOMPLETE


def test_conflict_status_does_not_silently_pick() -> None:
    """A wallet whose record contains any ``incomplete`` rows is reported
    as CONFLICT — no silent pick."""
    rec = WalletHistoryRecord(
        wallet_address="0xfixture",
        settled=(),
        early_exit=(),
        unresolved=(),
        incomplete=(IncompleteEvidence(
            wallet_address="0xfixture",
            market_condition_id="0xabc",
            identity_hash="i",
            side="BUY",
            reason="conflict_marker",
        ),),
        first_qualifying_trade=None, last_qualifying_trade=None,
        active_trading_days=0, distinct_events=(),
        buy_count=0, sell_count=0, two_sided_churn=False,
        market_concentration={}, event_concentration={},
        largest_market_pnl_share=None, largest_event_pnl_share=None,
        long_horizon_excluded=0, taxonomy_excluded=0, source_incomplete=0,
        evidence_completeness=0.0,
    )
    report = discover_short_horizon_specialists(history_records=(rec,), now=_now())
    assert report.candidates[0]["overall_status"] == STATUS_CONFLICT
    rec = _make_pr69_history_record()
    all_ev = [e for e in evidence_from_history(rec) if e.category_label == "__all__"][0]
    inp_a = build_wallet_score_input_v1(all_ev)
    inp_b = build_wallet_score_input_v1(all_ev)
    res_a = compute_wallet_score_v1(input=inp_a, now=_now())
    res_b = compute_wallet_score_v1(input=inp_b, now=_now())
    assert res_a.score == res_b.score
    assert res_a.verdict == res_b.verdict
    assert res_a.missing_essentials == res_b.missing_essentials


# --- F.6 thresholds are exactly the frozen values -------------------------


def test_frozen_thresholds_unmodified() -> None:
    assert GLOBAL_MIN_RESOLVED_MARKETS == 30
    assert GLOBAL_MIN_ACTIVE_TRADING_DAYS == 20
    assert GLOBAL_MIN_DISTINCT_EVENTS == 15
    assert CATEGORY_MIN_RESOLVED_MARKETS == 15
    assert CATEGORY_MIN_DISTINCT_EVENTS == 8
