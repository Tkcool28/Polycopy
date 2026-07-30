"""Persistent Specialist Category Wallet Score v1 — frozen formula (Phase 2 / Chunk 3).

Reuses the same frozen component weights as
``polycopy.scoring.wallet_score_v1.WalletScoreResult`` (Persistent
Specialist Wallet Score v1). Reusing the existing component functions
ensures a single source of truth for sub-formulas; this module owns
the category-specific verdict rules, gate enforcement, and typed
input/output contract.

Score composition (weights sum to 100):
- information_and_price_improvement: 30%
- verified_realized_performance: 15%
- chronological_consistency: 15%
- risk_and_drawdown_quality: 10%
- sample_reliability: 10%
- category_specialization: 15%
- concentration_quality: 5%

Verdict rules (frozen):
- 75.0000–100.0000 → COPY_CANDIDATE
- 55.0000–74.9999  → WATCHLIST
- below 55          → SKIP
- Missing essential evidence or missing essential gate value → INCOMPLETE

Category-eligibility gates for COPY_CANDIDATE (frozen):
- 15 resolved category markets
- 8 distinct category events
- 10 category-active days

Behavior:
- Score >= 75 AND every category gate passes → COPY_CANDIDATE
- Score >= 75 but any category gate fails     → WATCHLIST (NOT COPY_CANDIDATE)
- Score 55 .. < 75                             → WATCHLIST
- Score < 55                                   → SKIP
- Any missing essential input or missing gate value → INCOMPLETE
- Any category gate below the minimum          → gate failure recorded,
                                                 blocks COPY_CANDIDATE

The category score never defaults missing gates to zero; a missing
gate value is INCOMPLETE. A numeric placeholder cannot bypass a
failed category gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from polycopy.scoring.helpers import clamp
from polycopy.scoring.specialist_qualification_contract import (  # noqa: F401
    CATEGORY_MIN_ACTIVE_DAYS,
    CATEGORY_MIN_DISTINCT_EVENTS,
    CATEGORY_MIN_RESOLVED_MARKETS,
    CATEGORY_SCORE_THRESHOLD,
    evaluate_category_qualification,
)
from polycopy.scoring.wallet_score_v1 import (
    WalletScoreComponent,
    WalletVerdict,
    _category_specialization_component,
    _chronological_consistency_component,
    _concentration_quality_component,
    _info_price_improvement_component,
    _realized_performance_component,
    _risk_drawdown_component,
    _sample_reliability_component,
)

# ---- Formula identity (frozen) -------------------------------------------

CATEGORY_WALLET_FORMULA_NAME = "category_wallet_score"
CATEGORY_WALLET_FORMULA_VERSION = "1"

# ---- Category eligibility minimums (frozen, re-exported from shared contract
# via the top-level import above — no redundant re-assignment needed)

# ---- Verdict thresholds (frozen) -----------------------------------------
VERDICT_COPY_CANDIDATE_MIN = CATEGORY_SCORE_THRESHOLD  # 75.0
VERDICT_WATCHLIST_MIN = 55.0


# ---- Typed contracts -----------------------------------------------------


@dataclass(frozen=True)
class CategoryWalletScoreInputV1:
    """Typed input for Category Wallet Score v1 (Phase 2 + Phase 9).

    Identity fields:
        - wallet_id          (required)
        - category_label     (required, e.g. "crypto", "politics")

    Raw scoring fields mirror the wallet score inputs where
    category-specific evidence exists. All optional.

    Category eligibility gate values (frozen):
        - category_resolved_markets
        - category_distinct_events
        - category_active_days

    Metadata:
        - source_data_timestamp — point-in-time input identity for
                                  idempotency
        - is_sample             — flag for sample-only evidence
        - computed_at           — set by the score function, not by
                                  callers
        - formula_version       — pinned at module level; carried on
                                  the result for replay
    """

    wallet_id: str
    category_label: str

    # Raw scoring inputs
    info_score: Optional[float] = None
    win_rate: Optional[float] = None
    profit_factor: Optional[float] = None
    trade_intervals_std: Optional[float] = None
    trade_count: Optional[int] = None
    max_drawdown: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    sample_fraction: Optional[float] = None
    category_trade_count: Optional[int] = None
    category_distinct_markets: Optional[int] = None
    overall_trade_count: Optional[int] = None
    largest_winner_share: Optional[float] = None
    top_3_concentration: Optional[float] = None

    # Category gate values
    category_resolved_markets: Optional[int] = None
    category_distinct_events: Optional[int] = None
    category_active_days: Optional[int] = None

    # Metadata
    source_data_timestamp: Optional[str] = None
    is_sample: bool = False


@dataclass
class CategoryWalletScoreResultV1:
    """Result of category wallet score v1.

    Mirrors :class:`WalletScoreResult` for symmetry, plus the
    `category_label` identity and the category-specific
    `category_gate_failures` list.
    """

    wallet_id: str
    category_label: str
    score: float  # Final 0-100 score
    verdict: WalletVerdict
    input: Optional[CategoryWalletScoreInputV1] = None
    components: list[WalletScoreComponent] = field(default_factory=list)
    missing_essentials: list[str] = field(default_factory=list)
    category_gate_failures: list[str] = field(default_factory=list)
    computed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    formula_version: str = CATEGORY_WALLET_FORMULA_VERSION
    is_sample: bool = False
    source_data_timestamp: Optional[str] = None


# ---- Helpers --------------------------------------------------------------


def _category_gates_pass(
    inp: CategoryWalletScoreInputV1,
) -> tuple[bool, list[str]]:
    """Return (all_pass, list_of_failures).

    Delegates to the shared :func:`evaluate_category_qualification`
    so that category gate enforcement is identical across the wallet
    evaluator, category evaluator, and status monitor. Missing gate
    values (None) fail closed — they are NOT silently skipped.

    Only evidence gates are checked here; the score gate is evaluated
    separately in the verdict logic.
    """
    qual = evaluate_category_qualification(
        score=None,  # score checked separately in verdict logic
        category_resolved_markets=inp.category_resolved_markets,
        category_distinct_events=inp.category_distinct_events,
        category_active_days=inp.category_active_days,
    )
    # Filter out score failures — only evidence gates here.
    evidence_failures = [
        f for f in qual.gate_failures if not f.startswith("score")
    ]
    return (len(evidence_failures) == 0, evidence_failures)


# ---- Score function -------------------------------------------------------


def compute_category_wallet_score_v1(
    wallet_id: Optional[str] = None,
    category_label: Optional[str] = None,
    *,
    input: Optional[CategoryWalletScoreInputV1] = None,
    info_score: Optional[float] = None,
    win_rate: Optional[float] = None,
    profit_factor: Optional[float] = None,
    trade_intervals_std: Optional[float] = None,
    trade_count: Optional[int] = None,
    max_drawdown: Optional[float] = None,
    sharpe_ratio: Optional[float] = None,
    sample_fraction: Optional[float] = None,
    category_trade_count: Optional[int] = None,
    category_distinct_markets: Optional[int] = None,
    overall_trade_count: Optional[int] = None,
    largest_winner_share: Optional[float] = None,
    top_3_concentration: Optional[float] = None,
    category_resolved_markets: Optional[int] = None,
    category_distinct_events: Optional[int] = None,
    category_active_days: Optional[int] = None,
    source_data_timestamp: Optional[str] = None,
    now: Optional[datetime] = None,
    is_sample: bool = False,
) -> CategoryWalletScoreResultV1:
    """Compute Persistent Specialist Category Wallet Score v1.

    Identity contract (mirrors wallet score v1):
      * If a typed ``CategoryWalletScoreInputV1`` is passed as
        ``input=...``, ``input.wallet_id`` and
        ``input.category_label`` are the source of truth.
      * If positional values are passed and no ``input=...`` is
        given, the positional values are used.
      * If both are passed, they must match — a conflict raises
        ``ValueError``.
      * If neither is provided, OR if either identity field is
        empty, ``ValueError`` is raised.

    Verdict rules are documented in the module docstring. A high
    numeric score cannot bypass a failed category gate; a missing
    gate value cannot be silently coerced to zero — both produce
    INCOMPLETE.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if input is None:
        if wallet_id is None or wallet_id == "":
            raise ValueError(
                "compute_category_wallet_score_v1 requires a non-empty wallet_id "
                "either positionally or via input.wallet_id"
            )
        if category_label is None or category_label == "":
            raise ValueError(
                "compute_category_wallet_score_v1 requires a non-empty "
                "category_label either positionally or via input.category_label"
            )
        input = CategoryWalletScoreInputV1(
            wallet_id=wallet_id,
            category_label=category_label,
            info_score=info_score,
            win_rate=win_rate,
            profit_factor=profit_factor,
            trade_intervals_std=trade_intervals_std,
            trade_count=trade_count,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe_ratio,
            sample_fraction=sample_fraction,
            category_trade_count=category_trade_count,
            category_distinct_markets=category_distinct_markets,
            overall_trade_count=overall_trade_count,
            largest_winner_share=largest_winner_share,
            top_3_concentration=top_3_concentration,
            category_resolved_markets=category_resolved_markets,
            category_distinct_events=category_distinct_events,
            category_active_days=category_active_days,
            source_data_timestamp=source_data_timestamp,
            is_sample=is_sample,
        )
    else:
        if input.wallet_id is None or input.wallet_id == "":
            raise ValueError(
                "compute_category_wallet_score_v1 requires input.wallet_id to be "
                "non-empty; got empty input.wallet_id"
            )
        if input.category_label is None or input.category_label == "":
            raise ValueError(
                "compute_category_wallet_score_v1 requires input.category_label "
                "to be non-empty; got empty input.category_label"
            )
        if wallet_id is not None and wallet_id != input.wallet_id:
            raise ValueError(
                f"compute_category_wallet_score_v1 wallet_id conflict: "
                f"positional wallet_id={wallet_id!r} but "
                f"input.wallet_id={input.wallet_id!r}"
            )
        if category_label is not None and category_label != input.category_label:
            raise ValueError(
                f"compute_category_wallet_score_v1 category_label conflict: "
                f"positional category_label={category_label!r} but "
                f"input.category_label={input.category_label!r}"
            )
        wallet_id = input.wallet_id
        category_label = input.category_label

    components: list[WalletScoreComponent] = []
    missing_essentials: list[str] = []
    gate_failures: list[str] = []

    # Phase 9 / Phase 2: read all raw inputs from the typed input
    # so callers that pass `input=...` alone are correctly evaluated.
    info_score = input.info_score
    win_rate = input.win_rate
    profit_factor = input.profit_factor
    trade_intervals_std = input.trade_intervals_std
    trade_count = input.trade_count
    max_drawdown = input.max_drawdown
    sharpe_ratio = input.sharpe_ratio
    sample_fraction = input.sample_fraction
    category_trade_count = input.category_trade_count
    category_distinct_markets = input.category_distinct_markets
    overall_trade_count = input.overall_trade_count
    largest_winner_share = input.largest_winner_share
    top_3_concentration = input.top_3_concentration

    # Phase 2 essential evidence. Missing values must be INCOMPLETE,
    # not silently zero. The same essential fields as the wallet
    # score are required; the trade_count and win_rate missing
    # evidence produces INCOMPLETE.
    if input.trade_count is None:
        missing_essentials.append("trade_count")
    if input.win_rate is None:
        missing_essentials.append("win_rate")

    # Category gate enforcement. Any missing gate value is
    # INCOMPLETE; any below-minimum value is recorded as a gate
    # failure and blocks COPY_CANDIDATE.
    _, gate_failures = _category_gates_pass(input)
    missing_gate_values = [g for g in gate_failures if "missing" in g]
    if missing_gate_values:
        # Treat missing gate values as missing essentials so the
        # verdict is INCOMPLETE, not SKIP.
        missing_essentials.extend(missing_gate_values)
        gate_failures = [g for g in gate_failures if "missing" not in g]

    # Incomplete wins over partial compute. Return INCOMPLETE with
    # any partial component breakdown for audit. Include ALL gate
    # failures (including missing values) for complete auditability.
    if missing_essentials:
        full_qual = evaluate_category_qualification(
            score=None,
            category_resolved_markets=input.category_resolved_markets,
            category_distinct_events=input.category_distinct_events,
            category_active_days=input.category_active_days,
        )
        return CategoryWalletScoreResultV1(
            wallet_id=wallet_id,
            category_label=category_label,
            score=0.0,
            verdict=WalletVerdict.INCOMPLETE,
            input=input,
            components=components,
            missing_essentials=missing_essentials,
            category_gate_failures=list(full_qual.gate_failures),
            computed_at=now,
            is_sample=is_sample,
            source_data_timestamp=input.source_data_timestamp,
        )

    # Reuse the wallet score v1 component formulas (single source
    # of truth for sub-formulas). The frozen weights in
    # wallet_score_v1.WEIGHTS sum to 100 and are unchanged.
    raw, quality, note = _info_price_improvement_component(info_score)
    components.append(WalletScoreComponent(
        name="information_and_price_improvement",
        raw_score=raw,
        weight=30.0,
        quality=quality,
        formula="info_score * 100 (clamped)",
        note=note,
    ))

    raw, quality, note = _realized_performance_component(win_rate, profit_factor)
    components.append(WalletScoreComponent(
        name="verified_realized_performance",
        raw_score=raw,
        weight=15.0,
        quality=quality,
        formula="avg(win_rate*100, profit_factor normalized to 1-2)",
        note=note,
    ))

    raw, quality, note = _chronological_consistency_component(
        trade_intervals_std, trade_count
    )
    components.append(WalletScoreComponent(
        name="chronological_consistency",
        raw_score=raw,
        weight=15.0,
        quality=quality,
        formula="inverse_score(trade_intervals_std_hours, 0, 12)",
        note=note,
    ))

    raw, quality, note = _risk_drawdown_component(max_drawdown, sharpe_ratio)
    components.append(WalletScoreComponent(
        name="risk_and_drawdown_quality",
        raw_score=raw,
        weight=10.0,
        quality=quality,
        formula="avg(inverse(drawdown, 0, 0.5), sharpe/3*100)",
        note=note,
    ))

    raw, quality, note = _sample_reliability_component(trade_count, sample_fraction)
    components.append(WalletScoreComponent(
        name="sample_reliability",
        raw_score=raw,
        weight=10.0,
        quality=quality,
        formula="trade_count linear ramps 5→200, penalized by sample_fraction",
        note=note,
    ))

    raw, quality, note = _category_specialization_component(
        category_trade_count, category_distinct_markets, overall_trade_count
    )
    components.append(WalletScoreComponent(
        name="category_specialization",
        raw_score=raw,
        weight=15.0,
        quality=quality,
        formula="linear_score(category_share, 0.1, 0.4) with market bonus",
        note=note,
    ))

    raw, quality, note = _concentration_quality_component(
        largest_winner_share, top_3_concentration
    )
    components.append(WalletScoreComponent(
        name="concentration_quality",
        raw_score=raw,
        weight=5.0,
        quality=quality,
        formula="100 - largest_winner_penalty - top3_penalty",
        note=note,
    ))

    weighted_total = sum(c.weighted_score for c in components)
    final_score = clamp(round(weighted_total, 4))

    # Verdict rules (frozen, using shared qualification contract).
    # The contract enforces: a high numerical score can never override
    # insufficient evidence. Every gate is mandatory. Missing evidence
    # fails closed.
    cat_qual = evaluate_category_qualification(
        score=final_score,
        category_resolved_markets=input.category_resolved_markets,
        category_distinct_events=input.category_distinct_events,
        category_active_days=input.category_active_days,
    )
    # Deterministic failure ordering from the shared contract.
    # The score gate failure is NOT an evidence gate failure — it is
    # a quality verdict (WATCHLIST/SKIP), not an evidence gap. Only
    # evidence gate failures go into category_gate_failures.
    gate_failures = [
        f for f in cat_qual.gate_failures if not f.startswith("score")
    ]

    # Verdict rules (frozen, using shared contract):
    # - Evidence gate failure (missing or below minimum) → INCOMPLETE
    #   (insufficient evidence for a trustworthy decision)
    # - Score < 75 with evidence gates passing → WATCHLIST (>= 55) or SKIP
    # - Score >= 75 with all gates passing → COPY_CANDIDATE
    #
    # The score gate is NOT an evidence gate — a low score is a quality
    # verdict (WATCHLIST/SKIP), not an evidence gap (INCOMPLETE).
    evidence_gates_pass = not any(
        not f.startswith("score") for f in cat_qual.gate_failures
    )
    if not evidence_gates_pass:
        # Evidence gates fail (missing or below minimum) → INCOMPLETE
        verdict = WalletVerdict.INCOMPLETE
    elif final_score >= VERDICT_COPY_CANDIDATE_MIN:
        verdict = WalletVerdict.COPY_CANDIDATE
    elif final_score >= VERDICT_WATCHLIST_MIN:
        # Evidence gates pass but score < 75 → WATCHLIST
        verdict = WalletVerdict.WATCHLIST
    else:
        verdict = WalletVerdict.SKIP

    return CategoryWalletScoreResultV1(
        wallet_id=wallet_id,
        category_label=category_label,
        score=final_score,
        verdict=verdict,
        input=input,
        components=components,
        missing_essentials=missing_essentials,
        category_gate_failures=gate_failures,
        computed_at=now,
        is_sample=is_sample,
        source_data_timestamp=input.source_data_timestamp,
    )
