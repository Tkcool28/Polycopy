"""Wallet behavior classification for copyability scoring.

Classifies wallet trading patterns to determine eligibility for
copying. Only DIRECTIONAL wallets may receive COPY CANDIDATE
verdict. Other classifications are capped to WATCHLIST or SKIP
but retained for research.

Classifications:
- DIRECTIONAL: Focused on price-movement predictions (copyable)
- MARKET_MAKER_LP: Continuous two-sided market making
- ARBITRAGE_MULTI_LEG: Multi-leg arbitrage strategies
- HIGH_FREQUENCY_BOT: Rapid short-interval trading
- MIXED: Mixed / genuinely conflicting behavior evidence
- UNKNOWN: Insufficient data to classify

This module owns:
- The BehaviorEvidence dataclass (raw observables).
- The BehaviorClassificationResult dataclass (cap flags).
- The transparent threshold constants (no buried magic numbers).
- The classify_wallet_behavior() pure function.
- The behavior evidence loader that derives a BehaviorEvidence
  from persisted source_trades (no external I/O, no CLOB).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional


# ---- Transparent classifier thresholds -----------------------------------
#
# These are operator-facing constants. The classifier reads
# every threshold from this section; nothing is buried in a
# conditional. Adjust here, not inside the rule logic.

#: Minimum number of trades for a reliable classification.
#: Below this, the result is UNKNOWN.
MIN_TRADES_FOR_CLASSIFICATION = 5

#: Average seconds between trades below this plus a sufficient
#: trade count is HIGH_FREQUENCY_BOT.
HFT_AVG_INTERVAL_SECONDS = 10.0
#: Minimum trade count to qualify for HFT.
HFT_MIN_TRADE_COUNT = 50

#: Proportion of a wallet's markets in which it traded both
#: BUY and SELL at least once. Above this proportion with a
#: sufficient sample, classify as MARKET_MAKER_LP.
TWO_SIDED_MARKET_SHARE = 0.5
#: Minimum two-sided markets to qualify for MARKET_MAKER_LP.
TWO_SIDED_MARKET_MIN = 3
#: Minimum trades per market on average to qualify for MM
#: (rules out a wallet that happened to flip one side once).
TWO_SIDED_AVG_TRADES_PER_MARKET = 4

#: Two outcomes in the same market with opposing positions
#: inside this window is an arbitrage-like pattern.
ARB_SAME_MARKET_WINDOW_SECONDS = 60
#: Markets across which trades within this window suggest
#: cross-market arbitrage.
ARB_MULTI_MARKET_WINDOW_SECONDS = 60
#: Number of distinct markets traded in a single window to
#: count as multi-leg arbitrage.
ARB_MIN_DISTINCT_MARKETS = 3

#: Distinct markets traded above this without a clear pattern
#: suggests MIXED (not just "diversified directional").
MIXED_DISTINCT_MARKETS = 20

#: Direct conflicts between MM and directional evidence
#: yield MIXED. (Specifically: at least 2 two-sided markets
#: AND at least 2 markets with strong one-sided dominance.)
MIXED_CONFLICT_TWO_SIDED = 2
MIXED_CONFLICT_DOMINANT = 2

#: One-sided dominance threshold — a market where one side
#: accounts for at least this fraction of trades.
DOMINANT_SIDE_FRACTION = 0.80
#: Minimum trades in a market to call it "dominant".
DOMINANT_SIDE_MIN_TRADES = 3


# ---- Behavior classifications --------------------------------------------


class BehaviorClassification(str, enum.Enum):
    """Trading behavior classification for wallet eligibility."""

    DIRECTIONAL = "directional"
    MARKET_MAKER_LP = "market_maker_lp"
    ARBITRAGE_MULTI_LEG = "arbitrage_multi_leg"
    HIGH_FREQUENCY_BOT = "high_frequency_bot"
    MIXED = "mixed"
    UNKNOWN = "unknown"


# ---- Evidence contract ---------------------------------------------------


@dataclass
class BehaviorEvidence:
    """Evidence collected for behavior classification.

    All fields are observable metrics derived from persisted
    wallet trade history. None means "not computed" — the
    classifier treats None as missing evidence and falls
    back to UNKNOWN where appropriate.
    """

    # Trade-level metrics
    trade_count: Optional[int] = None
    avg_trades_per_day: Optional[float] = None
    avg_time_between_trades_seconds: Optional[float] = None

    # Market-level diversity
    distinct_markets_traded: Optional[int] = None
    is_two_sided_market_making: Optional[bool] = None
    two_sided_market_count: Optional[int] = None

    # Multi-leg detection
    is_multi_leg_pattern: Optional[bool] = None
    multi_market_burst_count: Optional[int] = None

    # Arbitrage pattern detection
    is_price_arbitrage_pattern: Optional[bool] = None
    opposing_outcome_event_count: Optional[int] = None

    # Directional evidence
    dominant_side_market_count: Optional[int] = None


@dataclass
class BehaviorClassificationResult:
    """Result of wallet behavior classification.

    Cap semantics:
      - DIRECTIONAL does not cap → COPY_CANDIDATE possible.
      - MARKET_MAKER_LP / ARBITRAGE_MULTI_LEG / HIGH_FREQUENCY_BOT
        are SKIP.
      - MIXED / UNKNOWN cap at WATCHLIST (cannot be
        COPY_CANDIDATE).
    """

    classification: BehaviorClassification
    reasons: list[str] = field(default_factory=list)
    is_eligible_for_copy: bool = False
    is_watchlist_cap: bool = False
    is_skip: bool = False

    @property
    def verdict_cap(self) -> Optional[str]:
        """Maximum verdict this classification can receive."""
        if self.is_eligible_for_copy:
            return None
        if self.is_watchlist_cap:
            return "watchlist"
        if self.is_skip:
            return "skip"
        return None


# ---- Pure classifier -----------------------------------------------------


def classify_wallet_behavior(
    evidence: BehaviorEvidence,
) -> BehaviorClassificationResult:
    """Classify wallet behavior from trade evidence.

    Pure function: no I/O, no hidden state. Reads every
    threshold from the module-level constants. Order of
    rules is fixed: HFT → MM → Arbitrage → Conflict → MIXED
    → DIRECTIONAL (only with positive evidence).

    DIRECTIONAL is never the default. It is returned ONLY
    when positive directional evidence is present and no
    exclusion pattern was found. Insufficient evidence →
    UNKNOWN.
    """
    reasons: list[str] = []

    # ---- 1. Insufficient evidence → UNKNOWN ------------------------
    trade_count = evidence.trade_count
    if trade_count is None or trade_count < MIN_TRADES_FOR_CLASSIFICATION:
        reasons.append(
            f"insufficient_trades_for_classification "
            f"(min={MIN_TRADES_FOR_CLASSIFICATION}, got={trade_count})"
        )
        return BehaviorClassificationResult(
            classification=BehaviorClassification.UNKNOWN,
            reasons=reasons,
            is_eligible_for_copy=False,
            is_watchlist_cap=True,
            is_skip=False,
        )

    # ---- 2. HFT detection ------------------------------------------
    avg_time = evidence.avg_time_between_trades_seconds
    if (
        avg_time is not None
        and avg_time < HFT_AVG_INTERVAL_SECONDS
        and trade_count >= HFT_MIN_TRADE_COUNT
    ):
        reasons.append(
            f"high_frequency_detected "
            f"(avg_interval_s<{HFT_AVG_INTERVAL_SECONDS}, "
            f"trade_count>={HFT_MIN_TRADE_COUNT})"
        )
        return BehaviorClassificationResult(
            classification=BehaviorClassification.HIGH_FREQUENCY_BOT,
            reasons=reasons,
            is_eligible_for_copy=False,
            is_watchlist_cap=False,
            is_skip=True,
        )

    # ---- 3. Market-maker LP detection ------------------------------
    two_sided_count = evidence.two_sided_market_count or 0
    distinct = evidence.distinct_markets_traded or 0
    avg_trades_per_market = (
        trade_count / distinct if distinct > 0 else 0.0
    )
    if (
        two_sided_count >= TWO_SIDED_MARKET_MIN
        and avg_trades_per_market >= TWO_SIDED_AVG_TRADES_PER_MARKET
        and (two_sided_count / distinct) >= TWO_SIDED_MARKET_SHARE
    ):
        reasons.append(
            f"market_maker_lp_detected "
            f"(two_sided_markets={two_sided_count}, "
            f"distinct={distinct}, avg_per_market={avg_trades_per_market:.2f})"
        )
        return BehaviorClassificationResult(
            classification=BehaviorClassification.MARKET_MAKER_LP,
            reasons=reasons,
            is_eligible_for_copy=False,
            is_watchlist_cap=False,
            is_skip=True,
        )

    # ---- 4. Arbitrage / multi-leg detection ------------------------
    opposing = evidence.opposing_outcome_event_count or 0
    multi_market_bursts = evidence.multi_market_burst_count or 0
    if opposing > 0:
        reasons.append(
            f"multi_leg_arbitrage_pattern_detected "
            f"(opposing_outcome_events={opposing}, "
            f"window_s<{ARB_SAME_MARKET_WINDOW_SECONDS})"
        )
        return BehaviorClassificationResult(
            classification=BehaviorClassification.ARBITRAGE_MULTI_LEG,
            reasons=reasons,
            is_eligible_for_copy=False,
            is_watchlist_cap=False,
            is_skip=True,
        )
    if multi_market_bursts > 0:
        reasons.append(
            f"multi_market_burst_detected "
            f"(bursts={multi_market_bursts}, "
            f"markets_per_burst>={ARB_MIN_DISTINCT_MARKETS}, "
            f"window_s<{ARB_MULTI_MARKET_WINDOW_SECONDS})"
        )
        return BehaviorClassificationResult(
            classification=BehaviorClassification.ARBITRAGE_MULTI_LEG,
            reasons=reasons,
            is_eligible_for_copy=False,
            is_watchlist_cap=False,
            is_skip=True,
        )

    # ---- 5. MIXED detection (genuine conflict) ---------------------
    dominant_count = evidence.dominant_side_market_count or 0
    if (
        two_sided_count >= MIXED_CONFLICT_TWO_SIDED
        and dominant_count >= MIXED_CONFLICT_DOMINANT
    ):
        reasons.append(
            f"mixed_behavior_conflict_detected "
            f"(two_sided_markets={two_sided_count}>="
            f"{MIXED_CONFLICT_TWO_SIDED}, "
            f"dominant_markets={dominant_count}>="
            f"{MIXED_CONFLICT_DOMINANT})"
        )
        return BehaviorClassificationResult(
            classification=BehaviorClassification.MIXED,
            reasons=reasons,
            is_eligible_for_copy=False,
            is_watchlist_cap=True,
            is_skip=False,
        )

    # ---- 6. MIXED detection (high market diversity w/o pattern) ---
    if distinct > MIXED_DISTINCT_MARKETS:
        reasons.append(
            f"high_market_diversity_without_clear_pattern "
            f"(distinct_markets={distinct}>{MIXED_DISTINCT_MARKETS})"
        )
        return BehaviorClassificationResult(
            classification=BehaviorClassification.MIXED,
            reasons=reasons,
            is_eligible_for_copy=False,
            is_watchlist_cap=True,
            is_skip=False,
        )

    # ---- 7. DIRECTIONAL requires POSITIVE evidence -----------------
    #
    # A wallet with no exclusion pattern is NOT automatically
    # DIRECTIONAL. The wallet must have at least one
    # predominantly one-sided market. Otherwise we have no
    # positive evidence of "directional" trading and the
    # result is UNKNOWN.
    if dominant_count == 0:
        reasons.append(
            "no_positive_directional_evidence "
            "(no market with >=80% one-side dominance)"
        )
        return BehaviorClassificationResult(
            classification=BehaviorClassification.UNKNOWN,
            reasons=reasons,
            is_eligible_for_copy=False,
            is_watchlist_cap=True,
            is_skip=False,
        )

    reasons.append(
        f"directional_classified "
        f"(dominant_side_markets={dominant_count}, "
        f"two_sided_markets={two_sided_count})"
    )
    return BehaviorClassificationResult(
        classification=BehaviorClassification.DIRECTIONAL,
        reasons=reasons,
        is_eligible_for_copy=True,
        is_watchlist_cap=False,
        is_skip=False,
    )


# ---- Evidence loader from persisted source_trades ------------------------


def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp string. Returns None on
    malformed input. Accepts the 'Z' suffix and explicit
    offsets."""
    if not isinstance(ts, str) or not ts:
        return None
    candidate = ts.strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def load_behavior_evidence_from_rows(
    rows: Iterable[dict],
    *,
    include_sample: bool = False,
) -> BehaviorEvidence:
    """Derive a BehaviorEvidence from an iterable of source_trades.

    Each row is expected to be a sqlite3.Row-like mapping with
    keys: trader_address, side, market_source_id, outcome,
    timestamp, is_sample.

    Excludes:
      - sentinel trader_address values (lowercased match
        against the wallet-identity sentinel set)
      - rows with malformed timestamps
      - rows flagged is_sample=1 unless include_sample=True

    Canonicalizes trader_address using the project's
    canonical_wallet_address helper.

    Returns BehaviorEvidence with the metrics enumerated in
    the spec: trade_count, avg_trades_per_day, average time
    between trades, distinct markets, two-sided market
    making, multi-leg, arbitrage-like patterns.

    This function is a PURE derivation — no I/O, no CLOB.
    """
    from polycopy.db.wallet_identity import (
        canonical_wallet_address,
    )

    # Bucketing strategy:
    #   - trades: list of (ts, market_id, outcome, side) tuples
    #     in chronological order
    #   - per-market: (BUY_count, SELL_count) totals
    #   - per-(market, outcome): side counts (for opposing-outcome
    #     detection)
    trades: list[tuple[datetime, str, str, str]] = []
    per_market_sides: dict[str, dict[str, int]] = {}
    per_market_outcome_sides: dict[tuple[str, str], dict[str, int]] = {}

    for row in rows:
        is_sample = bool(row["is_sample"]) if "is_sample" in row.keys() else False
        if is_sample and not include_sample:
            continue

        addr = canonical_wallet_address(row["trader_address"])
        if addr is None:
            # Sentinel or malformed address → exclude.
            continue

        ts = _parse_iso(row["timestamp"]) if "timestamp" in row.keys() else None
        if ts is None:
            # Malformed timestamp → exclude.
            continue

        side = str(row["side"]).upper() if "side" in row.keys() else ""
        if side not in ("BUY", "SELL"):
            continue

        market_id = str(row["market_source_id"]) if "market_source_id" in row.keys() else ""
        outcome = str(row["outcome"]) if "outcome" in row.keys() else ""
        if not market_id or not outcome:
            continue

        trades.append((ts, market_id, outcome, side))
        per_market_sides.setdefault(market_id, {"BUY": 0, "SELL": 0})[side] += 1
        per_market_outcome_sides.setdefault(
            (market_id, outcome), {"BUY": 0, "SELL": 0}
        )[side] += 1

    trade_count = len(trades)
    if trade_count == 0:
        return BehaviorEvidence(
            trade_count=0,
            avg_trades_per_day=None,
            avg_time_between_trades_seconds=None,
            distinct_markets_traded=0,
            is_two_sided_market_making=False,
            two_sided_market_count=0,
            is_multi_leg_pattern=False,
            multi_market_burst_count=0,
            is_price_arbitrage_pattern=False,
            opposing_outcome_event_count=0,
            dominant_side_market_count=0,
        )

    # Sort chronologically.
    trades.sort(key=lambda t: t[0])

    # ---- Time-based metrics --------------------------------------
    timestamps = [t[0] for t in trades]
    intervals_seconds: list[float] = []
    for i in range(1, len(timestamps)):
        delta = (timestamps[i] - timestamps[i - 1]).total_seconds()
        if delta >= 0:
            intervals_seconds.append(delta)
        else:
            # Out-of-order timestamps (data quality issue) — treat
            # as a zero-length interval; do not introduce negatives.
            intervals_seconds.append(0.0)

    avg_time_between = (
        sum(intervals_seconds) / len(intervals_seconds)
        if intervals_seconds
        else None
    )

    # Active days: number of distinct UTC calendar days.
    active_days_set = {ts.date() for ts in timestamps}
    active_days = len(active_days_set)
    avg_trades_per_day = trade_count / active_days if active_days > 0 else None

    # ---- Market-level metrics ------------------------------------
    distinct_markets = len(per_market_sides)

    two_sided_market_count = 0
    dominant_side_market_count = 0
    for market_id, sides in per_market_sides.items():
        buy = sides.get("BUY", 0)
        sell = sides.get("SELL", 0)
        total = buy + sell
        if total < 1:
            continue
        if buy > 0 and sell > 0:
            two_sided_market_count += 1
        # Dominant-side market: at least DOMINANT_SIDE_MIN_TRADES
        # trades and >=DOMINANT_SIDE_FRACTION of them on one side.
        if total >= DOMINANT_SIDE_MIN_TRADES:
            share = max(buy, sell) / total
            if share >= DOMINANT_SIDE_FRACTION:
                dominant_side_market_count += 1

    is_two_sided = two_sided_market_count >= TWO_SIDED_MARKET_MIN

    # ---- Opposing-outcome (multi-leg) detection -----------------
    # For each (market, outcome) pair, we have BUY and SELL
    # counts. A "multi-leg" pattern is observed when, in the
    # same market, the wallet takes BOTH sides of an outcome
    # (or both outcomes on the same side) within a small time
    # window. A simpler measurable proxy: opposing-outcome
    # events = (market, outcome) pairs where the wallet has
    # activity on at least 2 distinct outcomes in the same
    # market AND the activity is interleaved chronologically.
    opposing_outcome_event_count = 0
    per_market_outcome_seq: dict[str, list[tuple[datetime, str, str]]] = {}
    for ts, market_id, outcome, side in trades:
        per_market_outcome_seq.setdefault(market_id, []).append(
            (ts, outcome, side)
        )
    for market_id, seq in per_market_outcome_seq.items():
        outcomes_in_market = {o for _, o, _ in seq}
        if len(outcomes_in_market) < 2:
            continue
        # Check for any pair (o1, o2) where o1 trades are
        # interleaved with o2 trades within
        # ARB_SAME_MARKET_WINDOW_SECONDS of one another.
        sorted_seq = sorted(seq, key=lambda x: x[0])
        for i in range(len(sorted_seq)):
            ts_i, out_i, _ = sorted_seq[i]
            for j in range(i + 1, len(sorted_seq)):
                ts_j, out_j, _ = sorted_seq[j]
                if out_i == out_j:
                    continue
                delta = (ts_j - ts_i).total_seconds()
                if delta < 0 or delta > ARB_SAME_MARKET_WINDOW_SECONDS:
                    if delta > ARB_SAME_MARKET_WINDOW_SECONDS:
                        break  # Sorted — no point going further.
                    continue
                opposing_outcome_event_count += 1
                break  # Count the (market) once.

    is_multi_leg = opposing_outcome_event_count > 0

    # ---- Multi-market bursts -------------------------------------
    # A burst is a window of ARB_MULTI_MARKET_WINDOW_SECONDS
    # seconds during which the wallet traded in
    # ARB_MIN_DISTINCT_MARKETS distinct markets.
    burst_count = 0
    left = 0
    for right in range(len(timestamps)):
        # Shrink left until the window is within bound.
        while (
            left < right
            and (timestamps[right] - timestamps[left]).total_seconds()
            > ARB_MULTI_MARKET_WINDOW_SECONDS
        ):
            left += 1
        window_markets = {trades[k][1] for k in range(left, right + 1)}
        if len(window_markets) >= ARB_MIN_DISTINCT_MARKETS:
            burst_count += 1
            # Skip past the burst to avoid double-counting.
            left = right + 1

    is_arb = burst_count > 0 or opposing_outcome_event_count > 0

    return BehaviorEvidence(
        trade_count=trade_count,
        avg_trades_per_day=avg_trades_per_day,
        avg_time_between_trades_seconds=avg_time_between,
        distinct_markets_traded=distinct_markets,
        is_two_sided_market_making=is_two_sided,
        two_sided_market_count=two_sided_market_count,
        is_multi_leg_pattern=is_multi_leg,
        multi_market_burst_count=burst_count,
        is_price_arbitrage_pattern=is_arb,
        opposing_outcome_event_count=opposing_outcome_event_count,
        dominant_side_market_count=dominant_side_market_count,
    )


