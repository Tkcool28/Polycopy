#!/usr/bin/env python3
"""seed_demo_data.py — Seed the database with realistic fictional demo data.

Creates a complete, sanitized, clearly-labeled fictional dataset for
demonstration and development. ALL data is marked is_sample=True.

Demonstrates:
- Multiple verdicts: COPY_CANDIDATE, WATCHLIST, SKIP, INCOMPLETE
- Wallet clusters (related wallets with shared signals)
- Paper positions (open + closed)
- Paper orders (pending, filled, rejected)
- Signals with varying confidence/edge
- Markets with varying liquidity/spread/volume
- Rejections: stale, low liquidity, spread too wide, price too far from mark
- Experiment run records
- Raw snapshot provenance

Usage:
    python scripts/seed_demo_data.py [--db path/to/db.sqlite]

Exit codes:
    0 — success
    1 — error
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polycopy.config.settings import get_settings
from polycopy.db.database import Database
from polycopy.domain.experiment import ExperimentRun, ExperimentStatus
from polycopy.domain.market import Market, MarketOutcome
from polycopy.domain.order import Order, OrderSide, OrderStatus, OrderType
from polycopy.domain.performance import PerformanceSummary
from polycopy.domain.position import Position
from polycopy.domain.raw_snapshot import RawSnapshot
from polycopy.domain.signal import Signal, SignalStrength
from polycopy.domain.source_trade import SourceTrade
from polycopy.domain.wallet import Wallet, WalletBalance
from polycopy.scoring.engine import score_wallet
from polycopy.utils.concurrency import FileLock, lock_path

logger = logging.getLogger(__name__)

# ── Fixed UUIDs for reproducible demo data ──────────────────────────────────

WALLETS = {
    "alpha": uuid.UUID("10000000-0000-0000-0000-000000000001"),  # COPY_CANDIDATE
    "beta": uuid.UUID("10000000-0000-0000-0000-000000000002"),   # COPY_CANDIDATE
    "gamma": uuid.UUID("10000000-0000-0000-0000-000000000003"),  # WATCHLIST
    "delta": uuid.UUID("10000000-0000-0000-0000-000000000004"),  # SKIP
    "epsilon": uuid.UUID("10000000-0000-0000-0000-000000000005"),  # INCOMPLETE
    "zeta": uuid.UUID("10000000-0000-0000-0000-000000000006"),    # COPY_CANDIDATE (cluster with alpha)
}

MARKETS = {
    "election": uuid.UUID("20000000-0000-0000-0000-000000000001"),
    "btc_150k": uuid.UUID("20000000-0000-0000-0000-000000000002"),
    "ai_regulation": uuid.UUID("20000000-0000-0000-0000-000000000003"),
    "low_liquidity": uuid.UUID("20000000-0000-0000-0000-000000000004"),
    "wide_spread": uuid.UUID("20000000-0000-0000-0000-000000000005"),
    "resolved_yes": uuid.UUID("20000000-0000-0000-0000-000000000006"),
}

NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


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


def seed_demo_data(db: Database, force: bool = False) -> None:
    """Insert all demo data into the database.

    Args:
        db: connected database.
        force: if True, clear existing sample data first.
    """
    if force:
        _clear_sample_data(db)

    logger.info("Seeding demo data (all is_sample=True)...")

    _seed_wallets(db)
    _seed_markets(db)
    _seed_source_trades(db)
    _seed_scores(db)
    _seed_signals(db)
    _seed_orders(db)
    _seed_positions(db)
    _seed_decision_log(db)
    _seed_performance_summaries(db)
    _seed_raw_snapshots(db)
    _seed_experiment_runs(db)

    logger.info("Demo data seeded successfully.")


def _clear_sample_data(db: Database) -> None:
    """Remove all existing sample data for a clean re-seed."""
    logger.info("Clearing existing sample data...")
    # Delete child rows before parent rows so this remains valid with
    # SQLite foreign-key enforcement enabled. In particular:
    # decision_log -> orders/wallets/markets, orders/positions/signals ->
    # wallets/markets, wallet_balances/performance_summaries -> wallets,
    # and market_outcomes -> markets.
    tables_with_is_sample = [
        "decision_log", "signals", "positions", "orders",
        "wallet_balances", "performance_summaries", "source_trades",
        "raw_snapshots", "experiment_runs", "wallets", "markets",
    ]
    tables_without_is_sample = [
        "market_outcomes",  # no is_sample column; cleared before markets
    ]
    for table in tables_without_is_sample:
        db.execute(f"DELETE FROM {table}")
    for table in tables_with_is_sample:
        db.execute(f"DELETE FROM {table} WHERE is_sample = 1")
    db.conn.commit()


def _seed_wallets(db: Database) -> None:
    """Seed fictional wallets with varied profiles."""
    wallets = [
        # Alpha: high-performing trader (COPY_CANDIDATE)
        Wallet(
            id=WALLETS["alpha"],
            address="0xALPHA_SMART_MONEY_ADDRESS_000001",
            label="alpha-smart-money  [SAMPLE DATA]",
            balances=[
                WalletBalance(currency="USDC", amount=250000.0, as_of=NOW, is_sample=True),
                WalletBalance(currency="WETH", amount=50.0, as_of=NOW, is_sample=True),
            ],
            is_sample=True,
        ),
        # Beta: consistent trader (COPY_CANDIDATE)
        Wallet(
            id=WALLETS["beta"],
            address="0xBETA_CONSISTENT_TRADER_000002",
            label="beta-consistent  [SAMPLE DATA]",
            balances=[
                WalletBalance(currency="USDC", amount=120000.0, as_of=NOW, is_sample=True),
            ],
            is_sample=True,
        ),
        # Gamma: moderate performer (WATCHLIST)
        Wallet(
            id=WALLETS["gamma"],
            address="0XGAMMA_MODERATE_TRADER_000003",
            label="gamma-moderate  [SAMPLE DATA]",
            balances=[
                WalletBalance(currency="USDC", amount=45000.0, as_of=NOW, is_sample=True),
            ],
            is_sample=True,
        ),
        # Delta: poor performer (SKIP)
        Wallet(
            id=WALLETS["delta"],
            address="0xDELTA_POOR_PERFORMER_000004",
            label="delta-poor  [SAMPLE DATA]",
            balances=[
                WalletBalance(currency="USDC", amount=2000.0, as_of=NOW, is_sample=True),
            ],
            is_sample=True,
        ),
        # Epsilon: new trader with incomplete data (INCOMPLETE)
        Wallet(
            id=WALLETS["epsilon"],
            address="0xEPSILON_NEW_TRADER_000005",
            label="epsilon-new  [SAMPLE DATA]",
            balances=[
                WalletBalance(currency="USDC", amount=500.0, as_of=NOW, is_sample=True),
            ],
            is_sample=True,
        ),
        # Zeta: related to alpha (cluster) (COPY_CANDIDATE)
        Wallet(
            id=WALLETS["zeta"],
            address="0xZETA_CLUSTER_WALLET_000006",
            label="zeta-cluster-related  [SAMPLE DATA]",
            balances=[
                WalletBalance(currency="USDC", amount=80000.0, as_of=NOW, is_sample=True),
            ],
            is_sample=True,
        ),
    ]

    for wallet in wallets:
        db.execute(
            """INSERT OR REPLACE INTO wallets (id, address, label, is_sample, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (str(wallet.id), wallet.address, wallet.label, 1, NOW.isoformat()),
        )
        for bal in wallet.balances:
            db.execute(
                """INSERT INTO wallet_balances (wallet_id, currency, amount, as_of, is_sample)
                   VALUES (?, ?, ?, ?, ?)""",
                (str(wallet.id), bal.currency, bal.amount, bal.as_of.isoformat(), 1),
            )

    db.conn.commit()
    logger.info("  Seeded %d wallets", len(wallets))


