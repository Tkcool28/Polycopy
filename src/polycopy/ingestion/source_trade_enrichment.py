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

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from polycopy.scoring.wallet_evidence import (
    CATEGORY_TAXONOMY_PARTIAL,
    CATEGORY_TAXONOMY_USABLE,
    classify_category_taxonomy,
    normalize_category_label,
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


def _resolve_market_meta(market: Any) -> dict[str, Any]:
    """Extract taxonomy/event/series from a Gamma market object (Mapping or dict)."""
    if market is None:
        return {}
    get = market.get if isinstance(market, dict) else (
        lambda k, d=None: market.get(k, d) if hasattr(market, "get") else d
    )
    out: dict[str, Any] = {}
    cat = normalize_category_label(get("category"))
    if cat is not None:
        out["category"] = cat
    tags = get("tags")
    if isinstance(tags, list):
        out["tags"] = tags
    event = get("events")
    if isinstance(event, list) and event:
        out["event"] = event[0]
    series = get("series")
    if isinstance(series, list) and series:
        out["series"] = series[0]
    slug = get("slug")
    if isinstance(slug, str) and slug.strip():
        out["market_slug"] = slug
    title = get("question") or get("title")
    if isinstance(title, str) and title.strip():
        out["market_title"] = title
    end_date = get("end_date")
    if end_date is not None:
        out["end_date"] = str(end_date)
    return out


def _build_evidence(
    row: dict[str, Any],
    *,
    gamma_market: Optional[Any] = None,
) -> dict[str, Any]:
    """Resolve normalized evidence from the source trade + optional Gamma market.

    The source_trades.metadata_json is the trusted provenance; if a live Gamma
    market is supplied it is merged ONLY for fields the metadata does not
    already carry (no silent overwrite). Category is taken from taxonomy only;
    it is never inferred from wallet behavior.
    """
    meta = _coerce_metadata_json(row.get("metadata_json"))
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

    # Market metadata from source-trade provenance (trusted).
    gm = _resolve_market_meta(gamma_market) if gamma_market is not None else {}
    merged_meta = dict(meta)
    for k, v in gm.items():
        merged_meta.setdefault(k, v)  # never overwrite existing provenance

    classification = classify_category_taxonomy(merged_meta)
    status = str(classification.status)
    if status == CATEGORY_TAXONOMY_USABLE and classification.category_label:
        ev["normalized_category"] = classification.category_label
        ev["taxonomy_status"] = "usable"
    elif status == CATEGORY_TAXONOMY_PARTIAL:
        ev["taxonomy_status"] = "partial"
    else:
        ev["taxonomy_status"] = "unavailable"

    event = merged_meta.get("event")
    if isinstance(event, dict):
        ev["event_identity"] = event.get("id") or event.get("slug")

    slug = merged_meta.get("market_slug") or (gm.get("market_slug") if gm else None)
    if slug:
        ev["market_slug"] = slug
    title = merged_meta.get("market_title") or (gm.get("market_title") if gm else None)
    if title:
        ev["market_title"] = title

    end_date = merged_meta.get("end_date") or (gm.get("end_date") if gm else None)
    if end_date:
        ev["market_end_at"] = str(end_date)
    if row.get("timestamp"):
        ev["market_start_at"] = str(row["timestamp"])

    ev["evidence_source"] = "source_trade_metadata"
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
                gamma_market = gamma_resolver(condition_id)
        except Exception:  # bounded; record and continue on metadata only
            reason_codes.append("gamma_error")
            gamma_market = None
            # Do not fail enrichment on a network error; mark as durable error
            # state but still persist the metadata-derived evidence.

    ev = _build_evidence(row, gamma_market=gamma_market)
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
