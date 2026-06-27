"""Deterministic copyability scoring engine.

Formula version: v1

Score composition (weights sum to 100):
  - sharpe_ratio (20): risk-adjusted returns
  - win_rate (15): fraction of profitable trades
  - trade_consistency (15): trade frequency / regularity
  - data_recency (15): how recent is the latest data
  - data_completeness (10): how many fields are populated
  - volume_tenure (10): how long the wallet has been active
  - market_correlation (15): does the wallet trade correlated markets

Verdict rules (deterministic, applied after scoring):
  - any critical missing field → INCOMPLETE
  - score >= 70 AND no critical missing → COPY_CANDIDATE
  - score >= 50 AND no critical missing → WATCHLIST
  - score < 50 → SKIP

All components are tagged with DataQuality (observed/calculated/inferred/unknown).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from polycopy.domain.copyability import (
    CopyabilityScore,
    DataQuality,
    MissingField,
    ScoreComponent,
    Verdict,
)

# ── Thresholds and constants ──────────────────────────────────────────────────

# Sharpe ratio is capped at 3.0 for scoring (maps to 100)
MAX_SHARPE = 3.0

# Data recency: trades within 60s are "fresh" (score 100), decay to 0 at 1 hour
RECENCY_FRESH_SECONDS = 60.0
RECENCY_STALE_SECONDS = 3600.0

# Trade consistency: ideal is 5-50 trades in observed window
CONSISTENCY_MIN = 5
CONSISTENCY_MAX = 50

# Volume tenure: wallet active for >30 days scores 100, linear ramp from 0
TENURE_FULL_DAYS = 30

# Weights (must sum to 100)
WEIGHTS = {
    "sharpe_ratio": 20,
    "win_rate": 15,
    "trade_consistency": 15,
    "data_recency": 15,
    "data_completeness": 10,
    "volume_tenure": 10,
    "market_correlation": 15,
}

# Critical fields: if missing, verdict is always INCOMPLETE
CRITICAL_FIELDS = {"trade_count", "win_rate", "sharpe_ratio"}


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Clamp value to [lo, hi]."""
    return max(lo, min(hi, value))


def _sharpe_component(sharpe: Optional[float]) -> tuple[float, DataQuality, str]:
    """Score sharpe ratio: 0 → 0, MAX_SHARPE → 100, linear."""
    if sharpe is None:
        return 0.0, DataQuality.UNKNOWN, "sharpe_ratio=None (missing)"
    quality = DataQuality.CALCULATED
    note = f"sharpe={sharpe:.3f}"
    score = _clamp((sharpe / MAX_SHARPE) * 100.0)
    return score, quality, note


def _win_rate_component(win_rate: Optional[float]) -> tuple[float, DataQuality, str]:
    """Score win_rate: 0% → 0, 100% → 100, linear."""
    if win_rate is None:
        return 0.0, DataQuality.UNKNOWN, "win_rate=None (missing)"
    quality = DataQuality.CALCULATED
    note = f"win_rate={win_rate:.3f}"
    score = _clamp(win_rate * 100.0)
    return score, quality, note


def _trade_consistency_component(trade_count: Optional[int]) -> tuple[float, DataQuality, str]:
    """Score trade frequency: ramp up from CONSISTENCY_MIN to CONSISTENCY_MAX."""
    if trade_count is None:
        return 0.0, DataQuality.UNKNOWN, "trade_count=None (missing)"
    quality = DataQuality.OBSERVED
    if trade_count < CONSISTENCY_MIN:
        # Linear ramp: 0 at 0 trades, 100 at CONSISTENCY_MIN
        score = _clamp((trade_count / CONSISTENCY_MIN) * 100.0)
        note = f"trade_count={trade_count} (below minimum {CONSISTENCY_MIN})"
    elif trade_count <= CONSISTENCY_MAX:
        score = 100.0
        note = f"trade_count={trade_count} (optimal range)"
    else:
        # Diminishing returns past MAX: gentle decay
        score = _clamp(100.0 - (trade_count - CONSISTENCY_MAX) * 0.5)
        note = f"trade_count={trade_count} (above optimal, slight decay)"
    return score, quality, note


