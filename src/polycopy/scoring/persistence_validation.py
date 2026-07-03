"""Application-level validators for PR 4 persistence.

Every CHECK constraint added to a CREATE TABLE definition in
``polycopy.db.schema_v10`` is mirrored here so that the same
invariants are enforced on databases that already exist at v10 or
v11 and therefore cannot receive new CHECKs through SQLite's
``ALTER TABLE``.

Behavior contract:

  * Reject with :class:`PersistenceValidationError` (a ValueError
    subclass) on any malformed input — never clamp, never silently
    coerce to NULL.
  * Finite numeric checks reject NaN, +inf, and -inf.
  * Optional numeric checks skip when the value is ``None``.
  * Enum checks accept both the canonical string values and the
    canonical enum instances; they reject unknown strings
    (including legacy lowercase aliases for exit tracks and
    uppercase aliases for shadow verdicts).
  * ``is_approved`` is enforced to be either 0 or 1; the runtime
    never sets it to 1 (PR 4 paper signals are NEVER approved).

The helpers are intentionally narrow — each is one decorator-free
function so the persistence layer can call them inline before
INSERT.
"""

from __future__ import annotations

import enum
import math
from typing import Any, Iterable, Optional, Union


# ---- Error ---------------------------------------------------------------


class PersistenceValidationError(ValueError):
    """Raised when a PR 4 persistence input fails a canonical invariant.

    Subclass of ValueError so callers that catch ValueError keep
    working, but typed catches can be precise.
    """


# ---- Canonical string sets (must mirror SQL CHECK constraints) ---------


# V1 (wallet/category/trade/paper_signal) — lowercase.
CANONICAL_V1_VERDICTS: frozenset[str] = frozenset({
    "copy_candidate",
    "watchlist",
    "skip",
    "incomplete",
})


# V2 shadow — uppercase.
CANONICAL_SHADOW_VERDICTS: frozenset[str] = frozenset({
    "SHADOW_COPY_CANDIDATE",
    "SHADOW_WATCHLIST",
    "SHADOW_SKIP",
    "SHADOW_INCOMPLETE",
})


# Exit tracks — uppercase canonical seven.
CANONICAL_EXIT_TRACKS: frozenset[str] = frozenset({
    "HOLD_TO_RESOLUTION",
    "EXIT_24H",
    "EXIT_72H",
    "FAVORABLE_MOVE_005",
    "FAVORABLE_MOVE_010",
    "FAVORABLE_MOVE_015",
    "THESIS_OR_LIQUIDITY_FAILURE",
})


# Delay scenarios — lowercase enum values.
CANONICAL_DELAY_SCENARIOS: frozenset[str] = frozenset({
    "theoretical_immediate",
    "delay_30_seconds",
    "delay_2_minutes",
    "delay_5_minutes",
    "delay_15_minutes",
    "actual_measured_delay",
})


# Behavior classification — lowercase enum values.
CANONICAL_BEHAVIOR_CLASSIFICATIONS: frozenset[str] = frozenset({
    "directional",
    "market_maker_lp",
    "arbitrage_multi_leg",
    "high_frequency_bot",
    "mixed",
    "unknown",
})


# ---- Finite / numeric primitives ----------------------------------------


def _is_finite_number(value: Any) -> bool:
    """True iff value is a finite real number (int or float, no NaN/inf)."""
    if isinstance(value, bool):
        # ``bool`` is technically a subclass of int but should never
        # be treated as a numeric score.
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def require_finite(
    value: Any,
    *,
    field: str,
) -> None:
    """Reject NaN / +-infinity / non-numeric values."""
    if not _is_finite_number(value):
        raise PersistenceValidationError(
            f"{field}: expected finite numeric, got {value!r}"
        )


def require_optional_score(
    value: Any,
    *,
    field: str,
) -> None:
    """Reject None-pass-through; else enforce [0, 100]."""
    if value is None:
        return
    require_finite(value, field=field)
    if value < 0 or value > 100:
        raise PersistenceValidationError(
            f"{field}: score must be in [0, 100], got {value!r}"
        )


def require_optional_fill_ratio(
    value: Any,
    *,
    field: str,
) -> None:
    """Reject if outside [0, 1]."""
    if value is None:
        return
    require_finite(value, field=field)
    if value < 0 or value > 1:
        raise PersistenceValidationError(
            f"{field}: fill ratio must be in [0, 1], got {value!r}"
        )


