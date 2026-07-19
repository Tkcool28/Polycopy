"""Bounded research-wallet discovery bridge (PR #72).

A SAFE FRONT DOOR into the already-connected PR #71 evidence/scoring pipeline.

Scope (strictly bounded):
  bounded public Polymarket market activity
    -> bounded complete trade fetches
    -> extract attributable real wallet addresses
    -> canonical wallet rows (wallets, is_sample=0)
    -> optional specialist research-watch rows (specialist_evidence_watchlist)

It MUST NOT:
  * label wallets smart;
  * persist full market history, source trades, snapshots;
  * write capability flags, experiment runs;
  * score wallets, approve wallets, dispatch trades;
  * create candidates or paper signals;
  * authorize or execute anything;
  * modify production outside the two allowed tables;
  * add timers or systemd units;
  * use leaderboard data;
  * use unrelated wallet statistics;
  * call collect_smart_money_data.run_collection;
  * call evaluate_wallet;

Write scope is exactly two tables:
  * wallets
  * specialist_evidence_watchlist

Safety gates (all required before ANY writable DB open):
  * --allow-live (authorize bounded public network reads)
  * --write
  * --confirm-production-db
  * the global Polycopy operational lock
  * complete bound set (market / trade-per-market / wallet-count / runtime / memory)

Discovery flow (two stages):
  Stage 1 (discover_candidates):
    1. fetch at most --market-limit active markets;
    2. for each market, fetch at most --trade-limit-per-market trades;
    3. distinguish complete, partial, and failed fetches;
    4. discard all data from partial or failed fetches;
    5. extract attributable trader/proxy-wallet addresses only from COMPLETE
       trade fetches;
    6. canonicalize addresses;
    7. reject anonymous, sentinel, malformed, all-zero, and repeated-character
       fixture addresses;
    8. deduplicate deterministically (tracking before-dedupe count);
    9. sort candidates deterministically;
    10. stop at --max-wallets.

  Stage 2 (persist_candidates):
    - New wallet: created_wallet_id set, existing_wallet_id null, action="created" or "created_wallet_and_watch"
    - Existing wallet: existing_wallet_id set, created_wallet_id null, action="existing_wallet"
    - Each candidate retains deterministic provenance from first sorted observation

Default behavior is DRY-RUN: no network, no DB write, structured JSON only.
A discovery never calls the broad legacy collect_smart_money_data.run_collection
path and never calls evaluate_wallet.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Sequence

from polycopy.db.wallet_identity import canonical_wallet_address

if TYPE_CHECKING:
    pass  # TYPE_CHECKING guard for runtime imports if needed


# ── Output contract ────────────────────────────────────────────────────────────
@dataclass
class DiscoveryResult:
    run_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    dry_run: bool = True
    market_limit: int = 0
    trade_limit_per_market: int = 0
    max_wallets: int = 0
    markets_requested: int = 0
    markets_completed: int = 0
    markets_partial: int = 0
    markets_failed: int = 0
    trades_examined: int = 0
    anonymous_rejected: int = 0
    malformed_rejected: int = 0
    fixture_rejected: int = 0
    duplicate_rejected: int = 0
    existing_wallets: int = 0
    would_create_wallets: int = 0
    new_wallets: int = 0
    watches_existing: int = 0
    would_create_watches: int = 0
    watches_created: int = 0
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "dry_run": self.dry_run,
            "market_limit": self.market_limit,
            "trade_limit_per_market": self.trade_limit_per_market,
            "max_wallets": self.max_wallets,
            "markets_requested": self.markets_requested,
            "markets_completed": self.markets_completed,
            "markets_partial": self.markets_partial,
            "markets_failed": self.markets_failed,
            "trades_examined": self.trades_examined,
            "anonymous_rejected": self.anonymous_rejected,
            "malformed_rejected": self.malformed_rejected,
            "fixture_rejected": self.fixture_rejected,
            "duplicate_rejected": self.duplicate_rejected,
            "existing_wallets": self.existing_wallets,
            "would_create_wallets": self.would_create_wallets,
            "new_wallets": self.new_wallets,
            "watches_existing": self.watches_existing,
            "would_create_watches": self.would_create_watches,
            "watches_created": self.watches_created,
            "candidates": self.candidates,
        }


# ── Bounds ───────────────────────────────────────────────────────────────────
def _default_bounds() -> dict[str, Any]:
    """Safe defaults per spec: market-limit <= 10, trade-limit-per-market <= 100, max-wallets <= 5."""
    return {
        "market_limit": 10,
        "trade_limit_per_market": 100,
        "max_wallets": 5,
    }


# ── Address validation (sentinel / anonymous / malformed / all-zero / repeated-char rejection) ────────────
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
    # all-zero and all-f sentinel
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
def _redact(raw: str) -> str:
    """Redact an address for rejection reporting (keep first 6, last 4 chars)."""
    if not isinstance(raw, str) or len(raw) < 10:
        return raw
    return raw[:6] + "…" + raw[-4:]


def _upsert_wallet(db, canonical: str, perform_writes: bool) -> tuple[Optional[str], bool]:
    """Idempotently create a canonical NON-sample wallet row.

    Returns (wallet_id, created). When perform_writes is False, returns (None, False)
    and does NOT execute any INSERT. Mirrors the canonical insert used by
    collect_smart_money_data (canonical_address + is_sample=0) so discovery
    and the legacy collector share one identity invariant.
    """
    existing = db.fetchone(
        "SELECT id FROM wallets WHERE canonical_address = ?", (canonical,))
    if existing is not None:
        return str(existing[0]), False

    if not perform_writes:
        return None, False

    wallet_id = f"wa_{uuid.uuid4().hex[:16]}"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.conn.execute(
        """INSERT INTO wallets
           (id, address, canonical_address, label, is_sample, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (wallet_id, canonical, canonical, "discovery", 0, now),
    )
    row = db.fetchone(
        "SELECT id FROM wallets WHERE canonical_address = ?", (canonical,))
    return str(row[0]), True