def _data_recency_component(
    latest_trade_ts: Optional[datetime],
    now: datetime,
) -> tuple[float, DataQuality, str]:
    """Score recency: 100 at 0s, linear decay to 0 at RECENCY_STALE_SECONDS."""
    if latest_trade_ts is None:
        return 0.0, DataQuality.UNKNOWN, "latest_trade_ts=None (missing)"
    quality = DataQuality.OBSERVED
    age_seconds = (now - latest_trade_ts).total_seconds()
    if age_seconds <= RECENCY_FRESH_SECONDS:
        score = 100.0
        note = f"age={age_seconds:.0f}s (fresh)"
    elif age_seconds >= RECENCY_STALE_SECONDS:
        score = 0.0
        note = f"age={age_seconds:.0f}s (stale)"
    else:
        # Linear decay between fresh and stale
        ratio = 1.0 - ((age_seconds - RECENCY_FRESH_SECONDS) / (RECENCY_STALE_SECONDS - RECENCY_FRESH_SECONDS))
        score = _clamp(ratio * 100.0)
        note = f"age={age_seconds:.0f}s (decaying)"
    return score, quality, note


def _data_completeness_component(
    fields_present: set[str],
    fields_expected: set[str],
) -> tuple[float, DataQuality, str]:
    """Score completeness: fraction of expected fields that are present."""
    if not fields_expected:
        return 100.0, DataQuality.OBSERVED, "no expected fields defined"
    quality = DataQuality.OBSERVED
    present_count = len(fields_present & fields_expected)
    total = len(fields_expected)
    score = _clamp((present_count / total) * 100.0)
    missing = fields_expected - fields_present
    note = f"{present_count}/{total} fields present"
    if missing:
        note += f" (missing: {', '.join(sorted(missing))})"
    return score, quality, note


def _volume_tenure_component(
    first_trade_ts: Optional[datetime],
    now: datetime,
) -> tuple[float, DataQuality, str]:
    """Score wallet tenure: 0 at 0 days, 100 at TENURE_FULL_DAYS."""
    if first_trade_ts is None:
        return 0.0, DataQuality.UNKNOWN, "first_trade_ts=None (missing)"
    quality = DataQuality.OBSERVED
    days_active = (now - first_trade_ts).total_seconds() / 86400.0
    score = _clamp((days_active / TENURE_FULL_DAYS) * 100.0)
    note = f"tenure={days_active:.1f}d (target={TENURE_FULL_DAYS}d)"
    return score, quality, note


def _market_correlation_component(
    markets_traded: Optional[int],
) -> tuple[float, DataQuality, str]:
    """Score market diversification: 1 market → 40, 5+ markets → 100."""
    if markets_traded is None:
        return 0.0, DataQuality.UNKNOWN, "markets_traded=None (missing)"
    quality = DataQuality.OBSERVED
    if markets_traded <= 1:
        score = 40.0 if markets_traded == 1 else 0.0
        note = f"markets={markets_traded} (concentrated)"
    elif markets_traded >= 5:
        score = 100.0
        note = f"markets={markets_traded} (diversified)"
    else:
        # Linear ramp from 40 at 1 to 100 at 5
        score = 40.0 + (markets_traded - 1) * 15.0
        note = f"markets={markets_traded} (moderate)"
    return score, quality, note


def compute_verdict(
    score: float,
    missing_fields: list[MissingField],
) -> Verdict:
    """Apply hard verdict rules deterministically.

    Rule priority:
    1. Any critical missing field → INCOMPLETE
    2. score >= 70 → COPY_CANDIDATE
    3. score >= 50 → WATCHLIST
    4. score < 50 → SKIP
    """
    critical_missing = [m for m in missing_fields if m.severity == "critical"]
    if critical_missing:
        return Verdict.INCOMPLETE
    if score >= 70:
        return Verdict.COPY_CANDIDATE
    if score >= 50:
        return Verdict.WATCHLIST
    return Verdict.SKIP


