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
  - data-api /trades is unauthenticated and returns wallet-attributed trades.
    Use the ``market=<conditionId>`` parameter for per-market fetches; callers
    still filter client-side defensively because upstream responses can contain
    malformed or stray rows.
  - **Round-10 stabilization:** every per-market request explicitly passes
    ``takerOnly=false`` so that maker-side fills (where the watched wallet
    was the liquidity provider) are NOT silently dropped. Polymarket's
    data-api defaults to ``takerOnly=true``, which would exclude any smart
    wallet acting as a maker. The default is wrong for our use case
    (smart-money discovery) and must be overridden on EVERY market-specific
    request, including every pagination page.
  - Pagination: data-api uses offset+limit (limit hard-capped at ~1000).
  - Per trade, the response includes:
      proxyWallet      (real 0x address, identifies trader) — MAY be missing
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

P2 fix (2026-06-28): when ``proxyWallet`` (or ``maker``/``trader``) is missing,
the adapter persists the trade with ``trader_address=None``. Anonymous trades
remain in ``source_trades`` as market-level observations, but they are EXCLUDED
from wallet discovery and ``evaluate_wallet`` scoring. The downstream
collector (``scripts/collect_smart_money_data.py``) tracks
``anonymous_trades_skipped`` separately from ``wallets_discovered``.

P1 fix (2026-06-28): ``source_trade_id`` is computed by
:func:`deterministic_source_trade_id_v2`, which hashes a canonical payload of
every distinguishing row field (``transactionHash``, ``asset``,
``conditionId``, ``side``, ``outcome``, ``outcomeIndex``, ``price``, ``size``,
``timestamp``, ``proxyWallet``). Two rows that share an on-chain
``transactionHash`` but differ in any other field get DIFFERENT IDs, so they
do not collide on the ``UNIQUE(source, source_trade_id)`` constraint and
cannot overwrite each other in ``source_trades``.

The adapter fetches live trades per market with bounded offset pagination.
Older global-window cache behavior is intentionally bypassed for the live
``fetch_trades_for_market`` path so separate markets cannot share stale rows.

**Round-10 fetch-result contract**: :meth:`fetch_trades_for_market` returns
a :class:`MarketTradeFetchResult` instead of a bare ``list[SourceTrade]``
so callers can distinguish a complete fetch from a partial one. A partial
fetch (some pages succeeded, a later page failed) MUST NOT be scored as
complete — the caller must surface it explicitly. See
:class:`MarketTradeFetchResult` for status semantics.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Literal, Optional, cast

import httpx

from polycopy.domain.market import Market, MarketOutcome
from polycopy.domain.order import OrderSide
from polycopy.domain.source_trade import SourceTrade, is_sentinel_trader_address
from polycopy.providers.market_data import MarketDataProvider
from polycopy.providers.resolution import ResolutionProvider
from polycopy.providers.trade_feed import TradeFeedProvider

logger = logging.getLogger(__name__)


# ── Fetch-result contract ─────────────────────────────────────────────────────
#
# A multi-page market fetch can end in one of three states:
#
#   "complete" — every requested page was fetched successfully and the
#                termination condition (short page, empty page, max_pages,
#                max_rows) was legitimate. The trades list is safe for
#                downstream persistence, scoring, and audit.
#
#   "partial"   — at least one page succeeded but a later page failed
#                (timeout, HTTP 429, HTTP 5xx, invalid JSON, malformed
#                response, network exception). The trades list is a prefix
#                and MUST NOT be treated as a complete market history.
#                Callers either persist with an explicit partial marker
#                or discard it (PR #3 discards to keep the historical
#                source_trades table deterministic).
#
#   "failed"    — the first page failed. No trustworthy page set exists.
#                trades list is empty. Nothing should be persisted or
#                scored from this attempt.
#
# ``MarketTradeFetchResult`` is a dataclass that ALSO acts as a list-
# compatible iterable so existing call sites that did ``for t in result``
# and ``len(result)`` continue to work unchanged. New callers should
# branch on ``result.status`` before persisting/scoring.
MarketFetchStatus = Literal["complete", "partial", "failed"]


