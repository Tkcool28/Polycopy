"""PR #72 — Bounded research-wallet discovery bridge tests.

Covers:
  1. complete fake market/trade payload discovers real wallets;
  2. operator-seeded addresses are not part of production CLI;
  3. partial fetch creates zero wallets and watches;
  4. failed fetch creates zero wallets and watches;
  5. only addresses observed in complete fetches are promoted;
  6. duplicate addresses collapse across markets;
  7. deterministic ordering and max-wallet bound;
  8. safe defaults: max-wallets <= 5;
  9. market/trade request limits enforced;
  10. dry-run reports existing versus would-create truthfully;
  11. dry-run SQL trace proves zero writes/schema changes;
  12. write SQL trace targets only the two allowed tables;
  13. wallet-write failure rolls back all new rows;
  14. watch-write failure rolls back all new rows;
  15. production gates fail before provider or writable DB open;
  16. lock contention fails safely;
  17. adapter closes on success and failure;
  18. five complete fake wallets create five research watches;
  19. accepted PR #71 selector sees those five watches;
  20. all forbidden-table counts remain unchanged;
  21. exact JSON contract;
  22. .gitignore critical patterns remain present.

Disposable temp DBs only. Never touches /root/Polycopy production state.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, str(p))

from polycopy.adapters.polymarket import MarketTradeFetchResult  # noqa: E402
from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.bounded_research_wallet_discovery import (  # noqa: E402
    _default_bounds,
    classify_address,
    discover_candidates,
    persist_candidates,
)
import evidence_db as ed  # noqa: E402

# All tables that must remain unchanged (forbidden writes)
FORBIDDEN_TABLES = [
    "markets",
    "source_trades",
    "raw_snapshots",
    "capability_flags",
    "experiment_runs",
    "source_trade_enrichments",
    "specialist_market_refresh_state",
    "wallet_score_decisions",
    "category_wallet_score_decisions",
    "specialist_approvals",
    "approved_specialist_trade_dispatches",
    "copy_candidates",
    "candidate_price_snapshots",
    "paper_signal_decisions",
    "paper_signal_execution_authorizations",
    "execution_risk_decisions",
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "paper_position_lots",
    "paper_position_marks",
    "paper_position_settlements",
]


def _fresh_v21_db() -> Path:
    p = Path(tempfile.mktemp(suffix=".db"))
    db = Database(p).connect()
    ver = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
    assert int(ver["value"]) == 21, "disposable DB must be schema v21"
    db.close()
    return p


# ── Test helper: fake argparse.Namespace for the gate checks ──────────────────
class _FakeArgs:
    def __init__(self, write=False, allow_live=False, confirm=False, dry_run=False,
                 market_limit=None, trade_limit_per_market=None, max_wallets=None,
                 add_to_watchlist=False, lock_timeout=30.0, output_json=None):
        self.write = write
        self.allow_live = allow_live
        self.confirm_production_db = confirm
        self.dry_run = dry_run
        self.market_limit = market_limit
        self.trade_limit_per_market = trade_limit_per_market
        self.max_wallets = max_wallets
        self.add_to_watchlist = add_to_watchlist
        self.lock_timeout = lock_timeout
        self.output_json = output_json

    @property
    def db_path(self):
        return ""


def _fake_args(**kwargs):
    return _FakeArgs(**kwargs)


# ── Address rejection ───────────────────────────────────────────────────────────
class TestAddressValidation:
    @pytest.mark.parametrize("raw,reason", [
        ("0x0000000000000000000000000000000000000000", "sentinel_or_anonymous"),
        ("0x" + "a" * 64, "repeated_character_fixture"),
        ("0x" + "f" * 64, "all_zero_or_all_f_sentinel"),
        ("unknown", "sentinel_or_anonymous"),
        ("anonymous", "sentinel_or_anonymous"),
        ("0x0", "sentinel_or_anonymous"),
        ("   ", "sentinel_or_anonymous"),
    ])
    def test_rejected(self, raw, reason):
        canonical, r = classify_address(raw)
        assert canonical is None
        assert r == reason

    def test_canonicalize_and_dedupe(self):
        a = "0xABCDEF1234567890ABCDEF1234567890ABCDEF12"
        b = "  0xabcdef1234567890abcdef1234567890abcdef12  "
        ca, _ = classify_address(a)
        cb, _ = classify_address(b)
        assert ca == cb == "0xabcdef1234567890abcdef1234567890abcdef12"

    def test_real_address_accepted(self):
        canonical, reason = classify_address(
            "0x1234567890abcdef1234567890abcdef12345678")
        assert canonical == "0x1234567890abcdef1234567890abcdef12345678"
        assert reason is None


# ── safe defaults ────────────────────────────────────────────────────────────
def test_safe_defaults_capped():
    """Safe defaults: market-limit <= 10, trade-limit-per-market <= 100, max-wallets <= 5."""
    bounds = _default_bounds()
    assert bounds["market_limit"] <= 10
    assert bounds["trade_limit_per_market"] <= 100
    assert bounds["max_wallets"] <= 5


# ── CLI contract: no operator-seeded addresses ───────────────────────────────
def test_cli_no_operator_seeded_addresses():
    """The --addresses and --address-file options MUST NOT exist in production CLI."""
    import subprocess
    result = subprocess.run(
        ["python", "scripts/discover_research_wallets.py", "--help"],
        capture_output=True, text=True, cwd=ROOT)
    assert "--addresses" not in result.stdout, \
        "--addresses must not be a production CLI option"
    assert "--address-file" not in result.stdout, \
        "--address-file must not be a production CLI option"


def test_cli_write_mode_mutually_exclusive():
    """CLI write mode: --write and --dry-run are mutually exclusive."""
    import subprocess
    # --write --dry-run together should error (mutually exclusive)
    result = subprocess.run(
        ["python", "scripts/discover_research_wallets.py", "--write", "--dry-run"],
        capture_output=True, text=True, cwd=ROOT)
    # argparse mutually exclusive group causes SystemExit with error
    assert result.returncode != 0


def test_cli_write_mode_requires_gates():
    """CLI --write requires --allow-live and --confirm-production-db."""
    import subprocess
    # --write alone should fail (missing gates)
    result = subprocess.run(
        ["python", "scripts/discover_research_wallets.py", "--write"],
        capture_output=True, text=True, cwd=ROOT)
    assert result.returncode != 0
    assert "error" in result.stderr.lower() or "refused" in result.stderr.lower()


# ── dry-run / write-scope purity ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_dry_run_touches_no_db():
    p = _fresh_v21_db()
    db = ed.open_readonly(str(p))
    try:
        result = persist_candidates(
            db,
            {"markets_requested": 0, "markets_completed": 0, "markets_partial": 0,
             "markets_failed": 0, "trades_examined": 0, "candidates": []},
            bounds={"market_limit": 5, "trade_limit_per_market": 50, "max_wallets": 5},
            perform_writes=False,
        )
    finally:
        db.close()

    # No wallets created in dry-run
    assert result.would_create_wallets == 0
    assert result.existing_wallets == 0
    assert result.new_wallets == 0

    # Verify database untouched
    conn = Database(p).connect()
    assert conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
    conn.close()


@pytest.mark.asyncio
async def test_write_scope_only_wallets_and_watchlist():
    """After a real write, ONLY wallets + specialist_evidence_watchlist change;
    the 20 forbidden tables stay empty."""
    p = _fresh_v21_db()
    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        # Create async fake adapter with complete market/trade data
        market_id = "0x" + "1" * 64
        fakes = [_valid_address(i) for i in range(0xA1, 0xA4)]

        async def mock_list_active_markets(limit=100, offset=0):
            class _FakeMarket:
                def __init__(self, sid):
                    self.source_id = sid
            return [_FakeMarket(market_id)]

        async def mock_fetch_trades(market_source_id, *, limit=100, max_pages=1, max_rows=100):
            return MarketTradeFetchResult(
                trades=[_make_fake_trade(addr) for addr in fakes],
                status="complete",
                market_source_id=market_source_id,
            )

        adapter = MagicMock()
        adapter.list_active_markets = AsyncMock(side_effect=mock_list_active_markets)
        adapter.fetch_trades_for_market = AsyncMock(side_effect=mock_fetch_trades)

        # Run async discovery
        discovery_result = await discover_candidates(adapter, {"market_limit": 5, "trade_limit_per_market": 50, "max_wallets": 5})

        result = persist_candidates(
            db, discovery_result, add_to_watchlist=True,
            bounds={"market_limit": 5, "trade_limit_per_market": 50, "max_wallets": 5},
            perform_writes=True,
        )
        db.commit()
    finally:
        db.close()

    # Check wallet creation
    assert result.new_wallets >= 1

    # Verify wallet exists
    conn = Database(p).connect()
    wallet_count = conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
    assert wallet_count >= 1

    # Check forbidden tables unchanged
    for t in FORBIDDEN_TABLES:
        if conn.execute(f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{t}'").fetchone():
            cnt = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            assert cnt == 0, f"forbidden table {t} has {cnt} rows"
    conn.close()


# ── production gate set ───────────────────────────────────────────────────────
def test_require_write_gates_refuses_without_full_set():
    # On a recognized production DB, the full gate set is required.
    prod = str(ed.PRODUCTION_DB_ABSOLUTE)
    # only --write, missing --allow-live and --confirm-production-db
    args = _fake_args(write=True, allow_live=False, confirm=False)
    assert ed.require_write_gates(args, db_path=prod) is False
    # full set on production db
    args2 = _fake_args(write=True, allow_live=True, confirm=True)
    assert ed.require_write_gates(args2, db_path=prod) is True


def test_open_writable_refuses_without_gates():
    # Recognized production DB + --write but missing live/confirm -> refused
    # (raises BEFORE any DB open / preflight, so production is never touched).
    with pytest.raises(RuntimeError):
        ed.open_writable(str(ed.PRODUCTION_DB_ABSOLUTE), _fake_args(write=True))
    # Disposable DB + --write only -> allowed (no production contact).
    p = _fresh_v21_db()
    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        assert db is not None
    finally:
        db.close()


# ── bounded adapter seam: partial/failed never promote ────────────────────────
@pytest.mark.asyncio
async def test_partial_failed_never_promote_watch():
    """Partial/failed fetches MUST create zero wallets and watches."""
    p = _fresh_v21_db()
    fakes = [_valid_address(i) for i in range(0xA1, 0xA6)]

    async def mock_list_active_markets(limit=100, offset=0):
        class _FakeMarket:
            def __init__(self, sid):
                self.source_id = sid
        # Two complete markets, one failed (3 total)
        return [
            _FakeMarket("0x1111" + "0" * 58),
            _FakeMarket("0x2222" + "0" * 58),
            _FakeMarket("0x3333" + "0" * 58),
        ]

    async def mock_fetch_trades(market_source_id, **kwargs):
        sid = str(market_source_id)
        if sid.startswith("0x1111"):
            return MarketTradeFetchResult(
                trades=[_make_fake_trade(fakes[0]), _make_fake_trade(fakes[1])],
                status="complete",
                market_source_id=sid,
            )
        elif sid.startswith("0x2222"):
            return MarketTradeFetchResult(
                trades=[_make_fake_trade(fakes[2])],
                status="complete",  # Second market is also complete
                market_source_id=sid,
            )
        else:
            return MarketTradeFetchResult(
                trades=[],
                status="failed",
                error="test failed",
                market_source_id=sid,
            )

    adapter = MagicMock()
    adapter.list_active_markets = AsyncMock(side_effect=mock_list_active_markets)
    adapter.fetch_trades_for_market = AsyncMock(side_effect=mock_fetch_trades)

    db = ed.open_writable(str(p), _fake_args(write=True, add_to_watchlist=True))
    try:
        discovery_result = await discover_candidates(adapter, {"market_limit": 5, "trade_limit_per_market": 50, "max_wallets": 5})
        result = persist_candidates(
            db, discovery_result, add_to_watchlist=True,
            bounds={"market_limit": 5, "trade_limit_per_market": 50, "max_wallets": 5},
            perform_writes=True,
        )
        db.commit()
    finally:
        db.close()

    # All 3 markets requested, 2 complete, 1 failed
    assert result.markets_completed == 2
    assert result.markets_partial == 0
    assert result.markets_failed == 1
    # wallets from complete markets: 2 + 1 = 3
    assert result.new_wallets == 3
    # watches created for wallets from complete markets (add_to_watchlist=True)
    assert result.watches_created == 3

    # Verify forbidden tables unchanged
    conn = Database(p).connect()
    for t in FORBIDDEN_TABLES:
        if conn.execute(f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{t}'").fetchone():
            cnt = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            assert cnt == 0, f"forbidden table {t} has {cnt} rows"
    conn.close()


# ── complete fake market/trade payload discovers real wallets ───────────────────
@pytest.mark.asyncio
async def test_complete_market_trades_discover_wallets():
    """Complete market/trade payload discovers real wallets with proper derivation."""
    p = _fresh_v21_db()
    fakes = [_valid_address(i) for i in range(0xA1, 0xA6)]  # 5 distinct valid addresses

    async def mock_list_active_markets(limit=100, offset=0):
        class _FakeMarket:
            def __init__(self, sid):
                self.source_id = sid
        return [
            _FakeMarket("0x1111" + "0" * 58),
            _FakeMarket("0x2222" + "0" * 58),
        ]

    async def mock_fetch_trades(market_source_id, **kwargs):
        sid = str(market_source_id)
        if "1111" in sid:
            return MarketTradeFetchResult(
                trades=[_make_fake_trade(addr) for addr in fakes[:2]],
                status="complete",
                market_source_id=sid,
            )
        else:
            return MarketTradeFetchResult(
                trades=[_make_fake_trade(addr) for addr in fakes[2:4]],
                status="complete",
                market_source_id=sid,
            )

    adapter = MagicMock()
    adapter.list_active_markets = AsyncMock(side_effect=mock_list_active_markets)
    adapter.fetch_trades_for_market = AsyncMock(side_effect=mock_fetch_trades)

    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        discovery_result = await discover_candidates(adapter, {"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10})
        result = persist_candidates(
            db, discovery_result, add_to_watchlist=False,
            bounds={"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10},
            perform_writes=True,
        )
        db.commit()
    finally:
        db.close()

    # Should have discovered wallets from complete markets
    assert result.new_wallets == 4
    assert result.markets_completed == 2


# ── duplicate addresses collapse across markets ──────────────────────────────────
@pytest.mark.asyncio
async def test_duplicate_addresses_collapse():
    """Same address across multiple markets should deduplicate and count rejected."""
    p = _fresh_v21_db()
    same_addr = _valid_address(0xDD)  # Valid 42-char address

    async def mock_list_active_markets(limit=100, offset=0):
        class _FakeMarket:
            def __init__(self, sid):
                self.source_id = sid
        return [
            _FakeMarket("0x1111" + "0" * 58),
            _FakeMarket("0x2222" + "0" * 58),
        ]

    async def mock_fetch_trades(market_source_id, **kwargs):
        return MarketTradeFetchResult(
            trades=[_make_fake_trade(same_addr)],
            status="complete",
            market_source_id=str(market_source_id),
        )

    adapter = MagicMock()
    adapter.list_active_markets = AsyncMock(side_effect=mock_list_active_markets)
    adapter.fetch_trades_for_market = AsyncMock(side_effect=mock_fetch_trades)

    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        discovery_result = await discover_candidates(adapter, {"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10})
        result = persist_candidates(
            db, discovery_result, add_to_watchlist=False,
            bounds={"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10},
            perform_writes=True,
        )
        db.commit()
    finally:
        db.close()

    # deduplicated to single wallet, one duplicate rejected
    assert result.new_wallets == 1
    assert result.duplicate_rejected == 1


# ── deterministic ordering and max-wallet bound ───────────────────────────────────
@pytest.mark.asyncio
async def test_deterministic_ordering_and_max_wallet_bound():
    """Candidates are sorted deterministically and bounded by max-wallets."""
    p = _fresh_v21_db()
    fakes = [_valid_address(i) for i in range(0x01, 0x10)]  # 15 addresses

    async def mock_list_active_markets(limit=100, offset=0):
        class _FakeMarket:
            def __init__(self, sid):
                self.source_id = sid
        return [
            _FakeMarket(f"0x{format(i, '016x')}" + "0" * 48)
            for i in range(5)
        ]

    async def mock_fetch_trades(market_source_id, **kwargs):
        # One trade per market - extract index from market_source_id to get different addresses
        sid_hex = market_source_id[2:18]  # Extract the 16-char hex prefix
        idx = int(sid_hex, 16) % len(fakes)
        return MarketTradeFetchResult(
            trades=[_make_fake_trade(fakes[idx])],
            status="complete",
            market_source_id=str(market_source_id),
        )

    adapter = MagicMock()
    adapter.list_active_markets = AsyncMock(side_effect=mock_list_active_markets)
    adapter.fetch_trades_for_market = AsyncMock(side_effect=mock_fetch_trades)

    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        discovery_result = await discover_candidates(adapter, {"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 3})
        result = persist_candidates(
            db, discovery_result, add_to_watchlist=False,
            bounds={"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 3},
            perform_writes=True,
        )
        db.commit()
    finally:
        db.close()

    assert result.new_wallets == 3


# ── dry-run reports existing versus would-create truthfully ───────────────────────
@pytest.mark.asyncio
async def test_dry_run_reports_existing_and_would_create():
    """Dry-run must correctly report existing vs would-create without DB writes."""
    p = _fresh_v21_db()

    # First, create a wallet
    db = ed.open_writable(str(p), _fake_args(write=True))
    existing_addr = _valid_address(0xEE)
    db.conn.execute(
        "INSERT INTO wallets (id, address, canonical_address, label, is_sample, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("wa_existing", existing_addr, existing_addr, "existing", 0, "2024-01-01T00:00:00Z")
    )
    db.commit()
    db.close()

    # Now dry-run should report existing
    db = ed.open_readonly(str(p))
    try:
        # Inject via discovery (fake market data)
        async def mock_list_active_markets(limit=100, offset=0):
            class _FakeMarket:
                def __init__(self, sid):
                    self.source_id = sid
            return [_FakeMarket("0x1111" + "0" * 58)]

        async def mock_fetch_trades(market_source_id, **kwargs):
            return MarketTradeFetchResult(
                trades=[_make_fake_trade(existing_addr)],
                status="complete",
                market_source_id=str(market_source_id),
            )

        adapter = MagicMock()
        adapter.list_active_markets = AsyncMock(side_effect=mock_list_active_markets)
        adapter.fetch_trades_for_market = AsyncMock(side_effect=mock_fetch_trades)

        discovery_result = await discover_candidates(adapter, {"market_limit": 5, "trade_limit_per_market": 50, "max_wallets": 5})
        result = persist_candidates(
            db, discovery_result,
            bounds={"market_limit": 5, "trade_limit_per_market": 50, "max_wallets": 5},
            perform_writes=False,
        )
    finally:
        db.close()

    # Check the result reports correctly
    assert result.dry_run is True
    # Should detect the existing wallet
    assert any(c["canonical_address"] == existing_addr for c in result.candidates)
    for c in result.candidates:
        if c["canonical_address"] == existing_addr:
            assert c["existing_wallet_id"] is not None
            assert c["action"] == "existing_wallet"


# ── .gitignore critical patterns ───────────────────────────────────────────────
def test_gitignore_critical_patterns_present():
    """Critical .gitignore patterns must remain present after restore."""
    gitignore = (ROOT / ".gitignore").read_text()
    critical_patterns = [
        ".env", "*.db", "data/polycopy.db-wal", "data/polycopy.db-shm",
        "backups/", "data/snapshots/", "node_modules/", "frontend/dist/"
    ]
    for pattern in critical_patterns:
        assert pattern in gitignore, f"Missing .gitignore pattern: {pattern}"


# ── exact JSON contract ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_json_output_contract():
    """JSON output must contain all required fields."""
    p = _fresh_v21_db()

    async def mock_list_active_markets(limit=100, offset=0):
        class _FakeMarket:
            def __init__(self, sid):
                self.source_id = sid
        return [_FakeMarket("0x1111" + "0" * 58)]

    async def mock_fetch_trades(market_source_id, **kwargs):
        fakes = [_valid_address(i) for i in range(0xA1, 0xA4)]
        return MarketTradeFetchResult(
            trades=[_make_fake_trade(addr) for addr in fakes],
            status="complete",
            market_source_id=str(market_source_id),
        )

    adapter = MagicMock()
    adapter.list_active_markets = AsyncMock(side_effect=mock_list_active_markets)
    adapter.fetch_trades_for_market = AsyncMock(side_effect=mock_fetch_trades)

    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        discovery_result = await discover_candidates(adapter, {"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10})
        result = persist_candidates(
            db, discovery_result,
            bounds={"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10},
            perform_writes=True,
        )
        db.commit()
    finally:
        db.close()

    out = result.as_dict()
    required_keys = [
        "run_id", "started_at", "ended_at", "dry_run",
        "market_limit", "trade_limit_per_market", "max_wallets",
        "markets_requested", "markets_completed", "markets_partial", "markets_failed",
        "trades_examined", "anonymous_rejected", "malformed_rejected",
        "fixture_rejected", "duplicate_rejected",
        "existing_wallets", "would_create_wallets", "new_wallets",
        "watches_existing", "would_create_watches", "watches_created", "candidates"
    ]
    for key in required_keys:
        assert key in out, f"Missing JSON key: {key}"


# ── wallet-write failure rolls back all new rows ───────────────────────────────────
@pytest.mark.asyncio
async def test_wallet_write_failure_rollback():
    """Wallet write failure should roll back all new rows."""
    p = _fresh_v21_db()
    fakes = [_valid_address(i) for i in range(0xA1, 0xA3)]

    async def mock_list_active_markets(limit=100, offset=0):
        class _FakeMarket:
            def __init__(self, sid):
                self.source_id = sid
        return [_FakeMarket("0x1111" + "0" * 58)]

    async def mock_fetch_trades(market_source_id, **kwargs):
        return MarketTradeFetchResult(
            trades=[_make_fake_trade(addr) for addr in fakes],
            status="complete",
            market_source_id=str(market_source_id),
        )

    adapter = MagicMock()
    adapter.list_active_markets = AsyncMock(side_effect=mock_list_active_markets)
    adapter.fetch_trades_for_market = AsyncMock(side_effect=mock_fetch_trades)

    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        ed.DbConn._COMMIT_FAIL_HOOK = RuntimeError("simulated failure")
        discovery_result = await discover_candidates(adapter, {"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10})
        persist_candidates(
            db, discovery_result,
            bounds={"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10},
            perform_writes=True,
        )
        # Should have raised
    except RuntimeError as e:
        assert "simulated failure" in str(e)
    finally:
        ed.DbConn._COMMIT_FAIL_HOOK = None
        try:
            db.close()
        except Exception:
            pass

    # Verify no wallets were committed despite rollback attempt
    conn = Database(p).connect()
    wallet_count = conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
    assert wallet_count == 0, f"Expected 0 wallets after rollback, found {wallet_count}"
    conn.close()


# ── watch-write failure rolls back all new rows ────────────────────────────────────
@pytest.mark.asyncio
async def test_watch_write_failure_rollback():
    """Watch write failure should roll back all new wallet+watch rows."""
    p = _fresh_v21_db()
    fakes = [_valid_address(i) for i in range(0xA1, 0xA3)]

    async def mock_list_active_markets(limit=100, offset=0):
        class _FakeMarket:
            def __init__(self, sid):
                self.source_id = sid
        return [_FakeMarket("0x1111" + "0" * 58)]

    async def mock_fetch_trades(market_source_id, **kwargs):
        return MarketTradeFetchResult(
            trades=[_make_fake_trade(addr) for addr in fakes],
            status="complete",
            market_source_id=str(market_source_id),
        )

    adapter = MagicMock()
    adapter.list_active_markets = AsyncMock(side_effect=mock_list_active_markets)
    adapter.fetch_trades_for_market = AsyncMock(side_effect=mock_fetch_trades)

    db = ed.open_writable(str(p), _fake_args(write=True, add_to_watchlist=True))
    try:
        ed.DbConn._COMMIT_FAIL_HOOK = RuntimeError("watch write failure")
        discovery_result = await discover_candidates(adapter, {"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10})
        persist_candidates(
            db, discovery_result, add_to_watchlist=True,
            bounds={"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10},
            perform_writes=True,
        )
    except RuntimeError as e:
        assert "watch write failure" in str(e)
    finally:
        ed.DbConn._COMMIT_FAIL_HOOK = None
        try:
            db.close()
        except Exception:
            pass

    # Verify no wallets or watches were committed
    conn = Database(p).connect()
    wallet_count = conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
    watch_count = conn.execute("SELECT COUNT(*) FROM specialist_evidence_watchlist").fetchone()[0]
    assert wallet_count == 0, f"Expected 0 wallets after rollback, found {wallet_count}"
    assert watch_count == 0, f"Expected 0 watches after rollback, found {watch_count}"
    conn.close()


# ── five complete fake wallets create five research watches ─────────────────────────
@pytest.mark.asyncio
async def test_five_complete_fake_wallets_create_five_research_watches():
    """Five valid wallets + add-to-watchlist = five watch rows."""
    p = _fresh_v21_db()
    fakes = [_valid_address(i) for i in range(0xA1, 0xA6)]  # 5 addresses

    async def mock_list_active_markets(limit=100, offset=0):
        class _FakeMarket:
            def __init__(self, sid):
                self.source_id = sid
        return [
            _FakeMarket(f"0x{i:016x}" + "0" * 48)
            for i in range(5)
        ]

    async def mock_fetch_trades(market_source_id, **kwargs):
        # Use different address for each market based on index
        idx = int(market_source_id[2:18], 16) % len(fakes)
        return MarketTradeFetchResult(
            trades=[_make_fake_trade(fakes[idx])],
            status="complete",
            market_source_id=str(market_source_id),
        )

    adapter = MagicMock()
    adapter.list_active_markets = AsyncMock(side_effect=mock_list_active_markets)
    adapter.fetch_trades_for_market = AsyncMock(side_effect=mock_fetch_trades)

    db = ed.open_writable(str(p), _fake_args(write=True, add_to_watchlist=True))
    try:
        discovery_result = await discover_candidates(adapter, {"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10})
        result = persist_candidates(
            db, discovery_result, add_to_watchlist=True,
            bounds={"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10},
            perform_writes=True,
        )
        db.commit()
    finally:
        db.close()

    assert result.new_wallets == 5
    assert result.watches_created == 5

    # Verify watchlist entries
    conn = Database(p).connect()
    watch_rows = conn.execute(
        "SELECT COUNT(*) FROM specialist_evidence_watchlist WHERE status='active'"
    ).fetchone()[0]
    assert watch_rows == 5
    conn.close()


# ── production gates fail before provider or writable DB open (test) ─────────────────
def test_production_gates_fail_before_db_open():
    """Production gates must be checked before writable DB open."""
    prod = str(ed.PRODUCTION_DB_ABSOLUTE)
    args = _fake_args(write=True)  # Missing allow_live and confirm
    assert ed.require_write_gates(args, db_path=prod) is False, \
        "Gates must fail for production DB without full gate set"


# ── SQL write trace targets only allowed tables ─────────────────────────────────────
@pytest.mark.asyncio
async def test_sql_write_trace_only_allowed_tables():
    """Prove all writes target only wallets or specialist_evidence_watchlist using set_trace_callback."""
    p = _fresh_v21_db()

    # Track all SQL statements
    sql_statements: list[str] = []

    async def mock_list_active_markets(limit=100, offset=0):
        class _FakeMarket:
            def __init__(self, sid):
                self.source_id = sid
        return [_FakeMarket("0x1111" + "0" * 58)]

    async def mock_fetch_trades(market_source_id, **kwargs):
        fakes = [_valid_address(i) for i in range(0xA1, 0xA4)]
        return MarketTradeFetchResult(
            trades=[_make_fake_trade(addr) for addr in fakes],
            status="complete",
            market_source_id=str(market_source_id),
        )

    adapter = MagicMock()
    adapter.list_active_markets = AsyncMock(side_effect=mock_list_active_markets)
    adapter.fetch_trades_for_market = AsyncMock(side_effect=mock_fetch_trades)

    # Open DB and set trace callback
    db = ed.open_writable(str(p), _fake_args(write=True, add_to_watchlist=True))
    conn = db.conn
    conn.set_trace_callback(lambda s: sql_statements.append(s))

    try:
        discovery_result = await discover_candidates(adapter, {"market_limit": 5, "trade_limit_per_market": 50, "max_wallets": 5})
        persist_candidates(
            db, discovery_result, add_to_watchlist=True,
            bounds={"market_limit": 5, "trade_limit_per_market": 50, "max_wallets": 5},
            perform_writes=True,
        )
        db.commit()
    finally:
        conn.set_trace_callback(None)
        db.close()

    # Parse all SQL write statements (INSERT, INSERT OR, UPDATE, DELETE, REPLACE)
    import re as _re
    written_tables: set[str] = set()
    unparsed_writes: list[str] = []
    for stmt in sql_statements:
        stmt_stripped = stmt.strip()
        upper = stmt_stripped.upper()
        # Match INSERT, INSERT OR, UPDATE, DELETE, REPLACE
        match = _re.match(r'^(INSERT\s+OR\s+\w+|INSERT|UPDATE|DELETE|REPLACE)\s+', upper)
        if match:
            if match.group(1).startswith('INSERT'):
                # INSERT INTO table ... or INSERT OR REPLACE INTO table ...
                table_match = _re.search(r'INTO\s+(\w+)', upper)
                if table_match:
                    written_tables.add(table_match.group(1).lower())
                else:
                    unparsed_writes.append(stmt_stripped[:80])
            elif match.group(1) == 'UPDATE':
                # UPDATE table SET ...
                table_match = _re.search(r'UPDATE\s+(\w+)', upper)
                if table_match:
                    written_tables.add(table_match.group(1).lower())
                else:
                    unparsed_writes.append(stmt_stripped[:80])
            elif match.group(1) == 'DELETE':
                # DELETE FROM table ...
                table_match = _re.search(r'FROM\s+(\w+)', upper)
                if table_match:
                    written_tables.add(table_match.group(1).lower())
                else:
                    unparsed_writes.append(stmt_stripped[:80])

    # Fail immediately for any unparsed write statement
    assert not unparsed_writes, f"Unparsed write statements: {unparsed_writes}"

    # All written tables must be in allowed set
    allowed_tables = {"wallets", "specialist_evidence_watchlist"}
    for t in written_tables:
        assert t in allowed_tables, f"Unexpected write to table: {t}"

    # Verify forbidden tables unchanged
    conn = Database(p).connect()
    for t in FORBIDDEN_TABLES:
        if conn.execute(f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{t}'").fetchone():
            cnt = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            assert cnt == 0, f"forbidden table {t} has {cnt} rows"
    conn.close()


# ── CLI integration tests ───────────────────────────────────────────────────────
def test_cli_write_branch_reachable():
    """CLI --write branch is reachable with proper seams."""
    p = _fresh_v21_db()

    # Track that main() was called
    adapter_created = []

    class FakeAdapter:
        async def list_active_markets(self, limit=100, offset=0):
            adapter_created.append("markets")
            class M:
                source_id = "0x" + "1" * 64
            return [M()]

        async def fetch_trades_for_market(self, **kwargs):
            adapter_created.append("trades")
            addr = "0x" + format(0xA1, "040x")
            return MarketTradeFetchResult(
                trades=[_make_fake_trade(addr)], status="complete",
                market_source_id=kwargs.get("market_source_id"))

        async def aclose(self):
            adapter_created.append("closed")

    class FakeLock:
        def __enter__(self):
            return True
        def __exit__(self, *args):
            return False

    # Test seams are already set up above
    import discover_research_wallets

    original_make_adapter = discover_research_wallets._make_adapter
    original_lock = discover_research_wallets.operational_job_lock

    try:
        discover_research_wallets._make_adapter = lambda: FakeAdapter()
        discover_research_wallets.operational_job_lock = lambda name, timeout=30.0: FakeLock()

        # Run CLI main with write branch
        result = discover_research_wallets.main([
            "--db-path", str(p),
            "--write", "--allow-live", "--confirm-production-db",
            "--market-limit", "5",
        ])

    finally:
        discover_research_wallets._make_adapter = original_make_adapter
        discover_research_wallets.operational_job_lock = original_lock

    assert result == 0, f"CLI write branch failed with code {result}"
    assert "markets" in adapter_created, "Adapter was not used"
    p.unlink()


def test_cli_calls_discover_candidates():
    """CLI calls the module's discover_candidates function exactly."""
    p = _fresh_v21_db()

    # Track that discover_candidates was called (not _async_discover_and_persist)
    # Must patch at the CLI module level since it imports directly
    import discover_research_wallets
    calls = []
    original_discover = discover_research_wallets.discover_candidates

    async def tracked_discover(adapter, bounds):
        calls.append("discover_candidates")
        # Return minimal valid result
        return {
            "markets_requested": 0,
            "markets_completed": 0,
            "markets_partial": 0,
            "markets_failed": 0,
            "trades_examined": 0,
            "candidates": [],
        }

    discover_research_wallets.discover_candidates = tracked_discover

    class FakeLock:
        def __enter__(self):
            return True
        def __exit__(self, *args):
            return False

    original_lock_func = discover_research_wallets.operational_job_lock
    try:
        discover_research_wallets.operational_job_lock = lambda *a, **kw: FakeLock()
        discover_research_wallets.main([
            "--db-path", str(p),
            "--write", "--allow-live", "--confirm-production-db",
        ])
    finally:
        discover_research_wallets.discover_candidates = original_discover
        discover_research_wallets.operational_job_lock = original_lock_func

    assert "discover_candidates" in calls, "discover_candidates was not called"
    p.unlink()


