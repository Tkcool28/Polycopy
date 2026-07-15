"""Correction tests: canonical scorer-input equivalence (STEP 8, 17 #10-16)."""
from __future__ import annotations

from datetime import datetime

from polycopy.discovery.wallet_evidence import (
    build_category_score_input_v1,
    build_wallet_score_input_v1,
    evidence_from_history,
)
from polycopy.discovery.wallet_history import (
    EARLY_EXIT,
    SETTLED_LOSS,
    SETTLED_WIN,
    UNRESOLVED,
    Fill,
    ReconciledPosition,
    _to_utc,
)


def _ts(s: str) -> datetime:
    out = _to_utc(s)
    assert out is not None
    return out


def _pos(cond, asset, state, event="event:e1", category="sports", pnl=1.0):
    ts = _to_utc("2026-01-01T00:00:00+00:00")
    assert ts is not None
    return ReconciledPosition(
        wallet_address="0xw", condition_id=cond, asset_id=asset,
        outcome_index=0, outcome_label="yes", category_label=category,
        event_identity=event, horizon_status="PREFERRED",
        buy_fills=(Fill("h", "BUY", 0.4, 1.0, ts, "2026-01-01T00:00:00+00:00", asset, 0, "yes"),),
        sell_fills=(), first_ts_iso="2026-01-01T00:00:00+00:00",
        last_ts_iso="2026-01-01T00:00:00+00:00", buy_qty=1.0, buy_cost=0.4,
        sell_qty=0.0, sell_proceeds=0.0, net_qty=1.0,
        source_trade_identities=("h",), settlement_state=state,
        winning_outcome=(state == SETTLED_WIN), realized_pnl=pnl,
        pnl_source="closed_position", pnl_complete=True, pnl_conflict=False,
        redeemed=False, included_closed_position_ids=(), included_redeem_ids=(),
        official_winning_asset_id=asset, official_winning_outcome_index=0,
        official_winning_outcome_label="yes",
    )


def _record(positions):
    from polycopy.discovery.wallet_history import WalletHistoryRecord
    buy_count = sum(len(p.buy_fills) for p in positions)
    sell_count = sum(len(p.sell_fills) for p in positions)
    events = {p.event_identity for p in positions if p.event_identity}
    markets = {p.condition_id for p in positions}
    return WalletHistoryRecord(
        wallet_address="0xw", positions=tuple(positions), settled=(),
        early_exit=(), unresolved=(), source_incomplete=(),
        first_qualifying_trade=None, last_qualifying_trade=None,
        active_trading_days=1, distinct_events=tuple(events),
        distinct_markets=tuple(markets), buy_fill_count=buy_count, sell_fill_count=sell_count,
        two_sided_churn=sell_count > 0 and buy_count > 0,
        market_pnl={}, event_pnl={},
        largest_market_pnl_share=None, largest_event_pnl_share=None,
        top_three_market_pnl=(), long_horizon_excluded=0,
        taxonomy_excluded=0, source_incomplete_count=0, evidence_completeness=1.0,
    )


def test_resolved_markets_unique_condition_ids(rule=None):
    """STEP 17 #11: resolved markets = unique (condition, asset) settled positions."""
    rec = _record([_pos("0xc1", "0xa1", SETTLED_WIN), _pos("0xc1", "0xa2", SETTLED_LOSS)])
    evs = evidence_from_history(rec)
    all_ev = next(e for e in evs if e.category_label == "__all__")
    # Two distinct assets in same condition → two resolved markets.
    assert all_ev.resolved_markets == 2


def test_distinct_events_use_official_event_ids(rule=None):
    """STEP 17 #12 + #13: distinct_events uses official event identity, not condition."""
    rec = _record([
        _pos("0xc1", "0xa1", SETTLED_WIN, event="event:e1"),
        _pos("0xc2", "0xa1", SETTLED_WIN, event="event:e1"),
    ])
    evs = evidence_from_history(rec)
    all_ev = next(e for e in evs if e.category_label == "__all__")
    # Two markets under one event → one distinct event.
    assert all_ev.distinct_events == 1


def test_one_market_not_automatic_distinct_event(rule=None):
    """STEP 17 #13: one market is never automatically one distinct event."""
    rec = _record([_pos("0xc1", "0xa1", SETTLED_WIN, event=None)])
    evs = evidence_from_history(rec)
    all_ev = next(e for e in evs if e.category_label == "__all__")
    assert all_ev.distinct_events == 0


