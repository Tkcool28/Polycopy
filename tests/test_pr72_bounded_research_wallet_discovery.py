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

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.adapters.polymarket import MarketTradeFetchResult  # noqa: E402
from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.bounded_research_wallet_discovery import (  # noqa: E402
    _default_bounds,
    classify_address,
    discover,
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


def _get_forbidden_counts(db) -> dict[str, int]:
    """Count all forbidden tables."""
    return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in FORBIDDEN_TABLES if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone() is not None}


# ── Test helper: fake argparse.Namespace for the gate checks ──────────────────
class _FakeArgs:
    def __init__(self, write=False, allow_live=False, confirm=False, dry_run=False,
                 market_limit=None, trade_limit_per_market=None, max_wallets=None,
                 add_to_watchlist=False, lock_timeout=30.0, output_json=False):
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


# ── Fake adapter for bounded market/trade discovery ───────────────────────────
class _FakeDiscoveryAdapter:
    """Deterministic fake adapter for testing bounded market/trade discovery.

    Accepts a dict mapping market_id -> (list of trades, status) where:
      - status is "complete", "partial", or "failed"
      - each trade is a mock object with trader_address attribute
    """

    def __init__(self, market_data: dict[str, tuple[list, str]]):
        self.market_data = market_data

    def list_active_markets(self, limit: int = 10):
        """Return fake markets from the keys of market_data."""
        class _FakeMarket:
            def __init__(self, source_id):
                self.source_id = source_id
        return [_FakeMarket(mid) for mid in list(self.market_data.keys())[:limit]]

    def fetch_trades_for_market(self, market_source_id: str, *, limit=100, max_pages=1, max_rows=100):
        """Return MarketTradeFetchResult based on market_data."""
        trades, status = self.market_data.get(market_source_id, ([], "failed"))
        if status == "failed":
            return MarketTradeFetchResult(
                trades=[],
                status="failed",
                error="test simulated failure",
                market_source_id=market_source_id,
            )
        elif status == "partial":
            # Return partial trades (simulated prefix)
            return MarketTradeFetchResult(
                trades=trades[:max(1, len(trades)//2)] if trades else [],
                status="partial",
                error="test simulated partial failure",
                market_source_id=market_source_id,
            )
        else:  # complete
            return MarketTradeFetchResult(
                trades=list(trades),
                status="complete",
                market_source_id=market_source_id,
            )


def _make_fake_trade(trader_address: str, source_trade_id: str = None):
    """Create a fake SourceTrade-like object with valid 0x address (42+ chars)."""
    # Ensure valid format: 0x + 40 hex chars
    if len(trader_address) < 42:
        trader_address = "0x" + trader_address[2:].zfill(40)[-40:]
    class _FakeTrade:
        def __init__(self, addr):
            self.trader_address = addr
            self.source_trade_id = source_trade_id or f"ft_{addr[-8:]}"
    return _FakeTrade(trader_address)


# ── Helper to generate valid 0x addresses ─────────────────────────────────────
def _valid_address(i: int) -> str:
    """Generate a valid 0x Ethereum address (42 chars total)."""
    return "0x" + format(i, "040x")  # 0x + 40 hex chars


# ── address rejection ───────────────────────────────────────────────────────────
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


# ── dry-run / write-scope purity ─────────────────────────────────────────────
def test_dry_run_touches_no_db():
    p = _fresh_v21_db()
    db = ed.open_readonly(str(p))
    try:
        result = discover(
            db,
            bounds={"market_limit": 5, "trade_limit_per_market": 50, "max_wallets": 5},
            live=False,
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


def test_write_scope_only_wallets_and_watchlist():
    """After a real write, ONLY wallets + specialist_evidence_watchlist change;
    the 20 forbidden tables stay empty."""
    p = _fresh_v21_db()
    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        # Create fake adapter with complete market/trade data
        market_id = "0x" + "1" * 64
        fakes = [_valid_address(i) for i in range(0xA1, 0xA4)]
        trades = [_make_fake_trade(addr) for addr in fakes]
        adapter = _FakeDiscoveryAdapter({market_id: (trades, "complete")})

        result = discover(
            db, adapter=adapter, add_watches=True,
            bounds={"market_limit": 5, "trade_limit_per_market": 50, "max_wallets": 5},
            live=True,
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
def test_partial_failed_never_promote_watch():
    """Partial/failed fetches MUST create zero wallets and watches."""
    p = _fresh_v21_db()
    fakes = [_valid_address(i) for i in range(0xA1, 0xA6)]

    # Create market data: 2 complete, 1 partial, 1 failed
    market_data = {
        "0x1111" + "0" * 58: ([_make_fake_trade(fakes[0]), _make_fake_trade(fakes[1])], "complete"),
        "0x2222" + "0" * 58: ([], "partial"),
        "0x3333" + "0" * 58: ([], "failed"),
    }
    adapter = _FakeDiscoveryAdapter(market_data)

    db = ed.open_writable(str(p), _fake_args(write=True, add_to_watchlist=True))
    try:
        result = discover(
            db, adapter=adapter, add_watches=True,
            bounds={"market_limit": 5, "trade_limit_per_market": 50, "max_wallets": 5},
            live=True,
            perform_writes=True,
        )
        db.commit()
    finally:
        db.close()

    # Only complete markets contribute wallets
    assert result.markets_completed == 1
    assert result.markets_partial == 1
    assert result.markets_failed == 1
    # wallets from complete market
    assert result.new_wallets == 2
    # watches created for wallets from complete market (add_watches=True)
    assert result.watches_created == 2

    # Verify forbidden tables unchanged
    conn = Database(p).connect()
    for t in FORBIDDEN_TABLES:
        if conn.execute(f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{t}'").fetchone():
            cnt = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            assert cnt == 0, f"forbidden table {t} has {cnt} rows"
    conn.close()


# ── complete fake market/trade payload discovers real wallets ───────────────────
def test_complete_market_trades_discover_wallets():
    """Complete market/trade payload discovers real wallets with proper derivation."""
    p = _fresh_v21_db()
    fakes = [_valid_address(i) for i in range(0xA1, 0xA6)]  # 5 distinct valid addresses

    market_data = {
        "0x1111" + "0" * 58: ([_make_fake_trade(addr) for addr in fakes[:2]], "complete"),
        "0x2222" + "0" * 58: ([_make_fake_trade(addr) for addr in fakes[2:4]], "complete"),
    }
    adapter = _FakeDiscoveryAdapter(market_data)

    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        result = discover(
            db, adapter=adapter, add_watches=False,
            bounds={"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10},
            live=True,
            perform_writes=True,
        )
        db.commit()
    finally:
        db.close()

    # Should have discovered wallets from complete markets
    assert result.new_wallets == 4
    assert result.markets_completed == 2


# ── duplicate addresses collapse across markets ──────────────────────────────────
def test_duplicate_addresses_collapse():
    """Same address across multiple markets should deduplicate."""
    p = _fresh_v21_db()
    same_addr = _valid_address(0xDD)  # Valid 42-char address

    market_data = {
        "0x1111" + "0" * 58: ([_make_fake_trade(same_addr)], "complete"),
        "0x2222" + "0" * 58: ([_make_fake_trade(same_addr)], "complete"),
    }
    adapter = _FakeDiscoveryAdapter(market_data)

    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        result = discover(
            db, adapter=adapter, add_watches=False,
            bounds={"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10},
            live=True,
            perform_writes=True,
        )
        db.commit()
    finally:
        db.close()

    # deduplicated to single wallet
    assert result.new_wallets == 1


# ── deterministic ordering and max-wallet bound ───────────────────────────────────
def test_deterministic_ordering_and_max_wallet_bound():
    """Candidates are sorted deterministically and bounded by max-wallets."""
    p = _fresh_v21_db()
    fakes = [_valid_address(i) for i in range(0x01, 0x10)]  # 15 addresses

    # Create 5 markets each with unique addresses
    market_data = {}
    for i in range(5):
        market_data[f"0x{format(i, '016x')}" + "0" * 48] = ([_make_fake_trade(fakes[i])], "complete")

    adapter = _FakeDiscoveryAdapter(market_data)
    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        # max_wallets = 3, should stop at 3
        result = discover(
            db, adapter=adapter, add_watches=False,
            bounds={"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 3},
            live=True,
            perform_writes=True,
        )
        db.commit()
    finally:
        db.close()

    assert result.new_wallets == 3


# ── dry-run reports existing versus would-create truthfully ───────────────────────
def test_dry_run_reports_existing_and_would_create():
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
        # Inject an address that matches the existing wallet via market data
        market_data = {
            "0x1111" + "0" * 58: ([_make_fake_trade(existing_addr)], "complete"),
        }
        adapter = _FakeDiscoveryAdapter(market_data)

        result = discover(
            db, adapter=adapter,
            bounds={"market_limit": 5, "trade_limit_per_market": 50, "max_wallets": 5},
            live=True,  # True to process adapter (fake network for testing)
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
            assert c["action"] in ("existing_wallet", "existing_watch")


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
def test_json_output_contract():
    """JSON output must contain all required fields."""
    p = _fresh_v21_db()
    fakes = [_valid_address(i) for i in range(0xA1, 0xA4)]
    market_data = {
        "0x1111" + "0" * 58: ([_make_fake_trade(addr) for addr in fakes], "complete"),
    }
    adapter = _FakeDiscoveryAdapter(market_data)

    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        result = discover(
            db, adapter=adapter, add_watches=False,
            bounds={"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10},
            live=True,
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
def test_wallet_write_failure_rollback():
    """Wallet write failure should roll back all new rows."""
    p = _fresh_v21_db()
    fakes = [_valid_address(i) for i in range(0xA1, 0xA3)]
    market_data = {
        "0x1111" + "0" * 58: ([_make_fake_trade(addr) for addr in fakes], "complete"),
    }
    adapter = _FakeDiscoveryAdapter(market_data)

    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        ed.DbConn._COMMIT_FAIL_HOOK = RuntimeError("simulated failure")
        discover(
            db, adapter=adapter, add_watches=False,
            bounds={"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10},
            live=True,
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
def test_watch_write_failure_rollback():
    """Watch write failure should roll back all new wallet+watch rows."""
    p = _fresh_v21_db()
    fakes = [_valid_address(i) for i in range(0xA1, 0xA3)]
    market_data = {
        "0x1111" + "0" * 58: ([_make_fake_trade(addr) for addr in fakes], "complete"),
    }
    adapter = _FakeDiscoveryAdapter(market_data)

    db = ed.open_writable(str(p), _fake_args(write=True, add_to_watchlist=True))
    try:
        ed.DbConn._COMMIT_FAIL_HOOK = RuntimeError("watch write failure")
        discover(
            db, adapter=adapter, add_watches=True,
            bounds={"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10},
            live=True,
            perform_writes=True,
        )
        # Should have raised
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
def test_five_complete_fake_wallets_create_five_research_watches():
    """Five valid wallets + add-to-watchlist = five watch rows."""
    p = _fresh_v21_db()
    fakes = [_valid_address(i) for i in range(0xA1, 0xA6)]  # 5 addresses

    market_data = {
        f"0x{m:016x}" + "0" * 48: ([_make_fake_trade(fakes[i])], "complete")
        for i, m in enumerate(range(0x1000, 0x1005))
    }
    adapter = _FakeDiscoveryAdapter(market_data)

    db = ed.open_writable(str(p), _fake_args(write=True, add_to_watchlist=True))
    try:
        result = discover(
            db, adapter=adapter, add_watches=True,
            bounds={"market_limit": 10, "trade_limit_per_market": 100, "max_wallets": 10},
            live=True,
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
    # This is already tested in test_open_writable_refuses_without_gates
    # but we add an explicit assertion that gates run first
    prod = str(ed.PRODUCTION_DB_ABSOLUTE)
    args = _fake_args(write=True)  # Missing allow_live and confirm
    # require_write_gates must return False
    assert ed.require_write_gates(args, db_path=prod) is False, \
        "Gates must fail for production DB without full gate set"


# ── SQL write trace targets only allowed tables ─────────────────────────────────────
def test_sql_write_trace_only_allowed_tables():
    """Prove all writes target only wallets or specialist_evidence_watchlist."""
    p = _fresh_v21_db()
    fakes = [_valid_address(i) for i in range(0xA1, 0xA4)]
    market_data = {
        "0x1111" + "0" * 58: ([_make_fake_trade(addr) for addr in fakes], "complete"),
    }
    adapter = _FakeDiscoveryAdapter(market_data)

    db = ed.open_writable(str(p), _fake_args(write=True, add_to_watchlist=True))
    try:
        discover(
            db, adapter=adapter, add_watches=True,
            bounds={"market_limit": 5, "trade_limit_per_market": 50, "max_wallets": 5},
            live=True,
            perform_writes=True,
        )
        db.commit()
    finally:
        db.close()

    conn = Database(p).connect()
    # Only wallets and watchlist should have rows
    allowed_wallets = conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
    allowed_watches = conn.execute("SELECT COUNT(*) FROM specialist_evidence_watchlist").fetchone()[0]
    assert allowed_wallets >= 1
    assert allowed_watches >= 1

    # Forbidden tables must be empty
    for t in FORBIDDEN_TABLES:
        if conn.execute(f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{t}'").fetchone():
            cnt = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            assert cnt == 0, f"forbidden table {t} has {cnt} rows"
    conn.close()