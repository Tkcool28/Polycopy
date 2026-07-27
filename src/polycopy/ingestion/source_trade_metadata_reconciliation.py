"""One bounded metadata-only reconciliation boundary for ``source_trades``.

Initial ingestion is owned solely by :mod:`source_trade_writer`.  This module
is deliberately narrower: it can update an *existing* row's ``metadata_json``
and nothing else.  It has no INSERT/UPSERT SQL and uses an exact immutable row
id or the canonical ``(source, source_trade_id)`` identity.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from polycopy.ingestion.canonical_metadata import _CanonicalMergeMetadata
from polycopy.ingestion.source_trade_metadata import serialize_source_trade_metadata


def serialize_canonical_merge_metadata(metadata: _CanonicalMergeMetadata) -> str:
    """Serialize authority issued by the completed canonical merge only.

    An ordinary mapping, a canonical builder output, or a caller-created value
    cannot enter this path.  The opaque carrier can be issued only at the end
    of :func:`merge_canonical_metadata` after authoritative reconciliation.
    """
    if type(metadata) is not _CanonicalMergeMetadata:
        raise TypeError("metadata replacement requires canonical merge output")
    return json.dumps(metadata, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class MetadataReconcileResult:
    status: str  # updated | reused | conflict | missing
    changed: bool = False


def reconcile_metadata_json(
    db: Any,
    metadata: Mapping[str, Any] | str | None,
    *,
    source: str | None = None,
    source_trade_id: str | None = None,
    internal_id: str | None = None,
    allow_nonempty_replace: bool = False,
    commit: bool = True,
) -> MetadataReconcileResult:
    """Reconcile metadata for one existing row, transactionally.

    Normal mappings are always serialized through the PR #79 exact-type trust
    boundary.  Only the private carrier above may replace non-empty metadata,
    and only for a caller that already completed the shared authoritative
    merge.  Thus arbitrary mappings cannot self-certify a snapshot, while a
    legitimate previously-persisted snapshot is not stripped on enrichment.
    """
    by_internal = internal_id is not None
    by_identity = source is not None and source_trade_id is not None
    if by_internal == by_identity:
        raise ValueError("provide exactly one immutable row selector")
    if allow_nonempty_replace and type(metadata) is not _CanonicalMergeMetadata:
        raise ValueError("non-empty metadata replacement requires canonical merge output")

    if type(metadata) is _CanonicalMergeMetadata:
        serialized = serialize_canonical_merge_metadata(metadata)
    elif isinstance(metadata, Mapping) or metadata is None:
        serialized = serialize_source_trade_metadata(metadata)
    else:
        # A JSON string from an untrusted caller is not a serialization bypass.
        try:
            parsed = json.loads(metadata)
        except (TypeError, ValueError):
            parsed = None
        serialized = serialize_source_trade_metadata(parsed if isinstance(parsed, Mapping) else None)

    try:
        if by_internal:
            row = db.conn.execute(
                "SELECT metadata_json FROM source_trades WHERE id=?", (internal_id,)
            ).fetchone()
        else:
            row = db.conn.execute(
                "SELECT metadata_json FROM source_trades WHERE source=? AND source_trade_id=?",
                (source, source_trade_id),
            ).fetchone()
        if row is None:
            return MetadataReconcileResult("missing")
        current = row[0] if isinstance(row[0], str) else None
        # Legacy callers may hand back the exact bytes just read from an
        # existing row.  Recognize that as a true zero-write before applying
        # the untrusted-input serializer; it cannot introduce or upgrade any
        # evidence and preserves historical canonical bytes on replay.
        if isinstance(metadata, str) and current and current.strip() == metadata.strip():
            return MetadataReconcileResult("reused")
        if current == serialized:
            return MetadataReconcileResult("reused")
        if current and current.strip() and not allow_nonempty_replace:
            return MetadataReconcileResult("conflict")
        if by_internal:
            cur = db.conn.execute(
                "UPDATE source_trades SET metadata_json=? WHERE id=?",
                (serialized, internal_id),
            )
        else:
            cur = db.conn.execute(
                "UPDATE source_trades SET metadata_json=? WHERE source=? AND source_trade_id=?",
                (serialized, source, source_trade_id),
            )
        if cur.rowcount != 1:
            raise sqlite3.IntegrityError("metadata target disappeared")
        if commit:
            db.conn.commit()
        return MetadataReconcileResult("updated", changed=True)
    except sqlite3.Error:
        if commit:
            db.conn.rollback()
        return MetadataReconcileResult("conflict")


__all__ = ["MetadataReconcileResult", "reconcile_metadata_json", "serialize_canonical_merge_metadata"]
