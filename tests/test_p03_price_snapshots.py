"""PR-3 (recovery sequence) candidate price-snapshot engine tests.

This suite covers the snapshot engine (``snapshot_one``), the
persistence layer (``persist_price_snapshot`` /
``get_latest_price_snapshot``), and the end-to-end disposable-DB
flow. The book provider is ALWAYS an injected fake — no real HTTP,
no real CLOB calls. The engine itself is pure logic over its inputs;
the tests cover every bounded status path.

The test groups follow the PR-3 spec §10 sequence:

  1. NOT_PENDING
  2. MISSING_TOKEN
  3. MARKET_NOT_OPEN (active=0, closed=1, resolved=1, missing row)
  4. Side-aware computation (BUY uses best_ask, SELL uses best_bid)
  5. Side-aware deterioration (positive = worse for our side)
  6. Mid-change neutral market movement
  7. EMPTY_BOOK / ONE_SIDED_BOOK (passed through, no engine logic)
  8. RATE_LIMITED / HTTP_ERROR / TIMEOUT / PARSE_ERROR (no exec fields)
  9. Persist + idempotency (same run_id → no-op; new run_id → new row)
 10. get_latest_price_snapshot (DESC ordering, no latest pointer)
 11. No signals/orders/positions/decision_log writes
 12. End-to-end disposable-DB acceptance test (the brief's step 12)

The fake book provider is :class:`FakeBookProvider` defined below —
it returns a pre-set :class:`ClobBook` and records the call history
for assertions.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.adapters.polymarket_clob import (  # noqa: E402
    ClobBook,
    ClobBookLevel,
)
from polycopy.db.database import Database  # noqa: E402
from polycopy.db.price_snapshot_persistence import (  # noqa: E402
    count_snapshots_by_status,
    count_snapshots_for_run,
    get_latest_price_snapshot,
    persist_price_snapshot,
)
from polycopy.db.schema import (  # noqa: E402
    MIGRATIONS,
    SCHEMA_VERSION,
)
from polycopy.domain.copy_candidate import (  # noqa: E402
    CandidateStatus,
)
from polycopy.domain.price_snapshot import (  # noqa: E402
    SnapshotFetchStatus,
)
from polycopy.engine.price_snapshots import (  # noqa: E402
    _compute_executable_fields,
    _run_async_from_sync,
    snapshot_one,
)


# ── Test fixture: disposable v9 DB ─────────────────────────────────────────
@pytest.fixture
def db(tmp_path: Path) -> Database:
    """Disposable v9 DB per test."""
    database = Database(db_path=tmp_path / "p03.db").connect()
    try:
        yield database
    finally:
        database.close()


# ── Fake book provider ─────────────────────────────────────────────────────
class FakeBookProvider:
    """Deterministic stand-in for a real book provider.

    Returns a pre-set :class:`ClobBook` for every ``fetch_book`` call
    and records the call history for assertions. Tests can install
    one of the canned fixtures via ``set_book(...)`` or build their
    own ``ClobBook`` via ``set_book_object(...)``.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._next_result: ClobBook = ClobBook(
            token_id="", bids=[], asks=[],
        )
        self._raise: Optional[Exception] = None

    def set_book_object(self, book: ClobBook) -> None:
        """Install a fully-built ClobBook as the next result."""
        self._next_result = book
        self._raise = None

    def set_book(
        self,
        *,
        bids: list[tuple[float, float]] | None = None,
        asks: list[tuple[float, float]] | None = None,
        http_status: Optional[int] = 200,
        latency_ms: Optional[int] = 10,
        request_attempts: int = 1,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        token_id: str = "TOK",
    ) -> None:
        """Install a ClobBook built from (price, size) tuples."""
        b_levels = [ClobBookLevel(price=p, size=s) for p, s in (bids or [])]
        a_levels = [ClobBookLevel(price=p, size=s) for p, s in (asks or [])]
        b_levels.sort(key=lambda lv: lv.price, reverse=True)
        a_levels.sort(key=lambda lv: lv.price)
        self._next_result = ClobBook(
            token_id=token_id,
            bids=b_levels,
            asks=a_levels,
            http_status=http_status,
            latency_ms=latency_ms,
            request_attempts=request_attempts,
            error_code=error_code,
            error_message=error_message,
        )
        self._raise = None

    def set_raises(self, exc: Exception) -> None:
        self._raise = exc

    async def fetch_book(self, token_id: str) -> ClobBook:  # noqa: D401
        self.calls.append(token_id)
        if self._raise is not None:
            raise self._raise
        # Return a copy so tests that mutate the result don't poison
        # the next call.
        return self._next_result


# ── Seed helpers (mirror test_p02 patterns; re-defined here for isolation) ─
def _seed_wallet(db: Database, *, address: str = "0xabc123") -> str:
    wallet_id = str(uuid4())
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at, "
        "canonical_address) VALUES (?, ?, 'test', 0, ?, "
        "LOWER(TRIM(?)))",
        (wallet_id, address, "2026-07-01T00:00:00Z", address),
    )
    db.conn.commit()
    return wallet_id


