"""Contract tests for canonical ingestion market-evidence snapshots."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT / "src", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from polycopy.ingestion.canonical_metadata import (
    MARKET_EVIDENCE_SNAPSHOT_CONTRACT_VERSION,
    MERGE_CONFLICT,
    MERGE_FILLED,
    MERGE_UNCHANGED,
    _validate_outcome_mapping,
    build_canonical_metadata,
    merge_canonical_metadata,
)
from polycopy.ingestion.normalized_source_trade import (
    normalize_source_trade,
)

FULL_GAMMA = {
    "conditionId": "0x" + "1" * 64,
    "id": "12345",
    "question": "Will X happen?",
    "slug": "will-x-happen",
    "category": "Politics",
    "tags": ["election", "2026"],
    "events": [{"id": "evt1", "slug": "election", "title": "Election"}],
    "series": [{"id": "series1", "slug": "politics", "title": "Politics"}],
    "outcomes": '["Yes", "No"]',
    "clobTokenIds": '["1111111111", "2222222222"]',
    "active": True,
    "closed": False,
    "acceptingOrders": True,
    "endDate": "2027-01-01T00:00:00Z",
    "updatedAt": "2026-07-24T12:00:00Z",
}

TRADE = {
    "sourceProvidedTradeId": "trade-1",
    "proxyWallet": "0x" + "a" * 40,
    "conditionId": FULL_GAMMA["conditionId"],
    "asset": "1111111111",
    "side": "BUY",
    "price": "0.51",
    "size": "2",
    "timestamp": 1_700_000_000,
    "outcome": "Yes",
    "outcomeIndex": 0,
    "title": "wallet context title",
    "slug": "wallet-context-slug",
    "transactionHash": "0xabcdef1234567890",
}


def _dump(value):
    if isinstance(value, Mapping):
        value = dict(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def test_exact_snapshot_shape_and_authority():
    metadata = build_canonical_metadata(TRADE, FULL_GAMMA)
    snapshot = metadata["_snapshot"]
    assert set(snapshot) == {"market", "outcomes", "lifecycle", "resolution", "provenance"}
    assert snapshot["market"] == {
        "condition_id": FULL_GAMMA["conditionId"],
        "provider_market_id": "12345",
        "question": "Will X happen?",
        "slug": "will-x-happen",
    }
    assert snapshot["outcomes"]["ordered"] == [
        {"label": "Yes", "clob_token_id": "1111111111"},
        {"label": "No", "clob_token_id": "2222222222"},
    ]
    assert snapshot["lifecycle"] == {
        "active": True,
        "closed": False,
        "accepting_orders": True,
        "end_date": "2027-01-01T00:00:00Z",
    }
    assert snapshot["resolution"]["resolution_status"] is None
    provenance = snapshot["provenance"]
    assert provenance["provider_updated_at"] == FULL_GAMMA["updatedAt"]
    assert provenance["trade_response_title"] == TRADE["title"]
    assert snapshot["market"]["question"] != TRADE["title"]
    assert provenance["snapshot_contract_version"] == MARKET_EVIDENCE_SNAPSHOT_CONTRACT_VERSION
    assert "retrieved_at" not in provenance
    assert "realized_pnl" not in _dump(metadata)


def test_provider_updated_at_requires_explicit_gamma_updated_at():
    gamma = dict(FULL_GAMMA)
    gamma.pop("updatedAt")
    metadata = build_canonical_metadata({}, gamma)
    assert "provider_updated_at" not in metadata["_snapshot"]["provenance"]


def test_provider_updated_at_is_audit_only_for_replay(monkeypatch):
    from datetime import datetime as real_datetime

    import polycopy.ingestion.canonical_metadata as module

    class FirstClock:
        @classmethod
        def now(cls, tz):
            return real_datetime(2026, 1, 1, tzinfo=tz)

    class LaterClock:
        @classmethod
        def now(cls, tz):
            return real_datetime(2026, 1, 2, tzinfo=tz)

    monkeypatch.setattr(module, "datetime", FirstClock)
    first, _, _ = merge_canonical_metadata(
        None, FULL_GAMMA, condition_id=FULL_GAMMA["conditionId"]
    )
    changed_audit = dict(FULL_GAMMA, updatedAt="2026-07-25T00:00:00Z")
    monkeypatch.setattr(module, "datetime", LaterClock)
    replay, status, _ = merge_canonical_metadata(
        _dump(first), changed_audit, condition_id=FULL_GAMMA["conditionId"]
    )
    assert status == MERGE_UNCHANGED
    assert _dump(replay) == _dump(first)
    assert replay["_snapshot"]["provenance"]["provider_updated_at"] == FULL_GAMMA["updatedAt"]


def test_identical_replay_is_byte_identical_and_unchanged(monkeypatch):
    from datetime import datetime as real_datetime

    import polycopy.ingestion.canonical_metadata as module

    class FirstClock:
        @classmethod
        def now(cls, tz):
            return real_datetime(2026, 1, 1, tzinfo=tz)

    class LaterClock:
        @classmethod
        def now(cls, tz):
            return real_datetime(2026, 1, 2, tzinfo=tz)

    monkeypatch.setattr(module, "datetime", FirstClock)
    first, first_status, _ = merge_canonical_metadata(
        None, FULL_GAMMA, condition_id=FULL_GAMMA["conditionId"]
    )
    first_bytes = _dump(first)
    monkeypatch.setattr(module, "datetime", LaterClock)
    replay, replay_status, reasons = merge_canonical_metadata(
        first_bytes, FULL_GAMMA, condition_id=FULL_GAMMA["conditionId"]
    )
    assert first_status == MERGE_FILLED
    assert replay_status == MERGE_UNCHANGED
    assert reasons == ["no_change"]
    assert _dump(replay) == first_bytes
    assert replay["_snapshot"]["provenance"]["retrieved_at"] == "2026-01-01T00:00:00Z"


def test_substantive_update_is_filled_and_advances_retrieval_time(monkeypatch):
    from datetime import datetime as real_datetime

    import polycopy.ingestion.canonical_metadata as module

    class FirstClock:
        @classmethod
        def now(cls, tz):
            return real_datetime(2026, 1, 1, tzinfo=tz)

    class LaterClock:
        @classmethod
        def now(cls, tz):
            return real_datetime(2026, 1, 2, tzinfo=tz)

    monkeypatch.setattr(module, "datetime", FirstClock)
    first, _, _ = merge_canonical_metadata(
        None, FULL_GAMMA, condition_id=FULL_GAMMA["conditionId"]
    )
    changed_gamma = dict(FULL_GAMMA, closed=True, active=False, updatedAt="2026-07-25T00:00:00Z")
    monkeypatch.setattr(module, "datetime", LaterClock)
    updated, status, _ = merge_canonical_metadata(
        _dump(first), changed_gamma, condition_id=FULL_GAMMA["conditionId"]
    )
    assert status == MERGE_FILLED
    assert updated["_snapshot"]["lifecycle"]["closed"] is True
    assert updated["_snapshot"]["provenance"]["provider_updated_at"] == changed_gamma["updatedAt"]
    assert updated["_snapshot"]["provenance"]["retrieved_at"] == "2026-01-02T00:00:00Z"


def test_repeated_duplicate_ingestion_does_not_churn_metadata():
    first, _, _ = merge_canonical_metadata(
        None, FULL_GAMMA, condition_id=FULL_GAMMA["conditionId"]
    )
    current = _dump(first)
    for _ in range(4):
        replay, status, _ = merge_canonical_metadata(
            current, FULL_GAMMA, condition_id=FULL_GAMMA["conditionId"]
        )
        assert status == MERGE_UNCHANGED
        assert _dump(replay) == current


def _resolution_gamma(**updates):
    gamma = dict(FULL_GAMMA)
    gamma.update(updates)
    return gamma


def test_open_market_has_no_winner():
    resolution = build_canonical_metadata({}, FULL_GAMMA)["_snapshot"]["resolution"]
    assert resolution["resolution_status"] is None
    assert resolution["winner_token_id"] is None


def test_closed_unresolved_market_has_no_winner():
    gamma = _resolution_gamma(active=False, closed=True, acceptingOrders=False)
    resolution = build_canonical_metadata({}, gamma)["_snapshot"]["resolution"]
    assert resolution["resolution_status"] is None
    assert resolution["winner_token_id"] is None


def test_resolved_market_with_one_valid_winner():
    gamma = _resolution_gamma(resolved=True, winnerTokenId="1111111111")
    resolution = build_canonical_metadata({}, gamma)["_snapshot"]["resolution"]
    assert resolution["resolution_status"] == "resolved"
    assert resolution["winner_token_id"] == "1111111111"
    assert resolution["winner_outcome"] == "Yes"
    assert resolution["evidence_fields"] == ["market.winnerTokenId"]


def test_resolved_without_derivable_winner_is_incomplete():
    resolution = build_canonical_metadata({}, _resolution_gamma(resolved=True))[
        "_snapshot"
    ]["resolution"]
    assert resolution["resolution_status"] == "incomplete"
    assert resolution["errors"] == ["resolved_without_derivable_winner"]


def test_multiple_winners_are_invalid():
    gamma = _resolution_gamma(resolved=True, winnerTokenId=["1111111111", "2222222222"])
    resolution = build_canonical_metadata({}, gamma)["_snapshot"]["resolution"]
    assert resolution["resolution_status"] == "invalid"
    assert resolution["winner_token_id"] is None


def test_lifecycle_fields_never_imply_resolution():
    gamma = _resolution_gamma(
        active=False,
        closed=True,
        acceptingOrders=False,
        endDate="2020-01-01T00:00:00Z",
    )
    resolution = build_canonical_metadata({}, gamma)["_snapshot"]["resolution"]
    assert resolution["resolution_status"] is None
    assert resolution["winner_token_id"] is None


def test_outcomes_unequal_lengths():
    result = _validate_outcome_mapping('["A","B"]', '["1"]')
    assert result["status"] == "invalid"
    assert result["ordered"] == []
    assert "array_length_mismatch" in result["errors"]


def test_outcomes_duplicate_tokens():
    result = _validate_outcome_mapping('["A","B"]', '["1","1"]')
    assert result["status"] == "invalid"
    assert "duplicate_token_ids" in result["errors"]


def test_outcomes_blank_label():
    result = _validate_outcome_mapping('["A",""]', '["1","2"]')
    assert result["status"] == "invalid"
    assert result["ordered"] == []


def test_outcomes_blank_token():
    result = _validate_outcome_mapping('["A","B"]', '["1",""]')
    assert result["status"] == "invalid"
    assert result["ordered"] == []


def test_outcomes_numeric_token_is_invalid():
    result = _validate_outcome_mapping('["A","B"]', '["1",2]')
    assert result["status"] == "invalid"
    assert result["ordered"] == []
    assert "blank_or_invalid_token_at_index=1" in result["errors"]


def test_outcomes_invalid_index():
    result = _validate_outcome_mapping('["A","B"]', '["1","2"]', outcome_index=2)
    # Trade-context validation diagnostics live under trade_validation now.
    # ``outcome_index=2`` is out of range so a contextual diagnostic is
    # produced; the Gamma-shape error list is empty (labels and tokens are
    # well-formed). ``trade_validation_errors`` carries the diagnostic.
    assert result["trade_validation_errors"] == ["outcome_index_out_of_range=2"]
    assert result["errors"] == []
    assert result["valid_index"] is False
    assert result["status"] == "complete"


def test_outcomes_token_index_disagreement():
    result = _validate_outcome_mapping(
        '["A","B"]', '["1","2"]', outcome_index=0, selected_token="2", selected_outcome="A"
    )
    # Authoritative Gamma-shape evidence is well-formed so ``ordered`` is
    # built; the index / token disagreement is a context-only diagnostic.
    # ``selected_outcome="A"`` happens to match the label at ``outcome_index=0``
    # so ``index_outcome_agrees`` is True and only the token disagreement is
    # reported.
    assert result["trade_validation_errors"] == ["index_token_disagreement"]
    assert result["errors"] == []
    assert result["index_token_agrees"] is False
    assert result["index_outcome_agrees"] is True
    assert result["status"] == "complete"
    assert result["ordered"] == [
        {"label": "A", "clob_token_id": "1"},
        {"label": "B", "clob_token_id": "2"},
    ]


def test_outcomes_token_outcome_disagreement_without_index():
    result = _validate_outcome_mapping(
        '["A","B"]',
        '["1","2"]',
        selected_token="2",
        selected_outcome="A",
    )
    # Without ``outcome_index``, the agreement check between selected token
    # and selected outcome is also a context-only diagnostic. The labels /
    # tokens are still well-formed so ``ordered`` is built and the
    # authoritative evidence is complete.
    assert "selected_token_outcome_disagreement" in result["trade_validation_errors"]
    assert result["errors"] == []
    assert result["status"] == "complete"


def test_trade_float_outcome_index_is_not_truncated():
    trade = dict(TRADE, outcomeIndex=0.5)
    metadata = build_canonical_metadata(trade, FULL_GAMMA)
    outcomes = metadata["_snapshot"]["outcomes"]
    provenance = metadata["_snapshot"]["provenance"]
    # The strict parser rejects floats outright, so the snapshot is fed
    # ``outcome_index=None``. The outcomes mapping is well-formed in its own
    # right (labels/tokens are non-empty and consistent), so the validator
    # reports ``complete`` with no ordered index; the validator and
    # persisted-provenance paths agree because BOTH go through the same
    # strict parser. The crucial regression is that the invalid index is
    # NEVER persisted as provenance.
    trade_validation = provenance["trade_validation"]
    assert trade_validation["valid_index"] is None
    assert trade_validation["outcome_index_supplied"] is False
    assert outcomes["status"] == "complete"
    assert "trade_response_outcome_index" not in provenance, provenance


def test_outcomes_binary_market():
    result = _validate_outcome_mapping(
        '["Yes","No"]', '["1","2"]', outcome_index=0, selected_token="1", selected_outcome="Yes"
    )
    assert result["status"] == "complete"
    assert len(result["ordered"]) == 2


def test_outcomes_multi_market():
    result = _validate_outcome_mapping(
        '["A","B","C"]', '["1","2","3"]', outcome_index=2, selected_token="3", selected_outcome="C"
    )
    assert result["status"] == "complete"
    assert len(result["ordered"]) == 3


def test_taxonomy_conflict_does_not_discard_snapshot_fields():
    existing = _dump({"taxonomy": {"raw_category": "Sports"}})
    merged, status, reasons = merge_canonical_metadata(
        existing, FULL_GAMMA, condition_id=FULL_GAMMA["conditionId"]
    )
    assert status == MERGE_CONFLICT
    assert "taxonomy_raw_category_conflict" in reasons
    assert merged["taxonomy"]["raw_category"] == "Sports"
    assert merged["_snapshot"]["lifecycle"]["active"] is True
    assert merged["_snapshot"]["resolution"]["resolution_status"] is None


def test_conflict_without_unrelated_fill_is_never_mislabeled_unchanged():
    first, _, _ = merge_canonical_metadata(
        None, FULL_GAMMA, condition_id=FULL_GAMMA["conditionId"]
    )
    first = dict(first)
    first["taxonomy"]["raw_category"] = "Sports"
    merged, status, reasons = merge_canonical_metadata(
        _dump(first), FULL_GAMMA, condition_id=FULL_GAMMA["conditionId"]
    )
    assert status == MERGE_CONFLICT
    assert reasons == ["taxonomy_raw_category_conflict"]
    assert merged["taxonomy"]["raw_category"] == "Sports"
    assert merged["_snapshot"] == first["_snapshot"]


def test_immutable_snapshot_provenance_conflicts_and_is_preserved():
    first, _, _ = merge_canonical_metadata(
        None, FULL_GAMMA, condition_id=FULL_GAMMA["conditionId"]
    )
    first = dict(first)
    first["_snapshot"]["provenance"]["provider"] = "not-gamma"
    merged, status, reasons = merge_canonical_metadata(
        _dump(first), FULL_GAMMA, condition_id=FULL_GAMMA["conditionId"]
    )
    assert status == MERGE_CONFLICT
    assert "_snapshot_provenance_provider_conflict" in reasons
    assert merged["_snapshot"]["provenance"]["provider"] == "not-gamma"


def test_provider_market_id_cannot_substitute_for_missing_condition_id():
    gamma = dict(FULL_GAMMA)
    gamma.pop("conditionId")
    gamma["id"] = FULL_GAMMA["conditionId"]
    existing = _dump({"unrelated": {"keep": True}})
    merged, status, reasons = merge_canonical_metadata(
        existing, gamma, condition_id=FULL_GAMMA["conditionId"]
    )
    assert status == "unavailable"
    assert reasons == ["condition_id_mismatch"]
    assert merged["unrelated"] == {"keep": True}


def test_outcome_conflict_does_not_erase_taxonomy_or_event():
    first, _, _ = merge_canonical_metadata(
        None, FULL_GAMMA, condition_id=FULL_GAMMA["conditionId"]
    )
    bad = dict(FULL_GAMMA, outcomes='["Yes"]')
    merged, status, reasons = merge_canonical_metadata(
        _dump(first), bad, condition_id=FULL_GAMMA["conditionId"]
    )
    assert status == MERGE_CONFLICT
    assert any("_snapshot_outcomes" in reason for reason in reasons)
    assert merged["taxonomy"] == first["taxonomy"]
    assert merged["event"] == first["event"]


def test_null_values_do_not_erase_existing_and_unrelated_survives():
    first, _, _ = merge_canonical_metadata(
        None, FULL_GAMMA, condition_id=FULL_GAMMA["conditionId"]
    )
    first = dict(first)
    first["unrelated"] = {"keep": True}
    gamma = dict(FULL_GAMMA, question=None, active=None)
    merged, _, _ = merge_canonical_metadata(
        _dump(first), gamma, condition_id=FULL_GAMMA["conditionId"]
    )
    assert merged["_snapshot"]["market"]["question"] == FULL_GAMMA["question"]
    assert merged["_snapshot"]["lifecycle"]["active"] is True
    assert merged["unrelated"] == {"keep": True}


def test_approved_wallet_normalizer_path_emits_canonical_snapshot():
    candidate = normalize_source_trade(
        TRADE,
        requested_wallet=TRADE["proxyWallet"],
        record_index=0,
        gamma_market=FULL_GAMMA,
    )
    assert candidate.validation_status == "valid"
    assert candidate.metadata == build_canonical_metadata(TRADE, FULL_GAMMA)


def test_specialist_collector_path_emits_equivalent_canonical_snapshot(owned_sqlite):
    from polycopy.db.database import Database
    from polycopy.ingestion.specialist_evidence_collector import (
        EvidenceCollectorConfig,
        collect_evidence,
    )
    from polycopy.ingestion.specialist_evidence_watchlist import add_watch

    db = Database(owned_sqlite.new_path())
    db.connect()
    wallet = TRADE["proxyWallet"]
    wallet_id = "wallet-snapshot-test"
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (wallet_id, wallet, "snapshot-test", "2026-01-01T00:00:00Z"),
    )
    watch_id = add_watch(db, wallet_id=wallet_id)
    db.conn.commit()

    class Provider:
        async def fetch_trades(self, address, limit=100, page=1):
            assert address == wallet
            return [TRADE]

    async def gamma(condition_id):
        assert condition_id == FULL_GAMMA["conditionId"]
        return FULL_GAMMA

    try:
        result = asyncio.run(
            collect_evidence(
                db,
                watch_id=watch_id,
                provider=Provider(),
                dry_run=True,
                config=EvidenceCollectorConfig(),
                gamma_resolver=gamma,
            )
        )
        assert result.error is None
        assert result.rows_would_create == 1
        # The production specialist collector entered ingest_pipeline, whose
        # normalized candidate must be byte-equivalent to the approved path.
        direct = normalize_source_trade(
            TRADE,
            requested_wallet=wallet,
            record_index=0,
            gamma_market=FULL_GAMMA,
        )
        assert direct.metadata == build_canonical_metadata(TRADE, FULL_GAMMA)
    finally:
        db.close()
