"""Regression tests for the v5 migration child-before-parent deletion order.

Codex P2 finding (round 8): the pre-fix v5 migration attempted to delete
dependent rows in the wrong order. Concretely, a ``decision_log`` row whose
``order_id`` pointed to a sentinel-wallet's order was left in place when the
migration tried to delete the sentinel-wallet ``orders`` row, and SQLite
(``PRAGMA foreign_keys = ON``) raised ``FOREIGN KEY constraint failed``.

The fix:
  1. ``DELETE FROM decision_log WHERE order_id IN (sentinel-wallet orders)``
     — removes cross-references first, regardless of the decision log's own
     ``wallet_id``.
  2. ``DELETE FROM decision_log WHERE wallet_id IN (sentinel wallets)``
  3. ``DELETE FROM wallet_balances WHERE wallet_id IN (sentinel wallets)``
  4. ``DELETE FROM performance_summaries WHERE wallet_id IN (sentinel wallets)``
  5. ``DELETE FROM positions WHERE wallet_id IN (sentinel wallets)``
  6. ``DELETE FROM orders WHERE wallet_id IN (sentinel wallets)``
  7. ``DELETE FROM wallets WHERE <sentinel predicate>``
  8. ``PRAGMA foreign_key_check``

This file exercises the migration end-to-end against a real SQLite database
(``sqlite3.connect``), seeds realistic cross-references that reproduce the
exact pre-fix failure, and asserts the migration succeeds with FKs ON and
the post-migration DB has no orphan dependents.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest as _pytest  # noqa: F401  -- module marker for pytest discovery

# Ensure scripts/ is importable for run_scan / repository tests.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.db.schema import MIGRATIONS, SCHEMA_VERSION, _V5_DDL  # noqa: E402


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _init_db_at_version(db_path: Path, target: int) -> sqlite3.Connection:
    """Init a DB and run migrations 1..target with raw sqlite3 for boundary control."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    for v in range(1, target + 1):
        for stmt in MIGRATIONS[v]:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(v),),
        )
    conn.commit()
    return conn


def _seed_market(conn) -> str:
    market_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO markets (id, source_id, source, question, active, closed, "
        "resolved, volume_24h, fetched_at, is_sample) "
        "VALUES (?, 'mkt-p29', 'polymarket', 'P29?', 1, 0, 0, 1000.0, ?, 0)",
        (market_id, datetime.now(timezone.utc).isoformat()),
    )
    return market_id


def _insert_wallet(conn, address: str) -> str:
    wid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) "
        "VALUES (?, ?, 'p29', 0, ?)",
        (wid, address, datetime.now(timezone.utc).isoformat()),
    )
    return wid


def _insert_order(conn, market_id: str, wallet_id: str) -> str:
    oid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO orders (id, market_id, wallet_id, side, order_type, outcome, "
        "quantity, price, status, created_at, updated_at, is_sample) "
        "VALUES (?, ?, ?, 'buy', 'market', 'Yes', 1.0, 0.5, 'pending', ?, ?, 0)",
        (oid, market_id, wallet_id, now, now),
    )
    return oid


def _insert_decision_log(
    conn, wallet_id: str, market_id: str, order_id: str | None
) -> str:
    dlid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO decision_log (id, wallet_id, market_id, decision_type, "
        "signal_ids, order_id, rationale, metrics, created_at, is_sample) "
        "VALUES (?, ?, ?, 'follow', '[]', ?, 'r', '{}', ?, 0)",
        (dlid, wallet_id, market_id, order_id, datetime.now(timezone.utc).isoformat()),
    )
    return dlid


