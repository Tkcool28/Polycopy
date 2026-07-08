"""Shared market persistence helpers."""

from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

from polycopy.db.database import Database
from polycopy.domain.market import Market
from polycopy.engine.market_resolution_truth import (
    MarketResolutionTruth,
    MarketTruthApplication,
    apply_market_resolution_truth,
)


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


# ── PR24A: resolution-truth persistence ──────────────────────────────────────
#
# The two helpers below consume a ``MarketResolutionTruth`` record
# (from ``polycopy.engine.market_resolution_truth``) and write the
# winner-truth columns added in v14. They are intentionally separate
# from ``persist_market_preserving_identity`` because the truth
# check is an out-of-band event (after a market is already in the DB
# with the latest outcomes) and the operational lock boundary lives
# with the *truth check*, not the initial ingest.
#
# All writers are NO-OPs when the truth is unresolved / ambiguous /
# unverifiable. We never fabricate winners.
#
# These helpers run inside a single transaction supplied by the caller
# (typically the backfill script or a future scheduled resolution
# job) and intentionally DO NOT touch the operational lock themselves
# — the lock is the caller's responsibility, so this code stays
# composable from tests and from any future ingestion path.


def _read_outcomes_for_market(
    db: Database,
    market_id: str,
) -> list[sqlite3.Row]:
    """Return every ``market_outcomes`` row for the given market."""
    return list(
        db.conn.execute(
            "SELECT id, market_id, label, clob_token_id, is_winner "
            "FROM market_outcomes WHERE market_id = ?",
            (market_id,),
        ).fetchall()
    )


def apply_resolution_truth_to_market(
    db: Database,
    truth: MarketResolutionTruth,
    *,
    outcomes: Optional[Iterable[sqlite3.Row]] = None,
) -> MarketTruthApplication:
    """Persist the truth record and update ``is_winner`` flags on outcomes.

    Behavior:

    * Resolved truth + matching outcome(s) → ``markets.resolved=1``,
      ``markets.winning_token_id=<token>``, ``markets.resolution_checked_at``,
      ``markets.resolution_source``; exactly one
      ``market_outcomes.is_winner=1``; every other outcome with a
      non-NULL ``clob_token_id`` gets ``is_winner=0``. Outcomes with
      NULL ``clob_token_id`` are left unchanged.

    * Unresolved truth → ``markets.resolved`` is forced to ``0``,
      ``markets.winning_token_id`` cleared, but
      ``markets.resolution_checked_at`` and
      ``markets.resolution_source`` are still updated so audit can
      see when we last checked. Outcome ``is_winner`` flags are
      **preserved** (we never silently zero them out).

    * Ambiguous truth (multiple matching outcomes) → only
      ``markets.resolution_checked_at`` and
      ``markets.resolution_source`` are updated; winner flags are
      preserved; ``markets.resolved`` is left unchanged.

    Returns the :class:`MarketTruthApplication` describing what was
    written so callers can log / report it.
    """
    rows = list(outcomes) if outcomes is not None else _read_outcomes_for_market(db, truth.market_id)
    application = apply_market_resolution_truth(truth, outcomes=rows)

    started_transaction = not db.conn.in_transaction
    try:
        if started_transaction:
            db.execute("BEGIN IMMEDIATE")

        # Update markets row. resolution_checked_at and
        # resolution_source are always set when we record a check
        # (even for unresolved / ambiguous).
        db.execute(
            """UPDATE markets
                  SET resolution_checked_at = ?,
                      resolution_source = ?
                WHERE id = ?""",
            (
                truth.checked_at,
                truth.source,
                truth.market_id,
            ),
        )

        if application.ambiguous:
            # Truth is ambiguous — record the check but do NOT mark
            # any winner. Leave markets.resolved / winning_token_id
            # as they were so a future unambiguous check can correct
            # them.
            pass
        elif not application.resolved:
            # Truth explicitly says unresolved: clear
            # winning_token_id and force resolved=0. Resolution
            # outcome label is preserved if the upstream supplied
            # one (some sources do); cleared only if both resolved
            # and winning_token_id are absent.
            db.execute(
                """UPDATE markets
                      SET resolved = 0,
                          winning_token_id = NULL,
                          resolution_checked_at = ?,
                          resolution_source = ?
                    WHERE id = ?""",
                (
                    truth.checked_at,
                    truth.source,
                    truth.market_id,
                ),
            )
        else:
            # Resolved with a winner. The application.ambiguous
            # branch above already handled the case where two
            # outcomes share the winning token.
            db.execute(
                """UPDATE markets
                      SET resolved = 1,
                          winning_token_id = ?,
                          resolution_outcome = COALESCE(?, resolution_outcome),
                          resolution_checked_at = ?,
                          resolution_source = ?
                    WHERE id = ?""",
                (
                    truth.winning_token_id,
                    truth.resolution_outcome,
                    truth.checked_at,
                    truth.source,
                    truth.market_id,
                ),
            )

            # Update is_winner flags in-place. We only touch rows
            # present in the mapping (outcome with NULL clob_token_id
            # is intentionally omitted).
            for outcome_id, flag in application.is_winner_by_outcome_id.items():
                db.execute(
                    "UPDATE market_outcomes SET is_winner = ? WHERE id = ?",
                    (flag, int(outcome_id)),
                )

        if started_transaction:
            db.conn.commit()
    except Exception:
        if started_transaction:
            db.conn.rollback()
        raise

    return application


def clear_winner_truth(db: Database, market_id: str) -> None:
    """Clear winner truth columns for one market. Used by tests; never by runtime paths.

    The helper intentionally preserves ``markets.resolution_checked_at``
    and ``markets.resolution_source`` (they are audit fields, not
    winner fields) and zeroes ``market_outcomes.is_winner``.
    """
    started_transaction = not db.conn.in_transaction
    try:
        if started_transaction:
            db.execute("BEGIN IMMEDIATE")
        db.execute(
            """UPDATE markets
                  SET resolved = 0,
                      winning_token_id = NULL
                WHERE id = ?""",
            (market_id,),
        )
        db.execute(
            "UPDATE market_outcomes SET is_winner = NULL WHERE market_id = ?",
            (market_id,),
        )
        if started_transaction:
            db.conn.commit()
    except Exception:
        if started_transaction:
            db.conn.rollback()
        raise