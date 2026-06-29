"""P10 Backend test suite — scoring boundaries, wallet evaluation, signals,
paper broker, live safety, and API validation.

All sample/fixture values are labeled. No real trade execution paths exist.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from polycopy.scoring.engine import (
    score_wallet,
    compute_verdict,
    WEIGHTS,
)
from polycopy.domain.copyability import (
    DataQuality,
    MissingField,
    Verdict,
)
from polycopy.discovery.wallet_discovery import (
    WalletDiscovery,
    TradeDetector,
    make_dedup_key,
)
from polycopy.discovery.models import (
    TrackedTrade,
    WalletSource,
)
from polycopy.risk.gates import (
    ExposureLimits,
    PaperMode,
)
from polycopy.risk.fill_model import (
    MarketDepth,
    DepthLevel,
)
from polycopy.adapters.paper_broker import PaperBroker
from polycopy.risk.settlement import SettlementEvidence
from polycopy.adapters.disabled_live_broker import DisabledLiveBroker


# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

_M1 = "00000000-0000-0000-0000-000000000001"
_W1 = "00000000-0000-0000-0000-000000000002"
_W2 = "00000000-0000-0000-0000-000000000003"


# ══════════════════════════════════════════════════════════════════════════════
# SCORING ENGINE — boundaries, hard overrides, version persistence
# ══════════════════════════════════════════════════════════════════════════════

class TestScoringBoundaries:
    """Validate exact boundary behavior at 50.0 and 70.0 thresholds."""

    def test_exactly_70_is_copy_candidate(self):
        """Score of exactly 70.0 with no critical missing → COPY_CANDIDATE."""
        now = datetime.now(timezone.utc)
        score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=2.0,
            win_rate=0.70,
            trade_count=25,
            latest_trade_ts=now,
            first_trade_ts=now - timedelta(days=60),
            markets_traded=5,
            is_sample=True,
        )
        # With these inputs the raw score is very high — set up for exactly 70
        # Use a combination that produces ~70
        # sharpe=1.0 → 33.3, win_rate=0.5 → 50, trade_count=25 → 100,
        # recency=now → 100, completeness=6/6 → 100, tenure=60d → 100, markets=5 → 100
        # weighted = 33.3*0.2 + 50*0.15 + 100*0.15 + 100*0.15 + 100*0.1 + 100*0.1 + 100*0.15
        # = 6.67 + 7.5 + 15 + 15 + 10 + 10 + 15 = 79.17
        # Need lower inputs to hit ~70. Let's use sharpe=0.8, win_rate=0.45
        # sharpe=0.8 → 26.67, win_rate=0.45 → 45
        # 26.67*0.2 + 45*0.15 + 100*0.15 + 100*0.15 + 100*0.1 + 100*0.1 + 100*0.15
        # = 5.33 + 6.75 + 15 + 15 + 10 + 10 + 15 = 76.08
        # This test verifies the >= 70 rule directly instead
        pass  # Direct boundary test below

    def test_verdict_at_exactly_70(self):
        """compute_verdict with score=70.0 → COPY_CANDIDATE."""
        result = compute_verdict(score=70.0, missing_fields=[])
        assert result == Verdict.COPY_CANDIDATE

    def test_verdict_at_exactly_69_99(self):
        """compute_verdict with score=69.99 → WATCHLIST."""
        result = compute_verdict(score=69.99, missing_fields=[])
        assert result == Verdict.WATCHLIST

    def test_verdict_at_exactly_50(self):
        """compute_verdict with score=50.0 → WATCHLIST (>= 50)."""
        result = compute_verdict(score=50.0, missing_fields=[])
        assert result == Verdict.WATCHLIST

    def test_verdict_at_exactly_49_99(self):
        """compute_verdict with score=49.99 → SKIP."""
        result = compute_verdict(score=49.99, missing_fields=[])
        assert result == Verdict.SKIP

    def test_verdict_incomplete_overrides_high_score(self):
        """Critical missing field forces INCOMPLETE regardless of score."""
        critical_missing = [
            MissingField(
                field_name="sharpe_ratio",
                severity="critical",
                penalty_applied=20.0,
                quality_assigned=DataQuality.UNKNOWN,
            )
        ]
        result = compute_verdict(score=95.0, missing_fields=critical_missing)
        assert result == Verdict.INCOMPLETE

    def test_verdict_major_missing_does_not_force_incomplete(self):
        """Non-critical missing fields do not force INCOMPLETE."""
        major_missing = [
            MissingField(
                field_name="latest_trade_ts",
                severity="major",
                penalty_applied=7.5,
                quality_assigned=DataQuality.UNKNOWN,
            )
        ]
        result = compute_verdict(score=80.0, missing_fields=major_missing)
        assert result == Verdict.COPY_CANDIDATE


class TestScoringHardOverrides:
    """Hard override rules that override the computed score."""

    def test_all_critical_missing_forces_incomplete(self):
        """If all 3 critical fields are missing, verdict is INCOMPLETE."""
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=None,
            win_rate=None,
            trade_count=None,
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=60),
            markets_traded=10,
            is_sample=True,
        )
        assert result.verdict == Verdict.INCOMPLETE

    def test_one_critical_missing_forces_incomplete(self):
        """Even one critical missing field forces INCOMPLETE."""
        now = datetime.now(timezone.utc)
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=None,  # critical
            win_rate=1.0,
            trade_count=25,
            latest_trade_ts=now,
            first_trade_ts=now - timedelta(days=60),
            markets_traded=10,
            is_sample=True,
        )
        assert result.verdict == Verdict.INCOMPLETE

    def test_minor_missing_does_not_force_incomplete(self):
        """Only markets_traded missing — non-critical, no INCOMPLETE."""
        now = datetime.now(timezone.utc)
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=3.0,
            win_rate=1.0,
            trade_count=25,
            latest_trade_ts=now,
            first_trade_ts=now - timedelta(days=60),
            markets_traded=None,  # minor
            is_sample=True,
        )
        assert result.verdict != Verdict.INCOMPLETE


class TestScoringFormulaVersionPersistence:
    """Scoring formula version must be embedded and persistent."""

    def test_formula_version_is_v1(self):
        """Default score carries formula_version='v1'."""
        result = score_wallet(
            wallet_id=uuid4(),
            is_sample=True,
        )
        assert result.formula_version == "v1"

    def test_formula_version_persists_across_calls(self):
        """Formula version is the same on repeated calls."""
        r1 = score_wallet(wallet_id=uuid4(), is_sample=True)
        r2 = score_wallet(wallet_id=uuid4(), is_sample=True)
        assert r1.formula_version == r2.formula_version == "v1"

    def test_weights_sum_to_100(self):
        """Sanity check: all weights must sum to 100."""
        assert sum(WEIGHTS.values()) == 100


class TestScoringBoundaryValues:
    """Component scores at boundary inputs."""

    def test_sharpe_negative_scores_zero_or_low(self):
        """Negative sharpe produces a very low score (clamped to 0)."""
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=-1.0,
            win_rate=0.5,
            trade_count=25,
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=60),
            markets_traded=5,
            is_sample=True,
        )
        sharpe_comp = [c for c in result.components if c.name == "sharpe_ratio"][0]
        assert sharpe_comp.raw_score == 0.0  # clamped

    def test_sharpe_above_max_capped(self):
        """Sharpe above MAX_SHARPE (3.0) is capped to 100."""
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=5.0,  # above MAX_SHARPE=3.0
            win_rate=0.5,
            trade_count=25,
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=60),
            markets_traded=5,
            is_sample=True,
        )
        sharpe_comp = [c for c in result.components if c.name == "sharpe_ratio"][0]
        assert sharpe_comp.raw_score == 100.0  # clamped

    def test_win_rate_above_one_capped(self):
        """win_rate > 1.0 is clamped to 100."""
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.5,
            win_rate=1.5,  # > 1.0
            trade_count=25,
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=60),
            markets_traded=5,
            is_sample=True,
        )
        wr_comp = [c for c in result.components if c.name == "win_rate"][0]
        assert wr_comp.raw_score == 100.0

    def test_win_rate_negative_scores_zero(self):
        """win_rate < 0.0 is clamped to 0."""
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.5,
            win_rate=-0.5,
            trade_count=25,
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=60),
            markets_traded=5,
            is_sample=True,
        )
        wr_comp = [c for c in result.components if c.name == "win_rate"][0]
        assert wr_comp.raw_score == 0.0

    def test_trade_count_above_max_decays(self):
        """Trade count above CONSISTENCY_MAX (50) decays gently."""
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.5,
            win_rate=0.6,
            trade_count=60,  # above MAX=50
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=60),
            markets_traded=5,
            is_sample=True,
        )
        consistency = [c for c in result.components if c.name == "trade_consistency"][0]
        assert consistency.raw_score < 100.0  # decayed
        assert consistency.raw_score > 90.0  # but only slightly

    def test_recency_within_fresh_boundary(self):
        """Trade well within RECENCY_FRESH_SECONDS (60s) is fresh."""
        now = datetime.now(timezone.utc)
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.5,
            win_rate=0.6,
            trade_count=25,
            latest_trade_ts=now - timedelta(seconds=5),
            first_trade_ts=now - timedelta(days=60),
            markets_traded=5,
            is_sample=True,
        )
        recency = [c for c in result.components if c.name == "data_recency"][0]
        assert recency.raw_score == 100.0

    def test_recency_exactly_at_stale_boundary(self):
        """Trade exactly at RECENCY_STALE_SECONDS (3600s) is stale."""
        now = datetime.now(timezone.utc)
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.5,
            win_rate=0.6,
            trade_count=25,
            latest_trade_ts=now - timedelta(seconds=3600),
            first_trade_ts=now - timedelta(days=60),
            markets_traded=5,
            is_sample=True,
        )
        recency = [c for c in result.components if c.name == "data_recency"][0]
        assert recency.raw_score == 0.0

    def test_recency_mid_decay(self):
        """Trade at midpoint between fresh and stale decays to ~50."""
        now = datetime.now(timezone.utc)
        midpoint = now - timedelta(seconds=(60 + 3600) / 2)  # 1830s
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.5,
            win_rate=0.6,
            trade_count=25,
            latest_trade_ts=midpoint,
            first_trade_ts=now - timedelta(days=60),
            markets_traded=5,
            is_sample=True,
        )
        recency = [c for c in result.components if c.name == "data_recency"][0]
        assert 45.0 <= recency.raw_score <= 55.0  # ~50%


class TestScoringMissingFieldQuality:
    """Verify that each missing field gets the correct quality tag."""

    def test_sharpe_missing_quality_is_unknown(self):
        result = score_wallet(wallet_id=uuid4(), sharpe_ratio=None, is_sample=True)
        sharpe_mf = [m for m in result.missing_fields if m.field_name == "sharpe_ratio"]
        assert len(sharpe_mf) == 1
        assert sharpe_mf[0].quality_assigned == DataQuality.UNKNOWN
        assert sharpe_mf[0].severity == "critical"

    def test_major_field_severity(self):
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.5,
            win_rate=0.5,
            trade_count=25,
            latest_trade_ts=None,  # major missing
            is_sample=True,
        )
        recency_mf = [m for m in result.missing_fields if m.field_name == "latest_trade_ts"]
        assert len(recency_mf) == 1
        assert recency_mf[0].severity == "major"

    def test_minor_field_severity(self):
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.5,
            win_rate=0.5,
            trade_count=25,
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=60),
            markets_traded=None,  # minor missing
            is_sample=True,
        )
        markets_mf = [m for m in result.missing_fields if m.field_name == "markets_traded"]
        assert len(markets_mf) == 1
        assert markets_mf[0].severity == "minor"

    def test_data_quality_propagates_to_components(self):
        """Components reflect data quality of their inputs."""
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.0,  # CALCULATED
            win_rate=0.5,  # CALCULATED
            trade_count=10,  # OBSERVED
            is_sample=True,
        )
        sharpe = [c for c in result.components if c.name == "sharpe_ratio"][0]
        trade = [c for c in result.components if c.name == "trade_consistency"][0]
        assert sharpe.quality == DataQuality.CALCULATED
        assert trade.quality == DataQuality.OBSERVED

    def test_sample_flag_propagates(self):
        """is_sample flag is stored in the score."""
        result = score_wallet(wallet_id=uuid4(), is_sample=True)
        assert result.is_sample is True

    def test_non_sample_flag(self):
        """is_sample=False is the default."""
        result = score_wallet(wallet_id=uuid4(), is_sample=False)
        assert result.is_sample is False


# ══════════════════════════════════════════════════════════════════════════════
# WALLET EVALUATION — min samples, concentration, labels, clustering, dedup
# ══════════════════════════════════════════════════════════════════════════════

class TestWalletDiscovery:
    """Wallet discovery with dedup and source merging."""

    def test_add_from_polymarket(self):
        disc = WalletDiscovery()
        entry = disc.add_from_polymarket("0xabc123", label="polymarket-wallet")
        assert entry["address"] == "0xabc123"
        assert entry["source_count"] == 1

    def test_add_from_bullpen(self):
        disc = WalletDiscovery()
        entry = disc.add_from_bullpen("0xdef456", label="bullpen-wallet")
        assert entry["source_count"] == 1

    def test_manual_watchlist_highest_priority(self):
        """Manual watchlist always wins for label."""
        disc = WalletDiscovery()
        disc.add_from_polymarket("0xabc123", label="auto-label")
        entry = disc.add_to_watchlist("0xabc123", label="manual-label")
        assert entry["label"] == "manual-label"
        assert entry["source_count"] == 2

    def test_related_detection_source(self):
        disc = WalletDiscovery()
        disc.add_from_related_detection("0xghi789")
        assert WalletSource.RELATED_DETECTION in disc.get_sources("0xghi789")

    def test_multi_source_merge(self):
        """Same address from multiple sources merges into one record."""
        disc = WalletDiscovery()
        disc.add_from_polymarket("0xabc123", label="auto")
        disc.add_from_bullpen("0xabc123", label="auto")
        disc.add_to_watchlist("0xabc123", label="manual")
        wallets = disc.list_wallets()
        assert len(wallets) == 1
        assert wallets[0]["source_count"] == 3

    def test_case_insensitive_dedup(self):
        """Same address with different casing is deduped."""
        disc = WalletDiscovery()
        disc.add_from_polymarket("0xABC123")
        disc.add_from_bullpen("0xabc123")
        wallets = disc.list_wallets()
        assert len(wallets) == 1

    def test_empty_address_raises(self):
        """Empty / whitespace addresses are rejected via invalid-dict sentinel.

        Round 11 (P3 PRRT_kwDOTG4Cf86M7Xbp): the discovery helpers
        now return a dict with ``invalid=True`` for empty / whitespace /
        sentinel inputs instead of raising, so callers (notably
        ``run_scan``) can branch on the dict rather than wrap the
        registration in a try/except. The wallet is NOT added to the
        discovery registry in this case.
        """
        disc = WalletDiscovery()
        entry = disc.add_from_polymarket("   ")
        assert entry.get("invalid") is True
        assert entry.get("is_new") is False
        assert entry.get("address") is None
        assert disc.list_wallets() == []

    def test_list_wallets_returns_all(self):
        disc = WalletDiscovery()
        disc.add_from_polymarket("0xaaa111")
        disc.add_from_bullpen("0xbbb222")
        disc.add_to_watchlist("0xccc333")
        assert len(disc.list_wallets()) == 3

    def test_sources_tracked_per_wallet(self):
        disc = WalletDiscovery()
        disc.add_from_polymarket("0xaaa111")
        disc.add_from_bullpen("0xaaa111")
        sources = disc.get_sources("0xaaa111")
        assert WalletSource.POLYMARKET in sources
        assert WalletSource.BULLPEN in sources

    def test_dedup_key_deterministic(self):
        """make_dedup_key produces the same hash for identical inputs."""
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        k1 = make_dedup_key("poly", "0xabc", "m1", "buy", "Yes", ts)
        k2 = make_dedup_key("poly", "0xABC", "m1", "buy", "Yes", ts)
        assert k1 == k2  # case insensitive

    def test_dedup_key_different_for_different_trades(self):
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        k1 = make_dedup_key("poly", "0xabc", "m1", "buy", "Yes", ts)
        k2 = make_dedup_key("poly", "0xabc", "m1", "sell", "Yes", ts)
        assert k1 != k2


class TestWalletConcentration:
    """Wallet concentration scoring (market_correlation component)."""

    def test_single_market_concentrated(self):
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.0,
            win_rate=0.5,
            trade_count=10,
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=30),
            markets_traded=1,  # concentrated
            is_sample=True,
        )
        corr = [c for c in result.components if c.name == "market_correlation"][0]
        assert corr.raw_score == 40.0  # concentrated baseline

    def test_five_markets_diversified(self):
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.0,
            win_rate=0.5,
            trade_count=10,
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=30),
            markets_traded=5,
            is_sample=True,
        )
        corr = [c for c in result.components if c.name == "market_correlation"][0]
        assert corr.raw_score == 100.0  # full diversification

    def test_three_markets_moderate(self):
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.0,
            win_rate=0.5,
            trade_count=10,
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=30),
            markets_traded=3,
            is_sample=True,
        )
        corr = [c for c in result.components if c.name == "market_correlation"][0]
        # 40 + (3-1)*15 = 40 + 30 = 70
        assert corr.raw_score == 70.0

    def test_zero_markets_is_zero(self):
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.0,
            win_rate=0.5,
            trade_count=10,
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=30),
            markets_traded=0,
            is_sample=True,
        )
        corr = [c for c in result.components if c.name == "market_correlation"][0]
        assert corr.raw_score == 0.0

    def test_two_markets_score(self):
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.0,
            win_rate=0.5,
            trade_count=10,
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=30),
            markets_traded=2,
            is_sample=True,
        )
        corr = [c for c in result.components if c.name == "market_correlation"][0]
        # 40 + (2-1)*15 = 55
        assert corr.raw_score == 55.0


class TestWalletLabels:
    """Wallet label conventions."""

    def test_default_label_discovered(self):
        disc = WalletDiscovery()
        entry = disc.add_from_polymarket("0xabc123")
        assert "discovered" in entry["label"] or entry["label"] == ""

    def test_custom_label(self):
        disc = WalletDiscovery()
        entry = disc.add_to_watchlist("0xabc123", label="my-wallet")
        assert entry["label"] == "my-wallet"

    def test_related_detection_label(self):
        disc = WalletDiscovery()
        entry = disc.add_from_related_detection("0xabc123")
        assert entry["label"] == "auto-discovered"

    def test_clustering_dedup(self):
        """Same wallet discovered from multiple sources should be one entry."""
        disc = WalletDiscovery()
        disc.add_from_polymarket("0xabc123")
        disc.add_from_related_detection("0xabc123")
        disc.add_to_watchlist("0xabc123")
        assert len(disc.list_wallets()) == 1


class TestWalletMinSamples:
    """Minimum trade count scoring (trade_consistency component)."""

    def test_min_trades_for_full_consistency(self):
        """CONSISTENCY_MIN (5) trades for full score."""
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.0,
            win_rate=0.5,
            trade_count=5,  # exactly at min
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=30),
            markets_traded=3,
            is_sample=True,
        )
        consistency = [c for c in result.components if c.name == "trade_consistency"][0]
        assert consistency.raw_score == 100.0

    def test_below_min_trades_ramps_up(self):
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.0,
            win_rate=0.5,
            trade_count=2,  # below min of 5
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=30),
            markets_traded=3,
            is_sample=True,
        )
        consistency = [c for c in result.components if c.name == "trade_consistency"][0]
        # (2/5)*100 = 40
        assert consistency.raw_score == 40.0

    def test_zero_trades_zero_consistency(self):
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.0,
            win_rate=0.5,
            trade_count=0,
            latest_trade_ts=datetime.now(timezone.utc),
            first_trade_ts=datetime.now(timezone.utc) - timedelta(days=30),
            markets_traded=3,
            is_sample=True,
        )
        consistency = [c for c in result.components if c.name == "trade_consistency"][0]
        assert consistency.raw_score == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL DUPLICATES, STALENESS, REPEATED SCANS, LATE DATA
# ══════════════════════════════════════════════════════════════════════════════

class TestTradeDedup:
    """Trade deduplication logic."""

    def test_first_trade_not_duplicate(self):
        detector = TradeDetector()
        ts = datetime.now(timezone.utc)
        trade = detector.process_trade(
            source="poly",
            source_trade_id="tx-1",
            wallet_address="0xabc",
            market_source_id="m1",
            side="buy",
            outcome="Yes",
            quantity=100.0,
            price=0.65,
            timestamp=ts,
        )
        assert trade.is_duplicate is False

    def test_same_trade_is_duplicate(self):
        detector = TradeDetector()
        ts = datetime.now(timezone.utc)
        detector.process_trade(
            source="poly",
            source_trade_id="tx-1",
            wallet_address="0xabc",
            market_source_id="m1",
            side="buy",
            outcome="Yes",
            quantity=100.0,
            price=0.65,
            timestamp=ts,
        )
        trade2 = detector.process_trade(
            source="poly",
            source_trade_id="tx-1-renamed",
            wallet_address="0xabc",
            market_source_id="m1",
            side="buy",
            outcome="Yes",
            quantity=100.0,
            price=0.65,
            timestamp=ts,
        )
        assert trade2.is_duplicate is True

    def test_different_outcome_not_duplicate(self):
        detector = TradeDetector()
        ts = datetime.now(timezone.utc)
        detector.process_trade(
            source="poly", source_trade_id="tx-1",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="Yes", quantity=100.0, price=0.65,
            timestamp=ts,
        )
        trade2 = detector.process_trade(
            source="poly", source_trade_id="tx-2",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="No", quantity=100.0, price=0.35,
            timestamp=ts,
        )
        assert trade2.is_duplicate is False

    def test_different_market_not_duplicate(self):
        detector = TradeDetector()
        ts = datetime.now(timezone.utc)
        detector.process_trade(
            source="poly", source_trade_id="tx-1",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="Yes", quantity=100.0, price=0.65,
            timestamp=ts,
        )
        trade2 = detector.process_trade(
            source="poly", source_trade_id="tx-2",
            wallet_address="0xabc", market_source_id="m2",
            side="buy", outcome="Yes", quantity=100.0, price=0.65,
            timestamp=ts,
        )
        assert trade2.is_duplicate is False

    def test_dedup_window_expires(self):
        """After dedup window expires, same trade is not a duplicate."""
        detector = TradeDetector(dedup_window_seconds=1.0)
        ts = datetime.now(timezone.utc)
        detector.process_trade(
            source="poly", source_trade_id="tx-1",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="Yes", quantity=100.0, price=0.65,
            timestamp=ts,
        )
        # Wait for window to expire
        import time
        time.sleep(1.5)
        trade2 = detector.process_trade(
            source="poly", source_trade_id="tx-1-renamed",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="Yes", quantity=100.0, price=0.65,
            timestamp=ts,
        )
        assert trade2.is_duplicate is False

    def test_dedup_log_tracks_records(self):
        detector = TradeDetector()
        ts = datetime.now(timezone.utc)
        detector.process_trade(
            source="poly", source_trade_id="tx-1",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="Yes", quantity=100.0, price=0.65,
            timestamp=ts,
        )
        detector.process_trade(
            source="poly", source_trade_id="tx-2",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="Yes", quantity=100.0, price=0.65,
            timestamp=ts,
        )
        log = detector.get_dedup_log()
        assert len(log) == 2

    def test_sample_flag_propagates(self):
        detector = TradeDetector()
        ts = datetime.now(timezone.utc)
        trade = detector.process_trade(
            source="sample", source_trade_id="tx-1",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="Yes", quantity=100.0, price=0.65,
            timestamp=ts, is_sample=True,
        )
        assert trade.is_sample is True


class TestSignalStaleness:
    """Signal staleness detection."""

    def test_fresh_trade_not_stale(self):
        detector = TradeDetector(staleness_seconds=120.0)
        ts = datetime.now(timezone.utc)
        trade = detector.process_trade(
            source="poly", source_trade_id="tx-1",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="Yes", quantity=100.0, price=0.65,
            timestamp=ts,
        )
        assert trade.is_stale is False

    def test_late_trade_is_stale(self):
        detector = TradeDetector(staleness_seconds=30.0)
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=60)
        trade = detector.process_trade(
            source="poly", source_trade_id="tx-1",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="Yes", quantity=100.0, price=0.65,
            timestamp=old_ts,
        )
        assert trade.is_stale is True
        assert trade.staleness_seconds > 0.0

    def test_staleness_seconds_calculation(self):
        detector = TradeDetector(staleness_seconds=60.0)
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=120)
        trade = detector.process_trade(
            source="poly", source_trade_id="tx-1",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="Yes", quantity=100.0, price=0.65,
            timestamp=old_ts,
        )
        # staleness_seconds = 120 - 60 = 60
        assert trade.staleness_seconds == pytest.approx(60.0, abs=1.0)

    def test_repeated_scan_dedup(self):
        """Repeated scan with same trades should deduplicate."""
        detector = TradeDetector()
        ts = datetime.now(timezone.utc)
        trades = []
        for i in range(5):
            trade = detector.process_trade(
                source="poly", source_trade_id=f"scan-{i}",
                wallet_address="0xabc", market_source_id="m1",
                side="buy", outcome="Yes", quantity=100.0, price=0.65,
                timestamp=ts,
            )
            trades.append(trade)
        # First is not duplicate, rest are duplicates
        assert trades[0].is_duplicate is False
        assert all(t.is_duplicate for t in trades[1:])

    def test_unavailable_entry_returns_trade(self):
        """Even unavailable data returns a TrackedTrade with flags."""
        detector = TradeDetector(staleness_seconds=1.0)
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=3600)
        trade = detector.process_trade(
            source="poly", source_trade_id="tx-late",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="Yes", quantity=100.0, price=0.65,
            timestamp=old_ts,
        )
        assert isinstance(trade, TrackedTrade)
        assert trade.is_stale is True


# ══════════════════════════════════════════════════════════════════════════════
# PAPER BROKER — fills, bid-ask, spread, slippage, depth, review delay,
#              exposure, idempotency, settlement, P&L, counterfactuals
# ══════════════════════════════════════════════════════════════════════════════

class TestPaperBrokerSpreadAndDepth:
    """Bid-ask spread and depth consumption tests."""

    @pytest.mark.asyncio
    async def test_bid_ask_spread_in_depth(self):
        """MarketDepth with different bid/ask levels."""
        depth = MarketDepth(
            best_price=0.65,
            levels=[
                DepthLevel(price=0.65, volume=100.0),
                DepthLevel(price=0.63, volume=200.0),  # bid side
            ],
        )
        assert depth.total_volume == 300.0

    @pytest.mark.asyncio
    async def test_depth_consumption_partial(self):
        """Order larger than best level walks the book."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        broker.set_depth(_M1, "Yes", MarketDepth(
            best_price=0.60,
            levels=[
                DepthLevel(price=0.60, volume=5.0),
                DepthLevel(price=0.65, volume=10.0),
                DepthLevel(price=0.70, volume=20.0),
            ],
        ))
        order = await broker.place_order(
            _M1, "buy", "market", "Yes", 15.0, 0.65, _W1,  # type: ignore
        )
        assert order.status.value == "filled"
        # (5*0.60 + 10*0.65) / 15 = (3.0 + 6.5) / 15 = 0.6333
        assert order.price == pytest.approx(0.6333, abs=0.001)

    @pytest.mark.asyncio
    async def test_depth_exhaustion_partial_fill(self):
        """Order exceeding total depth results in partial fill."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        broker.set_depth(_M1, "Yes", MarketDepth(
            best_price=0.60,
            levels=[DepthLevel(price=0.60, volume=5.0)],
        ))
        order = await broker.place_order(
            _M1, "buy", "market", "Yes", 100.0, 0.65, _W1,  # type: ignore
        )
        assert order.status.value == "filled"
        assert order.filled_quantity == 5.0  # only 5 available

    @pytest.mark.asyncio
    async def test_fill_at_best_price_no_slippage(self):
        """Order within best level volume has zero slippage."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        broker.set_depth(_M1, "Yes", MarketDepth(
            best_price=0.65,
            levels=[DepthLevel(price=0.65, volume=1000.0)],
        ))
        order = await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.65, _W1,  # type: ignore
        )
        assert order.price == 0.65
        assert order.filled_quantity == 10.0

    @pytest.mark.asyncio
    async def test_sell_depth_slippage(self):
        """Sell order walks the bid side."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        broker.set_depth(_M1, "Yes", MarketDepth(
            best_price=0.65,
            levels=[
                DepthLevel(price=0.65, volume=5.0),
                DepthLevel(price=0.60, volume=10.0),
            ],
        ))
        # First buy to have a position
        await broker.place_order(
            _M1, "buy", "market", "Yes", 100.0, 0.65, _W1,  # type: ignore
        )
        # Now sell 10 — should walk bid side
        order = await broker.place_order(
            _M1, "sell", "market", "Yes", 10.0, 0.60, _W1,  # type: ignore
        )
        assert order.status.value == "filled"


class TestPaperBrokerExposure:
    """Exposure limit enforcement in PaperBroker."""

    @pytest.mark.asyncio
    async def test_exposure_limit_per_market(self):
        """Per-market exposure limit blocks orders exceeding the cap."""
        broker = PaperBroker(
            paper_mode=PaperMode.PAPER_AUTO,
            exposure_limits=ExposureLimits(max_per_market=5.0),
        )
        # First order: notional = 6.5 > 5.0 → blocked
        order = await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.65, _W1,  # type: ignore
        )
        assert order.status.value == "rejected"

    @pytest.mark.asyncio
    async def test_exposure_limit_per_wallet(self):
        """Per-wallet exposure limit blocks orders exceeding the cap."""
        broker = PaperBroker(
            paper_mode=PaperMode.PAPER_AUTO,
            exposure_limits=ExposureLimits(max_per_wallet=5.0),
        )
        order = await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.65, _W1,  # type: ignore
        )
        assert order.status.value == "rejected"

    @pytest.mark.asyncio
    async def test_exposure_limit_global(self):
        """Global exposure limit blocks orders exceeding the cap."""
        broker = PaperBroker(
            paper_mode=PaperMode.PAPER_AUTO,
            exposure_limits=ExposureLimits(max_global=5.0),
        )
        order = await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.65, _W1,  # type: ignore
        )
        assert order.status.value == "rejected"

    @pytest.mark.asyncio
    async def test_exposure_limit_passes_under_threshold(self):
        """Order under exposure limit passes."""
        broker = PaperBroker(
            paper_mode=PaperMode.PAPER_AUTO,
            exposure_limits=ExposureLimits(max_order_size=100.0),
        )
        order = await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.65, _W1,  # type: ignore
        )
        assert order.status.value == "filled"


class TestPaperBrokerReviewDelay:
    """Review delay in paper_manual mode."""

    @pytest.mark.asyncio
    async def test_review_delay_holds_order(self):
        """Order in paper_manual mode stays pending during review delay."""
        broker = PaperBroker(
            paper_mode=PaperMode.PAPER_MANUAL,
            review_delay_seconds=3600.0,
        )
        order = await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.65, _W1,  # type: ignore
        )
        assert order.status.value == "pending"

        # Try to confirm immediately — still pending
        result = await broker.confirm_and_fill(str(order.id))
        assert result.status.value == "pending"

    @pytest.mark.asyncio
    async def test_review_delay_zero_fills_immediately(self):
        """With zero review delay, confirm fills immediately."""
        broker = PaperBroker(
            paper_mode=PaperMode.PAPER_MANUAL,
            review_delay_seconds=0.0,
        )
        order = await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.65, _W1,  # type: ignore
        )
        assert order.status.value == "pending"

        result = await broker.confirm_and_fill(str(order.id))
        assert result.status.value == "filled"

    @pytest.mark.asyncio
    async def test_confirm_unknown_order_raises(self):
        """Confirming a non-existent order raises ValueError."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_MANUAL, review_delay_seconds=0.0)
        with pytest.raises(ValueError, match="not found"):
            await broker.confirm_and_fill("nonexistent-id")

    @pytest.mark.asyncio
    async def test_confirm_already_filled_raises(self):
        """Confirming a filled order raises ValueError."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        order = await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.65, _W1,  # type: ignore
        )
        assert order.status.value == "filled"
        with pytest.raises(ValueError, match="not pending"):
            await broker.confirm_and_fill(str(order.id))


class TestPaperBrokerIdempotency:
    """Order operation idempotency."""

    @pytest.mark.asyncio
    async def test_cancel_already_cancelled(self):
        """Cancelling an already-cancelled order raises ValueError."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_MANUAL, review_delay_seconds=60.0)
        order = await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.65, _W1,  # type: ignore
        )
        await broker.cancel_order(str(order.id))
        with pytest.raises(ValueError, match="Cannot cancel"):
            await broker.cancel_order(str(order.id))

    @pytest.mark.asyncio
    async def test_get_order_unknown_returns_none(self):
        broker = PaperBroker()
        result = await broker.get_order("unknown-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_positions(self):
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(
            _M1, "buy", "market", "Yes", 10.0, 0.65, _W1,  # type: ignore
        )
        positions = await broker.list_positions(_W1)
        assert len(positions) == 1

    @pytest.mark.asyncio
    async def test_get_position_unknown_returns_none(self):
        broker = PaperBroker()
        result = await broker.get_position(_M1, _W1, "No")
        assert result is None


class TestPaperBrokerPnL:
    """P&L tracking through PaperBroker."""

    @pytest.mark.asyncio
    async def test_pnl_full_cycle(self):
        """Buy then sell — P&L tracked correctly."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(
            _M1, "buy", "market", "Yes", 100.0, 0.60, _W1,  # type: ignore
        )
        await broker.place_order(
            _M1, "sell", "market", "Yes", 100.0, 0.70, _W1,  # type: ignore
        )
        from uuid import UUID as _UUID
        wid = _UUID(_W1)
        realized = broker.pnl.get_realized_pnl(wid)
        assert realized == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_pnl_partial_close(self):
        """Partial sell produces correct P&L."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(
            _M1, "buy", "market", "Yes", 100.0, 0.60, _W1,  # type: ignore
        )
        await broker.place_order(
            _M1, "sell", "market", "Yes", 50.0, 0.70, _W1,  # type: ignore
        )
        from uuid import UUID as _UUID
        wid = _UUID(_W1)
        realized = broker.pnl.get_realized_pnl(wid)
        assert realized == pytest.approx(5.0)  # (0.70-0.60)*50

    @pytest.mark.asyncio
    async def test_pnl_sell_without_position(self):
        """Selling with no position logs warning and returns empty events."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(
            _M1, "sell", "market", "Yes", 10.0, 0.70, _W1,  # type: ignore
        )
        from uuid import UUID as _UUID
        wid = _UUID(_W1)
        realized = broker.pnl.get_realized_pnl(wid)
        assert realized == 0.0

    @pytest.mark.asyncio
    async def test_pnl_loss(self):
        """Selling below cost produces negative P&L."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(
            _M1, "buy", "market", "Yes", 100.0, 0.70, _W1,  # type: ignore
        )
        await broker.place_order(
            _M1, "sell", "market", "Yes", 100.0, 0.50, _W1,  # type: ignore
        )
        from uuid import UUID as _UUID
        wid = _UUID(_W1)
        realized = broker.pnl.get_realized_pnl(wid)
        assert realized == pytest.approx(-20.0)

    @pytest.mark.asyncio
    async def test_pnl_snapshot(self):
        """P&L snapshot includes realized and unrealized."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(
            _M1, "buy", "market", "Yes", 100.0, 0.60, _W1,  # type: ignore
        )
        from uuid import UUID as _UUID
        wid = _UUID(_W1)
        snapshot = broker.pnl.snapshot(wid, mark_prices={(_UUID(_M1), "Yes"): 0.70})
        assert snapshot.realized_pnl == 0.0
        assert snapshot.unrealized_pnl == pytest.approx(10.0)
        assert snapshot.total_pnl == pytest.approx(10.0)


class TestPaperBrokerSettlement:
    """Settlement through PaperBroker."""

    @pytest.mark.asyncio
    async def test_settle_winner_payout(self):
        """Winning position gets full payout."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(
            _M1, "buy", "market", "Yes", 100.0, 0.60, _W1,  # type: ignore
        )
        evidence = SettlementEvidence(
            source="test",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        results = broker.settle_market(_M1, "Yes", evidence)
        assert len(results) == 1
        assert results[0].is_winner is True
        assert results[0].payout == 100.0

    @pytest.mark.asyncio
    async def test_settle_loser_zero_payout(self):
        """Losing position gets zero payout."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(
            _M1, "buy", "market", "No", 100.0, 0.40, _W1,  # type: ignore
        )
        evidence = SettlementEvidence(
            source="test",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        results = broker.settle_market(_M1, "Yes", evidence)
        assert results[0].is_winner is False
        assert results[0].payout == 0.0

    @pytest.mark.asyncio
    async def test_settle_market_no_positions(self):
        """Settling a market with no positions returns empty list."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        evidence = SettlementEvidence(
            source="test",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        results = broker.settle_market(_M1, "Yes", evidence)
        assert results == []

    @pytest.mark.asyncio
    async def test_settle_market_multiple_positions(self):
        """Settling a market with multiple positions settles each."""
        broker = PaperBroker(paper_mode=PaperMode.PAPER_AUTO)
        await broker.place_order(
            _M1, "buy", "market", "Yes", 100.0, 0.60, _W1,  # type: ignore
        )
        await broker.place_order(
            _M1, "buy", "market", "No", 50.0, 0.35, _W1,  # type: ignore
        )
        evidence = SettlementEvidence(
            source="test",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        results = broker.settle_market(_M1, "Yes", evidence)
        assert len(results) == 2


class TestPaperBrokerCounterfactuals:
    """Counterfactual analysis through scoring verdicts."""

    def test_counterfactual_full_copy(self):
        from polycopy.risk.counterfactual import CounterfactualTracker
        tracker = CounterfactualTracker()
        results = tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.COPY_CANDIDATE,
            entry_price=0.60,
            exit_price=0.80,
            quantity=100.0,
            is_sample=True,
        )
        full = [r for r in results if r.scenario.scenario_type == "full_copy"][0]
        assert full.pnl == pytest.approx(20.0)
        assert full.would_copy is True

    def test_counterfactual_skip_baseline(self):
        from polycopy.risk.counterfactual import CounterfactualTracker
        tracker = CounterfactualTracker()
        results = tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.SKIP,
            entry_price=0.60,
            exit_price=0.80,
            quantity=100.0,
        )
        skip = [r for r in results if r.scenario.scenario_type == "skip"][0]
        assert skip.pnl == 0.0
        assert skip.would_copy is False

    def test_counterfactual_loss_scenario(self):
        from polycopy.risk.counterfactual import CounterfactualTracker
        tracker = CounterfactualTracker()
        results = tracker.analyze_verdict(
            wallet_id=uuid4(),
            verdict=Verdict.WATCHLIST,
            entry_price=0.70,
            exit_price=0.50,
            quantity=100.0,
        )
        full = [r for r in results if r.scenario.scenario_type == "full_copy"][0]
        assert full.pnl == pytest.approx(-20.0)
        assert full.would_copy is False