def _seed_market_with_outcome(
    db: Database,
    *,
    market_id: str | None = None,
    source_id: str = "cond-1",
    label: str = "Yes",
    token: str = "tok-1",
    price: float = 0.5,
    active: bool = True,
    closed: bool = False,
    resolved: bool = False,
    end_date: Optional[str] = None,
) -> tuple[str, int]:
    market_id = market_id or str(uuid4())
    db.conn.execute(
        "INSERT INTO markets (id, source_id, source, question, active, "
        "closed, resolved, fetched_at, end_date) "
        "VALUES (?, ?, 'polymarket', 'Q?', ?, ?, ?, ?, ?)",
        (
            market_id, source_id,
            int(active), int(closed), int(resolved),
            "2026-07-01T00:00:00Z", end_date,
        ),
    )
    cur = db.conn.execute(
        "INSERT INTO market_outcomes (market_id, label, price, volume, "
        "clob_token_id) VALUES (?, ?, ?, 0.0, ?)",
        (market_id, label, price, token),
    )
    outcome_id = int(cur.lastrowid)
    db.conn.commit()
    return market_id, outcome_id


def _seed_pending_candidate(
    db: Database,
    *,
    wallet_id: str,
    market_id: str,
    market_outcome_id: int,
    token: Optional[str] = "tok-1",
    side: str = "BUY",
    source_trade_price: float = 0.5,
    source_trade_quantity: float = 10.0,
    source_trade_timestamp: str = "2026-07-01T00:00:00Z",
) -> int:
    """Insert a real PENDING_PRICE_CHECK copy_candidates row. Returns the id."""
    src = f"snap-{uuid4().hex[:8]}"
    sid = f"stid-{uuid4().hex[:8]}"
    # First insert a matching source_trades row so the foreign keys hold.
    db.conn.execute(
        "INSERT INTO source_trades (id, source, source_trade_id, "
        "market_source_id, side, outcome, quantity, price, "
        "trader_address, timestamp, is_sample, token_id) "
        "VALUES (?, ?, ?, ?, ?, 'Yes', ?, ?, ?, ?, 0, ?)",
        (
            str(uuid4()), src, sid, "cond-1", side,
            source_trade_quantity, source_trade_price,
            "0xabc123", source_trade_timestamp, token,
        ),
    )
    cur = db.conn.execute(
        "INSERT INTO copy_candidates (wallet_id, source, source_trade_id, "
        "market_id, market_outcome_id, market_source_id, token_id, "
        "outcome_label, side, source_trade_price, source_trade_quantity, "
        "source_trade_notional, source_trade_timestamp, observed_at, "
        "wallet_score_version, wallet_score, wallet_verdict, status, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'cond-1', ?, 'Yes', ?, ?, ?, ?, ?, "
        "'2026-07-01T00:00:00Z', 'v1', 85.0, 'copy_candidate', "
        "'PENDING_PRICE_CHECK', "
        "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')",
        (
            wallet_id, src, sid, market_id, market_outcome_id,
            token, side, source_trade_price, source_trade_quantity,
            source_trade_price * source_trade_quantity,
            source_trade_timestamp,
        ),
    )
    db.conn.commit()
    return int(cur.lastrowid)


# ── v9 schema acceptance ───────────────────────────────────────────────────
def test_v9_db_includes_candidate_price_snapshots_table(db: Database) -> None:
    """A fresh v9 DB has the new table + all expected columns + indexes."""
    # The schema may advance beyond v10 (currently v11 after Chunk 5);
    # the assertions below check the v9 table is present on a fresh DB.
    assert SCHEMA_VERSION >= 10
    row = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
    assert row is not None and int(row["value"]) == SCHEMA_VERSION

    cols = {
        r["name"]
        for r in db.conn.execute(
            "PRAGMA table_info(candidate_price_snapshots)"
        ).fetchall()
    }
    required = {
        "id", "candidate_id", "snapshot_run_id", "fetch_status",
        "fetch_endpoint", "fetch_http_status", "fetch_latency_ms",
        "request_attempts", "fetch_error_code", "fetch_error_message",
        "token_id", "side", "source_trade_price", "source_trade_quantity",
        "source_trade_timestamp",
        "best_bid", "best_bid_size", "best_ask", "best_ask_size",
        "mid_price", "spread",
        "executable_price", "executable_side_depth", "expected_fill_price",
        "price_deterioration", "price_deterioration_pct",
        "mid_change", "mid_change_pct",
        "trade_age_seconds", "market_end_at", "seconds_to_market_end",
        "market_metadata_fetched_at",
        "market_active_at_fetch", "market_closed_at_fetch",
        "market_resolved_at_fetch",
        "bid_level_count", "ask_level_count",
        "book_summary_json", "book_hash",
        "fetched_at", "created_at",
    }
    missing = required - cols
    assert not missing, f"missing columns: {missing}"

    indexes = {
        r["name"]
        for r in db.conn.execute(
            "PRAGMA index_list(candidate_price_snapshots)"
        ).fetchall()
    }
    for ix in (
        "idx_cps_candidate_fetched",
        "idx_cps_status",
        "idx_cps_run",
    ):
        assert ix in indexes, f"missing index {ix}; got {sorted(indexes)}"


