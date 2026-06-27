"""Polycopy provider interfaces — abstract base classes for data providers and brokers.

All concrete adapters implement these interfaces. The paper trading platform
depends only on these ABCs, never on concrete adapters directly.
"""

from polycopy.providers.wallet_data import WalletDataProvider
from polycopy.providers.market_data import MarketDataProvider
from polycopy.providers.trade_feed import TradeFeedProvider
from polycopy.providers.resolution import ResolutionProvider
from polycopy.providers.execution_broker import ExecutionBroker

__all__ = [
    "WalletDataProvider",
    "MarketDataProvider",
    "TradeFeedProvider",
    "ResolutionProvider",
    "ExecutionBroker",
]
