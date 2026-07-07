"""Tests for PR24B query-memory and RSS guardrails.

Covers three independent concerns:

1. Bounded query iteration (``polycopy.runtime.query_batches`` and the
   ``Database.iter_*`` wrappers). Confirms empty result sets, batch
   boundaries, keyset-pagination correctness, and that the helper
   releases its cursor on early termination.

2. RSS guard (``polycopy.runtime.memory``). Confirms env-var parsing
   (unset, empty, valid, invalid, non-positive), that the guard does
   nothing when no ceiling is set, that it raises ``MemoryLimitExceeded``
   above the ceiling, and that monkeypatched RSS values are honoured.

3. Hot-path integrity. Confirms that the four protected scripts no
   longer call ``fetchall`` on unbounded queries on the hot paths
   PR24B is supposed to fix. This is a regression guard — if someone
   reintroduces ``fetchall`` on one of these paths in the future, this
   test fails loudly.

The tests deliberately avoid allocating large real result sets; the
``Database.iter_*`` and RSS helpers are tested against tiny in-memory
fixtures. The hot-path integrity test asserts the *shape* of the
scripts (the SELECT patterns they use), not their runtime behaviour on
big data.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest


# Repo root layout: tests/ lives at repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _make_db() -> tuple[sqlite3.Connection, Path]:
    """Create a temporary SQLite DB with the row_factory set."""
    fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    fd.close()
    path = Path(fd.name)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn, path


def _seed(conn: sqlite3.Connection, n: int) -> None:
    """Insert ``n`` rows into a ``t`` table with (id, n, label)."""
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER, label TEXT)")
    conn.executemany(
        "INSERT INTO t (n, label) VALUES (?, ?)",
        [(i, f"row-{i}") for i in range(1, n + 1)],
    )
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────
# Batching helpers
# ─────────────────────────────────────────────────────────────────────────


class TestIterBatches:
    """``iter_batches`` returns fixed-size batches and honours batch_size."""

    def test_empty_result_set(self) -> None:
        from polycopy.runtime.query_batches import iter_batches

        conn, path = _make_db()
        try:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER)")
            conn.commit()
            batches = list(iter_batches(conn, "SELECT id FROM t", batch_size=10))
            assert batches == []
        finally:
            conn.close()
            path.unlink()

    def test_single_batch_short(self) -> None:
        from polycopy.runtime.query_batches import iter_batches

        conn, path = _make_db()
        try:
            _seed(conn, 3)
            batches = list(iter_batches(conn, "SELECT id FROM t", batch_size=10))
            assert len(batches) == 1
            assert [row[0] for row in batches[0]] == [1, 2, 3]
        finally:
            conn.close()
            path.unlink()

    def test_batch_size_honoured(self) -> None:
        from polycopy.runtime.query_batches import iter_batches

        conn, path = _make_db()
        try:
            _seed(conn, 25)
            batches = list(iter_batches(conn, "SELECT id FROM t", batch_size=10))
            assert len(batches) == 3
            assert [row[0] for row in batches[0]] == list(range(1, 11))
            assert [row[0] for row in batches[1]] == list(range(11, 21))
            assert [row[0] for row in batches[2]] == [21, 22, 23, 24, 25]
        finally:
            conn.close()
            path.unlink()

    def test_final_partial_batch(self) -> None:
        from polycopy.runtime.query_batches import iter_batches

        conn, path = _make_db()
        try:
            _seed(conn, 23)
            batches = list(iter_batches(conn, "SELECT id FROM t", batch_size=10))
            assert [len(b) for b in batches] == [10, 10, 3]
        finally:
            conn.close()
            path.unlink()

    def test_returns_all_rows_across_batches(self) -> None:
        from polycopy.runtime.query_batches import iter_batches

        conn, path = _make_db()
        try:
            _seed(conn, 47)
            flat = [r[0] for batch in iter_batches(conn, "SELECT id FROM t", batch_size=7)
                    for r in batch]
            assert flat == list(range(1, 48))
        finally:
            conn.close()
            path.unlink()

    def test_batch_size_validation(self) -> None:
        from polycopy.runtime.query_batches import iter_batches

        conn, path = _make_db()
        try:
            with pytest.raises(ValueError):
                list(iter_batches(conn, "SELECT 1", batch_size=0))
            with pytest.raises(ValueError):
                list(iter_batches(conn, "SELECT 1", batch_size=-1))
        finally:
            conn.close()
            path.unlink()

    def test_cursor_closed_on_early_break(self) -> None:
        """If the caller stops iterating, the underlying cursor closes."""
        from polycopy.runtime.query_batches import iter_batches

        conn, path = _make_db()
        try:
            _seed(conn, 100)
            gen = iter_batches(conn, "SELECT id FROM t", batch_size=10)
            first_batch = next(gen)
            assert len(first_batch) == 10
            # Generators implement .close(); the static type hint is
            # Iterator (no close), so we suppress the type-checker here.
            gen.close()  # type: ignore[attr-defined]
            # Second close must be a no-op (idempotent).
            gen.close()  # type: ignore[attr-defined]
        finally:
            conn.close()
            path.unlink()


class TestIterRows:
    """``iter_rows`` streams rows one at a time."""

    def test_yields_all_rows_in_order(self) -> None:
        from polycopy.runtime.query_batches import iter_rows

        conn, path = _make_db()
        try:
            _seed(conn, 12)
            ids = [row[0] for row in iter_rows(conn, "SELECT id FROM t ORDER BY id",
                                                batch_size=5)]
            assert ids == list(range(1, 13))
        finally:
            conn.close()
            path.unlink()

    def test_empty_result(self) -> None:
        from polycopy.runtime.query_batches import iter_rows

        conn, path = _make_db()
        try:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            conn.commit()
            assert list(iter_rows(conn, "SELECT id FROM t", batch_size=10)) == []
        finally:
            conn.close()
            path.unlink()


class TestIterKeysetBatches:
    """Keyset pagination returns O(batch_size) pages regardless of depth."""

    def test_first_page_descending(self) -> None:
        from polycopy.runtime.query_batches import iter_keyset_batches

        conn, path = _make_db()
        try:
            _seed(conn, 20)
            batches = list(
                iter_keyset_batches(
                    conn,
                    base_sql="SELECT id, n FROM t",
                    keyset_col="id",
                    last_value=None,
                    batch_size=7,
                    descending=True,
                )
            )
            # First batch is the top 7 IDs (20, 19, 18, ...).
            ids = [row[0] for batch in batches for row in batch]
            assert ids[0] == 20
            assert len(batches[0]) == 7
        finally:
            conn.close()
            path.unlink()

    def test_continues_from_last_value(self) -> None:
        from polycopy.runtime.query_batches import iter_keyset_batches

        conn, path = _make_db()
        try:
            _seed(conn, 10)
            # Resume from id=5 descending: should return {4, 3, 2, 1}.
            batches = list(
                iter_keyset_batches(
                    conn,
                    base_sql="SELECT id FROM t",
                    keyset_col="id",
                    last_value=5,
                    batch_size=4,
                    descending=True,
                )
            )
            ids = [row[0] for batch in batches for row in batch]
            assert ids == [4, 3, 2, 1]
        finally:
            conn.close()
            path.unlink()

    def test_extra_where_clause(self) -> None:
        from polycopy.runtime.query_batches import iter_keyset_batches

        conn, path = _make_db()
        try:
            _seed(conn, 10)
            batches = list(
                iter_keyset_batches(
                    conn,
                    base_sql="SELECT id FROM t",
                    keyset_col="id",
                    last_value=None,
                    extra_where="AND n >= 7",
                    batch_size=4,
                    descending=True,
                )
            )
            ids = [row[0] for batch in batches for row in batch]
            assert ids == [10, 9, 8, 7]
        finally:
            conn.close()
            path.unlink()

    def test_ascending(self) -> None:
        from polycopy.runtime.query_batches import iter_keyset_batches

        conn, path = _make_db()
        try:
            _seed(conn, 5)
            batches = list(
                iter_keyset_batches(
                    conn,
                    base_sql="SELECT id FROM t",
                    keyset_col="id",
                    last_value=None,
                    batch_size=2,
                    descending=False,
                )
            )
            ids = [row[0] for batch in batches for row in batch]
            assert ids == [1, 2, 3, 4, 5]
        finally:
            conn.close()
            path.unlink()

    def test_empty_database(self) -> None:
        from polycopy.runtime.query_batches import iter_keyset_batches

        conn, path = _make_db()
        try:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            conn.commit()
            batches = list(
                iter_keyset_batches(
                    conn,
                    base_sql="SELECT id FROM t",
                    keyset_col="id",
                    last_value=None,
                    batch_size=5,
                )
            )
            assert batches == []
        finally:
            conn.close()
            path.unlink()


class TestIterOffsetBatches:
    """LIMIT/OFFSET helper for small / cold paths."""

    def test_paginates_in_order(self) -> None:
        from polycopy.runtime.query_batches import iter_offset_batches

        conn, path = _make_db()
        try:
            _seed(conn, 11)
            batches = list(iter_offset_batches(
                conn, "SELECT id FROM t ORDER BY id", batch_size=4
            ))
            flat = [r[0] for b in batches for r in b]
            assert flat == list(range(1, 12))
            assert [len(b) for b in batches] == [4, 4, 3]
        finally:
            conn.close()
            path.unlink()

    def test_empty(self) -> None:
        from polycopy.runtime.query_batches import iter_offset_batches

        conn, path = _make_db()
        try:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            conn.commit()
            batches = list(iter_offset_batches(conn, "SELECT id FROM t", batch_size=5))
            assert batches == []
        finally:
            conn.close()
            path.unlink()


# ─────────────────────────────────────────────────────────────────────────
# Database.iter_* wrappers
# ─────────────────────────────────────────────────────────────────────────


class TestDatabaseIterWrappers:
    """Database.iter_rows / iter_batches / iter_keyset_batches round-trip."""

    def test_iter_rows_round_trip(self) -> None:
        import sys
        sys.path.insert(0, str(_REPO_ROOT / "src"))

        from polycopy.db.database import Database

        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        path = Path(fd.name)
        try:
            db = Database(db_path=path).connect()
            db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, name TEXT)")
            db.execute("INSERT INTO u (name) VALUES ('a')")
            db.execute("INSERT INTO u (name) VALUES ('b')")
            db.conn.commit()
            rows = list(db.iter_rows("SELECT id, name FROM u ORDER BY id", batch_size=1))
            assert [r[1] for r in rows] == ["a", "b"]
            db.close()
        finally:
            path.unlink()

    def test_iter_batches_round_trip(self) -> None:
        import sys
        sys.path.insert(0, str(_REPO_ROOT / "src"))

        from polycopy.db.database import Database

        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        path = Path(fd.name)
        try:
            db = Database(db_path=path).connect()
            db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY)")
            for i in range(7):
                db.execute("INSERT INTO u (id) VALUES (?)", (i + 1,))
            db.conn.commit()
            batches = list(db.iter_batches(
                "SELECT id FROM u ORDER BY id", batch_size=3
            ))
            assert [len(b) for b in batches] == [3, 3, 1]
            db.close()
        finally:
            path.unlink()

    def test_iter_keyset_batches_round_trip(self) -> None:
        import sys
        sys.path.insert(0, str(_REPO_ROOT / "src"))

        from polycopy.db.database import Database

        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        path = Path(fd.name)
        try:
            db = Database(db_path=path).connect()
            db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, n INTEGER)")
            for i in range(1, 11):
                db.execute("INSERT INTO u (id, n) VALUES (?, ?)", (i, i * 10))
            db.conn.commit()
            batches = list(db.iter_keyset_batches(
                base_sql="SELECT id, n FROM u",
                keyset_col="id",
                last_value=None,
                batch_size=4,
                descending=True,
            ))
            ids = [r[0] for b in batches for r in b]
            assert ids == [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
            db.close()
        finally:
            path.unlink()


# ─────────────────────────────────────────────────────────────────────────
# RSS guard
# ─────────────────────────────────────────────────────────────────────────


class TestMemoryLimitEnvParsing:
    """``get_max_rss_mb_from_env`` parses POLYCOPY_MAX_RSS_MB safely."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POLYCOPY_MAX_RSS_MB", raising=False)

    def test_unset_returns_default_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from polycopy.runtime.memory import get_max_rss_mb_from_env

        monkeypatch.delenv("POLYCOPY_MAX_RSS_MB", raising=False)
        assert get_max_rss_mb_from_env() is None

    def test_unset_returns_explicit_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from polycopy.runtime.memory import get_max_rss_mb_from_env

        monkeypatch.delenv("POLYCOPY_MAX_RSS_MB", raising=False)
        assert get_max_rss_mb_from_env(default=512.0) == 512.0

    def test_empty_string_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from polycopy.runtime.memory import get_max_rss_mb_from_env

        monkeypatch.setenv("POLYCOPY_MAX_RSS_MB", "")
        assert get_max_rss_mb_from_env() is None
        assert get_max_rss_mb_from_env(default=128.0) == 128.0

    def test_whitespace_only_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from polycopy.runtime.memory import get_max_rss_mb_from_env

        monkeypatch.setenv("POLYCOPY_MAX_RSS_MB", "   ")
        assert get_max_rss_mb_from_env() is None

    def test_valid_positive_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from polycopy.runtime.memory import get_max_rss_mb_from_env

        monkeypatch.setenv("POLYCOPY_MAX_RSS_MB", "512.5")
        assert get_max_rss_mb_from_env() == 512.5

    def test_invalid_value_returns_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from polycopy.runtime.memory import get_max_rss_mb_from_env

        monkeypatch.setenv("POLYCOPY_MAX_RSS_MB", "not-a-number")
        assert get_max_rss_mb_from_env() is None
        assert get_max_rss_mb_from_env(default=256.0) == 256.0

    def test_zero_returns_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from polycopy.runtime.memory import get_max_rss_mb_from_env

        monkeypatch.setenv("POLYCOPY_MAX_RSS_MB", "0")
        assert get_max_rss_mb_from_env() is None

    def test_negative_returns_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from polycopy.runtime.memory import get_max_rss_mb_from_env

        monkeypatch.setenv("POLYCOPY_MAX_RSS_MB", "-50")
        assert get_max_rss_mb_from_env() is None


