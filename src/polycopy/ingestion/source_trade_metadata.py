"""Canonical, versioned metadata evidence for ``source_trades``.

This module deliberately preserves only the bounded PR66 evidence contract.
Unknown upstream fields are excluded rather than becoming accidental schema.
Event slugs remain event identity; this module never assigns specialist or
category-scoring labels.

The shared canonical builder now lives in
:mod:`polycopy.ingestion.canonical_metadata`; ``build_metadata_from_gamma_market``
is a thin delegation so the approved-wallet collector emits the exact same
canonical shape as the research evidence plane.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from polycopy.ingestion.canonical_metadata import (
    build_canonical_metadata,
    normalize_source_trade_metadata as _normalize_source_trade_metadata,
)
from polycopy.taxonomy.official_polymarket import (
    TAXONOMY_USABLE,
    OfficialTaxonomyResult,
)

METADATA_VERSION = "1"
_CANONICAL_SEPARATORS = (",", ":")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _scalar(value: Any) -> str | None:
    """Return a normalized scalar string, or ``None`` for unusable values."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _first_scalar(*values: Any) -> str | None:
    for value in values:
        normalized = _scalar(value)
        if normalized is not None:
            return normalized
    return None


def _tags(value: Any) -> list[str]:
    """Normalize raw tags to a deterministic, de-duplicated string list."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    normalized = {_scalar(item) for item in value}
    return sorted(tag for tag in normalized if tag is not None)


def normalize_source_trade_metadata(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the exact PR66 metadata contract from an upstream-like mapping.

    Delegates to the shared canonical builder (``canonical_metadata``). Accepts
    either raw upstream fields (``eventId``, ``category``, ``series``) or an
    already canonical-shaped mapping. Never copies unknown fields.
    """
    return _normalize_source_trade_metadata(raw)


def _official_category_for_v1_metadata(result: OfficialTaxonomyResult) -> str | None:
    """Return only a conflict-free trusted broad category for metadata v1.

    Metadata v1 deliberately has no provenance field.  Preserve its exact shape
    and pass only resolver-approved evidence to ``raw_category``; unknown,
    specific-tag, and conflicting evidence stays unavailable fail-closed.
    """
    if result.status != TAXONOMY_USABLE:
        return None
    if result.source == "market.category":
        return result.market_category_value
    if result.source == "event.category":
        return result.event_category_value
    if result.source == "series.category":
        return result.series_category_value
    return result.category_label


def build_metadata_from_gamma_market(
    trade: Mapping[str, Any] | None,
    gamma_market: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the canonical PR66 metadata contract from a trusted Gamma market.

    Thin delegation to :func:`polycopy.ingestion.canonical_metadata.build_canonical_metadata`
    — the single canonical producer shared by the research evidence plane. The
    approved-wallet collector therefore emits a byte-identical nested shape to
    collection, backfill, and per-trade enrichment.
    """
    return build_canonical_metadata(trade, gamma_market)


def serialize_source_trade_metadata(raw: Mapping[str, Any] | None) -> str:
    """Serialize canonical source-trade evidence deterministically."""
    return json.dumps(
        normalize_source_trade_metadata(raw),
        sort_keys=True,
        separators=_CANONICAL_SEPARATORS,
    )


def serialize_gamma_market_metadata(
    trade: Mapping[str, Any] | None,
    gamma_market: Mapping[str, Any] | None,
) -> str:
    """Serialize the canonical Gamma-derived metadata deterministically."""
    return json.dumps(
        build_metadata_from_gamma_market(trade, gamma_market),
        sort_keys=True,
        separators=_CANONICAL_SEPARATORS,
    )


__all__ = [
    "METADATA_VERSION",
    "build_metadata_from_gamma_market",
    "normalize_source_trade_metadata",
    "serialize_gamma_market_metadata",
    "serialize_source_trade_metadata",
]
