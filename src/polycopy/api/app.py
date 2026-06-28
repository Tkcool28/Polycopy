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

All state-changing endpoints use SQLite-backed idempotency keys to prevent
duplicate processing across restarts. No real trade execution path exists.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional, cast
from uuid import NAMESPACE_URL, UUID, uuid5

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
from polycopy.db.database import get_database
from polycopy.providers.bidask import BidAskProvider
from polycopy.risk.idempotency import IdempotencyStore

logger = logging.getLogger(__name__)

# ── App construction ──────────────────────────────────────────────────────────

app = FastAPI(
    title="Polycopy API",
    version="0.3.0",
    description="Paper trading platform for Polymarket prediction markets",
)

# ── Idempotency store (SQLite-backed; survives restarts) ──────────────────────
_idempotency_store = IdempotencyStore()

# ── Bid/ask snapshot provider (for paper preview fill simulation) ─────────────
_bidask_provider = BidAskProvider()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_sample_data() -> bool:
    """Check if explicit demo/sample API mode is enabled."""
    settings = get_settings()
    return settings.enable_demo_data


def _repository() -> DashboardRepository:
    """Create a repository bound to current settings/SQLite connection."""
    return DashboardRepository(settings=get_settings())


def _persist_paper_result(result: dict[str, object], decision_type: str, rationale: str) -> None:
    """Persist a PAPER ONLY order result plus audit rows to SQLite.

    This is intentionally local/paper-only persistence. It writes no live broker
    state and makes no network calls.
    """
    from polycopy.db.database import get_database

    db = get_database()
    now = str(result["updated_at"] or result["created_at"])
    market_id = str(result["market_id"])
    wallet_id = str(result["wallet_id"])
    order_id = str(result["id"])
    outcome = str(result["outcome"])
    side = str(result["side"])
    quantity = float(result["quantity"])  # type: ignore[arg-type]
    price = float(result["price"])  # type: ignore[arg-type]
    filled_quantity = float(result["filled_quantity"])  # type: ignore[arg-type]
    is_sample = 1 if bool(result.get("is_sample", True)) else 0

    db.execute(
        "INSERT OR IGNORE INTO wallets (id, address, label, is_sample, created_at) VALUES (?, ?, ?, ?, ?)",
        (wallet_id, "0xPAPER_ONLY_SAMPLE_WALLET", "paper-wallet [DEMO DATA / SAMPLE DATA]", is_sample, now),
    )
    db.execute(
        "INSERT OR IGNORE INTO markets (id, source_id, source, question, fetched_at, is_sample) VALUES (?, ?, ?, ?, ?, ?)",
        (market_id, f"paper-{market_id}", "paper_preview", "PAPER ONLY preview market", now, is_sample),
    )
    db.execute(
        """
        INSERT OR REPLACE INTO orders (
            id, market_id, wallet_id, side, order_type, outcome, quantity, price,
            status, filled_quantity, source_order_id, signal_id, created_at, updated_at, is_sample
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order_id,
            market_id,
            wallet_id,
            side,
            str(result["order_type"]),
            outcome,
            quantity,
            price,
            str(result["status"]),
            filled_quantity,
            None,
            None,
            str(result["created_at"]),
            str(result["updated_at"]),
            is_sample,
        ),
    )
    if str(result["status"]) == "filled" and filled_quantity > 0:
        position_id = str(uuid5(NAMESPACE_URL, f"polycopy-position:{market_id}:{wallet_id}:{outcome}"))
        db.execute(
            """
            INSERT OR REPLACE INTO positions (
                id, market_id, wallet_id, outcome, quantity, avg_entry_price,
                current_price, realized_pnl, opened_at, updated_at, is_sample
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (position_id, market_id, wallet_id, outcome, filled_quantity, price, price, 0.0, str(result["created_at"]), now, is_sample),
        )
    decision_id = str(uuid5(NAMESPACE_URL, f"polycopy-decision:{decision_type}:{order_id}"))
    db.execute(
        """
        INSERT OR IGNORE INTO decision_log (
            id, wallet_id, market_id, decision_type, signal_ids, order_id,
            rationale, metrics, created_at, is_sample
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            wallet_id,
            market_id,
            decision_type,
            "[]",
            order_id,
            rationale,
            json.dumps({"paper_only": True, "status": str(result["status"]), "filled_quantity": filled_quantity}, sort_keys=True),
            now,
            is_sample,
        ),
    )
    db.conn.commit()


OPEN_EXPOSURE_ORDER_STATUSES = ("pending", "accepted", "partially_filled")


def _position_exposure_where(db, where: str = "", params: tuple[object, ...] = ()) -> float:
    row = db.fetchone(
        f"SELECT COALESCE(SUM(quantity * avg_entry_price), 0) AS total FROM positions{where}",
        params,
    )
    return float(row["total"] if row else 0.0)


def _open_order_exposure_where(db, where: str = "", params: tuple[object, ...] = ()) -> float:
    row = db.fetchone(
        f"""
        SELECT COALESCE(SUM((quantity - filled_quantity) * price), 0) AS total
          FROM orders
         WHERE side = 'buy'
           AND status IN ({','.join('?' for _ in OPEN_EXPOSURE_ORDER_STATUSES)})
           AND quantity > filled_quantity
           {where}
        """,
        (*OPEN_EXPOSURE_ORDER_STATUSES, *params),
    )
    return float(row["total"] if row else 0.0)


def _compute_existing_exposure(db, column: str, value: str, exclude_order_id: str) -> float:
    """Compute paper exposure for a market or wallet.

    Exposure is open long position cost basis plus other open buy order notional.
    Filled orders are represented by positions and are intentionally not summed
    again, so sells and partial closes reduce risk immediately.
    """
    if column not in {"market_id", "wallet_id"}:
        raise ValueError(f"Unsupported exposure column: {column}")
    return _position_exposure_where(db, f" WHERE {column} = ?", (value,)) + _open_order_exposure_where(
        db, f" AND {column} = ? AND id != ?", (value, exclude_order_id)
    )


def _compute_existing_outcome_exposure(db, market_id: str, outcome: str, exclude_order_id: str) -> float:
    """Compute total exposure for a (market, outcome) pair across all wallets."""
    return _position_exposure_where(db, " WHERE market_id = ? AND outcome = ?", (market_id, outcome)) + _open_order_exposure_where(
        db, " AND market_id = ? AND outcome = ? AND id != ?", (market_id, outcome, exclude_order_id)
    )


def _compute_existing_global_exposure(db, exclude_order_id: str) -> float:
    """Compute total global paper exposure across open positions and buy orders."""
    return _position_exposure_where(db) + _open_order_exposure_where(db, " AND id != ?", (exclude_order_id,))


def _adjust_usdc_balance(db, wallet_id: str, delta: float, now: datetime, is_sample: bool) -> None:
    """Apply a simulated paper-cash balance delta when a USDC row exists.

    The API never touches real funds. If no USDC balance has been seeded for a
    wallet, leave balances absent rather than fabricating starting cash.
    """
    row = db.fetchone(
        "SELECT id, amount FROM wallet_balances WHERE wallet_id = ? AND currency = 'USDC' ORDER BY id DESC LIMIT 1",
        (wallet_id,),
    )
    if row is None:
        return
    new_amount = float(row["amount"]) + delta
    if new_amount < -1e-9:
        raise ValueError("Insufficient simulated USDC balance for paper cash update.")
    db.execute(
        "UPDATE wallet_balances SET amount = ?, as_of = ?, is_sample = ? WHERE id = ?",
        (max(0.0, new_amount), now.isoformat(), int(is_sample), row["id"]),
    )


def _upsert_position(db, market_id: str, wallet_id: str, outcome: str, side: str, filled_qty: float, price: float, now: datetime, is_sample: bool) -> dict[str, float]:
    """Create or update a position after an order fill.

    Buys increase long exposure using weighted-average cost. Sells require an
    existing long, realize P&L using that average-cost basis, reduce quantity,
    and leave a zero-quantity row on full close so historical realized P&L is
    retained by the current schema.
    """
    position_id = str(uuid5(NAMESPACE_URL, f"polycopy-position:{market_id}:{wallet_id}:{outcome}"))
    existing = db.fetchone(
        "SELECT * FROM positions WHERE market_id = ? AND wallet_id = ? AND outcome = ?",
        (market_id, wallet_id, outcome),
    )
    if side == "sell":
        if existing is None:
            raise ValueError("Cannot approve sell order without an open position.")
        held_qty = float(existing["quantity"])
        if filled_qty > held_qty + 1e-9:
            raise ValueError(f"Cannot approve sell quantity {filled_qty:g}; only {held_qty:g} held.")
        avg_entry = float(existing["avg_entry_price"])
        realized = (price - avg_entry) * filled_qty
        new_qty = max(0.0, held_qty - filled_qty)
        db.execute(
            """
            UPDATE positions
            SET quantity = ?, current_price = ?, realized_pnl = ?, updated_at = ?
            WHERE market_id = ? AND wallet_id = ? AND outcome = ?
            """,
            (new_qty, price, float(existing["realized_pnl"]) + realized, now.isoformat(), market_id, wallet_id, outcome),
        )
        return {"realized_pnl": realized, "remaining_quantity": new_qty}

    if existing is None:
        db.execute(
            """
            INSERT INTO positions (
                id, market_id, wallet_id, outcome, quantity, avg_entry_price,
                current_price, realized_pnl, opened_at, updated_at, is_sample
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (position_id, market_id, wallet_id, outcome, filled_qty, price, price, 0.0, now.isoformat(), now.isoformat(), is_sample),
        )
        return {"realized_pnl": 0.0, "remaining_quantity": filled_qty}
    else:
        existing_qty = float(existing["quantity"])
        new_qty = existing_qty + filled_qty
        new_avg = (existing_qty * float(existing["avg_entry_price"]) + filled_qty * price) / new_qty
        db.execute(
            """
            UPDATE positions
            SET quantity = ?, avg_entry_price = ?, current_price = ?, updated_at = ?
            WHERE market_id = ? AND wallet_id = ? AND outcome = ?
            """,
            (new_qty, new_avg, price, now.isoformat(), market_id, wallet_id, outcome),
        )
        return {"realized_pnl": 0.0, "remaining_quantity": new_qty}


def _check_idempotency(scope: str, request_hash: str) -> tuple[bool, str]:
    """Check if a request is a duplicate using the SQLite idempotency store.

    Returns (is_duplicate, message).
    """
    prev = _idempotency_store.lookup(scope, request_hash)
    if prev:
        created = prev.get("_created_at", "unknown")
        return True, f"Duplicate submission: scope={scope} hash={request_hash[:12]}... first seen at {created}"
    return False, f"New submission: scope={scope} hash={request_hash[:12]}..."


def _make_idempotency_key(prefix: str, **payload: object) -> str:
    """Build a deterministic idempotency key from parts."""
    return IdempotencyStore.compute_request_hash(prefix, **payload)


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
    """Preview a paper order — estimate fill price, fees, spread, risk gates.

    Uses the real FillModel + RiskGate pipeline: executable bid/ask,
    configurable slippage/fees/review delay, source-entry deterioration,
    price-impact, staleness, liquidity, exposure, kill-switch, paper-mode checks.

    Returns full preview fields. Missing market data (no bid/ask snapshot)
    returns status=INCOMPLETE (HTTP 422).
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

    settings = get_settings()
    now = datetime.now(timezone.utc)

    # ── Gather market data snapshot ───────────────────────────────────────────
    snapshot = _bidask_provider.get_snapshot(str(request.market_id), request.outcome)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"No bid/ask snapshot for market {request.market_id} outcome {request.outcome}. PAPER ONLY — fill data required.",
        )

    bid = snapshot.bid
    ask = snapshot.ask
    spread = snapshot.spread
    snapshot_time = snapshot.snapshot_time

    # Source-entry deterioration
    source_entry_age: float | None = None
    if request.received_at:
        try:
            received_dt = datetime.fromisoformat(request.received_at)
            if received_dt.tzinfo is None:
                received_dt = received_dt.replace(tzinfo=timezone.utc)
            source_entry_age = (now - received_dt).total_seconds()
        except (ValueError, TypeError):
            source_entry_age = None

    is_stale = False
    if source_entry_age is not None and settings.staleness_seconds > 0:
        is_stale = source_entry_age > settings.staleness_seconds

    # ── Fill model ────────────────────────────────────────────────────────────
    from polycopy.risk.fill_model import FillModel
    from polycopy.risk.gates import (
        ExposureLimits,
        OrderKillSwitch,
        PaperMode,
        RiskGate,
    )

    fill_model = FillModel(default_fee_rate=settings.fill_fee_rate)
    paper_mode = PaperMode(settings.paper_mode)

    if request.side == "buy":
        # Buy side: walk the ask depth
        depth_model = snapshot.ask_depth_model()
    else:
        # Sell side: walk the bid depth
        depth_model = snapshot.bid_depth_model()

    quote = fill_model.quote_fill(
        side=request.side,
        quantity=request.quantity,
        depth=depth_model,
        fee_rate=settings.fill_fee_rate,
        is_sample=True,
    )

    estimated_fill = quote.expected_price
    slippage = quote.slippage
    estimated_fee = quote.fee
    fillable_qty = quote.fillable_volume
    is_complete = quote.is_complete_fill
    notional = estimated_fill * fillable_qty if fillable_qty > 0 else 0.0
    total_cost = quote.total_cost if fillable_qty > 0 else 0.0

    # Max loss: worst-case cost if filled at the far edge of the book
    depth_available = depth_model.total_volume
    worst_price = depth_model.levels[-1].price if depth_model.levels else (ask if request.side == "buy" else bid)
    fill_for_max = fillable_qty if fillable_qty > 0 else (quantity or 0.0)
    if request.side == "buy":
        max_loss = abs((worst_price - estimated_fill) * fill_for_max + estimated_fee)
    else:
        max_loss = abs((estimated_fill - worst_price) * fill_for_max + estimated_fee)

    # Price impact ratio
    price_impact = None
    if depth_available and depth_available > 0:
        price_impact = round(notional / depth_available, 6)

    # Spread cost
    if is_complete:
        if request.side == "buy":
            spread_cost = round((ask - bid) * fillable_qty, 6)
        else:
            spread_cost = round((ask - bid) * fillable_qty, 6)
    else:
        spread_cost = 0.0

    # ── Risk gates ────────────────────────────────────────────────────────────
    ks = OrderKillSwitch(active=settings.order_kill_switch)
    exposure_limits = ExposureLimits(
        max_per_market=settings.max_exposure_per_market,
        max_per_wallet=settings.max_exposure_per_wallet,
        max_per_outcome=settings.max_exposure_per_outcome,
        max_global=settings.max_exposure_global,
        max_order_size=settings.max_order_size,
    )
    risk_gate = RiskGate(
        kill_switch=ks,
        paper_mode=paper_mode,
        exposure_limits=exposure_limits,
    )

    order_notional = estimated_fill * request.quantity
    gate_result = risk_gate.check(order_notional=order_notional)

    passed_gates: list[str] = []
    failed_gates: list[str] = []
    if gate_result.is_blocked:
        failed_gates = [gate_result.gate_name]
    else:
        passed_gates = [gate_result.gate_name]

    # Stale source entry: extra gate flag
    if is_stale:
        failed_gates = failed_gates + ["source_entry_staleness"]

    # Review delay expires_at
    from polycopy.risk.fill_model import ReviewDelay
    if paper_mode == PaperMode.PAPER_MANUAL:
        review = ReviewDelay(delay_seconds=settings.review_delay_seconds, started_at=now)
        expires_at = review.expires_at.isoformat()
        review_delay = settings.review_delay_seconds
    else:
        expires_at = None
        review_delay = 0.0

    # Determine overall preview status
    if gate_result.is_blocked:
        preview_status = "rejected"
        rejection_reason = gate_result.reason
    elif not is_complete:
        preview_status = "incomplete"
        rejection_reason = None
    elif is_stale:
        preview_status = "rejected"
        rejection_reason = f"Source entry stale: {source_entry_age:.0f}s > threshold {settings.staleness_seconds:.0f}s"
    elif depth_available < request.quantity:
        preview_status = "incomplete"
        rejection_reason = f"Insufficient depth: {depth_available:.0f} available < {request.quantity:.0f} requested"
    else:
        preview_status = "pending"
        rejection_reason = None

    # Exposure impact (additional notional if filled)
    exposure_impact = order_notional

    return PaperOrderPreview(
        market_id=request.market_id,
        outcome=request.outcome,
        side=request.side,
        quantity=request.quantity,
        price=request.price,
        requested_price=request.price,
        estimated_fill_price=round(estimated_fill, 6),
        estimated_fee=round(estimated_fee, 6),
        estimated_total_cost=round(total_cost, 6),
        bid=bid,
        ask=ask,
        spread=round(spread, 6),
        spread_cost=round(spread_cost, 6),
        depth_available=depth_available if depth_available else None,
        fillable_quantity=fillable_qty if fillable_qty > 0 else None,
        is_complete_fill=is_complete,
        snapshot_timestamp=snapshot_time.isoformat(),
        slippage=round(slippage, 6),
        fee_rate=settings.fill_fee_rate,
        fee=round(estimated_fee, 6),
        review_delay_seconds=review_delay,
        expires_at=expires_at,
        source_entry_age_seconds=round(source_entry_age, 2) if source_entry_age is not None else None,
        staleness_seconds=settings.staleness_seconds,
        is_stale=is_stale,
        price_impact_ratio=price_impact,
        exposure_impact=round(exposure_impact, 6),
        max_loss=round(max_loss, 6),
        passed_gates=passed_gates,
        failed_gates=failed_gates,
        rejection_reason=rejection_reason,
        fill_model_version="polycopy-fill-v1",
        status=preview_status,
        is_sample=True,
    )


@app.post("/paper/approve", response_model=OrderView, tags=["paper"])
async def approve_paper_order(request: PaperOrderApproveRequest):
    """Approve a pending paper order through the PaperBroker workflow.

    Uses SQLite-backed idempotency keyed by (scope=paper_approve, order_id, notes hash).
    Replaying the same payload returns the stored result (idempotent).

    Loads request.order_id from SQLite, validates it is pending, runs
    current stale-price + risk checks, then transitions that exact order
    to filled. Creates/updates the associated position exactly once and
    persists a decision log entry against the same order.

    PAPER ONLY — no real trade execution.
    """
    from polycopy.risk.gates import ExposureLimits, PaperMode

    settings = get_settings()
    now = datetime.now(timezone.utc)

    scope = f"paper_approve:{request.idempotency_key}" if request.idempotency_key else "paper_approve"
    req_hash = IdempotencyStore.compute_request_hash(
        "paper_approve", order_id=str(request.order_id), notes=request.notes or "", idempotency_key=request.idempotency_key or ""
    )
    if request.idempotency_key:
        _idempotency_store._ensure_table()  # noqa: SLF001 - conflict check for optional client key
        existing_key = _idempotency_store.db.fetchone("SELECT request_hash FROM idempotency_keys WHERE scope = ? LIMIT 1", (scope,))
        if existing_key is not None and existing_key["request_hash"] != req_hash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Idempotency key was already used with a different payload")
    prev = _idempotency_store.lookup(scope, req_hash)
    if prev:
        return OrderView(
            id=UUID(cast(str, prev["id"])),
            market_id=UUID(cast(str, prev["market_id"])),
            wallet_id=UUID(cast(str, prev["wallet_id"])),
            side=cast(str, prev["side"]),
            order_type=cast(str, prev["order_type"]),
            outcome=cast(str, prev["outcome"]),
            quantity=float(cast(str, prev["quantity"])),
            price=float(cast(str, prev["price"])),
            status=cast(str, prev["status"]),
            filled_quantity=float(cast(str, prev["filled_quantity"])),
            created_at=datetime.fromisoformat(cast(str, prev["created_at"])),
            updated_at=datetime.fromisoformat(cast(str, prev["updated_at"])) if prev.get("updated_at") else None,
            is_sample=bool(prev.get("is_sample", True)),
        )

    # Load the existing order from SQLite
    db = get_database()
    row = db.fetchone("SELECT * FROM orders WHERE id = ?", (str(request.order_id),))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order {request.order_id} not found.",
        )

    current_status = row["status"]
    if current_status in ("filled",):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Order {request.order_id} is already filled.",
        )
    if current_status in ("cancelled", "rejected"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Order {request.order_id} is {current_status} and cannot be approved.",
        )
    if current_status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Order {request.order_id} is in status {current_status}, not pending.",
        )

    # Validate preview/order is not expired
    order_created_at = datetime.fromisoformat(row["created_at"]) if row["created_at"] else now
    if order_created_at.tzinfo is None:
        order_created_at = order_created_at.replace(tzinfo=timezone.utc)
    age_seconds = (now - order_created_at).total_seconds()
    if settings.order_preview_max_age_seconds > 0 and age_seconds > settings.order_preview_max_age_seconds:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Order {request.order_id} preview expired ({age_seconds:.0f}s old).",
        )

    # Re-run current stale-price check
    source_entry_age: float | None = None
    if row["created_at"]:
        try:
            created_dt = datetime.fromisoformat(row["created_at"])
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            source_entry_age = (now - created_dt).total_seconds()
        except (ValueError, TypeError):
            source_entry_age = None

    is_stale = False
    if source_entry_age is not None and settings.staleness_seconds > 0:
        is_stale = source_entry_age > settings.staleness_seconds
    if is_stale:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Order {request.order_id} source entry stale ({source_entry_age:.0f}s > {settings.staleness_seconds:.0f}s).",
        )

    # Re-run risk checks
    paper_mode = PaperMode(settings.paper_mode)
    exposure_limits = ExposureLimits(
        max_per_market=settings.max_exposure_per_market,
        max_per_wallet=settings.max_exposure_per_wallet,
        max_per_outcome=settings.max_exposure_per_outcome,
        max_global=settings.max_exposure_global,
        max_order_size=settings.max_order_size,
    )
    from polycopy.risk.gates import OrderKillSwitch, RiskGate

    ks = OrderKillSwitch(active=settings.order_kill_switch)
    risk_gate = RiskGate(
        kill_switch=ks,
        paper_mode=paper_mode,
        exposure_limits=exposure_limits,
    )

    side = str(row["side"])
    filled_quantity = float(row["quantity"])
    if side == "sell":
        existing_position = db.fetchone(
            "SELECT quantity FROM positions WHERE market_id = ? AND wallet_id = ? AND outcome = ?",
            (row["market_id"], row["wallet_id"], row["outcome"]),
        )
        held_qty = float(existing_position["quantity"]) if existing_position is not None else 0.0
        if existing_position is None or filled_quantity > held_qty + 1e-9:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot approve sell order: quantity {filled_quantity:g} exceeds held position {held_qty:g}.",
            )

    order_notional = 0.0 if side == "sell" else float(row["price"]) * filled_quantity
    # Compute current exposure excluding this order (it's pending, not filled)
    market_exposure = _compute_existing_exposure(db, "market_id", row["market_id"], exclude_order_id=str(request.order_id))
    wallet_exposure = _compute_existing_exposure(db, "wallet_id", row["wallet_id"], exclude_order_id=str(request.order_id))
    outcome_exposure = _compute_existing_outcome_exposure(db, row["market_id"], row["outcome"], exclude_order_id=str(request.order_id))
    global_exposure = _compute_existing_global_exposure(db, exclude_order_id=str(request.order_id))

    gate_result = risk_gate.check(
        order_notional=order_notional,
        market_exposure=market_exposure,
        wallet_exposure=wallet_exposure,
        outcome_exposure=outcome_exposure,
        global_exposure=global_exposure,
    )
    if gate_result.is_blocked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Risk gate blocked: {gate_result.gate_name} — {gate_result.reason}",
        )

    # Transition the exact order to filled
    db.execute(
        """
        UPDATE orders
        SET status = 'filled',
            filled_quantity = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (filled_quantity, now.isoformat(), str(request.order_id)),
    )

    # Create or update position
    fee = float(row["price"]) * filled_quantity * settings.fill_fee_rate
    slippage = 0.0
    try:
        position_metrics = _upsert_position(db, row["market_id"], row["wallet_id"], row["outcome"], side, filled_quantity, float(row["price"]), now, bool(row["is_sample"]))
        if side == "sell":
            _adjust_usdc_balance(db, row["wallet_id"], float(row["price"]) * filled_quantity - fee, now, bool(row["is_sample"]))
    except ValueError as exc:
        db.conn.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # Persist decision log
    decision_id = str(uuid5(NAMESPACE_URL, f"polycopy-decision:paper_approve:{request.order_id}"))
    db.execute(
        """
        INSERT OR IGNORE INTO decision_log (
            id, wallet_id, market_id, decision_type, signal_ids, order_id,
            rationale, metrics, created_at, is_sample
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            row["wallet_id"],
            row["market_id"],
            "paper_approve",
            "[]",
            str(request.order_id),
            request.notes or "Operator approved PAPER ONLY simulated order.",
            json.dumps(
                {
                    "paper_only": True,
                    "status": "filled",
                    "filled_quantity": filled_quantity,
                    "fee": round(fee, 6),
                    "fee_rate": settings.fill_fee_rate,
                    "slippage": slippage,
                    "realized_pnl": round(position_metrics["realized_pnl"], 6),
                    "remaining_quantity": round(position_metrics["remaining_quantity"], 6),
                },
                sort_keys=True,
            ),
            now.isoformat(),
            bool(row["is_sample"]),
        ),
    )
    db.conn.commit()

    # Build result
    result: dict[str, object] = {
        "id": str(request.order_id),
        "market_id": row["market_id"],
        "wallet_id": row["wallet_id"],
        "side": row["side"],
        "order_type": row["order_type"],
        "outcome": row["outcome"],
        "quantity": float(row["quantity"]),
        "price": float(row["price"]),
        "status": "filled",
        "filled_quantity": filled_quantity,
        "created_at": row["created_at"],
        "updated_at": now.isoformat(),
        "is_sample": bool(row["is_sample"]),
    }

    # Store in idempotency store
    _idempotency_store.check_and_store(scope, req_hash, result)

    return OrderView(
        id=request.order_id,
        market_id=UUID(row["market_id"]),
        wallet_id=UUID(row["wallet_id"]),
        side=row["side"],
        order_type=row["order_type"],
        outcome=row["outcome"],
        quantity=float(row["quantity"]),
        price=float(row["price"]),
        status="filled",
        filled_quantity=filled_quantity,
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else now,
        updated_at=now,
        is_sample=bool(row["is_sample"]),
    )


@app.post("/paper/reject", response_model=OrderView, tags=["paper"])
async def reject_paper_order(request: PaperOrderRejectRequest):
    """Reject (cancel) a pending paper order through the PaperBroker workflow.

    Uses SQLite-backed idempotency keyed by (scope=paper_reject, order_id, notes hash).
    Replaying the same payload returns the stored result (idempotent).

    Loads request.order_id from SQLite, validates it is pending, transitions
    that exact order to cancelled/rejected. Persists operator note and one
    decision log entry against the same order.

    PAPER ONLY — no real trade execution.
    """
    now = datetime.now(timezone.utc)

    scope = f"paper_reject:{request.idempotency_key}" if request.idempotency_key else "paper_reject"
    req_hash = IdempotencyStore.compute_request_hash(
        "paper_reject", order_id=str(request.order_id), notes=request.notes or "", idempotency_key=request.idempotency_key or ""
    )
    if request.idempotency_key:
        _idempotency_store._ensure_table()  # noqa: SLF001 - conflict check for optional client key
        existing_key = _idempotency_store.db.fetchone("SELECT request_hash FROM idempotency_keys WHERE scope = ? LIMIT 1", (scope,))
        if existing_key is not None and existing_key["request_hash"] != req_hash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Idempotency key was already used with a different payload")
    prev = _idempotency_store.lookup(scope, req_hash)
    if prev:
        # Replay stored result
        return OrderView(
            id=UUID(cast(str, prev["id"])),
            market_id=UUID(cast(str, prev["market_id"])),
            wallet_id=UUID(cast(str, prev["wallet_id"])),
            side=cast(str, prev["side"]),
            order_type=cast(str, prev["order_type"]),
            outcome=cast(str, prev["outcome"]),
            quantity=float(cast(str, prev["quantity"])),
            price=float(cast(str, prev["price"])),
            status=cast(str, prev["status"]),
            filled_quantity=float(cast(str, prev["filled_quantity"])),
            created_at=datetime.fromisoformat(cast(str, prev["created_at"])),
            updated_at=datetime.fromisoformat(cast(str, prev["updated_at"])) if prev.get("updated_at") else None,
            is_sample=bool(prev.get("is_sample", True)),
        )

    # Load the existing order from SQLite
    db = get_database()
    row = db.fetchone("SELECT * FROM orders WHERE id = ?", (str(request.order_id),))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order {request.order_id} not found.",
        )

    current_status = row["status"]
    if current_status == "filled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Order {request.order_id} is already filled and cannot be rejected.",
        )
    if current_status in ("cancelled", "rejected"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Order {request.order_id} is already {current_status}.",
        )
    if current_status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Order {request.order_id} is in status {current_status}, not pending.",
        )

    # Transition the exact order to cancelled
    db.execute(
        """
        UPDATE orders
        SET status = 'cancelled',
            updated_at = ?
        WHERE id = ?
        """,
        (now.isoformat(), str(request.order_id)),
    )

    # Persist decision log with operator note
    decision_id = str(uuid5(NAMESPACE_URL, f"polycopy-decision:paper_reject:{request.order_id}"))
    db.execute(
        """
        INSERT OR IGNORE INTO decision_log (
            id, wallet_id, market_id, decision_type, signal_ids, order_id,
            rationale, metrics, created_at, is_sample
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            row["wallet_id"],
            row["market_id"],
            "paper_reject",
            "[]",
            str(request.order_id),
            request.notes or "Operator rejected PAPER ONLY simulated order.",
            json.dumps({"paper_only": True, "status": "cancelled"}, sort_keys=True),
            now.isoformat(),
            bool(row["is_sample"]),
        ),
    )
    db.conn.commit()

    # Build result
    result: dict[str, object] = {
        "id": str(request.order_id),
        "market_id": row["market_id"],
        "wallet_id": row["wallet_id"],
        "side": row["side"],
        "order_type": row["order_type"],
        "outcome": row["outcome"],
        "quantity": float(row["quantity"]),
        "price": float(row["price"]),
        "status": "cancelled",
        "filled_quantity": 0.0,
        "created_at": row["created_at"],
        "updated_at": now.isoformat(),
        "is_sample": bool(row["is_sample"]),
    }

    # Store in idempotency store
    _idempotency_store.check_and_store(scope, req_hash, result)

    return OrderView(
        id=request.order_id,
        market_id=UUID(row["market_id"]),
        wallet_id=UUID(row["wallet_id"]),
        side=row["side"],
        order_type=row["order_type"],
        outcome=row["outcome"],
        quantity=float(row["quantity"]),
        price=float(row["price"]),
        status="cancelled",
        filled_quantity=0.0,
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else now,
        updated_at=now,
        is_sample=bool(row["is_sample"]),
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
    """Check if an idempotency key has already been processed (SQLite-backed).

    Returns whether the key is a duplicate. Does NOT register the key.
    """
    # key is treated as a request hash; we check both approve and reject scopes
    for scope in ("paper_approve", "paper_reject"):
        prev = _idempotency_store.lookup(scope, key)
        if prev:
            return IdempotencyKeyResponse(
                key=key,
                is_duplicate=True,
                message=f"Key {key[:16]}... was already processed in scope={scope}.",
            )
    return IdempotencyKeyResponse(
        key=key,
        is_duplicate=False,
        message=f"Key {key[:16]}... is new (not yet registered).",
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