def score_wallet(
    wallet_id: UUID,
    market_id: Optional[UUID] = None,
    sharpe_ratio: Optional[float] = None,
    win_rate: Optional[float] = None,
    trade_count: Optional[int] = None,
    latest_trade_ts: Optional[datetime] = None,
    first_trade_ts: Optional[datetime] = None,
    markets_traded: Optional[int] = None,
    fields_present: Optional[set[str]] = None,
    fields_expected: Optional[set[str]] = None,
    now: Optional[datetime] = None,
    is_sample: bool = False,
) -> CopyabilityScore:
    """Compute deterministic 0-100 copyability score for a wallet.

    All inputs are optional — missing inputs produce UNKNOWN quality components
    with score 0, and are tracked in missing_fields. The verdict will be INCOMPLETE
    if any critical field is missing.

    Args:
        wallet_id: the wallet being scored.
        market_id: optional market-specific scope.
        sharpe_ratio: risk-adjusted return metric.
        win_rate: fraction of winning trades [0, 1].
        trade_count: total number of observed trades.
        latest_trade_ts: timestamp of most recent trade.
        first_trade_ts: timestamp of first observed trade.
        markets_traded: number of distinct markets traded.
        fields_present: set of field names that have data.
        fields_expected: set of field names expected for full scoring.
        now: current UTC timestamp (defaults to datetime.now(utc)).
        is_sample: True if scoring from sample/fixture data.

    Returns:
        CopyabilityScore with full component breakdown and verdict.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if fields_present is None:
        fields_present = set()
        # Auto-detect which fields were explicitly provided (not None)
        if sharpe_ratio is not None:
            fields_present.add("sharpe_ratio")
        if win_rate is not None:
            fields_present.add("win_rate")
        if trade_count is not None:
            fields_present.add("trade_count")
        if latest_trade_ts is not None:
            fields_present.add("latest_trade_ts")
        if first_trade_ts is not None:
            fields_present.add("first_trade_ts")
        if markets_traded is not None:
            fields_present.add("markets_traded")
    if fields_expected is None:
        fields_expected = {
            "sharpe_ratio", "win_rate", "trade_count",
            "latest_trade_ts", "first_trade_ts", "markets_traded",
        }

    components: list[ScoreComponent] = []
    missing: list[MissingField] = []

    # ── 1. Sharpe ratio (weight 20) ─────────────────────────────────────────
    raw, quality, note = _sharpe_component(sharpe_ratio)
    comp = ScoreComponent(
        name="sharpe_ratio",
        raw_score=raw,
        weight=WEIGHTS["sharpe_ratio"],
        quality=quality,
        formula=f"clamp(sharpe / {MAX_SHARPE} * 100, 0, 100)",
        note=note,
    )
    components.append(comp)
    if sharpe_ratio is None:
        missing.append(MissingField(
            field_name="sharpe_ratio",
            severity="critical",
            penalty_applied=WEIGHTS["sharpe_ratio"],
            quality_assigned=DataQuality.UNKNOWN,
            note="Critical field missing: sharpe_ratio",
        ))

    # ── 2. Win rate (weight 15) ────────────────────────────────────────────
    raw, quality, note = _win_rate_component(win_rate)
    comp = ScoreComponent(
        name="win_rate",
        raw_score=raw,
        weight=WEIGHTS["win_rate"],
        quality=quality,
        formula="clamp(win_rate * 100, 0, 100)",
        note=note,
    )
    components.append(comp)
    if win_rate is None:
        missing.append(MissingField(
            field_name="win_rate",
            severity="critical",
            penalty_applied=WEIGHTS["win_rate"],
            quality_assigned=DataQuality.UNKNOWN,
            note="Critical field missing: win_rate",
        ))

    # ── 3. Trade consistency (weight 15) ───────────────────────────────────
    raw, quality, note = _trade_consistency_component(trade_count)
    comp = ScoreComponent(
        name="trade_consistency",
        raw_score=raw,
        weight=WEIGHTS["trade_consistency"],
        quality=quality,
        formula=f"ramp({CONSISTENCY_MIN}→{CONSISTENCY_MAX}→decay)",
        note=note,
    )
    components.append(comp)
    if trade_count is None:
        missing.append(MissingField(
            field_name="trade_count",
            severity="critical",
            penalty_applied=WEIGHTS["trade_consistency"],
            quality_assigned=DataQuality.UNKNOWN,
            note="Critical field missing: trade_count",
        ))

    # ── 4. Data recency (weight 15) ────────────────────────────────────────
    raw, quality, note = _data_recency_component(latest_trade_ts, now)
    comp = ScoreComponent(
        name="data_recency",
        raw_score=raw,
        weight=WEIGHTS["data_recency"],
        quality=quality,
        formula=f"linear_decay({RECENCY_FRESH_SECONDS}s→{RECENCY_STALE_SECONDS}s)",
        note=note,
    )
    components.append(comp)
    if latest_trade_ts is None:
        missing.append(MissingField(
            field_name="latest_trade_ts",
            severity="major",
            penalty_applied=WEIGHTS["data_recency"] * 0.5,
            quality_assigned=DataQuality.UNKNOWN,
            note="Major field missing: latest_trade_ts",
        ))

    # ── 5. Data completeness (weight 10) ───────────────────────────────────
    raw, quality, note = _data_completeness_component(fields_present, fields_expected)
    comp = ScoreComponent(
        name="data_completeness",
        raw_score=raw,
        weight=WEIGHTS["data_completeness"],
        quality=quality,
        formula="present_count / total_expected * 100",
        note=note,
    )
    components.append(comp)

    # ── 6. Volume tenure (weight 10) ───────────────────────────────────────
    raw, quality, note = _volume_tenure_component(first_trade_ts, now)
    comp = ScoreComponent(
        name="volume_tenure",
        raw_score=raw,
        weight=WEIGHTS["volume_tenure"],
        quality=quality,
        formula=f"clamp(days_active / {TENURE_FULL_DAYS} * 100, 0, 100)",
        note=note,
    )
    components.append(comp)
    if first_trade_ts is None:
        missing.append(MissingField(
            field_name="first_trade_ts",
            severity="major",
            penalty_applied=WEIGHTS["volume_tenure"] * 0.5,
            quality_assigned=DataQuality.UNKNOWN,
            note="Major field missing: first_trade_ts",
        ))

    # ── 7. Market correlation (weight 15) ──────────────────────────────────
    raw, quality, note = _market_correlation_component(markets_traded)
    comp = ScoreComponent(
        name="market_correlation",
        raw_score=raw,
        weight=WEIGHTS["market_correlation"],
        quality=quality,
        formula="ramp(1→5 markets, 40→100)",
        note=note,
    )
    components.append(comp)
    if markets_traded is None:
        missing.append(MissingField(
            field_name="markets_traded",
            severity="minor",
            penalty_applied=WEIGHTS["market_correlation"] * 0.3,
            quality_assigned=DataQuality.UNKNOWN,
            note="Minor field missing: markets_traded",
        ))

    # ── Final score ────────────────────────────────────────────────────────
    weighted_total = sum(c.weighted_score for c in components)
    penalty_total = sum(m.penalty_applied for m in missing)
    final_score = _clamp(weighted_total - penalty_total)

    verdict = compute_verdict(final_score, missing)

    return CopyabilityScore(
        wallet_id=wallet_id,
        market_id=market_id,
        score=round(final_score, 2),
        verdict=verdict,
        components=components,
        missing_fields=missing,
        formula_version="v1",
        computed_at=now,
        is_sample=is_sample,
    )
