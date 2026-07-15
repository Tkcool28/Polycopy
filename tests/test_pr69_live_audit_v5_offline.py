"""STEP 15 — Live Audit V5 (depth-first) OFFLINE equivalent.

The production CLI requires Polymarket credentials (--allow-live) which are
NOT present in this environment, so a real network audit cannot run here.
This harness drives the EXACT same aggregate chain the live CLI uses —
``WalletHistoryFetcher.fetch`` -> per-wallet ``_fetch_one`` -> the single
shared ``reconcile_positions`` -> ``evidence_from_history`` — through a mock
adapter fed realistic multi-wallet data covering EVERY settlement state:

  W1: 2 SETTLED_WIN + 1 REDEEM_CONFIRMED_OUTCOME_UNKNOWN
  W2: 1 EARLY_EXIT (closed+ts) + 1 SOURCE_INCOMPLETE (closed no ts) + 1 UNRESOLVED
  W3: 1 SETTLED_LOSS (official winner is the OTHER asset)

It then asserts the report-level invariants (STEP 12/14): the stage counters
reconcile, the score inputs are honest (win/loss only, never outcome-unknown
counted as a loss), and EARLY_EXIT is reachable and excluded from wins.
"""

from __future__ import annotations

import asyncio

from datetime import datetime, timezone

from polycopy.discovery._safe_get import _RequestBudget
from polycopy.discovery.wallet_evidence import evidence_from_history
from polycopy.discovery.wallet_history import (
    EARLY_EXIT,
    REDEEM_CONFIRMED_OUTCOME_UNKNOWN,
    RESOLVED_OUTCOME_UNKNOWN,
    SETTLED_LOSS,
    SETTLED_WIN,
    SOURCE_INCOMPLETE_STATE,
    UNRESOLVED,
    WalletHistoryFetcher,
)
from polycopy.discovery.wallet_seeds import SeedWallet


W1 = "0x" + "a1" + "0" * 38
W2 = "0x" + "b2" + "0" * 38
W3 = "0x" + "c3" + "0" * 38

FILL_TS = "2024-01-01T00:00:00+00:00"
CLOSED_TS = "2024-03-01T00:00:00+00:00"


class _AuditAdapter:
    """Returns realistic, state-specific rows per wallet."""

    def __init__(self):
        self.gets = 0

    def _trades(self, wallet):
        if wallet == W1:
            return [
                {"conditionId": "0xw1a", "assetId": "0xaw", "side": "BUY", "price": "0.5",
                 "size": "1.0", "timestamp": FILL_TS, "transactionHash": "t1", "user": W1},
                {"conditionId": "0xw1b", "assetId": "0xaw", "side": "BUY", "price": "0.4",
                 "size": "1.0", "timestamp": FILL_TS, "transactionHash": "t2", "user": W1},
                {"conditionId": "0xw1c", "assetId": "0xaw", "side": "BUY", "price": "0.5",
                 "size": "1.0", "timestamp": FILL_TS, "transactionHash": "t3", "user": W1},
            ]
        if wallet == W2:
            return [
                {"conditionId": "0xw2a", "assetId": "0xaw", "side": "BUY", "price": "0.5",
                 "size": "1.0", "timestamp": FILL_TS, "transactionHash": "t4", "user": W2},
                {"conditionId": "0xw2b", "assetId": "0xaw", "side": "BUY", "price": "0.5",
                 "size": "1.0", "timestamp": FILL_TS, "transactionHash": "t5", "user": W2},
                {"conditionId": "0xw2c", "assetId": "0xaw", "side": "BUY", "price": "0.5",
                 "size": "1.0", "timestamp": FILL_TS, "transactionHash": "t6", "user": W2},
            ]
        return [
            {"conditionId": "0xw3a", "assetId": "0xaw", "side": "BUY", "price": "0.5",
             "size": "1.0", "timestamp": FILL_TS, "transactionHash": "t7", "user": W3},
        ]

    def _closed(self, wallet):
        if wallet == W2:
            return [
                {"conditionId": "0xw2a", "assetId": "0xaw", "closedAt": CLOSED_TS, "user": W2},
                {"conditionId": "0xw2b", "assetId": "0xaw", "closedAt": None, "user": W2},
            ]
        return []

    def _redeem(self, wallet):
        if wallet == W1:
            return [{"conditionId": "0xw1c", "assetId": "0xaw", "user": W1}]
        return []

    async def wallet_trades(self, **kw):
        return self._trades(kw.get("wallet_address", "")), []

    async def wallet_closed_positions(self, **kw):
        return self._closed(kw.get("wallet_address", "")), []

    async def wallet_redeem_activity(self, **kw):
        return self._redeem(kw.get("wallet_address", "")), []

    async def get_market_raw(self, condition_id, **kw):
        self.gets += 1
        if condition_id in ("0xw1a", "0xw1b"):
            return {"closed": True, "resolved": True, "winningAssetId": "0xaw",
                    "clobTokenIds": ["0xaw", "0xother"], "outcomePrices": ["0.98", "0.02"],
                    "winningOutcomeIndex": 0, "winningOutcomeLabel": "Yes",
                    "events": [{"id": f"ev-{condition_id}"}]}
        if condition_id == "0xw1c":
            # Redeemed but winner unproven by official feed.
            return {"closed": True, "resolved": True, "winningAssetId": None,
                    "clobTokenIds": ["0xaw", "0xother"], "outcomePrices": None,
                    "events": [{"id": "ev-w1c"}]}
        if condition_id == "0xw3a":
            return {"closed": True, "resolved": True, "winningAssetId": "0xother",
                    "clobTokenIds": ["0xaw", "0xother"], "outcomePrices": ["0.02", "0.98"],
                    "winningOutcomeIndex": 1, "winningOutcomeLabel": "No",
                    "events": [{"id": "ev-w3a"}]}
        return {"closed": False, "resolved": False, "winningAssetId": None,
                "clobTokenIds": ["0xaw", "0xother"], "outcomePrices": None,
                "events": [{"id": f"ev-{condition_id}"}]}

    async def aclose(self):
        pass


