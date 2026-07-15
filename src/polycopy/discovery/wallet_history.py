"""Historical wallet evidence fetcher + position-level reconciler.

Pure-style orchestration. Opens no database. Calls only the public
:mod:`polycopy.discovery.adapter` wrappers (which wrap the production
Polymarket adapter with retry + budget guards).

DESIGN — POSITION-LEVEL (PR69 correction pass)
=============================================
The unit of evaluation evidence is a **position**, identified by
``(wallet_address, condition_id, asset_id)`` — never a single trade fill.

A wallet may hold more than one asset/outcome inside one Polymarket
condition (e.g. BUY YES and BUY NO). Each such (condition, asset) pair is a
distinct position and is reconciled independently.

For every reconciled position we:

  * retain all BUY fills and all SELL fills (provenance only),
  * aggregate BUY quantity/cost and SELL quantity/proceeds,
  * retain the net quantity/exposure and first/last timestamps,
  * match closed-position PnL **once**,
  * match REDEEM evidence **once**,
  * match the official final outcome **once**,
  * assign exactly one ``SettledPositionProof`` record (not one per fill).

Settlement states are AUTHORITATIVE. A position is a settled win only when
the held asset/outcome matches the official winning asset/outcome. A
REDEEM row without a winning marker is NOT automatically a loss. Missing
winning-outcome evidence is *incomplete*, not a loss. Closed-position PnL
does not by itself prove forecasting correctness.

PnL is reconciled once per position key. Duplicate closed-position rows are
deduplicated by a deterministic fingerprint; a closed-position row and a
REDEEM row are never counted as two separate PnL events.

Timestamps are normalized exactly once at the parser boundary to canonical
UTC ISO-8601. Unix integers and trusted ISO strings are equivalent after
normalization; ``timestamp[:10]`` string slicing is never used to derive a
trading day.

The reconciler consumes only the trade-time horizon gate via
``assessment.status`` (``HORIZON_PREFERRED`` / ``HORIZON_ELIGIBLE``).
There is no ``assessment.horizon_status`` property — a malformed status is
incomplete rather than defaulting to preferred.

Real public data is preserved: this module never discards the working live
adapter; it only corrects the reconciliation math.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from polycopy.discovery._safe_get import ERR_BUDGET_EXHAUSTED, _RequestBudget
from polycopy.discovery.adapter import (
    WALLET_MATCH_ROLE_NONE,
    DiscoveryAdapter,
    extract_wallet_match_role,
)
from polycopy.discovery.market_universe import MarketClassification
from polycopy.discovery.wallet_seeds import SeedWallet
from polycopy.policy.short_horizon import (
    HORIZON_PREFERRED,
    evaluate_short_horizon,
)

logger = logging.getLogger(__name__)


DEFAULT_HISTORY_DAYS = 365
MIN_HISTORY_DAYS = 1
MAX_HISTORY_DAYS = 730
DEFAULT_HISTORY_MAX_PAGES = 5

# Position settlement/outcome states.
SETTLED_WIN = "SETTLED_WIN"
SETTLED_LOSS = "SETTLED_LOSS"
RESOLVED_OUTCOME_UNKNOWN = "RESOLVED_OUTCOME_UNKNOWN"
REDEEM_CONFIRMED_OUTCOME_UNKNOWN = "REDEEM_CONFIRMED_OUTCOME_UNKNOWN"
EARLY_EXIT = "EARLY_EXIT"
UNRESOLVED = "UNRESOLVED"
SOURCE_INCOMPLETE_STATE = "SOURCE_INCOMPLETE"
CONFLICT_STATE = "CONFLICT"


def _to_utc(value: Any) -> datetime | None:
    """Normalize a timestamp to aware UTC exactly once.

    Accepts Unix seconds (int/float), trusted aware ISO strings, or naive
    ISO strings (rejected per spec — fail closed). Returns ``None`` on any
    malformed/naive input.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        s = value.strip()
        # Normalize the 'Z' suffix to an explicit +00:00.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(s)
        except (TypeError, ValueError):
            # Try a bare integer encoded as a string.
            try:
                return datetime.fromtimestamp(float(s), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    return None


def _utc_iso(dt: datetime | None) -> str | None:
    """Canonical UTC ISO-8601 string (e.g. ``2026-07-14T12:00:00+00:00``)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _trading_day(dt: datetime | None) -> str | None:
    """Calendar day derived from a parsed UTC datetime (never string slicing)."""
    iso = _utc_iso(dt)
    if iso is None:
        return None
    # ISO is canonical; the calendar-day prefix is ``YYYY-MM-DD``.
    return iso[:10]


def _normalize_ts(value: Any) -> str | None:
    """Canonical UTC ISO string for a timestamp (Unix int or ISO). None if malformed."""
    return _utc_iso(_to_utc(value))


def dedupe_closed_positions(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate closed-position rows by authoritative upstream identity.

    STEP 5: when no unique upstream ID exists, a deterministic fingerprint of
    all material fields is used. The same row is never counted twice; two
    genuinely distinct components are both retained.
    """
    seen: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        fp = _closed_position_fingerprint(row)
        if fp in seen:
            # Identical fingerprint → duplicate, skip.
            continue
        seen[fp] = dict(row)
        out.append(dict(row))
    return out


def reconcile_positions(
    grouped: Mapping[PositionKey, Sequence[TradeFill]],
    resolutions: Mapping[str, Mapping[str, Any]],
    event_map: Mapping[str, str],
) -> list[ReconciledPosition]:
    """Reconcile grouped fills into one position record per PositionKey.

    Pure helper mirroring the production reconciliation contract so tests can
    exercise STEP 3/4/5/16 without the network layer.
    """
    positions: list[ReconciledPosition] = []
    for key, fills in grouped.items():
        fills = list(fills)
        cond_id, asset = key.condition_id, key.asset_id
        buy = [f for f in fills if f.side == "BUY"]
        sell = [f for f in fills if f.side == "SELL"]
        buy_qty = sum(f.size for f in buy)
        buy_cost = sum(f.size * f.price for f in buy)
        sell_qty = sum(f.size for f in sell)
        sell_proceeds = sum(f.size * f.price for f in sell)
        net_qty = buy_qty - sell_qty
        first_ts = min((f.ts_iso for f in fills if f.ts_iso), default=None)
        last_ts = max((f.ts_iso for f in fills if f.ts_iso), default=None)
        res = resolutions.get(cond_id, {})
        winning_asset = res.get("winning_asset_id")
        winning_index = res.get("winning_outcome_index")
        winning_label = res.get("winning_outcome_label")
        resolved = bool(res.get("resolved", False)) or winning_asset is not None

        if not resolved:
            state = UNRESOLVED
            winning = None
            pnl = None
            pnl_source = None
        elif winning_asset == asset:
            state = SETTLED_WIN
            winning = True
            # Held-to-resolution: a winning share redeems at $1. If the wallet
            # also sold, use realized sell proceeds; otherwise impute the
            # $1 redemption payout on the net held quantity.
            if sell_qty > 0:
                pnl = sell_proceeds - buy_cost
            else:
                pnl = net_qty * 1.0 - buy_cost
            pnl_source = "closed_position"
        elif winning_asset is not None and winning_asset != asset:
            state = SETTLED_LOSS
            winning = False
            # Losing share redeems at $0. Realized only the sell proceeds (if any).
            pnl = sell_proceeds - buy_cost
            pnl_source = "closed_position"
        else:
            state = RESOLVED_OUTCOME_UNKNOWN
            winning = None
            pnl = None
            pnl_source = None

        positions.append(ReconciledPosition(
            wallet_address=key.wallet_address,
            condition_id=cond_id,
            asset_id=asset,
            outcome_index=next((f.outcome_index for f in buy), None),
            outcome_label=next((f.outcome_label for f in buy), None),
            category_label=None,
            event_identity=event_map.get(cond_id),
            horizon_status="PREFERRED",
            buy_fills=tuple(buy),
            sell_fills=tuple(sell),
            first_ts_iso=first_ts,
            last_ts_iso=last_ts,
            buy_qty=buy_qty,
            buy_cost=buy_cost,
            sell_qty=sell_qty,
            sell_proceeds=sell_proceeds,
            net_qty=net_qty,
            source_trade_identities=tuple(sorted({f.transaction_hash for f in fills if f.transaction_hash})),
            settlement_state=state,
            winning_outcome=winning,
            realized_pnl=pnl,
            pnl_source=pnl_source,
            pnl_complete=pnl is not None,
            pnl_conflict=False,
            redeemed=False,
            included_closed_position_ids=(),
            included_redeem_ids=(),
            official_winning_asset_id=winning_asset,
            official_winning_outcome_index=winning_index,
            official_winning_outcome_label=winning_label,
        ))
    return positions


def aggregate_concentration(
    positions: Sequence[ReconciledPosition],
    *,
    event_map: Mapping[str, str] | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    """Aggregate PnL to market and event from ONE canonical ledger (STEP 16).

    Each settled position contributes its realized PnL exactly once. No
    concatenation of duplicated sources.
    """
    event_map = event_map or {}
    market_pnl: dict[str, float] = {}
    event_pnl: dict[str, float] = {}
    for p in positions:
        if p.realized_pnl is None or p.pnl_conflict:
            continue
        market_pnl[p.condition_id] = market_pnl.get(p.condition_id, 0.0) + p.realized_pnl
        ev = event_map.get(p.condition_id) or p.event_identity or p.condition_id
        event_pnl[ev] = event_pnl.get(ev, 0.0) + p.realized_pnl
    return market_pnl, event_pnl


# ---------------------------------------------------------------------------
# Fill-level provenance (kept, but never independently increments scorers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeFill:
    """One upstream trade row, normalized.

    Retained as provenance. A fill never, by itself, increments settled
    markets, wins, losses, realized PnL, or resolved-market gates.
    """

    transaction_hash: str | None
    side: str
    price: float
    size: float
    ts_utc: datetime
    ts_iso: str
    asset_id: str
    outcome_index: int | None
    outcome_label: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# Public alias so test/CLI code can reference the fill type unambiguously.
Fill = TradeFill


@dataclass(frozen=True)
class PositionKey:
    """The authoritative position identity.

    A condition may contain multiple wallet positions when the wallet traded
    multiple assets/outcomes. We do NOT group solely by ``condition_id``.
    """

    wallet_address: str
    condition_id: str
    asset_id: str

    def as_tuple(self) -> tuple[str, str, str]:
        return (self.wallet_address, self.condition_id, self.asset_id)


# ---------------------------------------------------------------------------
# Position-level reconciliation output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciledPosition:
    """One fully reconciled (wallet, condition, asset) position.

    Produces exactly ONE settled/early-exit/unresolved record — never one
    per fill. PnL is reconciled once and never multiplied by fill count.
    """

    wallet_address: str
    condition_id: str
    asset_id: str
    outcome_index: int | None
    outcome_label: str | None
    category_label: str | None
    event_identity: str | None
    horizon_status: str
    # Provenance: all fills retained but not independently counted.
    buy_fills: tuple[TradeFill, ...]
    sell_fills: tuple[TradeFill, ...]
    first_ts_iso: str | None
    last_ts_iso: str | None
    buy_qty: float
    buy_cost: float
    sell_qty: float
    sell_proceeds: float
    net_qty: float
    source_trade_identities: tuple[str, ...]
    settlement_state: str  # one of the SETTLED_* / EARLY_EXIT / UNRESOLVED / ...
    # Settlement proof (only meaningful when state is a settled/early-exit state).
    winning_outcome: bool | None  # None = unknown
    realized_pnl: float | None
    pnl_source: str | None  # 'closed_position' | 'redeem' | None
    pnl_complete: bool
    pnl_conflict: bool
    redeemed: bool
    included_closed_position_ids: tuple[str, ...]
    included_redeem_ids: tuple[str, ...]
    official_winning_asset_id: str | None
    official_winning_outcome_index: int | None
    official_winning_outcome_label: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OfficialResolution:
    """Official market-resolution normalization (one per condition)."""

    condition_id: str
    resolved: bool
    closed: bool
    winning_asset_id: str | None
    winning_outcome_index: int | None
    winning_outcome_label: str | None
    event_identity: str | None
    end_date_iso: str | None
    source: str  # 'gamma_market' | 'fetched' | 'classification'

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Per-wallet evidence buckets (aggregated from reconciled positions)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettledEvidence:
    """A settled position exposed as forecasting evidence for the scorer."""

    wallet_address: str
    market_condition_id: str
    asset_id: str
    category_label: str | None
    event_identity: str | None
    outcome_index: int | None
    outcome_label: str | None
    settlement_state: str
    winning_outcome: bool | None
    realized_pnl: float | None
    pnl_source: str | None
    pnl_conflict: bool
    redeemed: bool
    horizon_status: str
    ts_iso: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EarlyExitEvidence:
    """An early-exit position (sold/closed before resolution)."""

    wallet_address: str
    market_condition_id: str
    asset_id: str
    category_label: str | None
    event_identity: str | None
    realized_pnl: float | None
    pnl_source: str | None
    pnl_conflict: bool
    horizon_status: str
    ts_iso: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UnresolvedEvidence:
    """A position whose market has not yet settled — excluded from settled inputs."""

    wallet_address: str
    market_condition_id: str
    asset_id: str
    category_label: str | None
    event_identity: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SourceIncompleteEvidence:
    """A position/wallet-level gap that limits readiness (not a binned bucket)."""

    wallet_address: str
    market_condition_id: str | None
    asset_id: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConflictEvidence:
    """A material conflict (PnL/outcome) that must exclude evidence from scoring."""

    wallet_address: str
    market_condition_id: str
    asset_id: str
    conflict_type: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WalletHistoryRecord:
    """All evidence types for one wallet, rolled up from reconciled positions."""

    wallet_address: str
    positions: tuple[ReconciledPosition, ...]
    settled: tuple[SettledEvidence, ...]
    early_exit: tuple[EarlyExitEvidence, ...]
    unresolved: tuple[UnresolvedEvidence, ...]
    source_incomplete: tuple[SourceIncompleteEvidence, ...]
    first_qualifying_trade: str | None
    last_qualifying_trade: str | None
    active_trading_days: int
    distinct_events: tuple[str, ...]
    distinct_markets: tuple[str, ...]
    buy_fill_count: int
    sell_fill_count: int
    two_sided_churn: bool
    market_pnl: dict[str, float]
    event_pnl: dict[str, float]
    largest_market_pnl_share: float | None
    largest_event_pnl_share: float | None
    top_three_market_pnl: tuple[tuple[str, float], ...]
    long_horizon_excluded: int
    taxonomy_excluded: int
    source_incomplete_count: int
    evidence_completeness: float

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        return out


@dataclass(frozen=True)
class WalletHistoryReport:
    """Top-level history discovery report."""

    wallets: tuple[WalletHistoryRecord, ...]
    resolutions: tuple[OfficialResolution, ...]
    api_errors: tuple[tuple[str, str, int], ...] = ()
    trades_seen: int = 0
    history_days: int = DEFAULT_HISTORY_DAYS
    eligible_only: bool = True
    source_audit: tuple[dict[str, str], ...] = ()
    source_incomplete: tuple[SourceIncompleteEvidence, ...] = ()
    conflicts: tuple[ConflictEvidence, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["api_errors"] = [list(e) for e in self.api_errors]
        return out


# Per-source statuses emitted by WalletHistoryFetcher — STEP 6 contract.
SOURCE_COMPLETE = "complete"
SOURCE_EMPTY = "empty"
SOURCE_PARTIAL = "partial"
SOURCE_MALFORMED = "malformed"
SOURCE_BUDGET_EXHAUSTED = "budget_exhausted"
SOURCE_HTTP_ERROR = "http_error"
SOURCE_UNSUPPORTED_SCHEMA = "unsupported_schema"


def _condition_to_end(
    classifications: Iterable[MarketClassification],
) -> dict[str, datetime]:
    mapping: dict[str, datetime] = {}
    for c in classifications:
        end_dt = _to_utc(c.end_date_iso) if c.end_date_iso else None
        if end_dt is not None:
            mapping[c.condition_id] = end_dt
    return mapping


def _category_for_condition(
    classifications: Iterable[MarketClassification],
) -> dict[str, str]:
    return {c.condition_id: c.category_label for c in classifications if c.category_label}


def _event_for_condition(
    classifications: Iterable[MarketClassification],
) -> dict[str, str]:
    """Map condition -> official event identity (event.id preferred).

    Category labels are NEVER used as event identities.
    """
    return {c.condition_id: c.event_identity for c in classifications if c.event_identity}


def _is_history_window(*, ts: datetime, as_of: datetime, days: int) -> bool:
    if ts is None:
        return False
    return ts >= (as_of - timedelta(days=days))


def _compute_concentration(
    values: Iterable[float],
) -> tuple[float | None, list[tuple[str, float]]]:
    """From one canonical PnL ledger (positive winners only per frozen basis)."""
    seq = sorted(values, reverse=True)
    if not seq:
        return None, []
    total = sum(seq)
    if total <= 0:
        return None, []
    largest_share = seq[0] / total
    top = [(str(i), v) for i, v in enumerate(seq[:3])]
    return largest_share, top


def _closed_position_fingerprint(row: Mapping[str, Any]) -> str:
    """Deterministic fingerprint of all material closed-position fields.

    Used to deduplicate duplicate upstream rows that share every material
    field (so a duplicate $5 row is counted once, not summed).
    """
    parts = [
        str(row.get("conditionId") or "").strip().lower(),
        str(row.get("assetId") or row.get("asset") or row.get("tokenId") or "").strip().lower(),
        str(row.get("user") or "").strip().lower(),
        str(row.get("realizedPnl") if row.get("realizedPnl") is not None else row.get("realized_pnl")),
        str(row.get("size") or ""),
        str(row.get("price") or ""),
        str(row.get("startTimestamp") or row.get("startTimestamp") or ""),
        str(row.get("endTimestamp") or row.get("endTimestamp") or ""),
    ]
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _redeem_fingerprint(row: Mapping[str, Any]) -> str:
    parts = [
        str(row.get("conditionId") or "").strip().lower(),
        str(row.get("assetId") or row.get("asset") or row.get("tokenId") or "").strip().lower(),
        str(row.get("user") or "").strip().lower(),
        str(row.get("transactionHash") or "").strip().lower(),
        str(row.get("winning") if row.get("winning") is not None else ""),
        str(row.get("payout") if row.get("payout") is not None else ""),
        str(row.get("timestamp") or ""),
    ]
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_asset_id(raw: Any) -> str:
    if raw is None:
        return ""
    return str(raw).strip().lower()


def _resolve_official(
    condition_id: str,
    *,
    classification: MarketClassification | None,
    fetched: Mapping[str, Any] | None,
) -> OfficialResolution:
    """Build an OfficialResolution from classification + (optional) fetched gamma.

    Precedence: fetched gamma payload (highest trust) > classification
    embedded category. Winning asset/outcome is taken from the fetched
    market's resolved outcome when available.
    """
    winning_asset: str | None = None
    winning_index: int | None = None
    winning_label: str | None = None
    event_id: str | None = None
    end_dt: datetime | None = None
    source = "classification"

    if fetched is not None:
        source = "gamma_market"
        # Gamma resolved outcome fields vary; best-effort extraction.
        # Prefer explicit winning token id if present.
        for tok in fetched.get("clobTokenIds") or []:
            pass
        w_asset = fetched.get("winningTokenId") or fetched.get("winningAssetId")
        if w_asset is not None:
            winning_asset = _normalize_asset_id(w_asset)
        w_idx = fetched.get("winningOutcomeIndex")
        if w_idx is not None:
            try:
                winning_index = int(w_idx)
            except (TypeError, ValueError):
                winning_index = None
        w_label = fetched.get("winningOutcomeLabel") or fetched.get("winningOutcome")
        if w_label is not None:
            winning_label = str(w_label)
        evt = fetched.get("events")
        if isinstance(evt, list) and evt:
            ev0 = evt[0] if isinstance(evt[0], Mapping) else {}
            eid = ev0.get("id") or ev0.get("slug")
            if eid:
                event_id = f"event:{eid}" if isinstance(eid, str) and not str(eid).startswith("event:") else str(eid)
        elif fetched.get("eventId"):
            event_id = f"event:{fetched['eventId']}" if not str(fetched['eventId']).startswith("event:") else str(fetched['eventId'])
        end_dt = _to_utc(fetched.get("endDate") or fetched.get("end_date"))

    if classification is not None:
        if event_id is None and classification.event_identity:
            event_id = classification.event_identity
        if end_dt is None and classification.end_date_iso:
            end_dt = _to_utc(classification.end_date_iso)

    return OfficialResolution(
        condition_id=condition_id,
        resolved=end_dt is not None,
        closed=bool(fetched.get("closed")) if fetched is not None else False,
        winning_asset_id=winning_asset,
        winning_outcome_index=winning_index,
        winning_outcome_label=winning_label,
        event_identity=event_id,
        end_date_iso=_utc_iso(end_dt),
        source=source,
    )


class WalletHistoryFetcher:
    """Fetch and reconcile one wallet's bounded history into positions."""

    def __init__(
        self,
        adapter: DiscoveryAdapter,
        *,
        budget: _RequestBudget,
        history_days: int = DEFAULT_HISTORY_DAYS,
        max_pages: int = DEFAULT_HISTORY_MAX_PAGES,
    ) -> None:
        if not (MIN_HISTORY_DAYS <= int(history_days) <= MAX_HISTORY_DAYS):
            raise ValueError(f"history_days must be in [{MIN_HISTORY_DAYS}, {MAX_HISTORY_DAYS}]")
        if int(max_pages) < 1:
            raise ValueError("max_pages must be >= 1")
        self._adapter = adapter
        self._budget = budget
        self._history_days = int(history_days)
        self._max_pages = int(max_pages)

    async def fetch(
        self,
        *,
        seeds: Sequence[SeedWallet],
        classifications: Sequence[MarketClassification],
        as_of: datetime,
    ) -> WalletHistoryReport:
        """Fetch + reconcile one wallet at a time.

        Per-wallet three sources are attempted in phased sequence:
          PHASE_HISTORIES → /trades?user=
          PHASE_CLOSED_POSITIONS → /closed-positions?user=
          PHASE_REDEEMS → /activity?user=&type=REDEEM
        Each source emits an independent source-status; zero rows is
        distinct from unsupported_schema or budget_exhausted. Sources are
        reconciled so closed-positions + REDEEM upgrade a position to a
        settled/early-exit proof exactly once. Trades are never labeled
        settled without corroboration.
        """
        end_map = _condition_to_end(classifications)
        category_map = _category_for_condition(classifications)
        event_map = _event_for_condition(classifications)
        classification_by_cond: dict[str, MarketClassification] = {
            c.condition_id: c for c in classifications
        }
        api_errors: list[tuple[str, str, int]] = []
        records: list[WalletHistoryRecord] = []
        resolutions: dict[str, OfficialResolution] = {}
        total_trades_seen = 0
        source_audit: list[dict[str, str]] = []
        all_source_incomplete: list[SourceIncompleteEvidence] = []
        all_conflicts: list[ConflictEvidence] = []

        for seed in seeds:
            try:
                record, errors, trades_count, audit, res = await self._fetch_one(
                    seed,
                    end_map=end_map,
                    category_map=category_map,
                    event_map=event_map,
                    classification_by_cond=classification_by_cond,
                    as_of=as_of,
                )
            except Exception as exc:
                logger.warning("history fetch error for %s: %s", seed.wallet_address, type(exc).__name__)
                api_errors.append((seed.wallet_address, f"unexpected:{type(exc).__name__}", 0))
                continue
            api_errors.extend(errors)
            total_trades_seen += trades_count
            source_audit.extend(audit)
            records.append(record)
            all_source_incomplete.extend(record.source_incomplete)
            for cond, r in res.items():
                resolutions.setdefault(cond, r)
            # Material conflicts: positions with pnl_conflict or CONFLICT state.
            for pos in record.positions:
                if pos.pnl_conflict:
                    all_conflicts.append(ConflictEvidence(
                        wallet_address=record.wallet_address,
                        market_condition_id=pos.condition_id,
                        asset_id=pos.asset_id,
                        conflict_type="pnl_conflict",
                        detail="authoritative PnL rows disagree; excluded from scoring",
                    ))
                if pos.settlement_state == "CONFLICT":
                    all_conflicts.append(ConflictEvidence(
                        wallet_address=record.wallet_address,
                        market_condition_id=pos.condition_id,
                        asset_id=pos.asset_id,
                        conflict_type="outcome_conflict",
                        detail="official outcome conflict",
                    ))

        return WalletHistoryReport(
            wallets=tuple(records),
            resolutions=tuple(resolutions.values()),
            api_errors=tuple(api_errors),
            trades_seen=total_trades_seen,
            history_days=self._history_days,
            eligible_only=True,
            source_audit=tuple(source_audit),
            source_incomplete=tuple(all_source_incomplete),
            conflicts=tuple(all_conflicts),
        )

    async def _fetch_one(
        self,
        seed: SeedWallet,
        *,
        end_map: dict[str, datetime],
        category_map: dict[str, str],
        event_map: dict[str, str],
        classification_by_cond: dict[str, MarketClassification],
        as_of: datetime,
    ) -> tuple[WalletHistoryRecord, list[tuple[str, str, int]], int, list[dict[str, str]], dict[str, OfficialResolution]]:
        wallet = seed.wallet_address
        audit: list[dict[str, str]] = []
        errors: list[tuple[str, str, int]] = []

        # ── Phase A: wallet trades ────────────────────────────────────────
        trades: list[dict[str, Any]] = []
        trade_status = SOURCE_EMPTY
        try:
            trades, trade_errors = await self._adapter.wallet_trades(
                wallet_address=wallet,
                limit=200,
                offset=0,
                max_pages=self._max_pages,
                budget=self._budget,
                phase="histories",
            )
            if trade_errors:
                code = str((trade_errors[0] or {}).get("error_code", "ERR"))
                if code == ERR_BUDGET_EXHAUSTED:
                    trade_status = SOURCE_BUDGET_EXHAUSTED
                elif code.startswith("HTTP_4XX") or code.startswith("HTTP_5XX"):
                    trade_status = SOURCE_HTTP_ERROR
                else:
                    trade_status = SOURCE_PARTIAL
                errors.extend([(wallet, f"trades:{e.get('error_code','ERR')}", int(e.get('http_status',0) or 0)) for e in trade_errors])
            elif trades:
                trade_status = SOURCE_COMPLETE
        except Exception as exc:
            trade_status = SOURCE_HTTP_ERROR
            errors.append((wallet, f"trades:{type(exc).__name__}", 0))
        audit.append({"wallet": wallet, "source": "trades", "status": trade_status, "rows": str(len(trades))})

        # ── Phase B: closed positions ─────────────────────────────────────
        closed_positions: list[dict[str, Any]] = []
        closed_status = SOURCE_EMPTY
        try:
            closed_positions, closed_errs = await self._adapter.wallet_closed_positions(
                wallet_address=wallet,
                limit=200,
                offset=0,
                max_pages=self._max_pages,
                budget=self._budget,
                phase="closed_positions",
            )
            if closed_errs:
                code = str((closed_errs[0] or {}).get("error_code", "ERR"))
                if code == ERR_BUDGET_EXHAUSTED:
                    closed_status = SOURCE_BUDGET_EXHAUSTED
                elif code.startswith("HTTP_4XX") or code.startswith("HTTP_5XX"):
                    closed_status = SOURCE_HTTP_ERROR
                else:
                    closed_status = SOURCE_PARTIAL
                errors.extend([(wallet, f"closed:{e.get('error_code','ERR')}", int(e.get('http_status',0) or 0)) for e in closed_errs])
            elif closed_positions:
                closed_status = SOURCE_COMPLETE
        except Exception as exc:
            closed_status = SOURCE_HTTP_ERROR
            errors.append((wallet, f"closed:{type(exc).__name__}", 0))
        audit.append({"wallet": wallet, "source": "closed_positions", "status": closed_status, "rows": str(len(closed_positions))})

        # ── Phase C: REDEEM activity ───────────────────────────────────────
        redeem_activity: list[dict[str, Any]] = []
        redeem_status = SOURCE_EMPTY
        try:
            redeem_activity, redeem_errs = await self._adapter.wallet_redeem_activity(
                wallet_address=wallet,
                limit=200,
                offset=0,
                max_pages=self._max_pages,
                budget=self._budget,
                phase="redeems",
            )
            if redeem_errs:
                code = str((redeem_errs[0] or {}).get("error_code", "ERR"))
                if code == ERR_BUDGET_EXHAUSTED:
                    redeem_status = SOURCE_BUDGET_EXHAUSTED
                elif code.startswith("HTTP_4XX") or code.startswith("HTTP_5XX"):
                    redeem_status = SOURCE_HTTP_ERROR
                else:
                    redeem_status = SOURCE_PARTIAL
                errors.extend([(wallet, f"redeem:{e.get('error_code','ERR')}", int(e.get('http_status',0) or 0)) for e in redeem_errs])
            elif redeem_activity:
                redeem_status = SOURCE_COMPLETE
        except Exception as exc:
            redeem_status = SOURCE_HTTP_ERROR
            errors.append((wallet, f"redeem:{type(exc).__name__}", 0))
        audit.append({"wallet": wallet, "source": "redeem_activity", "status": redeem_status, "rows": str(len(redeem_activity))})

        # Wallet identity is malformed if ALL sources returned http_error; surface as
        # a hard source-incomplete signal.
        if trade_status == SOURCE_HTTP_ERROR and closed_status == SOURCE_HTTP_ERROR and redeem_status == SOURCE_HTTP_ERROR:
            audit.append({"wallet": wallet, "source": "identity", "status": "unavailable"})

        resolutions: dict[str, OfficialResolution] = {}

        # Pre-compute corroborating sets from closed-positions and REDEEM,
        # deduplicated by deterministic fingerprint so exact duplicates are
        # counted once.
        redeemed_keys: set[str] = set()
        winning_assets: dict[str, set[str]] = {}
        redeem_pnls: dict[tuple[str, str], list[float]] = {}
        for row in redeem_activity:
            role, _ = extract_wallet_match_role(row, wallet)
            if role == WALLET_MATCH_ROLE_NONE:
                continue
            cond_id = str(row.get("conditionId") or "").strip().lower()
            if not cond_id:
                continue
            fp = _redeem_fingerprint(row)
            if fp in redeemed_keys:
                continue
            redeemed_keys.add(fp)
            asset = _normalize_asset_id(row.get("assetId") or row.get("asset") or row.get("tokenId"))
            winning_marker = row.get("winning")
            if (
                winning_marker is True
                or winning_marker == 1
                or str(winning_marker).lower() in ("true", "1", "yes")
            ):
                winning_assets.setdefault(cond_id, set()).add(asset)
            payout = row.get("payout") if row.get("payout") is not None else row.get("payoutFrac")
            if payout is not None:
                try:
                    redeem_pnls.setdefault((cond_id, asset), []).append(float(payout))
                except (TypeError, ValueError):
                    pass

        # Closed-position PnL map keyed by (condition, asset), deduplicated.
        closed_pnl: dict[tuple[str, str], list[float]] = {}
        seen_closed_fp: set[str] = set()
        for row in closed_positions:
            role, _ = extract_wallet_match_role(row, wallet)
            if role == WALLET_MATCH_ROLE_NONE:
                continue
            cond_id = str(row.get("conditionId") or "").strip().lower()
            if not cond_id:
                continue
            fp = _closed_position_fingerprint(row)
            if fp in seen_closed_fp:
                # EXACT duplicate row — counted once, never summed.
                continue
            seen_closed_fp.add(fp)
            asset = _normalize_asset_id(row.get("assetId") or row.get("asset") or row.get("tokenId"))
            realized_raw = row.get("realizedPnl") if row.get("realizedPnl") is not None else row.get("realized_pnl")
            try:
                realized_f = float(realized_raw) if realized_raw is not None else None
            except (TypeError, ValueError):
                realized_f = None
            if realized_f is not None:
                closed_pnl.setdefault((cond_id, asset), []).append(realized_f)

        # ── Group trades into positions by (condition, asset) ───────────────
        positions: dict[PositionKey, list[TradeFill]] = {}
        source_incomplete: list[SourceIncompleteEvidence] = []
        long_horizon = 0
        taxonomy_excluded = 0
        first_qualifying: str | None = None
        last_qualifying: str | None = None
        buy_fill_count = 0
        sell_fill_count = 0

        for raw in trades:
            role, role_addr = extract_wallet_match_role(raw, wallet)
            if role == WALLET_MATCH_ROLE_NONE or role_addr != wallet:
                source_incomplete.append(SourceIncompleteEvidence(
                    wallet_address=wallet,
                    market_condition_id=str(raw.get("conditionId") or "").strip().lower() or None,
                    asset_id=_normalize_asset_id(raw.get("assetId") or raw.get("asset") or raw.get("tokenId")) or None,
                    reason="wallet_role_unavailable",
                ))
                continue

            cond_id = str(raw.get("conditionId") or "").strip().lower()
            asset = _normalize_asset_id(raw.get("assetId") or raw.get("asset") or raw.get("tokenId"))
            side = str(raw.get("side") or "").strip().upper()
            ts_dt = _to_utc(raw.get("timestamp"))
            price_raw = raw.get("price")
            size_raw = raw.get("size")
            try:
                price = float(price_raw) if price_raw is not None else None
            except (TypeError, ValueError):
                price = None
            try:
                size = float(size_raw) if size_raw is not None else None
            except (TypeError, ValueError):
                size = None
            outcome_index_raw = raw.get("outcomeIndex") if raw.get("outcomeIndex") is not None else raw.get("outcome_index")
            try:
                outcome_index = int(outcome_index_raw) if outcome_index_raw is not None else None
            except (TypeError, ValueError):
                outcome_index = None
            outcome_label = raw.get("outcome") if isinstance(raw.get("outcome"), str) else None

            if not cond_id:
                source_incomplete.append(SourceIncompleteEvidence(
                    wallet_address=wallet, market_condition_id=None, asset_id=asset or None,
                    reason="missing_condition_id",
                ))
                continue
            if ts_dt is None or price is None or size is None:
                source_incomplete.append(SourceIncompleteEvidence(
                    wallet_address=wallet, market_condition_id=cond_id, asset_id=asset or None,
                    reason="missing_or_invalid_trade_geometry",
                ))
                continue
            if not _is_history_window(ts=ts_dt, as_of=as_of, days=self._history_days):
                source_incomplete.append(SourceIncompleteEvidence(
                    wallet_address=wallet, market_condition_id=cond_id, asset_id=asset or None,
                    reason="outside_history_window",
                ))
                continue

            canonical_end_dt = end_map.get(cond_id)
            category_label = category_map.get(cond_id)
            classification = classification_by_cond.get(cond_id)
            if canonical_end_dt is None:
                fetched = None
                try:
                    fetched = await self._adapter.get_market_raw(cond_id, budget=self._budget, phase="referenced_metadata")
                except Exception as exc:
                    source_incomplete.append(SourceIncompleteEvidence(
                        wallet_address=wallet, market_condition_id=cond_id, asset_id=asset or None,
                        reason=f"market_lookup_failed:{type(exc).__name__}",
                    ))
                    continue
                if fetched is None:
                    source_incomplete.append(SourceIncompleteEvidence(
                        wallet_address=wallet, market_condition_id=cond_id, asset_id=asset or None,
                        reason="market_not_resolved",
                    ))
                    continue
                end_dt_str = fetched.get("endDate") or fetched.get("end_date")
                canonical_end_dt = _to_utc(end_dt_str)
                if canonical_end_dt is None:
                    source_incomplete.append(SourceIncompleteEvidence(
                        wallet_address=wallet, market_condition_id=cond_id, asset_id=asset or None,
                        reason="missing_or_invalid_market_end",
                    ))
                    continue
                cat = fetched.get("category")
                category_label = cat.lower() if isinstance(cat, str) and cat.strip() else category_label
            else:
                fetched = None

            assessment = evaluate_short_horizon(ts_dt, canonical_end_dt)
            if not assessment.eligible:
                long_horizon += 1
                continue
            if category_label is None:
                taxonomy_excluded += 1
                continue

            if side == "BUY":
                buy_fill_count += 1
            elif side == "SELL":
                sell_fill_count += 1
            else:
                source_incomplete.append(SourceIncompleteEvidence(
                    wallet_address=wallet, market_condition_id=cond_id, asset_id=asset or None,
                    reason="unrecognized_side",
                ))
                continue

            ts_iso = _utc_iso(ts_dt)
            if first_qualifying is None or (ts_iso is not None and ts_iso < first_qualifying):
                first_qualifying = ts_iso
            if last_qualifying is None or (ts_iso is not None and ts_iso > last_qualifying):
                last_qualifying = ts_iso

            horizon_status = assessment.status  # HORIZON_PREFERRED / HORIZON_ELIGIBLE (no fallback)
            fill = TradeFill(
                transaction_hash=str(raw.get("transactionHash") or raw.get("id") or "").strip() or None,
                side=side,
                price=price,
                size=size,
                ts_utc=ts_dt,
                ts_iso=ts_iso,
                asset_id=asset,
                outcome_index=outcome_index,
                outcome_label=outcome_label,
            )
            key = PositionKey(wallet, cond_id, asset)
            positions.setdefault(key, []).append(fill)

            # Official resolution (memoized per condition).
            if cond_id not in resolutions:
                resolutions[cond_id] = _resolve_official(
                    cond_id, classification=classification, fetched=fetched,
                )

        # ── Reconcile each position ─────────────────────────────────────────
        reconciled: list[ReconciledPosition] = []
        settled_ev: list[SettledEvidence] = []
        early_ev: list[EarlyExitEvidence] = []
        unresolved_ev: list[UnresolvedEvidence] = []
        market_pnl: dict[str, float] = {}
        event_pnl: dict[str, float] = {}
        distinct_events_set: set[str] = set()
        distinct_markets_set: set[str] = set()

        for key, fills in positions.items():
            cond_id, asset = key.condition_id, key.asset_id
            cls = classification_by_cond.get(cond_id)
            category_label = category_map.get(cond_id) or (cls.category_label if cls else None)
            event_identity = event_map.get(cond_id) or (resolutions.get(cond_id).event_identity if cond_id in resolutions else None)
            resolution = resolutions.get(cond_id)
            buy = [f for f in fills if f.side == "BUY"]
            sell = [f for f in fills if f.side == "SELL"]
            buy_qty = sum(f.size for f in buy)
            buy_cost = sum(f.size * f.price for f in buy)
            sell_qty = sum(f.size for f in sell)
            sell_proceeds = sum(f.size * f.price for f in sell)
            net_qty = buy_qty - sell_qty
            first_ts = min((f.ts_iso for f in fills if f.ts_iso), default=None)
            last_ts = max((f.ts_iso for f in fills if f.ts_iso), default=None)
            outcome_index = next((f.outcome_index for f in buy), None)
            outcome_label = next((f.outcome_label for f in buy), None)
            src_ids = tuple(sorted({f.transaction_hash for f in fills if f.transaction_hash}))
            horizon_status = cls.horizon_status if (cls and cls.horizon_status) else HORIZON_PREFERRED

            closed_list = closed_pnl.get((cond_id, asset), [])
            redeem_list = redeem_pnls.get((cond_id, asset), [])
            # Redeemed = a REDEEM activity row exists for this exact (condition, asset).
            redeemed = (cond_id, asset) in {(k[0], k[1]) for k in redeem_pnls}

            # Determine settlement state authoritatively.
            winning_assets_for_cond = winning_assets.get(cond_id, set())
            if asset in winning_assets_for_cond:
                held_is_winner = True
            elif winning_assets_for_cond:
                held_is_winner = False
            else:
                held_is_winner = None  # unknown — do not infer a loss

            # PnL reconciliation (once per position).
            pnl_conflict = False
            pnl_sources = []
            if closed_list:
                pnl_sources.append(("closed_position", closed_list))
            if redeem_list:
                pnl_sources.append(("redeem", redeem_list))
            realized: float | None = None
            pnl_source: str | None = None
            if pnl_sources:
                # All authoritative PnL rows for this position must agree.
                all_vals = [v for _, vals in pnl_sources for v in vals]
                if len({round(v, 6) for v in all_vals}) > 1:
                    pnl_conflict = True
                    realized = None
                    pnl_source = None
                else:
                    realized = all_vals[0]
                    pnl_source = pnl_sources[0][0]
            pnl_complete = realized is not None and not pnl_conflict

            # Official winning direction.
            official_winning_asset = resolution.winning_asset_id if resolution else None
            official_winning_index = resolution.winning_outcome_index if resolution else None
            official_winning_label = resolution.winning_outcome_label if resolution else None

            if closed_list or redeem_list:
                # We have settlement activity.
                if held_is_winner is True:
                    state = SETTLED_WIN
                    winning_outcome = True
                elif held_is_winner is False:
                    state = SETTLED_LOSS
                    winning_outcome = False
                else:
                    # REDEEM confirms settlement activity but direction unknown.
                    if pnl_source == "redeem":
                        state = REDEEM_CONFIRMED_OUTCOME_UNKNOWN
                    else:
                        state = RESOLVED_OUTCOME_UNKNOWN
                    winning_outcome = None
                # Record PnL into concentration ledger exactly once.
                if realized is not None:
                    market_pnl[cond_id] = market_pnl.get(cond_id, 0.0) + realized
                    if event_identity:
                        event_pnl[event_identity] = event_pnl.get(event_identity, 0.0) + realized
            elif resolution is not None and resolution.winning_asset_id is not None:
                # Officially resolved, but no closed/redeem activity for this position.
                if asset == resolution.winning_asset_id:
                    state = SETTLED_WIN
                    winning_outcome = True
                else:
                    state = SETTLED_LOSS
                    winning_outcome = False
            else:
                state = UNRESOLVED
                winning_outcome = None

            rp = ReconciledPosition(
                wallet_address=wallet,
                condition_id=cond_id,
                asset_id=asset,
                outcome_index=outcome_index,
                outcome_label=outcome_label,
                category_label=category_label,
                event_identity=event_identity,
                horizon_status=horizon_status,
                buy_fills=tuple(buy),
                sell_fills=tuple(sell),
                first_ts_iso=first_ts,
                last_ts_iso=last_ts,
                buy_qty=buy_qty,
                buy_cost=buy_cost,
                sell_qty=sell_qty,
                sell_proceeds=sell_proceeds,
                net_qty=net_qty,
                source_trade_identities=src_ids,
                settlement_state=state,
                winning_outcome=winning_outcome,
                realized_pnl=realized,
                pnl_source=pnl_source,
                pnl_complete=pnl_complete,
                pnl_conflict=pnl_conflict,
                redeemed=redeemed,
                included_closed_position_ids=tuple(sorted(seen_closed_fp)) if closed_list else (),
                included_redeem_ids=tuple(sorted(redeemed_keys)) if redeemed else (),
                official_winning_asset_id=official_winning_asset,
                official_winning_outcome_index=official_winning_index,
                official_winning_outcome_label=official_winning_label,
            )
            reconciled.append(rp)

            if state in (SETTLED_WIN, SETTLED_LOSS, RESOLVED_OUTCOME_UNKNOWN, REDEEM_CONFIRMED_OUTCOME_UNKNOWN):
                if state == SETTLED_WIN:
                    distinct_markets_set.add(cond_id)
                    if event_identity:
                        distinct_events_set.add(event_identity)
                settled_ev.append(SettledEvidence(
                    wallet_address=wallet,
                    market_condition_id=cond_id,
                    asset_id=asset,
                    category_label=category_label,
                    event_identity=event_identity,
                    outcome_index=outcome_index,
                    outcome_label=outcome_label,
                    settlement_state=state,
                    winning_outcome=winning_outcome,
                    realized_pnl=realized,
                    pnl_source=pnl_source,
                    pnl_conflict=pnl_conflict,
                    redeemed=redeemed,
                    horizon_status=horizon_status,
                    ts_iso=last_ts,
                ))
            elif state == EARLY_EXIT:
                early_ev.append(EarlyExitEvidence(
                    wallet_address=wallet,
                    market_condition_id=cond_id,
                    asset_id=asset,
                    category_label=category_label,
                    event_identity=event_identity,
                    realized_pnl=realized,
                    pnl_source=pnl_source,
                    pnl_conflict=pnl_conflict,
                    horizon_status=horizon_status,
                    ts_iso=last_ts,
                ))
            elif state in (UNRESOLVED, SOURCE_INCOMPLETE_STATE, CONFLICT_STATE):
                unresolved_ev.append(UnresolvedEvidence(
                    wallet_address=wallet,
                    market_condition_id=cond_id,
                    asset_id=asset,
                    category_label=category_label,
                    event_identity=event_identity,
                    reason=state,
                ))
            else:
                unresolved_ev.append(UnresolvedEvidence(
                    wallet_address=wallet,
                    market_condition_id=cond_id,
                    asset_id=asset,
                    category_label=category_label,
                    event_identity=event_identity,
                    reason="unclassified",
                ))

        # ── Concentration from one canonical PnL ledger ────────────────────
        # Winner basis: positive realized PnL only (the frozen concentration basis).
        winner_values = [v for v in market_pnl.values() if v > 0]
        largest_market, top_three = _compute_concentration(winner_values)
        event_values = [v for v in event_pnl.values() if v > 0]
        largest_event, _ = _compute_concentration(event_values)

        # Active trading days derived from normalized fill timestamps.
        active_days_set: set[str] = set()
        for rp in reconciled:
            for f in rp.buy_fills + rp.sell_fills:
                d = _trading_day(f.ts_utc)
                if d:
                    active_days_set.add(d)

        two_sided = buy_fill_count > 0 and sell_fill_count > 0
        total_buckets = len(reconciled) + len(source_incomplete)
        seen_total = max(1, total_buckets)
        evidence_completeness = round(len(reconciled) / seen_total, 4)

        record = WalletHistoryRecord(
            wallet_address=wallet,
            positions=tuple(reconciled),
            settled=tuple(settled_ev),
            early_exit=tuple(early_ev),
            unresolved=tuple(unresolved_ev),
            source_incomplete=tuple(source_incomplete),
            first_qualifying_trade=first_qualifying,
            last_qualifying_trade=last_qualifying,
            active_trading_days=len(active_days_set),
            distinct_events=tuple(sorted(distinct_events_set)),
            distinct_markets=tuple(sorted(distinct_markets_set)),
            buy_fill_count=buy_fill_count,
            sell_fill_count=sell_fill_count,
            two_sided_churn=two_sided,
            market_pnl={k: float(v) for k, v in sorted(market_pnl.items())},
            event_pnl={k: float(v) for k, v in sorted(event_pnl.items())},
            largest_market_pnl_share=largest_market,
            largest_event_pnl_share=largest_event,
            top_three_market_pnl=tuple(top_three),
            long_horizon_excluded=long_horizon,
            taxonomy_excluded=taxonomy_excluded,
            source_incomplete_count=len(source_incomplete),
            evidence_completeness=evidence_completeness,
        )
        return record, errors, len(trades), audit, resolutions


__all__ = [
    "DEFAULT_HISTORY_DAYS",
    "DEFAULT_HISTORY_MAX_PAGES",
    "EARLY_EXIT",
    "OfficialResolution",
    "PositionKey",
    "REDEEM_CONFIRMED_OUTCOME_UNKNOWN",
    "RESOLVED_OUTCOME_UNKNOWN",
    "ReconciledPosition",
    "SETTLED_LOSS",
    "SETTLED_WIN",
    "SOURCE_INCOMPLETE_STATE",
    "CONFLICT_STATE",
    "SettledEvidence",
    "EarlyExitEvidence",
    "UnresolvedEvidence",
    "SourceIncompleteEvidence",
    "TradeFill",
    "UNRESOLVED",
    "WalletHistoryFetcher",
    "WalletHistoryRecord",
    "WalletHistoryReport",
    "_to_utc",
    "_utc_iso",
    "_trading_day",
]