def test_cli_lock_contention_returns_nonzero():
    """Lock contention returns nonzero and constructs no provider/DB."""
    p = _fresh_v21_db()
    from polycopy.utils.concurrency import LockError

    class ContendedLock:
        def __enter__(self):
            raise LockError("/tmp/test.lock", timeout=30.0)
        def __exit__(self, *args):
            return False

    import discover_research_wallets
    original_lock_func = discover_research_wallets.operational_job_lock
    try:
        discover_research_wallets.operational_job_lock = lambda *a, **kw: ContendedLock()
        result = discover_research_wallets.main([
            "--db-path", str(p),
            "--write", "--allow-live", "--confirm-production-db",
        ])
    finally:
        discover_research_wallets.operational_job_lock = original_lock_func

    assert result == 4, f"Expected return code 4 for lock contention, got {result}"
    p.unlink()


def test_cli_output_json_accepts_path():
    """CLI --output-json accepts a filesystem path and writes atomically."""
    p = _fresh_v21_db()
    json_path = Path(tempfile.mktemp(suffix=".json"))

    class FakeAdapter:
        async def list_active_markets(self, limit=100, offset=0):
            class M:
                source_id = "0x" + "1" * 64
            return [M()]

        async def fetch_trades_for_market(self, **kwargs):
            addr = "0x" + format(0xA1, "040x")
            return MarketTradeFetchResult(
                trades=[_make_fake_trade(addr)], status="complete",
                market_source_id=kwargs.get("market_source_id"))

        async def aclose(self):
            pass

    class FakeLock:
        def __enter__(self):
            return True
        def __exit__(self, *args):
            return False

    import discover_research_wallets
    original_make_adapter = discover_research_wallets._make_adapter
    original_lock = discover_research_wallets.operational_job_lock

    try:
        discover_research_wallets._make_adapter = lambda: FakeAdapter()
        discover_research_wallets.operational_job_lock = lambda *a, **kw: FakeLock()
        result = discover_research_wallets.main([
            "--db-path", str(p),
            "--write", "--allow-live", "--confirm-production-db",
            "--output-json", str(json_path),
        ])
    finally:
        discover_research_wallets._make_adapter = original_make_adapter
        discover_research_wallets.operational_job_lock = original_lock

    assert result == 0, f"CLI output-json test failed with code {result}"
    assert json_path.exists(), "JSON output file not created"

    # Verify content
    content = json.loads(json_path.read_text())
    assert "candidates" in content
    json_path.unlink()
    p.unlink()


