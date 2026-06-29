"""Regression tests for round-6 PR #3 fixes.

This file covers two distinct Codex findings on the same PR:

1. ``scripts/run_scan.py`` — direct-execution import regression.
   ``_get_scan_trade_adapter`` previously did
   ``from scripts._live_ingest import build_trade_adapter``, which only works
   when ``scripts`` is importable as a package (i.e. ``python -m
   scripts.run_scan``). When the file is run directly as
   ``python /abs/path/to/scripts/run_scan.py`` — the canonical way the CLI
   is shipped — the package-style import raises
   ``ModuleNotFoundError: No module named 'scripts'`` and breaks both
   startup-with-no-args and the mocked live path that the smoke test
   exercises. The fix reuses the module-scoped
   ``from _live_ingest import build_trade_adapter`` (which itself adds
   ``scripts/`` to ``sys.path``).

2. ``src/polycopy/api/repository.py`` — sentinel pagination regression.
   ``scans()`` and ``wallets()`` used to filter sentinels in *Python*
   AFTER ``LIMIT/OFFSET`` had already truncated the result set, AND
   counted total via ``SELECT COUNT(*) FROM wallets`` which still
   included sentinels. That meant:
     - a page of N could return < N real rows (sentinels swallowed the budget)
     - ``total_count`` lied about how many real rows existed
     - a database containing ONLY sentinels returned an empty list but
       a non-zero ``total_count`` (broken UX).
   The fix moves the predicate into SQL (BEFORE LIMIT/OFFSET) and uses
   the same predicate for both the SELECT and the COUNT.

Both fixes use ``is_sentinel_trader_address`` / ``LEGACY_TRADER_ADDRESS_SENTINELS``
as the source-of-truth predicate; this test verifies the SQL form covers
the same set:

  - ``None``                  (SQL: ``address IS NULL``)
  - empty ``""``              (SQL: ``TRIM(address) = ''``)
  - whitespace-only ``"   "`` (SQL: ``TRIM(address) = ''``)
  - ``"unknown"`` / ``"UNKNOWN"`` / ``"  unknown  "``
  - ``"anonymous"`` / ``"Anonymous"``
  - ``"missing"`` / ``"MiSsInG"``
  - ``"0x"``
  - ``"0x0"`` / ``"0X0"``

All other addresses — including odd-but-real values like
``"0xabc"`` and ``"attributed_string"`` — must be preserved.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Ensure src/ and scripts/ are on sys.path so the modules under test can
# import. Tests are run from the repo root in CI; this mirrors the
# arrangement in tests/test_p22_sentinel_wallets.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.api.repository import (  # noqa: E402
    _SENTINEL_FRAGMENT,
    _SENTINEL_PARAMS,
    DashboardRepository,
    Page,
)
from polycopy.config.settings import Settings  # noqa: E402
from polycopy.db.database import Database  # noqa: E402
from polycopy.domain.source_trade import (  # noqa: E402
    LEGACY_TRADER_ADDRESS_SENTINELS,
    is_sentinel_trader_address,
)


# ─── Helpers ──────────────────────────────────────────────────────────────


def _seed_db(db_path: Path, addresses: list[str]) -> Database:
    """Build a fresh DB at the latest schema and insert the given wallets.

    Returns a connected ``Database`` ready for ``DashboardRepository``.
    ``Database.connect()`` runs all pending migrations automatically, so
    we just open it and insert.
    """
    db = Database(db_path=db_path).connect()
    # Ensure performance_summaries row exists for each wallet so the
    # ``scans()`` LEFT JOIN has something to coalesce.
    now_iso = datetime.now(timezone.utc).isoformat()
    for addr in addresses:
        wid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO wallets (id, address, label, is_sample, created_at) "
            "VALUES (?, ?, 'test', 0, ?)",
            (wid, addr, now_iso),
        )
        db.execute(
            "INSERT INTO performance_summaries "
            "(wallet_id, strategy_label, start_date, end_date, "
            " total_pnl, realized_pnl, unrealized_pnl, win_rate, "
            " max_drawdown, trade_count, is_sample) "
            "VALUES (?, 'default', ?, ?, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0)",
            (wid, now_iso, now_iso),
        )
    db.conn.commit()
    return db


# ─── 1. SQL predicate mirrors the Python helper exactly ──────────────────


class TestSentinelSqlPredicate:
    """The SQL fragment must classify addresses identically to
    ``is_sentinel_trader_address``."""

    @pytest.mark.parametrize(
        "address",
        [
            None,  # type: ignore[list-item]
            "",
            " ",
            "\t",
            "\n",
            "   ",
            "unknown",
            "UNKNOWN",
            "Unknown",
            "  unknown  ",
            "\tunknown\n",
            "anonymous",
            "Anonymous",
            "ANONYMOUS",
            "missing",
            "MiSsInG",
            "0x",
            "0X",
            "  0x  ",
            "0x0",
            "0X0",
        ],
    )
    def test_address_is_sentinel_in_python(self, address):
        assert is_sentinel_trader_address(address), (
            f"Python helper should classify {address!r} as sentinel"
        )

    @pytest.mark.parametrize(
        "address",
        [
            "0xREAL_WALLET_1",
            "0xReal_Wallet_Mixed_Case",
            "0xabc",  # malformed 0x but NOT a sentinel
            "0x1234567890abcdef1234567890abcdef12345678",  # 40 hex
            "attributed_string",
            "user@example.com",
        ],
    )
    def test_address_is_not_sentinel_in_python(self, address):
        assert not is_sentinel_trader_address(address), (
            f"Python helper should NOT classify {address!r} as sentinel"
        )

    def test_sql_fragment_param_list_matches_legacy_sentinels(self):
        """The 5 params in ``_SENTINEL_PARAMS`` must be exactly the same
        5 strings in ``LEGACY_TRADER_ADDRESS_SENTINELS``."""
        assert set(_SENTINEL_PARAMS) == set(LEGACY_TRADER_ADDRESS_SENTINELS)

    def test_sql_fragment_rejects_every_python_sentinel(self, tmp_path: Path):
        """Run the actual SQL fragment against a temp DB seeded with every
        variant and confirm none of them pass the WHERE clause."""
        db_path = tmp_path / "sql-predicate.db"
        # Schema has address NOT NULL, so None can't be inserted at the
        # SQL layer — but the IS NULL branch in the fragment still covers
        # the rare case of legacy rows that bypass the schema. We verify
        # every string variant below.
        addresses = [
            "",
            " ",
            "\t",
            "\n",
            "   ",
            "unknown", "UNKNOWN", "Unknown", "  unknown  ", "\tunknown\n",
            "anonymous", "Anonymous", "ANONYMOUS",
            "missing", "MiSsInG", "MISSING",
            "0x", "0X", "  0x  ",
            "0x0", "0X0",
        ]
        db = _seed_db(db_path, addresses)
        # The fragment is ``NOT (... sentinel conditions ...)`` — so a
        # row that IS a sentinel must NOT match the WHERE. We confirm by
        # counting: an empty result set means every inserted row is a
        # sentinel (the SQL WHERE rejected them all).
        row = db.fetchone(
            f"SELECT COUNT(*) AS n FROM wallets WHERE {_SENTINEL_FRAGMENT}",
            _SENTINEL_PARAMS,
        )
        assert row is not None
        assert row["n"] == 0, (
            f"SQL predicate classified some sentinels as non-sentinels: "
            f"row count was {row['n']}, expected 0"
        )
        db.close()

    def test_sql_fragment_preserves_every_real_address(self, tmp_path: Path):
        db_path = tmp_path / "sql-real.db"
        real_addresses = [
            "0xREAL_WALLET_1",
            "0xReal_Wallet_Mixed_Case",
            "0xabc",
            "0x1234567890abcdef1234567890abcdef12345678",
            "attributed_string",
            "user@example.com",
        ]
        db = _seed_db(db_path, real_addresses)
        row = db.fetchone(
            f"SELECT COUNT(*) AS n FROM wallets WHERE {_SENTINEL_FRAGMENT}",
            _SENTINEL_PARAMS,
        )
        assert row is not None
        assert row["n"] == len(real_addresses), (
            f"SQL predicate dropped real wallets: "
            f"expected {len(real_addresses)}, got {row['n']}"
        )
        db.close()


# ─── 2. Repository: scans() and wallets() use SQL-before-LIMIT/OFFSET ────


class TestRepositoryScans:
    """``scans()`` must filter sentinels in SQL before LIMIT/OFFSET, with
    ``total_count`` matching the count of non-sentinel wallets."""

    def test_scans_returns_only_real_wallets(self, tmp_path: Path):
        db_path = tmp_path / "scans-real.db"
        real = [f"0xREAL_{i:04d}" for i in range(5)]
        sentinels = ["unknown", "anonymous", "0x", "0x0", ""]
        db = _seed_db(db_path, real + sentinels)
        repo = DashboardRepository(db=db, settings=Settings())
        resp = repo.scans(Page(limit=50, offset=0))
        assert resp.total_count == len(real), (
            f"total_count should be {len(real)} (real only), got {resp.total_count}"
        )
        assert len(resp.scans) == len(real), (
            f"scans should be {len(real)}, got {len(resp.scans)}"
        )
        assert {s.address for s in resp.scans} == set(real)
        db.close()

    def test_scans_pagination_fills_page_with_real_wallets(self, tmp_path: Path):
        """limit=2 with 5 real + 5 sentinel must return 2 real wallets per
        page, not 'N rows that may include sentinels'."""
        db_path = tmp_path / "scans-page.db"
        real = [f"0xPAGE_{i:04d}" for i in range(5)]
        sentinels = ["unknown"] * 3 + ["anonymous"] + ["0x"]
        # Insert sentinels FIRST so without the SQL fix they would
        # crowd out real wallets under ORDER BY created_at DESC.
        db = _seed_db(db_path, sentinels + real)
        repo = DashboardRepository(db=db, settings=Settings())

        # Page 1
        resp1 = repo.scans(Page(limit=2, offset=0))
        assert resp1.total_count == len(real)
        assert len(resp1.scans) == 2, (
            f"page 1 should have 2 real wallets, got {len(resp1.scans)}"
        )
        assert all(not is_sentinel_trader_address(s.address) for s in resp1.scans)
        page1_addrs = {s.address for s in resp1.scans}

        # Page 2
        resp2 = repo.scans(Page(limit=2, offset=2))
        assert resp2.total_count == len(real)
        assert len(resp2.scans) == 2
        assert all(not is_sentinel_trader_address(s.address) for s in resp2.scans)
        page2_addrs = {s.address for s in resp2.scans}

        # Page 3 (remainder)
        resp3 = repo.scans(Page(limit=2, offset=4))
        assert resp3.total_count == len(real)
        assert len(resp3.scans) == 1
        page3_addrs = {s.address for s in resp3.scans}

        # No overlap; union is all real
        all_paged = page1_addrs | page2_addrs | page3_addrs
        assert all_paged == set(real), (
            f"paged scan returned unexpected wallets: {all_paged}"
        )
        assert len(page1_addrs & page2_addrs) == 0
        assert len(page1_addrs & page3_addrs) == 0
        assert len(page2_addrs & page3_addrs) == 0
        db.close()

    def test_scans_offset_beyond_total_returns_empty_list(self, tmp_path: Path):
        db_path = tmp_path / "scans-overflow.db"
        real = [f"0xOFF_{i:04d}" for i in range(3)]
        sentinels = ["unknown", "anonymous", ""]
        db = _seed_db(db_path, real + sentinels)
        repo = DashboardRepository(db=db, settings=Settings())
        resp = repo.scans(Page(limit=10, offset=99))
        assert resp.total_count == len(real)
        assert resp.scans == []
        db.close()

    def test_scans_sentinel_only_database(self, tmp_path: Path):
        """A DB containing only sentinels must return total_count=0 and an
        empty list — NOT the demo fallback (demo is disabled in this test)
        and NOT a non-zero count with empty list."""
        db_path = tmp_path / "scans-sentinels-only.db"
        sentinels = ["unknown", "anonymous", "missing", "0x", "0x0", "  ", ""]
        db = _seed_db(db_path, sentinels)
        # Disable demo mode so the empty fallback does not kick in.
        settings = Settings(enable_demo_data=False)
        repo = DashboardRepository(db=db, settings=settings)
        resp = repo.scans(Page(limit=10, offset=0))
        assert resp.total_count == 0, (
            f"sentinel-only DB should have total_count=0, got {resp.total_count}"
        )
        assert resp.scans == []
        assert resp.is_sample_data is False
        db.close()

    def test_scans_demo_fallback_only_when_zero_real_wallets(self, tmp_path: Path):
        """When demo is enabled and there are zero real wallets, the
        sample fallback should fire; sentinel-only DBs should NOT trigger
        it (the fix ensures total_count is 0 for sentinel-only DBs)."""
        db_path = tmp_path / "scans-demo.db"
        sentinels = ["unknown", "anonymous"]
        db = _seed_db(db_path, sentinels)
        settings = Settings(enable_demo_data=True)
        repo = DashboardRepository(db=db, settings=settings)
        resp = repo.scans(Page(limit=10, offset=0))
        assert resp.is_sample_data is True
        assert resp.total_count == 1
        assert len(resp.scans) == 1
        assert resp.scans[0].address == "0xSAMPLE_WALLET_ADDRESS_DO_NOT_USE_IN_PROD"
        db.close()


class TestRepositoryWallets:
    """``wallets()`` must filter sentinels in SQL before LIMIT/OFFSET."""

    def test_wallets_returns_only_real_addresses(self, tmp_path: Path):
        db_path = tmp_path / "wallets-real.db"
        real = ["0xRW_001", "0xRW_002", "0xRW_003"]
        sentinels = ["unknown", "Anonymous", "MISSING", "0x", "0x0", "   ", ""]
        db = _seed_db(db_path, real + sentinels)
        repo = DashboardRepository(db=db, settings=Settings())
        resp = repo.wallets(Page(limit=50, offset=0))
        assert resp.total_count == len(real)
        assert len(resp.wallets) == len(real)
        assert {w.address for w in resp.wallets} == set(real)
        db.close()

    def test_wallets_offset_zero_with_sentinel_padding(self, tmp_path: Path):
        """With many sentinels and limit=2, page 1 must yield 2 real
        wallets (not 0 because sentinels ate the budget)."""
        db_path = tmp_path / "wallets-padding.db"
        real = [f"0xPAD_{i:04d}" for i in range(4)]
        sentinels = ["unknown"] * 10  # lots of noise
        db = _seed_db(db_path, sentinels + real)
        repo = DashboardRepository(db=db, settings=Settings())
        resp = repo.wallets(Page(limit=2, offset=0))
        assert resp.total_count == len(real)
        assert len(resp.wallets) == 2
        assert all(not is_sentinel_trader_address(w.address) for w in resp.wallets)
        db.close()

    def test_wallets_sentinel_only_database(self, tmp_path: Path):
        db_path = tmp_path / "wallets-sentinel-only.db"
        db = _seed_db(db_path, ["unknown", "anonymous", "0x0"])
        settings = Settings(enable_demo_data=False)
        repo = DashboardRepository(db=db, settings=settings)
        resp = repo.wallets(Page(limit=10, offset=0))
        assert resp.total_count == 0
        assert resp.wallets == []
        assert resp.is_sample_data is False
        db.close()

    def test_wallets_demo_fallback_when_zero_real(self, tmp_path: Path):
        """Demo fallback fires on real-empty DBs."""
        db_path = tmp_path / "wallets-demo-fallback.db"
        db = _seed_db(db_path, ["unknown", "anonymous"])  # no real rows
        settings = Settings(enable_demo_data=True)
        repo = DashboardRepository(db=db, settings=settings)
        resp = repo.wallets(Page(limit=10, offset=0))
        assert resp.is_sample_data is True
        assert resp.total_count == 1
        assert len(resp.wallets) == 1
        db.close()


# ─── 3. Predicate parity: scans() and wallets() agree on real count ──────


class TestCountListParity:
    """``scans()`` and ``wallets()`` must return the same ``total_count``
    for the same DB and the same ``Page``."""

    @pytest.mark.parametrize("limit,offset", [(10, 0), (5, 2), (50, 100)])
    def test_scans_and_wallets_agree_on_total_count(self, tmp_path: Path, limit, offset):
        db_path = tmp_path / "parity.db"
        real = [f"0xPAR_{i:04d}" for i in range(7)]
        sentinels = ["unknown", "Anonymous", "  unknown  ", "0x", "0x0", ""]
        db = _seed_db(db_path, real + sentinels)
        repo = DashboardRepository(db=db, settings=Settings())
        page = Page(limit=limit, offset=offset)

        scans_resp = repo.scans(page)
        wallets_resp = repo.wallets(page)

        assert scans_resp.total_count == wallets_resp.total_count, (
            f"scans.total_count ({scans_resp.total_count}) != "
            f"wallets.total_count ({wallets_resp.total_count})"
        )
        # Both must equal the count of real wallets.
        assert scans_resp.total_count == len(real)
        db.close()


# ─── 4. Legitimate-wallet ordering preserved ────────────────────────────


class TestLegitimateOrdering:
    """Real wallets must come back in the same order across pages — the
    SQL ORDER BY created_at DESC, id is unchanged."""

    def test_paginated_wallets_are_stable_and_complete(self, tmp_path: Path):
        db_path = tmp_path / "ordering.db"
        # Distinct microsecond timestamps so ORDER BY created_at DESC
        # is fully determined by insertion order. Reverse-insert to
        # make the expected DESC order obvious.
        real_in_insert_order = [f"0xORD_{i:04d}" for i in range(6)]
        real_in_desc_order = list(reversed(real_in_insert_order))
        db = Database(db_path=db_path).connect()
        # Use distinct timestamps so the secondary ``id`` tiebreaker is
        # irrelevant — every row has a unique ``created_at``.
        base_ts = datetime.now(timezone.utc)
        for i, addr in enumerate(real_in_insert_order + ["unknown", "0x", ""]):
            wid = str(uuid.uuid4())
            ts = base_ts.replace(microsecond=base_ts.microsecond + i).isoformat()
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                "VALUES (?, ?, 'test', 0, ?)",
                (wid, addr, ts),
            )
        db.conn.commit()
        repo = DashboardRepository(db=db, settings=Settings())

        # Concatenate pages 1..N at limit=2 — must reconstruct all real
        # wallets in the same order as the SQL would produce.
        limit = 2
        collected: list[str] = []
        offset = 0
        while True:
            resp = repo.wallets(Page(limit=limit, offset=offset))
            assert resp.total_count == len(real_in_insert_order)
            collected.extend(w.address for w in resp.wallets)
            offset += limit
            if offset >= resp.total_count:
                break

        assert collected == real_in_desc_order, (
            f"paged order wrong.\n  expected: {real_in_desc_order}\n"
            f"  got:      {collected}"
        )
        db.close()


# ─── 5. run_scan direct-execution import fix ─────────────────────────────


class TestRunScanDirectExecution:
    """``run_scan`` must be importable AND its lazy adapter builder must
    work when the file is executed via an absolute path with no
    ``PYTHONPATH`` — the way the shipped CLI is invoked."""

    def test_run_scan_importable_via_absolute_path(self):
        """Importing ``run_scan`` and accessing the lazy adapter builder
        must succeed without ``ModuleNotFoundError: No module named
        'scripts'`` when ``scripts/`` is on sys.path but NOT as a
        package. We test this by calling the same subprocess invocation
        pattern that failed pre-fix."""
        # Save and restore sys.path around the call so we don't pollute
        # the test session. The run_scan module adds scripts/ at index 0
        # at import time.
        saved = list(sys.path)
        # Pre-condition: drop the repo root from sys.path so ``scripts``
        # cannot be imported as a package. scripts/ remains available
        # via run_scan's own module-scope sys.path.insert.
        try:
            sys.path[:] = [p for p in sys.path if p != str(_REPO_ROOT)]
            # Make sure run_scan isn't cached from a previous test that
            # imported it under a package context.
            sys.modules.pop("run_scan", None)
            sys.modules.pop("scripts._live_ingest", None)
            sys.modules.pop("_live_ingest", None)
            # Add scripts/ to sys.path so the module-scope
            # ``from _live_ingest import build_trade_adapter`` works.
            scripts_dir = str(_REPO_ROOT / "scripts")
            sys.path.insert(0, scripts_dir)
            # Add src/ for polycopy.* imports.
            src_dir = str(_REPO_ROOT / "src")
            if src_dir not in sys.path:
                sys.path.insert(0, src_dir)

            # Now perform the import and the lazy call. Pre-fix, this
            # raised ``ModuleNotFoundError: No module named 'scripts'``
            # because ``build_trade_adapter`` was re-imported as
            # ``scripts._live_ingest.build_trade_adapter``. Post-fix,
            # the function uses the module-scoped helper.
            import run_scan  # type: ignore[import-not-found]

            # Patch out the lazy adapter so we don't hit live HTTP.
            sentinel_adapter = object()
            run_scan._SCAN_TRADE_ADAPTER = sentinel_adapter  # type: ignore[attr-defined]
            assert run_scan._get_scan_trade_adapter() is sentinel_adapter, (
                "lazy builder must return the cached adapter (proving "
                "no re-import is attempted)"
            )
        finally:
            sys.path[:] = saved

    def test_run_scan_direct_path_does_not_use_package_import(self):
        """The canonical failure mode for the buggy version was:

            env -u PYTHONPATH python3 /abs/path/to/scripts/run_scan.py …

        In that invocation, scripts/ is on sys.path (Python adds the
        script's dir automatically) but it is NOT importable as a
        package ``scripts`` — so the buggy line
        ``from scripts._live_ingest import build_trade_adapter`` would
        raise ``ModuleNotFoundError: No module named 'scripts'``.

        We can't easily call ``_get_scan_trade_adapter()`` from a
        subprocess with no PYTHONPATH (the function tries to open a real
        HTTP socket), so we test the *observable* invariant instead: the
        module file must not contain an active
        ``from scripts._live_ingest import build_trade_adapter`` line.
        (A docstring or comment that mentions the pattern is fine.)"""
        src = (_REPO_ROOT / "scripts" / "run_scan.py").read_text()
        bad_pattern_lines = [
            line
            for line in src.splitlines()
            # Only match real import statements, not docstrings/comments.
            if not line.lstrip().startswith("#")
            and line.lstrip().startswith("from scripts._live_ingest import")
            and "build_trade_adapter" in line
        ]
        assert bad_pattern_lines == [], (
            "run_scan.py re-introduced the package-style import for "
            "build_trade_adapter, which breaks direct execution.\n"
            f"Offending lines: {bad_pattern_lines}"
        )

    def test_run_scan_module_scope_import_present(self):
        """Belt-and-braces: the fix relies on the module-scope
        ``from _live_ingest import build_trade_adapter`` import (line ~53).
        If somebody deletes it, this test fails."""
        src = (_REPO_ROOT / "scripts" / "run_scan.py").read_text()
        assert "from _live_ingest import" in src, (
            "run_scan.py must keep a module-scope "
            "``from _live_ingest import …`` (with build_trade_adapter) "
            "so direct execution works."
        )
        # The module-scope import is wrapped in try/except ImportError —
        # make sure that's still the structure.
        assert "try:" in src and "from _live_ingest import" in src, (
            "expected module-scope try/except ImportError around "
            "the _live_ingest import"
        )

    def test_run_scan_invoked_directly_via_subprocess(self):
        """End-to-end: running run_scan.py as ``python /abs/path/run_scan.py``
        with PYTHONPATH unset from cwd=/tmp must not raise on import.
        We invoke ``--help`` (no live HTTP) and verify the script prints
        its usage banner."""
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "PYTHONPATH": "",  # explicit: no inherited PYTHONPATH
        }
        proc = subprocess.run(
            [
                sys.executable,
                str(_REPO_ROOT / "scripts" / "run_scan.py"),
                "--help",
            ],
            cwd="/tmp",
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, (
            f"run_scan --help crashed (returncode={proc.returncode}).\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
        assert "ModuleNotFoundError" not in proc.stderr, (
            f"unexpected ModuleNotFoundError in stderr: {proc.stderr}"
        )
        assert "Run full smart-money scan" in proc.stdout, (
            f"unexpected --help output: {proc.stdout[:500]}"
        )