def require_optional_nonnegative(
    value: Any,
    *,
    field: str,
) -> None:
    """Reject negative or NaN/inf."""
    if value is None:
        return
    require_finite(value, field=field)
    if value < 0:
        raise PersistenceValidationError(
            f"{field}: must be >= 0, got {value!r}"
        )


def require_optional_nonnegative_int(
    value: Any,
    *,
    field: str,
) -> None:
    """Reject negative, NaN/inf.

    Accepts integers or floats (integer-valued floats are tolerated
    because the SQLite columns are REAL and many runtime paths pass
    ``30.0`` rather than ``30``). The schema CHECK is ``>= 0``,
    not ``typeof(x) = 'integer'``, so the application check mirrors
    that.
    """
    if value is None:
        return
    require_finite(value, field=field)
    if value < 0:
        raise PersistenceValidationError(
            f"{field}: must be >= 0, got {value!r}"
        )


def require_optional_delay_seconds(
    value: Any,
    *,
    field: str,
) -> None:
    """Delay seconds must be finite and >= 0 when present."""
    require_optional_nonnegative(value, field=field)


def require_optional_measured_delay_seconds(
    value: Any,
    *,
    field: str,
) -> None:
    """Measured delay is identical to delay-seconds contract."""
    require_optional_nonnegative(value, field=field)


def require_optional_boolean_int(
    value: Any,
    *,
    field: str,
) -> None:
    """Reject anything except 0, 1, None, or a Python bool.

    Python ``True``/``False`` are coerced to 1/0 because many
    runtime paths return ``True``/``False`` from comparison
    expressions while the SQLite CHECK accepts ``IN (0, 1)``.
    Other types are rejected.
    """
    if value is None:
        return
    if isinstance(value, bool):
        return  # True == 1, False == 0 — accepted.
    if isinstance(value, int):
        if value not in (0, 1):
            raise PersistenceValidationError(
                f"{field}: must be 0 or 1, got {value!r}"
            )
        return
    raise PersistenceValidationError(
        f"{field}: expected 0 or 1, got {type(value).__name__}={value!r}"
    )


# ---- Enum checks --------------------------------------------------------


def _enum_to_str(value: Any) -> str:
    """Return the canonical string for a str-enum instance, or the value itself."""
    if isinstance(value, enum.Enum):
        return str(value.value)
    return str(value)


def require_canonical_v1_verdict(
    value: Any,
    *,
    field: str,
) -> None:
    if value is None:
        raise PersistenceValidationError(
            f"{field}: V1 verdict cannot be None"
        )
    s = _enum_to_str(value)
    if s not in CANONICAL_V1_VERDICTS:
        raise PersistenceValidationError(
            f"{field}: invalid V1 verdict {value!r}; "
            f"expected one of {sorted(CANONICAL_V1_VERDICTS)}"
        )


def require_canonical_shadow_verdict(
    value: Any,
    *,
    field: str,
    optional: bool = False,
) -> None:
    if value is None:
        if optional:
            return
        raise PersistenceValidationError(
            f"{field}: shadow verdict cannot be None"
        )
    s = _enum_to_str(value)
    if s not in CANONICAL_SHADOW_VERDICTS:
        raise PersistenceValidationError(
            f"{field}: invalid shadow verdict {value!r}; "
            f"expected one of {sorted(CANONICAL_SHADOW_VERDICTS)}"
        )


def require_canonical_exit_track(
    value: Any,
    *,
    field: str,
) -> None:
    if value is None:
        raise PersistenceValidationError(
            f"{field}: exit track cannot be None"
        )
    s = _enum_to_str(value)
    if s not in CANONICAL_EXIT_TRACKS:
        raise PersistenceValidationError(
            f"{field}: invalid exit track {value!r}; "
            f"expected one of {sorted(CANONICAL_EXIT_TRACKS)} "
            f"(lowercase legacy aliases are rejected)"
        )


def require_canonical_delay_scenario(
    value: Any,
    *,
    field: str,
    optional: bool = False,
) -> None:
    if value is None:
        if optional:
            return
        raise PersistenceValidationError(
            f"{field}: delay scenario cannot be None"
        )
    s = _enum_to_str(value)
    if s not in CANONICAL_DELAY_SCENARIOS:
        raise PersistenceValidationError(
            f"{field}: invalid delay scenario {value!r}; "
            f"expected one of {sorted(CANONICAL_DELAY_SCENARIOS)}"
        )


