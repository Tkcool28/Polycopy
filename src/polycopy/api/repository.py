"""Persistent read repository for Polycopy API routes.

FastAPI handlers should stay thin: this module owns SQLite access, row mapping,
and the explicit demo-mode fallback rules for dashboard read endpoints.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from polycopy.api.responses import (
    DataHealthResponse,
    DecisionLogResponse,
    DecisionLogView,
    ExperimentMetricsResponse,
    ExperimentMetricView,
    OrderView,
    OrdersResponse,
    PortfolioSummary,
    PositionView,
    PositionsResponse,
    RiskConsoleResponse,
    RiskGateView,
    ScanResponse,
    ScanResult,
    SignalView,
    SignalsResponse,
    SourceHealthView,
    WalletBalanceView,
    WalletDetailView,
    WalletsResponse,
)
from polycopy.config.settings import Settings
from polycopy.db.database import Database, get_database
from polycopy.db.wallet_identity import (
    address_is_sentinel_params,
    address_is_sentinel_sql,
    is_sentinel_trader_address,
)

SAMPLE_LABEL = "DEMO DATA / SAMPLE DATA"

# Single source of truth for the SQL predicate that mirrors
# ``is_sentinel_trader_address`` in :mod:`polycopy.domain.source_trade`.
# The Python helper checks (None, non-string, whitespace-only, case-insensitive
# match against ``LEGACY_TRADER_ADDRESS_SENTINELS`` after stripping ASCII
# whitespace). The SQL fragment is built once from
# :func:`polycopy.db.wallet_identity.address_is_sentinel_sql` so EVERY
# path (repository scans/wallets, migration cleanup, run_scan loader,
# collect_smart_money dedup, live smoke) agrees byte-for-byte. Editing
# the predicate in one place updates all of them.
_SENTINEL_FRAGMENT: str = address_is_sentinel_sql("address")
_SENTINEL_PARAMS: tuple[str, ...] = address_is_sentinel_params()

SAMPLE_WALLET_ID = UUID("00000000-0000-0000-0000-000000000001")
SAMPLE_MARKET_ID = UUID("00000000-0000-0000-0000-000000000010")
SAMPLE_SIGNAL_ID = UUID("00000000-0000-0000-0000-000000000011")
SAMPLE_ORDER_ID = UUID("00000000-0000-0000-0000-000000000012")
SAMPLE_POSITION_ID = UUID("00000000-0000-0000-0000-000000000013")
SAMPLE_DECISION_ID = UUID("00000000-0000-0000-0000-000000000014")
SAMPLE_EXPERIMENT_ID = UUID("00000000-0000-0000-0000-000000000015")
SAMPLE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _derive_overall_status(sources: list[SourceHealthView]) -> str:
    """Derive an overall health status from per-source statuses."""
    if not sources:
        return "unavailable"
    if all(s.status == "ok" for s in sources):
        return "healthy"
    if all(s.status in ("stale", "partial") for s in sources):
        return "degraded"
    if any(s.status in ("ok", "stale", "partial") for s in sources):
        return "degraded"
    return "unavailable"


def _dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _uuid_list(value: Any) -> list[UUID]:
    if value in (None, ""):
        return []
    try:
        raw = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    return [UUID(str(item)) for item in raw]


def _json_obj(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    try:
        raw = json.loads(str(value))
    except json.JSONDecodeError:
        return {"parse_status": "INCOMPLETE"}
    return raw if isinstance(raw, dict) else {"value": raw}


def _is_sample(row: Any) -> bool:
    return bool(row["is_sample"])


@dataclass(frozen=True)
class Page:
    limit: int = 50
    offset: int = 0


class DashboardRepository:
    """Read-only repository for persisted dashboard data."""

    def __init__(self, db: Database | None = None, settings: Settings | None = None) -> None:
        self.db = db or get_database()
        self.settings = settings

    @property
    def demo_enabled(self) -> bool:
        return bool(self.settings and self.settings.enable_demo_data)

    def scans(self, page: Page) -> ScanResponse:
        # Sentinel / empty / whitespace-only wallet addresses are excluded
        # in SQL — BEFORE LIMIT/OFFSET — so paging semantics are correct
        # (limit means "N real wallets per page", not "N rows that may
        # include sentinels") AND the total_count matches what the page
        # query would return. The Python helper ``is_sentinel_trader_address``
        # is still consulted as a defensive belt-and-braces check on the
        # rows returned by the DB (in case the schema ever drifts).
        where = f" WHERE {_SENTINEL_FRAGMENT}"
        total = self._count("wallets", where, _SENTINEL_PARAMS)
        rows = self.db.fetchall(
            f"""
            SELECT w.id, w.address, w.label, w.is_sample,
                   COALESCE(ps.trade_count, 0) AS source_count,
                   ps.total_pnl, ps.win_rate
              FROM wallets w
              LEFT JOIN performance_summaries ps ON ps.wallet_id = w.id
            {where}
             ORDER BY w.created_at DESC, w.id
             LIMIT ? OFFSET ?
            """,
            _SENTINEL_PARAMS + (page.limit, page.offset),
        )
        rows = [r for r in rows if not is_sentinel_trader_address(r["address"])]
        if total == 0 and self.demo_enabled:
            return ScanResponse(scans=self._sample_scans()[page.offset : page.offset + page.limit], total_count=1, is_sample_data=True)
        scans = [
            ScanResult(
                address=row["address"],
                label=self._label(row["label"], _is_sample(row)),
                sources=["persisted"],
                source_count=int(row["source_count"] or 0),
                score=None,
                verdict="INCOMPLETE" if row["total_pnl"] is None and row["win_rate"] is None else "persisted",
                is_sample=_is_sample(row),
            )
            for row in rows
        ]
        return ScanResponse(scans=scans, total_count=total, is_sample_data=any(s.is_sample for s in scans))

    def wallets(self, page: Page) -> WalletsResponse:
        # Same predicate as scans(): sentinel exclusion happens in SQL,
        # BEFORE LIMIT/OFFSET, using the SAME WHERE clause as the count.
        # This guarantees count/list parity even when the page boundary
        # cuts across the non-sentinel region.
        where = f" WHERE {_SENTINEL_FRAGMENT}"
        total = self._count("wallets", where, _SENTINEL_PARAMS)
        rows = self.db.fetchall(
            f"SELECT id, address, label, is_sample FROM wallets{where} ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
            _SENTINEL_PARAMS + (page.limit, page.offset),
        )
        # Defensive Python filter on top of SQL: guards against schema drift
        # or a buggy migration that reintroduces sentinel rows.
        rows = [r for r in rows if not is_sentinel_trader_address(r["address"])]
        if total == 0 and self.demo_enabled:
            sample = self._sample_wallets()[page.offset : page.offset + page.limit]
            return WalletsResponse(wallets=sample, total_count=1, is_sample_data=True)
        wallets = [self._wallet_from_row(row) for row in rows]
        return WalletsResponse(wallets=wallets, total_count=total, is_sample_data=any(w.is_sample for w in wallets))

    def wallet(self, wallet_id: UUID) -> WalletDetailView | None:
        row = self.db.fetchone("SELECT id, address, label, is_sample FROM wallets WHERE id = ?", (str(wallet_id),))
        # Defensive: refuse to return a sentinel / empty / whitespace-only
        # wallet even if its UUID somehow exists. The v5 migration deletes
        # such rows on upgrade, but a manual post-upgrade INSERT could
        # reintroduce them.
        if row is not None and not is_sentinel_trader_address(row["address"]):
            return self._wallet_from_row(row)
        if self.demo_enabled and wallet_id == SAMPLE_WALLET_ID:
            return self._sample_wallets()[0]
        return None

    def signals(self, page: Page, market_id: UUID | None = None) -> SignalsResponse:
        where = ""
        params: list[Any] = []
        if market_id is not None:
            where = " WHERE market_id = ?"
            params.append(str(market_id))
        total = self._count("signals", where, tuple(params))
        rows = self.db.fetchall(
            f"""
            SELECT id, market_id, source, strength, confidence, edge_estimate, predicted_prob,
                   market_prob, reasoning, produced_at, is_sample
              FROM signals{where}
             ORDER BY produced_at DESC, id LIMIT ? OFFSET ?
            """,
            tuple(params + [page.limit, page.offset]),
        )
        if total == 0 and self.demo_enabled:
            signals = self._sample_signals()
            if market_id is not None:
                signals = [s for s in signals if s.market_id == market_id]
            return SignalsResponse(signals=signals[page.offset : page.offset + page.limit], total_count=len(signals), is_sample_data=True)
        signals = [self._signal_from_row(row) for row in rows]
        return SignalsResponse(signals=signals, total_count=total, is_sample_data=any(s.is_sample for s in signals))

    def signal(self, signal_id: UUID) -> SignalView | None:
        row = self.db.fetchone("SELECT * FROM signals WHERE id = ?", (str(signal_id),))
        if row is not None:
            return self._signal_from_row(row)
        if self.demo_enabled and signal_id == SAMPLE_SIGNAL_ID:
            return self._sample_signals()[0]
        return None

    def orders(self, wallet_id: UUID | None = None, status_filter: str | None = None) -> OrdersResponse:
        clauses: list[str] = []
        params: list[Any] = []
        if wallet_id is not None:
            clauses.append("wallet_id = ?")
            params.append(str(wallet_id))
        if status_filter is not None:
            clauses.append("status = ?")
            params.append(status_filter)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.db.fetchall(f"SELECT * FROM orders{where} ORDER BY created_at DESC, id", tuple(params))
        if not rows and self._count("orders", where, tuple(params)) == 0 and self.demo_enabled:
            orders = self._sample_orders()
            if wallet_id is not None:
                orders = [o for o in orders if o.wallet_id == wallet_id]
            if status_filter is not None:
                orders = [o for o in orders if o.status == status_filter]
            return OrdersResponse(orders=orders, total_count=len(orders), is_sample_data=bool(orders))
        orders = [self._order_from_row(row) for row in rows]
        return OrdersResponse(orders=orders, total_count=len(orders), is_sample_data=any(o.is_sample for o in orders))

    def positions(self, wallet_id: UUID | None = None) -> PositionsResponse:
        where = " WHERE wallet_id = ?" if wallet_id is not None else ""
        params = (str(wallet_id),) if wallet_id is not None else ()
        rows = self.db.fetchall(f"SELECT * FROM positions{where} ORDER BY opened_at DESC, id", params)
        if not rows and self._count("positions", where, params) == 0 and self.demo_enabled:
            positions = self._sample_positions()
            if wallet_id is not None:
                positions = [p for p in positions if p.wallet_id == wallet_id]
            return self._positions_response(positions)
        return self._positions_response([self._position_from_row(row) for row in rows])

    def portfolio_summary(self) -> PortfolioSummary:
        rows = self.db.fetchall("SELECT * FROM positions")
        if not rows and self.demo_enabled:
            return self._portfolio_summary_from_positions(self._sample_positions(), True)
        positions = [self._position_from_row(row) for row in rows]
        return self._portfolio_summary_from_positions(positions, any(p.is_sample for p in positions))

    def decisions(self, page: Page, wallet_id: UUID | None = None) -> DecisionLogResponse:
        where = " WHERE wallet_id = ?" if wallet_id is not None else ""
        params = (str(wallet_id),) if wallet_id is not None else ()
        total = self._count("decision_log", where, params)
        rows = self.db.fetchall(f"SELECT * FROM decision_log{where} ORDER BY created_at DESC, id LIMIT ? OFFSET ?", params + (page.limit, page.offset))
        if total == 0 and self.demo_enabled:
            entries = self._sample_decisions()
            if wallet_id is not None:
                entries = [e for e in entries if e.wallet_id == wallet_id]
            return DecisionLogResponse(entries=entries[page.offset : page.offset + page.limit], total_count=len(entries), is_sample_data=True)
        entries = [self._decision_from_row(row) for row in rows]
        return DecisionLogResponse(entries=entries, total_count=total, is_sample_data=any(e.is_sample for e in entries))

    def experiments(self, page: Page) -> ExperimentMetricsResponse:
        total = self._count("experiment_runs")
        rows = self.db.fetchall("SELECT * FROM experiment_runs ORDER BY COALESCE(started_at, '') DESC, id LIMIT ? OFFSET ?", (page.limit, page.offset))
        if total == 0 and self.demo_enabled:
            experiments = self._sample_experiments()
            return ExperimentMetricsResponse(experiments=experiments[page.offset : page.offset + page.limit], total_count=len(experiments), profitable_count=0, is_sample_data=True)
        experiments = [self._experiment_from_row(row) for row in rows]
        profitable = sum(1 for e in experiments if float(e.result_summary.get("pnl", 0) or 0) > 0)
        return ExperimentMetricsResponse(experiments=experiments, total_count=total, profitable_count=profitable, is_sample_data=any(e.is_sample for e in experiments))

    def data_health(self) -> DataHealthResponse:
        """Build data health response from provider_health + raw_snapshots tables."""
        from polycopy.risk.freshness import seconds_since

        # Check provider_health table
        ph_rows = self.db.fetchall(
            "SELECT provider, capability, status, last_success, last_attempt, "
            "http_status, error_message, live_count, sample_count, is_sample "
            "FROM provider_health ORDER BY provider, capability"
        )

        # Fallback: if no provider_health rows exist, derive from raw_snapshots
        staleness_threshold = self.settings.staleness_seconds if self.settings else 120.0

        if not ph_rows:
            rows = self.db.fetchall(
                "SELECT source, COUNT(*) AS n, MIN(fetched_at) AS oldest, "
                "MAX(fetched_at) AS newest, MAX(is_sample) AS is_sample "
                "FROM raw_snapshots GROUP BY source ORDER BY source"
            )
            if not rows and self.demo_enabled:
                return DataHealthResponse(
                    sources=[SourceHealthView(
                        source=f"sample_snapshot_source [{SAMPLE_LABEL}]",
                        last_success_at=SAMPLE_TIME,
                        last_attempt_at=SAMPLE_TIME,
                        status="ok",
                        live_count=0,
                        sample_count=1,
                        is_sample=True,
                        details="Demo mode enabled; sample provenance only.",
                    )],
                    snapshot_count=1,
                    oldest_snapshot=SAMPLE_TIME,
                    newest_snapshot=SAMPLE_TIME,
                    missing_capabilities=[],
                    overall_status="healthy",
                )

            sources = []
            for row in rows:
                newest = _dt(row["newest"])
                freshness = seconds_since(newest) if newest else None
                status = "ok" if row["n"] else "unavailable"
                if freshness is not None and freshness > staleness_threshold:
                    status = "stale"
                sources.append(SourceHealthView(
                    source=self._label(row["source"], bool(row["is_sample"])),
                    last_success_at=newest,
                    last_attempt_at=newest,
                    status=status,
                    live_count=row["n"] if not row["is_sample"] else 0,
                    sample_count=row["n"] if row["is_sample"] else 0,
                    is_sample=bool(row["is_sample"]),
                    freshness_seconds=freshness,
                    details=f"{row['n']} persisted snapshots",
                ))
            count_row = self.db.fetchone(
                "SELECT COUNT(*) AS n, MIN(fetched_at) AS oldest, MAX(fetched_at) AS newest FROM raw_snapshots"
            )
            count = int(count_row["n"] if count_row else 0)
            missing: list[str] = []
            overall = _derive_overall_status(sources)
            return DataHealthResponse(
                sources=sources,
                snapshot_count=count,
                oldest_snapshot=_dt(count_row["oldest"]) if count_row else None,
                newest_snapshot=_dt(count_row["newest"]) if count_row else None,
                missing_capabilities=missing,
                overall_status=overall,
            )

        # Build from provider_health rows
        sources = []
        missing_capabilities = []
        for row in ph_rows:
            last_success = _dt(row["last_success"]) if row["last_success"] else None
            last_attempt = _dt(row["last_attempt"]) if row["last_attempt"] else None
            freshness = seconds_since(last_success) if last_success else None

            status = row["status"]
            if status == "ok" and freshness is not None and freshness > staleness_threshold:
                status = "stale"
            if status == "disabled":
                missing_capabilities.append(f"{row['provider']}.{row['capability']}")

            sources.append(SourceHealthView(
                source=f"{row['provider']}.{row['capability']}",
                last_success_at=last_success,
                last_attempt_at=last_attempt,
                status=status,
                http_status=row["http_status"],
                live_count=row["live_count"],
                sample_count=row["sample_count"],
                is_sample=bool(row["is_sample"]),
                error_message=row["error_message"] or "",
                freshness_seconds=freshness,
                details=f"{row['live_count']} live / {row['sample_count']} sample",
            ))

        count_row = self.db.fetchone(
            "SELECT COUNT(*) AS n, MIN(fetched_at) AS oldest, MAX(fetched_at) AS newest FROM raw_snapshots"
        )
        count = int(count_row["n"] if count_row else 0)

        # Determine overall status
        overall = _derive_overall_status(sources)

        return DataHealthResponse(
            sources=sources,
            snapshot_count=count,
            oldest_snapshot=_dt(count_row["oldest"]) if count_row and count_row["oldest"] else None,
            newest_snapshot=_dt(count_row["newest"]) if count_row and count_row["newest"] else None,
            missing_capabilities=missing_capabilities,
            overall_status=overall,
        )

    def risk_console(self, settings: Settings) -> RiskConsoleResponse:
        summary = self.portfolio_summary()
        gates = [
            RiskGateView(gate_name="order_kill_switch", verdict="blocked" if settings.order_kill_switch else "pass", reason="Kill switch engaged — all orders blocked." if settings.order_kill_switch else "Kill switch inactive."),
            RiskGateView(gate_name="paper_mode", verdict="pass", reason=f"PAPER ONLY mode is {settings.paper_mode}."),
            RiskGateView(gate_name="exposure_limit.order_size", verdict="pass", reason=f"Max order size: {settings.max_order_size} (0 = unlimited)."),
            RiskGateView(gate_name="exposure_limit.per_market", verdict="pass", reason=f"Max per market: {settings.max_exposure_per_market} (0 = unlimited)."),
            RiskGateView(gate_name="exposure_limit.per_wallet", verdict="pass", reason=f"Max per wallet: {settings.max_exposure_per_wallet} (0 = unlimited)."),
            RiskGateView(gate_name="exposure_limit.per_outcome", verdict="pass", reason=f"Max per outcome: {settings.max_exposure_per_outcome} (0 = unlimited)."),
            RiskGateView(gate_name="exposure_limit.global", verdict="pass", reason=f"Max global: {settings.max_exposure_global} (0 = unlimited)."),
        ]
        exposures = {
            "global": summary.total_cost_basis,
            "per_wallet": summary.total_cost_basis if summary.wallet_count else 0.0,
        }
        return RiskConsoleResponse(kill_switch_active=settings.order_kill_switch, paper_mode=settings.paper_mode, exposure_limits={"max_order_size": settings.max_order_size, "max_per_market": settings.max_exposure_per_market, "max_per_wallet": settings.max_exposure_per_wallet, "max_per_outcome": settings.max_exposure_per_outcome, "max_global": settings.max_exposure_global}, current_exposures=exposures, gates=gates, is_sample_data=summary.is_sample_data)

    def decision_export(self, fmt: str) -> tuple[str, str]:
        response = self.decisions(Page(limit=500, offset=0))
        entries = [e.model_dump(mode="json") for e in response.entries]
        if fmt == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["id", "wallet_id", "market_id", "decision_type", "signal_ids", "order_id", "rationale", "metrics", "created_at", "is_sample"])
            for e in entries:
                writer.writerow([e["id"], e["wallet_id"], e["market_id"], e["decision_type"], "|".join(e["signal_ids"]), e.get("order_id") or "", e["rationale"], json.dumps(e["metrics"], sort_keys=True), e["created_at"], e["is_sample"]])
            return output.getvalue(), "text/csv"
        return json.dumps({"format": "json", "entries": entries, "is_sample_data": response.is_sample_data}), "application/json"

    def _count(self, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
        row = self.db.fetchone(f"SELECT COUNT(*) AS n FROM {table}{where}", params)
        return int(row["n"] if row else 0)

    def _balances(self, wallet_id: str) -> list[WalletBalanceView]:
        rows = self.db.fetchall("SELECT currency, amount, as_of, is_sample FROM wallet_balances WHERE wallet_id = ? ORDER BY currency", (wallet_id,))
        return [WalletBalanceView(currency=row["currency"], amount=float(row["amount"]), as_of=_dt(row["as_of"]) or SAMPLE_TIME, is_sample=_is_sample(row)) for row in rows]

    def _wallet_from_row(self, row: Any) -> WalletDetailView:
        return WalletDetailView(id=UUID(row["id"]), address=row["address"], label=self._label(row["label"], _is_sample(row)), balances=self._balances(row["id"]), is_sample=_is_sample(row))

    def _signal_from_row(self, row: Any) -> SignalView:
        return SignalView(id=UUID(row["id"]), market_id=UUID(row["market_id"]), source=self._label(row["source"], _is_sample(row)), strength=row["strength"], confidence=float(row["confidence"]), edge_estimate=float(row["edge_estimate"]), predicted_prob=float(row["predicted_prob"]), market_prob=float(row["market_prob"]), reasoning=self._label(row["reasoning"], _is_sample(row)), produced_at=_dt(row["produced_at"]) or SAMPLE_TIME, is_sample=_is_sample(row))

    def _order_from_row(self, row: Any) -> OrderView:
        return OrderView(id=UUID(row["id"]), market_id=UUID(row["market_id"]), wallet_id=UUID(row["wallet_id"]), side=row["side"], order_type=row["order_type"], outcome=row["outcome"], quantity=float(row["quantity"]), price=float(row["price"]), status=row["status"], filled_quantity=float(row["filled_quantity"]), signal_id=UUID(row["signal_id"]) if row["signal_id"] else None, created_at=_dt(row["created_at"]) or SAMPLE_TIME, updated_at=_dt(row["updated_at"]), is_sample=_is_sample(row))

    def _position_from_row(self, row: Any) -> PositionView:
        qty = float(row["quantity"])
        avg = float(row["avg_entry_price"])
        current = float(row["current_price"])
        return PositionView(id=UUID(row["id"]), market_id=UUID(row["market_id"]), wallet_id=UUID(row["wallet_id"]), outcome=row["outcome"], quantity=qty, avg_entry_price=avg, current_price=current, realized_pnl=float(row["realized_pnl"]), unrealized_pnl=round((current - avg) * qty, 6), opened_at=_dt(row["opened_at"]) or SAMPLE_TIME, updated_at=_dt(row["updated_at"]), is_sample=_is_sample(row))

    def _decision_from_row(self, row: Any) -> DecisionLogView:
        return DecisionLogView(id=UUID(row["id"]), wallet_id=UUID(row["wallet_id"]), market_id=UUID(row["market_id"]), decision_type=row["decision_type"], signal_ids=_uuid_list(row["signal_ids"]), order_id=UUID(row["order_id"]) if row["order_id"] else None, rationale=self._label(row["rationale"], _is_sample(row)), metrics=_json_obj(row["metrics"]), created_at=_dt(row["created_at"]) or SAMPLE_TIME, is_sample=_is_sample(row))

    def _experiment_from_row(self, row: Any) -> ExperimentMetricView:
        return ExperimentMetricView(id=UUID(row["id"]), label=self._label(row["label"], _is_sample(row)), strategy_config=_json_obj(row["strategy_config"]), status=row["status"], started_at=_dt(row["started_at"]), ended_at=_dt(row["ended_at"]), result_summary=_json_obj(row["result_summary"]), error_message=row["error_message"], is_sample=_is_sample(row))

    def _positions_response(self, positions: list[PositionView]) -> PositionsResponse:
        return PositionsResponse(positions=positions, total_count=len(positions), total_unrealized_pnl=round(sum(p.unrealized_pnl for p in positions), 6), total_cost_basis=round(sum(p.avg_entry_price * p.quantity for p in positions), 6), is_sample_data=any(p.is_sample for p in positions))

    def _portfolio_summary_from_positions(self, positions: list[PositionView], is_sample: bool) -> PortfolioSummary:
        cost = sum(p.avg_entry_price * p.quantity for p in positions)
        value = sum(p.current_price * p.quantity for p in positions)
        realized = sum(p.realized_pnl for p in positions)
        unrealized = sum(p.unrealized_pnl for p in positions)
        wallets = {p.wallet_id for p in positions}
        return PortfolioSummary(total_positions=len(positions), total_cost_basis=round(cost, 6), total_market_value=round(value, 6), total_unrealized_pnl=round(unrealized, 6), total_realized_pnl=round(realized, 6), total_pnl=round(realized + unrealized, 6), wallet_count=len(wallets), is_sample_data=is_sample)

    def _label(self, value: str, is_sample: bool) -> str:
        return value if not is_sample or "SAMPLE DATA" in value or "DEMO DATA" in value else f"{value} [{SAMPLE_LABEL}]"

    def _sample_scans(self) -> list[ScanResult]:
        return [ScanResult(address="0xSAMPLE_WALLET_ADDRESS_DO_NOT_USE_IN_PROD", label=f"sample-wallet [{SAMPLE_LABEL}]", sources=["demo_mode"], source_count=1, score=72.5, verdict="copy_candidate", is_sample=True)]

    def _sample_wallets(self) -> list[WalletDetailView]:
        return [WalletDetailView(id=SAMPLE_WALLET_ID, address="0xSAMPLE_WALLET_ADDRESS_DO_NOT_USE_IN_PROD", label=f"sample-wallet [{SAMPLE_LABEL}]", balances=[WalletBalanceView(currency="USDC", amount=1000.0, as_of=SAMPLE_TIME, is_sample=True)], is_sample=True)]

    def _sample_signals(self) -> list[SignalView]:
        return [SignalView(id=SAMPLE_SIGNAL_ID, market_id=SAMPLE_MARKET_ID, source=f"sample [{SAMPLE_LABEL}]", strength="buy", confidence=0.72, edge_estimate=0.08, predicted_prob=0.65, market_prob=0.57, reasoning=f"Demo signal only [{SAMPLE_LABEL}]", produced_at=SAMPLE_TIME, is_sample=True)]

    def _sample_orders(self) -> list[OrderView]:
        return [OrderView(id=SAMPLE_ORDER_ID, market_id=SAMPLE_MARKET_ID, wallet_id=SAMPLE_WALLET_ID, side="buy", order_type="limit", outcome="Yes", quantity=10.0, price=0.65, status="pending", filled_quantity=0.0, created_at=SAMPLE_TIME, updated_at=SAMPLE_TIME, is_sample=True)]

    def _sample_positions(self) -> list[PositionView]:
        return [PositionView(id=SAMPLE_POSITION_ID, market_id=SAMPLE_MARKET_ID, wallet_id=SAMPLE_WALLET_ID, outcome="Yes", quantity=10.0, avg_entry_price=0.65, current_price=0.72, realized_pnl=0.0, unrealized_pnl=0.7, opened_at=SAMPLE_TIME, updated_at=SAMPLE_TIME, is_sample=True)]

    def _sample_decisions(self) -> list[DecisionLogView]:
        return [DecisionLogView(id=SAMPLE_DECISION_ID, wallet_id=SAMPLE_WALLET_ID, market_id=SAMPLE_MARKET_ID, decision_type="skip", signal_ids=[], rationale=f"Score below threshold — skipped [{SAMPLE_LABEL}]", metrics={"score": 42.0, "threshold": 70.0}, created_at=SAMPLE_TIME, is_sample=True)]

    def _sample_experiments(self) -> list[ExperimentMetricView]:
        return [ExperimentMetricView(id=SAMPLE_EXPERIMENT_ID, label=f"sample-experiment [{SAMPLE_LABEL}]", strategy_config={"copy_threshold": 70.0, "paper_mode": "paper_manual"}, status="completed", started_at=SAMPLE_TIME, ended_at=SAMPLE_TIME, result_summary={"total_trades": 0, "pnl": 0.0, "note": "Demo mode sample"}, is_sample=True)]
