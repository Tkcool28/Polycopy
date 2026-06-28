"""Polymarket public API adapter.

Uses httpx to call read-only Gamma and CLOB endpoints documented in the
Phase 1 data capability audit. No authentication, no order placement.
All data fetched is marked is_sample=False (it's live public data).

Endpoints used:
  Gamma (https://gamma-api.polymarket.com):
    GET /markets, GET /markets/{conditionId}, GET /events
  CLOB (https://clob.polymarket.com):
    GET /markets (paginated via next_cursor, returns tokens with bid/ask)

CLOB trades endpoint returns 401 Unauthorized — recorded in capability audit.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

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

    def __init__(
        self,
        gamma_base_url: str,
        clob_base_url: str,
        timeout: float = 10.0,
        rate_limit_rps: float = 2.0,
    ) -> None:
        self.gamma_base_url = gamma_base_url.rstrip("/")
        self.clob_base_url = clob_base_url.rstrip("/")
        self.timeout = timeout
        self.rate_limit_rps = rate_limit_rps
        self._client = None  # httpx.AsyncClient, lazily created

    async def _get_client(self):
        """Lazy-init httpx.AsyncClient."""
        if self._client is None or self._client.is_closed:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=self.gamma_base_url,
                timeout=self.timeout,
                headers={"User-Agent": "polycopy-readonly/0.3"},
            )
        return self._client

    async def _get_clob_client(self):
        """Lazy-init httpx.AsyncClient for CLOB base URL."""
        import httpx

        return httpx.AsyncClient(
            base_url=self.clob_base_url,
            timeout=self.timeout,
            headers={"User-Agent": "polycopy-readonly/0.3"},
        )

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
        all_markets = await self.list_active_markets(limit=200)
        q_lower = query.lower()
        return [m for m in all_markets if q_lower in m.question.lower()][:limit]

    async def get_markets_by_volume(self, limit: int = 20, min_volume_24h: float = 0) -> list[Market]:
        """Top markets by 24h volume."""
        client = await self._get_client()
        params = {"order": "volume24hr", "ascending": "false", "limit": limit, "active": "true", "closed": "false"}
        resp = await client.get("/markets", params=params)
        resp.raise_for_status()
        markets = [self._parse_gamma_market(m) for m in resp.json()]
        if min_volume_24h > 0:
            markets = [m for m in markets if m.volume_24h >= min_volume_24h]
        return markets

    # ── CLOB market listing with pagination ──────────────────────────────────

    async def list_clob_markets_paginated(
        self, limit: int = 100, max_pages: int = 1
    ) -> tuple[list[dict], list[dict]]:
        """Fetch CLOB markets with cursor pagination.

        Returns (market_list, fetch_errors) where each entry in market_list
        is a raw CLOB market dict, and fetch_errors captures any HTTP errors
        encountered during pagination.

        CLOB returns up to 1000 items per page with next_cursor.
        The CLOB endpoint does NOT reliably filter by closed/active —
        callers must filter client-side.
        """
        markets: list[dict] = []
        errors: list[dict] = []
        next_cursor = "MA=="
        page = 0

        async with await self._get_clob_client() as client:
            while page < max_pages:
                try:
                    resp = await client.get(
                        "/markets", params={"next_cursor": next_cursor, "limit": limit}
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                    page_data = payload.get("data", [])
                    markets.extend(page_data)

                    # Check for next page
                    new_cursor = payload.get("next_cursor")
                    if not new_cursor or new_cursor == next_cursor:
                        break
                    next_cursor = new_cursor
                    page += 1
                except Exception as exc:
                    errors.append({
                        "page": page,
                        "cursor": next_cursor,
                        "error": type(exc).__name__,
                        "message": str(exc)[:300],
                    })
                    logger.warning("CLOB pagination error page=%d: %s", page, exc)
                    break

        return markets, errors

    # ── TradeFeedProvider ────────────────────────────────────────────────────

    async def get_recent_trades(
        self, market_source_id: str, since: datetime, limit: int = 100
    ) -> list[SourceTrade]:
        """Fetch recent trades for a market.

        CLOB /trades endpoint requires authentication (HTTP 401).
        This method records the failure and returns empty list.
        Trades are NOT available via public read-only endpoints.
        """
        # CLOB trades endpoint is authenticated — record and return empty.
        logger.debug(
            "CLOB /trades requires auth (401). Market %s trades unavailable via public API.",
            market_source_id,
        )
        return []

    async def get_trades_by_address(
        self, trader_address: str, since: datetime, limit: int = 100
    ) -> list[SourceTrade]:
        """Fetch trades by address.

        Not available via public read-only CLOB endpoints.
        """
        logger.debug("CLOB trades-by-address not available via public API for %s", trader_address)
        return []

    # ── ResolutionProvider ───────────────────────────────────────────────────

    async def check_resolution(self, market_id: str) -> Optional[Market]:
        """Check resolution via Gamma API."""
        return await self.get_market(market_id)

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
            end_date=_parse_optional_dt(data.get("endDate")),
            fetched_at=datetime.now(timezone.utc),
            is_sample=False,
        )

    # ── CLOB market parser (for bid/ask from tokens) ────────────────────────

    @staticmethod
    def parse_clob_tokens(tokens: list[dict]) -> list[MarketOutcome]:
        """Parse CLOB token list into MarketOutcome objects.

        CLOB tokens have: token_id, outcome, price (optional), winner (optional).
        """
        outcomes = []
        for token in tokens:
            price = float(token.get("price", 0.5))
            # Clamp to [0, 1]
            price = max(0.0, min(1.0, price))
            outcomes.append(
                MarketOutcome(
                    label=token.get("outcome", "Unknown"),
                    price=price,
                )
            )
        return outcomes


def _parse_optional_dt(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string to datetime or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        # Handle both Z suffix and +00:00
        value_str = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(value_str)
    except (ValueError, TypeError):
        return None