def require_canonical_behavior_classification(
    value: Any,
    *,
    field: str,
    optional: bool = False,
) -> None:
    if value is None:
        if optional:
            return
        raise PersistenceValidationError(
            f"{field}: behavior classification cannot be None"
        )
    s = _enum_to_str(value)
    if s not in CANONICAL_BEHAVIOR_CLASSIFICATIONS:
        raise PersistenceValidationError(
            f"{field}: invalid behavior classification {value!r}; "
            f"expected one of {sorted(CANONICAL_BEHAVIOR_CLASSIFICATIONS)}"
        )


# ---- Auto-approval guardrail --------------------------------------------


def require_unapproved(
    is_approved: Any,
    *,
    field: str = "is_approved",
    auto_approve_requested: Optional[bool] = None,
) -> None:
    """Enforce the PR 4 paper-signal safety contract.

    - ``is_approved`` must be 0 or 1 (boolean int).
    - When ``is_approved == 1`` is set, raise unconditionally (PR 4
      paper signals are NEVER approved).
    - When ``auto_approve_requested`` is True AND ``is_approved == 0``,
      the caller is expected to record a safety reason. We don't
      raise here — we only enforce the invariant that
      ``is_approved != 1``.
    """
    require_optional_boolean_int(is_approved, field=field)
    if is_approved == 1:
        raise PersistenceValidationError(
            f"{field}: PR 4 paper signals are NEVER approved; "
            f"auto-approve attempts are rejected. "
            f"auto_approve_requested={auto_approve_requested!r}"
        )


# ---- Composite validator entry points ----------------------------------


def validate_decision_row(
    *,
    final_score: Any,
    verdict: Any,
) -> None:
    """Validate a V1 decision row's final_score + verdict."""
    require_optional_score(final_score, field="final_score")
    require_canonical_v1_verdict(verdict, field="verdict")


def validate_shadow_row(
    *,
    final_score: Any,
    verdict: Any,
    delay_scenario: Any,
    delay_seconds: Any = None,
    fill_percentage: Any = None,
    measured_delay_seconds: Any = None,
    copied_trade_count: Any = None,
    days_since_last_trade: Any = None,
) -> None:
    """Validate a V2 shadow-decision row's score + verdict + scenario."""
    require_optional_score(final_score, field="final_score")
    require_canonical_shadow_verdict(verdict, field="verdict")
    require_canonical_delay_scenario(
        delay_scenario, field="delay_scenario", optional=True,
    )
    require_optional_delay_seconds(delay_seconds, field="delay_seconds")
    require_optional_fill_ratio(fill_percentage, field="fill_percentage")
    require_optional_measured_delay_seconds(
        measured_delay_seconds, field="measured_delay_seconds",
    )
    require_optional_nonnegative_int(
        copied_trade_count, field="copied_trade_count",
    )
    require_optional_nonnegative_int(
        days_since_last_trade, field="days_since_last_trade",
    )


def validate_trade_row(
    *,
    final_score: Any,
    verdict: Any,
    intended_stake: Any = None,
    executable_depth: Any = None,
    fill_percentage: Any = None,
    trade_age_seconds: Any = None,
    seconds_to_market_end: Any = None,
    market_active: Any = None,
    market_closed: Any = None,
    market_resolved: Any = None,
) -> None:
    """Validate a trade-copyability row."""
    require_optional_score(final_score, field="final_score")
    require_canonical_v1_verdict(verdict, field="verdict")
    require_optional_nonnegative(intended_stake, field="intended_stake")
    require_optional_nonnegative(executable_depth, field="executable_depth")
    require_optional_fill_ratio(fill_percentage, field="fill_percentage")
    require_optional_nonnegative_int(
        trade_age_seconds, field="trade_age_seconds",
    )
    require_optional_nonnegative_int(
        seconds_to_market_end, field="seconds_to_market_end",
    )
    require_optional_boolean_int(market_active, field="market_active")
    require_optional_boolean_int(market_closed, field="market_closed")
    require_optional_boolean_int(market_resolved, field="market_resolved")