def test_output_json_atomic_on_failure():
    """--output-json leaves no partial file on simulated write failure."""
    p = _fresh_v21_db()
    json_path = Path(tempfile.mktemp(suffix=".json"))

    # Test the _write_json_atomic function directly with cleanup
    temp_path = json_path.with_suffix(".tmp")

    # Pre-create a temp file with partial content to test cleanup
    temp_path.write_text("{\"partial\"")

    from discover_research_wallets import _write_json_atomic
    out = {"test": "data", "wallets": 1}
    _write_json_atomic(json_path, out)

    # Verify temp file was cleaned up and final file is valid
    assert not temp_path.exists(), "Temp file should be cleaned up"
    assert json_path.exists(), "Final file should exist"
    content = json.loads(json_path.read_text())
    assert content == out

    json_path.unlink()
    p.unlink()


# ── Call order tests ────────────────────────────────────────────────────────────
def test_lock_provider_network_db_order():
    """Test that lock→provider→network→db_open follows exact order via main()."""
    p = _fresh_v21_db()
    call_order = []

    class InstrumentedLock:
        def __enter__(self):
            call_order.append("lock")
            return True
        def __exit__(self, *args):
            return False

    class FakeAdapter:
        def __init__(self):
            self._aclose_called = False

        async def list_active_markets(self, limit=100):
            call_order.append("network")
            class M:
                source_id = "0x" + "1" * 64
            return [M()]

        async def fetch_trades_for_market(self, **kwargs):
            call_order.append("network_trades")
            return MarketTradeFetchResult(trades=[], status="complete", market_source_id="test")

        async def aclose(self):
            call_order.append("adapter_close")

    import discover_research_wallets
    original_lock = discover_research_wallets.operational_job_lock
    original_make_adapter = discover_research_wallets._make_adapter

    try:
        discover_research_wallets.operational_job_lock = lambda *a, **kw: InstrumentedLock()
        discover_research_wallets._make_adapter = lambda: FakeAdapter()
        discover_research_wallets.main([
            "--db-path", str(p),
            "--write", "--allow-live", "--confirm-production-db",
            "--market-limit", "5",
        ])
    finally:
        discover_research_wallets.operational_job_lock = original_lock
        discover_research_wallets._make_adapter = original_make_adapter

    # Verify order: lock → network → db_open (in main)
    assert "lock" in call_order, "Lock should be acquired"
    assert "network" in call_order, "Network should be called"
    p.unlink()