def _add_watch_idempotent(db, wallet_id: str, source: str, reason: Optional[str], perform_writes: bool) -> tuple[Optional[str], bool]:
    """Idempotently add an active research watch.

    When perform_writes is False, returns (None, False) and does NOT execute
    any INSERT. Returns (watch_id, created).
    """
    # Check for existing active watch
    existing = db.fetchone(
        """SELECT id FROM specialist_evidence_watchlist
           WHERE wallet_id = ? AND status = 'active'""",
        (wallet_id,),
    )
    if existing is not None:
        return str(existing[0]), False

    if not perform_writes:
        return None, False

    wid = f"sew_{uuid.uuid4().hex[:16]}"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.conn.execute(
        """INSERT INTO specialist_evidence_watchlist
           (id, wallet_id, status, source, reason, created_by, created_at, max_new_trades_per_run)
           VALUES (?, ?, 'active', ?, ?, ?, ?, ?)""",
        (wid, wallet_id, source, reason, "discovery", now, 25),
    )
    return wid, True


def _extract_addresses_from_candidates(
    candidates: Sequence[dict],
) -> list[tuple[str, str, Optional[str]]]:
    """Extract (canonical_address, market_id, trade_id) from discovery candidates.

    Only returns addresses that are Attributable Wallet Addresses (AWAs).
    None / sentinel / malformed addresses from trades are skipped.
    """
    result: list[tuple[str, str, Optional[str]]] = []
    for c in candidates:
        addr = c.get("canonical_address", "")
        market_id = c.get("source_market_id", "")
        trade_id = c.get("source_trade_id_or_hash")
        if addr and str(addr).strip():
            s_addr = str(addr).strip()
            if s_addr.startswith("0x") and len(s_addr) >= 42:
                result.append((s_addr.lower(), market_id, trade_id))
    return result


