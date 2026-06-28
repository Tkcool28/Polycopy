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
