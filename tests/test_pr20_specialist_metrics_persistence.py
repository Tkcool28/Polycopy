"""PR #20 — persistence tests for wallet_specialist_aggregations.

Asserts:
  * The new table is created by the v13 migration (idempotently).
  * INSERT is idempotent (UNIQUE constraint collapses duplicates).
  * Re-running with same idempotency key produces zero new rows.
  * No FK violation when an aggregation row coexists with a
    wallet_score_decisions row for the same wallet.
  * Empty source_trades → ``quality='incomplete'``, missing_essentials
    contains ``trade_count`` and the BLOCKED metrics.
  * No accidental writes to orders/positions/signals/wallet_balances.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from polycopy.db.database import Database
from polycopy.db.schema import SCHEMA_VERSION, CURRENT_DDL
from polycopy.scoring.specialist_metrics import aggregate_specialist_metrics
from polycopy.scoring.specialist_metrics_persistence import (
    generate_specialist_idempotency_key,
    load_specialist_aggregations_for_wallet,
    persist_wallet_specialist_aggregation,
)


def _make_db() -> Database:
    """Create a fresh in-memory Database with the current schema."""
    fd, path = tempfile.mkstemp(suffix=".db")
    import os
    os.close(fd)
    db = Database(db_path=Path(path))
    db.connect()
    # Apply the full schema.
    for stmt in CURRENT_DDL:
        db.execute(stmt)
    return db


def _make_wallet(db: Database, wallet_id: str = "w-uuid-1") -> str:
    """Insert a wallets row so FK references resolve."""
    db.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at, canonical_address) "
        "VALUES (?, ?, 'test', 0, ?, ?)",
        (wallet_id, f"0xwallet-{wallet_id}", "2026-07-04T00:00:00+00:00",
         f"0xwallet-{wallet_id}"),
    )
    return wallet_id


def _make_trade(db: Database, wallet_id: str, market_id: str = "market-1",
                timestamp: str = "2026-07-01T00:00:00+00:00",
                is_sample: int = 0,
                source_trade_id: str | None = None) -> None:
    sid = source_trade_id or f"poly:{market_id}:{timestamp}"
    db.execute(
        "INSERT INTO source_trades (id, source, source_trade_id, market_source_id, "
        "side, outcome, quantity, price, trader_address, timestamp, is_sample, token_id) "
        "VALUES (?, 'polymarket_data_api', ?, ?, 'buy', 'Yes', 1.0, 0.5, ?, ?, ?, NULL)",
        (
            f"trade-{sid}",
            sid,
            market_id,
            f"0xwallet-{wallet_id}",
            timestamp,
            is_sample,
        ),
    )


class V13SchemaTests(unittest.TestCase):
    def test_schema_version_is_13(self):
        self.assertEqual(SCHEMA_VERSION, 13)

    def test_new_table_present(self):
        db = _make_db()
        try:
            rows = db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='wallet_specialist_aggregations'"
            )
            self.assertEqual(len(rows), 1)
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)

    def test_indexes_present(self):
        db = _make_db()
        try:
            rows = db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'idx_wsa_%'"
            )
            names = {r["name"] for r in rows}
            self.assertIn("idx_wsa_wallet", names)
            self.assertIn("idx_wsa_category", names)
            self.assertIn("idx_wsa_quality", names)
            self.assertIn("idx_wsa_wallet_category", names)
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)


class PersistenceTests(unittest.TestCase):
    def test_idempotent_insert(self):
        db = _make_db()
        try:
            wallet_id = _make_wallet(db)
            metrics = aggregate_specialist_metrics(
                wallet_id=wallet_id,
                category_label="us-politics",
                all_trades_for_wallet=[],
                category_trades_for_wallet=[],
            )
            ts = "2026-07-01T00:00:00+00:00"
            # First insert: should succeed.
            persist_wallet_specialist_aggregation(  # noqa: F841
                db,
                wallet_id=wallet_id,
                category_label="us-politics",
                source_data_timestamp=ts,
                metrics=metrics,
            )
            # Second insert with same key: INSERT OR IGNORE → no row.
            persist_wallet_specialist_aggregation(  # noqa: F841
                db,
                wallet_id=wallet_id,
                category_label="us-politics",
                source_data_timestamp=ts,
                metrics=metrics,
            )
            rows = db.fetchall(
                "SELECT * FROM wallet_specialist_aggregations WHERE wallet_id=?",
                (wallet_id,),
            )
            self.assertEqual(len(rows), 1, "duplicate idempotency key must collapse to 1 row")
            # Quality reflects the empty bundle.
            self.assertEqual(rows[0]["quality"], "incomplete")
            self.assertIn("trade_count", json.loads(rows[0]["missing_essentials_json"]))
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)

    def test_quality_partial_for_real_wallet_with_blocked_metrics(self):
        db = _make_db()
        try:
            wallet_id = _make_wallet(db, "w-uuid-2")
            _make_trade(db, wallet_id, market_id="m1", timestamp="2026-07-01T00:00:00+00:00")
            _make_trade(db, wallet_id, market_id="m2", timestamp="2026-07-02T00:00:00+00:00")
            trades = db.fetchall("SELECT * FROM source_trades")
            metrics = aggregate_specialist_metrics(
                wallet_id=wallet_id,
                category_label="us-politics",
                all_trades_for_wallet=[dict(t) for t in trades],
                category_trades_for_wallet=[dict(t) for t in trades],
            )
            persist_wallet_specialist_aggregation(
                db,
                wallet_id=wallet_id,
                category_label="us-politics",
                source_data_timestamp="2026-07-02T00:00:00+00:00",
                metrics=metrics,
            )
            rows = load_specialist_aggregations_for_wallet(db, wallet_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["trade_count"], 2)
            self.assertEqual(rows[0]["distinct_markets"], 2)
            self.assertEqual(rows[0]["active_trading_days"], 2)
            self.assertEqual(rows[0]["sample_reliability_score"], 1.0)
            self.assertEqual(rows[0]["quality"], "partial")
            missing = json.loads(rows[0]["missing_essentials_json"])
            self.assertIn("resolved_markets", missing)
            self.assertIn("win_rate_realized", missing)
            self.assertIn("realized_pnl", missing)
            self.assertIn("profit_factor", missing)
            self.assertIn("max_drawdown", missing)
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)

    def test_coexists_with_wallet_score_decisions(self):
        """A specialist aggregation row must NOT interfere with the
        existing wallet_score_decisions writes (no shared FK)."""
        db = _make_db()
        try:
            wallet_id = _make_wallet(db, "w-uuid-3")
            db.execute(
                "INSERT INTO wallet_score_decisions (wallet_id, formula_name, formula_version, "
                "idempotency_key, final_score, verdict, computed_at, created_at) "
                "VALUES (?, 'wallet_score', '1', 'idem-test', 0.0, 'incomplete', "
                "'2026-07-04T00:00:00+00:00', '2026-07-04T00:00:00+00:00')",
                (wallet_id,),
            )
            metrics = aggregate_specialist_metrics(
                wallet_id=wallet_id,
                category_label="us-politics",
                all_trades_for_wallet=[],
                category_trades_for_wallet=[],
            )
            persist_wallet_specialist_aggregation(
                db,
                wallet_id=wallet_id,
                category_label="us-politics",
                source_data_timestamp="2026-07-04T00:00:00+00:00",
                metrics=metrics,
            )
            # Both rows coexist.
            sd = db.fetchone("SELECT COUNT(*) AS c FROM wallet_score_decisions WHERE wallet_id=?", (wallet_id,))
            sa = db.fetchone("SELECT COUNT(*) AS c FROM wallet_specialist_aggregations WHERE wallet_id=?", (wallet_id,))
            self.assertIsNotNone(sd)
            self.assertIsNotNone(sa)
            self.assertEqual(sd["c"], 1)
            self.assertEqual(sa["c"], 1)
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)


class SafetyTests(unittest.TestCase):
    def test_no_orders_positions_signals_balances_writes(self):
        """Paper-only safety: persisting an aggregation row must NOT
        write to orders / positions / signals / wallet_balances /
        paper_signal_decisions (is_approved=1)."""
        db = _make_db()
        try:
            wallet_id = _make_wallet(db, "w-uuid-safety")
            metrics = aggregate_specialist_metrics(
                wallet_id=wallet_id,
                category_label="",
                all_trades_for_wallet=[],
                category_trades_for_wallet=[],
            )
            persist_wallet_specialist_aggregation(
                db,
                wallet_id=wallet_id,
                category_label="",
                source_data_timestamp="2026-07-04T00:00:00+00:00",
                metrics=metrics,
            )
            for table in ("orders", "positions", "signals", "wallet_balances",
                          "paper_signal_decisions"):
                row = db.fetchone(f"SELECT COUNT(*) AS c FROM {table}")
                self.assertIsNotNone(row)
                self.assertEqual(row["c"], 0,
                                 f"safety violation: {table} should be empty")
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)

    def test_quality_validation_rejects_unknown_value(self):
        db = _make_db()
        try:
            wallet_id = _make_wallet(db, "w-uuid-bad-quality")
            metrics = {
                "wallet_id": wallet_id,
                "category_label": "",
                "trade_count": None,
                "distinct_markets": None,
                "distinct_events": None,
                "active_trading_days": None,
                "category_trade_count": None,
                "category_distinct_markets": None,
                "category_active_days": None,
                "category_concentration": None,
                "sample_reliability_score": None,
                "holding_period_days": None,
                "behavior_classification": "unknown",
                "copyability_evidence_state": "unknown",
                "price_improvement_state": "unknown",
                "component_scores_json": {},
                "quality": "fictional_quality",
                "missing_essentials_json": ["trade_count"],
            }
            with self.assertRaises(ValueError):
                persist_wallet_specialist_aggregation(
                    db,
                    wallet_id=wallet_id,
                    category_label="",
                    source_data_timestamp="2026-07-04T00:00:00+00:00",
                    metrics=metrics,
                )
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)


class IdempotencyKeyTests(unittest.TestCase):
    def test_same_inputs_same_key(self):
        k1 = generate_specialist_idempotency_key(
            wallet_id="w1", category_label="x", source_data_timestamp="2026-07-04T00:00:00+00:00"
        )
        k2 = generate_specialist_idempotency_key(
            wallet_id="w1", category_label="x", source_data_timestamp="2026-07-04T00:00:00+00:00"
        )
        self.assertEqual(k1, k2)

    def test_different_category_different_key(self):
        k1 = generate_specialist_idempotency_key(
            wallet_id="w1", category_label="x", source_data_timestamp=None
        )
        k2 = generate_specialist_idempotency_key(
            wallet_id="w1", category_label="y", source_data_timestamp=None
        )
        self.assertNotEqual(k1, k2)

    def test_different_timestamp_different_key(self):
        k1 = generate_specialist_idempotency_key(
            wallet_id="w1", category_label="x", source_data_timestamp="2026-07-04T00:00:00+00:00"
        )
        k2 = generate_specialist_idempotency_key(
            wallet_id="w1", category_label="x", source_data_timestamp="2026-07-05T00:00:00+00:00"
        )
        self.assertNotEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()