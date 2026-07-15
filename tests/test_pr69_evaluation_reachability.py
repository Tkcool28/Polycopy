"""STEP 13 focused regressions for PR69 evaluation-reachability correction.

Proves the exact-head behaviors introduced by the evaluation-reachability
pass (head 2b934d93):

  * STEP 2  — stage counters make trades_seen -> positions auditable
  * STEP 3  — referenced metadata cached once per unique condition
  * STEP 4  — ``_resolve_official``: end date is NOT resolution evidence
  * STEP 5  — REDEEM existence is independent of payout
  * STEP 6  — EARLY_EXIT reachable (closed/redeemed before official resolution)
  * STEP 7  — one shared reconcile_positions used by tests + live path
  * STEP 8  — decision metrics separate outcome-unknown from wins/losses
  * STEP 9  — trade_count = settled_wins + settled_losses
  * STEP 12 — report consistency invariants hold

These do NOT weaken existing meaningful tests; they pin the new contract.
"""
from __future__ import annotations

import asyncio

from datetime import datetime, timezone

from polycopy.discovery.wallet_evidence import evidence_from_history
from polycopy.discovery.wallet_history import (
    EARLY_EXIT,
    Fill,
    OfficialResolution,
    PositionKey,
    REDEEM_CONFIRMED_OUTCOME_UNKNOWN,
    SETTLED_WIN,
    SOURCE_INCOMPLETE_STATE,
    UNRESOLVED,
    WalletHistoryFetcher,
    WalletHistoryRecord,
    _resolve_official,
    reconcile_positions,
)


def _fill(ts_iso: str, side: str, size: float, price: float, asset: str) -> Fill:
    from polycopy.discovery.wallet_history import _to_utc

    dt = _to_utc(ts_iso)
    assert dt is not None
    return Fill(
        transaction_hash="h",
        side=side,
        price=price,
        size=size,
        ts_utc=dt,
        ts_iso=ts_iso,
        asset_id=asset,
        outcome_index=0,
        outcome_label="Yes",
    )


W = "0xwallet"
C1 = "0xc1"
A = "0xasset"


# ── STEP 4: end date is NOT resolution evidence ───────────────────────────
def test_resolve_official_end_date_not_resolution():
    cls = type("C", (), {"condition_id": C1, "end_date_iso": "2024-01-01T00:00:00+00:00", "event_identity": None})()
    res = _resolve_official(C1, classification=cls, fetched=None)
    assert res.ended is True
    assert res.resolved is False  # end date alone never means resolved
    assert res.winning_asset_id is None
    assert res.resolution_missing_reasons  # explains why not resolved


def test_resolve_official_winner_from_fetched():
    cls = type("C", (), {"condition_id": C1, "end_date_iso": None, "event_identity": None})()
    fetched = {
        "closed": True,
        "resolved": True,
        "winningAssetId": A,
        "winningTokenId": A,
        "clobTokenIds": [A, "0xother"],
        "outcomePrices": ["0.98", "0.02"],
        "outcomes": ["Yes", "No"],
        "winningOutcomeIndex": 0,
        "winningOutcomeLabel": "Yes",
        "events": [{"id": "ev1"}],
    }
    res = _resolve_official(C1, classification=cls, fetched=fetched)
    assert res.resolved is True
    assert res.winning_asset_id == A
    assert res.winning_outcome_index == 0
    assert res.winning_outcome_label == "Yes"
    assert res.event_identity == "event:ev1"


def test_resolve_official_ended_but_winner_unknown():
    cls = type("C", (), {"condition_id": C1, "end_date_iso": "2024-01-01T00:00:00+00:00", "event_identity": None})()
    fetched = {"closed": True, "resolved": True, "winningAssetId": None, "outcomePrices": None}
    res = _resolve_official(C1, classification=cls, fetched=fetched)
    assert res.resolved is True
    assert res.winning_asset_id is None
    assert "resolved_but_winning_asset_missing" in res.resolution_missing_reasons