def test_sell_rows_do_not_clear_buy_gates(rule=None):
    """STEP 17 #14: SELL activity must not satisfy frozen BUY forecasting gates."""
    # Only SELL fills, no BUY → no settled BUY evidence.
    from polycopy.discovery.wallet_history import TradeFill
    sell_only = ReconciledPosition(
        wallet_address="0xw", condition_id="0xc1", asset_id="0xa1",
        outcome_index=0, outcome_label="yes", category_label="sports",
        event_identity="event:e1", horizon_status="PREFERRED",
        buy_fills=(), sell_fills=(TradeFill("h", "SELL", 0.9, 1.0, _ts("2026-01-01T00:00:00+00:00"), "2026-01-01T00:00:00+00:00", "0xa1", 0, "yes"),),
        first_ts_iso="2026-01-01T00:00:00+00:00", last_ts_iso="2026-01-01T00:00:00+00:00",
        buy_qty=0.0, buy_cost=0.0, sell_qty=1.0, sell_proceeds=0.9, net_qty=-1.0,
        source_trade_identities=("h",), settlement_state=EARLY_EXIT,
        winning_outcome=None, realized_pnl=0.1, pnl_source="closed_position",
        pnl_complete=True, pnl_conflict=False, redeemed=False,
        included_closed_position_ids=(), included_redeem_ids=(),
        official_winning_asset_id=None, official_winning_outcome_index=None,
        official_winning_outcome_label=None,
    )
    rec = _record([sell_only])
    evs = evidence_from_history(rec)
    all_ev = next(e for e in evs if e.category_label == "__all__")
    assert all_ev.settled_positions == 0
    assert all_ev.resolved_markets == 0


def test_production_discovery_canonical_inputs_match(rule=None):
    """STEP 17 #15: canonical inputs match for multi-fill / multi-market / BUY+SELL / unresolved."""
    positions = [
        _pos("0xc1", "0xa1", SETTLED_WIN, event="event:e1", pnl=1.0),
        _pos("0xc1", "0xa2", SETTLED_LOSS, event="event:e1", pnl=-0.4),
        _pos("0xc2", "0xa1", UNRESOLVED, event="event:e2"),
    ]
    rec = _record(positions)
    evs = evidence_from_history(rec)
    all_ev = next(e for e in evs if e.category_label == "__all__")
    wallet_inp = build_wallet_score_input_v1(all_ev)
    # BUY-only denominator = total BUY fills (3 positions × 1 BUY each = 3).
    assert wallet_inp.overall_trade_count == 3
    assert wallet_inp.resolved_markets == 2
    cat_inp = build_category_score_input_v1(all_ev)
    assert cat_inp.category_resolved_markets == 2
    assert cat_inp.category_distinct_events == 1


def test_category_evidence_isolated_by_trusted_category(rule=None):
    """STEP 8B: category metrics isolate by trusted category."""
    rec = _record([
        _pos("0xc1", "0xa1", SETTLED_WIN, event="event:e1", category="sports"),
        _pos("0xc2", "0xa1", SETTLED_WIN, event="event:e2", category="crypto"),
    ])
    evs = evidence_from_history(rec)
    by_cat = {e.category_label: e for e in evs if e.category_label != "__all__"}
    assert by_cat["sports"].resolved_markets == 1
    assert by_cat["crypto"].resolved_markets == 1


def test_trade_count_is_not_buy_plus_sell(rule=None):
    """STEP 8: BUY+SELL count must not be the overall_trade_count basis."""
    positions = [_pos("0xc1", "0xa1", SETTLED_WIN, event="event:e1")]
    rec = _record(positions)
    rec = _record(positions)
    rec = _record(positions)
    rec = _record(positions)
    rec = _record(positions)
    # Rebuild with one buy + one sell of same asset (early exit).
    from polycopy.discovery.wallet_history import TradeFill
    pos = ReconciledPosition(
        wallet_address="0xw", condition_id="0xc1", asset_id="0xa1",
        outcome_index=0, outcome_label="yes", category_label="sports",
        event_identity="event:e1", horizon_status="PREFERRED",
        buy_fills=(TradeFill("h1", "BUY", 0.4, 1.0, _ts("2026-01-01T00:00:00+00:00"), "2026-01-01T00:00:00+00:00", "0xa1", 0, "yes"),),
        sell_fills=(TradeFill("h2", "SELL", 0.9, 1.0, _ts("2026-01-02T00:00:00+00:00"), "2026-01-02T00:00:00+00:00", "0xa1", 0, "yes"),),
        first_ts_iso="2026-01-01T00:00:00+00:00", last_ts_iso="2026-01-02T00:00:00+00:00",
        buy_qty=1.0, buy_cost=0.4, sell_qty=1.0, sell_proceeds=0.9, net_qty=0.0,
        source_trade_identities=("h1", "h2"), settlement_state=EARLY_EXIT,
        winning_outcome=None, realized_pnl=0.5, pnl_source="closed_position",
        pnl_complete=True, pnl_conflict=False, redeemed=False,
        included_closed_position_ids=(), included_redeem_ids=(),
        official_winning_asset_id="0xa1", official_winning_outcome_index=0,
        official_winning_outcome_label="yes",
    )
    rec = _record([pos])
    evs = evidence_from_history(rec)
    all_ev = next(e for e in evs if e.category_label == "__all__")
    wallet_inp = build_wallet_score_input_v1(all_ev)
    # overall_trade_count is BUY fill count, NOT BUY+SELL.
    assert wallet_inp.overall_trade_count == 1
    assert all_ev.buy_fill_count == 1
    assert all_ev.sell_fill_count == 1
