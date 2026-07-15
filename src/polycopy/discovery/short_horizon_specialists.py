"""PR69 short-horizon specialist discovery engine.

Public surface used by the operator CLI. This engine is pure: it does no
HTTP, does not open any DB, and never invokes the production bridge.

The engine takes a fully-populated :class:`WalletHistoryReport` plus the
:class:`MarketUniverseAudit` and :class:`SeedReport` that fed it, rolls
each wallet/category pair up into :class:`WalletCategoryEvidence`, builds
the canonical scoring inputs via the SHARED builder functions (so
production and discovery feed identical inputs into the frozen scorers),
and returns the deterministic :class:`DiscoveryReport` that the CLI
serializes.

No formula weights, versions, or evidence gates are modified in this
module. Every score, gate failure, and missing-essential value is
produced by the upstream frozen :func:`compute_wallet_score_v1` /
:func:`compute_category_wallet_score_v1` paths.

Candidate statuses emitted by this engine (per SPEC):
  * ``READY_FOR_REVIEW`` — wallet score + category score both compute
    and clear all frozen minimums/gates; no material conflict.
  * ``COMPLETE_BUT_BELOW_THRESHOLD`` — both scores compute but the
    wallet score is below the COPY_CANDIDATE/WATCHLIST cut.
  * ``INSUFFICIENT_SETTLED_EVIDENCE`` — at least one score is INCOMPLETE.
  * ``TAXONOMY_INCOMPLETE`` — at least one row is missing a category.
  * ``LONG_HORIZON_HEAVY`` — discovery-time horizon rejects dominate.
  * ``SOURCE_INCOMPLETE`` — incomplete evidence dominates.
  * ``CONFLICT`` — taxonomy conflict recorded.
  * ``ERROR`` — unclassified error.

The engine NEVER auto-approves any wallet. READY_FOR_REVIEW is a
report-level signal; it does NOT mutate the production `wallets` table
or any candidate/approval/score-decision persistence layer.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from polycopy.discovery.market_universe import (
    MarketClassification,
    MarketUniverseAudit,
)
from polycopy.discovery.taxonomy_enricher import EnrichmentAudit
from polycopy.discovery.wallet_evidence import (
    WalletCategoryEvidence,
    build_category_score_input_v1,
    build_wallet_score_input_v1,
    evidence_from_history,
)
from polycopy.discovery.wallet_history import WalletHistoryRecord, WalletHistoryReport
from polycopy.discovery.wallet_seeds import SeedReport
from polycopy.scoring.category_wallet_score_v1 import (
    CategoryWalletScoreInputV1,
    compute_category_wallet_score_v1,
)
from polycopy.scoring.wallet_score_v1 import (
    CATEGORY_MIN_ACTIVE_DAYS,
    CATEGORY_MIN_DISTINCT_EVENTS,
    CATEGORY_MIN_RESOLVED_MARKETS,
    GLOBAL_MIN_ACTIVE_TRADING_DAYS,
    GLOBAL_MIN_DISTINCT_EVENTS,
    GLOBAL_MIN_RESOLVED_MARKETS,
    VERDICT_COPY_CANDIDATE_MIN,
    VERDICT_WATCHLIST_MIN,
    WalletScoreInputV1,
    WalletVerdict,
    compute_wallet_score_v1,
)

logger = logging.getLogger(__name__)

DISCOVERY_CONTRACT_VERSION = "pr69-short-horizon-discovery-v1"

STATUS_READY_FOR_REVIEW = "READY_FOR_REVIEW"
STATUS_COMPLETE_BUT_BELOW_THRESHOLD = "COMPLETE_BUT_BELOW_THRESHOLD"
STATUS_INSUFFICIENT_SETTLED_EVIDENCE = "INSUFFICIENT_SETTLED_EVIDENCE"
STATUS_TAXONOMY_INCOMPLETE = "TAXONOMY_INCOMPLETE"
STATUS_LONG_HORIZON_HEAVY = "LONG_HORIZON_HEAVY"
STATUS_SOURCE_INCOMPLETE = "SOURCE_INCOMPLETE"
STATUS_CONFLICT = "CONFLICT"
STATUS_ERROR = "ERROR"


@dataclass(frozen=True)
class WalletCandidateResult:
    """One wallet's scoring result + status + breakdown."""

    wallet_address: str
    sources: tuple[str, ...]
    overall_status: str
    overall_wallet_score: float
    overall_wallet_verdict: str
    overall_missing_essentials: tuple[str, ...]
    overall_gate_failures: tuple[str, ...]
    category_results: tuple[dict[str, Any], ...]
    qualifying_settled: int
    qualifying_trades: int
    preferred_trades: int
    settled_wins: int
    settled_losses: int
    early_exits: int
    unresolved_trades: int
    realized_qualifying_pnl: float | None
    active_trading_days: int
    distinct_events: int
    largest_market_pnl_share: float | None
    evidence_completeness: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryReport:
    """Final report shape consumed by the CLI / JSON serializer."""

    contract_version: str
    generated_at_utc: str
    requested: dict[str, Any]
    universe_audit: dict[str, Any]
    taxonomy_audit: dict[str, Any]
    seed_audit: dict[str, Any]
    history_audit: dict[str, Any]
    candidates: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]
    fallback: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _category_status(
    *,
    score_verdict: WalletVerdict,
    missing_essentials: Sequence[str],
    gate_failures: Sequence[str],
) -> str:
    if missing_essentials:
        return STATUS_INSUFFICIENT_SETTLED_EVIDENCE
    if any("category_resolved_markets" in g for g in gate_failures):
        return STATUS_INSUFFICIENT_SETTLED_EVIDENCE
    if score_verdict == WalletVerdict.INCOMPLETE:
        return STATUS_INSUFFICIENT_SETTLED_EVIDENCE
    if score_verdict == WalletVerdict.COPY_CANDIDATE or score_verdict == WalletVerdict.WATCHLIST:
        return STATUS_READY_FOR_REVIEW
    return STATUS_COMPLETE_BUT_BELOW_THRESHOLD