def _seed_markets(db: Database) -> None:
    """Seed fictional markets with varied characteristics."""
    markets = [
        # Election: high volume, tight spread
        Market(
            id=MARKETS["election"],
            source_id="sample-election-2028",
            question="Will Trump win 2028 US presidential election?  [SAMPLE DATA]",
            outcomes=[
                MarketOutcome(label="Yes", price=0.58, volume=2500000.0),
                MarketOutcome(label="No", price=0.42, volume=1800000.0),
            ],
            source="sample",
            active=True, closed=False, resolved=False,
            volume_24h=4300000.0,
            end_date=NOW + timedelta(days=365),
            fetched_at=NOW, is_sample=True,
        ),
        # BTC 150k: moderate volume
        Market(
            id=MARKETS["btc_150k"],
            source_id="sample-btc-150k-2026",
            question="Will BTC exceed $150,000 by end of 2026?  [SAMPLE DATA]",
            outcomes=[
                MarketOutcome(label="Yes", price=0.35, volume=900000.0),
                MarketOutcome(label="No", price=0.65, volume=700000.0),
            ],
            source="sample",
            active=True, closed=False, resolved=False,
            volume_24h=1600000.0,
            end_date=NOW + timedelta(days=180),
            fetched_at=NOW, is_sample=True,
        ),
        # AI Regulation: moderate volume
        Market(
            id=MARKETS["ai_regulation"],
            source_id="sample-ai-regulation",
            question="Will US pass comprehensive AI regulation by Q4 2026?  [SAMPLE DATA]",
            outcomes=[
                MarketOutcome(label="Yes", price=0.72, volume=400000.0),
                MarketOutcome(label="No", price=0.28, volume=200000.0),
            ],
            source="sample",
            active=True, closed=False, resolved=False,
            volume_24h=600000.0,
            end_date=NOW + timedelta(days=180),
            fetched_at=NOW, is_sample=True,
        ),
        # Low liquidity: thin book (rejection candidate)
        Market(
            id=MARKETS["low_liquidity"],
            source_id="sample-low-liq-market",
            question="Will a major bank adopt Polymarket by 2027?  [SAMPLE DATA]",
            outcomes=[
                MarketOutcome(label="Yes", price=0.55, volume=500.0),
                MarketOutcome(label="No", price=0.45, volume=300.0),
            ],
            source="sample",
            active=True, closed=False, resolved=False,
            volume_24h=800.0,
            fetched_at=NOW, is_sample=True,
        ),
        # Wide spread: high slippage (rejection candidate)
        Market(
            id=MARKETS["wide_spread"],
            source_id="sample-wide-spread",
            question="Will Polymarket volume exceed $10B monthly?  [SAMPLE DATA]",
            outcomes=[
                MarketOutcome(label="Yes", price=0.50, volume=5000.0),
                MarketOutcome(label="No", price=0.50, volume=5000.0),
            ],
            source="sample",
            active=True, closed=False, resolved=False,
            volume_24h=10000.0,
            fetched_at=NOW, is_sample=True,
        ),
        # Resolved: for settlement demo
        Market(
            id=MARKETS["resolved_yes"],
            source_id="sample-resolved-yes",
            question="Will Ethereum reach $10,000 by end of 2025?  [SAMPLE DATA]",
            outcomes=[
                MarketOutcome(label="Yes", price=0.85, volume=2000000.0),
                MarketOutcome(label="No", price=0.15, volume=500000.0),
            ],
            source="sample",
            active=False, closed=True, resolved=True,
            resolution_outcome="Yes",
            volume_24h=0.0,
            end_date=NOW - timedelta(days=180),
            fetched_at=NOW, is_sample=True,
        ),
    ]

    for market in markets:
        db.execute(
            """INSERT OR REPLACE INTO markets
               (id, source_id, source, question, active, closed, resolved,
                resolution_outcome, volume_24h, end_date, fetched_at, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(market.id), market.source_id, market.source, market.question,
                int(market.active), int(market.closed), int(market.resolved),
                market.resolution_outcome, market.volume_24h,
                market.end_date.isoformat() if market.end_date else None,
                market.fetched_at.isoformat(), 1,
            ),
        )
        for outcome in market.outcomes:
            db.execute(
                """INSERT INTO market_outcomes (market_id, label, price, volume)
                   VALUES (?, ?, ?, ?)""",
                (str(market.id), outcome.label, outcome.price, outcome.volume),
            )

    db.conn.commit()
    logger.info("  Seeded %d markets", len(markets))


def _seed_source_trades(db: Database) -> None:
    """Seed fictional trades for each wallet."""
    trades = [
        # Alpha: many recent, profitable-looking trades
        *[
            SourceTrade(
                id=uuid.UUID(f"30000000-0000-0000-0000-{i:012d}"),
                source="sample",
                source_trade_id=f"sample-alpha-trade-{i:03d}",
                market_source_id="sample-election-2028",
                side=OrderSide.BUY,
                outcome="Yes",
                quantity=100.0 + i * 10,
                price=0.55 + (i % 5) * 0.01,
                trader_address="0xALPHA_SMART_MONEY_ADDRESS_000001",
                timestamp=NOW - timedelta(minutes=i * 30),
                is_sample=True,
            )
            for i in range(1, 21)
        ],
        # Beta: consistent moderate trades
        *[
            SourceTrade(
                id=uuid.UUID(f"30000000-0000-0001-0000-{i:012d}"),
                source="sample",
                source_trade_id=f"sample-beta-trade-{i:03d}",
                market_source_id="sample-btc-150k-2026",
                side=OrderSide.BUY,
                outcome="No",
                quantity=50.0 + i * 5,
                price=0.62 + (i % 3) * 0.01,
                trader_address="0xBETA_CONSISTENT_TRADER_000002",
                timestamp=NOW - timedelta(hours=i * 2),
                is_sample=True,
            )
            for i in range(1, 16)
        ],
        # Gamma: fewer trades, mixed
        *[
            SourceTrade(
                id=uuid.UUID(f"30000000-0000-0002-0000-{i:012d}"),
                source="sample",
                source_trade_id=f"sample-gamma-trade-{i:03d}",
                market_source_id="sample-ai-regulation",
                side=OrderSide.BUY if i % 2 == 0 else OrderSide.SELL,
                outcome="Yes" if i % 2 == 0 else "No",
                quantity=20.0 + i * 3,
                price=0.70 + (i % 4) * 0.01,
                trader_address="0XGAMMA_MODERATE_TRADER_000003",
                timestamp=NOW - timedelta(hours=i * 6),
                is_sample=True,
            )
            for i in range(1, 11)
        ],
        # Delta: few trades, mostly losing
        *[
            SourceTrade(
                id=uuid.UUID(f"30000000-0000-0003-0000-{i:012d}"),
                source="sample",
                source_trade_id=f"sample-delta-trade-{i:03d}",
                market_source_id="sample-election-2028",
                side=OrderSide.BUY,
                outcome="No",
                quantity=5.0 + i,
                price=0.45 - (i % 3) * 0.01,
                trader_address="0xDELTA_POOR_PERFORMER_000004",
                timestamp=NOW - timedelta(hours=i * 12),
                is_sample=True,
            )
            for i in range(1, 6)
        ],
        # Epsilon: only 1 trade (INCOMPLETE data)
        SourceTrade(
            id=uuid.UUID("30000000-0000-0004-0000-000000000001"),
            source="sample",
            source_trade_id="sample-epsilon-trade-001",
            market_source_id="sample-ai-regulation",
            side=OrderSide.BUY,
            outcome="Yes",
            quantity=2.0,
            price=0.72,
            trader_address="0xEPSILON_NEW_TRADER_000005",
            timestamp=NOW - timedelta(days=1),
            is_sample=True,
        ),
        # Zeta: clustered with alpha (similar timing, same market)
        *[
            SourceTrade(
                id=uuid.UUID(f"30000000-0000-0005-0000-{i:012d}"),
                source="sample",
                source_trade_id=f"sample-zeta-trade-{i:03d}",
                market_source_id="sample-election-2028",
                side=OrderSide.BUY,
                outcome="Yes",
                quantity=80.0 + i * 8,
                price=0.56 + (i % 4) * 0.01,
                trader_address="0xZETA_CLUSTER_WALLET_000006",
                timestamp=NOW - timedelta(minutes=i * 30 + 5),  # 5min offset from alpha
                is_sample=True,
            )
            for i in range(1, 18)
        ],
        # Stale trade (for staleness rejection demo)
        SourceTrade(
            id=uuid.UUID("30000000-0000-0006-0000-000000000001"),
            source="sample",
            source_trade_id="sample-stale-trade-001",
            market_source_id="sample-election-2028",
            side=OrderSide.BUY,
            outcome="Yes",
            quantity=200.0,
            price=0.50,
            trader_address="0xALPHA_SMART_MONEY_ADDRESS_000001",
            timestamp=NOW - timedelta(hours=2),  # stale (> 120s threshold)
            is_sample=True,
        ),
    ]

    for trade in trades:
        db.execute(
            """INSERT OR REPLACE INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(trade.id), trade.source, trade.source_trade_id,
                trade.market_source_id,
                trade.side.value if hasattr(trade.side, "value") else str(trade.side),
                trade.outcome, trade.quantity, trade.price,
                trade.trader_address, trade.timestamp.isoformat(), 1,
            ),
        )

    db.conn.commit()
    logger.info("  Seeded %d source trades", len(trades))


