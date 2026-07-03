"""Version 11 schema migration — additive typed-input columns for V2 shadow.

Adds the missing typed-input columns to ``shadow_decisions`` so the
typed :class:`ShadowScoreInputV2` round-trips through persistence
without lossy coercion.

All statements are additive ``ALTER TABLE ... ADD COLUMN`` and are
idempotent via the migration runner's ``PRAGMA table_info`` guard.
"""

from __future__ import annotations


_V11_DDL = [
    # ── Chunk 5 — V2 shadow typed-input columns (additive, idempotent) ──────────
    "ALTER TABLE shadow_decisions ADD COLUMN source_price REAL;",
    "ALTER TABLE shadow_decisions ADD COLUMN delayed_copy_price REAL;",
    "ALTER TABLE shadow_decisions ADD COLUMN slippage REAL;",
    "ALTER TABLE shadow_decisions ADD COLUMN spread REAL;",
    "ALTER TABLE shadow_decisions ADD COLUMN intended_stake REAL;",
    "ALTER TABLE shadow_decisions ADD COLUMN executable_depth REAL;",
    "ALTER TABLE shadow_decisions ADD COLUMN wallet_skill_persistence_input REAL;",
    "ALTER TABLE shadow_decisions ADD COLUMN copied_realized_performance_input REAL;",
    "ALTER TABLE shadow_decisions ADD COLUMN concentration_correlation_input REAL;",
    "ALTER TABLE shadow_decisions ADD COLUMN measured_delay_seconds REAL;",
    "ALTER TABLE shadow_decisions ADD COLUMN missing_forward_reasons_json TEXT;",
    "ALTER TABLE shadow_decisions ADD COLUMN price_snapshot_id TEXT REFERENCES candidate_price_snapshots(id);",
    "ALTER TABLE shadow_decisions ADD COLUMN depth_hash TEXT;",
]