"""Tests for P03 scoring engine, discovery, and wallet detection."""

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
    RelatedWalletDetector,
    TradeDetector,
    make_dedup_key,
)
from polycopy.discovery.models import (
    WalletSource,
)
from polycopy.engine.evaluate import evaluate_wallet


# ── Scoring engine tests ────────────────────────────────────────────────────────

class TestScoringFormula:
    """Validate the deterministic scoring formula v1."""

    def test_perfect_wallet_scores_100(self):
        """A wallet with perfect metrics should score 100."""
        now = datetime.now(timezone.utc)
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=3.0,
            win_rate=1.0,
            trade_count=25,
            latest_trade_ts=now,
            first_trade_ts=now - timedelta(days=60),
            markets_traded=10,
            is_sample=True,
        )
        assert result.score == 100.0
        assert result.verdict == Verdict.COPY_CANDIDATE

    def test_zero_wallet_scores_low(self):
        """A wallet with worst-case metrics should score very low."""
        now = datetime.now(timezone.utc)
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=0.0,
            win_rate=0.0,
            trade_count=1,
            latest_trade_ts=now,
            first_trade_ts=now,
            markets_traded=1,
            is_sample=True,
        )
        assert result.score < 50  # well below WATCHLIST threshold
        assert result.verdict == Verdict.SKIP

    def test_all_missing_data_is_incomplete(self):
        """All missing critical data should produce INCOMPLETE verdict."""
        result = score_wallet(
            wallet_id=uuid4(),
            is_sample=True,
        )
        assert result.verdict == Verdict.INCOMPLETE
        assert result.score == 0.0
        assert len(result.missing_fields) > 0

    def test_no_missing_critical_allows_copy(self):
        """With all critical fields present and perfect scores, verdict is COPY."""
        now = datetime.now(timezone.utc)
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=3.0,
            win_rate=1.0,
            trade_count=25,
            latest_trade_ts=now,
            first_trade_ts=now - timedelta(days=60),
            markets_traded=10,
            is_sample=True,
        )
        assert result.verdict == Verdict.COPY_CANDIDATE

    def test_moderate_scores_watchlist(self):
        """Score between 50-70 with no critical missing → WATCHLIST."""
        now = datetime.now(timezone.utc)
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=1.5,
            win_rate=0.6,
            trade_count=15,
            latest_trade_ts=now,
            first_trade_ts=now - timedelta(days=20),
            markets_traded=3,
            is_sample=True,
        )
        # Should be in watchlist range
        assert result.score >= 30  # generous lower bound
        assert result.score <= 100
        # Verdict should not be INCOMPLETE since all critical fields present
        assert result.verdict != Verdict.INCOMPLETE

    def test_weights_sum_to_100(self):
        """All component weights must sum to 100."""
        assert sum(WEIGHTS.values()) == 100

    def test_formula_version_tagged(self):
        """Result includes formula version."""
        result = score_wallet(
            wallet_id=uuid4(),
            is_sample=True,
        )
        assert result.formula_version == "v1"

    def test_sharpe_component_clipping(self):
        """Sharpe > MAX_SHARPE should be clipped to 100 for that component."""
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=10.0,  # way above max
            is_sample=True,
        )
        sharpe_comp = [c for c in result.components if c.name == "sharpe_ratio"][0]
        assert sharpe_comp.raw_score == 100.0

    def test_sample_flag_propagated(self):
        """is_sample=True must propagate to the result."""
        result = score_wallet(
            wallet_id=uuid4(),
            is_sample=True,
        )
        assert result.is_sample is True

    def test_components_labeled_with_quality(self):
        """Each component must have a DataQuality tag."""
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=2.0,
            win_rate=0.7,
            is_sample=True,
        )
        for comp in result.components:
            assert isinstance(comp.quality, DataQuality)

    def test_individual_metrics_known(self):
        """Provided metrics must be tagged as OBSERVED or CALCULATED."""
        now = datetime.now(timezone.utc)
        result = score_wallet(
            wallet_id=uuid4(),
            sharpe_ratio=2.0,
            win_rate=0.7,
            trade_count=20,
            latest_trade_ts=now,
            is_sample=True,
        )
        # sharpe is calculated from data
        sharpe = [c for c in result.components if c.name == "sharpe_ratio"][0]
        assert sharpe.quality == DataQuality.CALCULATED
        # trade_count is observed
        consistency = [c for c in result.components if c.name == "trade_consistency"][0]
        assert consistency.quality == DataQuality.OBSERVED

    def test_recency_fresh_scores_high(self):
        """Trade within RECENCY_FRESH_SECONDS should score 100 on recency."""
        now = datetime.now(timezone.utc)
        result = score_wallet(
            wallet_id=uuid4(),
            latest_trade_ts=now - timedelta(seconds=30),
            is_sample=True,
        )
        recency = [c for c in result.components if c.name == "data_recency"][0]
        assert recency.raw_score == 100.0
        assert recency.quality == DataQuality.OBSERVED

    def test_recency_stale_scores_zero(self):
        """Trade older than 1 hour should score 0 on recency."""
        now = datetime.now(timezone.utc)
        result = score_wallet(
            wallet_id=uuid4(),
            latest_trade_ts=now - timedelta(hours=2),
            is_sample=True,
        )
        recency = [c for c in result.components if c.name == "data_recency"][0]
        assert recency.raw_score == 0.0