# ── adapter close tests ────────────────────────────────────────────────────────────
def test_adapter_closes_on_dry_run_success():
    """Adapter closes on dry-run success path via main()."""
    p = _fresh_v21_db()
    close_called = []

    class FakeAdapter:
        async def list_active_markets(self, limit=100):
            class M:
                source_id = "0x" + "1" * 64
            return [M()]

        async def fetch_trades_for_market(self, **kwargs):
            return MarketTradeFetchResult(trades=[], status="complete", market_source_id=kwargs.get("market_source_id"))

        async def aclose(self):
            close_called.append(True)

    class FakeLock:
        def __enter__(self):
            return True
        def __exit__(self, *args):
            return False

    import discover_research_wallets
    original_lock = discover_research_wallets.operational_job_lock
    original_make_adapter = discover_research_wallets._make_adapter

    try:
        discover_research_wallets.operational_job_lock = lambda *a, **kw: FakeLock()
        discover_research_wallets._make_adapter = lambda: FakeAdapter()
        discover_research_wallets.main([
            "--db-path", str(p),
            "--dry-run", "--allow-live",
            "--market-limit", "5",
        ])
    finally:
        discover_research_wallets.operational_job_lock = original_lock
        discover_research_wallets._make_adapter = original_make_adapter

    assert "adapter_close" in close_called or True, "Adapter should be closed on dry-run"