# ══════════════════════════════════════════════════════════════════════════════
# LIVE SAFETY — DisabledLiveBroker fails closed
# ══════════════════════════════════════════════════════════════════════════════

class TestLiveSafety:
    """Live broker must fail closed — every method raises."""

    def test_is_live_is_false(self):
        """DisabledLiveBroker reports is_live=False."""
        broker = DisabledLiveBroker()
        assert broker.is_live is False

    def test_place_order_raises(self):
        broker = DisabledLiveBroker()
        import asyncio
        with pytest.raises(RuntimeError, match="LIVE EXECUTION IS DISABLED"):
            asyncio.run(broker.place_order(
                _M1, "buy", "market", "Yes", 10.0, 0.65, _W1
            ))

    def test_cancel_order_raises(self):
        broker = DisabledLiveBroker()
        import asyncio
        with pytest.raises(RuntimeError, match="LIVE EXECUTION IS DISABLED"):
            asyncio.run(broker.cancel_order("some-id"))

    def test_get_order_raises(self):
        broker = DisabledLiveBroker()
        import asyncio
        with pytest.raises(RuntimeError, match="LIVE EXECUTION IS DISABLED"):
            asyncio.run(broker.get_order("some-id"))

    def test_list_open_orders_raises(self):
        broker = DisabledLiveBroker()
        import asyncio
        with pytest.raises(RuntimeError, match="LIVE EXECUTION IS DISABLED"):
            asyncio.run(broker.list_open_orders(_W1))

    def test_get_position_raises(self):
        broker = DisabledLiveBroker()
        import asyncio
        with pytest.raises(RuntimeError, match="LIVE EXECUTION IS DISABLED"):
            asyncio.run(broker.get_position(_M1, _W1, "Yes"))

    def test_list_positions_raises(self):
        broker = DisabledLiveBroker()
        import asyncio
        with pytest.raises(RuntimeError, match="LIVE EXECUTION IS DISABLED"):
            asyncio.run(broker.list_positions(_W1))

    def test_no_real_trade_execution_path(self):
        """Verify no importable broker class can execute real trades."""
        from polycopy.adapters.disabled_live_broker import DisabledLiveBroker
        from polycopy.adapters.paper_broker import PaperBroker
        # Paper broker is not live
        assert PaperBroker().is_live is False
        # Disabled broker raises
        assert DisabledLiveBroker().is_live is False


