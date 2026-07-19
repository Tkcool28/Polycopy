"""Bounded research-wallet discovery bridge (PR #72).

A SAFE FRONT DOOR into the already-connected PR #71 evidence/scoring pipeline.

Scope (strictly bounded):
  bounded public Polymarket activity
    -> canonical real wallet rows (wallets, is_sample=0)
    -> optional specialist research-watch rows (specialist_evidence_watchlist)

It MUST NOT:
  * label wallets smart;
  * persist full market history, source trades, snapshots;
  * write capability flags, experiment runs;
  * score wallets, approve wallets, dispatch trades;
  * create candidates or paper signals;
  * authorize or execute anything;
  * modify production outside the two allowed tables;
  * add timers or systemd units.

Write scope is exactly two tables:
  * wallets
  * specialist_evidence_watchlist

Safety gates (all required before ANY writable DB open):
  * --allow-live (authorize bounded public network reads)
  * --write
  * --confirm-production-db
  * the global Polycopy operational lock
  * complete bound set (market / trade-per-market / wallet-count / runtime / memory)

Default behavior is DRY-RUN: no network, no DB write, structured JSON only.
A discovery never calls the broad legacy collect_smart_money_data.run_collection
path and never calls evaluate_wallet.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Sequence

from polycopy.db.wallet_identity import canonical_wallet_address


# ── Output contract ────────────────────────────────────────────────────────────
@dataclass
class DiscoveryResult:
    mode: str  # "dry_run" | "live"
    gate_reason: Optional[str] = None
    requested_addresses: int = 0
    accepted_addresses: int = 0
    rejected_addresses: int = 0
    partial_fetches: int = 0
    failed_fetches: int = 0
    wallets_created: int = 0
    wallets_existing: int = 0
    watches_added: int = 0
    watches_existing: int = 0
    promoted_from_partial: int = 0  # must always be 0; partials never promote
    db_written: bool = False
    bounds: dict[str, Any] = field(default_factory=dict)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    addresses: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "gate_reason": self.gate_reason,
            "requested_addresses": self.requested_addresses,
            "accepted_addresses": self.accepted_addresses,
            "rejected_addresses": self.rejected_addresses,
            "partial_fetches": self.partial_fetches,
            "failed_fetches": self.failed_fetches,
            "wallets_created": self.wallets_created,
            "wallets_existing": self.wallets_existing,
            "watches_added": self.watches_added,
            "watches_existing": self.watches_existing,
            "promoted_from_partial": self.promoted_from_partial,
            "db_written": self.db_written,
            "bounds": self.bounds,
            "rejected": self.rejected,
            "addresses": self.addresses,
        }


# ── Bounds ───────────────────────────────────────────────────────────────────
DEFAULT_MAX_WALLETS = 100
DEFAULT_MAX_MARKETS_PER_WALLET = 200
DEFAULT_MAX_TRADES_PER_MARKET = 200
DEFAULT_MAX_RUNTIME_S = 300.0
DEFAULT_MAX_MEMORY_MB = 512
REQUEST_BUDGET = 100  # bounded public reads


def _default_bounds() -> dict[str, Any]:
    return {
        "max_wallets": DEFAULT_MAX_WALLETS,
        "max_markets_per_wallet": DEFAULT_MAX_MARKETS_PER_WALLET,
        "max_trades_per_market": DEFAULT_MAX_TRADES_PER_MARKET,
        "max_runtime_s": DEFAULT_MAX_RUNTIME_S,
        "max_memory_mb": DEFAULT_MAX_MEMORY_MB,
        "request_budget": REQUEST_BUDGET,
    }


# ── Adapter seam (bounded public read) ─────────────────────────────────────────
class DiscoveryAdapter(Protocol):
    """Bounded public-read adapter. Returns canonical public activity for an
    address. Implementations MUST distinguish complete / partial / failed
    fetches and MUST NEVER return data that would create candidate/signal/
    approval/execution rows.

    For the live path, inject a thin wrapper over PolymarketPublicAdapter.
    For tests, inject a deterministic fake (scripts/.../fakes).
    """

    def fetch_wallet_activity(self, address: str, bounds: dict[str, Any]) -> "FetchOutcome":
        ...


@dataclass
class FetchOutcome:
    address: str
    status: str  # "complete" | "partial" | "failed"
    markets: int = 0
    trades: int = 0
    error: Optional[str] = None


# ── Address validation (sentinel / anonymous / malformed rejection) ────────────
def classify_address(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Return (canonical_address, reject_reason).

    Rejects sentinel, anonymous, malformed, all-zero, and obvious
    repeated-character fixture addresses via canonical_wallet_address plus an
    explicit repeated-character/sentinel guard. Canonicalization and
    deduplication happen here.
    """
    canonical = canonical_wallet_address(raw)
    if canonical is None:
        return None, "sentinel_or_anonymous"
    if canonical in {
        "0x" + "0" * 64,
        "0x" + "f" * 64,
    }:
        return None, "all_zero_or_all_f_sentinel"
    # obvious repeated-character fixture (e.g. 0xaaaa...aaaa)
    body = canonical[2:]
    if len(set(body)) == 1:
        return None, "repeated_character_fixture"
    return canonical, None


