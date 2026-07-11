"""Bounded, canonical-only collection for exactly one approved source wallet.

This module reuses the PR24Z pipeline and its single source-trade writer.  It
never discovers wallets, creates fallback identities, or invokes scoring.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any

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
        }


async def collect(provider: ingest_pipeline.RealTradeSourceProvider, wallet: str) -> CollectionResult:
    """Fetch one bounded wallet page and retain only BUY source-provided IDs."""
    result = await ingest_pipeline.run_ingestion(
        provider, wallet, record_limit=MAX_RECORDS, max_pages=MAX_PAGES, requested_wallet=wallet
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
    return CollectionResult(
        wallet=wallet,
        raw_records=result.counters.raw_records,
        buy_records=result.counters.raw_buy_records,
        sell_records_excluded=result.counters.raw_sell_records,
        accepted_rows=canonical_rows,
        rejected_records=rejected,
        fallback_identities=fallback,
        ambiguous_identities=ambiguous,
        errors=1 if result.error else 0,
    )


def collect_sync(provider: ingest_pipeline.RealTradeSourceProvider, wallet: str) -> CollectionResult:
    return asyncio.run(collect(provider, wallet))
