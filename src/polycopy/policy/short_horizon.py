"""Pure, fail-closed short-horizon policy for copy and discovery workflows."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

PREFERRED_END_DAYS = 14
MAX_CAPITAL_LOCK_DAYS = 30
RESOLUTION_BUFFER_DAYS = 6
POLICY_VERSION = "short-horizon-v1"

HORIZON_PREFERRED = "HORIZON_PREFERRED"
HORIZON_ELIGIBLE = "HORIZON_ELIGIBLE"
HORIZON_TOO_LONG = "HORIZON_TOO_LONG"
HORIZON_UNAVAILABLE = "HORIZON_UNAVAILABLE"
HORIZON_INVALID = "HORIZON_INVALID"
HORIZON_ALREADY_ENDED = "HORIZON_ALREADY_ENDED"
ACTUAL_LOCK_TOO_LONG = "ACTUAL_LOCK_TOO_LONG"


@dataclass(frozen=True)
class ShortHorizonAssessment:
    reference_timestamp: datetime | None
    market_end_timestamp: datetime | None
    expected_release_timestamp: datetime | None
    actual_redeem_timestamp: datetime | None
    scheduled_end_seconds: int | None
    expected_lock_seconds: int | None
    actual_lock_seconds: int | None
    preferred: bool
    eligible: bool
    status: str
    reason_codes: tuple[str, ...]
    policy_version: str = POLICY_VERSION


def _utc_timestamp(value: Any) -> datetime:
    """Parse only exact aware timestamps; never silently assign a timezone."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise TypeError("timestamp is absent or not a datetime/ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("naive timestamp is not trusted")
    return parsed.astimezone(timezone.utc)


def _result(
    *, reference: datetime | None, end: datetime | None = None,
    expected_release: datetime | None = None, redeem: datetime | None = None,
    scheduled_seconds: int | None = None, expected_seconds: int | None = None,
    actual_seconds: int | None = None, preferred: bool = False, eligible: bool = False,
    status: str, reasons: tuple[str, ...],
) -> ShortHorizonAssessment:
    return ShortHorizonAssessment(
        reference_timestamp=reference, market_end_timestamp=end,
        expected_release_timestamp=expected_release, actual_redeem_timestamp=redeem,
        scheduled_end_seconds=scheduled_seconds, expected_lock_seconds=expected_seconds,
        actual_lock_seconds=actual_seconds, preferred=preferred, eligible=eligible,
        status=status, reason_codes=reasons,
    )


def evaluate_short_horizon(
    reference_timestamp: datetime | str,
    market_end_timestamp: datetime | str | None,
    *,
    actual_redeem_timestamp: datetime | str | None = None,
    preferred_end_days: int = PREFERRED_END_DAYS,
    max_capital_lock_days: int = MAX_CAPITAL_LOCK_DAYS,
    resolution_buffer_days: int = RESOLUTION_BUFFER_DAYS,
) -> ShortHorizonAssessment:
    """Evaluate exact UTC timestamps under the immutable default policy.

    Parameter overrides are retained solely for bounded report configuration;
    callers must validate policy caps before invoking the function.  No calendar
    rounding is performed: every comparison is a timedelta comparison.
    """
    try:
        reference = _utc_timestamp(reference_timestamp)
    except (TypeError, ValueError, OverflowError):
        return _result(reference=None, status=HORIZON_INVALID, reasons=("HORIZON_REFERENCE_INVALID",))
    if market_end_timestamp is None:
        return _result(reference=reference, status=HORIZON_UNAVAILABLE, reasons=("HORIZON_END_MISSING",))
    try:
        end = _utc_timestamp(market_end_timestamp)
    except (TypeError, ValueError, OverflowError):
        return _result(reference=reference, status=HORIZON_INVALID, reasons=("HORIZON_END_INVALID",))
    if min(preferred_end_days, max_capital_lock_days, resolution_buffer_days) < 0:
        return _result(reference=reference, end=end, status=HORIZON_INVALID, reasons=("HORIZON_POLICY_INVALID",))
    scheduled_delta = end - reference
    scheduled_seconds = int(scheduled_delta.total_seconds())
    if scheduled_seconds < 0:
        return _result(reference=reference, end=end, scheduled_seconds=scheduled_seconds, status=HORIZON_ALREADY_ENDED, reasons=("HORIZON_END_BEFORE_TRADE",))
    expected_release = end + timedelta(days=resolution_buffer_days)
    expected_seconds = int((expected_release - reference).total_seconds())
    actual_redeem: datetime | None = None
    actual_seconds: int | None = None
    if actual_redeem_timestamp is not None:
        try:
            actual_redeem = _utc_timestamp(actual_redeem_timestamp)
            actual_seconds = int((actual_redeem - reference).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return _result(reference=reference, end=end, expected_release=expected_release, scheduled_seconds=scheduled_seconds, expected_seconds=expected_seconds, status=HORIZON_INVALID, reasons=("ACTUAL_REDEEM_INVALID",))
        if actual_seconds < 0:
            return _result(reference=reference, end=end, expected_release=expected_release, redeem=actual_redeem, scheduled_seconds=scheduled_seconds, expected_seconds=expected_seconds, actual_seconds=actual_seconds, status=HORIZON_INVALID, reasons=("ACTUAL_REDEEM_BEFORE_TRADE",))
    hard_seconds = max_capital_lock_days * 24 * 60 * 60
    if expected_seconds > hard_seconds:
        return _result(reference=reference, end=end, expected_release=expected_release, redeem=actual_redeem, scheduled_seconds=scheduled_seconds, expected_seconds=expected_seconds, actual_seconds=actual_seconds, status=HORIZON_TOO_LONG, reasons=("LONG_HORIZON_REJECTED",))
    if actual_seconds is not None and actual_seconds > hard_seconds:
        return _result(reference=reference, end=end, expected_release=expected_release, redeem=actual_redeem, scheduled_seconds=scheduled_seconds, expected_seconds=expected_seconds, actual_seconds=actual_seconds, status=ACTUAL_LOCK_TOO_LONG, reasons=(ACTUAL_LOCK_TOO_LONG,))
    preferred = scheduled_seconds <= preferred_end_days * 24 * 60 * 60
    status = HORIZON_PREFERRED if preferred else HORIZON_ELIGIBLE
    reason = "SHORT_HORIZON_PREFERRED" if preferred else "SHORT_HORIZON_ELIGIBLE"
    return _result(reference=reference, end=end, expected_release=expected_release, redeem=actual_redeem, scheduled_seconds=scheduled_seconds, expected_seconds=expected_seconds, actual_seconds=actual_seconds, preferred=preferred, eligible=True, status=status, reasons=(reason,))


__all__ = [
    "ACTUAL_LOCK_TOO_LONG", "HORIZON_ALREADY_ENDED", "HORIZON_ELIGIBLE", "HORIZON_INVALID",
    "HORIZON_PREFERRED", "HORIZON_TOO_LONG", "HORIZON_UNAVAILABLE", "MAX_CAPITAL_LOCK_DAYS",
    "POLICY_VERSION", "PREFERRED_END_DAYS", "RESOLUTION_BUFFER_DAYS", "ShortHorizonAssessment",
    "evaluate_short_horizon",
]