def _seed_scores(db: Database) -> None:
    """Compute and persist copyability scores for each wallet."""
    # Use the scoring engine for real computation
    score_configs = [
        (WALLETS["alpha"], 2.1, 0.73, 20, NOW - timedelta(minutes=30), NOW - timedelta(days=45)),
        (WALLETS["beta"], 1.5, 0.65, 15, NOW - timedelta(hours=2), NOW - timedelta(days=30)),
        (WALLETS["gamma"], 0.8, 0.55, 10, NOW - timedelta(hours=6), NOW - timedelta(days=14)),
        (WALLETS["delta"], 0.2, 0.30, 5, NOW - timedelta(hours=12), NOW - timedelta(days=7)),
        (WALLETS["epsilon"], None, None, 1, None, None),  # INCOMPLETE
        (WALLETS["zeta"], 1.8, 0.70, 17, NOW - timedelta(minutes=35), NOW - timedelta(days=40)),
    ]

    for wallet_id, sharpe, win_rate, trade_count, latest_ts, first_ts in score_configs:
        score = score_wallet(
            wallet_id=wallet_id,
            sharpe_ratio=sharpe,
            win_rate=win_rate,
            trade_count=trade_count,
            latest_trade_ts=latest_ts,
            first_trade_ts=first_ts,
            markets_traded=3 if trade_count and trade_count > 1 else None,
            now=NOW,
            is_sample=True,
        )
        db.execute(
            """INSERT OR REPLACE INTO signals
               (id, market_id, source, strength, confidence, edge_estimate,
                predicted_prob, market_prob, reasoning, produced_at, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(score.id), str(MARKETS["election"]), "scoring_engine",
                "buy" if score.score >= 70 else "neutral",
                min(score.score / 100.0, 0.95),
                round((score.score / 100.0) - 0.5, 4),
                score.score / 100.0,
                0.5,
                f"Score={score.score:.1f} Verdict={score.verdict.value}",
                NOW.isoformat(), 1,
            ),
        )

    db.conn.commit()
    logger.info("  Seeded %d scores", len(score_configs))


def _seed_signals(db: Database) -> None:
    """Seed trading signals for markets."""
    signals = [
        Signal(
            id=uuid.UUID("40000000-0000-0000-0000-000000000001"),
            market_id=MARKETS["election"],
            source="demo_signal_v1",
            strength=SignalStrength.BUY,
            confidence=0.82,
            edge_estimate=0.08,
            predicted_prob=0.66,
            market_prob=0.58,
            reasoning="Smart money accumulation detected on Yes side  [SAMPLE DATA]",
            produced_at=NOW, is_sample=True,
        ),
        Signal(
            id=uuid.UUID("40000000-0000-0000-0000-000000000002"),
            market_id=MARKETS["btc_150k"],
            source="demo_signal_v1",
            strength=SignalStrength.SELL,
            confidence=0.65,
            edge_estimate=-0.05,
            predicted_prob=0.60,
            market_prob=0.65,
            reasoning="Institutional flow suggests BTC downside  [SAMPLE DATA]",
            produced_at=NOW, is_sample=True,
        ),
        Signal(
            id=uuid.UUID("40000000-0000-0000-0000-000000000003"),
            market_id=MARKETS["ai_regulation"],
            source="demo_signal_v1",
            strength=SignalStrength.STRONG_BUY,
            confidence=0.90,
            edge_estimate=0.22,
            predicted_prob=0.82,
            market_prob=0.70,
            reasoning="High conviction signal from multiple smart money wallets  [SAMPLE DATA]",
            produced_at=NOW, is_sample=True,
        ),
        Signal(
            id=uuid.UUID("40000000-0000-0000-0000-000000000004"),
            market_id=MARKETS["low_liquidity"],
            source="demo_signal_v1",
            strength=SignalStrength.NEUTRAL,
            confidence=0.30,
            edge_estimate=0.02,
            predicted_prob=0.52,
            market_prob=0.50,
            reasoning="REJECTION: Liquidity too low for position sizing  [SAMPLE DATA]",
            produced_at=NOW, is_sample=True,
        ),
        Signal(
            id=uuid.UUID("40000000-0000-0000-0000-000000000005"),
            market_id=MARKETS["wide_spread"],
            source="demo_signal_v1",
            strength=SignalStrength.NEUTRAL,
            confidence=0.25,
            edge_estimate=0.01,
            predicted_prob=0.51,
            market_prob=0.50,
            reasoning="REJECTION: Spread too wide, edge consumed by slippage  [SAMPLE DATA]",
            produced_at=NOW, is_sample=True,
        ),
    ]

    for signal in signals:
        db.execute(
            """INSERT OR REPLACE INTO signals
               (id, market_id, source, strength, confidence, edge_estimate,
                predicted_prob, market_prob, reasoning, produced_at, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(signal.id), str(signal.market_id), signal.source,
                signal.strength.value, signal.confidence, signal.edge_estimate,
                signal.predicted_prob, signal.market_prob, signal.reasoning,
                signal.produced_at.isoformat(), 1,
            ),
        )

    db.conn.commit()
    logger.info("  Seeded %d signals", len(signals))


