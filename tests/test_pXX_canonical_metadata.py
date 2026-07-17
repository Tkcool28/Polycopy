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
    # Re-merging a row that already equals the builder output must be UNCHANGED.
    existing = json.dumps(build_canonical_metadata({}, GAMMA), sort_keys=True)
    new_meta, status, rc = merge_canonical_metadata(existing, GAMMA, condition_id="0xcond1")
    assert status == MERGE_UNCHANGED
    assert new_meta["taxonomy"]["raw_category"] == "Politics"
    assert "no_change" in rc


def test_merge_conflict():
    existing = json.dumps(
        {"taxonomy": {"raw_category": "Sports"}}, sort_keys=True
    )
    new_meta, status, rc = merge_canonical_metadata(existing, GAMMA, condition_id="0xcond1")
    assert status == MERGE_CONFLICT
    # existing value preserved; no overwrite
    assert new_meta["taxonomy"]["raw_category"] == "Sports"
    assert any("conflict" in c for c in rc)


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


# ── S3 canonical-metadata tags contract ──────────────────────────────────────
# tags rules:
#   * missing / None / "" / empty list / empty tuple  -> fillable
#   * non-empty list                                  -> compared as a normalized
#                                                      order-insensitive set
#   * non-empty non-list (str/dict/scalar)            -> TYPE CONFLICT (not
#                                                      overwritten)


def _gamma_with_tags(tags):
    return FakeMarket(
        {
            "conditionId": "0xc1",
            "category": "Politics",
            "clobTokenIds": json.dumps(["0xtok_a"]),
            "tags": tags,
        }
    )


def test_missing_tags_fillable_from_gamma():
    gamma = _gamma_with_tags(["election"])
    existing = json.dumps({"taxonomy": {"raw_category": "Politics"}}, sort_keys=True)
    merged, status, rc = merge_canonical_metadata(
        existing, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_FILLED
    assert merged["taxonomy"].get("tags") == ["election"], merged


def test_empty_string_tags_fillable():
    gamma = _gamma_with_tags(["election"])
    existing = json.dumps(
        {"taxonomy": {"raw_category": "Politics", "tags": ""}}, sort_keys=True
    )
    merged, status, rc = merge_canonical_metadata(
        existing, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_FILLED
    assert merged["taxonomy"]["tags"] == ["election"], merged


def test_list_tags_compared_as_normalized_set():
    gamma = _gamma_with_tags(["election", "2026"])
    # Start from the real producer output (full canonical shape, empty event/
    # series) and only reorder the tags — that is the realistic stored row.
    existing = build_canonical_metadata({}, gamma)
    existing["taxonomy"]["tags"] = ["2026", "election"]
    existing = json.dumps(existing, sort_keys=True)
    merged, status, rc = merge_canonical_metadata(
        existing, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_UNCHANGED, (status, rc)
    assert merged["taxonomy"]["tags"] == ["2026", "election"], merged


def test_wrong_type_tags_string_conflict_not_overwritten():
    gamma = _gamma_with_tags(["election"])
    existing_tags = "election"  # non-empty non-list -> type conflict
    existing = json.dumps(
        {"taxonomy": {"raw_category": "Politics", "tags": existing_tags}},
        sort_keys=True,
    )
    merged, status, rc = merge_canonical_metadata(
        existing, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_CONFLICT
    assert any("type_conflict" in c for c in rc), rc
    assert merged["taxonomy"]["tags"] == existing_tags, merged


def test_wrong_type_tags_dict_conflict_not_overwritten():
    gamma = _gamma_with_tags(["election"])
    existing_tags = {"a": 1}  # non-empty non-list -> type conflict
    existing = json.dumps(
        {"taxonomy": {"raw_category": "Politics", "tags": existing_tags}},
        sort_keys=True,
    )
    merged, status, rc = merge_canonical_metadata(
        existing, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_CONFLICT
    assert any("type_conflict" in c for c in rc), rc
    assert merged["taxonomy"]["tags"] == existing_tags, merged


def test_wrong_type_tags_scalar_conflict_not_overwritten():
    gamma = _gamma_with_tags(["election"])
    existing_tags = 42  # non-empty scalar -> type conflict
    existing = json.dumps(
        {"taxonomy": {"raw_category": "Politics", "tags": existing_tags}},
        sort_keys=True,
    )
    merged, status, rc = merge_canonical_metadata(
        existing, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_CONFLICT
    assert any("type_conflict" in c for c in rc), rc
    assert merged["taxonomy"]["tags"] == existing_tags, merged
