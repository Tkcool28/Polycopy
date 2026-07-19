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

Discovery flow:
  1. fetch at most --market-limit active markets;
  2. for each market, fetch at most --trade-limit-per-market trades;
  3. distinguish complete, partial, and failed fetches;
  4. discard all data from partial or failed fetches;
  5. extract attributable trader/proxy-wallet addresses only from COMPLETE
     trade fetches;
  6. canonicalize addresses;
  7. reject anonymous, sentinel, malformed, all-zero, and repeated-character
     fixture addresses;
  8. deduplicate deterministically;
  9. sort candidates deterministically;
  10. stop at --max-wallets.

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
        return str(existing[0]), False
    wallet_id = f"wa_{uuid.uuid4().hex[:16]}"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.conn.execute(
        """INSERT INTO wallets
           (id, address, canonical_address, label, is_sample, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (wallet_id, canonical, canonical, "discovery", 0, now),
    )
    row = db.fetchone(
        "SELECT id FROM wallets WHERE canonical_address = ?", (canonical,)
    )
    return str(row[0]), True


def _add_watch_idempotent(db, wallet_id: str, source: str, reason: Optional[str]) -> tuple[str, bool]:
    """Idempotently add an active research watch. Returns (watch_id, created)."""
    # Check for existing active watch
    existing = db.fetchone(
        """SELECT id FROM specialist_evidence_watchlist
           WHERE wallet_id = ? AND status = 'active'""",
        (wallet_id,),
    )
    if existing is not None:
        return str(existing[0]), False
    wid = f"sew_{uuid.uuid4().hex[:16]}"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.conn.execute(
        """INSERT INTO specialist_evidence_watchlist
           (id, wallet_id, status, source, reason, created_by, created_at, max_new_trades_per_run)
           VALUES (?, ?, 'active', ?, ?, ?, ?, ?)""",
        (wid, wallet_id, source, reason, "discovery", now, 25),
    )
    return wid, True


def _extract_addresses_from_trades(trades: Sequence[Any]) -> list[str]:
    """Extract unique proxyWallet addresses from trades.

    Only returns addresses that are Attributable Wallet Addresses (AWAs).
    None / sentinel / malformed addresses from trades are skipped.
    """
    addresses: set[str] = set()
    for trade in trades:
        # SourceTrade has trader_address attribute
        addr = getattr(trade, "trader_address", None)
        if addr is not None and str(addr).strip():
            # Basic 0x check - must look like an Ethereum address (0x + 40+ hex)
            s_addr = str(addr).strip()
            if s_addr.startswith("0x") and len(s_addr) >= 42:
                addresses.add(s_addr.lower())
    return list(addresses)


def discover(
    db,
    raw_addresses: Sequence[str] = (),
    *,
    adapter: Optional[Any] = None,  # PolymarketPublicAdapter or fake
    add_watches: bool = False,
    bounds: Optional[dict[str, Any]] = None,
    live: bool = False,
    perform_writes: bool = False,
) -> DiscoveryResult:
    """Run bounded discovery over public markets and their trades.

    The production flow:
      1. fetch at most --market-limit active markets;
      2. for each market, fetch at most --trade-limit-per-market trades;
      3. distinguish complete, partial, and failed fetches;
      4. discard all data from partial or failed fetches;
      5. extract attributable trader/proxy-wallet addresses only from COMPLETE
         trade fetches;
      6. canonicalize addresses;
      7. reject anonymous, sentinel, malformed, all-zero, and repeated-character
         fixture addresses;
      8. deduplicate deterministically;
      9. sort candidates deterministically;
      10. stop at --max-wallets.

    Writes ONLY wallets + specialist_evidence_watchlist, in a single
    caller-owned transaction. The caller (CLI) opens the writable connection
    inside the operational lock and commits/rolls back. This function performs
    the inserts and returns the result; it does NOT commit (the CLI owns the
    transaction boundary so a failure rolls back ALL new wallet/watch rows).

    When ``perform_writes`` is False (DRY-RUN), the function performs
    bounded public reads (if live) and records would-be actions WITHOUT
    executing any INSERT — so a dry-run never touches the database.
    """
    bounds = bounds or _default_bounds()
    result = DiscoveryResult()
    result.run_id = f"discovery_{uuid.uuid4().hex[:8]}"
    result.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result.dry_run = not perform_writes
    result.market_limit = int(bounds.get("market_limit", 10))
    result.trade_limit_per_market = int(bounds.get("trade_limit_per_market", 100))
    result.max_wallets = int(bounds.get("max_wallets", 5))

    market_limit = result.market_limit
    trade_limit = result.trade_limit_per_market
    max_wallets = result.max_wallets

    # STEP 1-2: Fetch bounded public markets (live only)
    complete_market_addresses: dict[str, Any] = {}  # market_id -> trades list
    if live and adapter is not None:
        markets_fetched = 0
        markets = adapter.list_active_markets(limit=market_limit)

        for market in markets:
            market_id = getattr(market, "source_id", None) or getattr(market, "condition_id", None)
            if market_id is None:
                continue

            result.markets_requested += 1

            # Fetch trades for this market using the bounded per-market fetch
            fetch_result = adapter.fetch_trades_for_market(
                market_source_id=str(market_id),
                limit=trade_limit,
                max_pages=1,
                max_rows=trade_limit,
            )

            if fetch_result.status == "complete":
                result.markets_completed += 1
                trades = list(fetch_result)
                result.trades_examined += len(trades)
                complete_market_addresses[str(market_id)] = trades
            elif fetch_result.status == "partial":
                result.markets_partial += 1
                # Discard all data from partial fetch - do NOT promote
            else:  # failed
                result.markets_failed += 1
                # Discard all data from failed fetch - do NOT promote

            markets_fetched += 1
            if markets_fetched >= market_limit:
                break

    # STEP 3-5: Extract addresses from COMPLETE fetches only
    discovered_canonical: set[str] = set()

    # Extract from complete market fetches
    for market_id, trades in complete_market_addresses.items():
        for addr in _extract_addresses_from_trades(trades):
            discovered_canonical.add(addr)

    # Include operator-seeded addresses (for test injection support)
    for raw in raw_addresses:
        canonical, _ = classify_address(raw)
        if canonical is not None:
            discovered_canonical.add(canonical)

    # STEP 6-9: Normalize, reject, deduplicate, and bound
    ordered_addresses: list[str] = []
    for addr in discovered_canonical:
        canonical, reason = classify_address(addr)
        if canonical is None:
            if reason == "sentinel_or_anonymous":
                result.anonymous_rejected += 1
            elif reason == "repeated_character_fixture":
                result.fixture_rejected += 1
            else:
                result.malformed_rejected += 1
            continue

        if canonical in ordered_addresses:
            result.duplicate_rejected += 1
            continue

        ordered_addresses.append(canonical)

    # Sort for deterministic ordering
    ordered_addresses = sorted(ordered_addresses)[:max_wallets]

    # STEP 10: Process each candidate
    for canonical in ordered_addresses:
        candidate = {
            "address": _redact(canonical),
            "canonical_address": canonical,
            "source_market_id": None,
            "source_trade_id_or_hash": None,
            "fetch_status": "complete",
            "existing_wallet_id": None,
            "created_wallet_id": None,
            "existing_watch_id": None,
            "created_watch_id": None,
            "action": "reject",
            "reason": "unknown",
        }

        # Find source market/trade info for this address
        for market_id, trades in complete_market_addresses.items():
            for trade in trades:
                t_addr = getattr(trade, "trader_address", None)
                if t_addr is not None and str(t_addr).lower() == canonical:
                    candidate["source_market_id"] = market_id
                    # Get trade identifier
                    trade_id = getattr(trade, "source_trade_id", None)
                    tx_hash = getattr(trade, "transaction_hash", None)
                    if trade_id:
                        candidate["source_trade_id_or_hash"] = trade_id
                    elif tx_hash:
                        candidate["source_trade_id_or_hash"] = tx_hash
                    break
            if candidate["source_market_id"]:
                break

        if perform_writes:
            wid, created = _upsert_wallet(db, canonical)
            candidate["created_wallet_id"] = wid

            if created:
                candidate["action"] = "created_wallet"
                result.new_wallets += 1
                if add_watches:
                    w_id, w_created = _add_watch_idempotent(
                        db, wid, "discovery", "bounded research-wallet discovery (PR72)")
                    candidate["created_watch_id"] = w_id
                    if w_created:
                        candidate["action"] = "created_wallet_and_watch"
                        result.watches_created += 1
                    else:
                        candidate["existing_watch_id"] = w_id
                        candidate["action"] = "created_wallet_existing_watch"
                        result.watches_existing += 1
            else:
                candidate["action"] = "existing_wallet"
                result.existing_wallets += 1
                if add_watches:
                    watch = db.fetchone(
                        """SELECT id FROM specialist_evidence_watchlist
                           WHERE wallet_id = ? AND status = 'active'""",
                        (wid,))
                    if watch:
                        candidate["existing_watch_id"] = str(watch[0])
                        result.watches_existing += 1
                    else:
                        w_id, w_created = _add_watch_idempotent(
                            db, wid, "discovery", "bounded research-wallet discovery (PR72)")
                        candidate["created_watch_id"] = w_id
                        result.watches_created += 1
        else:
            # Dry-run: check existing state for reporting
            existing = db.fetchone(
                "SELECT id FROM wallets WHERE canonical_address = ?", (canonical,)
            )
            if existing:
                candidate["existing_wallet_id"] = str(existing[0])
                candidate["action"] = "existing_wallet"
                candidate["reason"] = "wallet_already_exists"
                result.existing_wallets += 1
            else:
                candidate["action"] = "would_create_wallet"
                candidate["reason"] = "valid_address_no_wallet"
                result.would_create_wallets += 1

            # Check existing watch state
            watch = db.fetchone(
                """SELECT id FROM specialist_evidence_watchlist
                   WHERE wallet_id = (SELECT id FROM wallets WHERE canonical_address = ?) AND status = 'active'""",
                (canonical,))
            if watch:
                candidate["existing_watch_id"] = str(watch[0])
                result.watches_existing += 1
            elif add_watches:
                candidate["action"] = "would_create_wallet_and_watch"
                result.would_create_watches += 1

        result.candidates.append(candidate)

    result.ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return result