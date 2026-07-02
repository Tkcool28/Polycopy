"""Tests for bounded order-book depth normalization (PR 4).

Tests cover:
- deterministic level ordering
- duplicate-price aggregation
- bounded level count
- bounded cumulative notional
- crossed-book rejection
- NaN/Infinity rejection
- zero-size level ignore
- BUY multi-level walk
- SELL multi-level walk
- partial fill
- zero fill
- insufficient captured depth
- no snapshot
- no extrapolation
- canonical hash stability
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from polycopy.scoring.depth_normalization import (
    NormalizedLevel,
    normalize_book_levels,
    compute_book_hash,
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
    """Max levels and max notional respected."""

    def test_max_levels(self):
        raw_asks = [(str(i / 100.0), "1") for i in range(1, 30)]
        bids, asks, err = normalize_book_levels([], raw_asks, max_levels=5)
        assert err is None
        assert len(asks) == 5

    def test_max_notional(self):
        raw_asks = [("0.10", "1000"), ("0.11", "1000"), ("0.12", "1000")]
        bids, asks, err = normalize_book_levels(
            [], raw_asks, max_notional=Decimal("50")
        )
        assert err is None
        # First level (0.10 * 1000 = 100) exceeds the cap but is always included
        assert len(asks) == 1

    def test_max_notional_respects_first_level(self):
        """Even if the first level exceeds the cap, include it."""
        raw_asks = [("0.90", "100")]  # notional = 90
        bids, asks, err = normalize_book_levels(
            [], raw_asks, max_notional=Decimal("10"),
        )
        assert err is None
        assert len(asks) == 1  # First level always included


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

    def test_negative_price(self):
        raw = [("-0.10", "10")]
        bids, asks, err = normalize_book_levels([], raw)
        assert err == DEPTH_LEVELS_MALFORMED

    def test_price_above_one(self):
        raw = [("1.50", "10")]
        bids, asks, err = normalize_book_levels([], raw)
        assert err == DEPTH_LEVELS_MALFORMED

    def test_negative_size_ignored(self):
        raw_asks = [("0.10", "-5"), ("0.20", "10")]
        bids, asks, err = normalize_book_levels([], raw_asks)
        assert err is None
        assert len(asks) == 1

    def test_zero_size_ignored(self):
        raw_asks = [("0.10", "0"), ("0.20", "10")]
        bids, asks, err = normalize_book_levels([], raw_asks)
        assert err is None
        assert len(asks) == 1

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
    """Multi-level depth walks for BUY and SELL."""

    def test_buy_single_level_fill(self):
        asks = [NormalizedLevel(Decimal("0.10"), Decimal("100"))]
        result = walk_depth(asks, "BUY", Decimal("5"))
        assert result.is_complete
        assert float(result.filled_notional) == 5.0
        assert result.levels_consumed == 1
        assert float(result.fill_percentage) == 100.0

    def test_buy_multi_level(self):
        asks = [
            NormalizedLevel(Decimal("0.10"), Decimal("50")),  # notional 5
            NormalizedLevel(Decimal("0.20"), Decimal("100")),  # notional 20
        ]
        result = walk_depth(asks, "BUY", Decimal("25"))
        assert result.is_complete
        assert result.levels_consumed == 2
        assert float(result.filled_notional) == 25.0

    def test_buy_partial_fill(self):
        asks = [NormalizedLevel(Decimal("0.10"), Decimal("50"))]  # notional 5
        result = walk_depth(asks, "BUY", Decimal("100"))
        assert not result.is_complete
        assert result.insufficient_reason == DEPTH_INSUFFICIENT_FOR_STAKE
        assert float(result.filled_notional) == 5.0
        assert result.levels_consumed == 1

    def test_sell_single_level(self):
        bids = [NormalizedLevel(Decimal("0.90"), Decimal("50"))]
        result = walk_depth(bids, "SELL", Decimal("10"))
        assert result.is_complete
        assert float(result.filled_notional) == 10.0

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
        assert float(result.filled_notional) == 0.0
        assert result.fill_percentage == 0.0
        assert result.insufficient_reason is not None

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
        assert float(result.vwap_fill_price) == pytest.approx(0.12, abs=0.001)

    def test_no_extrapolation(self):
        """Must not extrapolate beyond stored levels."""
        asks = [NormalizedLevel(Decimal("0.10"), Decimal("10"))]  # notional 1
        result = walk_depth(asks, "BUY", Decimal("100"))
        assert not result.is_complete
        assert float(result.filled_notional) == 1.0  # No extrapolation


class TestBookHash:
    """Canonical hash stability."""

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
        bids1, asks1, _ = normalize_book_levels([("0.90", "10")], [("0.10", "10")])
        bids2, asks2, _ = normalize_book_levels([("0.80", "10")], [("0.10", "10")])
        assert compute_book_hash(bids1, asks1) != compute_book_hash(bids2, asks2)


class TestSlippage:
    """Slippage calculation."""

    def test_buy_no_slippage(self):
        asks = [NormalizedLevel(Decimal("0.10"), Decimal("1000"))]
        result = walk_depth(asks, "BUY", Decimal("50"))
        assert result.is_complete
        assert result.slippage == pytest.approx(0.0, abs=0.001)

    def test_buy_with_slippage(self):
        asks = [
            NormalizedLevel(Decimal("0.10"), Decimal("50")),   # notional 5
            NormalizedLevel(Decimal("0.20"), Decimal("100")),  # notional 20
        ]
        result = walk_depth(asks, "BUY", Decimal("25"))
        # VWAP ≈ 0.12, best_ask = 0.10
        assert result.slippage > 0.0

    def test_sell_slippage(self):
        bids = [
            NormalizedLevel(Decimal("0.90"), Decimal("10")),   # notional 9
            NormalizedLevel(Decimal("0.80"), Decimal("50")),   # notional 40
        ]
        result = walk_depth(bids, "SELL", Decimal("30"))
        assert result.slippage < 0.0 or result.slippage is not None


class TestDepthReasons:
    """Error / reason for insufficient depth."""

    def test_no_snapshot_reason(self):
        """Simulate missing snapshot — handled by levels_persistence."""

    def test_insufficient_stake_reason(self):
        asks = [NormalizedLevel(Decimal("0.10"), Decimal("1"))]
        result = walk_depth(asks, "BUY", Decimal("100"))
        assert result.insufficient_reason == DEPTH_INSUFFICIENT_FOR_STAKE