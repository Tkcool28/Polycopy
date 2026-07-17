"""Full-dictionary parity across every canonical_metadata producer (PR #71 S2).

Every evidence path MUST emit a byte-equivalent canonical metadata dict for the
same trusted Gamma market. Collection, backfill, and per-trade enrichment all
route through ``build_canonical_metadata`` (directly or via delegate), so the
nested shape (metadata_version, event, taxonomy, series) must be identical.

S2 narrow correction adds fail-closed REAL Gamma token membership via the
proven ``parse_clob_token_ids`` contract (clobTokenIds JSON-string OR bare
list), fail-closed existing-metadata parsing, metadata_version conflict
handling, and full namespace merge rules.
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
        "clobTokenIds": json.dumps(["0xtok_a", "0xtok_b"]),
    }
)


def _canon(meta) -> str:
    return json.dumps(meta, sort_keys=True)


# ---------------------------------------------------------------------------
# Producer parity
# ---------------------------------------------------------------------------


def test_collection_writer_matches_builder():
    """source_trade_metadata delegates to canonical_metadata — byte-equal."""
    a = build_canonical_metadata({}, GAMMA)
    b = source_trade_metadata.build_metadata_from_gamma_market({}, GAMMA)
    assert _canon(a) == _canon(b)
    for ns in ("event", "taxonomy", "series"):
        assert ns in a
    assert a["metadata_version"] == "1"


def test_merge_full_equals_collection_writer():
    merged, status, rc = merge_canonical_metadata(
        _canon({}), GAMMA, condition_id="0xcond1"
    )
    assert status == MERGE_FILLED
    assert _canon(merged) == _canon(build_canonical_metadata({}, GAMMA))


def test_all_three_producers_identical():
    writer = build_canonical_metadata({}, GAMMA)
    merged, _, _ = merge_canonical_metadata(_canon({}), GAMMA, condition_id="0xcond1")
    remerged, status, _ = merge_canonical_metadata(
        _canon(writer), GAMMA, condition_id="0xcond1"
    )
    assert status == MERGE_UNCHANGED
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
    no_cat = FakeMarket({"conditionId": "0xc2", "question": "Who wins?"})
    merged, status, rc = merge_canonical_metadata(None, no_cat, condition_id="0xc2")
    assert status == MERGE_UNAVAILABLE
    assert "taxonomy_unavailable" in rc
    built = build_canonical_metadata({}, no_cat)
    assert not built["taxonomy"].get("raw_category")


# ---------------------------------------------------------------------------
# REAL Gamma token membership (parse_clob_token_ids contract)
# ---------------------------------------------------------------------------


def _gamma_with_tokens(clob):
    return FakeMarket(
        {
            "conditionId": "0xc1",
            "category": "Politics",
            "clobTokenIds": clob,
        }
    )


def test_clobtokenids_json_string_accepted():
    """clobTokenIds as a JSON-encoded list string is parsed into membership."""
    gamma = _gamma_with_tokens(json.dumps(["0xtok_a", "0xtok_b"]))
    merged, status, rc = merge_canonical_metadata(
        None, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_FILLED
    assert merged["taxonomy"]["raw_category"] == "Politics"


def test_clobtokenids_bare_list_accepted():
    """clobTokenIds as a bare list is parsed into membership."""
    gamma = _gamma_with_tokens(["0xtok_a", "0xtok_b"])
    merged, status, rc = merge_canonical_metadata(
        None, gamma, condition_id="0xc1", token_id="0xtok_b"
    )
    assert status == MERGE_FILLED


def test_token_membership_fails_closed():
    """A trade token_id not in the Gamma condition is rejected."""
    gamma = _gamma_with_tokens(json.dumps(["0xtok_a", "0xtok_b"]))
    merged, status, rc = merge_canonical_metadata(
        None, gamma, condition_id="0xc1", token_id="0xtok_other"
    )
    assert status == MERGE_UNAVAILABLE
    assert "token_id_not_in_condition" in rc


def test_token_membership_missing_clobtokenids():
    """Token present but Gamma has no clobTokenIds -> unavailable."""
    gamma = FakeMarket({"conditionId": "0xc1", "category": "Politics"})
    merged, status, rc = merge_canonical_metadata(
        None, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_UNAVAILABLE
    assert "token_membership_unavailable" in rc


def test_token_membership_malformed_clobtokenids():
    """Malformed clobTokenIds JSON + source token -> unavailable."""
    gamma = _gamma_with_tokens("{not-json")
    merged, status, rc = merge_canonical_metadata(
        None, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_UNAVAILABLE
    assert "token_membership_unavailable" in rc


def test_token_membership_duplicate_ambiguous():
    """Duplicate matching token occurrences -> unavailable/ambiguous."""
    gamma = _gamma_with_tokens(json.dumps(["0xtok_a", "0xtok_a"]))
    merged, status, rc = merge_canonical_metadata(
        None, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_UNAVAILABLE
    assert "token_membership_ambiguous" in rc


# ---------------------------------------------------------------------------
# Existing metadata fail-closed
# ---------------------------------------------------------------------------


def test_malformed_existing_metadata_blocked():
    gamma = _gamma_with_tokens(json.dumps(["0xtok_a"]))
    raw = "{not valid json"
    merged, status, rc = merge_canonical_metadata(
        raw, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_UNAVAILABLE
    assert "existing_metadata_malformed_json" in rc
    # original raw value is returned so callers preserve it verbatim
    assert merged == raw


def test_non_object_existing_metadata_blocked():
    gamma = _gamma_with_tokens(json.dumps(["0xtok_a"]))
    raw = json.dumps([1, 2, 3])  # JSON array, not an object
    merged, status, rc = merge_canonical_metadata(
        raw, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_UNAVAILABLE
    assert "existing_metadata_not_object" in rc
    assert merged == raw


# ---------------------------------------------------------------------------
# metadata_version conflict
# ---------------------------------------------------------------------------


def test_metadata_version_two_blocked_not_rewritten():
    gamma = _gamma_with_tokens(json.dumps(["0xtok_a"]))
    existing = _canon({"metadata_version": "2", "taxonomy": {"raw_category": "Sports"}})
    merged, status, rc = merge_canonical_metadata(
        existing, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_CONFLICT
    assert "version_conflict" in rc
    # version "2" is NOT silently rewritten to "1"
    assert merged.get("metadata_version") == "2"
    assert merged["taxonomy"]["raw_category"] == "Sports"


# ---------------------------------------------------------------------------
# Namespace merge rules
# ---------------------------------------------------------------------------


def test_non_dict_canonical_namespace_blocked():
    gamma = _gamma_with_tokens(json.dumps(["0xtok_a"]))
    existing = _canon({"taxonomy": "not-a-dict"})
    merged, status, rc = merge_canonical_metadata(
        existing, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_CONFLICT
    assert any("not_dict_conflict" in c for c in rc)
    # original non-empty non-dict value preserved
    assert merged["taxonomy"] == "not-a-dict"


def test_missing_optional_gamma_field_preserves_existing():
    """Gamma lacks event/series; existing populated event/series is kept."""
    gamma = FakeMarket(
        {
            "conditionId": "0xc1",
            "category": "Politics",
            "clobTokenIds": json.dumps(["0xtok_a"]),
            # no events/series keys
        }
    )
    existing = _canon(
        {
            "metadata_version": "1",
            "taxonomy": {"raw_category": "Politics"},
            "event": {"id": "evt_kept", "slug": "kept", "title": "Kept Event"},
            "series": {"id": "s_kept", "slug": "kept", "title": "Kept Series"},
        }
    )
    merged, status, rc = merge_canonical_metadata(
        existing, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_UNCHANGED
    assert merged["event"]["id"] == "evt_kept"
    assert merged["series"]["id"] == "s_kept"


def test_existing_empty_field_filled_from_gamma():
    """Existing empty event + incoming populated event -> filled (not conflict)."""
    gamma = FakeMarket(
        {
            "conditionId": "0xc1",
            "category": "Politics",
            "clobTokenIds": json.dumps(["0xtok_a"]),
            "events": [{"id": "evt_new", "slug": "new", "title": "New Event"}],
            "series": [],
        }
    )
    existing = _canon(
        {
            "taxonomy": {"raw_category": "Politics"},
            "event": {"id": "", "slug": "", "title": ""},
        }
    )
    merged, status, rc = merge_canonical_metadata(
        existing, gamma, condition_id="0xc1", token_id="0xtok_a"
    )
    assert status == MERGE_FILLED
    assert merged["event"]["id"] == "evt_new"
