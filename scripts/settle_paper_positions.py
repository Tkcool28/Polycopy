#!/usr/bin/env python3
"""settle_paper_positions.py — Settle resolved paper positions.

Checks Polymarket for resolved markets, then settles all open paper
positions for those markets using the SettlementEngine's idempotent
settlement logic.

Steps:
1. Query DB for resolved markets (or check live Polymarket)
2. For each resolved market, find all open positions
3. Settle positions (idempotent — safe to re-run)
4. Update positions with settlement results
5. Log settlements and missing data
6. Record experiment run

Exit codes:
    0 — success (settlements processed)
    1 — fatal error
    2 — lock held
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polycopy.config.settings import get_settings
from polycopy.db.database import Database
from polycopy.domain.experiment import ExperimentRun, ExperimentStatus
from polycopy.risk.settlement import SettlementEvidence, SettlementEngine
from polycopy.utils.concurrency import LockError
from polycopy.runtime.locks import DEFAULT_OPERATIONAL_LOCK_TIMEOUT_S, operational_job_lock
from polycopy.runtime.memory import (
    MemoryLimitExceeded,
    check_rss_limit,
    get_max_rss_mb_from_env,
)

logger = logging.getLogger(__name__)


# PR24B: bounded streaming for the per-market positions read (previously
# unbounded ``fetchall``), and a per-position RSS poll.
_SETTLE_BATCH_SIZE = 500
_RSS_POLL_EVERY_ROWS = 500
_SETTLE_MAX_RSS_MB = get_max_rss_mb_from_env()


def setup_logging(verbosity: int = 0) -> None:
    level = logging.WARNING
    if verbosity >= 1:
        level = logging.INFO
    if verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )


class SettlementResult:
    """Tracks settlement run outcomes."""

    def __init__(self) -> None:
        self.markets_checked: int = 0
        self.markets_resolved: int = 0
        self.positions_settled: int = 0
        self.total_payout: float = 0.0
        self.total_positions_value: float = 0.0
        self.missing_data_log: list[str] = []
        self.errors: list[str] = []
        self.started_at = datetime.now(timezone.utc)
        self.ended_at: datetime | None = None

    def summary(self) -> str:
        return (
            f"settlement run complete\n"
            f"  markets checked: {self.markets_checked}\n"
            f"  markets resolved: {self.markets_resolved}\n"
            f"  positions settled: {self.positions_settled}\n"
            f"  total payout: {self.total_payout:.4f}\n"
            f"  total positions value: {self.total_positions_value:.4f}\n"
            f"  missing data entries: {len(self.missing_data_log)}\n"
            f"  errors: {len(self.errors)}"
        )


async def run_settlement(
    db: Database,
    settings=None,
    use_sample: bool = False,
    dry_run: bool = False,
) -> SettlementResult:
    """Execute settlement for all resolved markets with open positions.

    Args:
        db: connected database.
        settings: app settings.
        use_sample: use sample resolution data.
        dry_run: if True, compute but do not persist settlements.
    """
    if settings is None:
        settings = get_settings()

    result = SettlementResult()
    settlement_engine = SettlementEngine()
    now = datetime.now(timezone.utc)

    # ── Find markets with open positions ──────────────────────────────────
    logger.info("Finding markets with open positions...")
    position_rows = db.fetchall(
        "SELECT DISTINCT market_id FROM positions WHERE quantity > 0"
    )
    market_ids = [row["market_id"] for row in position_rows]
    logger.info("  Found %d markets with positions", len(market_ids))

    if not market_ids:
        logger.info("No open positions requiring settlement.")
        result.ended_at = now
        _record_experiment(db, result)
        return result

    # ── Check each market for resolution ──────────────────────────────────
    for market_id in market_ids:
        result.markets_checked += 1
        try:
            resolution = await _check_resolution(db, market_id, use_sample)
            if resolution is None:
                logger.debug("Market %s not yet resolved", market_id[:8])
                continue

            result.markets_resolved += 1
            resolution_outcome, evidence = resolution
            logger.info(
                "Market %s resolved: outcome=%s",
                market_id[:8], resolution_outcome,
            )

            # ── Settle positions for this market ───────────────────────────
            # PR24B: previously ``fetchall("SELECT * FROM positions ...")``
            # for every resolved market. Replaced with a bounded cursor
            # over explicit columns; only the columns actually consumed
            # by ``SettlementEngine.settle_position`` and ``_persist_settlement``
            # are projected.
            positions: list = []
            for pos in db.iter_rows(
                """SELECT id, market_id, wallet_id, outcome, quantity,
                          avg_entry_price, is_sample
                   FROM positions
                   WHERE market_id = ? AND quantity > 0""",
                (market_id,),
                batch_size=_SETTLE_BATCH_SIZE,
            ):
                positions.append(pos)
                if len(positions) % _RSS_POLL_EVERY_ROWS == 0:
                    check_rss_limit(
                        f"settle:market={market_id[:8]}(rows={len(positions)})",
                        _SETTLE_MAX_RSS_MB,
                    )

            for pos in positions:
                try:
                    evidence_obj = SettlementEvidence(
                        source=evidence["source"],
                        market_source_id=evidence["market_source_id"],
                        resolution_outcome=resolution_outcome,
                        raw_evidence=evidence.get("raw", {}),
                        observed_at=now,
                    )

                    settlement_result = settlement_engine.settle_position(
                        position_id=UUID(pos["id"]),
                        market_id=UUID(market_id),
                        wallet_id=UUID(pos["wallet_id"]),
                        outcome=pos["outcome"],
                        quantity=pos["quantity"],
                        avg_entry_price=pos["avg_entry_price"],
                        evidence=evidence_obj,
                        is_sample=pos["is_sample"],
                    )

                    result.positions_settled += 1
                    result.total_payout += settlement_result.payout
                    result.total_positions_value += pos["quantity"] * pos["avg_entry_price"]

                    if not dry_run:
                        _persist_settlement(db, settlement_result, dict(pos))

                    logger.info(
                        "  Position %s: outcome=%s winner=%s payout=%.4f",
                        pos["id"][:8], pos["outcome"],
                        settlement_result.is_winner, settlement_result.payout,
                    )

                except ValueError as e:
                    if "conflict" in str(e):
                        result.errors.append(f"Settlement conflict: {e}")
                        logger.error("Settlement conflict: %s", e)
                    else:
                        raise

        except Exception as e:
            result.errors.append(f"Settlement error for {market_id}: {e}")
            logger.warning("Failed to process market %s: %s", market_id[:8], e)

    # ── Commit and record ─────────────────────────────────────────────────
    if not dry_run:
        db.conn.commit()

    result.ended_at = now
    if not dry_run:
        _record_experiment(db, result)

    return result


async def _check_resolution(
    db, market_id: str, use_sample: bool
) -> tuple[str, dict] | None:
    """Check if a market has resolved.

    Returns (resolution_outcome, evidence_dict) or None if not resolved.
    """
    # First check DB
    market_row = db.fetchone(
        "SELECT resolved, resolution_outcome, source_id, source FROM markets WHERE id = ?",
        (market_id,),
    )

    if market_row is None:
        return None

    if market_row["resolved"] and market_row["resolution_outcome"]:
        return market_row["resolution_outcome"], {
            "source": "local_db",
            "market_source_id": market_row["source_id"],
            "raw": {"resolution_outcome": market_row["resolution_outcome"]},
        }

    if use_sample:
        return _get_sample_resolution(market_id)

    # Check live Polymarket
    settings = get_settings()
    import httpx

    async with httpx.AsyncClient(
        base_url=settings.gamma_base_url,
        timeout=settings.http_timeout_seconds,
    ) as client:
        try:
            source_id = market_row["source_id"]
            if source_id.startswith("sample-"):
                return _get_sample_resolution(market_id)

            resp = await client.get(f"/markets/{source_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()

            if data.get("resolved"):
                outcome = data.get("resolutionOutcome", "")
                # Update DB with resolution
                db.execute(
                    "UPDATE markets SET resolved = 1, resolution_outcome = ? WHERE id = ?",
                    (outcome, market_id),
                )
                return outcome, {
                    "source": "polymarket_gamma",
                    "market_source_id": source_id,
                    "raw": data,
                }
            return None

        except Exception as e:
            logger.warning("Resolution check failed for %s: %s", market_id, e)
            return None


def _get_sample_resolution(market_id: str) -> tuple[str, dict] | None:
    """Return sample resolution for known sample markets."""
    resolutions = {
        "sample-market-001": "Yes",
        "sample-market-002": "No",
    }
    db = None
    try:
        settings = get_settings()
        db = Database(db_path=settings.db_path)
        db.connect()
        row = db.fetchone("SELECT source_id FROM markets WHERE id = ?", (market_id,))
        if row is None:
            return None
        source_id = row["source_id"]
        outcome = resolutions.get(source_id)
        if outcome:
            return outcome, {
                "source": "sample",
                "market_source_id": source_id,
                "raw": {"resolution_outcome": outcome, "note": "SAMPLE"},
            }
    except Exception:
        pass
    finally:
        if db:
            db.close()
    return None


def _persist_settlement(
    db: Database, settlement_result, position_row: dict
) -> None:
    """Persist settlement result to the position record.

    Since the positions table has CHECK(quantity > 0), we cannot set
    quantity=0. Instead, we mark the position as fully settled by
    recording realized_pnl and setting a tiny residual quantity.
    Alternatively, we delete the position row entirely.
    """
    # Known P06 limitation: realized P&L is computed by SettlementEngine and
    # aggregated in SettlementResult, but there is not yet a closed-position
    # ledger/audit table to persist per-position settlement P&L. Avoid writing
    # partial audit state here; P07+ should add a dedicated settlement ledger.
    # Delete the position (it's fully settled and closed).
    db.execute(
        "DELETE FROM positions WHERE id = ?",
        (position_row["id"],),
    )


def _record_experiment(db: Database, result: SettlementResult) -> None:
    """Record the settlement run as an experiment entry."""
    run = ExperimentRun(
        label=f"settlement-{result.started_at.strftime('%Y%m%dT%H%M%S')}",
        strategy_config={
            "script": "settle_paper_positions.py",
            "markets_checked": result.markets_checked,
        },
        status=ExperimentStatus.COMPLETED,
        started_at=result.started_at,
        ended_at=result.ended_at,
        result_summary={
            "markets_checked": result.markets_checked,
            "markets_resolved": result.markets_resolved,
            "positions_settled": result.positions_settled,
            "total_payout": round(result.total_payout, 4),
            "total_positions_value": round(result.total_positions_value, 4),
            "errors": len(result.errors),
        },
        is_sample=False,
    )
    try:
        db.execute(
            """INSERT INTO experiment_runs
               (id, label, strategy_config, status, started_at, ended_at,
                result_summary, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(run.id), run.label, json.dumps(run.strategy_config),
                run.status.value,
                run.started_at.isoformat() if run.started_at else None,
                run.ended_at.isoformat() if run.ended_at else None,
                json.dumps(run.result_summary), int(run.is_sample),
            ),
        )
        db.conn.commit()
    except Exception as e:
        logger.warning("Failed to record experiment: %s", e)


