"""Raw snapshot domain model — provenance for ingested API data."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class RawSnapshot(BaseModel):
    """An immutable snapshot of raw API data with provenance metadata.

    The actual payload is stored as a file on disk (referenced by path).
    This model records the metadata: source, hash, timestamps, and fetch context.
    """

    id: UUID = Field(default_factory=uuid4, description="Unique snapshot ID.")
    source: str = Field(description="Data source, e.g. 'polymarket_gamma', 'polymarket_clob'.")
    endpoint: str = Field(description="API endpoint that was called.")
    query_params: dict[str, Any] = Field(default_factory=dict, description="Query parameters used.")
    file_path: str = Field(description="Relative path to the snapshot file on disk.")
    content_hash: str = Field(description="Hex digest of file contents (for integrity verification).")
    hash_algo: str = Field(default="sha256", description="Hash algorithm used.")
    content_type: str = Field(default="application/json", description="MIME type of the snapshot.")
    size_bytes: int = Field(ge=0, description="File size in bytes.")
    fetched_at: datetime = Field(description="When the data was fetched (UTC).")
    ingested_at: datetime = Field(description="When the snapshot was recorded in our system (UTC).")
    is_sample: bool = Field(default=False, description="True if this is sample/fixture data, not live.")
