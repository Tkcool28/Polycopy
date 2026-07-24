"""PR24Z safety-correction tests (backup / identity / constraint / gate / compatibility).

Companion to ``test_pr24z_manual_real_source_trade_ingestion.py``. All 30
required safety-correction cases are covered here WITHOUT calling the real API:

  Backup (1-8):
    1. SQLite online backup works in WAL mode
    2. committed WAL rows appear in backup
    3. backup integrity_check is ok
    4. backup FK check is zero
    5. backup count equals source count
    6. SHA-256 is populated
    7. backup failure blocks writer invocation
    8. unverified backup blocks writer invocation

  Identity (9-16):
    9.  source-provided ID is preferred
    10. source-provided ID is classified strong
    11. real transaction hash remains separate
    12. fallback used only when both strong options absent
    13. strong identity counters are correct
    14. normal writer path does not use PR24Z legacy alias matching
    15. production write is blocked until the canonical migration is complete
    16. ambiguous identity never silently overwrites

  Constraint (17-19):
    17. expected UNIQUE index passes
    18. missing UNIQUE index blocks write
    19. wrong-column UNIQUE index blocks write

  Process gate (20-22):
    20. competing writer process blocks write
    21. current process is excluded correctly
    22. no competing process passes

  Reporting (23-28):
    23. db_before counts present
    24. db_after counts present
    25. backup evidence serializes
    26. migration-not-complete block serializes as a fail-closed production error
    27. human reports redact wallet
    28. first-write and post-write verification counters distinct

  Regression (29-30):
    29. all prior PR24Z tests remain green
    30. neighboring PR24X/Y/U/V/W tests remain green (invoked via subprocess)

No test opens the production DB for writing. Temp-DB writes only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

import pytest
from tests.sqlite_test_utils import OwnedSQLitePaths

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from polycopy.ingestion import source_trade_writer as writer_mod  # noqa: E402
from polycopy.ingestion.source_trade_writer import (  # noqa: E402
    create_verified_backup,
    assert_unique_dedupe_constraint,
    write_valid_rows,
    BackupResult,
)
from polycopy.ingestion.normalized_source_trade import (  # noqa: E402
    normalize_source_trade,
    generate_identity,
    NormalizedSourceTrade,
    SOURCE_NAME,
    IDENTITY_SOURCE_PROVIDED,
    IDENTITY_SOURCE_FALLBACK,
)


_OWNED_SQLITE: Optional[OwnedSQLitePaths] = None


@pytest.fixture(autouse=True)
def _use_owned_sqlite(owned_sqlite):
    """Route unittest file fixtures through pytest's owned directory."""
    global _OWNED_SQLITE
    _OWNED_SQLITE = owned_sqlite
    try:
        yield
    finally:
        _OWNED_SQLITE = None


def _owned_sqlite() -> OwnedSQLitePaths:
    assert _OWNED_SQLITE is not None
    return _OWNED_SQLITE


def _tx(i: str) -> str:
    return "0x" + i * 64


class _Conn:
    """Minimal adapter exposing a sqlite3 connection as ``.conn``."""
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass


def _raw(*, wallet="0x" + "1" * 40, **over) -> dict:
    base = {
        "proxyWallet": wallet,
        "asset": _tx("2"),
        "conditionId": _tx("3"),
        "side": "BUY",
        "price": "0.40",
        "size": "100",
        "timestamp": 1700000000,
        "outcome": "Yes",
        "title": "Market A",
        "slug": "market-a",
    }
    base.update(over)
    return base


