"""Snapshot contract version for the expanded market evidence metadata."""
from __future__ import annotations

SNAPSHOT_CONTRACT_VERSION = "2"

# This version bumps when the canonical build_canonical_metadata output adds
# new top-level namespaces (market, outcomes, lifecycle, provenance) or
# changes field names within existing ones. Downstream consumers that read
# metadata_version should still find taxonomy/event/series unchanged.