def load_behavior_evidence(
    db,
    wallet_id: str,
    *,
    include_sample: bool = False,
) -> BehaviorEvidence:
    """Load trades for ``wallet_id`` from a Database and derive
    a BehaviorEvidence.

    Convenience wrapper around
    :func:`load_behavior_evidence_from_rows` for callers that
    have an open Database. Uses the canonical trader_address
    form via a JOIN with the wallets table when possible;
    falls back to the address normalization helper otherwise.

    Excludes sentinel / anonymous / malformed rows. Does NOT
    call any CLOB endpoint. Returns an empty BehaviorEvidence
    when the wallet has no observable trades.
    """
    canonical = wallet_id
    # Try to canonicalize from the wallets table.
    try:
        row = db.fetchone(
            "SELECT address, canonical_address FROM wallets WHERE id = ?",
            (wallet_id,),
        )
    except Exception:
        row = None
    if row is not None:
        for key in ("canonical_address", "address"):
            value = row[key]
            if value:
                from polycopy.db.wallet_identity import (
                    canonical_wallet_address,
                )
                canon = canonical_wallet_address(value)
                if canon is not None:
                    canonical = canon
                    break

    rows = db.fetchall(
        "SELECT trader_address, side, market_source_id, outcome, "
        "timestamp, is_sample FROM source_trades "
        "WHERE trader_address = ? OR trader_address = ? "
        "ORDER BY timestamp ASC",
        (canonical, wallet_id),
    )
    return load_behavior_evidence_from_rows(
        rows, include_sample=include_sample
    )


# ---- Backwards-compatible name used by paper_signal.py -------------------

_load_behavior_evidence_for_wallet = load_behavior_evidence