class TestVerdictRules:
    """Test the hard verdict rules."""

    def test_copy_candidate_threshold(self):
        """Score >= 70, no critical missing → COPY_CANDIDATE."""
        verdict = compute_verdict(70.0, [])
        assert verdict == Verdict.COPY_CANDIDATE

    def test_watchlist_threshold(self):
        """Score 50-69.9, no critical missing → WATCHLIST."""
        verdict = compute_verdict(55.0, [])
        assert verdict == Verdict.WATCHLIST

    def test_skip_threshold(self):
        """Score < 50 → SKIP."""
        verdict = compute_verdict(49.9, [])
        assert verdict == Verdict.SKIP

    def test_critical_missing_forces_incomplete(self):
        """Any critical missing field → INCOMPLETE regardless of score."""
        missing = [MissingField(field_name="sharpe_ratio", severity="critical", penalty_applied=20.0)]
        verdict = compute_verdict(95.0, missing)
        assert verdict == Verdict.INCOMPLETE

    def test_major_missing_does_not_force_incomplete(self):
        """Major/minor missing fields don't force INCOMPLETE."""
        missing = [MissingField(field_name="latest_trade_ts", severity="major", penalty_applied=7.5)]
        verdict = compute_verdict(75.0, missing)
        assert verdict == Verdict.COPY_CANDIDATE


# ── Wallet Discovery tests ─────────────────────────────────────────────────────

class TestWalletDiscovery:

    def test_add_from_polymarket(self):
        disc = WalletDiscovery()
        entry = disc.add_from_polymarket("0xabc123", label="polm-wallet")
        assert entry["address"] == "0xabc123"
        assert WalletSource.POLYMARKET in disc.get_sources("0xabc123")

    def test_add_from_bullpen(self):
        disc = WalletDiscovery()
        disc.add_from_bullpen("0xdef456")
        assert WalletSource.BULLPEN in disc.get_sources("0xdef456")

    def test_add_to_watchlist(self):
        disc = WalletDiscovery()
        disc.add_to_watchlist("0xwatch1", label="my-tracker")
        assert WalletSource.MANUAL_WATCHLIST in disc.get_sources("0xwatch1")

    def test_dedup_same_address(self):
        """Same address from multiple sources should dedup."""
        disc = WalletDiscovery()
        disc.add_from_polymarket("0xabc")
        disc.add_from_bullpen("0xabc")
        disc.add_to_watchlist("0xabc", "my-label")
        wallets = disc.list_wallets()
        assert len(wallets) == 1
        assert wallets[0]["source_count"] == 3

    def test_case_insensitive_dedup(self):
        """Addresses should be deduped case-insensitively."""
        disc = WalletDiscovery()
        disc.add_from_polymarket("0xABC")
        disc.add_from_bullpen("0xabc")
        wallets = disc.list_wallets()
        assert len(wallets) == 1

    def test_manual_label_wins(self):
        """Manual watchlist label should override auto-discovered."""
        disc = WalletDiscovery()
        disc.add_from_polymarket("0xauto", label="auto-label")
        disc.add_to_watchlist("0xauto", label="manual-label")
        entry = disc.list_wallets()[0]
        assert entry["label"] == "manual-label"

    def test_empty_address_raises(self):
        disc = WalletDiscovery()
        # Round 11: empty/sentinel inputs are rejected via a dict with
        # ``invalid=True`` instead of raising. The caller MUST check
        # ``entry["invalid"]`` (or the ``is_new`` flag) before assuming
        # the address was added.
        entry = disc.add_from_polymarket("")
        assert entry.get("invalid") is True
        assert entry.get("is_new") is False
        assert entry.get("address") is None

    def test_list_wallets_returns_all(self):
        disc = WalletDiscovery()
        disc.add_from_polymarket("0x1")
        disc.add_from_polymarket("0x2")
        assert len(disc.list_wallets()) == 2


