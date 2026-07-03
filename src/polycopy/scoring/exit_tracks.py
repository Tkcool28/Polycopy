"""Canonical exit-experiment research tracks (Chunk 5 — Phase 11).

Frozen identifier set for the seven exit research tracks registered
for every qualifying paper signal. These identifiers are what is
written to ``exit_experiment_registrations.experiment_type``.

Canonical identifiers (frozen, exactly seven):

    HOLD_TO_RESOLUTION
    EXIT_24H
    EXIT_72H
    FAVORABLE_MOVE_005
    FAVORABLE_MOVE_010
    FAVORABLE_MOVE_015
    THESIS_OR_LIQUIDITY_FAILURE

Scheduling:

    HOLD_TO_RESOLUTION              scheduled_at = NULL
    EXIT_24H                        signal evaluation timestamp + 24h
    EXIT_72H                        signal evaluation timestamp + 72h
    FAVORABLE_MOVE_005              scheduled_at = NULL
    FAVORABLE_MOVE_010              scheduled_at = NULL
    FAVORABLE_MOVE_015              scheduled_at = NULL
    THESIS_OR_LIQUIDITY_FAILURE     scheduled_at = NULL

The +24h and +72h scheduling MUST derive from the immutable signal
evaluation timestamp — never from wall-clock now() at registration
time.

Aliases / historical identifiers are explicitly listed under
``LEGACY_ALIASES`` so safety searches can grep for them.
"""

from __future__ import annotations

import enum
from datetime import datetime, timedelta, timezone
from typing import Optional


# ---- Canonical exit-research track identifiers (frozen) ------------------

class ExitTrack(str, enum.Enum):
    """Canonical exit-experiment research track identifiers."""

    HOLD_TO_RESOLUTION = "HOLD_TO_RESOLUTION"
    EXIT_24H = "EXIT_24H"
    EXIT_72H = "EXIT_72H"
    FAVORABLE_MOVE_005 = "FAVORABLE_MOVE_005"
    FAVORABLE_MOVE_010 = "FAVORABLE_MOVE_010"
    FAVORABLE_MOVE_015 = "FAVORABLE_MOVE_015"
    THESIS_OR_LIQUIDITY_FAILURE = "THESIS_OR_LIQUIDITY_FAILURE"


# Exactly seven tracks. A test asserts this invariant.
CANONICAL_EXIT_TRACKS: tuple[ExitTrack, ...] = (
    ExitTrack.HOLD_TO_RESOLUTION,
    ExitTrack.EXIT_24H,
    ExitTrack.EXIT_72H,
    ExitTrack.FAVORABLE_MOVE_005,
    ExitTrack.FAVORABLE_MOVE_010,
    ExitTrack.FAVORABLE_MOVE_015,
    ExitTrack.THESIS_OR_LIQUIDITY_FAILURE,
)


# Tracks whose scheduled_at is derived from the signal evaluation
# timestamp (the others are observation-time tracks and have NULL
# scheduled_at).
TIME_OFFSET_TRACKS: dict[ExitTrack, timedelta] = {
    ExitTrack.EXIT_24H: timedelta(hours=24),
    ExitTrack.EXIT_72H: timedelta(hours=72),
}


# ---- Legacy aliases (explicit, NOT canonical) ----------------------------
#
# These were used by the pre-PR-4 / pre-Phase-11 schema. They are
# kept here as a single, audited registry so the safety search
# (`grep` for any of them) finds a known, explained match. Production
# code MUST NOT use these identifiers when registering new exits.

LEGACY_ALIASES: tuple[str, ...] = (
    "hold_to_resolution",
    "exit_24h",
    "exit_72h",
    "favorable_move_5pct",
    "favorable_move_10_pct",
    "favorable_move_15_pct",
    "thesis_failure",
    "liquidity_failure",
    "hold",
    "exit_1d",
    "exit_3d",
    "favorable_move_5pct_legacy",
)


def compute_scheduled_at(
    track: ExitTrack,
    *,
    signal_evaluation_timestamp: datetime,
) -> Optional[datetime]:
    """Return the scheduled_at for ``track`` derived from the
    immutable signal evaluation timestamp.

    Tracks in ``TIME_OFFSET_TRACKS`` get their ``signal_evaluation_timestamp``
    + the offset (second=0, microsecond=0, UTC). Other tracks return
    ``None`` (observation-time).

    Raises :class:`ValueError` when ``track`` is not a canonical
    :class:`ExitTrack`.
    """
    if not isinstance(track, ExitTrack):
        raise ValueError(
            f"compute_scheduled_at requires an ExitTrack enum value, "
            f"got {track!r}"
        )
    offset = TIME_OFFSET_TRACKS.get(track)
    if offset is None:
        return None
    if signal_evaluation_timestamp.tzinfo is None:
        # Treat naive timestamps as UTC by convention; this matches
        # the paper-signal runtime contract.
        signal_evaluation_timestamp = signal_evaluation_timestamp.replace(
            tzinfo=timezone.utc
        )
    return (
        signal_evaluation_timestamp + offset
    ).replace(second=0, microsecond=0)


# ---- Migration helper ----------------------------------------------------


def canonical_for_legacy_alias(alias: str) -> ExitTrack:
    """Map a legacy ``experiment_type`` string to its canonical
    :class:`ExitTrack`.

    Raises :class:`ValueError` when ``alias`` does not match any
    known legacy identifier. Callers should treat unknown strings
    as data corruption.
    """
    mapping: dict[str, ExitTrack] = {
        "hold_to_resolution": ExitTrack.HOLD_TO_RESOLUTION,
        "exit_24h": ExitTrack.EXIT_24H,
        "exit_72h": ExitTrack.EXIT_72H,
        "favorable_move_5pct": ExitTrack.FAVORABLE_MOVE_005,
        "favorable_move_10_pct": ExitTrack.FAVORABLE_MOVE_010,
        "favorable_move_15_pct": ExitTrack.FAVORABLE_MOVE_015,
        "thesis_failure": ExitTrack.THESIS_OR_LIQUIDITY_FAILURE,
        "liquidity_failure": ExitTrack.THESIS_OR_LIQUIDITY_FAILURE,
    }
    try:
        return mapping[alias]
    except KeyError as exc:
        raise ValueError(
            f"Unknown legacy exit-experiment alias: {alias!r}"
        ) from exc