def _apply_v5(conn) -> None:
    for stmt in _V5_DDL:
        conn.execute(stmt)
    conn.execute(
        "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _rowcount(conn, table: str, where: str = "1=1", params: tuple = ()) -> int:
    return conn.execute(
        f"SELECT COUNT(*) AS c FROM {table} WHERE {where}", params
    ).fetchone()["c"]


# ─── 1. End-to-end cross-reference migration succeeds ─────────────────────────


class TestMigrationCrossReferenceOrdering:
    """The v5 migration must remove cross-referencing decision_log rows BEFORE
    deleting the orders they reference, even when the decision_log's own
    ``wallet_id`` belongs to a real (non-sentinel) wallet."""

    def test_sentinel_wallet_with_order_and_decision_log_order_id_migrates(
        self, tmp_path: Path
    ):
        """Sentinel wallet with an order referenced by a decision_log row
        (owned by a real wallet) must migrate successfully. Pre-fix this
        raised ``FOREIGN KEY constraint failed``."""
        db_path = tmp_path / "p29-cross-ref.db"
        conn = _init_db_at_version(db_path, 4)

        market_id = _seed_market(conn)

        sentinel_wallet = _insert_wallet(conn, "unknown")
        sentinel_order = _insert_order(conn, market_id, sentinel_wallet)

        real_wallet = _insert_wallet(conn, "0xREAL_KEEP")
        # Decision log whose wallet is real but whose order_id points at
        # the sentinel's order — this is the exact pre-fix failure shape.
        _insert_decision_log(conn, real_wallet, market_id, sentinel_order)
        conn.commit()

        # Apply v5 with FKs ON to prove the deletion order is correct.
        conn.execute("PRAGMA foreign_keys = ON")
        _apply_v5(conn)

        # Cross-reference decision_log row is gone.
        assert _rowcount(conn, "decision_log") == 0
        # Sentinel wallet and its order are gone.
        assert _rowcount(conn, "wallets", "address = 'unknown'") == 0
        assert _rowcount(conn, "orders") == 0
        # Real wallet survives.
        assert _rowcount(conn, "wallets", "address = '0xREAL_KEEP'") == 1
        # PRAGMA foreign_key_check is clean.
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == [], f"FK violations: {fk_violations}"

    def test_referencing_decision_log_removed_before_orders_deletion_can_block(
        self, tmp_path: Path
    ):
        """The migration's step-1 cross-reference DELETE must remove ALL
        decision_log rows whose order_id targets a sentinel-wallet order,
        even if their own ``wallet_id`` is a real wallet. Otherwise the
        orders DELETE in step 6 would fail with FOREIGN KEY constraint
        failed when ``PRAGMA foreign_keys = ON`` is in effect."""
        db_path = tmp_path / "p29-step1.db"
        conn = _init_db_at_version(db_path, 4)
        market_id = _seed_market(conn)

        sentinel_wallet = _insert_wallet(conn, "anonymous")
        sentinel_order = _insert_order(conn, market_id, sentinel_wallet)

        # Two decision_log rows: one owned by the real wallet, one owned
        # by ANOTHER sentinel (cross-references and same-wallet variants).
        real_wallet = _insert_wallet(conn, "0xALSO_REAL")
        _insert_decision_log(conn, real_wallet, market_id, sentinel_order)
        _insert_decision_log(conn, sentinel_wallet, market_id, sentinel_order)
        conn.commit()

        # Snapshot pre-migration state.
        assert _rowcount(conn, "decision_log") == 2
        assert _rowcount(conn, "orders") == 1

        conn.execute("PRAGMA foreign_keys = ON")
        _apply_v5(conn)

        # Both decision_log rows are gone (cross-ref + by-wallet).
        assert _rowcount(conn, "decision_log") == 0
        # Sentinel order is gone.
        assert _rowcount(conn, "orders") == 0
        # Real wallet survives.
        assert _rowcount(conn, "wallets", "address = '0xALSO_REAL'") == 1
        # No FK violations.
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == [], f"FK violations: {fk_violations}"

    def test_migration_succeeds_with_foreign_keys_enforced(self, tmp_path: Path):
        """The migration must succeed with PRAGMA foreign_keys = ON the
        whole way through. ``PRAGMA foreign_keys = OFF`` is a defense-in-
        depth safety net, not the correctness mechanism."""
        db_path = tmp_path / "p29-fks-on.db"
        conn = _init_db_at_version(db_path, 4)
        market_id = _seed_market(conn)

        # Heavy cross-references and dependents so any wrong order trips FKs.
        sentinel_wallet = _insert_wallet(conn, "missing")
        sentinel_order = _insert_order(conn, market_id, sentinel_wallet)
        real_wallet = _insert_wallet(conn, "0xREAL")
        for _ in range(3):
            _insert_decision_log(conn, real_wallet, market_id, sentinel_order)
        _insert_decision_log(conn, sentinel_wallet, market_id, sentinel_order)
        conn.execute(
            "INSERT INTO positions (id, market_id, wallet_id, outcome, quantity, "
            "avg_entry_price, current_price, opened_at, updated_at, is_sample) "
            "VALUES (?, ?, ?, 'Yes', 1.0, 0.5, 0.6, ?, ?, 0)",
            (
                str(uuid.uuid4()),
                market_id,
                sentinel_wallet,
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

        # Pre-migration FK check is itself clean.
        conn.execute("PRAGMA foreign_keys = ON")
        pre = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert pre == [], f"unexpected pre-migration FK violations: {pre}"

        # Apply v5 with FKs ON.
        _apply_v5(conn)

        # Migration succeeded; no FK violations; sentinels gone; reals kept.
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == [], f"FK violations: {fk_violations}"
        assert _rowcount(conn, "wallets", "address = 'missing'") == 0
        assert _rowcount(conn, "orders") == 0
        assert _rowcount(conn, "decision_log") == 0
        assert _rowcount(conn, "wallets", "address = '0xREAL'") == 1


# ─── 2. Post-migration integrity ─────────────────────────────────────────────


class TestPostMigrationIntegrity:
    def test_pragma_foreign_key_check_is_empty_after_migration(self, tmp_path: Path):
        """PRAGMA foreign_key_check must return no rows after the migration
        has run, even with a heavy dependent cross-reference graph."""
        db_path = tmp_path / "p29-fkcheck.db"
        conn = _init_db_at_version(db_path, 4)
        market_id = _seed_market(conn)

        sentinel_wallet = _insert_wallet(conn, "0x0")
        sentinel_order = _insert_order(conn, market_id, sentinel_wallet)
        real_wallet = _insert_wallet(conn, "0xREAL_PRIMARY")
        _insert_decision_log(conn, real_wallet, market_id, sentinel_order)

        # Add a second sentinel with multiple dependents to stress the
        # child-before-parent order.
        sentinel_wallet_2 = _insert_wallet(conn, "unknown")
        sentinel_order_2 = _insert_order(conn, market_id, sentinel_wallet_2)
        _insert_decision_log(conn, real_wallet, market_id, sentinel_order_2)
        _insert_decision_log(conn, sentinel_wallet_2, market_id, sentinel_order_2)

        conn.execute(
            "INSERT INTO wallet_balances (wallet_id, currency, amount, as_of, is_sample) "
            "VALUES (?, 'USDC', 100.0, ?, 0)",
            (sentinel_wallet_2, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "INSERT INTO performance_summaries (wallet_id, strategy_label, start_date, "
            "end_date, total_pnl, realized_pnl, unrealized_pnl, win_rate, max_drawdown, "
            "trade_count, is_sample) "
            "VALUES (?, 'default', '2024-01-01', '2024-12-31', 0.0, 0.0, 0.0, 0.5, 0.0, 0, 0)",
            (sentinel_wallet_2,),
        )
        conn.commit()

        conn.execute("PRAGMA foreign_keys = ON")
        _apply_v5(conn)

        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == [], f"FK violations: {fk_violations}"
        # All sentinel dependents gone.
        assert _rowcount(conn, "wallet_balances") == 0
        assert _rowcount(conn, "performance_summaries") == 0
        # Real wallet remains.
        assert _rowcount(conn, "wallets", "address = '0xREAL_PRIMARY'") == 1

    def test_real_wallet_order_and_decision_log_survive(self, tmp_path: Path):
        """Real-wallet orders and decision_log rows must be preserved
        byte-for-byte. Only sentinel rows are removed."""
        db_path = tmp_path / "p29-real-survive.db"
        conn = _init_db_at_version(db_path, 4)
        market_id = _seed_market(conn)

        real_wallet = _insert_wallet(conn, "0xREAL_PRIMARY")
        real_order = _insert_order(conn, market_id, real_wallet)
        real_dl = _insert_decision_log(conn, real_wallet, market_id, real_order)
        conn.commit()

        conn.execute("PRAGMA foreign_keys = ON")
        _apply_v5(conn)

        # Real wallet row, its order, and its decision_log are untouched.
        assert _rowcount(conn, "wallets", "address = '0xREAL_PRIMARY'") == 1
        orders = conn.execute("SELECT id FROM orders").fetchall()
        assert [o["id"] for o in orders] == [real_order]
        dls = conn.execute("SELECT id FROM decision_log").fetchall()
        assert [d["id"] for d in dls] == [real_dl]
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == [], f"FK violations: {fk_violations}"

    def test_mixed_sentinel_and_real_data_migrates(self, tmp_path: Path):
        """A DB with both sentinel and real wallets + dependents must
        clean up ONLY the sentinel rows."""
        db_path = tmp_path / "p29-mixed.db"
        conn = _init_db_at_version(db_path, 4)
        market_id = _seed_market(conn)

        sentinel_wallet = _insert_wallet(conn, "unknown")
        sentinel_order = _insert_order(conn, market_id, sentinel_wallet)
        _insert_decision_log(conn, sentinel_wallet, market_id, sentinel_order)

        real_a = _insert_wallet(conn, "0xREAL_A")
        real_order_a = _insert_order(conn, market_id, real_a)
        real_dl_a = _insert_decision_log(conn, real_a, market_id, real_order_a)

        real_b = _insert_wallet(conn, "0xReal_B_Mixed")
        real_order_b = _insert_order(conn, market_id, real_b)
        real_dl_b = _insert_decision_log(conn, real_b, market_id, real_order_b)
        conn.commit()

        conn.execute("PRAGMA foreign_keys = ON")
        _apply_v5(conn)

        # Sentinel wallet + its dependents gone.
        assert _rowcount(conn, "wallets", "address = 'unknown'") == 0
        # Real wallets + their orders + decision_log rows preserved.
        real_survivors = sorted(
            r["address"] for r in conn.execute("SELECT wallets.address FROM wallets WHERE wallets.address LIKE '0xREAL%'").fetchall()
        )
        assert real_survivors == ["0xREAL_A", "0xReal_B_Mixed"], real_survivors
        surviving_order_ids = sorted(
            r["id"] for r in conn.execute("SELECT id FROM orders").fetchall()
        )
        assert surviving_order_ids == sorted([real_order_a, real_order_b])
        surviving_dl_ids = sorted(
            r["id"] for r in conn.execute("SELECT id FROM decision_log").fetchall()
        )
        assert surviving_dl_ids == sorted([real_dl_a, real_dl_b])
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == [], f"FK violations: {fk_violations}"


# ─── 3. Migration idempotency ────────────────────────────────────────────────


class TestMigrationIdempotency:
    def test_rerunning_v5_is_a_no_op(self, tmp_path: Path):
        """Re-applying v5 to an already-migrated DB must be a no-op
        (zero sentinel rows, zero FK violations, no errors)."""
        db_path = tmp_path / "p29-idem.db"
        conn = _init_db_at_version(db_path, 4)
        market_id = _seed_market(conn)

        sentinel_wallet = _insert_wallet(conn, "unknown")
        sentinel_order = _insert_order(conn, market_id, sentinel_wallet)
        real_wallet = _insert_wallet(conn, "0xREAL_KEEP")
        _insert_decision_log(conn, real_wallet, market_id, sentinel_order)
        conn.commit()

        conn.execute("PRAGMA foreign_keys = ON")
        _apply_v5(conn)

        after_first = {
            "wallets": sorted(
                r["address"] for r in conn.execute("SELECT wallets.address FROM wallets WHERE wallets.address LIKE '0xREAL%'").fetchall()
            ),
            "orders": _rowcount(conn, "orders"),
            "decision_log": _rowcount(conn, "decision_log"),
        }
        # Re-apply v5 (must be safe).
        for stmt in _V5_DDL:
            conn.execute(stmt)
        conn.commit()

        after_second = {
            "wallets": sorted(
                r["address"] for r in conn.execute("SELECT wallets.address FROM wallets WHERE wallets.address LIKE '0xREAL%'").fetchall()
            ),
            "orders": _rowcount(conn, "orders"),
            "decision_log": _rowcount(conn, "decision_log"),
        }
        assert after_first == after_second, (
            f"idempotency violated: {after_first} -> {after_second}"
        )
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == [], f"FK violations: {fk_violations}"

    def test_reopening_migrated_db_is_idempotent(self, tmp_path: Path):
        """Re-opening an already-migrated DB through Database.connect()
        must not re-run v5 DDL (already at SCHEMA_VERSION)."""
        db_path = tmp_path / "p29-reopen.db"
        conn = _init_db_at_version(db_path, 4)
        market_id = _seed_market(conn)
        sentinel_wallet = _insert_wallet(conn, "unknown")
        sentinel_order = _insert_order(conn, market_id, sentinel_wallet)
        real_wallet = _insert_wallet(conn, "0xREAL_KEEP")
        _insert_decision_log(conn, real_wallet, market_id, sentinel_order)
        conn.commit()

        # Apply v5 manually with FKs ON.
        conn.execute("PRAGMA foreign_keys = ON")
        _apply_v5(conn)
        assert _rowcount(conn, "wallets", "address = 'unknown'") == 0
        assert _rowcount(conn, "wallets", "address = '0xREAL_KEEP'") == 1

        # Close the connection and reopen via Database helper — should
        # detect version 5 and skip the migration block entirely.
        conn.close()

        from polycopy.db.database import Database

        db = Database(db_path=db_path).connect()
        try:
            # Schema still at v5.
            version_row = db.fetchone("SELECT value FROM _meta WHERE key = 'schema_version'")
            assert version_row is not None, "_meta row missing after reopen"
            assert version_row["value"] == str(SCHEMA_VERSION)
            # Sentinel gone, real wallet present.
            wallets = sorted(
                r["address"] for r in db.fetchall("SELECT wallets.address FROM wallets WHERE wallets.address LIKE '0xREAL%'")
            )
            assert wallets == ["0xREAL_KEEP"], wallets
            # FK check clean.
            fk_violations = db.conn.execute("PRAGMA foreign_key_check").fetchall()
            assert fk_violations == [], f"FK violations: {fk_violations}"
        finally:
            db.close()


# ─── 4. Defensive validation of the _V5_DDL shape ─────────────────────────────


class TestV5DDLStructure:
    """Sanity-check the SQL emitted by the v5 migration matches the spec."""

    def test_every_v5_entry_is_plain_string(self):
        for i, stmt in enumerate(_V5_DDL):
            assert isinstance(stmt, str), (
                f"entry {i} is not a string: {type(stmt).__name__}"
            )

    def test_orders_delete_has_balanced_parens(self):
        orders_del = [s for s in _V5_DDL if s.lstrip().startswith("DELETE FROM orders")]
        assert len(orders_del) == 1, f"expected 1 orders DELETE, got {len(orders_del)}"
        stmt = orders_del[0]
        assert stmt.count("(") == stmt.count(")"), (
            f"unbalanced parens: {stmt!r}"
        )

    def test_cross_reference_decision_log_delete_exists(self):
        cross_ref = [
            s for s in _V5_DDL
            if s.lstrip().startswith("DELETE FROM decision_log")
            and "order_id IN" in s
        ]
        assert len(cross_ref) == 1, (
            f"expected 1 cross-reference decision_log DELETE, got {len(cross_ref)}"
        )

    def test_deletion_order_matches_spec(self):
        """Confirm the order of destructive operations matches the spec:
          1. decision_log (cross-ref)
          2. decision_log (by wallet)
          3. wallet_balances
          4. performance_summaries
          5. positions
          6. orders
          7. wallets
        """
        del_targets: list[str] = []
        for stmt in _V5_DDL:
            s = stmt.lstrip()
            if s.startswith("DELETE FROM "):
                del_targets.append(s.split()[2].rstrip(";"))
        assert del_targets == [
            "decision_log",
            "decision_log",
            "wallet_balances",
            "performance_summaries",
            "positions",
            "orders",
            "wallets",
        ], f"unexpected deletion order: {del_targets}"

    def test_no_literal_backslash_sentinel_in_trim_args(self):
        """SQLite does NOT interpret backslash escapes in string literals
        (e.g. ``'\\t'`` is two characters, not a tab). Real fixes use
        either ``X'09'`` hex literals, ``char()`` calls, or ``trim()``
        with no second arg. The migration must use the hex-literal form."""
        for i, stmt in enumerate(_V5_DDL):
            # Disallow patterns like "' \\t" (literal backslash-t inside
            # a string). The hex form is X'09' etc.
            assert "\\t" not in stmt, f"entry {i} contains literal backslash-t: {stmt!r}"
            assert "\\n" not in stmt, f"entry {i} contains literal backslash-n: {stmt!r}"
            assert "\\r" not in stmt, f"entry {i} contains literal backslash-r: {stmt!r}"
            assert "\\v" not in stmt, f"entry {i} contains literal backslash-v: {stmt!r}"
            assert "\\f" not in stmt, f"entry {i} contains literal backslash-f: {stmt!r}"
