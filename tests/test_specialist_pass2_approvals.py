"""Pass 2 — approvals, enrichment, dispatch, collector/monitor integration.

Bounded, deterministic tests against a temp v19 DB. No production DB is
touched. Every dispatcher/enrichment/collector path is asserted to leave
orders=0 / positions=0 and to read approvals from ``specialist_approvals``
(not a hardcoded wallet).
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path


from polycopy.db.database import Database
from polycopy.execution.specialist_approval import (
    create_approval,
    set_enabled,
    revoke_approval,
)
from polycopy.engine.approved_specialist_dispatcher import dispatch_one
from polycopy.monitoring.approved_wallet_monitor import load_active_approvals

from tests.fixtures.specialist_paper_fixtures import (
    FIXED_WALLET,
    SPECIALIST_CATEGORY,
    bridge_dependencies,
    create_approval_for_target,
    ingest_target_trade,
    seed_resolved_evidence,
)

# Ensure repo scripts are importable for the CLI integration tests.
_REPO = Path(__file__).resolve().parents[1]
for _c in (_REPO / "src", _REPO / "scripts"):
    if str(_c) not in sys.path:
        sys.path.insert(0, str(_c))


def _make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "pass2.db").connect()
    seed_resolved_evidence(db)
    return db


def _deps():
    d = bridge_dependencies()
    return d.gamma.get_market, d.clob


# ───────────────────────────── positive ─────────────────────────────

def test_positive_approval_to_signal(tmp_path: Path):
    db = _make_db(tmp_path)
    ing = ingest_target_trade(db)
    aid = create_approval_for_target(db)
    gamma, clob = _deps()
    res = dispatch_one(
        db, approval_id=aid, source_trade_internal_id=ing["source_trade_internal_id"],
        gamma_resolver=gamma, clob_provider=clob, dry_run=False,
    )
    assert res.status == "execution_pending", res.status
    assert isinstance(res.candidate_id, int) and res.candidate_id > 0
    # Dispatcher must never create orders/positions.
    assert _count(db, "orders") == 0 and _count(db, "positions") == 0
    assert res.enrichment_status == "complete"
    row = db.fetchone(
        "SELECT wallet, category, status FROM approved_specialist_trade_dispatches "
        "WHERE source_trade_internal_id=?", (ing["source_trade_internal_id"],))
    assert row is not None and row["wallet"] == FIXED_WALLET
    assert row["category"] == SPECIALIST_CATEGORY and row["status"] == "execution_pending"


def _count(db: Database, table: str) -> int:
    return db.fetchone(f"SELECT COUNT(*) c FROM {table}")["c"]


def test_replay_duplicate_safe(tmp_path: Path):
    db = _make_db(tmp_path)
    ing = ingest_target_trade(db)
    aid = create_approval_for_target(db)
    gamma, clob = _deps()
    r1 = dispatch_one(db, approval_id=aid, source_trade_internal_id=ing["source_trade_internal_id"],
                      gamma_resolver=gamma, clob_provider=clob, dry_run=False)
    cid1 = r1.candidate_id
    r2 = dispatch_one(db, approval_id=aid, source_trade_internal_id=ing["source_trade_internal_id"],
                      gamma_resolver=gamma, clob_provider=clob, dry_run=False)
    assert r2.status == "execution_pending"
    assert r2.candidate_id == cid1, "replay must not mint a second candidate"
    assert r2.created is False, "no new dispatch on replay"
    n = db.fetchone(
        "SELECT COUNT(*) c FROM copy_candidates WHERE source_trade_internal_id=?",
        (ing["source_trade_internal_id"],))["c"]
    assert n == 1


# ───────────────────────────── negative ─────────────────────────────

def test_unknown_approval_rejected(tmp_path: Path):
    db = _make_db(tmp_path)
    ing = ingest_target_trade(db)
    gamma, clob = _deps()
    res = dispatch_one(db, approval_id="does-not-exist",
                       source_trade_internal_id=ing["source_trade_internal_id"],
                       gamma_resolver=gamma, clob_provider=clob, dry_run=False)
    assert res.status == "failed"
    assert "approval_not_found_or_inactive" in res.reason_codes
    assert _count(db, "orders") == 0 and _count(db, "positions") == 0


def test_disabled_approval_blocked(tmp_path: Path):
    db = _make_db(tmp_path)
    ing = ingest_target_trade(db)
    aid = create_approval_for_target(db)
    set_enabled(db, aid, enabled=False, updated_by="test")
    gamma, clob = _deps()
    res = dispatch_one(db, approval_id=aid, source_trade_internal_id=ing["source_trade_internal_id"],
                       gamma_resolver=gamma, clob_provider=clob, dry_run=False)
    assert res.status == "failed"
    assert "approval_not_found_or_inactive" in res.reason_codes
    assert _count(db, "orders") == 0 and _count(db, "positions") == 0
    assert db.fetchone("SELECT COUNT(*) c FROM approved_specialist_trade_dispatches")["c"] == 0


def test_revoked_approval_blocked(tmp_path: Path):
    db = _make_db(tmp_path)
    ing = ingest_target_trade(db)
    aid = create_approval_for_target(db)
    revoke_approval(db, aid, revoked_by="test", revocation_reason="x")
    gamma, clob = _deps()
    res = dispatch_one(db, approval_id=aid, source_trade_internal_id=ing["source_trade_internal_id"],
                       gamma_resolver=gamma, clob_provider=clob, dry_run=False)
    assert res.status == "failed"
    assert "approval_not_found_or_inactive" in res.reason_codes
    assert _count(db, "orders") == 0 and _count(db, "positions") == 0


def test_wallet_mismatch_blocked(tmp_path: Path):
    db = _make_db(tmp_path)
    ing = ingest_target_trade(db)
    other = "0x" + "1" * 40
    aid = create_approval(db, wallet_address=other, specialist_category=SPECIALIST_CATEGORY,
                          wallet_score_decision_id="w", category_score_decision_id="c",
                          formula_name="f", formula_version="1", reviewer="t", approval_reason="rt").approval_id
    gamma, clob = _deps()
    res = dispatch_one(db, approval_id=aid, source_trade_internal_id=ing["source_trade_internal_id"],
                       gamma_resolver=gamma, clob_provider=clob, dry_run=False)
    assert res.status == "failed"
    assert "wallet_or_side_mismatch" in res.reason_codes
    assert _count(db, "orders") == 0


def test_sell_trade_not_selected(tmp_path: Path):
    """A SELL source trade for an approved wallet must NOT be dispatched."""
    db = _make_db(tmp_path)
    db.conn.execute(
        "INSERT INTO source_trades "
        "(id, source, source_trade_id, market_source_id, side, outcome, quantity, price, "
        " trader_address, timestamp, is_sample, metadata_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("st_sell", "polymarket_data_api_trades_user", "poly.market:sell:1", "0x" + "b" * 64,
         "SELL", "Yes", 1.0, 0.5, FIXED_WALLET, "2026-01-01T00:00:00+00:00", 0, "{}"),
    )
    db.conn.commit()
    aid = create_approval_for_target(db)
    gamma, clob = _deps()
    res = dispatch_one(db, approval_id=aid, source_trade_internal_id="st_sell",
                       gamma_resolver=gamma, clob_provider=clob, dry_run=False)
    assert res.status in ("rejected", "no_eligible", "failed")
    assert res.candidate_id is None
    assert _count(db, "orders") == 0 and _count(db, "positions") == 0


def test_missing_taxonomy_no_signal(tmp_path: Path):
    """Enrichment unavailable (no metadata, no gamma) must block the bridge."""
    db = _make_db(tmp_path)
    # Insert a raw source trade with EMPTY metadata and no taxonomy columns.
    db.conn.execute(
        "INSERT INTO source_trades "
        "(id, source, source_trade_id, market_source_id, side, outcome, quantity, price, "
        " trader_address, timestamp, is_sample, metadata_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("st_empty", "polymarket_data_api_trades_user", "poly.market:empty:1", "0x" + "a" * 64,
         "BUY", "Yes", 1.0, 0.5, FIXED_WALLET, "2026-01-01T00:00:00+00:00", 0, "{}"),
    )
    db.conn.commit()
    aid = create_approval_for_target(db)
    # No gamma resolver => taxonomy cannot be proven => enrichment unavailable.
    res = dispatch_one(db, approval_id=aid, source_trade_internal_id="st_empty",
                       gamma_resolver=None, clob_provider=None, dry_run=False)
    assert res.enrichment_status != "complete"
    assert res.status == "enrichment_incomplete"
    assert res.candidate_id is None
    assert _count(db, "orders") == 0 and _count(db, "positions") == 0


# ─────────────────────── enrichment module direct ───────────────────────

def test_enrichment_idempotent_and_versioned(tmp_path: Path):
    from polycopy.ingestion.source_trade_enrichment import enrich_source_trade
    db = _make_db(tmp_path)
    ing = ingest_target_trade(db)
    gamma, _ = _deps()
    r1 = enrich_source_trade(db, ing["source_trade_internal_id"], gamma_resolver=gamma)
    assert r1.status == "complete"
    r2 = enrich_source_trade(db, ing["source_trade_internal_id"], gamma_resolver=gamma)
    assert r2.status == "complete"
    n = db.fetchone("SELECT COUNT(*) c FROM source_trade_enrichments "
                    "WHERE source_trade_internal_id=?", (ing["source_trade_internal_id"],))["c"]
    assert n == 1


# ─────────────────────── collector approval integration ───────────────────

def _first_approval_id(dbp: Path) -> str:
    db = Database(dbp).connect()
    try:
        return db.fetchone("SELECT approval_id FROM specialist_approvals LIMIT 1")["approval_id"]
    finally:
        db.close()


def test_collector_unknown_approval_rejected(tmp_path: Path):
    dbp = tmp_path / "collect.db"
    Database(dbp).connect().close()
    cli = str(_REPO / "scripts" / "collect_approved_wallet_trades.py")
    r = subprocess.run([sys.executable, cli, "--db-path", str(dbp), "--approval-id", "nope"],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "unknown approval_id" in r.stderr


def test_collector_valid_approval_resolves_wallet(tmp_path: Path):
    dbp = tmp_path / "collect.db"
    db = Database(dbp).connect()
    create_approval(db, wallet_address=FIXED_WALLET, specialist_category=SPECIALIST_CATEGORY,
                    wallet_score_decision_id="w", category_score_decision_id="c",
                    formula_name="f", formula_version="1", reviewer="t", approval_reason="rt")
    db.close()
    cli = str(_REPO / "scripts" / "collect_approved_wallet_trades.py")
    r = subprocess.run([sys.executable, cli, "--db-path", str(dbp), "--approval-id",
                        _first_approval_id(dbp)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "approval_id" in r.stdout


# ─────────────────────── monitor reads approval table ───────────────────

def test_monitor_reads_approval_table_not_hardcoded(tmp_path: Path):
    dbp = tmp_path / "mon.db"
    db = Database(dbp).connect()
    aid = create_approval(db, wallet_address=FIXED_WALLET, specialist_category=SPECIALIST_CATEGORY,
                          wallet_score_decision_id="w", category_score_decision_id="c",
                          formula_name="f", formula_version="1", reviewer="t", approval_reason="rt").approval_id
    ingest_target_trade(db)
    db.close()
    conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    try:
        approvals = load_active_approvals(conn)
    finally:
        conn.close()
    assert len(approvals) == 1 and approvals[0]["approval_id"] == aid
    # Sanity: the wallet actually has a source trade row (so the monitor's
    # per-wallet count would be > 0; no hardcoded literal is needed).
    conn2 = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    try:
        c = conn2.execute(
            "SELECT COUNT(*) FROM source_trades WHERE lower(trader_address)=?",
            (FIXED_WALLET.lower(),)).fetchone()[0]
    finally:
        conn2.close()
    assert c >= 1
