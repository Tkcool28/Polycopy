"""WalletDataProvider interface — read wallet balances and identity."""

from __future__ import annotations

from abc import ABC, abstractmethod

from polycopy.domain.wallet import Wallet


class WalletDataProvider(ABC):
    """Provides wallet identity and balance data."""

    @abstractmethod
    async def get_wallet(self, wallet_address: str) -> Wallet:
        """Fetch wallet data including balances."""
        ...

    @abstractmethod
    async def list_wallets(self) -> list[Wallet]:
        """List all tracked wallets."""
        ...