# ── STEP 5: REDEEM existence independent of payout ────────────────────────
def test_redeem_existence_independent_of_payout():
    res = OfficialResolution(condition_id=C1, resolved=False, closed=False,
                             winning_asset_id=None, winning_outcome_index=None,
                             winning_outcome_label=None, event_identity="ev1",
                             end_date_iso=None, ended=False, source="classification")
    positions = reconcile_positions(
        {PositionKey(W, C1, A): [_fill("2024-01-01T00:00:00+00:00", "BUY", 1.0, 0.5, A)]},
        {C1: res}, {"0xc1": "ev1"},
        redeem_by_position={(C1, A): [{"conditionId": C1, "assetId": A}]},  # no payout key
    )
    p = positions[0]
    assert p.redeemed is True           # redeem row present
    assert p.settlement_state == EARLY_EXIT  # no resolution -> early exit, not loss


# ── STEP 6: EARLY_EXIT reachable ───────────────────────────────────────────
def test_early_exit_closed_before_resolution_with_timing():
    res = OfficialResolution(condition_id=C1, resolved=False, closed=False,
                             winning_asset_id=None, winning_outcome_index=None,
                             winning_outcome_label=None, event_identity="ev1",
                             end_date_iso=None, ended=False, source="classification")
    ts = "2024-01-01T00:00:00+00:00"
    positions = reconcile_positions(
        {PositionKey(W, C1, A): [_fill(ts, "BUY", 1.0, 0.5, A),
                                 _fill("2024-01-05T00:00:00+00:00", "SELL", 1.0, 0.4, A)]},
        {C1: res}, {"0xc1": "ev1"},
        closed_by_position={(C1, A): [{"conditionId": C1, "assetId": A, "closedAt": ts}]},
    )
    assert positions[0].settlement_state == EARLY_EXIT


def test_early_exit_redeem_only_no_resolution():
    res = OfficialResolution(condition_id=C1, resolved=False, closed=False,
                             winning_asset_id=None, winning_outcome_index=None,
                             winning_outcome_label=None, event_identity="ev1",
                             end_date_iso=None, ended=False, source="classification")
    positions = reconcile_positions(
        {PositionKey(W, C1, A): [_fill("2024-01-01T00:00:00+00:00", "BUY", 1.0, 0.5, A)]},
        {C1: res}, {"0xc1": "ev1"},
        redeem_by_position={(C1, A): [{"conditionId": C1, "assetId": A}]},
    )
    assert positions[0].settlement_state == EARLY_EXIT


def test_source_incomplete_closed_no_timestamp():
    res = OfficialResolution(condition_id=C1, resolved=False, closed=False,
                             winning_asset_id=None, winning_outcome_index=None,
                             winning_outcome_label=None, event_identity="ev1",
                             end_date_iso=None, ended=False, source="classification")
    positions = reconcile_positions(
        {PositionKey(W, C1, A): [_fill("2024-01-01T00:00:00+00:00", "BUY", 1.0, 0.5, A)]},
        {C1: res}, {"0xc1": "ev1"},
        closed_by_position={(C1, A): [{"conditionId": C1, "assetId": A}]},  # no ts
    )
    assert positions[0].settlement_state == SOURCE_INCOMPLETE_STATE


def test_unresolved_no_evidence():
    res = OfficialResolution(condition_id=C1, resolved=False, closed=False,
                             winning_asset_id=None, winning_outcome_index=None,
                             winning_outcome_label=None, event_identity="ev1",
                             end_date_iso=None, ended=False, source="classification")
    positions = reconcile_positions(
        {PositionKey(W, C1, A): [_fill("2024-01-01T00:00:00+00:00", "BUY", 1.0, 0.5, A)]},
        {C1: res}, {"0xc1": "ev1"},
    )
    assert positions[0].settlement_state == UNRESOLVED


def test_redeem_confirmed_outcome_unknown():
    res = OfficialResolution(condition_id=C1, resolved=True, closed=True,
                             winning_asset_id=None, winning_outcome_index=None,
                             winning_outcome_label=None, event_identity="ev1",
                             end_date_iso=None, ended=True, source="gamma_market",
                             resolution_missing_reasons=("resolved_but_winning_asset_missing",))
    positions = reconcile_positions(
        {PositionKey(W, C1, A): [_fill("2024-01-01T00:00:00+00:00", "BUY", 1.0, 0.5, A)]},
        {C1: res}, {"0xc1": "ev1"},
        redeem_by_position={(C1, A): [{"conditionId": C1, "assetId": A}]},
    )
    assert positions[0].settlement_state == REDEEM_CONFIRMED_OUTCOME_UNKNOWN


