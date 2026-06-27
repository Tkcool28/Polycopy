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
from typing import Optional
from uuid import UUID

from fastapi import Body, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse, Response

from polycopy.api.responses import (
    ConfigView,
    DataHealthResponse,
    DecisionLogResponse,
    ErrorResponse,
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
    PositionsResponse,
    RiskConsoleResponse,
    ScanResponse,
    SignalView,
    SignalsResponse,
    SystemStatusResponse,
    WalletDetailView,
    WalletsResponse,
)
from polycopy.api.repository import DashboardRepository, Page
from polycopy.config.settings import get_settings

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
    """Check if explicit demo/sample API mode is enabled."""
    settings = get_settings()
    return settings.enable_demo_data


def _repository() -> DashboardRepository:
    """Create a repository bound to current settings/SQLite connection."""
    return DashboardRepository(settings=get_settings())


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
    """List discovered wallets with optional scoring data."""
    return _repository().scans(Page(limit=limit, offset=offset))


# ── Wallets ───────────────────────────────────────────────────────────────────

@app.get("/wallets", response_model=WalletsResponse, tags=["wallets"])
async def list_wallets(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """List tracked wallets with balances."""
    return _repository().wallets(Page(limit=limit, offset=offset))


@app.get("/wallets/{wallet_id}", response_model=WalletDetailView, tags=["wallets"])
async def get_wallet_detail(wallet_id: UUID):
    """Get a specific wallet by ID with full balance information.

    Returns 404 if wallet_id is not found.
    """
    wallet = _repository().wallet(wallet_id)
    if wallet is not None:
        return wallet
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
    """List trading signals."""
    return _repository().signals(Page(limit=limit, offset=offset), market_id=market_id)


@app.get("/signals/{signal_id}", response_model=SignalView, tags=["signals"])
async def get_signal_detail(signal_id: UUID):
    """Get a specific signal by ID."""
    signal = _repository().signal(signal_id)
    if signal is not None:
        return signal
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Signal {signal_id} not found.")


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
    """List PAPER ONLY orders, optionally filtered by wallet or status."""
    return _repository().orders(wallet_id=wallet_id, status_filter=status_filter)


# ── Positions & portfolio ────────────────────────────────────────────────────

@app.get("/positions", response_model=PositionsResponse, tags=["portfolio"])
async def list_positions(
    wallet_id: Optional[UUID] = Query(default=None),
):
    """List open positions, optionally filtered by wallet."""
    return _repository().positions(wallet_id=wallet_id)


@app.get("/portfolio/summary", response_model=PortfolioSummary, tags=["portfolio"])
async def portfolio_summary():
    """Portfolio summary across all wallets."""
    return _repository().portfolio_summary()


# ── Decision log ──────────────────────────────────────────────────────────────

@app.get("/decision-log", response_model=DecisionLogResponse, tags=["decision-log"])
async def list_decisions(
    wallet_id: Optional[UUID] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """List decision log entries."""
    return _repository().decisions(Page(limit=limit, offset=offset), wallet_id=wallet_id)


# ── Experiment metrics ────────────────────────────────────────────────────────

@app.get("/experiments", response_model=ExperimentMetricsResponse, tags=["experiments"])
async def list_experiments(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """List experiment runs and their metrics."""
    return _repository().experiments(Page(limit=limit, offset=offset))


# ── Data health ────────────────────────────────────────────────────────────────

@app.get("/data/health", response_model=DataHealthResponse, tags=["data"])
async def data_health():
    """Data source health monitoring.

    Reports on snapshot freshness and source availability.
    """
    return _repository().data_health()


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
    """Risk console overview — current PAPER ONLY risk state."""
    settings = get_settings()
    return _repository().risk_console(settings)


# ── Decision log export ───────────────────────────────────────────────────────

@app.get("/decision-log/export", tags=["decision-log"])
async def decision_log_export(format: str = Query(default="json", pattern="^(json|csv)$")):
    """Export decision log entries as JSON or CSV."""
    content, media_type = _repository().decision_export(format)
    filename = f"decision-log.{format}"
    if format == "csv":
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    return JSONResponse(
        content=__import__("json").loads(content),
        headers={"Content-Disposition": f"attachment; filename={filename}"},
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
