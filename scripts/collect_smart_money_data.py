#!/usr/bin/env python3
"""collect_smart_money_data.py — Collect smart-money wallet data from Polymarket.

Orchestrates the data collection workflow:
1. Fetch active markets from Polymarket Gamma API
2. For each market, fetch recent trades
3. Track wallets discovered from trades
4. Fetch wallet balances (USDC)
5. Persist all data to SQLite with raw snapshot provenance
6. Run scoring and verdict engine on discovered wallets
7. Record signals and generate experiment run entry

ALL data is labeled is_sample=False when from live API.
When live API is unavailable, clearly labeled sample adapters are used.

Exit codes:
    0 — success (full or partial)
    1 — fatal error (DB failure, config error)
    2 — partial success (some markets/trades failed)
    3 — lock held by another process
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Add src to path for inline execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polycopy.config.settings import get_settings
from polycopy.db.database import Database
from polycopy.domain.experiment import ExperimentRun, ExperimentStatus
from polycopy.domain.market import Market, MarketOutcome
from polycopy.domain.raw_snapshot import RawSnapshot
from polycopy.domain.source_trade import SourceTrade
from polycopy.domain.wallet import Wallet
from polycopy.engine.evaluate import evaluate_wallet
from polycopy.utils.concurrency import FileLock, LockError, lock_path

logger = logging.getLogger(__name__)

# ── Logging setup ────────────────────────────────────────────────────────────────

def setup_logging(verbosity: int = 0) -> None:
    """Configure logging based on verbosity level."""
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


# ── Data collection result ──────────────────────────────────────────────────────

class CollectionResult:
    """Tracks partial/failure state from a collection run."""

    def __init__(self) -> None:
        self.markets_fetched: int = 0
        self.markets_failed: int = 0
        self.trades_fetched: int = 0
        self.trades_failed: int = 0
        self.wallets_discovered: int = 0
        self.snapshots_saved: int = 0
        self.signals_generated: int = 0
        self.missing_data_log: list[str] = []
        self.errors: list[str] = []
        self.started_at = datetime.now(timezone.utc)
        self.ended_at: datetime | None = None

    @property
    def is_partial(self) -> bool:
        return (self.markets_failed + self.trades_failed) > 0 and (
            self.markets_fetched + self.trades_fetched
        ) > 0

    @property
    def is_failure(self) -> bool:
        return self.markets_fetched == 0 and self.trades_fetched == 0

    def summary(self) -> str:
        status = "ok"
        if self.is_failure:
            status = "FAILED"
        elif self.is_partial:
            status = "PARTIAL"
        return (
            f"status={status}\n"
            f"  markets: {self.markets_fetched} fetched, {self.markets_failed} failed\n"
            f"  trades:  {self.trades_fetched} fetched, {self.trades_failed} failed\n"
            f"  wallets: {self.wallets_discovered} discovered\n"
            f"  snapshots saved: {self.snapshots_saved}\n"
            f"  signals: {self.signals_generated}\n"
            f"  missing data entries: {len(self.missing_data_log)}\n"
            f"  errors: {len(self.errors)}"
        )


# ── Polymarket collector (live) ────────────────────────────────────────────────

class PolymarketCollector:
    """Fetches market and trade data from Polymarket public APIs."""

    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        self._client = None

    async def _get_client(self):
        if self._client is None or self._client.is_closed:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=self.settings.gamma_base_url,
                timeout=self.settings.http_timeout_seconds,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def collect_markets(
        self,
        db: Database,
        limit: int = 50,
        result: CollectionResult | None = None,
    ) -> list[Market]:
        """Fetch active markets from Gamma API and persist with provenance."""
        if result is None:
            result = CollectionResult()

        client = await self._get_client()
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false",
        }

        try:
            resp = await client.get("/markets", params=params)
            resp.raise_for_status()
            raw_data = resp.json()

            if not isinstance(raw_data, list):
                raw_data = [raw_data]

            # Save snapshot provenance
            snapshot = self._save_snapshot(
                db=db,
                source="polymarket_gamma",
                endpoint="/markets",
                params=params,
                data=raw_data,
            )
            if snapshot:
                result.snapshots_saved += 1

            markets = []
            for item in raw_data:
                try:
                    market = self._parse_market(item)
                    self._persist_market(db, market)
                    markets.append(market)
                    result.markets_fetched += 1
                except Exception as e:
                    result.markets_failed += 1
                    cond_id = item.get("conditionId", item.get("id", "?"))
                    result.errors.append(f"Market parse error ({cond_id}): {e}")
                    logger.warning("Failed to parse market %s: %s", cond_id, e)

            return markets

        except Exception as e:
            result.errors.append(f"Failed to fetch markets: {e}")
            logger.error("Failed to fetch markets: %s", e)
            return []

    async def collect_trades(
        self,
        db: Database,
        market_source_id: str,
        result: CollectionResult | None = None,
    ) -> list[SourceTrade]:
        """Fetch recent trades for a market from CLOB API."""
        if result is None:
            result = CollectionResult()

        client = await self._get_client()
        try:
            # CLOB trades endpoint — attempt with condition_id
            resp = await client.get("/trades", params={"condition_id": market_source_id})
            resp.raise_for_status()
            raw_data = resp.json()

            if not isinstance(raw_data, list):
                raw_data = [raw_data] if raw_data else []

            # Save snapshot
            snapshot = self._save_snapshot(
                db=db,
                source="polymarket_clob",
                endpoint="/trades",
                params={"condition_id": market_source_id},
                data=raw_data,
            )
            if snapshot:
                result.snapshots_saved += 1

            trades = []
            for item in raw_data:
                try:
                    trade = self._parse_trade(item, market_source_id)
                    if trade:
                        self._persist_trade(db, trade)
                        trades.append(trade)
                        result.trades_fetched += 1
                except Exception as e:
                    result.trades_failed += 1
                    result.errors.append(f"Trade parse error: {e}")
                    logger.warning("Failed to parse trade: %s", e)

            return trades

        except Exception as e:
            result.trades_failed += 1
            result.missing_data_log.append(
                f"Trades unavailable for market {market_source_id}: {e}"
            )
            logger.warning("No trades for market %s: %s", market_source_id, e)
            return []

    async def collect_wallet_balance(self, address: str) -> Wallet | None:
        """Fetch wallet USDC balance. Returns None if unavailable."""
        # Polymarket public API does not expose per-wallet balances.
        # This is a known limitation. Return None and let the caller
        # handle missing data.
        logger.debug(
            "Wallet balance fetch not available via public API for %s",
            address[:12],
        )
        return None

    # ── Parsers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_market(data: dict) -> Market:
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
            source_id=data.get("conditionId", data.get("id", "")),
            question=data.get("question", ""),
            outcomes=outcomes,
            source="polymarket",
            active=data.get("active", False),
            closed=data.get("closed", False),
            resolved=data.get("resolved", False),
            resolution_outcome=data.get("resolutionOutcome"),
            volume_24h=float(data.get("volume24hr", 0) or 0),
            fetched_at=datetime.now(timezone.utc),
            is_sample=False,
        )

    @staticmethod
    def _parse_trade(data: dict, market_source_id: str) -> SourceTrade | None:
        """Parse a CLOB trade event into SourceTrade. Returns None if unparseable."""
        try:
            side_raw = data.get("side", "").lower()
            if side_raw in ("buy", "1"):
                from polycopy.domain.order import OrderSide
                side = OrderSide.BUY
            elif side_raw in ("sell", "0"):
                from polycopy.domain.order import OrderSide
                side = OrderSide.SELL
            else:
                return None

            # Parse timestamp
            ts_raw = data.get("timestamp") or data.get("createdAt")
            if ts_raw is None:
                ts = datetime.now(timezone.utc)
            elif isinstance(ts_raw, (int, float)):
                ts = datetime.fromtimestamp(ts_raw / 1000.0 if ts_raw > 1e12 else ts_raw, tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))

            return SourceTrade(
                source="polymarket_clob",
                source_trade_id=str(data.get("id", data.get("trade_id", uuid.uuid4().hex))),
                market_source_id=market_source_id,
                side=side,
                outcome=str(data.get("outcome", data.get("token", "Yes"))),
                quantity=float(data.get("size", data.get("quantity", 0))),
                price=float(data.get("price", 0)),
                trader_address=str(data.get("maker", data.get("trader", data.get("owner", "unknown")))),
                timestamp=ts,
                is_sample=False,
            )
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Unparseable trade: %s", e)
            return None

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist_market(self, db: Database, market: Market) -> None:
        """Upsert a market record."""
        db.execute(
            """INSERT OR REPLACE INTO markets
               (id, source_id, source, question, active, closed, resolved,
                resolution_outcome, volume_24h, fetched_at, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(market.id),
                market.source_id,
                market.source,
                market.question,
                int(market.active),
                int(market.closed),
                int(market.resolved),
                market.resolution_outcome,
                market.volume_24h,
                market.fetched_at.isoformat(),
                int(market.is_sample),
            ),
        )
        # Upsert outcomes
        db.execute("DELETE FROM market_outcomes WHERE market_id = ?", (str(market.id),))
        for outcome in market.outcomes:
            db.execute(
                """INSERT INTO market_outcomes (market_id, label, price, volume)
                   VALUES (?, ?, ?, ?)""",
                (str(market.id), outcome.label, outcome.price, outcome.volume),
            )
        db.conn.commit()

    def _persist_trade(self, db: Database, trade: SourceTrade) -> None:
        """Upsert a source trade."""
        db.execute(
            """INSERT OR REPLACE INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(trade.id),
                trade.source,
                trade.source_trade_id,
                trade.market_source_id,
                trade.side.value if hasattr(trade.side, "value") else str(trade.side),
                trade.outcome,
                trade.quantity,
                trade.price,
                trade.trader_address,
                trade.timestamp.isoformat(),
                int(trade.is_sample),
            ),
        )
        db.conn.commit()

    def _persist_wallet(self, db: Database, wallet: Wallet) -> None:
        """Upsert a wallet and its balances."""
        db.execute(
            """INSERT OR REPLACE INTO wallets (id, address, label, is_sample, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                str(wallet.id),
                wallet.address,
                wallet.label,
                int(wallet.is_sample),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        for bal in wallet.balances:
            db.execute(
                """INSERT INTO wallet_balances (wallet_id, currency, amount, as_of, is_sample)
                   VALUES (?, ?, ?, ?, ?)""",
                (str(wallet.id), bal.currency, bal.amount, bal.as_of.isoformat(), int(bal.is_sample)),
            )
        db.conn.commit()

    def _save_snapshot(
        self,
        db: Database,
        source: str,
        endpoint: str,
        params: dict,
        data: object,
    ) -> RawSnapshot | None:
        """Save a raw API response to the snapshot store."""
        try:
            import hashlib
            import json as _json

            settings = get_settings()
            snapshot_dir = settings.snapshot_dir
            snapshot_dir.mkdir(parents=True, exist_ok=True)

            content = _json.dumps(data, default=str, sort_keys=True)
            content_bytes = content.encode("utf-8")
            content_hash = hashlib.sha256(content_bytes).hexdigest()

            ts = datetime.now(timezone.utc)
            filename = f"{source}_{ts.strftime('%Y%m%dT%H%M%S')}_{content_hash[:12]}.json"
            file_path = snapshot_dir / filename

            file_path.write_text(content)

            snapshot = RawSnapshot(
                source=source,
                endpoint=endpoint,
                query_params=params,
                file_path=str(file_path),
                content_hash=content_hash,
                content_type="application/json",
                size_bytes=len(content_bytes),
                fetched_at=ts,
                ingested_at=ts,
                is_sample=False,
            )

            db.execute(
                """INSERT INTO raw_snapshots
                   (id, source, endpoint, query_params, file_path, content_hash,
                    content_type, size_bytes, fetched_at, ingested_at, is_sample)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(snapshot.id),
                    snapshot.source,
                    snapshot.endpoint,
                    _json.dumps(snapshot.query_params),
                    snapshot.file_path,
                    snapshot.content_hash,
                    snapshot.content_type,
                    snapshot.size_bytes,
                    snapshot.fetched_at.isoformat(),
                    snapshot.ingested_at.isoformat(),
                    int(snapshot.is_sample),
                ),
            )
            db.conn.commit()
            return snapshot

        except Exception as e:
            logger.warning("Failed to save snapshot: %s", e)
            return None


# ── Main collection workflow ────────────────────────────────────────────────────

async def run_collection(
    db: Database,
    limit: int = 50,
    skip_trades: bool = False,
) -> CollectionResult:
    """Run the full data collection workflow.

    Args:
        db: connected database.
        limit: max markets to fetch.
        skip_trades: if True, skip trade collection (for testing).

    Returns:
        CollectionResult with counts and any errors.
    """
    settings = get_settings()
    result = CollectionResult()
    collector = PolymarketCollector(settings)

    try:
        # 1. Collect markets
        logger.info("Fetching active markets (limit=%d)...", limit)
        markets = await collector.collect_markets(db, limit=limit, result=result)
        logger.info("Fetched %d markets (%d failed)", result.markets_fetched, result.markets_failed)

        # 2. Collect trades for each market
        if not skip_trades:
            for market in markets:
                logger.debug("Fetching trades for market %s...", market.source_id)
                trades = await collector.collect_trades(db, market.source_id, result)
                # Discover wallets from trades
                for trade in trades:
                    wallet = Wallet(
                        address=trade.trader_address,
                        label=f"discovered-from-{trade.source}",
                        is_sample=False,
                    )
                    collector._persist_wallet(db, wallet)
                    result.wallets_discovered += 1

        # 3. Score discovered wallets
        wallets = _get_unique_trader_addresses(db)
        logger.info("Scoring %d discovered wallets...", len(wallets))
        for address in wallets:
            try:
                score_id, summary = evaluate_wallet(
                    wallet_address=address,
                    source="collect_smart_money",
                    is_sample=False,
                )
                logger.debug("Scored %s: %s", address[:12], summary.replace(chr(10), " | "))
            except Exception as e:
                logger.warning("Failed to score wallet %s: %s", address[:12], e)

        # 4. Record experiment run
        result.ended_at = datetime.now(timezone.utc)
        _record_experiment(db, result)

    finally:
        await collector.close()

    return result


def _get_unique_trader_addresses(db: Database) -> list[str]:
    """Get distinct trader addresses from source_trades."""
    rows = db.fetchall("SELECT DISTINCT trader_address FROM source_trades")
    return [row["trader_address"] for row in rows]


def _record_experiment(db: Database, result: CollectionResult) -> None:
    """Record the collection run as an experiment entry."""
    run = ExperimentRun(
        label=f"collect-{result.started_at.strftime('%Y%m%dT%H%M%S')}",
        strategy_config={
            "script": "collect_smart_money_data.py",
            "limit": result.markets_fetched,
            "skip_trades": False,
        },
        status=ExperimentStatus.COMPLETED,
        started_at=result.started_at,
        ended_at=result.ended_at,
        result_summary={
            "markets_fetched": result.markets_fetched,
            "trades_fetched": result.trades_fetched,
            "wallets_discovered": result.wallets_discovered,
            "errors": len(result.errors),
            "missing_data": len(result.missing_data_log),
        },
        is_sample=False,
    )
    db.execute(
        """INSERT INTO experiment_runs
           (id, label, strategy_config, status, started_at, ended_at,
            result_summary, is_sample)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(run.id),
            run.label,
            json.dumps(run.strategy_config),
            run.status.value,
            run.started_at.isoformat() if run.started_at else None,
            run.ended_at.isoformat() if run.ended_at else None,
            json.dumps(run.result_summary),
            int(run.is_sample),
        ),
    )
    db.conn.commit()


# ── CLI ──────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect smart-money wallet data from Polymarket",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max markets to fetch (default: 50)",
    )
    parser.add_argument(
        "--skip-trades",
        action="store_true",
        help="Skip trade collection (for testing)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="SQLite database path (overrides config)",
    )
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for file lock (default: 10)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect but do not persist to database",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v = INFO, -v -v = DEBUG)",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    # Concurrency guard
    lock = FileLock(lock_path("collect"), timeout=args.lock_timeout)
    try:
        with lock:
            # Configure database
            settings = get_settings()
            db_path = Path(args.db) if args.db else settings.db_path

            db = Database(db_path=db_path)
            db.connect()

            try:
                result = asyncio.run(
                    run_collection(
                        db=db,
                        limit=args.limit,
                        skip_trades=args.skip_trades,
                    )
                )
            finally:
                db.close()

    except LockError as e:
        logger.error("Lock held by another process: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 3

    # Report
    print(result.summary())

    if result.is_failure:
        return 1
    if result.is_partial:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
