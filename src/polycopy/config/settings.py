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

    # ── Polymarket credentials (ONLY for broker_mode=polymarket) ───────────
    polymarket_private_key: Optional[str] = Field(default=None, description="Wallet private key. NEVER set in paper mode.")

    # ── Database ────────────────────────────────────────────────────────────
    db_path: Path = Field(default=Path("polycopy.db"), description="SQLite database path.")
    db_echo: bool = Field(default=False, description="Echo SQL statements (debug).")

    # ── Snapshot provenance ─────────────────────────────────────────────────
    snapshot_dir: Path = Field(default=Path("data/snapshots"), description="Directory for raw API snapshots.")
    snapshot_hash_algo: str = Field(default="sha256", description="Hash algorithm for snapshot provenance.")

    # ── Logging ─────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO", description="Logging level.")

    # ── HTTP ────────────────────────────────────────────────────────────────
    http_timeout_seconds: float = Field(default=10.0, description="HTTP request timeout.")
    http_rate_limit_rps: float = Field(default=2.0, description="Max requests per second to public APIs.")

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
