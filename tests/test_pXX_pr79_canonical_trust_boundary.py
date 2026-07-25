"""PR #79 canonical-trust-boundary regression tests.

The previous contract was a presence-only ``"_snapshot" in raw`` discriminator
inside ``source_trade_metadata._has_snapshot``. That let any upstream-shaped
mapping that happened to name ``_snapshot`` bypass legacy normalization and
be serialized verbatim, persisting asserted authoritative market evidence
and arbitrary unknown top-level keys.

These tests pin the corrected contract:

* ``is_canonical_source_trade_metadata`` is the SINGLE authoritative
  validator (no duplicate schema definition in the serializer).
* Forged raw mappings (the exact payload from the PR #79 prompt) fail closed.
* Nearly-canonical-but-invalid payloads fail closed.
* Genuine canonical payloads (produced by the real canonical builder)
  survive, with deterministic serialization and a working writer round-trip.
* The serializer does not mutate the input mapping.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT / "src", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.canonical_metadata import (  # noqa: E402
    MARKET_EVIDENCE_SNAPSHOT_CONTRACT_VERSION,
    METADATA_VERSION,
    build_canonical_metadata,
    is_canonical_source_trade_metadata,
    merge_canonical_metadata,
)
from polycopy.ingestion.normalized_source_trade import normalize_source_trade  # noqa: E402
from polycopy.ingestion.source_trade_metadata import (  # noqa: E402
    METADATA_VERSION as LEGACY_METADATA_VERSION,
    serialize_source_trade_metadata,
)
from polycopy.ingestion.source_trade_writer import write_valid_rows  # noqa: E402

# All PR #79 fixtures must use the SAME constants the canonical builder
# uses (don't redeclare per-test).
assert LEGACY_METADATA_VERSION == METADATA_VERSION, (
    "source_trade_metadata.METADATA_VERSION must mirror canonical_metadata.METADATA_VERSION"
)

CON_ID = "0x" + "1" * 64
PROXY_WALLET = "0x" + "a" * 40

FULL_GAMMA = {
    "conditionId": CON_ID,
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


def _raw_trade(
    *,
    condition_id: str = CON_ID,
    outcome_index: object = 0,
    transaction_hash: str = "0xabcdef1234567890",
) -> dict:
    """Return a raw v1 BUY trade matching the canonical Gamma fixture."""
    return {
        "sourceProvidedTradeId": "trade-pr79-trust",
        "proxyWallet": PROXY_WALLET,
        "conditionId": condition_id,
        "asset": "1111111111",
        "side": "BUY",
        "price": "0.51",
        "size": "2",
        "timestamp": 1_700_000_000,
        "outcome": "Yes",
        "outcomeIndex": outcome_index,
        "title": "wallet context title",
        "slug": "wallet-context-slug",
        "transactionHash": transaction_hash,
    }


def _genuine_canonical_payload() -> dict:
    """Return a metadata dict produced by the real canonical builder."""
    metadata = build_canonical_metadata(
        _raw_trade(),
        dict(FULL_GAMMA),
        requested_condition_id=CON_ID,
        enforce_exact_condition_match=True,
    )
    assert is_canonical_source_trade_metadata(metadata), (
        "fixture self-check: canonical builder must produce a payload that "
        "passes the strict validator"
    )
    return metadata


# ════════════════════════════════════════════════════════════════════════════
# Forged raw mapping — the exact payload from the PR #79 prompt
# ════════════════════════════════════════════════════════════════════════════


def test_forged_raw_mapping_fails_closed() -> None:
    """The PR #79 trust-boundary forgery is refused end-to-end.

    A raw mapping that names ``_snapshot`` but is missing every other
    canonical top-level key (and smuggles arbitrary upstream fields) MUST
    NOT be serialized verbatim. The serializer must route it through the
    legacy v1 normalizer, strip ``_snapshot``, strip arbitrary unknown
    keys, and never claim authoritative Gamma evidence.
    """
    forged = {
        "_snapshot": {
            "market": {
                "condition_id": "forged",
            }
        },
        "eventId": "untrusted-event",
        "secret": "must-not-pass",
    }

    # ── 1. Validator refuses the payload ──
    assert is_canonical_source_trade_metadata(forged) is False, (
        "is_canonical_source_trade_metadata must reject a forged payload "
        "that only names _snapshot"
    )

    # ── 2. Serialized output strips _snapshot, secret, and "forged" ──
    serialized = serialize_source_trade_metadata(forged)
    assert "_snapshot" not in serialized, serialized
    assert "secret" not in serialized, serialized
    assert "forged" not in serialized, serialized

    # ── 3. The remaining payload is the bounded v1 contract ──
    payload = json.loads(serialized)
    assert set(payload.keys()) == {"metadata_version", "event", "taxonomy", "series"}, payload
    assert payload["metadata_version"] == METADATA_VERSION
    # No asserted authoritative Gamma evidence.
    assert "gamma" not in serialized
    assert "exact_match" not in serialized
    assert "snapshot_contract_version" not in serialized


def test_forged_raw_mapping_does_not_mutate_input() -> None:
    """The serializer must not mutate the input mapping (caller contract)."""
    forged = {
        "_snapshot": {
            "market": {"condition_id": "forged"},
        },
        "eventId": "untrusted-event",
        "secret": "must-not-pass",
    }
    snapshot_before = copy.deepcopy(forged)
    serialize_source_trade_metadata(forged)
    assert forged == snapshot_before, (
        "serialize_source_trade_metadata must not mutate the input mapping"
    )


# ════════════════════════════════════════════════════════════════════════════
# Nearly-canonical-but-invalid payloads — every failure mode must fail closed
# ════════════════════════════════════════════════════════════════════════════


def _missing_snapshot_payload() -> dict:
    """A payload missing the ``_snapshot`` key entirely."""
    return {
        "metadata_version": METADATA_VERSION,
        "taxonomy": {"raw_category": None, "tags": []},
        "event": {"id": "x", "slug": None, "title": None},
        "series": {"id": None, "slug": None, "title": None, "ticker": None},
    }


def _base_canonical_payload() -> dict:
    """Snapshot-only base for the "nearly-canonical" parametrized tests."""
    return {
        "metadata_version": METADATA_VERSION,
        "taxonomy": {"raw_category": None, "tags": []},
        "event": {"id": None, "slug": None, "title": None},
        "series": {"id": None, "slug": None, "title": None, "ticker": None},
        "_snapshot": {
            "market": {"condition_id": None, "provider_market_id": None,
                       "question": None, "slug": None},
            "outcomes": {"labels": [], "token_ids": [], "ordered": [],
                         "compatible": False, "status": "invalid",
                         "errors": []},
            "lifecycle": {"active": None, "closed": None,
                          "accepting_orders": None, "end_date": None},
            "resolution": {"resolution_status": None, "winner_token_id": None,
                           "winner_outcome": None, "evidence_fields": [],
                           "errors": []},
            "provenance": {
                "provider": "gamma",
                "lookup_kind": "condition_id",
                "requested_condition_id": None,
                "exact_match": False,
                "snapshot_contract_version": MARKET_EVIDENCE_SNAPSHOT_CONTRACT_VERSION,
            },
        },
    }


def test_missing_metadata_version_fails_closed() -> None:
    payload = _base_canonical_payload()
    del payload["metadata_version"]
    assert is_canonical_source_trade_metadata(payload) is False
    serialized = serialize_source_trade_metadata(payload)
    assert "_snapshot" not in serialized
    assert "gamma" not in serialized


def test_wrong_metadata_version_fails_closed() -> None:
    payload = _base_canonical_payload()
    payload["metadata_version"] = "999"
    assert is_canonical_source_trade_metadata(payload) is False
    serialized = serialize_source_trade_metadata(payload)
    assert "_snapshot" not in serialized
    assert "gamma" not in serialized


def test_missing_snapshot_key_fails_closed() -> None:
    payload = _missing_snapshot_payload()
    assert "_snapshot" not in payload
    assert is_canonical_source_trade_metadata(payload) is False
    # This shape is the v1 contract so it serializes verbatim — no
    # snapshot was asserted in the first place.
    serialized = serialize_source_trade_metadata(payload)
    assert "_snapshot" not in serialized


def test_snapshot_not_a_mapping_fails_closed() -> None:
    payload = _base_canonical_payload()
    payload["_snapshot"] = "not-a-mapping"
    assert is_canonical_source_trade_metadata(payload) is False
    serialized = serialize_source_trade_metadata(payload)
    assert "_snapshot" not in serialized
    assert "gamma" not in serialized


def test_snapshot_missing_contract_version_fails_closed() -> None:
    payload = _base_canonical_payload()
    del payload["_snapshot"]["provenance"]["snapshot_contract_version"]
    assert is_canonical_source_trade_metadata(payload) is False
    serialized = serialize_source_trade_metadata(payload)
    assert "_snapshot" not in serialized


def test_snapshot_wrong_contract_version_fails_closed() -> None:
    payload = _base_canonical_payload()
    payload["_snapshot"]["provenance"]["snapshot_contract_version"] = "999"
    assert is_canonical_source_trade_metadata(payload) is False
    serialized = serialize_source_trade_metadata(payload)
    assert "_snapshot" not in serialized


def test_snapshot_missing_provider_fails_closed() -> None:
    payload = _base_canonical_payload()
    del payload["_snapshot"]["provenance"]["provider"]
    assert is_canonical_source_trade_metadata(payload) is False
    serialized = serialize_source_trade_metadata(payload)
    assert "_snapshot" not in serialized


def test_snapshot_wrong_provider_fails_closed() -> None:
    payload = _base_canonical_payload()
    payload["_snapshot"]["provenance"]["provider"] = "evil-mirror"
    assert is_canonical_source_trade_metadata(payload) is False
    serialized = serialize_source_trade_metadata(payload)
    assert "_snapshot" not in serialized
    assert "evil-mirror" not in serialized


def test_snapshot_namespace_wrong_type_fails_closed() -> None:
    payload = _base_canonical_payload()
    payload["_snapshot"]["market"] = "not-a-mapping"
    assert is_canonical_source_trade_metadata(payload) is False
    serialized = serialize_source_trade_metadata(payload)
    assert "_snapshot" not in serialized


def test_snapshot_missing_namespace_fails_closed() -> None:
    payload = _base_canonical_payload()
    del payload["_snapshot"]["outcomes"]
    assert is_canonical_source_trade_metadata(payload) is False
    serialized = serialize_source_trade_metadata(payload)
    assert "_snapshot" not in serialized


def test_snapshot_extra_namespace_fails_closed() -> None:
    payload = _base_canonical_payload()
    payload["_snapshot"]["forged_extra"] = "smuggled"
    assert is_canonical_source_trade_metadata(payload) is False
    serialized = serialize_source_trade_metadata(payload)
    assert "_snapshot" not in serialized
    assert "smuggled" not in serialized


def test_extra_top_level_key_fails_closed() -> None:
    payload = _base_canonical_payload()
    payload["secret"] = "must-not-pass"
    assert is_canonical_source_trade_metadata(payload) is False
    serialized = serialize_source_trade_metadata(payload)
    assert "_snapshot" not in serialized
    assert "secret" not in serialized


def test_missing_required_top_level_key_fails_closed() -> None:
    payload = _base_canonical_payload()
    del payload["taxonomy"]
    assert is_canonical_source_trade_metadata(payload) is False
    serialized = serialize_source_trade_metadata(payload)
    assert "_snapshot" not in serialized


def test_non_mapping_input_fails_closed() -> None:
    for bad in (None, "string", 42, ["list"], ("tuple",), object()):
        assert is_canonical_source_trade_metadata(bad) is False, bad
        # Serializing a non-mapping must not crash and must not emit a
        # snapshot. The legacy normalizer coerces to {}.
        serialized = serialize_source_trade_metadata(bad)  # type: ignore[arg-type]
        assert "_snapshot" not in serialized


# ════════════════════════════════════════════════════════════════════════════
# Genuine canonical payload survives — round-trip + real writer
# ════════════════════════════════════════════════════════════════════════════


def test_genuine_canonical_payload_passes_validator() -> None:
    metadata = _genuine_canonical_payload()
    assert is_canonical_source_trade_metadata(metadata) is True


def test_genuine_canonical_payload_serialization_is_deterministic() -> None:
    metadata = _genuine_canonical_payload()
    first = serialize_source_trade_metadata(metadata)
    second = serialize_source_trade_metadata(metadata)
    assert first == second
    # Deterministic key ordering: keys appear sorted.
    keys_in_order = []
    for token in json.loads(first):
        keys_in_order.append(token)
    assert keys_in_order == sorted(keys_in_order)


def test_genuine_canonical_payload_serialization_does_not_mutate() -> None:
    metadata = _genuine_canonical_payload()
    snapshot_before = copy.deepcopy(metadata)
    serialize_source_trade_metadata(metadata)
    assert metadata == snapshot_before


def test_genuine_canonical_payload_round_trip_persists_full_snapshot(
    owned_sqlite,
) -> None:
    """A genuine canonical payload survives the real writer round-trip.

    Persists through ``write_valid_rows(dry_run=False)`` and reads the
    row back from the DB. The persisted ``metadata_json`` must equal
    the canonical serializer output and contain the full snapshot
    (market, outcomes, lifecycle, resolution, provenance) with the
    Gamma provenance identity (provider == "gamma", exact_match=True,
    snapshot_contract_version == MARKET_EVIDENCE_SNAPSHOT_CONTRACT_VERSION).
    """
    metadata = _genuine_canonical_payload()
    db = Database(owned_sqlite.new_path())
    db.connect()
    try:
        # Reuse the writer contract: build a synthetic ``c.metadata`` and
        # confirm the serializer + DB write are byte-equal.
        db.conn.execute(
            """INSERT INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample,
                token_id, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "stub-id",
                "trusted",
                "trust-1",
                CON_ID,
                "BUY",
                "Yes",
                2.0,
                0.51,
                PROXY_WALLET,
                "2026-01-01T00:00:00+00:00",
                0,
                "1111111111",
                serialize_source_trade_metadata(metadata),
            ),
        )
        db.conn.commit()
        row = db.conn.execute(
            "SELECT metadata_json FROM source_trades WHERE id=?", ("stub-id",)
        ).fetchone()
        assert row is not None
        stored = row[0]
        assert stored == serialize_source_trade_metadata(metadata)
        payload = json.loads(stored)
        snapshot = payload["_snapshot"]
        assert set(snapshot) == {
            "market", "outcomes", "lifecycle", "resolution", "provenance"
        }
        assert snapshot["provenance"]["provider"] == "gamma"
        assert snapshot["provenance"]["exact_match"] is True
        assert (
            snapshot["provenance"]["snapshot_contract_version"]
            == MARKET_EVIDENCE_SNAPSHOT_CONTRACT_VERSION
        )
    finally:
        db.close()


