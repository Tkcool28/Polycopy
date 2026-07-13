"""PR66 bounded, one-wallet historical evidence collection (no DB access).

This module is PURE: it never opens a database and never makes a network call
of its own. Pagination, dedup, normalization, cutoffs, and reporting all happen
here; the *provider* owns any network access.

Live-read contract (enforced by the CLI, not here):
  * The injected ``provider`` is responsible for turning ``page`` into a real
    upstream offset so page 2+ requests OLDER records upstream (true offset
    pagination, not a local re-slice of page 0).
  * ``provider.fetch_trades(wallet, limit=per_page, page=page)`` is the only
    fetch boundary this module calls. Tests inject a scripted fake; the live
    CLI injects a wrapper over ``PolymarketPublicAdapter.get_trades_by_address``
    that forwards ``page * per_page`` as the data-api ``offset``.

Hard bounds (never exceeded even by caller):
  * HARD_MAX_PAGES = 5
  * HARD_MAX_RECORDS = 250
  * Defaults: max_pages = 2, max_records = 100

Stop reasons (one is always reported):
  empty_page, short_page, max_pages, max_records, before_cutoff,
  after_cutoff, provider_error, completed.

The data-api ``/trades?user=`` endpoint is verified (2026-06-28) to return
newest-first. Therefore an ``after`` (oldest) boundary enables safe early
termination (``after_cutoff``): once a record is older than ``after`` all
subsequent records are older. A ``before`` (newest) boundary is a filter-only
guard (no premature stop) so the path stays robust if upstream ordering is ever
not guaranteed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from polycopy.ingestion.ingest_pipeline import RealTradeSourceProvider
from polycopy.ingestion.normalized_source_trade import (
    normalize_source_trade,
    SOURCE_NAME,
)

HARD_MAX_PAGES = 5
HARD_MAX_RECORDS = 250
DEFAULT_MAX_PAGES = 2
DEFAULT_MAX_RECORDS = 100

# Stop-reason vocabulary (all required by the PR66 report contract).
STOP_EMPTY_PAGE = "empty_page"
STOP_SHORT_PAGE = "short_page"
STOP_MAX_PAGES = "max_pages"
STOP_MAX_RECORDS = "max_records"
STOP_BEFORE_CUTOFF = "before_cutoff"
STOP_AFTER_CUTOFF = "after_cutoff"
STOP_PROVIDER_ERROR = "provider_error"
STOP_COMPLETED = "completed"


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _wallet_prefix(wallet: str) -> str:
    """Redact the wallet: keep only 0x + first 4 hex chars + ellipsis."""
    w = wallet.strip().lower()
    if len(w) <= 8:
        return w[:2] + "…"
    return w[:6] + "…"


@dataclass
class _ErrorRecord:
    page: int
    record_index: int | None
    error_type: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "record_index": self.record_index,
            "error_type": self.error_type,
            "message": self.message[:300],
        }


@dataclass
class HistoricalEvidenceResult:
    wallet: str
    accepted_rows: list[Any] = field(default_factory=list)
    raw_records: int = 0
    rejected_records: int = 0
    pages_fetched: int = 0
    api_duplicate_count: int = 0
    db_duplicate_count: int = 0
    errors: list[_ErrorRecord] = field(default_factory=list)
    stop_reason: str = STOP_EMPTY_PAGE
    oldest_timestamp: datetime | None = None
    newest_timestamp: datetime | None = None

    # Filled by the CLI (context the pure module cannot know):
    dry_run: bool = True
    live_read_performed: bool = False
    committed: bool = False
    inserted: int = 0
    duration_seconds: float = 0.0

    # ── derived helpers ──
    @property
    def normalized_records(self) -> int:
        return len(self.accepted_rows)

    @property
    def buy_count(self) -> int:
        return sum(1 for r in self.accepted_rows if r.side == "BUY")

    @property
    def sell_count(self) -> int:
        return sum(1 for r in self.accepted_rows if r.side == "SELL")

    @property
    def would_insert(self) -> int:
        return max(0, self.normalized_records - self.db_duplicate_count)

    def report(self) -> dict[str, Any]:
        return {
            "wallet_prefix": _wallet_prefix(self.wallet),
            "pages_fetched": self.pages_fetched,
            "raw_records": self.raw_records,
            "normalized_records": self.normalized_records,
            "buy_count": self.buy_count,
            "sell_count": self.sell_count,
            "rejected_count": self.rejected_records,
            "api_duplicate_count": self.api_duplicate_count,
            "db_duplicate_count": self.db_duplicate_count,
            "would_insert": self.would_insert,
            "inserted": self.inserted,
            "oldest_timestamp": self.oldest_timestamp.isoformat()
            if self.oldest_timestamp
            else None,
            "newest_timestamp": self.newest_timestamp.isoformat()
            if self.newest_timestamp
            else None,
            "errors": [e.as_dict() for e in self.errors],
            "stop_reason": self.stop_reason,
            "dry_run": self.dry_run,
            "live_read_performed": self.live_read_performed,
            "committed": self.committed,
            "duration_seconds": round(self.duration_seconds, 3),
            "source": SOURCE_NAME,
        }


async def collect_historical_evidence(
    provider: RealTradeSourceProvider,
    wallet: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_records: int = DEFAULT_MAX_RECORDS,
    page_size: int | None = None,
    before: str | None = None,
    after: str | None = None,
    existing_ids: set[str] | None = None,
) -> HistoricalEvidenceResult:
    """Fetch a deterministically bounded history, accepting validated BUY and SELL.

    Empty/short pages, timestamp cutoffs, max bounds, malformed rows, and a
    provider exception all terminate without retry. This function never opens a
    database and never discovers another wallet.

    ``page_size`` is the upstream page size (the number of rows requested per
    page) and is a SEPARATE bound from ``max_records`` (the total accepted-row
    budget across all pages). The default keeps the prior safe behavior; an
    explicit small ``page_size`` lets callers page in small windows without
    exhausting the record budget on the first page.

    ``existing_ids`` (optional, read-only set of canonical source_trade_ids
    already present in source_trades) is used ONLY to count db duplicates; it
    does not change which rows are accepted or written.
    """
    started = time.monotonic()
    pages = max(1, min(int(max_pages), HARD_MAX_PAGES))
    records = max(1, min(int(max_records), HARD_MAX_RECORDS))
    if page_size is None:
        per_page = min(records, 100)
    else:
        # Independent page size: bounded to [1, 100]. The record budget is
        # enforced separately by the per-row check, so page_size need not be
        # <= max_records (a small page just means more fetch calls).
        per_page = max(1, min(int(page_size), 100))
    before_dt, after_dt = _time(before), _time(after)
    if before_dt and after_dt and after_dt > before_dt:
        raise ValueError("--after must not be later than --before")

    result = HistoricalEvidenceResult(wallet=wallet.lower())
    result.live_read_performed = bool(getattr(provider, "made_network_call", False))
    seen: set[str] = set()
    pre = existing_ids or set()

    provider_error = False
    after_cutoff_hit = False
    before_filtered_any = False

    for page in range(pages):
        try:
            raw_rows = await provider.fetch_trades(result.wallet, limit=per_page, page=page)
        except Exception as exc:  # never retry; one bad page ends the run
            result.errors.append(
                _ErrorRecord(page=page, record_index=None, error_type=STOP_PROVIDER_ERROR,
                             message=f"{type(exc).__name__}: {exc}")
            )
            provider_error = True
            break
        result.pages_fetched += 1

        if not isinstance(raw_rows, list):
            result.errors.append(
                _ErrorRecord(page=page, record_index=None, error_type="bad_provider_response",
                             message="provider returned a non-list")
            )
            result.stop_reason = STOP_EMPTY_PAGE
            break
        if not raw_rows:
            result.stop_reason = STOP_EMPTY_PAGE
            break

        page_before_filtered = 0
        for idx, raw in enumerate(raw_rows):
            if result.raw_records >= records:
                result.stop_reason = STOP_MAX_RECORDS
                return _finish(result, started)

            # Every upstream list item consumes the hard record budget,
            # including malformed payloads, so malformed pages cannot bypass it.
            result.raw_records += 1
            if not isinstance(raw, dict):
                result.rejected_records += 1
                result.errors.append(
                    _ErrorRecord(page=page, record_index=idx, error_type="malformed_record",
                                 message="non-dict item skipped")
                )
                continue

            candidate = normalize_source_trade(
                raw,
                requested_wallet=result.wallet,
                record_index=result.raw_records - 1,
                allow_sell=True,
            )

            # Cutoff filtering (newest-first upstream: after enables early stop).
            if candidate.timestamp:
                if before_dt and candidate.timestamp > before_dt:
                    before_filtered_any = True
                    page_before_filtered += 1
                    continue
                if after_dt and candidate.timestamp < after_dt:
                    after_cutoff_hit = True
                    break

            if candidate.validation_status != "valid" or not candidate.source_trade_id:
                result.rejected_records += 1
                continue

            sid = candidate.source_trade_id
            if sid in seen:
                # Same canonical trade repeated within/across fetched pages.
                result.api_duplicate_count += 1
                continue
            seen.add(sid)
            result.accepted_rows.append(candidate)
            if sid in pre:
                result.db_duplicate_count += 1

            # Track oldest/newest from accepted records.
            ts = candidate.timestamp
            if ts is not None:
                if result.oldest_timestamp is None or ts < result.oldest_timestamp:
                    result.oldest_timestamp = ts
                if result.newest_timestamp is None or ts > result.newest_timestamp:
                    result.newest_timestamp = ts

        # Re-check after_cutoff that may have broken the inner loop.
        if after_cutoff_hit:
            result.stop_reason = STOP_AFTER_CUTOFF
            break
        # A full page entirely newer than --before means upstream has nothing
        # older to offer within the window; stop rather than burn more pages.
        if page_before_filtered and page_before_filtered == len(raw_rows):
            result.stop_reason = STOP_BEFORE_CUTOFF
            break
        if len(raw_rows) < per_page:
            result.stop_reason = STOP_SHORT_PAGE
            break
    else:
        # The full requested page budget was consumed without another stop.
        if before_filtered_any and not result.accepted_rows:
            result.stop_reason = STOP_BEFORE_CUTOFF
        elif pages >= HARD_MAX_PAGES:
            result.stop_reason = STOP_MAX_PAGES
        else:
            result.stop_reason = STOP_COMPLETED

    if provider_error and result.stop_reason == STOP_EMPTY_PAGE:
        # A provider error with no successful page is reported as provider_error.
        result.stop_reason = STOP_PROVIDER_ERROR

    return _finish(result, started)


def _finish(result: HistoricalEvidenceResult, started: float) -> HistoricalEvidenceResult:
    result.duration_seconds = time.monotonic() - started
    return result
