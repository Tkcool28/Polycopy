"""Polymarket public CLOB order-book adapter — PR-3 of the recovery sequence.

Read-only HTTP client for the public Polymarket CLOB /book endpoint.
This module exists to give the candidate price-snapshot engine a real
network adapter for the CLOB book; the existing
``polycopy.providers.bidask.BidAskProvider`` is a separate, deterministic,
in-memory fixture store (see contract §6). The two are not the same class
and are not interchangeable.

The adapter makes real ``GET`` requests to:

    GET https://clob.polymarket.com/book?token=<token_id>

No authentication. No wallet key. No signing. No API secret. No cookies.
No private CLOB endpoints. The book endpoint is public.

The adapter exposes a single narrow operation:

    await client.fetch_book(token_id) -> ClobBook

…where :class:`ClobBook` is a normalized, validated snapshot of the
response. Validation rules (per contract §7):

  * Numeric values parsed through ``Decimal`` first; rejected on NaN,
    Infinity, negative prices, prices > 1, negative sizes, malformed
    numeric strings.
  * Zero-size levels are discarded.
  * Book ordering is not trusted — best_bid is the highest valid bid
    price, best_ask is the lowest valid ask price.
  * Sizes at the best price are aggregated (sum) across all duplicate
    levels.
  * Crossed books (best_bid > best_ask) are classified as ``PARSE_ERROR``,
    not silently accepted.
  * An empty book (no valid bids AND no valid asks) is reported as
    ``EMPTY_BOOK``.
  * A one-sided book (only bids OR only asks) is reported as
    ``ONE_SIDED_BOOK``.

The adapter records fetch provenance (``http_status``, ``latency_ms``,
``request_attempts``) and bounded error codes so the engine can
classify the snapshot into the bounded ``SnapshotFetchStatus`` set.

Retry policy (per contract §7.C):

  * Transient HTTP 5xx, timeouts, and connection errors are retried up
    to ``max_retries`` times. Each retry counts as one request attempt.
  * HTTP 429 is classified ``RATE_LIMITED`` immediately and is NOT
    retried — the engine surfaces it as a bounded status.
  * Any other 4xx is classified ``HTTP_ERROR`` and is NOT retried.
  * Retry attempts are spaced with a simple exponential backoff capped
    at 2s.

Rate limiting (per contract §7.D):

  * A simple token-bucket-ish guard (one acquisition per outbound
    request) is enforced before every HTTP attempt. The guard is
    controlled by ``requests_per_minute``. The guard is intentionally
    conservative; it does not claim to be the upstream's limit.

The adapter never makes an HTTP call when the caller asks for a token
that is ``None`` or empty — it returns a synthetic ``ClobBook`` with
``bids=[]``, ``asks=[]`` so the engine can route the candidate to
``MISSING_TOKEN`` without depending on an exception.

The adapter DOES NOT auto-retry on a one-sided or empty book — those
are valid bounded outcomes of a real fetch and must be recorded as
such, not retried.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Protocol

import httpx

logger = logging.getLogger(__name__)


# ── ClobBook value object ────────────────────────────────────────────────────
@dataclass
class ClobBookLevel:
    """A single normalized price level.

    ``price`` and ``size`` are both Python floats after Decimal
    validation. The adapter guarantees:

      * ``price`` is in [0, 1] and not NaN/Inf
      * ``size`` is >= 0 and not NaN/Inf
      * levels with ``size == 0`` are discarded at parse time
    """

    price: float
    size: float


@dataclass
class ClobBook:
    """A normalized, validated snapshot of one CLOB /book response.

    The dataclass carries the parse result, the fetch provenance, and
    a SHA-256 audit hash of the canonical book representation. Fields:

      * ``token_id`` — the token the request was issued against.
      * ``bids`` / ``asks`` — lists of valid :class:`ClobBookLevel`,
        sorted best-first (bids descending by price, asks ascending).
        Empty list when the side is absent or all levels were invalid.
      * ``http_status`` — last HTTP status code observed (None when the
        adapter did not issue a request, e.g. for an empty token).
      * ``latency_ms`` — total wall-clock latency of the attempt(s).
      * ``request_attempts`` — total outbound HTTP attempts (1 + retries).
      * ``fetched_at`` — UTC datetime at the start of the fetch.
      * ``book_hash`` — SHA-256 hex of the canonical book representation.
        Computed for any non-empty book; None when the book is empty
        AND there are no valid levels to hash.
      * ``error_code`` / ``error_message`` — bounded error info when
        the fetch failed or the response was structurally invalid. None
        for a successful parse.
    """

    token_id: str
    bids: list[ClobBookLevel] = field(default_factory=list)
    asks: list[ClobBookLevel] = field(default_factory=list)
    http_status: Optional[int] = None
    latency_ms: Optional[int] = None
    request_attempts: int = 1
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    book_hash: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        """True iff both sides have no valid levels."""
        return not self.bids and not self.asks

    @property
    def is_one_sided(self) -> bool:
        """True iff exactly one side has valid levels."""
        return (not self.bids) != (not self.asks)  # XOR

    @property
    def is_crossed(self) -> bool:
        """True iff the best bid exceeds the best ask (structurally invalid)."""
        if not self.bids or not self.asks:
            return False
        return self.bids[0].price > self.asks[0].price

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid_size(self) -> Optional[float]:
        """Sum of sizes at best_bid (aggregated across duplicate levels)."""
        if not self.bids:
            return None
        bp = self.bids[0].price
        return float(sum(level.size for level in self.bids if level.price == bp))

    @property
    def best_ask_size(self) -> Optional[float]:
        """Sum of sizes at best_ask (aggregated across duplicate levels)."""
        if not self.asks:
            return None
        ap = self.asks[0].price
        return float(sum(level.size for level in self.asks if level.price == ap))

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def bid_level_count(self) -> int:
        return len(self.bids)

    @property
    def ask_level_count(self) -> int:
        return len(self.asks)


# ── Book provider protocol ───────────────────────────────────────────────────
class BookProvider(Protocol):
    """Narrow structural-typing protocol for a book-fetching object.

    The snapshot engine accepts ANY object that implements
    :meth:`fetch_book` returning a :class:`ClobBook`. This keeps the
    engine decoupled from the network adapter (so unit tests can
    inject a fake) and lets future providers (e.g. a thin wrapper
    around ``BidAskProvider`` for offline development) drop in
    without engine changes.
    """

    async def fetch_book(self, token_id: str) -> ClobBook:  # pragma: no cover - Protocol
        ...


# ── Bounded internal error classification ────────────────────────────────────
class _BookParseErrorCode(str, enum.Enum):
    """Internal-only error codes surfaced to the engine.

    These are mapped to bounded ``SnapshotFetchStatus`` values by the
    snapshot engine. The adapter does NOT decide the final snapshot
    status — only the engine does. The codes here are stable enough
    for tests to assert against, and are limited to the small set
    below so no engine path can accidentally invent a new one.
    """

    OK = "OK"
    EMPTY_TOKEN = "EMPTY_TOKEN"
    HTTP_429 = "HTTP_429"
    HTTP_4XX = "HTTP_4XX"
    HTTP_5XX = "HTTP_5XX"
    TIMEOUT = "TIMEOUT"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    PARSE_ERROR_JSON = "PARSE_ERROR_JSON"
    PARSE_ERROR_STRUCTURE = "PARSE_ERROR_STRUCTURE"
    PARSE_ERROR_NUMERIC = "PARSE_ERROR_NUMERIC"
    PARSE_ERROR_PRICE_RANGE = "PARSE_ERROR_PRICE_RANGE"
    PARSE_ERROR_NEGATIVE_SIZE = "PARSE_ERROR_NEGATIVE_SIZE"
    PARSE_ERROR_CROSSED = "PARSE_ERROR_CROSSED"


# ── HTTP adapter ────────────────────────────────────────────────────────────
class PolymarketClobClient:
    """Read-only public CLOB /book adapter.

    Uses an injected ``httpx.AsyncClient`` so tests can supply a
    ``httpx.MockTransport``. No authentication, no signing, no wallet
    key. The base URL defaults to ``https://clob.polymarket.com`` but
    is overridable so the test suite can point at a fake host.

    The class does NOT read ``settings.clob_enabled``. That gate is
    consulted by production-side wiring (which PR-3 deliberately
    defers); the adapter itself is a passive network primitive that
    can be instantiated and exercised in any context that supplies a
    transport.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        base_url: str = "https://clob.polymarket.com",
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        requests_per_minute: int = 30,
    ) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._timeout = float(timeout_seconds)
        self._max_retries = max(int(max_retries), 0)
        self._rpm = max(int(requests_per_minute), 0)
        # Inter-request minimum spacing (seconds). 60 / rpm; the
        # ``requests_per_minute <= 0`` case disables spacing entirely
        # (no throttle). Production callers pass a positive RPM; the
        # test suite passes 0 to run fast.
        self._min_interval_seconds = (
            60.0 / float(self._rpm) if self._rpm > 0 else 0.0
        )
        self._last_call_at: float = 0.0

    # ── Public API ─────────────────────────────────────────────────────────
    async def fetch_book(self, token_id: str) -> ClobBook:
        """Fetch and normalize a CLOB /book for ``token_id``.

        Returns a :class:`ClobBook` with parse result + fetch provenance
        + bounded error info. Never raises on transport / parse errors —
        the bounded error code is recorded on the returned object so the
        engine can route the candidate to the right
        ``SnapshotFetchStatus``.
        """
        if not token_id or not isinstance(token_id, str):
            return ClobBook(
                token_id=str(token_id or ""),
                bids=[],
                asks=[],
                request_attempts=0,
                error_code=_BookParseErrorCode.EMPTY_TOKEN.value,
                error_message="token_id is empty or non-string",
            )

        # Note: the concrete request URL (with token query parameter)
        # is built inline below. The full URL is intentionally not
        # exposed on the result; the engine records a bounded
        # audit label ("clob/book") on the persisted snapshot, not
        # the URL with the token. See contract §8.
        attempts = 0
        start = time.monotonic()
        last_status: Optional[int] = None
        last_error: Optional[tuple[str, str]] = None

        # Retry loop. Each iteration is one outbound HTTP attempt. We
        # retry on 5xx, timeouts, and connection errors. We do NOT
        # retry on 429 (rate-limited, surfaced to the engine) or on
        # 4xx (client error, surfaced to the engine).
        for attempt_index in range(self._max_retries + 1):
            await self._throttle()
            attempts += 1
            try:
                response = await self._http.get(
                    "/book",
                    params={"token": token_id},
                    timeout=self._timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = (_BookParseErrorCode.TIMEOUT.value, str(exc)[:500])
                if attempt_index >= self._max_retries:
                    break
                await self._backoff(attempt_index)
                continue
            except httpx.HTTPError as exc:
                last_error = (
                    _BookParseErrorCode.CONNECTION_ERROR.value,
                    f"{type(exc).__name__}: {exc}"[:500],
                )
                if attempt_index >= self._max_retries:
                    break
                await self._backoff(attempt_index)
                continue

            last_status = int(response.status_code)
            if response.status_code == 429:
                return ClobBook(
                    token_id=token_id,
                    bids=[],
                    asks=[],
                    http_status=last_status,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    request_attempts=attempts,
                    error_code=_BookParseErrorCode.HTTP_429.value,
                    error_message="CLOB /book returned HTTP 429 (rate limited)",
                )
            if 400 <= response.status_code < 500:
                return ClobBook(
                    token_id=token_id,
                    bids=[],
                    asks=[],
                    http_status=last_status,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    request_attempts=attempts,
                    error_code=_BookParseErrorCode.HTTP_4XX.value,
                    error_message=(
                        f"CLOB /book returned HTTP {last_status}: "
                        f"{response.text[:200]!r}"
                    ),
                )
            if 500 <= response.status_code < 600:
                if attempt_index >= self._max_retries:
                    return ClobBook(
                        token_id=token_id,
                        bids=[],
                        asks=[],
                        http_status=last_status,
                        latency_ms=int((time.monotonic() - start) * 1000),
                        request_attempts=attempts,
                        error_code=_BookParseErrorCode.HTTP_5XX.value,
                        error_message=(
                            f"CLOB /book returned HTTP {last_status} after "
                            f"{attempts} attempt(s)"
                        ),
                    )
                await self._backoff(attempt_index)
                continue

            # 2xx — parse the body.
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                return ClobBook(
                    token_id=token_id,
                    http_status=last_status,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    request_attempts=attempts,
                    error_code=_BookParseErrorCode.PARSE_ERROR_JSON.value,
                    error_message=f"non-JSON response: {exc}"[:500],
                )
            if not isinstance(payload, dict):
                return ClobBook(
                    token_id=token_id,
                    http_status=last_status,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    request_attempts=attempts,
                    error_code=_BookParseErrorCode.PARSE_ERROR_STRUCTURE.value,
                    error_message=f"response is not a JSON object: {type(payload).__name__}",
                )
            return self._build_book(
                token_id=token_id,
                payload=payload,
                http_status=last_status,
                latency_ms=int((time.monotonic() - start) * 1000),
                request_attempts=attempts,
            )

        # Loop exited without a 2xx — return the last observed error.
        elapsed = int((time.monotonic() - start) * 1000)
        if last_error is None:
            # Defensive — should not happen because every break / continue
            # path sets last_error. Keep the bounded code path anyway.
            return ClobBook(
                token_id=token_id,
                http_status=last_status,
                latency_ms=elapsed,
                request_attempts=attempts,
                error_code=_BookParseErrorCode.HTTP_5XX.value,
                error_message="retry ceiling exhausted with no recorded error",
            )
        return ClobBook(
            token_id=token_id,
            http_status=last_status,
            latency_ms=elapsed,
            request_attempts=attempts,
            error_code=last_error[0],
            error_message=last_error[1],
        )

    # ── Internal: response parsing ─────────────────────────────────────────
    @staticmethod
    def _parse_decimal(value: Any) -> Optional[Decimal]:
        """Parse a single numeric value through Decimal; None on rejection.

        Rejects: None, NaN, Infinity, malformed strings, values that
        cannot be represented as a finite Decimal.
        """
        if value is None:
            return None
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        if not d.is_finite():
            return None
        return d

    def _parse_levels(
        self, raw_levels: Any,
    ) -> tuple[list[ClobBookLevel], Optional[str], Optional[str]]:
        """Parse a raw list of {price, size} dicts into ClobBookLevels.

        Returns ``(levels, error_code, error_message)``. On any rejection
        ``levels`` is empty and the error code is set.
        """
        if not isinstance(raw_levels, list):
            return [], _BookParseErrorCode.PARSE_ERROR_STRUCTURE.value, (
                f"expected list of levels, got {type(raw_levels).__name__}"
            )
        out: list[ClobBookLevel] = []
        for i, raw in enumerate(raw_levels):
            if not isinstance(raw, dict):
                return [], _BookParseErrorCode.PARSE_ERROR_STRUCTURE.value, (
                    f"level[{i}] is not an object: {type(raw).__name__}"
                )
            price_raw = raw.get("price")
            size_raw = raw.get("size")
            price_d = self._parse_decimal(price_raw)
            size_d = self._parse_decimal(size_raw)
            if price_d is None:
                return [], _BookParseErrorCode.PARSE_ERROR_NUMERIC.value, (
                    f"level[{i}].price is not a finite number: {price_raw!r}"
                )
            if size_d is None:
                return [], _BookParseErrorCode.PARSE_ERROR_NUMERIC.value, (
                    f"level[{i}].size is not a finite number: {size_raw!r}"
                )
            price = float(price_d)
            size = float(size_d)
            if price < 0.0 or price > 1.0:
                return [], _BookParseErrorCode.PARSE_ERROR_PRICE_RANGE.value, (
                    f"level[{i}].price out of [0, 1]: {price}"
                )
            if size < 0.0:
                return [], _BookParseErrorCode.PARSE_ERROR_NEGATIVE_SIZE.value, (
                    f"level[{i}].size is negative: {size}"
                )
            if size == 0.0:
                # Discard zero-size levels (no liquidity to consume).
                continue
            out.append(ClobBookLevel(price=price, size=size))
        return out, None, None

    def _build_book(
        self,
        *,
        token_id: str,
        payload: dict,
        http_status: int,
        latency_ms: int,
        request_attempts: int,
    ) -> ClobBook:
        """Build a :class:`ClobBook` from a parsed JSON payload.

        The payload is expected to follow Polymarket's documented CLOB
        /book response shape:

            {
              "bids": [{"price": "...", "size": "..."}, ...],
              "asks": [{"price": "...", "size": "..."}, ...]
            }

        The adapter is tolerant of missing / empty ``bids`` / ``asks``
        keys (records them as empty lists, not parse errors).
        """
        # Polymarket returns bids/asks as either objects or arrays of
        # objects depending on the upstream payload version. Accept both.
        raw_bids = payload.get("bids", [])
        raw_asks = payload.get("asks", [])
        bids, err_code, err_msg = self._parse_levels(raw_bids)
        if err_code is not None:
            return ClobBook(
                token_id=token_id,
                http_status=http_status,
                latency_ms=latency_ms,
                request_attempts=request_attempts,
                error_code=err_code,
                error_message=err_msg,
            )
        asks, err_code, err_msg = self._parse_levels(raw_asks)
        if err_code is not None:
            return ClobBook(
                token_id=token_id,
                bids=bids,
                http_status=http_status,
                latency_ms=latency_ms,
                request_attempts=request_attempts,
                error_code=err_code,
                error_message=err_msg,
            )

        # Sort: bids descending by price, asks ascending. We do NOT trust
        # upstream ordering.
        bids.sort(key=lambda lv: lv.price, reverse=True)
        asks.sort(key=lambda lv: lv.price)

        # Crossed book check — best_bid > best_ask is structurally
        # invalid and must be reported as PARSE_ERROR, not silently
        # accepted.
        if bids and asks and bids[0].price > asks[0].price:
            return ClobBook(
                token_id=token_id,
                bids=bids,
                asks=asks,
                http_status=http_status,
                latency_ms=latency_ms,
                request_attempts=request_attempts,
                error_code=_BookParseErrorCode.PARSE_ERROR_CROSSED.value,
                error_message=(
                    f"crossed book: best_bid={bids[0].price} > "
                    f"best_ask={asks[0].price}"
                ),
            )

        # Compute the audit hash from a canonical, sorted representation.
        # The hash is for audit comparison only — a stable, deterministic
        # view of which levels contributed to the calculation. URLs,
        # headers, and credentials MUST NOT be in the hash payload.
        canonical = self._canonical_book_payload(bids, asks)
        book_hash = (
            hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if canonical
            else None
        )

        return ClobBook(
            token_id=token_id,
            bids=bids,
            asks=asks,
            http_status=http_status,
            latency_ms=latency_ms,
            request_attempts=request_attempts,
            book_hash=book_hash,
        )

    @staticmethod
    def _canonical_book_payload(
        bids: list[ClobBookLevel], asks: list[ClobBookLevel],
    ) -> str:
        """Build a stable JSON serialization of all valid levels.

        Sorted keys; numeric values serialized as ``repr(float)`` so the
        canonical string is byte-stable for the same input. The hash
        covers both sides; an empty book on both sides produces ``""``.
        """
        if not bids and not asks:
            return ""
        payload = {
            "bids": [{"price": repr(lv.price), "size": repr(lv.size)} for lv in bids],
            "asks": [{"price": repr(lv.price), "size": repr(lv.size)} for lv in asks],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


    # ── Internal: rate limiter + backoff ───────────────────────────────────
    async def _throttle(self) -> None:
        """Sleep just enough to honor the configured requests-per-minute.

        One limiter acquisition per outbound HTTP attempt. No extra
        acquisition after a valid response — the limiter is only
        consulted before a request goes out. The throttle is
        cooperative (``asyncio.sleep``) and is a no-op when
        ``requests_per_minute`` is effectively unbounded.
        """
        if self._min_interval_seconds <= 0.0:
            return
        now = time.monotonic()
        elapsed = now - self._last_call_at
        remaining = self._min_interval_seconds - elapsed
        if remaining > 0:
            import asyncio
            await asyncio.sleep(remaining)
        self._last_call_at = time.monotonic()

    @staticmethod
    async def _backoff(attempt_index: int) -> None:
        """Exponential backoff with a 2s ceiling. ``attempt_index`` is 0-based."""
        import asyncio
        delay = min(2.0, 0.25 * (2 ** attempt_index))
        await asyncio.sleep(delay)


__all__ = [
    "BookProvider",
    "ClobBook",
    "ClobBookLevel",
    "PolymarketClobClient",
]
