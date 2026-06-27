"""Performance metrics domain model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class PerformanceSummary(BaseModel):
    """Aggregate performance metrics for a wallet or strategy over a period."""

    wallet_id: UUID = Field(description="Wallet this summary belongs to.")
    strategy_label: str = Field(default="default", description="Strategy label if scoped.")
    start_date: datetime = Field(description="Period start (UTC).")
    end_date: datetime = Field(description="Period end (UTC).")
    total_pnl: float = Field(description="Total profit and loss.")
    realized_pnl: float = Field(description="Realized P&L from closed positions.")
    unrealized_pnl: float = Field(description="Unrealized P&L from open positions.")
    win_rate: float = Field(ge=0.0, le=1.0, description="Fraction of winning trades.")
    sharpe_ratio: float | None = Field(default=None, description="Annualized Sharpe ratio, if computable.")
    max_drawdown: float = Field(ge=0.0, description="Maximum drawdown (non-negative).")
    trade_count: int = Field(ge=0, description="Number of trades in period.")
    is_sample: bool = Field(default=False, description="True if this is sample/fixture data, not live.")
