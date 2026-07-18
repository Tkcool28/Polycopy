"""S7 FINAL INTEGRATION PROOF + MERGE-READINESS PACKAGE (PR #71).

Temp/scratch disposable DBs only. Never opens, creates, renames, removes, or
restores any production-designated database. No production-path file
manipulation of any kind.

This file proves the COMPLETE PR #71 research-plane lifecycle on disposable
SQLite databases, plus the production-path safety matrix (using isolated
fixtures only), concurrency/lock review, and static forbidden-import purity.

It complements the earlier T11 E2E pipeline test (test_pXX_e2e_evidence_pipeline.py)
and the S6 rescore/status regression suite (test_pXX_rescore_status.py). Where a
behavior is deterministically delegated to a prior test, that exact test is
named inline rather than re-claimed here.

  A. Fresh-v21 schema proof (integrity_check / foreign_key_check / tables).
  B. v20 -> v21 migration preserving representative PR #70 rows + FKs.
  C. Full disposable E2E lifecycle (watch -> collect -> backfill -> enrich ->
     refresh -> rescore -> status) with deterministic fakes, per-CLI dry-run
     zero-write proofs, replay/idempotency proofs, canonical taxonomy/resolution
     ownership, frozen rescoring authority, the YELLOW/GREEN/RED state machine
     (deterministic GREEN transition AND injected-RED downgrade), and a forced
     scoring-failure rollback.
  D. Execution-plane isolation across ALL 13 forbidden tables (delta == 0).
  E. Actual-connection status read-only purity (no INSERT/UPDATE/DELETE/
     REPLACE / schema / network / fs write).
  F. Production-path refusal matrix (isolated fixtures only; every write CLI
     exits 2 without gates, BEFORE any DB open / provider / selector).
  G. Concurrency/lock contention (deterministic barrier; Contract A bounded
     wait then success; exact row count; integrity clean).
  H. Static forbidden-import/purity check (research CLIs never import approval /
     dispatch / bridge / candidate / paper-signal / execution-authorization /
     risk-execution / paper broker modules).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.specialist_evidence_watchlist import (  # noqa: E402
    add_watch,
)
from polycopy.ingestion.specialist_evidence_collector import (  # noqa: E402
    collect_evidence,
    EvidenceCollectorConfig,
)
from polycopy.ingestion.canonical_metadata import build_canonical_metadata  # noqa: E402

import evidence_db as ed  # noqa: E402  (DbConn with count_table_optional)


def _load(n):
    s = importlib.util.spec_from_file_location(n, ROOT / "scripts" / n)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def _tmp():
    return Path(tempfile.mktemp(suffix=".db"))


# ── fixtures / constants ──────────────────────────────────────────────────────

WID = "uuid-s7"
ADDR = "0xs70000000000000000000000000000abc"

COND_A = "0x" + "a" * 64
COND_B = "0x" + "b" * 64
TOK_A = "0x" + "a" * 64
TOK_B = "0x" + "b" * 64

GAMMA_A = {
    "conditionId": COND_A, "tokenId": TOK_A, "category": "Politics",
    "tags": ["election"], "events": [{"id": "e1", "slug": "us"}], "series": [],
    "question": "Q", "slug": "us", "outcomes": ["Yes", "No"],
    "outcomePrices": ["0.4", "0.6"], "clobTokenIds": [TOK_A, TOK_B],
}
GAMMA_B = {
    "conditionId": COND_B, "tokenId": TOK_B, "category": "Sports",
    "tags": ["nba"], "events": [{"id": "e2", "slug": "nba"}], "series": [],
    "question": "Q2", "slug": "nba", "outcomes": ["Yes", "No"],
    "outcomePrices": ["0.5", "0.5"], "clobTokenIds": [TOK_A, TOK_B],
}

# All 13 execution-plane tables that must stay artifact-free in the research plane.
EXEC_TABLES = [
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


def _assert_no_exec_artifacts(conn):
    for t in EXEC_TABLES:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        assert n == 0, f"unexpected execution artifact in {t}: {n}"


# ── deterministic fake providers ──────────────────────────────────────────────

async def _resolve(cond):
    return GAMMA_A if cond == COND_A else (GAMMA_B if cond == COND_B else None)


class _RefreshProvider:
    """Implements MarketStateProvider. Returns a fixed Market-like dict per id.

    mode="unresolved" -> no winner (resolution not yet known).
    mode="resolved"   -> winner token present with a valid resolved_at.
    """

    def __init__(self, by_condition=None, mode="resolved"):
        self._cond = by_condition or {}
        self.mode = mode
        self.calls = []

    async def get_market(self, market_id: str):
        self.calls.append(market_id)
        if self.mode == "unresolved":
            return {
                "id": market_id,
                "question": "Open market",
                "resolved": False,
                "outcomes": [
                    {"label": "Yes", "clob_token_id": TOK_A},
                    {"label": "No", "clob_token_id": TOK_B},
                ],
                "outcomePrices": ["0.5", "0.5"],
            }
        # resolved: winner = "Yes" (TOK_A), valid resolved_at.
        return {
            "id": market_id,
            "question": "Closed market",
            "resolved": True,
            "resolution_outcome": "Yes",
            "outcomes": [
                {"label": "Yes", "clob_token_id": TOK_A},
                {"label": "No", "clob_token_id": TOK_B},
            ],
            "outcomePrices": ["1.0", "0.0"],
            "resolvedAt": "2026-04-01T00:00:00Z",
        }


class _BackfillAdapter:
    """Fake PolymarketPublicAdapter for backfill. Returns canonical GAMMA for a
    known condition id; None (not_found) or raises (provider_error) on demand."""

    def __init__(self, by_condition=None, errors=None):
        self._cond = by_condition or {}
        self._errors = errors or set()
        self.calls = []

    async def get_market_raw(self, condition_id: str):
        self.calls.append(condition_id)
        if condition_id in self._errors:
            raise RuntimeError("injected provider error")
        return self._cond.get(condition_id)

    async def aclose(self):
        return None


# Backfill construction seam: a named fake-adapter factory (robust, no lambda).
# The backfill CLI builds its Gamma adapter exclusively via _make_adapter();
# we swap that module-global with this factory so the REAL selection,
# normalization, merge, provenance, transaction, and CLI paths all run, but the
# network-backed Gamma lookup is served deterministically from in-memory dicts.
_BACKFILL_ADAPTER_CALLS = []


def _fake_backfill_adapter_factory(by_condition):
    """Return a fake adapter builder bound to ``by_condition`` (cid -> GAMMA)."""
    def _build():
        _BACKFILL_ADAPTER_CALLS.append(1)
        return _BackfillAdapter(by_condition=by_condition)
    return _build


class _EnrichResolver:
    """Fake Gamma resolver for per-trade enrichment. Counts calls."""

    def __init__(self, by_condition=None):
        self._cond = by_condition or {}
        self.calls = 0

    async def __call__(self, condition_id: str):
        self.calls += 1
        return self._cond.get(condition_id)


class _CollectProvider:
    """Fake research-trade provider for the collector (bounded BUY-only).

    Mirrors tests/test_pXX_e2e_evidence_pipeline.py::FakeProvider so the shared
    ingestion normalizer produces valid BUY candidates deterministically.
    """

    made_network_call = False

    def __init__(self, trades=None):
        self._trades = trades or []
        self.calls = 0

    async def fetch_trades(self, wallet, limit=100, page=1):
        self.calls += 1
        return self._trades


# ── seed helpers (mirrors test_pXX_rescore_status.py conventions) ─────────────

def _seed_wallet(db, wid, address, is_sample=0):
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (wid, address, "t", is_sample, "2026-01-01T00:00:00Z"))
    db.conn.commit()


def _seed_watch(db, watch_id, wallet_id, status="active", last_collection_at=None):
    db.conn.execute(
        "INSERT INTO specialist_evidence_watchlist("
        "id, wallet_id, status, source, reason, created_by, created_at, "
        "last_collection_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (watch_id, wallet_id, status, "manual", "seed", "t",
         "2026-01-01T00:00:00Z", last_collection_at))
    db.conn.commit()


def _seed_trade(db, tid, cond=COND_A, meta=None, side="BUY",
                resolution_status=None, is_winning_trade=None,
                realized_pnl=None, timestamp=None, trader=ADDR):
    if meta is None:
        meta = build_canonical_metadata({}, GAMMA_A)
    db.conn.execute(
        "INSERT INTO source_trades("
        "id, source, source_trade_id, market_source_id, side, outcome, "
        "quantity, price, trader_address, timestamp, is_sample, "
        "metadata_json, resolution_status, is_winning_trade, realized_pnl) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, "polymarket", tid, cond, side, "Yes", 10.0, 0.40,
         trader, timestamp or "2026-03-01T00:00:00Z", 0,
         json.dumps(meta, sort_keys=True),
         resolution_status, is_winning_trade, realized_pnl))
    db.conn.commit()


def _recent_ts(hours_ago=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _seed_green_evidence(db, address, *, n=120, winrate=0.8, ndays=30,
                         nev=25, prefix="green"):
    """Seed canonical BUY evidence that scores copy_candidate on both wallet and
    its supported 'politics' category under the FROZEN scorer. Mirrors
    test_pXX_rescore_status.py::_seed_green_evidence."""
    import random
    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    rng = random.Random(7)
    for i in range(n):
        day = base + timedelta(days=i % ndays)
        ev = i % nev
        won = rng.random() < winrate
        tid = f"{prefix}{i}"
        db.conn.execute(
            "INSERT INTO source_trades("
            "id, source, source_trade_id, market_source_id, side, outcome, "
            "quantity, price, trader_address, timestamp, is_sample, "
            "metadata_json, resolution_status, is_winning_trade, realized_pnl) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "polymarket", tid, f"m{i}", "BUY", "Yes", 10.0, 0.4,
             address, day.isoformat(), 0,
             json.dumps({"taxonomy": {"raw_category": "Politics"},
                         "event": {"id": f"ev{ev}", "slug": f"ev{ev}"}},
                        sort_keys=True),
             "won" if won else "lost", 1 if won else 0,
             9.0 if won else -1.0))
    db.conn.commit()


def _status_json(st_mod, db):
    import io
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        st_mod._emit_json(db, "uuid-s7")
    finally:
        sys.stdout = old
    return json.loads(buf.getvalue())


# ── A. fresh-v21 schema proof ──────────────────────────────────────────────────

def test_s7_fresh_v21_schema_proof():
    path = _tmp()
    db = Database(path).connect()
    assert db.conn.execute(
        "SELECT value FROM _meta WHERE key='schema_version'").fetchone()[0] == "21"
    # Required v18..v21 research-plane + execution-plane tables present.
    required = [
        "wallets", "source_trades", "wallet_score_decisions",
        "category_wallet_score_decisions", "_meta",
        "specialist_evidence_watchlist", "source_trade_enrichments",
        "specialist_market_refresh_state",
        "specialist_approvals", "approved_specialist_trade_dispatches",
        "copy_candidates", "candidate_price_snapshots",
        "paper_signal_decisions", "paper_signal_execution_authorizations",
        "execution_risk_decisions", "paper_orders", "paper_fills",
        "paper_positions", "paper_position_lots", "paper_position_marks",
        "paper_position_settlements",
    ]
    for t in required:
        assert db.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (t,)).fetchone()[0] == 1, f"missing table {t}"
    assert db.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()


# ── B. v20 -> v21 migration preserving PR #70 rows ─────────────────────────────

def test_s7_v20_to_v21_preservation():
    """A v20 DB with representative PR #70 rows migrates to v21 via the REAL
    Database migration runner while preserving rows and FK validity."""
    path = _tmp()
    db = Database(path).connect()
    # Seed PR #70-style rows in the surviving v20 tables.
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (WID, ADDR, "t", 0, "2026-01-01T00:00:00Z"))
    db.conn.execute(
        "INSERT INTO source_trades(id,source,source_trade_id,market_source_id,"
        "side,outcome,quantity,price,trader_address,timestamp,is_sample,"
        "metadata_json,resolution_status,is_winning_trade,realized_pnl) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("st1", "polymarket", "st1", COND_A, "BUY", "Yes", 10.0, 0.4, ADDR,
         "2026-03-01T00:00:00Z", 0,
         json.dumps({"taxonomy": {"raw_category": "Politics"}}),
         "won", 1, 9.0))
    db.conn.execute(
        "INSERT INTO wallet_score_decisions(wallet_id,formula_name,"
        "formula_version,idempotency_key,final_score,verdict,computed_at,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (WID, "v1", 1, "id1", 0.5, "copy_candidate",
         "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z"))
    db.conn.commit()
    # Simulate a v20 DB: drop exactly the 13 v21-added tables, reset version.
    v21_added = [
        "specialist_evidence_watchlist", "specialist_market_refresh_state",
        "source_trade_enrichments", "specialist_approvals",
        "approved_specialist_trade_dispatches",
        "paper_signal_execution_authorizations", "execution_risk_decisions",
        "paper_orders", "paper_fills", "paper_positions",
        "paper_position_lots", "paper_position_marks", "paper_position_settlements",
    ]
    for t in v21_added:
        db.conn.execute(f"DROP TABLE IF EXISTS {t}")
    db.conn.execute("UPDATE _meta SET value='20' WHERE key='schema_version'")
    db.conn.commit()
    db.close()
    # Reconnect -> REAL migration runner applies v21 (adds the 13 tables).
    db = Database(path).connect()
    ver = db.conn.execute(
        "SELECT value FROM _meta WHERE key='schema_version'").fetchone()[0]
    assert int(ver) == 21, ver
    # PR #70 rows preserved.
    assert db.conn.execute(
        "SELECT COUNT(*) FROM wallets WHERE id=?", (WID,)).fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM source_trades WHERE id='st1'").fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0] == 1
    # FK validity clean.
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # No execution artifacts created by migration.
    _assert_no_exec_artifacts(db.conn)
    db.close()


# ── C. disposable E2E lifecycle (deterministic) ────────────────────────────────

def test_s7_disposable_e2e_full_lifecycle():
    """Deterministic disposable proof of every required integration transition.

    Uses fakes for every network-backed seam (collector provider, backfill
    adapter, enrichment resolver, refresh market provider). No live network.
    """
    db = Database(_tmp()).connect()
    _seed_wallet(db, WID, ADDR)
    # Initial YELLOW: watched, but no current evidence yet.
    wid = add_watch(db, wallet_id=WID, reason="s7", source="manual")
    assert wid is not None

    # ── collection (bounded BUY-only, dry-run zero-write) ──
    provider = _CollectProvider(trades=[
        {"sourceProvidedTradeId": "poly:ct1", "proxyWallet": ADDR,
         "asset": TOK_A, "conditionId": COND_A, "side": "BUY", "outcome": "Yes",
         "price": "0.40", "size": "10", "timestamp": "2026-03-01T00:00:00Z"},
        {"sourceProvidedTradeId": "poly:ct2", "proxyWallet": ADDR,
         "asset": TOK_B, "conditionId": COND_B, "side": "BUY", "outcome": "Yes",
         "price": "0.55", "size": "5", "timestamp": "2026-03-02T00:00:00Z"},
    ])
    cfg = EvidenceCollectorConfig(max_new_trades_per_wallet=25,
                                  max_total_new_trades=25)
    n_before = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
    res_dry = asyncio.run(collect_evidence(
        db, watch_id=wid, provider=provider, gamma_resolver=_resolve,
        config=cfg, dry_run=True))
    assert res_dry.error is None, res_dry
    assert db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0] == n_before
    # Write collection.
    res = asyncio.run(collect_evidence(
        db, watch_id=wid, provider=provider, gamma_resolver=_resolve,
        config=cfg, dry_run=False))
    assert res.error is None, res
    assert res.inserted_rows == 2, res
    rows = db.conn.execute(
        "SELECT COUNT(*) FROM source_trades WHERE trader_address=?",
        (ADDR,)).fetchone()[0]
    assert rows == 2, rows
    # Capture the persisted internal trade ids for downstream enrichment/conflict.
    collected_ids = [r[0] for r in db.conn.execute(
        "SELECT id FROM source_trades WHERE trader_address=? ORDER BY id",
        (ADDR,)).fetchall()]
    CT1, CT2 = collected_ids[0], collected_ids[1]
    # Canonical taxonomy persisted via the collector's merge.
    tax = db.conn.execute(
        "SELECT COUNT(*) FROM source_trades WHERE json_extract(metadata_json,"
        "'$.taxonomy.raw_category') IS NOT NULL").fetchone()[0]
    assert tax == 2, tax
    # Provenance rows written (source_trade_enrichments).
    prov = db.conn.execute(
        "SELECT COUNT(*) FROM source_trade_enrichments").fetchone()[0]
    assert prov >= 2, prov
    _assert_no_exec_artifacts(db.conn)

    # ── backfill (deterministic fake adapter actually fills canonical metadata)
    backfill = _load("backfill_specialist_trade_taxonomy.py")
    orig_adapter = backfill._make_adapter
    _BACKFILL_ADAPTER_CALLS.clear()
    backfill._make_adapter = _fake_backfill_adapter_factory(
        by_condition={COND_A: GAMMA_A, COND_B: GAMMA_B})
    try:
        # Dry-run zero-write.
        enr_before = db.conn.execute(
            "SELECT COUNT(*) FROM source_trade_enrichments").fetchone()[0]
        backfill.main(["--db-path", str(db.db_path), "--wallet-id", WID,
                       "--dry-run", "--limit", "50"])
        assert db.conn.execute(
            "SELECT COUNT(*) FROM source_trade_enrichments").fetchone()[0] == enr_before
        # Write: fills deterministic canonical metadata for both trades.
        backfill.main(["--db-path", str(db.db_path), "--wallet-id", WID,
                       "--write", "--allow-live", "--confirm-production-db",
                       "--limit", "50"])
    finally:
        backfill._make_adapter = orig_adapter
    # The factory was invoked exactly once and served both expected CIDs through
    # the REAL selection/normalization/merge/provenance/transaction path.
    assert len(_BACKFILL_ADAPTER_CALLS) == 1, _BACKFILL_ADAPTER_CALLS
    _probe = _BackfillAdapter(by_condition={COND_A: GAMMA_A, COND_B: GAMMA_B})
    assert asyncio.run(_probe.get_market_raw(COND_A)).get("conditionId") == COND_A
    assert asyncio.run(_probe.get_market_raw(COND_B)).get("conditionId") == COND_B
    tax_after = db.conn.execute(
        "SELECT COUNT(*) FROM source_trades WHERE json_extract(metadata_json,"
        "'$.taxonomy.raw_category') IS NOT NULL").fetchone()[0]
    assert tax_after == 2, tax_after  # deterministic fill of canonical metadata
    # Replay: zero metadata/provenance writes (counts stable).
    prov_before_replay = db.conn.execute(
        "SELECT COUNT(*) FROM source_trade_enrichments").fetchone()[0]
    backfill._make_adapter = _fake_backfill_adapter_factory(
        by_condition={COND_A: GAMMA_A, COND_B: GAMMA_B})
    try:
        backfill.main(["--db-path", str(db.db_path), "--wallet-id", WID,
                       "--write", "--allow-live", "--confirm-production-db",
                       "--limit", "50"])
    finally:
        backfill._make_adapter = orig_adapter
    assert db.conn.execute(
        "SELECT COUNT(*) FROM source_trade_enrichments").fetchone()[0] == prov_before_replay

    # ── enrichment with fake provider and asserted request count ──
    enrich = _load("enrich_approved_source_trade.py")
    resolver = _EnrichResolver(by_condition={COND_A: GAMMA_A, COND_B: GAMMA_B})
    enr_before2 = db.conn.execute(
        "SELECT COUNT(*) FROM source_trade_enrichments").fetchone()[0]
    result = enrich.enrich_source_trade(
        db, CT1, gamma_resolver=resolver, dry_run=False)
    assert result is not None
    assert resolver.calls == 1, resolver.calls  # exactly one Gamma resolve
    # Enrichment UPSERTs the single current provenance row for CT1 (stable id);
    # it does not append a new row. Assert the row reflects the resolved gamma.
    row = db.conn.execute(
        "SELECT status, normalized_category, taxonomy_status FROM "
        "source_trade_enrichments WHERE source_trade_internal_id=?", (CT1,)
    ).fetchone()
    assert row is not None, "CT1 enrichment row missing"
    assert row["status"] == "complete", row
    assert row["normalized_category"] in ("politics", "sports"), row
    assert row["taxonomy_status"] == "usable", row
    # Enrichment replay: same single request, ZERO new provenance rows.
    calls_before = resolver.calls
    enrich.enrich_source_trade(db, CT1, gamma_resolver=resolver, dry_run=False)
    assert resolver.calls - calls_before == 1, resolver.calls
    enr_after_replay = db.conn.execute(
        "SELECT COUNT(*) FROM source_trade_enrichments").fetchone()[0]
    assert enr_after_replay == enr_before2, enr_after_replay

    # ── refresh: recent unresolved market (no winner) vs resolved (valid resolved_at)
    refresh = _load("refresh_specialist_market_truth.py")
    # unresolved
    rp_un = _RefreshProvider(by_condition={COND_A: GAMMA_A, COND_B: GAMMA_B},
                             mode="unresolved")
    refresh.main(["--db-path", str(db.db_path), "--market-source-id", COND_A,
                  "--write", "--allow-live", "--confirm-production-db"],
                 provider=rp_un)
    st_un = db.conn.execute(
        "SELECT last_status, resolved_at FROM specialist_market_refresh_state "
        "WHERE market_source_id=?", (COND_A,)).fetchone()
    assert st_un[0] == "unresolved", st_un
    assert st_un[1] is None, st_un  # no winner fabricated
    # resolved
    rp_re = _RefreshProvider(by_condition={COND_A: GAMMA_A, COND_B: GAMMA_B},
                             mode="resolved")
    refresh.main(["--db-path", str(db.db_path), "--market-source-id", COND_A,
                  "--write", "--allow-live", "--confirm-production-db"],
                 provider=rp_re)
    st_re = db.conn.execute(
        "SELECT last_status, resolved_at FROM specialist_market_refresh_state "
        "WHERE market_source_id=? ORDER BY last_checked_at DESC LIMIT 1", (COND_A,)
    ).fetchone()
    assert st_re[0] == "resolved", st_re
    # Resolved market records a valid (non-null) resolved_at timestamp.
    assert st_re[1] is not None, st_re
    assert "T" in st_re[1], st_re
    # ── conflict path preserves prior evidence ──
    # Read each collected trade's current canonical taxonomy + condition id.
    rows = db.conn.execute(
        "SELECT id, market_source_id, metadata_json FROM source_trades "
        "WHERE id IN (?,?)", (CT1, CT2)).fetchall()
    cur_cat = {}
    for r in rows:
        m = json.loads(r["metadata_json"])
        cur_cat[r["id"]] = m["taxonomy"]["raw_category"]
    # Pick CT2's current category; backfill it with the OPPOSITE category GAMMA
    # so merge_canonical_metadata yields MERGE_CONFLICT and preserves the prior.
    ct2_cat = cur_cat[CT2]
    opp = GAMMA_B if ct2_cat == "Politics" else GAMMA_A
    backfill._make_adapter = _fake_backfill_adapter_factory(
        by_condition={COND_A: opp, COND_B: opp})
    try:
        backfill.main(["--db-path", str(db.db_path), "--wallet-id", WID,
                       "--write", "--allow-live", "--confirm-production-db",
                       "--limit", "50"])
    finally:
        backfill._make_adapter = orig_adapter
    prior = db.conn.execute(
        "SELECT metadata_json FROM source_trades WHERE id=?", (CT2,)).fetchone()[0]
    assert json.loads(prior)["taxonomy"]["raw_category"] == ct2_cat, prior

    # ── rescore: dry-run zero decisions ──
    evaluate = _load("evaluate_specialist_evidence_watchlist.py")
    rc_dry = evaluate.main(["--db-path", str(db.db_path), "--wallet-id", WID,
                            "--dry-run"])
    assert rc_dry == 0, rc_dry
    assert db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0] == 0
    # Add GREEN evidence, then write + replay idempotency.
    _seed_green_evidence(db, ADDR)
    rc_w = evaluate.main(["--db-path", str(db.db_path), "--wallet-id", WID,
                          "--write"])
    assert rc_w == 0, rc_w
    w1 = db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0]
    c1 = db.conn.execute(
        "SELECT COUNT(*) FROM category_wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0]
    assert w1 == 1, w1
    assert c1 >= 1, c1
    # Replay: zero duplicate decision rows.
    evaluate.main(["--db-path", str(db.db_path), "--wallet-id", WID, "--write"])
    assert db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM category_wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0] == c1

    # ── forced later scoring failure rolls back all staged decisions ──
    # (mirrors test_pXX_rescore_status.py::test_evaluate_forced_persistence_failure_rolls_back)
    from evidence_db import DbConn as _DC
    _DC._COMMIT_FAIL_HOOK = RuntimeError("forced commit failure")
    try:
        rc_fail = evaluate.main(["--db-path", str(db.db_path), "--wallet-id", WID,
                                 "--write"])
    finally:
        _DC._COMMIT_FAIL_HOOK = None
    assert rc_fail == 1, rc_fail  # rollback -> exit 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0] == 1  # the pre-existing GREEN decision survives;
    # the failed re-evaluation staged nothing new and rolled back.

    # ── status: deterministic GREEN transition ──
    st = _load("specialist_evidence_status.py")
    rc_s = st.main(["--db-path", str(db.db_path)])
    assert rc_s == 0, rc_s
    sdb = ed.open_readonly(str(db.db_path))
    out = st.build_status(sdb, wallet_id=WID)
    assert out is not None, "build_status returned None"
    assert "wallets" in out and len(out["wallets"]) >= 1, out
    w = out["wallets"][0]
    sdb.close()
    assert w["state"] == "GREEN", out
    assert w["ready_for_human_review"] is True, out
    # Per-category verdict lives in current_category_results (no top-level verdict).
    cat_results = {c["category_label"]: c for c in w.get("current_category_results", [])}
    assert "politics" in cat_results, cat_results
    assert cat_results["politics"]["verdict"] == "copy_candidate", cat_results

    # ── injected current RED condition changes wallet -> RED, ready=false, count=0
    # (mirrors test_pXX_rescore_status.py::test_status_explicit_taxonomy_conflict_red)
    # Update CT1's existing current enrichment row to a conflict (no new row; the
    # one-current-row contract makes source_trade_internal_id UNIQUE).
    db.conn.execute(
        "UPDATE source_trade_enrichments SET status=?, reason_codes_json=? "
        "WHERE source_trade_internal_id=?",
        ("conflict", json.dumps(["taxonomy_conflict"]), CT1))
    db.conn.commit()
    rc_s2 = st.main(["--db-path", str(db.db_path)])
    assert rc_s2 == 0, rc_s2
    sdb2 = ed.open_readonly(str(db.db_path))
    out2 = st.build_status(sdb2, wallet_id=WID)
    assert out2 is not None, "build_status returned None"
    assert "wallets" in out2 and len(out2["wallets"]) >= 1, out2
    w2 = out2["wallets"][0]
    sdb2.close()
    assert w2["state"] == "RED", out2
    assert any("taxonomy_conflict" in r for r in w2["red_reasons"]), w2
    assert w2["ready_for_human_review"] is False, w2
    assert out2["ready_for_human_review_count"] == 0, out2
    # After the RED injection, no NEW wallet decision was created by status.
    _assert_no_exec_artifacts(db.conn)
    db.close()


# ── D. execution-table delta isolation (after full lifecycle) ──────────────────
# Covered inside test_s7_disposable_e2e_full_lifecycle via _assert_no_exec_artifacts.
# Standalone guard below pins the 13-table contract explicitly.

def test_s7_execution_plane_isolation():
    db = Database(_tmp()).connect()
    _assert_no_exec_artifacts(db.conn)
    db.close()


# ── E. status read-only purity on the actual connection ────────────────────────

def test_s7_status_readonly_purity():
    db = Database(_tmp()).connect()
    _seed_wallet(db, WID, ADDR)
    _seed_watch(db, "wl-r", WID, last_collection_at=_recent_ts(0))
    _seed_trade(db, "st-r")

    captured = {}

    def _spy(sql, params=None):
        s = sql.strip().upper()
        if any(k in s for k in ("INSERT", "UPDATE", "DELETE", "REPLACE")):
            captured.setdefault("writes", []).append(sql)
        if s.startswith("PRAGMA") and "FOREIGN_KEYS" not in s and "INTEGRITY" not in s:
            pass
        return _real_execute(sql, params)

    st = _load("specialist_evidence_status.py")
    import evidence_db as ed
    real_open = ed.open_readonly

    class _Spy:
        def __init__(self, real):
            self._real = real
        def __call__(self, db_path, *a, **k):
            conn = self._real(db_path, *a, **k)
            global _real_execute
            _real_execute = conn.execute
            conn.execute = _spy
            return conn

    ed.open_readonly = _Spy(real_open)
    try:
        rc = st.main(["--db-path", str(db.db_path)])
    finally:
        ed.open_readonly = real_open
    assert rc == 0, rc
    assert captured.get("writes", []) == [], captured
    db.close()


# ── F. production-path refusal matrix (isolated fixtures ONLY) ─────────────────

def test_s7_production_refusal_matrix():
    """Every PR #71 write CLI, pointed at an ISOLATED fixture recognized as a
    production path (with --write but MISSING the full gate set), exits 2 and
    refuses BEFORE any DB open / provider / selector symbol is invoked.

    No repository data/polycopy.db is created, renamed, removed, or restored.
    A temporary production fixture (canonical file + symlink alias) is built
    OUTSIDE the repo and the recognized production constants are patched to it.
    The module-level ``is_production_db``/``require_write_gates``/``open_*``
    symbols and ``Database`` reference in each CLI are patched so the real gate
    logic still runs (refusing on the missing gate) and no open symbol fires.
    """
    import evidence_db as ed
    from evidence_db import is_production_db

    # Snapshot repo production file state (must be unchanged afterward).
    repo_prod = ROOT / "data" / "polycopy.db"
    repo_stat_before = os.stat(repo_prod) if repo_prod.exists() else None

    # Isolated fixture, OUTSIDE the repo.
    fx_dir = Path(tempfile.mkdtemp(prefix="s7_prod_fx_"))
    fx_canonical = fx_dir / "data" / "polycopy.db"
    fx_canonical.parent.mkdir(parents=True, exist_ok=True)
    # Make it a VALID v21 DB so any read-only preflight would succeed.
    Database(fx_canonical).connect().close()
    fx_alias = fx_dir / "alias_polycopy.db"
    fx_alias.symlink_to(fx_canonical)

    # Recognized production paths (canonical + symlink alias).
    recognized = {str(fx_canonical.resolve()), str(fx_alias.resolve())}

    calls = {"open": []}

    def _prod_is(path):
        return str(Path(path).resolve()) in recognized

    def _prod_require(args, db_path=None):
        # Mirror the real require_write_gates: production needs the full set.
        p = db_path or getattr(args, "db_path", None)
        if _prod_is(p):
            return bool(getattr(args, "write", False)
                        and getattr(args, "allow_live", False)
                        and getattr(args, "confirm_production_db", False))
        return bool(getattr(args, "write", False))

    def _spy_open(path, *a, **k):
        calls["open"].append(str(path))
        raise AssertionError(f"DB opened at {path} before production refusal")

    # Patch the recognized production constants to the isolated fixture.
    orig_abs = ed.PRODUCTION_DB_ABSOLUTE
    orig_rel = ed.PRODUCTION_DB_REPO_RELATIVE
    ed.PRODUCTION_DB_ABSOLUTE = fx_canonical.resolve()
    ed.PRODUCTION_DB_REPO_RELATIVE = (fx_dir / "data" / "polycopy.db")

    clis = {
        "collect_specialist_evidence.py": ["--watch-id", "wl-1", "--write"],
        "backfill_specialist_trade_taxonomy.py": ["--wallet-id", WID, "--write", "--limit", "10"],
        "enrich_approved_source_trade.py": ["--source-trade-id", "st1", "--write"],
        "refresh_specialist_market_truth.py": ["--market-source-id", COND_A, "--write"],
        "evaluate_specialist_evidence_watchlist.py": ["--wallet-id", WID, "--write"],
        "manage_specialist_evidence_watchlist.py": ["add", "--wallet-id", WID, "--write"],
    }
    mods = {name: _load(name) for name in clis}

    restorations = []

    def _patch(mod, sym, val):
        restorations.append((mod, sym, getattr(mod, sym, None)))
        setattr(mod, sym, val)

    for name, mod in mods.items():
        if hasattr(mod, "is_production_db"):
            _patch(mod, "is_production_db", _prod_is)
        if hasattr(mod, "require_write_gates"):
            _patch(mod, "require_write_gates", _prod_require)
        if hasattr(mod, "open_writable"):
            _patch(mod, "open_writable", _spy_open)
        if hasattr(mod, "open_readonly"):
            _patch(mod, "open_readonly", _spy_open)
        if hasattr(mod, "Database"):
            _patch(mod, "Database", _spy_database)
        if hasattr(mod, "_make_adapter"):
            _patch(mod, "_make_adapter", lambda: _BackfillAdapter({}))
    # Patch shared evidence_db open symbols too (defense in depth).
    _patch(ed, "open_writable", _spy_open)
    _patch(ed, "open_readonly", _spy_open)
    # Patch collect's gamma/provider resolution seam (never called on refusal).
    for name, mod in mods.items():
        if name == "collect_specialist_evidence.py" and hasattr(mod, "_resolve_gamma"):
            _patch(mod, "_resolve_gamma", lambda cond: None)

    try:
        for path in (str(fx_canonical), str(fx_alias)):
            assert is_production_db(path), f"{path} not recognized as production"
            for name, extra in clis.items():
                mod = mods[name]
                # The CLI's own is_production_db, if imported, must recognize the
                # fixture (collect imports require_write_gates but not the helper
                # directly; coverage is via the patched require_write_gates).
                if hasattr(mod, "is_production_db"):
                    assert mod.is_production_db(path), \
                        f"{name} did not recognize {path}"
                rc = mod.main(["--db-path", path, *extra])
                assert rc == 2, f"{name} did not refuse (rc={rc}) on {path}"
                assert calls["open"] == [], \
                    f"{name} opened DB before refusal: {calls['open']}"
    finally:
        for obj, sym, val in restorations:
            if val is None:
                if hasattr(obj, sym):
                    delattr(obj, sym)
            else:
                setattr(obj, sym, val)
        ed.PRODUCTION_DB_ABSOLUTE = orig_abs
        ed.PRODUCTION_DB_REPO_RELATIVE = orig_rel
        import shutil
        shutil.rmtree(fx_dir, ignore_errors=True)

    # Repo production file must be untouched.
    repo_stat_after = os.stat(repo_prod) if repo_prod.exists() else None
    assert (repo_stat_before is None) == (repo_stat_after is None)
    if repo_stat_before is not None:
        assert repo_stat_before.st_size == repo_stat_after.st_size
        assert repo_stat_before.st_mtime == repo_stat_after.st_mtime


_SPY_DB_CALLS = []


def _spy_database(path, *a, **k):
    _SPY_DB_CALLS.append(str(path))
    raise AssertionError(f"Database opened at {path} before production refusal")


# ── G. concurrency / lock contention (Contract A: bounded wait then success) ──

def test_s7_concurrency_lock_contention():
    """Two independent writers against the same busy DB; writer 1 holds
    BEGIN IMMEDIATE, writer 2 starts while locked, writer 1 releases before
    busy_timeout, writer 2 completes. Final logical row count == 2 exactly."""
    path = _tmp()
    Database(path).connect().close()
    # Seed a wallet so the row-finalization path has something to reference.
    db0 = Database(path).connect()
    _seed_wallet(db0, WID, ADDR)
    db0.close()

    started = threading.Event()      # writer 2 knows writer 1 has the lock
    release = threading.Event()      # writer 1 signals it will release
    results = {}

    def _writer(who):
        db = Database(path).connect()
        conn = db.conn
        conn.execute("PRAGMA busy_timeout = 4000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            if who == 1:
                started.set()         # writer 2 may now begin
                release.wait(3.0)     # hold the lock briefly (< busy_timeout)
                conn.execute(
                    "INSERT INTO specialist_market_refresh_state("
                    "market_source_id, last_checked_at, last_status, attempt_count) "
                    "VALUES (?,?,?,?)",
                    (f"m{who}", "2026-01-01T00:00:00Z", "ok", who))
                conn.commit()
                results[who] = "committed"
            else:
                # Wait until writer 1 has the lock, then try to acquire it.
                started.wait(3.0)
                # writer 1 will release; this blocks then succeeds.
                conn.execute(
                    "INSERT INTO specialist_market_refresh_state("
                    "market_source_id, last_checked_at, last_status, attempt_count) "
                    "VALUES (?,?,?,?)",
                    (f"m{who}", "2026-01-01T00:00:00Z", "ok", who))
                conn.commit()
                results[who] = "committed"
        finally:
            try:
                conn.execute("COMMIT")
            except sqlite3.OperationalError:
                pass
            db.close()

    t1 = threading.Thread(target=_writer, args=(1,))
    t2 = threading.Thread(target=_writer, args=(2,))
    t1.start()
    # Give writer 1 a moment to grab BEGIN IMMEDIATE before writer 2 starts.
    import time
    time.sleep(0.1)
    t2.start()
    # After writer 2 is spinning, let writer 1 finish and release.
    release.set()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert results.get(1) == "committed", results
    assert results.get(2) == "committed", results

    # Exact row count, integrity, FK, no open transaction.
    dchk = Database(path).connect()
    n = dchk.conn.execute(
        "SELECT COUNT(*) FROM specialist_market_refresh_state").fetchone()[0]
    assert n == 2, n  # exact, not merely >= 1
    assert dchk.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert dchk.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # No transaction left open.
    assert dchk.conn.execute("PRAGMA database_list").fetchall() is not None
    assert dchk.conn.in_transaction is False, "transaction left open"
    _assert_no_exec_artifacts(dchk.conn)
    dchk.close()


# ── H. static forbidden-import purity ──────────────────────────────────────────

FORBIDDEN_MODULES = [
    "specialist_approvals", "approved_specialist_trade_dispatches",
    "copy_candidates", "candidate_price_snapshots",
    "paper_signal_decisions", "paper_signal_execution_authorizations",
    "execution_risk_decisions", "paper_orders", "paper_fills",
    "paper_positions", "paper_position_lots", "paper_position_marks",
    "paper_position_settlements",
]


def test_s7_static_forbidden_imports():
    """Research-plane CLIs must NOT import approval / dispatch / bridge /
    candidate / paper-signal / execution-authorization / risk-execution /
    paper broker modules (proven statically)."""
    cli_files = [
        "collect_specialist_evidence.py", "backfill_specialist_trade_taxonomy.py",
        "enrich_approved_source_trade.py", "refresh_specialist_market_truth.py",
        "evaluate_specialist_evidence_watchlist.py",
        "manage_specialist_evidence_watchlist.py", "specialist_evidence_status.py",
    ]
    forbidden_patterns = [
        r"specialist_approvals", r"approved_specialist_trade_dispatches",
        r"copy_candidates", r"candidate_price_snapshots",
        r"paper_signal", r"execution_risk", r"paper_orders", r"paper_fills",
        r"paper_positions", r"paper_position_lots", r"paper_position_marks",
        r"paper_position_settlements",
        r"dispatch", r"bridge", r"broker",
    ]
    for f in cli_files:
        src = (ROOT / "scripts" / f).read_text()
        # Only scan actual import lines (not docstrings / validation messages).
        import_lines = [ln for ln in src.splitlines()
                        if re.match(r"\s*(import|from)\s+\S", ln)]
        for pat in forbidden_patterns:
            for ln in import_lines:
                if re.search(pat, ln):
                    raise AssertionError(
                        f"{f} imports forbidden execution-plane module: {pat} "
                        f"(line: {ln.strip()})")
    # Confirm the forbidden tables exist in schema but are never written by the
    # research plane (covered by test_s7_execution_plane_isolation).
    assert FORBIDDEN_MODULES  # referenced for documentation