# ── STEP 7: shared reconcile_positions returns a flat list ─────────────────
def test_reconcile_returns_flat_list_not_tuple():
    res = OfficialResolution(condition_id=C1, resolved=True, closed=True,
                             winning_asset_id=A, winning_outcome_index=0,
                             winning_outcome_label="Yes", event_identity="ev1",
                             end_date_iso=None, ended=False, source="gamma_market")
    out = reconcile_positions(
        {PositionKey(W, C1, A): [_fill("2024-01-01T00:00:00+00:00", "BUY", 1.0, 0.5, A)]},
        {C1: res}, {"0xc1": "ev1"},
    )
    assert isinstance(out, list)
    assert out[0].settlement_state == SETTLED_WIN


# ── STEP 2/8/9: decision metrics + stage counters ──────────────────────────
def test_stage_counters_and_score_inputs():
    res_win = OfficialResolution(condition_id="0xcw", resolved=True, closed=True,
                                 winning_asset_id="0xaw", winning_outcome_index=0,
                                 winning_outcome_label="Yes", event_identity="evw",
                                 end_date_iso=None, ended=False, source="gamma_market")
    res_lose = OfficialResolution(condition_id="0xcl", resolved=True, closed=True,
                                  winning_asset_id="0xaw", winning_outcome_index=0,
                                  winning_outcome_label="Yes", event_identity="evl",
                                  end_date_iso=None, ended=False, source="gamma_market")
    grouped = {
        PositionKey(W, "0xcw", "0xaw"): [_fill("2024-01-01T00:00:00+00:00", "BUY", 1.0, 0.5, "0xaw")],
        PositionKey(W, "0xcl", "0xal"): [_fill("2024-01-01T00:00:00+00:00", "BUY", 1.0, 0.5, "0xal")],
        PositionKey(W, "0xcu", "0xau"): [_fill("2024-01-01T00:00:00+00:00", "BUY", 1.0, 0.5, "0xau")],
    }
    resolutions = {
        "0xcw": res_win,
        "0xcl": res_lose,  # winner is 0xaw, asset held is 0xal -> loss
        "0xcu": OfficialResolution(condition_id="0xcu", resolved=True, closed=True,
                                   winning_asset_id=None, winning_outcome_index=None,
                                   winning_outcome_label=None, event_identity="evu",
                                   end_date_iso=None, ended=True, source="gamma_market",
                                   resolution_missing_reasons=("x",)),
    }
    counters = __import__("polycopy.discovery.wallet_history", fromlist=["HistoryStageCounters"]).HistoryStageCounters()
    positions = reconcile_positions(
        grouped, resolutions, {"0xcw": "evw", "0xcl": "evl", "0xcu": "evu"},
        counters=counters,
    )
    assert counters.positions_grouped == 3
    assert counters.settled_wins == 1
    assert counters.settled_losses == 1
    assert counters.resolved_outcome_unknown == 1
    assert counters.scoreable_positions == 2

    rec = WalletHistoryRecord(
        wallet_address=W, positions=tuple(positions), settled=(), early_exit=(),
        unresolved=(), source_incomplete=(), first_qualifying_trade=None,
        last_qualifying_trade=None, active_trading_days=1, distinct_events=(),
        distinct_markets=(), buy_fill_count=3, sell_fill_count=0,
        two_sided_churn=False, market_pnl={}, event_pnl={},
        largest_market_pnl_share=None, largest_event_pnl_share=None,
        top_three_market_pnl=(), long_horizon_excluded=0, taxonomy_excluded=0,
        source_incomplete_count=0, evidence_completeness=1.0, stage_counters=counters,
    )
    ev = evidence_from_history(rec)[0]
    assert ev.settled_wins == 1 and ev.settled_losses == 1 and ev.outcome_unknown == 1
    assert ev.win_rate == 0.5                      # wins / (wins+losses)
    assert ev.resolved_markets == 3                # includes outcome-unknown
    from polycopy.discovery.wallet_evidence import build_wallet_score_input_v1
    assert build_wallet_score_input_v1(ev).trade_count == 2  # wins+losses


# ── STEP 12: report consistency invariant (wins+losses == decision count) ──
def test_report_invariant_wins_losses_eq_decision():
    res = OfficialResolution(condition_id=C1, resolved=True, closed=True,
                             winning_asset_id=A, winning_outcome_index=0,
                             winning_outcome_label="Yes", event_identity="ev1",
                             end_date_iso=None, ended=False, source="gamma_market")
    from polycopy.discovery.wallet_history import HistoryStageCounters
    c = HistoryStageCounters()
    reconcile_positions(
        {PositionKey(W, C1, A): [_fill("2024-01-01T00:00:00+00:00", "BUY", 1.0, 0.5, A)]},
        {C1: res}, {"0xc1": "ev1"}, counters=c,
    )
    assert c.settled_wins + c.settled_losses == 1


