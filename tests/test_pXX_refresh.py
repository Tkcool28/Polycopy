"""T6 resolution-refresh tests (plan Task 9).

Temp/scratch DBs only. Never opens production.

Exercises the market-centric refresh WITHOUT a ``markets`` row: distinct
unresolved ``market_source_id`` -> authoritative get_market -> update all
linked ``source_trades`` consistently; record ``specialist_market_refresh_state``
(bookkeeping only).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import refresh_specialist_market_truth as refresh  # noqa: E402
from polycopy.db.database import Database  # noqa: E402


def _tmp():
    return Path(tempfile.mktemp(suffix=".db"))


def _open():
    # Tests may create a fresh v21 schema for setup (the CLIs themselves use
    # the shared evidence_db helper, never Database().connect()).
    p = _tmp()
    return Database(p).connect(), p


def _seed_wallet(db, wid="uuid-w", address="0xgood0000000000000000000000000000refr"):
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (wid, address, "t", 0, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()


def _insert_trade(db, tid, condition, status=None, winner=None, side="BUY"):
    db.conn.execute(
        "INSERT INTO source_trades("
        "id, source, source_trade_id, market_source_id, side, outcome, "
        "quantity, price, trader_address, timestamp, is_sample, "
        "resolution_status, winning_token_id, metadata_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, "polymarket", tid, condition, side, "Yes", 10.0, 0.40,
         "0xgood00000000000000000000000000000refr",
         "2026-02-01T00:00:00Z", 0,
         status or "unresolved", winner, json.dumps({}, sort_keys=True)),
    )
    db.conn.commit()


COND = "0x" + "c" * 64
TOK = "0x" + "c" * 64


def test_refresh_works_without_markets_row():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", COND)  # unresolved, no markets row

    def _resolver(cid):
        return {"resolutionStatus": "resolved", "winner": TOK}

    rc = refresh.main(
        ["--db-path", str(db.db_path), "--write", "--allow-live",
         "--confirm-production-db"],
        get_market=_resolver,
    )
    assert rc == 0, rc
    row = db.conn.execute(
        "SELECT resolution_status, winning_token_id FROM source_trades "
        "WHERE source_trade_id='polymarket:t1'"
    ).fetchone()
    assert dict(row)["resolution_status"] == "resolved"
    assert dict(row)["winning_token_id"] == TOK
    db.close()


def test_refresh_updates_all_linked_trades():
    db, _ = _open()
    _seed_wallet(db)
    for i in range(3):
        _insert_trade(db, f"polymarket:t{i}", COND)

    def _resolver(cid):
        return {"resolutionStatus": "resolved", "winner": TOK}

    rc = refresh.main(
        ["--db-path", str(db.db_path), "--write", "--allow-live",
         "--confirm-production-db"],
        get_market=_resolver,
    )
    assert rc == 0, rc
    n = db.conn.execute(
        "SELECT COUNT(*) FROM source_trades WHERE resolution_status='resolved' "
        "AND winning_token_id=?", (TOK,)
    ).fetchone()[0]
    assert n == 3, n
    # Bookkeeping row exists.
    m = db.conn.execute(
        "SELECT last_status FROM specialist_market_refresh_state "
        "WHERE market_source_id=?", (COND,)
    ).fetchone()
    assert m is not None and dict(m)["last_status"] == "resolved"
    db.close()


def test_refresh_unresolved_no_claim():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", COND)

    def _resolver(cid):
        return {"resolutionStatus": "unresolved"}  # upstream unresolved

    rc = refresh.main(
        ["--db-path", str(db.db_path), "--write", "--allow-live",
         "--confirm-production-db"],
        get_market=_resolver,
    )
    assert rc == 0, rc
    row = db.conn.execute(
        "SELECT resolution_status, winning_token_id FROM source_trades "
        "WHERE source_trade_id='polymarket:t1'"
    ).fetchone()
    assert dict(row)["resolution_status"] == "unresolved"
    assert dict(row)["winning_token_id"] is None
    db.close()


def test_refresh_replay_unchanged():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", COND)

    def _resolver(cid):
        return {"resolutionStatus": "resolved", "winner": TOK}

    for _ in range(2):
        rc = refresh.main(
            ["--db-path", str(db.db_path), "--write", "--allow-live",
             "--confirm-production-db"],
            get_market=_resolver,
        )
        assert rc == 0, rc
    n = db.conn.execute(
        "SELECT COUNT(*) FROM source_trades WHERE resolution_status='resolved'"
    ).fetchone()[0]
    assert n == 1, n
    db.close()


def test_refresh_conflict_blocks():
    db, _ = _open()
    _seed_wallet(db)
    # Pre-existing conflicting winners on disk.
    _insert_trade(db, "polymarket:t1", COND, status="resolved", winner=TOK)
    _insert_trade(db, "polymarket:t2", COND, status="resolved", winner="0x" + "d" * 64)

    def _resolver(cid):
        return {"resolutionStatus": "resolved", "winner": TOK}

    rc = refresh.main(
        ["--db-path", str(db.db_path), "--write", "--allow-live",
         "--confirm-production-db"],
        get_market=_resolver,
    )
    assert rc == 0, rc
    # First trade keeps its winner; second keeps its different winner (no overwrite).
    rows = db.conn.execute(
        "SELECT source_trade_id, winning_token_id FROM source_trades "
        "WHERE market_source_id=?", (COND,)
    ).fetchall()
    by_id = {dict(r)["source_trade_id"]: dict(r)["winning_token_id"] for r in rows}
    assert by_id["polymarket:t1"] == TOK
    assert by_id["polymarket:t2"] == "0x" + "d" * 64
    db.close()


def test_refresh_dry_run_writes_nothing():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", COND)

    def _resolver(cid):
        return {"resolutionStatus": "resolved", "winner": TOK}

    rc = refresh.main(
        ["--db-path", str(db.db_path)],  # no --write
        get_market=_resolver,
    )
    assert rc == 0, rc
    row = db.conn.execute(
        "SELECT resolution_status FROM source_trades "
        "WHERE source_trade_id='polymarket:t1'"
    ).fetchone()
    assert dict(row)["resolution_status"] == "unresolved"
    db.close()


def test_refresh_production_refused_without_gate():
    prod = (ROOT / "data" / "polycopy.db")
    rc = refresh.main(
        ["--db-path", str(prod), "--write"],  # missing --allow-live/--confirm
        get_market=lambda cid: None,
    )
    assert rc != 0, "production write without gate must be refused"