def validate_paper_signal_row(
    *,
    signal_family: Any = None,
    wallet_score: Any,
    trade_score: Any,
    shadow_score: Any,
    shadow_verdict: Any,
    final_verdict: Any,
    is_approved: Any,
    auto_approve_requested: Optional[bool] = None,
) -> None:
    """Validate a paper-signal row, including the auto-approval guardrail.

    ``signal_family`` is validated as a lowercase V1 verdict enum
    (signal_family and final_verdict share the same canonical set).
    """
    if signal_family is not None:
        require_canonical_v1_verdict(signal_family, field="signal_family")
    require_optional_score(wallet_score, field="wallet_score")
    require_optional_score(trade_score, field="trade_score")
    require_optional_score(shadow_score, field="shadow_score")
    require_canonical_shadow_verdict(
        shadow_verdict, field="shadow_verdict", optional=True,
    )
    require_canonical_v1_verdict(final_verdict, field="final_verdict")
    require_unapproved(
        is_approved,
        field="is_approved",
        auto_approve_requested=auto_approve_requested,
    )


def validate_wallet_row(
    *,
    final_score: Any,
    verdict: Any,
    trade_count: Any = None,
    category_trade_count: Any = None,
    category_distinct_markets: Any = None,
    overall_trade_count: Any = None,
    resolved_markets: Any = None,
    active_trading_days: Any = None,
    distinct_events: Any = None,
    category_resolved_markets: Any = None,
    category_distinct_events: Any = None,
    category_active_days: Any = None,
) -> None:
    """Validate a wallet-score row."""
    validate_decision_row(final_score=final_score, verdict=verdict)
    for fname, fval in (
        ("trade_count", trade_count),
        ("category_trade_count", category_trade_count),
        ("category_distinct_markets", category_distinct_markets),
        ("overall_trade_count", overall_trade_count),
        ("resolved_markets", resolved_markets),
        ("active_trading_days", active_trading_days),
        ("distinct_events", distinct_events),
        ("category_resolved_markets", category_resolved_markets),
        ("category_distinct_events", category_distinct_events),
        ("category_active_days", category_active_days),
    ):
        require_optional_nonnegative_int(fval, field=fname)


def validate_category_row(
    *,
    final_score: Any,
    verdict: Any,
    trade_count: Any = None,
    category_trade_count: Any = None,
    category_distinct_markets: Any = None,
    overall_trade_count: Any = None,
    category_resolved_markets: Any = None,
    category_distinct_events: Any = None,
    category_active_days: Any = None,
) -> None:
    """Validate a category-wallet-score row."""
    validate_decision_row(final_score=final_score, verdict=verdict)
    for fname, fval in (
        ("trade_count", trade_count),
        ("category_trade_count", category_trade_count),
        ("category_distinct_markets", category_distinct_markets),
        ("overall_trade_count", overall_trade_count),
        ("category_resolved_markets", category_resolved_markets),
        ("category_distinct_events", category_distinct_events),
        ("category_active_days", category_active_days),
    ):
        require_optional_nonnegative_int(fval, field=fname)


def validate_exit_track(
    experiment_type: Any,
) -> None:
    """Validate a single exit-track identifier."""
    require_canonical_exit_track(experiment_type, field="experiment_type")


def validate_exit_track_batch(
    experiment_types: Iterable[Union[str, enum.Enum]],
) -> None:
    """Validate a full set of exit tracks for one paper signal."""
    for t in experiment_types:
        validate_exit_track(t)


__all__ = [
    "PersistenceValidationError",
    "CANONICAL_V1_VERDICTS",
    "CANONICAL_SHADOW_VERDICTS",
    "CANONICAL_EXIT_TRACKS",
    "CANONICAL_DELAY_SCENARIOS",
    "CANONICAL_BEHAVIOR_CLASSIFICATIONS",
    "require_finite",
    "require_optional_score",
    "require_optional_fill_ratio",
    "require_optional_nonnegative",
    "require_optional_nonnegative_int",
    "require_optional_delay_seconds",
    "require_optional_measured_delay_seconds",
    "require_optional_boolean_int",
    "require_canonical_v1_verdict",
    "require_canonical_shadow_verdict",
    "require_canonical_exit_track",
    "require_canonical_delay_scenario",
    "require_canonical_behavior_classification",
    "require_unapproved",
    "validate_decision_row",
    "validate_shadow_row",
    "validate_trade_row",
    "validate_paper_signal_row",
    "validate_wallet_row",
    "validate_category_row",
    "validate_exit_track",
    "validate_exit_track_batch",
]