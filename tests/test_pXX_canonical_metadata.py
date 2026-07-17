"""Canonical metadata builder + safe-merge parity/conflict tests (plan Task 3/4)."""
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
    build_canonical_metadata,
    merge_canonical_metadata,
    normalize_source_trade_metadata,
)


class FakeMarket(collections.abc.Mapping):
    """Plan §5b Mapping-compatible Gamma market."""

    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


GAMMA = FakeMarket(
    {
        "conditionId": "0xcond1",
        "category": "Politics",
        "tags": ["election", "2026"],
        "events": [{"id": "evt1", "slug": "us-election", "title": "US Election"}],
        "series": [{"id": "s1", "slug": "pol", "title": "Politics Series"}],
    }
)


def test_build_canonical_shape():
    meta = build_canonical_metadata({}, GAMMA)
    assert "taxonomy" in meta and "raw_category" in meta["taxonomy"]
    assert meta["taxonomy"]["raw_category"] == "Politics"
    assert meta["event"]["slug"] == "us-election"
    assert meta["series"]["slug"] == "pol"
    # deterministic JSON
    assert json.dumps(meta, sort_keys=True) == json.dumps(meta, sort_keys=True)


def test_build_matches_in_collection_writer():
    # source_trade_metadata.build_metadata_from_gamma_market must be byte-equal.
    from polycopy.ingestion import source_trade_metadata

    a = build_canonical_metadata({}, GAMMA)
    b = source_trade_metadata.build_metadata_from_gamma_market({}, GAMMA)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_merge_filled():
    new_meta, status, rc = merge_canonical_metadata(None, GAMMA, condition_id="0xcond1")
    assert status == MERGE_FILLED
    assert new_meta["taxonomy"]["raw_category"] == "Politics"


def test_merge_preserves_unrelated():
    existing = json.dumps({"foo": "bar"})
    new_meta, status, rc = merge_canonical_metadata(existing, GAMMA, condition_id="0xcond1")
    assert status == MERGE_FILLED
    assert new_meta.get("foo") == "bar"
    assert new_meta["taxonomy"]["raw_category"] == "Politics"


def test_merge_unchanged():
    existing = json.dumps(build_canonical_metadata({}, GAMMA), sort_keys=True)
    new_meta, status, rc = merge_canonical_metadata(existing, GAMMA, condition_id="0xcond1")
    assert status == MERGE_UNCHANGED
    assert new_meta["taxonomy"]["raw_category"] == "Politics"


def test_merge_conflict():
    existing = json.dumps(
        {"taxonomy": {"raw_category": "Sports"}}, sort_keys=True
    )
    new_meta, status, rc = merge_canonical_metadata(existing, GAMMA, condition_id="0xcond1")
    assert status == MERGE_CONFLICT
    # existing value preserved; no overwrite
    assert new_meta["taxonomy"]["raw_category"] == "Sports"
    assert "taxonomy_conflict" in rc


def test_merge_condition_mismatch_unavailable():
    new_meta, status, rc = merge_canonical_metadata(None, GAMMA, condition_id="0xWRONG")
    assert status == MERGE_UNAVAILABLE
    assert "condition_id_mismatch" in rc


def test_merge_missing_gamma_unavailable():
    new_meta, status, rc = merge_canonical_metadata(None, None, condition_id="0xcond1")
    assert status == MERGE_UNAVAILABLE
    assert "gamma_missing" in rc


def test_merge_no_title_inference():
    # A market with no category but a title must NOT infer taxonomy.
    no_cat = FakeMarket({"conditionId": "0xc2", "question": "Who wins?"})
    new_meta, status, rc = merge_canonical_metadata(None, no_cat, condition_id="0xc2")
    assert status == MERGE_UNAVAILABLE
    assert "taxonomy_unavailable" in rc