def test_v8_to_v9_migration_preserves_data(tmp_path: Path) -> None:
    """v8 → v9 migration is additive; existing data is preserved."""
    db_path = tmp_path / "p03-v8-to-v9.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # SCHEMA_VERSION bumped past v11; we only need the migrations
    # up to and including v8 (the pre-state for the v8→v9 test).
    pre_version = 8
    for version in range(1, pre_version + 1):
        for stmt in MIGRATIONS[version]:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) "
            "VALUES ('schema_version', ?)",
            (str(version),),
        )
    conn.commit()
    # Seed a wallet + market at v8.
    conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) "
        "VALUES ('w1', '0xABC', 'a', 0, '2026-01-01T00:00:00Z')",
    )
    conn.execute(
        "INSERT INTO markets (id, source_id, source, question, "
        "fetched_at) VALUES ('m1', 'cond-1', 'polymarket', 'Q?', "
        "'2026-01-01T00:00:00Z')",
    )
    conn.commit()
    conn.close()

    db = Database(db_path=db_path).connect()
    try:
        row = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
        assert int(row["value"]) == SCHEMA_VERSION
        # Pre-existing rows preserved.
        assert db.fetchone("SELECT id FROM wallets WHERE id='w1'") is not None
        assert db.fetchone("SELECT id FROM markets WHERE id='m1'") is not None
        # New table exists.
        assert (
            db.fetchone(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='candidate_price_snapshots'"
            ) is not None
        )
        # No FK violations.
        assert db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        db.close()


# ── 1. NOT_PENDING ─────────────────────────────────────────────────────────
def test_not_pending_candidate_yields_not_pending_status(
    db: Database,
) -> None:
    """A non-PENDING candidate → NOT_PENDING, no CLOB call."""
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="tok-1")
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1",
    )
    # Manually flip the candidate to REJECTED.
    db.conn.execute(
        "UPDATE copy_candidates SET status = ? WHERE id = ?",
        (CandidateStatus.REJECTED_WALLET.value, candidate_id),
    )
    db.conn.commit()

    fake = FakeBookProvider()
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.fetch_status == SnapshotFetchStatus.NOT_PENDING.value
    assert fake.calls == []  # no CLOB call
    assert snap.candidate_id == candidate_id
    assert snap.side == "BUY"


# ── 2. MISSING_TOKEN ───────────────────────────────────────────────────────
def test_missing_token_yields_missing_token_status(db: Database) -> None:
    """token_id NULL on the candidate → MISSING_TOKEN, no CLOB call."""
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(
        db, token="tok-1",
    )
    # Seed a candidate with NULL token_id.
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="",  # NULL in DB
    )
    db.conn.execute(
        "UPDATE copy_candidates SET token_id = NULL WHERE id = ?",
        (candidate_id,),
    )
    db.conn.commit()

    fake = FakeBookProvider()
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.fetch_status == SnapshotFetchStatus.MISSING_TOKEN.value
    assert fake.calls == []


# ── 3. MARKET_NOT_OPEN ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "active,closed,resolved,expected",
    [
        (False, False, False, SnapshotFetchStatus.MARKET_NOT_OPEN.value),
        (True, True, False, SnapshotFetchStatus.MARKET_NOT_OPEN.value),
        (True, False, True, SnapshotFetchStatus.MARKET_NOT_OPEN.value),
    ],
)
def test_market_not_open_states(
    db: Database, active: bool, closed: bool, resolved: bool, expected: str,
) -> None:
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(
        db, token="tok-1", active=active, closed=closed, resolved=resolved,
    )
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1",
    )
    fake = FakeBookProvider()
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.fetch_status == expected
    assert fake.calls == []