def main() -> int:
    parser = argparse.ArgumentParser(description="Settle resolved paper positions")
    parser.add_argument("--db", type=str, default=None, help="SQLite database path")
    parser.add_argument("--use-sample", action="store_true", help="Use sample resolution data")
    parser.add_argument("--dry-run", action="store_true", help="Compute but do not persist")
    parser.add_argument("--lock-timeout", type=float, default=DEFAULT_OPERATIONAL_LOCK_TIMEOUT_S, help="Lock timeout seconds")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args()

    setup_logging(args.verbose)

    # PR24D: shared global operational-jobs lock.
    try:
        with operational_job_lock("settle", timeout=args.lock_timeout):
            settings = get_settings()
            db_path = Path(args.db) if args.db else settings.db_path
            db = Database(db_path=db_path)
            db.connect()
            try:
                result = asyncio.run(run_settlement(
                    db=db, settings=settings,
                    use_sample=args.use_sample,
                    dry_run=args.dry_run,
                ))
            finally:
                db.close()
    except LockError as e:
        logger.error("Lock held: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except MemoryLimitExceeded as e:
        logger.error("RSS limit exceeded during settlement: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 3

    print(result.summary())

    if result.missing_data_log:
        for msg in result.missing_data_log[:5]:
            print(f"  WARN: {msg}", file=sys.stderr)
    if result.errors:
        for err in result.errors[:5]:
            print(f"  ERROR: {err}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