def _seed_orders(db: Database) -> None:
    """Seed paper orders in various states."""
    orders = [
        # Filled order (alpha bought election Yes)
        Order(
            id=uuid.UUID("50000000-0000-0000-0000-000000000001"),
            market_id=MARKETS["election"],
            wallet_id=WALLETS["alpha"],
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            outcome="Yes",
            quantity=100.0,
            price=0.57,
            status=OrderStatus.FILLED,
            filled_quantity=100.0,
            created_at=NOW - timedelta(hours=3),
            updated_at=NOW - timedelta(hours=3),
            is_sample=True,
        ),
        # Pending order (beta, waiting for review)
        Order(
            id=uuid.UUID("50000000-0000-0000-0000-000000000002"),
            market_id=MARKETS["btc_150k"],
            wallet_id=WALLETS["beta"],
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            outcome="No",
            quantity=50.0,
            price=0.63,
            status=OrderStatus.PENDING,
            filled_quantity=0.0,
            created_at=NOW - timedelta(minutes=15),
            updated_at=NOW - timedelta(minutes=15),
            is_sample=True,
        ),
        # Rejected order (delta, risk gate)
        Order(
            id=uuid.UUID("50000000-0000-0000-0000-000000000003"),
            market_id=MARKETS["election"],
            wallet_id=WALLETS["delta"],
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            outcome="Yes",
            quantity=500.0,
            price=0.58,
            status=OrderStatus.REJECTED,
            filled_quantity=0.0,
            created_at=NOW - timedelta(hours=1),
            updated_at=NOW - timedelta(hours=1),
            is_sample=True,
        ),
        # Cancelled order (gamma)
        Order(
            id=uuid.UUID("50000000-0000-0000-0000-000000000004"),
            market_id=MARKETS["ai_regulation"],
            wallet_id=WALLETS["gamma"],
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            outcome="Yes",
            quantity=20.0,
            price=0.72,
            status=OrderStatus.CANCELLED,
            filled_quantity=0.0,
            created_at=NOW - timedelta(hours=2),
            updated_at=NOW - timedelta(hours=1),
            is_sample=True,
        ),
    ]

    for order in orders:
        updated_at = order.updated_at.isoformat() if order.updated_at is not None else None
        db.execute(
            """INSERT OR REPLACE INTO orders
               (id, market_id, wallet_id, side, order_type, outcome, quantity,
                price, status, filled_quantity, created_at, updated_at, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(order.id), str(order.market_id), str(order.wallet_id),
                order.side.value, order.order_type.value, order.outcome,
                order.quantity, order.price, order.status.value,
                order.filled_quantity, order.created_at.isoformat(),
                updated_at, 1,
            ),
        )

    db.conn.commit()
    logger.info("  Seeded %d orders", len(orders))


def _seed_positions(db: Database) -> None:
    """Seed paper positions."""
    positions = [
        # Open position: alpha long election Yes
        Position(
            id=uuid.UUID("60000000-0000-0000-0000-000000000001"),
            market_id=MARKETS["election"],
            wallet_id=WALLETS["alpha"],
            outcome="Yes",
            quantity=100.0,
            avg_entry_price=0.57,
            current_price=0.58,
            realized_pnl=0.0,
            opened_at=NOW - timedelta(hours=3),
            updated_at=NOW,
            is_sample=True,
        ),
        # Open position: beta short BTC 150k
        Position(
            id=uuid.UUID("60000000-0000-0000-0000-000000000002"),
            market_id=MARKETS["btc_150k"],
            wallet_id=WALLETS["beta"],
            outcome="No",
            quantity=50.0,
            avg_entry_price=0.63,
            current_price=0.65,
            realized_pnl=0.0,
            opened_at=NOW - timedelta(hours=1),
            updated_at=NOW,
            is_sample=True,
        ),
        # Open position: zeta long election Yes (cluster)
        Position(
            id=uuid.UUID("60000000-0000-0000-0000-000000000003"),
            market_id=MARKETS["election"],
            wallet_id=WALLETS["zeta"],
            outcome="Yes",
            quantity=80.0,
            avg_entry_price=0.56,
            current_price=0.58,
            realized_pnl=0.0,
            opened_at=NOW - timedelta(hours=2),
            updated_at=NOW,
            is_sample=True,
        ),
        # Open position: resolved market (for settlement demo)
        Position(
            id=uuid.UUID("60000000-0000-0000-0000-000000000004"),
            market_id=MARKETS["resolved_yes"],
            wallet_id=WALLETS["alpha"],
            outcome="Yes",
            quantity=200.0,
            avg_entry_price=0.80,
            current_price=0.85,
            realized_pnl=0.0,
            opened_at=NOW - timedelta(days=200),
            updated_at=NOW - timedelta(days=180),
            is_sample=True,
        ),
    ]

    for pos in positions:
        updated_at = pos.updated_at.isoformat() if pos.updated_at is not None else None
        db.execute(
            """INSERT OR REPLACE INTO positions
               (id, market_id, wallet_id, outcome, quantity, avg_entry_price,
                current_price, realized_pnl, opened_at, updated_at, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(pos.id), str(pos.market_id), str(pos.wallet_id),
                pos.outcome, pos.quantity, pos.avg_entry_price,
                pos.current_price, pos.realized_pnl,
                pos.opened_at.isoformat(), updated_at, 1,
            ),
        )

    db.conn.commit()
    logger.info("  Seeded %d positions", len(positions))