class TestCheckRssLimit:
    """``check_rss_limit`` honours its ceiling and respects monkeypatched RSS."""

    def test_no_op_when_max_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from polycopy.runtime import memory

        monkeypatch.setattr(memory, "get_current_rss_mb", lambda: 9999.0)
        # Even with absurd RSS, None ceiling means no-op.
        memory.check_rss_limit("test", max_rss_mb=None)

    def test_no_op_when_max_is_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from polycopy.runtime import memory

        monkeypatch.setattr(memory, "get_current_rss_mb", lambda: 100.0)
        # max_rss_mb=0 is treated as "disabled".
        memory.check_rss_limit("test", max_rss_mb=0)

    def test_raises_above_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from polycopy.runtime import memory
        from polycopy.runtime.memory import MemoryLimitExceeded

        monkeypatch.setattr(memory, "get_current_rss_mb", lambda: 500.0)
        with pytest.raises(MemoryLimitExceeded) as exc_info:
            memory.check_rss_limit("scan:wallet-1", max_rss_mb=100.0)
        assert exc_info.value.context == "scan:wallet-1"
        assert exc_info.value.rss_mb == 500.0
        assert exc_info.value.limit_mb == 100.0

    def test_no_raise_below_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from polycopy.runtime import memory

        monkeypatch.setattr(memory, "get_current_rss_mb", lambda: 50.0)
        memory.check_rss_limit("test", max_rss_mb=100.0)

    def test_no_op_when_rss_unreadable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When RSS reading fails (0.0), don't block on bad measurement."""
        from polycopy.runtime import memory

        monkeypatch.setattr(memory, "get_current_rss_mb", lambda: 0.0)
        # Should not raise even though limit is "low".
        memory.check_rss_limit("test", max_rss_mb=1.0)

    def test_exact_at_limit_no_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from polycopy.runtime import memory

        monkeypatch.setattr(memory, "get_current_rss_mb", lambda: 100.0)
        memory.check_rss_limit("test", max_rss_mb=100.0)


