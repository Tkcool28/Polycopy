"""Snapshot provenance — archive raw API responses with integrity hashes.

When data is fetched from any provider, this module saves the raw response
to disk and records a RawSnapshot with a content hash for integrity
verification. This creates an immutable audit trail.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from polycopy.domain.raw_snapshot import RawSnapshot

logger = logging.getLogger(__name__)


class SnapshotProvenance:
    """Manages raw snapshot archival and provenance tracking.

    Snapshots are saved to disk as files. Provenance metadata (including
    a content hash) is returned as a RawSnapshot domain object for
    database recording.
    """

    def __init__(self, snapshot_dir: Path, hash_algo: str = "sha256") -> None:
        self.snapshot_dir = snapshot_dir
        self.hash_algo = hash_algo
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        source: str,
        endpoint: str,
        data: Any,
        query_params: dict[str, Any] | None = None,
        content_type: str = "application/json",
        fetched_at: datetime | None = None,
        is_sample: bool = False,
    ) -> RawSnapshot:
        """Save raw data to disk and return a RawSnapshot with provenance metadata.

        Args:
            source: Data source name (e.g. 'polymarket_gamma').
            endpoint: API endpoint that was called.
            data: The raw data to archive (will be JSON-serialized).
            query_params: Query parameters used in the request.
            content_type: MIME type of the data.
            fetched_at: When the data was fetched (defaults to now).
            is_sample: Whether this is sample/fixture data.

        Returns:
            RawSnapshot with file path, hash, and provenance metadata.
        """
        now = datetime.now(timezone.utc)
        if fetched_at is None:
            fetched_at = now

        # Serialize data
        content = json.dumps(data, indent=2, sort_keys=True, default=str).encode("utf-8")

        # Compute hash
        hasher = hashlib.new(self.hash_algo, content)
        content_hash = hasher.hexdigest()

        # Build file path: {snapshot_dir}/{source}/{date}/{hash}.{ext}
        date_str = fetched_at.strftime("%Y-%m-%d")
        ext = "json" if content_type == "application/json" else "bin"
        relative_path = f"{source}/{date_str}/{content_hash}.{ext}"
        file_path = self.snapshot_dir / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to disk (don't overwrite if hash matches — idempotent)
        if not file_path.exists():
            file_path.write_bytes(content)
            logger.debug("Snapshot saved: %s (%d bytes, %s=%s)", relative_path, len(content), self.hash_algo, content_hash)
        else:
            logger.debug("Snapshot already exists: %s", relative_path)

        return RawSnapshot(
            id=uuid4(),
            source=source,
            endpoint=endpoint,
            query_params=query_params or {},
            file_path=relative_path,
            content_hash=content_hash,
            hash_algo=self.hash_algo,
            content_type=content_type,
            size_bytes=len(content),
            fetched_at=fetched_at,
            ingested_at=now,
            is_sample=is_sample,
        )

    def verify(self, snapshot: RawSnapshot) -> bool:
        """Verify a snapshot's integrity by recomputing its hash.

        Returns True if the file exists and the hash matches.
        """
        file_path = self.snapshot_dir / snapshot.file_path
        if not file_path.exists():
            logger.warning("Snapshot file missing: %s", snapshot.file_path)
            return False

        content = file_path.read_bytes()
        hasher = hashlib.new(snapshot.hash_algo, content)
        computed = hasher.hexdigest()

        if computed != snapshot.content_hash:
            logger.error(
                "Snapshot hash mismatch for %s: expected=%s computed=%s",
                snapshot.file_path, snapshot.content_hash, computed,
            )
            return False

        return True
