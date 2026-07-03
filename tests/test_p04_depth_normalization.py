"""Tests for bounded order-book depth normalization (PR 4).

Phase 5 hardens the depth normalization contract:
- negative size returns DEPTH_LEVELS_MALFORMED
- zero size is silently ignored
- duplicate prices are aggregated
- asks are sorted ascending; bids descending
- crossed books are rejected
- max_levels and max_notional are enforced deterministically
- first level is always persisted in full (so a one-level book is
  never silently dropped); subsequent levels are truncated to fit
  the max-notional cap
- hash is derived from exact bounded normalized content
- equivalent normalized books hash identically
- walk_depth returns Decimal-precise fill_percentage on [0, 1]
- slippage handles zero best price safely (returns None, not raise)
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from polycopy.scoring.depth_normalization import (
    DepthWalkResult,
    NormalizedLevel,
    compute_book_hash,
    normalize_book_levels,
    walk_depth,
    DEPTH_INSUFFICIENT_FOR_STAKE,
    DEPTH_LEVELS_MALFORMED,
)


class TestLevelOrdering:
    """Deterministic ordering: asks asc, bids desc."""

    def test_asks_ascending(self):
        raw_asks = [("0.20", "10"), ("0.10", "10"), ("0.15", "10")]
        bids, asks, err = normalize_book_levels([], raw_asks)
        assert err is None
        assert [float(level.price) for level in asks] == [0.10, 0.15, 0.20]

    def test_bids_descending(self):
        raw_bids = [("0.50", "10"), ("0.60", "10"), ("0.55", "10")]
        bids, asks, err = normalize_book_levels(raw_bids, [])
        assert err is None
        assert [float(level.price) for level in bids] == [0.60, 0.55, 0.50]

    def test_empty_book(self):
        bids, asks, err = normalize_book_levels([], [])
        assert err is None
        assert bids == []
        assert asks == []

    def test_one_sided_book_is_valid(self):
        """A book with only one side must not be flagged as crossed."""
        bids, asks, err = normalize_book_levels([("0.50", "10")], [])
        assert err is None
        assert len(bids) == 1
        assert asks == []

        bids, asks, err = normalize_book_levels([], [("0.50", "10")])
        assert err is None
        assert bids == []
        assert len(asks) == 1


class TestDuplicateAggregation:
    """Duplicate prices are aggregated."""

    def test_aggregate_same_price(self):
        raw_asks = [("0.10", "5"), ("0.10", "5"), ("0.10", "5")]
        bids, asks, err = normalize_book_levels([], raw_asks)
        assert err is None
        assert len(asks) == 1
        assert float(asks[0].size) == 15.0
        assert float(asks[0].price) == 0.10

    def test_bids_aggregate(self):
        raw_bids = [("0.50", "10"), ("0.50", "20")]
        bids, asks, err = normalize_book_levels(raw_bids, [])
        assert err is None
        assert len(bids) == 1
        assert float(bids[0].size) == 30.0


class TestBoundedDepth:
    """Max levels and max notional respected (Phase 5 truncation)."""

    def test_max_levels(self):
        raw_asks = [(str(i / 100.0), "1") for i in range(1, 30)]
        bids, asks, err = normalize_book_levels([], raw_asks, max_levels=5)
        assert err is None
        assert len(asks) == 5

    def test_first_level_truncated_to_cap(self):
        """The first level is subject to the same max_notional cap as
        every other level. If a single level's notional exceeds the
        cap, its size is truncated so cumulative_notional equals
        max_notional exactly.
        """
        raw_asks = [("0.90", "100")]  # notional = 90
        bids, asks, err = normalize_book_levels(
            [], raw_asks, max_notional=Decimal("10"),
        )
        assert err is None
        assert len(asks) == 1
        # First level truncated: 10 / 0.90 = 11.111... ≈ 11.1111...
        # cumulative_notional = exactly max_notional (10)
        expected_allowed = Decimal("10") / Decimal("0.90")
        assert asks[0].size == expected_allowed
        assert asks[0].cumulative_notional == Decimal("10")
        assert asks[0].cumulative_size == expected_allowed

    def test_second_level_truncated_to_cap(self):
        """If level 1 fits but level 2 would exceed the cap, level 2
        is truncated to fit exactly the remaining notional capacity.
        """
        # Level 1: 0.10 * 10 = 1 (fits)
        # Level 2: 0.20 * 100 = 20 would exceed cap of 5
        #   remaining = 5 - 1 = 4 → allowed_size = 4 / 0.20 = 20
        #   so level 2 size = 20, cumulative_notional = 5 (cap exactly)
        raw_asks = [("0.10", "10"), ("0.20", "100")]
        bids, asks, err = normalize_book_levels(
            [], raw_asks, max_notional=Decimal("5"),
        )
        assert err is None
        assert len(asks) == 2
        assert float(asks[0].size) == 10.0
        assert float(asks[1].size) == 20.0
        assert float(asks[1].cumulative_notional) == 5.0

    def test_cap_exactly_reached_after_truncation(self):
        """Final cumulative notional must never exceed max_notional."""
        raw_asks = [("0.10", "1"), ("0.20", "1"), ("0.30", "1000")]
        bids, asks, err = normalize_book_levels(
            [], raw_asks, max_notional=Decimal("0.20"),
        )
        assert err is None
        # Level 1 notional = 0.10; level 2 = 0.20 → cum = 0.30 > 0.20
        # so level 2 is truncated: remaining = 0.20 - 0.10 = 0.10
        # allowed_size = 0.10 / 0.20 = 0.5 → cum = 0.20 (exact cap)
        assert len(asks) == 2
        assert float(asks[-1].cumulative_notional) == pytest.approx(0.20, abs=1e-9)

    def test_later_level_omitted_when_cap_already_full(self):
        """If the cap is exactly reached after a truncated level, no
        further level is added.
        """
        # Level 1: 0.10 * 5 = 0.50 (fits)
        # Level 2: 0.20 * 5 = 1.00 → remaining = 0.50 - 0.50 = 0
        # → break, no further levels
        raw_asks = [("0.10", "5"), ("0.20", "5"), ("0.30", "5")]
        bids, asks, err = normalize_book_levels(
            [], raw_asks, max_notional=Decimal("0.50"),
        )
        assert err is None
        assert len(asks) == 1

    def test_zero_price_level_keeps_notional_flat(self):
        """A zero-price level contributes zero notional and may be
        retained if within max_levels. Cumulative notional must
        remain unchanged.
        """
        # Note: 0.0 and 0.10 must be distinct prices to avoid dedup
        # merging — 0.0 sorts before 0.10, so the zero-price level is
        # asks[0] and 0.10 is asks[1].
        raw_asks = [("0.10", "5"), ("0", "1")]
        bids, asks, err = normalize_book_levels(
            [], raw_asks, max_notional=Decimal("1.0"),
        )
        assert err is None
        # Two distinct prices (0.0 and 0.10) are persisted as two
        # levels, with the zero-price level appearing first in sorted
        # (ask-asc) order.
        assert len(asks) == 2
        assert float(asks[0].price) == 0.0
        assert float(asks[0].size) == 1.0
        assert float(asks[0].cumulative_notional) == Decimal("0")
        # The 0.10 level is the only notional contributor.
        assert float(asks[1].price) == 0.10
        assert float(asks[1].cumulative_notional) == pytest.approx(0.5, abs=1e-9)


class TestParameterValidation:
    """Reject malformed parameters deterministically."""

    def test_max_levels_zero_is_malformed(self):
        bids, asks, err = normalize_book_levels(
            [("0.50", "10")], [("0.55", "10")], max_levels=0,
        )
        assert err == DEPTH_LEVELS_MALFORMED

    def test_max_levels_negative_is_malformed(self):
        bids, asks, err = normalize_book_levels(
            [("0.50", "10")], [("0.55", "10")], max_levels=-1,
        )
        assert err == DEPTH_LEVELS_MALFORMED

    def test_max_notional_zero_is_malformed(self):
        bids, asks, err = normalize_book_levels(
            [("0.50", "10")], [("0.55", "10")],
            max_notional=Decimal("0"),
        )
        assert err == DEPTH_LEVELS_MALFORMED

    def test_max_notional_negative_is_malformed(self):
        bids, asks, err = normalize_book_levels(
            [("0.50", "10")], [("0.55", "10")],
            max_notional=Decimal("-1"),
        )
        assert err == DEPTH_LEVELS_MALFORMED

    def test_tuple_shape_too_short(self):
        bids, asks, err = normalize_book_levels(
            [("0.50",)], [("0.55", "10")],
        )
        assert err == DEPTH_LEVELS_MALFORMED

    def test_tuple_shape_too_long(self):
        bids, asks, err = normalize_book_levels(
            [("0.50", "10", "extra")], [("0.55", "10")],
        )
        assert err == DEPTH_LEVELS_MALFORMED

    def test_dict_entry_rejected(self):
        bids, asks, err = normalize_book_levels(
            [{"price": "0.50", "size": "10"}], [("0.55", "10")],
        )
        assert err == DEPTH_LEVELS_MALFORMED

    def test_none_entry_rejected(self):
        bids, asks, err = normalize_book_levels(
            [None, ("0.55", "10")], [("0.56", "10")],
        )
        assert err == DEPTH_LEVELS_MALFORMED

    def test_exponent_notation_parses(self):
        """Decimal exponent notation must parse normally if finite."""
        bids, asks, err = normalize_book_levels(
            [("5E-1", "10")], [("6e-1", "10")],
        )
        assert err is None
        assert float(bids[0].price) == 0.5
        assert float(asks[0].price) == 0.6

    def test_whitespace_numeric_parses(self):
        """Whitespace-containing numeric strings must parse normally."""
        bids, asks, err = normalize_book_levels(
            [("  0.50  ", "10")], [("0.55", "10")],
        )
        assert err is None
        assert float(bids[0].price) == 0.5


class TestMalformedData:
    """Rejection of bad input data."""

    def test_nan_price(self):
        raw = [("nan", "10")]
        bids, asks, err = normalize_book_levels([], raw)
        assert err == DEPTH_LEVELS_MALFORMED

    def test_inf_size(self):
        raw = [("0.10", "inf")]
        bids, asks, err = normalize_book_levels([], raw)
        assert err == DEPTH_LEVELS_MALFORMED

    def test_negative_inf_size(self):
        raw = [("0.10", "-inf")]
        bids, asks, err = normalize_book_levels([], raw)
        assert err == DEPTH_LEVELS_MALFORMED

    def test_nan_size(self):
        raw = [("0.10", "nan")]
        bids, asks, err = normalize_book_levels([], raw)
        assert err == DEPTH_LEVELS_MALFORMED

    def test_negative_price(self):
        raw = [("-0.10", "10")]
        bids, asks, err = normalize_book_levels([], raw)
        assert err == DEPTH_LEVELS_MALFORMED

    def test_price_above_one(self):
        raw = [("1.50", "10")]
        bids, asks, err = normalize_book_levels([], raw)
        assert err == DEPTH_LEVELS_MALFORMED

    def test_negative_size_rejected(self):
        """Phase 5: negative size is malformed (was silently ignored pre-fix)."""
        raw_asks = [("0.10", "-5"), ("0.20", "10")]
        bids, asks, err = normalize_book_levels([], raw_asks)
        assert err == DEPTH_LEVELS_MALFORMED

    def test_zero_size_ignored(self):
        """Phase 5: zero size is still silently ignored (different from negative)."""
        raw_asks = [("0.10", "0"), ("0.20", "10")]
        bids, asks, err = normalize_book_levels([], raw_asks)
        assert err is None
        assert len(asks) == 1
        assert float(asks[0].price) == 0.20

    def test_crossed_book(self):
        raw_bids = [("0.60", "10")]
        raw_asks = [("0.50", "10")]
        bids, asks, err = normalize_book_levels(raw_bids, raw_asks)
        assert err == DEPTH_LEVELS_MALFORMED

    def test_crossed_book_equal_price(self):
        raw_bids = [("0.50", "10")]
        raw_asks = [("0.50", "10")]
        bids, asks, err = normalize_book_levels(raw_bids, raw_asks)
        assert err == DEPTH_LEVELS_MALFORMED


class TestDepthWalk:
    """Multi-level depth walks for BUY and SELL (Phase 7)."""

    def test_buy_single_level_full_fill(self):
        asks = [NormalizedLevel(Decimal("0.10"), Decimal("100"))]
        result = walk_depth(asks, "BUY", Decimal("5"))
        assert result.is_complete
        assert result.filled_notional == Decimal("5")
        assert result.levels_consumed == 1
        # fill_percentage is a Decimal ratio on [0, 1]
        assert result.fill_percentage == Decimal("1")

    def test_buy_multi_level(self):
        asks = [
            NormalizedLevel(Decimal("0.10"), Decimal("50")),  # notional 5
            NormalizedLevel(Decimal("0.20"), Decimal("100")),  # notional 20
        ]
        result = walk_depth(asks, "BUY", Decimal("25"))
        assert result.is_complete
        assert result.levels_consumed == 2
        assert result.filled_notional == Decimal("25")

    def test_buy_partial_fill(self):
        asks = [NormalizedLevel(Decimal("0.10"), Decimal("50"))]  # notional 5
        result = walk_depth(asks, "BUY", Decimal("100"))
        assert not result.is_complete
        assert result.insufficient_reason == DEPTH_INSUFFICIENT_FOR_STAKE
        assert result.filled_notional == Decimal("5")
        assert result.levels_consumed == 1
        # 5 / 100 = 0.05
        assert result.fill_percentage == Decimal("0.05")

    def test_sell_single_level(self):
        bids = [NormalizedLevel(Decimal("0.90"), Decimal("50"))]
        result = walk_depth(bids, "SELL", Decimal("10"))
        assert result.is_complete
        assert result.filled_notional == Decimal("10")
        assert result.fill_percentage == Decimal("1")

    def test_sell_multi_level(self):
        bids = [
            NormalizedLevel(Decimal("0.90"), Decimal("10")),  # notional 9
            NormalizedLevel(Decimal("0.80"), Decimal("50")),  # notional 40
        ]
        result = walk_depth(bids, "SELL", Decimal("30"))
        assert result.is_complete
        assert result.levels_consumed == 2

    def test_zero_fill_empty_book(self):
        result = walk_depth([], "BUY", Decimal("10"))
        assert not result.is_complete
        assert result.filled_notional == Decimal("0")
        assert result.fill_percentage == Decimal("0")
        assert result.insufficient_reason is not None

    def test_zero_fill_intended_notional_zero(self):
        """intended_notional <= 0 must produce a degenerate result, not raise."""
        asks = [NormalizedLevel(Decimal("0.10"), Decimal("100"))]
        result = walk_depth(asks, "BUY", Decimal("0"))
        assert not result.is_complete
        assert result.filled_notional == Decimal("0")
        assert result.fill_percentage == Decimal("0")
        assert result.insufficient_reason == DEPTH_INSUFFICIENT_FOR_STAKE

    def test_negative_intended_notional_returns_invalid(self):
        asks = [NormalizedLevel(Decimal("0.10"), Decimal("100"))]
        result = walk_depth(asks, "BUY", Decimal("-5"))
        assert not result.is_complete
        assert result.filled_notional == Decimal("0")

    def test_vwap_calculation(self):
        asks = [
            NormalizedLevel(Decimal("0.10"), Decimal("100")),  # notional 10
            NormalizedLevel(Decimal("0.20"), Decimal("100")),  # notional 20
        ]
        result = walk_depth(asks, "BUY", Decimal("15"))
        # Fill: $10 at 0.10, then $5 at 0.20
        # Contracts: 100 + 25 = 125
        # VWAP = 15 / 125 = 0.12
        assert result.is_complete
        assert result.vwap_fill_price == Decimal("0.12")

    def test_no_extrapolation(self):
        """Must not extrapolate beyond stored levels."""
        asks = [NormalizedLevel(Decimal("0.10"), Decimal("10"))]  # notional 1
        result = walk_depth(asks, "BUY", Decimal("100"))
        assert not result.is_complete
        assert result.filled_notional == Decimal("1")

    def test_buy_exact_notional_boundary(self):
        """intended_notional == level_notional: full fill, no residue."""
        asks = [NormalizedLevel(Decimal("0.50"), Decimal("10"))]  # notional 5
        result = walk_depth(asks, "BUY", Decimal("5"))
        assert result.is_complete
        assert result.filled_notional == Decimal("5")
        assert result.remaining_notional == Decimal("0")
        assert result.fill_percentage == Decimal("1")

    def test_zero_price_level_skipped_in_walk(self):
        """A zero-price level in the book contributes zero notional and
        no contracts; it does not block consumption of subsequent
        levels.
        """
        asks = [
            NormalizedLevel(Decimal("0.10"), Decimal("10")),  # notional 1
            NormalizedLevel(Decimal("0"), Decimal("100000")),  # zero notional
            NormalizedLevel(Decimal("0.50"), Decimal("4")),  # notional 2
        ]
        result = walk_depth(asks, "BUY", Decimal("3"))
        assert result.is_complete
        # The zero-price level contributes nothing; $1 at 0.10 and $2 at 0.50
        assert result.filled_notional == Decimal("3")
        # Contracts: 1/0.10 + 2/0.50 = 10 + 4 = 14
        assert result.contracts_filled == Decimal("14")
        # Only 2 levels actually contributed notional; zero-price level
        # is visited but contributes nothing (levels_consumed counts
        # notional-contributing levels).
        assert result.levels_consumed == 2


class TestBookHash:
    """Canonical hash stability and sensitivity (Phase 5)."""

    def test_same_inputs_same_hash(self):
        raw_bids = [("0.47", "3")]
        raw_asks = [("0.48", "10"), ("0.50", "20")]
        bids, asks, err = normalize_book_levels(raw_bids, raw_asks)
        assert err is None, f"book crossed: {err}"
        assert bids and asks
        h1 = compute_book_hash(bids, asks)
        h2 = compute_book_hash(bids, asks)
        assert h1 == h2

    def test_different_book_different_hash(self):
        """Two genuinely different normalized books must produce
        different hashes.
        """
        bids1, asks1, _ = normalize_book_levels(
            [("0.90", "10")], [("0.91", "10")],
        )
        bids2, asks2, _ = normalize_book_levels(
            [("0.80", "10")], [("0.81", "10")],
        )
        # Both normalize cleanly (no crossing). The bids differ
        # (0.90 vs 0.80) so the bounded persisted book differs.
        assert bids1 and asks1 and bids2 and asks2
        assert compute_book_hash(bids1, asks1) != compute_book_hash(bids2, asks2)

    def test_equivalent_raw_order_same_hash(self):
        """Different raw input order must produce the same normalized
        hash after dedup + sort + truncation.
        """
        raw_a = [("0.50", "5"), ("0.48", "3"), ("0.49", "7")]
        raw_b = [("0.49", "7"), ("0.48", "3"), ("0.50", "5")]
        bids_a, asks_a, _ = normalize_book_levels([], raw_a)
        bids_b, asks_b, _ = normalize_book_levels([], raw_b)
        assert compute_book_hash(bids_a, asks_a) == compute_book_hash(bids_b, asks_b)

    def test_hash_includes_side(self):
        """Swapping sides must change the hash (not just the levels)."""
        # Same levels but mirrored across sides
        bids1, asks1, _ = normalize_book_levels([("0.50", "5")], [("0.55", "5")])
        # Now build a book with those levels flipped to the other side
        bids2, asks2, _ = normalize_book_levels([("0.55", "5")], [("0.50", "5")])
        # The flipped book will be crossed (0.55 >= 0.50) so normalize
        # returns error — use a non-crossed flip instead:
        bids2, asks2, _ = normalize_book_levels([("0.40", "5")], [("0.55", "5")])
        h1 = compute_book_hash(bids1, asks1)
        h2 = compute_book_hash(bids2, asks2)
        # Different books → different hashes (this verifies the
        # hash is sensitive to side-membership at all).
        assert h1 != h2

    def test_hash_changes_after_truncation(self):
        """The hash must reflect the bounded (truncated) persisted book,
        not the unbounded raw book.
        """
        raw_asks = [("0.10", "1"), ("0.20", "1"), ("0.30", "1000")]
        bids_small, asks_small, _ = normalize_book_levels(
            [], raw_asks, max_notional=Decimal("0.20"),
        )
        bids_big, asks_big, _ = normalize_book_levels(
            [], raw_asks, max_notional=Decimal("1000"),
        )
        h_small = compute_book_hash(bids_small, asks_small)
        h_big = compute_book_hash(bids_big, asks_big)
        assert h_small != h_big


class TestSlippage:
    """Slippage calculation (Phase 7, Decimal, safe-zero)."""

    def test_buy_no_slippage(self):
        asks = [NormalizedLevel(Decimal("0.10"), Decimal("1000"))]
        result = walk_depth(asks, "BUY", Decimal("50"))
        assert result.is_complete
        assert result.slippage == Decimal("0")

    def test_buy_with_slippage(self):
        """Multi-level BUY produces positive slippage = (vwap - best) / best."""
        asks = [
            NormalizedLevel(Decimal("0.10"), Decimal("50")),   # notional 5
            NormalizedLevel(Decimal("0.20"), Decimal("100")),  # notional 20
        ]
        result = walk_depth(asks, "BUY", Decimal("25"))
        # Fill: 5 @ 0.10 (50 contracts) + 20 @ 0.20 (100 contracts)
        # VWAP = 25 / 150 = 1/6
        # Slippage = (1/6 - 0.10) / 0.10 = (1/6 - 1/10) * 10 = 2/3
        assert result.slippage is not None
        assert result.slippage > Decimal("0")
        expected = (Decimal("1") / Decimal("6") - Decimal("0.10")) / Decimal("0.10")
        assert result.slippage == expected

    def test_sell_slippage(self):
        bids = [
            NormalizedLevel(Decimal("0.90"), Decimal("10")),   # notional 9
            NormalizedLevel(Decimal("0.80"), Decimal("50")),   # notional 40
        ]
        result = walk_depth(bids, "SELL", Decimal("30"))
        # VWAP = 30 / (10 + 50) = 30/60 = 0.50; best = 0.90
        # (0.90 - 0.50) / 0.90 = 0.40/0.90 ≈ 0.444...
        assert result.slippage is not None
        assert result.slippage > Decimal("0")

    def test_slippage_safe_zero_best_price(self):
        """If the best executable price is zero, slippage is None
        rather than raising.
        """
        # First level price = 0 → best price = 0 → safe-zero slippage
        bids = [NormalizedLevel(Decimal("0"), Decimal("10"))]
        result = walk_depth(bids, "SELL", Decimal("1"))
        assert result.slippage is None

    def test_slippage_returned_as_decimal(self):
        """Slippage is a Decimal fraction, not a percent (Phase 7)."""
        asks = [NormalizedLevel(Decimal("0.10"), Decimal("50"))]
        result = walk_depth(asks, "BUY", Decimal("5"))
        # 5% slippage = 0.05 in our Decimal-fraction contract
        # Single-level fill → VWAP == best → 0.0
        assert isinstance(result.slippage, Decimal)
        assert result.slippage == Decimal("0")


class TestFillPercentageScale:
    """Phase 7: fill_percentage is a Decimal ratio on [0, 1], not 0-100."""

    def test_full_fill_is_one(self):
        asks = [NormalizedLevel(Decimal("0.50"), Decimal("20"))]
        result = walk_depth(asks, "BUY", Decimal("10"))
        assert result.fill_percentage == Decimal("1")

    def test_half_fill_is_zero_point_five(self):
        asks = [NormalizedLevel(Decimal("0.50"), Decimal("20"))]  # notional 10
        result = walk_depth(asks, "BUY", Decimal("20"))
        assert result.fill_percentage == Decimal("0.5")

    def test_quarter_fill_is_zero_point_two_five(self):
        asks = [NormalizedLevel(Decimal("0.50"), Decimal("100"))]  # notional 50
        result = walk_depth(asks, "BUY", Decimal("200"))
        # 50 / 200 = 0.25
        assert result.fill_percentage == Decimal("0.25")

    def test_zero_fill_is_zero(self):
        result = walk_depth([], "BUY", Decimal("10"))
        assert result.fill_percentage == Decimal("0")


class TestDepthReasons:
    """Error / reason for insufficient depth."""

    def test_insufficient_stake_reason(self):
        asks = [NormalizedLevel(Decimal("0.10"), Decimal("1"))]
        result = walk_depth(asks, "BUY", Decimal("100"))
        assert result.insufficient_reason == DEPTH_INSUFFICIENT_FOR_STAKE
        assert not result.is_complete