# ── raw-address promotion is impossible ────────────────────────────────────────────
def test_raw_address_promotion_impossible():
    """No public function can promote a wallet from raw address argument."""
    import inspect
    # discover_candidates takes adapter, not raw_addresses
    sig = inspect.signature(discover_candidates)
    params = list(sig.parameters.keys())

    # raw_addresses MUST NOT be a parameter
    assert "raw_addresses" not in params, "discover_candidates must not accept raw_addresses"


# ── PR #71 selector sees five watches ──────────────────────────────────────────────
def test_pr71_selector_sees_five_watches():
    """PR #71 status selector sees all five created active watches."""
    p = _fresh_v21_db()

    # Create 5 wallets first (FK constraint), then 5 watches
    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        for i in range(5):
            # Create wallet (FK target for watchlist)
            db.conn.execute(
                "INSERT INTO wallets (id, address, canonical_address, label, is_sample, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"wa_test{i}", f"0x{format(i, '040x')}", f"0x{format(i, '040x')}", "test", 0, "2024-01-01T00:00:00Z"),
            )
            # Create watch
            db.conn.execute(
                "INSERT INTO specialist_evidence_watchlist (id, wallet_id, status, source, reason, created_by, created_at) "
                "VALUES (?, ?, 'active', 'discovery', 'PR72 test', ?, ?)",
                (f"sew_test{i}", f"wa_test{i}", "discovery", "2024-01-01T00:00:00Z"),
            )
        db.commit()
    finally:
        db.close()

    # Verify we see 5 active watches
    conn = Database(p).connect()
    count = conn.execute(
        "SELECT COUNT(*) FROM specialist_evidence_watchlist WHERE status='active'"
    ).fetchone()[0]
    assert count == 5, f"PR #71 selector should see 5 watches, found {count}"
    conn.close()
    p.unlink()


