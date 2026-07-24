"""S3 real-historical-taxonomy backfill tests (plan Task 8, S3).

Temp/scratch DBs only. Never opens production.

These tests exercise the REAL Gamma path: the backfill CLI resolves the
authoritative raw Gamma market through
``PolymarketPublicAdapter.get_market_raw`` (the canonical condition-ID route).
We patch ``get_market_raw`` to return a real-shaped Gamma payload (with
``clobTokenIds``) so no network is touched and the production-path guard is
never opened. The patch proves the CLI uses the real provider path and that a
single Gamma call serves all trades sharing a condition id.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.canonical_metadata import (  # noqa: E402
    merge_canonical_metadata,
)
from polycopy.ingestion.normalized_source_trade import SOURCE_NAME  # noqa: E402

CLI = "backfill_specialist_trade_taxonomy"

# Canonical approved-wallet source (the real repository writer value).
CANON_SOURCE = SOURCE_NAME

GCOND = "0x" + "a" * 64
GTOK = "0x" + "a" * 64
# Real-shaped Gamma payload (clobTokenIds as a JSON-encoded list string).
GAMMA_PAYLOAD = {
    "conditionId": GCOND,
    "clobTokenIds": json.dumps([GTOK, "0xf" + "0" * 63]),
    "category": "Politics",
    "tags": ["election"],
    "events": [{"id": "e1", "slug": "us", "title": "US Election"}],
    "series": [],
    "question": "Who wins?",
    "slug": "us-election",
    "outcomes": ["Yes", "No"],
    "outcomePrices": ["0.4", "0.6"],
}


def _patched_get_market_raw(call_counter: dict):
    """Async fake for get_market_raw: counts calls, returns the real payload.

    Accepts ``self`` because the method is patched onto the class (Python binds
    the instance automatically).
    """

    async def _fake(self, market_id: str):
        call_counter["n"] += 1
        # Lower-cased exact match against GCOND (the real route is case-exact).
        if market_id and str(market_id).lower() == GCOND:
            return dict(GAMMA_PAYLOAD)
        return None

    return _fake


def _tmp():
    raise RuntimeError("_tmp is provided by the module-owned SQLite fixture")


@pytest.fixture(autouse=True)
def _owned_sqlite_paths(monkeypatch, owned_sqlite):
    """Route this module's disposable SQLite files through pytest ownership."""
    monkeypatch.setitem(globals(), "_tmp", owned_sqlite.new_path)


def _open():
    p = _tmp()
    return Database(p).connect(), p


def _seed_wallet(db, wid="uuid-w", address="0xgood0000000000000000000000000000000abc"):
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (wid, address, "t", 0, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()


def _insert_trade(db, tid, condition, token=None, metadata=None, side="BUY",
                  source=CANON_SOURCE):
    db.conn.execute(
        "INSERT INTO source_trades("
        "id, source, source_trade_id, market_source_id, token_id, side, "
        "outcome, quantity, price, trader_address, timestamp, is_sample, "
        "metadata_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, source, tid, condition, token,
         side, "Yes", 10.0, 0.40,
         "0xgood0000000000000000000000000000000abc", "2026-02-01T00:00:00Z",
         0, json.dumps(metadata or {}, sort_keys=True)),
    )
    db.conn.commit()


def _run_cli(db_path, extra, call_counter):
    with mock.patch(
        "polycopy.adapters.polymarket.PolymarketPublicAdapter.get_market_raw",
        new=_patched_get_market_raw(call_counter),
    ):
        import importlib

        mod = importlib.import_module(CLI)
        return mod.main(
            ["--db-path", str(db_path), "--allow-live", *extra]
        )


def test_backfill_fills_taxonomy_from_real_gamma():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK, {})
    counter = {"n": 0}
    rc = _run_cli(
        db.db_path,
        ["--source-trade-id", "polymarket:t1", "--write",
         "--confirm-production-db", "--limit", "10"],
        counter,
    )
    assert rc == 0, rc
    m = json.loads(dict(db.conn.execute(
        "SELECT metadata_json FROM source_trades WHERE source_trade_id='polymarket:t1'"
    ).fetchone())["metadata_json"])
    assert m["taxonomy"]["raw_category"] == "Politics", m
    # Shape matches the shared producer directly.
    ref, _, _ = merge_canonical_metadata(
        json.dumps({}), GAMMA_PAYLOAD, condition_id=GCOND, token_id=GTOK)
    assert m["taxonomy"]["raw_category"] == ref["taxonomy"]["raw_category"]
    assert counter["n"] >= 1  # real Gamma path was used
    db.close()


