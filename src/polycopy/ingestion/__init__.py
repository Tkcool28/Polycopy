"""PR24Z — Manual real source-trade ingestion package.

Contains the normalized in-memory source-trade candidate model, BUY-only
validation, stable-identity generation, the single centralized
``SourceTradeWriter`` (DB-only, no network), and the thin fetch→normalize→
validate→dedupe→write orchestration used by ``scripts/ingest_real_source_trades.py``.

Hard guardrails (see task card):
  * Dry-run default; production write requires --allow-live --write
    --confirm-production-db.
  * BUY-only. SELL / missing-side rejected as unsupported.
  * One and only one component (``SourceTradeWriter``) may INSERT into
    source_trades for this new path.
  * No scoring, no candidates, no signals, no snapshots, no orders, no
    positions, no settlement mutation, no timers, no automation.
"""