# ── STEP 3: metadata cached once per unique condition (end-to-end) ─────────
def test_metadata_cache_once_per_condition():
    """STEP 3: every unique referenced condition is fetched exactly once via
    the live ``_fetch_one`` path; each extra fill for the same condition does
    not trigger a second GET."""

    from polycopy.discovery._safe_get import _RequestBudget
    from polycopy.discovery.wallet_seeds import SeedWallet

    calls = {"n": 0}
    WADDR = "0x" + "c0ffee" + "0" * 34  # valid 42-char 0x address

    class _FakeAdapter:
        async def wallet_trades(self, **kw):
            # Two distinct conditions, two fills each.
            return [
                {"conditionId": "0xca", "assetId": "0xaw", "side": "BUY",
                 "price": "0.5", "size": "1.0",
                 "timestamp": "2024-01-01T00:00:00+00:00",
                 "transactionHash": "h1", "user": WADDR},
                {"conditionId": "0xca", "assetId": "0xaw", "side": "BUY",
                 "price": "0.5", "size": "1.0",
                 "timestamp": "2024-01-01T00:00:00+00:00",
                 "transactionHash": "h2", "user": WADDR},
                {"conditionId": "0xcb", "assetId": "0xaw", "side": "BUY",
                 "price": "0.5", "size": "1.0",
                 "timestamp": "2024-01-01T00:00:00+00:00",
                 "transactionHash": "h3", "user": WADDR},
                {"conditionId": "0xcb", "assetId": "0xaw", "side": "BUY",
                 "price": "0.5", "size": "1.0",
                 "timestamp": "2024-01-01T00:00:00+00:00",
                 "transactionHash": "h4", "user": WADDR},
            ], []

        async def wallet_closed_positions(self, **kw):
            return [], []

        async def wallet_redeem_activity(self, **kw):
            return [], []

        async def get_market_raw(self, condition_id, **kw):
            calls["n"] += 1  # must be exactly 2 (one per unique condition)
            return {"closed": True, "resolved": True, "winningAssetId": "0xaw",
                    "clobTokenIds": ["0xaw"], "outcomePrices": ["1.0"],
                    "outcomes": ["Yes"], "events": [{"id": "ev1"}]}

        async def aclose(self):
            pass

    fetcher = WalletHistoryFetcher(
        _FakeAdapter(),
        budget=_RequestBudget(max_requests=100, phase_caps={"referenced_metadata": 100}),
        history_days=365, max_pages=1,
    )
    record, errors, trades_count, audit, resolutions = asyncio.run(
        fetcher._fetch_one(
            SeedWallet(wallet_address=WADDR, sources=("market_first",)),
            end_map={}, category_map={}, event_map={},
            classification_by_cond={}, as_of=datetime(2024, 6, 1, tzinfo=timezone.utc),
        )
    )
    assert calls["n"] == 2, f"expected 2 unique GETs, got {calls['n']}"
    assert record.stage_counters.positions_grouped == 2
    assert record.stage_counters.metadata_lookup_complete == 2
    assert len(record.positions) == 2


# ── Long-horizon / historical condition still grouped (not dropped) ────────
def test_historical_condition_grouped_not_dropped():
    # A condition whose end date is in the past (long horizon) but with an
    # official resolution must still produce a position (STEP 1 of the fix:
    # grouping happens before horizon filtering).
    res = OfficialResolution(condition_id=C1, resolved=True, closed=True,
                             winning_asset_id=A, winning_outcome_index=0,
                             winning_outcome_label="Yes", event_identity="ev1",
                             end_date_iso="2020-01-01T00:00:00+00:00", ended=True,
                             source="gamma_market")
    positions = reconcile_positions(
        {PositionKey(W, C1, A): [_fill("2024-01-01T00:00:00+00:00", "BUY", 1.0, 0.5, A)]},
        {C1: res}, {"0xc1": "ev1"},
    )
    assert len(positions) == 1
    assert positions[0].settlement_state == SETTLED_WIN
