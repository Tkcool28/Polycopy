#!/usr/bin/env python3
"""Backfill resolution-truth columns from existing market rows (PR24A).

This is a one-shot, idempotent, **default-dry-run** script that
materializes the resolution-truth columns introduced in v14 from the
``markets`` / ``market_outcomes`` rows already in the DB. It does NOT
call the upstream Polymarket API; it only inspects rows whose
``markets.resolved = 1`` and writes the corresponding ``is_winner``
flag + ``winning_token_id`` for each.

Why a backfill at all?
======================

When v14 lands, every existing market row has NULL winner columns.
The runtime path that *captures* new resolution truth is intentionally
out of scope for PR24A (we did not enable timers, did not enable
PR20, did not enable specialist aggregation). But pre-existing data
that already claims resolution can be backfilled from its own row +
its own outcomes without any upstream call.

This script makes the backfill explicit, dry-run-first, and idempotent.

Hard guardrails (do not weaken without explicit PR24A follow-up)
================================================================

* Default mode is ``--dry-run`` — the script PRINTS the planned writes
  and exits without modifying anything.
* ``--apply`` is required to actually mutate the DB. Even then, the
  script does NOT touch operational lock contention (it is read-only
  with respect to market ingest) and does NOT call any external API.
* The operational lock is acquired on ``--apply`` so this script
  cannot overlap with a scheduled collection / scan / settle / update
  job. Lock timeout defaults to 30s; pass ``--lock-timeout 0`` for
  fail-fast.
* No data is ever deleted. Only ``UPDATE`` on ``markets`` /
  ``market_outcomes`` / ``source_trades`` is performed, and only on
  rows explicitly enumerated by the script.
* ``--limit`` and ``--market-id`` are applied at the SQL level so
  the script can be scoped to a single market for safe manual runs.
* ``--json`` emits a structured report on stdout that downstream
  tooling can parse.

Operational usage
==================

Dry-run a single market::

    PYTHONPATH=src python scripts/backfill_resolution_truth.py \\
        --market-id <uuid> --dry-run --json

Apply globally with a hard limit::

    PYTHONPATH=src python scripts/backfill_resolution_truth.py \\
        --apply --limit 1000 --lock-timeout 0

Re-run any time — the script is idempotent.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# Allow running as a script without PYTHONPATH=src
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from polycopy.config.settings import get_settings
from polycopy.db.database import Database
from polycopy.db.market_persistence import apply_resolution_truth_to_market
from polycopy.engine.market_resolution_truth import (
    MarketResolutionTruth,
    MarketTruthApplication,
    apply_market_resolution_truth,
)
from polycopy.engine.trade_settlement import (
    SETTLEMENT_STATUSES,
    settle_source_trade_against_truth,
)
from polycopy.runtime.locks import operational_job_lock
from polycopy.utils.concurrency import LockError


logger = logging.getLogger("backfill_resolution_truth")


# ── Report structures ────────────────────────────────────────────────────────


@dataclass
class MarketPlan:
    """Planned (or applied) writes for one market."""

    market_id: str
    resolved: bool
    winning_token_id: Optional[str]
    is_winner_flags: dict[int, int] = field(default_factory=dict)
    ambiguous: bool = False
    no_match: bool = False  # True when truth wins on paper but no outcome matched


@dataclass
class TradeSettlementPlan:
    """Planned (or applied) settlement for one source_trade row."""

    trade_id: str
    market_id: str
    resolution_status: str
    is_winning_trade: Optional[int]
    winning_token_id: Optional[str]
    realized_pnl: Optional[float]


@dataclass
class BackfillReport:
    """Top-level report emitted in JSON mode."""

    dry_run: bool
    markets_seen: int
    markets_planned: int
    markets_ambiguous: int
    markets_no_match: int
    trades_seen: int
    trades_settled: int
    trades_skipped_unresolved: int
    trades_skipped_missing_token: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    plan: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""


# ── Pure planning helpers ────────────────────────────────────────────────────


def _truth_from_market_row(row: sqlite3.Row) -> Optional[MarketResolutionTruth]:
    """Build a :class:`MarketResolutionTruth` from an existing ``markets`` row.

    Only rows with ``resolved = 1`` AND a non-NULL
    ``resolution_outcome`` label can produce a non-trivial truth. The
    label is matched against ``market_outcomes.label`` exactly
    (case-sensitive, trimmed) to pick the winning token id. If the
    label has no matching outcome, we return ``resolved=True,
    winning_token_id=None`` so the persistence layer records the
    check but does NOT mark a winner.
    """
    market_id = str(row["id"])
    if not int(row["resolved"]):
        return None  # unresolved market — not in scope for this backfill

    label = (row["resolution_outcome"] or "").strip()
    winning_token_id: Optional[str] = None
    if label:
        # Resolve the winning token by matching the label against
        # ``market_outcomes.label`` exactly. The DB enforces a UNIQUE
        # constraint on (market_id, label) implicitly via no explicit
        # index, but a market can have at most two outcomes per
        # binary contract; we still defend against the duplicate-label
        # case by raising only if more than one outcome with the same
        # label shares a non-NULL token id.
        pass  # filled in by the caller (we don't have access to outcomes here)
    return MarketResolutionTruth(
        market_id=market_id,
        resolved=True,
        winning_token_id=winning_token_id,
        resolution_outcome=label or None,
        source="backfill_resolution_truth",
        checked_at=_now_iso(),
    )


def _now_iso() -> str:
    """UTC ISO-8601 timestamp with ``+00:00`` offset."""
    return datetime.now(timezone.utc).isoformat()


# ── SQL planning ─────────────────────────────────────────────────────────────


_SELECT_RESOLVED_MARKETS = """
SELECT m.id           AS id,
       m.source_id    AS source_id,
       m.source       AS source,
       m.resolved     AS resolved,
       m.resolution_outcome AS resolution_outcome,
       m.winning_token_id   AS winning_token_id
