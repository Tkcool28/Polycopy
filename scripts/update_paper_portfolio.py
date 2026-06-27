#!/usr/bin/env python3
"""update_paper_portfolio.py — Update paper portfolio with current marks and P&L.

Reads open positions from the database, fetches current market prices
(via sample or live Polymarket), computes mark-to-market values, and
updates the positions table with current_price and unrealized_pnl.

Also:
- Checks pending paper orders for review-delay eligibility
- Logs missing data (unresolvable markets, stale prices)
- Records experiment run for the portfolio update cycle

Exit codes:
    0 — success
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
from polycopy.domain.market import Market, MarketOutcome
from polycopy.risk.marks import MarkEngine, MarkPrice
from polycopy.utils.concurrency import FileLock, LockError, lock_path

logger = logging.getLogger(__name__)


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


class PortfolioUpdateResult:
    """Tracks portfolio update outcomes."""

    def __init__(self) -> None:
        self.positions_updated: int = 0
        self.positions_missing_price: int = 0
        self.orders_checked: int = 0
        self.orders_ready: int = 0
        self.total_unrealized_pnl: float = 0.0
        self.total_market_value: float = 0.0
        self.total_cost_basis: float = 0.0
        self.missing_data_log: list[str] = []
        self.errors: list[str] = []
        self.started_at = datetime.now(timezone.utc)
        self.ended_at: datetime | None = None

    def summary(self) -> str:
        return (
            f"portfolio update complete\n"
            f"  positions updated: {self.positions_updated}\n"
            f"  positions missing price: {self.positions_missing_price}\n"
            f"  orders checked: {self.orders_checked}\n"
            f"  orders ready to fill: {self.orders_ready}\n"
            f"  total cost basis: {self.total_cost_basis:.4f}\n"
            f"  total market value: {self.total_market_value:.4f}\n"
            f"  total unrealized P&L: {self.total_unrealized_pnl:.4f}\n"
            f"  missing data entries: {len(self.missing_data_log)}\n"
            f"  errors: {len(self.errors)}"
        )


async def update_portfolio(
    db: Database,
    settings=None,
    use_sample: bool = False,
) -> PortfolioUpdateResult:
    """Update all open positions with current mark prices.

    Steps:
    1. Load all open positions from DB
    2. Fetch current prices for each market/outcome
    3. Compute mark-to-market
    4. Update positions table with current_price and unrealized_pnl
    5. Check pending orders for review-delay eligibility
    6. Record experiment run
    """
    if settings is None:
        settings = get_settings()

    result = PortfolioUpdateResult()
    mark_engine = MarkEngine(use_conservative_mark=settings.use_conservative_mark)
    now = datetime.now(timezone.utc)

    # ── Load open positions ────────────────────────────────────────────────
    logger.info("Loading open positions...")
    positions = db.fetchall(
        "SELECT * FROM positions WHERE quantity > 0"
    )
    logger.info("  Found %d open positions", len(positions))

    if not positions:
        logger.info("No open positions to update.")
        result.ended_at = now
        _record_experiment(db, result)
        return result

    # ── Fetch current prices ──────────────────────────────────────────────
    logger.info("Fetching current market prices...")
    market_ids = set(p["market_id"] for p in positions)

    for market_id in market_ids:
        try:
            market = await _fetch_market_prices(db, market_id, use_sample)
            if market is None:
                result.missing_data_log.append(
                    f"Market {market_id} not available from any source"
                )
                result.positions_missing_price += 1
                continue

            for outcome in market.outcomes:
                mark = MarkPrice(
                    market_id=market_id,
                    outcome=outcome.label,
                    mark_price=outcome.price,
                    bid_price=outcome.price - 0.01,  # estimated spread
                    ask_price=outcome.price + 0.01,
                    source=market.source,
                    observed_at=now,
                    is_sample=market.is_sample,
                )
                mark_engine.update_price(mark)

        except Exception as e:
            result.errors.append(f"Price fetch error for {market_id}: {e}")
            logger.warning("Failed to fetch prices for %s: %s", market_id, e)

    # ── Mark-to-market each position ──────────────────────────────────────
    logger.info("Computing mark-to-market for %d positions...", len(positions))
    for pos in positions:
        try:
            position_id = pos["id"]
            market_id = pos["market_id"]
            outcome = pos["outcome"]
            quantity = pos["quantity"]
            avg_entry = pos["avg_entry_price"]

            maybe_mark = mark_engine.get_mark(market_id, outcome)
            if maybe_mark is None:
                result.positions_missing_price += 1
                result.missing_data_log.append(
                    f"No price for {market_id}/{outcome} (position {position_id[:8]})"
                )
                continue

            mark = maybe_mark
            current_price = mark.mark_price
            unrealized_pnl = (current_price - avg_entry) * quantity
            market_value = current_price * quantity
            cost_basis = avg_entry * quantity

            # Update position in DB
            db.execute(
                """UPDATE positions
                   SET current_price = ?, updated_at = ?
                   WHERE id = ?""",
                (current_price, now.isoformat(), position_id),
            )

            result.positions_updated += 1
            result.total_unrealized_pnl += unrealized_pnl
            result.total_market_value += market_value
            result.total_cost_basis += cost_basis

        except Exception as e:
            result.errors.append(f"Position update error {pos['id'] if 'id' in pos.keys() else '?'}: {e}")
            logger.warning("Failed to update position: %s", e)

    # ── Check pending orders ──────────────────────────────────────────────
    logger.info("Checking pending paper orders...")
    pending_orders = db.fetchall(
        "SELECT * FROM orders WHERE status = 'pending'"
    )
    result.orders_checked = len(pending_orders)

    from polycopy.risk.fill_model import ReviewDelay
    for order in pending_orders:
        review = ReviewDelay(
            delay_seconds=settings.review_delay_seconds,
            started_at=order["created_at"],
        )
        try:
            if review.is_eligible(now):
                result.orders_ready += 1
                logger.info(
                    "Order %s ready for review (review delay elapsed)",
                    str(order["id"])[:8],
                )
        except Exception as e:
            logger.debug("Review check error: %s", e)

    db.conn.commit()

    # ── Record experiment ─────────────────────────────────────────────────
    result.ended_at = now
    _record_experiment(db, result)

    return result


async def _fetch_market_prices(
    db, market_id: str, use_sample: bool
) -> Market | None:
    """Fetch current market prices. Returns None if unavailable."""
    if use_sample:
        return _get_sample_market(market_id)

    settings = get_settings()
    import httpx

    async with httpx.AsyncClient(
        base_url=settings.gamma_base_url,
        timeout=settings.http_timeout_seconds,
    ) as client:
        try:
            # Try to find the market by source_id
            market_row = db.fetchone(
                "SELECT source_id FROM markets WHERE id = ?", (market_id,)
            )
            if market_row is None:
                return None

            source_id = market_row["source_id"]
            if source_id.startswith("sample-"):
                logger.warning(
                    "Market %s is sample-backed; refusing sample prices without --use-sample",
                    market_id,
                )
                return None

            resp = await client.get(f"/markets/{source_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()

            # Parse outcomes
            import json as _json
            outcomes_raw = data.get("outcomes", "[]")
            prices_raw = data.get("outcomePrices", "[]")
            if isinstance(outcomes_raw, str):
                outcomes_raw = _json.loads(outcomes_raw)
            if isinstance(prices_raw, str):
                prices_raw = _json.loads(prices_raw)

            outcomes = []
            for i, label in enumerate(outcomes_raw):
                price = float(prices_raw[i]) if i < len(prices_raw) else 0.5
                outcomes.append(MarketOutcome(label=str(label), price=price))

            return Market(
                id=UUID(market_id),
                source_id=source_id,
                question=data.get("question", ""),
                outcomes=outcomes,
                source="polymarket",
                active=data.get("active", False),
                closed=data.get("closed", False),
                resolved=data.get("resolved", False),
                volume_24h=float(data.get("volume24hr", 0) or 0),
                fetched_at=datetime.now(timezone.utc),
                is_sample=False,
            )

        except Exception as e:
            logger.warning(
                "Failed to fetch live market %s; no sample price fallback without --use-sample: %s",
                market_id,
                e,
            )
            return None


def _get_sample_market(market_id: str) -> Market | None:
    """Return a labeled sample market for the given ID."""
    now = datetime.now(timezone.utc)
    return Market(
        id=UUID(market_id),
        source_id="sample-market-fallback",
        question="Sample market (fallback pricing)  [SAMPLE DATA]",
        outcomes=[
            MarketOutcome(label="Yes", price=0.65, volume=50000.0),
            MarketOutcome(label="No", price=0.35, volume=30000.0),
        ],
        source="sample",
        active=True, closed=False, resolved=False,
        volume_24h=80000.0,
        fetched_at=now, is_sample=True,
    )


def _record_experiment(db: Database, result: PortfolioUpdateResult) -> None:
    """Record the portfolio update as an experiment run."""
    run = ExperimentRun(
        label=f"portfolio-update-{result.started_at.strftime('%Y%m%dT%H%M%S')}",
        strategy_config={
            "script": "update_paper_portfolio.py",
            "positions_updated": result.positions_updated,
        },
        status=ExperimentStatus.COMPLETED,
        started_at=result.started_at,
        ended_at=result.ended_at,
        result_summary={
            "positions_updated": result.positions_updated,
            "positions_missing_price": result.positions_missing_price,
            "orders_checked": result.orders_checked,
            "orders_ready": result.orders_ready,
            "total_unrealized_pnl": round(result.total_unrealized_pnl, 4),
            "total_market_value": round(result.total_market_value, 4),
            "total_cost_basis": round(result.total_cost_basis, 4),
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
    parser = argparse.ArgumentParser(description="Update paper portfolio with current marks")
    parser.add_argument("--db", type=str, default=None, help="SQLite database path")
    parser.add_argument("--use-sample", action="store_true", help="Use sample pricing data")
    parser.add_argument("--lock-timeout", type=float, default=10.0, help="Lock timeout seconds")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args()

    setup_logging(args.verbose)

    lock = FileLock(lock_path("portfolio_update"), timeout=args.lock_timeout)
    try:
        with lock:
            settings = get_settings()
            db_path = Path(args.db) if args.db else settings.db_path
            db = Database(db_path=db_path)
            db.connect()
            try:
                result = asyncio.run(update_portfolio(db=db, settings=settings, use_sample=args.use_sample))
            finally:
                db.close()
    except LockError as e:
        logger.error("Lock held: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

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