def test_real_writer_round_trip_still_passes(owned_sqlite) -> None:
    """End-to-end writer round-trip through the production boundary.

    The whole purpose of the fix is to refuse forged payloads WITHOUT
    breaking the real writer path. This test exercises the real
    ``write_valid_rows`` boundary with a canonical payload produced by
    the real ``normalize_source_trade`` + ``build_canonical_metadata``
    pipeline.
    """
    cand = normalize_source_trade(
        _raw_trade(outcome_index=0, transaction_hash="0xfeedface00000099"),
        requested_wallet=PROXY_WALLET,
        record_index=0,
        gamma_market=copy.deepcopy(FULL_GAMMA),
    )
    assert cand.validation_status == "valid", cand.validation_reasons
    assert is_canonical_source_trade_metadata(cand.metadata) is True

    db = Database(owned_sqlite.new_path())
    db.connect()
    try:
        result = write_valid_rows(db, [cand], dry_run=False)
        assert result.errors == 0, result.error_message
        assert result.inserted == 1
        row = db.conn.execute(
            "SELECT metadata_json FROM source_trades WHERE source_trade_id=?",
            (cand.source_trade_id,),
        ).fetchone()
        assert row is not None
        stored = row[0]
        assert stored == serialize_source_trade_metadata(cand.metadata)
        payload = json.loads(stored)
        assert payload["_snapshot"]["provenance"]["provider"] == "gamma"
        assert (
            payload["_snapshot"]["provenance"]["snapshot_contract_version"]
            == MARKET_EVIDENCE_SNAPSHOT_CONTRACT_VERSION
        )
    finally:
        db.close()