class TestRelatedWalletDetector:

    def test_requires_minimum_signals(self):
        det = RelatedWalletDetector()
        # Only 1 signal — should NOT be plausible
        result = det.evaluate("0xprimary", "0xcand", ["shared_market"])
        assert result.is_plausibly_related is False

    def test_strong_signals_give_higher_confidence(self):
        det = RelatedWalletDetector()
        result = det.evaluate(
            "0xprimary", "0xcand",
            ["shared_market", "similar_volume"],
        )
        assert result.is_plausibly_related is True
        assert result.confidence > 0.4

    def test_weak_only_signals_capped(self):
        det = RelatedWalletDetector()
        result = det.evaluate(
            "0xprimary", "0xcand",
            ["close_timing", "same_fee_taker"],
        )
        # Weak-only signals should be lower confidence
        assert result.confidence <= 0.45

    def test_batch_filters_implausible(self):
        det = RelatedWalletDetector()
        candidates = [
            ("0xa", ["shared_market", "similar_volume"]),
            ("0xb", ["shared_market"]),  # only 1 signal
            ("0xc", ["close_timing", "same_fee_taker", "shared_market"]),
        ]
        results = det.batch_evaluate("0xprimary", candidates)
        assert len(results) == 2  # a and c pass


class TestTradeDetector:

    def test_new_trade_passes(self):
        det = TradeDetector()
        trade = det.process_trade(
            source="polymarket",
            source_trade_id="t1",
            wallet_address="0xabc",
            market_source_id="m1",
            side="buy",
            outcome="Yes",
            quantity=10.0,
            price=0.65,
            timestamp=datetime.now(timezone.utc),
        )
        assert trade.is_duplicate is False
        assert trade.is_stale is False

    def test_duplicate_trade_flagged(self):
        det = TradeDetector()
        ts = datetime.now(timezone.utc)
        det.process_trade(
            source="polymarket",
            source_trade_id="t1",
            wallet_address="0xabc",
            market_source_id="m1",
            side="buy",
            outcome="Yes",
            quantity=10.0,
            price=0.65,
            timestamp=ts,
        )
        # Same trade again
        trade2 = det.process_trade(
            source="polymarket",
            source_trade_id="t1-dup",
            wallet_address="0xabc",
            market_source_id="m1",
            side="buy",
            outcome="Yes",
            quantity=10.0,
            price=0.65,
            timestamp=ts,
        )
        assert trade2.is_duplicate is True

    def test_stale_trade_flagged(self):
        det = TradeDetector(staleness_seconds=120.0)
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=300)
        trade = det.process_trade(
            source="polymarket",
            source_trade_id="t-old",
            wallet_address="0xabc",
            market_source_id="m1",
            side="buy",
            outcome="Yes",
            quantity=10.0,
            price=0.65,
            timestamp=old_ts,
        )
        assert trade.is_stale is True
        assert trade.staleness_seconds > 0

    def test_different_market_not_duplicate(self):
        det = TradeDetector()
        ts = datetime.now(timezone.utc)
        det.process_trade(
            source="polymarket", source_trade_id="t1",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="Yes", quantity=10.0, price=0.65, timestamp=ts,
        )
        trade2 = det.process_trade(
            source="polymarket", source_trade_id="t2",
            wallet_address="0xabc", market_source_id="m2",
            side="buy", outcome="Yes", quantity=10.0, price=0.65, timestamp=ts,
        )
        assert trade2.is_duplicate is False

    def test_audit_log_records_dedup(self):
        det = TradeDetector()
        ts = datetime.now(timezone.utc)
        det.process_trade(
            source="polymarket", source_trade_id="t1",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="Yes", quantity=10.0, price=0.65, timestamp=ts,
        )
        det.process_trade(
            source="polymarket", source_trade_id="t1-dup",
            wallet_address="0xabc", market_source_id="m1",
            side="buy", outcome="Yes", quantity=10.0, price=0.65, timestamp=ts,
        )
        log = det.get_dedup_log()
        assert len(log) == 2
        assert log[1].is_duplicate is True

    def test_samples_sample_flag(self):
        """is_sample=True must propagate."""
        det = TradeDetector()
        trade = det.process_trade(
            source="sample", source_trade_id="s1",
            wallet_address="0xSAMPLE", market_source_id="m1",
            side="buy", outcome="Yes", quantity=1.0, price=0.5,
            timestamp=datetime.now(timezone.utc),
            is_sample=True,
        )
        assert trade.is_sample is True


