"""Canonical wallet/category evidence builder for PR69 discovery.

The single point of truth that converts reconciled history evidence into
the canonical typed input objects expected by the frozen specialist
scorers. Both the discovery CLI and (optionally) the production bridge
ingestion pipeline MUST go through this builder so a single source
fixture yields identical scoring inputs on both sides.

The build helpers are pure; they take a :class:`WalletHistoryRecord` (now
rolled up from **positions**, not per-fill) and return:
  * :class:`polycopy.scoring.wallet_score_v1.WalletScoreInputV1` for
    the wallet-wide score.
  * :class:`polycopy.scoring.category_wallet_score_v1.CategoryWalletScoreInputV1`
    for the per-category score.

These are the EXACT typed input objects the production path constructs
via :func:`polycopy.scoring.wallet_evidence.build_wallet_score_input_v1`
and :func:`polycopy.scoring.wallet_evidence.build_category_score_input_v1`,
so an identical canonical fixture MUST produce identical inputs on both
sides — and therefore identical scores, missing essentials, and gate
failures. This is the "scorer reuse proof" the operator audit relies on.

PR69 correction (evaluation correctness):
  * BUY-only forecasting evidence (SELL activity is coverage, never a
    settled win/loss gate).
  * Unique resolved markets = unique (condition, asset) positions that
    settled, NOT ``len(settled fills)``.
  * distinct_events uses the official event identity, never condition IDs
    and never category labels.
  * realized PnL comes from one position-level ledger (no per-fill
    multiplication).
  * The overall ``trade_count`` denominator is the number of distinct
    settled BUY positions (matching PR67's resolved_buy_trades basis),
    never BUY+SELL.
  * Category evidence is isolated by trusted category.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from polycopy.discovery.wallet_history import (
    EARLY_EXIT,
    RESOLVED_OUTCOME_UNKNOWN,
    REDEEM_CONFIRMED_OUTCOME_UNKNOWN,
    SETTLED_LOSS,
    SETTLED_WIN,
    UNRESOLVED,
    WalletHistoryRecord,
)
from polycopy.scoring.category_wallet_score_v1 import CategoryWalletScoreInputV1
from polycopy.scoring.wallet_score_v1 import WalletScoreInputV1


@dataclass(frozen=True)
class WalletCategoryEvidence:
    """Per-wallet+category roll-up used by the scorer.

    Only positions that passed the horizon gate and had a USABLE category
    at trade-time participate. Early-exit positions are exposed in coverage
    but excluded from settled win/loss counts per spec.
    """

    wallet_address: str
    category_label: str
    qualifying_positions: int
    settled_positions: int
    settled_wins: int
    settled_losses: int
    outcome_unknown: int
    early_exits: int
    unresolved_positions: int
    redeemed_positions: int
    realized_qualifying_pnl: float | None
    win_rate: float | None
    profit_factor: float | None
    active_trading_days: int
    first_qualifying_trade: str | None
    last_qualifying_trade: str | None
    resolved_markets: int
    distinct_events: int
    distinct_markets: int
    buy_fill_count: int
    sell_fill_count: int
    two_sided_churn: bool
    largest_market_pnl_share: float | None
    largest_event_pnl_share: float | None
    # Raw counts (kept for audit and for PR67 wallet-evidence parity).
    long_horizon_excluded: int
    taxonomy_excluded: int
    source_incomplete_count: int
    evidence_completeness: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "wallet_address": self.wallet_address,
            "category_label": self.category_label,
            "qualifying_positions": self.qualifying_positions,
            "settled_positions": self.settled_positions,
            "settled_wins": self.settled_wins,
            "settled_losses": self.settled_losses,
            "outcome_unknown": self.outcome_unknown,
            "early_exits": self.early_exits,
            "unresolved_positions": self.unresolved_positions,
            "redeemed_positions": self.redeemed_positions,
            "realized_qualifying_pnl": self.realized_qualifying_pnl,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "active_trading_days": self.active_trading_days,
            "first_qualifying_trade": self.first_qualifying_trade,
            "last_qualifying_trade": self.last_qualifying_trade,
            "resolved_markets": self.resolved_markets,
            "distinct_events": self.distinct_events,
            "distinct_markets": self.distinct_markets,
            "buy_fill_count": self.buy_fill_count,
            "sell_fill_count": self.sell_fill_count,
            "two_sided_churn": self.two_sided_churn,
            "largest_market_pnl_share": self.largest_market_pnl_share,
            "largest_event_pnl_share": self.largest_event_pnl_share,
            "long_horizon_excluded": self.long_horizon_excluded,
            "taxonomy_excluded": self.taxonomy_excluded,
            "source_incomplete_count": self.source_incomplete_count,
            "evidence_completeness": self.evidence_completeness,
        }


def evidence_from_history(
    record: WalletHistoryRecord,
    *,
    category_label: str | None = None,
) -> tuple[WalletCategoryEvidence, ...]:
    """Roll up a wallet's history into one evidence row per category.

    Args:
        record: a fully reconciled wallet history row (position-level).
        category_label: when set, restricts the rollup to a single
            category (the per-category score path). When ``None``,
            returns one row per category plus a wallet-wide row
            (where ``category_label='__all__'``).
    """
    positions = list(record.positions)

    def scope(pos: Iterable) -> list:
        if category_label is None:
            return list(pos)
        return [p for p in pos if (p.category_label or "__unknown__") == category_label]

    def build_one(label: str, scoped_positions: list) -> WalletCategoryEvidence:
        decision = [p for p in scoped_positions if p.settlement_state in (SETTLED_WIN, SETTLED_LOSS)]
        outcome_unknown_positions = [p for p in scoped_positions if p.settlement_state in (RESOLVED_OUTCOME_UNKNOWN, REDEEM_CONFIRMED_OUTCOME_UNKNOWN)]
        early = [p for p in scoped_positions if p.settlement_state == EARLY_EXIT]
        unresolved = [p for p in scoped_positions if p.settlement_state == UNRESOLVED]
        settled = decision + outcome_unknown_positions
        qualifying = settled + early + unresolved

        wins = sum(1 for p in decision if p.settlement_state == SETTLED_WIN)
        losses = sum(1 for p in decision if p.settlement_state == SETTLED_LOSS)
        outcome_unknown = len(outcome_unknown_positions)
        redeemed = sum(1 for p in settled if p.redeemed)

        # PnL aggregation — one canonical position-level ledger.
        complete_pnl: list[float] = []
        for p in settled:
            if p.pnl_conflict:
                # CONFLICT PnL is excluded from scoring (per spec).
                continue
            if p.realized_pnl is not None:
                complete_pnl.append(p.realized_pnl)
        total_pnl = sum(complete_pnl) if (complete_pnl and not any(p.pnl_conflict for p in settled)) else None
        if any(p.pnl_conflict for p in settled):
            total_pnl = None
        win_rate = (wins / (wins + losses)) if (wins + losses) > 0 else None
        gross_gain = sum(max(0.0, v) for v in complete_pnl)
        gross_loss = -sum(min(0.0, v) for v in complete_pnl)
        profit_factor = (gross_gain / gross_loss) if (complete_pnl and gross_loss > 0) else None

        # Active trading days derived from the actual position fill timestamps.
        active_days: set[str] = set()
        first_qualifying = None
        last_qualifying = None
        for p in qualifying:
            for f in p.buy_fills + p.sell_fills:
                if f.ts_iso:
                    d = f.ts_iso[:10]
                    active_days.add(d)
                    if first_qualifying is None or f.ts_iso < first_qualifying:
                        first_qualifying = f.ts_iso
                    if last_qualifying is None or f.ts_iso > last_qualifying:
                        last_qualifying = f.ts_iso

        # Resolved markets = unique (condition, asset) settled positions
        # (incl. outcome-unknown/redeem-unknown — the market resolved; only
        # UNRESOLVED/EARLY_EXIT/SOURCE_INCOMPLETE are excluded). Win-rate and
        # score trade_count use the decision subset (wins+losses) below.
        resolved_market_keys = {(p.condition_id, p.asset_id) for p in settled}
        distinct_events = sorted({p.event_identity for p in settled if p.event_identity})
        distinct_markets = sorted({p.condition_id for p in settled})

        return WalletCategoryEvidence(
            wallet_address=record.wallet_address,
            category_label=label,
            qualifying_positions=len(qualifying),
            settled_positions=len(decision),
            settled_wins=wins,
            settled_losses=losses,
            outcome_unknown=outcome_unknown,
            early_exits=len(early),
            unresolved_positions=len(unresolved),
            redeemed_positions=redeemed,
            realized_qualifying_pnl=total_pnl,
            win_rate=win_rate,
            profit_factor=profit_factor,
            active_trading_days=len(active_days),
            first_qualifying_trade=first_qualifying,
            last_qualifying_trade=last_qualifying,
            resolved_markets=len(resolved_market_keys),
            distinct_events=len(distinct_events),
            distinct_markets=len(distinct_markets),
            buy_fill_count=record.buy_fill_count,
            sell_fill_count=record.sell_fill_count,
            two_sided_churn=record.two_sided_churn,
            largest_market_pnl_share=record.largest_market_pnl_share,
            largest_event_pnl_share=record.largest_event_pnl_share,
            long_horizon_excluded=record.long_horizon_excluded,
            taxonomy_excluded=record.taxonomy_excluded,
            source_incomplete_count=record.source_incomplete_count,
            evidence_completeness=record.evidence_completeness,
        )

    if category_label is not None:
        return (build_one(category_label, scope(positions)),)

    by_category: dict[str, list] = {}
    for p in positions:
        by_category.setdefault(p.category_label or "__unknown__", []).append(p)
    out: list[WalletCategoryEvidence] = []
    for label, group in sorted(by_category.items()):
        out.append(build_one(label, group))
    out.append(build_one("__all__", list(positions)))
    return tuple(out)


def build_wallet_score_input_v1(
    evidence: WalletCategoryEvidence,
    *,
    overall_trade_count: int | None = None,
) -> WalletScoreInputV1:
    """Construct the canonical typed input for the wallet-wide scorer.

    Pure; does not call the scorer. Mirrors PR67's persisted BUY-evidence
    denominator: ``overall_trade_count`` is the total BUY fill count
    (the PR67 ``total_buy_trades`` basis), while ``trade_count`` is the
    number of **distinct settled BUY positions** (PR67's
    ``resolved_buy_trades`` denominator). Category counts are isolated.
    """
    if overall_trade_count is None:
        overall_trade_count = evidence.buy_fill_count
    return WalletScoreInputV1(
        wallet_id=evidence.wallet_address,
        trade_count=(evidence.settled_wins + evidence.settled_losses) or None,
        win_rate=evidence.win_rate,
        profit_factor=evidence.profit_factor,
        sample_fraction=0.0,
        category_trade_count=None,
        category_distinct_markets=None,
        overall_trade_count=overall_trade_count,
        largest_winner_share=evidence.largest_market_pnl_share,
        top_3_concentration=None,
        resolved_markets=evidence.resolved_markets,
        active_trading_days=evidence.active_trading_days,
        distinct_events=evidence.distinct_events,
        category_resolved_markets=None,
        category_distinct_events=None,
        category_active_days=None,
    )


def build_category_score_input_v1(
    evidence: WalletCategoryEvidence,
    *,
    overall_trade_count: int | None = None,
) -> CategoryWalletScoreInputV1:
    """Construct the canonical typed input for the per-category scorer.

    BUY-only basis: ``category_trade_count`` is the count of qualifying
    BUY positions (not BUY+SELL); ``trade_count`` is the distinct settled
    BUY positions. The category gates consume ``category_resolved_markets``
    (unique settled positions in the category) and ``category_distinct_events``.
    """
    if overall_trade_count is None:
        overall_trade_count = evidence.buy_fill_count
    return CategoryWalletScoreInputV1(
        wallet_id=evidence.wallet_address,
        category_label=evidence.category_label,
        trade_count=(evidence.settled_wins + evidence.settled_losses) or None,
        win_rate=evidence.win_rate,
        profit_factor=evidence.profit_factor,
        sample_fraction=0.0,
        category_trade_count=evidence.qualifying_positions,
        category_distinct_markets=evidence.distinct_markets,
        overall_trade_count=overall_trade_count,
        largest_winner_share=evidence.largest_market_pnl_share,
        top_3_concentration=None,
        category_resolved_markets=evidence.resolved_markets,
        category_distinct_events=evidence.distinct_events,
        category_active_days=evidence.active_trading_days,
    )


def _fingerprint(evidence: WalletCategoryEvidence) -> str:
    """Deterministic evidence fingerprint for idempotency keys."""
    payload = evidence.as_dict()
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


__all__ = [
    "WalletCategoryEvidence",
    "build_wallet_score_input_v1",
    "build_category_score_input_v1",
    "evidence_from_history",
]
