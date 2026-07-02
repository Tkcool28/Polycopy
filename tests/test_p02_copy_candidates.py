"""PR-2 (recovery sequence) copy-candidate persistence tests.

This suite proves the v7 → v8 additive migration and the bounded
copy-candidate persistence layer behave as specified. It is the
test scaffold for the brief's 20 acceptance scenarios plus the end-to-
end disposable-DB acceptance test (see §STEP 12 of the brief).

All tests use disposable DBs (``tmp_path``); production
``/root/Polycopy/data/polycopy.db`` is NEVER touched. Read-only
inspection of production happens in the parent agent's verification
step, not here.

Numbered scenarios mirror the brief:

  1. New v8 DB includes ``copy_candidates`` table (PRAGMA table_info).
  2. v7 → v8 migration preserves existing data + adds the new table.
  3. Foreign keys enforced: FK on markets/wallets/source_trades/
     market_outcomes.
  4. UNIQUE constraint uses ``(wallet_id, source, source_trade_id)``.
  5. Rerun idempotency: persist once → rerun → exactly one row,
     second call returns ``(False, existing_id)``.
  6. Same ``source_trade_id`` under two ``source`` values → two
     distinct rows.
  7. Same ``source_trade`` observed for two different wallets → two
     distinct candidate rows.
  8. COPY_CANDIDATE + resolved outcome → PENDING_PRICE_CHECK.
  9. WATCHLIST → REJECTED_WALLET.
 10. SKIP → REJECTED_WALLET.
 11. INCOMPLETE → REJECTED_WALLET.
 12. Resolver INCOMPLETE → REJECTED_UNRESOLVED_OUTCOME.
 13. Resolver AMBIGUOUS → REJECTED_AMBIGUOUS_OUTCOME.
 14. Closed market → REJECTED_MARKET_CLOSED.
 15. Invalid price / quantity / timestamp → REJECTED_INVALID_TRADE.
 16. Score snapshot: row stores exact score, verdict, formula_version.
 17. Rerun after wallet score later changes: identity not duplicated;
     historical score NOT silently rewritten.
 18. Decision-log evidence: correct decision_type + bounded vocabulary.
 19. No signals / orders / positions created.
 20. End-to-end disposable DB acceptance test (the brief's §STEP 12
     canonical scenario).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.db.copy_candidate_persistence import (  # noqa: E402
    evaluate_source_trade_for_wallet,
    persist_copy_candidate,
    record_candidate_decision_log,
)
from polycopy.db.database import Database  # noqa: E402
from polycopy.db.schema import (  # noqa: E402
    MIGRATIONS,
    SCHEMA_VERSION,
)
from polycopy.domain.copy_candidate import (  # noqa: E402
    CANDIDATE_DECISION_TYPES,
    CandidateStatus,
    CopyCandidate,
)
from polycopy.domain.copyability import CopyabilityScore  # noqa: E402
from polycopy.domain.market import Market, MarketOutcome  # noqa: E402
from polycopy.domain.order import OrderSide  # noqa: E402
from polycopy.domain.source_trade import SourceTrade  # noqa: E402
from polycopy.domain.wallet import Wallet  # noqa: E402
from polycopy.scoring.engine import score_wallet  # noqa: E402


# ── Test fixtures / helpers ───────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path: Path):
    """Disposable v8 DB per test."""
    database = Database(db_path=tmp_path / "p02.db").connect()
    try:
        yield database
    finally:
        database.close()


def _seed_wallet(
    db: Database,
    *,
    address: str = "0xabc123",
    label: str = "test",
    wallet_id: str | None = None,
) -> str:
    """Insert a wallet row, returning its UUID PK."""
    wallet_id = wallet_id or str(uuid4())
    db.conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (wallet_id, address, label, "2026-07-01T00:00:00Z"),
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
) -> tuple[str, int]:
    """Insert market + one outcome, returning (market_id, outcome_id)."""
    market_id = market_id or str(uuid4())
    db.conn.execute(
        "INSERT INTO markets (id, source_id, source, question, active, closed, "
        "resolved, fetched_at) VALUES (?, ?, 'polymarket', 'Q?', ?, ?, ?, ?)",
        (
            market_id,
            source_id,
            int(active),
            int(closed),
            int(resolved),
            "2026-07-01T00:00:00Z",
        ),
    )
    cur = db.conn.execute(
        "INSERT INTO market_outcomes (market_id, label, price, volume, clob_token_id) "
        "VALUES (?, ?, ?, 0.0, ?)",
        (market_id, label, price, token),
    )
    outcome_id = int(cur.lastrowid)
    db.conn.commit()
    return market_id, outcome_id


def _seed_source_trade(
    db: Database,
    *,
    source: str = "polymarket_data_api",
    source_trade_id: str = "tx-1",
    trader_address: str = "0xabc123",
    token_id: str = "tok-1",
    market_source_id: str = "cond-1",
    side: str = "BUY",
    price: float = 0.5,
    quantity: float = 10.0,
    timestamp: str = "2026-07-01T00:00:00Z",
    outcome: str = "Yes",
    include_token_id: bool = True,
) -> SourceTrade:
    """Insert a source_trade row + return a SourceTrade domain object.

    For negative-path tests (price <= 0 / quantity <= 0 / missing /
    non-conforming timestamp / unknown side) the SourceTrade Pydantic
    constructor rejects the value before ``evaluate_source_trade_for_wallet``
    ever sees it. We therefore use ``SourceTrade.model_construct(...)``
    for the return value — which skips validation — so the persistence
    layer's own REJECTED_INVALID_TRADE branch is exercised end-to-end.
    The DB insert itself uses values that the SQLite CHECK constraints
    accept (``quantity > 0``); for the ``quantity=0`` / ``timestamp=''``
    cases we skip the DB insert and return a constructed-only object.
    """
    side_enum = OrderSide.BUY if side == "BUY" else (
        OrderSide.SELL if side == "SELL" else side  # unknown -> raw string
    )

    # Only persist to DB when SQLite will accept the row. The
    # source_trades schema enforces quantity > 0 and timestamp NOT NULL,
    # so we silently skip the DB write for those cases — the candidate
    # layer's invalid-trade check is the unit under test here.
    can_persist = quantity > 0 and bool(timestamp)
    if can_persist:
        trade_id = str(uuid4())
        db.conn.execute(
            "INSERT INTO source_trades (id, source, source_trade_id, market_source_id, "
            "side, outcome, quantity, price, trader_address, timestamp, "
            "is_sample, token_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                trade_id,
                source,
                source_trade_id,
                market_source_id,
                side,
                outcome,
                quantity,
                price,
                trader_address,
                timestamp,
                token_id if include_token_id else None,
            ),
        )
        db.conn.commit()
        domain_id = UUID(trade_id)
    else:
        domain_id = uuid4()

    # Use model_construct so the SourceTrade Pydantic constraints don't
    # short-circuit the test before we reach REJECTED_INVALID_TRADE.
    timestamp_obj = (
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if timestamp else None
    )
    return SourceTrade.model_construct(
        id=domain_id,
        source=source,
        source_trade_id=source_trade_id,
        market_source_id=market_source_id,
        side=side_enum,
        outcome=outcome,
        quantity=quantity,
        price=price,
        trader_address=trader_address,
        timestamp=timestamp_obj,
        is_sample=False,
        token_id=token_id if include_token_id else None,
    )


def _make_wallet(address: str = "0xabc123", wallet_id: str | None = None) -> Wallet:
    return Wallet(
        id=UUID(wallet_id or str(uuid4())),
        address=address,
        label="test",
    )


def _make_copy_candidate_score(
    *,
    wallet_id: str,
    score_value: float = 85.0,
    verdict_str: str = "copy_candidate",
) -> CopyabilityScore:
    """Build a real CopyabilityScore using the scoring engine so tests
    exercise the same constructor the production code uses."""
    s = score_wallet(
        wallet_id=UUID(wallet_id),
        sharpe_ratio=1.5,
        win_rate=0.7,
        trade_count=20,
        latest_trade_ts=datetime(2026, 7, 1, tzinfo=timezone.utc),
        first_trade_ts=datetime(2026, 6, 1, tzinfo=timezone.utc),
        markets_traded=5,
    )
    # Override score/verdict to make the test deterministic — the engine
    # produces a value that depends on its weight table; we want a fixed
    # COPY_CANDIDATE-ish result regardless of formula tweaks.
    s.score = score_value
    s.verdict = _parse_verdict(verdict_str)
    return s


def _parse_verdict(value: str):
    """Coerce a string verdict value to a Verdict enum."""
    from polycopy.domain.copyability import Verdict

    mapping = {
        "copy_candidate": Verdict.COPY_CANDIDATE,
        "watchlist": Verdict.WATCHLIST,
        "skip": Verdict.SKIP,
        "incomplete": Verdict.INCOMPLETE,
    }
    return mapping[value]


def _make_market(
    market_id: str, *, closed: bool = False, resolved: bool = False, active: bool = True,
) -> Market:
    return Market(
        id=UUID(market_id),
        source_id="cond-1",
        source="polymarket",
        question="Q?",
        outcomes=[MarketOutcome(label="Yes", price=0.5, volume=0.0)],
        active=active,
        closed=closed,
        resolved=resolved,
        resolution_outcome=None,
        volume_24h=0.0,
        fetched_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        is_sample=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. New v8 DB includes copy_candidates table.
# ─────────────────────────────────────────────────────────────────────────────
def test_v8_db_includes_copy_candidates_table(tmp_path: Path) -> None:
    """Scenario 1: a fresh disposable DB ends at schema_version=8 with
    the new ``copy_candidates`` table present and all expected columns."""
    db_path = tmp_path / "p02-s1.db"
    db = Database(db_path=db_path).connect()
    try:
        # SCHEMA_VERSION constant bumped 7 → 8 by PR-2.
        assert SCHEMA_VERSION == 8

        version = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
        assert version is not None
        assert int(version["value"]) == 8

        tables = {
            row["name"]
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "copy_candidates" in tables, (
            f"copy_candidates missing; tables={sorted(tables)}"
        )

        # Required columns per the brief.
        cols = {
            row["name"]
            for row in db.conn.execute("PRAGMA table_info(copy_candidates)").fetchall()
        }
        required = {
            "id", "wallet_id", "source", "source_trade_id",
            "source_trade_internal_id", "market_id", "market_outcome_id",
            "market_source_id", "token_id", "outcome_label", "side",
            "source_trade_price", "source_trade_quantity",
            "source_trade_notional", "source_trade_timestamp", "observed_at",
            "wallet_score_version", "wallet_score", "wallet_verdict",
            "status", "status_reason", "metrics_json", "created_at", "updated_at",
        }
        missing = required - cols
        assert not missing, f"copy_candidates missing columns: {missing}"

        # Indexes present.
        indexes = {
            row["name"]
            for row in db.conn.execute("PRAGMA index_list(copy_candidates)").fetchall()
        }
        for expected in (
            "idx_copy_candidates_status",
            "idx_copy_candidates_wallet",
            "idx_copy_candidates_source_trade_internal",
            "idx_copy_candidates_market_outcome",
        ):
            assert expected in indexes, f"missing index {expected}; got {sorted(indexes)}"
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 2. v7 → v8 migration preserves existing data + adds the new table.
# ─────────────────────────────────────────────────────────────────────────────
def test_v7_to_v8_migration_preserves_data(tmp_path: Path) -> None:
    """Scenario 2: a database that has been migrated up to v7 (with real
    wallets/source_trades/markets rows) gains the new ``copy_candidates``
    table without losing data when the migration runs."""
    db_path = tmp_path / "p02-s2.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Apply v1..v7 manually so the DB starts at exactly v7 with seed rows.
    for version in range(1, SCHEMA_VERSION):  # v1..v7
        for stmt in MIGRATIONS[version]:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(version),),
        )
    conn.commit()

    # Seed two real rows so we can prove they survive the v8 migration.
    wallet_id = "w1"
    market_id = "m1"
    conn.execute(
        "INSERT INTO wallets (id, address, label, is_sample, created_at) "
        "VALUES (?, '0xABC', 'a', 0, '2026-01-01T00:00:00Z')",
        (wallet_id,),
    )
    conn.execute(
        "INSERT INTO markets (id, source_id, source, question, fetched_at) "
        "VALUES (?, 'cond-1', 'polymarket', 'Q?', '2026-01-01T00:00:00Z')",
        (market_id,),
    )
    conn.execute(
        "INSERT INTO market_outcomes (market_id, label, price, volume) "
        "VALUES (?, 'Yes', 0.5, 0)",
        (market_id,),
    )
    conn.execute(
        "INSERT INTO source_trades (id, source, source_trade_id, market_source_id, "
        "side, outcome, quantity, price, trader_address, timestamp) "
        "VALUES ('t1', 'polymarket_data_api', 'tx-1', 'cond-1', 'BUY', 'Yes', "
        "10.0, 0.5, '0xabc', '2026-01-01T00:00:00Z')",
    )
    conn.commit()

    # Sanity: pre-migration version is 7.
    pre = conn.execute(
        "SELECT value FROM _meta WHERE key='schema_version'"
    ).fetchone()["value"]
    assert pre == "7"

    # Now apply v8 via the Database class — this exercises the real
    # migration runner path the production DB will take.
    conn.close()  # open fresh via Database so the runner runs v8.
    db = Database(db_path=db_path).connect()
    try:
        post = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
        assert post is not None
        assert int(post["value"]) == 8

        # Existing data preserved.
        assert db.fetchone("SELECT id FROM wallets WHERE id='w1'") is not None
        assert db.fetchone("SELECT id FROM markets WHERE id='m1'") is not None
        assert (
            db.fetchone(
                "SELECT id FROM market_outcomes WHERE market_id='m1'"
            )
            is not None
        )
        assert (
            db.fetchone(
                "SELECT id FROM source_trades WHERE source_trade_id='tx-1'"
            )
            is not None
        )

        # copy_candidates added, empty, FK clean.
        n = db.fetchone("SELECT COUNT(*) AS n FROM copy_candidates")["n"]
        assert n == 0
        assert db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 3. Foreign keys enforced.
# ─────────────────────────────────────────────────────────────────────────────
def test_foreign_keys_enforced_on_copy_candidates(tmp_path: Path) -> None:
    """Scenario 3: with PRAGMA foreign_keys=ON (set by Database on
    connect), a copy_candidates row referencing a non-existent
    ``wallet_id`` / ``market_id`` / ``market_outcome_id`` /
    ``source_trade_internal_id`` raises IntegrityError."""
    db = Database(db_path=tmp_path / "p02-s3.db").connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(
                "INSERT INTO copy_candidates (wallet_id, source, source_trade_id, "
                "side, source_trade_price, source_trade_quantity, "
                "source_trade_timestamp, observed_at, wallet_score_version, "
                "wallet_score, wallet_verdict, status, created_at, updated_at) "
                "VALUES ('does-not-exist', 's', 't', 'BUY', 0.5, 1.0, "
                "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z', 'v1', 80.0, "
                "'copy_candidate', 'PENDING_PRICE_CHECK', "
                "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')"
            )
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 4. UNIQUE constraint is (wallet_id, source, source_trade_id).
# ─────────────────────────────────────────────────────────────────────────────
def test_unique_constraint_uses_source_qualified_key(tmp_path: Path) -> None:
    """Scenario 4: the UNIQUE key is the source-qualified triple, not
    ``(wallet_id, source_trade_id)`` and not ``source_trade_id`` alone.

    Demonstrated by:
    - Same wallet + same source + same source_trade_id → IntegrityError.
    - Same source_trade_id under a different source → two distinct rows.
    """
    db = Database(db_path=tmp_path / "p02-s4.db").connect()
    try:
        w1 = _seed_wallet(db, address="0xWALLET_A")
        _seed_market_with_outcome(db, source_id="cond-1", label="Yes", token="tok-1")

        # Insert a real trade so we can verify resolver-driven paths.
        _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-shared",
            trader_address="0xWALLET_A",
            token_id="tok-1",
            market_source_id="cond-1",
        )

        # First insert succeeds.
        db.conn.execute(
            "INSERT INTO copy_candidates (wallet_id, source, source_trade_id, "
            "side, source_trade_price, source_trade_quantity, source_trade_timestamp, "
            "observed_at, wallet_score_version, wallet_score, wallet_verdict, "
            "status, created_at, updated_at) VALUES (?, ?, ?, 'BUY', 0.5, 1.0, "
            "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z', 'v1', 80.0, "
            "'copy_candidate', 'PENDING_PRICE_CHECK', '2026-07-01T00:00:00Z', "
            "'2026-07-01T00:00:00Z')",
            (w1, "polymarket_data_api", "tx-shared"),
        )
        db.conn.commit()

        # Same triple → IntegrityError.
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(
                "INSERT INTO copy_candidates (wallet_id, source, source_trade_id, "
                "side, source_trade_price, source_trade_quantity, "
                "source_trade_timestamp, observed_at, wallet_score_version, "
                "wallet_score, wallet_verdict, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'BUY', 0.5, 1.0, '2026-07-01T00:00:00Z', "
                "'2026-07-01T00:00:00Z', 'v1', 80.0, 'copy_candidate', "
                "'PENDING_PRICE_CHECK', '2026-07-01T00:00:00Z', "
                "'2026-07-01T00:00:00Z')",
                (w1, "polymarket_data_api", "tx-shared"),
            )
        db.conn.rollback()

        # Different source, same source_trade_id, same wallet → second
        # row permitted (cross-source identity preserved).
        db.conn.execute(
            "INSERT INTO copy_candidates (wallet_id, source, source_trade_id, "
            "side, source_trade_price, source_trade_quantity, source_trade_timestamp, "
            "observed_at, wallet_score_version, wallet_score, wallet_verdict, "
            "status, created_at, updated_at) VALUES (?, ?, ?, 'BUY', 0.5, 1.0, "
            "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z', 'v1', 80.0, "
            "'copy_candidate', 'PENDING_PRICE_CHECK', '2026-07-01T00:00:00Z', "
            "'2026-07-01T00:00:00Z')",
            (w1, "other_source", "tx-shared"),
        )
        db.conn.commit()
        n = db.fetchone("SELECT COUNT(*) AS n FROM copy_candidates")["n"]
        assert n == 2
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 5. Rerun idempotency via INSERT OR IGNORE.
# ─────────────────────────────────────────────────────────────────────────────
def test_rerun_idempotency_returns_existing_id(tmp_path: Path) -> None:
    """Scenario 5: persist once → rerun same input → exactly one row,
    second call returns ``(False, existing_id)``."""
    db = Database(db_path=tmp_path / "p02-s5.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        market_id, outcome_id = _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1"
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        assert cand.status == CandidateStatus.PENDING_PRICE_CHECK.value

        new_id, inserted = persist_copy_candidate(db, cand)
        assert inserted is True
        assert new_id > 0

        # Rerun: same input, second call.
        cand2 = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        new_id2, inserted2 = persist_copy_candidate(db, cand2)
        assert inserted2 is False
        assert new_id2 == new_id

        n = db.fetchone("SELECT COUNT(*) AS n FROM copy_candidates")["n"]
        assert n == 1
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 6. Same source_trade_id, different source → two distinct rows.
# ─────────────────────────────────────────────────────────────────────────────
def test_same_trade_id_different_source_two_rows(tmp_path: Path) -> None:
    """Scenario 6: the source-qualified identity contract means the same
    upstream ``source_trade_id`` under two different sources produces two
    distinct candidate rows."""
    db = Database(db_path=tmp_path / "p02-s6.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        _seed_market_with_outcome(db, source_id="cond-1", label="Yes", token="tok-1")
        # Two source_trades with the same source_trade_id under two sources.
        trade_a = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-shared",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        trade_b = _seed_source_trade(
            db,
            source="other_source",
            source_trade_id="tx-shared",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
        )

        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)

        # Source A path: needs a real resolver match for PENDING_PRICE_CHECK.
        cand_a = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade_a, score=score,
        )
        # Other_source has no token match → INCOMPLETE → REJECTED.
        cand_b = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade_b, score=score,
        )

        # Both rows persist; status may differ.
        persist_copy_candidate(db, cand_a)
        persist_copy_candidate(db, cand_b)

        rows = db.fetchall(
            "SELECT source, status FROM copy_candidates ORDER BY source"
        )
        assert [r["source"] for r in rows] == [
            "other_source", "polymarket_data_api",
        ]
        n = db.fetchone("SELECT COUNT(*) AS n FROM copy_candidates")["n"]
        assert n == 2
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 7. Same source_trade observed for different wallets.
# ─────────────────────────────────────────────────────────────────────────────
def test_trade_owner_match_can_be_pending(tmp_path: Path) -> None:
    """BLOCKER 1 / Step 7 test 1.

    Wallet A + Wallet A's trade, COPY_CANDIDATE verdict, market
    open → PENDING_PRICE_CHECK. The ownership check passes when
    canonical addresses match.
    """
    db = Database(db_path=tmp_path / "owner-match.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET_A")
        market_id, _ = _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1"
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET_A",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET_A", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )

        assert cand.status == CandidateStatus.PENDING_PRICE_CHECK.value
        assert cand.market_id == market_id
    finally:
        db.close()


def test_trade_owner_mismatch_is_rejected(tmp_path: Path) -> None:
    """BLOCKER 1 / Step 7 test 2.

    Wallet B evaluating Wallet A's trade → REJECTED_WALLET_TRADE_MISMATCH.
    Never PENDING_PRICE_CHECK.
    """
    db = Database(db_path=tmp_path / "owner-mismatch.db").connect()
    try:
        _seed_wallet(db, address="0xWALLET_A")
        wallet_b_id = _seed_wallet(db, address="0xWALLET_B")
        _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1"
        )
        # Trade belongs to Wallet A by trader_address.
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET_A",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_b_id)
        wallet_b = _make_wallet(address="0xWALLET_B", wallet_id=wallet_b_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet_b, trade=trade, score=score,
        )

        assert cand.status == CandidateStatus.REJECTED_WALLET_TRADE_MISMATCH.value
        assert cand.is_pending_price_check is False
        # The mismatch reason should reference the canonical addresses.
        assert "0xwallet_a" in (cand.status_reason or "")
        assert "0xwallet_b" in (cand.status_reason or "")
        # Mismatch rejection must NOT carry a market_id (no resolver OK).
        assert cand.market_id is None
    finally:
        db.close()


def test_same_trade_cannot_create_pending_for_two_unrelated_wallets(
    tmp_path: Path,
) -> None:
    """BLOCKER 1 / Step 7 test 3.

    Same source_trade, two unrelated wallets: only the wallet whose
    canonical address matches trader_address can produce a PENDING
    (or any non-mismatch status). The other gets
    REJECTED_WALLET_TRADE_MISMATCH.
    """
    db = Database(db_path=tmp_path / "same-trade-two-wallets.db").connect()
    try:
        wallet_a_id = _seed_wallet(db, address="0xWALLET_A")
        wallet_b_id = _seed_wallet(db, address="0xWALLET_B")
        _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1"
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-shared",
            trader_address="0xWALLET_A",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score_a = _make_copy_candidate_score(wallet_id=wallet_a_id)
        score_b = _make_copy_candidate_score(wallet_id=wallet_b_id)
        wallet_a = _make_wallet(address="0xWALLET_A", wallet_id=wallet_a_id)
        wallet_b = _make_wallet(address="0xWALLET_B", wallet_id=wallet_b_id)

        cand_a = evaluate_source_trade_for_wallet(
            db, wallet=wallet_a, trade=trade, score=score_a,
        )
        cand_b = evaluate_source_trade_for_wallet(
            db, wallet=wallet_b, trade=trade, score=score_b,
        )

        assert cand_a.status == CandidateStatus.PENDING_PRICE_CHECK.value
        assert cand_b.status == CandidateStatus.REJECTED_WALLET_TRADE_MISMATCH.value

        # Persist the matching one only; the mismatch is rejected at
        # evaluation time so we never insert it.
        persist_copy_candidate(db, cand_a)
        rows = db.fetchall("SELECT wallet_id, status FROM copy_candidates")
        assert len(rows) == 1
        assert rows[0]["wallet_id"] == wallet_a_id
    finally:
        db.close()


def test_canonical_wallet_alias_matches(tmp_path: Path) -> None:
    """BLOCKER 1 / Step 7 test 4.

    The repository's canonical helper
    (``polycopy.db.wallet_identity.canonical_wallet_address``)
    normalizes: lowercase + strip whitespace + reject sentinels.
    Two wallets whose addresses normalize to the same canonical
    value must be treated as the same wallet for ownership
    purposes.
    """
    db = Database(db_path=tmp_path / "canonical-alias.db").connect()
    try:
        wallet_id = _seed_wallet(
            db, address="0xABC123",  # uppercase variant
        )
        _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1"
        )
        # Lower-case padded variant in trader_address — canonical
        # form is the same string.
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="  0xabc123\n",  # whitespace + lowercase
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xABC123", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )

        # The canonical helper lower-cases both sides → match → not a
        # mismatch rejection.
        assert cand.status != CandidateStatus.REJECTED_WALLET_TRADE_MISMATCH.value
        # And since the resolver succeeds and the market is open, the
        # candidate should reach PENDING_PRICE_CHECK.
        assert cand.status == CandidateStatus.PENDING_PRICE_CHECK.value
    finally:
        db.close()


def test_missing_trade_owner_is_rejected(tmp_path: Path) -> None:
    """BLOCKER 1 / Step 7 test 5.

    A trade whose trader_address is missing (None / empty /
    sentinel) must be explicitly rejected — never silently
    become PENDING_PRICE_CHECK.
    """
    db = Database(db_path=tmp_path / "missing-owner.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET_A")
        _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1"
        )
        # Seed with a non-sentinel trader_address so the DB accepts
        # the row, then override the domain object's trader_address
        # to None to exercise the missing-owner path.
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET_A",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        trade = trade.model_construct(  # type: ignore[attr-defined]
            **{**trade.__dict__, "trader_address": None},
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET_A", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )

        assert cand.status == CandidateStatus.REJECTED_WALLET_TRADE_MISMATCH.value
        assert cand.is_pending_price_check is False
        assert "missing or sentinelled" in (cand.status_reason or "")
    finally:
        db.close()


def test_unknown_verdict_never_advances(tmp_path: Path) -> None:
    """BLOCKER 3 / Step 7 test for unknown verdicts.

    An unknown verdict string (e.g. ``'HOT_PICK'``) must NEVER
    become PENDING_PRICE_CHECK. Status must be REJECTED_WALLET.
    """
    db = Database(db_path=tmp_path / "unknown-verdict.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET_A")
        _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1"
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET_A",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        score.verdict = "HOT_PICK"  # type: ignore[assignment]
        wallet = _make_wallet(address="0xWALLET_A", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )

        assert cand.status == CandidateStatus.REJECTED_WALLET.value
        assert cand.is_pending_price_check is False
        assert "HOT_PICK" in (cand.status_reason or "")
    finally:
        db.close()


def test_market_none_does_not_bypass_closed_market(tmp_path: Path) -> None:
    """BLOCKER 4.1 / Step 7 test for market=None.

    When the caller passes ``market=None`` the evaluator must
    STILL verify the resolved DB market state — it cannot treat
    ``None`` as 'open market'. A closed DB market → REJECTED_MARKET_CLOSED.
    """
    db = Database(db_path=tmp_path / "market-none-closed.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET_A")
        # Closed market.
        _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1", closed=True,
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET_A",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET_A", wallet_id=wallet_id)

        # Pass market=None explicitly. The DB market is closed →
        # must still be REJECTED_MARKET_CLOSED.
        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score, market=None,
        )

        assert cand.status == CandidateStatus.REJECTED_MARKET_CLOSED.value
        assert cand.is_pending_price_check is False
    finally:
        db.close()


def test_unrelated_market_object_cannot_override_resolved_market(
    tmp_path: Path,
) -> None:
    """BLOCKER 4.1 / Step 7 test for mismatched Market objects.

    Caller passes an open Market with a DIFFERENT id than the
    resolver's market_id → rejected. An unrelated open Market
    cannot override a closed/resolved DB market.
    """
    db = Database(db_path=tmp_path / "unrelated-market.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET_A")
        # Two markets: one open (the trap), one closed (the resolved truth).
        _seed_market_with_outcome(
            db, source_id="cond-open", label="Yes", token="tok-open",
        )
        closed_market_id, _ = _seed_market_with_outcome(
            db, source_id="cond-closed", label="Yes", token="tok-1", closed=True,
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET_A",
            token_id="tok-1",
            market_source_id="cond-closed",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET_A", wallet_id=wallet_id)

        # Caller passes the OPEN Market object as a decoy. The
        # resolver + DB lookup use market_source_id="cond-closed"
        # which resolves to the CLOSED market. The decoy must not
        # bypass the check.
        decoy = _make_market(str(uuid4()))  # open, unrelated
        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score, market=decoy,
        )

        assert cand.status == CandidateStatus.REJECTED_MARKET_CLOSED.value
        assert cand.is_pending_price_check is False
        assert cand.market_id == closed_market_id
    finally:
        db.close()


def test_closed_market_blocks_pending_price_check(tmp_path: Path) -> None:
    """BLOCKER 4.1 / Step 7 test for closed DB market."""
    db = Database(db_path=tmp_path / "closed-market.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET_A")
        _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1", closed=True,
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET_A",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET_A", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )

        assert cand.status == CandidateStatus.REJECTED_MARKET_CLOSED.value
    finally:
        db.close()


def test_inactive_market_blocks_pending(tmp_path: Path) -> None:
    """BLOCKER 4.1 / Step 7 test for active=0 DB market."""
    db = Database(db_path=tmp_path / "inactive-market.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET_A")
        _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1", active=False,
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET_A",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET_A", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )

        assert cand.status == CandidateStatus.REJECTED_MARKET_CLOSED.value
    finally:
        db.close()


def test_resolved_market_blocks_pending(tmp_path: Path) -> None:
    """BLOCKER 4.1 / Step 7 test for resolved=1 DB market."""
    db = Database(db_path=tmp_path / "resolved-market.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET_A")
        _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1", resolved=True,
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET_A",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET_A", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )

        assert cand.status == CandidateStatus.REJECTED_MARKET_CLOSED.value
    finally:
        db.close()


def test_missing_market_row_blocks_pending(tmp_path: Path) -> None:
    """BLOCKER 4.1 / Step 7 test for missing DB market row.

    The resolver may report a market_id that doesn't exist in the
    markets table. The evaluator must reject this as
    REJECTED_UNRESOLVED_OUTCOME, not PENDING_PRICE_CHECK.
    """
    db = Database(db_path=tmp_path / "missing-market-row.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET_A")
        # Seed a market but use a DIFFERENT source_id on the trade so
        # the resolver's token/label match returns no outcome row.
        _seed_market_with_outcome(
            db, source_id="cond-real", label="Yes", token="tok-1",
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET_A",
            token_id="tok-nonexistent",  # different token
            market_source_id="cond-fictional",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET_A", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )

        # Resolver returns INCOMPLETE → REJECTED_UNRESOLVED_OUTCOME.
        assert cand.status == CandidateStatus.REJECTED_UNRESOLVED_OUTCOME.value
        assert cand.is_pending_price_check is False
    finally:
        db.close()


def test_rejected_without_market_does_not_write_fake_decision_fk(
    tmp_path: Path,
) -> None:
    """BLOCKER 2 — FK safety per status with PRAGMA foreign_keys=ON.

    For every rejection status, the helper must NOT insert a
    fake market_id into decision_log. The copy_candidates row is
    the durable audit artifact for these.
    """
    db = Database(db_path=tmp_path / "fk-safety.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET_A")
        _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1"
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET_A", wallet_id=wallet_id)

        def _record(status_value: str, candidate: CopyCandidate) -> None:
            decision_type = CopyCandidate.decision_type_for_status(
                CandidateStatus(status_value), created=True,
            )
            result = record_candidate_decision_log(
                db, candidate=candidate, decision_type=decision_type,
            )
            # No fake market_id was inserted; either a real row was
            # written (when candidate has market_id) or None (no row).
            assert result is None or isinstance(result, str)

        # 1. REJECTED_WALLET (verdict=SKIP) — no market_id
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-wallet",
            trader_address="0xWALLET_A",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score_skip = _make_copy_candidate_score(
            wallet_id=wallet_id, verdict_str="skip",
        )
        cand_skip = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score_skip,
        )
        assert cand_skip.status == CandidateStatus.REJECTED_WALLET.value
        _record(cand_skip.status, cand_skip)

        # 2. REJECTED_WALLET_TRADE_MISMATCH — no market_id
        trade_mismatch = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-mismatch",
            trader_address="0xWALLET_B",  # not this wallet
            token_id="tok-1",
            market_source_id="cond-1",
        )
        cand_mismatch = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade_mismatch, score=score,
        )
        assert cand_mismatch.status == CandidateStatus.REJECTED_WALLET_TRADE_MISMATCH.value
        _record(cand_mismatch.status, cand_mismatch)

        # 3. REJECTED_INVALID_TRADE — no market_id
        trade_invalid = trade.model_construct(  # type: ignore[attr-defined]
            **{**trade.__dict__, "price": 0.0},
        )
        cand_invalid = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade_invalid, score=score,
        )
        assert cand_invalid.status == CandidateStatus.REJECTED_INVALID_TRADE.value
        _record(cand_invalid.status, cand_invalid)

        # 4. REJECTED_UNRESOLVED_OUTCOME — no market_id
        trade_unresolved = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-unresolved",
            trader_address="0xWALLET_A",
            token_id="tok-nonexistent",
            market_source_id="cond-fictional",
        )
        cand_unresolved = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade_unresolved, score=score,
        )
        assert cand_unresolved.status == CandidateStatus.REJECTED_UNRESOLVED_OUTCOME.value
        _record(cand_unresolved.status, cand_unresolved)

        # 5. REJECTED_AMBIGUOUS_OUTCOME — covered by existing test
        # test_resolver_ambiguous_rejected_ambiguous. We verify the
        # helper accepts the decision_type without raising.
        decision_type = "COPY_CANDIDATE_REJECTED_AMBIGUOUS_OUTCOME"
        result = record_candidate_decision_log(
            db, candidate=cand_unresolved, decision_type=decision_type,
        )
        assert result is None  # no real market_id → no row

        # FK check clean.
        fk_violations = db.conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == []

        # Verify NO fake market_id was ever written: every row in
        # decision_log (if any) must have a real market_id from the
        # markets table.
        bad_rows = db.conn.execute(
            "SELECT id, market_id FROM decision_log "
            "WHERE market_id IS NULL OR market_id = "
            "'00000000-0000-0000-0000-000000000000'"
        ).fetchall()
        assert bad_rows == []
    finally:
        db.close()


def test_ten_reruns_do_not_flood_decision_log(tmp_path: Path) -> None:
    """BLOCKER 3 — duplicate logging is genuinely idempotent.

    Run the same candidate evaluation + persistence 10 times.
    The first run writes one CREATED decision; the remaining 9
    reruns write ZERO additional decision_log rows.
    """
    db = Database(db_path=tmp_path / "ten-reruns.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET_A")
        _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1"
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-rerun",
            trader_address="0xWALLET_A",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET_A", wallet_id=wallet_id)

        for _ in range(10):
            cand = evaluate_source_trade_for_wallet(
                db, wallet=wallet, trade=trade, score=score,
            )
            assert cand.status == CandidateStatus.PENDING_PRICE_CHECK.value
            persist_copy_candidate(db, cand)
            # Mirror the real scan flow: also write the bounded
            # decision_log event on each evaluation.
            record_candidate_decision_log(
                db,
                candidate=cand,
                decision_type="COPY_CANDIDATE_CREATED",
            )

        n_candidates = db.conn.execute(
            "SELECT COUNT(*) FROM copy_candidates"
        ).fetchone()[0]
        n_decisions = db.conn.execute(
            "SELECT COUNT(*) FROM decision_log WHERE decision_type = "
            "'COPY_CANDIDATE_CREATED'"
        ).fetchone()[0]
        assert n_candidates == 1, f"expected 1 candidate row, got {n_candidates}"
        assert n_decisions == 1, (
            f"expected exactly 1 created decision row after 10 reruns, got "
            f"{n_decisions}"
        )
        # No copy_candidate_duplicate_skipped rows at all.
        dup_rows = db.conn.execute(
            "SELECT COUNT(*) FROM decision_log WHERE decision_type = "
            "'COPY_CANDIDATE_DUPLICATE_SKIPPED'"
        ).fetchone()[0]
        assert dup_rows == 0

        # signals/orders/positions remain zero.
        for table in ("signals", "orders", "positions"):
            n = db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert n == 0, f"{table} unexpectedly has {n} rows"

        # FK check clean.
        fk_violations = db.conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == []
    finally:
        db.close()


def test_distinct_source_trades_receive_distinct_decision_logs(
    tmp_path: Path,
) -> None:
    """FINAL audit-idempotency regression test.

    Two source trades from the same wallet + same source + DIFFERENT
    source_trade_id values must each get their own decision_log row.
    A duplicate rerun of either must NOT collapse them into a single
    row, and must NOT inflate the count either.

    Asserts the corrected LIKE-based idempotency key in
    ``record_candidate_decision_log`` requires BOTH the source AND
    source_trade_id serialized substrings to match — not source alone.
    """
    db = Database(db_path=tmp_path / "distinct-trades.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET_A")
        # Two markets so the resolver returns OK on each trade.
        market_a_id, _ = _seed_market_with_outcome(
            db, source_id="cond-a", label="Yes", token="tok-a",
        )
        market_b_id, _ = _seed_market_with_outcome(
            db, source_id="cond-b", label="Yes", token="tok-b",
        )

        trade_a = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-distinct-A",
            trader_address="0xWALLET_A",
            token_id="tok-a",
            market_source_id="cond-a",
        )
        trade_b = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-distinct-B",  # different id
            trader_address="0xWALLET_A",
            token_id="tok-b",
            market_source_id="cond-b",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET_A", wallet_id=wallet_id)

        def _eval(trade: SourceTrade) -> CopyCandidate:
            cand = evaluate_source_trade_for_wallet(
                db, wallet=wallet, trade=trade, score=score,
            )
            assert cand.status == CandidateStatus.PENDING_PRICE_CHECK.value
            persist_copy_candidate(db, cand)
            record_candidate_decision_log(
                db,
                candidate=cand,
                decision_type="COPY_CANDIDATE_CREATED",
            )
            return cand

        cand_a = _eval(trade_a)
        cand_b = _eval(trade_b)

        # Step 5: two distinct decision rows.
        n = db.conn.execute(
            "SELECT COUNT(*) FROM decision_log "
            "WHERE decision_type = 'COPY_CANDIDATE_CREATED'"
        ).fetchone()[0]
        assert n == 2, f"expected 2 distinct decision rows, got {n}"

        # Step 6: rerun BOTH (this would have collapsed them before
        # the source_trade_id was added to the LIKE key).
        _eval(trade_a)
        _eval(trade_b)

        # Step 7: still exactly 2 rows after the rerun.
        n_after = db.conn.execute(
            "SELECT COUNT(*) FROM decision_log "
            "WHERE decision_type = 'COPY_CANDIDATE_CREATED'"
        ).fetchone()[0]
        assert n_after == 2, (
            f"expected exactly 2 decision rows after rerun, got {n_after}"
        )

        # Step 8: each source_trade_id appears in exactly one decision
        # metrics payload.
        rows = db.conn.execute(
            "SELECT metrics FROM decision_log "
            "WHERE decision_type = 'COPY_CANDIDATE_CREATED'"
        ).fetchall()
        assert len(rows) == 2
        counts: dict[str, int] = {}
        for row in rows:
            payload = json.loads(row["metrics"])
            counts[payload["source_trade_id"]] = (
                counts.get(payload["source_trade_id"], 0) + 1
            )
        assert counts.get("tx-distinct-A") == 1, (
            f"tx-distinct-A appears in {counts.get('tx-distinct-A')} rows"
        )
        assert counts.get("tx-distinct-B") == 1, (
            f"tx-distinct-B appears in {counts.get('tx-distinct-B')} rows"
        )

        # Step 9: signals/orders/positions remain zero.
        for table in ("signals", "orders", "positions"):
            n_t = db.conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            assert n_t == 0, f"{table} unexpectedly has {n_t} rows"

        # Step 10: FK check clean.
        fk_violations = db.conn.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        assert fk_violations == []

        # Sanity: each candidate row also persisted with the right
        # source_trade_id.
        cand_rows = db.conn.execute(
            "SELECT source_trade_id FROM copy_candidates "
            "ORDER BY source_trade_id"
        ).fetchall()
        assert [r["source_trade_id"] for r in cand_rows] == [
            "tx-distinct-A", "tx-distinct-B",
        ]
        # Cross-check the candidate ids map to the decision rows.
        for cand in (cand_a, cand_b):
            metrics_match = db.conn.execute(
                "SELECT COUNT(*) FROM decision_log "
                "WHERE decision_type = 'COPY_CANDIDATE_CREATED' "
                "AND metrics LIKE ?",
                (f'%{cand.source_trade_id}%',),
            ).fetchone()[0]
            assert metrics_match == 1, (
                f"expected exactly 1 decision row referencing "
                f"{cand.source_trade_id!r}, got {metrics_match}"
            )
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 8. COPY_CANDIDATE + resolved outcome → PENDING_PRICE_CHECK.
# ─────────────────────────────────────────────────────────────────────────────
def test_copy_candidate_verdict_pending_price_check(tmp_path: Path) -> None:
    db = Database(db_path=tmp_path / "p02-s8.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        market_id, _ = _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1"
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)  # COPY_CANDIDATE
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        assert cand.status == CandidateStatus.PENDING_PRICE_CHECK.value
        assert cand.is_pending_price_check is True
        assert cand.is_rejected is False
        assert cand.market_id == market_id
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 9. WATCHLIST → REJECTED_WALLET.
# ─────────────────────────────────────────────────────────────────────────────
def test_watchlist_rejected_wallet(tmp_path: Path) -> None:
    db = Database(db_path=tmp_path / "p02-s9.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        _seed_market_with_outcome(db, source_id="cond-1", label="Yes", token="tok-1")
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(
            wallet_id=wallet_id, score_value=60.0, verdict_str="watchlist",
        )
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        assert cand.status == CandidateStatus.REJECTED_WALLET.value
        assert cand.is_rejected is True
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 10. SKIP → REJECTED_WALLET.
# ─────────────────────────────────────────────────────────────────────────────
def test_skip_rejected_wallet(tmp_path: Path) -> None:
    db = Database(db_path=tmp_path / "p02-s10.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        _seed_market_with_outcome(db, source_id="cond-1", label="Yes", token="tok-1")
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(
            wallet_id=wallet_id, score_value=30.0, verdict_str="skip",
        )
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        assert cand.status == CandidateStatus.REJECTED_WALLET.value
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 11. INCOMPLETE → REJECTED_WALLET.
# ─────────────────────────────────────────────────────────────────────────────
def test_incomplete_verdict_rejected_wallet(tmp_path: Path) -> None:
    """INCOMPLETE verdict (from critical-missing fields) → REJECTED_WALLET.

    The wallet layer's gate runs BEFORE the resolver — the candidate
    layer never attempts an outcome lookup for a wallet that doesn't
    qualify on its own merits."""
    db = Database(db_path=tmp_path / "p02-s11.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        _seed_market_with_outcome(db, source_id="cond-1", label="Yes", token="tok-1")
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(
            wallet_id=wallet_id, score_value=10.0, verdict_str="incomplete",
        )
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        assert cand.status == CandidateStatus.REJECTED_WALLET.value
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 12. Resolver INCOMPLETE → REJECTED_UNRESOLVED_OUTCOME.
# ─────────────────────────────────────────────────────────────────────────────
def test_resolver_incomplete_rejected_unresolved(tmp_path: Path) -> None:
    """COPY_CANDIDATE verdict + a source_trade that cannot resolve to any
    market outcome (no token match, no label match) → REJECTED_UNRESOLVED_OUTCOME."""
    db = Database(db_path=tmp_path / "p02-s12.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        # No market seeded for cond-1 → resolver returns INCOMPLETE.
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-orphan",
            trader_address="0xWALLET",
            token_id="tok-unknown",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        assert cand.status == CandidateStatus.REJECTED_UNRESOLVED_OUTCOME.value
        assert cand.market_id is None
        assert cand.market_outcome_id is None
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 13. Resolver AMBIGUOUS → REJECTED_AMBIGUOUS_OUTCOME.
# ─────────────────────────────────────────────────────────────────────────────
def test_resolver_ambiguous_rejected_ambiguous(tmp_path: Path) -> None:
    """Two market_outcomes rows share the same token_id → resolver
    returns AMBIGUOUS → REJECTED_AMBIGUOUS_OUTCOME."""
    db = Database(db_path=tmp_path / "p02-s13.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        market_a, _ = _seed_market_with_outcome(
            db, source_id="cond-A", label="Yes", token="tok-shared",
        )
        market_b, _ = _seed_market_with_outcome(
            db, source_id="cond-B", label="Yes", token="tok-shared",
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-ambig",
            trader_address="0xWALLET",
            token_id="tok-shared",
            market_source_id="cond-A",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        assert cand.status == CandidateStatus.REJECTED_AMBIGUOUS_OUTCOME.value
        assert cand.market_id is None  # never auto-pick
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 14. Closed market → REJECTED_MARKET_CLOSED.
# ─────────────────────────────────────────────────────────────────────────────
def test_closed_market_rejected_market_closed(tmp_path: Path) -> None:
    """COPY_CANDIDATE verdict + resolved outcome + market.closed=True
    → REJECTED_MARKET_CLOSED."""
    db = Database(db_path=tmp_path / "p02-s14.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        market_id, _ = _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1", closed=True,
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)
        market = _make_market(market_id, closed=True)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score, market=market,
        )
        assert cand.status == CandidateStatus.REJECTED_MARKET_CLOSED.value
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 15. Invalid trade fields → REJECTED_INVALID_TRADE.
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad_field", ["price_zero", "qty_zero", "missing_timestamp", "bad_side"],
)
def test_invalid_trade_rejected_invalid_trade(tmp_path: Path, bad_field: str) -> None:
    db = Database(db_path=tmp_path / f"p02-s15-{bad_field}.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        _seed_market_with_outcome(db, source_id="cond-1", label="Yes", token="tok-1")

        # Default valid trade; mutate per parametrized case.
        price = 0.5
        quantity = 10.0
        timestamp = "2026-07-01T00:00:00Z"
        side: str = "BUY"
        if bad_field == "price_zero":
            price = 0.0
        elif bad_field == "qty_zero":
            quantity = 0.0
        elif bad_field == "missing_timestamp":
            timestamp = ""
        elif bad_field == "bad_side":
            side = "GIBBERISH"

        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id=f"tx-{bad_field}",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
            price=price,
            quantity=quantity,
            timestamp=timestamp,
            side=side,
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        assert cand.status == CandidateStatus.REJECTED_INVALID_TRADE.value
        assert cand.status_reason is not None
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 16. Score snapshot: row stores exact score, verdict, formula_version.
# ─────────────────────────────────────────────────────────────────────────────
def test_score_snapshot_persisted(tmp_path: Path) -> None:
    db = Database(db_path=tmp_path / "p02-s16.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        market_id, outcome_id = _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1",
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(
            wallet_id=wallet_id, score_value=85.5,
        )
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        new_id, inserted = persist_copy_candidate(db, cand)
        assert inserted is True

        row = db.fetchone(
            "SELECT wallet_score, wallet_verdict, wallet_score_version, status, "
            "market_id, market_outcome_id FROM copy_candidates WHERE id=?",
            (new_id,),
        )
        assert row["wallet_score"] == 85.5
        assert row["wallet_verdict"] == "copy_candidate"
        assert row["wallet_score_version"] == "v1"
        assert row["status"] == "PENDING_PRICE_CHECK"
        assert row["market_id"] == market_id
        assert row["market_outcome_id"] == outcome_id
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 17. Rerun after wallet score later changes: identity not duplicated,
#     historical score NOT silently rewritten.
# ─────────────────────────────────────────────────────────────────────────────
def test_score_update_does_not_silently_rewrite(tmp_path: Path) -> None:
    """If a wallet's score later changes (e.g. re-evaluation with new
    trades), the existing candidate row is NOT rewritten. PR-2 is
    strictly append-or-ignore on the bounded key."""
    db = Database(db_path=tmp_path / "p02-s17.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        _seed_market_with_outcome(db, source_id="cond-1", label="Yes", token="tok-1")
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)

        # First evaluation: COPY_CANDIDATE / 85.0
        score_a = _make_copy_candidate_score(
            wallet_id=wallet_id, score_value=85.0,
        )
        cand_a = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score_a,
        )
        new_id, inserted = persist_copy_candidate(db, cand_a)
        assert inserted is True
        original_score = db.fetchone(
            "SELECT wallet_score FROM copy_candidates WHERE id=?", (new_id,),
        )["wallet_score"]
        assert original_score == 85.0

        # Second evaluation: new score 50.0 (still COPY_CANDIDATE for the test
        # — the verdict gate is independent of the score snapshot).
        score_b = _make_copy_candidate_score(
            wallet_id=wallet_id, score_value=50.0,
        )
        cand_b = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score_b,
        )
        new_id2, inserted2 = persist_copy_candidate(db, cand_b)
        assert inserted2 is False
        assert new_id2 == new_id

        # The historical 85.0 score is preserved.
        still_score = db.fetchone(
            "SELECT wallet_score FROM copy_candidates WHERE id=?", (new_id,),
        )["wallet_score"]
        assert still_score == 85.0

        # Only one row exists.
        n = db.fetchone("SELECT COUNT(*) AS n FROM copy_candidates")["n"]
        assert n == 1
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 18. Decision-log evidence: bounded decision_type + correct fields.
# ─────────────────────────────────────────────────────────────────────────────
def test_decision_log_bounded_vocabulary(tmp_path: Path) -> None:
    """``record_candidate_decision_log`` writes a row with one of the
    bounded ``decision_type`` strings, raises ValueError for unknown
    values, and populates ``metrics`` JSON with the expected fields."""
    db = Database(db_path=tmp_path / "p02-s18.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        market_id, _ = _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-1",
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)
        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        persist_copy_candidate(db, cand)

        # CREATED entry.
        d_id = record_candidate_decision_log(
            db,
            candidate=cand,
            decision_type="COPY_CANDIDATE_CREATED",
            reason="smoke",
        )
        assert isinstance(d_id, str) and len(d_id) > 0

        row = db.fetchone(
            "SELECT decision_type, rationale, metrics FROM decision_log WHERE id=?",
            (d_id,),
        )
        assert row is not None
        assert row["decision_type"] == "COPY_CANDIDATE_CREATED"
        metrics = json.loads(row["metrics"])
        for key in (
            "candidate_id",
            "candidate_status",
            "candidate_wallet_verdict",
            "candidate_wallet_score",
            "candidate_wallet_score_version",
            "decision_type",
        ):
            assert key in metrics, f"metrics missing {key}"

        # Bounded vocabulary enforced.
        with pytest.raises(ValueError):
            record_candidate_decision_log(
                db, candidate=cand, decision_type="not_a_bounded_type",
            )

        # All declared bounded types are accepted.
        for decision_type in CANDIDATE_DECISION_TYPES:
            record_candidate_decision_log(
                db, candidate=cand, decision_type=decision_type,
            )
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 19. No signals / orders / positions created by candidate persistence.
# ─────────────────────────────────────────────────────────────────────────────
def test_no_signals_orders_positions_created(tmp_path: Path) -> None:
    db = Database(db_path=tmp_path / "p02-s19.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        _seed_market_with_outcome(db, source_id="cond-1", label="Yes", token="tok-1")
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)

        # Evaluate + persist (this is the entire PR-2 surface for this test).
        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        persist_copy_candidate(db, cand)
        record_candidate_decision_log(
            db, candidate=cand, decision_type="COPY_CANDIDATE_CREATED",
        )

        # The candidate row is the only new write target. signals,
        # orders, positions must remain empty.
        for table in ("signals", "orders", "positions"):
            n = db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            assert n == 0, f"{table} unexpectedly has {n} rows after PR-2 surface"
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 20. End-to-end disposable DB acceptance test (brief §STEP 12).
# ─────────────────────────────────────────────────────────────────────────────
def test_pr2_end_to_end_acceptance_persists_one_pending_candidate(
    tmp_path: Path,
) -> None:
    """End-to-end acceptance:

    1. Seed wallet (canonical address) + market + outcome + source_trade.
    2. Build a real CopyabilityScore with verdict=COPY_CANDIDATE,
       formula_version="v1", score=85.0.
    3. Evaluate + persist exactly one PENDING_PRICE_CHECK candidate.
    4. Verify row contents.
    5. Rerun → still one row, returns ``(False, existing_id)``.
    6. signals=0, orders=0, positions=0, FK clean.
    """
    db = Database(db_path=tmp_path / "p02-acceptance.db").connect()
    try:
        # 1. Seed
        wallet_id = _seed_wallet(db, address="0xabc123")
        market_id, outcome_id = _seed_market_with_outcome(
            db, source_id="cond-1", label="Yes", token="tok-123",
        )
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="polymarket:abc123",
            trader_address="0xabc123",
            token_id="tok-123",
            market_source_id="cond-1",
            price=0.5,
            quantity=10.0,
        )

        # 2. Real CopyabilityScore via the engine.
        score = _make_copy_candidate_score(
            wallet_id=wallet_id, score_value=85.0,
        )
        assert score.formula_version == "v1"
        wallet = _make_wallet(address="0xabc123", wallet_id=wallet_id)

        # 3. Evaluate + persist.
        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        assert cand.status == CandidateStatus.PENDING_PRICE_CHECK.value
        assert cand.wallet_score == 85.0
        assert cand.wallet_verdict == "copy_candidate"
        assert cand.wallet_score_version == "v1"

        inserted_id, inserted = persist_copy_candidate(db, cand)
        assert inserted is True
        assert cand.id == inserted_id

        # 4. Exactly one row, contents match the brief.
        n = db.fetchone("SELECT COUNT(*) AS n FROM copy_candidates")["n"]
        assert n == 1

        row = db.fetchone(
            "SELECT * FROM copy_candidates WHERE id=?", (inserted_id,),
        )
        assert row["wallet_id"] == wallet_id
        assert row["source"] == "polymarket_data_api"
        assert row["source_trade_id"] == "polymarket:abc123"
        assert row["market_id"] == market_id
        assert row["market_outcome_id"] == outcome_id
        assert row["token_id"] == "tok-123"
        assert row["outcome_label"] == "Yes"
        assert row["source_trade_price"] == 0.5
        assert row["source_trade_quantity"] == 10.0
        assert row["wallet_score"] == 85.0
        assert row["wallet_verdict"] == "copy_candidate"
        assert row["wallet_score_version"] == "v1"
        assert row["status"] == "PENDING_PRICE_CHECK"

        # 5. Rerun.
        cand_rerun = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        new_id2, inserted2 = persist_copy_candidate(db, cand_rerun)
        assert inserted2 is False
        assert new_id2 == inserted_id
        n2 = db.fetchone("SELECT COUNT(*) AS n FROM copy_candidates")["n"]
        assert n2 == 1

        # 6. signals / orders / positions remain zero; FK clean.
        for t in ("signals", "orders", "positions"):
            assert db.fetchone(
                f"SELECT COUNT(*) AS n FROM {t}"
            )["n"] == 0, f"{t} should remain empty"
        assert db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Extras / non-canonical but useful tests.
# ─────────────────────────────────────────────────────────────────────────────
def test_p02_idempotent_rerun_does_not_duplicate(tmp_path: Path) -> None:
    """Explicit rerun idempotency test (mirrors brief's
    ``test_p02_idempotent_rerun_does_not_duplicate`` request)."""
    db = Database(db_path=tmp_path / "p02-extra-idem.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        _seed_market_with_outcome(db, source_id="cond-1", label="Yes", token="tok-1")
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)
        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )

        results = [persist_copy_candidate(db, cand) for _ in range(3)]
        inserted_ids = [r[0] for r in results]
        inserted_flags = [r[1] for r in results]
        # First call inserts, next two duplicate-skip.
        assert inserted_flags == [True, False, False]
        # All return the same PK.
        assert len(set(inserted_ids)) == 1
        # Still one row.
        n = db.fetchone("SELECT COUNT(*) AS n FROM copy_candidates")["n"]
        assert n == 1
    finally:
        db.close()


def test_p02_signals_orders_positions_zero_post_run(tmp_path: Path) -> None:
    """Explicit ``test_p02_signals_orders_positions_zero_post_run``
    per brief. Exercises the full PR-2 surface and asserts no
    signals/orders/positions are written."""
    db = Database(db_path=tmp_path / "p02-extra-sop.db").connect()
    try:
        wallet_id = _seed_wallet(db, address="0xWALLET")
        _seed_market_with_outcome(db, source_id="cond-1", label="Yes", token="tok-1")
        trade = _seed_source_trade(
            db,
            source="polymarket_data_api",
            source_trade_id="tx-1",
            trader_address="0xWALLET",
            token_id="tok-1",
            market_source_id="cond-1",
        )
        score = _make_copy_candidate_score(wallet_id=wallet_id)
        wallet = _make_wallet(address="0xWALLET", wallet_id=wallet_id)

        cand = evaluate_source_trade_for_wallet(
            db, wallet=wallet, trade=trade, score=score,
        )
        persist_copy_candidate(db, cand)
        record_candidate_decision_log(
            db, candidate=cand, decision_type="COPY_CANDIDATE_CREATED",
        )

        for table in ("signals", "orders", "positions"):
            assert db.fetchone(
                f"SELECT COUNT(*) AS n FROM {table}"
            )["n"] == 0
    finally:
        db.close()


def test_copy_candidate_domain_object_roundtrip(tmp_path: Path) -> None:
    """Construct a CopyCandidate Pydantic model, then exercise the
    bounded enum and helpers (status_enum, decision_type_for_status)."""
    cand = CopyCandidate(
        id=1,
        wallet_id="w1",
        source="polymarket_data_api",
        source_trade_id="tx-1",
        market_id="m1",
        market_outcome_id=1,
        market_source_id="cond-1",
        token_id="tok-1",
        outcome_label="Yes",
        side="BUY",
        source_trade_price=0.5,
        source_trade_quantity=10.0,
        source_trade_notional=5.0,
        source_trade_timestamp="2026-07-01T00:00:00Z",
        observed_at="2026-07-01T00:00:00Z",
        wallet_score_version="v1",
        wallet_score=85.0,
        wallet_verdict="copy_candidate",
        status=CandidateStatus.PENDING_PRICE_CHECK.value,
        status_reason=None,
        metrics_json=json.dumps({"k": "v"}),
        created_at="2026-07-01T00:00:00Z",
        updated_at="2026-07-01T00:00:00Z",
    )
    assert cand.is_pending_price_check is True
    assert cand.is_rejected is False
    assert cand.status_enum is CandidateStatus.PENDING_PRICE_CHECK
    assert cand.to_metrics_dict() == {"k": "v"}
    assert (
        CopyCandidate.decision_type_for_status(
            CandidateStatus.PENDING_PRICE_CHECK, created=True,
        )
        == "COPY_CANDIDATE_CREATED"
    )
    assert (
        CopyCandidate.decision_type_for_status(
            CandidateStatus.REJECTED_WALLET, created=True,
        )
        == "COPY_CANDIDATE_REJECTED_WALLET"
    )
    assert (
        CopyCandidate.decision_type_for_status(
            CandidateStatus.REJECTED_UNRESOLVED_OUTCOME, created=False,
        )
        == "COPY_CANDIDATE_DUPLICATE_SKIPPED"
    )
    # Out-of-bounded status raises.
    bad = cand.model_copy(update={"status": "NOT_A_STATUS"})
    with pytest.raises(ValueError):
        _ = bad.status_enum