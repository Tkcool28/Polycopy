"""Bounded async HTTP helpers for PR69 discovery.

Used ONLY by the report-only discovery path. This module opens databases,
never invokes the production bridge, and never writes anything. Every
call counts toward a shared request budget that the operator CLI owns.

Contract:
  * GET only; no body, no auth.
  * 10-15s timeout per attempt; up to 3 attempts (initial + 2 retries).
  * Exponential backoff with jitter on 5xx and network errors.
  * Honors ``Retry-After`` on 429 (one re-read per attempt, capped at
    the per-call timeout); no infinite loops.
  * HTTP 4xx other than 429 returns fail-closed immediately.
  * Returns ``(data, status, retries, error_code)`` so callers can
    record a precise failure reason without inspecting the body.
  * Sanitized error messages; no auth tokens in logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

logger = logging.getLogger(__name__)


# Stable error codes that the audit/CI checks can pattern-match on.
ERR_TIMEOUT = "TIMEOUT"
ERR_CONNECTION_ERROR = "CONNECTION_ERROR"
ERR_HTTP_4XX = "HTTP_4XX"
ERR_HTTP_429 = "HTTP_429_NO_RETRY_AFTER"
ERR_HTTP_5XX = "HTTP_5XX"
ERR_RETRIES_EXHAUSTED = "RETRIES_EXHAUSTED"
ERR_RETRY_AFTER_EXCEEDED = "RETRY_AFTER_EXCEEDED"
ERR_INVALID_JSON = "INVALID_JSON"
ERR_INVALID_RESPONSE = "INVALID_RESPONSE"
ERR_BUDGET_EXHAUSTED = "REQUEST_BUDGET_EXHAUSTED"


@dataclass(frozen=True)
class SafeGetResult:
    """Result of one bounded GET.

    Exactly one of ``data`` or ``error_code`` is non-None on a terminal
    state. ``retries`` records retry attempts; ``status`` records the
    last HTTP status (0 = never saw a response).
    """

    data: Any
    status: int
    retries: int
    error_code: str | None


class _RequestBudget:
    """Mutable shared budget for the operator CLI to govern audit HTTP usage.

    Decremented per ATTEMPT (initial + retries). The audit report records
    ``budget.remaining_initial - budget.remaining`` as ``requests_used``.

    When ``phase_caps`` is supplied, each call to :meth:`acquire` is also
    scoped to a named phase. A phase can never exceed its cap and the
    remaining-budget check is unaffected.
    """

    __slots__ = ("remaining", "initial", "lock", "phase_caps", "phase_used", "phase_skipped")

    def __init__(self, max_requests: int, phase_caps: dict[str, int] | None = None) -> None:
        self.initial = max(0, int(max_requests))
        self.remaining = self.initial
        self.lock = asyncio.Lock()
        self.phase_caps: dict[str, int] = dict(phase_caps or {})
        self.phase_used: dict[str, int] = {}
        self.phase_skipped: dict[str, int] = {}

    async def acquire(self, phase: str | None = None) -> bool:
        async with self.lock:
            if self.remaining <= 0:
                if phase is not None:
                    self.phase_skipped[phase] = self.phase_skipped.get(phase, 0) + 1
                return False
            if phase is not None:
                cap = self.phase_caps.get(phase)
                if cap is not None and self.phase_used.get(phase, 0) >= cap:
                    self.phase_skipped[phase] = self.phase_skipped.get(phase, 0) + 1
                    return False
                self.phase_used[phase] = self.phase_used.get(phase, 0) + 1
            self.remaining -= 1
            return True

    def used(self) -> int:
        return self.initial - self.remaining

    def remaining_for(self, phase: str) -> int | None:
        cap = self.phase_caps.get(phase)
        if cap is None:
            return None
        return max(0, cap - self.phase_used.get(phase, 0))


def _backoff_seconds(attempt_index: int, base: float = 0.25, cap: float = 4.0) -> float:
    """Exponential backoff with bounded jitter. attempt_index is 0-based."""
    delay = min(cap, base * (2 ** attempt_index))
    return delay + random.uniform(0.0, 0.1 * delay)


async def safe_get_json(
    client: httpx.AsyncClient,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    timeout_seconds: float = 12.0,
    max_retries: int = 2,
    budget: _RequestBudget | None = None,
    phase: str | None = None,
    label: str = "safe_get_json",
) -> SafeGetResult:
    """One bounded GET returning parsed JSON with retry + budget guards.

    NEVER raises. ``label`` is for operator log readability only — no
    secrets, no full URLs.
    """
    if budget is not None and not await budget.acquire(phase):
        return SafeGetResult(data=None, status=0, retries=0, error_code=ERR_BUDGET_EXHAUSTED)

    attempts_max = max(0, int(max_retries)) + 1
    last_status = 0
    for attempt_index in range(attempts_max):
        if attempt_index > 0:
            if budget is not None and not await budget.acquire(phase):
                return SafeGetResult(
                    data=None,
                    status=last_status,
                    retries=attempt_index,
                    error_code=ERR_BUDGET_EXHAUSTED,
                )
            await asyncio.sleep(_backoff_seconds(attempt_index - 1))

        try:
            response = await client.get(
                path,
                params=dict(params or {}),
                timeout=float(timeout_seconds),
            )
        except httpx.TimeoutException:
            last_status = 0
            logger.debug("%s attempt=%d: timeout", label, attempt_index)
            if attempt_index >= attempts_max - 1:
                return SafeGetResult(data=None, status=0, retries=attempt_index, error_code=ERR_TIMEOUT)
            continue
        except httpx.HTTPError as exc:
            last_status = 0
            logger.debug("%s attempt=%d: %s", label, attempt_index, type(exc).__name__)
            if attempt_index >= attempts_max - 1:
                return SafeGetResult(
                    data=None,
                    status=0,
                    retries=attempt_index,
                    error_code=f"{ERR_CONNECTION_ERROR}:{type(exc).__name__}",
                )
            continue

        last_status = int(response.status_code)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after and attempt_index < attempts_max - 1:
                try:
                    wait_seconds = float(retry_after)
                except ValueError:
                    wait_seconds = 0.0
                wait_seconds = max(0.0, min(wait_seconds, float(timeout_seconds)))
                if wait_seconds > 0:
                    logger.debug("%s attempt=%d: honoring Retry-After=%.1fs", label, attempt_index, wait_seconds)
                    await asyncio.sleep(wait_seconds)
                    continue
            return SafeGetResult(
                data=None,
                status=429,
                retries=attempt_index,
                error_code=ERR_HTTP_429 if attempt_index == 0 else ERR_RETRY_AFTER_EXCEEDED,
            )
        if 400 <= response.status_code < 500:
            return SafeGetResult(
                data=None,
                status=response.status_code,
                retries=attempt_index,
                error_code=ERR_HTTP_4XX,
            )
        if response.status_code >= 500:
            if attempt_index >= attempts_max - 1:
                return SafeGetResult(
                    data=None,
                    status=response.status_code,
                    retries=attempt_index,
                    error_code=ERR_HTTP_5XX,
                )
            continue

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return SafeGetResult(
                data=None,
                status=response.status_code,
                retries=attempt_index,
                error_code=ERR_INVALID_JSON,
            )
        if not isinstance(payload, (list, dict)):
            return SafeGetResult(
                data=None,
                status=response.status_code,
                retries=attempt_index,
                error_code=ERR_INVALID_RESPONSE,
            )
        return SafeGetResult(
            data=payload,
            status=response.status_code,
            retries=attempt_index,
            error_code=None,
        )

    return SafeGetResult(
        data=None,
        status=last_status,
        retries=attempts_max,
        error_code=ERR_RETRIES_EXHAUSTED,
    )


__all__ = [
    "ERR_BUDGET_EXHAUSTED",
    "ERR_CONNECTION_ERROR",
    "ERR_HTTP_4XX",
    "ERR_HTTP_429",
    "ERR_HTTP_5XX",
    "ERR_INVALID_JSON",
    "ERR_INVALID_RESPONSE",
    "ERR_RETRIES_EXHAUSTED",
    "ERR_RETRY_AFTER_EXCEEDED",
    "ERR_TIMEOUT",
    "SafeGetResult",
    "_RequestBudget",
    "safe_get_json",
]
