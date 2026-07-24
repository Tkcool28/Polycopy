"""Tests for PR 4 wallet behavior classification (Phase 12 / Chunk 3).

Covers:
- classifier transparent thresholds (constants)
- classifier decision matrix:
  - directional (with positive evidence)
  - insufficient history → UNKNOWN
  - clear two-sided market maker
  - isolated buy/sell pair not enough for market maker
  - sustained HFT wallet
  - one rapid interval not enough for HFT
  - clear multi-leg arbitrage
  - ordinary diversified directional wallet not arbitrage
  - conflicting directional and market-making evidence → MIXED
  - high market count with positive directional evidence remains DIRECTIONAL
- high market count without positive directional evidence remains UNKNOWN
  - malformed timestamps excluded
  - anonymous/sentinel rows excluded
  - sample/non-sample separation
  - behavior reasons deterministic
  - behavior verdict caps correct

Tests use:
- direct unit tests against the pure classifier
- a BehaviorEvidenceLoader that operates on in-memory
  source_trades-shaped rows (no Database)
- end-to-end tests that go through a real Database with
  persisted rows
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from polycopy.scoring.behavior_classification import (
    ARB_MIN_DISTINCT_MARKETS,
    ARB_MULTI_MARKET_WINDOW_SECONDS,
    ARB_SAME_MARKET_WINDOW_SECONDS,
    DOMINANT_SIDE_FRACTION,
    DOMINANT_SIDE_MIN_TRADES,
    HFT_AVG_INTERVAL_SECONDS,
    HFT_MIN_TRADE_COUNT,
    MIN_TRADES_FOR_CLASSIFICATION,
    MIXED_CONFLICT_DOMINANT,
    MIXED_CONFLICT_TWO_SIDED,
    TWO_SIDED_AVG_TRADES_PER_MARKET,
    TWO_SIDED_MARKET_MIN,
    TWO_SIDED_MARKET_SHARE,
    BehaviorClassification,
    BehaviorEvidence,
    classify_wallet_behavior,
    load_behavior_evidence_from_rows,
)


# ---- Helpers ------------------------------------------------------------


def _row(
    *,
    address: str = "0xaaa1111111111111111111111111111111111111",
    side: str = "BUY",
    market_source_id: str = "m1",
    outcome: str = "Yes",
    ts: str = "2026-01-01T00:00:00Z",
    is_sample: int = 0,
) -> dict:
    return {
        "trader_address": address,
        "side": side,
        "market_source_id": market_source_id,
        "outcome": outcome,
        "timestamp": ts,
        "is_sample": is_sample,
    }


def _base_time() -> datetime:
    return datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _ts(offset_seconds: int) -> str:
    return (_base_time() + timedelta(seconds=offset_seconds)).isoformat().replace(
        "+00:00", "Z"
    )


# ---- 1. Transparent threshold constants --------------------------------


class TestTransparentThresholds:
    def test_min_trades_constant(self) -> None:
        assert MIN_TRADES_FOR_CLASSIFICATION == 5

    def test_hft_constants(self) -> None:
        assert HFT_AVG_INTERVAL_SECONDS == 10.0
        assert HFT_MIN_TRADE_COUNT == 50

    def test_market_maker_constants(self) -> None:
        assert TWO_SIDED_MARKET_SHARE == 0.5
        assert TWO_SIDED_MARKET_MIN == 3
        assert TWO_SIDED_AVG_TRADES_PER_MARKET == 4

    def test_arb_constants(self) -> None:
        assert ARB_SAME_MARKET_WINDOW_SECONDS == 60
        assert ARB_MULTI_MARKET_WINDOW_SECONDS == 60
        assert ARB_MIN_DISTINCT_MARKETS == 3

    def test_dominant_side_constants(self) -> None:
        assert DOMINANT_SIDE_FRACTION == 0.80
        assert DOMINANT_SIDE_MIN_TRADES == 3

    def test_mixed_constants(self) -> None:
        assert MIXED_CONFLICT_TWO_SIDED == 2
        assert MIXED_CONFLICT_DOMINANT == 2


# ---- 2. Direct classifier unit tests -----------------------------------


class TestDirectClassifier:
    def test_insufficient_trades_is_unknown(self) -> None:
        result = classify_wallet_behavior(BehaviorEvidence(trade_count=4))
        assert result.classification == BehaviorClassification.UNKNOWN
        assert result.is_watchlist_cap is True
        assert result.is_skip is False

    def test_no_trade_count_is_unknown(self) -> None:
        result = classify_wallet_behavior(BehaviorEvidence())
        assert result.classification == BehaviorClassification.UNKNOWN

    def test_clear_directional_with_positive_evidence(self) -> None:
        result = classify_wallet_behavior(BehaviorEvidence(
            trade_count=50,
            avg_time_between_trades_seconds=1000.0,
            distinct_markets_traded=10,
            two_sided_market_count=0,
            dominant_side_market_count=5,
        ))
        assert result.classification == BehaviorClassification.DIRECTIONAL
        assert result.is_eligible_for_copy is True
        assert result.is_watchlist_cap is False
        assert result.is_skip is False

    def test_high_frequency_threshold(self) -> None:
        # avg_time=5s, trade_count=100 → HFT
        result = classify_wallet_behavior(BehaviorEvidence(
            trade_count=100,
            avg_time_between_trades_seconds=5.0,
            distinct_markets_traded=5,
        ))
        assert result.classification == BehaviorClassification.HIGH_FREQUENCY_BOT
        assert result.is_skip is True

    def test_one_rapid_interval_not_hft(self) -> None:
        # avg_time=5s, trade_count=10 → NOT HFT (need 50+ trades)
        result = classify_wallet_behavior(BehaviorEvidence(
            trade_count=10,
            avg_time_between_trades_seconds=5.0,
            distinct_markets_traded=5,
            dominant_side_market_count=3,
        ))
        assert result.classification != BehaviorClassification.HIGH_FREQUENCY_BOT

    def test_hft_count_threshold(self) -> None:
        # avg_time=5s, trade_count=49 → NOT HFT (need 50+)
        result = classify_wallet_behavior(BehaviorEvidence(
            trade_count=49,
            avg_time_between_trades_seconds=5.0,
            distinct_markets_traded=5,
        ))
        assert result.classification != BehaviorClassification.HIGH_FREQUENCY_BOT

    def test_market_maker_two_sided(self) -> None:
        result = classify_wallet_behavior(BehaviorEvidence(
            trade_count=80,
            avg_time_between_trades_seconds=300.0,
            distinct_markets_traded=8,
            two_sided_market_count=5,
            dominant_side_market_count=0,
        ))
        assert result.classification == BehaviorClassification.MARKET_MAKER_LP
        assert result.is_skip is True

    def test_isolated_buy_sell_pair_not_enough_for_mm(self) -> None:
        # 1 two-sided market (with min 3 required) → not MM
        result = classify_wallet_behavior(BehaviorEvidence(
            trade_count=20,
            avg_time_between_trades_seconds=600.0,
            distinct_markets_traded=10,
            two_sided_market_count=1,
            dominant_side_market_count=5,
        ))
        assert result.classification != BehaviorClassification.MARKET_MAKER_LP

    def test_arbitrage_via_opposing_outcome(self) -> None:
        result = classify_wallet_behavior(BehaviorEvidence(
            trade_count=30,
            avg_time_between_trades_seconds=200.0,
            distinct_markets_traded=5,
            two_sided_market_count=0,
            dominant_side_market_count=0,
            opposing_outcome_event_count=2,
        ))
        assert result.classification == BehaviorClassification.ARBITRAGE_MULTI_LEG
        assert result.is_skip is True

    def test_arbitrage_via_multi_market_burst(self) -> None:
        result = classify_wallet_behavior(BehaviorEvidence(
            trade_count=30,
            avg_time_between_trades_seconds=200.0,
            distinct_markets_traded=10,
            two_sided_market_count=0,
            dominant_side_market_count=0,
            multi_market_burst_count=1,
        ))
        assert result.classification == BehaviorClassification.ARBITRAGE_MULTI_LEG

    def test_mixed_due_to_conflict(self) -> None:
        # 2+ two-sided markets AND 2+ dominant markets → MIXED
        result = classify_wallet_behavior(BehaviorEvidence(
            trade_count=100,
            avg_time_between_trades_seconds=200.0,
            distinct_markets_traded=15,
            two_sided_market_count=3,
            dominant_side_market_count=4,
        ))
        assert result.classification == BehaviorClassification.MIXED
        assert result.is_watchlist_cap is True

    def test_high_market_count_without_directional_evidence_is_unknown(self) -> None:
        result = classify_wallet_behavior(BehaviorEvidence(
            trade_count=200,
            avg_time_between_trades_seconds=300.0,
            distinct_markets_traded=25,
            two_sided_market_count=0,
            dominant_side_market_count=0,
        ))
        assert result.classification == BehaviorClassification.UNKNOWN
        assert result.is_watchlist_cap is True

    def test_directional_no_silent_default(self) -> None:
        """A wallet with trades but no dominant market is UNKNOWN,
        not silently DIRECTIONAL. This is the spec rule from
        Phase 12."""
        result = classify_wallet_behavior(BehaviorEvidence(
            trade_count=30,
            avg_time_between_trades_seconds=400.0,
            distinct_markets_traded=8,
            two_sided_market_count=0,
            dominant_side_market_count=0,
        ))
        assert result.classification == BehaviorClassification.UNKNOWN
        assert result.is_watchlist_cap is True

    def test_reasons_are_deterministic(self) -> None:
        ev = BehaviorEvidence(
            trade_count=50,
            avg_time_between_trades_seconds=5.0,
            distinct_markets_traded=5,
        )
        r1 = classify_wallet_behavior(ev)
        r2 = classify_wallet_behavior(ev)
        assert r1.reasons == r2.reasons

    def test_verdict_caps_correct(self) -> None:
        # DIRECTIONAL: no cap
        d = classify_wallet_behavior(BehaviorEvidence(
            trade_count=50, distinct_markets_traded=5,
            dominant_side_market_count=3,
        ))
        assert d.verdict_cap is None
        # MM: skip
        mm = classify_wallet_behavior(BehaviorEvidence(
            trade_count=50, distinct_markets_traded=5,
            two_sided_market_count=4,
        ))
        assert mm.verdict_cap == "skip"
        # MIXED: watchlist
        mx = classify_wallet_behavior(BehaviorEvidence(
            trade_count=200, distinct_markets_traded=25,
        ))
        assert mx.verdict_cap == "watchlist"
        # UNKNOWN: watchlist
        u = classify_wallet_behavior(BehaviorEvidence(trade_count=2))
        assert u.verdict_cap == "watchlist"


# ---- 3. Loader: directional wallet --------------------------------------


def _directional_rows(n: int = 30) -> list[dict]:
    """Trades 30 times in 5 different markets, predominantly BUY."""
    rows = []
    markets = [f"m{i}" for i in range(5)]
    for i in range(n):
        market = markets[i % 5]
        side = "BUY" if i % 5 != 0 else "SELL"  # Mostly BUY
        rows.append(_row(
            market_source_id=market,
            side=side,
            outcome="Yes",
            ts=_ts(i * 3600),
        ))
    return rows


class TestLoaderDirectional:
    def test_clear_directional_loads_as_directional(self) -> None:
        ev = load_behavior_evidence_from_rows(_directional_rows(30))
        result = classify_wallet_behavior(ev)
        assert result.classification == BehaviorClassification.DIRECTIONAL
        assert ev.trade_count == 30
        assert ev.distinct_markets_traded == 5
        assert ev.dominant_side_market_count >= 3


# ---- 4. Loader: insufficient history → UNKNOWN --------------------------


class TestLoaderInsufficientHistory:
    def test_few_trades_unknown(self) -> None:
        ev = load_behavior_evidence_from_rows(_directional_rows(3))
        result = classify_wallet_behavior(ev)
        assert result.classification == BehaviorClassification.UNKNOWN

    def test_no_trades_unknown(self) -> None:
        ev = load_behavior_evidence_from_rows([])
        result = classify_wallet_behavior(ev)
        assert result.classification == BehaviorClassification.UNKNOWN


# ---- 5. Loader: market maker --------------------------------------------


def _market_maker_rows(n: int = 60) -> list[dict]:
    """Trades 60 times in 4 markets, BUY and SELL on each market."""
    rows = []
    markets = [f"mm{i}" for i in range(4)]
    for i in range(n):
        market = markets[i % 4]
        # For each market, alternate sides deterministically.
        # i%4 picks market, but the side needs to vary within a
        # market. Use a (market, side) cycle based on (i // 4) % 2.
        side = "BUY" if (i // 4) % 2 == 0 else "SELL"
        rows.append(_row(
            market_source_id=market, side=side, outcome="Yes",
            ts=_ts(i * 600),
        ))
    return rows


class TestLoaderMarketMaker:
    def test_two_sided_markets_detected(self) -> None:
        ev = load_behavior_evidence_from_rows(_market_maker_rows(60))
        assert ev.two_sided_market_count == 4
        result = classify_wallet_behavior(ev)
        assert result.classification == BehaviorClassification.MARKET_MAKER_LP


# ---- 6. Loader: HFT ------------------------------------------------------


def _hft_rows(n: int = 100) -> list[dict]:
    """100 trades 1 second apart in a few markets."""
    rows = []
    for i in range(n):
        rows.append(_row(
            market_source_id=f"hf{i % 3}", side="BUY", outcome="Yes",
            ts=_ts(i),
        ))
    return rows


class TestLoaderHighFrequency:
    def test_hft_classified(self) -> None:
        ev = load_behavior_evidence_from_rows(_hft_rows(100))
        result = classify_wallet_behavior(ev)
        assert result.classification == BehaviorClassification.HIGH_FREQUENCY_BOT

    def test_one_rapid_interval_not_hft(self) -> None:
        # 10 trades 1s apart, then nothing — total trade_count=10
        rows = _hft_rows(10)
        ev = load_behavior_evidence_from_rows(rows)
        result = classify_wallet_behavior(ev)
        # 10 < 50, so not HFT. And there's no dominant market
        # so the result is UNKNOWN.
        assert result.classification != BehaviorClassification.HIGH_FREQUENCY_BOT


# ---- 7. Loader: arbitrage -----------------------------------------------


def _arb_opposing_outcome_rows() -> list[dict]:
    """Within a 30s window, buy Yes and buy No on the same market."""
    return [
        _row(market_source_id="mA", side="BUY", outcome="Yes", ts=_ts(0)),
        _row(market_source_id="mA", side="BUY", outcome="No", ts=_ts(20)),
    ] * 10  # repeated for trade_count


def _arb_multi_market_burst_rows() -> list[dict]:
    """Within a 30s window, trade in 3 different markets."""
    rows = []
    for i in range(30):
        rows.append(_row(
            market_source_id=f"m{i % 3}", side="BUY", outcome="Yes",
            ts=_ts(i * 5),
        ))
    return rows


class TestLoaderArbitrage:
    def test_opposing_outcome_classified_as_arb(self) -> None:
        rows = _arb_opposing_outcome_rows()
        ev = load_behavior_evidence_from_rows(rows)
        assert ev.opposing_outcome_event_count > 0
        result = classify_wallet_behavior(ev)
        assert result.classification == BehaviorClassification.ARBITRAGE_MULTI_LEG

    def test_multi_market_burst_classified_as_arb(self) -> None:
        ev = load_behavior_evidence_from_rows(_arb_multi_market_burst_rows())
        assert ev.multi_market_burst_count > 0
        result = classify_wallet_behavior(ev)
        assert result.classification == BehaviorClassification.ARBITRAGE_MULTI_LEG


# ---- 8. Loader: ordinary diversified directional wallet -----------------


class TestLoaderDiversifiedDirectional:
    def test_directional_in_many_markets(self) -> None:
        # 50 trades across 8 distinct markets, strongly one-sided
        # per market. No market has BOTH sides — so this cannot
        # be a market maker. Result must be DIRECTIONAL.
        rows = []
        for i in range(50):
            market = f"div-{i % 8}"  # 8 distinct markets
            side = "BUY"  # always BUY (no two-sided markets)
            rows.append(_row(
                market_source_id=market, side=side, outcome="Yes",
                ts=_ts(i * 3600),
            ))
        ev = load_behavior_evidence_from_rows(rows)
        result = classify_wallet_behavior(ev)
        # Should be DIRECTIONAL (has dominant markets), not ARB,
        # not MM, not MIXED (8 markets < 20).
        assert result.classification == BehaviorClassification.DIRECTIONAL
        assert ev.distinct_markets_traded == 8
        assert ev.two_sided_market_count == 0
        assert ev.dominant_side_market_count == 8


# ---- 9. Loader: conflicting patterns → MIXED ---------------------------


class TestLoaderMixedConflict:
    def test_directional_and_mm_mixed(self) -> None:
        # 6 markets: 3 are slightly two-sided (below the MM
        # threshold) AND 3 are strongly dominant. The
        # combination of having both two-sided markets AND
        # dominant markets triggers MIXED (genuine conflict).
        # The two-sided markets do NOT trip the MM rule because
        # their trade density is too low to qualify as MM
        # (avg trades/market overall falls below 4 for the
        # 2-sided subset, and the share threshold needs the
        # two-sided markets to be the majority).
        rows = []
        # Slightly two-sided markets: mmA, mmB, mmC
        # 2 trades each, both sides. Total 6 trades.
        for i in range(6):
            market = f"mm-{i % 3}"
            side = "BUY" if i % 2 == 0 else "SELL"
            rows.append(_row(
                market_source_id=market, side=side, outcome="Yes",
                ts=_ts(i * 1800),
            ))
        # Dominant markets: dirA, dirB, dirC — 5 BUYs each, 0 SELLs
        for i in range(15):
            market = f"dir-{i % 3}"
            side = "BUY"
            rows.append(_row(
                market_source_id=market, side=side, outcome="Yes",
                ts=_ts(36000 + i * 1800),
            ))
        ev = load_behavior_evidence_from_rows(rows)
        # Sanity: should have 3 two-sided markets and 3 dominant.
        assert ev.two_sided_market_count == 3
        assert ev.dominant_side_market_count == 3
        # But the share of two-sided is 3/6 = 0.5, which
        # EXACTLY meets the threshold. avg_trades/market = 21/6
        # = 3.5, which is BELOW 4. So MM should not fire, and
        # we should land at the MIXED conflict branch.
        result = classify_wallet_behavior(ev)
        assert result.classification == BehaviorClassification.MIXED


# ---- 10. Loader: malformed timestamps -----------------------------------


class TestLoaderMalformedTimestamps:
    def test_malformed_timestamp_excluded(self) -> None:
        rows = _directional_rows(20)
        # Replace half with malformed timestamps.
        for i in range(0, 20, 2):
            rows[i]["timestamp"] = "not-a-timestamp"
        ev = load_behavior_evidence_from_rows(rows)
        # 10 valid rows.
        assert ev.trade_count == 10
        result = classify_wallet_behavior(ev)
        # 10 trades, not 30, so no dominant market, so UNKNOWN.
        assert result.classification == BehaviorClassification.UNKNOWN

    def test_empty_timestamp_excluded(self) -> None:
        rows = _directional_rows(20)
        for i in range(0, 20, 2):
            rows[i]["timestamp"] = ""
        ev = load_behavior_evidence_from_rows(rows)
        assert ev.trade_count == 10


# ---- 11. Loader: anonymous / sentinel rows excluded --------------------


class TestLoaderSentinelsExcluded:
    def test_anonymous_excluded(self) -> None:
        rows = _directional_rows(20)
        for r in rows:
            r["trader_address"] = "anonymous"
        ev = load_behavior_evidence_from_rows(rows)
        assert ev.trade_count == 0

    def test_unknown_excluded(self) -> None:
        rows = _directional_rows(20)
        for r in rows:
            r["trader_address"] = "unknown"
        ev = load_behavior_evidence_from_rows(rows)
        assert ev.trade_count == 0

    def test_missing_excluded(self) -> None:
        rows = _directional_rows(20)
        for r in rows:
            r["trader_address"] = "missing"
        ev = load_behavior_evidence_from_rows(rows)
        assert ev.trade_count == 0

    def test_zero_address_excluded(self) -> None:
        rows = _directional_rows(20)
        for r in rows:
            r["trader_address"] = "0x0"
        ev = load_behavior_evidence_from_rows(rows)
        assert ev.trade_count == 0


# ---- 12. Sample / non-sample separation --------------------------------


class TestLoaderSampleSeparation:
    def test_sample_rows_excluded_by_default(self) -> None:
        rows = _directional_rows(20)
        for r in rows:
            r["is_sample"] = 1
        ev = load_behavior_evidence_from_rows(rows)
        assert ev.trade_count == 0

    def test_sample_rows_included_when_requested(self) -> None:
        rows = _directional_rows(20)
        for r in rows:
            r["is_sample"] = 1
        ev = load_behavior_evidence_from_rows(rows, include_sample=True)
        assert ev.trade_count == 20

    def test_mixed_real_and_sample(self) -> None:
        rows = _directional_rows(20)
        # First 10 are real, rest are sample.
        for i in range(10, 20):
            rows[i]["is_sample"] = 1
        ev_default = load_behavior_evidence_from_rows(rows)
        ev_inclusive = load_behavior_evidence_from_rows(
            rows, include_sample=True
        )
        assert ev_default.trade_count == 10
        assert ev_inclusive.trade_count == 20


# ---- 13. End-to-end via Database ---------------------------------------


class TestEndToEndDatabaseLoader:
    """Wires the loader through a real Database with persisted
    source_trades to prove the integration works."""

    def test_persisted_directional_wallet(self, tmp_path: Path) -> None:
        from polycopy.db.database import Database
        db = Database(db_path=tmp_path / "be.db").connect()
        try:
            wallet_id = "0xW" + uuid4().hex[:10]
            db.conn.execute(
                "INSERT INTO wallets (id, address, label, is_sample, "
                "created_at, canonical_address) VALUES "
                "(?, ?, 'w', 0, ?, ?)",
                (wallet_id, wallet_id.lower(),
                 "2026-01-01T00:00:00Z", wallet_id.lower()),
            )
            db.conn.commit()

            for r in _directional_rows(30):
                db.conn.execute(
                    "INSERT INTO source_trades (id, source, "
                    "source_trade_id, market_source_id, side, outcome, "
                    "quantity, price, trader_address, timestamp, "
                    "is_sample) VALUES (?, 'polymarket_data_api', ?, "
                    "?, ?, ?, 1.0, 0.5, ?, ?, ?)",
                    (uuid4().hex, uuid4().hex,
                     r["market_source_id"], r["side"], r["outcome"],
                     wallet_id.lower(), r["timestamp"], r["is_sample"]),
                )
            db.conn.commit()

            from polycopy.scoring.behavior_classification import (
                load_behavior_evidence,
            )
            ev = load_behavior_evidence(db, wallet_id)
            result = classify_wallet_behavior(ev)
            assert result.classification == BehaviorClassification.DIRECTIONAL
        finally:
            db.close()
