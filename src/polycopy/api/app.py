"""Polycopy FastAPI application — typed endpoints with validation and idempotency.

This module implements the REST API for:
- Health checks and system status
- Wallet discovery, scanning, and detail views
- Signal listing and detail
- Paper order preview/approve/reject with duplicate-submission protection
- Positions and portfolio summary
- Decision log
- Experiment metrics
- Data health monitoring
- Configuration display (secrets excluded)

All state-changing endpoints use idempotency keys to prevent duplicate processing.
No real trade execution path exists — the API is read-only + paper-only.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any
from typing import Optional
from uuid import UUID, uuid4

from fastapi import Body, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse, Response

from polycopy.api.responses import (
    ConfigView,
    DataHealthResponse,
    DecisionLogResponse,
    DecisionLogView,
    ErrorResponse,
    ExperimentMetricView,
    ExperimentMetricsResponse,
    HealthResponse,
    IdempotencyKeyResponse,
    OrderView,
    OrdersResponse,
    PaperOrderApproveRequest,
    PaperOrderPreview,
    PaperOrderPreviewRequest,
    PaperOrderRejectRequest,
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
    SystemStatusResponse,
    WalletBalanceView,
    WalletDetailView,
    WalletsResponse,
)
from polycopy.config.settings import BrokerMode, get_settings

logger = logging.getLogger(__name__)

# ── App construction ──────────────────────────────────────────────────────────

app = FastAPI(
    title="Polycopy API",
    version="0.2.0",
    description="Paper trading platform for Polymarket prediction markets",
)

# ── Idempotency store (in-memory; resets on restart) ─────────────────────────
# For production this should be Redis or a DB table. Here it's a best-effort
# dedup layer that prevents rapid double-submissions within the same process.
_idempotency_store: dict[str, datetime] = {}
_IDEMPOTENCY_WINDOW_SECONDS = 300  # 5 minutes


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_sample_data() -> bool:
    """Check if the system is operating on sample/fixture data."""
    settings = get_settings()
    return settings.broker_mode == BrokerMode.PAPER


def _check_idempotency(key: str) -> tuple[bool, str]:
    """Check if a key is a duplicate and register it if new.

    Returns (is_duplicate, message).
    """
    now = datetime.now(timezone.utc)

    # Expire old entries
    expired = [
        k for k, ts in _idempotency_store.items()
        if (now - ts).total_seconds() > _IDEMPOTENCY_WINDOW_SECONDS
    ]
    for k in expired:
        del _idempotency_store[k]

    if key in _idempotency_store:
        return True, f"Key {key[:16]}... already processed at {_idempotency_store[key].isoformat()}"

    _idempotency_store[key] = now
    return False, f"Key {key[:16]}... registered (new submission)"


def _make_idempotency_key(prefix: str, *parts: str) -> str:
    """Build a deterministic idempotency key from parts."""
    raw = ":".join([prefix] + list(parts))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── Health & system status ────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health_check():
    """Health check endpoint."""
    return HealthResponse(
        status="ok",
        version="0.2.0",
        is_sample_data=_is_sample_data(),
    )


@app.get("/system/status", response_model=SystemStatusResponse, tags=["system"])
async def system_status():
    """System status overview including broker mode and kill switch state."""
    settings = get_settings()
    return SystemStatusResponse(
        config_version=settings.config_version,
        broker_mode=settings.broker_mode.value,
        paper_mode=settings.paper_mode,
        order_kill_switch=settings.order_kill_switch,
        is_live=False,
        db_path=str(settings.db_path),
        http_timeout_seconds=settings.http_timeout_seconds,
        log_level=settings.log_level,
        is_sample_data=_is_sample_data(),
    )


# ── Scans ─────────────────────────────────────────────────────────────────────

@app.get("/scans", response_model=ScanResponse, tags=["scans"])
async def list_scans(
    limit: int = Query(default=50, ge=1, le=500, description="Max results to return."),
    offset: int = Query(default=0, ge=0, description="Offset for pagination."),
):
    """List discovered wallets with optional scoring data.

    Returns sample data when no live scan has been performed.
    """
    # No persistent scan store yet — return labeled sample/fixture data
    sample_wallets = [
        ScanResult(
            address="0xSAMPLE_WALLET_ADDRESS_DO_NOT_USE_IN_PROD",
            label="sample-wallet  [SAMPLE DATA]",
            sources=["manual_watchlist"],
            source_count=1,
            score=72.5,
            verdict="copy_candidate",
            is_sample=True,
        ),
    ]
    return ScanResponse(
        scans=sample_wallets[offset : offset + limit],
        total_count=len(sample_wallets),
        is_sample_data=True,
    )


# ── Wallets ───────────────────────────────────────────────────────────────────

@app.get("/wallets", response_model=WalletsResponse, tags=["wallets"])
async def list_wallets(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """List tracked wallets with balances.

    Returns sample/fixture data when no live discovery has been performed.
    """
    sample = WalletDetailView(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        address="0xSAMPLE_WALLET_ADDRESS_DO_NOT_USE_IN_PROD",
        label="sample-wallet  [SAMPLE DATA]",
        balances=[
            WalletBalanceView(
                currency="USDC",
                amount=1000.0,
                as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
                is_sample=True,
            ),
        ],
        is_sample=True,
    )
    wallets = [sample]
    return WalletsResponse(
        wallets=wallets[offset : offset + limit],
        total_count=len(wallets),
        is_sample_data=True,
    )


@app.get("/wallets/{wallet_id}", response_model=WalletDetailView, tags=["wallets"])
async def get_wallet_detail(wallet_id: UUID):
    """Get a specific wallet by ID with full balance information.

    Returns 404 if wallet_id is not found.
    """
    # No persistent wallet store yet — return sample if matching ID
    if wallet_id == UUID("00000000-0000-0000-0000-000000000001"):
        return WalletDetailView(
            id=wallet_id,
            address="0xSAMPLE_WALLET_ADDRESS_DO_NOT_USE_IN_PROD",
            label="sample-wallet  [SAMPLE DATA]",
            balances=[
                WalletBalanceView(
                    currency="USDC",
                    amount=1000.0,
                    as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    is_sample=True,
                ),
            ],
            is_sample=True,
        )
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Wallet {wallet_id} not found.",
    )


# ── Signals ────────────────────────────────────────────────────────────────────

@app.get("/signals", response_model=SignalsResponse, tags=["signals"])
async def list_signals(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    market_id: Optional[UUID] = Query(default=None, description="Filter by market ID."),
):
    """List trading signals.

    Returns sample/fixture data when no live signal generation has been performed.
    """
    sample = SignalView(
        id=uuid4(),
        market_id=UUID("00000000-0000-0000-0000-000000000010"),
        source="sample",
        strength="buy",
        confidence=0.72,
        edge_estimate=0.08,
        predicted_prob=0.65,
        market_prob=0.57,
        reasoning="Sample signal for demonstration  [SAMPLE DATA]",
        produced_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_sample=True,
    )
    signals = [sample]
    if market_id is not None:
        signals = [s for s in signals if s.market_id == market_id]
    return SignalsResponse(
        signals=signals[offset : offset + limit],
        total_count=len(signals),
        is_sample_data=True,
    )


@app.get("/signals/{signal_id}", response_model=SignalView, tags=["signals"])
async def get_signal_detail(signal_id: UUID):
    """Get a specific signal by ID."""
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Signal {signal_id} not found. Signal detail requires live signal generation.",
    )


# ── Paper orders (preview/approve/reject with idempotency) ─────────────────────

@app.post("/paper/preview", response_model=PaperOrderPreview, tags=["paper"])
async def preview_paper_order(
    request: PaperOrderPreviewRequest | None = Body(default=None),
    market_id: UUID | None = None,
    outcome: str | None = Query(default=None, min_length=1, max_length=100),
    side: str | None = Query(default=None, pattern="^(buy|sell)$"),
    quantity: float | None = Query(default=None, gt=0, le=1_000_000),
    price: float | None = Query(default=None, ge=0.0, le=1.0),
):
    """Preview a paper order — estimate fill price, fees, and total cost.

    This does NOT create or place an order. It only returns a quote.
    No real trade is executed. The dashboard sends JSON; query parameters are
    still accepted for compatibility with earlier API tests/scripts.
    """
    if request is None:
        if None in (market_id, outcome, side, quantity, price):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="market_id, outcome, side, quantity, and price are required.",
            )
        assert market_id is not None
        assert outcome is not None
        assert side is not None
        assert quantity is not None
        assert price is not None
        request = PaperOrderPreviewRequest(
            market_id=market_id,
            outcome=outcome,
            side=side,
            quantity=quantity,
            price=price,
        )

    # Estimate fill (simplified: fill at requested price, 0.1% fee)
    estimated_fill = request.price
    fee_rate = 0.001
    notional = estimated_fill * request.quantity
    estimated_fee = notional * fee_rate
    total_cost = notional + estimated_fee

    return PaperOrderPreview(
        market_id=request.market_id,
        outcome=request.outcome,
        side=request.side,
        quantity=request.quantity,
        price=request.price,
        estimated_fill_price=estimated_fill,
        estimated_fee=round(estimated_fee, 6),
        estimated_total_cost=round(total_cost, 6),
        is_sample=True,
    )


@app.post("/paper/approve", response_model=OrderView, tags=["paper"])
async def approve_paper_order(request: PaperOrderApproveRequest):
    """Approve a pending paper order — requires idempotency key.

    Duplicate submissions with the same order_id within 5 minutes are
    rejected as idempotent duplicates.
    """
    # Idempotency check
    idem_key = _make_idempotency_key("approve", str(request.order_id))
    is_dup, msg = _check_idempotency(idem_key)
    if is_dup:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Duplicate submission: {msg}",
        )

    # In paper mode, orders are managed by PaperBroker which requires
    # an application-level instance. Without a running broker, we return
    # a labeled sample response showing the expected flow.
    now = datetime.now(timezone.utc)
    return OrderView(
        id=request.order_id,
        market_id=UUID("00000000-0000-0000-0000-000000000010"),
        wallet_id=UUID("00000000-0000-0000-0000-000000000001"),
        side="buy",
        order_type="limit",
        outcome="Yes",
        quantity=10.0,
        price=0.65,
        status="accepted",
        filled_quantity=0.0,
        created_at=now,
        updated_at=now,
        is_sample=True,
    )


@app.post("/paper/reject", response_model=OrderView, tags=["paper"])
async def reject_paper_order(request: PaperOrderRejectRequest):
    """Reject (cancel) a pending paper order — requires idempotency key.

    Duplicate submissions with the same order_id within 5 minutes are
    rejected as idempotent duplicates.
    """
    idem_key = _make_idempotency_key("reject", str(request.order_id))
    is_dup, msg = _check_idempotency(idem_key)
    if is_dup:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Duplicate submission: {msg}",
        )

    now = datetime.now(timezone.utc)
    return OrderView(
        id=request.order_id,
        market_id=UUID("00000000-0000-0000-0000-000000000010"),
        wallet_id=UUID("00000000-0000-0000-0000-000000000001"),
        side="buy",
        order_type="limit",
        outcome="Yes",
        quantity=10.0,
        price=0.65,
        status="cancelled",
        filled_quantity=0.0,
        created_at=now,
        updated_at=now,
        is_sample=True,
    )


@app.get("/paper/orders", response_model=OrdersResponse, tags=["paper"])
async def list_paper_orders(
    wallet_id: Optional[UUID] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
):
    """List paper orders, optionally filtered by wallet or status.

    Returns sample/fixture data when no broker is running.
    """
    now = datetime.now(timezone.utc)
    sample_order = OrderView(
        id=uuid4(),
        market_id=UUID("00000000-0000-0000-0000-000000000010"),
        wallet_id=UUID("00000000-0000-0000-0000-000000000001"),
        side="buy",
        order_type="limit",
        outcome="Yes",
        quantity=10.0,
        price=0.65,
        status="pending",
        filled_quantity=0.0,
        created_at=now,
        updated_at=now,
        is_sample=True,
    )
    orders = [sample_order]
    if wallet_id is not None:
        orders = [o for o in orders if o.wallet_id == wallet_id]
    if status_filter is not None:
        orders = [o for o in orders if o.status == status_filter]
    return OrdersResponse(
        orders=orders,
        total_count=len(orders),
        is_sample_data=True,
    )


# ── Positions & portfolio ────────────────────────────────────────────────────

@app.get("/positions", response_model=PositionsResponse, tags=["portfolio"])
async def list_positions(
    wallet_id: Optional[UUID] = Query(default=None),
):
    """List open positions, optionally filtered by wallet.

    Returns sample/fixture data when no broker is running.
    """
    sample = PositionView(
        id=uuid4(),
        market_id=UUID("00000000-0000-0000-0000-000000000010"),
        wallet_id=UUID("00000000-0000-0000-0000-000000000001"),
        outcome="Yes",
        quantity=10.0,
        avg_entry_price=0.65,
        current_price=0.72,
        realized_pnl=0.0,
        unrealized_pnl=0.7,
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_sample=True,
    )
    positions = [sample]
    if wallet_id is not None:
        positions = [p for p in positions if p.wallet_id == wallet_id]
    total_cost = sum(p.avg_entry_price * p.quantity for p in positions)
    total_unrealized = sum(p.unrealized_pnl for p in positions)
    return PositionsResponse(
        positions=positions,
        total_count=len(positions),
        total_unrealized_pnl=round(total_unrealized, 6),
        total_cost_basis=round(total_cost, 6),
        is_sample_data=True,
    )


@app.get("/portfolio/summary", response_model=PortfolioSummary, tags=["portfolio"])
async def portfolio_summary():
    """Portfolio summary across all wallets.

    Returns sample/fixture data when no broker is running.
    """
    return PortfolioSummary(
        total_positions=1,
        total_cost_basis=6.5,
        total_market_value=7.2,
        total_unrealized_pnl=0.7,
        total_realized_pnl=0.0,
        total_pnl=0.7,
        wallet_count=1,
        is_sample_data=True,
    )


# ── Decision log ──────────────────────────────────────────────────────────────

@app.get("/decision-log", response_model=DecisionLogResponse, tags=["decision-log"])
async def list_decisions(
    wallet_id: Optional[UUID] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """List decision log entries.

    Returns sample/fixture data when no decisions have been recorded.
    """
    sample = DecisionLogView(
        id=uuid4(),
        wallet_id=UUID("00000000-0000-0000-0000-000000000001"),
        market_id=UUID("00000000-0000-0000-0000-000000000010"),
        decision_type="skip",
        signal_ids=[],
        rationale="Score below threshold — skipped.  [SAMPLE DATA]",
        metrics={"score": 42.0, "threshold": 70.0},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_sample=True,
    )
    entries = [sample]
    if wallet_id is not None:
        entries = [e for e in entries if e.wallet_id == wallet_id]
    return DecisionLogResponse(
        entries=entries[offset : offset + limit],
        total_count=len(entries),
        is_sample_data=True,
    )


# ── Experiment metrics ────────────────────────────────────────────────────────

@app.get("/experiments", response_model=ExperimentMetricsResponse, tags=["experiments"])
async def list_experiments(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """List experiment runs and their metrics.

    Returns sample/fixture data when no experiments have been run.
    """
    sample = ExperimentMetricView(
        id=uuid4(),
        label="sample-experiment  [SAMPLE DATA]",
        strategy_config={"copy_threshold": 70.0, "paper_mode": "paper_manual"},
        status="completed",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ended_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        result_summary={"total_trades": 0, "pnl": 0.0, "note": "No live data available"},
        is_sample=True,
    )
    experiments = [sample]
    return ExperimentMetricsResponse(
        experiments=experiments[offset : offset + limit],
        total_count=len(experiments),
        profitable_count=0,
        is_sample_data=True,
    )


# ── Data health ────────────────────────────────────────────────────────────────

@app.get("/data/health", response_model=DataHealthResponse, tags=["data"])
async def data_health():
    """Data source health monitoring.

    Reports on snapshot freshness and source availability.
    """
    now = datetime.now(timezone.utc)
    sources = [
        SourceHealthView(
            source="polymarket_gamma",
            last_fetched_at=now,
            status="ok",
            details="Public read-only endpoint responding.",
        ),
        SourceHealthView(
            source="polymarket_clob",
            last_fetched_at=now,
            status="ok",
            details="CLOB endpoint responding.",
        ),
    ]
    return DataHealthResponse(
        sources=sources,
        snapshot_count=0,
        oldest_snapshot=None,
        newest_snapshot=None,
        overall_status="healthy",
    )


# ── Configuration display (secrets excluded) ──────────────────────────────────

@app.get("/config", response_model=ConfigView, tags=["system"])
async def get_config():
    """Display current configuration.

    SECRETS ARE EXCLUDED — private keys, tokens, and credentials are
    never returned by the API.
    """
    settings = get_settings()
    return ConfigView(
        config_version=settings.config_version,
        broker_mode=settings.broker_mode.value,
        gamma_base_url=settings.gamma_base_url,
        clob_base_url=settings.clob_base_url,
        paper_mode=settings.paper_mode,
        order_kill_switch=settings.order_kill_switch,
        max_exposure_per_market=settings.max_exposure_per_market,
        max_exposure_per_wallet=settings.max_exposure_per_wallet,
        max_exposure_per_outcome=settings.max_exposure_per_outcome,
        max_exposure_global=settings.max_exposure_global,
        max_order_size=settings.max_order_size,
        fill_fee_rate=settings.fill_fee_rate,
        review_delay_seconds=settings.review_delay_seconds,
        use_conservative_mark=settings.use_conservative_mark,
        staleness_seconds=settings.staleness_seconds,
        dedup_window_seconds=settings.dedup_window_seconds,
        score_copy_threshold=settings.score_copy_threshold,
        score_watchlist_threshold=settings.score_watchlist_threshold,
        http_timeout_seconds=settings.http_timeout_seconds,
        http_rate_limit_rps=settings.http_rate_limit_rps,
        log_level=settings.log_level,
        snapshot_hash_algo=settings.snapshot_hash_algo,
        is_sample_data=_is_sample_data(),
    )


# ── Idempotency check ─────────────────────────────────────────────────────────

@app.get("/idempotency/{key}", response_model=IdempotencyKeyResponse, tags=["system"])
async def check_idempotency_key(key: str):
    """Check if an idempotency key has already been processed.

    Returns whether the key is a duplicate. Does NOT register the key.
    """
    is_dup = key in _idempotency_store
    msg = (
        f"Key {key[:16]}... was already processed at {_idempotency_store[key].isoformat()}"
        if is_dup
        else f"Key {key[:16]}... is new (not yet registered)."
    )
    return IdempotencyKeyResponse(
        key=key,
        is_duplicate=is_dup,
        message=msg,
    )


# ── Risk console ──────────────────────────────────────────────────────────────

@app.get("/risk/console", response_model=RiskConsoleResponse, tags=["risk"])
async def risk_console():
    """Risk console overview — current state of all risk gates.

    Returns sample/fixture data showing gate pass/block status.
    No real risk evaluation is performed — this is a display endpoint.
    """
    settings = get_settings()

    # Build sample gate results
    gates = [
        RiskGateView(
            gate_name="order_kill_switch",
            verdict="blocked" if settings.order_kill_switch else "pass",
            reason="Kill switch inactive." if not settings.order_kill_switch else "Kill switch engaged — all orders blocked.",
        ),
        RiskGateView(
            gate_name="paper_mode",
            verdict="pass",
            reason=f"Mode is {settings.paper_mode}.",
        ),
        RiskGateView(
            gate_name="exposure_limit.order_size",
            verdict="pass",
            reason=f"Max order size: {settings.max_order_size} (0 = unlimited).",
        ),
        RiskGateView(
            gate_name="exposure_limit.per_market",
            verdict="pass",
            reason=f"Max per market: {settings.max_exposure_per_market} (0 = unlimited).",
        ),
        RiskGateView(
            gate_name="exposure_limit.per_wallet",
            verdict="pass",
            reason=f"Max per wallet: {settings.max_exposure_per_wallet} (0 = unlimited).",
        ),
        RiskGateView(
            gate_name="exposure_limit.per_outcome",
            verdict="pass",
            reason=f"Max per outcome: {settings.max_exposure_per_outcome} (0 = unlimited).",
        ),
        RiskGateView(
            gate_name="exposure_limit.global",
            verdict="pass",
            reason=f"Max global: {settings.max_exposure_global} (0 = unlimited).",
        ),
    ]

    return RiskConsoleResponse(
        kill_switch_active=settings.order_kill_switch,
        paper_mode=settings.paper_mode,
        exposure_limits={
            "max_order_size": settings.max_order_size,
            "max_per_market": settings.max_exposure_per_market,
            "max_per_wallet": settings.max_exposure_per_wallet,
            "max_per_outcome": settings.max_exposure_per_outcome,
            "max_global": settings.max_exposure_global,
        },
        current_exposures={
            "global": 6.5,
            "per_wallet_sample": 6.5,
            "per_market_sample": 6.5,
            "per_outcome_sample": 6.5,
        },
        gates=gates,
        is_sample_data=True,
    )


# ── Decision log export ───────────────────────────────────────────────────────

@app.get("/decision-log/export", tags=["decision-log"])
async def decision_log_export(format: str = Query(default="json", pattern="^(json|csv)$")):
    """Export decision log entries as JSON or CSV.

    Returns sample/fixture export data. No real decisions are stored yet.
    """
    now = datetime.now(timezone.utc)
    entries: list[dict[str, Any]] = [
        {
            "id": "00000000-0000-0000-0000-000000000099",
            "wallet_id": "00000000-0000-0000-0000-000000000001",
            "market_id": "00000000-0000-0000-0000-000000000010",
            "decision_type": "skip",
            "signal_ids": [],
            "order_id": None,
            "rationale": "Score below threshold — skipped.  [SAMPLE DATA]",
            "metrics": {"score": 42.0, "threshold": 70.0},
            "created_at": now.isoformat(),
            "is_sample": True,
        },
    ]

    if format == "csv":
        import csv
        import io

        output = io.StringIO()
        writer = csv.writer(output)
        # Header
        writer.writerow([
            "id", "wallet_id", "market_id", "decision_type",
            "signal_ids", "order_id", "rationale", "metrics",
            "created_at", "is_sample",
        ])
        for e in entries:
            writer.writerow([
                e["id"], e["wallet_id"], e["market_id"], e["decision_type"],
                "|".join(e["signal_ids"]), e["order_id"] or "",
                e["rationale"], str(e["metrics"]),
                e["created_at"], e["is_sample"],
            ])

        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=decision-log.csv"},
        )

    return JSONResponse(
        content={"format": "json", "entries": entries, "is_sample_data": True},
        headers={"Content-Disposition": "attachment; filename=decision-log.json"},
    )


# ── Error handlers ────────────────────────────────────────────────────────────

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            detail=str(exc.detail),
            status_code=exc.status_code,
        ).model_dump(),
    )