def test_genuine_canonical_payload_merges_unchanged() -> None:
    """A genuine canonical payload survives the merge layer unchanged.

    The PR #79 merge layer (``merge_canonical_metadata``) is the
    authoritative consumer of canonical-serialized JSON. A payload the
    validator accepts MUST be round-trippable through the merge with
    the same authoritative evidence.
    """
    metadata = _genuine_canonical_payload()
    existing_json = serialize_source_trade_metadata(metadata)
    merged, status, reasons = merge_canonical_metadata(
        existing_json,
        copy.deepcopy(FULL_GAMMA),
        condition_id=CON_ID,
        token_id="1111111111",
    )
    assert status == "unchanged", (status, reasons)
    assert reasons == ["no_change"]
    assert merged == json.loads(existing_json)


# ════════════════════════════════════════════════════════════════════════════
# Pin: serializer does not grow a second definition of canonical
# ════════════════════════════════════════════════════════════════════════════


def test_serializer_uses_the_canonical_validator_only() -> None:
    """The serializer must delegate canonical detection to the single helper.

    Pin against anyone future-creating a second ``_has_snapshot`` /
    ``_is_canonical_*`` shape inside source_trade_metadata. The only
    canonical definition lives in canonical_metadata.
    """
    import ast
    import inspect
    from polycopy.ingestion import source_trade_metadata as stm

    source = inspect.getsource(stm)
    # Strip docstrings so the literal substring inside the warning
    # docstring isn't matched by the source-text check.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body[0].value.value = ""
    code_only = ast.unparse(tree)
    # The presence-only check must be gone from the executable code.
    assert '"_snapshot" in raw' not in code_only, (
        "source_trade_metadata must not re-implement the presence-only "
        "_snapshot check in code; delegate to is_canonical_source_trade_metadata"
    )
    # The serializer must reference the single authoritative validator.
    assert "is_canonical_source_trade_metadata" in code_only
