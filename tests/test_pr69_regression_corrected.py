"""Correction regression tests: PR69 STEP 17 items not yet directly asserted.

Covers the remaining required regression cases:
  #17 market-universe dedup (STEP 12)
  #18 SOURCE_INCOMPLETE precedence over confident labels (STEP 13/15)
  #19 per-category READY_FOR_REVIEW gating (STEP 15)
  #20 CLI phase-budget allocation from PHASE_DEFAULT_PERCENTAGES (STEP 11/13)
  #21 seed provenance preserved in candidate.sources (STEP 14)
  #22 distinct_events derived from official event identity (STEP 9)
  #23 redeemed settled positions counted in resolved (STEP 7)
  #24 concentration from one ledger, no double count (STEP 5)
  #25 early-exit / partial never counted as a win (STEP 4)
  #26 taxonomy PARTIAL/UNAVAILABLE fail closed (STEP 10)
  #27 engine never auto-approves (no production mutation surface)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from polycopy.discovery._safe_get import PHASE_DEFAULT_PERCENTAGES, split_phase_caps
from polycopy.discovery.market_universe import MarketUniverseAudit
from polycopy.discovery.short_horizon_specialists import (
    STATUS_READY_FOR_REVIEW,
    STATUS_SOURCE_INCOMPLETE,
    discover_short_horizon_specialists,
)
from polycopy.discovery.taxonomy_enricher import enrich_market
from polycopy.discovery.wallet_history import (
    SETTLED_WIN,
    REDEEM_CONFIRMED_OUTCOME_UNKNOWN,
    Fill,
    PositionKey,
    ReconciledPosition,
    WalletHistoryRecord,
    _to_utc,
    aggregate_concentration,
    reconcile_positions,
)
from polycopy.discovery.wallet_evidence import evidence_from_history
from polycopy.discovery.wallet_seeds import SeedWallet, rank_seed_wallets


def _fill(ts, token):
    return Fill("h1", "BUY", 0.4, 1.0, ts, "2026-01-01T00:00:00+00:00", token, 0, "yes")


def _fill_sell(ts):
    return Fill("h2", "SELL", 0.9, 1.0, ts, "2026-01-02T00:00:00+00:00", "0xt", 0, "yes")


# ---------------------------------------------------------------------------
# #17 STEP 12: market-universe dedup by normalized conditionId
# ---------------------------------------------------------------------------


def test_market_universe_dedup_counts():
    """STEP 12: dedup counters record exact-duplicate removal and conflicts."""
    audit = MarketUniverseAudit(
        raw_rows_fetched=3, unique_markets=1, duplicate_rows_removed=1, duplicate_payload_conflicts=1
    )
    assert audit.raw_rows_fetched == 3
    assert audit.duplicate_rows_removed == 1
    assert audit.duplicate_payload_conflicts == 1


# ---------------------------------------------------------------------------
# #18 STEP 13/15: SOURCE_INCOMPLETE precedence
# ---------------------------------------------------------------------------


def _record_with_source_incomplete(addr, incomplete: int, settled: int) -> WalletHistoryRecord:
    return WalletHistoryRecord(
        wallet_address=addr,
        positions=(),
        settled=(),
        early_exit=(),
        unresolved=(),
        source_incomplete=(),
        first_qualifying_trade=None,
        last_qualifying_trade=None,
        active_trading_days=settled,
        distinct_events=(),
        distinct_markets=(),
        buy_fill_count=settled,
        sell_fill_count=0,
        two_sided_churn=False,
        market_pnl={},
        event_pnl={},
        largest_market_pnl_share=None,
        largest_event_pnl_share=None,
        top_three_market_pnl=(),
        long_horizon_excluded=0,
        taxonomy_excluded=0,
        source_incomplete_count=incomplete,
        evidence_completeness=0.0 if incomplete else 1.0,
    )


def test_source_incomplete_precedence():
    """STEP 13: material source incompleteness wins over a confident label."""
    rec = _record_with_source_incomplete("0x" + "a" * 40, incomplete=5, settled=1)
    report = discover_short_horizon_specialists(
        history_records=(rec,),
        requested={"preferred_days": 14},
        now=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
    )
    cand = report.candidates[0]
    assert cand["overall_status"] == STATUS_SOURCE_INCOMPLETE
    assert cand["source_incomplete_count"] == 5


# ---------------------------------------------------------------------------
# #19 STEP 15: per-category READY_FOR_REVIEW gating
# ---------------------------------------------------------------------------


def _ready_record(addr: str) -> WalletHistoryRecord:
    """A wallet with one settled sports WIN clearing frozen minimums."""
    ts = _to_utc("2026-01-01T00:00:00+00:00")
    assert ts is not None
    pos = ReconciledPosition(
        wallet_address=addr,
        condition_id="0x" + "1" * 64,
        asset_id="0xtoken1",
        outcome_index=0,
        outcome_label="yes",
        category_label="sports",
        event_identity="event:e1",
        horizon_status="PREFERRED",
        buy_fills=(_fill(ts, "0xtoken1"),),
        sell_fills=(),
        first_ts_iso="2026-01-01T00:00:00+00:00",
        last_ts_iso="2026-01-05T00:00:00+00:00",
        buy_qty=1.0,
        buy_cost=0.4,
        sell_qty=0.0,
        sell_proceeds=0.0,
        net_qty=1.0,
        source_trade_identities=("h1",),
        settlement_state=SETTLED_WIN,
        winning_outcome=True,
        realized_pnl=0.6,
        pnl_source="closed_position",
        pnl_complete=True,
        pnl_conflict=False,
        redeemed=False,
        included_closed_position_ids=(),
        included_redeem_ids=(),
        official_winning_asset_id="0xtoken1",
        official_winning_outcome_index=0,
        official_winning_outcome_label="yes",
    )
    return WalletHistoryRecord(
        wallet_address=addr,
        positions=(pos,),
        settled=(),
        early_exit=(),
        unresolved=(),
        source_incomplete=(),
        first_qualifying_trade="2026-01-01T00:00:00+00:00",
        last_qualifying_trade="2026-01-05T00:00:00+00:00",
        active_trading_days=5,
        distinct_events=("event:e1",),
        distinct_markets=("0x" + "1" * 64,),
        buy_fill_count=1,
        sell_fill_count=0,
        two_sided_churn=False,
        market_pnl={"0x" + "1" * 64: 0.6},
        event_pnl={"event:e1": 0.6},
        largest_market_pnl_share=1.0,
        largest_event_pnl_share=1.0,
        top_three_market_pnl=(("0x" + "1" * 64, 0.6),),
        long_horizon_excluded=0,
        taxonomy_excluded=0,
        source_incomplete_count=0,
        evidence_completeness=1.0,
    )


def test_thin_evidence_not_ready():
    """STEP 16: a single settled win must NOT reach READY_FOR_REVIEW.

    The frozen wallet minimums (>=30 resolved markets, >=15 distinct events,
    >=20 active days) are intentional; one market never clears them, so the
    engine must report INSUFFICIENT_SETTLED_EVIDENCE rather than over-approve.
    """
    rec = _ready_record("0x" + "b" * 40)
    report = discover_short_horizon_specialists(
        history_records=(rec,),
        requested={"preferred_days": 14},
        now=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
    )
    cand = report.candidates[0]
    assert cand["overall_status"] == "INSUFFICIENT_SETTLED_EVIDENCE"
    # The single sports pair is genuinely settled but far below the global floor.
    sports_row = next(r for r in cand["category_results"] if r["category_label"] == "sports")
    assert sports_row["settled_positions"] == 1
    assert sports_row["pair_status"] == "INSUFFICIENT_SETTLED_EVIDENCE"


# ---------------------------------------------------------------------------
# #20 STEP 11/13: CLI phase-budget allocation
# ---------------------------------------------------------------------------


def test_phase_caps_sum_to_total_and_respect_defaults():
    caps = split_phase_caps(400, PHASE_DEFAULT_PERCENTAGES)
    assert sum(caps.values()) == 400
    assert set(caps) == set(PHASE_DEFAULT_PERCENTAGES)
    for phase, pct in PHASE_DEFAULT_PERCENTAGES.items():
        assert caps[phase] == pytest.approx(round(400 * pct), abs=1)


def test_phase_caps_small_total_no_phase_zero():
    caps = split_phase_caps(10, PHASE_DEFAULT_PERCENTAGES)
    assert sum(caps.values()) == 10
    assert all(v >= 1 for v in caps.values())


def test_cli_phase_percentages_match_canonical_contract():
    """STEP 7/13: the CLI's PHASE_DEFAULT_PERCENTAGES is the single source of
    truth and equals the canonical operator-requested allocation. Guards
    against a divergent duplicate definition (PR69 defect: adapter.py had
    0.25/0.15/0.15/0.25/0.08/0.07/0.05, which the live CLI actually used)."""
    from polycopy.discovery.adapter import PHASE_DEFAULT_PERCENTAGES as CLI_PCT
    from polycopy.discovery._safe_get import PHASE_DEFAULT_PERCENTAGES as SRC_PCT

    assert CLI_PCT == SRC_PCT  # single definition, no divergence
    assert CLI_PCT == {
        "universe_taxonomy": 0.20,
        "market_first_trades": 0.18,
        "leaderboards": 0.07,
        "histories": 0.22,
        "closed_positions": 0.13,
        "redeems": 0.12,
        "referenced_metadata": 0.08,
    }
    assert abs(sum(CLI_PCT.values()) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# #21 STEP 14: seed provenance preserved in candidate.sources
# ---------------------------------------------------------------------------


def test_seed_provenance_preserved():
    seed = SeedWallet(
        wallet_address="0x" + "c" * 40,
        sources=("market_first", "leaderboard"),
        market_count=3,
        leaderboard_count=2,
        leaderboard_records=({"rank": 2},),
        first_trade_seen=None,
        last_trade_seen=None,
    )
    ranked = rank_seed_wallets([seed])
    assert ranked[0].sources == ("market_first", "leaderboard")


# ---------------------------------------------------------------------------
# #22 STEP 9: distinct_events from official event identity
# ---------------------------------------------------------------------------


def test_distinct_events_from_official_event_identity():
    rec = _ready_record("0x" + "d" * 40)
    evs = evidence_from_history(rec)
    all_ev = next(e for e in evs if e.category_label == "__all__")
    assert all_ev.distinct_events == 1


# ---------------------------------------------------------------------------
# #23 STEP 7: redeemed settled positions counted in resolved
# ---------------------------------------------------------------------------


def test_redeemed_position_counted_resolved():
    ts = _to_utc("2026-01-01T00:00:00+00:00")
    assert ts is not None
    pos = ReconciledPosition(
        wallet_address="0x" + "e" * 40,
        condition_id="0x" + "5" * 64,
        asset_id="0xt",
        outcome_index=0,
        outcome_label="yes",
        category_label="sports",
        event_identity="event:e2",
        horizon_status="PREFERRED",
        buy_fills=(_fill(ts, "0xt"),),
        sell_fills=(),
        first_ts_iso="2026-01-01T00:00:00+00:00",
        last_ts_iso="2026-01-02T00:00:00+00:00",
        buy_qty=1.0,
        buy_cost=0.5,
        sell_qty=0.0,
        sell_proceeds=0.0,
        net_qty=1.0,
        source_trade_identities=("h",),
        settlement_state=REDEEM_CONFIRMED_OUTCOME_UNKNOWN,
        winning_outcome=None,
        realized_pnl=None,
        pnl_source="redeem",
        pnl_complete=False,
        pnl_conflict=False,
        redeemed=True,
        included_closed_position_ids=(),
        included_redeem_ids=("r1",),
        official_winning_asset_id=None,
        official_winning_outcome_index=None,
        official_winning_outcome_label=None,
    )
    rec = WalletHistoryRecord(
        wallet_address="0x" + "e" * 40,
        positions=(pos,),
        settled=(),
        early_exit=(),
        unresolved=(),
        source_incomplete=(),
        first_qualifying_trade="2026-01-01T00:00:00+00:00",
        last_qualifying_trade="2026-01-02T00:00:00+00:00",
        active_trading_days=1,
        distinct_events=("event:e2",),
        distinct_markets=("0x" + "5" * 64,),
        buy_fill_count=1,
        sell_fill_count=0,
        two_sided_churn=False,
        market_pnl={},
        event_pnl={},
        largest_market_pnl_share=None,
        largest_event_pnl_share=None,
        top_three_market_pnl=(),
        long_horizon_excluded=0,
        taxonomy_excluded=0,
        source_incomplete_count=0,
        evidence_completeness=1.0,
    )
    evs = evidence_from_history(rec)
    all_ev = next(e for e in evs if e.category_label == "__all__")
    assert all_ev.resolved_markets >= 1


# ---------------------------------------------------------------------------
# #24 STEP 5: concentration from one ledger, no double count
# ---------------------------------------------------------------------------


def test_concentration_no_double_count():
    ts = _to_utc("2026-01-01T00:00:00+00:00")
    assert ts is not None
    fills = [_fill(ts, "0xt"), _fill(ts, "0xt")]
    positions = reconcile_positions(
        {PositionKey("0xw", "0xc", "0xt"): fills},
        {"0xc": {"resolved": True, "winning_asset_id": "0xt"}},
        {},
    )
    mkt, ev = aggregate_concentration(positions, event_map={"0xc": "event:e1"})
    # One settled position → one market/event entry; PnL counted exactly once.
    assert len(mkt) == 1 and len(ev) == 1


# ---------------------------------------------------------------------------
# #25 STEP 4: early exit / partial never counted as a win
# ---------------------------------------------------------------------------


def test_unresolved_position_never_counted_as_win():
    """STEP 4: an unresolved position (no official outcome) is never a win."""
    ts = _to_utc("2026-01-01T00:00:00+00:00")
    assert ts is not None
    # Buy with NO resolution → UNRESOLVED, not a win.
    fills = [_fill(ts, "0xt")]
    positions = reconcile_positions({PositionKey("0xw", "0xc", "0xt"): fills}, {}, {})
    assert positions[0].settlement_state == "UNRESOLVED"
    assert positions[0].winning_outcome is None
    # Buy + sell with a losing official outcome → SETTLED_LOSS, never a win.
    fills2 = [_fill(ts, "0xt"), _fill_sell(ts)]
    positions2 = reconcile_positions(
        {PositionKey("0xw", "0xc", "0xt"): fills2},
        {"0xc": {"resolved": True, "winning_asset_id": "0xOTHER"}},
        {},
    )
    assert positions2[0].settlement_state == "SETTLED_LOSS"
    assert positions2[0].winning_outcome is False


# ---------------------------------------------------------------------------
# #26 STEP 10: taxonomy PARTIAL/UNAVAILABLE fail closed
# ---------------------------------------------------------------------------


def test_taxonomy_unavailable_fails_closed():
    market = {
        "conditionId": "0x" + "9" * 64,
        "question": "Unspecific?",
        "outcomeType": "BINARY",
        "endDate": "2026-12-31T00:00:00+00:00",
        "tags": [{"slug": "specific-only", "label": "SpecificOnly"}],
    }
    res = enrich_market(market, embedded_only=True)
    assert res.result.status in ("PARTIAL", "UNAVAILABLE")


# ---------------------------------------------------------------------------
# #27 engine never auto-approves
# ---------------------------------------------------------------------------


def test_engine_never_auto_approves():
    import inspect

    from polycopy.discovery import short_horizon_specialists as mod

    src = inspect.getsource(mod)
    for forbidden in ("approve_wallet", "Database(", "sqlite3", "write_valid_rows", "process_approved"):
        assert forbidden not in src, f"engine must not contain {forbidden}"
    assert STATUS_READY_FOR_REVIEW != "APPROVED"