# ── Stage 1: Async discovery ──────────────────────────────────────────────────
async def discover_candidates(
    adapter,
    bounds: dict[str, Any],
) -> dict[str, Any]:
    """Async discovery: fetch bounded markets and trades, return candidate addresses.

    Returns discovery result with:
      - markets_requested, markets_completed, markets_partial, markets_failed
      - trades_examined
      - candidates: list of {canonical_address, source_market_id, source_trade_id_or_hash}

    Partial/failed fetch data is discarded.
    Does NOT open a DB or perform any writes.
    """
    market_limit = bounds.get("market_limit", 10)
    trade_limit = bounds.get("trade_limit_per_market", 100)

    discovery_result = {
        "markets_requested": 0,
        "markets_completed": 0,
        "markets_partial": 0,
        "markets_failed": 0,
        "trades_examined": 0,
        "candidates": [],  # Each: {canonical_address, source_market_id, source_trade_id_or_hash}
    }

    markets_fetched = 0

    for market in await adapter.list_active_markets(limit=market_limit):
        market_id = getattr(market, "source_id", None) or getattr(market, "condition_id", None)
        if market_id is None:
            continue

        discovery_result["markets_requested"] += 1

        fetch_result = await adapter.fetch_trades_for_market(
            market_source_id=str(market_id),
            limit=trade_limit,
            max_pages=1,
            max_rows=trade_limit,
        )

        if fetch_result.status == "complete":
            discovery_result["markets_completed"] += 1
            trades = list(fetch_result)
            discovery_result["trades_examined"] += len(trades)
            for trade in trades:
                addr = getattr(trade, "trader_address", None)
                if addr is not None and str(addr).strip():
                    trade_id = getattr(trade, "source_trade_id", None)
                    if not trade_id:
                        trade_id = getattr(trade, "transaction_hash", None)
                    discovery_result["candidates"].append({
                        "canonical_address": str(addr).lower().strip(),
                        "source_market_id": str(market_id),
                        "source_trade_id_or_hash": trade_id,
                    })
        elif fetch_result.status == "partial":
            discovery_result["markets_partial"] += 1
            # Discard all data from partial fetch
        else:
            discovery_result["markets_failed"] += 1
            # Discard all data from failed fetch

        markets_fetched += 1
        if markets_fetched >= market_limit:
            break

    return discovery_result