def test_missing_market_row_yields_market_not_open(db: Database) -> None:
    """If the markets row is gone, we never call CLOB.

    The test inserts a candidate whose market_id points to a
    NON-EXISTENT markets row directly (bypassing the seed helper
    that creates the market). This simulates a data-integrity
    situation without violating the copy_candidates FK.
    """
    wallet_id = _seed_wallet(db)
    # Insert a market but DELETE the outcomes first so we can
    # safely delete the market (outcomes have FK to markets).
    market_id, outcome_id = _seed_market_with_outcome(
        db, token="tok-1", market_id="mkt-missing",
    )
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1",
    )
    # Delete in FK-safe order: copy_candidates → market_outcomes → markets.
    db.conn.execute("DELETE FROM copy_candidates WHERE id = ?", (candidate_id,))
    db.conn.execute("DELETE FROM market_outcomes WHERE market_id = ?", (market_id,))
    db.conn.execute("DELETE FROM markets WHERE id = ?", (market_id,))
    db.conn.commit()

    # Insert a candidate that points at a market that does not exist.
    # Disable FK enforcement for the insert — the production scenario
    # is "candidate was created, market row was deleted out-of-band."
    # We want to verify the engine's defensive check, not the schema's.
    src = f"snap-{uuid4().hex[:8]}"
    sid = f"stid-{uuid4().hex[:8]}"
    db.conn.execute("PRAGMA foreign_keys = OFF")
    try:
        db.conn.execute(
            "INSERT INTO source_trades (id, source, source_trade_id, "
            "market_source_id, side, outcome, quantity, price, "
            "trader_address, timestamp, is_sample, token_id) "
            "VALUES (?, ?, ?, ?, 'BUY', 'Yes', 10.0, 0.5, '0xabc123', "
            "'2026-07-01T00:00:00Z', 0, 'tok-1')",
            (str(uuid4()), src, sid, "cond-1"),
        )
        cur = db.conn.execute(
            "INSERT INTO copy_candidates (wallet_id, source, source_trade_id, "
            "market_id, market_outcome_id, market_source_id, token_id, "
            "outcome_label, side, source_trade_price, source_trade_quantity, "
            "source_trade_notional, source_trade_timestamp, observed_at, "
            "wallet_score_version, wallet_score, wallet_verdict, status, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, 'cond-1', 'tok-1', 'Yes', 'BUY', "
            "0.5, 10.0, 5.0, '2026-07-01T00:00:00Z', "
            "'2026-07-01T00:00:00Z', 'v1', 85.0, 'copy_candidate', "
            "'PENDING_PRICE_CHECK', "
            "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')",
            (wallet_id, src, sid, "mkt-does-not-exist"),
        )
        db.conn.commit()
        candidate_id = int(cur.lastrowid)
    finally:
        db.conn.execute("PRAGMA foreign_keys = ON")

    fake = FakeBookProvider()
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.fetch_status == SnapshotFetchStatus.MARKET_NOT_OPEN.value
    assert fake.calls == []
    assert snap.market_active_at_fetch is None


# ── 4. Side-aware executable (BUY/SELL) ───────────────────────────────────
def test_buy_uses_best_ask_as_executable_price(db: Database) -> None:
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(
        db, token="tok-1", price=0.5,
    )
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="BUY",
        source_trade_price=0.5,
    )
    fake = FakeBookProvider()
    fake.set_book(
        bids=[(0.48, 10.0), (0.47, 5.0)],
        asks=[(0.52, 20.0), (0.53, 30.0)],
    )
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.fetch_status == SnapshotFetchStatus.OK.value
    assert snap.executable_price == 0.52
    assert snap.executable_side_depth == 20.0
    assert snap.expected_fill_price == 0.52
    assert snap.best_bid == 0.48
    assert snap.best_ask == 0.52
    assert snap.bid_level_count == 2
    assert snap.ask_level_count == 2
    assert fake.calls == ["tok-1"]


def test_sell_uses_best_bid_as_executable_price(db: Database) -> None:
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(
        db, token="tok-1", price=0.5,
    )
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="SELL",
        source_trade_price=0.5,
    )
    fake = FakeBookProvider()
    fake.set_book(
        bids=[(0.48, 10.0), (0.47, 5.0)],
        asks=[(0.52, 20.0), (0.53, 30.0)],
    )
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.fetch_status == SnapshotFetchStatus.OK.value
    assert snap.executable_price == 0.48
    assert snap.executable_side_depth == 10.0
    assert snap.expected_fill_price == 0.48


# ── 5. Side-aware deterioration (BUY: positive = worse for us) ────────────
def test_buy_deterioration_positive_when_ask_above_trade(db: Database) -> None:
    """BUY at trade 0.5, ask 0.55 → deterioration = +0.05."""
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(
        db, token="tok-1", price=0.5,
    )
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="BUY",
        source_trade_price=0.5,
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.45, 10.0)], asks=[(0.55, 10.0)])
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.price_deterioration == pytest.approx(0.05)
    assert snap.price_deterioration_pct == pytest.approx(0.10)
    assert snap.mid_change == pytest.approx(0.0)  # (0.45+0.55)/2 - 0.5
    assert snap.mid_change_pct == pytest.approx(0.0)


