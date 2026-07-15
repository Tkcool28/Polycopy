"""Bounded, report-only public-read adapter for PR69 discovery.

Built on top of the canonical :class:`polycopy.adapters.polymarket.PolymarketPublicAdapter`
client lifecycle. Provides every read-only wrapper the discovery CLI needs
beyond what the production adapter already exposes (market list with date
filters, event lookup, event tags, series, wallet trades, wallet closed
positions, activity filtered to REDEEM, etc.).

Used only by the report-only CLI. NEVER persists, approves, or schedules.

Network safety contract (PR69 STEP 3):
  * GET-only, no auth, no body.
  * httpx client reuse via the underlying :class:`PolymarketPublicAdapter`.
  * Falls back to constructing short-lived clients if no adapter is passed,
    so unit tests can drive this with a mocked transport.
  * Bounded pagination, no infinite loops, deterministic rows.
  * Sanitized error messages; never log the full URL with credentials.

This module owns no budget logic of its own — the operator CLI passes a
shared :class:`RequestBudget` to every method so total request count is
globally capped.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from polycopy.adapters.polymarket import PolymarketPublicAdapter
from polycopy.discovery._safe_get import (
    ERR_BUDGET_EXHAUSTED,
    _RequestBudget,
    safe_get_json,
)

logger = logging.getLogger(__name__)


# Hard caps. Audited by STEP 11 tests. Callers requesting larger values are
# clipped silently; the report records the actual cap used.
LEADERBOARD_MAX_LIMIT = 100
MARKET_LIST_MAX_LIMIT = 500
MAX_PAGES = 50
DEFAULT_PAGE_SIZE = 100
DEFAULT_TIMEOUT = 12.0
DEFAULT_MAX_RETRIES = 2

LEADERBOARD_CATEGORIES = frozenset(
    {
        "OVERALL",
        "POLITICS",
        "SPORTS",
        "ESPORTS",
        "CRYPTO",
        "CULTURE",
        "MENTIONS",
        "WEATHER",
        "ECONOMICS",
        "TECH",
        "FINANCE",
    }
)
LEADERBOARD_PERIODS = frozenset({"DAY", "WEEK", "MONTH", "ALL"})
LEADERBOARD_ORDERS = frozenset({"PNL", "VOL"})


@dataclass(frozen=True)
class PaginatedMarkets:
    """One page of bounded Gamma market results."""

    markets: tuple[dict[str, Any], ...]
    next_offset: int
    status: str  # "complete" | "partial" | "failed"
    error_code: str | None
    pages_fetched: int


@dataclass(frozen=True)
class TradesPage:
    """One page of bounded data-api trade results."""

    trades: tuple[dict[str, Any], ...]
    next_offset: int
    status: str
    error_code: str | None
    pages_fetched: int


class DiscoveryAdapter:
    """Public-read adapter used only by the PR69 audit CLI.

    Holds a reference to (or constructs) the underlying PolymarketPublicAdapter
    so all three httpx clients (gamma, clob, data) share a single lifecycle.
    """

    def __init__(
        self,
        underlying: PolymarketPublicAdapter | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._underlying = underlying
        self._owns_underlying = underlying is None
        self._timeout = float(timeout_seconds)
        self._max_retries = int(max_retries)

    async def aclose(self) -> None:
        if self._owns_underlying and self._underlying is not None:
            await self._underlying.aclose()

    async def __aenter__(self) -> "DiscoveryAdapter":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ── Client factories (lazy + reusable) ──────────────────────────────────

    async def _ensure_underlying(self) -> PolymarketPublicAdapter:
        if self._underlying is None:
            self._underlying = PolymarketPublicAdapter(
                gamma_base_url="https://gamma-api.polymarket.com",
                clob_base_url="https://clob.polymarket.com",
                data_api_base_url="https://data-api.polymarket.com",
                timeout=self._timeout,
            )
        return self._underlying

    async def _gamma(self) -> httpx.AsyncClient:
        under = await self._ensure_underlying()
        return await under._get_gamma_client()  # noqa: SLF001 (intentional reuse)

    async def _clob(self) -> httpx.AsyncClient:
        under = await self._ensure_underlying()
        return await under._get_clob_client()  # noqa: SLF001

    async def _data(self) -> httpx.AsyncClient:
        under = await self._ensure_underlying()
        return await under._get_data_client()  # noqa: SLF001

    # ── Markets ─────────────────────────────────────────────────────────────

    async def list_active_markets(
        self,
        *,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
        tag_slug: str | None = None,
        tag_id: str | None = None,
        limit: int = MARKET_LIST_MAX_LIMIT,
        offset: int = 0,
        max_pages: int = MAX_PAGES,
        page_size: int = DEFAULT_PAGE_SIZE,
        budget: _RequestBudget | None = None,
        phase: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """List active, non-closed Gamma markets with bounded pagination.

        Returns ``(markets, errors)`` where ``errors`` records per-page
        outage without aborting. Page size + offset are deterministic;
        callers can rely on ``markets`` being ordered by the upstream
        ``order`` param.

        Note: as of 2026-07 the live Gamma endpoint does not honor
        ``end_date_min``/``end_date_max`` filters for the
        ``active=true&closed=false`` slice; callers must apply temporal
        filtering client-side.
        """
        page_size = max(1, min(int(page_size), MARKET_LIST_MAX_LIMIT))
        limit = max(1, min(int(limit), MARKET_LIST_MAX_LIMIT))
        max_pages = max(1, min(int(max_pages), MAX_PAGES))
        offset = max(0, int(offset))

        out: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        next_offset = offset
        for page_index in range(max_pages):
            if len(out) >= limit:
                break
            params: dict[str, Any] = {
                "active": "true",
                "closed": "false",
                "limit": page_size,
                "offset": next_offset,
                "order": "endDate",
                "ascending": "true",
            }
            if end_date_min:
                params["end_date_min"] = end_date_min
            if end_date_max:
                params["end_date_max"] = end_date_max
            if tag_slug:
                params["tag_slug"] = str(tag_slug)
            if tag_id:
                params["tag_id"] = str(tag_id)
            client = await self._gamma()
            result = await safe_get_json(
                client, "/markets",
                params=params,
                timeout_seconds=self._timeout,
                max_retries=self._max_retries,
                budget=budget,
                phase=phase,
                label="markets/list",
            )
            if result.error_code is not None:
                errors.append({
                    "page": page_index,
                    "offset": next_offset,
                    "error_code": result.error_code,
                    "http_status": result.status,
                })
                return out, errors
            rows = _extract_rows(result.data)
            out.extend(rows)
            if len(rows) < page_size:
                break  # upstream exhausted
            next_offset += page_size
        return out[:limit], errors

    async def get_market_raw(
        self,
        market_id: str,
        *,
        budget: _RequestBudget | None = None,
        phase: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch raw Gamma market JSON by condition ID (or numeric id)."""
        under = await self._ensure_underlying()
        if hasattr(under, "get_market_raw"):
            under_method = getattr(under, "get_market_raw")
            try:
                return await under_method(market_id, budget=budget)
            except TypeError:
                # Underlying adapter may not accept budget kwarg.
                return await under_method(market_id)
        # Fallback path for tests that don't bind the production adapter.
        client = await self._gamma()
        result = await safe_get_json(
            client, "/markets",
            params={"condition_ids": str(market_id).strip()},
            timeout_seconds=self._timeout,
            max_retries=self._max_retries,
            budget=budget,
            phase=phase,
            label="markets/by-condition",
        )
        if result.error_code is not None:
            return None
        rows = _extract_rows(result.data)
        for item in rows:
            if str(item.get("conditionId", "")) == str(market_id).strip():
                return item
        return None

    async def get_market_tags(
        self,
        market_id: str,
        *,
        budget: _RequestBudget | None = None,
        phase: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return only the ``tags`` list of a market (id/label/slug)."""
        market = await self.get_market_raw(market_id, budget=budget, phase=phase)
        if market is None:
            return []
        tags = market.get("tags") if isinstance(market.get("tags"), list) else []
        return [dict(item) for item in tags if isinstance(item, dict)]

    async def get_event_raw(
        self,
        event_id: str | int,
        *,
        budget: _RequestBudget | None = None,
        phase: str | None = None,
    ) -> dict[str, Any] | None:
        client = await self._gamma()
        result = await safe_get_json(
            client, "/events",
            params={"id": str(event_id)},
            timeout_seconds=self._timeout,
            max_retries=self._max_retries,
            budget=budget,
            phase=phase,
            label="events/by-id",
        )
        if result.error_code is not None:
            return None
        rows = _extract_rows(result.data)
        for item in rows:
            if str(item.get("id", "")) == str(event_id):
                return item
        return None

    async def get_event_tags(
        self,
        event_id: str | int,
        *,
        budget: _RequestBudget | None = None,
        phase: str | None = None,
    ) -> list[dict[str, Any]]:
        event = await self.get_event_raw(event_id, budget=budget, phase=phase)
        if event is None:
            return []
        tags = event.get("tags") if isinstance(event.get("tags"), list) else []
        return [dict(item) for item in tags if isinstance(item, dict)]

    async def get_series_raw(
        self,
        series_id: str | int,
        *,
        budget: _RequestBudget | None = None,
        phase: str | None = None,
    ) -> dict[str, Any] | None:
        """Best-effort lookup. The Gamma series endpoint shape is not part
        of the PR67 metadata contract; absence is treated as 'series n/a'
        by the taxonomy enricher, never as 'category n/a'."""
        client = await self._gamma()
        result = await safe_get_json(
            client, f"/series/{series_id}",
            timeout_seconds=self._timeout,
            max_retries=self._max_retries,
            budget=budget,
            phase=phase,
            label="series/by-id",
        )
        if result.error_code is not None:
            return None
        if isinstance(result.data, dict):
            return dict(result.data)
        return None

    # ── Wallet & leaderboard ────────────────────────────────────────────────

    async def get_public_leaderboard(
        self,
        *,
        category: str,
        time_period: str,
        order_by: str,
        limit: int = 25,
        budget: _RequestBudget | None = None,
        phase: str | None = None,
    ) -> list[dict[str, Any]]:
        """Category leaderboard (DATA-API). Validates enum arguments client-side."""
        if category.upper() not in LEADERBOARD_CATEGORIES:
            raise ValueError(f"category must be one of {sorted(LEADERBOARD_CATEGORIES)}")
        if time_period.upper() not in LEADERBOARD_PERIODS:
            raise ValueError(f"time_period must be one of {sorted(LEADERBOARD_PERIODS)}")
        if order_by.upper() not in LEADERBOARD_ORDERS:
            raise ValueError(f"order_by must be one of {sorted(LEADERBOARD_ORDERS)}")
        bounded_limit = max(1, min(int(limit), LEADERBOARD_MAX_LIMIT))
        client = await self._data()
        result = await safe_get_json(
            client, "/v1/leaderboard",
            params={
                "limit": bounded_limit,
                "category": category.upper(),
                "timePeriod": time_period.upper(),
                "orderBy": order_by.upper(),
            },
            timeout_seconds=self._timeout,
            max_retries=self._max_retries,
            budget=budget,
            phase=phase,
            label=f"leaderboard/{category}/{time_period}/{order_by}",
        )
        if result.error_code is not None:
            return []
        return _extract_rows(result.data)[:bounded_limit]

    async def wallet_trades(
        self,
        *,
        wallet_address: str,
        limit: int = 100,
        offset: int = 0,
        max_pages: int = 5,
        budget: _RequestBudget | None = None,
        phase: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Public wallet trade history.

        Always sends ``takerOnly=false`` so maker fills are included; bounded
        pagination only; returns ``(trades, errors)``.

        Live contract (PR69 STEP 4):
          * endpoint ``/trades`` with query param ``user=<wallet>``;
          * response envelope is a raw JSON list (``[{"proxyWallet": ...}]``);
          * ``proxyWallet`` is the canonical wallet identity;
          * ``timestamp`` is a Unix integer;
          * ``conditionId`` identifies the market.

        Wallet matching: compare ``proxyWallet`` case-insensitively to the
        queried wallet. Rows where ``proxyWallet`` is absent or does not match
        are rejected. ``makerAddress`` / ``takerAddress`` are NEVER substituted
        for wallet identity (they are retained only as secondary provenance
        inside the row). Malformed rows fail closed.
        """
        address = (wallet_address or "").strip().lower()
        if not address.startswith("0x") or len(address) != 42:
            raise ValueError("wallet_address must be a 0x-prefixed 40-hex-string")
        page_size = max(1, min(int(limit), 500))
        max_pages = max(1, min(int(max_pages), MAX_PAGES))
        offset = max(0, int(offset))

        out: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        next_offset = offset
        for page_index in range(max_pages):
            if len(out) >= limit:
                break
            params = {
                "user": address,
                "limit": page_size,
                "offset": next_offset,
                "takerOnly": "false",
            }
            client = await self._data()
            result = await safe_get_json(
                client, "/trades",
                params=params,
                timeout_seconds=self._timeout,
                max_retries=self._max_retries,
                budget=budget,
                phase=phase,
                label=f"trades/{address[:8]}",
            )
            if result.error_code is not None:
                errors.append({
                    "page": page_index,
                    "offset": next_offset,
                    "error_code": result.error_code,
                    "http_status": result.status,
                })
                return out, errors
            rows = _extract_rows(result.data)
            # STEP 4: normalize to the queried wallet via proxyWallet identity.
            matched: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    # Malformed row — fail closed (excluded from output).
                    continue
                proxy = row.get("proxyWallet")
                if not isinstance(proxy, str) or not proxy.strip():
                    # proxyWallet absent -> rejected; do NOT substitute maker/taker.
                    continue
                if proxy.strip().lower() != address:
                    # proxyWallet present but does not match the queried wallet.
                    continue
                matched.append(row)
            out.extend(matched)
            if len(rows) < page_size:
                break  # upstream exhausted
            next_offset += page_size
        return out[:limit], errors

    async def market_trades(
        self,
        *,
        condition_id: str,
        limit: int = 100,
        offset: int = 0,
        max_pages: int = 1,
        budget: _RequestBudget | None = None,
        phase: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Public market trade history (one page by default)."""
        cond = (condition_id or "").strip().lower()
        page_size = max(1, min(int(limit), 1000))
        max_pages = max(1, min(int(max_pages), MAX_PAGES))
        offset = max(0, int(offset))

        out: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        next_offset = offset
        for page_index in range(max_pages):
            if len(out) >= limit:
                break
            params = {
                "market": cond,
                "limit": page_size,
                "offset": next_offset,
                "takerOnly": "false",
            }
            client = await self._data()
            result = await safe_get_json(
                client, "/trades",
                params=params,
                timeout_seconds=self._timeout,
                max_retries=self._max_retries,
                budget=budget,
                phase=phase,
                label=f"market-trades/{cond[:8]}",
            )
            if result.error_code is not None:
                errors.append({
                    "page": page_index,
                    "offset": next_offset,
                    "error_code": result.error_code,
                    "http_status": result.status,
                })
                return out, errors
            rows = _extract_rows(result.data)
            out.extend(rows)
            if len(rows) < page_size:
                break
            next_offset += page_size
        return out[:limit], errors

    async def wallet_closed_positions(
        self,
        *,
        wallet_address: str,
        limit: int = 100,
        offset: int = 0,
        max_pages: int = 5,
        budget: _RequestBudget | None = None,
        phase: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        address = (wallet_address or "").strip().lower()
        if not address.startswith("0x") or len(address) != 42:
            raise ValueError("wallet_address must be a 0x-prefixed 40-hex-string")
        page_size = max(1, min(int(limit), 500))
        max_pages = max(1, min(int(max_pages), MAX_PAGES))
        offset = max(0, int(offset))

        out: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        next_offset = offset
        for page_index in range(max_pages):
            if len(out) >= limit:
                break
            client = await self._data()
            result = await safe_get_json(
                client, "/closed-positions",
                params={"user": address, "limit": page_size, "offset": next_offset},
                timeout_seconds=self._timeout,
                max_retries=self._max_retries,
                budget=budget,
                phase=phase,
                label=f"closed-positions/{address[:8]}",
            )
            if result.error_code is not None:
                errors.append({
                    "page": page_index,
                    "offset": next_offset,
                    "error_code": result.error_code,
                    "http_status": result.status,
                })
                return out, errors
            rows = _extract_rows(result.data)
            out.extend(rows)
            if len(rows) < page_size:
                break
            next_offset += page_size
        return out[:limit], errors

    async def wallet_redeem_activity(
        self,
        *,
        wallet_address: str,
        limit: int = 100,
        offset: int = 0,
        max_pages: int = 5,
        budget: _RequestBudget | None = None,
        phase: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Activity rows filtered to ``type=REDEEM`` for a wallet."""
        address = (wallet_address or "").strip().lower()
        if not address.startswith("0x") or len(address) != 42:
            raise ValueError("wallet_address must be a 0x-prefixed 40-hex-string")
        page_size = max(1, min(int(limit), 500))
        max_pages = max(1, min(int(max_pages), MAX_PAGES))
        offset = max(0, int(offset))

        out: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        next_offset = offset
        for page_index in range(max_pages):
            if len(out) >= limit:
                break
            client = await self._data()
            result = await safe_get_json(
                client, "/activity",
                params={"user": address, "type": "REDEEM", "limit": page_size, "offset": next_offset},
                timeout_seconds=self._timeout,
                max_retries=self._max_retries,
                budget=budget,
                phase=phase,
                label=f"activity-redeem/{address[:8]}",
            )
            if result.error_code is not None:
                errors.append({
                    "page": page_index,
                    "offset": next_offset,
                    "error_code": result.error_code,
                    "http_status": result.status,
                })
                return out, errors
            rows = _extract_rows(result.data)
            out.extend(rows)
            if len(rows) < page_size:
                break
            next_offset += page_size
        return out[:limit], errors


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    """Normalize the observed upstream envelope variants to a row list.

    Accepted shapes (in order):

    1. raw JSON list — ``[{...}, {...}]``
    2. ``{"data": [...]}``
    3. ``{"trades": [...]}`` / ``{"positions": [...]}`` / ``{"activity": [...]}``
    4. ``{"result": [...]}`` — observed in some scrapers

    Anything else returns ``[]`` so the caller can record
    ``unsupported_schema`` rather than fabricating a row.
    """
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "trades", "positions", "activity", "result"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [dict(item) for item in inner if isinstance(item, dict)]
    return []


def extract_wallet_address(row: Mapping[str, Any]) -> str | None:
    """Return a normalized lowercase 0x40-hex address from any leaderboard /
    trade / closed-position / activity row, or ``None`` if missing.

    Never infers identity from rank, name, or pseudonym.
    """
    for key in ("proxyWallet", "user", "wallet", "address", "trader_address", "account"):
        value = row.get(key) if isinstance(row, Mapping) else None
        if not value:
            continue
        text = str(value).strip().lower()
        if text.startswith("0x") and len(text) == 42:
            return text
    return None


def extract_wallet_match_role(
    row: Mapping[str, Any],
    queried_wallet: str,
) -> tuple[str, str | None]:
    """Identify the queried wallet's *role* in a single row.

    Returns ``(role, normalized_address)`` where ``role`` is one of:

    - ``proxy_wallet`` — row's ``proxyWallet`` equals queried.
    - ``user`` — row's ``user`` equals queried.
    - ``account`` — row's ``account`` equals queried.
    - ``address`` — row's ``address`` equals queried.
    - ``maker`` — row's ``makerAddress`` equals queried.
    - ``taker`` — row's ``takerAddress`` equals queried.
    - ``unavailable`` — no trusted identity field matches.

    The queried wallet's normalized address (lowercase 0x40-hex) is
    returned alongside the role so the caller can record provenance
    without re-normalizing.

    Ambiguity policy: if more than one identity field matches but with
    inconsistent addresses, the row is treated as ``unavailable`` and the
    caller must fail closed. The current upstream schema carries at most
    one canonical identity field per row (e.g. ``proxyWallet`` for
    ``/trades``, ``/closed-positions``, ``/activity``), so a conflict
    indicates either schema drift or a malformed payload.
    """
    if not queried_wallet:
        return WALLET_MATCH_ROLE_NONE, None
    target = queried_wallet.strip().lower()
    if not (target.startswith("0x") and len(target) == 42):
        return WALLET_MATCH_ROLE_NONE, None

    # Single-pass scan over the candidate identity fields, recording
    # the first hit (priority ordered by document[/observed] stability).
    candidate_role_keys: tuple[tuple[str, str], ...] = (
        (WALLET_MATCH_ROLE_PROXY, "proxyWallet"),
        (WALLET_MATCH_ROLE_USER, "user"),
        (WALLET_MATCH_ROLE_ACCOUNT, "account"),
        (WALLET_MATCH_ROLE_ADDRESS, "address"),
        (WALLET_MATCH_ROLE_MAKER, "makerAddress"),
        (WALLET_MATCH_ROLE_TAKER, "takerAddress"),
    )
    matched_role: str | None = None
    matched_address: str | None = None
    for role, key in candidate_role_keys:
        value = row.get(key) if isinstance(row, Mapping) else None
        if not value:
            continue
        text = str(value).strip().lower()
        if not (text.startswith("0x") and len(text) == 42):
            continue
        if text == target:
            if matched_role is not None and matched_address != text:
                # Two identity fields disagree — fail closed.
                return WALLET_MATCH_ROLE_NONE, None
            matched_role = role
            matched_address = text
    if matched_role is None:
        return WALLET_MATCH_ROLE_NONE, None
    return matched_role, matched_address


def list_broad_categories() -> tuple[str, ...]:
    """The ten official broad categories the audit CLI iterates.

    The 11th upstream enum value ``OVERALL`` is deliberately excluded — it
    is not a category, it is the cross-category aggregate.
    """
    return tuple(sorted(c for c in LEADERBOARD_CATEGORIES if c != "OVERALL"))


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_TIMEOUT",
    "DiscoveryAdapter",
    "LEADERBOARD_CATEGORIES",
    "LEADERBOARD_MAX_LIMIT",
    "LEADERBOARD_ORDERS",
    "LEADERBOARD_PERIODS",
    "MARKET_LIST_MAX_LIMIT",
    "MAX_PAGES",
    "PaginatedMarkets",
    "PHASE_CLOSED_POSITIONS",
    "PHASE_EVENT_TAGS",
    "PHASE_HISTORIES",
    "PHASE_LEADERBOARDS",
    "PHASE_MARKET_FIRST_TRADES",
    "PHASE_MARKET_TAGS",
    "PHASE_REDEEMS",
    "PHASE_REFERENCED_METADATA",
    "PHASE_SERIES",
    "PHASE_UNIVERSE_TAXONOMY",
    "PHASE_DEFAULT_PERCENTAGES",
    "WALLET_MATCH_ROLE_PROXY",
    "WALLET_MATCH_ROLE_USER",
    "WALLET_MATCH_ROLE_ADDRESS",
    "WALLET_MATCH_ROLE_MAKER",
    "WALLET_MATCH_ROLE_TAKER",
    "WALLET_MATCH_ROLE_ACCOUNT",
    "WALLET_MATCH_ROLE_NONE",
    "TradesPage",
    "extract_wallet_address",
    "extract_wallet_match_role",
    "list_broad_categories",
    # Re-exported so callers don't have to import from _safe_get directly.
    "ERR_BUDGET_EXHAUSTED",
]

# Phase identifiers used to scope the request budget.
PHASE_UNIVERSE_TAXONOMY = "universe_taxonomy"
PHASE_MARKET_FIRST_TRADES = "market_first_trades"
PHASE_LEADERBOARDS = "leaderboards"
PHASE_HISTORIES = "histories"
PHASE_CLOSED_POSITIONS = "closed_positions"
PHASE_REDEEMS = "redeems"
PHASE_REFERENCED_METADATA = "referenced_metadata"
PHASE_MARKET_TAGS = "market_tags"
PHASE_EVENT_TAGS = "event_tags"
PHASE_SERIES = "series"

# Default percentage-of-budget allocation for a meaningful audit.
# These map directly to the operator-requested percentages in STEP 7.
PHASE_DEFAULT_PERCENTAGES: dict[str, float] = {
    PHASE_UNIVERSE_TAXONOMY: 0.25,
    PHASE_MARKET_FIRST_TRADES: 0.15,
    PHASE_LEADERBOARDS: 0.15,
    PHASE_HISTORIES: 0.25,
    PHASE_CLOSED_POSITIONS: 0.08,
    PHASE_REDEEMS: 0.07,
    PHASE_REFERENCED_METADATA: 0.05,
}


# Wallet identity role match constants — STEP 3 / STEP 10.
WALLET_MATCH_ROLE_PROXY = "proxy_wallet"
WALLET_MATCH_ROLE_USER = "user"
WALLET_MATCH_ROLE_ADDRESS = "address"
WALLET_MATCH_ROLE_MAKER = "maker"
WALLET_MATCH_ROLE_TAKER = "taker"
WALLET_MATCH_ROLE_ACCOUNT = "account"
WALLET_MATCH_ROLE_NONE = "unavailable"
