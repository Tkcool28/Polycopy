"""Authoritative, durable enrichment for one exact approved source trade.

This module resolves and persists the canonical, scorer-visible evidence
required to prove a source trade is a safe, traceable copy-candidate input. It
operates on ONE exact ``source_trades.id`` (never an arbitrary wallet-history
fetch) and performs TWO atomic writes inside one SAVEPOINT:

  1. ``source_trades.metadata_json`` — the SCORER-VISIBLE canonical nested
     contract (``taxonomy.raw_category``), written ONLY when the merge status
     permits (FILLED / UNCHANGED).
  2. ``source_trade_enrichments`` — one CURRENT audit/provenance row, owned by
     :mod:`polycopy.ingestion.source_trade_provenance`.

The scoring authority is, and remains,
``source_trades.metadata_json['taxonomy']['raw_category']``.
``source_trade_enrichments`` is audit/provenance state only and is never a
scoring authority.

S5 repair scope
---------------
* The broken enrichment-versioning contract (which mutated ``enrichment_id`` to
  ``archived:<id>`` and inserted a second row to dodge
  ``UNIQUE(source_trade_internal_id)``) is REMOVED. There is exactly one
  current audit row per source trade. See
  :func:`source_trade_provenance.write_provenance` for the contract.
* Honest merge-status / reason handling (FILLED / UNCHANGED / CONFLICT /
  UNAVAILABLE) — conflict and unavailable preserve ``metadata_json``
  byte-for-byte and never report complete.
* Exact source-trade eligibility (Polymarket BUY, non-sample, non-empty
  market_source_id) with explicit refusal reason codes and zero provider calls
  or DB writes after a refusal.
* The real Gamma route: ``PolymarketPublicAdapter.get_market_raw(condition_id)``,
  at most once per trade, with provider-error distinct from not-found and a
  fail-closed ``aclose()`` in a finally block.
* Exact source-trade identity: ``token_id`` from ``source_trades.token_id``,
  ``condition_id``/``market_id`` from ``market_source_id``,
  ``outcome_identity`` from ``outcome``. A missing token stays missing and
  fails closed through the canonical token-membership contract.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from polycopy.ingestion.canonical_metadata import (
    MERGE_CONFLICT,
    MERGE_UNAVAILABLE,
    merge_canonical_metadata,
)
from polycopy.ingestion.source_trade_provenance import (
    STATUS_ERROR,
    build_provenance_payload,
    enrichment_status_allows_dispatch,
    evidence_hash,
    get_current_enrichment,
    write_provenance,
)
from polycopy.ingestion.normalized_source_trade import SOURCE_NAME

# Exact, repository-proven Polymarket source_trades writer values (no fuzzy
# matching, no id prefixes). A bare "polymarket" literal is NOT a proven
# source_trades writer value here (it is used for the markets/raw_snapshots
# tables), so it is intentionally excluded.
POLYMARKET_SOURCES = frozenset({SOURCE_NAME, "polymarket_clob"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Refusal reason codes (explicit; zero provider calls / DB writes after these).
SOURCE_TRADE_NOT_FOUND = "source_trade_not_found"
SOURCE_NOT_SUPPORTED = "source_not_supported"
SELL_NOT_SUPPORTED = "sell_not_supported"
SAMPLE_TRADE_REFUSED = "sample_trade_refused"
MISSING_MARKET_IDENTITY = "missing_market_identity"


def _read_source_trade(db: Any, source_trade_internal_id: str) -> Optional[dict[str, Any]]:
    row = db.fetchone(
        "SELECT id, source, source_trade_id, market_source_id, token_id, "
        "side, outcome, timestamp, metadata_json, is_sample FROM source_trades "
        "WHERE id=?",
        (source_trade_internal_id,),
    )
    return dict(row) if row is not None else None


def get_enrichment(db: Any, source_trade_internal_id: str) -> Optional[dict[str, Any]]:
    """Return the single current enrichment row for a source trade (or None).

    Delegates to the shared one-current-row provenance module. Kept as a stable
    public alias so existing callers/tests keep working.
    """
    return get_current_enrichment(db, source_trade_internal_id)


@dataclass
class _Eligibility:
    ok: bool
    reason: Optional[str] = None


def _check_eligibility(row: dict[str, Any]) -> _Eligibility:
    """Exact source-trade eligibility (no prefix/fuzzy matching).

    Accepts only:
      * source in {polymarket_data_api_trades_user, polymarket_clob}
      * side == BUY
      * is_sample == 0
      * non-empty market_source_id
    """
    if not row.get("id"):
        return _Eligibility(ok=False, reason=SOURCE_TRADE_NOT_FOUND)
    side = str(row.get("side") or "").upper()
    if side != "BUY":
        return _Eligibility(ok=False, reason=SELL_NOT_SUPPORTED)
    if bool(row.get("is_sample")):
        return _Eligibility(ok=False, reason=SAMPLE_TRADE_REFUSED)
    source = row.get("source")
    if source not in POLYMARKET_SOURCES:
        return _Eligibility(ok=False, reason=SOURCE_NOT_SUPPORTED)
    if not (row.get("market_source_id") or "").strip():
        return _Eligibility(ok=False, reason=MISSING_MARKET_IDENTITY)
    return _Eligibility(ok=True)


def _call_gamma_resolver(
    gamma_resolver: Callable[[str], Any], condition_id: str
) -> Any:
    """Invoke a sync OR async gamma resolver, bounded to one call."""
    market = gamma_resolver(condition_id)
    if inspect.isawaitable(market):
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    return ex.submit(asyncio.run, market).result()
        except RuntimeError:
            pass
        return asyncio.run(market)
    return market


# Gamma resolution states (distinct from ordinary missing evidence).
GAMMA_FOUND = "found"
GAMMA_NOT_FOUND = "not_found"
GAMMA_PROVIDER_ERROR = "provider_error"
GAMMA_AMBIGUOUS = "ambiguous"
GAMMA_MALFORMED = "malformed"


def resolve_gamma_state(
    gamma_resolver: Callable[[str], Any], condition_id: str
) -> tuple[Optional[dict[str, Any]], str, Optional[str]]:
    """Resolve one condition id through the real Gamma route, distinguishing:

      * found            -> authoritative Gamma dict returned
      * not_found        -> resolver returned None (no exact match)
      * provider_error    -> exception raised (NEVER conflated with not_found)
      * ambiguous        -> resolver signalled an ambiguous selection
      * malformed        -> resolver signalled an unexpected payload shape

    The resolver must be a thin wrapper around
    ``PolymarketPublicAdapter.get_market_raw`` (NOT a catch-everything wrapper
    that swallows provider exceptions into None — that would convert provider
    failure into ordinary missing evidence). Returns ``(market, state, reason)``.
    """
    try:
        market = _call_gamma_resolver(gamma_resolver, condition_id)
    except ValueError as exc:
        msg = str(exc)
        if "ambiguous" in msg:
            return None, GAMMA_AMBIGUOUS, msg
        return None, GAMMA_MALFORMED, msg
    except Exception as exc:  # HTTP / network / client failure
        return None, GAMMA_PROVIDER_ERROR, f"{type(exc).__name__}: {exc}"
    if market is None:
        return None, GAMMA_NOT_FOUND, None
    return market, GAMMA_FOUND, None


@dataclass
class EnrichmentResult:
    source_trade_internal_id: str
    enrichment_id: Optional[str]
    status: str
    created: bool
    updated: bool
    reason_codes: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    metadata_changed: bool = False
    operational_error: bool = False
    provider_error: bool = False
    selection_error: bool = False
    error_message: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_trade_internal_id": self.source_trade_internal_id,
            "enrichment_id": self.enrichment_id,
            "status": self.status,
            "created": self.created,
            "updated": self.updated,
            "metadata_changed": self.metadata_changed,
            "operational_error": self.operational_error,
            "provider_error": self.provider_error,
            "selection_error": self.selection_error,
            "reason_codes": self.reason_codes,
            "evidence": self.evidence,
            "error_message": self.error_message,
        }

    def with_provider_error(self) -> "EnrichmentResult":
        """Return a copy flagged as a Gamma provider (hard) failure."""
        return EnrichmentResult(
            source_trade_internal_id=self.source_trade_internal_id,
            enrichment_id=self.enrichment_id,
            status=self.status,
            created=self.created,
            updated=self.updated,
            reason_codes=list(self.reason_codes),
            evidence=self.evidence,
            metadata_changed=self.metadata_changed,
            operational_error=self.operational_error,
            provider_error=True,
            error_message=self.error_message,
        )

    def allows_dispatch(self) -> bool:
        """Convenience seam: mirror the dispatcher gate without invoking it."""
        return enrichment_status_allows_dispatch(self.status)


def enrich_source_trade(
    db: Any,
    source_trade_internal_id: str,
    *,
    gamma_resolver: Optional[Callable[[str], Any]] = None,
    dry_run: bool = False,
) -> EnrichmentResult:
    """Resolve and persist (or link) authoritative enrichment for one trade.

    The ``gamma_resolver`` (optional) is ``Callable[[condition_id] -> market|None``
    and is invoked at most once, behind the caller's own timeout. It MUST be a
    thin wrapper around ``PolymarketPublicAdapter.get_market_raw`` (see
    :func:`resolve_gamma_state`) — NOT a catch-everything wrapper that returns
    None on provider error.
    """
    row = _read_source_trade(db, source_trade_internal_id)
    if row is None:
        return EnrichmentResult(
            source_trade_internal_id=source_trade_internal_id,
            enrichment_id=None,
            status=STATUS_ERROR,
            created=False,
            updated=False,
            reason_codes=[SOURCE_TRADE_NOT_FOUND],
            selection_error=True,
            error_message="source_trade_internal_id not found",
        )

    eligibility = _check_eligibility(row)
    if not eligibility.ok:
        # Explicit refusal. No provider call, no DB write.
        return EnrichmentResult(
            source_trade_internal_id=source_trade_internal_id,
            enrichment_id=None,
            status=STATUS_ERROR,
            created=False,
            updated=False,
            reason_codes=[eligibility.reason or SOURCE_NOT_SUPPORTED],
            selection_error=True,
            error_message=f"eligibility refused: {eligibility.reason}",
        )

    condition_id = row.get("market_source_id") or ""

    # One bounded Gamma request, if authorized.
    gamma_market: Optional[Any] = None
    gamma_state = GAMMA_NOT_FOUND
    gamma_reason: Optional[str] = None
    if gamma_resolver is not None:
        gamma_market, gamma_state, gamma_reason = resolve_gamma_state(
            gamma_resolver, condition_id
        )

    # A Gamma provider error is an operational (hard) failure: it remains
    # distinguishable from ordinary not-found, and the library can create an
    # audit row in the caller-owned transaction. The CLI rolls the transaction
    # back and returns exit 1, so the audit row is NOT durably persisted by
    # this CLI. status stays "error" for dispatcher-blocking/audit semantics;
    # provider_error flags the hard case.
    if gamma_state == GAMMA_PROVIDER_ERROR:
        payload = build_provenance_payload(
            source_trade=row,
            canonical_meta=row.get("metadata_json"),
            gamma_market=None,
            merge_status=MERGE_UNAVAILABLE,
            gamma_state=gamma_state,
            gamma_reason=gamma_reason,
            merge_reasons=[],
        )
        return _persist(
            db, source_trade_internal_id, payload, dry_run=dry_run,
            metadata_json=None,
        ).with_provider_error()
    merge_reasons: list[str]
    canonical_meta: Any
    if gamma_market is not None:
        canonical_meta, merge_status, merge_reasons = merge_canonical_metadata(
            row.get("metadata_json"),
            gamma_market,
            condition_id=condition_id,
            token_id=row.get("token_id"),
        )
    else:
        # No Gamma evidence: merge against an empty market. This preserves
        # existing metadata (merge status unchanged/unavailable) without a
        # network call.
        canonical_meta, merge_status, merge_reasons = merge_canonical_metadata(
            row.get("metadata_json"),
            None,
            condition_id=condition_id,
            token_id=row.get("token_id"),
        )

    # Honest merge-status handling.
    if merge_status == MERGE_CONFLICT:
        # Preserve source_trades.metadata_json byte-for-byte; no metadata write.
        payload = build_provenance_payload(
            source_trade=row,
            canonical_meta=canonical_meta,
            gamma_market=gamma_market,
            merge_status=merge_status,
            gamma_state=gamma_state,
            gamma_reason=gamma_reason,
            merge_reasons=merge_reasons,
        )
        return _persist(
            db, source_trade_internal_id, payload, dry_run=dry_run,
            metadata_json=None,
        )

    if merge_status == MERGE_UNAVAILABLE:
        # Preserve source_trades.metadata_json byte-for-byte; no metadata write.
        # status stays unavailable (unless a provider error escalates to error).
        payload = build_provenance_payload(
            source_trade=row,
            canonical_meta=canonical_meta,
            gamma_market=gamma_market,
            merge_status=merge_status,
            gamma_state=gamma_state,
            gamma_reason=gamma_reason,
            merge_reasons=merge_reasons,
        )
        return _persist(
            db, source_trade_internal_id, payload, dry_run=dry_run,
            metadata_json=None,
        )

    # MERGE_FILLED / MERGE_UNCHANGED: canonical metadata may be persisted and
    # classified. Write the deterministic canonical metadata to source_trades.
    # On CONFLICT/UNAVAILABLE we passed metadata_json=None to skip the write.
    new_metadata_json = json.dumps(canonical_meta, sort_keys=True, separators=(",", ":"))
    payload = build_provenance_payload(
        source_trade=row,
        canonical_meta=canonical_meta,
        gamma_market=gamma_market,
        merge_status=merge_status,
        gamma_state=gamma_state,
        gamma_reason=gamma_reason,
        merge_reasons=merge_reasons,
    )
    return _persist(
        db, source_trade_internal_id, payload, dry_run=dry_run,
        metadata_json=new_metadata_json,
    )


def _persist(
    db: Any,
    source_trade_internal_id: str,
    payload: dict[str, Any],
    *,
    dry_run: bool,
    metadata_json: Optional[str],
) -> EnrichmentResult:
    """Atomic metadata + provenance write inside one SAVEPOINT.

    ``metadata_json`` is None when the merge forbids a metadata write
    (CONFLICT / UNAVAILABLE). On any failure we ROLLBACK TO SAVEPOINT and
    RELEASE it so metadata_json and the enrichment row are left unchanged.

    Replay contract (zero-write when nothing changed)
    -------------------------------------------------
    * When ``metadata_json`` is provided, compare its exact bytes against the
      currently stored ``source_trades.metadata_json``. Pass a metadata UPDATE
      to the SAVEPOINT only when the bytes DIFFER. An identical-byte metadata
      value (e.g. an already-canonical serialization) produces NO UPDATE.
    * The provenance layer independently no-ops when the evidence hash is
      unchanged. So an equivalent replay executes zero SQL against either
      table and preserves every field exactly.
    """
    ev_hash = evidence_hash(payload)

    if dry_run:
        # Compute/report only; zero metadata and provenance writes.
        return EnrichmentResult(
            source_trade_internal_id=source_trade_internal_id,
            enrichment_id=None,
            status=payload["status"],
            created=False,
            updated=False,
            reason_codes=list(payload["reason_codes"]),
            evidence=payload,
        )

    # Determine whether the metadata write is actually required. Comparing the
    # exact stored bytes (not just the parsed object) is what makes an
    # already-canonical value a true zero-write on replay.
    metadata_write_required = False
    if metadata_json is not None:
        row = db.fetchone(
            "SELECT metadata_json FROM source_trades WHERE id=?",
            (source_trade_internal_id,),
        )
        current_meta = row["metadata_json"] if row is not None else None
        metadata_write_required = (current_meta != metadata_json)

    db.conn.execute("SAVEPOINT s5_enrich")
    try:
        metadata_changed = False
        if metadata_write_required:
            db.conn.execute(
                "UPDATE source_trades SET metadata_json = ? WHERE id = ?",
                (metadata_json, source_trade_internal_id),
            )
            metadata_changed = True
        changed, is_new, enrichment_id = write_provenance(
            db,
            source_trade_internal_id=source_trade_internal_id,
            payload=payload,
            evidence_hash_value=ev_hash,
        )
        db.conn.execute("RELEASE SAVEPOINT s5_enrich")
    except Exception as exc:
        db.conn.execute("ROLLBACK TO SAVEPOINT s5_enrich")
        db.conn.execute("RELEASE SAVEPOINT s5_enrich")
        # Operational (persistence) failure: no partial write survives, and the
        # caller/CLI returns controlled nonzero. status stays error for audit/
        # dispatcher-blocking semantics, but operational_error flags the hard
        # failure so commit is never attempted.
        return EnrichmentResult(
            source_trade_internal_id=source_trade_internal_id,
            enrichment_id=None,
            status=STATUS_ERROR,
            created=False,
            updated=False,
            reason_codes=["persist_failed"],
            evidence=payload,
            operational_error=True,
            error_message=f"{type(exc).__name__}: {exc}",
        )

    return EnrichmentResult(
        source_trade_internal_id=source_trade_internal_id,
        enrichment_id=enrichment_id,
        status=payload["status"],
        created=is_new,
        updated=(changed and not is_new),
        metadata_changed=metadata_changed,
        reason_codes=list(payload["reason_codes"]),
        evidence=payload,
    )