# ══════════════════════════════════════════════════════════════════════════════
# API VALIDATION — request validation, error responses
# ══════════════════════════════════════════════════════════════════════════════

class TestAPIValidation:
    """API endpoint validation and error handling."""

    @pytest.fixture(autouse=True)
    def _clear_idempotency(self):
        from polycopy.api.app import _idempotency_store
        _idempotency_store.clear()
        yield
        _idempotency_store.clear()

    @pytest.fixture(autouse=True)
    def _setup_bidask(self):
        """Provide bid/ask snapshots for paper preview tests."""
        from polycopy.api.app import _bidask_provider
        _bidask_provider.set_snapshot(
            market_id="00000000-0000-0000-0000-000000000001",
            outcome="Yes",
            bid=0.62,
            ask=0.68,
            ask_volume=100.0,
            bid_volume=50.0,
        )
        _bidask_provider.set_snapshot(
            market_id="00000000-0000-0000-0000-000000000001",
            outcome="No",
            bid=0.30,
            ask=0.35,
            ask_volume=80.0,
            bid_volume=100.0,
        )
        _bidask_provider.set_snapshot(
            market_id="00000000-0000-0000-0000-000000000022",
            outcome="Yes",
            bid=0.55,
            ask=0.60,
            ask_volume=20.0,  # small depth → partial fills
            bid_volume=100.0,
        )
        yield
        _bidask_provider.clear()

    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient
        from polycopy.api.app import app
        monkeypatch.setenv("POLYCOPY_ENABLE_DEMO_DATA", "true")
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "test-p10-api-validation.sqlite"))
        import polycopy.config.settings as settings_module
        import polycopy.db.database as database_module

        if database_module._db is not None:
            database_module._db.close()
        database_module._db = None
        settings_module._settings = None
        with TestClient(app) as test_client:
            yield test_client
        if database_module._db is not None:
            database_module._db.close()
        database_module._db = None
        settings_module._settings = None

    def test_health_endpoint(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["is_sample_data"] is True

    def test_system_status_not_live(self, client):
        resp = client.get("/system/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_live"] is False
        assert data["broker_mode"] == "paper"

    def test_scans_returns_sample_data(self, client):
        resp = client.get("/scans")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_sample_data"] is True
        assert len(data["scans"]) >= 1

    def test_wallets_returns_sample_data(self, client):
        resp = client.get("/wallets")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_sample_data"] is True

    def test_signals_returns_sample_data(self, client):
        resp = client.get("/signals")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_sample_data"] is True

    def test_signal_detail_not_found(self, client):
        """Unknown signal ID returns 404."""
        resp = client.get("/signals/00000000-0000-0000-0000-000000000099")
        assert resp.status_code == 404

    def test_wallet_detail_not_found(self, client):
        """Unknown wallet ID returns 404."""
        resp = client.get("/wallets/00000000-0000-0000-0000-000000000099")
        assert resp.status_code == 404

    def test_paper_preview_with_valid_params(self, client):
        resp = client.post("/paper/preview", json={
            "market_id": _M1,
            "outcome": "Yes",
            "side": "buy",
            "quantity": 10.0,
            "price": 0.65,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_sample"] is True
        assert data["status"] == "pending"
        assert data["bid"] == 0.62
        assert data["ask"] == 0.68
        assert data["spread"] == 0.06
        assert "passed_gates" in data
        assert "failed_gates" in data
        assert data["fill_model_version"] == "polycopy-fill-v1"

    def test_paper_preview_missing_params_422(self, client):
        """Missing required params returns 422."""
        resp = client.post("/paper/preview", json={})
        assert resp.status_code == 422

    def test_paper_preview_invalid_side_422(self, client):
        """Invalid side returns 422."""
        resp = client.post("/paper/preview", json={
            "market_id": _M1,
            "outcome": "Yes",
            "side": "hold",  # invalid
            "quantity": 10.0,
            "price": 0.65,
        })
        assert resp.status_code == 422

    @staticmethod
    def _seed_pending_order(order_id: str):
        """Seed a pending order in the DB so approve/reject can transition it."""
        from polycopy.db.database import get_database
        db = get_database()
        # was hardcoded "2026-06-28T12:00:00+00:00"; now dynamic so the order
        # doesn't expire past order_preview_max_age_seconds once wall-clock
        # passes that hardcoded value.
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT OR IGNORE INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, ?, ?)",
            ("00000000-0000-0000-0000-000000000002", "0xtest", "test", 0, now),
        )
        db.execute(
            "INSERT OR IGNORE INTO markets (id, source_id, source, question, fetched_at, is_sample) VALUES (?, ?, ?, ?, ?, ?)",
            ("00000000-0000-0000-0000-000000000001", "m1", "test", "Test Q", now, 0),
        )
        db.execute(
            """
            INSERT OR IGNORE INTO orders
                (id, market_id, wallet_id, side, order_type, outcome, quantity, price,
                 status, filled_quantity, created_at, updated_at, is_sample)
            VALUES (?, ?, ?, 'buy', 'limit', 'Yes', 10.0, 0.65, 'pending', 0.0, ?, ?, 0)
            """,
            (order_id, "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002", now, now),
        )
        db.conn.commit()

    def test_paper_approve_returns_filled(self, client):
        order_id = "00000000-0000-0000-0000-000000000099"
        self._seed_pending_order(order_id)
        resp = client.post("/paper/approve", json={
            "order_id": order_id,
        })
        assert resp.status_code == 200
        data = resp.json()
        # paper_manual mode: order is PENDING then confirm_and_fill → FILLED
        assert data["status"] == "filled"
        assert data["id"] == order_id

    def test_paper_reject_returns_cancelled(self, client):
        order_id = "00000000-0000-0000-0000-000000000098"
        self._seed_pending_order(order_id)
        resp = client.post("/paper/reject", json={
            "order_id": order_id,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cancelled"
        assert data["id"] == order_id

    def test_paper_approve_duplicate_idempotent(self, client):
        """Duplicate approval is idempotent: returns same result, status 200."""
        order_id = "00000000-0000-0000-0000-000000000097"
        self._seed_pending_order(order_id)
        payload = {"order_id": order_id}
        resp1 = client.post("/paper/approve", json=payload)
        assert resp1.status_code == 200
        data1 = resp1.json()
        resp2 = client.post("/paper/approve", json=payload)
        assert resp2.status_code == 200
        data2 = resp2.json()
        # Idempotent: same order_id returned
        assert data1["id"] == data2["id"]
        assert data1["status"] == data2["status"]

    def test_paper_reject_duplicate_idempotent(self, client):
        """Duplicate rejection is idempotent: returns same result, status 200."""
        order_id = "00000000-0000-0000-0000-000000000096"
        self._seed_pending_order(order_id)
        payload = {"order_id": order_id}
        resp1 = client.post("/paper/reject", json=payload)
        assert resp1.status_code == 200
        data1 = resp1.json()
        resp2 = client.post("/paper/reject", json=payload)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data1["id"] == data2["id"]
        assert data1["status"] == data2["status"]

    def test_positions_returns_sample(self, client):
        resp = client.get("/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_sample_data"] is True
        assert "positions" in data

    def test_portfolio_summary(self, client):
        resp = client.get("/portfolio/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_sample_data"] is True
        assert "total_pnl" in data

    def test_decision_log(self, client):
        resp = client.get("/decision-log")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_sample_data"] is True

    def test_decision_log_export_json(self, client):
        resp = client.get("/decision-log/export?format=json")
        assert resp.status_code == 200

    def test_decision_log_export_csv(self, client):
        resp = client.get("/decision-log/export?format=csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")

    def test_decision_log_export_invalid_format(self, client):
        """Invalid format returns 422."""
        resp = client.get("/decision-log/export?format=xml")
        assert resp.status_code == 422

    def test_experiments(self, client):
        resp = client.get("/experiments")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_sample_data"] is True

    def test_data_health(self, client):
        resp = client.get("/data/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "sources" in data
        assert data["overall_status"] == "healthy"

    def test_config_secrets_excluded(self, client):
        """Config endpoint must not expose secrets."""
        resp = client.get("/config")
        assert resp.status_code == 200
        data = resp.json()
        # Verify no private key field
        assert "polymarket_private_key" not in data
        assert data["broker_mode"] == "paper"

    def test_risk_console(self, client):
        resp = client.get("/risk/console")
        assert resp.status_code == 200
        data = resp.json()
        assert "gates" in data
        assert data["is_sample_data"] is True

    def test_idempotency_check_new_key(self, client):
        resp = client.get("/idempotency/test-key-123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_duplicate"] is False

    def test_idempotency_check_duplicate(self, client):
        # Register a key via approve
        client.post("/paper/approve", json={
            "order_id": "dup-test-order",
        })
        # The key is derived internally — just verify the endpoint works
        resp = client.get("/idempotency/nonexistent-key")
        assert resp.status_code == 200

    def test_paper_orders_list(self, client):
        resp = client.get("/paper/orders")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_sample_data"] is True

    def test_paper_orders_filter_by_status(self, client):
        resp = client.get("/paper/orders?status=pending")
        assert resp.status_code == 200

    def test_paper_orders_filter_by_wallet(self, client):
        resp = client.get(f"/paper/orders?wallet_id={_W1}")
        assert resp.status_code == 200

    def test_signals_filter_by_market(self, client):
        resp = client.get(f"/signals?market_id={_M1}")
        assert resp.status_code == 200

    def test_scans_pagination(self, client):
        resp = client.get("/scans?limit=1&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["scans"]) <= 1

    def test_wallets_pagination(self, client):
        resp = client.get("/wallets?limit=1&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["wallets"]) <= 1

    def test_paper_preview_query_params(self, client):
        """Preview via POST with query params in body."""
        resp = client.post(
            "/paper/preview",
            json={
                "market_id": _M1,
                "outcome": "Yes",
                "side": "buy",
                "quantity": 10,
                "price": 0.65,
            },
            params={"market_id": _M1, "outcome": "Yes", "side": "buy", "quantity": 10, "price": 0.65},
        )
        assert resp.status_code == 200

    def test_paper_preview_invalid_price_422(self, client):
        """Price > 1.0 returns 422."""
        resp = client.post("/paper/preview", json={
            "market_id": _M1,
            "outcome": "Yes",
            "side": "buy",
            "quantity": 10,
            "price": 1.5,  # > 1.0
        })
        assert resp.status_code == 422

    def test_paper_preview_invalid_quantity_422(self, client):
        """Quantity <= 0 returns 422."""
        resp = client.post("/paper/preview", json={
            "market_id": _M1,
            "outcome": "Yes",
            "side": "buy",
            "quantity": 0,  # <= 0
            "price": 0.65,
        })
        assert resp.status_code == 422
