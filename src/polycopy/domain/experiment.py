"""Experiment run domain model — tracking backtests and paper trials."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ExperimentStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExperimentRun(BaseModel):
    """A single experiment/backtest/trial run."""

    id: UUID = Field(default_factory=uuid4, description="Unique run ID.")
    label: str = Field(description="Human-readable experiment label.")
    strategy_config: dict[str, Any] = Field(default_factory=dict, description="Strategy parameters snapshot.")
    status: ExperimentStatus = Field(default=ExperimentStatus.PENDING, description="Run status.")
    started_at: datetime | None = Field(default=None, description="When the run started (UTC).")
    ended_at: datetime | None = Field(default=None, description="When the run ended (UTC).")
    result_summary: dict[str, Any] = Field(default_factory=dict, description="Structured result metrics.")
    error_message: str | None = Field(default=None, description="Error message if failed.")
    is_sample: bool = Field(default=False, description="True if this is sample/fixture data, not live.")