def test_backfill_preserves_unrelated_and_idempotent():
    db, _ = _open()
    _seed_wallet(db)
    meta = {"foo": "bar"}
    _insert_trade(db, "polymarket:t1", GCOND, GTOK, meta)
    base = str(db.db_path)
    for _ in range(2):  # two identical runs
        counter = {"n": 0}
        rc = _run_cli(
            base,
            ["--source-trade-id", "polymarket:t1", "--write",
             "--confirm-production-db"],
            counter,
        )
        assert rc == 0, rc
    m = json.loads(dict(db.conn.execute(
        "SELECT metadata_json FROM source_trades WHERE source_trade_id='polymarket:t1'"
    ).fetchone())["metadata_json"])
    assert m["taxonomy"]["raw_category"] == "Politics"
    assert m["foo"] == "bar"
    # Provenance: exactly one row (second run is idempotent -> INSERT OR IGNORE).
    n = db.conn.execute(
        "SELECT COUNT(*) FROM source_trade_enrichments "
        "WHERE enrichment_id=?", ("bk:polymarket:t1",)
    ).fetchone()[0]
    assert n == 1, n
    db.close()


def test_backfill_conflict_blocks_overwrite():
    db, _ = _open()
    _seed_wallet(db)
    # Pre-existing taxonomy that CONFLICTS with gamma category.
    meta = {"taxonomy": {"raw_category": "Sports"}, "foo": "bar"}
    _insert_trade(db, "polymarket:t1", GCOND, GTOK, meta)
    counter = {"n": 0}
    rc = _run_cli(
        db.db_path,
        ["--source-trade-id", "polymarket:t1", "--write",
         "--confirm-production-db"],
        counter,
    )
    assert rc == 0, rc
    m = json.loads(dict(db.conn.execute(
        "SELECT metadata_json FROM source_trades WHERE source_trade_id='polymarket:t1'"
    ).fetchone())["metadata_json"])
    assert m["taxonomy"]["raw_category"] == "Sports"  # NOT overwritten
    n = db.conn.execute(
        "SELECT COUNT(*) FROM source_trade_enrichments WHERE status='conflict'"
    ).fetchone()[0]
    assert n == 1, n
    db.close()


def test_backfill_unavailable_preserves_metadata():
    db, _ = _open()
    _seed_wallet(db)
    # Real Gamma returns None for this condition -> unavailable; metadata kept.
    _insert_trade(db, "polymarket:t1", "0x" + "b" * 64, "0x" + "b" * 64,
                  {"foo": "bar"})
    counter = {"n": 0}
    rc = _run_cli(
        db.db_path,
        ["--source-trade-id", "polymarket:t1", "--write",
         "--confirm-production-db"],
        counter,
    )
    assert rc == 0, rc
    m = json.loads(dict(db.conn.execute(
        "SELECT metadata_json FROM source_trades WHERE source_trade_id='polymarket:t1'"
    ).fetchone())["metadata_json"])
    assert m == {"foo": "bar"}, m
    assert counter["n"] >= 1  # real Gamma path consulted
    db.close()


def test_backfill_bounded_batches():
    db, _ = _open()
    _seed_wallet(db)
    for i in range(12):
        _insert_trade(db, f"polymarket:t{i}", GCOND, GTOK, {})
    base = str(db.db_path)
    counter = {"n": 0}
    rc = _run_cli(base, ["--source-trade-id", "polymarket:t0", "--write",
                          "--confirm-production-db", "--limit", "5"], counter)
    # --source-trade-id selects exactly one trade regardless of --limit.
    assert rc == 0, rc
    filled = db.conn.execute(
        "SELECT COUNT(*) FROM source_trades WHERE json_extract(metadata_json,'$.taxonomy.raw_category') IS NOT NULL"
    ).fetchone()[0]
    assert filled == 1, filled
    db.close()


def test_backfill_dry_run_writes_nothing():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK, {})
    counter = {"n": 0}
    rc = _run_cli(
        db.db_path,
        ["--source-trade-id", "polymarket:t1", "--limit", "10"],  # no --write
        counter,
    )
    assert rc == 0, rc
    m = json.loads(dict(db.conn.execute(
        "SELECT metadata_json FROM source_trades WHERE source_trade_id='polymarket:t1'"
    ).fetchone())["metadata_json"])
    assert "taxonomy" not in m or not m.get("taxonomy", {}).get("raw_category")
    assert counter["n"] >= 1  # dry-run still performs bounded public reads
    db.close()


def test_backfill_production_refused_without_gate():
    # Point --db-path at the REAL production path with a VALID selector but
    # missing the production write gates. The guard refuses BEFORE any write
    # when --write lacks --allow-live --confirm-production-db, so even a
    # missing production file is safe (no connect, no write).
    prod = (ROOT / "data" / "polycopy.db")
    rc = __import__("subprocess").run(
        [sys.executable, str(ROOT / "scripts" / "backfill_specialist_trade_taxonomy.py"),
         "--db-path", str(prod), "--write", "--allow-live",
         "--source-trade-id", "polymarket:any"],  # valid selector + live, missing --confirm
        capture_output=True, text=True,
    )
    assert rc.returncode != 0, "production write without gate must be refused"
    assert "production" in rc.stderr.lower(), rc.stderr


if __name__ == "__main__":
    unittest.main()
