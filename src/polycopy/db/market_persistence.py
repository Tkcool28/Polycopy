"""Shared market persistence helpers."""

from __future__ import annotations

from polycopy.db.database import Database
from polycopy.domain.market import Market


def persist_market_preserving_identity(db: Database, market: Market) -> str:
    """Upsert a market while preserving its internal ID by source identity.

    ``Market.id`` is generated before persistence, but the durable identity for
    an external market is ``(source, source_id)``.  Re-ingesting the same market
    must therefore update the existing parent row in place instead of replacing
    it with a fresh UUID; otherwise child rows such as ``market_outcomes`` can be
    orphaned when foreign-key enforcement is unavailable or bypassed.

    The parent upsert and outcome refresh are committed atomically.  Outcomes
    are always deleted/reinserted for the preserved parent ID so stale outcomes
    are removed without changing the market's primary key.
    """

    end_date = market.end_date.isoformat() if market.end_date is not None else None
    started_transaction = not db.conn.in_transaction

    try:
        if started_transaction:
            db.execute("BEGIN IMMEDIATE")

        existing = db.fetchone(
            "SELECT id FROM markets WHERE source = ? AND source_id = ?",
            (market.source, market.source_id),
        )
        persisted_id = str(existing["id"] if existing is not None else market.id)

        db.execute(
            """INSERT INTO markets
               (id, source_id, source, question, active, closed, resolved,
                resolution_outcome, volume_24h, end_date, fetched_at, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, source_id) DO UPDATE SET
                   question = excluded.question,
                   active = excluded.active,
                   closed = excluded.closed,
                   resolved = excluded.resolved,
                   resolution_outcome = excluded.resolution_outcome,
                   volume_24h = excluded.volume_24h,
                   end_date = excluded.end_date,
                   fetched_at = excluded.fetched_at,
                   is_sample = excluded.is_sample""",
            (
                persisted_id,
                market.source_id,
                market.source,
                market.question,
                int(market.active),
                int(market.closed),
                int(market.resolved),
                market.resolution_outcome,
                market.volume_24h,
                end_date,
                market.fetched_at.isoformat(),
                int(market.is_sample),
            ),
        )
        db.execute("DELETE FROM market_outcomes WHERE market_id = ?", (persisted_id,))
        for outcome in market.outcomes:
            db.execute(
                """INSERT INTO market_outcomes
                   (market_id, label, price, volume, clob_token_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    persisted_id,
                    outcome.label,
                    outcome.price,
                    outcome.volume,
                    outcome.clob_token_id,
                ),
            )
        if started_transaction:
            db.conn.commit()
    except Exception:
        if started_transaction:
            db.conn.rollback()
        raise

    return persisted_id