@dataclass(frozen=True)
class MarketTradeFetchResult:
    """Result of a multi-page market trade fetch.

    Iterable + sized so legacy code that did ``len(result)`` and
    ``for t in result`` continues to work; new code MUST branch on
    ``status`` before treating ``trades`` as complete history.
    """

    trades: list[SourceTrade] = field(default_factory=list)
    status: MarketFetchStatus = "complete"
    pages_fetched: int = 0
    rows_fetched: int = 0
    error: Optional[str] = None
    market_source_id: str = ""

    def __post_init__(self) -> None:
        # Frozen dataclass: must use object.__setattr__ to mutate.
        if self.status == "complete" and self.error is not None:
            raise ValueError("status=complete cannot have an error message")
        if self.status == "failed" and self.trades:
            raise ValueError(
                "status=failed cannot carry trades (no trustworthy page set)"
            )
        if self.status == "partial" and not self.error:
            raise ValueError(
                "status=partial must carry an error explaining the truncation"
            )

    # Iterable interface so legacy ``for t in result`` / ``len(result)``
    # / ``result[0]`` keep working. New code SHOULD branch on ``status``
    # before iterating.
    def __iter__(self) -> Iterator[SourceTrade]:
        return iter(self.trades)

    def __len__(self) -> int:
        return len(self.trades)

    def __getitem__(self, idx: int) -> SourceTrade:
        return self.trades[idx]

    def __bool__(self) -> bool:
        return bool(self.trades)


def _empty_complete(market_source_id: str) -> MarketTradeFetchResult:
    """An empty page (no rows) on the first request is a complete result
    with zero rows, not a failure. A clean "no trades for this market"
    must look like success so the caller does not falsely treat the
    market as failed."""
    return MarketTradeFetchResult(
        trades=[],
        status="complete",
        pages_fetched=0,
        rows_fetched=0,
        market_source_id=market_source_id,
    )


def build_market_trade_params(
    market_source_id: str,
    *,
    limit: int,
    offset: int,
) -> dict[str, str]:
    """Build the canonical request params for one ``GET /trades`` call.

    Round-11 (Codex P2 PRRT_kwDOTG4Cf86M7BQV): every code path that asks
    the data-api for a market's trade history must use the SAME params.
    Previously the snapshot path omitted ``takerOnly=false`` and recorded
    a taker-only payload as provenance while downstream persistence and
    scoring received a maker-inclusive payload.

    Required params:

      * ``market``  — the conditionId (hex string, original case preserved
        on the wire; the adapter filters rows client-side using lowercase
        so cross-market rows can never contaminate the result).
      * ``limit``   — max rows the upstream returns for this request.
      * ``offset``  — pagination cursor; must be ``0`` for the first page.
      * ``takerOnly=false`` — EXPLICITLY include both maker and taker
        fills. Polymarket's data-api defaults to ``takerOnly=true``
        and would silently exclude any smart wallet acting as a
        liquidity provider. We never rely on that default.

    This helper is the SINGLE source of truth for the per-market
    ``/trades`` request shape. Both ``fetch_trades_for_market`` (the
    persisted/scored path) and ``_snapshot_market_first_page`` (the
    provenance path) use it. A future default change by the upstream
    cannot drift them out of alignment.
    """
    # NOTE: ``takerOnly`` is sent as the string "false" because the
    # data-api's URL parser accepts both ``"false"`` and ``"true"``
    # only on the wire; an undeclared or boolean ``False`` token may be
    # rejected or normalized by intermediate proxies.
    return {
        "market": str(market_source_id),
        "limit": str(int(limit)),
        "offset": str(int(offset)),
        "takerOnly": "false",
    }


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


# Match a "real" on-chain transaction hash: 0x + 8+ hex chars.
# Short/non-hex values (e.g. "0xshort", "0x", "garbage") are treated as missing.
_TX_HASH_RE = re.compile(r"^0x[0-9a-f]{8,}$")