# ── Stage 2: Persistence ──────────────────────────────────────────────────────
def persist_candidates(
    db,
    discovery_result: dict[str, Any],
    add_to_watchlist: bool = False,
    bounds: Optional[dict[str, Any]] = None,
    perform_writes: bool = False,
) -> DiscoveryResult:
    """Persist discovery candidates into the database.

    Stage 2 after stage 1 (discover_candidates) completes.
    Performs deduplication tracking, wallet existence checks, and optional writes.
    """
    bounds = bounds or _default_bounds()
    result = DiscoveryResult()
    result.run_id = f"discovery_{uuid.uuid4().hex[:8]}"
    result.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result.dry_run = not perform_writes
    result.market_limit = int(bounds.get("market_limit", 10))
    result.trade_limit_per_market = int(bounds.get("trade_limit_per_market", 100))
    result.max_wallets = int(bounds.get("max_wallets", 5))

    # Copy discovery stats
    result.markets_requested = discovery_result.get("markets_requested", 0)
    result.markets_completed = discovery_result.get("markets_completed", 0)
    result.markets_partial = discovery_result.get("markets_partial", 0)
    result.markets_failed = discovery_result.get("markets_failed", 0)
    result.trades_examined = discovery_result.get("trades_examined", 0)

    candidates_raw = discovery_result.get("candidates", [])
    max_wallets = result.max_wallets

    # Track duplicates BEFORE deduplication
    seen_before_dedupe: set[str] = set()
    duplicate_count = 0

    # Build ordered list with provenance
    ordered_with_provenance: list[tuple[str, str, Optional[str]]] = []
    for c in candidates_raw:
        canonical = c.get("canonical_address", "")
        market_id = c.get("source_market_id", "")
        trade_id = c.get("source_trade_id_or_hash")

        can, reason = classify_address(canonical)
        if can is None:
            if reason == "sentinel_or_anonymous":
                result.anonymous_rejected += 1
            elif reason == "repeated_character_fixture":
                result.fixture_rejected += 1
            else:
                result.malformed_rejected += 1
            continue

        if can in seen_before_dedupe:
            duplicate_count += 1
            continue
        seen_before_dedupe.add(can)
        ordered_with_provenance.append((can, market_id, trade_id))

    result.duplicate_rejected = duplicate_count

    # Sort deterministically and bound
    ordered_with_provenance = sorted(ordered_with_provenance, key=lambda x: x[0])[:max_wallets]

    # Process each candidate
    for canonical, market_id, trade_id in ordered_with_provenance:
        # Check existing wallet state
        existing = db.fetchone(
            "SELECT id FROM wallets WHERE canonical_address = ?", (canonical,))

        if perform_writes:
            wid, created = _upsert_wallet(db, canonical, perform_writes=True)

            if created:
                result.new_wallets += 1
                if add_to_watchlist:
                    w_id, w_created = _add_watch_idempotent(
                        db, wid, "discovery", "bounded research-wallet discovery (PR72)", perform_writes=True)
                    if w_created:
                        result.watches_created += 1
                    else:
                        result.watches_existing += 1
            else:
                result.existing_wallets += 1
                wid = str(existing[0])
                if add_to_watchlist:
                    watch = db.fetchone(
                        """SELECT id FROM specialist_evidence_watchlist
                           WHERE wallet_id = ? AND status = 'active'""",
                        (wid,))
                    if watch:
                        result.watches_existing += 1
                    else:
                        w_id, w_created = _add_watch_idempotent(
                            db, wid, "discovery", "bounded research-wallet discovery (PR72)", perform_writes=True)
                        result.watches_created += 1

            # Record candidate with proper provenance
            candidate = {
                "address": _redact(canonical),
                "canonical_address": canonical,
                "source_market_id": market_id,
                "source_trade_id_or_hash": trade_id,
                "fetch_status": "complete",
                "created_wallet_id": wid if created else None,
                "existing_wallet_id": wid if not created else None,
                "existing_watch_id": None,
                "created_watch_id": None,
                "action": "created_wallet_and_watch" if created and add_to_watchlist else ("created_wallet" if created else "existing_wallet"),
                "reason": "created" if created else "wallet_already_exists",
            }

            # Add watch IDs if applicable
            if add_to_watchlist and wid:
                watch = db.fetchone(
                    """SELECT id FROM specialist_evidence_watchlist
                       WHERE wallet_id = ? AND status = 'active'""",
                    (wid,))
                if watch:
                    candidate["existing_watch_id"] = str(watch[0])
        else:
            # Dry-run: check existing state for reporting
            if existing:
                wid = str(existing[0])
                candidate = {
                    "address": _redact(canonical),
                    "canonical_address": canonical,
                    "source_market_id": market_id,
                    "source_trade_id_or_hash": trade_id,
                    "fetch_status": "complete",
                    "existing_wallet_id": wid,
                    "created_wallet_id": None,
                    "existing_watch_id": None,
                    "created_watch_id": None,
                    "action": "existing_wallet",
                    "reason": "wallet_already_exists",
                }
                result.existing_wallets += 1

                # Check watch state
                watch = db.fetchone(
                    """SELECT id FROM specialist_evidence_watchlist
                       WHERE wallet_id = ? AND status = 'active'""",
                    (wid,))
                if watch:
                    candidate["existing_watch_id"] = str(watch[0])
                    result.watches_existing += 1
            else:
                candidate = {
                    "address": _redact(canonical),
                    "canonical_address": canonical,
                    "source_market_id": market_id,
                    "source_trade_id_or_hash": trade_id,
                    "fetch_status": "complete",
                    "existing_wallet_id": None,
                    "created_wallet_id": None,
                    "existing_watch_id": None,
                    "created_watch_id": None,
                    "action": "would_create_wallet",
                    "reason": "valid_address_no_wallet",
                }
                result.would_create_wallets += 1
                if add_to_watchlist:
                    candidate["action"] = "would_create_wallet_and_watch"
                    result.would_create_watches += 1

        result.candidates.append(candidate)

    result.ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return result


# ── Legacy entry point (kept for test compatibility) ───────────────────────────
def discover(
    db,
    *args,
    **kwargs,
) -> DiscoveryResult:
    """Legacy wrapper maintained for backward compatibility.

    DEPRECATED: Use discover_candidates + persist_candidates instead.
    This function will be removed in a future version.
    """
    raise RuntimeError(
        "discover() is deprecated; use discover_candidates() + persist_candidates() instead"
    )