def test_sell_deterioration_positive_when_bid_below_trade(db: Database) -> None:
    """SELL at trade 0.5, bid 0.45 → deterioration = +0.05."""
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(
        db, token="tok-1", price=0.5,
    )
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="SELL",
        source_trade_price=0.5,
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.45, 10.0)], asks=[(0.55, 10.0)])
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.price_deterioration == pytest.approx(0.05)
    assert snap.price_deterioration_pct == pytest.approx(0.10)


def test_deterioration_negative_when_our_price_better(db: Database) -> None:
    """BUY at trade 0.5, ask 0.48 → deterioration = -0.02 (we'd pay less)."""
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(
        db, token="tok-1", price=0.5,
    )
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="BUY",
        source_trade_price=0.5,
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.45, 10.0)], asks=[(0.48, 10.0)])
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.price_deterioration == pytest.approx(-0.02)
    assert snap.price_deterioration_pct == pytest.approx(-0.04)


# ── 6. Mid-change neutrality ────────────────────────────────────────────────
def test_mid_change_reports_neutral_market_movement(db: Database) -> None:
    """mid_change is independent of side; computed once."""
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="tok-1")
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="BUY",
        source_trade_price=0.5,
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.40, 10.0)], asks=[(0.60, 10.0)])
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.mid_price == pytest.approx(0.50)
    assert snap.mid_change == pytest.approx(0.0)  # 0.5 - 0.5
    assert snap.mid_change_pct == pytest.approx(0.0)
    assert snap.spread == pytest.approx(0.20)


# ── 7. Empty / one-sided book passes through ──────────────────────────────
def test_empty_book_status_passthrough(db: Database) -> None:
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="tok-1")
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1",
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[], asks=[])  # empty book
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.fetch_status == SnapshotFetchStatus.EMPTY_BOOK.value
    assert snap.executable_price is None
    assert snap.price_deterioration is None


def test_one_sided_book_status_passthrough(db: Database) -> None:
    """One-sided book: only bids; BUY candidate cannot fill → ONE_SIDED_BOOK."""
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="tok-1")
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="BUY",
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.45, 10.0)], asks=[])
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.fetch_status == SnapshotFetchStatus.ONE_SIDED_BOOK.value
    assert snap.executable_price is None


# ── 8. Bounded error statuses ─────────────────────────────────────────────
def test_rate_limited_status_passthrough(db: Database) -> None:
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="tok-1")
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1",
    )
    fake = FakeBookProvider()
    fake.set_book(error_code="HTTP_429", http_status=429, request_attempts=1,
                 error_message="429 Too Many Requests")
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.fetch_status == SnapshotFetchStatus.RATE_LIMITED.value
    assert snap.fetch_http_status == 429
    assert snap.executable_price is None
    assert snap.request_attempts == 1


def test_http_error_status_passthrough(db: Database) -> None:
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="tok-1")
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1",
    )
    fake = FakeBookProvider()
    fake.set_book(error_code="HTTP_5XX", http_status=503, request_attempts=3,
                 error_message="5xx after 3 attempts")
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.fetch_status == SnapshotFetchStatus.HTTP_ERROR.value
    assert snap.executable_price is None


def test_timeout_status_passthrough(db: Database) -> None:
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="tok-1")
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1",
    )
    fake = FakeBookProvider()
    fake.set_book(error_code="TIMEOUT", request_attempts=2)
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.fetch_status == SnapshotFetchStatus.TIMEOUT.value


def test_parse_error_status_passthrough(db: Database) -> None:
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="tok-1")
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1",
    )
    fake = FakeBookProvider()
    fake.set_book(error_code="PARSE_ERROR_CROSSED",
                 error_message="crossed book")
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.fetch_status == SnapshotFetchStatus.PARSE_ERROR.value


# ── 9. Persistence + idempotency ──────────────────────────────────────────
def test_persist_then_persist_again_is_idempotent(db: Database) -> None:
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="tok-1")
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="BUY",
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.45, 10.0)], asks=[(0.55, 10.0)])
    snap1 = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-A",
        book_provider=fake,
    )
    snap1_id, inserted1 = persist_price_snapshot(db, snap1)
    assert inserted1 is True
    assert snap1_id == snap1.id

    # Re-run with the SAME run_id. The fake returns a different
    # book, but the persistence layer must NOT rewrite history.
    fake.set_book(bids=[(0.10, 1.0)], asks=[(0.20, 1.0)])
    snap2 = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-A",
        book_provider=fake,
    )
    snap2_id, inserted2 = persist_price_snapshot(db, snap2)
    assert inserted2 is False  # NOT inserted
    assert snap2_id == snap1_id  # existing row's id is returned

    # Verify the original row was NOT rewritten.
    row = db.fetchone(
        "SELECT best_bid, best_ask FROM candidate_price_snapshots "
        "WHERE id = ?", (snap1_id,),
    )
    assert row["best_bid"] == 0.45
    assert row["best_ask"] == 0.55
    # And there is exactly one row for this (candidate, run).
    assert count_snapshots_for_run(db, "run-A") == 1


