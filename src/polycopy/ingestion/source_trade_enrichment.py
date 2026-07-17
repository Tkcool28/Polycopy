"""Authoritative, durable enrichment for one exact approved source trade.

This module resolves and persists the normalized evidence required to prove a
source trade is a safe, traceable copy-candidate input. It operates on ONE
exact ``source_trades.id`` (never an arbitrary wallet history fetch) and writes
ONLY the ``source_trade_enrichments`` durable state row. Canonical
``source_trades`` columns (token_id, condition_id, market_source_id, outcome,
timestamp) remain the system of record; this table records the resolved
normalized evidence + completion status + provenance, not a copy of those
columns.

Design rules (per Pass 2 spec):
  * Use existing adapters and PR66 taxonomy logic. Do NOT invent category.
  * Do NOT infer a category from wallet behavior when market taxonomy is
    unavailable. When taxonomy is missing/partial, status becomes
    ``incomplete`` / ``unavailable`` and the bridge is never invoked by the
    dispatcher.
  * Do NOT overwrite conflicting evidence silently; a material change in
    evidence hash creates a new enrichment record version (versioned by
    ``created_at``) rather than clobbering the prior proof.
  * Replay with identical evidence must not create a duplicate operational
    state: the table has UNIQUE(source_trade_internal_id); a second call with
    the same resolved evidence hash returns the existing current record.
  * Every network operation is bounded. The optional ``gamma_resolver`` is
    called at most once, behind the caller's own timeout.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from polycopy.ingestion.canonical_metadata import (
    merge_canonical_metadata,
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

_VALID_STATUSES = frozenset({
    STATUS_PENDING, STATUS_COMPLETE, STATUS_INCOMPLETE,
    STATUS_UNAVAILABLE, STATUS_CONFLICT, STATUS_ERROR,
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    from uuid import uuid4

    return str(uuid4())


def _read_source_trade(db: Any, source_trade_internal_id: str) -> Optional[dict[str, Any]]:
    row = db.fetchone(
        "SELECT id, source, source_trade_id, market_source_id, token_id, "
        "outcome, timestamp, metadata_json, is_sample FROM source_trades "
        "WHERE id=?",
        (source_trade_internal_id,),
    )
    return dict(row) if row is not None else None


def _coerce_metadata_json(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _call_gamma_resolver(gamma_resolver: Callable[[str], Any], condition_id: str) -> Any:
    """Invoke a sync OR async gamma resolver, bounded to one call.

    The dispatcher passes a synchronous ``gamma.get_market``; the research
    collector passes an ``async`` resolver. Support both without leaking a
    coroutine into the canonical builder.
    """
    market = gamma_resolver(condition_id)
    if inspect.isawaitable(market):
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Rare: nested loop. Fall back to a fresh loop in a thread.
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    return ex.submit(asyncio.run, market).result()
        except RuntimeError:
            pass
        return asyncio.run(market)
    return market


def _gamma_slug(gamma_market: Any) -> Optional[str]:
    if gamma_market is None:
        return None
    get = gamma_market.get if hasattr(gamma_market, "get") else (lambda k, d=None: d)
    slug = get("slug")
    if isinstance(slug, str) and slug.strip():
        return slug.strip()
    return None


def _build_evidence(
    row: dict[str, Any],
    canonical_metadata: dict[str, Any],
    *,
    gamma_market: Optional[Any] = None,
) -> dict[str, Any]:
    """Resolve normalized evidence from the CANONICAL nested metadata.

    ``canonical_metadata`` is the shared nested shape produced by
    ``merge_canonical_metadata`` (``taxonomy``/``event``/``series``). The
    scorer-facing taxonomy lives ONLY in
    ``canonical_metadata["taxonomy"]["raw_category"]``; this provenance row is
    audit-only. Category is taken from taxonomy classification only, and is
    NEVER inferred from wallet behavior or market title.
    """
    ev: dict[str, Any] = {}

    # Token / condition / market identity (system of record precedence).
    if row.get("token_id"):
        ev["token_id"] = row["token_id"]
    else:
        cond = row.get("market_source_id")
        if cond:
            ev["token_id"] = cond
    if row.get("market_source_id"):
        ev["condition_id"] = row["market_source_id"]
    ev["market_id"] = row.get("market_source_id")
    if row.get("outcome"):
        ev["outcome_identity"] = row["outcome"]

    # Classify taxonomy from the CANONICAL nested metadata (the fix: the scorer
    # reads this exact shape, so enrichment must classify the identical shape).
    classification = classify_category_taxonomy(canonical_metadata)
    status = str(classification.status)
    if status == CATEGORY_TAXONOMY_USABLE and classification.category_label:
        ev["normalized_category"] = classification.category_label
        ev["taxonomy_status"] = "usable"
    elif status == CATEGORY_TAXONOMY_PARTIAL:
        ev["taxonomy_status"] = "partial"
    else:
        ev["taxonomy_status"] = "unavailable"

    event = canonical_metadata.get("event")
    if isinstance(event, dict):
        ev["event_identity"] = event.get("id") or event.get("slug")

    slug = _gamma_slug(gamma_market)
    if slug:
        ev["market_slug"] = slug

    if row.get("timestamp"):
        ev["market_start_at"] = str(row["timestamp"])

    ev["evidence_source"] = "canonical_metadata"
    if gamma_market is not None:
        ev["gamma_source"] = "gamma_market_resolver"
    return ev


def _evidence_hash(ev: dict[str, Any]) -> str:
    import hashlib

    canonical = json.dumps(ev, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _classify_status(ev: dict[str, Any], reason_codes: list[str]) -> str:
    """Map resolved evidence + reason codes to a durable enrichment status."""
    if "taxonomy_unavailable" in reason_codes or ev.get("taxonomy_status") == "unavailable":
        return STATUS_INCOMPLETE
    if ev.get("taxonomy_status") == "partial":
        # Partial taxonomy is a blocker for a durable copy decision; treat as
        # incomplete (bridge not invoked by dispatcher).
        return STATUS_INCOMPLETE
    if "gamma_error" in reason_codes:
        return STATUS_ERROR
    if not ev.get("normalized_category"):
        return STATUS_INCOMPLETE
    # Usable taxonomy + category present => authoritative evidence complete.
    return STATUS_COMPLETE


@dataclass
class EnrichmentResult:
    source_trade_internal_id: str
    enrichment_id: Optional[str]
    status: str
    created: bool
    updated: bool
    reason_codes: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_trade_internal_id": self.source_trade_internal_id,
            "enrichment_id": self.enrichment_id,
            "status": self.status,
            "created": self.created,
            "updated": self.updated,
            "reason_codes": self.reason_codes,
            "evidence": self.evidence,
            "error_message": self.error_message,
        }


def get_enrichment(db: Any, source_trade_internal_id: str) -> Optional[dict[str, Any]]:
    row = db.fetchone(
        "SELECT * FROM source_trade_enrichments WHERE source_trade_internal_id=?",
        (source_trade_internal_id,),
    )
    return dict(row) if row is not None else None


def enrich_source_trade(
    db: Any,
    source_trade_internal_id: str,
    *,
    gamma_resolver: Optional[Callable[[str], Any]] = None,
    dry_run: bool = False,
) -> EnrichmentResult:
    """Resolve and persist (or link) authoritative enrichment for one trade.

    The ``gamma_resolver`` (optional) is ``Callable[[condition_id] -> market|None``
    and is invoked at most once, bounded by the caller's own timeout. It is used
    only to FILL missing provenance fields, never to overwrite existing ones.
    """
    row = _read_source_trade(db, source_trade_internal_id)
    if row is None:
        return EnrichmentResult(
            source_trade_internal_id=source_trade_internal_id,
            enrichment_id=None,
            status=STATUS_ERROR,
            created=False,
            updated=False,
            reason_codes=["source_trade_not_found"],
            error_message="source_trade_internal_id not found",
        )

    reason_codes: list[str] = []
    gamma_market: Optional[Any] = None
    if gamma_resolver is not None:
        try:
            condition_id = row.get("market_source_id")
            if condition_id:
                gamma_market = _call_gamma_resolver(gamma_resolver, condition_id)
        except Exception:  # bounded; record and continue on metadata only
            reason_codes.append("gamma_error")
            gamma_market = None
            # Do not fail enrichment on a network error; mark as durable error
            # state but still persist the metadata-derived evidence.

    # Build the CANONICAL nested metadata via the shared service. This reuses
    # the exact shape the scorer reads (taxonomy.raw_category), merging the
    # resolved Gamma market onto the stored source_trades.metadata_json
    # (never overwriting existing trusted provenance). Fixes the prior defect
    # where a flat Gamma dict was classified instead of the nested shape.
    canonical_meta, _merge_status, _merge_reasons = merge_canonical_metadata(
        row.get("metadata_json"),
        gamma_market,
        condition_id=row.get("market_source_id") or "",
        token_id=row.get("token_id"),
    )

    ev = _build_evidence(row, canonical_meta, gamma_market=gamma_market)
    if ev.get("taxonomy_status") == "unavailable":
        reason_codes.append("taxonomy_unavailable")
    elif ev.get("taxonomy_status") == "partial":
        reason_codes.append("taxonomy_partial")

    evidence_hash = _evidence_hash(ev)
    status = _classify_status(ev, reason_codes)

    existing = get_enrichment(db, source_trade_internal_id)
    if existing is not None:
        # Idempotent replay: identical evidence hash => no change.
        if existing.get("evidence_hash") == evidence_hash:
            return EnrichmentResult(
                source_trade_internal_id=source_trade_internal_id,
                enrichment_id=existing["enrichment_id"],
                status=existing["status"],
                created=False,
                updated=False,
                reason_codes=reason_codes,
                evidence=ev,
            )
        # Material change: version the evidence by creating a NEW current
        # record. The prior record is preserved (we do not DELETE it); the
        # UNIQUE(source_trade_internal_id) constraint means we must first
        # detach the old id from the unique slot to keep provenance history.
        if dry_run:
            return EnrichmentResult(
                source_trade_internal_id=source_trade_internal_id,
                enrichment_id=existing["enrichment_id"],
                status=STATUS_CONFLICT,
                created=False,
                updated=False,
                reason_codes=["evidence_changed"] + reason_codes,
                evidence=ev,
                error_message="evidence hash changed; dry-run refuses write",
            )
        _new_id = _uuid()
        db.conn.execute(
            "UPDATE source_trade_enrichments SET enrichment_id=? "
            "WHERE enrichment_id=?",
            (f"archived:{existing['enrichment_id']}", existing["enrichment_id"]),
        )
        _persist_new(db, _new_id, source_trade_internal_id, ev, evidence_hash,
                     status, reason_codes, gamma_market is not None)
        return EnrichmentResult(
            source_trade_internal_id=source_trade_internal_id,
            enrichment_id=_new_id,
            status=status,
            created=True,
            updated=False,
            reason_codes=["evidence_changed"] + reason_codes,
            evidence=ev,
        )

    if dry_run:
        return EnrichmentResult(
            source_trade_internal_id=source_trade_internal_id,
            enrichment_id=None,
            status=status,
            created=False,
            updated=False,
            reason_codes=reason_codes,
            evidence=ev,
        )

    _new_id = _uuid()
    _persist_new(db, _new_id, source_trade_internal_id, ev, evidence_hash,
                 status, reason_codes, gamma_market is not None)
    return EnrichmentResult(
        source_trade_internal_id=source_trade_internal_id,
        enrichment_id=_new_id,
        status=status,
        created=True,
        updated=False,
        reason_codes=reason_codes,
        evidence=ev,
    )


def _persist_new(
    db: Any,
    enrichment_id: str,
    source_trade_internal_id: str,
    ev: dict[str, Any],
    evidence_hash: str,
    status: str,
    reason_codes: list[str],
    used_gamma: bool,
) -> None:
    now = _now_iso()
    db.conn.execute(
        """INSERT INTO source_trade_enrichments (
               enrichment_id, source_trade_internal_id, status,
               token_id, condition_id, market_id, market_slug, market_title,
               outcome_identity, event_identity, normalized_category,
               taxonomy_status, market_start_at, market_end_at, market_state,
               evidence_source, gamma_source, evidence_hash, reason_codes_json,
               fetched_at, created_at, updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            enrichment_id,
            source_trade_internal_id,
            status,
            ev.get("token_id"),
            ev.get("condition_id"),
            ev.get("market_id"),
            ev.get("market_slug"),
            ev.get("market_title"),
            ev.get("outcome_identity"),
            ev.get("event_identity"),
            ev.get("normalized_category"),
            ev.get("taxonomy_status"),
            ev.get("market_start_at"),
            ev.get("market_end_at"),
            ev.get("market_state"),
            ev.get("evidence_source"),
            ev.get("gamma_source") if used_gamma else None,
            evidence_hash,
            json.dumps(reason_codes, sort_keys=True),
            now,
            now,
            now,
        ),
    )
    db.conn.commit()
