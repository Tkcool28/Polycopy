"""Engine package — orchestrates discovery, trade detection, and copyability scoring.

The CopyEngine ties together:
1. WalletDiscovery — multi-source wallet dedup
2. RelatedWalletDetector — conservative related-wallet detection
3. TradeDetector — trade dedup and staleness handling
4. score_wallet — deterministic 0-100 copyability scoring

It provides the main CLI-facing function `evaluate_wallet` that accepts
raw metrics and returns a CopyableScore with verdict.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from polycopy.scoring.engine import score_wallet

__all__ = [
    "evaluate_wallet",
    "score_wallet",
]


def evaluate_wallet(
    wallet_address: str,
    source: str = "unknown",
    sharpe_ratio: Optional[float] = None,
    win_rate: Optional[float] = None,
    trade_count: Optional[int] = None,
    latest_trade_ts: Optional[datetime] = None,
    first_trade_ts: Optional[datetime] = None,
    markets_traded: Optional[int] = None,
    manual_watchlist: bool = False,
    now: Optional[datetime] = None,
    is_sample: bool = False,
) -> tuple[UUID, str]:
    """Evaluate a wallet and return (score_id, summary_string).

    This is the main entry point for CLI and automation. It:
    1. Computes a deterministic 0-100 copyability score
    2. Returns verdict + component breakdown

    Args:
        wallet_address: the public address being evaluated.
        source: where this data came from (for labeling).
        sharpe_ratio: risk-adjusted returns metric.
        win_rate: fraction of profitable trades [0, 1].
        trade_count: total observed trades.
        latest_trade_ts: timestamp of most recent trade.
        first_trade_ts: timestamp of first observed trade.
        markets_traded: number of distinct markets traded.
        manual_watchlist: True if wallet was manually added (affects labeling).
        now: current UTC timestamp.
        is_sample: True if data is from sample/fixture sources.

    Returns:
        Tuple of (score_id, summary_string).
    """
    wallet_id = uuid4()
    result = score_wallet(
        wallet_id=wallet_id,
        sharpe_ratio=sharpe_ratio,
        win_rate=win_rate,
        trade_count=trade_count,
        latest_trade_ts=latest_trade_ts,
        first_trade_ts=first_trade_ts,
        markets_traded=markets_traded,
        now=now,
        is_sample=is_sample,
    )

    source_label = f" [{source}]" if source != "unknown" else ""
    watchlist_label = " [WATCHLIST]" if manual_watchlist else ""
    summary = (
        f"wallet={wallet_address[:12]}...{source_label}{watchlist_label}\n"
        f"  {result.summary()}\n"
        f"  components:"
    )
    for comp in result.components:
        tag = comp.quality.value[:3].upper()  # OBS/CAL/INF/UNK
        summary += f"\n    [{tag}] {comp.name}: {comp.raw_score:.1f} × {comp.weight:.0f}% = {comp.weighted_score:.1f}"
    if result.missing_fields:
        summary += "\n  missing:"
        for mf in result.missing_fields:
            summary += f"\n    [{mf.severity.upper()}] {mf.field_name} ({mf.quality_assigned.value})"
    if is_sample:
        summary += "\n  *** SAMPLE DATA ***"

    return result.id, summary