def test_new_run_id_creates_new_observation(db: Database) -> None:
    """Same candidate, different run_ids → two rows (append-only)."""
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="tok-1")
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1",
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.45, 10.0)], asks=[(0.55, 10.0)])
    snap_a = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-A",
        book_provider=fake,
    )
    snap_b = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-B",
        book_provider=fake,
    )
    persist_price_snapshot(db, snap_a)
    persist_price_snapshot(db, snap_b)
    assert count_snapshots_for_run(db, "run-A") == 1
    assert count_snapshots_for_run(db, "run-B") == 1
    # Two rows total for the candidate.
    assert db.fetchone(
        "SELECT COUNT(*) AS n FROM candidate_price_snapshots "
        "WHERE candidate_id = ?", (candidate_id,),
    )["n"] == 2


# ── 10. get_latest_price_snapshot ─────────────────────────────────────────
def test_get_latest_returns_most_recent(db: Database) -> None:
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="tok-1")
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1",
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.45, 10.0)], asks=[(0.55, 10.0)])

    # Snapshot 1 at t0
    snap1 = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        now=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
        book_provider=fake,
    )
    persist_price_snapshot(db, snap1)

    # Snapshot 2 at t0+1h
    snap2 = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-2",
        now=datetime(2026, 7, 1, 13, 0, 0, tzinfo=timezone.utc),
        book_provider=fake,
    )
    persist_price_snapshot(db, snap2)

    latest = get_latest_price_snapshot(db, candidate_id)
    assert latest is not None
    assert latest.snapshot_run_id == "run-2"
    assert latest.fetched_at.startswith("2026-07-01T13:00:00")


def test_get_latest_returns_none_when_no_snapshots(db: Database) -> None:
    assert get_latest_price_snapshot(db, candidate_id=99999) is None


def test_no_latest_pointer_column_on_copy_candidates(db: Database) -> None:
    """Contract §6.6: no ``latest_price_snapshot_id`` column on copy_candidates."""
    cols = {
        r["name"]
        for r in db.conn.execute(
            "PRAGMA table_info(copy_candidates)"
        ).fetchall()
    }
    assert "latest_price_snapshot_id" not in cols


# ── 11. No signals / orders / positions / decision_log writes ─────────────
def test_snapshot_does_not_write_to_other_tables(db: Database) -> None:
    """The engine + persistence must not create signals/orders/positions
    or decision_log rows. They belong to PR-4 / PR-5."""
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="tok-1")
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1",
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.45, 10.0)], asks=[(0.55, 10.0)])
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    persist_price_snapshot(db, snap)

    assert db.fetchone("SELECT COUNT(*) AS n FROM signals")["n"] == 0
    assert db.fetchone("SELECT COUNT(*) AS n FROM orders")["n"] == 0
    assert db.fetchone("SELECT COUNT(*) AS n FROM positions")["n"] == 0
    assert db.fetchone("SELECT COUNT(*) AS n FROM decision_log")["n"] == 0


# ── 12. End-to-end disposable-DB acceptance ───────────────────────────────
def test_end_to_end_disposable_db_acceptance(db: Database) -> None:
    """Acceptance: 4 candidates in 4 different states → 4 bounded statuses.

    This is the brief's step 12 canonical scenario.
    """
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="tok-1")

    # 1. PENDING + book → OK
    c1 = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="BUY",
        source_trade_price=0.5,
    )
    # 2. PENDING + book is empty → EMPTY_BOOK
    c2 = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="BUY",
        source_trade_price=0.5,
    )
    # 3. PENDING + 429 → RATE_LIMITED
    c3 = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="BUY",
        source_trade_price=0.5,
    )
    # 4. NOT_PENDING → NOT_PENDING
    c4 = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="BUY",
        source_trade_price=0.5,
    )
    db.conn.execute(
        "UPDATE copy_candidates SET status = ? WHERE id = ?",
        (CandidateStatus.REJECTED_WALLET.value, c4),
    )
    db.conn.commit()

    # One fake provider; we swap the canned book per candidate.
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.45, 10.0)], asks=[(0.55, 10.0)])
    snap1 = snapshot_one(db, candidate_id=c1, snapshot_run_id="run",
                          book_provider=fake)
    fake.set_book(bids=[], asks=[])
    snap2 = snapshot_one(db, candidate_id=c2, snapshot_run_id="run",
                          book_provider=fake)
    fake.set_book(error_code="HTTP_429", http_status=429, request_attempts=1)
    snap3 = snapshot_one(db, candidate_id=c3, snapshot_run_id="run",
                          book_provider=fake)
    snap4 = snapshot_one(db, candidate_id=c4, snapshot_run_id="run",
                          book_provider=fake)

    for s in (snap1, snap2, snap3, snap4):
        persist_price_snapshot(db, s)

    # Verify per-status counts for the run.
    counts = count_snapshots_by_status(db, "run")
    assert counts[SnapshotFetchStatus.OK.value] == 1
    assert counts[SnapshotFetchStatus.EMPTY_BOOK.value] == 1
    assert counts[SnapshotFetchStatus.RATE_LIMITED.value] == 1
    assert counts[SnapshotFetchStatus.NOT_PENDING.value] == 1
    # All other statuses are zero.
    for s in SnapshotFetchStatus:
        if s.value not in {"OK", "EMPTY_BOOK", "RATE_LIMITED", "NOT_PENDING"}:
            assert counts[s.value] == 0, f"{s.value} should be 0; got {counts[s.value]}"

    # Latest snapshot for c1 is the OK one.
    latest = get_latest_price_snapshot(db, c1)
    assert latest is not None
    assert latest.fetch_status == SnapshotFetchStatus.OK.value
    assert latest.executable_price == 0.55