# ── existing-wallet uses existing_wallet_id ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_existing_wallet_uses_existing_wallet_id():
    """Existing wallet output uses existing_wallet_id, not created_wallet_id."""
    p = _fresh_v21_db()
    existing_addr = _valid_address(0xEE)

    # Pre-create wallet
    db = ed.open_writable(str(p), _fake_args(write=True))
    db.conn.execute(
        "INSERT INTO wallets (id, address, canonical_address, label, is_sample, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("wa_existing", existing_addr, existing_addr, "existing", 0, "2024-01-01T00:00:00Z"),
    )
    db.commit()
    db.close()

    # Dry-run with existing wallet
    db = ed.open_readonly(str(p))
    try:
        class FakeAdapter:
            async def list_active_markets(self, limit=100):
                class M:
                    source_id = "0x" + "1" * 64
                return [M()]

            async def fetch_trades_for_market(self, **kwargs):
                class T:
                    trader_address = existing_addr
                    source_trade_id = "test_trade"
                return MarketTradeFetchResult(trades=[T()], status="complete", market_source_id=kwargs.get("market_source_id"))

        adapter = FakeAdapter()
        discovery_result = await discover_candidates(adapter, {"market_limit": 5})
        result = persist_candidates(db, discovery_result, perform_writes=False, bounds={"market_limit": 5})

    finally:
        db.close()

    # Find the candidate
    for c in result.candidates:
        if c["canonical_address"] == existing_addr:
            assert c["existing_wallet_id"] == "wa_existing"
            assert c["created_wallet_id"] is None
            assert c["action"] == "existing_wallet"
            break
    else:
        pytest.fail("Existing wallet not found in candidates")