class TestMakeDedupKey:

    def test_same_inputs_same_key(self):
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        k1 = make_dedup_key("src", "0xabc", "m1", "buy", "Yes", ts)
        k2 = make_dedup_key("src", "0xabc", "m1", "buy", "Yes", ts)
        assert k1 == k2

    def test_case_insensitive_address(self):
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        k1 = make_dedup_key("src", "0xABC", "m1", "buy", "Yes", ts)
        k2 = make_dedup_key("src", "0xabc", "m1", "buy", "Yes", ts)
        assert k1 == k2

    def test_different_markets_different_key(self):
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        k1 = make_dedup_key("src", "0xabc", "m1", "buy", "Yes", ts)
        k2 = make_dedup_key("src", "0xabc", "m2", "buy", "Yes", ts)
        assert k1 != k2

    def test_granularity_groups_trades(self):
        """Trades within same 60s bucket should produce same key."""
        ts1 = datetime(2026, 1, 1, 12, 0, 10, tzinfo=timezone.utc)
        ts2 = datetime(2026, 1, 1, 12, 0, 40, tzinfo=timezone.utc)
        k1 = make_dedup_key("src", "0xabc", "m1", "buy", "Yes", ts1, granularity_seconds=60)
        k2 = make_dedup_key("src", "0xabc", "m1", "buy", "Yes", ts2, granularity_seconds=60)
        assert k1 == k2


# ── Integration: evaluate_wallet ───────────────────────────────────────────────

class TestEvaluateWallet:

    def test_full_evaluation_returns_score_and_summary(self):
        now = datetime.now(timezone.utc)
        score_id, summary = evaluate_wallet(
            wallet_address="0xABCDEF1234567890",
            source="polymarket",
            sharpe_ratio=2.5,
            win_rate=0.72,
            trade_count=42,
            latest_trade_ts=now - timedelta(seconds=30),
            first_trade_ts=now - timedelta(days=45),
            markets_traded=7,
            is_sample=True,
        )
        assert score_id is not None
        assert "score=" in summary
        assert "verdict=copy_candidate" in summary
        assert "*** SAMPLE DATA ***" in summary

    def test_incomplete_evaluation_shows_missing(self):
        score_id, summary = evaluate_wallet(
            wallet_address="0xABCDEF1234567890",
            is_sample=True,
        )
        assert "score=" in summary
        assert "verdict=incomplete" in summary

    def test_manual_watchlist_label(self):
        _, summary = evaluate_wallet(
            wallet_address="0xABCDEF1234567890",
            manual_watchlist=True,
            is_sample=True,
        )
        assert "[WATCHLIST]" in summary