# ── Core discovery (pure; takes a DB connection + adapter) ────────────────────
def _upsert_wallet(db, canonical: str) -> tuple[str, bool]:
    """Idempotently create a canonical NON-sample wallet row.

    Returns (wallet_id, created). Mirrors the canonical insert used by
    collect_smart_money_data (canonical_address + is_sample=0) so discovery
    and the legacy collector share one identity invariant.
    """
    existing = db.fetchone(
        "SELECT id FROM wallets WHERE canonical_address = ?", (canonical,)
    )
    if existing is not None:
        return dict(existing)["id"], False
    wallet_id = f"wa_{uuid.uuid4().hex}"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.conn.execute(
        """INSERT INTO wallets
           (id, address, canonical_address, label, is_sample, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(canonical_address) DO UPDATE SET
             label = excluded.label,
             is_sample = CASE WHEN excluded.is_sample = 0 AND is_sample = 1
                              THEN 0 ELSE is_sample END
           RETURNING id""",
        (wallet_id, canonical, canonical, "discovery", 0, now),
    )
    row = db.fetchone(
        "SELECT id FROM wallets WHERE canonical_address = ?", (canonical,)
    )
    return dict(row)["id"], True


def _add_watch_idempotent(db, wallet_id: str, source: str, reason: Optional[str]) -> tuple[str, bool]:
    """Idempotently add an active research watch. Returns (watch_id, created)."""
    from polycopy.ingestion.specialist_evidence_watchlist import (
        active_watch_for_wallet,
        _uuid,
        _now_iso,
    )
    existing = active_watch_for_wallet(db, wallet_id)
    if existing is not None:
        return existing, False
    wid = _uuid()
    db.conn.execute(
        "INSERT INTO specialist_evidence_watchlist("
        "id, wallet_id, status, source, reason, created_by, created_at,"
        "max_new_trades_per_run) VALUES (?,?, 'active', ?,?,?,?,?)",
        (wid, wallet_id, source, reason, "discovery", _now_iso(), 25),
    )
    return wid, True


def discover(
    db,
    raw_addresses: Sequence[str],
    *,
    adapter: Optional[DiscoveryAdapter] = None,
    add_watches: bool = False,
    bounds: Optional[dict[str, Any]] = None,
    live: bool = False,
    perform_writes: bool = False,
) -> DiscoveryResult:
    """Run bounded discovery over a de-duplicated, canonicalized address list.

    Writes ONLY wallets + specialist_evidence_watchlist, in a single
    caller-owned transaction. The caller (CLI) opens the writable connection
    inside the operational lock and commits/rolls back. This function performs
    the inserts and returns the result; it does NOT commit (the CLI owns the
    transaction boundary so a failure rolls back ALL new wallet/watch rows).

    When ``perform_writes`` is False (DRY-RUN), the function classifies,
    validates, canonicalizes, deduplicates, applies bounds, and records the
    would-be wallet/watch actions WITHOUT executing any INSERT — so a dry-run
    never touches the database.
    """
    bounds = bounds or _default_bounds()
    result = DiscoveryResult(mode="live" if live else "dry_run")
    result.bounds = dict(bounds)

    # canonicalize + dedupe + classify + bound wallet count
    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []  # (canonical, raw)
    for raw in raw_addresses:
        result.requested_addresses += 1
        canonical, reason = classify_address(raw)
        if canonical is None:
            result.rejected_addresses += 1
            result.rejected.append({"address": _redact(raw), "reason": reason})
            continue
        if canonical in seen:
            continue  # dedupe
        seen.add(canonical)
        ordered.append((canonical, raw))

    if len(ordered) > int(bounds["max_wallets"]):
        # Trim to bound; record the overflow as rejected by bound.
        overflow = ordered[int(bounds["max_wallets"]):]
        ordered = ordered[: int(bounds["max_wallets"])]
        for canonical, raw in overflow:
            result.rejected_addresses += 1
            result.rejected.append(
                {"address": _redact(raw), "reason": "exceeds_max_wallets_bound"})

    result.accepted_addresses = len(ordered)

    for canonical, raw in ordered:
        status = "complete"
        if live and adapter is not None:
            outcome = adapter.fetch_wallet_activity(canonical, bounds)
            status = outcome.status
            if status == "partial":
                result.partial_fetches += 1
            elif status == "failed":
                result.failed_fetches += 1
        # Partial or failed upstream fetches NEVER promote a candidate; we still
        # persist the canonical wallet row (it is a real attributable address),
        # but we record the fetch status and do NOT treat partial as eligible
        # for watches unless explicitly requested AND complete.
        wid = None
        created = False
        if perform_writes and db is not None:
            wid, created = _upsert_wallet(db, canonical)
        if created:
            result.wallets_created += 1
        elif perform_writes:
            result.wallets_existing += 1
        else:
            # dry-run: assume "would create" for a not-yet-seen canonical; the
            # caller can verify exact existing/new split only on a real read.
            result.wallets_created += 1

        watch_added = False
        if add_watches and perform_writes and db is not None:
            # Only add a research watch for a COMPLETE fetch; never promote a
            # candidate from a partial/failed fetch.
            if status == "complete":
                w_id, w_created = _add_watch_idempotent(
                    db, wid, "discovery",
                    "bounded research-wallet discovery (PR72)")
                if w_created:
                    result.watches_added += 1
                    watch_added = True
                else:
                    result.watches_existing += 1
                    watch_added = True
        result.addresses.append({
            "canonical_address": canonical,
            "fetch_status": status,
            "wallet_id": wid,
            "wallet_created": created,
            "watch_added": watch_added,
        })

    return result


def _redact(raw: str) -> str:
    if not isinstance(raw, str) or len(raw) < 10:
        return raw
    return raw[:6] + "…" + raw[-4:]
