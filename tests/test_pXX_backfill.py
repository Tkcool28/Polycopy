"""T5 backfill tests (plan Task 8).

Temp/scratch DBs only. Never opens production.

The backfill's resolver synthesizes the Gamma Mapping from each trade's OWN
stored ``metadata_json['gamma']`` block — so we seed trades with a gamma
block but missing/empty ``taxonomy`` to exercise the fill path, and with a
conflicting ``taxonomy`` to exercise the conflict block. This proves the
backfill reuses the SHARED canonical_metadata.merge_canonical_metadata
service (same nested shape as collection), without network.
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

from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.canonical_metadata import (  # noqa: E402
    merge_canonical_metadata,
)


def _tmp():
    return Path(tempfile.mktemp(suffix=".db"))


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


def _insert_trade(db, tid, condition, token=None, metadata=None, side="BUY"):
    db.conn.execute(
        "INSERT INTO source_trades("
        "id, source, source_trade_id, market_source_id, side, "
        "outcome, quantity, price, trader_address, timestamp, is_sample, "
        "metadata_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, "polymarket", tid, condition,
         side, "Yes", 10.0, 0.40,
         "0xgood0000000000000000000000000000000abc", "2026-02-01T00:00:00Z",
         0, json.dumps(metadata or {}, sort_keys=True)),
    )
    db.conn.commit()


GCOND = "0x" + "a" * 64
GTOK = "0x" + "a" * 64
GAMMA = {
    "conditionId": GCOND,
    # ACTUAL Gamma shape: clobTokenIds as a JSON-encoded list string.
    "clobTokenIds": json.dumps([GTOK, "0xf" + "0" * 63]),
    "category": "Politics",
    "tags": ["election"],
    "events": [{"id": "e1", "slug": "us", "ticker": "US"}],
    "series": [], "question": "Who wins?", "slug": "us-election",
    "outcomes": ["Yes", "No"], "outcomePrices": ["0.4", "0.6"],
}


def _meta_with_gamma_no_taxonomy():
    # Has a trusted gamma block but no taxonomy -> backfill should FILL.
    return {"gamma": GAMMA, "event": {}, "foo": "bar"}


def test_backfill_fills_taxonomy_from_gamma_block():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK, _meta_with_gamma_no_taxonomy())
    rc = __import__("subprocess").run(
        [sys.executable, str(ROOT / "scripts" / "backfill_specialist_trade_taxonomy.py"),
         "--db-path", str(db.db_path), "--write", "--allow-live", "--confirm-production-db",
         "--limit", "10"],
        capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stderr
    row = db.conn.execute(
        "SELECT metadata_json FROM source_trades WHERE source_trade_id='polymarket:t1'"
    ).fetchone()
    m = json.loads(dict(row)["metadata_json"])
    assert m["taxonomy"]["raw_category"] == "Politics", m
    assert m["foo"] == "bar"  # unrelated preserved
    # Shape matches the shared producer directly.
    ref, _, _ = merge_canonical_metadata(
        json.dumps({"foo": "bar"}), GAMMA, condition_id=GCOND, token_id=GTOK)
    assert m["taxonomy"]["raw_category"] == ref["taxonomy"]["raw_category"]
    db.close()


def test_backfill_preserves_unrelated_metadata_and_idempotent():
    db, _ = _open()
    _seed_wallet(db)
    meta = _meta_with_gamma_no_taxonomy()
    _insert_trade(db, "polymarket:t1", GCOND, GTOK, meta)
    base = str(db.db_path)
    for _ in range(2):  # two identical runs
        rc = __import__("subprocess").run(
            [sys.executable, str(ROOT / "scripts" / "backfill_specialist_trade_taxonomy.py"),
             "--db-path", base, "--write", "--allow-live", "--confirm-production-db"],
            capture_output=True, text=True,
        )
        assert rc.returncode == 0, rc.stderr
    # Still exactly the same single trade; no duplicate metadata change.
    m = json.loads(dict(db.conn.execute(
        "SELECT metadata_json FROM source_trades WHERE source_trade_id='polymarket:t1'"
    ).fetchone())["metadata_json"])
    assert m["taxonomy"]["raw_category"] == "Politics"
    assert m["foo"] == "bar"
    # Provenance: exactly one row written (second run is idempotent ->
    # INSERT OR IGNORE skips the duplicate). No overwrite, no duplicate.
    n = db.conn.execute(
        "SELECT COUNT(*) FROM source_trade_enrichments "
        "WHERE status IN ('complete','unavailable','conflict')"
    ).fetchone()[0]
    assert n == 1, n
    db.close()


def test_backfill_conflict_blocks_overwrite():
    db, _ = _open()
    _seed_wallet(db)
    # Pre-existing taxonomy that CONFLICTS with gamma category.
    meta = {"gamma": GAMMA, "taxonomy": {"raw_category": "Sports"}, "foo": "bar"}
    _insert_trade(db, "polymarket:t1", GCOND, GTOK, meta)
    rc = __import__("subprocess").run(
        [sys.executable, str(ROOT / "scripts" / "backfill_specialist_trade_taxonomy.py"),
         "--db-path", str(db.db_path), "--write", "--allow-live", "--confirm-production-db"],
        capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stderr
    m = json.loads(dict(db.conn.execute(
        "SELECT metadata_json FROM source_trades WHERE source_trade_id='polymarket:t1'"
    ).fetchone())["metadata_json"])
    assert m["taxonomy"]["raw_category"] == "Sports"  # NOT overwritten
    # Conflict provenance recorded.
    n = db.conn.execute(
        "SELECT COUNT(*) FROM source_trade_enrichments WHERE status='conflict'"
    ).fetchone()[0]
    assert n == 1, n
    db.close()


def test_backfill_missing_gamma_unavailable():
    db, _ = _open()
    _seed_wallet(db)
    # No gamma block at all -> should stay unavailable, no error.
    _insert_trade(db, "polymarket:t1", GCOND, GTOK, {"foo": "bar"})
    rc = __import__("subprocess").run(
        [sys.executable, str(ROOT / "scripts" / "backfill_specialist_trade_taxonomy.py"),
         "--db-path", str(db.db_path), "--write", "--allow-live", "--confirm-production-db"],
        capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stderr
    m = json.loads(dict(db.conn.execute(
        "SELECT metadata_json FROM source_trades WHERE source_trade_id='polymarket:t1'"
    ).fetchone())["metadata_json"])
    assert "taxonomy" not in m or not m.get("taxonomy", {}).get("raw_category")
    db.close()


def test_backfill_bounded_batches():
    db, _ = _open()
    _seed_wallet(db)
    for i in range(12):
        _insert_trade(db, f"polymarket:t{i}", GCOND, GTOK, _meta_with_gamma_no_taxonomy())
    base = str(db.db_path)
    # Batch of 5.
    rc = __import__("subprocess").run(
        [sys.executable, str(ROOT / "scripts" / "backfill_specialist_trade_taxonomy.py"),
         "--db-path", base, "--write", "--allow-live", "--confirm-production-db", "--limit", "5"],
        capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stderr
    filled = db.conn.execute(
        "SELECT COUNT(*) FROM source_trades WHERE json_extract(metadata_json,'$.taxonomy.raw_category') IS NOT NULL"
    ).fetchone()[0]
    assert filled == 5, filled
    # Remaining 7 fill on next run (no limit).
    rc = __import__("subprocess").run(
        [sys.executable, str(ROOT / "scripts" / "backfill_specialist_trade_taxonomy.py"),
         "--db-path", base, "--write", "--allow-live", "--confirm-production-db"],
        capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stderr
    filled = db.conn.execute(
        "SELECT COUNT(*) FROM source_trades WHERE json_extract(metadata_json,'$.taxonomy.raw_category') IS NOT NULL"
    ).fetchone()[0]
    assert filled == 12, filled
    db.close()


def test_backfill_dry_run_writes_nothing():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK, _meta_with_gamma_no_taxonomy())
    rc = __import__("subprocess").run(
        [sys.executable, str(ROOT / "scripts" / "backfill_specialist_trade_taxonomy.py"),
         "--db-path", str(db.db_path), "--limit", "10"],  # no --write
        capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stderr
    m = json.loads(dict(db.conn.execute(
        "SELECT metadata_json FROM source_trades WHERE source_trade_id='polymarket:t1'"
    ).fetchone())["metadata_json"])
    assert "taxonomy" not in m or not m.get("taxonomy", {}).get("raw_category")
    db.close()


def test_backfill_production_refused_without_gate():
    # Point --db-path at the REAL production path. The guard refuses BEFORE
    # any DB open/write when --write lacks --allow-live --confirm-production-db,
    # so even a missing production file is safe (no connect, no write).
    prod = (ROOT / "data" / "polycopy.db")
    rc = __import__("subprocess").run(
        [sys.executable, str(ROOT / "scripts" / "backfill_specialist_trade_taxonomy.py"),
         "--db-path", str(prod), "--write"],  # missing --allow-live/--confirm
        capture_output=True, text=True,
    )
    assert rc.returncode != 0, "production write without gate must be refused"
    assert "production" in rc.stderr.lower(), rc.stderr
