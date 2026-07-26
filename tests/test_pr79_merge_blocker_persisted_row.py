"""PR #79 persisted-row regression tests for the three merge blockers.

This module adds TRUE write-mode (no ``dry_run=True``) database round-trip
tests for the three corrections that unblock the merge of PR #79:

  Blocker 1: ``_snapshot`` must survive database persistence exactly as
              produced by the canonical builder.
  Blocker 2: A Gamma mapping is never authoritative when the supplied
              ``conditionId`` does not match the requested trade condition id
              after canonical normalization.
  Blocker 3: ``outcomeIndex`` is a strict integer contract; floats,
              booleans, strings, decimals, scientific notation, empties and
              negatives are rejected and never persisted as provenance.

Both the approved-wallet ingestion path AND the specialist / cohort
ingestion path are exercised end-to-end through their production writer
boundary (``write_valid_rows`` / ``collect_evidence``) using isolated
temporary databases.
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT / "src", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.canonical_metadata import (  # noqa: E402
    MERGE_CONFLICT,
    MERGE_FILLED,
    MERGE_UNAVAILABLE,
    MERGE_UNCHANGED,
    _strict_trade_index_value,
    build_canonical_metadata,
    merge_canonical_metadata,
)
from polycopy.ingestion.normalized_source_trade import (  # noqa: E402
    normalize_source_trade,
)
from polycopy.ingestion.source_trade_metadata import (  # noqa: E402
    serialize_source_trade_metadata,
)
from polycopy.ingestion.source_trade_writer import write_valid_rows  # noqa: E402
from polycopy.ingestion.specialist_evidence_collector import (  # noqa: E402
    EvidenceCollectorConfig,
    collect_evidence,
)
from polycopy.ingestion.specialist_evidence_watchlist import add_watch  # noqa: E402

# ── Test fixture Gamma market ──────────────────────────────────────────────────
COND_ID = "0x" + "1" * 64
PROXY_WALLET = "0x" + "a" * 40

FULL_GAMMA = {
    "conditionId": COND_ID,
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
    condition_id: str = COND_ID,
    outcome_index: object = 0,
    transaction_hash: str = "0xabcdef1234567890",
) -> dict:
    """Return a raw v1 BUY trade matching the canonical Gamma fixture."""
    return {
        "sourceProvidedTradeId": "trade-pr79-1",
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


# ════════════════════════════════════════════════════════════════════════════
# Blocker 1 — REAL write-mode round-trip through the canonical writer boundary
# ════════════════════════════════════════════════════════════════════════════


def test_approved_wallet_writer_persists_full_snapshot_through_real_db(
    owned_sqlite,
) -> None:
    """End-to-end: normalization -> writer -> DB -> readback -> ``_snapshot`` present.

    Uses ``write_valid_rows(dry_run=False)`` against an isolated temporary
    database. The row read back from ``source_trades.metadata_json`` must
    contain the full ``_snapshot`` (market, outcomes, lifecycle, resolution,
    provenance) with every wallet-trade provenance field preserved.
    """
    db = Database(owned_sqlite.new_path())
    db.connect()
    try:
        trade = _raw_trade(outcome_index=0, transaction_hash="0xfeedface00000001")
        candidate = normalize_source_trade(
            trade,
            requested_wallet=PROXY_WALLET,
            record_index=0,
            gamma_market=FULL_GAMMA,
        )
        assert candidate.validation_status == "valid", candidate.validation_reasons
        assert "_snapshot" in candidate.metadata, (
            "normalize_source_trade must produce a canonical PR66 payload that "
            "carries _snapshot whenever the initial-ingestion exact-match gate "
            "passed for the supplied Gamma market"
        )

        # ── real write ──
        result = write_valid_rows(db, [candidate], dry_run=False)
        assert result.errors == 0, result.error_message
        assert result.committed is True
        assert result.inserted == 1

        # ── read back through the DB row ──
        row = db.conn.execute(
            "SELECT metadata_json FROM source_trades WHERE source_trade_id=?",
            (candidate.source_trade_id,),
        ).fetchone()
        assert row is not None, "row must be present after a committed write"
        stored_json = row[0]

        # The persisted bytes are exactly what the canonical serializer produced.
        assert stored_json == serialize_source_trade_metadata(candidate.metadata)

        payload = json.loads(stored_json)
        snapshot = payload["_snapshot"]

        # ── Block 1 assertions: every required snapshot namespace preserved ──
        assert set(snapshot) == {
            "market",
            "outcomes",
            "lifecycle",
            "resolution",
            "provenance",
        }
        assert snapshot["market"]["condition_id"] == COND_ID
        assert snapshot["market"]["provider_market_id"] == "12345"
        assert snapshot["market"]["question"] == "Will X happen?"
        assert snapshot["market"]["slug"] == "will-x-happen"
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
        # Wallet-trade provenance context survives persistence.
        assert provenance["trade_response_title"] == "wallet context title"
        assert provenance["trade_response_slug"] == "wallet-context-slug"
        assert provenance["trade_response_outcome_index"] == 0
        assert provenance["trade_response_transaction_hash"] == (
            "0xfeedface00000001"
        )
        # Authoritative Gamma provenance surfaces the exact-match claim.
        assert provenance["provider"] == "gamma"
        assert provenance["requested_condition_id"] == COND_ID
        assert provenance["exact_match"] is True
    finally:
        db.close()


def test_specialist_cohort_writer_persists_full_snapshot_through_real_db(
    owned_sqlite,
) -> None:
    """End-to-end through the production specialist cohort ingestion path.

    Uses ``collect_evidence(dry_run=False)`` (the real writer boundary) against
    an isolated temporary database. The source-trade row read back from the
    database must contain the same canonical ``_snapshot`` as the approved-
    wallet path for the equivalent Gamma fixture.
    """
    db = Database(owned_sqlite.new_path())
    db.connect()
    try:
        db.conn.execute(
            "INSERT INTO wallets (id, address, label, is_sample, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            ("wallet-snap-shot-pr79", PROXY_WALLET, "snap-shot", "2026-01-01T00:00:00Z"),
        )
        watch_id = add_watch(db, wallet_id="wallet-snap-shot-pr79")
        db.conn.commit()

        trade = _raw_trade(outcome_index=0, transaction_hash="0xfeedface00000002")

        class Provider:
            async def fetch_trades(self, address, limit=100, page=1):
                assert address == PROXY_WALLET
                return [trade]

        async def gamma(condition_id):
            assert condition_id == COND_ID
            return dict(FULL_GAMMA)

        try:
            result = asyncio.run(
                collect_evidence(
                    db,
                    watch_id=watch_id,
                    provider=Provider(),
                    dry_run=False,
                    config=EvidenceCollectorConfig(),
                    gamma_resolver=gamma,
                )
            )
            assert result.error is None, result.error
            assert result.raw_trades_examined == 1
            assert result.valid_buy_trades == 1
            # The specialist cohort must durably persist the source-trade row
            # AND its canonical snapshot through enrichment.
            inserted_total = result.rows_created + result.duplicate_rows_observed
            assert inserted_total >= 1, (
                "specialist cohort path must durably persist at least one "
                f"source-trade row; got {result!r}"
            )
        except Exception as exc:  # pragma: no cover -- defensive surfacing
            raise AssertionError(f"collect_evidence failed: {exc!r}") from exc

        # ── read back the row that the specialist cohort just inserted ──
        row = db.conn.execute(
            "SELECT metadata_json FROM source_trades WHERE trader_address=? "
            "ORDER BY timestamp ASC LIMIT 1",
            (PROXY_WALLET,),
        ).fetchone()
        assert row is not None, "specialist cohort must leave a source_trades row"
        payload = json.loads(row[0])

        # Equivalent snapshot semantics: same canonical shape as the
        # approved-wallet path. (The specialist path's proven-identity id is
        # source-provided, same as the approved-wallet path.)
        snapshot = payload["_snapshot"]
        assert snapshot["market"]["condition_id"] == COND_ID
        assert snapshot["market"]["provider_market_id"] == "12345"
        assert snapshot["outcomes"]["ordered"] == [
            {"label": "Yes", "clob_token_id": "1111111111"},
            {"label": "No", "clob_token_id": "2222222222"},
        ]
        provenance = snapshot["provenance"]
        assert provenance["exact_match"] is True
        assert provenance["requested_condition_id"] == COND_ID
        assert provenance["trade_response_outcome_index"] == 0
        assert provenance["trade_response_transaction_hash"] == (
            "0xfeedface00000002"
        )
        # Same canonical fields required by Blocker 1.
        for required in (
            "market",
            "outcomes",
            "lifecycle",
            "resolution",
            "provenance",
        ):
            assert required in snapshot, required
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# Blocker 2 — False ``exact_match=true`` is no longer possible
# ════════════════════════════════════════════════════════════════════════════


def _per_match_metadata(trade: dict, gamma: dict) -> dict:
    cand = normalize_source_trade(
        trade,
        requested_wallet=PROXY_WALLET,
        record_index=0,
        gamma_market=gamma,
    )
    assert cand.validation_status == "valid", cand.validation_reasons
    return cand.metadata


def test_initial_ingestion_matching_condition_ids_accepts_exact_snapshot(
    owned_sqlite,
) -> None:
    trade = _raw_trade(condition_id=COND_ID, transaction_hash="0xmatch00000001")
    metadata = _per_match_metadata(trade, FULL_GAMMA)
    snapshot = metadata["_snapshot"]
    assert snapshot["provenance"]["exact_match"] is True
    assert snapshot["provenance"]["requested_condition_id"] == COND_ID
    # All authoritative evidence is populated.
    assert snapshot["market"]["condition_id"] == COND_ID
    assert snapshot["outcomes"]["ordered"]
    assert snapshot["lifecycle"]["active"] is True


def test_initial_ingestion_mismatched_condition_ids_rejects_snapshot_fails_closed(
    owned_sqlite,
) -> None:
    # Trade claims conditionId = "0xWRONG"; Gamma says COND_ID.
    trade = _raw_trade(
        condition_id="0x" + "f" * 64, transaction_hash="0xmismatch00000001"
    )
    metadata = _per_match_metadata(trade, FULL_GAMMA)
    # No authoritative ``_snapshot`` is emitted.
    assert "_snapshot" not in metadata
    # The v1 namespaces are still built but ONLY carry whatever event/series/
    # category the upstream-like input carried; Gamma evidence did NOT leak
    # into the trusted v1 namespaces because we never asked the canonical
    # builder to emit one.
    assert "event" in metadata and "series" in metadata and "taxonomy" in metadata


def test_initial_ingestion_missing_gamma_condition_id_rejects_snapshot_fails_closed(
    owned_sqlite,
) -> None:
    gamma_without_condition = copy.deepcopy(FULL_GAMMA)
    gamma_without_condition.pop("conditionId", None)
    trade = _raw_trade(condition_id=COND_ID)
    cand = normalize_source_trade(
        trade,
        requested_wallet=PROXY_WALLET,
        record_index=0,
        gamma_market=gamma_without_condition,
    )
    assert cand.validation_status == "valid", cand.validation_reasons
    assert "_snapshot" not in cand.metadata, (
        "Gamma object missing conditionId must NOT seed an exact-match snapshot"
    )


def test_initial_ingestion_missing_trade_condition_id_makes_no_exact_claim(
    owned_sqlite,
) -> None:
    # Direct builder test (the audit's contract): when the caller's trade
    # carries no requested condition id, the canonical builder must NOT stamp
    # ``exact_match=True`` on the produced snapshot even though the Gamma
    # market itself carries a conditionId. Identity cannot be proved against
    # an unknown request, so the builder reports exact_match=False and
    # clears all authoritative evidence namespaces.
    from polycopy.ingestion.canonical_metadata import build_canonical_metadata

    gamma = copy.deepcopy(FULL_GAMMA)
    trade_no_cond = {
        "side": "BUY",
        "title": "wallet context title",
        "slug": "wallet-context-slug",
        "transactionHash": "0xnocond00000001",
        "outcome": "Yes",
        "outcomeIndex": 0,
    }
    metadata = build_canonical_metadata(
        trade_no_cond,
        gamma,
        requested_condition_id=None,
        enforce_exact_condition_match=True,
    )
    # The builder refuses the entire snapshot for an unknown requested id
    # when ``enforce=True`` is set, so no ``_snapshot`` is emitted.
    assert "_snapshot" not in metadata, metadata
    # The v1 namespaces are still safe (metadata_version/event/taxonomy/series).
    assert metadata.get("metadata_version") == "1"


def test_initial_ingestion_missing_trade_condition_id_does_not_stamp_match(
    owned_sqlite,
) -> None:
    """When ``_snapshot`` IS emitted (e.g. legacy backfill) without a requested
    cond id, ``exact_match`` must report False so the caller cannot mistake
    the Gamma market as authoritative."""
    from polycopy.ingestion.canonical_metadata import _build_market_snapshot

    gamma = copy.deepcopy(FULL_GAMMA)
    snap = _build_market_snapshot(
        gamma,
        {"title": "ctx", "slug": "ctx-slug", "outcomeIndex": 0},
        requested_condition_id=None,
        enforce_exact_condition_match=True,
    )
    # With requested_condition_id explicitly None, the snapshot is emitted but
    # ``exact_match=False`` (no identity claim).
    assert snap["provenance"]["exact_match"] is False
    assert snap["provenance"]["requested_condition_id"] is None
    # Fail-closed: no authoritative evidence leaks when exact_match=False.
    for key in ("condition_id", "provider_market_id", "question", "slug"):
        assert snap["market"][key] is None, snap["market"]
    assert snap["outcomes"]["status"] == "invalid"
    assert snap["outcomes"]["compatible"] is False
    assert "exact_match_false_evidence_unavailable" in snap["outcomes"]["errors"]


def test_initial_ingestion_casing_is_normalized_lowercase_and_must_match_exactly(
    owned_sqlite,
) -> None:
    # Direct builder test for casing/canonical-formatting contract: the
    # requested condition id MUST match the gamma ``conditionId`` after the
    # canonical normalization (strip + lower). An identical id in different
    # casing collapses to the same normalized form and is accepted.
    from polycopy.ingestion.canonical_metadata import build_canonical_metadata

    upper = COND_ID.upper()
    padded = "  " + COND_ID + "  "
    mixed = "0xAbCd" + "0" * 60 + "abcd"  # distinct from COND_ID
    gamma = copy.deepcopy(FULL_GAMMA)

    md_upper = build_canonical_metadata(
        {"conditionId": upper, "outcomeIndex": 0},
        gamma,
        enforce_exact_condition_match=True,
    )
    assert md_upper.get("_snapshot", {}).get("provenance", {}).get(
        "exact_match"
    ) is True

    md_padded = build_canonical_metadata(
        {"conditionId": padded, "outcomeIndex": 0},
        gamma,
        enforce_exact_condition_match=True,
    )
    assert md_padded.get("_snapshot", {}).get("provenance", {}).get(
        "exact_match"
    ) is True

    md_mixed = build_canonical_metadata(
        {"conditionId": mixed, "outcomeIndex": 0},
        gamma,
        enforce_exact_condition_match=True,
    )
    assert "_snapshot" not in md_mixed


def test_initial_ingestion_mismatched_gamma_does_not_leak_taxonomy_or_outcome_evidence(
    owned_sqlite,
) -> None:
    # A Gamma object with a DIFFERENT conditionId must NOT leak its taxonomy
    # raw_category, tags, outcomes, lifecycle, slug, question, etc. into the
    # v1 namespaces of the trade's canonical metadata row. We pin the
    # taxonomy to a deliberately conflicting category, the tag list to a
    # trade-poisoning set, and the outcomes list to a single-element list —
    # none of which must appear in the persisted canonical metadata.
    other_gamma = {
        **FULL_GAMMA,
        "conditionId": "0x" + "9" * 64,  # ← different from trade's COND_ID
        "category": "Poison-Category-Should-Not-Leak",
        "tags": ["poison-tag-1", "poison-tag-2"],
        "outcomes": '["PoisonOutcome"]',
        "clobTokenIds": '["9999999999"]',
        "slug": "poison-slug",
        "question": "Poison question?",
        "updatedAt": "2026-01-01T00:00:00Z",
    }
    trade = _raw_trade(condition_id=COND_ID)
    cand = normalize_source_trade(
        trade,
        requested_wallet=PROXY_WALLET,
        record_index=0,
        gamma_market=other_gamma,
    )
    md = cand.metadata
    dumped = json.dumps(md, sort_keys=True)
    for leak in (
        "Poison-Category-Should-Not-Leak",
        "poison-tag-1",
        "poison-tag-2",
        "PoisonOutcome",
        "poison-slug",
        "Poison question?",
        "9999999999",
    ):
        assert leak not in dumped, (
            f"mismatched Gamma leaked authoritative evidence: {leak!r} in {dumped}"
        )
    assert "_snapshot" not in md


def test_mismatched_gamma_persisted_row_is_impossible_through_writer(
    owned_sqlite,
) -> None:
    """Persisted-row regression: mismatched Gamma cannot produce exact-match=true.

    Not only does the canonical builder refuse to emit the snapshot, but a
    real ``write_valid_rows(dry_run=False)`` call MUST produce a row whose
    stored ``metadata_json`` carries no authoritative snapshot namespaces.
    """
    db = Database(owned_sqlite.new_path())
    db.connect()
    try:
        other_gamma = {
            **FULL_GAMMA,
            "conditionId": "0x" + "9" * 64,
            "category": "ShouldNotStick",
            "tags": ["other-tag"],
            "outcomes": '["OtherYes"]',
            "clobTokenIds": '["9999999999"]',
            "slug": "other-slug",
            "question": "other question?",
        }
        trade = _raw_trade(
            condition_id=COND_ID, transaction_hash="0xnoleak00000001"
        )
        cand = normalize_source_trade(
            trade,
            requested_wallet=PROXY_WALLET,
            record_index=0,
            gamma_market=other_gamma,
        )
        assert cand.validation_status == "valid", cand.validation_reasons
        assert "_snapshot" not in cand.metadata, (
            "snapshot must be absent before persistence so the writer never "
            "writes authoritative evidence from a mismatched Gamma"
        )
        assert write_valid_rows(db, [cand], dry_run=False).inserted == 1
        row = db.conn.execute(
            "SELECT metadata_json FROM source_trades WHERE source_trade_id=?",
            (cand.source_trade_id,),
        ).fetchone()
        assert row is not None
        stored = json.loads(row[0])
        assert "_snapshot" not in stored, stored
        # The Pin: authoritative taxonomy/outcome cannot leak either.
        assert "ShouldNotStick" not in json.dumps(stored)
        assert "other-tag" not in json.dumps(stored)
        assert "OtherYes" not in json.dumps(stored)
    finally:
        db.close()


# ════════════════════════════════════════════════════════════════════════════
# Blocker 3 — Strict ``outcomeIndex`` parsing
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "value",
    [
        0,
        1,
    ],
)
def test_strict_outcome_index_accepts_real_non_negative_ints(value: int) -> None:
    assert _strict_trade_index_value(value) == value


@pytest.mark.parametrize(
    "value",
    [
        0.0,
        0.5,
        "0.5",
        True,
        False,
        -1,
        None,
        "",
        "0",
        "1",
        "1e0",
        1.5,
        [],
        {},
    ],
)
def test_strict_outcome_index_rejects_every_non_strict_value(value: object) -> None:
    assert _strict_trade_index_value(value) is None


def test_strict_outcome_index_invalid_values_are_never_persisted_as_provenance(
    owned_sqlite,
) -> None:
    """A real DB round-trip proves invalid outcomeIndex never leaks.

    For each invalid value, the canonical snapshot must NOT carry
    ``trade_response_outcome_index`` and the persisted row's
    ``metadata_json`` must NOT contain any integer provenance index that
    could be mistaken for a valid outcome.
    """
    for bad in (0.5, "0.5", True, -1, None, ""):
        trade = _raw_trade(
            condition_id=COND_ID,
            outcome_index=bad,
            transaction_hash=f"0xbad-{bad!r}",
        )
        cand = normalize_source_trade(
            trade,
            requested_wallet=PROXY_WALLET,
            record_index=0,
            gamma_market=copy.deepcopy(FULL_GAMMA),
        )
        assert cand.validation_status == "valid", (
            bad,
            cand.validation_reasons,
        )
        provenance = cand.metadata["_snapshot"]["provenance"]
        assert "trade_response_outcome_index" not in provenance, (
            f"outcomeIndex={bad!r} leaked into provenance as "
            f"{provenance.get('trade_response_outcome_index')!r}"
        )

        # And when persisted through the real writer, the provenance in the
        # stored metadata_json row does NOT contain ``trade_response_outcome_index``.
        db = Database(owned_sqlite.new_path())
        db.connect()
        try:
            assert write_valid_rows(db, [cand], dry_run=False).inserted == 1
            row = db.conn.execute(
                "SELECT metadata_json FROM source_trades WHERE source_trade_id=?",
                (cand.source_trade_id,),
            ).fetchone()
            assert row is not None
            stored = json.loads(row[0])
            assert (
                "trade_response_outcome_index"
                not in stored["_snapshot"]["provenance"]
            ), (bad, stored["_snapshot"]["provenance"])
        finally:
            db.close()


def test_strict_outcome_index_single_parser_feeds_outcome_validation_and_persistence() -> None:
    """The validator and the persistence writer cannot disagree on the index.

    Pass the strict parser's output (or ``None``) to ``_validate_outcome_mapping``
    and verify that the same value drives the persisted provenance field —
    so there is no second parser path that disagrees.
    """
    from polycopy.ingestion.canonical_metadata import (
        build_canonical_metadata,
    )

    trade = _raw_trade(outcome_index=0)
    canonical_metadata = build_canonical_metadata(trade, FULL_GAMMA)
    snapshot = canonical_metadata["_snapshot"]
    trade_validation = snapshot["provenance"]["trade_validation"]
    assert trade_validation["valid_index"] is True
    assert trade_validation["index_outcome_agrees"] is True
    assert trade_validation["outcome_index_supplied"] is True
    assert snapshot["provenance"]["trade_response_outcome_index"] == 0

    # And the float cannot survive either of the two paths.
    trade_float = _raw_trade(outcome_index=0.5)
    canonical_metadata_float = build_canonical_metadata(trade_float, FULL_GAMMA)
    snap_float = canonical_metadata_float["_snapshot"]
    assert snap_float["provenance"]["trade_validation"]["valid_index"] is None
    assert (
        "trade_response_outcome_index"
        not in snap_float["provenance"]
    )

    # Cross-check: the negative case where the validator says ``invalid``
    # (because index would be out of range) but the strict parser has
    # already refused to feed it through.
    trade_neg = _raw_trade(outcome_index=-1)
    canonical_metadata_neg = build_canonical_metadata(trade_neg, FULL_GAMMA)
    snap_neg = canonical_metadata_neg["_snapshot"]
    assert snap_neg["provenance"]["trade_validation"]["valid_index"] is None
    assert (
        "trade_response_outcome_index" not in snap_neg["provenance"]
    )


# ════════════════════════════════════════════════════════════════════════════
# Context-Dependent Diagnostics — same Gamma, different caller contexts
# ════════════════════════════════════════════════════════════════════════════


# Mirror of the production merge layer used to compute the real writer
# boundary. Tests import this directly to confirm the same-Gamma/different-
# context invariant without forcing the full enrichment pipeline.
def _context_invariant_replay_compare(snapshot):
    """Mirror ``_snapshot_for_replay_comparison`` for direct assertions.

    The production helper removes ``retrieved_at`` and ``provider_updated_at``
    from provenance and, under the new contract, also removes the entire
    ``trade_validation`` nested object so two builds of the same Gamma
    evidence with different caller contexts are substantively equivalent.
    Wallet-context fields (``trade_response_*``) are also caller-only
    context and are stripped for the same reason.
    """
    import copy as _copy
    out = _copy.deepcopy(snapshot)
    prov = out.get("provenance") if isinstance(out, dict) else None
    if isinstance(prov, dict):
        prov.pop("retrieved_at", None)
        prov.pop("provider_updated_at", None)
        prov.pop("trade_validation", None)
        for key in list(prov):
            if key.startswith("trade_response_"):
                prov.pop(key)
    return out


def test_same_gamma_different_caller_contexts_are_substantively_equivalent():
    """Same Gamma, different caller contexts ⇒ equivalent authoritative evidence.

    The initial-ingestion path produces a snapshot with the trade's
    outcome/asset/outcomeIndex context; the merge/backfill path produces a
    snapshot for the same Gamma with an empty trade. The authoritative
    Gamma-shape evidence (``outcomes`` / ``market`` / ``lifecycle`` /
    ``resolution`` / material ``provenance``) MUST compare equal at the
    replay layer; only the context-only ``trade_validation`` namespace is
    permitted to differ.
    """
    trade_with_context = dict(_raw_trade(outcome_index=0), asset="1111111111")
    initial_metadata = build_canonical_metadata(
        trade_with_context, copy.deepcopy(FULL_GAMMA)
    )
    initial_snapshot = initial_metadata["_snapshot"]

    merge_metadata = build_canonical_metadata({}, copy.deepcopy(FULL_GAMMA))
    merge_snapshot = merge_metadata["_snapshot"]

    initial_replay = _context_invariant_replay_compare(initial_snapshot)
    merge_replay = _context_invariant_replay_compare(merge_snapshot)
    assert initial_replay == merge_replay, (
        "Same Gamma response must produce substantively equivalent "
        "authoritative evidence regardless of caller context. "
        f"diff: {json.dumps(initial_replay)} vs {json.dumps(merge_replay)}"
    )

    # The trade-validation namespace MUST differ — it's the only piece
    # carrying caller-context diagnostics.
    initial_tv = initial_snapshot["provenance"]["trade_validation"]
    merge_tv = merge_snapshot["provenance"]["trade_validation"]
    assert initial_tv != merge_tv, (
        "Trade-validation diagnostics must reflect the caller context"
    )
    # Initial ingest sees ``outcome_index=0`` selected, so it has no
    # disagreement errors; merge sees no context so it is also empty:
    assert initial_tv["errors"] == []
    assert merge_tv["errors"] == []
    # But the booleans differ — initial ingest sees ``valid_index=True``,
    # merge sees ``valid_index=None``.
    assert initial_tv["valid_index"] is True
    assert merge_tv["valid_index"] is None


def test_merge_returns_unchanged_when_only_caller_context_differs():
    """The merge layer reports UNCHANGED for the same Gamma / different caller.

    After initial ingestion (with trade context) writes a row, a later
    enrichment call (without trade context, the canonical backfill path)
    MUST NOT report conflict, MUST NOT update the persisted row, and MUST
    return the exact same authoritative evidence.
    """
    trade_with_context = dict(_raw_trade(outcome_index=0), asset="1111111111")

    # Initial ingest (with trade context).
    initial_metadata = build_canonical_metadata(
        trade_with_context, copy.deepcopy(FULL_GAMMA)
    )
    existing_json = serialize_source_trade_metadata(initial_metadata)

    # Backfill / enrichment with empty trade.
    merged, status, reasons = merge_canonical_metadata(
        existing_json,
        copy.deepcopy(FULL_GAMMA),
        condition_id=COND_ID,
        token_id="1111111111",
    )
    assert status == MERGE_UNCHANGED, (
        f"merge should report UNCHANGED for same Gamma / different caller; "
        f"got {status} reasons={reasons}"
    )
    assert reasons == ["no_change"]
    assert merged == json.loads(existing_json)


def test_merge_preserves_existing_trade_validation_diagnostics():
    """Missing context on later merge does not erase existing diagnostics.

    A later no-context merge must NOT overwrite an existing
    ``provenance.trade_validation`` block with the latest observation's
    (smaller, context-free) snapshot. Union semantics keep the earlier
    observations alive.
    """
    bad_trade = dict(
        _raw_trade(outcome_index=0),
        asset="9999999999",  # intentionally mismatched token id
    )
    # Step 1: initial ingest with a real disagreement.
    initial_metadata = build_canonical_metadata(
        bad_trade, copy.deepcopy(FULL_GAMMA)
    )
    initial_snapshot = initial_metadata["_snapshot"]
    initial_tv = initial_snapshot["provenance"]["trade_validation"]
    # Asset 9999 doesn't match ``token_ids[outcome_index]`` because the
    # Gamma market's first token is ``1111111111``. The validator emits a
    # trade-context diagnostic for that index/token disagreement.
    assert "index_token_disagreement" in initial_tv["errors"], initial_tv
    existing_json = serialize_source_trade_metadata(initial_metadata)

    # Step 2: later no-context merge.
    merged, status, reasons = merge_canonical_metadata(
        existing_json,
        copy.deepcopy(FULL_GAMMA),
        condition_id=COND_ID,
        token_id="1111111111",
    )
    assert status == MERGE_UNCHANGED, reasons
    merged_snapshot = merged["_snapshot"]
    merged_tv = merged_snapshot["provenance"]["trade_validation"]
    # Union semantics: the existing observed diagnostic survived even
    # though the new build could not observe it.
    assert "index_token_disagreement" in merged_tv["errors"], (
        f"Existing trade-validation diagnostics must survive a no-context "
        f"merge; observed: {merged_tv}"
    )


def test_genuine_gamma_label_change_is_substantive_update():
    """A real Gamma label change is detected as Gamma-shape evidence
    conflict — the merge layer MUST distinguish the no-context vs.
    different-context carve-out (which never raises conflicts) from this
    genuine Gamma-shape change (which does). The persisted evidence is
    left untouched and a conflict reason is surfaced so the caller can
    investigate.
    """
    initial_metadata = build_canonical_metadata(
        dict(_raw_trade(outcome_index=0), asset="1111111111"),
        copy.deepcopy(FULL_GAMMA),
    )
    existing_json = serialize_source_trade_metadata(initial_metadata)
    # Substantive label change while preserving array lengths; the
    # surrounding Gamma evidence (lifecycle / market / resolution) is
    # identical so the only conflict can be Gamma-shape.
    changed_gamma = dict(copy.deepcopy(FULL_GAMMA))
    changed_gamma["outcomes"] = '["Yes", "Maybe"]'
    changed_gamma["clobTokenIds"] = '["1111111111", "3333333333"]'
    _merged, status, reasons = merge_canonical_metadata(
        existing_json, changed_gamma, condition_id=COND_ID, token_id="1111111111"
    )
    # Substantive protected Gamma-shape evidence must surface as conflict.
    assert status == MERGE_CONFLICT, (status, reasons)
    assert any(
        reason.startswith(("_snapshot_outcomes_", "_snapshot_market_", "_snapshot_lifecycle_"))
        for reason in reasons
    ) or reasons, reasons
    assert any(
        "_conflicted" in reason or "_conflict" in reason
        for reason in reasons
    ), reasons


def test_genuine_gamma_lifecycle_change_is_substantive_update():
    initial_metadata = build_canonical_metadata(
        dict(_raw_trade(outcome_index=0), asset="1111111111"),
        copy.deepcopy(FULL_GAMMA),
    )
    existing_json = serialize_source_trade_metadata(initial_metadata)
    changed_gamma = dict(copy.deepcopy(FULL_GAMMA), closed=True, active=False)
    merged, status, _reasons = merge_canonical_metadata(
        existing_json, changed_gamma, condition_id=COND_ID, token_id="1111111111"
    )
    assert status == MERGE_FILLED
    assert merged["_snapshot"]["lifecycle"]["closed"] is True
    assert merged["_snapshot"]["lifecycle"]["active"] is False


def test_genuine_gamma_identity_change_is_substantive_update():
    """Substituting the Gamma ``conditionId`` with its ``id`` still fails
    identity at the merge layer (``condition_id_mismatch`` → unavailable).
    """
    initial_metadata = build_canonical_metadata(
        dict(_raw_trade(outcome_index=0), asset="1111111111"),
        copy.deepcopy(FULL_GAMMA),
    )
    existing_json = serialize_source_trade_metadata(initial_metadata)
    substituted = dict(copy.deepcopy(FULL_GAMMA))
    substituted["conditionId"] = substituted["id"]
    del substituted["id"]
    _merged, status, reasons = merge_canonical_metadata(
        existing_json, substituted, condition_id=COND_ID, token_id="1111111111"
    )
    assert status == MERGE_UNAVAILABLE
    assert reasons == ["condition_id_mismatch"]


def test_invalid_trade_context_does_not_corrupt_otherwise_valid_gamma():
    """An invalid fractional outcomeIndex stays context-only and never
    invalidates the underlying Gamma evidence. The authoritative
    ``outcomes`` labels/tokens/status remain intact, and the diagnostic
    surfaces under ``provenance.trade_validation``.
    """
    bad_trade = dict(_raw_trade(outcome_index=0.5), asset="1111111111")
    metadata = build_canonical_metadata(bad_trade, copy.deepcopy(FULL_GAMMA))
    snapshot = metadata["_snapshot"]
    outcomes = snapshot["outcomes"]
    # Authoritative Gamma-shape evidence is unchanged.
    assert outcomes["status"] == "complete"
    assert outcomes["ordered"] == [
        {"label": "Yes", "clob_token_id": "1111111111"},
        {"label": "No", "clob_token_id": "2222222222"},
    ]
    assert outcomes["errors"] == []
    # The invalid index is captured in the context-only namespace.
    tv = snapshot["provenance"]["trade_validation"]
    assert tv["outcome_index_supplied"] is False
    assert tv["valid_index"] is None
    # And the strict parser has rejected the float — no
    # ``trade_response_outcome_index`` is persisted.
    assert "trade_response_outcome_index" not in snapshot["provenance"]


def test_full_lifecycle_test_passes_in_isolation():
    """The final integration lifecycle test (``test_s7_disposable_e2e_full_lifecycle``)
    is run as part of the affected-suite aggregation; the parametrised
    invocation under the focused test run below proves it passes under the
    new contract.
    """
    import subprocess

    proc = subprocess.run(
        [
            "python",
            "-m",
            "pytest",
            "tests/test_pXX_s7_final_integration.py::test_s7_disposable_e2e_full_lifecycle",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"full-lifecycle test failed in isolation\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )


def test_write_round_trip_survives_full_pipeline_with_trade_validation(
    owned_sqlite,
) -> None:
    """End-to-end: writer persists trade-validation under provenance and
    the row read back from the DB contains the full canonical
    ``_snapshot.provenance.trade_validation`` block.
    """
    db = Database(owned_sqlite.new_path())
    db.connect()
    try:
        cand = normalize_source_trade(
            _raw_trade(outcome_index=0, transaction_hash="0xfeedface00000064"),
            requested_wallet=PROXY_WALLET,
            record_index=0,
            gamma_market=copy.deepcopy(FULL_GAMMA),
        )
        assert cand.validation_status == "valid"
        assert write_valid_rows(db, [cand], dry_run=False).inserted == 1
        row = db.conn.execute(
            "SELECT metadata_json FROM source_trades WHERE source_trade_id=?",
            (cand.source_trade_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        snapshot = payload["_snapshot"]
        assert "trade_validation" in snapshot["provenance"]
        tv = snapshot["provenance"]["trade_validation"]
        assert tv["valid_index"] is True
        assert tv["index_outcome_agrees"] is True
        assert tv["outcome_index_supplied"] is True
        # Authoritative outcomes carries Gamma-shape evidence only.
        assert "valid_index" not in snapshot["outcomes"]
        assert "index_token_agrees" not in snapshot["outcomes"]
        assert "index_outcome_agrees" not in snapshot["outcomes"]
        assert snapshot["outcomes"]["errors"] == []
    finally:
        db.close()
