"""Wallet discovery, dedup, related-wallet detection, and trade detection.

This module implements:
- Multi-source wallet discovery with deduplication
- Conservative possible-related-wallet detection (no false positives)
- Tracked-wallet trade detection with duplicate signal prevention
- Late/staleness handling for delayed data feeds
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from polycopy.db.wallet_identity import canonical_wallet_address
from polycopy.discovery.models import (
    DedupRecord,
    RelatedWalletCandidate,
    TrackedTrade,
    WalletSource,
)
from polycopy.discovery.source_trade_side import (
    normalize_source_trade_side_for_persistence,
)

logger = logging.getLogger(__name__)

# Import-safety note: ``polycopy.db.wallet_identity`` is pure Python — it
# depends only on ``polycopy.domain.source_trade`` (which imports nothing
# from discovery). Verified non-circular at PR #3 round 7 stabilization.
# If a future refactor introduces a reverse import, move
# ``canonical_wallet_address`` to ``polycopy.utils.identity`` and re-export
# from ``wallet_identity`` for backwards compatibility.

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_STALENESS_SECONDS = 120.0  # 2 minutes: trades older than this are "stale"
DEDUP_WINDOW_SECONDS = 60.0  # trades within this window are compared for dedup
MIN_RELATED_SIGNALS = 2  # minimum signals to flag a wallet as possibly related
RELATED_CONFIDENCE_THRESHOLD = 0.4  # minimum confidence to consider plausible


def make_dedup_key(
    source: str,
    trader_address: str,
    market_source_id: str,
    side: str,
    outcome: str,
    timestamp: datetime,
    granularity_seconds: int = 60,
) -> str:
    """Create a deterministic dedup key for a trade.

    Timestamp is truncated to `granularity_seconds` to handle slight
    clock differences between sources reporting the same trade.
    """
    ts_bucket = (int(timestamp.timestamp()) // granularity_seconds) * granularity_seconds
    raw = f"{source}:{trader_address.lower()}:{market_source_id}:{side}:{outcome}:{ts_bucket}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class WalletDiscovery:
    """Multi-source wallet discovery with deduplication.

    Wallets can be discovered from:
    - Polymarket (public API / trade feed)
    - Bullpen (when available)
    - Manual watchlist (user-provided addresses)
    - Related-wallet detection (heuristic clustering)

    Dedup logic:
    - Same address from different sources → merged into single wallet record
    - Manual watchlist entries always take precedence over auto-discovered
    """

    def __init__(self) -> None:
        self._wallets_by_address: dict[str, dict[str, Any]] = {}
        self._sources_by_address: dict[str, set[WalletSource]] = defaultdict(set)

    def add_from_polymarket(self, address: str, label: str = "") -> dict[str, Any]:
        """Register a wallet discovered via Polymarket."""
        return self._register(address, WalletSource.POLYMARKET, label)

    def add_from_bullpen(self, address: str, label: str = "") -> dict[str, Any]:
        """Register a wallet discovered via Bullpen."""
        return self._register(address, WalletSource.BULLPEN, label)

    def add_to_watchlist(self, address: str, label: str = "manual-watch") -> dict[str, Any]:
        """Register a wallet from manual watchlist (highest priority)."""
        return self._register(address, WalletSource.MANUAL_WATCHLIST, label)

    def add_from_related_detection(self, address: str, label: str = "") -> dict[str, Any]:
        """Register a wallet discovered via related-wallet detection."""
        return self._register(address, WalletSource.RELATED_DETECTION, label)

    def _canonical_key(self, address: Any) -> str | None:
        """Return the canonical key for ``address`` or ``None`` if invalid.

        Delegates to :func:`polycopy.db.wallet_identity.canonical_wallet_address`
        so the in-memory discovery registry and the DB layer agree byte-for-byte
        on identity (lowercase + strip ALL ASCII whitespace + sentinel rejection).
        Returns ``None`` for ``None`` / empty / whitespace-only / sentinel inputs
        so the caller can distinguish "invalid" from "real new wallet".
        """
        return canonical_wallet_address(address)

    def _register(self, address: str, source: WalletSource, label: str) -> dict[str, Any]:
        """Internal: register a wallet address from a given source.

        Round-9 canonicalization: uses
        :func:`canonical_wallet_address` (the shared
        single-source-of-truth helper) for the internal key, so the
        in-memory registry and ``wallets`` table agree on identity.
        Mixed-case ``"0xAbCd..."`` and padded ``"  0xabcd...  "`` collapse
        onto the same entry.

        Returns the registered entry dict with an ``"is_new"`` boolean key
        added: ``True`` iff this call added a new canonical address (not
        previously known to the registry), ``False`` otherwise. Existing
        tests that read other keys (e.g. ``entry["address"]``) continue to
        work; the new key is purely additive so callers that do strict
        equality ``entry == {...}`` would need to be updated.
        """
        canonical = self._canonical_key(address)
        if canonical is None:
            # Empty / whitespace / sentinel — reject explicitly. We don't
            # raise (legacy behavior of callers that pass through raw
            # strings); we return a sentinel "invalid" entry with
            # ``is_new=False`` so downstream code that reads the dict sees
            # a stable shape and knows it was rejected.
            return {
                "address": None,
                "label": label if label else "auto-discovered",
                "first_seen": datetime.now(timezone.utc).isoformat(),
                "source_count": 0,
                "sources": [],
                "is_new": False,
                "invalid": True,
            }

        is_new = canonical not in self._wallets_by_address
        if is_new:
            self._wallets_by_address[canonical] = {
                "address": canonical,
                "label": label if label else "auto-discovered",
                "first_seen": datetime.now(timezone.utc).isoformat(),
                "source_count": 0,
            }

        entry = self._wallets_by_address[canonical]
        self._sources_by_address[canonical].add(source)
        entry["source_count"] = len(self._sources_by_address[canonical])
        entry["sources"] = sorted(s.value for s in self._sources_by_address[canonical])

        # Manual watchlist always wins for label
        if source == WalletSource.MANUAL_WATCHLIST and label:
            entry["label"] = label

        # Additive key — existing readers (tests, callers) keep working.
        entry["is_new"] = is_new

        logger.debug(
            "Wallet %s registered from %s (total sources: %d, is_new=%s)",
            canonical[:12] + "...",
            source.value,
            entry["source_count"],
            is_new,
        )
        return entry

    def list_wallets(self) -> list[dict[str, Any]]:
        """Return all discovered wallets with their sources."""
        return list(self._wallets_by_address.values())

    def is_known_wallet(self, address: str) -> bool:
        """Return True if ``address`` (canonical form) is already registered.

        Uses the same canonicalization as :meth:`_register` (via
        :func:`canonical_wallet_address`) so a caller can ask "did I just
        add this wallet" without re-implementing the canonicalization.
        This is the source of truth for in-memory deduplication that
        backs ``wallets.address`` non-uniqueness. Returns ``False`` for
        empty / sentinel inputs (since they are not registered).
        """
        canonical = self._canonical_key(address)
        if canonical is None:
            return False
        return canonical in self._wallets_by_address

    def get_sources(self, address: str) -> set[WalletSource]:
        """Return the set of sources for a given wallet address.

        Uses :func:`canonical_wallet_address` for the lookup key so mixed
        case / padded variants resolve to the same set of sources.
        Returns an empty set for empty / sentinel / unknown inputs.
        """
        canonical = self._canonical_key(address)
        if canonical is None:
            return set()
        return self._sources_by_address.get(canonical, set())


class RelatedWalletDetector:
    """Conservative possible-related-wallet detector.

    Heuristics (all are weak individually; multiple must align):
    - shared_market: both wallets trade in the same market
    - close_timing: trades within 30 seconds of each other
    - similar_volume: within 20% quantity on the same outcome
    - same_fee_taker: both wallets share the same maker/fee tier (if known)

    Confidence calculation:
    - Each aligned signal adds ~0.25 confidence
    - Max confidence capped at 0.75 (never treated as confirmed)
    - Only >= MIN_RELATED_SIGNALS signals AND confidence >= threshold → plausible

    This is intentionally conservative: false positives are worse than
    false negatives for copy-trading, since false positives could lead
    to merging wallets that are actually independent.
    """

    SIGNAL_WEIGHT = 0.25
    MAX_CONFIDENCE = 0.75

    def evaluate(
        self,
        primary_address: str,
        candidate_address: str,
        signals: list[str],
    ) -> RelatedWalletCandidate:
        """Evaluate whether a candidate wallet is possibly related to a primary.

        Returns a RelatedWalletCandidate with confidence in [0, 0.75].
        Only marks as plausibly related if >= 2 signals AND confidence >= 0.4.
        """
        raw_confidence = min(len(signals) * self.SIGNAL_WEIGHT, self.MAX_CONFIDENCE)
        strong_signals = {"shared_market", "similar_volume", "shared_deposit"}
        strong_count = sum(1 for s in signals if s in strong_signals)
        if strong_count == 0 and len(signals) >= 2:
            # Only weak signals: reduce confidence
            raw_confidence = min(raw_confidence, 0.45)

        return RelatedWalletCandidate(
            primary_wallet_id=uuid4(),  # placeholder; real usage should pass actual UUID
            candidate_address=candidate_address.lower().strip(),
            confidence=round(raw_confidence, 3),
            signals=signals,
            source=WalletSource.RELATED_DETECTION,
            detected_at=datetime.now(timezone.utc),
        )

    def batch_evaluate(
        self,
        primary_address: str,
        candidates: list[tuple[str, list[str]]],
    ) -> list[RelatedWalletCandidate]:
        """Evaluate multiple candidates, returning only plausible ones."""
        results = []
        for addr, signals in candidates:
            if len(signals) < MIN_RELATED_SIGNALS:
                continue
            candidate = self.evaluate(primary_address, addr, signals)
            if candidate.is_plausibly_related:
                results.append(candidate)
        return results


class TradeDetector:
    """Detect and track trades from watched wallets with dedup and staleness handling.

    Key behaviors:
    - Dedup: trades matching a recent key (same address+market+side+outcome+time-bucket)
      are flagged as duplicates and not re-processed.
    - Staleness: trades older than staleness_threshold are marked `is_stale`
      and may be handled differently by downstream scoring.
    - Duplicate signal prevention: once a trade passes dedup, subsequent identical
      trades in the dedup window are silently dropped.
    """

    def __init__(
        self,
        staleness_seconds: float = DEFAULT_STALENESS_SECONDS,
        dedup_window_seconds: float = DEDUP_WINDOW_SECONDS,
        dedup_granularity_seconds: int = 60,
    ) -> None:
        self.staleness_seconds = staleness_seconds
        self.dedup_window_seconds = dedup_window_seconds
        self.dedup_granularity_seconds = dedup_granularity_seconds
        # dedup_key → (existing_key, timestamp) for dedup window tracking
        self._dedup_index: dict[str, tuple[str, datetime]] = {}
        self._dedup_log: list[DedupRecord] = []

    def process_trade(
        self,
        source: str,
        source_trade_id: str,
        wallet_address: str,
        market_source_id: str,
        side: str,
        outcome: str,
        quantity: float,
        price: float,
        timestamp: datetime,
        now: Optional[datetime] = None,
        is_sample: bool = False,
    ) -> Optional[TrackedTrade]:
        """Process an incoming trade through dedup and staleness checks.

        Returns a ``TrackedTrade`` with ``is_duplicate`` and ``is_stale`` flags
        set appropriately, OR ``None`` when the trade is skipped before
        persistence (e.g. invalid/missing side — PR24T controlled per-trade
        skip). A skipped trade is never persisted and its error is logged;
        it does not abort sibling trades in the collection batch.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        # Build dedup key
        dedup_key = make_dedup_key(
            source=source,
            trader_address=wallet_address,
            market_source_id=market_source_id,
            side=side,
            outcome=outcome,
            timestamp=timestamp,
            granularity_seconds=self.dedup_granularity_seconds,
        )

        # Check dedup window
        is_duplicate = False
        dedup_reason = "passed (no duplicate in window)"

        self._expire_dedup_window(now)

        if dedup_key in self._dedup_index:
            existing_key, _ = self._dedup_index[dedup_key]
            is_duplicate = True
            dedup_reason = f"duplicate of {existing_key[:16]}"

        record = DedupRecord(
            incoming_trade_id=source_trade_id,
            existing_trade_id=self._dedup_index.get(dedup_key, (source_trade_id, now))[0],
            dedup_key=dedup_key,
            is_duplicate=is_duplicate,
            reason=dedup_reason,
            checked_at=now,
        )
        self._dedup_log.append(record)

        # Register in dedup index (even duplicates, to catch re-submissions)
        if not is_duplicate:
            self._dedup_index[dedup_key] = (source_trade_id, now)

        # Calculate staleness
        age_seconds = (now - timestamp).total_seconds()
        is_stale = age_seconds > self.staleness_seconds
        staleness_seconds = max(age_seconds - self.staleness_seconds, 0.0)

        # PR24T: normalize side at the persistence boundary so future
        # source_trades.side rows are canonical (BUY/SELL uppercase).
        # Invalid/missing side must never be persisted. Instead of letting the
        # ValueError abort the whole collection batch, skip this one trade
        # cleanly (log context, return None, persist nothing). This keeps the
        # persistence guard strict while not crashing sibling trades.
        try:
            side_for_persistence = normalize_source_trade_side_for_persistence(side)
        except ValueError as exc:
            logger.warning(
                "Skipping source trade with invalid side before persistence: "
                "source_trade_id=%s wallet_address=%s market_source_id=%s "
                "side=%r error=%s",
                source_trade_id,
                wallet_address,
                market_source_id,
                side,
                exc,
            )
            return None

        trade = TrackedTrade(
            source_trade_id=source_trade_id,
            source=source,
            wallet_address=wallet_address.lower().strip(),
            market_source_id=market_source_id,
            side=side_for_persistence,
            outcome=outcome,
            quantity=quantity,
            price=price,
            timestamp=timestamp,
            received_at=now,
            is_duplicate=is_duplicate,
            is_stale=is_stale,
            staleness_seconds=round(staleness_seconds, 2),
            is_sample=is_sample,
        )

        if is_duplicate:
            logger.debug(
                "Duplicate trade dropped: %s from %s",
                source_trade_id[:16],
                wallet_address[:12],
            )
        elif is_stale:
            logger.warning(
                "Stale trade received: %s age=%.1fs (threshold=%.1fs)",
                source_trade_id[:16],
                age_seconds,
                self.staleness_seconds,
            )

        return trade

    def _expire_dedup_window(self, now: datetime) -> None:
        """Remove expired entries from the dedup index."""
        cutoff = now.timestamp() - self.dedup_window_seconds
        expired = [
            k for k, (_, ts) in self._dedup_index.items()
            if ts.timestamp() < cutoff
        ]
        for k in expired:
            del self._dedup_index[k]

    def get_dedup_log(self) -> list[DedupRecord]:
        """Return the dedup audit log."""
        return list(self._dedup_log)

    @property
    def dedup_window_size(self) -> int:
        """Current number of entries in the dedup window."""
        return len(self._dedup_index)