FROM markets m
WHERE m.resolved = 1
"""


def _fetch_resolved_markets(
    db: Database,
    *,
    market_id: Optional[str],
    limit: Optional[int],
) -> list[sqlite3.Row]:
    sql_parts = [_SELECT_RESOLVED_MARKETS]
    params: list[Any] = []
    if market_id is not None:
        sql_parts.append("AND m.id = ?")
        params.append(market_id)
    sql_parts.append("ORDER BY m.id")
    if limit is not None and limit > 0:
        sql_parts.append("LIMIT ?")
        params.append(int(limit))
    return list(db.conn.execute("\n".join(sql_parts), tuple(params)).fetchall())


def _fetch_outcomes_for_market(
    db: Database,
    market_id: str,
) -> list[sqlite3.Row]:
    return list(
        db.conn.execute(
            "SELECT id, label, clob_token_id FROM market_outcomes "
            "WHERE market_id = ?",
            (market_id,),
        ).fetchall()
    )


def _fetch_unsettled_trades_for_market(
    db: Database,
    market_id: str,
) -> list[sqlite3.Row]:
    """Return every ``source_trades`` row for this market that is still
    ``unresolved`` AND has a non-NULL ``token_id``.

    Settlement requires a known winning token and a non-NULL trade
    token, so pre-filtering here keeps the planning loop small.

    Note: trades with ``token_id IS NULL`` are NOT returned here —
    they cannot be settled against a winning token. They ARE counted
    separately via :func:`_count_unsettled_trades_missing_token` so
    the dry-run report makes their existence visible (PR24A2 PART 4).
    """
    return list(
        db.conn.execute(
            """
            SELECT st.id               AS id,
                   st.source           AS source,
                   st.source_trade_id  AS source_trade_id,
                   st.token_id         AS token_id,
                   st.price            AS price,
                   st.quantity         AS quantity,
                   st.market_source_id AS market_source_id
            FROM source_trades st
            WHERE st.market_source_id = (
                SELECT source_id FROM markets WHERE id = ?
            )
              AND st.resolution_status = 'unresolved'
              AND st.token_id IS NOT NULL
            """,
            (market_id,),
        ).fetchall()
    )


def _count_unsettled_trades_missing_token(
    db: Database,
    market_id: str,
) -> int:
    """Count ``source_trades`` rows for this market that are still
    ``unresolved`` but have ``token_id IS NULL``.

    Such trades cannot be settled against a winning token — there is
    no key to match on. PR24A2 PART 4 surfaced this as a reporting gap:
    the dry-run JSON previously reported ``trades_seen=0`` for a market
    whose only trade was missing a token, hiding the data entirely.
    The count is now exposed as
    ``report.trades_skipped_missing_token`` so downstream consumers
    (and the upcoming PR24I accounting layer) know the trades exist.
    """
    row = db.conn.execute(
        """
        SELECT COUNT(*) AS n
          FROM source_trades st
         WHERE st.market_source_id = (
                   SELECT source_id FROM markets WHERE id = ?
               )
           AND st.resolution_status = 'unresolved'
           AND st.token_id IS NULL
        """,
        (market_id,),
    ).fetchone()
    return int(row["n"])


def _ambiguous_market_settlement(
    trade_row: sqlite3.Row,
    truth: MarketResolutionTruth,
) -> Any:
    """Construct a no-P/L settlement record for a trade whose market
    is flagged ambiguous.

    The trade record keeps ``winning_token_id`` (for audit), but
    ``resolution_status='ambiguous'``, ``is_winning_trade=None``, and
    ``realized_pnl=None``. PR24A2 PART 1 propagates market-level
    ambiguity into per-trade settlement so accounting code never
    silently rolls ambiguous trades into ``won`` / ``lost``.
    """
    from polycopy.engine.trade_settlement import SourceTradeSettlement

    return SourceTradeSettlement(
        resolution_status="ambiguous",
        is_winning_trade=None,
        winning_token_id=truth.winning_token_id,
        realized_pnl=None,
        settlement_source="backfill_resolution_truth",
        resolved_at=_now_iso(),
    )


# ── Plan / apply loop ────────────────────────────────────────────────────────


def plan_truth_for_market(
    db: Database,
    market_row: sqlite3.Row,
) -> tuple[MarketResolutionTruth, MarketTruthApplication, list[sqlite3.Row], bool]:
    """Plan the truth + outcome writes for one market.

    Returns ``(truth, application, outcomes, needs_outcome_lookup)``.

    ``needs_outcome_lookup`` is True when the caller still needs to
    look up outcomes for the trade-settlement phase.
    """
    market_id = str(market_row["id"])
    outcomes = _fetch_outcomes_for_market(db, market_id)

    label = (market_row["resolution_outcome"] or "").strip() or None
    winning_token_id: Optional[str] = None

    # First preference: an existing winning_token_id (already populated
    # by some earlier ingest).
    existing_winner = market_row["winning_token_id"]
    if existing_winner:
        winning_token_id = str(existing_winner)
    elif label:
        # Resolve label -> token_id by exact match.
        candidates = [
            str(o["clob_token_id"])
            for o in outcomes
            if (o["label"] or "").strip() == label
            and o["clob_token_id"]
        ]
        # De-dupe while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for c in candidates:
            if c in seen:
                continue
            seen.add(c)
            deduped.append(c)
        if len(deduped) == 1:
            winning_token_id = deduped[0]
        elif len(deduped) > 1:
            # Ambiguous: keep winning_token_id=None; the persistence
            # layer will record the check but not mark a winner.
            winning_token_id = None
        else:
            winning_token_id = None

    truth = MarketResolutionTruth(
        market_id=market_id,
        resolved=bool(int(market_row["resolved"])),
        winning_token_id=winning_token_id,
        resolution_outcome=label,
        source="backfill_resolution_truth",
        checked_at=_now_iso(),
    )
    application = apply_market_resolution_truth(
        truth,
        outcomes=outcomes,
    )
    return truth, application, outcomes, True


def _plan_trade_settlements(
    db: Database,
    market_id: str,
    truth: MarketResolutionTruth,
    *,
    application: Optional[MarketTruthApplication] = None,
) -> list[tuple[sqlite3.Row, Any]]:
    """Compute settlement plans for every unresolved trade on this market.

    Behavior
    --------

    * If ``application.ambiguous is True`` (PR24A2 PART 1), every trade
      gets a synthetic ``resolution_status="ambiguous"`` settlement
      with ``is_winning_trade=None`` and ``realized_pnl=None``. The
      truth's ``winning_token_id`` is preserved on the record for
      audit. This stops the pre-PR24A2 behavior of silently settling
      ambiguous-market trades as ``won`` / ``lost`` against the truth
      record's ``winning_token_id``.

    * If the truth is unresolved or has no winning token, every trade
      gets a ``None`` settlement (counted as
      ``trades_skipped_unresolved``).

    * Trades with ``token_id IS NULL`` are NOT settled here (they
      cannot match a winning token); they are counted separately via
      :func:`_count_unsettled_trades_missing_token` so the report
      makes their existence visible.
    """
    trades = _fetch_unsettled_trades_for_market(db, market_id)

    if application is not None and application.ambiguous:
        # Propagate market-level ambiguity into per-trade settlement.
        # Do NOT use ``truth.winning_token_id`` to decide won/lost
        # here — the application already determined the market is
        # ambiguous.
        return [(t, _ambiguous_market_settlement(t, truth)) for t in trades]

    if not truth.resolved or truth.winning_token_id is None:
        return [(t, None) for t in trades]
    plans: list[tuple[sqlite3.Row, Any]] = []
    for t in trades:
        settlement = settle_source_trade_against_truth(
            source_trade=t,
            market_truth=truth,
            settlement_source="backfill_resolution_truth",
            resolved_at=_now_iso(),
        )
        plans.append((t, settlement))
    return plans


def _apply_trade_settlements(
    db: Database,
    plans: Iterable[tuple[sqlite3.Row, Any]],
) -> int:
    """Write each settlement. Returns the number of rows updated."""
    n = 0
    for trade_row, settlement in plans:
        if settlement is None:
            continue
        db.conn.execute(
            """
            UPDATE source_trades
               SET resolution_status = ?,
                   resolved_at = ?,
                   winning_token_id = ?,
                   is_winning_trade = ?,
                   realized_pnl = ?,
                   settlement_source = ?
             WHERE id = ?
            """,
            (
                settlement.resolution_status,
                settlement.resolved_at,
                settlement.winning_token_id,
                settlement.is_winning_trade,
                settlement.realized_pnl,
                settlement.settlement_source,
                str(trade_row["id"]),
            ),
        )
        n += 1
    return n


# ── CLI / entry point ────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backfill_resolution_truth",
        description=(
            "Backfill PR24A resolution-truth columns from existing market rows. "
            "Defaults to --dry-run."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the backfilled truth. Without this flag the "
        "script is a dry-run that prints planned writes only.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=True,  # default is dry-run
        help="(default) Print planned writes; do not modify the DB.",
    )
    p.add_argument(
        "--market-id",
        default=None,
        help="Limit to a single market UUID.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of markets to inspect.",
    )
    p.add_argument(
        "--skip-trades",
        action="store_true",
        help="Skip source-trade settlement planning/apply.",
    )
    p.add_argument(
        "--lock-timeout",
        type=float,
        default=30.0,
        help="Operational-lock timeout in seconds (0 = fail-fast). Default 30.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON report on stdout.",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose logging to stderr.",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)

    # argparse quirk: setting default=True for --dry-run means we
    # need to flip it when --apply is passed.
    dry_run = not args.apply

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    settings = get_settings()
    db = Database(Path(settings.db_path)).connect()

    report = BackfillReport(
        dry_run=dry_run,
        markets_seen=0,
        markets_planned=0,
        markets_ambiguous=0,
        markets_no_match=0,
        trades_seen=0,
        trades_settled=0,
        trades_skipped_unresolved=0,
        trades_skipped_missing_token=0,
        by_status={s: 0 for s in SETTLEMENT_STATUSES},
        started_at=_now_iso(),
    )

    def _run() -> int:
        try:
            markets = _fetch_resolved_markets(
                db, market_id=args.market_id, limit=args.limit
            )
        except Exception:
            logger.exception("failed to enumerate markets")
            return 2

        logger.info(
            "Backfill resolution-truth starting (dry_run=%s, scope=%s, limit=%s, skip_trades=%s)",
            dry_run,
            "single-market" if args.market_id else "global",
            args.limit,
            args.skip_trades,
        )
        report.markets_seen = len(markets)

        for market_row in markets:
            market_id = str(market_row["id"])
            try:
                truth, application, outcomes, _ = plan_truth_for_market(db, market_row)
            except Exception:
                logger.exception("planning failed for market=%s", market_id)
                continue

            plan_entry: dict[str, Any] = {
                "market_id": market_id,
                "resolved": truth.resolved,
                "winning_token_id": truth.winning_token_id,
                "resolution_outcome": truth.resolution_outcome,
                "is_winner_by_outcome_id": dict(application.is_winner_by_outcome_id),
                "ambiguous": application.ambiguous,
            }
            if truth.resolved and not application.ambiguous:
                if application.winner_outcome_id is not None:
                    report.markets_planned += 1
                else:
                    report.markets_no_match += 1
                    plan_entry["no_match"] = True
            elif application.ambiguous:
                report.markets_ambiguous += 1

            # Trade settlement planning.
            trade_plans: list[tuple[sqlite3.Row, Any]] = []
            if not args.skip_trades:
                trade_plans = _plan_trade_settlements(
                    db, market_id, truth, application=application,
                )
                report.trades_seen += len(trade_plans)
                # Count NULL-token trades separately (PR24A2 PART 4).
                # These trades cannot be settled but must be visible.
                report.trades_skipped_missing_token += (
                    _count_unsettled_trades_missing_token(db, market_id)
                )
                for _, settlement in trade_plans:
                    if settlement is None:
                        report.trades_skipped_unresolved += 1
                    else:
                        report.by_status[settlement.resolution_status] = (
                            report.by_status.get(settlement.resolution_status, 0) + 1
                        )
                        if settlement.resolution_status in ("won", "lost"):
                            report.trades_settled += 1

                plan_entry["trade_settlements"] = [
                    {
                        "trade_id": str(t["id"]),
                        "resolution_status": (
                            settlement.resolution_status if settlement else "unresolved"
                        ),
                        "is_winning_trade": (
                            settlement.is_winning_trade if settlement else None
                        ),
                        "winning_token_id": (
                            settlement.winning_token_id if settlement else None
                        ),
                        "realized_pnl": (
                            settlement.realized_pnl if settlement else None
                        ),
                    }
                    for t, settlement in trade_plans
                ]

            report.plan.append(plan_entry)

            if dry_run:
                continue

            # ── APPLY ───────────────────────────────────────────────
            apply_resolution_truth_to_market(db, truth, outcomes=outcomes)
            if not args.skip_trades and trade_plans:
                _apply_trade_settlements(db, trade_plans)

        try:
            db.conn.commit()
        except Exception:
            logger.exception("commit failed")
            db.conn.rollback()
            return 2

        report.finished_at = _now_iso()

        if args.json:
            print(json.dumps(asdict(report), indent=2, default=str))
        else:
            print(f"\nBackfill {'(dry-run)' if dry_run else '(APPLIED)'} summary:")
            print(f"  markets_seen:           {report.markets_seen}")
            print(f"  markets_planned:        {report.markets_planned}")
            print(f"  markets_ambiguous:      {report.markets_ambiguous}")
            print(f"  markets_no_match:       {report.markets_no_match}")
            print(f"  trades_seen:            {report.trades_seen}")
            print(f"  trades_settled (w/l):   {report.trades_settled}")
            print(f"  trades_skipped:         {report.trades_skipped_unresolved}")
            print(f"  trades_skipped_missing_token: {report.trades_skipped_missing_token}")
            print(f"  by_status:              {report.by_status}")

        return 0

    if dry_run:
        # Dry-run does NOT acquire the operational lock — it is
        # read-only and must not block scheduled jobs.
        return _run()

    try:
        with operational_job_lock(
            "backfill_resolution_truth",
            timeout=float(args.lock_timeout),
        ):
            return _run()
    except LockError:
        print(
            "ERROR: operational lock unavailable; another job is running. "
            "Re-run with --dry-run or wait for the other job to finish.",
            file=sys.stderr,
        )
        return 3
    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())