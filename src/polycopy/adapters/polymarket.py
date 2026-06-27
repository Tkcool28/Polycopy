"""Polymarket public API adapter skeleton.

Uses httpx to call read-only Gamma and CLOB endpoints documented in the
Phase 1 data capability audit. No authentication, no order placement.
All data fetched is marked is_sample=False (it's live public data).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from polycopy.domain.market import Market, MarketOutcome
from polycopy.domain.source_trade import SourceTrade
from polycopy.providers.market_data import MarketDataProvider
from polycopy.providers.resolution import ResolutionProvider
from polycopy.providers.trade_feed import TradeFeedProvider

logger = logging.getLogger(__name__)


class PolymarketPublicAdapter(MarketDataProvider, TradeFeedProvider, ResolutionProvider):
    """Read-only adapter for Polymarket public Gamma + CLOB APIs.

    All methods make real HTTP calls to documented public endpoints.
    No private/authenticated endpoints are used. No orders are placed.
    """

    def __init__(self, gamma_base_url: str, clob_base_url: str, timeout: float = 10.0) -> None:
        self.gamma_base_url = gamma_base_url.rstrip("/")
        self.clob_base_url = clob_base_url.rstrip("/")
        self.timeout = timeout
        self._client = None  # httpx.AsyncClient, lazily created

    async def _get_client(self):
        """Lazy-init httpx.AsyncClient."""
        if self._client is None or self._client.is_closed:
            import httpx

            self._client = httpx.AsyncClient(base_url=self.gamma_base_url, timeout=self.timeout)
        return self._client

    # ── MarketDataProvider ──────────────────────────────────────────────────

    async def get_market(self, market_id: str) -> Optional[Market]:
        """Fetch a single market from Gamma API by condition_id."""
        client = await self._get_client()
        resp = await client.get(f"/markets/{market_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return self._parse_gamma_market(data)

    async def list_active_markets(self, limit: int = 100, offset: int = 0) -> list[Market]:
        """List active markets from Gamma API."""
        client = await self._get_client()
        params = {"active": "true", "closed": "false", "limit": limit, "offset": offset}
        resp = await client.get("/markets", params=params)
        resp.raise_for_status()
        return [self._parse_gamma_market(m) for m in resp.json()]

    async def search_markets(self, query: str, limit: int = 20) -> list[Market]:
        """Search markets — Gamma API doesn't have full text search, so we filter client-side."""
        # Gamma API doesn't support text search natively.
        # Fallback: fetch active markets and filter by question substring.
        all_markets = await self.list_active_markets(limit=200)
        q_lower = query.lower()
        return [m for m in all_markets if q_lower in m.question.lower()][:limit]

    async def get_markets_by_volume(self, limit: int = 20, min_volume_24h: float = 0) -> list[Market]:
        """Top markets by 24h volume."""
        client = await self._get_client()
        params = {"order": "volume24hr", "ascending": "false", "limit": limit}
        resp = await client.get("/markets", params=params)
        resp.raise_for_status()
        markets = [self._parse_gamma_market(m) for m in resp.json()]
        if min_volume_24h > 0:
            markets = [m for m in markets if m.volume_24h >= min_volume_24h]
        return markets

    # ── TradeFeedProvider ────────────────────────────────────────────────────

    async def get_recent_trades(
        self, market_source_id: str, since: datetime, limit: int = 100
    ) -> list[SourceTrade]:
        """Fetch recent trades from CLOB API (public endpoint).

        NOTE: The CLOB trades endpoint requires further investigation.
        This skeleton returns empty list pending CLOB API documentation review.
        """
        # TODO: Implement once CLOB trades endpoint is documented/probed.
        logger.debug("CLOB trades endpoint not yet implemented for market %s", market_source_id)
        return []

    async def get_trades_by_address(
        self, trader_address: str, since: datetime, limit: int = 100
    ) -> list[SourceTrade]:
        """Fetch trades by address from CLOB API.

        NOTE: Not yet implemented. Needs authenticated CLOB endpoint.
        """
        # TODO: Implement once CLOB API supports trade queries by address.
        logger.debug("CLOB trades-by-address not yet implemented for %s", trader_address)
        return []

    # ── ResolutionProvider ───────────────────────────────────────────────────

    async def check_resolution(self, market_id: str) -> Optional[Market]:
        """Check resolution via Gamma API."""
        client = await self._get_client()
        resp = await client.get(f"/markets/{market_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        market = self._parse_gamma_market(data)
        if market.resolved:
            return market
        return None

    async def list_resolved_since(self, since_timestamp: str, limit: int = 100) -> list[Market]:
        """List resolved markets. Gamma API supports closed=true filter."""
        client = await self._get_client()
        params = {"closed": "true", "limit": limit}
        resp = await client.get("/markets", params=params)
        resp.raise_for_status()
        markets = [self._parse_gamma_market(m) for m in resp.json()]
        return [m for m in markets if m.resolved]

    # ── Gamma market parser ─────────────────────────────────────────────────

    @staticmethod
    def _parse_gamma_market(data: dict) -> Market:
        """Parse a Gamma API market JSON object into our Market domain model.

        Gamma returns outcomes/outcomePrices as JSON-encoded strings.
        """
        import json
        from datetime import timezone

        # Parse outcomes — Gamma returns these as JSON-encoded strings
        outcomes_raw = data.get("outcomes", "[]")
        prices_raw = data.get("outcomePrices", "[]")
        if isinstance(outcomes_raw, str):
            outcomes_raw = json.loads(outcomes_raw)
        if isinstance(prices_raw, str):
            prices_raw = json.loads(prices_raw)

        outcomes = []
        for i, label in enumerate(outcomes_raw):
            price = float(prices_raw[i]) if i < len(prices_raw) else 0.5
            outcomes.append(MarketOutcome(label=str(label), price=price))

        return Market(
            source_id=data.get("conditionId", data.get("id", "")),
            question=data.get("question", ""),
            outcomes=outcomes,
            source="polymarket",
            active=data.get("active", False),
            closed=data.get("closed", False),
            resolved=data.get("resolved", False),
            resolution_outcome=data.get("resolutionOutcome"),
            volume_24h=float(data.get("volume24hr", 0) or 0),
            fetched_at=datetime.now(timezone.utc),
            is_sample=False,  # This is LIVE public data
        )