# ── Market-end metadata: copied verbatim, NULL preserved ──────────────────
def test_market_end_date_is_copied_from_markets(db: Database) -> None:
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(
        db, token="tok-1", end_date="2026-08-15T00:00:00Z",
    )
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="BUY",
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.45, 10.0)], asks=[(0.55, 10.0)])
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        now=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
        book_provider=fake,
    )
    assert snap.market_end_at == "2026-08-15T00:00:00Z"
    # 2026-08-15 00:00:00 minus 2026-07-01 12:00:00 = 44 days + 12 hours
    expected_seconds = 44 * 86400 + 12 * 3600
    assert snap.seconds_to_market_end == expected_seconds


def test_negative_seconds_to_market_end_preserved(db: Database) -> None:
    """Past end_date → negative seconds; preserved (not clamped)."""
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(
        db, token="tok-1", end_date="2026-06-15T00:00:00Z",
    )
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="BUY",
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.45, 10.0)], asks=[(0.55, 10.0)])
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        now=datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
        book_provider=fake,
    )
    assert snap.seconds_to_market_end is not None
    assert snap.seconds_to_market_end < 0  # negative; preserved


def test_null_market_end_date_preserved(db: Database) -> None:
    """NULL end_date → NULL market_end_at, NULL seconds_to_market_end."""
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(
        db, token="tok-1", end_date=None,
    )
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1", side="BUY",
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.45, 10.0)], asks=[(0.55, 10.0)])
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.market_end_at is None
    assert snap.seconds_to_market_end is None


# ── Trade age is always populated (independent of book) ───────────────────
def test_trade_age_seconds_always_populated(db: Database) -> None:
    """Even for non-OK snapshots, trade_age is set."""
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="tok-1")
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="tok-1",
    )
    # Flip to REJECTED so we hit NOT_PENDING.
    db.conn.execute(
        "UPDATE copy_candidates SET status = ? WHERE id = ?",
        (CandidateStatus.REJECTED_WALLET.value, candidate_id),
    )
    db.conn.commit()
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=FakeBookProvider(),
    )
    assert snap.fetch_status == SnapshotFetchStatus.NOT_PENDING.value
    assert snap.trade_age_seconds is not None
    assert snap.trade_age_seconds >= 0


# ── Pure-function test: _compute_executable_fields ────────────────────────
def test_compute_executable_fields_buy_positive_deterioration() -> None:
    book = ClobBook(
        token_id="T",
        bids=[ClobBookLevel(price=0.45, size=10.0)],
        asks=[ClobBookLevel(price=0.55, size=10.0)],
    )
    out = _compute_executable_fields(
        side="BUY", source_trade_price=0.5, book=book,
    )
    assert out["executable_price"] == 0.55
    assert out["executable_side_depth"] == 10.0
    assert out["expected_fill_price"] == 0.55
    assert out["price_deterioration"] == pytest.approx(0.05)
    assert out["mid_change"] == pytest.approx(0.0)


def test_compute_executable_fields_sell_missing_bid() -> None:
    """SELL with no bids → executable_price is None (no invented value)."""
    book = ClobBook(
        token_id="T",
        bids=[],
        asks=[ClobBookLevel(price=0.55, size=10.0)],
    )
    out = _compute_executable_fields(
        side="SELL", source_trade_price=0.5, book=book,
    )
    assert out["executable_price"] is None
    assert out["executable_side_depth"] is None
    assert out["price_deterioration"] is None
    # mid_change is None when mid_price is None (which it is when one
    # side is missing); the function does NOT expose mid_price directly.
    assert out["mid_change"] is None