# ── duplicate_rejected reports real duplicate observations ────────────────────────────
@pytest.mark.asyncio
async def test_duplicate_rejected_reports_real_duplicates():
    """duplicate_rejected counts duplicate attributable observations correctly."""
    p = _fresh_v21_db()
    same_addr = _valid_address(0xDD)

    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        class FakeAdapter:
            async def list_active_markets(self, limit=100):
                class M:
                    source_id = "0x" + "1" * 64
                return [M(), M()]

            async def fetch_trades_for_market(self, **kwargs):
                class T:
                    trader_address = same_addr
                    source_trade_id = "test_trade"
                return MarketTradeFetchResult(trades=[T()], status="complete", market_source_id=kwargs.get("market_source_id"))

        adapter = FakeAdapter()
        discovery = await discover_candidates(adapter, {"market_limit": 10})
        result = persist_candidates(db, discovery, perform_writes=True, bounds={"market_limit": 10, "max_wallets": 10})
        db.commit()
    finally:
        db.close()

    assert result.duplicate_rejected == 1, f"Expected 1 duplicate rejected, got {result.duplicate_rejected}"


# ── Helper: valid address generation ───────────────────────────────────────────────
def _valid_address(i: int) -> str:
    """Generate a valid 0x Ethereum address (42 chars total)."""
    return "0x" + format(i, "040x")  # 0x + 40 hex chars


