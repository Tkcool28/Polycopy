"""Polycopy adapter implementations — concrete providers for various data sources.

Note: Discovery and scoring modules live in separate packages:
- polycopy.discovery — wallet/trade discovery, dedup, related-wallet detection
- polycopy.scoring — deterministic copyability scoring engine
- polycopy.engine — orchestration entry point
"""

from polycopy.adapters.sample import (
    SampleWalletDataProvider,
    SampleMarketDataProvider,
    SampleTradeFeedProvider,
    SampleResolutionProvider,
)
from polycopy.adapters.bullpen import BullpenReadOnlyAdapter
from polycopy.adapters.polymarket import PolymarketPublicAdapter
from polycopy.adapters.paper_broker import PaperBroker
from polycopy.adapters.disabled_live_broker import DisabledLiveBroker
from polycopy.adapters.snapshot_provenance import SnapshotProvenance

__all__ = [
    "SampleWalletDataProvider",
    "SampleMarketDataProvider",
    "SampleTradeFeedProvider",
    "SampleResolutionProvider",
    "BullpenReadOnlyAdapter",
    "PolymarketPublicAdapter",
    "PaperBroker",
    "DisabledLiveBroker",
    "SnapshotProvenance",
]
