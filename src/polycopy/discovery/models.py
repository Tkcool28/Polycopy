"""Discovery domain models — wallet clusters, related-wallet candidates, and tracked trades.

These models support:
- Deduplication of wallets discovered from multiple sources
- Conservative possible-related-wallet detection (clustering heuristics)
- Tracked-wallet trade detection with duplicate signal prevention
- Late data handling (stale trades, outdated snapshots)
"""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class WalletSource(str, enum.Enum):
    """Source from which a wallet was discovered."""

    POLYMARKET = "polymarket"
    BULLPEN = "bullpen"
    MANUAL_WATCHLIST = "manual_watchlist"
    RELATED_DETECTION = "related_detection"


class RelatedWalletCandidate(BaseModel):
    """A wallet that is *possibly* related to a tracked wallet.

    Conservative detection: only flagged when multiple weak signals align
    (e.g. shared market participation + close timing). Never treated as
    confirmed ownership — only as a candidate for dedup avoidance.
    """

    id: UUID = Field(default_factory=uuid4)
    primary_wallet_id: UUID = Field(description="The known tracked wallet.")
    candidate_address: str = Field(description="Possibly-related wallet address.")
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Heuristic confidence (0 = unrelated, 1 = certain). Conservative: rarely > 0.6.",
    )
    signals: list[str] = Field(
        default_factory=list,
        description="Which signals aligned, e.g. ['shared_market', 'close_timing', 'similar_volume'].",
    )
    source: WalletSource = Field(default=WalletSource.RELATED_DETECTION)
    detected_at: datetime = Field(description="UTC detection timestamp.")
    is_sample: bool = Field(default=False)

    @property
    def is_plausibly_related(self) -> bool:
        """Only consider plausible if confidence >= 0.4 and at least 2 signals."""
        return self.confidence >= 0.4 and len(self.signals) >= 2


class TrackedTrade(BaseModel):
    """A trade from a tracked/enriched wallet, with dedup and staleness tracking."""

    id: UUID = Field(default_factory=uuid4)
    source_trade_id: str = Field(description="Original trade ID from the source.")
    source: str = Field(description="Data source name.")
    wallet_address: str = Field(description="The trader's wallet address.")
    market_source_id: str = Field(description="Source-specific market identifier.")
    side: str = Field(description="buy or sell.")
    outcome: str = Field(description="Outcome token.")
    quantity: float = Field(gt=0.0)
    price: float = Field(ge=0.0, le=1.0)
    timestamp: datetime = Field(description="Trade timestamp (UTC).")
    received_at: datetime = Field(description="When our system received this trade (UTC).")
    is_duplicate: bool = Field(
        default=False,
        description="True if this trade was filtered as a duplicate.",
    )
    is_stale: bool = Field(
        default=False,
        description="True if the trade is older than the staleness threshold.",
    )
    staleness_seconds: float = Field(
        default=0.0,
        description="How many seconds old this trade is relative to freshness threshold.",
    )
    is_sample: bool = Field(default=False)

    @property
    def latency_ms(self) -> float:
        """End-to-end latency from trade timestamp to system receipt."""
        delta = (self.received_at - self.timestamp).total_seconds()
        return max(delta, 0.0) * 1000.0


class DedupRecord(BaseModel):
    """Records a deduplication decision for auditability."""

    id: UUID = Field(default_factory=uuid4)
    incoming_trade_id: str = Field(description="The trade that arrived for dedup check.")
    existing_trade_id: str = Field(description="The trade it was matched against.")
    dedup_key: str = Field(description="The compound key used for dedup, e.g. 'source:address:market:side:outcome:timestamp_minute'.")
    is_duplicate: bool = Field(description="True if identified as duplicate.")
    reason: str = Field(description="Why it was flagged as duplicate or passed through.")
    checked_at: datetime = Field(description="UTC timestamp of dedup check.")