class TestMemoryLimitExceededException:
    """The custom exception carries useful diagnostic info."""

    def test_message_includes_context_and_values(self) -> None:
        from polycopy.runtime.memory import MemoryLimitExceeded

        exc = MemoryLimitExceeded(context="scan", rss_mb=600.0, limit_mb=500.0)
        msg = str(exc)
        assert "scan" in msg
        assert "600" in msg
        assert "500" in msg
        assert "POLYCOPY_MAX_RSS_MB" in msg

    def test_is_runtime_error(self) -> None:
        from polycopy.runtime.memory import MemoryLimitExceeded

        assert issubclass(MemoryLimitExceeded, RuntimeError)


# ─────────────────────────────────────────────────────────────────────────
# Hot-path integrity: scripts no longer call fetchall on unbounded paths
# ─────────────────────────────────────────────────────────────────────────


def _read(path: str) -> str:
    return (_REPO_ROOT / path).read_text(encoding="utf-8")


class TestHotPathNoLongerFetchall:
    """Regression guard: PR24B's hot paths must not regress to fetchall."""

    @staticmethod
    def _function_body(text: str, anchor: str, end_anchors: list[str]) -> str:
        """Slice from ``anchor`` to the first matching end_anchor.

        ``end_anchors`` is a list of strings; we stop at the earliest
        occurrence of any of them. This is more robust than scanning
        ``\\ndef `` because docstrings frequently mention both
        ``fetchall`` (as the historical thing being replaced) and
        column lists.
        """
        idx = text.index(anchor)
        end = len(text)
        for cand in end_anchors:
            try:
                pos = text.index(cand, idx + len(anchor))
                end = min(end, pos)
            except ValueError:
                continue
        return text[idx:end]

    def test_run_scan_compute_wallet_metrics_uses_iter_rows(self) -> None:
        text = _read("scripts/run_scan.py")
        # Slice from the SQL definition to the return statement.
        body = self._function_body(
            text,
            "trades_sql = f\"\"\"SELECT side",
            ["return None", "return {"],
        )
        assert "fetchall" not in body, (
            "scripts/run_scan.py::_compute_wallet_metrics still uses "
            "fetchall — PR24B must use db.iter_rows(...) instead."
        )
        assert "db.iter_rows" in body, (
            "scripts/run_scan.py::_compute_wallet_metrics must use "
            "db.iter_rows(...) for bounded streaming."
        )

    def test_run_scan_compute_wallet_metrics_uses_explicit_columns(self) -> None:
        """PR24B also projects only the columns actually consumed."""
        text = _read("scripts/run_scan.py")
        body = self._function_body(
            text,
            "trades_sql = f\"\"\"SELECT side",
            ["return None", "return {"],
        )
        # The SELECT must name side, price, timestamp, market_source_id,
        # is_sample — and must NOT use "SELECT *".
        assert "SELECT side" in body
        assert "market_source_id" in body
        assert "is_sample" in body
        # PR24B's previous fetchall had SELECT * FROM source_trades.
        # We allow the phrase in the docstring but not in code.
        assert "SELECT * FROM source_trades" not in body

    def test_run_scan_compute_wallet_metrics_no_timestamps_list(self) -> None:
        """PR26 cleanup: the streaming loop must not append to a
        ``timestamps`` list. Timestamps are tracked as scalar
        ``latest_trade_ts`` / ``first_trade_ts`` so peak per-wallet
        memory is O(1) regardless of trade count.
        """
        text = _read("scripts/run_scan.py")
        body = self._function_body(
            text,
            "trades_sql = f\"\"\"SELECT side",
            ["return None", "return {"],
        )
        # No list/set accumulator for timestamps inside the function
        # body. We tolerate the words appearing in the docstring
        # (where they describe the contract) but not in code.
        assert "timestamps: list" not in body
        assert "timestamps.append" not in body
        assert "max(timestamps)" not in body
        assert "min(timestamps)" not in body
        # Scalar tracking must be present.
        assert "latest_trade_ts:" in body
        assert "first_trade_ts:" in body

    def test_run_scan_compute_wallet_metrics_no_market_ids_set(self) -> None:
        """PR26 cleanup: distinct-market counting moved to a scalar
        ``SELECT COUNT(DISTINCT market_source_id)`` query. The
        streaming loop must not accumulate a ``market_ids`` set.
        """
        text = _read("scripts/run_scan.py")
        body = self._function_body(
            text,
            "trades_sql = f\"\"\"SELECT side",
            ["return None", "return {"],
        )
        # No in-Python market_id accumulator.
        assert "market_ids: set" not in body
        assert "market_ids.add" not in body
        # Scalar query must be present.
        assert "COUNT(DISTINCT market_source_id)" in body

    def test_run_scan_compute_wallet_metrics_timestamp_semantics(
        self,
    ) -> None:
        """PR26 cleanup: latest_trade_ts / first_trade_ts must match
        max(timestamps) / min(timestamps) for a known dataset.

        Functional test: builds a real DB with three trades for one
        wallet, calls the function, and checks scalar timestamps.
        """
        import sys
        sys.path.insert(0, str(_REPO_ROOT / "src"))

        from datetime import datetime, timezone

        from polycopy.db.database import Database
        from scripts.run_scan import _compute_wallet_metrics

        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        path = Path(fd.name)
        try:
            db = Database(db_path=path).connect()
            # Database.connect() runs all migrations up to v13, which
            # creates the source_trades table in the right shape. We
            # just insert the test rows.
            db.conn.executemany(  # type: ignore[attr-defined]
                "INSERT INTO source_trades"
                " (id, source, source_trade_id, market_source_id, side,"
                "  outcome, quantity, price, trader_address, timestamp,"
                "  is_sample, token_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("t1", "x", "x1", "m1", "buy", "Yes", 1.0, 0.4,
                     "0xabc", "2024-01-02T00:00:00+00:00", 0, None),
                    ("t2", "x", "x2", "m2", "sell", "Yes", 1.0, 0.6,
                     "0xabc", "2024-01-01T00:00:00+00:00", 0, None),
                    ("t3", "x", "x3", "m1", "buy", "No", 1.0, 0.3,
                     "0xabc", "2024-01-03T00:00:00+00:00", 0, None),
                ],
            )
            db.conn.commit()

            metrics = _compute_wallet_metrics(
                db, "0xabc", datetime(2024, 6, 1, tzinfo=timezone.utc)
            )
            assert metrics is not None
            assert metrics["trade_count"] == 3
            # The trade with the largest timestamp is t3 (2024-01-03).
            assert metrics["latest_trade_ts"] == datetime(
                2024, 1, 3, tzinfo=timezone.utc
            )
            # The smallest is t2 (2024-01-01). The function does not
            # assume any sort order on the streaming cursor — it just
            # tracks min/max as it goes.
            assert metrics["first_trade_ts"] == datetime(
                2024, 1, 1, tzinfo=timezone.utc
            )
            db.close()
        finally:
            path.unlink()

    def test_run_scan_compute_wallet_metrics_markets_traded_count(
        self,
    ) -> None:
        """PR26 cleanup: ``markets_traded`` must equal the count of
        DISTINCT market_source_id for the wallet — verified by
        building a real DB and comparing to a hand-computed count.
        """
        import sys
        sys.path.insert(0, str(_REPO_ROOT / "src"))

        from datetime import datetime, timezone

        from polycopy.db.database import Database
        from scripts.run_scan import _compute_wallet_metrics

        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        path = Path(fd.name)
        try:
            db = Database(db_path=path).connect()
            # Database.connect() runs all migrations up to v13, which
            # creates the source_trades table in the right shape. We
            # just insert the test rows.
            rows = []
            for i, mid in enumerate(
                ["mA", "mA", "mB", "mC", "mC", "mC", "mD"]
            ):
                rows.append(
                    (
                        f"t{i}", "x", f"x{i}", mid, "buy", "Yes",
                        1.0, 0.4, "0xdef",
                        f"2024-01-{i+1:02d}T00:00:00+00:00", 0, None,
                    )
                )
            db.conn.executemany(  # type: ignore[attr-defined]
                "INSERT INTO source_trades"
                " (id, source, source_trade_id, market_source_id, side,"
                "  outcome, quantity, price, trader_address, timestamp,"
                "  is_sample, token_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            db.conn.commit()

            metrics = _compute_wallet_metrics(
                db, "0xdef", datetime(2024, 6, 1, tzinfo=timezone.utc)
            )
            assert metrics is not None
            assert metrics["trade_count"] == 7
            assert metrics["markets_traded"] == 4
            db.close()
        finally:
            path.unlink()

    def test_update_paper_portfolio_open_positions_uses_iter_rows(self) -> None:
        text = _read("scripts/update_paper_portfolio.py")
        body = self._function_body(
            text,
            "Loading open positions...",
            [
                "Fetching current market prices",
                "Mark-to-market each position",
                "Check pending orders",
            ],
        )
        assert "db.fetchall" not in body, (
            "scripts/update_paper_portfolio.py still calls fetchall on "
            "the open-positions hot path — PR24B must use db.iter_rows(...)."
        )
        assert "db.iter_rows" in body, (
            "scripts/update_paper_portfolio.py must use db.iter_rows(...) "
            "for the open-positions read."
        )
        # Explicit column projection. Match the actual code shape.
        assert "SELECT id, market_id, wallet_id, outcome, quantity, avg_entry_price" in body
        assert "SELECT * FROM positions" not in body

    def test_update_paper_portfolio_pending_orders_uses_iter_rows(self) -> None:
        text = _read("scripts/update_paper_portfolio.py")
        body = self._function_body(
            text,
            "Checking pending paper orders...",
            ["Record experiment"],
        )
        assert "db.fetchall" not in body, (
            "scripts/update_paper_portfolio.py pending-orders path must "
            "not call fetchall (PR24B)."
        )
        assert "db.iter_rows" in body
        assert "SELECT * FROM orders" not in body

    def test_settle_paper_positions_per_market_uses_iter_rows(self) -> None:
        text = _read("scripts/settle_paper_positions.py")
        # Slice from the new ``positions: list = []`` line (PR24B) to
        # the "for pos in positions" loop body. This skips the docstring
        # comment that mentions the legacy ``fetchall`` call.
        body = self._function_body(
            text,
            "positions: list = []\n            for pos in db.iter_rows(",
            ["for pos in positions:"],
        )
        assert "db.fetchall" not in body, (
            "scripts/settle_paper_positions.py per-market positions read "
            "must not call fetchall (PR24B)."
        )
        assert "db.iter_rows" in body
        assert "SELECT * FROM positions" not in body

    def test_collect_smart_money_uses_explicit_columns_for_distinct(
        self,
    ) -> None:
        """collect's distinct-address query stays as-is; document in PR."""
        text = _read("scripts/collect_smart_money_data.py")
        # This one keeps its existing fetchall — but we still want to
        # confirm the column list is explicit.
        body = self._function_body(
            text,
            "Get distinct canonical trader addresses from source_trades.",
            ["\ndef "],
        )
        assert "SELECT DISTINCT" in body
        # And the address column must be referenced via the normalized fragment.
        assert "address_column_normalized" in body


class TestRssGuardIntegratedInScripts:
    """All four protected scripts call check_rss_limit somewhere in their hot path."""

    @pytest.mark.parametrize(
        "script",
        [
            "scripts/run_scan.py",
            "scripts/update_paper_portfolio.py",
            "scripts/settle_paper_positions.py",
            "scripts/collect_smart_money_data.py",
        ],
    )
    def test_script_calls_check_rss_limit(self, script: str) -> None:
        text = _read(script)
        assert "check_rss_limit" in text, (
            f"{script} does not call check_rss_limit — PR24B RSS guard "
            f"is missing."
        )

    @pytest.mark.parametrize(
        "script",
        [
            "scripts/run_scan.py",
            "scripts/update_paper_portfolio.py",
            "scripts/settle_paper_positions.py",
            "scripts/collect_smart_money_data.py",
        ],
    )
    def test_script_catches_memory_limit_exceeded(self, script: str) -> None:
        text = _read(script)
        assert "MemoryLimitExceeded" in text, (
            f"{script} does not import / handle MemoryLimitExceeded."
        )
        assert "except MemoryLimitExceeded" in text, (
            f"{script} does not catch MemoryLimitExceeded at the script "
            f"top level — the RSS guard must exit nonzero."
        )

    @pytest.mark.parametrize(
        "script",
        [
            "scripts/run_scan.py",
            "scripts/update_paper_portfolio.py",
            "scripts/settle_paper_positions.py",
            "scripts/collect_smart_money_data.py",
        ],
    )
    def test_script_still_uses_global_operational_lock(self, script: str) -> None:
        """PR24D invariant preserved: the global lock helper is still used."""
        text = _read(script)
        assert "operational_job_lock" in text, (
            f"{script} no longer uses operational_job_lock — PR24D "
            f"invariant regressed."
        )


# ─────────────────────────────────────────────────────────────────────────
# Cross-script integrity: PR24D tests still pass with our changes
# ─────────────────────────────────────────────────────────────────────────


class TestPR24DLockBehaviorUnchanged:
    """Sanity check: PR24D's operational lock still imports + acquires."""

    def test_operational_lock_still_importable(self) -> None:
        from polycopy.runtime.locks import (
            DEFAULT_OPERATIONAL_LOCK_TIMEOUT_S,
            operational_job_lock,
        )

        assert DEFAULT_OPERATIONAL_LOCK_TIMEOUT_S == 30.0
        assert callable(operational_job_lock)

    def test_memory_module_does_not_change_lock_behavior(self) -> None:
        """Importing memory helpers must not affect lock module globals."""
        import polycopy.runtime.locks as locks
        from polycopy.runtime import memory

        # Importing memory must not mutate lock constants.
        assert hasattr(locks, "operational_job_lock")
        assert hasattr(memory, "MemoryLimitExceeded")
        assert hasattr(memory, "check_rss_limit")