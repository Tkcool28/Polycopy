"""Polymarket public API adapter.

Uses httpx to call read-only public endpoints documented in the
Phase 1 data capability audit and verified live during the P21 trade-ingestion
fix. No authentication, no order placement.

All data fetched is marked is_sample=False (it's live public data).

Endpoints used:
  Gamma (https://gamma-api.polymarket.com):
    GET /markets, GET /markets/{conditionId}, GET /events
  CLOB (https://clob.polymarket.com):
    GET /markets (paginated via next_cursor, returns tokens with bid/ask)
    GET /book (per-token bid/ask)
  Data API (https://data-api.polymarket.com):
    GET /trades   — full unauthenticated trade history with wallet attribution
    GET /positions — wallet positions (requires user=<addr>)
    GET /holders   — top holders per market

Trade ingestion contract (verified 2026-06-28):
  - CLOB /trades requires authentication (HTTP 401 even without headers). It is
    NOT a public endpoint.
  - data-api /trades is unauthenticated and returns full wallet-attributed
    trades. The conditionId filter parameter is IGNORED by the server — it
    always returns the most-recent N global trades. Callers MUST filter
    client-side.
  - Pagination: data-api uses offset+limit (limit hard-capped at ~1000).
  - Per trade, the response includes:
      proxyWallet      (real 0x address, identifies trader)
      side             ("BUY" | "SELL")
      asset            (CLOB token ID for the traded outcome)
      conditionId      (hex 0x market identifier)
      size             (quantity)
      price            (probability [0, 1])
      timestamp        (Unix seconds)
      outcome          (human label, e.g. "Yes"/"No"/"Up")
      outcomeIndex     (0-based index into clobTokenIds)
      transactionHash  (unique per trade; perfect natural dedup key)
      title, slug      (denormalized market metadata)
      name, pseudonym  (user metadata; not address-bound)

The adapter caches a single global window per process and slices it per
conditionId; this matches the server's actual behavior (no per-market filter)
and minimizes network roundtrips.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional, cast

import httpx

from polycopy.domain.market import Market, MarketOutcome
from polycopy.domain.order import OrderSide
from polycopy.domain.source_trade import SourceTrade
from polycopy.providers.market_data import MarketDataProvider
from polycopy.providers.resolution import ResolutionProvider
from polycopy.providers.trade_feed import TradeFeedProvider

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _to_datetime(value: Any) -> datetime:
    """Coerce an int/float Unix timestamp (seconds) or ISO string into UTC datetime."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    raise ValueError(f"cannot parse datetime from {value!r}")


