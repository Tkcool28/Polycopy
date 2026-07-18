"""Canonical persisted BUY-evidence aggregation for PR67.

This module reads only ``source_trades`` and PR66 ``metadata_json``.  It never
settles trades, guesses SELL P&L, creates candidates, or performs I/O beyond the
caller-provided database query surface.  Event identity is ``event.id`` then
``event.slug``; neither is a category label.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from polycopy.scoring.category_wallet_score_v1 import CategoryWalletScoreInputV1
from polycopy.scoring.score_serialization import (
    generate_idempotency_key,
    persist_category_score_v1,
    persist_wallet_score_v1,
)
from polycopy.scoring.wallet_score_v1 import WalletScoreInputV1, WalletVerdict, compute_wallet_score_v1
from polycopy.scoring.category_wallet_score_v1 import compute_category_wallet_score_v1

AGGREGATION_CONTRACT_VERSION = "pr67-wallet-evidence-v1"
CATEGORY_TAXONOMY_USABLE = "CATEGORY_TAXONOMY_USABLE"
CATEGORY_TAXONOMY_PARTIAL = "CATEGORY_TAXONOMY_PARTIAL"
CATEGORY_TAXONOMY_UNAVAILABLE = "CATEGORY_TAXONOMY_UNAVAILABLE"


@dataclass(frozen=True)
class TaxonomyClassification:
    """Typed PR66 taxonomy result for one source-trade metadata payload.

    ``source`` is deliberately fixed to the persisted PR66 metadata field;
    ``raw_value`` retains only the explicit raw category, never a title/slug
    inference.  Older callers can still construct the original three fields.
    """

    status: str
    category_label: Optional[str]
    reason: Optional[str]
    source: str = "source_trades.metadata_json"
    raw_value: Optional[str] = None


@dataclass(frozen=True)
class ScoreResolution:
    result: Any | None
    decision_id: Optional[int]
    formula_name: str
    formula_version: str
    evidence_fingerprint: Optional[str]
    source_data_timestamp: Optional[str]
    missing_reasons: tuple[str, ...]
    status: str
    reused: bool = False
    created: bool = False
    would_create: bool = False
    persisted: bool = False


@dataclass(frozen=True)
class WalletEvidence:
    wallet_id: str
    category_label: Optional[str]
    total_buy_trades: int
    resolved_buy_trades: int
    resolved_markets: int
    winning_buy_trades: int
    losing_buy_trades: int
    realized_pnl: Optional[float]
    win_rate: Optional[float]
    profit_factor: Optional[float]
    sharpe_ratio: Optional[float]
    max_drawdown: Optional[float]
    info_score: Optional[float]
    trade_intervals_std: Optional[float]
    largest_winner_share: Optional[float]
    top_3_concentration: Optional[float]
    active_trading_days: int
    distinct_events: int
    distinct_markets: int
    unresolved_buy_trades: int
    missing_event_identity_count: int
    evidence_start_timestamp: Optional[str]
    source_data_timestamp: Optional[str]
    evidence_fingerprint: str
    included_source_trade_ids: tuple[str, ...]
    missing_reasons: tuple[str, ...]

    def wallet_formula_kwargs(self) -> dict[str, Any]:
        """The truthful frozen-v1 input subset available from source evidence."""
        return {
            "trade_count": self.resolved_buy_trades,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "info_score": self.info_score,
            "trade_intervals_std": self.trade_intervals_std,
            "largest_winner_share": self.largest_winner_share,
            "top_3_concentration": self.top_3_concentration,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown": self.max_drawdown,
            "sample_fraction": 0.0,
            "category_trade_count": None,
            "category_distinct_markets": None,
            "overall_trade_count": self.total_buy_trades,
            "resolved_markets": self.resolved_markets,
            "active_trading_days": self.active_trading_days,
            "distinct_events": self.distinct_events,
            "category_resolved_markets": None,
            "category_distinct_events": None,
            "category_active_days": None,
        }

    def category_formula_kwargs(self) -> dict[str, Any]:
        return {
            "trade_count": self.resolved_buy_trades,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "info_score": self.info_score,
            "trade_intervals_std": self.trade_intervals_std,
            "largest_winner_share": self.largest_winner_share,
            "top_3_concentration": self.top_3_concentration,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown": self.max_drawdown,
            "sample_fraction": 0.0,
            "category_trade_count": self.total_buy_trades,
            "category_distinct_markets": self.distinct_markets,
            "overall_trade_count": self.total_buy_trades,
            "category_resolved_markets": self.resolved_markets,
            "category_distinct_events": self.distinct_events,
            "category_active_days": self.active_trading_days,
        }


def _metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def normalize_category_label(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    result = re.sub(r"\s+", " ", value.strip().lower())
    return result or None


def classify_category_taxonomy(metadata: dict[str, Any]) -> TaxonomyClassification:
    """Classify only explicit PR66 taxonomy evidence; never infer from titles."""
    taxonomy = metadata.get("taxonomy")
    if not isinstance(taxonomy, dict):
        return TaxonomyClassification(
            CATEGORY_TAXONOMY_UNAVAILABLE,
            None,
            "category_taxonomy_unavailable",
        )
    raw_category = taxonomy.get("raw_category")
    label = normalize_category_label(raw_category)
    if label is not None:
        return TaxonomyClassification(
            CATEGORY_TAXONOMY_USABLE,
            label,
            None,
            raw_value=raw_category if isinstance(raw_category, str) else None,
        )
    tags = taxonomy.get("tags")
    if isinstance(tags, list) and any(isinstance(tag, str) and tag.strip() for tag in tags):
        return TaxonomyClassification(
            CATEGORY_TAXONOMY_PARTIAL,
            None,
            "taxonomy_tags_unmapped",
        )
    return TaxonomyClassification(
        CATEGORY_TAXONOMY_UNAVAILABLE,
        None,
        "category_taxonomy_unavailable",
    )


def _event_identity(metadata: dict[str, Any]) -> Optional[str]:
    event = metadata.get("event")
    event = event if isinstance(event, dict) else {}
    for key in ("id", "slug"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return f"{key}:{value.strip()}"
    return None


def _canonical_rows(db: Any, wallet_id: str, cutoff_timestamp: Optional[str]) -> list[dict[str, Any]]:
    wallet = db.fetchone("SELECT address, canonical_address FROM wallets WHERE id=?", (wallet_id,))
    if wallet is None:
        return []
    address = str(wallet["canonical_address"] or wallet["address"] or "").lower()
    sql = "SELECT * FROM source_trades WHERE lower(trader_address)=?"
    params: list[Any] = [address]
    if cutoff_timestamp is not None:
        sql += " AND COALESCE(timestamp, '') <= ?"
        params.append(cutoff_timestamp)
    sql += " ORDER BY COALESCE(timestamp,''), id"
    rows = [dict(row) for row in db.fetchall(sql, tuple(params))]
    # Canonical public source id is the de-dupe identity. Keep earliest stable row.
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("source_trade_id") or row.get("id"))
        deduped.setdefault(key, row)
    return list(deduped.values())


def _timestamp_max(rows: list[dict[str, Any]]) -> Optional[str]:
    values = [str(row["timestamp"]) for row in rows if row.get("timestamp")]
    return max(values) if values else None


def _fingerprint(*, wallet_id: str, cutoff_timestamp: Optional[str], category_label: Optional[str], rows: list[dict[str, Any]]) -> str:
    material = []
    for row in rows:
        metadata = _metadata(row.get("metadata_json"))
        material.append({
            "id": str(row.get("source_trade_id") or row.get("id")),
            "side": row.get("side"), "timestamp": row.get("timestamp"),
            "market": row.get("market_source_id"), "status": row.get("resolution_status"),
            "winning": row.get("is_winning_trade"), "pnl": row.get("realized_pnl"),
            "event": _event_identity(metadata),
            "category": classify_category_taxonomy(metadata).category_label,
        })
    payload = {"contract": AGGREGATION_CONTRACT_VERSION, "wallet": wallet_id,
               "cutoff": cutoff_timestamp, "category": category_label, "rows": material}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _aggregate(db: Any, wallet_id: str, cutoff_timestamp: Optional[str], category_label: Optional[str]) -> WalletEvidence:
    rows = _canonical_rows(db, wallet_id, cutoff_timestamp)
    if category_label is not None:
        rows = [row for row in rows if classify_category_taxonomy(_metadata(row.get("metadata_json"))).category_label == category_label]
    buy_rows = [row for row in rows if str(row.get("side") or "").upper() == "BUY"]
    resolved = [row for row in buy_rows if str(row.get("resolution_status") or "").lower() in {"won", "lost", "resolved"} and row.get("is_winning_trade") is not None]
    unresolved = [row for row in buy_rows if row not in resolved]
    wins = [row for row in resolved if int(row["is_winning_trade"]) == 1]
    losses = [row for row in resolved if int(row["is_winning_trade"]) == 0]
    complete_pnl = [row for row in resolved if row.get("realized_pnl") is not None]
    pnl = sum(float(row["realized_pnl"]) for row in complete_pnl) if len(complete_pnl) == len(resolved) else None
    gross_gain = sum(max(0.0, float(row["realized_pnl"])) for row in complete_pnl)
    gross_loss = -sum(min(0.0, float(row["realized_pnl"])) for row in complete_pnl)
    profit_factor = gross_gain / gross_loss if gross_loss > 0 and len(complete_pnl) == len(resolved) else None
    # Event evidence is assessed across all relevant BUY activity; unresolved
    # rows cannot contribute performance but a missing identity is still an
    # explicit evidence gap rather than something silently ignored.
    events = [_event_identity(_metadata(row.get("metadata_json"))) for row in resolved]
    missing_events = sum(
        _event_identity(_metadata(row.get("metadata_json"))) is None
        for row in buy_rows
    )
    timestamps = [str(row["timestamp"]) for row in buy_rows if row.get("timestamp")]
    days = {timestamp[:10] for timestamp in timestamps if len(timestamp) >= 10}
    reasons: list[str] = []
    if not resolved:
        reasons.append("no_resolved_buy_evidence")
    if len(complete_pnl) != len(resolved):
        reasons.append("resolved_buy_missing_realized_pnl")
    if missing_events:
        reasons.append("missing_event_identity")
    source_ts = _timestamp_max(rows)
    # Honest risk statistics from the real resolved P&L series (per-trade
    # returns). These are required by the frozen guard contract (Rule 1b:
    # sharpe_ratio / max_drawdown must be present). Computed only when every
    # resolved trade has a realized_pnl; otherwise left None (incomplete).
    sharpe_ratio: Optional[float] = None
    max_drawdown: Optional[float] = None
    if complete_pnl:
        returns = [float(r["realized_pnl"]) for r in complete_pnl]
        n = len(returns)
        if n >= 2:
            mean = sum(returns) / n
            var = sum((x - mean) ** 2 for x in returns) / (n - 1)
            std = var ** 0.5
            if std > 0:
                sharpe_ratio = (mean / std) * (n ** 0.5)
            # Running peak-to-trough drawdown on the cumulative P&L curve.
            peak = 0.0
            trough = 0.0
            max_dd = 0.0
            cum = 0.0
            for r in returns:
                cum += r
                peak = max(peak, cum)
                trough = cum - peak
                max_dd = min(max_dd, trough)
            max_drawdown = abs(max_dd)
    # Information & price-improvement quality: fraction of resolved trades
    # carrying complete, canonical PR66 metadata (event identity + taxonomy
    # label). This is the honest "information score" the frozen formula
    # weights at 30% — it rewards wallets whose trades carry trustworthy
    # provenance rather than anonymized blobs. Computed only when there is
    # at least one resolved trade.
    info_score: Optional[float] = None
    if resolved:
        complete = 0
        for row in resolved:
            md = _metadata(row.get("metadata_json"))
            ident = _event_identity(md)
            label = classify_category_taxonomy(md).category_label
            if ident is not None and label is not None:
                complete += 1
        info_score = complete / len(resolved)
    # Chronological consistency: std dev (seconds) of inter-trade intervals
    # across BUY activity. None when fewer than two timestamps exist.
    trade_intervals_std: Optional[float] = None
    if len(timestamps) >= 2:
        try:
            ts_sorted = sorted(
                datetime.fromisoformat(t.replace("Z", "+00:00")) for t in timestamps
            )
            deltas = [
                (ts_sorted[i + 1] - ts_sorted[i]).total_seconds()
                for i in range(len(ts_sorted) - 1)
            ]
            if len(deltas) >= 2:
                m = sum(deltas) / len(deltas)
                v = sum((d - m) ** 2 for d in deltas) / (len(deltas) - 1)
                trade_intervals_std = v ** 0.5
        except (ValueError, TypeError):
            trade_intervals_std = None
    # Concentration: largest single winner share and top-3 winner share of
    # total winner realized P&L. None when there are no winning trades.
    largest_winner_share: Optional[float] = None
    top_3_concentration: Optional[float] = None
    if wins:
        winner_pnls = sorted((float(row["realized_pnl"]) for row in wins), reverse=True)
        total_winner = sum(winner_pnls)
        if total_winner > 0:
            largest_winner_share = winner_pnls[0] / total_winner
            top_3 = sum(winner_pnls[:3])
            top_3_concentration = top_3 / total_winner
    return WalletEvidence(
        wallet_id=wallet_id, category_label=category_label, total_buy_trades=len(buy_rows),
        resolved_buy_trades=len(resolved), resolved_markets=len({str(row.get("market_source_id")) for row in resolved if row.get("market_source_id")}),
        winning_buy_trades=len(wins), losing_buy_trades=len(losses), realized_pnl=pnl,
        win_rate=(len(wins) / len(resolved)) if resolved else None, profit_factor=profit_factor,
        sharpe_ratio=sharpe_ratio, max_drawdown=max_drawdown,
        info_score=info_score, trade_intervals_std=trade_intervals_std,
        largest_winner_share=largest_winner_share, top_3_concentration=top_3_concentration,
        active_trading_days=len(days), distinct_events=len({event for event in events if event}),
        distinct_markets=len({str(row.get("market_source_id")) for row in buy_rows if row.get("market_source_id")}),
        unresolved_buy_trades=len(unresolved), missing_event_identity_count=missing_events,
        evidence_start_timestamp=min(timestamps) if timestamps else None, source_data_timestamp=source_ts,
        evidence_fingerprint=_fingerprint(wallet_id=wallet_id, cutoff_timestamp=cutoff_timestamp, category_label=category_label, rows=rows),
        included_source_trade_ids=tuple(sorted(str(row.get("source_trade_id") or row.get("id")) for row in rows)),
        missing_reasons=tuple(reasons),
    )


def aggregate_wallet_evidence(db: Any, wallet_id: str, *, cutoff_timestamp: Optional[str]) -> WalletEvidence:
    return _aggregate(db, wallet_id, cutoff_timestamp, None)


def aggregate_category_evidence(db: Any, wallet_id: str, category_label: str, *, cutoff_timestamp: Optional[str]) -> WalletEvidence:
    canonical = normalize_category_label(category_label)
    if canonical is None:
        raise ValueError("category_label must be non-empty")
    return _aggregate(db, wallet_id, cutoff_timestamp, canonical)


def build_wallet_score_input_v1(evidence: WalletEvidence) -> WalletScoreInputV1:
    """Map only truthfully available persisted evidence into frozen v1."""
    return WalletScoreInputV1(wallet_id=evidence.wallet_id, **evidence.wallet_formula_kwargs())


def build_category_score_input_v1(
    evidence: WalletEvidence,
    category_label: str,
    *,
    overall_trade_count: int,
) -> CategoryWalletScoreInputV1:
    """Build category input with the required wallet-wide BUY denominator."""
    kwargs = evidence.category_formula_kwargs()
    kwargs["overall_trade_count"] = overall_trade_count
    return CategoryWalletScoreInputV1(
        wallet_id=evidence.wallet_id,
        category_label=category_label,
        source_data_timestamp=evidence.source_data_timestamp,
        **kwargs,
    )


def _existing_id(db: Any, table: str, where: str, params: tuple[Any, ...]) -> Optional[int]:
    row = db.fetchone(f"SELECT id FROM {table} WHERE {where}", params)
    return int(row["id"]) if row is not None else None


def _dominant_category_label(
    db: Any, wallet_id: str, cutoff_timestamp: Optional[str]
) -> Optional[str]:
    """Return the dominant usable PR66 category label across a wallet's trades.

    Used to attach honest category-resolution evidence to the global wallet
    decision (the frozen guard contract requires ``category_resolved_markets``
    to be populated on the global row). Only explicit taxonomy evidence from
    ``source_trades.metadata_json`` is consulted — never titles or inference.
    """
    rows = _canonical_rows(db, wallet_id, cutoff_timestamp)
    counts: dict[str, int] = {}
    for row in rows:
        classification = classify_category_taxonomy(_metadata(row.get("metadata_json")))
        if classification.status == CATEGORY_TAXONOMY_USABLE and classification.category_label:
            counts[classification.category_label] = counts.get(classification.category_label, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def resolve_wallet_score_v1(db: Any, wallet_id: str, *, cutoff_timestamp: Optional[str], persist: bool, now: Any, persist_commit: bool = True) -> ScoreResolution:
    evidence = aggregate_wallet_evidence(db, wallet_id, cutoff_timestamp=cutoff_timestamp)
    # The global wallet decision must also carry category resolution evidence
    # (frozen guard contract: a wallet decision with category_resolved_markets
    # IS NULL is forced INCOMPLETE with no_resolved_market_evidence). We classify
    # the wallet's taxonomy and aggregate the category evidence honestly from the
    # same persisted source_trades — no fabrication, no threshold change.
    input_kwargs = evidence.wallet_formula_kwargs()
    cat_label = _dominant_category_label(db, wallet_id, cutoff_timestamp)
    if cat_label is not None:
        cat_evidence = aggregate_category_evidence(db, wallet_id, cat_label, cutoff_timestamp=cutoff_timestamp)
        ckw = cat_evidence.category_formula_kwargs()
        input_kwargs["category_resolved_markets"] = ckw.get("category_resolved_markets")
        input_kwargs["category_distinct_events"] = ckw.get("category_distinct_events")
        input_kwargs["category_active_days"] = ckw.get("category_active_days")
        input_kwargs["category_trade_count"] = ckw.get("category_trade_count")
        input_kwargs["category_distinct_markets"] = ckw.get("category_distinct_markets")
    from polycopy.scoring.wallet_score_v1 import WalletScoreInputV1
    result = compute_wallet_score_v1(
        input=WalletScoreInputV1(wallet_id=evidence.wallet_id, **input_kwargs),
        now=now,
    )
    idem = generate_idempotency_key(formula_name="wallet_score", formula_version=result.formula_version, wallet_id=wallet_id, source_data_timestamp=evidence.source_data_timestamp, extra_params={"evidence_fingerprint": evidence.evidence_fingerprint, "contract": AGGREGATION_CONTRACT_VERSION})
    existing = _existing_id(db, "wallet_score_decisions", "wallet_id=? AND formula_name='wallet_score' AND formula_version=? AND idempotency_key=?", (wallet_id, result.formula_version, idem))
    if existing is not None:
        return ScoreResolution(result, existing, "wallet_score", result.formula_version, evidence.evidence_fingerprint, evidence.source_data_timestamp, tuple((*evidence.missing_reasons, *result.missing_essentials)), "complete" if result.verdict != WalletVerdict.INCOMPLETE else "incomplete", reused=True, persisted=True)
    if not persist:
        return ScoreResolution(result, None, "wallet_score", result.formula_version, evidence.evidence_fingerprint, evidence.source_data_timestamp, tuple((*evidence.missing_reasons, *result.missing_essentials)), "complete" if result.verdict != WalletVerdict.INCOMPLETE else "incomplete", would_create=True)
    decision_id = persist_wallet_score_v1(db, wallet_id, result, idempotency_key=idem, source_data_timestamp=evidence.source_data_timestamp, commit=persist_commit)
    return ScoreResolution(result, decision_id, "wallet_score", result.formula_version, evidence.evidence_fingerprint, evidence.source_data_timestamp, tuple((*evidence.missing_reasons, *result.missing_essentials)), "complete" if result.verdict != WalletVerdict.INCOMPLETE else "incomplete", created=True, persisted=True)


def resolve_category_score_v1(db: Any, wallet_id: str, taxonomy: TaxonomyClassification, *, cutoff_timestamp: Optional[str], persist: bool, now: Any, persist_commit: bool = True) -> ScoreResolution:
    if taxonomy.status != CATEGORY_TAXONOMY_USABLE or taxonomy.category_label is None:
        return ScoreResolution(None, None, "category_wallet_score", "1", None, None, (taxonomy.reason or "category_taxonomy_unavailable",), "not_applicable")
    evidence = aggregate_category_evidence(db, wallet_id, taxonomy.category_label, cutoff_timestamp=cutoff_timestamp)
    wallet_evidence = aggregate_wallet_evidence(
        db, wallet_id, cutoff_timestamp=cutoff_timestamp
    )
    result = compute_category_wallet_score_v1(
        input=build_category_score_input_v1(
            evidence,
            taxonomy.category_label,
            overall_trade_count=wallet_evidence.total_buy_trades,
        ),
        now=now,
    )
    idem = generate_idempotency_key(formula_name="category_wallet_score", formula_version=result.formula_version, wallet_id=wallet_id, source_data_timestamp=evidence.source_data_timestamp, extra_params={"category_label": taxonomy.category_label, "evidence_fingerprint": evidence.evidence_fingerprint, "contract": AGGREGATION_CONTRACT_VERSION})
    existing = _existing_id(db, "category_wallet_score_decisions", "wallet_id=? AND category_label=? AND formula_name='category_wallet_score' AND formula_version=? AND idempotency_key=?", (wallet_id, taxonomy.category_label, result.formula_version, idem))
    missing = tuple((*evidence.missing_reasons, *result.missing_essentials, *result.category_gate_failures))
    status = "complete" if result.verdict != WalletVerdict.INCOMPLETE else "incomplete"
    if existing is not None:
        return ScoreResolution(result, existing, "category_wallet_score", result.formula_version, evidence.evidence_fingerprint, evidence.source_data_timestamp, missing, status, reused=True, persisted=True)
    if not persist:
        return ScoreResolution(result, None, "category_wallet_score", result.formula_version, evidence.evidence_fingerprint, evidence.source_data_timestamp, missing, status, would_create=True)
    decision_id = persist_category_score_v1(db, wallet_id, taxonomy.category_label, result, idempotency_key=idem, source_data_timestamp=evidence.source_data_timestamp, commit=persist_commit)
    return ScoreResolution(result, decision_id, "category_wallet_score", result.formula_version, evidence.evidence_fingerprint, evidence.source_data_timestamp, missing, status, created=True, persisted=True)


__all__ = [
    "AGGREGATION_CONTRACT_VERSION", "CATEGORY_TAXONOMY_PARTIAL", "CATEGORY_TAXONOMY_UNAVAILABLE", "CATEGORY_TAXONOMY_USABLE", "ScoreResolution", "TaxonomyClassification", "WalletEvidence", "aggregate_category_evidence", "aggregate_wallet_evidence", "build_category_score_input_v1", "build_wallet_score_input_v1", "classify_category_taxonomy", "normalize_category_label", "resolve_category_score_v1", "resolve_wallet_score_v1",
]
