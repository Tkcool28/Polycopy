"""Expand canonical metadata builder with market/outcome/lifecycle/provenance snapshot.

This module is loaded as a package so ``from polycopy.ingestion.snapshot import ...``
works naturally. All helper functions are re-exported from the canonical_metadata
module — building and merge logic never change except for namespace addition.
"""
from __future__ import annotations

from polycopy.ingestion.canonical_metadata import (  # noqa: F401, E402
    MERGE_CONFLICT,
    MERGE_FILLED,
    MERGE_UNCHANGED,
    MERGE_UNAVAILABLE,
    build_canonical_metadata,
    merge_canonical_metadata,
    normalize_source_trade_metadata,
)
