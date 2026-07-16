"""Settlement — idempotent market resolution settlement with resolution evidence.

This module provides:
- SettlementEvidence: proof of market resolution (source, outcome, timestamp)
- SettlementResult: outcome of settling a position
- SettlementEngine: idempotent settlement of positions using resolution evidence

Idempotency: settling the same position multiple times with the same evidence
always produces the same result. Re-settlement with conflicting evidence is
flagged as an error.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass
class SettlementEvidence:
    """Proof that a market resolved to a specific outcome.

    Attributes:
        source: where the resolution data came from (e.g. "polymarket_gamma")
        market_source_id: the market's source-specific ID
        resolution_outcome: the winning outcome label (e.g. "Yes")
        evidence_hash: deterministic hash of the evidence for dedup
        raw_evidence: raw data that supports the resolution claim
        observed_at: when we observed this resolution (UTC)
    """

    source: str
    market_source_id: str
    resolution_outcome: str
    raw_evidence: dict = field(default_factory=dict)
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    evidence_hash: str = ""

    def __post_init__(self) -> None:
        if not self.evidence_hash:
            payload = json.dumps({
                "source": self.source,
                "market_source_id": self.market_source_id,
                "resolution_outcome": self.resolution_outcome,
                "raw_evidence": self.raw_evidence,
            }, sort_keys=True)
            self.evidence_hash = hashlib.sha256(payload.encode()).hexdigest()[:32]


@dataclass
class SettlementResult:
    """Result of settling a position.

    Attributes:
        position_id: the position being settled
        market_id: the market
        wallet_id: the wallet that held the position
        outcome: the position's outcome
        resolution_outcome: the market's winning outcome
        is_winner: whether the position won
        payout: payout amount (winners get their share value, losers get 0)
        evidence_hash: hash of the evidence used (for audit)
        settled_at: when settlement was performed (UTC)
        is_sample: True if settled from sample/fixture data
    """

    position_id: UUID
    market_id: UUID
    wallet_id: UUID
    outcome: str
    resolution_outcome: str
    is_winner: bool
    payout: float
    evidence_hash: str
    settled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_sample: bool = False

    @property
    def evidence_key(self) -> str:
        """Deterministic key for idempotency: position + evidence hash."""
        return f"{self.position_id}:{self.evidence_hash}"


class SettlementEngine:
    """Idempotent settlement of positions using resolution evidence.

    Guarantees:
    - Same position + same evidence → same result (idempotent)
    - Same position + conflicting evidence → error (never silently overwrite)
    - All settlements are logged with evidence hash for audit
    """

    def __init__(self) -> None:
        # evidence_key → SettlementResult for dedup/idempotency
        self._settled: dict[str, SettlementResult] = {}

    def settle_position(
        self,
        position_id: UUID,
        market_id: UUID,
        wallet_id: UUID,
        outcome: str,
        quantity: float,
        avg_entry_price: float,
        evidence: SettlementEvidence,
        is_sample: bool = False,
    ) -> SettlementResult:
        """Settle a position using resolution evidence.

        If this position was already settled with the SAME evidence,
        returns the cached result (idempotent).

        If this position was settled with DIFFERENT evidence,
        raises ValueError (conflicting resolution — operator must resolve).

        Args:
            position_id: the position to settle
            market_id: the market
            wallet_id: the wallet holding the position
            outcome: the position's outcome (e.g. "Yes")
            quantity: shares held
            avg_entry_price: average entry price (for payout calculation)
            evidence: resolution evidence
            is_sample: True for sample/fixture data

        Returns:
            SettlementResult with payout and evidence
        """
        # Resolution outcome comparison is case-insensitive: positions store
        # "Yes"/"No" while resolution evidence commonly uses "YES"/"NO".
        outcome_norm = (outcome or "").strip().upper()
        resolution_norm = (evidence.resolution_outcome or "").strip().upper()
        is_win = outcome_norm == resolution_norm and outcome_norm in ("YES", "NO")
        result = SettlementResult(
            position_id=position_id,
            market_id=market_id,
            wallet_id=wallet_id,
            outcome=outcome,
            resolution_outcome=evidence.resolution_outcome,
            is_winner=is_win,
            payout=quantity if is_win else 0.0,
            evidence_hash=evidence.evidence_hash,
            is_sample=is_sample,
        )

        key = result.evidence_key

        if key in self._settled:
            existing = self._settled[key]
            if existing.evidence_hash == result.evidence_hash:
                logger.info(
                    "Settlement idempotent: position %s already settled with same evidence.",
                    str(position_id)[:8],
                )
                return existing
            else:
                # Same position, different evidence — conflict!
                raise ValueError(
                    f"Settlement conflict for position {position_id}: "
                    f"existing evidence hash {existing.evidence_hash} != "
                    f"new evidence hash {result.evidence_hash}. "
                    f"Operator must resolve before re-settling."
                )

        self._settled[key] = result
        logger.info(
            "Position %s settled: outcome=%s resolution=%s winner=%s payout=%.4f",
            str(position_id)[:8],
            outcome,
            evidence.resolution_outcome,
            result.is_winner,
            result.payout,
        )
        return result

    def get_settlement(self, position_id: UUID, evidence_hash: str) -> Optional[SettlementResult]:
        """Look up a previous settlement by position + evidence hash."""
        key = f"{position_id}:{evidence_hash}"
        return self._settled.get(key)

    def list_settlements(self) -> list[SettlementResult]:
        """Return all settlement results."""
        return list(self._settled.values())

    @property
    def settlement_count(self) -> int:
        return len(self._settled)
