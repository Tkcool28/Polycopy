"""Canonical wallet/category evidence builder for PR69 discovery.

The single point of truth that converts reconciled history evidence into
the canonical typed input objects expected by the frozen specialist
scorers. Both the discovery CLI and (optionally) the production bridge
ingestion pipeline MUST go through this builder so a single source
fixture yields identical scoring inputs on both sides.

The build helpers are pure; they take a :class:`WalletHistoryRecord` and
return:
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

The shared builder lives here (NOT duplicated) and is consumed by
``short_horizon_specialists`` for scoring. Production code can also call
it on a populated WalletEvidence by passing the per-account metrics it
already knows — the input shape is identical.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from polycopy.discovery.wallet_history import (
    EarlyExitEvidence,
    SettledEvidence,
    UnresolvedEvidence,
    WalletHistoryRecord,
)
from polycopy.scoring.category_wallet_score_v1 import CategoryWalletScoreInputV1
from polycopy.scoring.wallet_score_v1 import WalletScoreInputV1


@dataclass(frozen=True)
class WalletCategoryEvidence:
    """Per-wallet+category roll-up used by the scorer.

    Only rows that passed the horizon gate and had a USABLE category at
    trade-time participate. Early-exit rows are exposed in coverage but
    excluded from settled win/loss counts per spec.
    """

    wallet_address: str
    category_label: str
    qualifying_trades: int
    preferred_trades: int
    preferred_share: float
    hard_eligible_share: float
    settled_trades: int
    settled_wins: int
    settled_losses: int
    redeemed_trades: int
    resolved_without_redeem: int
    early_exits: int
    unresolved_trades: int
    realized_qualifying_pnl: float | None
    win_rate: float | None
    profit_factor: float | None
    active_trading_days: int
    first_qualifying_trade: str | None
    last_qualifying_trade: str | None
    resolved_markets: int
    distinct_events: int
    buy_count: int
    sell_count: int
    two_sided_churn: bool
    largest_market_pnl_share: float | None
    largest_event_pnl_share: float | None

    # Raw counts (kept for audit and for PR67 wallet-evidence parity).
    long_horizon_excluded: int
    taxonomy_excluded: int
    source_incomplete: int
    evidence_completeness: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "wallet_address": self.wallet_address,
            "category_label": self.category_label,
            "qualifying_trades": self.qualifying_trades,
            "preferred_trades": self.preferred_trades,
            "preferred_share": self.preferred_share,
            "hard_eligible_share": self.hard_eligible_share,
            "settled_trades": self.settled_trades,
            "settled_wins": self.settled_wins,
            "settled_losses": self.settled_losses,
            "redeemed_trades": self.redeemed_trades,
            "resolved_without_redeem": self.resolved_without_redeem,
            "early_exits": self.early_exits,
            "unresolved_trades": self.unresolved_trades,
            "realized_qualifying_pnl": self.realized_qualifying_pnl,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "active_trading_days": self.active_trading_days,
            "first_qualifying_trade": self.first_qualifying_trade,
            "last_qualifying_trade": self.last_qualifying_trade,
            "resolved_markets": self.resolved_markets,
            "distinct_events": self.distinct_events,
            "buy_count": self.buy_count,
            "sell_count": self.sell_count,
            "two_sided_churn": self.two_sided_churn,
            "largest_market_pnl_share": self.largest_market_pnl_share,
            "largest_event_pnl_share": self.largest_event_pnl_share,
            "long_horizon_excluded": self.long_horizon_excluded,
            "taxonomy_excluded": self.taxonomy_excluded,
            "source_incomplete": self.source_incomplete,
            "evidence_completeness": self.evidence_completeness,
        }


def evidence_from_history(
    record: WalletHistoryRecord,
    *,
    category_label: str | None = None,
) -> tuple[WalletCategoryEvidence, ...]:
    """Roll up a wallet's history into one evidence row per category.

    Args:
        record: a fully reconciled wallet history row.
        category_label: when set, restricts the rollup to a single
            category (the per-category score path). When ``None``,
            returns one row per category plus a wallet-wide row
            (where ``category_label='__all__'``).
    """
    settled: list[SettledEvidence] = list(record.settled)
    early: list[EarlyExitEvidence] = list(record.early_exit)
    unresolved: list[UnresolvedEvidence] = list(record.unresolved)

    def scope(filter_: Iterable[SettledEvidence] | Iterable[UnresolvedEvidence] | Iterable[EarlyExitEvidence]):
        if category_label is None:
            return list(filter_)
        return [ev for ev in filter_ if getattr(ev, "category_label", None) == category_label]

    def build_one(label: str, settled_scope: list, early_scope: list, unresolved_scope: list) -> WalletCategoryEvidence:
        qualifying = settled_scope + early_scope + unresolved_scope
        # PnL aggregation for the canonical evidence:
        complete_pnl: list[float] = []
        partial_pnl_list: list[float] = []
        wins = 0
        losses = 0
        redeemed = 0
        resolved_no_redeem = 0
        for ev in settled_scope:
            if ev.winning_outcome:
                wins += 1
            else:
                losses += 1
            if ev.redeemed:
                redeemed += 1
            if ev.settled_realized_pnl is not None:
                complete_pnl.append(ev.settled_realized_pnl)
        for ev in early_scope:
            if ev.realized_pnl is not None:
                partial_pnl_list.append(ev.realized_pnl)
        pnl_known = len(complete_pnl) == len(settled_scope)
        total_pnl = sum(complete_pnl) if pnl_known else None
        win_rate = (wins / len(settled_scope)) if settled_scope else None
        gross_gain = sum(max(0.0, v) for v in complete_pnl)
        gross_loss = -sum(min(0.0, v) for v in complete_pnl)
        profit_factor = (gross_gain / gross_loss) if pnl_known and gross_loss > 0 else None
        preferred_trades = len([ev for ev in qualifying if getattr(ev, "horizon_status", "") == "HORIZON_PREFERRED"])
        qualifying_count = len(qualifying)
        preferred_share = (preferred_trades / qualifying_count) if qualifying_count else 0.0
        hard_eligible_share = 1.0 if (qualifying_count > 0) else 0.0
        first_qualifying = None
        last_qualifying = None
        active_days = set()
        for ev in qualifying:
            ts = getattr(ev, "timestamp", "") or ""
            if ts:
                if first_qualifying is None or ts < first_qualifying:
                    first_qualifying = ts
                if last_qualifying is None or ts > last_qualifying:
                    last_qualifying = ts
                if len(ts) >= 10:
                    active_days.add(ts[:10])
        # Resolved-without-redeem is a coverage metric settled outputs cannot
        # deliver because redemption status is the REDEEM probe.
        resolved_no_redeem = max(0, len(settled_scope) - redeemed)

        distinct_events = sorted({ev.market_condition_id for ev in settled_scope})

        return WalletCategoryEvidence(
            wallet_address=record.wallet_address,
            category_label=label,
            qualifying_trades=qualifying_count,
            preferred_trades=preferred_trades,
            preferred_share=preferred_share,
            hard_eligible_share=hard_eligible_share,
            settled_trades=len(settled_scope),
            settled_wins=wins,
            settled_losses=losses,
            redeemed_trades=redeemed,
            resolved_without_redeem=resolved_no_redeem,
            early_exits=len(early_scope),
            unresolved_trades=len(unresolved_scope),
            realized_qualifying_pnl=total_pnl,
            win_rate=win_rate,
            profit_factor=profit_factor,
            active_trading_days=len(active_days),
            first_qualifying_trade=first_qualifying,
            last_qualifying_trade=last_qualifying,
            resolved_markets=len(settled_scope),
            distinct_events=len(distinct_events),
            buy_count=record.buy_count,
            sell_count=record.sell_count,
            two_sided_churn=record.two_sided_churn,
            largest_market_pnl_share=record.largest_market_pnl_share,
            largest_event_pnl_share=record.largest_event_pnl_share,
            long_horizon_excluded=record.long_horizon_excluded,
            taxonomy_excluded=record.taxonomy_excluded,
            source_incomplete=record.source_incomplete,
            evidence_completeness=record.evidence_completeness,
        )

    if category_label is not None:
        # One shot.
        return (build_one(category_label, scope(settled), scope(early), scope(unresolved)),)

    by_category: dict[str, dict[str, list]] = {}
    for ev in settled:
        by_category.setdefault(ev.category_label or "__unknown__", {"s": [], "e": [], "u": []})["s"].append(ev)
    for ev in early:
        by_category.setdefault(ev.category_label or "__unknown__", {"s": [], "e": [], "u": []})["e"].append(ev)
    for ev in unresolved:
        by_category.setdefault(ev.category_label or "__unknown__", {"s": [], "e": [], "u": []})["u"].append(ev)
    out: list[WalletCategoryEvidence] = []
    for label, groups in sorted(by_category.items()):
        out.append(build_one(label, groups["s"], groups["e"], groups["u"]))
    # Always include an "__all__" overall evidence so wallet-wide scores
    # have a single canonical row.
    out.append(build_one(
        "__all__",
        list(settled),
        list(early),
        list(unresolved),
    ))
    return tuple(out)


def build_wallet_score_input_v1(
    evidence: WalletCategoryEvidence,
    *,
    overall_trade_count: int | None = None,
) -> WalletScoreInputV1:
    """Construct the canonical typed input for the wallet-wide scorer.

    Pure; does not call the scorer. The fields map onto the same shape
    that :func:`polycopy.scoring.wallet_evidence.build_wallet_score_input_v1`
    returns for production — a fixture fed through BOTH builders MUST
    produce structurally identical inputs.
    """
    if overall_trade_count is None:
        overall_trade_count = evidence.buy_count + evidence.sell_count
    return WalletScoreInputV1(
        wallet_id=evidence.wallet_address,
        trade_count=evidence.settled_trades or evidence.qualifying_trades or None,
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
    """Construct the canonical typed input for the per-category scorer."""
    if overall_trade_count is None:
        overall_trade_count = evidence.buy_count + evidence.sell_count
    return CategoryWalletScoreInputV1(
        wallet_id=evidence.wallet_address,
        category_label=evidence.category_label,
        trade_count=evidence.settled_trades or evidence.qualifying_trades or None,
        win_rate=evidence.win_rate,
        profit_factor=evidence.profit_factor,
        sample_fraction=0.0,
        category_trade_count=evidence.qualifying_trades,
        category_distinct_markets=evidence.distinct_events,
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
