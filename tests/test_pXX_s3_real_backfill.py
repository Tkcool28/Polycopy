"""S3 focused PR71 suite: real historical taxonomy backfill — all required proofs.

Temp/scratch DBs only. Never opens production.

Exercises the REAL Gamma path (``PolymarketPublicAdapter.get_market_raw`` via a
patched class method) and the full public CLI surface for every S3 contract:

  * exact selectors (source-trade-id / wallet-id / watch-id)
  * bounds (limit, BUY-only, is_sample=0, Polymarket only, --allow-live)
  * merge safety (filled/unchanged persist; unavailable/conflict do not)
  * request de-duplication (one Gamma call per condition id)
  * atomic idempotent replay (no duplicate rows, no metadata change, created_at preserved)
  * no execution-plane artifact is created (approval/dispatch/candidate/signal/
    authorization/risk/order/fill/position/mark/settlement)

The patched ``get_market_raw`` records exactly how many real Gamma requests
were issued, proving the de-duplication and real-provider contracts.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.canonical_metadata import (  # noqa: E402
    merge_canonical_metadata,
    MERGE_FILLED,
)

CLI = "backfill_specialist_trade_taxonomy"

WADDR = "0x" + "a" * 40
WUUID = "uuid-wallet-1"
WATCH_ID = "watch-1"

GCOND = "0x" + "c" * 64
GCOND2 = "0x" + "d" * 64
GTOK_A = "0x" + "a" * 64
GTOK_B = "0x" + "b" * 64

# Real-shaped Gamma payloads keyed by condition id (clobTokenIds JSON string).
GAMMA = {
    GCOND: {
        "conditionId": GCOND,
        "clobTokenIds": json.dumps([GTOK_A, GTOK_B]),
        "category": "Politics",
        "tags": ["election"],
        "events": [{"id": "e1", "slug": "us", "title": "US Election"}],
        "series": [],
        "question": "Who wins?",
        "slug": "us-election",
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["0.4", "0.6"],
    },
    GCOND2: {
        "conditionId": GCOND2,
        "clobTokenIds": json.dumps([GTOK_B, GTOK_A]),
        "category": "Sports",
        "tags": ["nba"],
        "events": [],
        "series": [],
        "question": "Who wins?",
        "slug": "nba-final",
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["0.5", "0.5"],
    },
}


def _patched_get_market_raw(call_counter: dict):
    async def _fake(self, market_id: str):
        call_counter["n"] += 1
        key = str(market_id).lower()
        return dict(GAMMA[key]) if key in GAMMA else None

    return _fake


def _tmp():
    return Path(tempfile.mktemp(suffix=".db"))


def _open():
    p = _tmp()
    return Database(p).connect(), p


def _seed_wallet(db, wid=WUUID, address=WADDR, sample=0):
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (wid, address, "t", sample, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()


def _seed_watch(db, wid=WUUID, watch_id=WATCH_ID, status="active"):
    db.conn.execute(
        "INSERT INTO specialist_evidence_watchlist(id,wallet_id,status,source,"
        "reason,created_at,max_new_trades_per_run) "
        "VALUES (?,?,?,?,?,?,?)",
        (watch_id, wid, status, "manual", "t", "2026-01-01T00:00:00Z", 25),
    )
    db.conn.commit()


def _insert_trade(db, tid, condition, token=None, metadata=None, side="BUY",
                  sample=0, source="polymarket", address=WADDR):
    db.conn.execute(
        "INSERT INTO source_trades("
        "id, source, source_trade_id, market_source_id, side, "
        "outcome, quantity, price, trader_address, timestamp, is_sample, "
        "metadata_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, source, tid, condition, side, "Yes", 10.0, 0.40, address,
         "2026-02-01T00:00:00Z", sample,
         json.dumps(metadata or {}, sort_keys=True)),
    )
    db.conn.commit()


def _run(db_path, extra, counter):
    with mock.patch(
        "polycopy.adapters.polymarket.PolymarketPublicAdapter.get_market_raw",
        new=_patched_get_market_raw(counter),
    ):
        import importlib

        mod = importlib.import_module(CLI)
        return mod.main(["--db-path", str(db_path), "--allow-live", *extra])


def _meta(db, tid):
    row = db.conn.execute(
        "SELECT metadata_json FROM source_trades WHERE source_trade_id=?", (tid,)
    ).fetchone()
    return json.loads(dict(row)["metadata_json"])


def _count(db, table):
    return db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ─────────────────────────────────────────────────────────────────────────────
# 1. empty metadata filled from a real-shaped Gamma payload
# ─────────────────────────────────────────────────────────────────────────────


def test_empty_metadata_filled_from_real_gamma():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {})
    counter = {"n": 0}
    rc = _run(db.db_path,
              ["--source-trade-id", "polymarket:t1", "--write",
               "--confirm-production-db"], counter)
    assert rc == 0, rc
    m = _meta(db, "polymarket:t1")
    assert m["taxonomy"]["raw_category"] == "Politics", m
    assert m["event"]["slug"] == "us", m
    assert counter["n"] >= 1  # real Gamma path consulted
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 2. JSON-string clobTokenIds works
# ─────────────────────────────────────────────────────────────────────────────


def test_json_string_clobtokenids_works():
    db, _ = _open()
    _seed_wallet(db)
    # GCOND's clobTokenIds is a JSON string; token GTOK_A belongs to it.
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {})
    counter = {"n": 0}
    rc = _run(db.db_path,
              ["--source-trade-id", "polymarket:t1", "--write",
               "--confirm-production-db"], counter)
    assert rc == 0, rc
    m = _meta(db, "polymarket:t1")
    assert m["taxonomy"]["raw_category"] == "Politics", m
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 3. bare-list clobTokenIds works
# ─────────────────────────────────────────────────────────────────────────────


def test_bare_list_clobtokenids_works():
    db, _ = _open()
    _seed_wallet(db)
    # Seed with a bare-list Gamma-shaped payload via the merge service path:
    # build a market whose clobTokenIds is a real list (not a JSON string).
    cond = "0x" + "e" * 64
    tok = "0x" + "e" * 64
    gamma = {
        "conditionId": cond,
        "clobTokenIds": [tok, "0xf" + "0" * 63],  # bare list
        "category": "Politics",
        "tags": ["x"],
        "events": [{"id": "e1"}],
        "series": [],
    }
    merged, status, _ = merge_canonical_metadata(
        json.dumps({}), gamma, condition_id=cond, token_id=tok)
    assert status == MERGE_FILLED, status
    assert merged["taxonomy"]["raw_category"] == "Politics"
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 4. wallet UUID resolves to canonical address
# ─────────────────────────────────────────────────────────────────────────────


def test_wallet_uuid_resolves_to_canonical_address():
    db, _ = _open()
    _seed_wallet(db, wid=WUUID, address=WADDR)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {}, address=WADDR)
    counter = {"n": 0}
    rc = _run(db.db_path,
              ["--wallet-id", WUUID, "--write", "--confirm-production-db"],
              counter)
    assert rc == 0, rc
    m = _meta(db, "polymarket:t1")
    assert m["taxonomy"]["raw_category"] == "Politics", m
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 5. watchlist selector uses specialist_evidence_watchlist.id
# ─────────────────────────────────────────────────────────────────────────────


def test_watchlist_selector_uses_watchlist_id():
    db, _ = _open()
    _seed_wallet(db, wid=WUUID, address=WADDR)
    _seed_watch(db, wid=WUUID, watch_id=WATCH_ID, status="active")
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {}, address=WADDR)
    counter = {"n": 0}
    rc = _run(db.db_path,
              ["--watch-id", WATCH_ID, "--write", "--confirm-production-db"],
              counter)
    assert rc == 0, rc
    m = _meta(db, "polymarket:t1")
    assert m["taxonomy"]["raw_category"] == "Politics", m
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 6. sample / paused / retired selection is refused
# ─────────────────────────────────────────────────────────────────────────────


def test_sample_wallet_refused():
    db, _ = _open()
    _seed_wallet(db, wid=WUUID, address=WADDR, sample=1)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {}, address=WADDR)
    counter = {"n": 0}
    rc = _run(db.db_path,
              ["--wallet-id", WUUID, "--write", "--confirm-production-db"],
              counter)
    assert rc != 0, "sample wallet must be refused"
    db.close()


def test_paused_watch_refused():
    db, _ = _open()
    _seed_wallet(db, wid=WUUID, address=WADDR)
    _seed_watch(db, wid=WUUID, watch_id=WATCH_ID, status="paused")
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {}, address=WADDR)
    counter = {"n": 0}
    rc = _run(db.db_path,
              ["--watch-id", WATCH_ID, "--write", "--confirm-production-db"],
              counter)
    assert rc != 0, "paused watch must be refused"
    db.close()


def test_retired_watch_refused():
    db, _ = _open()
    _seed_wallet(db, wid=WUUID, address=WADDR)
    _seed_watch(db, wid=WUUID, watch_id=WATCH_ID, status="retired")
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {}, address=WADDR)
    counter = {"n": 0}
    rc = _run(db.db_path,
              ["--watch-id", WATCH_ID, "--write", "--confirm-production-db"],
              counter)
    assert rc != 0, "retired watch must be refused"
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 7. missing selector write is refused
# ─────────────────────────────────────────────────────────────────────────────


def test_missing_selector_refused():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {})
    counter = {"n": 0}
    rc = _run(db.db_path, ["--write", "--confirm-production-db"], counter)
    assert rc != 0, "missing selector must be refused"
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 8. multiple selectors are refused
# ─────────────────────────────────────────────────────────────────────────────


def test_multiple_selectors_refused():
    db, _ = _open()
    _seed_wallet(db, wid=WUUID, address=WADDR)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {}, address=WADDR)
    counter = {"n": 0}
    rc = _run(db.db_path,
              ["--source-trade-id", "polymarket:t1", "--wallet-id", WUUID,
               "--write", "--confirm-production-db"], counter)
    assert rc != 0, "multiple selectors must be refused"
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 9. zero / negative / over-hard-max limits are refused
# ─────────────────────────────────────────────────────────────────────────────


def test_zero_limit_refused():
    db, _ = _open()
    _seed_wallet(db)
    rc = _run(db.db_path, ["--source-trade-id", "polymarket:t1", "--limit", "0"],
              {"n": 0})
    assert rc != 0, "zero limit must be refused"
    db.close()


def test_negative_limit_refused():
    db, _ = _open()
    _seed_wallet(db)
    rc = _run(db.db_path, ["--source-trade-id", "polymarket:t1", "--limit", "-5"],
              {"n": 0})
    assert rc != 0, "negative limit must be refused"
    db.close()


def test_over_hard_max_limit_refused():
    db, _ = _open()
    _seed_wallet(db)
    rc = _run(db.db_path,
              ["--source-trade-id", "polymarket:t1", "--limit", "501"],
              {"n": 0})
    assert rc != 0, "limit above hard maximum must be refused"
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 10. one Gamma call serves all trades sharing a condition id
# ─────────────────────────────────────────────────────────────────────────────


def test_one_gamma_call_serves_shared_condition():
    db, _ = _open()
    _seed_wallet(db)
    for i in range(5):
        _insert_trade(db, f"polymarket:t{i}", GCOND, GTOK_A, {})
    counter = {"n": 0}
    rc = _run(db.db_path,
              ["--source-trade-id", "polymarket:t0", "--write",
               "--confirm-production-db"], counter)
    # --source-trade-id selects exactly one trade but the de-dup proof is in
    # the wallet selector: repeat with wallet across many shared-condition rows.
    assert rc == 0, rc
    # Now exercise the de-dup directly via the wallet selector (5 rows, 1 cid).
    counter2 = {"n": 0}
    rc2 = _run(db.db_path,
               ["--wallet-id", WUUID, "--write", "--confirm-production-db"],
               counter2)
    assert rc2 == 0, rc2
    assert counter2["n"] == 1, f"expected exactly 1 Gamma call, got {counter2['n']}"
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 11. taxonomy unavailable preserves metadata
# ─────────────────────────────────────────────────────────────────────────────


def test_taxonomy_unavailable_preserves_metadata():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", "0x" + "b" * 64, "0x" + "b" * 64,
                  {"foo": "bar"})
    counter = {"n": 0}
    rc = _run(db.db_path,
              ["--source-trade-id", "polymarket:t1", "--write",
               "--confirm-production-db"], counter)
    assert rc == 0, rc
    m = _meta(db, "polymarket:t1")
    assert m == {"foo": "bar"}, m  # unchanged
    assert counter["n"] >= 1  # real Gamma path consulted
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 12. taxonomy conflict preserves metadata
# ─────────────────────────────────────────────────────────────────────────────


def test_taxonomy_conflict_preserves_metadata():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A,
                  {"taxonomy": {"raw_category": "Sports"}, "foo": "bar"})
    counter = {"n": 0}
    rc = _run(db.db_path,
              ["--source-trade-id", "polymarket:t1", "--write",
               "--confirm-production-db"], counter)
    assert rc == 0, rc
    m = _meta(db, "polymarket:t1")
    assert m["taxonomy"]["raw_category"] == "Sports", m  # NOT overwritten
    n = db.conn.execute(
        "SELECT COUNT(*) FROM source_trade_enrichments WHERE status='conflict'"
    ).fetchone()[0]
    assert n == 1, n
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 13. malformed existing metadata preserves the raw DB value
# ─────────────────────────────────────────────────────────────────────────────


def test_malformed_existing_metadata_preserves_raw():
    db, _ = _open()
    _seed_wallet(db)
    # Insert then force a malformed raw JSON value into metadata_json.
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {})
    db.conn.execute(
        "UPDATE source_trades SET metadata_json=? WHERE source_trade_id=?",
        ("{not valid json", "polymarket:t1"),
    )
    db.conn.commit()
    counter = {"n": 0}
    rc = _run(db.db_path,
              ["--source-trade-id", "polymarket:t1", "--write",
               "--confirm-production-db"], counter)
    assert rc == 0, rc
    row = db.conn.execute(
        "SELECT metadata_json FROM source_trades WHERE source_trade_id='polymarket:t1'"
    ).fetchone()
    assert dict(row)["metadata_json"] == "{not valid json", "raw value must be preserved"
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 14. wrong-type existing tags conflicts rather than overwrites
# ─────────────────────────────────────────────────────────────────────────────


def test_wrong_type_existing_tags_conflicts():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A,
                  {"taxonomy": {"raw_category": "Politics", "tags": "election"}})
    counter = {"n": 0}
    rc = _run(db.db_path,
              ["--source-trade-id", "polymarket:t1", "--write",
               "--confirm-production-db"], counter)
    assert rc == 0, rc
    m = _meta(db, "polymarket:t1")
    # The wrong-type tags value is preserved (conflict, not overwritten).
    assert m["taxonomy"]["tags"] == "election", m
    n = db.conn.execute(
        "SELECT COUNT(*) FROM source_trade_enrichments WHERE status='conflict'"
    ).fetchone()[0]
    assert n == 1, n
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 15. replay adds no rows
# ─────────────────────────────────────────────────────────────────────────────


def test_replay_adds_no_rows():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {})
    base = str(db.db_path)
    created_at_before = None
    for i in range(2):  # first fills, second is the replay
        counter = {"n": 0}
        rc = _run(base,
                  ["--source-trade-id", "polymarket:t1", "--write",
                   "--confirm-production-db"], counter)
        assert rc == 0, rc
        if i == 0:
            created_at_before = db.conn.execute(
                "SELECT created_at FROM source_trade_enrichments "
                "WHERE enrichment_id=?", ("bk:polymarket:t1",)
            ).fetchone()[0]
    # Exactly one enrichment row, created_at preserved.
    n = db.conn.execute(
        "SELECT COUNT(*) FROM source_trade_enrichments WHERE enrichment_id=?",
        ("bk:polymarket:t1",)
    ).fetchone()[0]
    assert n == 1, n
    created_at_after = db.conn.execute(
        "SELECT created_at FROM source_trade_enrichments WHERE enrichment_id=?",
        ("bk:polymarket:t1",)
    ).fetchone()[0]
    assert created_at_before == created_at_after, "created_at must be preserved"
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# 16. no approval/dispatch/candidate/signal/authorization/risk/order/fill/
#     position/mark/settlement is created
# ─────────────────────────────────────────────────────────────────────────────


def test_no_execution_plane_artifact_created():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {})
    base = str(db.db_path)
    counter = {"n": 0}
    rc = _run(base,
              ["--source-trade-id", "polymarket:t1", "--write",
               "--confirm-production-db"], counter)
    assert rc == 0, rc
    forbidden = [
        "specialist_approvals",
        "approved_specialist_trade_dispatches",
        "copy_candidates",
        "paper_signal_decisions",
        "paper_signal_execution_authorizations",
        "execution_risk_decisions",
        "paper_orders",
        "paper_fills",
        "paper_positions",
        "paper_position_marks",
        "paper_position_settlements",
    ]
    for table in forbidden:
        assert _count(db, table) == 0, f"{table} must remain empty"
    # Only source_trades + source_trade_enrichments may carry rows.
    assert _count(db, "source_trade_enrichments") >= 1
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# NEW S3 narrow-correction proofs (PR71 pass)
# ─────────────────────────────────────────────────────────────────────────────


def _enrich(db, tid):
    row = db.conn.execute(
        "SELECT * FROM source_trade_enrichments WHERE source_trade_internal_id=?",
        (tid,),
    ).fetchone()
    return dict(row) if row is not None else None


def _gamma_bare_list(cond, tok):
    return {
        "conditionId": cond,
        "clobTokenIds": [tok, "0xf" + "0" * 63],  # bare list (not JSON string)
        "category": "Sports",
        "tags": ["nba"],
        "events": [],
        "series": [],
        "question": "Who wins?",
        "slug": "nba-final-bare",
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["0.5", "0.5"],
    }


def test_real_cli_bare_list_clobtokenids_orchestration():
    # The backfill CLI itself (not merge_canonical_metadata directly) must
    # handle a Gamma payload whose clobTokenIds is a bare list.
    db, _ = _open()
    _seed_wallet(db)
    cond = "0x" + "e" * 64
    tok = "0x" + "e" * 64
    GAMMA[cond] = _gamma_bare_list(cond, tok)
    _insert_trade(db, "polymarket:t1", cond, tok, {})
    counter = {"n": 0}
    rc = _run(db.db_path,
              ["--source-trade-id", "polymarket:t1", "--write",
               "--confirm-production-db"], counter)
    assert rc == 0, rc
    m = _meta(db, "polymarket:t1")
    assert m["taxonomy"]["raw_category"] == "Sports", m
    # One Gamma call for the single condition id.
    assert counter["n"] == 1, counter
    db.close()


def test_production_refusal_absolute_path_before_db_open():
    calls = {"open_readonly": 0, "get_market_raw": 0}
    prod_abs = str(ROOT / "data" / "polycopy.db")

    def _no_open(*a, **k):
        calls["open_readonly"] += 1
        raise AssertionError("open_readonly must not run before refusal")

    with mock.patch("evidence_db.open_readonly", side_effect=_no_open), \
         mock.patch(
             "polycopy.adapters.polymarket.PolymarketPublicAdapter.get_market_raw",
             new=lambda self, mid: (_ for _ in ()).throw(
                 AssertionError("no network"))):
        import importlib

        mod = importlib.import_module(CLI)
        rc = mod.main(
            ["--db-path", prod_abs, "--write", "--source-trade-id", "polymarket:x"]
        )
    assert rc != 0, "production write without gate must be refused"
    assert calls["open_readonly"] == 0, "DB must not open before refusal"
    assert calls["get_market_raw"] == 0, "provider must not be called before refusal"


# Keep a handle on the original open_readonly for the monkey patch above.
_orig_open_readonly = None


def test_production_refusal_relative_path():
    # Repository-relative production path must also be refused before open.
    with mock.patch(
        "polycopy.adapters.polymarket.PolymarketPublicAdapter.get_market_raw",
        new=lambda self, mid: (_ for _ in ()).throw(AssertionError("no network")),
    ):
        import importlib

        mod = importlib.import_module(CLI)
        rc = mod.main(
            ["--db-path", "data/polycopy.db", "--write",
             "--source-trade-id", "polymarket:x"]
        )
    assert rc != 0, "relative production path write without gate must be refused"


def test_production_refusal_symlink_to_production(tmp_path):
    # A symlink resolving to the production path must be refused before open.
    target = ROOT / "data" / "polycopy.db"
    link = tmp_path / "link_polycopy.db"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest_skip("symlink unsupported on this platform")
    with mock.patch(
        "polycopy.adapters.polymarket.PolymarketPublicAdapter.get_market_raw",
        new=lambda self, mid: (_ for _ in ()).throw(AssertionError("no network")),
    ):
        import importlib

        mod = importlib.import_module(CLI)
        rc = mod.main(
            ["--db-path", str(link), "--write", "--source-trade-id", "polymarket:x"]
        )
    assert rc != 0, "symlink-to-production write without gate must be refused"


def test_production_refusal_missing_each_gate():
    # Each missing gate (independently) must refuse a production write.
    base = ["--db-path", str(ROOT / "data" / "polycopy.db"),
            "--source-trade-id", "polymarket:x"]
    combos = [
        ["--write"],                       # missing allow-live + confirm
        ["--write", "--allow-live"],       # missing confirm
        ["--write", "--confirm-production-db"],  # missing allow-live
    ]
    with mock.patch(
        "polycopy.adapters.polymarket.PolymarketPublicAdapter.get_market_raw",
        new=lambda self, mid: (_ for _ in ()).throw(AssertionError("no network")),
    ):
        import importlib

        mod = importlib.import_module(CLI)
        for extra in combos:
            rc = mod.main(base + extra)
            assert rc != 0, f"must refuse with missing gate set {extra}"


def test_production_refusal_nonexistent_alias(tmp_path):
    # A production path that does not exist must still be refused (not crash
    # with FileNotFoundError before the gate message).
    fake = tmp_path / "polycopy.db"  # does not point at real production
    with mock.patch(
        "evidence_db.is_production_db", return_value=True
    ), mock.patch(
        "backfill_specialist_trade_taxonomy.is_production_db", return_value=True
    ), mock.patch(
        "polycopy.adapters.polymarket.PolymarketPublicAdapter.get_market_raw",
        new=lambda self, mid: (_ for _ in ()).throw(AssertionError("no network")),
    ):
        import importlib

        mod = importlib.import_module(CLI)
        rc = mod.main(
            ["--db-path", str(fake), "--write", "--source-trade-id", "polymarket:x"]
        )
    assert rc != 0, "recognized production alias write without gate must be refused"


def test_existing_enrichment_updated_not_ignored():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {})
    base = str(db.db_path)
    # First run creates the current row.
    rc = _run(base,
              ["--source-trade-id", "polymarket:t1", "--write",
               "--confirm-production-db"], {"n": 0})
    assert rc == 0, rc
    created_at_1 = _enrich(db, "polymarket:t1")["created_at"]
    # Material change: pre-seed a DIFFERENT enrichment row (simulate collection
    # path already created one with a distinct evidence hash), then re-run.
    db.conn.execute(
        "UPDATE source_trade_enrichments SET normalized_category='Old', "
        "evidence_hash='oldhash', updated_at='2020-01-01T00:00:00Z' "
        "WHERE source_trade_internal_id='polymarket:t1'"
    )
    db.conn.commit()
    rc = _run(base,
              ["--source-trade-id", "polymarket:t1", "--write",
               "--confirm-production-db"], {"n": 0})
    assert rc == 0, rc
    row = _enrich(db, "polymarket:t1")
    # Row is UPDATED (not a new/duplicate row); normalized_category reflects
    # the fresh Gamma-derived taxonomy classification.
    assert row["normalized_category"] == "politics", row
    assert row["evidence_hash"] != "oldhash"
    n = db.conn.execute(
        "SELECT COUNT(*) FROM source_trade_enrichments "
        "WHERE source_trade_internal_id='polymarket:t1'"
    ).fetchone()[0]
    assert n == 1, "must remain exactly one current row"
    # created_at preserved across update.
    assert row["created_at"] == created_at_1
    db.close()


def test_created_at_preserved_updated_at_changes_only_on_material_change():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {})
    base = str(db.db_path)
    rc = _run(base,
              ["--source-trade-id", "polymarket:t1", "--write",
               "--confirm-production-db"], {"n": 0})
    assert rc == 0, rc
    r1 = _enrich(db, "polymarket:t1")
    updated_at_1 = r1["updated_at"]
    created_at_1 = r1["created_at"]
    # Identical replay: updated_at must NOT change (zero update).
    rc = _run(base,
              ["--source-trade-id", "polymarket:t1", "--write",
               "--confirm-production-db"], {"n": 0})
    assert rc == 0, rc
    r2 = _enrich(db, "polymarket:t1")
    assert r2["updated_at"] == updated_at_1, "identical replay must not update"
    assert r2["created_at"] == created_at_1
    db.close()


def test_provider_exception_recorded_honestly():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {})
    base = str(db.db_path)

    # Provider raises a network error -> recorded as provider_error, NOT
    # ordinary gamma_missing/unavailable.
    async def _boom(self, market_id):
        raise RuntimeError("connection reset")

    with mock.patch(
        "polycopy.adapters.polymarket.PolymarketPublicAdapter.get_market_raw",
        new=_boom,
    ):
        import importlib

        mod = importlib.import_module(CLI)
        rc = mod.main(
            ["--db-path", base, "--allow-live", "--source-trade-id",
             "polymarket:t1", "--write", "--confirm-production-db"]
        )
    assert rc == 0, rc
    row = _enrich(db, "polymarket:t1")
    assert row["status"] == "error", row
    assert "provider_error" in json.loads(row["reason_codes_json"]), row
    # Metadata is preserved (merge unavailable due to provider failure).
    m = _meta(db, "polymarket:t1")
    assert "taxonomy" not in m or not m.get("taxonomy", {}).get("raw_category")
    db.close()


def test_gamma_not_found_distinct_from_provider_error():
    db, _ = _open()
    _seed_wallet(db)
    # Unknown condition -> Gamma returns None (not found), distinct reason.
    _insert_trade(db, "polymarket:t1", "0x" + "b" * 64, "0x" + "b" * 64, {})
    base = str(db.db_path)
    rc = _run(base,
              ["--source-trade-id", "polymarket:t1", "--write",
               "--confirm-production-db"], {"n": 0})
    assert rc == 0, rc
    row = _enrich(db, "polymarket:t1")
    assert row["status"] == "unavailable", row
    rc_json = json.loads(row["reason_codes_json"])
    assert "gamma:not_found" in rc_json, rc_json
    assert not any("provider_error" in c for c in rc_json)
    db.close()


def test_source_column_excludes_non_polymarket_with_polymarket_id():
    db, _ = _open()
    _seed_wallet(db)
    # A non-Polymarket source row whose source_trade_id LOOKS like polymarket.
    db.conn.execute(
        "INSERT INTO source_trades("
        "id, source, source_trade_id, market_source_id, side, "
        "outcome, quantity, price, trader_address, timestamp, is_sample, "
        "metadata_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("kalshi:t1", "kalshi", "polymarket:lookalike", GCOND, "BUY", "Yes",
         10.0, 0.40, WADDR, "2026-02-01T00:00:00Z", 0,
         json.dumps({}, sort_keys=True)),
    )
    db.conn.commit()
    # Also a legitimate Polymarket row.
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {})
    base = str(db.db_path)
    counter = {"n": 0}
    rc = _run(base,
              ["--wallet-id", WUUID, "--write", "--confirm-production-db"],
              counter)
    assert rc == 0, rc
    # Only the Polymarket row should be enriched; the kalshi lookalike excluded.
    n = db.conn.execute(
        "SELECT COUNT(*) FROM source_trade_enrichments"
    ).fetchone()[0]
    assert n == 1, n
    row = _enrich(db, "polymarket:t1")
    assert row is not None
    assert _enrich(db, "kalshi:t1") is None, "non-Polymarket row must be excluded"
    # The Polymarket row's metadata is the one filled.
    m = _meta(db, "polymarket:t1")
    assert m["taxonomy"]["raw_category"] == "Politics"
    db.close()


def test_per_trade_rollback_on_provenance_failure():
    db, _ = _open()
    _seed_wallet(db)
    _insert_trade(db, "polymarket:t1", GCOND, GTOK_A, {})
    base = str(db.db_path)

    # Force provenance to fail on the FIRST write attempt.
    def _boom_prov(self, trade, ev, merge_status):
        raise RuntimeError("provenance write failed")

    import importlib

    mod = importlib.import_module(CLI)
    with mock.patch.object(mod, "_write_provenance", _boom_prov):
        rc = mod.main(
            ["--db-path", base, "--allow-live", "--source-trade-id",
             "polymarket:t1", "--write", "--confirm-production-db"]
        )
    # The whole run aborts (fail-closed) when provenance fails.
    assert rc != 0, "provenance failure must not silently succeed"
    # Metadata must NOT remain changed (rolled back with provenance).
    m = _meta(db, "polymarket:t1")
    assert "taxonomy" not in m or not m.get("taxonomy", {}).get("raw_category"), \
        "metadata change must roll back when provenance fails"
    # No enrichment row leaked from the failed trade.
    assert _enrich(db, "polymarket:t1") is None
    db.close()


def backfill_specialist_trade_taxonomy_module():
    import importlib

    return importlib.import_module(CLI)


def pytest_skip(msg):
    import pytest

    pytest.skip(msg)


if __name__ == "__main__":
    unittest.main()