def deterministic_source_trade_id_v2(raw: dict) -> str:
    """Build a canonical, row-level ``source_trade_id`` for a data-api trade.

    The ID is computed from a sha256 over a canonical separator-joined payload
    that includes EVERY distinguishing row field. Two rows from the same
    on-chain transaction but with different assets/outcomes/sides/prices/sizes
    produce DIFFERENT IDs.

    Properties:
      - Deterministic: identical input dict → identical ID.
      - Idempotent: refetching the same data produces the same ID.
      - Row-distinguishing: rows from the same transactionHash but with
        different (asset, outcome, side, price, size, ts, wallet) get different
        IDs.
      - Input-order independent: every field is normalized to a canonical
        string before joining.
      - Missing fields become "" in the payload (no sentinel that could
        collide with a real value).
      - Short / non-hex transactionHash values are treated as missing and
        do NOT contribute to the canonical payload.
      - Versioned: payload starts with "v2|" so any legacy tx-hash-only IDs
        in production never collide with v2 IDs.

    Args:
        raw: dict from data-api /trades (any subset of canonical fields is OK;
             missing fields become "").

    Returns:
        str of the form ``"polymarket:<64-char-sha256-hex>"``.
    """
    # 1. transactionHash — lowercased, stripped, validated as a "real" 0x hash.
    tx = str(raw.get("transactionHash") or "").strip().lower()
    if not _TX_HASH_RE.match(tx):
        tx = ""  # treat short / non-hex / missing as missing

    asset = str(raw.get("asset") or "")

    cond = str(raw.get("conditionId") or "").strip().lower()

    side = str(raw.get("side") or "").strip().upper()

    outcome = str(raw.get("outcome") or "")

    outcome_index_raw = raw.get("outcomeIndex")
    outcome_index = "" if outcome_index_raw is None else str(outcome_index_raw)

    # Numeric fields formatted with fixed precision to avoid float-repr drift.
    price_raw: Any = raw.get("price")
    try:
        price_str = f"{float(price_raw):.10f}"
    except (TypeError, ValueError):
        price_str = ""

    size_raw: Any = raw.get("size")
    try:
        size_str = f"{float(size_raw):.10f}"
    except (TypeError, ValueError):
        size_str = ""

    ts_raw: Any = raw.get("timestamp")
    try:
        ts_int = int(float(ts_raw))
        ts_str = str(ts_int)
    except (TypeError, ValueError):
        ts_str = ""

    wallet = str(
        raw.get("proxyWallet") or raw.get("maker") or raw.get("trader") or ""
    ).strip().lower()
    if wallet.startswith("0x"):
        wallet = wallet[2:]

    payload = "|".join([
        "v2",
        tx,                # may be "" if missing / invalid
        asset, cond, side, outcome, outcome_index,
        price_str, size_str, ts_str, wallet,
    ])
    return "polymarket:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _deterministic_source_trade_id(tx_hash: Any, asset: Any, ts: Any, price: Any, size: Any) -> str:
    """DEPRECATED: kept as a back-compat shim for existing imports / tests.

    The old behavior was unsafe: it returned the lowercased tx_hash alone when
    present, which made all rows sharing a transaction collapse onto a single
    source_trade_id and overwrite each other in source_trades.

    The new row-distinguishing algorithm is :func:`deterministic_source_trade_id_v2`.
    Callers should migrate to passing the full raw dict to v2 directly. This
    shim logs a DeprecationWarning on first call per process and delegates to
    v2 by reconstructing a minimal raw dict from the legacy positional args.
    """
    warnings.warn(
        "_deterministic_source_trade_id is deprecated; use "
        "deterministic_source_trade_id_v2(raw_dict) for row-distinguishing IDs.",
        DeprecationWarning,
        stacklevel=2,
    )
    legacy_raw = {
        "transactionHash": tx_hash,
        "asset": asset,
        "timestamp": ts,
        "price": price,
        "size": size,
    }
    return deterministic_source_trade_id_v2(legacy_raw)


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

    async def _fetch_global_window(
        self, max_age_seconds: float = 30.0
    ) -> tuple[list[dict], bool]:
        """Fetch a single global trades window from data-api.

        Returns ``(window, fresh_fetch)`` where:
          - ``window`` is the list of raw trade dicts (cached between calls).
          - ``fresh_fetch`` is True iff a NEW HTTP fetch happened on this call.
            False indicates a cache hit (a recent fetch is still within
            ``max_age_seconds`` of validity). Callers MUST use ``fresh_fetch``
            to avoid double-counting work that was already done (e.g. snapshot
            provenance must be written exactly once per real upstream fetch,
            not once per market that consumed the cached window).

        Cached for ``max_age_seconds`` within this adapter instance to avoid
        hammering the upstream. On any HTTP error, returns the cached window
        (possibly empty) with ``fresh_fetch=False`` so callers can distinguish
        a real fetch from a degraded fallback.
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
            return list(self._window_trades), False

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
            # ``fresh_fetch=False`` so callers don't snapshot a degraded fallback.
            return list(self._window_trades), False

        if not isinstance(data, list):
            logger.warning("data-api /trades returned non-list: %s", type(data).__name__)
            return list(self._window_trades), False

        self._window_trades = [t for t in data if isinstance(t, dict)]
        self._window_lock_time = time.monotonic()
        self._window_fetched_at = datetime.now(timezone.utc)
        logger.info(
            "data-api global window refreshed: %d trades (limit=%d)",
            len(self._window_trades),
            self.data_api_window_size,
        )
        return list(self._window_trades), True

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
            # P2 fix (round 7): normalize ALL sentinel/empty/whitespace-only
            # wallet attribution to ``None`` via the shared
            # ``is_sentinel_trader_address`` helper — the SAME source of
            # truth used by the v5 migration, ``repository.py``, and
            # ``scripts/run_scan.py``. This covers ``unknown`` /
            # ``anonymous`` / ``missing`` / ``0x`` / ``0x0`` plus empty,
            # whitespace-only, case, and padded variants. Real 0x
            # addresses pass through unchanged. Anonymous trades persist
            # with ``trader_address=None`` and MUST NOT become wallet
            # rows downstream.
            #
            # Round-8 fix: normalize the canonical wallet identity to
            # lowercase at the parser boundary so that downstream
            # discovery (``WalletDiscovery._register`` already lowercases
            # keys) and metric queries (now ``LOWER(TRIM(...))``) all see
            # the same canonical value. Checksum-style 0x addresses from
            # the data-api are EIP-55 mixed-case; lowercasing them here
            # keeps ``source_trade_id`` (which already lowercases the
            # wallet before hashing) stable across case variants.
            trader_address: Optional[str] = None
            if wallet and not is_sentinel_trader_address(wallet):
                trader_address = wallet.lower()

            # P1 fix: build a row-distinguishing source_trade_id from the WHOLE
            # raw dict. Previously we passed only (tx_hash, asset, ts, price,
            # size), which collapsed all rows from the same transaction to a
            # single id and let later rows overwrite earlier ones in
            # source_trades (UNIQUE(source, source_trade_id) + INSERT OR REPLACE).
            source_trade_id = deterministic_source_trade_id_v2(raw)

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
                # PR-1: persist the upstream CLOB token id verbatim so the
                # canonical mapping helper can join ``source_trades.token_id``
                # to ``market_outcomes.clob_token_id`` without re-parsing the
                # raw payload. Empty/None becomes None (legacy fallback path
                # in resolve_trade_to_outcome).
                token_id=str(asset) if asset not in (None, "") else None,
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

        .. note::
           Round 7 change: per-market collection now uses
           :meth:`fetch_trades_for_market` (server-side ``?market=<id>``
           filter with bounded pagination) instead of slicing a cached
           global window. This method is retained as a fallback / global
           snapshot path and is NOT used by the per-market collector or
           run_scan paths anymore.
        """
        window, _fresh = await self._fetch_global_window()
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

    # ── Per-market fetcher (round 7) ─────────────────────────────────────────

    async def fetch_trades_for_market(
            self,
            market_source_id: str,
            *,
            since: Optional[datetime] = None,
            limit: int = 200,
            max_pages: int = 5,
            max_rows: int = 2000,
            market: Optional[Market] = None,
            asset_to_outcome: Optional[dict[str, str]] = None,
        ) -> MarketTradeFetchResult:
            """Fetch trades for a single market via the data-api server-side filter.

            Uses ``GET /trades?market=<conditionId>&takerOnly=false`` (verified
            live 2026-06-28: the server DOES honor ``market=`` and returns ONLY
            trades for that conditionId, with empty ``[]`` for unknown / inactive
            markets; ``takerOnly=false`` is REQUIRED to include maker-side
            fills, otherwise the data-api defaults to taker-only and silently
            drops any smart wallet acting as a liquidity provider).

            Pagination is via ``offset`` + ``limit``. Every page sends
            ``takerOnly=false`` so the contract holds across all pages.

            Args:
                market_source_id: hex ``conditionId`` for the market.
                since: optional lower bound; trades with ``timestamp < since`` are
                    dropped client-side after the fetch. Default is None.
                limit: per-page size (the API caps each request at this many rows).
                max_pages: hard upper bound on pagination depth.
                max_rows: hard upper bound on total rows retained.
                market: optional :class:`Market` for outcome mapping.
                asset_to_outcome: optional CLOB token_id → outcome label map.

            Returns:
                A :class:`MarketTradeFetchResult` with explicit status:

                  * ``"complete"`` — every requested page fetched successfully;
                    termination was legitimate (short/empty page, max_pages, or
                    max_rows). Caller may persist + score.
                  * ``"partial"`` — at least one page succeeded but a later page
                    failed (timeout, HTTP 429, 5xx, invalid JSON, malformed
                    response, network exception). The trades list is a prefix
                    and MUST NOT be treated as complete. Caller decides whether
                    to discard or persist-with-marker.
                  * ``"failed"`` — the first page failed. trades is empty.
                    Nothing should be persisted or scored.

                Never raises. On first-page exception returns ``"failed"``;
                on later-page exception returns ``"partial"`` with the prefix.
            """
            if not market_source_id or not str(market_source_id).strip():
                return MarketTradeFetchResult(
                    status="failed",
                    market_source_id=market_source_id or "",
                    error="empty market_source_id",
                )
            cond_lower = str(market_source_id).lower()
            since_ts = since.timestamp() if isinstance(since, datetime) else 0.0

            client = await self._get_data_client()
            seen: set[str] = set()
            out: list[SourceTrade] = []
            pages_fetched = 0
            rows_fetched = 0
            last_error: Optional[str] = None

            offset = 0
            for page in range(max_pages):
                try:
                    await self._throttle()
                    resp = await client.get(
                        "/trades",
                        params=build_market_trade_params(
                            market_source_id,
                            limit=limit,
                            offset=offset,
                        ),
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    last_error = (
                        f"{type(exc).__name__}: {str(exc)[:300]}"
                    )
                    logger.warning(
                        "fetch_trades_for_market: page=%d offset=%d failed: %s",
                        page, offset, last_error,
                    )
                    # Round-10 fix (Codex P2): NEVER silently return a prefix as
                    # if the fetch had completed. The caller MUST distinguish:
                    #   - first page (page == 0) → "failed" (no trustworthy data)
                    #   - later page (page >= 1) → "partial" (prefix exists)
                    if page == 0:
                        return MarketTradeFetchResult(
                            trades=[],
                            status="failed",
                            pages_fetched=0,
                            rows_fetched=0,
                            error=last_error,
                            market_source_id=cond_lower,
                        )
                    return MarketTradeFetchResult(
                        trades=out,
                        status="partial",
                        pages_fetched=pages_fetched,
                        rows_fetched=rows_fetched,
                        error=last_error,
                        market_source_id=cond_lower,
                    )

                if not isinstance(data, list):
                    last_error = f"non-list response: {type(data).__name__}"
                    logger.warning(
                        "fetch_trades_for_market: non-list response on page=%d: %s",
                        page, type(data).__name__,
                    )
                    if page == 0:
                        return MarketTradeFetchResult(
                            trades=[],
                            status="failed",
                            pages_fetched=0,
                            rows_fetched=0,
                            error=last_error,
                            market_source_id=cond_lower,
                        )
                    return MarketTradeFetchResult(
                        trades=out,
                        status="partial",
                        pages_fetched=pages_fetched,
                        rows_fetched=rows_fetched,
                        error=last_error,
                        market_source_id=cond_lower,
                    )

                # Empty or short page → graceful complete termination.
                pages_fetched += 1
                if not data or len(data) < limit:
                    for raw in data:
                        absorbed = self._absorb_trade(
                            raw, cond_lower, since_ts, seen, out,
                            market=market, asset_to_outcome=asset_to_outcome,
                        )
                        if absorbed:
                            rows_fetched += 1
                        if len(out) >= max_rows:
                            return MarketTradeFetchResult(
                                trades=out,
                                status="complete",
                                pages_fetched=pages_fetched,
                                rows_fetched=rows_fetched,
                                market_source_id=cond_lower,
                            )
                    # Legitimate end-of-stream: short page AND max_rows not hit.
                    return MarketTradeFetchResult(
                        trades=out,
                        status="complete",
                        pages_fetched=pages_fetched,
                        rows_fetched=rows_fetched,
                        market_source_id=cond_lower,
                    )

                # Full page → parse and advance.
                for raw in data:
                    absorbed = self._absorb_trade(
                        raw, cond_lower, since_ts, seen, out,
                        market=market, asset_to_outcome=asset_to_outcome,
                    )
                    if absorbed:
                        rows_fetched += 1
                    if len(out) >= max_rows:
                        return MarketTradeFetchResult(
                            trades=out,
                            status="complete",
                            pages_fetched=pages_fetched,
                            rows_fetched=rows_fetched,
                            market_source_id=cond_lower,
                        )
                offset += limit

            # max_pages reached with every page returning a full page → complete.
            return MarketTradeFetchResult(
                trades=out,
                status="complete",
                pages_fetched=pages_fetched,
                rows_fetched=rows_fetched,
                market_source_id=cond_lower,
            )

    def _absorb_trade(
        self,
        raw: Any,
        cond_lower: str,
        since_ts: float,
        seen: set[str],
        out: list[SourceTrade],
        *,
        market: Optional[Market],
        asset_to_outcome: Optional[dict[str, str]],
    ) -> bool:
        """Parse one raw trade row, dedup, and append to ``out``.

        Returns True if the row was appended to ``out``, False otherwise
        (skipped for malformed/wrong-market/duplicate). Used only by
        :meth:`fetch_trades_for_market` for accurate ``rows_fetched``
        accounting. Filters server-side errors (row with the wrong
        ``conditionId`` from a future API change) and the optional
        ``since`` lower bound.
        """
        try:
            raw_cond = str(raw.get("conditionId") or "").lower()
            if raw_cond != cond_lower:
                return False
            raw_ts = raw.get("timestamp")
            if raw_ts is None:
                return False
            try:
                raw_ts_f = float(raw_ts)
            except (TypeError, ValueError):
                return False
            if since_ts and raw_ts_f < since_ts:
                return False

            sid = deterministic_source_trade_id_v2(raw)
            if sid in seen:
                return False
            seen.add(sid)

            parsed = self._parse_data_api_trade(raw, market=market)
            if parsed is None:
                return False

            if asset_to_outcome:
                asset = str(raw.get("asset") or "")
                if asset in asset_to_outcome:
                    parsed = parsed.model_copy(update={"outcome": asset_to_outcome[asset]})

            out.append(parsed)
            return True
        except Exception as exc:  # never raise from a single bad row
            logger.debug("fetch_trades_for_market row skipped: %s: %s", type(exc).__name__, exc)
            return False

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

        Gamma returns outcomes/outcomePrices/clobTokenIds as JSON-encoded strings.
        PR-1: the positional ``clobTokenIds`` array is parsed via the SHARED
        :func:`parse_clob_token_ids` + :func:`zip_outcomes_with_tokens` helpers
        so the same identity normalization applies here as in every other
        Gamma parser in the codebase (``scripts/run_scan._parse_gamma_market``,
        ``scripts.collect_smart_money_data.PolymarketCollector._parse_market``,
        ``scripts/update_paper_portfolio``). Length mismatches or missing
        arrays produce ``clob_token_id=None`` for every outcome (INCOMPLETE),
        never a silent positional mapping.
        """
        outcomes_raw = data.get("outcomes", "[]")
        prices_raw = data.get("outcomePrices", "[]")
        if isinstance(outcomes_raw, str):
            outcomes_raw = json.loads(outcomes_raw)
        if isinstance(prices_raw, str):
            prices_raw = json.loads(prices_raw)

        # PR-1: parse clobTokenIds + zip with outcomes via the shared helpers.
        # Same source of truth used by every other parser.
        tokens = parse_clob_token_ids(data)
        zipped = zip_outcomes_with_tokens(
            outcomes_raw, tokens, source_label="polymarket.adapter"
        )
        # Map outcome index -> token id (None when the helper returned INCOMPLETE).
        token_by_index: dict[int, Optional[str]] = {
            idx: tok for idx, _, tok in zipped
        }
        outcomes: list[MarketOutcome] = []
        for i, label in enumerate(outcomes_raw):
            price = float(prices_raw[i]) if i < len(prices_raw) else 0.5
            outcomes.append(
                MarketOutcome(
                    label=str(label),
                    price=price,
                    clob_token_id=token_by_index.get(i),
                )
            )

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


# ── Shared CLOB-token parsing (PR-1) ─────────────────────────────────────────
# Gamma emits ``clobTokenIds`` as a JSON-encoded array (or a bare list) whose
# entries are positionally aligned with the ``outcomes`` and ``outcomePrices``
# arrays in the same payload. Persisting this identity into
# ``market_outcomes.clob_token_id`` is the foundation that PR-2's
# ``copy_candidates`` (signal candidates) and the canonical
# ``resolve_trade_to_outcome`` helper depend on. The function below is the
# single source of truth used by every Gamma parser in the codebase
# (``PolymarketPublicAdapter._parse_gamma_market``,
# ``scripts.run_scan._parse_gamma_market``,
# ``scripts.collect_smart_money_data.PolymarketCollector._parse_market``)
# so they all normalize the same way.
#
# Behavior (PR-1 spec):
#  * Missing ``data`` / missing ``clobTokenIds`` key  → empty list (no warning)
#  * Present but malformed JSON                       → empty list, warn
#  * List-type mismatches (dict, scalar, ...)         → empty list, warn
#  * Empty list                                       → empty list, no warn
#  * Any other shape                                   → empty list, warn
#
# Each entry is normalized to a Python ``str`` (``str(t)``). Numeric tokens
# are kept as their string form so SQLite ``TEXT`` comparisons match
# exactly what the data-api emits in its ``asset`` field — any deviation
# would defeat the join.
_MODULE_LOGGER = logging.getLogger(__name__)


def parse_clob_token_ids(data: dict) -> list[str]:
    """Return Gamma's ``clobTokenIds`` as a list of strings.

    PR-1 contract: returns ``[]`` whenever the field is absent, malformed,
    or shaped unlike a list. The caller is expected to treat ``len(tokens)
    != len(outcomes)`` as INCOMPLETE — see :func:`zip_outcomes_with_tokens`.
    """
    raw = data.get("clobTokenIds")
    if raw is None:
        return []
    parsed: Any = raw
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (ValueError, TypeError) as exc:
            _MODULE_LOGGER.warning(
                "parse_clob_token_ids: malformed clobTokenIds JSON (%s); treating as empty",
                type(exc).__name__,
            )
            return []
    if not isinstance(parsed, list):
        _MODULE_LOGGER.warning(
            "parse_clob_token_ids: unexpected clobTokenIds shape %s; treating as empty",
            type(parsed).__name__,
        )
        return []
    out: list[str] = []
    for i, entry in enumerate(parsed):
        if entry is None:
            out.append("")
            continue
        try:
            out.append(str(entry))
        except Exception as exc:
            _MODULE_LOGGER.warning(
                "parse_clob_token_ids: unstringifiable token at index %d (%s); treating as empty string",
                i, type(exc).__name__,
            )
            out.append("")
    return out


def zip_outcomes_with_tokens(
    outcomes_raw: list, tokens: list[str], *, source_label: str
) -> list[tuple[int, str, Optional[str]]]:
    """Zip outcomes with their CLOB tokens positionally.

    Returns a list of ``(index, label, clob_token_id_or_None)`` tuples, one
    per outcome, in the input order. The token for outcome ``i`` is
    ``tokens[i]`` ONLY when the array lengths match; otherwise EVERY outcome
    gets ``token=None`` (INCOMPLETE) and a structured warning is emitted.

    This is the single place the spec's "do NOT silently map by position
    when lengths differ" rule is enforced. Callers should pass the parsed
    ``outcomes`` list (already decoded from JSON) and the result of
    :func:`parse_clob_token_ids` for the same payload.
    """
    n_out = len(outcomes_raw)
    n_tok = len(tokens)
    if n_out == 0:
        return []
    if n_tok == 0:
        _MODULE_LOGGER.warning(
            "zip_outcomes_with_tokens[%s]: clobTokenIds missing/empty for %d outcomes; "
            "INCOMPLETE — clob_token_id will be NULL for every outcome (no silent position mapping)",
            source_label, n_out,
        )
        return [(i, str(label), None) for i, label in enumerate(outcomes_raw)]
    if n_tok != n_out:
        _MODULE_LOGGER.warning(
            "zip_outcomes_with_tokens[%s]: length mismatch (outcomes=%d, tokens=%d); "
            "INCOMPLETE — clob_token_id will be NULL for every outcome (no silent position mapping)",
            source_label, n_out, n_tok,
        )
        return [(i, str(label), None) for i, label in enumerate(outcomes_raw)]
    # Lengths match — zip positionally. NULL/empty-string token entries are
    # preserved as None so the column is normalized at the SQL boundary.
    zipped: list[tuple[int, str, Optional[str]]] = []
    for i, label in enumerate(outcomes_raw):
        tok = tokens[i]
        zipped.append((i, str(label), tok if tok else None))
    return zipped
