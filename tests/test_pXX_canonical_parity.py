"""Full-dictionary parity across every canonical_metadata producer (PR #71 S2).

Every evidence path MUST emit a byte-equivalent canonical metadata dict for the
same trusted Gamma market. Collection, backfill, and per-trade enrichment all
route through ``build_canonical_metadata`` (directly or via delegate), so the
nested shape (metadata_version, event, taxonomy, series) must be identical.
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
    build_canonical_metadata,
    merge_canonical_metadata,
)
from polycopy.ingestion import source_trade_metadata  # noqa: E402


class FakeMarket(collections.abc.Mapping):
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


def _canon(meta: dict) -> str:
    return json.dumps(meta, sort_keys=True)


def test_collection_writer_matches_builder():
    """source_trade_metadata delegates to canonical_metadata — byte-equal."""
    a = build_canonical_metadata({}, GAMMA)
    b = source_trade_metadata.build_metadata_from_gamma_market({}, GAMMA)
    assert _canon(a) == _canon(b)
    # every namespace present and metadata_version stamped
    for ns in ("event", "taxonomy", "series"):
        assert ns in a
    assert a["metadata_version"] == "1"


def test_merge_full_equals_collection_writer():
    """Merging an empty existing row must yield the collection-writer shape."""
    empty = _canon({})
    merged, status, rc = merge_canonical_metadata(empty, GAMMA, condition_id="0xcond1")
    assert status == "filled"
    assert _canon(merged) == _canon(build_canonical_metadata({}, GAMMA))


def test_all_three_producers_identical():
    """Collection writer, merge-from-empty, and re-merge of a filled row are
    all byte-equivalent for the same Gamma market."""
    writer = build_canonical_metadata({}, GAMMA)
    merged, _, _ = merge_canonical_metadata(
        _canon({}), GAMMA, condition_id="0xcond1"
    )
    # idempotent re-merge of the filled product must not drift
    remerged, status, _ = merge_canonical_metadata(
        _canon(writer), GAMMA, condition_id="0xcond1"
    )
    assert status == "unchanged"
    assert _canon(writer) == _canon(merged) == _canon(remerged)


def test_metadata_version_present_everywhere():
    filled, _, _ = merge_canonical_metadata(None, GAMMA, condition_id="0xcond1")
    unchanged, _, _ = merge_canonical_metadata(
        _canon(filled), GAMMA, condition_id="0xcond1"
    )
    unavailable, _, _ = merge_canonical_metadata(None, None, condition_id="0xcond1")
    conflict, _, _ = merge_canonical_metadata(
        _canon({"taxonomy": {"raw_category": "Sports"}}),
        GAMMA,
        condition_id="0xcond1",
    )
    for m in (filled, unchanged, unavailable, conflict):
        assert m.get("metadata_version") == "1", m


def test_no_title_inference_in_any_producer():
    """No producer may derive taxonomy from a question/title when category is
    absent — all must report taxonomy unavailable / leave it empty."""
    no_cat = FakeMarket({"conditionId": "0xc2", "question": "Who wins?"})
    merged, status, rc = merge_canonical_metadata(None, no_cat, condition_id="0xc2")
    assert status == "unavailable"
    assert "taxonomy_unavailable" in rc
    built = build_canonical_metadata({}, no_cat)
    assert not built["taxonomy"].get("raw_category")


def test_token_membership_fails_closed():
    """A trade token_id that is not in the Gamma condition is rejected."""
    gamma = FakeMarket(
        {
            "conditionId": "0xc1",
            "category": "Politics",
            "tokens": [{"tokenId": "0xtok_a"}],
        }
    )
    merged, status, rc = merge_canonical_metadata(
        None, gamma, condition_id="0xc1", token_id="0xtok_other"
    )
    assert status == "unavailable"
    assert "token_id_not_in_condition" in rc


def test_token_membership_accepted():
    gamma = FakeMarket(
        {
            "conditionId": "0xc1",
            "category": "Politics",
            "tokens": [{"tokenId": "0xtok_a"}],
        }
    )
    merged, status, rc = merge_canonical_metadata(
        None, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == "filled"
    assert merged["taxonomy"]["raw_category"] == "Politics"


def test_conflict_blocks_across_namespaces():
    """Conflicting non-empty event OR series (not just taxonomy) blocks merge."""
    gamma = FakeMarket(
        {
            "conditionId": "0xc1",
            "category": "Politics",
            "events": [{"id": "evt_new", "slug": "new", "title": "New"}],
            "series": [{"id": "s_new", "slug": "new", "title": "New"}],
        }
    )
    existing = _canon(
        {
            "taxonomy": {"raw_category": "Politics"},
            "event": {"id": "evt_old", "slug": "old", "title": "Old"},
            "series": {"id": "s_old", "slug": "old", "title": "Old"},
        }
    )
    merged, status, rc = merge_canonical_metadata(existing, gamma, condition_id="0xc1")
    assert status == "conflict"
    # existing preserved, not overwritten
    assert merged["event"]["id"] == "evt_old"
    assert merged["series"]["id"] == "s_old"