def _seed_decision_log(db: Database) -> None:
    """Seed decision log entries."""
    decisions = [
        {
            "id": "70000000-0000-0000-0000-000000000001",
            "wallet_id": str(WALLETS["alpha"]),
            "market_id": str(MARKETS["election"]),
            "decision_type": "copy_candidate",
            "signal_ids": [str(uuid.UUID("40000000-0000-0000-0000-000000000001"))],
            "rationale": "Score 82.5 — strong smart money signal  [SAMPLE DATA]",
            "metrics": {"score": 82.5, "threshold": 70.0},
        },
        {
            "id": "70000000-0000-0000-0000-000000000002",
            "wallet_id": str(WALLETS["gamma"]),
            "market_id": str(MARKETS["ai_regulation"]),
            "decision_type": "watchlist",
            "signal_ids": [],
            "rationale": "Score 58.2 — moderate signal, monitoring  [SAMPLE DATA]",
            "metrics": {"score": 58.2, "threshold": 70.0},
        },
        {
            "id": "70000000-0000-0000-0000-000000000003",
            "wallet_id": str(WALLETS["delta"]),
            "market_id": str(MARKETS["election"]),
            "decision_type": "skip",
            "signal_ids": [],
            "rationale": "Score 28.1 — below threshold, poor track record  [SAMPLE DATA]",
            "metrics": {"score": 28.1, "threshold": 50.0},
        },
        {
            "id": "70000000-0000-0000-0000-000000000004",
            "wallet_id": str(WALLETS["epsilon"]),
            "market_id": str(MARKETS["ai_regulation"]),
            "decision_type": "incomplete",
            "signal_ids": [],
            "rationale": "Insufficient data — only 1 trade observed  [SAMPLE DATA]",
            "metrics": {"trade_count": 1, "missing": ["sharpe_ratio", "win_rate"]},
        },
        {
            "id": "70000000-0000-0000-0000-000000000005",
            "wallet_id": str(WALLETS["delta"]),
            "market_id": str(MARKETS["low_liquidity"]),
            "decision_type": "rejection_stale",
            "signal_ids": [],
            "rationale": "REJECTION: Trade data stale (>120s old)  [SAMPLE DATA]",
            "metrics": {"staleness_seconds": 7200, "threshold": 120},
        },
        {
            "id": "70000000-0000-0000-0000-000000000006",
            "wallet_id": str(WALLETS["gamma"]),
            "market_id": str(MARKETS["wide_spread"]),
            "decision_type": "rejection_spread",
            "signal_ids": [],
            "rationale": "REJECTION: Bid-ask spread too wide, edge consumed  [SAMPLE DATA]",
            "metrics": {"spread": 0.05, "edge": 0.01},
        },
    ]

    for d in decisions:
        db.execute(
            """INSERT OR REPLACE INTO decision_log
               (id, wallet_id, market_id, decision_type, signal_ids,
                rationale, metrics, created_at, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                d["id"], d["wallet_id"], d["market_id"], d["decision_type"],
                json.dumps(d["signal_ids"]), d["rationale"],
                json.dumps(d["metrics"]), NOW.isoformat(), 1,
            ),
        )

    db.conn.commit()
    logger.info("  Seeded %d decision log entries", len(decisions))


def _seed_performance_summaries(db: Database) -> None:
    """Seed performance summary records."""
    summaries = [
        PerformanceSummary(
            wallet_id=WALLETS["alpha"],
            strategy_label="smart-money-copy",
            start_date=NOW - timedelta(days=30),
            end_date=NOW,
            total_pnl=12500.0,
            realized_pnl=8000.0,
            unrealized_pnl=4500.0,
            win_rate=0.68,
            sharpe_ratio=2.1,
            max_drawdown=0.12,
            trade_count=20,
            is_sample=True,
        ),
        PerformanceSummary(
            wallet_id=WALLETS["beta"],
            strategy_label="smart-money-copy",
            start_date=NOW - timedelta(days=30),
            end_date=NOW,
            total_pnl=5200.0,
            realized_pnl=4000.0,
            unrealized_pnl=1200.0,
            win_rate=0.60,
            sharpe_ratio=1.5,
            max_drawdown=0.08,
            trade_count=15,
            is_sample=True,
        ),
        PerformanceSummary(
            wallet_id=WALLETS["delta"],
            strategy_label="smart-money-copy",
            start_date=NOW - timedelta(days=30),
            end_date=NOW,
            total_pnl=-1800.0,
            realized_pnl=-1500.0,
            unrealized_pnl=-300.0,
            win_rate=0.30,
            sharpe_ratio=0.2,
            max_drawdown=0.25,
            trade_count=5,
            is_sample=True,
        ),
    ]

    for s in summaries:
        db.execute(
            """INSERT OR REPLACE INTO performance_summaries
               (wallet_id, strategy_label, start_date, end_date, total_pnl,
                realized_pnl, unrealized_pnl, win_rate, sharpe_ratio,
                max_drawdown, trade_count, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(s.wallet_id), s.strategy_label,
                s.start_date.isoformat(), s.end_date.isoformat(),
                s.total_pnl, s.realized_pnl, s.unrealized_pnl,
                s.win_rate, s.sharpe_ratio, s.max_drawdown,
                s.trade_count, 1,
            ),
        )

    db.conn.commit()
    logger.info("  Seeded %d performance summaries", len(summaries))