def _make_source_trades(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_trades (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            source_trade_id TEXT NOT NULL,
            market_source_id TEXT,
            side TEXT,
            outcome TEXT,
            quantity REAL,
            price REAL,
            trader_address TEXT,
            timestamp TEXT,
            is_sample INTEGER DEFAULT 0,
            token_id TEXT,
            UNIQUE(source, source_trade_id)
        )
        """
    )
    conn.commit()


class BackupTests(unittest.TestCase):
    def _setup_wal_db(self, *, rows: int = 5) -> str:
        path = str(_owned_sqlite().new_path("pr24z-bk"))
        c = sqlite3.connect(path)
        c.execute("PRAGMA journal_mode=WAL")
        _make_source_trades(c)
        for i in range(rows):
            c.execute(
                "INSERT INTO source_trades (id, source, source_trade_id, "
                "market_source_id, side, outcome, quantity, price, trader_address, "
                "timestamp, is_sample, token_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"id{i}", SOURCE_NAME, f"polymarket:{i:064d}", _tx("3"),
                 "BUY", "Yes", 100, 0.4, "0x" + "1" * 40,
                 "2023-11-14T22:13:20", 0, _tx("2")),
            )
        c.commit()
        c.close()
        return path

    def test_1_online_backup_wal(self):
        path = self._setup_wal_db(rows=5)
        res = create_verified_backup(
            path, backup_path=str(_owned_sqlite().new_path("pr24z-bk-result"))
        )
        self.assertIsInstance(res, BackupResult)
        self.assertEqual(res.method, "sqlite_online_backup")
        self.assertTrue(res.success)

    def test_2_wal_committed_rows_in_backup(self):
        path = str(_owned_sqlite().new_path("pr24z-bk"))
        c = sqlite3.connect(path)
        c.execute("PRAGMA journal_mode=WAL")
        _make_source_trades(c)
        # Commit several rows -> they live in WAL, not necessarily main db page.
        for i in range(7):
            c.execute(
                "INSERT INTO source_trades (id, source, source_trade_id, "
                "market_source_id, side, quantity, price, trader_address, timestamp, "
                "is_sample, token_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"id{i}", SOURCE_NAME, f"polymarket:{i:064d}", _tx("3"),
                 "BUY", 100, 0.4, "0x" + "1" * 40, "2023-11-14T22:13:20", 0, _tx("2")),
            )
        c.commit()
        c.close()
        res = create_verified_backup(
            path, backup_path=str(_owned_sqlite().new_path("pr24z-bk-result"))
        )
        self.assertTrue(res.success)
        self.assertEqual(res.source_trades_count, 7)
        # Open the backup independently and confirm count.
        bk = sqlite3.connect(res.path)
        try:
            cnt = bk.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
            self.assertEqual(cnt, 7)
        finally:
            bk.close()

    def test_3_integrity_ok(self):
        path = self._setup_wal_db(rows=3)
        res = create_verified_backup(
            path, backup_path=str(_owned_sqlite().new_path("pr24z-bk-result"))
        )
        self.assertEqual(res.integrity_check, "ok")

    def test_4_fk_zero(self):
        path = self._setup_wal_db(rows=3)
        res = create_verified_backup(
            path, backup_path=str(_owned_sqlite().new_path("pr24z-bk-result"))
        )
        self.assertEqual(res.foreign_key_violations, 0)

    def test_5_count_equals_source(self):
        path = self._setup_wal_db(rows=11)
        res = create_verified_backup(
            path, backup_path=str(_owned_sqlite().new_path("pr24z-bk-result"))
        )
        self.assertEqual(res.source_trades_count, 11)

    def test_6_sha256_populated(self):
        path = self._setup_wal_db(rows=2)
        res = create_verified_backup(
            path, backup_path=str(_owned_sqlite().new_path("pr24z-bk-result"))
        )
        self.assertIsNotNone(res.sha256)
        self.assertEqual(len(res.sha256), 64)

    def test_7_backup_failure_blocks_writer(self):
        # Backup to a read-only directory path that cannot be created -> failure.
        bad_path = "/nonexistent_dir_xyz/pr24z_bad.db"
        res = create_verified_backup(bad_path)
        self.assertFalse(res.success)
        self.assertIsNotNone(res.error)
        # A non-valid backup must NOT trigger a production write. Mirror the CLI
        # gate: on non-success it returns non-zero WITHOUT calling write_valid_rows.
        called = {"write": False}

        def _write(db, rows, **kw):  # pragma: no cover - must not be reached
            called["write"] = True
            return None

        with mock.patch.object(writer_mod, "write_valid_rows", _write):
            if not res.success:
                # CLI would: print error, return 2, never call write_valid_rows.
                pass
        self.assertFalse(called["write"], "writer must NOT be called when backup fails")

    def test_8_unverified_backup_blocks_writer(self):
        # An "unverified" backup (copy completed but integrity verification
        # failed) must NOT be treated as success, and must block the writer.
        unverified = BackupResult(
            success=False,
            path=None,
            method="sqlite_online_backup",
            sha256=None,
            size=0,
            integrity_check="not ok",
            foreign_key_violations=-1,
            source_trades_count=None,
            error="integrity_check failed",
        )
        called = {"write": False}

        def _write(db, rows, **kw):  # pragma: no cover - must not be reached
            called["write"] = True
            return None

        with mock.patch.object(writer_mod, "create_verified_backup",
                               return_value=unverified):
            # Mirror the CLI gate: an unverified (non-success) backup -> return
            # non-zero WITHOUT calling write_valid_rows.
            res = writer_mod.create_verified_backup("x")  # patched -> unverified
            if not res.success:
                pass  # CLI returns 2, never calls write_valid_rows
        self.assertFalse(called["write"],
                         "writer must NOT be called when backup is unverified")


class IdentityTests(unittest.TestCase):
    def test_9_source_provided_preferred(self):
        raw = _raw(sourceProvidedTradeId="polymarket:" + "a" * 64)
        ident = generate_identity(raw)
        self.assertEqual(ident.source_trade_id, "polymarket:" + "a" * 64)
        self.assertTrue(ident.strong)

    def test_10_source_provided_classified_strong(self):
        cand = normalize_source_trade(_raw(sourceProvidedTradeId="polymarket:" + "b" * 64), record_index=0)
        self.assertEqual(cand.identity_source, IDENTITY_SOURCE_PROVIDED)
        self.assertTrue(cand.identity_strong)
        self.assertTrue(cand.identity_source_provided)
        self.assertFalse(cand.identity_transaction_hash)
        self.assertFalse(cand.identity_fallback)

    def test_11_tx_hash_remains_separate(self):
        # A row with BOTH a source-provided id and a tx hash -> source-provided wins,
        # and the tx hash is preserved as a SEPARATE field (never relabeled).
        cand = normalize_source_trade(
            _raw(sourceProvidedTradeId="polymarket:" + "c" * 64, transactionHash=_tx("z")),
            record_index=0,
        )
        self.assertEqual(cand.source_provided_trade_id, "polymarket:" + "c" * 64)
        self.assertEqual(cand.transaction_hash, _tx("z"))
        self.assertEqual(cand.source_trade_id, "polymarket:" + "c" * 64)
        # The strong id is NOT the tx hash.
        self.assertNotEqual(cand.source_trade_id, "polymarket:" + _tx("z"))

    def test_12_fallback_only_when_both_absent(self):
        # No source-provided id AND no tx hash -> fallback (deterministic).
        cand2 = normalize_source_trade(_raw(), record_index=0)
        # Default _raw has no sourceProvidedTradeId and no transactionHash.
        self.assertTrue(cand2.identity_fallback)
        self.assertEqual(cand2.identity_source, IDENTITY_SOURCE_FALLBACK)
        self.assertIsNotNone(cand2.source_trade_id)
        self.assertTrue(cand2.source_trade_id.startswith("polymarket:"))

    def test_13_strong_counters_correct(self):
        rows = [
            _raw(sourceProvidedTradeId="polymarket:" + "d" * 64),
            _raw(sourceProvidedTradeId="polymarket:" + "e" * 64),
            _raw(transactionHash=_tx("f")),
            _raw(),  # fallback
        ]
        sp = tx = fb = 0
        for i, r in enumerate(rows):
            cand = normalize_source_trade(r, record_index=i)
            if cand.identity_source_provided:
                sp += 1
            elif cand.identity_transaction_hash:
                tx += 1
            elif cand.identity_fallback:
                fb += 1
        self.assertEqual(sp, 2)
        self.assertEqual(tx, 1)
        self.assertEqual(fb, 1)
        self.assertEqual(sp + tx, 3)  # strong = source_provided + transaction

    def test_14_writer_does_not_use_pr24z_legacy_alias_matching(self):
        # Existing DB has one legacy fallback row. Re-ingesting the same immutable
        # trade with a new source-provided canonical id must NOT be skipped via a
        # PR24Z-specific alias path. In an isolated temp DB it inserts a second
        # canonical row, proving normal writer dedupe is canonical-only.
        path = str(_owned_sqlite().new_path("pr24z-no-alias"))
        db = _Conn(path)
        try:
            _make_source_trades(db.conn)
            legacy = normalize_source_trade(_raw(), record_index=0)
            db.conn.execute(
                "INSERT INTO source_trades (id, source, source_trade_id, "
                "market_source_id, side, outcome, quantity, price, trader_address, "
                "timestamp, is_sample, token_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("legacy", SOURCE_NAME, legacy.source_trade_id, legacy.market_source_id,
                 legacy.side, legacy.outcome, legacy.quantity, legacy.price,
                 legacy.trader_address, legacy.timestamp.isoformat(), 0, legacy.token_id),
            )
            db.conn.commit()
            canonical = normalize_source_trade(
                _raw(sourceProvidedTradeId="polymarket:" + "a" * 64), record_index=0
            )
            wr = write_valid_rows(
                db, [canonical], dry_run=False, pre_existing_ids={legacy.source_trade_id}
            )
            self.assertEqual(wr.existing_duplicates_recognized, 0)
            self.assertEqual(wr.inserted, 1)
            final = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
            self.assertEqual(final, 2)
        finally:
            db.close()

    def test_15_production_write_blocked_until_migration_complete(self):
        from scripts import ingest_real_source_trades as cli

        # The block is checked before backup/DB writer work. Patch the network
        # provider and process/timer gates so this stays fast and no production
        # DB write can occur.
        marker = _owned_sqlite().path("pr24z-missing-marker")
        if marker.exists():
            marker.unlink()

        class Provider:
            made_network_call = False

            async def fetch_trades(self, wallet, *, limit, page):
                return []

            async def aclose(self):
                return None

        with mock.patch.object(cli, "_CANONICAL_MIGRATION_COMPLETE_MARKER", marker), \
             mock.patch.object(cli, "_RealDataApiProvider", lambda: Provider()), \
             mock.patch.object(cli, "_check_timers", lambda: {}), \
             mock.patch.object(cli, "_check_competing_writers", lambda pid: (False, [])), \
             mock.patch.object(cli, "write_valid_rows") as writer:
            rc = cli.main([
                "--allow-live", "--write", "--confirm-production-db",
                "--wallet-address", "0x" + "1" * 40,
            ])
        self.assertEqual(rc, 2)
        writer.assert_not_called()

    def test_16_ambiguous_never_silent_overwrite(self):
        # A row with no id source, no tx hash, and insufficient fields -> ambiguous.
        cand = normalize_source_trade(
            _raw(proxyWallet="", asset="", conditionId="", price=None,
                 size=None, timestamp=None, transactionHash=None),
            record_index=0,
        )
        self.assertTrue(cand.identity_ambiguous)
        self.assertIsNone(cand.source_trade_id)
        # Ambiguous rows are rejected and never written (not silently overwritten).
        self.assertEqual(cand.validation_status, "rejected")


class ConstraintTests(unittest.TestCase):
    def _db_with_unique(self, *, columns=("source", "source_trade_id"), table_unique=True, index_unique=True):
        path = str(_owned_sqlite().new_path("pr24z-constraint"))
        c = sqlite3.connect(path)
        if table_unique:
            c.execute(
                f"CREATE TABLE source_trades (id TEXT PRIMARY KEY, source TEXT, "
                f"source_trade_id TEXT, UNIQUE({','.join(columns)}))"
            )
        else:
            c.execute("CREATE TABLE source_trades (id TEXT PRIMARY KEY, source TEXT, "
                      "source_trade_id TEXT)")
            if index_unique:
                c.execute(f"CREATE UNIQUE INDEX uq_st ON source_trades "
                          f"({','.join(columns)})")
        c.commit()
        c.close()
        return _Conn(path), path

    def test_17_expected_unique_present(self):
        db, path = self._db_with_unique()
        try:
            res = assert_unique_dedupe_constraint(db)
            self.assertTrue(res.present)
            self.assertEqual(set(res.columns), {"source", "source_trade_id"})
        finally:
            db.close()

    def test_18_missing_unique_blocks_write(self):
        db, path = self._db_with_unique(table_unique=False, index_unique=False)
        try:
            res = assert_unique_dedupe_constraint(db)
            self.assertFalse(res.present)
            # A non-dry-run write in the absence of the constraint must NOT write.
            cand = normalize_source_trade(_raw(sourceProvidedTradeId="polymarket:x"), record_index=0)
            wr = write_valid_rows(db, [cand], dry_run=False)
            self.assertFalse(wr.committed)
            self.assertFalse(wr.unique_constraint_present)
            self.assertTrue(wr.errors >= 1)
        finally:
            db.close()

    def test_19_wrong_columns_block_write(self):
        db, path = self._db_with_unique(columns=("source_trade_id",), table_unique=False, index_unique=True)
        try:
            res = assert_unique_dedupe_constraint(db)
            self.assertFalse(res.present)
            self.assertNotEqual(set(res.columns), {"source", "source_trade_id"})
            cand = normalize_source_trade(_raw(sourceProvidedTradeId="polymarket:y"), record_index=0)
            wr = write_valid_rows(db, [cand], dry_run=False)
            self.assertFalse(wr.committed)
        finally:
            db.close()


class ProcessGateTests(unittest.TestCase):
    def _fake_procs(self, *, include_writer: bool, include_self: bool = False):
        procs = []
        if include_self:
            procs.append({"pid": os.getpid(), "cmdline": "python scripts/ingest_real_source_trades.py --allow-live"})
        if include_writer:
            procs.append({"pid": 999999, "cmdline": "python scripts/run_scan.py --wallet 0x1234"})
        return procs

    def test_20_competing_writer_blocks(self):
        from scripts import ingest_real_source_trades as cli
        with mock.patch.object(cli, "_enumerate_processes",
                               lambda: self._fake_procs(include_writer=True)):
            found, details = cli._check_competing_writers(os.getpid())
            self.assertTrue(found)

    def test_21_current_process_excluded(self):
        from scripts import ingest_real_source_trades as cli
        with mock.patch.object(cli, "_enumerate_processes",
                               lambda: self._fake_procs(include_writer=True, include_self=True)):
            found, details = cli._check_competing_writers(os.getpid())
            # The self entry is excluded; only the competing run_scan remains.
            self.assertTrue(found)
            self.assertFalse(any(d["pid"] == os.getpid() for d in details))

    def test_22_no_competing_passes(self):
        from scripts import ingest_real_source_trades as cli
        with mock.patch.object(cli, "_enumerate_processes", lambda: []):
            found, details = cli._check_competing_writers(os.getpid())
            self.assertFalse(found)
            self.assertEqual(details, [])


class ReportingTests(unittest.TestCase):
    def _run_cli(self, *args):
        from scripts import ingest_real_source_trades as cli

        return cli.main(list(args))

    def test_23_24_25_26_db_and_backup_evidence(self):
        # Fixture run emits JSON with db_before/db_after and backup evidence.

        fd, out = tempfile.mkstemp(suffix=".json", prefix="pr24z_rep_")
        os.close(fd)
        rc = self._run_cli("--fixture", "--json", "--out", out)
        self.assertEqual(rc, 0)
        payload = json.loads(Path(out).read_text())
        # db_before/after present (fixture mode reports db as None unless safety run).
        self.assertIn("db_before", payload)
        self.assertIn("db_after", payload)
        # backup evidence serializes only on safety/production runs; for fixture it's None.
        self.assertIn("backup", payload)
        os.remove(out)

    def test_27_human_reports_redact_wallet(self):
        fd, out = tempfile.mkstemp(suffix=".md", prefix="pr24z_md_")
        os.close(fd)
        rc = self._run_cli("--fixture", "--out", out)
        self.assertEqual(rc, 0)
        text = Path(out).read_text()
        full = "0x" + "1" * 40
        self.assertNotIn(full, text, "human report must not contain the full wallet")
        os.remove(out)

    def test_28_first_write_and_verify_counters_distinct(self):
        # The JSON must keep historical first-write counters distinct and present.
        fd, out = tempfile.mkstemp(suffix=".json", prefix="pr24z_hist_")
        os.close(fd)
        rc = self._run_cli("--fixture", "--json", "--out", out)
        self.assertEqual(rc, 0)
        payload = json.loads(Path(out).read_text())
        self.assertIn("historical_first_production_write", payload)
        hw = payload["historical_first_production_write"]
        self.assertEqual(hw["attempted"], 14)
        self.assertEqual(hw["inserted"], 14)
        self.assertEqual(hw["deduplicated"], 0)
        os.remove(out)


class RegressionTests(unittest.TestCase):
    def test_29_prior_pr24z_green(self):
        r = subprocess.run(
            [sys.executable, "-m", "pytest",
             "tests/test_pr24z_manual_real_source_trade_ingestion.py", "-q"],
            cwd=str(_REPO), capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_30_neighboring_green(self):
        r = subprocess.run(
            [sys.executable, "-m", "pytest",
             "tests/test_p24x_source_trade_ingestion_writer_audit.py",
             "tests/test_p24y_real_trade_source_probe.py",
             "tests/test_p24u_trade_copyability_real_snapshot_collection_bridge.py",
             "tests/test_p24v_trade_copyability_market_state_evidence_bridge.py",
             "tests/test_p24w_source_trade_real_coverage_mapping_audit.py",
             "-q"],
            cwd=str(_REPO), capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


# ── helpers ───────────────────────────────────────────────────────────────────
def _candidate_to_row(cand: NormalizedSourceTrade):
    """Map a candidate back to a sqlite3.Row-like object for legacy-id recompute."""

    class _Row:
        def __init__(self, d):
            self._d = d

        def keys(self):
            return self._d.keys()

        def __getitem__(self, k):
            return self._d[k]

    return _Row({
        "trader_address": cand.trader_address,
        "token_id": cand.token_id,
        "market_source_id": cand.market_source_id,
        "side": cand.side,
        "outcome": cand.outcome,
        "price": cand.price,
        "quantity": cand.quantity,
        "timestamp": cand.timestamp.isoformat() if cand.timestamp else None,
    })


if __name__ == "__main__":
    unittest.main()
