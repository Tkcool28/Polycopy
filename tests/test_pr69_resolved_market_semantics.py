"""STEP 2 — Resolved-market semantics regression (PR69 final live validation).

Proves `resolved_markets` counts UNIQUE condition_id, not (condition, asset)
pairs, and that `settled_positions` counts distinct settled decision position
keys. A wallet trading both YES and NO in one condition must yield ONE resolved
market, not two.

Required proofs:
  * 2 assets in each of 20 resolved conditions -> settled_positions=40,
    resolved_markets=20, wallet does NOT clear the 30-resolved-market gate.
  * repeated fills do not increase either count.
  * 1 asset in 30 distinct conditions -> resolved_markets=30.
"""

from __future__ import annotations

from datetime import datetime

from polycopy.discovery.wallet_evidence import evidence_from_history
from polycopy.discovery.wallet_history import (
    SETTLED_WIN,
    ReconciledPosition,
    TradeFill,
)
from polycopy.scoring.wallet_score_v1 import GLOBAL_MIN_RESOLVED_MARKETS


def _fill(ts: str, price: float) -> TradeFill:
    dt = datetime.fromisoformat(ts)
    return TradeFill(
        transaction_hash="h", side="BUY", price=price, size=1.0,
        ts_utc=dt, ts_iso=ts, asset_id="0xaw", outcome_index=0, outcome_label="Yes",
    )


def _win_pos(wallet: str, cond: str, asset: str, *, fills=1) -> ReconciledPosition:
    f = _fill("2024-01-01T00:00:00+00:00", 0.5)
    buy = tuple(f for _ in range(fills))
    return ReconciledPosition(
        wallet_address=wallet, condition_id=cond, asset_id=asset,
        outcome_index=0, outcome_label="Yes", category_label="SPORTS",
        event_identity=f"event:{cond}", horizon_status="PREFERRED",
        buy_fills=buy, sell_fills=(), first_ts_iso="2024-01-01T00:00:00+00:00",
        last_ts_iso="2024-01-01T00:00:00+00:00", buy_qty=1.0, buy_cost=0.5,
        sell_qty=0.0, sell_proceeds=0.0, net_qty=1.0,
        source_trade_identities=("h",), settlement_state=SETTLED_WIN,
        winning_outcome=True, realized_pnl=0.5, pnl_source="fill_economics",
        pnl_complete=True, pnl_conflict=False, redeemed=False,
        included_closed_position_ids=(), included_redeem_ids=(),
        official_winning_asset_id=asset, official_winning_outcome_index=0,
        official_winning_outcome_label="Yes",
    )


def _record(wallet: str, positions: list[ReconciledPosition]):
    from polycopy.discovery.wallet_history import WalletHistoryRecord

    return WalletHistoryRecord(
        wallet_address=wallet, positions=tuple(positions),
        settled=(), early_exit=(), unresolved=(), source_incomplete=(),
        first_qualifying_trade=None, last_qualifying_trade=None,
        active_trading_days=1, distinct_events=(), distinct_markets=(),
        buy_fill_count=len(positions), sell_fill_count=0, two_sided_churn=False,
        market_pnl={}, event_pnl={}, largest_market_pnl_share=None,
        largest_event_pnl_share=None, top_three_market_pnl=(),
        long_horizon_excluded=0, taxonomy_excluded=0, source_incomplete_count=0,
        evidence_completeness=1.0,
    )


def _all_row(record) -> dict:
    rows = evidence_from_history(record)
    return next(r for r in rows if r.category_label == "__all__").as_dict()


def test_two_assets_in_20_conditions_resolved_markets_20():
    wallet = "0x" + "ab" + "0" * 38
    positions = []
    for i in range(20):
        cond = f"0xcond{i:02d}"
        positions.append(_win_pos(wallet, cond, "0xyes"))
        positions.append(_win_pos(wallet, cond, "0xno"))
    row = _all_row(_record(wallet, positions))
    assert row["settled_positions"] == 40, row["settled_positions"]
    assert row["resolved_markets"] == 20, row["resolved_markets"]
    # 20 < 30 gate -> does NOT clear the global resolved-market minimum.
    assert row["resolved_markets"] < GLOBAL_MIN_RESOLVED_MARKETS, row["resolved_markets"]


def test_repeated_fills_do_not_increase_counts():
    wallet = "0x" + "ab" + "0" * 38
    positions = []
    for i in range(20):
        cond = f"0xcond{i:02d}"
        # 5 fills per (condition, asset) — still one position key each.
        positions.append(_win_pos(wallet, cond, "0xyes", fills=5))
        positions.append(_win_pos(wallet, cond, "0xno", fills=5))
    row = _all_row(_record(wallet, positions))
    assert row["settled_positions"] == 40, row["settled_positions"]
    assert row["resolved_markets"] == 20, row["resolved_markets"]


def test_one_asset_in_30_conditions_resolved_markets_30():
    wallet = "0x" + "cd" + "0" * 38
    positions = [_win_pos(wallet, f"0xcond{i:02d}", "0xyes") for i in range(30)]
    row = _all_row(_record(wallet, positions))
    assert row["settled_positions"] == 30, row["settled_positions"]
    assert row["resolved_markets"] == 30, row["resolved_markets"]
    # 30 == gate minimum -> clears exactly at the threshold (boundary).
    assert row["resolved_markets"] >= GLOBAL_MIN_RESOLVED_MARKETS, row["resolved_markets"]