def _seed_raw_snapshots(db: Database) -> None:
    """Seed raw snapshot provenance records."""
    snapshots = [
        RawSnapshot(
            source="polymarket_gamma",
            endpoint="/markets",
            query_params={"active": "true", "closed": "false", "limit": "50"},
            file_path="data/snapshots/polymarket_gamma_20260627T120000_abc123.json",
            content_hash="abc123def456abc123def456abc123def456ab",
            content_type="application/json",
            size_bytes=45000,
            fetched_at=NOW,
            ingested_at=NOW,
            is_sample=True,
        ),
        RawSnapshot(
            source="polymarket_clob",
            endpoint="/trades",
            query_params={"condition_id": "sample-election-2028"},
            file_path="data/snapshots/polymarket_clob_20260627T120005_def456.json",
            content_hash="def456abc789def456abc789def456abc789de",
            content_type="application/json",
            size_bytes=12000,
            fetched_at=NOW,
            ingested_at=NOW,
            is_sample=True,
        ),
    ]

    for snap in snapshots:
        db.execute(
            """INSERT OR REPLACE INTO raw_snapshots
               (id, source, endpoint, query_params, file_path, content_hash,
                content_type, size_bytes, fetched_at, ingested_at, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(snap.id), snap.source, snap.endpoint,
                json.dumps(snap.query_params), snap.file_path,
                snap.content_hash, snap.content_type, snap.size_bytes,
                snap.fetched_at.isoformat(), snap.ingested_at.isoformat(), 1,
            ),
        )

    db.conn.commit()
    logger.info("  Seeded %d raw snapshots", len(snapshots))


def _seed_experiment_runs(db: Database) -> None:
    """Seed experiment run records."""
    runs = [
        ExperimentRun(
            label="demo-seed-run-001",
            strategy_config={
                "script": "seed_demo_data.py",
                "description": "Initial demo data seed",
            },
            status=ExperimentStatus.COMPLETED,
            started_at=NOW - timedelta(minutes=5),
            ended_at=NOW - timedelta(minutes=4),
            result_summary={
                "wallets": 6,
                "markets": 6,
                "trades": 75,
                "signals": 5,
                "orders": 4,
                "positions": 4,
                "decision_log": 6,
                "performance_summaries": 3,
                "raw_snapshots": 2,
            },
            is_sample=True,
        ),
    ]

    for run in runs:
        db.execute(
            """INSERT OR REPLACE INTO experiment_runs
               (id, label, strategy_config, status, started_at, ended_at,
                result_summary, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(run.id), run.label, json.dumps(run.strategy_config),
                run.status.value,
                run.started_at.isoformat() if run.started_at else None,
                run.ended_at.isoformat() if run.ended_at else None,
                json.dumps(run.result_summary), 1,
            ),
        )

    db.conn.commit()
    logger.info("  Seeded %d experiment runs", len(runs))


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed demo data for Polycopy")
    parser.add_argument("--db", type=str, default=None, help="SQLite database path")
    parser.add_argument("--force", action="store_true", help="Clear existing sample data first")
    parser.add_argument("--lock-timeout", type=float, default=10.0, help="Lock timeout seconds")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args()

    setup_logging(args.verbose)

    lock = FileLock(lock_path("seed"), timeout=args.lock_timeout)
    try:
        with lock:
            settings = get_settings()
            db_path = Path(args.db) if args.db else settings.db_path
            db = Database(db_path=db_path)
            db.connect()
            try:
                seed_demo_data(db, force=args.force)
            finally:
                db.close()
    except Exception as e:
        logger.error("Seed failed: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print("Demo data seeded successfully.")
    print("  All data is marked is_sample=True.")
    print("  Use --force to clear and re-seed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
