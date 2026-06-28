"""SQLite-backed idempotency store scoped by action.

Provides persistent duplicate-submission protection for state-changing API
endpoints (approve/reject/settle). Uses SQLite so keys survive restarts.

Schema per key:
  - scope: action label (e.g. "paper_approve", "paper_reject", "settle")
  - request_hash: SHA-256 of normalized request payload
  - result_json: serialized result of the action
  - status: "completed" | "failed"
  - created_at: UTC ISO timestamp
  - last_accessed_at: UTC ISO timestamp (for cleanup)

Invariants:
  - Same scope + same request_hash → replay stored result (idempotent)
  - Same scope + different request_hash → new entry recorded
  - Cleanup removes entries older than retention window (default 24h)
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from polycopy.db.database import Database, get_database

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_SECONDS = 86400  # 24 hours


class IdempotencyStore:
    """Persistent SQLite-backed idempotency store.

    Stores request hashes keyed by (scope, request_hash).
    Replays stored results for duplicate submissions.
    Different payloads (different hash) are recorded as separate entries.
    """

    def __init__(self, db: Database | None = None, retention_seconds: int = DEFAULT_RETENTION_SECONDS) -> None:
        self._db = db
        self._retention_seconds = retention_seconds
        self._ensured_table = False

    @property
    def db(self) -> Database:
        if self._db is None or getattr(self._db, "_conn", None) is None:
            self._db = get_database(reload=True)
            self._ensured_table = False
        return self._db

    def _ensure_table(self) -> None:
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                scope          TEXT NOT NULL,
                request_hash   TEXT NOT NULL,
                result_json    TEXT NOT NULL DEFAULT '{}',
                status         TEXT NOT NULL DEFAULT 'completed',
                created_at     TEXT NOT NULL,
                last_accessed_at TEXT NOT NULL,
                PRIMARY KEY (scope, request_hash)
            )
            """
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_idempotency_created ON idempotency_keys(created_at)"
        )
        self.db.conn.commit()
        self._ensured_table = True

    @staticmethod
    def compute_request_hash(scope: str, *positional: object, **payload: object) -> str:
        """Compute a deterministic SHA-256 hash for a request payload.

        Args:
            scope: action scope (e.g. "paper_approve")
            *positional: positional values to include in the hash
            **payload: request fields to hash (must be JSON-serializable)

        Returns:
            32-char hex hash.
        """
        raw = json.dumps({"scope": scope, "_pos": list(positional), **payload}, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def check_and_store(
        self,
        scope: str,
        request_hash: str,
        result: object,
        status: str = "completed",
    ) -> tuple[bool, Optional[dict[str, object]]]:
        """Check a request against the store and record it if new.

        Args:
            scope: action label (e.g. "paper_approve")
            request_hash: hash of the request payload
            result: result to store (will be JSON-serialized)
            status: "completed" or "failed"

        Returns:
            (is_duplicate, stored_or_previous_result).
            If duplicate, returns the previously stored result.
            If new, stores and returns None.
        """
        self._ensure_table()
        now = datetime.now(timezone.utc).isoformat()

        row = self.db.fetchone(
            "SELECT result_json, status, created_at FROM idempotency_keys WHERE scope = ? AND request_hash = ?",
            (scope, request_hash),
        )

        if row is not None:
            # Duplicate request — replay stored result
            self.db.execute(
                "UPDATE idempotency_keys SET last_accessed_at = ? WHERE scope = ? AND request_hash = ?",
                (now, scope, request_hash),
            )
            self.db.conn.commit()
            previous = json.loads(row["result_json"])
            logger.info(
                "Idempotency replay: scope=%s hash=%s status=%s created=%s",
                scope,
                request_hash[:12],
                row["status"],
                row["created_at"],
            )
            return True, previous

        # New request — store it
        result_json = json.dumps(result, default=str)
        self.db.execute(
            """
            INSERT INTO idempotency_keys (scope, request_hash, result_json, status, created_at, last_accessed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (scope, request_hash, result_json, status, now, now),
        )
        self.db.conn.commit()
        logger.info(
            "Idempotency recorded: scope=%s hash=%s status=%s",
            scope,
            request_hash[:12],
            status,
        )
        return False, None

    def lookup(self, scope: str, request_hash: str) -> Optional[dict[str, object]]:
        """Look up a stored result without marking as duplicate.

        Returns the stored result dict or None if not found.
        """
        self._ensure_table()
        row = self.db.fetchone(
            "SELECT result_json, status, created_at FROM idempotency_keys WHERE scope = ? AND request_hash = ?",
            (scope, request_hash),
        )
        if row is None:
            return None
        data = json.loads(row["result_json"])
        data["_status"] = row["status"]
        data["_created_at"] = row["created_at"]
        return data

    def clear(self) -> int:
        """Remove all entries from the store. Used in tests."""
        try:
            self._ensure_table()
            cursor = self.db.execute("DELETE FROM idempotency_keys")
            deleted = cursor.rowcount or 0
            self.db.conn.commit()
        except (RuntimeError, Exception):
            # Database not connected or table missing (test fixture teardown order)
            deleted = 0
        logger.info("Idempotency clear: removed %d entries", deleted)
        return deleted

    def cleanup(self, retention_seconds: Optional[int] = None) -> int:
        """Remove entries older than retention window.

        Args:
            retention_seconds: override default retention (seconds).

        Returns:
            Number of rows deleted.
        """
        self._ensure_table()
        retention = retention_seconds or self._retention_seconds
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - retention

        cursor = self.db.execute(
            """
            DELETE FROM idempotency_keys
            WHERE strftime('%s', created_at) < ?
            """,
            (str(cutoff),),
        )
        deleted = cursor.rowcount or 0
        self.db.conn.commit()
        if deleted > 0:
            logger.info("Idempotency cleanup: removed %d entries older than %ds", deleted, retention)
        return deleted

    @property
    def entry_count(self) -> int:
        """Return total number of stored idempotency entries."""
        self._ensure_table()
        row = self.db.fetchone("SELECT COUNT(*) AS n FROM idempotency_keys")
        return int(row["n"] if row else 0)