def _score_wallet(
    evidence: WalletCategoryEvidence,
    *,
    now: datetime,
) -> tuple[WalletScoreInputV1, float, WalletVerdict, tuple[str, ...], tuple[str, ...]]:
    inp = build_wallet_score_input_v1(evidence)
    result = compute_wallet_score_v1(input=inp, now=now)
    return inp, result.score, result.verdict, tuple(result.missing_essentials), tuple(result.eligibility_gate_failures)


def _score_category(
    evidence: WalletCategoryEvidence,
    *,
    now: datetime,
) -> tuple[CategoryWalletScoreInputV1, float, WalletVerdict, tuple[str, ...], tuple[str, ...]]:
    inp = build_category_score_input_v1(evidence)
    result = compute_category_wallet_score_v1(input=inp, now=now)
    return (
        inp,
        result.score,
        result.verdict,
        tuple(result.missing_essentials),
        tuple(result.category_gate_failures),
    )


def _evidence_fingerprint(evidence: WalletCategoryEvidence) -> str:
    payload = evidence.as_dict()
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def discover_short_horizon_specialists(
    *,
    classifications: Sequence[MarketClassification] = (),
    universe_audit: MarketUniverseAudit | None = None,
    taxonomy_audit: EnrichmentAudit | None = None,
    seed_report: SeedReport | None = None,
    history_report: WalletHistoryReport | None = None,
    history_records: Sequence[WalletHistoryRecord] = (),
    requested: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> DiscoveryReport:
    """Build a deterministic DiscoveryReport from reconciled evidence.

    The engine is pure. It accepts already-fetched evidence and rolls it
    up into scoring inputs, runs the frozen scorers, and assigns status.

    Args:
        classifications: market-universe classifications (incl. excluded).
        universe_audit: the market-universe audit (for serialization).
        taxonomy_audit: the taxonomy enricher audit (for serialization).
        seed_report: the seed-channel audit (for serialization).
        history_report: optional full history report; mutually compatible
            with ``history_records`` (records take precedence).
        history_records: pre-rolled history rows to use for scoring.
        requested: any operator config snapshot for the report header.
        now: optional clock for the frozen scorers (defaults to utcnow).

    Returns:
        Deterministic :class:`DiscoveryReport`.
    """
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    requested_dict = dict(requested or {})

    if history_records:
        records = tuple(history_records)
    elif history_report is not None:
        records = history_report.wallets
    else:
        records = ()

    candidates: list[WalletCandidateResult] = []
    errors: list[str] = []

    for record in records:
        try:
            evs = evidence_from_history(record)
        except Exception as exc:
            errors.append(f"evidence_failure:{record.wallet_address}:{type(exc).__name__}")
            continue

        # Wallet-wide row is the one with category_label='__all__'.
        all_evidence: WalletCategoryEvidence | None = next(
            (e for e in evs if e.category_label == "__all__"), None
        )
        if all_evidence is None:
            errors.append(f"missing_overall_evidence:{record.wallet_address}")
            continue

        try:
            wallet_inp, wallet_score, wallet_verdict, wallet_missing, wallet_gates = _score_wallet(
                all_evidence, now=clock
            )
        except Exception as exc:
            errors.append(f"wallet_score_error:{record.wallet_address}:{type(exc).__name__}")
            wallet_score, wallet_verdict, wallet_missing, wallet_gates = (
                0.0, WalletVerdict.INCOMPLETE, ("score_error",), (),
            )

        category_rows: list[dict[str, Any]] = []
        for ev in evs:
            if ev.category_label == "__all__":
                continue
            try:
                cat_inp, cat_score, cat_verdict, cat_missing, cat_gates = _score_category(ev, now=clock)
            except Exception as exc:
                errors.append(
                    f"category_score_error:{record.wallet_address}:{ev.category_label}:{type(exc).__name__}"
                )
                cat_score = 0.0
                cat_verdict = WalletVerdict.INCOMPLETE
                cat_missing = ("score_error",)
                cat_gates = ()
            category_rows.append({
                "category_label": ev.category_label,
                "score": cat_score,
                "verdict": cat_verdict.value,
                "status": _category_status(
                    score_verdict=cat_verdict,
                    missing_essentials=cat_missing,
                    gate_failures=cat_gates,
                ),
                "missing_essentials": list(cat_missing),
                "gate_failures": list(cat_gates),
                "input_fingerprint": _evidence_fingerprint(ev),
                "qualifying_trades": ev.qualifying_trades,
                "settled_trades": ev.settled_trades,
                "settled_wins": ev.settled_wins,
                "settled_losses": ev.settled_losses,
                "win_rate": ev.win_rate,
                "profit_factor": ev.profit_factor,
                "active_trading_days": ev.active_trading_days,
                "category_resolved_markets": ev.resolved_markets,
                "category_distinct_events": ev.distinct_events,
                "category_active_days": ev.active_trading_days,
                "realized_qualifying_pnl": ev.realized_qualifying_pnl,
            })

        has_long_horizon = all_evidence.long_horizon_excluded > 0
        has_taxonomy_excluded = all_evidence.taxonomy_excluded > 0
        has_source_incomplete = all_evidence.source_incomplete > 0
        has_conflict = bool(record.incomplete)
        has_error = False

        if has_error:
            status = STATUS_ERROR
        elif has_conflict:
            status = STATUS_CONFLICT
        elif has_source_incomplete and all_evidence.source_incomplete > all_evidence.settled_trades:
            status = STATUS_SOURCE_INCOMPLETE
        elif has_long_horizon and all_evidence.long_horizon_excluded > all_evidence.settled_trades:
            status = STATUS_LONG_HORIZON_HEAVY
        elif has_taxonomy_excluded and all_evidence.taxonomy_excluded > all_evidence.settled_trades:
            status = STATUS_TAXONOMY_INCOMPLETE
        elif wallet_verdict == WalletVerdict.INCOMPLETE:
            status = STATUS_INSUFFICIENT_SETTLED_EVIDENCE
        elif wallet_verdict in (WalletVerdict.COPY_CANDIDATE, WalletVerdict.WATCHLIST):
            status = STATUS_READY_FOR_REVIEW
        else:
            status = STATUS_COMPLETE_BUT_BELOW_THRESHOLD
        candidates.append(WalletCandidateResult(
            wallet_address=record.wallet_address,
            sources=(),
            overall_status=status,
            overall_wallet_score=wallet_score,
            overall_wallet_verdict=wallet_verdict.value,
            overall_missing_essentials=wallet_missing,
            overall_gate_failures=wallet_gates,
            category_results=tuple(category_rows),
            qualifying_settled=all_evidence.settled_trades,
            qualifying_trades=all_evidence.qualifying_trades,
            preferred_trades=all_evidence.preferred_trades,
            settled_wins=all_evidence.settled_wins,
            settled_losses=all_evidence.settled_losses,
            early_exits=all_evidence.early_exits,
            unresolved_trades=all_evidence.unresolved_trades,
            realized_qualifying_pnl=all_evidence.realized_qualifying_pnl,
            active_trading_days=all_evidence.active_trading_days,
            distinct_events=all_evidence.distinct_events,
            largest_market_pnl_share=all_evidence.largest_market_pnl_share,
            evidence_completeness=all_evidence.evidence_completeness,
        ))

    return DiscoveryReport(
        contract_version=DISCOVERY_CONTRACT_VERSION,
        generated_at_utc=clock.isoformat(),
        requested=dict(requested_dict),
        universe_audit=universe_audit.as_dict() if universe_audit else {},
        taxonomy_audit=taxonomy_audit.as_dict() if taxonomy_audit else {},
        seed_audit=seed_report.as_dict() if seed_report else {},
        history_audit=history_report.as_dict() if history_report else {},
        candidates=tuple(c.as_dict() for c in candidates),
        errors=tuple(errors),
    )


def _merge_frozen_thresholds(report: dict[str, Any]) -> dict[str, Any]:
    """Expose the frozen scoring minimums in the audit header.

    Documents exactly which threshold a candidate must clear to be marked
    READY_FOR_REVIEW. No field here can be set from operator input.
    """
    return {
        "frozen_thresholds": {
            "wallet_score_verdict_copy_candidate_min": VERDICT_COPY_CANDIDATE_MIN,
            "wallet_score_verdict_watchlist_min": VERDICT_WATCHLIST_MIN,
            "wallet_global_min_resolved_markets": GLOBAL_MIN_RESOLVED_MARKETS,
            "wallet_global_min_active_trading_days": GLOBAL_MIN_ACTIVE_TRADING_DAYS,
            "wallet_global_min_distinct_events": GLOBAL_MIN_DISTINCT_EVENTS,
            "category_min_resolved_markets": CATEGORY_MIN_RESOLVED_MARKETS,
            "category_min_distinct_events": CATEGORY_MIN_DISTINCT_EVENTS,
            "category_min_active_days": CATEGORY_MIN_ACTIVE_DAYS,
        },
        "ready_to_wire_to_automation": False,
    }


def attach_frozen_thresholds(report: DiscoveryReport) -> dict[str, Any]:
    """Return the report dict extended with frozen thresholds metadata."""
    base = report.as_dict()
    base["fallback"] = {**base.get("fallback", {}), **_merge_frozen_thresholds(base)}
    return base


# Expose the runtime symbol the existing CLI imports to maintain
# backwards compatibility for the test that was written for the
# scaffold (it only used the old stub function name).
def discover_short_horizon_specialists_offline(
    *,
    now: datetime | None = None,
    markets: Sequence[Mapping[str, Any]] = (),
    market_trades: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    leaderboard: Sequence[Mapping[str, Any]] = (),
    requested: Mapping[str, Any] | None = None,
) -> DiscoveryReport:
    """Offline path: build the report directly from a fixture dict.

    The pure engine does the same aggregation it would for live data,
    but the inputs are caller-supplied maps so tests can drive it
    deterministically. Used by STEP 11 G tests to verify status
    classification against expected fixtures.
    """
    market_trades = market_trades or {}

    # Build minimal classifications from fixture.
    classifications: list[MarketClassification] = []
    for idx, market in enumerate(markets):
        classifications.append(MarketClassification(
            condition_id=str(market.get("conditionId") or market.get("condition_id") or f"unknown-{idx}"),
            question=str(market.get("question") or ""),
            end_date_iso=str(market.get("endDate") or market.get("end_date") or ""),
            category_label=None,
            taxonomy_source=None,
            taxonomy_status=None,
            horizon_status=None,
            bucket="MALFORMED",
            reasons=(),
            excluded=True,
            eligible=False,
        ))

    # Build minimal history_records per wallet.
    by_wallet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mid, rows in market_trades.items():
        for raw in rows:
            wallet = str(raw.get("proxyWallet") or "").strip().lower()
            if not wallet.startswith("0x") or len(wallet) != 42:
                continue
            by_wallet[wallet].append({
                "market": mid, "raw": raw,
            })

    history_records: list[WalletHistoryRecord] = []
    for wallet, items in by_wallet.items():
        history_records.append(WalletHistoryRecord(
            wallet_address=wallet,
            settled=(),
            early_exit=(),
            unresolved=(),
            incomplete=(),
            first_qualifying_trade=None,
            last_qualifying_trade=None,
            active_trading_days=0,
            distinct_events=(),
            buy_count=0,
            sell_count=0,
            two_sided_churn=False,
            market_concentration={},
            event_concentration={},
            largest_market_pnl_share=None,
            largest_event_pnl_share=None,
            long_horizon_excluded=0,
            taxonomy_excluded=0,
            source_incomplete=0,
            evidence_completeness=0.0,
        ))

    return discover_short_horizon_specialists(
        classifications=tuple(classifications),
        history_records=tuple(history_records),
        requested=requested,
        now=now,
    )


__all__ = [
    "DISCOVERY_CONTRACT_VERSION",
    "DiscoveryReport",
    "STATUS_COMPLETE_BUT_BELOW_THRESHOLD",
    "STATUS_CONFLICT",
    "STATUS_ERROR",
    "STATUS_INSUFFICIENT_SETTLED_EVIDENCE",
    "STATUS_LONG_HORIZON_HEAVY",
    "STATUS_READY_FOR_REVIEW",
    "STATUS_SOURCE_INCOMPLETE",
    "STATUS_TAXONOMY_INCOMPLETE",
    "WalletCandidateResult",
    "attach_frozen_thresholds",
    "discover_short_horizon_specialists",
    "discover_short_horizon_specialists_offline",
]
