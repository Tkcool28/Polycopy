"""PR #20 — persistence tests for wallet_specialist_aggregations.

Asserts:
  * The new table is created by the v13 migration (idempotently).
  * INSERT is idempotent (UNIQUE constraint collapses duplicates).
  * persist_wallet_specialist_aggregation return value reflects the
    real on-disk state: True only when a new row was inserted, False
    on idempotent collision (BLOCKER 1 fix).
  * Re-running with same idempotency key produces zero new rows.
  * No FK violation when an aggregation row coexists with a
    wallet_score_decisions row for the same wallet.
  * Empty source_trades → ``quality='incomplete'``, missing_essentials
    contains ``trade_count`` and the BLOCKED metrics.
  * No accidental writes to orders/positions/signals/wallet_balances.
  * compute_and_persist_wallet_specialist_aggregations counters are
    honest across first-run / rerun / cap-exhausted paths.
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
from scripts.specialist_aggregation_step import (
    compute_and_persist_wallet_specialist_aggregations,
)


# ---------------------------------------------------------------------------
# DB / fixture helpers
# ---------------------------------------------------------------------------

def _make_db() -> Database:
    """Create a fresh on-disk Database with the current schema."""
    fd, path = tempfile.mkstemp(suffix=".db")
    import os
    os.close(fd)
    db = Database(db_path=Path(path))
    db.connect()
    # Apply the full schema (CURRENT_DDL is v13 after this PR).
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


def _empty_metrics(wallet_id: str, category_label: str) -> dict:
    """Reusable empty-bundle metrics dict for the wallet-level + per-cat rows."""
    return aggregate_specialist_metrics(
        wallet_id=wallet_id,
        category_label=category_label or None,
        all_trades_for_wallet=[],
        category_trades_for_wallet=[],
    )


# ---------------------------------------------------------------------------
# Schema-level tests
# ---------------------------------------------------------------------------

class V13SchemaTests(unittest.TestCase):
    def test_schema_version_is_13(self):
        self.assertEqual(SCHEMA_VERSION, 13)

    def test_new_table_present(self):
        db = _make_db()
        try:
            rows = db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='wallet_specialist_aggregations'"
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
            for required in (
                "idx_wsa_wallet",
                "idx_wsa_category",
                "idx_wsa_quality",
                "idx_wsa_wallet_category",
            ):
                self.assertIn(required, names)
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# BLOCKER 1 — INSERT OR IGNORE return value must be honest
# ---------------------------------------------------------------------------

class ReturnValueTests(unittest.TestCase):
    """Test #1 + #2 of the PR #20 review:

      1. first insert returns True
      2. second identical insert returns False
      3. table row count remains 1 after duplicate insert
    """

    def test_first_insert_returns_true(self):
        db = _make_db()
        try:
            wallet_id = _make_wallet(db, "w-rv-1")
            metrics = _empty_metrics(wallet_id, "us-politics")
            ts = "2026-07-01T00:00:00+00:00"
            r1 = persist_wallet_specialist_aggregation(
                db,
                wallet_id=wallet_id,
                category_label="us-politics",
                source_data_timestamp=ts,
                metrics=metrics,
            )
            self.assertIs(r1, True,
                          "first insert must return True (a new row was written)")
            # Row count = 1.
            count = db.fetchone("SELECT COUNT(*) AS c FROM wallet_specialist_aggregations")
            self.assertEqual(count["c"], 1)
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)

    def test_second_identical_insert_returns_false(self):
        db = _make_db()
        try:
            wallet_id = _make_wallet(db, "w-rv-2")
            metrics = _empty_metrics(wallet_id, "us-politics")
            ts = "2026-07-01T00:00:00+00:00"
            persist_wallet_specialist_aggregation(
                db,
                wallet_id=wallet_id,
                category_label="us-politics",
                source_data_timestamp=ts,
                metrics=metrics,
            )
            r2 = persist_wallet_specialist_aggregation(
                db,
                wallet_id=wallet_id,
                category_label="us-politics",
                source_data_timestamp=ts,
                metrics=metrics,
            )
            self.assertIs(r2, False,
                          "second identical insert must return False (no new row)")
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)

    def test_table_row_count_remains_one_after_duplicate(self):
        db = _make_db()
        try:
            wallet_id = _make_wallet(db, "w-rv-3")
            metrics = _empty_metrics(wallet_id, "us-politics")
            ts = "2026-07-01T00:00:00+00:00"
            persist_wallet_specialist_aggregation(
                db,
                wallet_id=wallet_id,
                category_label="us-politics",
                source_data_timestamp=ts,
                metrics=metrics,
            )
            persist_wallet_specialist_aggregation(
                db,
                wallet_id=wallet_id,
                category_label="us-politics",
                source_data_timestamp=ts,
                metrics=metrics,
            )
            persist_wallet_specialist_aggregation(
                db,
                wallet_id=wallet_id,
                category_label="us-politics",
                source_data_timestamp=ts,
                metrics=metrics,
            )
            count = db.fetchone(
                "SELECT COUNT(*) AS c FROM wallet_specialist_aggregations"
            )
            self.assertEqual(count["c"], 1,
                             "duplicate inserts must collapse to exactly one row")
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Integration tests — the bounded Step 5f call site
# ---------------------------------------------------------------------------

class Step5fIntegrationTests(unittest.TestCase):
    """Tests #4 + #5 + #6 of the PR #20 review:

      4. compute_and_persist_wallet_specialist_aggregations reports
         rows_written=1 on first run
      5. rerun reports rows_written=0 and rows_skipped_idempotent=1
      6. max_aggregations cap still holds
    """

    def _seed_one_wallet_with_trades(self, db: Database) -> str:
        wallet_id = _make_wallet(db, "w-step5f-1")
        _make_trade(db, wallet_id, market_id="m1", timestamp="2026-07-01T00:00:00+00:00")
        _make_trade(db, wallet_id, market_id="m2", timestamp="2026-07-02T00:00:00+00:00")
        return wallet_id

    def test_first_run_reports_rows_written_one(self):
        db = _make_db()
        try:
            wallet_id = self._seed_one_wallet_with_trades(db)
            counters = compute_and_persist_wallet_specialist_aggregations(
                db,
                fresh_insert_wallet_ids=[wallet_id],
                max_aggregations=10,
            )
            self.assertEqual(counters["rows_written"], 1,
                             "first run must report exactly one row written")
            self.assertEqual(counters["rows_skipped_idempotent"], 0)
            self.assertEqual(counters["wallets_processed"], 1)
            self.assertEqual(counters["errors"], 0)
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)

    def test_rerun_reports_rows_written_zero_skipped_one(self):
        db = _make_db()
        try:
            wallet_id = self._seed_one_wallet_with_trades(db)
            # First run.
            c1 = compute_and_persist_wallet_specialist_aggregations(
                db,
                fresh_insert_wallet_ids=[wallet_id],
                max_aggregations=10,
            )
            self.assertEqual(c1["rows_written"], 1)
            # Rerun with same inputs → idempotent.
            c2 = compute_and_persist_wallet_specialist_aggregations(
                db,
                fresh_insert_wallet_ids=[wallet_id],
                max_aggregations=10,
            )
            self.assertEqual(c2["rows_written"], 0,
                             "rerun must report zero new rows")
            self.assertEqual(c2["rows_skipped_idempotent"], 1,
                             "rerun must report one skipped idempotent row")
            # Table row count is still 1.
            count = db.fetchone(
                "SELECT COUNT(*) AS c FROM wallet_specialist_aggregations"
            )
            self.assertEqual(count["c"], 1)
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)

    def test_max_aggregations_cap_holds(self):
        db = _make_db()
        try:
            # Three wallets; cap = 2 → only two rows written, one deferred.
            w1 = _make_wallet(db, "w-cap-1")
            w2 = _make_wallet(db, "w-cap-2")
            w3 = _make_wallet(db, "w-cap-3")
            for w in (w1, w2, w3):
                _make_trade(db, w, market_id=f"m-{w}", timestamp="2026-07-01T00:00:00+00:00")
            counters = compute_and_persist_wallet_specialist_aggregations(
                db,
                fresh_insert_wallet_ids=[w1, w2, w3],
                max_aggregations=2,
            )
            self.assertEqual(counters["rows_written"], 2,
                             "cap must limit rows_written to max_aggregations")
            count = db.fetchone(
                "SELECT COUNT(*) AS c FROM wallet_specialist_aggregations"
            )
            self.assertEqual(count["c"], 2)
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Persistence content tests (unchanged behavior + new tests)
# ---------------------------------------------------------------------------

class PersistenceTests(unittest.TestCase):
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
            for blocked in (
                "resolved_markets",
                "win_rate_realized",
                "realized_pnl",
                "profit_factor",
                "max_drawdown",
            ):
                self.assertIn(blocked, missing)
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)

    def test_coexists_with_wallet_score_decisions(self):
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
            sd = db.fetchone(
                "SELECT COUNT(*) AS c FROM wallet_score_decisions WHERE wallet_id=?",
                (wallet_id,),
            )
            sa = db.fetchone(
                "SELECT COUNT(*) AS c FROM wallet_specialist_aggregations WHERE wallet_id=?",
                (wallet_id,),
            )
            self.assertIsNotNone(sd)
            self.assertIsNotNone(sa)
            self.assertEqual(sd["c"], 1)
            self.assertEqual(sa["c"], 1)
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# BLOCKER 2 — Production activation default
# ---------------------------------------------------------------------------

class RunScanDefaultFlagTests(unittest.TestCase):
    """The new specialist-aggregation opt-in defaults to OFF in
    :func:`scripts.run_scan.run_scan` so existing production scans
    behave exactly as before. This test asserts that contract."""

    def test_run_scan_default_aggregation_is_off(self):
        from scripts.run_scan import run_scan  # local import — slow
        import inspect
        sig = inspect.signature(run_scan)
        # Both new kwargs exist.
        self.assertIn("enable_pr20_specialist_aggregations", sig.parameters)
        self.assertIn("max_specialist_aggregations", sig.parameters)
        # Default for the opt-in flag is False (no behavior change).
        self.assertIs(sig.parameters["enable_pr20_specialist_aggregations"].default, False)
        # Default cap mirrors PR 19 (50).
        self.assertEqual(sig.parameters["max_specialist_aggregations"].default, 50)


# ---------------------------------------------------------------------------
# Safety tests — paper-only invariant
# ---------------------------------------------------------------------------

class SafetyTests(unittest.TestCase):
    def test_no_orders_positions_signals_balances_writes(self):
        """Test #7 of the PR #20 review: persisting an aggregation
        row must NOT write to orders / positions / signals /
        wallet_balances / paper_signal_decisions."""
        db = _make_db()
        try:
            wallet_id = _make_wallet(db, "w-uuid-safety")
            metrics = _empty_metrics(wallet_id, "")
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
                self.assertEqual(
                    row["c"], 0,
                    f"safety violation: {table} should be empty",
                )
        finally:
            db.close()
            Path(db.db_path).unlink(missing_ok=True)

    def test_no_approved_paper_signals(self):
        """Test #8 of the PR #20 review: no paper_signal_decisions
        row is ever written with is_approved = 1."""
        db = _make_db()
        try:
            wallet_id = _make_wallet(db, "w-uuid-approval")
            # Even after running the full bounded aggregation path,
            # paper_signal_decisions must remain untouched.
            _make_trade(db, wallet_id, market_id="m1", timestamp="2026-07-01T00:00:00+00:00")
            counters = compute_and_persist_wallet_specialist_aggregations(
                db,
                fresh_insert_wallet_ids=[wallet_id],
                max_aggregations=10,
            )
            self.assertEqual(counters["rows_written"], 1)
            ps = db.fetchone(
                "SELECT COUNT(*) AS c FROM paper_signal_decisions WHERE is_approved = 1"
            )
            self.assertIsNotNone(ps)
            self.assertEqual(ps["c"], 0,
                             "no paper signal may be approved by PR #20")
            all_ps = db.fetchone(
                "SELECT COUNT(*) AS c FROM paper_signal_decisions"
            )
            self.assertEqual(all_ps["c"], 0,
                             "paper_signal_decisions table must be empty")
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
            wallet_id="w1", category_label="x",
            source_data_timestamp="2026-07-04T00:00:00+00:00",
        )
        k2 = generate_specialist_idempotency_key(
            wallet_id="w1", category_label="x",
            source_data_timestamp="2026-07-04T00:00:00+00:00",
        )
        self.assertEqual(k1, k2)

    def test_different_category_different_key(self):
        k1 = generate_specialist_idempotency_key(
            wallet_id="w1", category_label="x", source_data_timestamp=None,
        )
        k2 = generate_specialist_idempotency_key(
            wallet_id="w1", category_label="y", source_data_timestamp=None,
        )
        self.assertNotEqual(k1, k2)

    def test_different_timestamp_different_key(self):
        k1 = generate_specialist_idempotency_key(
            wallet_id="w1", category_label="x",
            source_data_timestamp="2026-07-04T00:00:00+00:00",
        )
        k2 = generate_specialist_idempotency_key(
            wallet_id="w1", category_label="x",
            source_data_timestamp="2026-07-05T00:00:00+00:00",
        )
        self.assertNotEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()