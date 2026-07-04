"""PR #20 — unit tests for specialist-metric pure aggregation functions.

These tests assert the **documented** behavior from the audit report:
  * READY-NOW metrics (M1, M4, M9, M10) compute honestly.
  * PARTIAL metrics (M11 holding period) return NULL for <2 trades.
  * SHADOW metrics are strings only — never numeric.
  * BLOCKED metrics (M5/M6/M7/M8) are NOT in the output dict.
  * No fake zeros: missing-evidence → None, never 0.
"""

from __future__ import annotations

import json
import unittest

from polycopy.scoring.specialist_metrics import (
    aggregate_specialist_metrics,
    compute_active_trading_days,
    compute_category_concentration,
    compute_distinct_events,
    compute_distinct_markets,
    compute_holding_period_days,
    compute_market_resolution_state,
    compute_per_wallet_per_category_trade_count,
    compute_sample_reliability_score,
    group_trades_by_market,
)


def _trade(timestamp: str, market_source_id: str, *, is_sample: int = 0) -> dict:
    return {
        "timestamp": timestamp,
        "market_source_id": market_source_id,
        "is_sample": is_sample,
        "trader_address": "0xabc",
        "source_trade_id": f"poly:{market_source_id[:10]}:{timestamp}",
    }


class DistinctMarketsTests(unittest.TestCase):
    def test_returns_count(self):
        trades = [
            _trade("2026-07-01T00:00:00+00:00", "m1"),
            _trade("2026-07-01T00:01:00+00:00", "m2"),
            _trade("2026-07-01T00:02:00+00:00", "m1"),  # duplicate
        ]
        self.assertEqual(compute_distinct_markets(trades), 2)

    def test_empty_returns_none(self):
        self.assertIsNone(compute_distinct_markets([]))

    def test_missing_market_id_dropped(self):
        trades = [
            _trade("2026-07-01T00:00:00+00:00", "m1"),
            {"timestamp": "2026-07-01T00:00:00+00:00", "market_source_id": None},
            {"timestamp": "2026-07-01T00:00:00+00:00"},  # missing key
        ]
        self.assertEqual(compute_distinct_markets(trades), 1)

    def test_distinct_events_alias_matches(self):
        trades = [_trade("2026-07-01T00:00:00+00:00", "m1"), _trade("2026-07-02T00:00:00+00:00", "m2")]
        self.assertEqual(compute_distinct_events(trades), compute_distinct_markets(trades))


class ActiveTradingDaysTests(unittest.TestCase):
    def test_dedupes_by_utc_day(self):
        trades = [
            _trade("2026-07-01T01:00:00+00:00", "m1"),
            _trade("2026-07-01T23:59:00+00:00", "m2"),  # same UTC day
            _trade("2026-07-02T00:00:00+00:00", "m3"),
            _trade("2026-07-03T12:00:00+00:00", "m4"),
        ]
        self.assertEqual(compute_active_trading_days(trades), 3)

    def test_handles_naive_timestamp_as_utc(self):
        trades = [
            _trade("2026-07-01T01:00:00", "m1"),
            _trade("2026-07-02T01:00:00", "m2"),
        ]
        self.assertEqual(compute_active_trading_days(trades), 2)

    def test_empty_returns_none(self):
        self.assertIsNone(compute_active_trading_days([]))


class CategoryConcentrationTests(unittest.TestCase):
    def test_basic_ratio(self):
        self.assertEqual(compute_category_concentration(20, 100), 0.2)

    def test_returns_none_for_missing_inputs(self):
        self.assertIsNone(compute_category_concentration(None, 100))
        self.assertIsNone(compute_category_concentration(20, None))

    def test_returns_none_for_zero_overall(self):
        self.assertIsNone(compute_category_concentration(0, 0))
        self.assertIsNone(compute_category_concentration(5, 0))

    def test_never_exceeds_one(self):
        # Defensive: if a bug ever produces category > overall, we
        # still return the honest ratio (caller may detect >1).
        ratio = compute_category_concentration(150, 100)
        self.assertEqual(ratio, 1.5)  # not silently clamped


class SampleReliabilityTests(unittest.TestCase):
    def test_all_real(self):
        trades = [_trade("2026-07-01T00:00:00+00:00", "m1", is_sample=0) for _ in range(5)]
        self.assertEqual(compute_sample_reliability_score(trades), 1.0)

    def test_all_sample(self):
        trades = [_trade("2026-07-01T00:00:00+00:00", "m1", is_sample=1) for _ in range(5)]
        self.assertEqual(compute_sample_reliability_score(trades), 0.0)

    def test_mixed(self):
        trades = [
            _trade("2026-07-01T00:00:00+00:00", "m1", is_sample=0),
            _trade("2026-07-01T00:01:00+00:00", "m1", is_sample=1),
            _trade("2026-07-01T00:02:00+00:00", "m1", is_sample=0),
            _trade("2026-07-01T00:03:00+00:00", "m1", is_sample=0),
        ]
        self.assertEqual(compute_sample_reliability_score(trades), 0.75)

    def test_empty_returns_none(self):
        self.assertIsNone(compute_sample_reliability_score([]))


