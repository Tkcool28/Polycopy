"""Tests for P04 settlement engine — idempotent settlement with resolution evidence."""

from uuid import uuid4

from polycopy.risk.settlement import (
    SettlementEngine,
    SettlementEvidence,
)


class TestSettlementEvidence:
    def test_evidence_hash_deterministic(self):
        e1 = SettlementEvidence(
            source="polymarket",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        e2 = SettlementEvidence(
            source="polymarket",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        assert e1.evidence_hash == e2.evidence_hash

    def test_different_evidence_different_hash(self):
        e1 = SettlementEvidence(
            source="polymarket",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        e2 = SettlementEvidence(
            source="polymarket",
            market_source_id="m1",
            resolution_outcome="No",
        )
        assert e1.evidence_hash != e2.evidence_hash

    def test_raw_evidence_included_in_hash(self):
        e1 = SettlementEvidence(
            source="test",
            market_source_id="m1",
            resolution_outcome="Yes",
            raw_evidence={"price": 0.7},
        )
        e2 = SettlementEvidence(
            source="test",
            market_source_id="m1",
            resolution_outcome="Yes",
            raw_evidence={"price": 0.8},
        )
        assert e1.evidence_hash != e2.evidence_hash


class TestSettlementEngine:
    def test_settle_winner(self):
        engine = SettlementEngine()
        evidence = SettlementEvidence(
            source="polymarket",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        result = engine.settle_position(
            position_id=uuid4(),
            market_id=uuid4(),
            wallet_id=uuid4(),
            outcome="Yes",
            quantity=100.0,
            avg_entry_price=0.60,
            evidence=evidence,
        )
        assert result.is_winner is True
        assert result.payout == 100.0

    def test_settle_loser(self):
        engine = SettlementEngine()
        evidence = SettlementEvidence(
            source="polymarket",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        result = engine.settle_position(
            position_id=uuid4(),
            market_id=uuid4(),
            wallet_id=uuid4(),
            outcome="No",
            quantity=100.0,
            avg_entry_price=0.40,
            evidence=evidence,
        )
        assert result.is_winner is False
        assert result.payout == 0.0

    def test_idempotent_same_evidence(self):
        engine = SettlementEngine()
        evidence = SettlementEvidence(
            source="polymarket",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        pid = uuid4()
        mid = uuid4()
        wid = uuid4()

        r1 = engine.settle_position(pid, mid, wid, "Yes", 100.0, 0.60, evidence)
        r2 = engine.settle_position(pid, mid, wid, "Yes", 100.0, 0.60, evidence)
        assert r1.payout == r2.payout
        assert engine.settlement_count == 1  # deduped

    def test_different_evidence_creates_separate_settlements(self):
        """Different evidence for same position creates separate settlement entries."""
        engine = SettlementEngine()
        evidence1 = SettlementEvidence(
            source="polymarket",
            market_source_id="m1",
            resolution_outcome="Yes",
            raw_evidence={"resolved": True},
        )
        evidence2 = SettlementEvidence(
            source="polymarket",
            market_source_id="m1",
            resolution_outcome="No",
            raw_evidence={"resolved": False},
        )
        pid = uuid4()
        mid = uuid4()
        wid = uuid4()

        r1 = engine.settle_position(pid, mid, wid, "Yes", 100.0, 0.60, evidence1)
        r2 = engine.settle_position(pid, mid, wid, "Yes", 100.0, 0.60, evidence2)
        assert r1.is_winner is True
        assert r2.is_winner is False
        assert engine.settlement_count == 2

    def test_list_settlements(self):
        engine = SettlementEngine()
        evidence = SettlementEvidence(
            source="test",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        engine.settle_position(uuid4(), uuid4(), uuid4(), "Yes", 10.0, 0.5, evidence)
        engine.settle_position(uuid4(), uuid4(), uuid4(), "No", 10.0, 0.5, evidence)
        assert engine.settlement_count == 2
        assert len(engine.list_settlements()) == 2

    def test_get_settlement(self):
        engine = SettlementEngine()
        evidence = SettlementEvidence(
            source="test",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        pid = uuid4()
        engine.settle_position(pid, uuid4(), uuid4(), "Yes", 10.0, 0.5, evidence)
        result = engine.get_settlement(pid, evidence.evidence_hash)
        assert result is not None
        assert result.is_winner is True

    def test_sample_flag(self):
        engine = SettlementEngine()
        evidence = SettlementEvidence(
            source="sample",
            market_source_id="m1",
            resolution_outcome="Yes",
        )
        result = engine.settle_position(
            uuid4(), uuid4(), uuid4(), "Yes", 10.0, 0.5, evidence, is_sample=True
        )
        assert result.is_sample is True
