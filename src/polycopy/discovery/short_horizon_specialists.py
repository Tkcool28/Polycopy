"""Pure, report-only PR69 short-horizon specialist discovery.

Inputs are caller-supplied public payloads.  This module opens no database,
performs no HTTP, persists nothing, and never approves a wallet.  Market-first
trades and leaderboard addresses are merely discovery seeds; only reconciled,
trade-time eligible evidence reaches the frozen canonical scorers.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from polycopy.policy.short_horizon import evaluate_short_horizon
from polycopy.scoring.category_wallet_score_v1 import CategoryWalletScoreInputV1, compute_category_wallet_score_v1
from polycopy.scoring.wallet_score_v1 import WalletScoreInputV1, compute_wallet_score_v1
from polycopy.taxonomy.official_polymarket import OfficialPolymarketTaxonomyResolverV1, TAXONOMY_USABLE

DISCOVERY_CONTRACT_VERSION = "pr69-short-horizon-discovery-v1"


def _time(value: Any) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _end(market: Mapping[str, Any]) -> Any:
    return market.get("endDate") or market.get("end_date") or market.get("endDateIso")


def _condition(market: Mapping[str, Any]) -> str:
    return str(market.get("conditionId") or market.get("condition_id") or "").lower()


def _wallet(row: Mapping[str, Any]) -> str | None:
    value = row.get("proxyWallet") or row.get("trader_address") or row.get("wallet") or row.get("user")
    text = str(value).strip().lower() if value is not None else ""
    return text if text.startswith("0x") and len(text) == 42 else None


def _identity(row: Mapping[str, Any]) -> str:
    for key in ("source_trade_id", "transactionHash", "id"):
        if row.get(key):
            return f"{key}:{row[key]}"
    # Exact payload identity only; no timing/early-exit heuristic is used.
    return "payload:" + hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class ReconciledTrade:
    wallet: str
    trade_id: str
    market_id: str
    category: str
    timestamp: str
    resolved: bool
    winning: bool | None
    realized_pnl: float | None
    horizon_status: str


@dataclass(frozen=True)
class DiscoveryReport:
    contract_version: str
    market_seed_count: int
    leaderboard_seed_count: int
    seeded_wallets: tuple[str, ...]
    reconciled_trade_count: int
    rejected: dict[str, int]
    wallets: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def discover_short_horizon_specialists(
    markets: Sequence[Mapping[str, Any]],
    market_trades: Mapping[str, Sequence[Mapping[str, Any]]],
    leaderboard: Sequence[Mapping[str, Any]] = (),
    *,
    now: datetime | None = None,
) -> DiscoveryReport:
    """Reconcile evidence and score report-only candidates with frozen v1 code.

    A trade must have a trusted official category and pass the horizon policy at
    its own timestamp.  Resolution is accepted only from explicit historical
    fields; selling or an early exit never becomes a win.  Dedupe is by exact
    public trade identity across every fetched page/market, never by wallet or
    market, so distinct fills remain evidence.
    """
    clock = now or datetime.now(timezone.utc)
    resolver = OfficialPolymarketTaxonomyResolverV1()
    accepted: list[ReconciledTrade] = []
    rejected: defaultdict[str, int] = defaultdict(int)
    seeds: set[str] = set()
    seen: set[str] = set()
    market_count = 0
    for market in markets:
        mid = _condition(market)
        taxonomy = resolver.resolve(market)
        if not mid or taxonomy.status != TAXONOMY_USABLE or not taxonomy.category_label:
            rejected["market_taxonomy_unusable"] += 1
            continue
        market_count += 1
        for raw in market_trades.get(mid, market_trades.get(str(market.get("conditionId") or ""), ())):
            if not isinstance(raw, Mapping):
                rejected["malformed_trade"] += 1
                continue
            wallet = _wallet(raw)
            timestamp = _time(raw.get("timestamp"))
            if not wallet or timestamp is None:
                rejected["trade_identity_or_time_unavailable"] += 1
                continue
            assessment = evaluate_short_horizon(timestamp, _end(market), actual_redeem_timestamp=raw.get("redeemedAt") or raw.get("redeem_timestamp"))
            if not assessment.eligible:
                rejected[f"horizon:{assessment.status}"] += 1
                continue
            identity = _identity(raw)
            if identity in seen:
                rejected["exact_public_trade_duplicate"] += 1
                continue
            seen.add(identity)
            seeds.add(wallet)
            status = str(raw.get("resolution_status") or raw.get("status") or "").lower()
            winning_raw = raw.get("is_winning_trade")
            winning = bool(winning_raw) if winning_raw in (0, 1, False, True) else None
            resolved = status in {"won", "lost", "resolved"} and winning is not None
            pnl = raw.get("realized_pnl")
            try:
                pnl_value = float(pnl) if pnl is not None else None
            except (TypeError, ValueError):
                pnl_value = None
            accepted.append(ReconciledTrade(wallet, identity, mid, taxonomy.category_label, timestamp.isoformat(), resolved, winning, pnl_value, assessment.status))
    for item in leaderboard:
        if isinstance(item, Mapping) and (address := _wallet(item)):
            seeds.add(address)
    rows: list[dict[str, Any]] = []
    for address in sorted(seeds):
        evidence = [item for item in accepted if item.wallet == address]
        resolved = [item for item in evidence if item.resolved]
        wins = [item for item in resolved if item.winning]
        pnl_complete = [item.realized_pnl for item in resolved if item.realized_pnl is not None]
        pnl_known = len(pnl_complete) == len(resolved)
        gross_gain = sum(max(0.0, value) for value in pnl_complete)
        gross_loss = -sum(min(0.0, value) for value in pnl_complete)
        categories = sorted({item.category for item in evidence})
        wallet_input = WalletScoreInputV1(wallet_id=address, trade_count=len(resolved), win_rate=(len(wins) / len(resolved)) if resolved else None, profit_factor=(gross_gain / gross_loss) if pnl_known and gross_loss else None, sample_fraction=0.0, overall_trade_count=len(evidence), resolved_markets=len({x.market_id for x in resolved}), active_trading_days=len({x.timestamp[:10] for x in evidence}), distinct_events=len({x.market_id for x in resolved}))
        wallet_score = compute_wallet_score_v1(input=wallet_input, now=clock)
        category_scores = []
        for category in categories:
            scoped = [item for item in evidence if item.category == category]
            scoped_resolved = [item for item in scoped if item.resolved]
            scoped_wins = [item for item in scoped_resolved if item.winning]
            scoped_input = CategoryWalletScoreInputV1(wallet_id=address, category_label=category, trade_count=len(scoped_resolved), win_rate=(len(scoped_wins) / len(scoped_resolved)) if scoped_resolved else None, sample_fraction=0.0, category_trade_count=len(scoped), category_distinct_markets=len({x.market_id for x in scoped}), overall_trade_count=len(evidence), category_resolved_markets=len({x.market_id for x in scoped_resolved}), category_distinct_events=len({x.market_id for x in scoped_resolved}), category_active_days=len({x.timestamp[:10] for x in scoped}))
            score = compute_category_wallet_score_v1(input=scoped_input, now=clock)
            category_scores.append({"category": category, "score": score.score, "verdict": score.verdict.value, "missing_essentials": score.missing_essentials, "gate_failures": score.category_gate_failures})
        rows.append({"wallet": address, "seed_sources": ["market_first"] if evidence else ["leaderboard"], "reconciled_trades": len(evidence), "resolved_trades": len(resolved), "wallet_score": wallet_score.score, "wallet_verdict": wallet_score.verdict.value, "wallet_missing_essentials": wallet_score.missing_essentials, "category_scores": category_scores})
    return DiscoveryReport(DISCOVERY_CONTRACT_VERSION, market_count, len(leaderboard), tuple(sorted(seeds)), len(accepted), dict(sorted(rejected.items())), tuple(rows))