class HoldingPeriodTests(unittest.TestCase):
    def test_span_in_days(self):
        trades = [
            _trade("2026-07-01T00:00:00+00:00", "m1"),
            _trade("2026-07-08T00:00:00+00:00", "m1"),
        ]
        self.assertEqual(compute_holding_period_days(trades), 7)

    def test_returns_none_for_single_trade(self):
        trades = [_trade("2026-07-01T00:00:00+00:00", "m1")]
        self.assertIsNone(compute_holding_period_days(trades))

    def test_returns_none_for_zero_trades(self):
        self.assertIsNone(compute_holding_period_days([]))

    def test_returns_zero_for_same_day(self):
        trades = [
            _trade("2026-07-01T00:00:00+00:00", "m1"),
            _trade("2026-07-01T23:59:00+00:00", "m2"),
        ]
        self.assertEqual(compute_holding_period_days(trades), 0)


class PerWalletPerCategoryTradeCountTests(unittest.TestCase):
    def test_counts_all_trades(self):
        trades = [_trade("2026-07-01T00:00:00+00:00", "m1") for _ in range(7)]
        self.assertEqual(compute_per_wallet_per_category_trade_count(trades), 7)


class MarketResolutionStateTests(unittest.TestCase):
    def test_resolved(self):
        market = {"resolved": 1, "resolution_outcome": "Yes", "closed": 0, "active": 1}
        self.assertEqual(compute_market_resolution_state(market), "resolved")

    def test_closed_unresolved(self):
        market = {"resolved": 0, "resolution_outcome": None, "closed": 1, "active": 0}
        self.assertEqual(compute_market_resolution_state(market), "closed_unresolved")

    def test_active(self):
        market = {"resolved": 0, "resolution_outcome": None, "closed": 0, "active": 1}
        self.assertEqual(compute_market_resolution_state(market), "active")

    def test_unknown(self):
        self.assertEqual(compute_market_resolution_state(None), "unknown")
        self.assertEqual(compute_market_resolution_state({}), "unknown")


class GroupByMarketTests(unittest.TestCase):
    def test_groups_correctly(self):
        trades = [
            _trade("2026-07-01T00:00:00+00:00", "m1"),
            _trade("2026-07-01T00:01:00+00:00", "m2"),
            _trade("2026-07-01T00:02:00+00:00", "m1"),
        ]
        grouped = group_trades_by_market(trades)
        self.assertEqual(set(grouped.keys()), {"m1", "m2"})
        self.assertEqual(len(grouped["m1"]), 2)
        self.assertEqual(len(grouped["m2"]), 1)

    def test_drops_missing_market_id(self):
        trades = [
            _trade("2026-07-01T00:00:00+00:00", "m1"),
            {"timestamp": "2026-07-01T00:00:00+00:00"},  # no market_source_id
            _trade("2026-07-01T00:01:00+00:00", ""),  # empty string
        ]
        grouped = group_trades_by_market(trades)
        self.assertEqual(list(grouped.keys()), ["m1"])


