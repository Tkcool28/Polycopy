"""Bounded, canonical-only collection for exactly one approved source wallet.

This module reuses the PR24Z pipeline and its single source-trade writer.  It
never discovers wallets, creates fallback identities, or invokes scoring.

PR68 additions (bounded canonical ingestion):
  * ``--source-trade-id`` selects EXACTLY one public external source_trade_id
    (no prefix, no internal-id, no fuzzy matching).
  * ``--limit`` bounds the write to at most N accepted rows (default small).
  * ``--allow-live`` authorizes the production-persistence gates (NOT live
    order execution).
  * ``--confirm-production-db`` confirms the target is the production DB and a
    verified backup is allowed. Without it, no production DB is opened.
  * A production write WITHOUT ``--source-trade-id`` is REJECTED (manual-only
    until automation is explicitly restored in a later operational task).
  * Metadata enrichment: when a selected row already exists with empty
    ``metadata_json``, the exact source identity may be enriched from the
    trusted Gamma market; immutable identity/economic columns are never
    touched. Material conflicts are reported and not overwritten.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

from polycopy.ingestion import ingest_pipeline
from polycopy.ingestion.normalized_source_trade import NormalizedSourceTrade

APPROVED_WALLET_ENV = "POLYCOPY_APPROVED_SOURCE_WALLET"
MAX_RECORDS = 25
MAX_PAGES = 1
NETWORK_TIMEOUT_S = 10.0
_WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$", re.IGNORECASE)


class UnsafeCollectorConfiguration(ValueError):
    """Raised for absent, malformed, plural, or conflicting wallet settings."""


def normalize_single_wallet(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        raise UnsafeCollectorConfiguration(f"{APPROVED_WALLET_ENV} is required")
    # Explicitly reject comma/whitespace-separated configuration rather than
    # silently selecting a wallet.
    if len(raw.split()) != 1 or "," in raw or ";" in raw:
        raise UnsafeCollectorConfiguration("exactly one approved wallet is required")
    if not _WALLET_RE.fullmatch(raw):
        raise UnsafeCollectorConfiguration("approved wallet is malformed")
    return raw.lower()


def resolve_wallet(cli_wallet: str | None, env: dict[str, str] | None = None) -> str:
    configured = normalize_single_wallet((env or os.environ).get(APPROVED_WALLET_ENV))
    if cli_wallet is None:
        return configured
    requested = normalize_single_wallet(cli_wallet)
    if requested != configured:
        raise UnsafeCollectorConfiguration("command-line wallet conflicts with approved wallet")
    return configured


@dataclass
class CollectionResult:
    wallet: str
    raw_records: int
    buy_records: int
    sell_records_excluded: int
    accepted_rows: list[NormalizedSourceTrade]
    rejected_records: int
    fallback_identities: int
    ambiguous_identities: int
    legacy_aliases_used: int = 0
    errors: int = 0

    # PR68 bounded-selection / enrichment accounting.
    requested_source_trade_id: Optional[str] = None
    selected_count: int = 0
    metadata_enriched: int = 0
    metadata_conflict: int = 0
    metadata_reused: int = 0
    # Taxonomy classification of accepted/enriched rows.
    taxonomy_usable: int = 0
    taxonomy_partial: int = 0
    taxonomy_unavailable: int = 0

    def report(self, *, existing_canonical_records: int = 0, writes_performed: int = 0,
               inserted: int = 0, deduplicated: int = 0, committed: bool = False) -> dict[str, Any]:
        attempted = len(self.accepted_rows)
        return {
            "wallet": self.wallet,
            "raw_records": self.raw_records,
            "buy_records": self.buy_records,
            "sell_records_excluded": self.sell_records_excluded,
            "existing_canonical_records": existing_canonical_records,
            "new_canonical_records": max(0, attempted - existing_canonical_records),
            "attempted": attempted,
            "inserted": inserted,
            "deduplicated": deduplicated,
            "rejected_records": self.rejected_records,
            "errors": self.errors,
            "committed": committed,
            "fallback_identities": self.fallback_identities,
            "ambiguous_identities": self.ambiguous_identities,
            "legacy_aliases_used": self.legacy_aliases_used,
            "writes_performed": writes_performed,
            "maximum_records": MAX_RECORDS,
            "maximum_pages": MAX_PAGES,
            "network_timeout_seconds": NETWORK_TIMEOUT_S,
            # PR68 fields
            "requested_source_trade_id": self.requested_source_trade_id,
            "selected_count": self.selected_count,
            "metadata_enriched": self.metadata_enriched,
            "metadata_conflict": self.metadata_conflict,
            "metadata_reused": self.metadata_reused,
            "taxonomy_usable": self.taxonomy_usable,
            "taxonomy_partial": self.taxonomy_partial,
            "taxonomy_unavailable": self.taxonomy_unavailable,
        }


def _classify_taxonomy(metadata: Optional[dict[str, Any]]) -> str:
    """Return usable|partial|unavailable for a canonical PR66 metadata dict."""
    if not isinstance(metadata, dict):
        return "unavailable"
    from polycopy.scoring.wallet_evidence import (
        CATEGORY_TAXONOMY_PARTIAL,
        CATEGORY_TAXONOMY_USABLE,
        classify_category_taxonomy,
    )

    try:
        cls = classify_category_taxonomy(metadata)
    except Exception:
        return "unavailable"
    status = str(cls.status)
    if status == CATEGORY_TAXONOMY_USABLE:
        return "usable"
    if status == CATEGORY_TAXONOMY_PARTIAL:
        return "partial"
    return "unavailable"


def _raw_gamma_resolver_adapter(adapter: Any) -> Any:
    """Wrap a PolymarketPublicAdapter as an async gamma_resolver(condition_id).

    Returns the trusted RAW Gamma market dict (which carries events/series/
    category), used only to populate ``source_trades.metadata_json``. Never
    used for scoring, mapping, or any downstream write.
    """
    async def _resolve(condition_id: str) -> Optional[dict[str, Any]]:
        try:
            return await adapter.get_market_raw(condition_id)
        except Exception:
            return None
    return _resolve


def collect(
    provider: ingest_pipeline.RealTradeSourceProvider,
    wallet: str,
    *,
    source_trade_id: Optional[str] = None,
    gamma_resolver: Optional[Any] = None,
) -> Any:
    """Async fetch retaining only BUY source-provided IDs (PR68 bounded).

    When ``source_trade_id`` is supplied, selection is EXACT-match on the
    public external id (no prefix, no internal id, no fuzzy). BUY-only rules
    still apply; SELL is never selected. ``gamma_resolver`` (optional) enriches
    canonical metadata from the trusted Gamma market for each retained row.
    """
    return _collect_async(
        provider, wallet, source_trade_id=source_trade_id, gamma_resolver=gamma_resolver
    )


async def _collect_async(
    provider: ingest_pipeline.RealTradeSourceProvider,
    wallet: str,
    *,
    source_trade_id: Optional[str] = None,
    gamma_resolver: Optional[Any] = None,
) -> CollectionResult:
    result = await ingest_pipeline.run_ingestion(
        provider, wallet, record_limit=MAX_RECORDS, max_pages=MAX_PAGES,
        requested_wallet=wallet, gamma_resolver=gamma_resolver,
    )
    canonical_rows: list[NormalizedSourceTrade] = []
    rejected = result.counters.rows_rejected
    fallback = 0
    ambiguous = 0
    for row in result.candidates:
        if row.validation_status != "valid":
            continue
        if row.identity_fallback:
            fallback += 1
            rejected += 1
            continue
        if not row.identity_source_provided:
            # Transaction identities are deliberately not a recurring
            # collector identity. Canonical source-provided ID is mandatory.
            rejected += 1
            continue
        canonical_rows.append(row)
    # Ambiguous rows are already invalid in the shared normalizer.
    ambiguous = sum(1 for row in result.candidates if not row.source_trade_id)

    # PR68 exact source-trade selection (no prefix, no internal id).
    # Normalize the requested id using the SAME canonical SourceTrade Writer
    # helper (_namespace_v2_id) that produced the persisted source_trade_id, so
    # the match is stable and there is no second normalization rule. This is an
    # exact match against the canonical public id (never fuzzy/prefix).
    req_id = source_trade_id
    if req_id is not None:
        from polycopy.ingestion.normalized_source_trade import _namespace_v2_id

        req_id = _namespace_v2_id(req_id)
    if req_id is not None:
        filtered = [r for r in canonical_rows if r.source_trade_id == req_id]
        rejected += len(canonical_rows) - len(filtered)
    else:
        filtered = canonical_rows

    collected = CollectionResult(
        wallet=wallet,
        raw_records=result.counters.raw_records,
        buy_records=result.counters.raw_buy_records,
        sell_records_excluded=result.counters.raw_sell_records,
        accepted_rows=filtered,
        rejected_records=rejected,
        fallback_identities=fallback,
        ambiguous_identities=ambiguous,
        errors=1 if result.error else 0,
        requested_source_trade_id=source_trade_id,
        selected_count=len(filtered),
    )
    # Taxonomy classification per accepted row (honest; no inference).
    for row in filtered:
        status = _classify_taxonomy(row.metadata)
        if status == "usable":
            collected.taxonomy_usable += 1
        elif status == "partial":
            collected.taxonomy_partial += 1
        else:
            collected.taxonomy_unavailable += 1
    return collected


def collect_sync(provider: ingest_pipeline.RealTradeSourceProvider, wallet: str) -> CollectionResult:
    return asyncio.run(collect(provider, wallet))
