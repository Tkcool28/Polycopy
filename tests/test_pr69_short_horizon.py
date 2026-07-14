from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polycopy.policy.short_horizon import (
    ACTUAL_LOCK_TOO_LONG,
    HORIZON_ALREADY_ENDED,
    HORIZON_ELIGIBLE,
    HORIZON_INVALID,
    HORIZON_PREFERRED,
    HORIZON_TOO_LONG,
    HORIZON_UNAVAILABLE,
    evaluate_short_horizon,
)

UTC = timezone.utc
REFERENCE = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def test_13_days_235959_is_preferred() -> None:
    result = evaluate_short_horizon(REFERENCE, REFERENCE + timedelta(days=13, hours=23, minutes=59, seconds=59))
    assert result.status == HORIZON_PREFERRED
    assert result.preferred is True and result.eligible is True


def test_exactly_14_days_is_preferred() -> None:
    result = evaluate_short_horizon(REFERENCE, REFERENCE + timedelta(days=14))
    assert result.status == HORIZON_PREFERRED


def test_over_14_is_eligible_when_under_hard_cap() -> None:
    result = evaluate_short_horizon(REFERENCE, REFERENCE + timedelta(days=14, seconds=1))
    assert result.status == HORIZON_ELIGIBLE
    assert result.preferred is False and result.eligible is True


def test_exactly_30_day_expected_release_is_eligible() -> None:
    result = evaluate_short_horizon(REFERENCE, REFERENCE + timedelta(days=24))
    assert result.expected_lock_seconds == 30 * 24 * 60 * 60
    assert result.status == HORIZON_ELIGIBLE


def test_one_second_over_30_day_expected_release_is_rejected() -> None:
    result = evaluate_short_horizon(REFERENCE, REFERENCE + timedelta(days=24, seconds=1))
    assert result.status == HORIZON_TOO_LONG
    assert result.eligible is False


def test_missing_and_malformed_end_are_fail_closed() -> None:
    assert evaluate_short_horizon(REFERENCE, None).status == HORIZON_UNAVAILABLE
    assert evaluate_short_horizon(REFERENCE, "not-a-date").status == HORIZON_INVALID


def test_naive_and_negative_dates_are_invalid() -> None:
    assert evaluate_short_horizon(REFERENCE, datetime(2026, 7, 16)).status == HORIZON_INVALID
    assert evaluate_short_horizon(REFERENCE, REFERENCE - timedelta(seconds=1)).status == HORIZON_ALREADY_ENDED


def test_actual_redeem_under_hard_cap_confirms_but_does_not_change_preference() -> None:
    result = evaluate_short_horizon(REFERENCE, REFERENCE + timedelta(days=14), actual_redeem_timestamp=REFERENCE + timedelta(days=20))
    assert result.status == HORIZON_PREFERRED
    assert result.actual_lock_seconds == 20 * 24 * 60 * 60


def test_actual_redeem_over_hard_cap_rejects_history() -> None:
    result = evaluate_short_horizon(REFERENCE, REFERENCE + timedelta(days=14), actual_redeem_timestamp=REFERENCE + timedelta(days=30, seconds=1))
    assert result.status == ACTUAL_LOCK_TOO_LONG
    assert result.eligible is False


def test_actual_redeem_cannot_rescue_scheduled_long_horizon() -> None:
    result = evaluate_short_horizon(REFERENCE, REFERENCE + timedelta(days=25), actual_redeem_timestamp=REFERENCE + timedelta(days=5))
    assert result.status == HORIZON_TOO_LONG
    assert result.eligible is False


@pytest.mark.parametrize("value", [REFERENCE.isoformat(), REFERENCE.strftime("%Y-%m-%dT%H:%M:%SZ")])
def test_utc_string_parsing_is_deterministic(value: str) -> None:
    result = evaluate_short_horizon(value, REFERENCE + timedelta(days=14))
    assert result.reference_timestamp == REFERENCE