def _run():
    adapter = _AuditAdapter()
    fetcher = WalletHistoryFetcher(
        adapter,
        budget=_RequestBudget(max_requests=200, phase_caps={"referenced_metadata": 100}),
        history_days=730, max_pages=1,
    )
    seeds = [
        SeedWallet(wallet_address=W1, sources=("market_first",)),
        SeedWallet(wallet_address=W2, sources=("market_first",)),
        SeedWallet(wallet_address=W3, sources=("market_first",)),
    ]
    as_of = datetime(2024, 6, 1, tzinfo=timezone.utc)
    return asyncio.run(fetcher.fetch(seeds=seeds, classifications=[], as_of=as_of)), adapter


def test_live_audit_v5_offline_depth_first():
    report, adapter = _run()
    c = report.stage_counters

    assert c.trades_fetched == 7, c.trades_fetched
    assert c.positions_grouped == 7, c.positions_grouped

    assert c.settled_wins == 2, c.settled_wins
    assert c.settled_losses == 1, c.settled_losses
    assert c.early_exit_positions == 1, c.early_exit_positions
    assert c.source_incomplete_count == 1, c.source_incomplete_count
    assert c.unresolved_positions == 1, c.unresolved_positions
    assert c.redeem_confirmed_outcome_unknown == 1, c.redeem_confirmed_outcome_unknown
    assert c.resolved_outcome_unknown == 0, c.resolved_outcome_unknown

    # Unique unresolved/redeem conditions fetched once: w1a,w1b,w1c,w2a,w2b,w2c,w3a = 7.
    assert adapter.gets == 7, adapter.gets

    wins = [p for w in report.wallets for p in w.positions if p.settlement_state == SETTLED_WIN]
    losses = [p for w in report.wallets for p in w.positions if p.settlement_state == SETTLED_LOSS]
    assert len(wins) == 2 and len(losses) == 1
    # Wallet-wide evidence rows across all wallets: totals must be honest.
    total_w = 0
    total_l = 0
    for w in report.wallets:
        if not w.positions:
            continue
        ev_rows = evidence_from_history(w)
        all_row = next(r for r in ev_rows if r.category_label == "__all__")
        total_w += all_row.settled_wins
        total_l += all_row.settled_losses
    assert total_w == 2, total_w
    assert total_l == 1, total_l

    all_states = [p.settlement_state for w in report.wallets for p in w.positions]
    assert EARLY_EXIT in all_states
    assert REDEEM_CONFIRMED_OUTCOME_UNKNOWN in all_states
    assert UNRESOLVED in all_states
    assert SOURCE_INCOMPLETE_STATE in all_states

    resolved = {p.condition_id for p in (wins + losses +
                 [p for w in report.wallets for p in w.positions
                  if p.settlement_state in (RESOLVED_OUTCOME_UNKNOWN, REDEEM_CONFIRMED_OUTCOME_UNKNOWN)])}
    assert len(resolved) == 4, resolved  # w1a, w1b, w1c, w3a
