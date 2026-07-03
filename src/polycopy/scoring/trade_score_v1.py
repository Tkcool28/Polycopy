"""Trade Copyability Score v1 — frozen formula.

Score composition (weights sum to 100):
- copy_price_quality: 30%
- fill_feasibility: 25%
- liquidity_and_spread_quality: 15%
- trade_freshness: 10%
- holding_period_quality: 10%
- market_and_resolution_quality: 5%
- strategy_and_data_quality: 5%

Verdict rules:
- 70.0000–100.0000 → COPY CANDIDATE
- 50.0000–69.9999 → WATCHLIST
- below 50 → SKIP
- Missing essential evidence → INCOMPLETE

Duration rules:
- Under 15 minutes: excluded
- 15 minutes to under 6 hours: experimental only (score 75 minimum)
- 6 hours to under 1 day: allowed, score 75
- 1–14 days: preferred, score 100
- 15–21 days: allowed, score 80
- 22–45 days: penalized, score 40
- Over 45 days: excluded
- Unknown: INCOMPLETE
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from polycopy.scoring.helpers import clamp, inverse_score
from polycopy.scoring.depth_normalization import (
    DepthWalkResult,
    DEPTH_INSUFFICIENT_FOR_STAKE,
    DEPTH_LEVELS_MALFORMED,
    DEPTH_NOT_CAPTURED,
    DEPTH_SNAPSHOT_MISMATCH,
)


class TradeVerdict(str, enum.Enum):
    """Verdict for trade copyability evaluation."""

    COPY_CANDIDATE = "copy_candidate"
    WATCHLIST = "watchlist"
    SKIP = "skip"
    INCOMPLETE = "incomplete"


@dataclass
class TradeScoreComponent:
    """Component score for trade copyability."""

    name: str
    raw_score: float
    weight: float
    quality: str
    formula: str
    note: str = ""

    @property
    def weighted_score(self) -> float:
        return self.raw_score * (self.weight / 100.0)


@dataclass
class TradeScoreResult:
    """Result of trade v1 copyability scoring.

    The `input` field is the typed `TradeCopyabilityInputV1` instance
    that produced this result. Persisters must read raw columns from
    `result.input.<field>`, not from `getattr(result, ..., None)`.
    """

    wallet_id: str
    source_trade_id: str
    score: float
    verdict: TradeVerdict
    input: Optional["TradeCopyabilityInputV1"] = None
    components: list[TradeScoreComponent] = field(default_factory=list)
    missing_essentials: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    formula_version: str = "1"
    is_sample: bool = False


@dataclass(frozen=True)
class TradeCopyabilityInputV1:
    """Typed input for Trade Copyability Score v1 (Phase 9 + Phase 7).

    Every raw input used by the score is a named, typed field with a
    deterministic default. Frozen so callers cannot mutate the input
    after the score has been computed, which guarantees replayability.

    `side` is explicitly Optional (not "BUY") — unknown or missing
    sides must produce INCOMPLETE (Phase 4.D).

    `depth_walk_result`, when supplied, is the SOLE source of truth
    for `fill_percentage`, `executable_depth`, and slippage. The
    raw `fill_percentage` / `executable_depth` / `best_bid_size` /
    `best_ask_size` fields are ignored when `depth_walk_result` is
    set. The depth-walk result also carries `insufficient_reason`
    and the deterministic `depth_hash` for idempotency.

    `depth_status_reason` is the rejection status when depth was
    not captured, malformed, or mismatched. One of
    DEPTH_NOT_CAPTURED, DEPTH_LEVELS_MALFORMED, DEPTH_SNAPSHOT_MISMATCH.
    When set, the verdict must be INCOMPLETE.

    `price_snapshot_id` is the FK reference to the persisted
    snapshot that produced this depth evidence (replayability).
    """

    wallet_id: str
    source_trade_id: str
    side: Optional[str] = None
    price_deterioration_pct: Optional[float] = None
    intended_stake: Optional[float] = None
    executable_depth: Optional[float] = None
    fill_percentage: Optional[float] = None
    spread: Optional[float] = None
    best_bid_size: Optional[float] = None
    best_ask_size: Optional[float] = None
    trade_age_seconds: Optional[float] = None
    seconds_to_market_end: Optional[float] = None
    market_active: Optional[bool] = None
    market_closed: Optional[bool] = None
    market_resolved: Optional[bool] = None
    has_valid_strategy: Optional[bool] = None
    has_complete_data: Optional[bool] = None
    market_category: Optional[str] = None
    # Phase 7: typed depth-walk evidence
    depth_walk_result: Optional["DepthWalkResult"] = None
    depth_status_reason: Optional[str] = None
    price_snapshot_id: Optional[str] = None
    depth_hash: Optional[str] = None


# Frozen weights (must sum to 100)
WEIGHTS = {
    "copy_price_quality": 30.0,
    "fill_feasibility": 25.0,
    "liquidity_and_spread_quality": 15.0,
    "trade_freshness": 10.0,
    "holding_period_quality": 10.0,
    "market_and_resolution_quality": 5.0,
    "strategy_and_data_quality": 5.0,
}

# Verdict thresholds
VERDICT_COPY_CANDIDATE_MIN = 70.0
VERDICT_WATCHLIST_MIN = 50.0


# Duration buckets and their scores
DURATION_EXCLUDED_SHORT = 15 * 60  # 15 minutes in seconds
DURATION_EXPERIMENTAL_MIN = 15 * 60
DURATION_PREFERRED_MIN = 6 * 3600  # 6 hours
DURATION_MAX = 24 * 3600  # 1 day (preferred threshold)
DURATION_PENALIZED_MIN = 15 * 24 * 3600  # 15 days
DURATION_PENALIZED_MAX = 45 * 24 * 3600  # 45 days


def _copy_price_quality_component(
    price_deterioration_pct: Optional[float],
    side: str,
) -> tuple[float, str, str]:
    """Score: deterioration affects score.

    BUY: deterioration positive when copy price exceeds source price.
    SELL: deterioration positive when copy price is below source price.

    Score: 0% deter = 100, 50%+ deter = 0.
    """
    if price_deterioration_pct is None:
        return 0.0, "unknown", "price_deterioration_pct missing"

    # For positive deterioration (worse), lower score
    # 0% deterioration → 100, 50% deterioration → 0
    score = clamp(inverse_score(price_deterioration_pct, 0.0, 0.5))
    return score, "observed", f"deterioration_pct={price_deterioration_pct:.2%}"


def _fill_feasibility_component(
    intended_stake: Optional[float],
    executable_depth: Optional[float],
    fill_percentage: Optional[float],
) -> tuple[float, str, str]:
    """Score: can we fill the intended stake? (Phase 4.A)

    Spec rule:
        fill_ratio = executable_depth / intended_stake

    A ratio of 1.0 (or more) means depth fully covers the stake and the
    component scores 100. A ratio of 0.25 means only 25% of the stake
    is fillable, so the component scores 25. The score is clamped to
    [0, 100] and never extrapolates beyond stored levels.

    An explicit `fill_percentage` from a depth-walk result (introduced
    in Phase 7) takes precedence over the simple ratio.
    """
    if intended_stake is None or executable_depth is None:
        return 0.0, "unknown", "intended_stake or executable_depth missing"

    if fill_percentage is not None:
        # Caller-supplied explicit fill pct (e.g. from a depth-walk
        # result) takes precedence.
        return clamp(fill_percentage * 100.0), "observed", \
            f"explicit_fill_pct={fill_percentage:.4f}"

    if intended_stake <= 0:
        # Degenerate "no position" state. Cannot divide by zero; treat
        # as 0 fill rather than raising.
        return 0.0, "observed", f"intended_stake<=0 intended={intended_stake}"

    if executable_depth <= 0:
        return 0.0, "observed", f"executable_depth<=0 depth={executable_depth}"

    # Spec: fill_ratio = executable_depth / intended_stake
    # Clamp to [0, 1] (do not extrapolate beyond 100%) and scale to 0-100.
    fill_ratio = min(1.0, executable_depth / intended_stake)
    return clamp(fill_ratio * 100.0), "observed", \
        f"depth={executable_depth} stake={intended_stake} fill_pct={fill_ratio:.4f}"


def _liquidity_spread_component(
    spread: Optional[float],
    best_bid_size: Optional[float],
    best_ask_size: Optional[float],
    intended_stake: Optional[float],
) -> tuple[float, str, str]:
    """Score: tighter spread and more depth = better.

    Spread: 0% → 100, 20%+ → 0.
    Liquidity: stake < 10% of depth → 100, stake > 100% → 0.
    """
    if spread is None:
        return 0.0, "unknown", "spread missing"

    # Spread score: 0% → 100, 20% → 0
    spread_score = clamp(inverse_score(spread, 0.0, 0.20))

    # Liquidity score
    liquidity_score = 100.0
    if intended_stake is not None and best_bid_size is not None and best_ask_size is not None:
        total_depth = best_bid_size + best_ask_size
        if total_depth > 0:
            stake_ratio = intended_stake / total_depth
            liquidity_score = clamp(inverse_score(stake_ratio, 0.1, 1.0))

    return (spread_score + liquidity_score) / 2.0, "observed", f"spread={spread:.3f}"


def _trade_freshness_component(
    trade_age_seconds: Optional[float],
) -> tuple[float, str, str]:
    """Score: fresher trades are better.

    0s old → 100, 3600s old → 0.
    """
    if trade_age_seconds is None:
        return 0.0, "unknown", "trade_age_seconds missing"

    score = clamp(inverse_score(trade_age_seconds, 0.0, 3600.0))
    return score, "observed", f"age_seconds={trade_age_seconds:.0f}"


def _holding_period_component(
    seconds_to_market_end: Optional[float],
) -> tuple[float, str, str]:
    """Frozen holding-period bucket scoring (Phase 4.B).

    Authoritative values (from the PR 4 spec, NOT pre-existing code/tests):

        < 15 minutes                              → excluded (0)
        15 minutes <= t < 6 hours                → experimental (40)
        6 hours   <= t < 1 day                   → allowed (75)
        1 day     <= t <= 14 days                → preferred (100)
        14 days   <  t <= 21 days                → allowed (80)
        21 days   <  t <= 45 days                → penalized (40)
        t > 45 days                              → excluded (0)
        t is None or negative                    → unknown (0, INCOMPLETE upstream)

    Boundary semantics (exact seconds):

        14m59s          → excluded (0)
        15m00s          → 40
        5h59m59s        → 40
        6h00m00s        → 75
        23h59m59s       → 75
        1d00h00m00s     → 100
        14d00h00m00s    → 100
        (14d, 21d]      → 80
        (21d, 45d]      → 40
        > 45d           → excluded (0)
    """
    if seconds_to_market_end is None or seconds_to_market_end < 0:
        return 0.0, "unknown", "invalid or missing seconds_to_market_end"

    age_days = seconds_to_market_end / (24 * 3600)

    if seconds_to_market_end < DURATION_EXCLUDED_SHORT:
        # < 15 minutes: excluded
        return 0.0, "observed", \
            f"duration_excluded_short={age_days:.4f}d (< 15min)"

    if seconds_to_market_end < DURATION_PREFERRED_MIN:
        # 15 minutes to under 6 hours: experimental (frozen score 40)
        return 40.0, "observed", \
            f"duration_experimental={age_days:.4f}d (15min-6h)"

    if seconds_to_market_end < DURATION_MAX:
        # 6 hours to under 1 day: allowed (frozen score 75)
        return 75.0, "observed", \
            f"duration_short_preferred={age_days:.4f}d (6h-1d)"

    if seconds_to_market_end <= 14 * 24 * 3600:
        # 1 day to 14 days: preferred (frozen score 100)
        return 100.0, "observed", \
            f"duration_preferred={age_days:.4f}d (1d-14d)"

    if seconds_to_market_end <= 21 * 24 * 3600:
        # > 14 days and up to 21 days: allowed (frozen score 80)
        return 80.0, "observed", \
            f"duration_long_allowed={age_days:.4f}d (>14d-21d)"

    if seconds_to_market_end <= DURATION_PENALIZED_MAX:
        # > 21 days and up to 45 days: penalized (frozen score 40)
        return 40.0, "observed", \
            f"duration_penalized={age_days:.4f}d (>21d-45d)"

    # > 45 days: excluded
    return 0.0, "observed", \
        f"duration_excluded_long={age_days:.4f}d (>45d)"


def _market_resolution_component(
    market_active: Optional[bool],
    market_closed: Optional[bool],
    market_resolved: Optional[bool],
) -> tuple[float, str, str]:
    """Score for market state.

    Closed or resolved market → 0.
    Inactive → 0.
    Active → 100.
    """
    if market_active is None:
        return 0.0, "unknown", "market_active missing"

    if market_closed or market_resolved:
        return 0.0, "observed", "market_closed_or_resolved"

    if not market_active:
        return 0.0, "observed", "market_inactive"

    return 100.0, "observed", "market_active"


def _strategy_data_component(
    has_valid_strategy: Optional[bool],
    has_complete_data: Optional[bool],
) -> tuple[float, str, str]:
    """Score for strategy and data quality.

    Both true → 100.
    Either missing → 0-100.
    """
    if has_valid_strategy is None and has_complete_data is None:
        return 0.0, "unknown", "strategy and data flags missing"

    score = 100.0
    if has_valid_strategy is None or has_valid_strategy is False:
        score -= 50.0
    if has_complete_data is None or has_complete_data is False:
        score -= 50.0

    return clamp(score), "observed", f"strategy={has_valid_strategy} data={has_complete_data}"


def compute_trade_score_v1(
    wallet_id: Optional[str] = None,
    source_trade_id: Optional[str] = None,
    *,
    input: Optional[TradeCopyabilityInputV1] = None,
    # Copy price quality
    price_deterioration_pct: Optional[float] = None,
    side: Optional[str] = None,

    # Fill feasibility
    intended_stake: Optional[float] = None,
    executable_depth: Optional[float] = None,
    fill_percentage: Optional[float] = None,

    # Liquidity and spread
    spread: Optional[float] = None,
    best_bid_size: Optional[float] = None,
    best_ask_size: Optional[float] = None,

    # Freshness
    trade_age_seconds: Optional[float] = None,

    # Holding period
    seconds_to_market_end: Optional[float] = None,

    # Market state
    market_active: Optional[bool] = None,
    market_closed: Optional[bool] = None,
    market_resolved: Optional[bool] = None,

    # Strategy and data
    has_valid_strategy: Optional[bool] = None,
    has_complete_data: Optional[bool] = None,

    # Market category (for short-crypto hard exclusion — Phase 4.E)
    market_category: Optional[str] = None,

    # Metadata
    now: Optional[datetime] = None,
    is_sample: bool = False,
) -> TradeScoreResult:
    """Compute Trade Copyability Score v1.

    Trade-identity contract (Phase 9 / Chunk 1):

      * If a typed `TradeCopyabilityInputV1` is passed as `input=...`,
        `input.wallet_id` and `input.source_trade_id` are the
        source of truth.
      * If positional `wallet_id` / `source_trade_id` are passed
        and no `input=...` is given, the positional values are used.
      * If both are passed, they must match — a conflict raises
        `ValueError`.
      * If neither is provided, OR if `input.wallet_id` /
        `input.source_trade_id` is the empty string, `ValueError`
        is raised. A result with empty IDs is never silently
        produced.

    All raw inputs remain optional. Missing essential evidence
    produces INCOMPLETE.

    `side` must be "BUY" or "SELL" — anything else (including the
    pre-Phase-4.D default of "BUY") now produces INCOMPLETE so the
    caller is forced to be explicit. There is no silent fallback.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if input is None:
        if wallet_id is None or wallet_id == "":
            raise ValueError(
                "compute_trade_score_v1 requires a non-empty wallet_id "
                "either positionally or via input.wallet_id"
            )
        if source_trade_id is None or source_trade_id == "":
            raise ValueError(
                "compute_trade_score_v1 requires a non-empty source_trade_id "
                "either positionally or via input.source_trade_id"
            )
        input = TradeCopyabilityInputV1(
            wallet_id=wallet_id,
            source_trade_id=source_trade_id,
            side=side,
            price_deterioration_pct=price_deterioration_pct,
            intended_stake=intended_stake,
            executable_depth=executable_depth,
            fill_percentage=fill_percentage,
            spread=spread,
            best_bid_size=best_bid_size,
            best_ask_size=best_ask_size,
            trade_age_seconds=trade_age_seconds,
            seconds_to_market_end=seconds_to_market_end,
            market_active=market_active,
            market_closed=market_closed,
            market_resolved=market_resolved,
            has_valid_strategy=has_valid_strategy,
            has_complete_data=has_complete_data,
            market_category=market_category,
        )
    else:
        # Explicit `input` is the source of truth. The wallet_id
        # and source_trade_id on it must be non-empty, and any
        # positional duplicates must match.
        if input.wallet_id is None or input.wallet_id == "":
            raise ValueError(
                "compute_trade_score_v1 requires input.wallet_id to be "
                "non-empty; got empty input.wallet_id"
            )
        if input.source_trade_id is None or input.source_trade_id == "":
            raise ValueError(
                "compute_trade_score_v1 requires input.source_trade_id to be "
                "non-empty; got empty input.source_trade_id"
            )
        if wallet_id is not None and wallet_id != input.wallet_id:
            raise ValueError(
                f"compute_trade_score_v1 wallet_id conflict: "
                f"positional wallet_id={wallet_id!r} but "
                f"input.wallet_id={input.wallet_id!r}"
            )
        if source_trade_id is not None and source_trade_id != input.source_trade_id:
            raise ValueError(
                f"compute_trade_score_v1 source_trade_id conflict: "
                f"positional source_trade_id={source_trade_id!r} but "
                f"input.source_trade_id={input.source_trade_id!r}"
            )
        wallet_id = input.wallet_id
        source_trade_id = input.source_trade_id

    # Phase 4.D: side must be explicit. No silent BUY fallback.
    if input.side is None or input.side not in ("BUY", "SELL"):
        return TradeScoreResult(
            wallet_id=wallet_id,
            source_trade_id=source_trade_id,
            input=input,
            score=0.0,
            verdict=TradeVerdict.INCOMPLETE,
            missing_essentials=["side"],
            computed_at=now,
            is_sample=is_sample,
        )

    # Phase 7: typed depth-walk integration.
    # The depth_walk_result (when supplied) is the SOLE source of truth
    # for fill_percentage, executable_depth, and slippage. Raw
    # best_bid_size / best_ask_size / fill_percentage / executable_depth
    # fields are IGNORED when depth_walk_result is present.
    #
    # depth_status_reason is the rejection status when depth evidence
    # was not available or malformed. INCOMPLETE propagation:
    #   - DEPTH_NOT_CAPTURED       → no depth captured at all
    #   - DEPTH_LEVELS_MALFORMED   → stored depth cannot be parsed
    #   - DEPTH_SNAPSHOT_MISMATCH  → stored depth disagrees with the
    #                                normalized bounded book for this
    #                                snapshot
    if input.depth_status_reason == DEPTH_NOT_CAPTURED:
        return TradeScoreResult(
            wallet_id=wallet_id,
            source_trade_id=source_trade_id,
            input=input,
            score=0.0,
            verdict=TradeVerdict.INCOMPLETE,
            missing_essentials=["depth_not_captured"],
            rejection_reasons=[DEPTH_NOT_CAPTURED],
            computed_at=now,
            is_sample=is_sample,
        )
    if input.depth_status_reason == DEPTH_LEVELS_MALFORMED:
        return TradeScoreResult(
            wallet_id=wallet_id,
            source_trade_id=source_trade_id,
            input=input,
            score=0.0,
            verdict=TradeVerdict.INCOMPLETE,
            missing_essentials=["depth_levels_malformed"],
            rejection_reasons=[DEPTH_LEVELS_MALFORMED],
            computed_at=now,
            is_sample=is_sample,
        )
    if input.depth_status_reason == DEPTH_SNAPSHOT_MISMATCH:
        return TradeScoreResult(
            wallet_id=wallet_id,
            source_trade_id=source_trade_id,
            input=input,
            score=0.0,
            verdict=TradeVerdict.INCOMPLETE,
            missing_essentials=["depth_snapshot_mismatch"],
            rejection_reasons=[DEPTH_SNAPSHOT_MISMATCH],
            computed_at=now,
            is_sample=is_sample,
        )

    # If depth_walk_result is set, override the raw fill/exec-depth
    # fields. The raw `fill_percentage` and `executable_depth` fields
    # are NOT used — the typed result is authoritative. This prevents
    # raw fields from silently overriding a typed depth walk.
    effective_fill_percentage: Optional[float] = None
    effective_executable_depth: Optional[float] = None
    effective_slippage: Optional[float] = None
    if input.depth_walk_result is not None:
        dw = input.depth_walk_result
        # fill_percentage on the typed result is a Decimal ratio
        # in [0, 1]. The fill_feasibility formula multiplies by 100
        # to bring it onto the 0-100 component scale.
        effective_fill_percentage = float(dw.fill_percentage)
        # executable_depth = filled_notional (in USDC). This is
        # what actually got filled, not the original full depth.
        effective_executable_depth = float(dw.filled_notional)
        # Slippage (Decimal fraction) may be None if best price was
        # zero — propagate None through.
        if dw.slippage is not None:
            effective_slippage = float(dw.slippage)
    else:
        # Fall back to raw input fields when no typed depth result
        # is supplied. This preserves the pre-Phase-7 behavior.
        effective_fill_percentage = input.fill_percentage
        effective_executable_depth = input.executable_depth
        # raw spread is the slippage proxy when no typed depth walk
        effective_slippage = None  # no typed slippage available

    # Phase 4.E: short-crypto hard exclusion (frozen formula).
    # A trade on a crypto-category market whose holding period is
    # under 6 hours is excluded outright (SKIP, score 0).
    if (input.market_category is not None
            and str(input.market_category).strip().lower() == "crypto"
            and input.seconds_to_market_end is not None
            and input.seconds_to_market_end < 6 * 3600):
        return TradeScoreResult(
            wallet_id=wallet_id,
            source_trade_id=source_trade_id,
            input=input,
            score=0.0,
            verdict=TradeVerdict.SKIP,
            rejection_reasons=["short_crypto_exclusion"],
            computed_at=now,
            is_sample=is_sample,
        )

    components: list[TradeScoreComponent] = []
    missing_essentials: list[str] = []
    rejection_reasons: list[str] = []

    # Check essential evidence (Phase 4.C + Phase 7). Holding period and
    # market_active are essential — without them the score silently
    # degrades with "unknown" quality on key components, which hides
    # data gaps from the operator.
    #
    # Phase 7: executable_depth and fill_percentage are checked against
    # the EFFECTIVE values (depth_walk_result overrides raw fields).
    # If intended_stake is missing or the depth walk says the stake
    # could not be filled at all, the verdict is INCOMPLETE.
    if input.intended_stake is None:
        missing_essentials.append("intended_stake")
    if effective_executable_depth is None:
        missing_essentials.append("executable_depth")
    if input.spread is None:
        missing_essentials.append("spread")
    if input.trade_age_seconds is None:
        missing_essentials.append("trade_age_seconds")
    # Phase 4.B/C: seconds_to_market_end is essential; None OR negative
    # is "unknown" per the spec and must produce INCOMPLETE.
    if input.seconds_to_market_end is None or input.seconds_to_market_end < 0:
        missing_essentials.append("seconds_to_market_end")
    if input.market_active is None:
        missing_essentials.append("market_active")

    # Phase 7: a partial fill is preserved as partial. If the depth
    # walk says the stake is not fully covered, we still proceed
    # with the partial fill (so the fill_feasibility component can
    # score the partial truthfully) but we record
    # DEPTH_INSUFFICIENT_FOR_STAKE so downstream code can see it.
    # Partial fill does NOT force INCOMPLETE on its own — only
    # missing essential evidence does.
    if (
        input.depth_walk_result is not None
        and not input.depth_walk_result.is_complete
    ):
        rejection_reasons.append(DEPTH_INSUFFICIENT_FOR_STAKE)

    if missing_essentials:
        return TradeScoreResult(
            wallet_id=wallet_id,
            source_trade_id=source_trade_id,
            input=input,
            score=0.0,
            verdict=TradeVerdict.INCOMPLETE,
            components=components,
            missing_essentials=missing_essentials,
            computed_at=now,
            is_sample=is_sample,
        )

    # Compute components (read from input so persisters see consistent values)
    raw, quality, note = _copy_price_quality_component(
        input.price_deterioration_pct, input.side
    )
    components.append(TradeScoreComponent(
        name="copy_price_quality",
        raw_score=raw,
        weight=WEIGHTS["copy_price_quality"],
        quality=quality,
        formula="inverse(deterioration_pct, 0, 0.5) * 100",
        note=note,
    ))

    raw, quality, note = _fill_feasibility_component(
        input.intended_stake, effective_executable_depth, effective_fill_percentage
    )
    components.append(TradeScoreComponent(
        name="fill_feasibility",
        raw_score=raw,
        weight=WEIGHTS["fill_feasibility"],
        quality=quality,
        formula="fill_ratio = executable_depth / intended_stake (clamped 0-1)",
        note=note,
    ))

    raw, quality, note = _liquidity_spread_component(
        input.spread, input.best_bid_size, input.best_ask_size, input.intended_stake
    )
    components.append(TradeScoreComponent(
        name="liquidity_and_spread_quality",
        raw_score=raw,
        weight=WEIGHTS["liquidity_and_spread_quality"],
        quality=quality,
        formula="avg(inverse(spread, 0, 0.2), inverse(stake_ratio, 0.1, 1.0))",
        note=note,
    ))

    raw, quality, note = _trade_freshness_component(input.trade_age_seconds)
    components.append(TradeScoreComponent(
        name="trade_freshness",
        raw_score=raw,
        weight=WEIGHTS["trade_freshness"],
        quality=quality,
        formula="inverse(trade_age_seconds, 0, 3600)",
        note=note,
    ))

    raw, quality, note = _holding_period_component(input.seconds_to_market_end)
    components.append(TradeScoreComponent(
        name="holding_period_quality",
        raw_score=raw,
        weight=WEIGHTS["holding_period_quality"],
        quality=quality,
        formula="duration_buckets",
        note=note,
    ))

    raw, quality, note = _market_resolution_component(
        input.market_active, input.market_closed, input.market_resolved
    )
    components.append(TradeScoreComponent(
        name="market_and_resolution_quality",
        raw_score=raw,
        weight=WEIGHTS["market_and_resolution_quality"],
        quality=quality,
        formula="active=100, closed/resolved=0",
        note=note,
    ))

    raw, quality, note = _strategy_data_component(
        input.has_valid_strategy, input.has_complete_data
    )
    components.append(TradeScoreComponent(
        name="strategy_and_data_quality",
        raw_score=raw,
        weight=WEIGHTS["strategy_and_data_quality"],
        quality=quality,
        formula="100 if both true, else partial/deduction",
        note=note,
    ))

    # Compute final score
    weighted_total = sum(c.weighted_score for c in components)
    final_score = clamp(round(weighted_total, 4))

    # Determine verdict
    if final_score >= VERDICT_COPY_CANDIDATE_MIN:
        verdict = TradeVerdict.COPY_CANDIDATE
    elif final_score >= VERDICT_WATCHLIST_MIN:
        verdict = TradeVerdict.WATCHLIST
    else:
        verdict = TradeVerdict.SKIP

    return TradeScoreResult(
        wallet_id=wallet_id,
        source_trade_id=source_trade_id,
        score=final_score,
        verdict=verdict,
        input=input,
        components=components,
        missing_essentials=missing_essentials,
        rejection_reasons=rejection_reasons,
        computed_at=now,
        is_sample=is_sample,
    )