"""Shared one-current-row provenance for specialist evidence source trades.

This is THE single production implementation of the ``source_trade_enrichments``
audit/provenance row. Both the S3 historical backfill
(``scripts/backfill_specialist_trade_taxonomy.py``) and the S5 per-trade
enrichment (``enrich_source_trade`` in ``source_trade_enrichment.py``) call
into this module so the provenance contract cannot diverge.

Scoring authority contract (never violated here)
------------------------------------------------
* The scorer reads ``source_trades.metadata_json['taxonomy']['raw_category']``.
* ``source_trade_enrichments`` is audit/provenance state ONLY and is never a
  scoring authority. ``normalized_category`` here mirrors the scorer's
  classification for convenience, but the scorer MUST NOT read it.

One-current-row contract (UNIQUE(source_trade_internal_id))
-----------------------------------------------------------
* no existing row            -> insert exactly one current row
* identical evidence hash    -> perform NO write (idempotent replay)
* materially changed evidence-> UPDATE the existing row IN PLACE
  - preserve ``enrichment_id``, ``source_trade_internal_id``, ``created_at``
  - update all current provenance fields, ``evidence_hash``, ``fetched_at``,
    ``updated_at``
* never create a duplicate row
* never invent an archived row
* never alter ``enrichment_id`` to bypass uniqueness
* remains safe when an ``approved_specialist_trade_dispatches`` row already
  references the ``enrichment_id`` (the FK stays valid because the id is stable)

Merge-status handling (driven by ``merge_canonical_metadata``)
--------------------------------------------------------------
* MERGE_FILLED / MERGE_UNCHANGED
    - canonical metadata may be classified
    - scorer-visible metadata may be persisted (by the caller)
    - normalized_category populated only when taxonomy is usable
* MERGE_CONFLICT
    - preserve source_trades.metadata_json byte-for-byte
    - provenance status = conflict
    - normalized_category = NULL
    - taxonomy_status = unavailable
    - exact conflict reasons preserved
    - never report enrichment complete
* MERGE_UNAVAILABLE
    - preserve source_trades.metadata_json byte-for-byte
    - provenance status = unavailable, unless a provider error requires
      status=error
    - normalized_category = NULL; taxonomy_status = unavailable
    - exact reasons preserved
    - never report enrichment complete

Provider-state precedence (high-level status)
--------------------------------------------
* provider_error            -> status=error
* merge conflict            -> status=conflict
* merge unavailable / not-found / malformed / ambiguous -> status=unavailable
* safe usable canonical taxonomy -> status=complete
* safe but partial taxonomy      -> status=incomplete

This module does NOT perform the metadata_json write or the Gamma call; the
caller owns those (and must place metadata + provenance inside one SAVEPOINT).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from polycopy.ingestion.canonical_metadata import (
    MERGE_CONFLICT,
    MERGE_FILLED,
    MERGE_UNCHANGED,
    MERGE_UNAVAILABLE,
)
from polycopy.scoring.wallet_evidence import (
    CATEGORY_TAXONOMY_PARTIAL,
    CATEGORY_TAXONOMY_USABLE,
    classify_category_taxonomy,
)

# Status vocabulary for source_trade_enrichments.status.
STATUS_PENDING = "pending"
STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete"
STATUS_UNAVAILABLE = "unavailable"
STATUS_CONFLICT = "conflict"
STATUS_ERROR = "error"

_VALID_STATUSES = frozenset(
    {
        STATUS_PENDING,
        STATUS_COMPLETE,
        STATUS_INCOMPLETE,
        STATUS_UNAVAILABLE,
        STATUS_CONFLICT,
        STATUS_ERROR,
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Provenance payload builder ───────────────────────────────────────────────


def build_provenance_payload(
    *,
    source_trade: Mapping[str, Any],
    canonical_meta: Any,
    gamma_market: Optional[Mapping[str, Any]],
    merge_status: str,
    gamma_state: str,
    gamma_reason: Optional[str] = None,
    merge_reasons: Optional[list[str]] = None,
    evidence_source: str = "canonical_metadata",
) -> dict[str, Any]:
    """Build the honest current provenance payload (audit-only, no scoring).

    ``canonical_meta`` is the merge output (a dict on FILLED/UNCHANGED, and the
    original preserved value — possibly a malformed string — on
    CONFLICT/UNAVAILABLE). We only classify a real dict.

    ``gamma_market`` is the authoritative raw Gamma dict (or None) used solely
    for the non-empty slug. It is NOT a metadata source.

    ``evidence_source`` tags the provenance row (e.g. "backfill" vs the default
    "canonical_metadata") so the audit trail can distinguish the producing
    path; it does not change any scoring behavior.

    Returns a flat dict ready to persist into ``source_trade_enrichments``
    (minus enrichment_id / created_at / updated_at / evidence_hash, which the
    persistence layer manages).
    """
    # On conflict/unavailable the merge returns the original preserved value
    # (which may be a malformed JSON string, not a dict). Only classify a dict.
    safe_meta = canonical_meta if isinstance(canonical_meta, dict) else {}

    usable = False
    normalized_category: Optional[str] = None
    taxonomy_status = "unavailable"
    if merge_status in (MERGE_FILLED, MERGE_UNCHANGED):
        cls = classify_category_taxonomy(safe_meta)
        status = str(cls.status)
        if status == CATEGORY_TAXONOMY_USABLE and cls.category_label:
            usable = True
            normalized_category = cls.category_label
            taxonomy_status = "usable"
        elif status == CATEGORY_TAXONOMY_PARTIAL:
            taxonomy_status = "partial"
        else:
            taxonomy_status = "unavailable"
    # MERGE_CONFLICT / MERGE_UNAVAILABLE -> normalized_category stays NULL,
    # taxonomy_status stays unavailable, and we never report complete.

    # High-level status precedence.
    if gamma_state == "provider_error":
        status = STATUS_ERROR
    elif merge_status == MERGE_CONFLICT:
        status = STATUS_CONFLICT
    elif merge_status == MERGE_UNAVAILABLE:
        status = STATUS_UNAVAILABLE
    elif usable:
        status = STATUS_COMPLETE
    else:
        status = STATUS_INCOMPLETE

    # market_slug only from a non-empty authoritative Gamma "slug" field.
    # Never fall back to question/title/event title/series title text.
    slug: Optional[str] = None
    if gamma_market is not None:
        raw_slug = gamma_market.get("slug")
        if isinstance(raw_slug, str) and raw_slug.strip():
            slug = raw_slug

    # Exact source-trade identity. token_id from the SOURCE TRADE (never Gamma's
    # clobTokenIds). condition_id/market_id from market_source_id. outcome from
    # outcome. A missing token_id stays missing (NULL) and fails closed through
    # the canonical token-membership contract in merge_canonical_metadata.
    token_id = source_trade.get("token_id")
    condition_id = source_trade.get("market_source_id") or ""
    outcome_identity = source_trade.get("outcome")

    # A trade timestamp is NOT an authoritative market start time. Never store
    # market_start_at from the source-trade timestamp (left NULL here). Trusted
    # Gamma fields would be mapped explicitly by the caller if ever available.
    market_start_at = None

    reason_codes: list[str] = []
    for r in merge_reasons or []:
        if r and r not in reason_codes:
            reason_codes.append(r)
    reason_codes.append(f"merge:{merge_status}")
    reason_codes.append(f"gamma:{gamma_state}")
    if gamma_state == "provider_error":
        reason_codes.append("provider_error")
    if gamma_reason:
        reason_codes.append(gamma_reason)

    return {
        "status": status,
        "token_id": token_id,
        "condition_id": condition_id,
        "market_id": condition_id,
        "market_slug": slug,
        "market_title": None,
        "outcome_identity": outcome_identity,
        "event_identity": None,
        "normalized_category": normalized_category,
        "taxonomy_status": taxonomy_status,
        "market_start_at": market_start_at,
        "evidence_source": evidence_source,
        "gamma_source": "gamma_market_raw" if gamma_market is not None else None,
        "reason_codes": reason_codes,
    }


def evidence_hash(payload: Mapping[str, Any]) -> str:
    """Deterministic SHA-256 over the provenance *substance* (field-stable).

    Diagnostic ``reason_codes`` are intentionally excluded: they can differ
    between a first ``filled`` write and an idempotent ``unchanged`` replay
    (e.g. ``merge:filled`` vs ``merge:unchanged``) even though the underlying
    evidence is byte-identical. Excluding them keeps the hash stable so an
    equivalent replay performs zero write (spec S5 §2 / §11 item 2).
    """
    stable = {k: v for k, v in payload.items() if k != "reason_codes"}
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── One-current-row persistence ──────────────────────────────────────────────


def get_current_enrichment(
    db: Any, source_trade_internal_id: str
) -> Optional[dict[str, Any]]:
    """Return the single current enrichment row for the source trade, or None."""
    row = db.fetchone(
        "SELECT * FROM source_trade_enrichments WHERE source_trade_internal_id=?",
        (source_trade_internal_id,),
    )
    return dict(row) if row is not None else None


def write_provenance(
    db: Any,
    *,
    source_trade_internal_id: str,
    payload: Mapping[str, Any],
    evidence_hash_value: str,
    enrichment_id_prefix: str = "enr",
) -> tuple[bool, bool, str]:
    """Upsert the single CURRENT provenance row for one source trade.

    Returns ``(changed, is_new, enrichment_id)``.

    Contract:
      * no existing row        -> INSERT one current row (new enrichment_id,
        created_at = now)
      * identical evidence_hash-> NO write (changed = False, is_new = False)
      * materially changed     -> UPDATE in place; preserve enrichment_id,
        source_trade_internal_id, created_at; bump updated_at/fetched_at
        and all current fields
      * never create a duplicate row (UNIQUE(source_trade_internal_id))
      * never alter enrichment_id to bypass uniqueness
      * never invent archived rows
      * safe when a dispatch FK references the enrichment_id (id stays stable)

    Does not commit; the caller owns the SAVEPOINT / transaction boundary.
    """
    now = _now()
    existing = get_current_enrichment(db, source_trade_internal_id)
    if existing is None:
        enrichment_id = f"{enrichment_id_prefix}:{source_trade_internal_id}"
        db.conn.execute(
            "INSERT INTO source_trade_enrichments ("
            "enrichment_id, source_trade_internal_id, status, token_id, "
            "condition_id, market_id, market_slug, market_title, "
            "outcome_identity, event_identity, normalized_category, "
            "taxonomy_status, market_start_at, evidence_source, gamma_source, "
            "evidence_hash, reason_codes_json, fetched_at, created_at, "
            "updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                enrichment_id,
                source_trade_internal_id,
                payload["status"],
                payload["token_id"],
                payload["condition_id"],
                payload["market_id"],
                payload["market_slug"],
                payload["market_title"],
                payload["outcome_identity"],
                payload["event_identity"],
                payload["normalized_category"],
                payload["taxonomy_status"],
                payload["market_start_at"],
                payload["evidence_source"],
                payload["gamma_source"],
                evidence_hash_value,
                json.dumps(payload["reason_codes"], sort_keys=True),
                now,
                now,
                now,
            ),
        )
        return True, True, enrichment_id

    # Existing current row: only update when evidence materially changed.
    if existing["evidence_hash"] == evidence_hash_value:
        return False, False, existing["enrichment_id"]

    db.conn.execute(
        "UPDATE source_trade_enrichments SET "
        "status=?, token_id=?, condition_id=?, market_id=?, market_slug=?, "
        "market_title=?, outcome_identity=?, event_identity=?, "
        "normalized_category=?, taxonomy_status=?, market_start_at=?, "
        "evidence_source=?, gamma_source=?, evidence_hash=?, "
        "reason_codes_json=?, fetched_at=?, updated_at=? "
        "WHERE source_trade_internal_id=?",
        (
            payload["status"],
            payload["token_id"],
            payload["condition_id"],
            payload["market_id"],
            payload["market_slug"],
            payload["market_title"],
            payload["outcome_identity"],
            payload["event_identity"],
            payload["normalized_category"],
            payload["taxonomy_status"],
            payload["market_start_at"],
            payload["evidence_source"],
            payload["gamma_source"],
            evidence_hash_value,
            json.dumps(payload["reason_codes"], sort_keys=True),
            now,
            now,
            source_trade_internal_id,
        ),
    )
    return True, False, existing["enrichment_id"]


# ── Dispatch-gate regression seam (pure; no I/O, no dispatch record) ──────────


def enrichment_status_allows_dispatch(status: Optional[str]) -> bool:
    """Return True iff an enrichment result permits the dispatcher bridge.

    The dispatcher may proceed ONLY when status == 'complete'. conflict,
    unavailable, incomplete, and error MUST block the bridge. This is a pure
    function so S5 can prove the contract without creating any approval or
    dispatch record.
    """
    return status == STATUS_COMPLETE