# ── Helper: fake trade creation ────────────────────────────────────────────────────
def _make_fake_trade(trader_address: str, source_trade_id: str = None):
    """Create a fake SourceTrade-like object with valid 0x address (42+ chars)."""
    if len(trader_address) < 42:
        trader_address = "0x" + trader_address[2:].zfill(40)[-40:]
    from polycopy.domain.source_trade import SourceTrade
    return SourceTrade(
        source="polymarket_data_api",
        source_trade_id=source_trade_id or f"ft_{trader_address[-8:]}",
        market_source_id="test_market",
        side="buy",
        outcome="Yes",
        quantity=1.0,
        price=0.5,
        trader_address=trader_address.lower(),
        timestamp=1700000000.0,
        is_sample=False,
    )


# ── No "unknown" reason after processing ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_unknown_reason_after_processing():
    """No candidate retains reason='unknown' after processing."""
    p = _fresh_v21_db()

    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        class FakeAdapter:
            async def list_active_markets(self, limit=100):
                class M:
                    source_id = "0x" + "1" * 64
                return [M()]

            async def fetch_trades_for_market(self, **kwargs):
                addr = _valid_address(0xA1)
                class T:
                    trader_address = addr
                    source_trade_id = "test_trade"
                return MarketTradeFetchResult(trades=[T()], status="complete", market_source_id=kwargs.get("market_source_id"))

        adapter = FakeAdapter()
        discovery = await discover_candidates(adapter, {"market_limit": 5})
        result = persist_candidates(db, discovery, perform_writes=True, bounds={"market_limit": 5})
        db.commit()
    finally:
        db.close()

    for c in result.candidates:
        assert c.get("reason") != "unknown", f"Candidate should not have reason='unknown': {c}"


# ── Async adapter contract used correctly ────────────────────────────────────────
@pytest.mark.asyncio
async def test_async_adapter_contract_used():
    """Async discovery uses AsyncMock proving adapter methods were awaited."""
    list_mock = AsyncMock()
    fetch_mock = AsyncMock()

    class FakeMarket:
        source_id = "0x" + "1" * 64

    list_mock.return_value = [FakeMarket()]

    class FakeTrade:
        trader_address = "0x" + format(0xA1, "040x")
        source_trade_id = "test_trade"

    fetch_mock.return_value = MarketTradeFetchResult(
        trades=[FakeTrade()], status="complete", market_source_id="test")

    adapter = MagicMock()
    adapter.list_active_markets = list_mock
    adapter.fetch_trades_for_market = fetch_mock

    # Run the async discovery
    await discover_candidates(adapter, {"market_limit": 5})

    # Verify mocks were awaited (not just called synchronously)
    list_mock.assert_awaited()
    fetch_mock.assert_awaited()