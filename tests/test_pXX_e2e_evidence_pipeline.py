"""T11 end-to-end evidence-accumulation proof (plan Task 13).

Temp/scratch DB only. Never opens production.

Drives the COMPLETE research-plane pipeline on one temp DB:
  watchlist -> collector (BUY-only) -> backfill (taxonomy) ->
  refresh (resolution) -> rescore (frozen) -> status (monitor)

Asserts the milestone guarantees:
  * integration-critical path produces real, populated evidence;
  * the pipeline is IDEMPOTENT (a second full pass adds 0 rows);
  * ZERO execution artifacts ever appear (specialist_approvals,
    approved_specialist_trade_dispatches, paper_*, execution_risk_decisions,
    paper_signal_decisions) -- the research plane cannot authorize trading.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.specialist_evidence_watchlist import add_watch  # noqa: E402
from polycopy.ingestion.specialist_evidence_collector import (  # noqa: E402
    collect_evidence,
    EvidenceCollectorConfig,
)


def _load(n):
    s = importlib.util.spec_from_file_location(n, ROOT / "scripts" / n)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def _tmp():
    return Path(tempfile.mktemp(suffix=".db"))


COND_A = "0x" + "a" * 64
COND_B = "0x" + "b" * 64
TOK_A = "0x" + "a" * 64
TOK_B = "0x" + "b" * 64
GAMMA_A = {
    "conditionId": COND_A, "tokenId": TOK_A, "category": "Politics",
    "tags": ["election"], "events": [{"id": "e1", "slug": "us"}], "series": [],
    "question": "Q", "slug": "us", "outcomes": ["Yes", "No"],
    "outcomePrices": ["0.4", "0.6"],
}
GAMMA_B = {
    "conditionId": COND_B, "tokenId": TOK_B, "category": "Sports",
    "tags": ["nba"], "events": [{"id": "e2", "slug": "nba"}], "series": [],
    "question": "Q2", "slug": "nba", "outcomes": ["Yes", "No"],
    "outcomePrices": ["0.5", "0.5"],
}

WID = "uuid-e2e"
ADDR = "0xe2e00000000000000000000000000000abc"


class FakeProvider:
    def __init__(self):
        self.made_network_call = False

    async def fetch_trades(self, wallet, limit=100, page=1):
        self.made_network_call = True
        return [
            {
                "sourceProvidedTradeId": "poly:1",
                "proxyWallet": ADDR,
                "asset": TOK_A,
                "conditionId": COND_A,
                "side": "BUY",
                "outcome": "Yes",
                "price": "0.40",
                "size": "10",
                "timestamp": "2026-03-01T00:00:00Z",
            },
            {
                "sourceProvidedTradeId": "poly:2",
                "proxyWallet": ADDR,
                "asset": TOK_B,
                "conditionId": COND_B,
                "side": "BUY",
                "outcome": "No",
                "price": "0.55",
                "size": "5",
                "timestamp": "2026-03-02T00:00:00Z",
            },
            # SELL must be excluded by the collector.
            {
                "sourceProvidedTradeId": "poly:3",
                "proxyWallet": ADDR,
                "asset": TOK_A,
                "conditionId": COND_A,
                "side": "SELL",
                "outcome": "Yes",
                "price": "0.45",
                "size": "10",
                "timestamp": "2026-03-03T00:00:00Z",
            },
        ]


async def _resolve(cond):
    return GAMMA_A if cond == COND_A else (GAMMA_B if cond == COND_B else None)


EXEC_TABLES = [
    "specialist_approvals",
    "approved_specialist_trade_dispatches",
    "paper_signal_decisions",
    "paper_signal_execution_authorizations",
    "execution_risk_decisions",
    "paper_orders",
    "paper_fills",
    "paper_positions",
]


def _assert_no_exec_artifacts(db):
    for t in EXEC_TABLES:
        n = db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        assert n == 0, f"unexpected execution artifact in {t}: {n}"


def _seed(db):
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (WID, ADDR, "t", 0, "2026-01-01T00:00:00Z"))
    db.conn.commit()
    add_watch(db, wallet_id=WID, reason="e2e", source="manual")


def test_end_to_end_evidence_pipeline():
    db = Database(_tmp()).connect()
    ev = _load("evaluate_specialist_evidence_watchlist.py")
    st = _load("specialist_evidence_status.py")
    backfill = _load("backfill_specialist_trade_taxonomy.py")
    refresh = _load("refresh_specialist_market_truth.py")

    _seed(db)
    watch_id = db.conn.execute(
        "SELECT id FROM specialist_evidence_watchlist").fetchone()["id"]

    provider = FakeProvider()
    # 1) Collect (BUY-only, idempotent).
    res = asyncio.run(collect_evidence(
        db, watch_id=watch_id, provider=provider, gamma_resolver=_resolve,
        config=EvidenceCollectorConfig(max_gamma_requests=5), dry_run=False))
    assert res.error is None, res
    assert res.inserted_rows == 2, res  # two BUY, SELL excluded
    assert res.enriched == 2, res
    # Backfill taxonomy onto the collected trades (they carry a gamma block
    # via the resolver during collection, but backfill is the explicit step).
    backfill.main(
        ["--db-path", str(db.db_path), "--write", "--allow-live",
         "--confirm-production-db", "--limit", "50"])
    # 2) Refresh resolution: mark COND_A resolved (inject authoritative
    #    resolver so the research plane does not need network in the proof).
    refresh.main(
        ["--db-path", str(db.db_path), "--write", "--allow-live",
         "--confirm-production-db", "--market-source-id", COND_A],
        get_market=lambda cid: (
            {"resolutionStatus": "resolved", "winner": TOK_A}
            if cid == COND_A else None))
    # 3) Rescore (frozen; honest incomplete without enough resolved evidence).
    ev.main(
        ["--db-path", str(db.db_path), "--write", "--allow-live",
         "--confirm-production-db", "--wallet-id", WID])
    # 4) Status (read-only monitor).
    import io
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        st.main(["--db-path", str(db.db_path), "--json"])
    finally:
        sys.stdout = old
    status = json.loads(buf.getvalue())

    # Milestone guarantees.
    rows = db.conn.execute(
        "SELECT COUNT(*) FROM source_trades").fetchone()[0]
    assert rows == 2, rows
    # Taxonomy filled by backfill.
    tax = db.conn.execute(
        "SELECT COUNT(*) FROM source_trades WHERE json_extract(metadata_json,"
        "'$.taxonomy.raw_category') IS NOT NULL").fetchone()[0]
    assert tax == 2, tax
    # Resolution refresh wrote onto source_trades (canonical truth).
    resolved = db.conn.execute(
        "SELECT COUNT(*) FROM source_trades WHERE resolution_status='resolved'"
    ).fetchone()[0]
    assert resolved == 1, resolved
    # Rescore persisted honest decisions.
    assert db.conn.execute(
        "SELECT COUNT(*) FROM wallet_score_decisions WHERE wallet_id=?",
        (WID,)).fetchone()[0] == 1
    # Monitor reports (overall may be YELLOW/RED, never GREEN without approval).
    assert status["overall_state"] in ("YELLOW", "RED"), status
    _assert_no_exec_artifacts(db)

    # Idempotency: a second full pass adds 0 trades and 0 taxonomies.
    res2 = asyncio.run(collect_evidence(
        db, watch_id=watch_id, provider=provider, gamma_resolver=_resolve,
        config=EvidenceCollectorConfig(max_gamma_requests=5), dry_run=False))
    assert res2.inserted_rows == 0, res2
    backfill.main(
        ["--db-path", str(db.db_path), "--write", "--allow-live",
         "--confirm-production-db"])
    refresh.main(
        ["--db-path", str(db.db_path), "--write", "--allow-live",
         "--confirm-production-db", "--market-source-id", COND_A],
        get_market=lambda cid: (
            {"resolutionStatus": "resolved", "winner": TOK_A}
            if cid == COND_A else None))
    ev.main(
        ["--db-path", str(db.db_path), "--write", "--allow-live",
         "--confirm-production-db", "--wallet-id", WID])
    rows_after = db.conn.execute(
        "SELECT COUNT(*) FROM source_trades").fetchone()[0]
    assert rows_after == 2, rows_after
    _assert_no_exec_artifacts(db)
    db.close()


def test_pipeline_rejects_sample_wallet():
    db = Database(_tmp()).connect()
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (WID, ADDR, "t", 1, "2026-01-01T00:00:00Z"))
    db.conn.commit()
    import pytest
    with pytest.raises(ValueError):
        add_watch(db, wallet_id=WID, reason="sample", source="manual")
    _assert_no_exec_artifacts(db)
    db.close()