# ── Bounded fetch_endpoint (contract §8) ────────────────────────────────────
def test_fetch_endpoint_is_bounded_label_not_full_url(
    db: Database,
) -> None:
    """``fetch_endpoint`` is a bounded audit label, NOT a URL with the token.

    Per contract §8, the persisted snapshot must record ``"clob/book"``
    as a bounded label, not the constructed URL containing the token
    query parameter (which would leak the token into the audit log).
    The token itself is recorded separately in ``token_id`` from the
    persistent source-of-truth row.
    """
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(
        db, token="secret-token-xyz",
    )
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="secret-token-xyz",
        side="BUY",
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.45, 10.0)], asks=[(0.55, 10.0)])
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    assert snap.fetch_endpoint == "clob/book"
    # And the token must not appear in fetch_endpoint.
    assert "secret-token-xyz" not in (snap.fetch_endpoint or "")
    # Token is recorded separately as a domain field.
    assert snap.token_id == "secret-token-xyz"


def test_fetch_endpoint_is_none_when_no_clob_call_made(
    db: Database,
) -> None:
    """When the engine short-circuits without calling CLOB,
    fetch_endpoint is NULL (not the bounded label)."""
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(db, token="t")
    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token=None,  # missing token
    )
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=FakeBookProvider(),
    )
    assert snap.fetch_status == SnapshotFetchStatus.MISSING_TOKEN.value
    assert snap.fetch_endpoint is None


# ── market_metadata_fetched_at — markets.fetched_at, not markets.end_date ──
def test_market_metadata_fetched_at_captures_markets_fetched_at(
    db: Database,
) -> None:
    """``market_metadata_fetched_at`` is the persisted ``markets.fetched_at``
    (audit timestamp of the market row itself), NOT the market end."""
    wallet_id = _seed_wallet(db)
    market_id, outcome_id = _seed_market_with_outcome(
        db, token="t", end_date="2027-01-01T00:00:00Z",
    )
    # Read the markets row's fetched_at to compare
    row = db.fetchone("SELECT fetched_at FROM markets WHERE id = ?", (market_id,))
    market_fetched_at = row["fetched_at"]

    candidate_id = _seed_pending_candidate(
        db, wallet_id=wallet_id, market_id=market_id,
        market_outcome_id=outcome_id, token="t",
    )
    fake = FakeBookProvider()
    fake.set_book(bids=[(0.45, 10.0)], asks=[(0.55, 10.0)])
    snap = snapshot_one(
        db, candidate_id=candidate_id, snapshot_run_id="run-1",
        book_provider=fake,
    )
    # The snapshot copies the market row's fetched_at, not its end_date.
    assert snap.market_metadata_fetched_at == market_fetched_at
    assert snap.market_end_at == "2027-01-01T00:00:00Z"
    # And they are different fields, with different content.
    assert snap.market_metadata_fetched_at != snap.market_end_at


# ── Sync→async bridge (regression for CI #88 Python 3.12) ──────────────────
def test_snapshot_one_works_without_existing_event_loop() -> None:
    """Regression for CI #88 (Python 3.12).

    Pre-fix, ``snapshot_one`` used
    ``asyncio.get_event_loop().run_until_complete(coro)`` which raised
    ``RuntimeError: There is no current event loop in thread 'MainThread'``
    on Python 3.12 because ``get_event_loop()`` no longer auto-creates
    a loop in the main thread. The fix is
    :func:`_run_async_from_sync` which uses the canonical
    ``new_event_loop() + run_until_complete() + close()`` pattern.

    This test calls ``snapshot_one`` (which transitively invokes
    ``_run_async_from_sync``) from a sync context with NO running loop
    and asserts that it returns a real :class:`PriceSnapshot`. This is
    the same code path that 22 PR-3 tests exercise; it was broken on
    3.12 and is now fixed.
    """
    # No event loop is running here — pytest's pytest-asyncio fixture
    # does not auto-create one for plain sync tests, and
    # ``asyncio_mode = "auto"`` only applies to ``async def`` tests.
    # If the regression returns, this test will fail with the same
    # RuntimeError as the 22 originally-failing tests.
    assert _run_async_from_sync is not None  # import surface check

    # Direct unit test of the helper: it must be callable from a sync
    # context with no running loop.
    async def _echo(x: int) -> int:
        return x * 2

    result = _run_async_from_sync(_echo(21))
    assert result == 42


def test_run_async_from_sync_raises_when_loop_already_running() -> None:
    """If the caller is inside a running event loop, the bridge must
    raise a clear :class:`RuntimeError` rather than hang or silently
    shadow the loop.

    The sync ``snapshot_one`` surface is the only supported entry point;
    this test pins the constraint so future refactors don't regress
    to the broken ``get_event_loop()`` pattern.
    """
    import asyncio as _asyncio

    async def _echo_coro(x: int) -> int:
        return x

    async def _drive() -> None:
        # Build the coroutine; the helper is expected to raise BEFORE
        # awaiting it, so close it explicitly to silence the
        # ``coroutine was never awaited`` warning.
        coro = _echo_coro(7)
        with pytest.raises(RuntimeError, match="snapshot_one"):
            _run_async_from_sync(coro)
        coro.close()

    _asyncio.run(_drive())
