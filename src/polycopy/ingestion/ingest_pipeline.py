"""PR24Z — Fetch → normalize → validate → dedupe orchestration (no DB write).

This module wires the pure normalization core
(``normalized_source_trade.py``) to an injectable provider (the same
``RealTradeSourceProvider`` contract PR24Y uses). It produces a list of
validated :class:`NormalizedSourceTrade` candidates plus a populated
:class:`IngestionCounters`, WITHOUT touching any database and WITHOUT any
network code of its own (the provider owns the network).

Dry-run by default: this pipeline never opens a DB. The CLI decides whether to
hand the valid candidates to ``SourceTradeWriter``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from polycopy.ingestion.normalized_source_trade import (
    DEFAULT_RECORD_LIMIT,
    HARD_MAX_RECORD_LIMIT,
    HARD_MAX_PAGES,
    IngestionCounters,
    NormalizedSourceTrade,
    normalize_source_trade,
)

# A real on-chain transaction hash: 0x + 8+ hex chars (for strong-id dup detection).
_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{8,}$")


@runtime_checkable
class RealTradeSourceProvider(Protocol):
    """Returns raw data-api ``/trades``-shaped dicts for ONE wallet page.

    Mirrors the PR24Y provider contract so we reuse exactly one source
    (``PolymarketPublicAdapter.get_trades_by_address`` in live mode). The CLI
    wraps the existing adapter; tests inject a fake that returns scripted
    pages and leaves ``made_network_call = False``.
    """

    made_network_call: bool = False

    async def fetch_trades(
        self, wallet: str, *, limit: int, page: int
    ) -> list[dict[str, Any]]:
        ...


@dataclass
class IngestionResult:
    """Full output of a bounded ingestion dry-run / planning pass."""

    candidates: list[NormalizedSourceTrade] = field(default_factory=list)
    valid_rows: list[NormalizedSourceTrade] = field(default_factory=list)
    counters: IngestionCounters = field(default_factory=IngestionCounters)
    network_calls_attempted: int = 0
    network_calls_succeeded: int = 0
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": [c.as_dict() for c in self.candidates],
            "valid_rows": [c.as_dict() for c in self.valid_rows],
            "counters": self.counters.as_dict(),
            "network_calls_attempted": self.network_calls_attempted,
            "network_calls_succeeded": self.network_calls_succeeded,
            "error": self.error,
        }


def _is_strong_id(ident: str) -> bool:
    return ident.startswith("polymarket:0x") and _TX_HASH_RE.match(ident[len("polymarket:"):]) is not None


async def run_ingestion(
    provider: RealTradeSourceProvider,
    wallet: str,
    *,
    record_limit: int = DEFAULT_RECORD_LIMIT,
    max_pages: int = HARD_MAX_PAGES,
    requested_wallet: Optional[str] = None,
) -> IngestionResult:
    """Bounded fetch → normalize → validate → dedupe.

    Returns an :class:`IngestionResult` with valid candidates ready for the
    writer. Never opens a database. Bounds records to ``record_limit`` and
    pages to ``max_pages`` (hard maxes enforced by the caller/CLI).

    Network counting follows PR24Y: only real external HTTP calls (where the
    provider sets ``made_network_call``) are counted; fixture calls are not.
    """
    record_limit = max(1, min(int(record_limit), HARD_MAX_RECORD_LIMIT))
    max_pages = max(1, min(int(max_pages), HARD_MAX_PAGES))
    req = (requested_wallet or wallet).strip().lower()

    result = IngestionResult()
    counters = result.counters
    counters.wallets_requested = 1

    seen_strong_ids: set[str] = set()
    seen_fallback_ids: set[str] = set()

    for page in range(max_pages):
        made_call = bool(getattr(provider, "made_network_call", False))
        if made_call:
            result.network_calls_attempted += 1
        try:
            rows = await provider.fetch_trades(wallet, limit=record_limit, page=page)
        except Exception as exc:  # never crash on one bad page
            result.error = f"provider error on page {page}: {type(exc).__name__}: {exc}"[:300]
            break
        if made_call:
            result.network_calls_succeeded += 1
            counters.pages_fetched += 1
        if not isinstance(rows, list) or not rows:
            break  # empty page -> stop pagination

        for i, raw in enumerate(rows):
            if not isinstance(raw, dict):
                continue
            counters.raw_records += 1
            cand = normalize_source_trade(raw, requested_wallet=req, record_index=counters.raw_records - 1)

            # Classification counters.
            if cand.side == "BUY":
                counters.raw_buy_records += 1
            elif cand.side == "SELL":
                counters.raw_sell_records += 1
            else:
                counters.unknown_side_records += 1

            # Identity counters (strong/fallback only here; ambiguous is
            # counted once via count_rejection below to avoid double counting).
            if cand.source_trade_id:
                counters.stable_ids_generated += 1
            if cand.identity_source_provided:
                counters.source_provided_identity_used_count += 1
            elif cand.identity_transaction_hash:
                counters.transaction_identity_used_count += 1
            elif cand.identity_fallback:
                counters.identity_fallback_used_count += 1
            # Invariant: strong = source_provided + transaction.
            counters.strong_identity_used_count = (
                counters.source_provided_identity_used_count
                + counters.transaction_identity_used_count
            )

            # Readiness counters.
            if cand.pr24u_ready:
                counters.pr24u_ready_count += 1
            if cand.pr24v_ready:
                counters.pr24v_ready_count += 1
            if cand.both_ready:
                counters.both_ready_count += 1

            # ── In-fetch dedupe (only for rows that HAVE a stable id) ──
            if cand.source_trade_id:
                bucket = seen_strong_ids if cand.identity_strong else seen_fallback_ids
                if cand.source_trade_id in bucket:
                    counters.duplicate_records_in_fetch += 1
                    # Still append so the reviewer sees it, but mark rejected.
                    cand.validation_status = "rejected"
                    if "duplicate_in_fetch" not in cand.validation_reasons:
                        cand.validation_reasons.append("duplicate_in_fetch")
                else:
                    bucket.add(cand.source_trade_id)

            # Rejection accounting (once).
            if cand.validation_status == "rejected":
                counters.rows_rejected += 1
                from polycopy.ingestion.normalized_source_trade import count_rejection
                count_rejection(counters, cand)
            else:
                counters.eligible_buy_records += 1

            result.candidates.append(cand)
            if cand.validation_status == "valid":
                result.valid_rows.append(cand)

        # Bounded hard cap across all pages.
        if counters.raw_records >= record_limit * max_pages:
            break

    return result