def _normalize_side(value: Any) -> Optional[OrderSide]:
    """Normalize various side encodings to OrderSide; None on unparseable."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("buy", "1"):
        return OrderSide.BUY
    if s in ("sell", "0"):
        return OrderSide.SELL
    return None


def _deterministic_source_trade_id(tx_hash: Any, asset: Any, ts: Any, price: Any, size: Any) -> str:
    """Build a deterministic source_trade_id from transaction hash when present,
    falling back to a sha256 of (asset, ts, price, size)."""
    if tx_hash:
        s = str(tx_hash).strip().lower()
        if s.startswith("0x") and len(s) >= 10:
            return s
    raw = f"{asset}|{ts}|{price}|{size}"
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


# ── Adapter ──────────────────────────────────────────────────────────────────


class PolymarketPublicAdapter(MarketDataProvider, TradeFeedProvider, ResolutionProvider):
    """Read-only adapter for Polymarket public Gamma + CLOB + data-api endpoints.

    All methods make real HTTP calls to documented public endpoints.
    No private/authenticated endpoints are used. No orders are placed.
    """

    def __init__(
        self,
        gamma_base_url: str,
        clob_base_url: str,
        data_api_base_url: str = "https://data-api.polymarket.com",
        timeout: float = 10.0,
        rate_limit_rps: float = 2.0,
        data_api_window_size: int = 1000,
        data_api_request_interval_seconds: float = 0.25,
    ) -> None:
        self.gamma_base_url = gamma_base_url.rstrip("/")
        self.clob_base_url = clob_base_url.rstrip("/")
        self.data_api_base_url = data_api_base_url.rstrip("/")
        self.timeout = timeout
        self.rate_limit_rps = rate_limit_rps
        self.data_api_window_size = int(data_api_window_size)
        self.data_api_request_interval_seconds = float(data_api_request_interval_seconds)
        # Lazy clients
        self._gamma_client: Optional[httpx.AsyncClient] = None
        self._clob_client: Optional[httpx.AsyncClient] = None
        self._data_client: Optional[httpx.AsyncClient] = None
        # Window cache (lifecycle-scoped): global trades from data-api.
        # This is needed because the data-api /trades endpoint IGNORES the
        # conditionId filter parameter — it always returns the most recent N
        # global trades. We cache and slice per conditionId.
        self._window_lock_time: float = 0.0
        self._window_trades: list[dict] = []
        self._window_fetched_at: Optional[datetime] = None
        self._last_data_call_at: float = 0.0

    # ── Client factories ───────────────────────────────────────────────────

    async def _get_gamma_client(self) -> httpx.AsyncClient:
        if self._gamma_client is None or self._gamma_client.is_closed:
            self._gamma_client = httpx.AsyncClient(
                base_url=self.gamma_base_url,
                timeout=self.timeout,
                headers={"User-Agent": "polycopy-readonly/0.4"},
            )
        return self._gamma_client

    async def _get_clob_client(self) -> httpx.AsyncClient:
        if self._clob_client is None or self._clob_client.is_closed:
            self._clob_client = httpx.AsyncClient(
                base_url=self.clob_base_url,
                timeout=self.timeout,
                headers={"User-Agent": "polycopy-readonly/0.4"},
            )
        return self._clob_client

    async def _get_data_client(self) -> httpx.AsyncClient:
        if self._data_client is None or self._data_client.is_closed:
            self._data_client = httpx.AsyncClient(
                base_url=self.data_api_base_url,
                timeout=self.timeout,
                headers={"User-Agent": "polycopy-readonly/0.4"},
            )
        return self._data_client

    async def aclose(self) -> None:
        for c in (self._gamma_client, self._clob_client, self._data_client):
            if c is not None and not c.is_closed:
                try:
                    await c.aclose()
                except Exception:
                    pass

    # ── Internal: global trade window ──────────────────────────────────────

    async def _throttle(self) -> None:
        """Sleep to respect per-request interval against data-api."""
        if self.data_api_request_interval_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_data_call_at
        remaining = self.data_api_request_interval_seconds - elapsed
        if remaining > 0:
            await _asyncio_sleep(remaining)
        self._last_data_call_at = time.monotonic()

    async def _fetch_global_window(self, max_age_seconds: float = 30.0) -> list[dict]:
        """Fetch a single global trades window from data-api.

        Returns the list of raw trade dicts. Cached for `max_age_seconds`
        within this adapter instance to avoid hammering the upstream.
        On any HTTP error, returns the cached window (possibly empty) and logs.
        """
        now = time.monotonic()
        if (
            self._window_trades
            and (now - self._window_lock_time) < max_age_seconds
        ):
            logger.debug(
                "data-api global window cache hit: %d trades (age=%.1fs)",
                len(self._window_trades),
                now - self._window_lock_time,
            )
            return list(self._window_trades)

        await self._throttle()
        client = await self._get_data_client()
        try:
            # The data-api ignores conditionId filter; we always pull a window.
            resp = await client.get(
                "/trades",
                params={"limit": self.data_api_window_size},
            )
            if resp.status_code == 429:
                # Rate-limited. Sleep once and retry.
                logger.warning("data-api returned 429; sleeping 2s and retrying once")
                await _asyncio_sleep(2.0)
                resp = await client.get(
                    "/trades",
                    params={"limit": self.data_api_window_size},
                )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning(
                "data-api global window fetch failed: %s: %s",
                type(exc).__name__,
                str(exc)[:300],
            )
            # Return last-good window (possibly empty); do NOT raise.
            return list(self._window_trades)

        if not isinstance(data, list):
            logger.warning("data-api /trades returned non-list: %s", type(data).__name__)
            return list(self._window_trades)

        self._window_trades = [t for t in data if isinstance(t, dict)]
        self._window_lock_time = time.monotonic()
        self._window_fetched_at = datetime.now(timezone.utc)
        logger.info(
            "data-api global window refreshed: %d trades (limit=%d)",
            len(self._window_trades),
            self.data_api_window_size,
        )
        return list(self._window_trades)

    # ── Trade parsing (shared) ─────────────────────────────────────────────

    def _parse_data_api_trade(
        self,
        raw: dict,
        market: Optional[Market] = None,
    ) -> Optional[SourceTrade]:
        """Parse a single data-api trade dict into a SourceTrade.

        Args:
            raw: dict from data-api /trades.
            market: optional Market to map asset → outcome label via clobTokenIds
                    ordering. If absent, the raw `outcome` string is used.

        Returns None if the trade is missing required fields or is malformed.
        Does NOT raise.
        """
        try:
            side = _normalize_side(raw.get("side"))
            if side is None:
                logger.debug("trade missing/unknown side, skipping: %s", raw.get("side"))
                return None

            asset = raw.get("asset")
            cond = raw.get("conditionId")
            size_raw = raw.get("size")
            price_raw = raw.get("price")
            ts_raw = raw.get("timestamp")
            if asset is None or cond is None or size_raw is None or price_raw is None or ts_raw is None:
                logger.debug("trade missing required field, skipping: %s", raw)
                return None

            try:
                size = float(size_raw)
                price = float(price_raw)
            except (TypeError, ValueError):
                logger.debug("trade size/price not numeric, skipping: %s", raw)
                return None
            if size <= 0:
                logger.debug("trade size <= 0, skipping: %s", raw)
                return None
            if not (0.0 <= price <= 1.0):
                # Some data-api trades have prices slightly outside [0,1] due to
                # rounding; clamp rather than skip.
                price = max(0.0, min(1.0, price))

            ts = _to_datetime(ts_raw)

            outcome_label = str(raw.get("outcome") or "")
            # If we have a Market, prefer the label derived from clobTokenIds
            # ordering using outcomeIndex (more reliable than the raw field
            # which is sometimes denormalized across markets).
            if market is not None:
                try:
                    pass  # NOTE: Market does not carry clobTokenIds,
                    # so the consumer of get_recent_trades must pass a tokens list via
                    # the optional kwarg in the higher-level collector. Here we just
                    # trust the raw outcome label.
                except Exception:
                    pass

            wallet = raw.get("proxyWallet") or raw.get("maker") or raw.get("trader") or ""
            wallet = str(wallet).strip()
            # On the data-api, proxyWallet is a real 0x address. If empty/null,
            # we record "unknown" — but we do NOT fabricate a fake address.
            if not wallet or wallet.lower() in ("none", "null", "0x"):
                trader_address = "unknown"
            else:
                trader_address = wallet

            tx_hash = raw.get("transactionHash")
            source_trade_id = _deterministic_source_trade_id(
                tx_hash, asset, ts_raw, price_raw, size_raw
            )

            return SourceTrade(
                source="polymarket_data_api",
                source_trade_id=source_trade_id,
                market_source_id=str(cond),
                side=side,
                outcome=outcome_label or "Unknown",
                quantity=size,
                price=price,
                trader_address=trader_address,
                timestamp=ts,
                is_sample=False,
            )
        except Exception as exc:
            logger.debug("unparseable trade skipped: %s: %s", type(exc).__name__, exc)
            return None

    # ── MarketDataProvider ──────────────────────────────────────────────────

    async def get_market(self, market_id: str) -> Optional[Market]:
        """Fetch a single market from Gamma API by condition_id."""
        client = await self._get_gamma_client()
        resp = await client.get(f"/markets/{market_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return self._parse_gamma_market(data)

    async def list_active_markets(self, limit: int = 100, offset: int = 0) -> list[Market]:
        """List active markets from Gamma API."""
        client = await self._get_gamma_client()
        params = cast("dict[str, Any]", {"active": "true", "closed": "false", "limit": int(limit), "offset": int(offset)})
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
        client = await self._get_gamma_client()
        params = cast("dict[str, Any]", {"order": "volume24hr", "ascending": "false", "limit": int(limit), "active": "true", "closed": "false"})
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
        """Fetch CLOB markets with cursor pagination."""
        markets: list[dict] = []
        errors: list[dict] = []
        next_cursor = "MA=="
        page = 0

        client = await self._get_clob_client()
        while page < max_pages:
            try:
                resp = await client.get(
                    "/markets", params=cast("dict[str, Any]", {"next_cursor": next_cursor, "limit": int(limit)})
                )
                resp.raise_for_status()
                payload = resp.json()
                page_data = payload.get("data", [])
                markets.extend(page_data)

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
        self,
        market_source_id: str,
        since: datetime,
        limit: int = 100,
        market: Optional[Market] = None,
        asset_to_outcome: Optional[dict[str, str]] = None,
    ) -> list[SourceTrade]:
        """Fetch recent trades for a market from the public data-api.

        Implementation contract (verified live 2026-06-28):
          - data-api /trades is unauthenticated; returns a global window of the
            most-recent N trades (~1000 cap). The conditionId filter is ignored
            by the server, so this method fetches a window ONCE per adapter
            instance (cached) and slices by conditionId locally.
          - `since` is honored client-side: trades with timestamp < since are
            dropped.
          - `limit` caps the per-market result.
          - `asset_to_outcome` (optional) maps a CLOB token_id → outcome label
            (e.g. {"123...": "Yes", "456...": "No"}). If provided, the trade's
            outcome label is rewritten to the matching market-specific label.
          - Never raises. On any error returns [].

        The data-api returns real 0x proxyWallet addresses per trade, so this
        method IS the wallet-discovery path. The capability flag
        `wallet_attribution_available` is therefore True whenever this method
        returns non-empty.
        """
        window = await self._fetch_global_window()
        if not window:
            logger.debug(
                "get_recent_trades: empty global window for market %s", market_source_id
            )
            return []

        cond_lower = str(market_source_id).lower()
        since_ts = since.timestamp() if isinstance(since, datetime) else 0.0

        out: list[SourceTrade] = []
        for raw in window:
            try:
                raw_cond = str(raw.get("conditionId") or "").lower()
                if raw_cond != cond_lower:
                    continue
                raw_ts = raw.get("timestamp")
                if raw_ts is None:
                    continue
                try:
                    raw_ts_f = float(raw_ts)
                except (TypeError, ValueError):
                    continue
                if raw_ts_f < since_ts:
                    continue

                parsed = self._parse_data_api_trade(raw, market=market)
                if parsed is None:
                    continue

                # Rewrite outcome label using caller-provided token map.
                if asset_to_outcome:
                    asset = str(raw.get("asset") or "")
                    if asset in asset_to_outcome:
                        parsed = parsed.model_copy(update={"outcome": asset_to_outcome[asset]})

                out.append(parsed)
                if len(out) >= limit:
                    break
            except Exception as exc:
                logger.debug("trade slice skipped: %s: %s", type(exc).__name__, exc)
                continue

        return out

    async def get_trades_by_address(
        self,
        trader_address: str,
        since: datetime,
        limit: int = 100,
    ) -> list[SourceTrade]:
        """Fetch trades by a specific trader address from data-api.

        The data-api `/trades?user=<addr>` filter is verified (2026-06-28):
        it returns the most-recent trades for that wallet across all markets.
        Pagination via offset+limit works. This is the wallet-discovery path.
        """
        if not trader_address or not str(trader_address).strip():
            return []
        await self._throttle()
        client = await self._get_data_client()
        since_ts = since.timestamp() if isinstance(since, datetime) else 0.0
        out: list[SourceTrade] = []
        try:
            resp = await client.get(
                "/trades",
                params={"user": trader_address, "limit": min(limit, self.data_api_window_size)},
            )
            if resp.status_code == 429:
                logger.warning("data-api returned 429 on /trades?user; sleeping 2s")
                await _asyncio_sleep(2.0)
                resp = await client.get(
                    "/trades",
                    params={"user": trader_address, "limit": min(limit, self.data_api_window_size)},
                )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning(
                "get_trades_by_address failed for %s: %s: %s",
                trader_address[:12], type(exc).__name__, str(exc)[:300],
            )
            return []

        if not isinstance(data, list):
            return []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            raw_ts = raw.get("timestamp")
            if raw_ts is None:
                continue
            try:
                if float(raw_ts) < since_ts:
                    continue
            except (TypeError, ValueError):
                continue
            parsed = self._parse_data_api_trade(raw)
            if parsed is not None:
                out.append(parsed)
            if len(out) >= limit:
                break
        return out

    # ── Capability probe (used by collector + run_scan) ─────────────────────

    async def probe_trade_capability(self) -> dict:
        """Probe whether public trade data is available with wallet attribution.

        Returns a dict:
          {
            "status": "ok" | "unavailable" | "partial",
            "wallet_attribution_available": bool,
            "trades_returned": int,
            "http_status": int,
            "error": Optional[str],
          }
        """
        result = {
            "status": "unavailable",
            "wallet_attribution_available": False,
            "trades_returned": 0,
            "http_status": 0,
            "error": None,
        }
        await self._throttle()
        client = await self._get_data_client()
        try:
            resp = await client.get("/trades", params={"limit": 5})
            result["http_status"] = resp.status_code
            if resp.status_code != 200:
                result["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
                return result
            data = resp.json()
            if not isinstance(data, list) or not data:
                result["status"] = "partial"
                result["error"] = "empty response"
                return result
            result["trades_returned"] = len(data)
            # Check wallet attribution
            wallets = []
            for t in data:
                if isinstance(t, dict):
                    w = t.get("proxyWallet")
                    if isinstance(w, str) and w.startswith("0x") and len(w) == 42:
                        wallets.append(w)
            if wallets:
                result["status"] = "ok"
                result["wallet_attribution_available"] = True
            else:
                result["status"] = "partial"
                result["error"] = "no proxyWallet field with real 0x address"
            return result
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            return result

    # ── ResolutionProvider ───────────────────────────────────────────────────

    async def check_resolution(self, market_id: str) -> Optional[Market]:
        """Return a market only when Gamma confirms a valid, final resolution."""
        client = await self._get_gamma_client()
        resp = await client.get(f"/markets/{market_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if data.get("disputed") or data.get("dispute"):
            return None
        market = self._parse_gamma_market(data)
        if not market.resolved or not market.closed:
            return None
        if not market.resolution_outcome:
            return None
        valid_outcomes = {outcome.label for outcome in market.outcomes}
        if market.resolution_outcome not in valid_outcomes:
            return None
        return market

    async def list_resolved_since(self, since_timestamp: str, limit: int = 100) -> list[Market]:
        """List resolved markets. Gamma API supports closed=true filter."""
        client = await self._get_gamma_client()
        params = cast("dict[str, Any]", {"closed": "true", "limit": int(limit)})
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

    @staticmethod
    def parse_clob_tokens(tokens: list[dict]) -> list[MarketOutcome]:
        """Parse CLOB token list into MarketOutcome objects."""
        outcomes = []
        for token in tokens:
            price = float(token.get("price", 0.5))
            price = max(0.0, min(1.0, price))
            outcomes.append(
                MarketOutcome(
                    label=token.get("outcome", "Unknown"),
                    price=price,
                )
            )
        return outcomes


# ── Module-level helpers (avoids pulling asyncio into the top of file) ─────────


async def _asyncio_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


def _parse_optional_dt(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string to datetime or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        value_str = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(value_str)
    except (ValueError, TypeError):
        return None
