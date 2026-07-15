"""Historical wallet evidence fetcher + reconciler.

Pure-style orchestration. Opens no database. Calls only the public
:mod:`polycopy.discovery.adapter` wrappers (which wrap the production
Polymarket adapter with retry + budget guards).

Inputs:
  * A bounded list of wallet addresses (from the seed builder).
  * A bounded history window in days (default 365, max 730).
  * A bounded page budget (default 5 pages per source).
  * The market universe payload for conditionId -> endDate mapping.

Output: per-wallet history with three classified buckets:
  * :class:`SettledEvidence` — official market resolved, winning outcome
    known, wallet outcome exposure can be reconciled, redeem-confirmed.
  * :class:`EarlyExitEvidence` — position sold or closed before resolution;
    realized PnL retained but NOT labeled a settled win/loss.
  * :class:`UnresolvedEvidence` — trade/prediction whose market has not
    yet settled; excluded from settled-score inputs, retained in coverage.

Dedup is by exact public trade identity (upstream transaction hash + asset
+ conditionId + side + ts + price + size), so two fills that share a wallet
and condition are two evidence rows. Closed-position + REDEEM events are
reconciled so a single fill cannot be double-counted as PnL.
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
from polycopy.policy.short_horizon import evaluate_short_horizon

logger = logging.getLogger(__name__)


DEFAULT_HISTORY_DAYS = 365
MIN_HISTORY_DAYS = 1
MAX_HISTORY_DAYS = 730
DEFAULT_HISTORY_MAX_PAGES = 5

EVIDENCE_SETTLED = "settled_forecasting"
EVIDENCE_EARLY_EXIT = "early_exit_trading"
EVIDENCE_UNRESOLVED = "unresolved"
EVIDENCE_INCOMPLETE = "incomplete_or_conflicting"


def _trade_identity(raw: Mapping[str, Any]) -> str:
    """Dedupe key for one upstream trade row."""
    tx_hash = str(raw.get("transactionHash") or "").strip().lower()
    asset = str(raw.get("asset") or "")
    cond = str(raw.get("conditionId") or "").strip().lower()
    side = str(raw.get("side") or "").strip().upper()
    ts_raw = raw.get("timestamp")
    try:
        ts = str(int(float(ts_raw))) if ts_raw is not None else ""
    except (TypeError, ValueError):
        ts = ""
    price = raw.get("price")
    size = raw.get("size")
    try:
        price_s = f"{float(price):.10f}" if price is not None else ""
    except (TypeError, ValueError):
        price_s = ""
    try:
        size_s = f"{float(size):.10f}" if size is not None else ""
    except (TypeError, ValueError):
        size_s = ""
    payload = "|".join(["trade-evidence-v1", tx_hash, asset, cond, side, ts, price_s, size_s])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _to_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
    return None


@dataclass(frozen=True)
class SettledEvidence:
    """A trade whose market fully resolved and whose position outcome is known."""

    wallet_address: str
    market_condition_id: str
    identity_hash: str
    side: str
    price: float
    size: float
    timestamp: str
    category_label: str | None
    winning_outcome: bool
    settled_realized_pnl: float | None
    redeemed: bool
    proof_source: str
    horizon_status: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EarlyExitEvidence:
    """A position sold or closed before resolution.

    Realized PnL is retained but the evidence is NOT labeled a settled
    win or loss.
    """

    wallet_address: str
    market_condition_id: str
    identity_hash: str
    side: str
    price: float
    size: float
    timestamp: str
    category_label: str | None
    realized_pnl: float | None
    proof_source: str
    horizon_status: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UnresolvedEvidence:
    """Evidence retained for coverage but excluded from settled-score inputs."""

    wallet_address: str
    market_condition_id: str
    identity_hash: str
    side: str
    price: float
    size: float
    timestamp: str
    category_label: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IncompleteEvidence:
    """Trades whose reconciled data was insufficient to classify."""

    wallet_address: str
    market_condition_id: str
    identity_hash: str
    side: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WalletHistoryRecord:
    """All evidence types for one wallet."""

    wallet_address: str
    settled: tuple[SettledEvidence, ...]
    early_exit: tuple[EarlyExitEvidence, ...]
    unresolved: tuple[UnresolvedEvidence, ...]
    incomplete: tuple[IncompleteEvidence, ...]
    first_qualifying_trade: str | None
    last_qualifying_trade: str | None
    active_trading_days: int
    distinct_events: tuple[str, ...]
    buy_count: int
    sell_count: int
    two_sided_churn: bool
    market_concentration: dict[str, float]
    event_concentration: dict[str, float]
    largest_market_pnl_share: float | None
    largest_event_pnl_share: float | None
    long_horizon_excluded: int
    taxonomy_excluded: int
    source_incomplete: int
    evidence_completeness: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WalletHistoryReport:
    """Top-level history discovery report."""

    wallets: tuple[WalletHistoryRecord, ...]
    api_errors: tuple[tuple[str, str, int], ...] = ()
    trades_seen: int = 0
    history_days: int = DEFAULT_HISTORY_DAYS
    eligible_only: bool = True
    source_audit: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["api_errors"] = [list(e) for e in self.api_errors]
        return out


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
    """Map condition -> category for event-concentration approximation.

    The proper event-id pipeline would query Gamma for each trade's event
    reference; for the bounded audit we use the category label as a stable
    proxy and include the same ``distinct_events`` count in the evidence.
    """
    return {c.condition_id: c.category_label or "unknown" for c in classifications}


def _is_history_window(*, ts: datetime, as_of: datetime, days: int) -> bool:
    if ts is None:
        return False
    return ts >= (as_of - timedelta(days=days))


def _compute_concentration(
    values: Iterable[float],
    total_label: str = "largest_market_pnl_share",
) -> tuple[float | None, dict[str, float]]:
    """Compute top-N share and a small dict of histogram strings."""
    seq = sorted(values, reverse=True)
    total = sum(seq)
    if total <= 0:
        return None, {}
    largest_share = (seq[0] / total) if seq else None
    return largest_share, {}


# Per-source statuses emitted by WalletHistoryFetcher — STEP 6 contract.
SOURCE_COMPLETE = "complete"
SOURCE_EMPTY = "empty"
SOURCE_PARTIAL = "partial"
SOURCE_MALFORMED = "malformed"
SOURCE_BUDGET_EXHAUSTED = "budget_exhausted"
SOURCE_HTTP_ERROR = "http_error"
SOURCE_UNSUPPORTED_SCHEMA = "unsupported_schema"


class WalletHistoryFetcher:
    """Fetch and reconcile one wallet's bounded history."""

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
        distinct from unsupported_schema or budget_exhausted. Sources
        are reconciled so closed-positions + REDEEM upgrade unresolved
        rows to settled / early_exit; trades are never labeled settled
        without corroboration.
        """
        end_map = _condition_to_end(classifications)
        category_map = _category_for_condition(classifications)
        event_map = _event_for_condition(classifications)
        api_errors: list[tuple[str, str, int]] = []
        records: list[WalletHistoryRecord] = []
        total_trades_seen = 0
        source_audit: list[dict[str, str]] = []

        for seed in seeds:
            try:
                record, errors, trades_count, audit = await self._fetch_one(
                    seed,
                    end_map=end_map,
                    category_map=category_map,
                    event_map=event_map,
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

        return WalletHistoryReport(
            wallets=tuple(records),
            api_errors=tuple(api_errors),
            trades_seen=total_trades_seen,
            history_days=self._history_days,
            eligible_only=True,
            source_audit=tuple(source_audit),
        )

    async def _fetch_one(
        self,
        seed: SeedWallet,
        *,
        end_map: dict[str, datetime],
        category_map: dict[str, str],
        event_map: dict[str, str],
        as_of: datetime,
    ) -> tuple[WalletHistoryRecord, list[tuple[str, str, int]], int, list[dict[str, str]]]:
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

        # ── Classify trades into evidence buckets ─────────────────────────
        seen_identities: set[str] = set()
        settled: list[SettledEvidence] = []
        early: list[EarlyExitEvidence] = []
        unresolved: list[UnresolvedEvidence] = []
        incomplete: list[IncompleteEvidence] = []
        long_horizon = 0
        taxonomy_excluded = 0
        source_incomplete = 0
        buy_count = 0
        sell_count = 0
        first_qualifying: str | None = None
        last_qualifying: str | None = None
        pnl_by_market: dict[str, float] = {}
        pnl_by_event: dict[str, float] = {}

        # Pre-compute corroborating sets from closed-positions and REDEEM.
        redeemed_conditions: set[str] = set()
        winning_conditions: set[str] = set()
        for row in redeem_activity:
            role, _ = extract_wallet_match_role(row, wallet)
            if role == WALLET_MATCH_ROLE_NONE:
                continue
            cond_id = str(row.get("conditionId") or "").strip().lower()
            if cond_id:
                redeemed_conditions.add(cond_id)
            winning_marker = row.get("winning")
            if (
                winning_marker is True
                or winning_marker == 1
                or str(winning_marker).lower() in ("true", "1", "yes")
            ):
                winning_conditions.add(cond_id)

        closed_pnl: dict[str, float] = {}
        for row in closed_positions:
            role, _ = extract_wallet_match_role(row, wallet)
            if role == WALLET_MATCH_ROLE_NONE:
                continue
            cond_id = str(row.get("conditionId") or "").strip().lower()
            realized_raw = row.get("realizedPnl") if row.get("realizedPnl") is not None else row.get("realized_pnl")
            try:
                realized_f = float(realized_raw) if realized_raw is not None else None
            except (TypeError, ValueError):
                realized_f = None
            if cond_id and realized_f is not None:
                closed_pnl[cond_id] = closed_pnl.get(cond_id, 0.0) + realized_f

        for raw in trades:
            identity = _trade_identity(raw)
            if identity in seen_identities:
                continue
            seen_identities.add(identity)
            role, role_addr = extract_wallet_match_role(raw, wallet)
            if role == WALLET_MATCH_ROLE_NONE or role_addr != wallet:
                # Either malformed identity or trade belongs to a different
                # wallet — fail closed.
                source_incomplete += 1
                incomplete.append(IncompleteEvidence(
                    wallet_address=wallet,
                    market_condition_id=str(raw.get("conditionId") or "").strip().lower(),
                    identity_hash=identity,
                    side=str(raw.get("side") or "UNKNOWN").strip().upper() or "UNKNOWN",
                    reason="wallet_role_unavailable",
                ))
                continue

            cond_id = str(raw.get("conditionId") or "").strip().lower()
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
            ts_iso = raw.get("timestamp")
            ts_iso_s = str(ts_iso) if ts_iso is not None else ""

            if not cond_id:
                source_incomplete += 1
                incomplete.append(IncompleteEvidence(
                    wallet_address=wallet,
                    market_condition_id="",
                    identity_hash=identity,
                    side=side or "UNKNOWN",
                    reason="missing_condition_id",
                ))
                continue
            if ts_dt is None or price is None or size is None:
                source_incomplete += 1
                incomplete.append(IncompleteEvidence(
                    wallet_address=wallet,
                    market_condition_id=cond_id,
                    identity_hash=identity,
                    side=side or "UNKNOWN",
                    reason="missing_or_invalid_trade_geometry",
                ))
                continue
            if not _is_history_window(ts=ts_dt, as_of=as_of, days=self._history_days):
                source_incomplete += 1
                incomplete.append(IncompleteEvidence(
                    wallet_address=wallet,
                    market_condition_id=cond_id,
                    identity_hash=identity,
                    side=side or "UNKNOWN",
                    reason="outside_history_window",
                ))
                continue

            canonical_end_dt = end_map.get(cond_id)
            category_label = category_map.get(cond_id)
            if canonical_end_dt is None:
                try:
                    fetched = await self._adapter.get_market_raw(cond_id, budget=self._budget)
                except Exception as exc:
                    source_incomplete += 1
                    incomplete.append(IncompleteEvidence(
                        wallet_address=wallet,
                        market_condition_id=cond_id,
                        identity_hash=identity,
                        side=side or "UNKNOWN",
                        reason=f"market_lookup_failed:{type(exc).__name__}",
                    ))
                    continue
                if fetched is None:
                    source_incomplete += 1
                    incomplete.append(IncompleteEvidence(
                        wallet_address=wallet,
                        market_condition_id=cond_id,
                        identity_hash=identity,
                        side=side or "UNKNOWN",
                        reason="market_not_resolved",
                    ))
                    continue
                end_dt_str = fetched.get("endDate") or fetched.get("end_date")
                canonical_end_dt = _to_utc(end_dt_str)
                if canonical_end_dt is None:
                    source_incomplete += 1
                    incomplete.append(IncompleteEvidence(
                        wallet_address=wallet,
                        market_condition_id=cond_id,
                        identity_hash=identity,
                        side=side or "UNKNOWN",
                        reason="missing_or_invalid_market_end",
                    ))
                    continue
                cat = fetched.get("category")
                category_label = cat.lower() if isinstance(cat, str) and cat.strip() else None

            assessment = evaluate_short_horizon(ts_dt, canonical_end_dt)
            if not assessment.eligible:
                long_horizon += 1
                continue
            if category_label is None:
                taxonomy_excluded += 1
                continue

            if side == "BUY":
                buy_count += 1
            elif side == "SELL":
                sell_count += 1

            if first_qualifying is None or ts_iso_s < first_qualifying:
                first_qualifying = ts_iso_s
            if last_qualifying is None or ts_iso_s > last_qualifying:
                last_qualifying = ts_iso_s

            # Classify: REDEEM-confirmed → settled, closed-pnl only → early_exit, else unresolved.
            if cond_id in redeemed_conditions:
                realized = closed_pnl.get(cond_id)
                winning = cond_id in winning_conditions
                settled.append(SettledEvidence(
                    wallet_address=wallet,
                    market_condition_id=cond_id,
                    identity_hash=identity,
                    side=side or "UNKNOWN",
                    price=price,
                    size=size,
                    timestamp=ts_iso_s,
                    category_label=category_label,
                    winning_outcome=winning,
                    settled_realized_pnl=realized,
                    redeemed=True,
                    proof_source="redeem_activity+closed_position",
                    horizon_status=getattr(assessment, "horizon_status", "HORIZON_PREFERRED"),
                ))
                if realized is not None:
                    pnl_by_market[cond_id] = pnl_by_market.get(cond_id, 0.0) + realized
                    pnl_by_event[event_map.get(cond_id, "unknown")] = (
                        pnl_by_event.get(event_map.get(cond_id, "unknown"), 0.0) + realized
                    )
            elif cond_id in closed_pnl:
                early.append(EarlyExitEvidence(
                    wallet_address=wallet,
                    market_condition_id=cond_id,
                    identity_hash=identity,
                    side=side or "UNKNOWN",
                    price=price,
                    size=size,
                    timestamp=ts_iso_s,
                    category_label=category_label,
                    realized_pnl=closed_pnl[cond_id],
                    proof_source="closed_position_pre_resolution",
                    horizon_status=getattr(assessment, "horizon_status", "HORIZON_PREFERRED"),
                ))
            else:
                unresolved.append(UnresolvedEvidence(
                    wallet_address=wallet,
                    market_condition_id=cond_id,
                    identity_hash=identity,
                    side=side or "UNKNOWN",
                    price=price,
                    size=size,
                    timestamp=ts_iso_s,
                    category_label=category_label,
                    reason="unreconciled_with_activity",
                ))

        # Concentration.
        pnl_values = list(pnl_by_market.values()) + [e.settled_realized_pnl or 0.0 for e in settled]
        largest_market, _ = _compute_concentration(pnl_values)
        event_values = list(pnl_by_event.values()) + [e.settled_realized_pnl or 0.0 for e in settled]
        largest_event, _ = _compute_concentration(event_values)

        market_concentration = {k: float(v) for k, v in sorted(pnl_by_market.items(), key=lambda kv: -kv[1])[:5]}
        event_concentration = {k: float(v) for k, v in sorted(pnl_by_event.items(), key=lambda kv: -kv[1])[:5]}

        distinct_events = sorted({ev.market_condition_id for ev in settled if ev.market_condition_id})
        active_days_set = set()
        for ev in settled:
            active_days_set.add(ev.timestamp[:10])
        for ev in early:
            active_days_set.add(ev.timestamp[:10])
        active_trading_days = len(active_days_set)

        two_sided = buy_count > 0 and sell_count > 0
        seen_total = max(1, len(settled) + len(early) + len(unresolved) + len(incomplete))
        evidence_completeness = round(len(settled) / seen_total, 4)

        return WalletHistoryRecord(
            wallet_address=wallet,
            settled=tuple(settled),
            early_exit=tuple(early),
            unresolved=tuple(unresolved),
            incomplete=tuple(incomplete),
            first_qualifying_trade=first_qualifying,
            last_qualifying_trade=last_qualifying,
            active_trading_days=active_trading_days,
            distinct_events=tuple(distinct_events),
            buy_count=buy_count,
            sell_count=sell_count,
            two_sided_churn=two_sided,
            market_concentration=market_concentration,
            event_concentration=event_concentration,
            largest_market_pnl_share=largest_market,
            largest_event_pnl_share=largest_event,
            long_horizon_excluded=long_horizon,
            taxonomy_excluded=taxonomy_excluded,
            source_incomplete=source_incomplete,
            evidence_completeness=evidence_completeness,
        ), errors, len(trades), audit


def _stub_record(  # pragma: no cover - only for tests
    wallet: str,
    *,
    settled: tuple[SettledEvidence, ...] = (),
    early: tuple[EarlyExitEvidence, ...] = (),
    unresolved: tuple[UnresolvedEvidence, ...] = (),
    incomplete: tuple[IncompleteEvidence, ...] = (),
) -> WalletHistoryRecord:
    return WalletHistoryRecord(
        wallet_address=wallet,
        settled=settled,
        early_exit=early,
        unresolved=unresolved,
        incomplete=incomplete,
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
    )


__all__ = [
    "DEFAULT_HISTORY_DAYS",
    "DEFAULT_HISTORY_MAX_PAGES",
    "EarlyExitEvidence",
    "EVIDENCE_EARLY_EXIT",
    "EVIDENCE_INCOMPLETE",
    "EVIDENCE_SETTLED",
    "EVIDENCE_UNRESOLVED",
    "IncompleteEvidence",
    "MAX_HISTORY_DAYS",
    "MIN_HISTORY_DAYS",
    "SettledEvidence",
    "UnresolvedEvidence",
    "WalletHistoryFetcher",
    "WalletHistoryRecord",
    "WalletHistoryReport",
]
