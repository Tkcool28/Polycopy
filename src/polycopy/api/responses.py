"""Shared Pydantic response models for the Polycopy API.

All models use Pydantic v2 for validation and JSON serialization.
Sample/fixture data is explicitly labeled with is_sample=True.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# ── Common envelope ──────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    """Health check response."""
    status: str = Field(description="Service health status.", examples=["ok"])
    version: str = Field(description="Application version.")
    is_sample_data: bool = Field(default=False, description="True if any sample data is loaded.")


class SystemStatusResponse(BaseModel):
    """System status overview."""
    config_version: int = Field(description="Schema version of the running config.")
    broker_mode: str = Field(description="Current broker mode.")
    paper_mode: str = Field(description="Current paper trading mode.")
    order_kill_switch: bool = Field(description="Whether the order kill switch is engaged.")
    is_live: bool = Field(description="Whether the broker executes real trades.")
    db_path: str = Field(description="Path to the SQLite database.")
    http_timeout_seconds: float = Field(description="HTTP request timeout.")
    log_level: str = Field(description="Current log level.")
    is_sample_data: bool = Field(default=False, description="True if sample data is loaded.")


# ── Scan / discovery ─────────────────────────────────────────────────────────

class ScanResult(BaseModel):
    """A single wallet scan result from discovery."""
    address: str = Field(description="Wallet address.")
    label: str = Field(description="Human-readable label.")
    sources: list[str] = Field(default_factory=list, description="Discovery sources.")
    source_count: int = Field(description="Number of discovery sources.")
    score: Optional[float] = Field(default=None, description="Copyability score (0-100) if scored.")
    verdict: Optional[str] = Field(default=None, description="Verdict if scored.")
    is_sample: bool = Field(default=False)


class ScanResponse(BaseModel):
    """Response for scan listing endpoint."""
    scans: list[ScanResult] = Field(default_factory=list)
    total_count: int = Field(description="Total wallets scanned.")
    is_sample_data: bool = Field(default=False)


# ── Wallets ───────────────────────────────────────────────────────────────────

class WalletBalanceView(BaseModel):
    currency: str
    amount: float
    as_of: datetime
    is_sample: bool = False


class WalletDetailView(BaseModel):
    id: UUID
    address: str
    label: str
    balances: list[WalletBalanceView] = Field(default_factory=list)
    is_sample: bool = False


class WalletsResponse(BaseModel):
    wallets: list[WalletDetailView] = Field(default_factory=list)
    total_count: int
    is_sample_data: bool = False


# ── Signals ────────────────────────────────────────────────────────────────────

class SignalView(BaseModel):
    id: UUID
    market_id: UUID
    source: str
    strength: str
    confidence: float
    edge_estimate: float
    predicted_prob: float
    market_prob: float
    reasoning: str
    produced_at: datetime
    is_sample: bool = False


class SignalsResponse(BaseModel):
    signals: list[SignalView] = Field(default_factory=list)
    total_count: int
    is_sample_data: bool = False


# ── Paper orders ──────────────────────────────────────────────────────────────

class PaperOrderPreview(BaseModel):
    """Preview of a paper order before confirmation.

    Fields mirror the full paper-fill pipeline: executable bid/ask,
    spread/depth snapshot, configurable slippage, fees, review delay,
    source-entry deterioration, and all risk-gate/fill-mode checks.
    Missing market data returns HTTP 422 with status=INCOMPLETE.
    """
    market_id: UUID
    outcome: str
    side: str
    quantity: float
    price: float
    # requested fill vs simulated fill
    requested_price: float = Field(description="Original limit price submitted.")
    estimated_fill_price: float = Field(description="Expected fill price including slippage.")
    estimated_fee: float = Field(description="Estimated fee.")
    estimated_total_cost: float = Field(default=0.0, description="Total cost including fee.")
    # market spread/depth snapshot
    bid: Optional[float] = Field(default=None, description="Best bid at decision time (polymarket).")
    ask: Optional[float] = Field(description="Best ask at decision time (polymarket).")
    spread: Optional[float] = Field(default=None, description="ask - bid (market spread).")
    spread_cost: float = Field(default=0.0, description="Cost due to crossing the spread.")
    depth_available: Optional[float] = Field(default=None, description="Volume available at executable price.")
    fillable_quantity: Optional[float] = Field(default=None, description="Fillable quantity given depth.")
    is_complete_fill: bool = Field(default=True, description="True if full qty is fillable.")
    snapshot_timestamp: Optional[str] = Field(default=None, description="ISO timestamp of bid/ask snapshot.")
    # slippage / fees / review delay
    slippage: float = Field(default=0.0, description="Price impact vs best price.")
    fee_rate: float = Field(default=0.001, description="Fee rate applied to notional.")
    fee: float = Field(default=0.0, description="Absolute fee amount = notional * fee_rate.")
    review_delay_seconds: float = Field(default=0.0, description="Seconds until paper_manual order can fill.")
    expires_at: Optional[str] = Field(default=None, description="ISO timestamp when preview quote expires.")
    # source-entry / deterioration / staleness
    source_entry_age_seconds: Optional[float] = Field(default=None, description="Age of underlying source trade at decision time.")
    staleness_seconds: float = Field(default=0.0, description="Configurable staleness threshold.")
    is_stale: bool = Field(default=False, description="True if source entry is older than staleness_seconds.")
    # risk / price-impact / liquidity / exposure
    price_impact_ratio: Optional[float] = Field(default=None, description="Order notional / depth_available (0-1+).")
    exposure_impact: float = Field(default=0.0, description="Additional global exposure if filled.")
    max_loss: float = Field(default=0.0, description="Worst-case cost if filled at worst price.")
    passed_gates: list[str] = Field(default_factory=list, description="Gate names that passed review.")
    failed_gates: list[str] = Field(default_factory=list, description="Gate names that blocked or flagged.")
    rejection_reason: Optional[str] = Field(default=None, description="Human-readable rejection reason if blocked.")
    # meta
    fill_model_version: str = Field(default="polycopy-fill-v1", description="Fill model identifier for audit.")
    status: str = Field(description=" preview status: pending | rejected | incomplete | expired.")
    is_sample: bool = False


class PaperOrderPreviewRequest(BaseModel):
    """Request body for previewing a paper order."""
    market_id: UUID = Field(description="Market ID for the paper order preview.")
    outcome: str = Field(min_length=1, max_length=100, description="Outcome label to trade.")
    side: str = Field(pattern="^(buy|sell)$", description="Paper order side: buy or sell.")
    quantity: float = Field(gt=0, le=1_000_000, description="Paper quantity to preview.")
    price: float = Field(ge=0.0, le=1.0, description="Limit price for the paper preview.")
    source_trade_id: Optional[str] = Field(default=None, description="Source trade ID that triggered this order (for deterioration/staleness tracking).")
    received_at: Optional[str] = Field(default=None, description="ISO timestamp when the source trade was first detected (UTC).")


class PaperOrderApproveRequest(BaseModel):
    """Request to approve (confirm and fill) a pending paper order."""
    order_id: UUID = Field(description="ID of the pending order to approve.")
    notes: Optional[str] = Field(default=None, description="Optional operator rationale / decision note.")


class PaperOrderRejectRequest(BaseModel):
    """Request to reject (cancel) a pending paper order."""
    order_id: UUID = Field(description="ID of the pending order to reject.")
    notes: Optional[str] = Field(default=None, description="Operator rationale for rejection.")


class OrderView(BaseModel):
    id: UUID
    market_id: UUID
    wallet_id: UUID
    side: str
    order_type: str
    outcome: str
    quantity: float
    price: float
    status: str
    filled_quantity: float
    signal_id: Optional[UUID] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    is_sample: bool = False


class OrdersResponse(BaseModel):
    orders: list[OrderView] = Field(default_factory=list)
    total_count: int
    is_sample_data: bool = False


# ── Positions & portfolio ────────────────────────────────────────────────────

class PositionView(BaseModel):
    id: UUID
    market_id: UUID
    wallet_id: UUID
    outcome: str
    quantity: float
    avg_entry_price: float
    current_price: float
    realized_pnl: float
    unrealized_pnl: float = Field(description="Computed: (current_price - avg_entry_price) * quantity.")
    opened_at: datetime
    updated_at: Optional[datetime] = None
    is_sample: bool = False


class PositionsResponse(BaseModel):
    positions: list[PositionView] = Field(default_factory=list)
    total_count: int
    total_unrealized_pnl: float
    total_cost_basis: float
    is_sample_data: bool = False


class PortfolioSummary(BaseModel):
    """Portfolio summary across all wallets."""
    total_positions: int
    total_cost_basis: float
    total_market_value: float
    total_unrealized_pnl: float
    total_realized_pnl: float
    total_pnl: float
    wallet_count: int
    is_sample_data: bool = False


# ── Decision log ──────────────────────────────────────────────────────────────

class DecisionLogView(BaseModel):
    id: UUID
    wallet_id: UUID
    market_id: UUID
    decision_type: str
    signal_ids: list[UUID] = Field(default_factory=list)
    order_id: Optional[UUID] = None
    rationale: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    is_sample: bool = False


class DecisionLogResponse(BaseModel):
    entries: list[DecisionLogView] = Field(default_factory=list)
    total_count: int
    is_sample_data: bool = False


# ── Experiment metrics ────────────────────────────────────────────────────────

class ExperimentMetricView(BaseModel):
    id: UUID
    label: str
    strategy_config: dict[str, Any] = Field(default_factory=dict)
    status: str
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    result_summary: dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None
    is_sample: bool = False


class ExperimentMetricsResponse(BaseModel):
    experiments: list[ExperimentMetricView] = Field(default_factory=list)
    total_count: int
    profitable_count: int
    is_sample_data: bool = False


# ── Data health ────────────────────────────────────────────────────────────────

class SourceHealthView(BaseModel):
    source: str
    last_success_at: Optional[datetime] = None
    last_attempt_at: Optional[datetime] = None
    status: str = Field(description="ok | partial | failure | disabled | unavailable")
    http_status: Optional[int] = None
    live_count: int = 0
    sample_count: int = 0
    error_message: str = ""
    freshness_seconds: Optional[float] = None
    is_sample: bool = False
    details: str = ""


class DataHealthResponse(BaseModel):
    sources: list[SourceHealthView] = Field(default_factory=list)
    snapshot_count: int
    oldest_snapshot: Optional[datetime] = None
    newest_snapshot: Optional[datetime] = None
    missing_capabilities: list[str] = Field(default_factory=list)
    overall_status: str = Field(description="healthy | degraded | unavailable")


# ── Configuration display (secrets excluded) ──────────────────────────────────

class ConfigView(BaseModel):
    """Configuration display — secrets (private keys, tokens) are NEVER included."""
    config_version: int
    broker_mode: str
    gamma_base_url: str
    clob_base_url: str
    paper_mode: str
    order_kill_switch: bool
    max_exposure_per_market: float
    max_exposure_per_wallet: float
    max_exposure_per_outcome: float
    max_exposure_global: float
    max_order_size: float
    fill_fee_rate: float
    review_delay_seconds: float
    use_conservative_mark: bool
    staleness_seconds: float
    dedup_window_seconds: float
    score_copy_threshold: float
    score_watchlist_threshold: float
    http_timeout_seconds: float
    http_rate_limit_rps: float
    log_level: str
    snapshot_hash_algo: str
    is_sample_data: bool = False


# ── Idempotency / dedup ───────────────────────────────────────────────────────

class IdempotencyKeyResponse(BaseModel):
    """Response for duplicate-submission check."""
    key: str = Field(description="Idempotency key submitted.")
    is_duplicate: bool = Field(description="True if this key was already processed.")
    message: str = Field(description="Status message.")


# ── Risk console ─────────────────────────────────────────────────────────────

class RiskGateView(BaseModel):
    """A single risk gate check result for display."""
    gate_name: str = Field(description="Name of the gate check.")
    verdict: str = Field(description="pass | blocked | needs_review")
    reason: str = Field(description="Human-readable reason.")
    is_sample: bool = False


class RiskConsoleResponse(BaseModel):
    """Risk console overview — current state of all risk gates."""
    kill_switch_active: bool = Field(description="Whether the order kill switch is engaged.")
    paper_mode: str = Field(description="Current paper trading mode.")
    exposure_limits: dict[str, float] = Field(description="Current exposure limits.")
    current_exposures: dict[str, float] = Field(description="Current exposure levels (sample).")
    gates: list[RiskGateView] = Field(description="Results of all gate checks.")
    is_sample_data: bool = False


# ── Error responses ──────────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    detail: str
    status_code: int
    is_sample_data: bool = False
