"""Copyability verdict domain model — deterministic 0-100 scoring and hard verdict rules.

This module defines:
- Verdict enum: COPY_CANDIDATE / WATCHLIST / SKIP / INCOMPLETE
- CopyabilityScore: 0-100 with component breakdown and data-quality labels
- ScoreComponent: individual scoring factor with observed/calculated/inferred/unknown tags
- MissingField: tracks which data fields were missing and how they affected scoring

All values are deterministic given the same inputs — no randomness, no ML.
"""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Verdict(str, enum.Enum):
    """Hard verdict rules for copyability."""

    COPY_CANDIDATE = "copy_candidate"
    WATCHLIST = "watchlist"
    SKIP = "skip"
    INCOMPLETE = "incomplete"


class DataQuality(str, enum.Enum):
    """Classification of each score component's data source.

    OBSERVED: directly measured from live/source data (API, snapshot).
    CALCULATED: derived deterministically from observed data (e.g. win_rate from trades).
    INFERRED: derived from incomplete/heuristic sources (e.g. related-wallet guess).
    UNKNOWN: field missing, capped to floor/penalty — not a real measurement.
    """

    OBSERVED = "observed"
    CALCULATED = "calculated"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class ScoreComponent(BaseModel):
    """A single scoring factor within the 0-100 copyability score.

    weight: contribution to final score (0-100 scale, all weights sum to 100).
    raw_score: the raw 0-100 score for this component before weighting.
    weight: percentage weight of this component in the final score (0-100).
    quality: whether this component's data is observed, calculated, inferred, or unknown.
    formula: human-readable formula string for transparency.
    note: optional explanatory note about data quality or missing info.
    """

    name: str = Field(description="Component name, e.g. 'sharpe_ratio', 'data_recency'.")
    raw_score: float = Field(ge=0.0, le=100.0, description="Raw 0-100 score before weighting.")
    weight: float = Field(ge=0.0, le=100.0, description="Weight as percentage (0-100), sums with others to 100.")
    quality: DataQuality = Field(description="Data quality tag.")
    formula: str = Field(description="Human-readable formula or source description.")
    note: str = Field(default="", description="Optional note about missing data or caveats.")

    @property
    def weighted_score(self) -> float:
        """Score contribution after weighting: raw_score * (weight / 100)."""
        return self.raw_score * (self.weight / 100.0)


class MissingField(BaseModel):
    """Tracks a missing data field and its scoring impact."""

    field_name: str = Field(description="Name of the missing field, e.g. 'trade_count', 'sharpe_ratio'.")
    severity: str = Field(description="'critical', 'major', or 'minor'.")
    penalty_applied: float = Field(description="Points deducted from 100 due to this missing field.")
    quality_assigned: DataQuality = Field(default=DataQuality.UNKNOWN)
    note: str = Field(default="")


class CopyabilityScore(BaseModel):
    """Deterministic 0-100 copyability score with full component breakdown.

    The final score is computed as sum(weighted_score) - sum(penalties),
    clamped to [0, 100]. Each component is tagged with DataQuality so
    downstream consumers can judge reliability.

    Verdict is derived deterministically from score + missing-field count:
        score >= 70 AND no critical missing → COPY_CANDIDATE
        score >= 50 AND no critical missing → WATCHLIST
        score < 50 → SKIP
        any critical missing → INCOMPLETE (signals insufficient data)
    """

    id: UUID = Field(default_factory=uuid4, description="Unique score instance ID.")
    wallet_id: UUID = Field(description="Wallet this score applies to.")
    market_id: UUID | None = Field(default=None, description="Market-specific score, or None for global.")
    score: float = Field(ge=0.0, le=100.0, description="Final deterministic score, clamped [0, 100].")
    verdict: Verdict = Field(description="Hard verdict derived from score + missing fields.")
    components: list[ScoreComponent] = Field(default_factory=list, description="All scoring components.")
    missing_fields: list[MissingField] = Field(default_factory=list, description="Fields that were missing.")
    formula_version: str = Field(default="v1", description="Version string for the scoring formula.")
    computed_at: datetime = Field(description="UTC timestamp of computation.")
    is_sample: bool = Field(default=False, description="True if scored from sample/fixture data.")

    def summary(self) -> str:
        """Human-readable one-line summary."""
        mc = len(self.missing_fields)
        return (
            f"score={self.score:.1f}/100 | verdict={self.verdict.value} | "
            f"components={len(self.components)} | missing={mc}"
        )
