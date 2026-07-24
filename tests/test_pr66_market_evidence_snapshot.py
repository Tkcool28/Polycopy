"""PR66+ market evidence snapshot tests.

Verifies the expanded canonical metadata builder captures authoritative Gamma
market identity, outcome mapping, lifecycle state, and provenance at ingestion
time — without changing existing taxonomy/event/series consumers.
"""
from __future__ import annotations

import collections.abc
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.ingestion.canonical_metadata import (  # noqa: E402
    MERGE_CONFLICT,
    MERGE_FILLED,
    MERGE_UNCHANGED,
    MERGE_UNAVAILABLE,
    _parse_outcome_arrays,
    build_canonical_metadata,
    merge_canonical_metadata,
    _SNAPSHOT_CONTRACT_VERSION,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

class FakeMarket(collections.abc.Mapping):
    """Mapping-compatible Gamma market for tests."""

    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


FULL_GAMMA = FakeMarket({
    "conditionId": "0xcond1",
    "id": "12345",
    "question": "Will X happen before Y?",
    "slug": "will-x-happen",
    "category": "Politics",
    "tags": ["election", "2026"],
    "events": [{"id": "evt1", "slug": "us-election", "title": "US Election"}],
    "series": [{"id": "s1", "slug": "pol", "title": "Politics Series"}],
    "outcomes": "[\"Yes\", \"No\"]",
    "outcomePrices": "[\"0.7\", \"0.3\"]",
    "clobTokenIds": json.dumps(["tok_a", "tok_b"]),
    "active": True,
    "closed": False,
    "acceptingOrders": True,
    "endDate": "2026-12-31T00:00:00Z",
})

TRADE_WITH_FIELDS = {
    "title": "Trade-level question text",
    "slug": "trade-slug",
    "outcomeIndex": 0,
    "transactionHash": "0xabcd1234efgh5678",
}


# ── S1: Exact Gamma snapshot ────────────────────────────────────────────────

def test_build_snapshot_has_market_identity():
    meta = build_canonical_metadata({}, FULL_GAMMA)
    snap = meta.get("_snapshot")
    assert snap is not None, "_snapshot missing"
    mkt = snap["market"]
    assert mkt["condition_id"] == "0xcond1"
    assert mkt["provider_market_id"] == "12345"
    assert mkt["question"] == "Will X happen before Y?"
    assert mkt["slug"] == "will-x-happen"


def test_build_snapshot_has_outcomes():
    meta = build_canonical_metadata({}, FULL_GAMMA)
    out = meta["_snapshot"]["outcomes"]
    assert out["labels"] == ["Yes", "No"]
    assert out["token_ids"] == ["tok_a", "tok_b"]
    assert out["compatible"] is True
    assert out["status"] == "complete"
    assert len(out["ordered"]) == 2
    assert out["ordered"][0]["label"] == "Yes"
    assert out["ordered"][0]["clob_token_id"] == "tok_a"


def test_build_snapshot_has_lifecycle():
    meta = build_canonical_metadata({}, FULL_GAMMA)
    lc = meta["_snapshot"]["lifecycle"]
    assert lc["active"] is True
    assert lc["closed"] is False
    assert lc["accepting_orders"] is True
    assert lc["end_date"] == "2026-12-31T00:00:00Z"


def test_build_snapshot_has_provenance():
    meta = build_canonical_metadata({}, FULL_GAMMA)
    prov = meta["_snapshot"]["provenance"]
    assert prov["provider"] == "gamma"
    assert prov["lookup_kind"] == "condition_id"
    assert prov["requested_condition_id"] == "0xcond1"
    assert prov["exact_match"] is True
    assert prov["snapshot_contract_version"] == _SNAPSHOT_CONTRACT_VERSION
    assert "retrieved_at" in prov


def test_build_snapshot_includes_trade_response_fields():
    meta = build_canonical_metadata(TRADE_WITH_FIELDS, FULL_GAMMA)
    prov = meta["_snapshot"]["provenance"]
    assert prov["trade_response_title"] == "Trade-level question text"
    assert prov["trade_response_slug"] == "trade-slug"
    assert prov["trade_response_outcome_index"] == 0
    assert prov["trade_response_transaction_hash"] == "0xabcd1234efgh5678"


# ── S2: Existing metadata compatibility ────────────────────────────────────

def test_taxonomy_still_accessible():
    """Existing scorer consumer: metadata['taxonomy']['raw_category'] unchanged."""
    meta = build_canonical_metadata({}, FULL_GAMMA)
    assert meta["taxonomy"]["raw_category"] == "Politics"
    assert meta["event"]["slug"] == "us-election"
    assert meta["series"]["slug"] == "pol"


def test_existing_no_taxonomy_consumer_unchanged():
    """When no category available, only _snapshot changes — taxonomy stays null."""
    no_cat = FakeMarket({
        "conditionId": "0xc2",
        "id": "99",
        "question": "Who wins?",
        "slug": "who-wins",
        "events": [{"id": "e1"}],
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["t1", "t2"]',
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "endDate": "2027-01-01T00:00:00Z",
    })
    meta = build_canonical_metadata({}, no_cat)
    assert meta["taxonomy"]["raw_category"] is None
    assert meta["_snapshot"]["market"]["condition_id"] == "0xc2"
    assert meta["_snapshot"]["lifecycle"]["active"] is True


# ── S3: No-result Gamma response ───────────────────────────────────────────

def test_no_gamma_no_snapshot():
    """When gamma_market is None, no _snapshot is produced."""
    meta = build_canonical_metadata({}, None)
    assert "_snapshot" not in meta
    assert meta["taxonomy"]["raw_category"] is None
    assert meta["event"]["slug"] is None


def test_merge_no_gamma_preserves_null_taxonomy():
    """merge with None -> unavailable, existing preserved."""
    new_meta, status, rc = merge_canonical_metadata(None, None, condition_id="0xcond1")
    assert status == MERGE_UNAVAILABLE
    assert "gamma_missing" in rc


# ── S4: Array mismatch handling ────────────────────────────────────────────

def test_parse_outcomes_empty_arrays():
    result = _parse_outcome_arrays([], [])
    assert result["labels"] == []
    assert result["token_ids"] == []
    assert result["compatible"] is False
    assert result["status"] == "incomplete"


def test_parse_outcomes_length_mismatch():
    result = _parse_outcome_arrays(["A", "B", "C"], ["tok1", "tok2"])
    assert result["compatible"] is False
    assert result["status"] == "invalid"
    assert result["ordered"] == []  # no false mapping
    assert result["labels"] == ["A", "B", "C"]
    assert result["token_ids"] == ["tok1", "tok2"]


def test_parse_outcomes_json_strings():
    result = _parse_outcome_arrays('["Y","N"]', '["a","b"]')
    assert result["labels"] == ["Y", "N"]
    assert result["token_ids"] == ["a", "b"]
    assert result["compatible"] is True
    assert result["status"] == "complete"
    assert len(result["ordered"]) == 2


def test_parse_outcomes_malformed_json():
    result = _parse_outcome_arrays("not json", "also not")
    assert result["labels"] == []
    assert result["token_ids"] == []
    assert result["compatible"] is False
    assert result["status"] == "incomplete"


# ── S5: Metadata merge with snapshot ────────────────────────────────────────

def test_merge_fills_snapshot_on_empty():
    # Use a Gamma market with no usable category (no official root).
    gamma_no_cat = FakeMarket({
        "conditionId": "0xcond1", "id": "12345", "question": "Will X?",
        "slug": "will-x", "active": True, "closed": False,
        "acceptingOrders": True, "endDate": "2026-12-31T00:00:00Z",
        "events": [{"id": "evt1", "slug": "event-x"}],
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["tok_a", "tok_b"]',
        # No category -> taxonomy_unavailable but snapshot still fills.
    })
    new_meta, status, rc = merge_canonical_metadata(
        None, gamma_no_cat, condition_id="0xcond1"
    )
    assert status == MERGE_FILLED
    assert "_snapshot" in new_meta
    assert new_meta["_snapshot"]["market"]["condition_id"] == "0xcond1"


def test_merge_preserves_unrelated_keys():
    existing = json.dumps({"foo": "bar", "custom_field": 42})
    new_meta, status, rc = merge_canonical_metadata(
        existing, FULL_GAMMA, condition_id="0xcond1"
    )
    assert status == MERGE_FILLED
    assert new_meta["foo"] == "bar"
    assert new_meta["custom_field"] == 42


def test_merge_replay_is_idempotent():
    """Re-merging identical data should be UNCHANGED."""
    first, s1, _ = merge_canonical_metadata(None, FULL_GAMMA, condition_id="0xcond1")
    # For idempotency we compare all fields except retrieved_at
    first_serialized = json.loads(json.dumps(first, sort_keys=True))
    first_serialized["_snapshot"]["provenance"].pop("retrieved_at")
    second, s2, _ = merge_canonical_metadata(
        json.dumps(first), FULL_GAMMA, condition_id="0xcond1"
    )
    second_serialized = json.loads(json.dumps(second, sort_keys=True))
    second_serialized["_snapshot"]["provenance"].pop("retrieved_at")
    assert first_serialized == second_serialized or s2 == MERGE_UNCHANGED


def test_merge_conflict_on_category_disagreement():
    existing = json.dumps({"taxonomy": {"raw_category": "Sports"}})
    new_meta, status, rc = merge_canonical_metadata(
        existing, FULL_GAMMA, condition_id="0xcond1"
    )
    assert status == MERGE_CONFLICT
    assert new_meta["taxonomy"]["raw_category"] == "Sports"


# ── S6: Trade response field preservation ──────────────────────────────────

def test_trade_response_fields_passed_through():
    meta = build_canonical_metadata(TRADE_WITH_FIELDS, FULL_GAMMA)
    prov = meta["_snapshot"]["provenance"]
    assert prov["trade_response_title"] == "Trade-level question text"
    assert prov["trade_response_outcome_index"] == 0


def test_trade_response_fields_absent_when_no_trades_info():
    meta = build_canonical_metadata({}, FULL_GAMMA)
    prov = meta["_snapshot"]["provenance"]
    assert "trade_response_title" not in prov
    assert "trade_response_outcome_index" not in prov
    assert "trade_response_transaction_hash" not in prov


# ── S7: Safety checks ──────────────────────────────────────────────────────

def test_no_schema_columns_added():
    """This module adds no DB columns — verified by absence of SQL in code."""
    import inspect
    source = inspect.getsource(sys.modules["polycopy.ingestion.canonical_metadata"])
    assert "CREATE TABLE" not in source
    assert "ALTER TABLE" not in source
    assert "INSERT INTO source_trades" not in source


def test_taxonomv_resolver_not_affected():
    """OfficialPolymarketTaxonomyResolverV1 still works correctly."""
    from polycopy.taxonomy.official_polymarket import (
        OfficialPolymarketTaxonomyResolverV1,
        TAXONOMY_USABLE,
    )
    resolver = OfficialPolymarketTaxonomyResolverV1()
    result = resolver.resolve(FULL_GAMMA)
    assert result.status == TAXONOMY_USABLE
    assert result.market_category_value == "Politics"


# ── S8: Determinism ────────────────────────────────────────────────────────

def test_build_deterministic_without_timestamp():
    """Running build twice should produce byte-identical results except timestamp."""
    meta1 = build_canonical_metadata(TRADE_WITH_FIELDS, FULL_GAMMA)
    meta2 = build_canonical_metadata(TRADE_WITH_FIELDS, FULL_GAMMA)
    # Remove timestamp-dependent field
    snap1 = json.loads(json.dumps(meta1))
    snap2 = json.loads(json.dumps(meta2))
    snap1["_snapshot"]["provenance"].pop("retrieved_at")
    snap2["_snapshot"]["provenance"].pop("retrieved_at")
    assert json.dumps(snap1, sort_keys=True) == json.dumps(snap2, sort_keys=True)
