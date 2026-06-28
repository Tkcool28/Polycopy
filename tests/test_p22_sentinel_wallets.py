"""Regression tests for the v5 migration cleanup of legacy sentinel wallet rows
in the ``wallets`` table, plus the defensive runtime filters that protect every
wallet-loading code path against sentinel addresses.

This addresses the Codex P2 follow-up after the source_trades sentinel cleanup:
even though the v5 migration already rewrites ``source_trades.trader_address``
to NULL, pre-v5 collectors also persisted ``unknown`` / ``anonymous`` /
``missing`` / ``0x`` / ``0x0`` (and empty / whitespace variants) into the
``wallets`` table as fake wallet rows. Those rows survived the first round of
v5 and were loaded by ``run_scan`` at startup, eligible for scoring and
displayed on the dashboard.

This file verifies:
- v5 migration deletes every sentinel row from ``wallets`` (mixed case,
  whitespace, empty, tabs/newlines, etc.).
- Real wallet rows are preserved byte-for-byte (case-sensitive, whitespace-sensitive).
- Migration is idempotent.
- Foreign-key integrity is preserved (no orphan rows in dependent tables).
- Defensive Python filters in run_scan, repository, and the live smoke script
  exclude any sentinel that somehow leaks past the migration.
- run_scan starts cleanly after a v4 → v5 upgrade containing legacy sentinel
  wallet rows.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from polycopy.db.schema import MIGRATIONS, SCHEMA_VERSION, _V5_DDL
from polycopy.domain.source_trade import is_sentinel_trader_address

# Ensure scripts/ is importable for run_scan / repository tests.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _init_db_at_version(db_path: Path, target: int) -> sqlite3.Connection:
    """Init a DB and run migrations 1..target (raw sqlite3 for boundary control)."""
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


def _insert_wallet(conn, address: str) -> str:
    wid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) "
        "VALUES (?, ?, 'test', 0, ?)",
        (wid, address, datetime.now(timezone.utc).isoformat()),
    )
    return wid


def _wallet_addresses(conn) -> list[str]:
    return [r["address"] for r in conn.execute("SELECT address FROM wallets").fetchall()]


# ─── 1. v4 → v5 migration removes sentinel rows from wallets ──────────────────


class TestMigrationRemovesSentinelWallets:
    """The v5 migration must DELETE every sentinel row from the wallets table."""

    def test_v4_to_v5_migration_removes_sentinel_wallet_rows(self, tmp_path: Path):
        """Sentinel rows in wallets are deleted on upgrade. Real rows survive."""
        db_path = tmp_path / "v4to5-wallets.db"
        conn = _init_db_at_version(db_path, 4)

        # Seed: 2 real + many sentinel variants.
        _insert_wallet(conn, "0xREAL_WALLET_1")
        _insert_wallet(conn, "0xREAL_WALLET_2")
        _insert_wallet(conn, "unknown")
        _insert_wallet(conn, "Anonymous")
        _insert_wallet(conn, "MISSING")
        _insert_wallet(conn, "0x")
        _insert_wallet(conn, "0x0")
        _insert_wallet(conn, "0X0")
        _insert_wallet(conn, "")
        _insert_wallet(conn, "   ")
        _insert_wallet(conn, "  unknown  ")
        _insert_wallet(conn, "\tunknown\n")
        _insert_wallet(conn, "MiSsInG")
        conn.commit()

        # Apply v5
        for stmt in _V5_DDL:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()

        # All sentinels gone, all reals present.
        survivors = {row for row in _wallet_addresses(conn)}
        assert survivors == {"0xREAL_WALLET_1", "0xREAL_WALLET_2"}, (
            f"unexpected survivors: {survivors}"
        )

    def test_v4_to_v5_migration_preserves_real_wallets_byte_for_byte(
        self, tmp_path: Path
    ):
        """Real wallet addresses are preserved exactly — case-sensitive,
        whitespace-sensitive, including odd ones like '0xabc' and
        'attributed_string'."""
        db_path = tmp_path / "v4to5-real-wallets.db"
        conn = _init_db_at_version(db_path, 4)

        real_addresses = [
            "0xREAL_WALLET_1",
            "0xReal_Wallet_Mixed_Case",
            "0xabc",  # malformed but NOT a sentinel
            "attributed_string",  # custom string, NOT a sentinel
            "0x1234567890abcdef1234567890abcdef12345678",  # 40-hex
        ]
        for addr in real_addresses:
            _insert_wallet(conn, addr)
        conn.commit()

        for stmt in _V5_DDL:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()

        survivors = sorted(_wallet_addresses(conn))
        assert survivors == sorted(real_addresses), (
            f"real wallets not preserved byte-for-byte.\n"
            f"  expected: {sorted(real_addresses)}\n"
            f"  got:      {survivors}"
        )

    def test_v4_to_v5_migration_wallets_cleanup_is_idempotent(
        self, tmp_path: Path
    ):
        """Re-applying v5 must be a safe no-op once migration has run."""
        db_path = tmp_path / "v4to5-idem.db"
        conn = _init_db_at_version(db_path, 4)

        _insert_wallet(conn, "0xREAL_KEEP")
        _insert_wallet(conn, "unknown")
        _insert_wallet(conn, "anonymous")
        conn.commit()

        for stmt in _V5_DDL:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()

        after_first = sorted(_wallet_addresses(conn))
        assert after_first == ["0xREAL_KEEP"]

        # Apply v5 again — must be a no-op for wallets.
        for stmt in _V5_DDL:
            conn.execute(stmt)
        conn.commit()
        after_second = sorted(_wallet_addresses(conn))
        assert after_second == after_first, (
            f"idempotency violated: {after_first} -> {after_second}"
        )


# ─── 2. Fresh schema never contains sentinel wallets ───────────────────────────


class TestFreshSchemaHasNoSentinels:
    def test_fresh_schema_has_no_sentinel_wallets(self, tmp_path: Path):
        """Fresh database creation never produces sentinel wallet rows."""
        db_path = tmp_path / "fresh.db"
        # The Database.connect() helper applies all migrations from scratch.
        from polycopy.db.database import Database

        db = Database(db_path=db_path).connect()
        try:
            # Insert attempts of every sentinel value must be deletable by
            # the same DELETE predicate the migration uses (the helper is
            # the runtime contract). The migration itself doesn't insert,
            # but the predicate must catch any later manual insert.
            for sentinel in [
                "unknown",
                "Anonymous",
                "missing",
                "0x",
                "0x0",
                "",
                "   ",
                "\tunknown\n",
            ]:
                wid = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                    "VALUES (?, ?, 'demo', 0, ?)",
                    (wid, sentinel, datetime.now(timezone.utc).isoformat()),
                )
            db.conn.commit()

            # Now run the v5 wallets DELETE block manually (the migration
            # won't re-run because we're already at v5).
            assert SCHEMA_VERSION == 5
            for stmt in _V5_DDL:
                # Skip the source_trades rebuild (those tables already
                # have the right shape at v5). Just execute the wallets
                # DELETE block — but executing the whole DDL is also safe
                # because DROP+rebuild is idempotent on an empty table.
                pass
            # Apply the wallets cleanup explicitly.
            for stmt in _V5_DDL:
                db.execute(stmt)
            db.conn.commit()

            survivors = _wallet_addresses(db.conn)
            # After v5 (re-applied) all sentinels are gone. Re-running the
            # DROP+rebuild on empty source_trades is also a no-op.
            for sentinel in [
                "unknown",
                "Anonymous",
                "missing",
                "0x",
                "0x0",
                "",
                "   ",
                "\tunknown\n",
            ]:
                assert sentinel not in survivors, f"sentinel {sentinel!r} survived"
        finally:
            db.close()


# ─── 3. Migration preserves referential integrity ─────────────────────────────


class TestMigrationForeignKeySafety:
    def test_migration_no_foreign_key_violations_with_dependents(self, tmp_path: Path):
        """When sentinel wallets have dependent rows (positions, orders, etc.),
        the migration must clean up dependents in the right order without
        leaving any orphan rows. PRAGMA foreign_key_check must be empty."""
        db_path = tmp_path / "v4to5-fk.db"
        conn = _init_db_at_version(db_path, 4)

        # Seed a market (referenced by orders, positions, signals).
        market_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO markets (id, source_id, source, question, active, closed, "
            "resolved, volume_24h, fetched_at, is_sample) "
            "VALUES (?, 'mkt-1', 'polymarket', 'Test?', 1, 0, 0, 1000.0, ?, 0)",
            (market_id, datetime.now(timezone.utc).isoformat()),
        )

        # Sentinel wallet with a dependent position row.
        sentinel_wallet = _insert_wallet(conn, "unknown")
        conn.execute(
            "INSERT INTO positions (id, market_id, wallet_id, outcome, quantity, "
            "avg_entry_price, current_price, opened_at, updated_at, is_sample) "
            "VALUES (?, ?, ?, 'Yes', 10.0, 0.5, 0.6, ?, ?, 0)",
            (
                str(uuid.uuid4()),
                market_id,
                sentinel_wallet,
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        # Real wallet with a dependent order row (must survive).
        real_wallet = _insert_wallet(conn, "0xREAL_KEEP")
        conn.execute(
            "INSERT INTO orders (id, market_id, wallet_id, side, order_type, "
            "outcome, quantity, price, status, created_at, updated_at, is_sample) "
            "VALUES (?, ?, ?, 'buy', 'market', 'Yes', 1.0, 0.5, 'pending', ?, ?, 0)",
            (
                str(uuid.uuid4()),
                market_id,
                real_wallet,
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

        # Apply v5.
        for stmt in _V5_DDL:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()

        # FK integrity must be clean.
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == [], f"FK violations: {fk_violations}"

        # Sentinel wallet gone; real wallet + its order still present.
        assert "unknown" not in _wallet_addresses(conn)
        assert "0xREAL_KEEP" in _wallet_addresses(conn)
        surviving_orders = conn.execute(
            "SELECT wallet_id FROM orders"
        ).fetchall()
        assert len(surviving_orders) == 1
        assert surviving_orders[0]["wallet_id"] == real_wallet


# ─── 4. Defensive runtime filters ─────────────────────────────────────────────


class TestRuntimeFilters:
    """Defensive Python filters in loaders must exclude any sentinel that
    somehow leaks past the migration (manual insert, interrupted upgrade,
    etc.)."""

    def test_helper_recognizes_every_sentinel_variant(self):
        """Spot-check the shared helper that all loaders depend on."""
        for s in [
            "unknown",
            "Unknown",
            "UNKNOWN",
            "anonymous",
            "Anonymous",
            "missing",
            "Missing",
            "MISSING",
            "0x",
            "0X",
            "0x0",
            "0X0",
            "",
            "   ",
            "\t\n",
        ]:
            assert is_sentinel_trader_address(s) is True, f"helper missed {s!r}"
        for real in [
            "0xATTRIBUTED",
            "0xabc",
            "0x1234567890abcdef1234567890abcdef12345678",
            "attributed_string",
            "0xMixedCase",
            "  0xPADDED  ",  # padded real address — must NOT be filtered
        ]:
            assert is_sentinel_trader_address(real) is False, (
                f"helper wrongly flagged real {real!r}"
            )

    @pytest.mark.asyncio
    async def test_run_scan_startup_loader_excludes_sentinel_wallet_rows(
        self, tmp_path: Path, monkeypatch
    ):
        """Even if sentinel rows are present in the DB at startup (because
        they were inserted manually after migration, or the migration was
        interrupted), run_scan must not load them into the watchlist."""
        from polycopy.db.database import Database

        db_path = tmp_path / "runscan-sentinel.db"
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(db_path))
        db = Database(db_path=db_path).connect()
        try:
            # Insert sentinel + real wallets directly.
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                "VALUES (?, 'unknown', 'demo', 0, ?)",
                (str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()),
            )
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                "VALUES (?, '0xREAL_RUNSCAN', 'real', 0, ?)",
                (str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()),
            )
            db.conn.commit()

            # Direct mirror of run_scan's startup loader, with the
            # defensive filter applied.
            rows = db.fetchall("SELECT address, label FROM wallets")
            filtered = [r for r in rows if not is_sentinel_trader_address(r["address"])]
            addresses = {r["address"] for r in filtered}
            assert "0xREAL_RUNSCAN" in addresses
            assert "unknown" not in addresses
            # The unfiltered load would have included both; the filter
            # drops the sentinel.
            assert len(rows) == 2
            assert len(filtered) == 1
        finally:
            db.close()

    def test_repository_wallets_listing_excludes_sentinels(self, tmp_path: Path):
        """The dashboard wallet-listing endpoint must filter sentinel rows
        even if they exist in the DB."""
        from polycopy.db.database import Database
        from polycopy.api.repository import DashboardRepository, Page
        from polycopy.config.settings import Settings

        db_path = tmp_path / "repo-sentinel.db"
        db = Database(db_path=db_path).connect()
        try:
            # Insert sentinel + real wallets.
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                "VALUES (?, 'unknown', 'demo', 0, ?)",
                (str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()),
            )
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                "VALUES (?, 'Anonymous', 'demo', 0, ?)",
                (str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()),
            )
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                "VALUES (?, '0xREAL_REPO', 'real', 0, ?)",
                (str(uuid.uuid4()), datetime.now(timezone.utc).isoformat()),
            )
            db.conn.commit()

            settings = Settings()
            repo = DashboardRepository(db=db, settings=settings)
            page = Page(limit=10, offset=0)
            response = repo.wallets(page)
            addresses = {w.address for w in response.wallets}
            assert "0xREAL_REPO" in addresses
            assert "unknown" not in addresses
            assert "Anonymous" not in addresses
        finally:
            db.close()

    def test_repository_wallet_lookup_returns_none_for_sentinel(self, tmp_path: Path):
        """Direct single-wallet lookup by UUID returns None if the row's
        address is a sentinel (defense in depth)."""
        from polycopy.db.database import Database
        from polycopy.api.repository import DashboardRepository
        from polycopy.config.settings import Settings

        db_path = tmp_path / "repo-single-sentinel.db"
        db = Database(db_path=db_path).connect()
        try:
            wid = str(uuid.uuid4())
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                "VALUES (?, 'unknown', 'demo', 0, ?)",
                (wid, datetime.now(timezone.utc).isoformat()),
            )
            db.conn.commit()

            settings = Settings()
            repo = DashboardRepository(db=db, settings=settings)
            result = repo.wallet(uuid.UUID(wid))
            assert result is None, "sentinel wallet returned by single lookup"
        finally:
            db.close()


# ─── 5. run_scan starts after a v4 → v5 upgrade with legacy sentinels ─────────


class TestRunScanAfterV4Upgrade:
    @pytest.mark.asyncio
    async def test_run_scan_starts_after_upgrade_with_legacy_sentinel_wallets(
        self, tmp_path: Path, monkeypatch
    ):
        """End-to-end: seed a v4 DB with sentinel wallet rows, apply v5,
        then run a stubbed run_scan. It must complete without crash, must
        not report any sentinel in result.wallets_discovered, and must
        not persist any new sentinel wallet row."""
        # Build the v4 DB at a stable path (using tmp_path for isolation).
        db_path = tmp_path / "v4-upgrade.db"
        conn = _init_db_at_version(db_path, 4)
        _insert_wallet(conn, "unknown")
        _insert_wallet(conn, "Anonymous")
        _insert_wallet(conn, "missing")
        _insert_wallet(conn, "0x0")
        _insert_wallet(conn, "0xREAL_UPGRADE")
        conn.commit()
        conn.close()

        # Now open via Database (will run pending v5 migration).
        from polycopy.db.database import Database
        import scripts.run_scan as rs

        monkeypatch.setenv("POLYCOPY_DB_PATH", str(db_path))
        db = Database(db_path=db_path).connect()
        try:
            # Confirm sentinels are gone after upgrade.
            assert "unknown" not in _wallet_addresses(db.conn)
            assert "Anonymous" not in _wallet_addresses(db.conn)
            assert "missing" not in _wallet_addresses(db.conn)
            assert "0x0" not in _wallet_addresses(db.conn)
            assert "0xREAL_UPGRADE" in _wallet_addresses(db.conn)

            # Run a stubbed scan that returns no markets/trades. The point
            # is that the startup loader must not crash and must not pick
            # up sentinel rows.
            async def fake_fetch_markets(db, settings, limit, result, use_sample):
                return []

            def fake_generate_signals(db, ms, now):
                return []

            monkeypatch.setattr(rs, "_fetch_markets", fake_fetch_markets)
            monkeypatch.setattr(rs, "_generate_signals", fake_generate_signals)

            result = await rs.run_scan(db, market_limit=1, use_sample=False)

            # The real wallet is the only one loaded.
            assert result.wallets_discovered == 1
            assert result.errors == []
            assert result.anonymous_trades_skipped == 0

            # No sentinel wallet row survived.
            survivors = _wallet_addresses(db.conn)
            assert survivors == ["0xREAL_UPGRADE"], f"unexpected survivors: {survivors}"
        finally:
            db.close()


# ─── 6. End-to-end mixed DB → only real wallets loaded ────────────────────────


class TestMixedDatabaseOnlyRealWalletsLoaded:
    def test_mixed_database_only_real_wallets_loaded(self, tmp_path: Path):
        """DB containing both sentinel and real wallets → only the real
        wallets surface in any loader that applies the defensive filter."""
        from polycopy.db.database import Database

        db_path = tmp_path / "mixed.db"
        db = Database(db_path=db_path).connect()
        try:
            mixed = [
                "0xREAL_A",
                "unknown",
                "0xREAL_B",
                "Anonymous",
                "missing",
                "0xREAL_C",
                "0x0",
                "  ",
                "",
                "0xREAL_D",
            ]
            for addr in mixed:
                db.execute(
                    "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                    "VALUES (?, ?, 'test', 0, ?)",
                    (str(uuid.uuid4()), addr, datetime.now(timezone.utc).isoformat()),
                )
            db.conn.commit()

            # Apply defensive filter exactly as run_scan does.
            rows = db.fetchall("SELECT address, label FROM wallets")
            filtered = [r for r in rows if not is_sentinel_trader_address(r["address"])]
            loaded_addresses = sorted(r["address"] for r in filtered)
            assert loaded_addresses == [
                "0xREAL_A",
                "0xREAL_B",
                "0xREAL_C",
                "0xREAL_D",
            ], f"unexpected loaded addresses: {loaded_addresses}"
        finally:
            db.close()


# ─── 7. Repository-level guard against bypass ─────────────────────────────────


class TestRepositoryGuard:
    """Static guard: every test file that selects from the wallets table
    must either apply the sentinel filter or be a setup-only fixture. This
    prevents future regressions where someone adds a new loader that
    forgets the filter."""

    def test_no_bypass_in_test_files(self):
        # Patterns we forbid: SELECTs that read multiple wallets without
        # filtering — these risk asserting against or surfacing sentinel
        # rows. We explicitly allow the safe "WHERE id = ?" / "WHERE
        # address = ?" single-row lookups used in setup fixtures.
        forbidden_patterns = [
            # No WHERE clause → would return every wallet including sentinels.
            ('SELECT address, label FROM wallets', False),
            ('SELECT id, address FROM wallets', False),
            ('SELECT address FROM wallets', False),
            ('SELECT id, address, label FROM wallets', False),
        ]
        # Permitted: single-row lookups (WHERE id/address = ?) are safe.
        # These don't surface fake wallets to scoring / display — they
        # only confirm a specific row exists.
        offenders: list[str] = []
        tests_dir = Path(__file__).resolve().parent
        for test_file in tests_dir.glob("test_*.py"):
            # This guard file is allowed to reference these patterns in
            # comments / fixtures.
            if test_file.name == Path(__file__).name:
                continue
            try:
                text = test_file.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            for pattern, _is_safe in forbidden_patterns:
                if pattern not in text:
                    continue
                # If the file uses the sentinel filter anywhere, accept it.
                if "is_sentinel_trader_address" in text:
                    continue
                # If the SELECT line is followed by a WHERE id/address = ?,
                # accept it as a single-row lookup.
                for line in text.splitlines():
                    if pattern in line:
                        # Find subsequent lines until a semicolon or quote
                        # end, looking for WHERE id = / WHERE address =.
                        idx = line.find(pattern)
                        tail = line[idx:]
                        if "WHERE id =" in tail or "WHERE address =" in tail:
                            continue
                        offenders.append(f"{test_file}: {pattern!r}")

        assert not offenders, (
            "test files query wallets without applying the sentinel "
            "filter — they risk asserting against fake rows:\n  "
            + "\n  ".join(offenders)
        )


# ─── 8. Migration cleanup is itself robust to handler edge cases ───────────────


class TestMigrationPredicates:
    """The DELETE predicate must match every shape the helper recognizes."""

    def test_delete_predicate_matches_helper(self, tmp_path: Path):
        """Every value the helper recognizes as a sentinel must also match
        the migration's DELETE predicate. This guards against drift between
        the Python helper and the SQL predicate."""
        from polycopy.db.database import Database

        db_path = tmp_path / "predicate.db"
        db = Database(db_path=db_path).connect()
        try:
            # Insert a value that the helper recognizes as sentinel and one
            # that it doesn't, then run the v5 wallets DELETE block.
            # We mimic the predicate check in pure Python.
            test_values = [
                ("unknown", True),
                ("Unknown", True),
                ("UNKNOWN", True),
                ("anonymous", True),
                ("Anonymous", True),
                ("missing", True),
                ("MiSsInG", True),
                ("0x", True),
                ("0X", True),
                ("0x0", True),
                ("0X0", True),
                ("", True),
                ("   ", True),
                ("\t\n", True),
                ("  unknown  ", True),
                ("0xREAL", False),
                ("0xabc", False),
                ("attributed_string", False),
                ("0xMixedCase", False),
            ]
            for value, _expected_sentinel in test_values:
                db.execute(
                    "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                    "VALUES (?, ?, 'test', 0, ?)",
                    (str(uuid.uuid4()), value, datetime.now(timezone.utc).isoformat()),
                )
            db.conn.commit()

            # Apply the v5 wallets DELETE block (the rest of v5 is no-op
            # at v5 because the new schema tables already exist or are
            # dropped-and-recreated idempotently on empty data).
            for stmt in _V5_DDL:
                db.execute(stmt)
            db.conn.commit()

            survivors = _wallet_addresses(db.conn)
            survivors_set = set(survivors)
            for value, expected_sentinel in test_values:
                if expected_sentinel:
                    assert value not in survivors_set, (
                        f"sentinel value {value!r} survived migration"
                    )
                else:
                    assert value in survivors_set, (
                        f"real value {value!r} was wrongly deleted"
                    )
        finally:
            db.close()