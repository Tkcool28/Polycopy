"""Typed V2 shadow scoring contracts (Chunk 5 — Phase 9).

Frozen dataclasses for the V2 shadow research track. The shadow
path is intentionally separate from V1 (paper signal / live)
so V2 can NEVER influence V1 outcomes, even when V2 inputs are
materially different.

Formula identity (frozen):

- name:    ``"Copy-Adjusted Alpha Score"``
- version: ``"2-shadow"``

Required typed inputs (frozen):

- ``wallet_id``                       : str
- ``source_trade_id``                 : str
- ``candidate_id``                    : Optional[int]
- ``delay_scenario``                  : DelayScenario enum value
- ``source_price``                    : Optional[float]
- ``delayed_copy_price``              : Optional[float]
- ``intended_stake``                  : Optional[float]
- ``executable_depth``                : Optional[float]
- ``fill_percentage``                 : Optional[float]
- ``slippage``                        : Optional[float]
- ``spread``                          : Optional[float]
- ``wallet_skill_persistence_input``  : Optional[float]
- ``copied_realized_performance_input`` : Optional[float]
- ``concentration_correlation_input`` : Optional[float]
- ``source_data_timestamp``           : Optional[str]
- ``price_snapshot_id``               : Optional[str]
- ``depth_hash``                      : Optional[str]
- ``missing_forward_reasons``         : tuple[str, ...]

The result keeps the exact typed input (immutable).

Missing forward evidence produces ``SHADOW_INCOMPLETE`` — never
silently substituted with zero. ``missing_forward_reasons``
is the explicit audit list.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Tuple


# ---- Formula identity (frozen) -------------------------------------------

SHADOW_FORMULA_NAME = "Copy-Adjusted Alpha Score"
SHADOW_FORMULA_VERSION = "2-shadow"


# ---- Delay scenarios (canonical, frozen) ---------------------------------

class DelayScenario(str, enum.Enum):
    """Canonical delay-scenario identifiers.

    Each scenario is a separate immutable research observation —
    the persisted table stores one row per (delay_scenario,
    idempotency key).
    """

    THEORETICAL_IMMEDIATE = "theoretical_immediate"
    DELAY_30_SECONDS = "delay_30_seconds"
    DELAY_2_MINUTES = "delay_2_minutes"
    DELAY_5_MINUTES = "delay_5_minutes"
    DELAY_15_MINUTES = "delay_15_minutes"
    ACTUAL_MEASURED_DELAY = "actual_measured_delay"


# Frozen delay-seconds mapping. ACTUAL_MEASURED_DELAY is computed
# from real persisted timestamps, never assumed.
DELAY_SCENARIO_SECONDS: dict[DelayScenario, Optional[float]] = {
    DelayScenario.THEORETICAL_IMMEDIATE: 0.0,
    DelayScenario.DELAY_30_SECONDS: 30.0,
    DelayScenario.DELAY_2_MINUTES: 120.0,
    DelayScenario.DELAY_5_MINUTES: 300.0,
    DelayScenario.DELAY_15_MINUTES: 900.0,
    DelayScenario.ACTUAL_MEASURED_DELAY: None,  # computed at runtime
}


# Bounded tolerance window (Repair 2c). For a fixed-delay scenario
# to consider a persisted snapshot valid, that snapshot's
# ``fetched_at`` must fall in
# ``[source_trade_timestamp + delay_seconds,
#    source_trade_timestamp + delay_seconds + tolerance]``.
# A snapshot that is much later than expected is rejected — it does
# not represent "the price 30s after the trade"; it represents a
# stale observation. THEORETICAL_IMMEDIATE and ACTUAL_MEASURED_DELAY
# have no fixed target so they don't use this map (their windows
# are defined by the scenario itself).
DELAY_SCENARIO_TOLERANCE_SECONDS: dict[DelayScenario, Optional[float]] = {
    DelayScenario.THEORETICAL_IMMEDIATE: None,
    DelayScenario.DELAY_30_SECONDS: 30.0,
    DelayScenario.DELAY_2_MINUTES: 60.0,
    DelayScenario.DELAY_5_MINUTES: 120.0,
    DelayScenario.DELAY_15_MINUTES: 300.0,
    DelayScenario.ACTUAL_MEASURED_DELAY: None,
}


# ---- Component weights (frozen, sum=100) ---------------------------------

SHADOW_WEIGHTS: dict[str, float] = {
    "delayed_entry_alpha": 30.0,
    "tradeable_price_retention": 20.0,
    "execution_feasibility": 15.0,
    "skill_persistence": 15.0,
    "copied_realized_performance": 10.0,
    "concentration_correlation": 10.0,
}

assert abs(sum(SHADOW_WEIGHTS.values()) - 100.0) < 1e-9, (
    "Shadow V2 weights must sum to 100"
)


# ---- Typed contracts ----------------------------------------------------


@dataclass(frozen=True)
class ShadowScoreInputV2:
    """Frozen typed input for V2 shadow scoring.

    Every field is an explicit, named typed attribute — no scattered
    kwargs. ``delay_scenario`` determines whether a delayed price is
    expected at all (THEORETICAL_IMMEDIATE uses the source price
    directly; ACTUAL_MEASURED_DELAY uses the persisted measured
    delay-seconds).
    """

    wallet_id: str
    source_trade_id: str
    candidate_id: Optional[int]
    delay_scenario: DelayScenario

    # Prices
    source_price: Optional[float]
    delayed_copy_price: Optional[float]

    # Execution / depth evidence
    intended_stake: Optional[float]
    executable_depth: Optional[float]
    fill_percentage: Optional[float]
    slippage: Optional[float]
    spread: Optional[float]

    # Wallet / portfolio evidence
    wallet_skill_persistence_input: Optional[float]
    copied_realized_performance_input: Optional[float]
    concentration_correlation_input: Optional[float]

    # Source / snapshot identity
    source_data_timestamp: Optional[str]
    price_snapshot_id: Optional[str]
    depth_hash: Optional[str]

    # Forward-evidence audit
    missing_forward_reasons: Tuple[str, ...] = field(default_factory=tuple)

    # Actual measured delay (only used for ACTUAL_MEASURED_DELAY).
    # When delay_scenario == ACTUAL_MEASURED_DELAY and this is None
    # at compute time, the result is SHADOW_INCOMPLETE.
    measured_delay_seconds: Optional[float] = None

    # Offset audit fields (Repair 2d). For every scenario:
    #   * ``target_delay_seconds`` is the scenario's requested delay
    #     (the constant from DELAY_SCENARIO_SECONDS); NULL for
    #     ACTUAL_MEASURED_DELAY (whose target is its own measured
    #     delay) and for scenarios where the typed contract elects
    #     not to surface a target.
    #   * ``actual_observed_delay_seconds`` is the measured offset
    #     between the source trade timestamp and the persisted
    #     snapshot's ``fetched_at``. NULL when no qualifying snapshot
    #     exists. The runtime enforces ``0 <= x <= 600`` (a 10-min
    #     ceiling matches the longest fixed-delay scenario plus its
    #     tolerance) and surfaces out-of-range values as a missing
    #     reason rather than crashing.
    #   * ``delay_error_seconds`` = ``actual_observed_delay_seconds -
    #     target_delay_seconds`` when both are available; NULL
    #     otherwise.
    target_delay_seconds: Optional[float] = None
    actual_observed_delay_seconds: Optional[float] = None
    delay_error_seconds: Optional[float] = None

    @property
    def delay_scenario_seconds(self) -> Optional[float]:
        """The frozen delay-seconds value for this scenario.

        Returns the constant from ``DELAY_SCENARIO_SECONDS`` for
        fixed scenarios, or ``None`` for ``ACTUAL_MEASURED_DELAY``
        (which derives from ``measured_delay_seconds`` at compute
        time).
        """
        return DELAY_SCENARIO_SECONDS.get(self.delay_scenario)


@dataclass(frozen=True)
class ShadowScoreResultV2:
    """Frozen typed result of V2 shadow scoring.

    Carries the exact typed input that produced it so the decision is
    replayable without any scattered lookups. ``input`` is a
    :class:`ShadowScoreInputV2`.
    """

    wallet_id: str
    source_trade_id: str
    candidate_id: Optional[int]
    delay_scenario: DelayScenario
    score: float
    verdict: str  # one of: SHADOW_COPY_CANDIDATE / SHADOW_WATCHLIST / SHADOW_SKIP / SHADOW_INCOMPLETE
    input: ShadowScoreInputV2
    component_scores: Tuple[dict, ...]  # tuple of {"name", "raw_score", "weight", "weighted_score", "quality", "formula", "note"}
    missing_forward_reasons: Tuple[str, ...]
    computed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    formula_name: str = SHADOW_FORMULA_NAME
    formula_version: str = SHADOW_FORMULA_VERSION
    measured_delay_seconds: Optional[float] = None


# ---- Verdict rules (frozen, separate from V1) ---------------------------

VERDICT_COPY_CANDIDATE_MIN = 70.0
VERDICT_WATCHLIST_MIN = 50.0

# Canonical V2 shadow verdicts — do NOT reuse V1 verdict enum.
VERDICT_SHADOW_COPY_CANDIDATE = "SHADOW_COPY_CANDIDATE"
VERDICT_SHADOW_WATCHLIST = "SHADOW_WATCHLIST"
VERDICT_SHADOW_SKIP = "SHADOW_SKIP"
VERDICT_SHADOW_INCOMPLETE = "SHADOW_INCOMPLETE"


# ---- Missing-evidence sentinel reason tokens ----------------------------

REASON_NO_ALPHA = "missing_alpha_signal"
REASON_NO_DELAYED_PRICE = "missing_delayed_copy_price"
REASON_NO_EXECUTION_EVIDENCE = "missing_execution_evidence"
REASON_NO_COPIED_PERFORMANCE = "missing_copied_realized_performance"
REASON_NO_CONCENTRATION = "missing_concentration_correlation_evidence"
REASON_NO_WALLET_PERSISTENCE = "missing_wallet_skill_persistence_input"
REASON_NO_SOURCE_PRICE = "missing_source_price"
REASON_INSUFFICIENT_DEPTH = "insufficient_executable_depth"
REASON_NO_MEASURED_DELAY = "missing_actual_measured_delay_seconds"