class AggregateSpecialistMetricsTests(unittest.TestCase):
    """Higher-level tests asserting the audit-mandated behavior."""

    def _bundle(self, n_trades: int = 10, *, n_markets: int = 2,
                include_category: bool = True,
                is_sample_frac: float = 0.0,
                category_label: str = "us-politics") -> tuple[list[dict], list[dict]]:
        """Return (all_trades, category_trades)."""
        all_trades = []
        category_trades = []
        for i in range(n_trades):
            market = f"market-{i % n_markets}"
            # First n_markets are in the named category; rest are elsewhere.
            in_category = include_category and (i % n_markets == 0)
            trade = _trade(
                f"2026-07-{(i % 30) + 1:02d}T00:00:00+00:00",
                market,
                is_sample=(1 if (i / max(1, n_trades)) < is_sample_frac else 0),
            )
            all_trades.append(trade)
            if in_category:
                category_trades.append(trade)
        return all_trades, category_trades

    def test_full_bundle_returns_observed_quality(self):
        all_trades, category_trades = self._bundle(n_trades=10, n_markets=2)
        out = aggregate_specialist_metrics(
            wallet_id="w1",
            category_label="us-politics",
            all_trades_for_wallet=all_trades,
            category_trades_for_wallet=category_trades,
        )
        self.assertEqual(out["trade_count"], 10)
        self.assertEqual(out["distinct_markets"], 2)
        self.assertEqual(out["distinct_events"], 2)
        self.assertGreaterEqual(out["active_trading_days"], 1)
        self.assertEqual(out["category_trade_count"], 5)
        self.assertEqual(out["category_distinct_markets"], 1)
        self.assertEqual(out["category_concentration"], 0.5)
        self.assertEqual(out["sample_reliability_score"], 1.0)
        # Blocked metrics MUST appear in missing_essentials_json.
        for blocked in ("resolved_markets", "win_rate_realized", "realized_pnl",
                        "profit_factor", "max_drawdown"):
            self.assertIn(blocked, out["missing_essentials_json"])
        # quality='partial' because missing essentials, but trade_count present.
        self.assertEqual(out["quality"], "partial")
        # SHADOW fields are strings only.
        self.assertEqual(out["behavior_classification"], "unknown")
        self.assertEqual(out["copyability_evidence_state"], "unknown")
        self.assertEqual(out["price_improvement_state"], "unknown")

    def test_no_category_yields_null_for_category_fields(self):
        all_trades, _ = self._bundle(n_trades=5, n_markets=1, include_category=False)
        out = aggregate_specialist_metrics(
            wallet_id="w1",
            category_label=None,  # no category resolvable
            all_trades_for_wallet=all_trades,
            category_trades_for_wallet=[],
        )
        self.assertEqual(out["trade_count"], 5)
        # category_trade_count is the literal length of
        # category_trades_for_wallet — empty by design when no
        # category resolves. That's an honest zero, not a fake
        # default; we mark it as missing-essential instead.
        self.assertEqual(out["category_trade_count"], 0)
        self.assertIsNone(out["category_distinct_markets"])
        self.assertIsNone(out["category_active_days"])
        self.assertIsNone(out["category_concentration"])
        self.assertIn("category_label", out["missing_essentials_json"])

    def test_empty_trades_returns_incomplete(self):
        out = aggregate_specialist_metrics(
            wallet_id="w1",
            category_label="x",
            all_trades_for_wallet=[],
            category_trades_for_wallet=[],
        )
        self.assertEqual(out["trade_count"], 0)
        self.assertIsNone(out["distinct_markets"])
        self.assertIsNone(out["distinct_events"])
        self.assertIsNone(out["active_trading_days"])
        self.assertIsNone(out["sample_reliability_score"])
        self.assertIsNone(out["holding_period_days"])
        self.assertIn("trade_count", out["missing_essentials_json"])
        self.assertEqual(out["quality"], "incomplete")

    def test_blocked_metrics_absent_from_dict(self):
        """The most important audit contract: blocked metrics MUST NOT
        appear as keys in the output dict, even with a 0 default."""
        all_trades, _ = self._bundle(n_trades=3, n_markets=1, include_category=False)
        out = aggregate_specialist_metrics(
            wallet_id="w1",
            category_label=None,
            all_trades_for_wallet=all_trades,
            category_trades_for_wallet=[],
        )
        for blocked in (
            "win_rate_realized",
            "realized_pnl",
            "profit_factor",
            "max_drawdown",
            "resolved_markets",
        ):
            self.assertNotIn(
                blocked,
                out,
                f"BLOCKED metric {blocked!r} must not appear in output dict",
            )
            self.assertNotIn(
                blocked,
                out["component_scores_json"],
                f"BLOCKED metric {blocked!r} must not appear in component_scores_json",
            )

    def test_component_scores_json_round_trips(self):
        all_trades, category_trades = self._bundle(n_trades=6, n_markets=2)
        out = aggregate_specialist_metrics(
            wallet_id="w1",
            category_label="x",
            all_trades_for_wallet=all_trades,
            category_trades_for_wallet=category_trades,
        )
        # JSON-serializable (the persistence layer requires this).
        encoded = json.dumps(out["component_scores_json"], sort_keys=True)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["wallet_id"], "w1")
        self.assertEqual(decoded["category_label"], "x")
        self.assertEqual(decoded["trade_count"], 6)

    def test_no_fake_zeros(self):
        """No metric that *cannot* be derived from an empty bundle
        may be substituted with a 0. Counts of zero are honest
        zeros (the wallet has no trades); aggregate or qualitative
        metrics must be ``None``."""
        out = aggregate_specialist_metrics(
            wallet_id="w1",
            category_label="x",
            all_trades_for_wallet=[],
            category_trades_for_wallet=[],
        )
        # Honest zero: the wallet literally has zero trades.
        self.assertEqual(out["trade_count"], 0)
        # Everything else that depends on having trades is None.
        for k, v in out.items():
            if k in (
                "wallet_id",
                "category_label",
                "trade_count",  # honest zero above
                "category_trade_count",  # honest zero: subset is empty by design
                "component_scores_json",
                "quality",
                "missing_essentials_json",
                "behavior_classification",
                "copyability_evidence_state",
                "price_improvement_state",
            ):
                continue
            if isinstance(v, (int, float)):
                self.assertIsNone(
                    v,
                    f"key {k!r} must be None for empty-wallet bundle, got {v!r}",
                )


if __name__ == "__main__":
    unittest.main()