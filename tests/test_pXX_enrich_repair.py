"""T9 enrichment-repair regression tests (plan Task 12).

Temp/scratch DBs only. Never opens production.

Proves the fix: per-trade enrichment now classifies the CANONICAL nested
metadata shape (``taxonomy.raw_category``) produced by the shared
``canonical_metadata.merge_canonical_metadata`` service — NOT a flat Gamma
dict. Before the fix, a flat Gamma dict was passed to classify_category_taxonomy
(which expects nested taxonomy.raw_category) -> always UNAVAILABLE.

Also proves: a flat GAMMA-shaped metadata_json is correctly canonicalized
to nested taxonomy on enrichment, and a properly nested source_trades
metadata classifies identically (byte-equivalent shape to collection).
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
from polycopy.ingestion.source_trade_enrichment import (  # noqa: E402
    enrich_source_trade,
    get_enrichment,
)


def _tmp():
    return Path(tempfile.mktemp(suffix=".db"))


def _open():
    p = _tmp()
    return Database(p).connect(), p


def _seed_wallet(db, wid="uuid-e", address="0xenrich000000000000000000000000000abc"):
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (wid, address, "t", 0, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()


FLAT_GAMMA = {
    "category": "Politics", "tags": ["election"],
    "events": [{"id": "e1", "slug": "us"}], "series": [],
    "question": "Who wins?", "slug": "us-election",
    "outcomes": ["Yes", "No"], "outcomePrices": ["0.4", "0.6"],
}


def _seed_trade(db, tid, cond, metadata_json, side="BUY"):
    db.conn.execute(
        "INSERT INTO source_trades("
        "id, source, source_trade_id, market_source_id, side, outcome, "
        "quantity, price, trader_address, timestamp, is_sample, "
        "metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, "polymarket", tid, cond, side, "Yes", 10.0, 0.40,
         "0xenrich000000000000000000000000000abc",
         "2026-02-01T00:00:00Z", 0, json.dumps(metadata_json, sort_keys=True)),
    )
    db.conn.commit()


COND = "0x" + "e" * 64
GTOK = "0x" + "e" * 64


def _fake_resolver(_cid):
    return {
        "conditionId": COND, "tokenId": GTOK, "category": "Politics",
        "tags": ["election"], "events": [{"id": "e1", "slug": "us"}],
        "series": [], "question": "Q", "slug": "us",
        "outcomes": ["Yes", "No"], "outcomePrices": ["0.4", "0.6"],
    }


def test_enrich_flat_gamma_now_classified_nested():
    """The regression: flat Gamma metadata must be canonicalized to nested
    taxonomy and classified as 'politics' (not left UNAVAILABLE)."""
    db, _ = _open()
    _seed_wallet(db)
    # Flat Gamma-shaped metadata_json (the shape that previously broke).
    _seed_trade(db, "polymarket:st1", COND, FLAT_GAMMA)
    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    assert res.status != "error", res
    enr = get_enrichment(db, "polymarket:st1")
    assert enr is not None
    assert enr["normalized_category"] == "politics", enr
    assert enr["taxonomy_status"] == "usable", enr
    db.close()


def test_enrich_nested_canonical_matches_flat():
    """A properly nested canonical metadata classifies identically to the
    flat Gamma form -> byte-equivalent shape across collection paths."""
    from polycopy.ingestion.canonical_metadata import build_canonical_metadata
    nested = build_canonical_metadata({}, FLAT_GAMMA)
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, nested)
    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    assert res.status != "error", res
    enr = get_enrichment(db, "polymarket:st1")
    assert enr["normalized_category"] == "politics", enr
    assert enr["taxonomy_status"] == "usable", enr
    db.close()


def test_enrich_no_taxonomy_unavailable():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, {"foo": "bar"})
    res = enrich_source_trade(db, "polymarket:st1", gamma_resolver=None)
    assert res.status != "error", res
    enr = get_enrichment(db, "polymarket:st1")
    assert enr["normalized_category"] is None, enr
    assert enr["taxonomy_status"] == "unavailable", enr
    db.close()


def test_enrich_idempotent_replay():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, FLAT_GAMMA)
    r1 = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    assert r1.created is True, r1
    r2 = enrich_source_trade(db, "polymarket:st1", gamma_resolver=_fake_resolver)
    assert r2.created is False, r2
    assert r2.status == r1.status, (r2.status, r1.status)
    db.close()


def test_enrich_dry_run_no_persist():
    db, _ = _open()
    _seed_wallet(db)
    _seed_trade(db, "polymarket:st1", COND, FLAT_GAMMA)
    res = enrich_source_trade(
        db, "polymarket:st1", gamma_resolver=_fake_resolver, dry_run=True)
    assert res.created is False, res
    enr = get_enrichment(db, "polymarket:st1")
    # Even though the dry-run produced evidence with a category, nothing is
    # persisted (no enrichment row).
    assert enr is None, enr
    db.close()
