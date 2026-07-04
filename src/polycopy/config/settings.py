"""Versioned application settings with env overrides and fail-closed validation.

All Polymarket secrets (private key, API key, etc.) are FORBIDDEN in paper mode.
The config will raise on startup if contradictory flags are set.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BrokerMode(str, enum.Enum):
    """Allowed broker modes. 'paper' is the safe default; 'polymarket' requires real credentials."""

    PAPER = "paper"
    POLYMARKET = "polymarket"


class Settings(BaseSettings):
    """Root application settings.

    Environment variables are prefixed with POLYCOPY_ and loaded from .env if present.
    Validation is fail-closed: contradictory or dangerous combinations raise immediately.
    """

    model_config = SettingsConfigDict(
        env_prefix="POLYCOPY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # ignore unknown env vars
    )

    # ── Versioning ──────────────────────────────────────────────────────────
    config_version: int = Field(default=1, description="Config schema version. Bump on breaking changes.")

    # ── Broker ──────────────────────────────────────────────────────────────
    broker_mode: BrokerMode = Field(default=BrokerMode.PAPER, description="Broker mode: 'paper' or 'polymarket'.")

    # ── Polymarket public endpoints (read-only, no auth) ───────────────────
    gamma_base_url: str = Field(default="https://gamma-api.polymarket.com", description="Gamma API base URL.")
    clob_base_url: str = Field(default="https://clob.polymarket.com", description="CLOB API base URL.")
    # NOTE: data-api.polymarket.com exposes /trades (full wallet-attributed trade
    # history, no auth) and is the actual public source for SourceTrade ingestion.
    # The CLOB /trades endpoint requires authentication (HTTP 401) and is NOT used.
    data_api_base_url: str = Field(
        default="https://data-api.polymarket.com",
        description="Public data API base URL. Provides unauthenticated /trades and /positions.",
    )
    # Maximum global-trades window per fetch (data-api hard-caps at ~1000). This
    # window is filtered client-side per market since data-api ignores the
    # conditionId filter parameter.
    data_api_window_size: int = Field(
        default=1000,
        description="Max trades returned by one data-api call (hard cap ~1000).",
    )
    # Per-market rate: sleep this long between per-market data-api calls.
    data_api_request_interval_seconds: float = Field(
        default=0.25,
        description="Seconds to sleep between per-market trade fetches.",
    )

    # ── CLOB order-book adapter (PR-3, read-only, disabled by default) ─────
    # ``clob_enabled`` is the runtime gate. When False, no production code
    # path instantiates ``PolymarketClobClient`` — the live book adapter is
    # only constructed explicitly by tests with a mocked transport. This
    # default of False is the load-bearing safety invariant for PR-3:
    # deploying PR-3 must not start hitting clob.polymarket.com.
    clob_enabled: bool = Field(
        default=False,
        description=(
            "Master gate for the live CLOB order-book adapter. MUST remain "
            "False in production until PR-3's runtime wiring is approved. "
            "Tests can override to True locally; no production code path "
            "consults this flag to make a network call without an explicit "
            "factory call that the caller controls."
        ),
    )

    # ── Specialist-metric aggregation (PR #20, dormant by default) ─────────
    # ``specialist_aggregations_enabled`` is the **single explicit
    # activation switch** for the PR #20 evidence layer. When False
    # (default), :func:`scripts.run_scan.run_scan` skips Step 5f
    # entirely — no writes to ``wallet_specialist_aggregations``,
    # no schema v13 reads, no observable behavior change.
    #
    # To activate Step 5f in production after the PR #19 24-hour
    # observation is clean, the **only** required change is to set
    # the env variable:
    #
    #     POLYCOPY_SPECIALIST_AGGREGATIONS_ENABLED=true
    #
    # (or flip the default in this Settings class from False to
    # True). No code change, no separate feature PR, no new
    # consumer, no formula change. The activation is a one-line /
    # one-word edit; it does not enable live trading, does not
    # create approvals / orders / positions / fills, and does not
    # touch ``TimeoutStartSec``.
    #
    # This default of False is the load-bearing safety invariant
    # for PR #20: deploying PR #20 must not start writing
    # specialist aggregation rows until the operator flips the
    # switch.
    specialist_aggregations_enabled: bool = Field(
        default=False,
        description=(
            "PR #20: enable Step 5f (specialist-metric aggregation evidence "
            "table). MUST remain False until the PR #19 24-hour observation "
            "is accepted. Activating this flag does NOT enable live trading, "
            "does NOT consume the new rows in any formula, and does NOT "
            "create orders/positions/approvals. One-line activation: set "
            "POLYCOPY_SPECIALIST_AGGREGATIONS_ENABLED=true."
        ),
    )

    # PR #20 specialist-metric aggregation cap (mirrors PR #19's
    # ``max_wallet_scores`` invariant). Default 50 — bounded, low,
    # and operator-overridable. This cap is only consulted when
    # ``specialist_aggregations_enabled`` is True.
    specialist_aggregations_max_rows_per_run: int = Field(
        default=50,
        description=(
            "PR #20: cap on ``wallet_specialist_aggregations`` rows written "
            "per run_scan invocation. Default 50; only consulted when "
            "specialist_aggregations_enabled is True."
        ),
    )

    clob_base_url: str = Field(
        default="https://clob.polymarket.com",
        description=(
            "Base URL for the public Polymarket CLOB HTTP API. The adapter "
            "appends ``/book?token=<id>`` at request time. No trailing slash "
            "is assumed; the adapter strips a single trailing slash defensively."
        ),
    )
    clob_timeout_seconds: float = Field(
        default=10.0,
        description="HTTP timeout for CLOB /book calls (seconds).",
    )
    clob_max_retries: int = Field(
        default=3,
        description=(
            "Max retry attempts for transient CLOB /book failures (5xx, "
            "timeout). 429s are classified RATE_LIMITED and surfaced as "
            "bounded fetch status, not retried beyond this cap."
        ),
    )
    clob_rpm: int = Field(
        default=30,
        description=(
            "Polycopy safety rate limit for the CLOB /book endpoint "
            "(requests per minute). NOT the platform's documented rate "
            "limit — it is a conservative Polycopy-side ceiling to bound "
            "blast radius during a future scan pass."
        ),
    )

    # ── Polymarket credentials (ONLY for broker_mode=polymarket) ───────────
    polymarket_private_key: Optional[str] = Field(default=None, description="Wallet private key. NEVER set in paper mode.")

    # ── Database ────────────────────────────────────────────────────────────
    db_path: Path = Field(default=Path("polycopy.db"), description="SQLite database path.")
    db_echo: bool = Field(default=False, description="Echo SQL statements (debug).")
    enable_demo_data: bool = Field(
        default=False,
        description=(
            "Explicit demo/sample API mode. When false, empty real DB tables return empty "
            "collections and no sample fallback. When true, demo records may be returned and "
            "must be visibly labeled is_sample=True / DEMO DATA / SAMPLE DATA."
        ),
    )

    # ── Snapshot provenance ─────────────────────────────────────────────────
    snapshot_dir: Path = Field(default=Path("data/snapshots"), description="Directory for raw API snapshots.")
    snapshot_hash_algo: str = Field(default="sha256", description="Hash algorithm for snapshot provenance.")

    # ── Logging ─────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO", description="Logging level.")

    # ── HTTP ────────────────────────────────────────────────────────────────
    http_timeout_seconds: float = Field(default=10.0, description="HTTP request timeout.")
    http_rate_limit_rps: float = Field(default=2.0, description="Max requests per second to public APIs.")

    # ── Wallet discovery ────────────────────────────────────────────────────
    manual_watchlist: list[str] = Field(
        default_factory=list,
        description="Hardcoded wallet addresses to track (never auto-discovered).",
    )

    # ── Trade detection / dedup ─────────────────────────────────────────────
    staleness_seconds: float = Field(
        default=120.0,
        description="Trades older than this are flagged as stale (seconds).",
    )
    dedup_window_seconds: float = Field(
        default=60.0,
        description="Window for deduplicating trades (seconds).",
    )
    dedup_granularity_seconds: int = Field(
        default=60,
        description="Timestamp truncation granularity for dedup key (seconds).",
    )

    # ── Scoring engine ─────────────────────────────────────────────────────
    score_copy_threshold: float = Field(
        default=70.0,
        description="Minimum score for COPY_CANDIDATE verdict (0-100).",
    )
    score_watchlist_threshold: float = Field(
        default=50.0,
        description="Minimum score for WATCHLIST verdict (0-100).",
    )

    # ── Related-wallet detection ───────────────────────────────────────────
    related_min_signals: int = Field(
        default=2,
        description="Minimum signals required to flag a wallet as possibly related.",
    )
    related_confidence_threshold: float = Field(
        default=0.4,
        description="Minimum heuristic confidence to consider a related-wallet plausible.",
    )

    # ── Paper trading modes (P04) ────────────────────────────────────────
    paper_mode: str = Field(
        default="paper_manual",
        description="Paper mode: research_only / paper_manual / paper_auto.",
    )
    order_kill_switch: bool = Field(
        default=False,
        description="Global kill switch — when True, ALL order creation is blocked.",
    )

    # ── Exposure limits (P04) ────────────────────────────────────────────
    max_exposure_per_market: float = Field(
        default=0.0,
        description="Max notional exposure per market (0 = unlimited).",
    )
    max_exposure_per_wallet: float = Field(
        default=0.0,
        description="Max notional exposure per wallet across all markets (0 = unlimited).",
    )
    max_exposure_per_outcome: float = Field(
        default=0.0,
        description="Max notional exposure per (market, outcome) pair (0 = unlimited).",
    )
    max_exposure_global: float = Field(
        default=0.0,
        description="Max global notional exposure across all wallets (0 = unlimited).",
    )
    max_order_size: float = Field(
        default=0.0,
        description="Max notional size of a single order (0 = unlimited).",
    )

    # ── Fill model (P04) ─────────────────────────────────────────────────
    fill_fee_rate: float = Field(
        default=0.001,
        description="Fee rate applied to paper order notional (0.1% default).",
    )
    review_delay_seconds: float = Field(
        default=30.0,
        description="Delay before paper orders can fill in paper_manual mode.",
    )
    order_preview_max_age_seconds: float = Field(
        default=3600.0,
        description="Max age (seconds) of a pending order before it expires and cannot be approved.",
    )
    use_conservative_mark: bool = Field(
        default=False,
        description="If True, mark positions at bid price (worst-case) instead of mid.",
    )

    # ── Fail-closed validators ──────────────────────────────────────────────

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}, got {v!r}")
        return upper

    @field_validator("snapshot_hash_algo")
    @classmethod
    def _validate_hash_algo(cls, v: str) -> str:
        import hashlib

        if v not in hashlib.algorithms_available:
            raise ValueError(f"snapshot_hash_algo {v!r} not available; see hashlib.algorithms_available")
        return v

    @field_validator("paper_mode")
    @classmethod
    def _validate_paper_mode(cls, v: str) -> str:
        allowed = {"research_only", "paper_manual", "paper_auto"}
        if v not in allowed:
            raise ValueError(f"paper_mode must be one of {allowed}, got {v!r}")
        return v

    @field_validator("clob_base_url")
    @classmethod
    def _validate_clob_base_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("clob_base_url must be a non-empty URL")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("clob_base_url must start with http:// or https://")
        # Strip a single trailing slash defensively; the adapter does the same.
        return v.rstrip("/") if v.endswith("/") else v

    @field_validator("clob_timeout_seconds")
    @classmethod
    def _validate_clob_timeout_seconds(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"clob_timeout_seconds must be > 0, got {v!r}")
        return float(v)

    @field_validator("clob_max_retries")
    @classmethod
    def _validate_clob_max_retries(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"clob_max_retries must be >= 0, got {v!r}")
        return int(v)

    @field_validator("clob_rpm")
    @classmethod
    def _validate_clob_rpm(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"clob_rpm must be >= 0, got {v!r}")
        return int(v)

    @model_validator(mode="after")
    def _fail_closed_no_secrets_in_paper_mode(self) -> "Settings":
        """Fail-closed: reject private key in paper mode."""
        if self.broker_mode == BrokerMode.PAPER and self.polymarket_private_key is not None:
            raise ValueError(
                "polymarket_private_key is set but broker_mode is 'paper'. "
                "Clear the key or switch to broker_mode='polymarket'."
            )
        return self


# ── Singleton accessor ──────────────────────────────────────────────────────────

_settings: Optional[Settings] = None


def get_settings(reload: bool = False) -> Settings:
    """Return the cached Settings instance. Use reload=True to re-read env."""
    global _settings
    if _settings is None or reload:
        _settings = Settings()
    return _settings
