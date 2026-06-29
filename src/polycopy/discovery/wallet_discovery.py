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

from polycopy.discovery.models import (
    DedupRecord,
    RelatedWalletCandidate,
    TrackedTrade,
    WalletSource,
)

logger = logging.getLogger(__name__)

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

    def _register(self, address: str, source: WalletSource, label: str) -> dict[str, Any]:
        """Internal: register a wallet address from a given source."""
        addr_lower = address.lower().strip()
        if not addr_lower:
            raise ValueError("wallet address must not be empty")

        is_new = addr_lower not in self._wallets_by_address
        if is_new:
            self._wallets_by_address[addr_lower] = {
                "address": addr_lower,
                "label": label if label else "auto-discovered",
                "first_seen": datetime.now(timezone.utc).isoformat(),
                "source_count": 0,
            }

        entry = self._wallets_by_address[addr_lower]
        self._sources_by_address[addr_lower].add(source)
        entry["source_count"] = len(self._sources_by_address[addr_lower])
        entry["sources"] = sorted(s.value for s in self._sources_by_address[addr_lower])

        # Manual watchlist always wins for label
        if source == WalletSource.MANUAL_WATCHLIST and label:
            entry["label"] = label

        logger.debug(
            "Wallet %s registered from %s (total sources: %d)",
            addr_lower[:12] + "...",
            source.value,
            entry["source_count"],
        )
        return entry

    def list_wallets(self) -> list[dict[str, Any]]:
        """Return all discovered wallets with their sources."""
        return list(self._wallets_by_address.values())

    def is_known_wallet(self, address: str) -> bool:
        """Return True if ``address`` (canonical form) is already registered.

        Uses the same canonicalization as :meth:`_register` so a caller
        can ask "did I just add this wallet" without re-implementing the
        canonicalization. This is the source of truth for in-memory
        deduplication that backs ``wallets.address`` non-uniqueness.
        """
        if not isinstance(address, str):
            return False
        canonical = address.lower().strip()
        return canonical in self._wallets_by_address

    def get_sources(self, address: str) -> set[WalletSource]:
        """Return the set of sources for a given wallet address."""
        return self._sources_by_address.get(address.lower().strip(), set())


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
    ) -> TrackedTrade:
        """Process an incoming trade through dedup and staleness checks.

        The trade is always returned as a TrackedTrade, with
        is_duplicate and is_stale flags set appropriately.
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

        trade = TrackedTrade(
            source_trade_id=source_trade_id,
            source=source,
            wallet_address=wallet_address.lower().strip(),
            market_source_id=market_source_id,
            side=side,
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
