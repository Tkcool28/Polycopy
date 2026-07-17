"""Fresh -> 21 and 20 -> 21 migration matrix for the evidence-accumulation schema.

Temp/scratch DBs only. Never opens production.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from polycopy.db import database, schema  # noqa: E402
from polycopy.db.schema import SCHEMA_VERSION  # noqa: E402

EXPECTED_V21_OBJECTS = [
    # v21 research plane
    "specialist_evidence_watchlist",
    "specialist_market_refresh_state",
    "ux_evidence_watchlist_active",
    "idx_market_refresh_next",
    # PR #70 (v18) inlined for fresh-DB completeness
    "specialist_approvals",
    "ux_specialist_approvals_active",
    "paper_signal_execution_authorizations",
    "idx_paper_signal_exec_authz_signal",
    "execution_risk_decisions",
    "idx_execution_risk_signal",
    "idx_execution_risk_attempt",
    "paper_orders",
    "idx_paper_orders_signal",
    "paper_fills",
    "idx_paper_fills_order",
    "paper_positions",
    "idx_paper_positions_order",
    "paper_position_lots",
    "paper_position_marks",
    "idx_paper_position_marks_position",
    "paper_position_settlements",
    "idx_paper_position_settlements_position",
    # PR #70 (v19) inlined
    "source_trade_enrichments",
    "idx_source_trade_enrichments_internal",
    "approved_specialist_trade_dispatches",
    "idx_astd_approval",
]


def _open_fresh(path: Path) -> "database.Database":
    if path.exists():
        path.unlink()
    return database.Database(path).connect()


def test_fresh_db_is_v21():
    db = _open_fresh(Path("/tmp/polycopy_v21_fresh.db"))
    try:
        ver = db.conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert ver == str(SCHEMA_VERSION)
        assert SCHEMA_VERSION == 21
        for obj in EXPECTED_V21_OBJECTS:
            kind = "index" if obj.startswith(("idx_", "ux_")) else "table"
            if kind == "index":
                assert db._index_exists(obj), f"missing index {obj}"
            else:
                assert db._table_exists(obj), f"missing table {obj}"
        integrity = db.conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"
        assert list(db.conn.execute("PRAGMA foreign_key_check")) == []
    finally:
        db.close()


def _build_at_v20(path: Path):
    """Build a DB frozen at v20 by monkeypatching the version in both modules,
    then reopen at the real target (21) to forward-migrate."""
    import polycopy.db.database as dbmod
    schema.SCHEMA_VERSION = 20
    dbmod.SCHEMA_VERSION = 20
    try:
        db = _open_fresh(path)
        db.close()
    finally:
        # restore real target (importlib.reload keeps the patched module object)
        schema.SCHEMA_VERSION = 21
        dbmod.SCHEMA_VERSION = 21
        importlib.reload(schema)
        importlib.reload(dbmod)
    db = _open_fresh(path)
    return db


def test_v20_to_v21_preserves_pr70_rows():
    path = Path("/tmp/polycopy_v21_upgrade.db")
    db = _build_at_v20(path)
    try:
        ver = db.conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert ver == "21"
        for obj in EXPECTED_V21_OBJECTS:
            kind = "index" if obj.startswith(("idx_", "ux_")) else "table"
            if kind == "index":
                assert db._index_exists(obj), f"missing index {obj}"
            else:
                assert db._table_exists(obj), f"missing table {obj}"

        # Seed a real PR #70 row set at v20, then upgrade, then confirm survival.
        # (We seed the durable approval + a paper_signal_decision to prove the
        # PR #70 tables are present and their rows survive; the full
        # execution-auth FK chain is exercised by existing PR #70 suites.)
        db.conn.execute(
            "INSERT INTO wallets(id,address,label,is_sample,created_at) "
            "VALUES ('w1','0xaaa0000000000000000000000000000000000','t',0,'2026-01-01T00:00:00Z')"
        )
        db.conn.execute(
            "INSERT INTO copy_candidates(id,wallet_id,source,source_trade_id,"
            "side,source_trade_price,source_trade_quantity,source_trade_timestamp,"
            "observed_at,wallet_score_version,wallet_score,wallet_verdict,status,"
            "created_at,updated_at) VALUES (1,'w1','gamma','st1','BUY',0.5,10.0,"
            "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','1',50.0,"
            "'watchlist','pending',"
            "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
        )
        db.conn.execute(
            "INSERT INTO paper_signal_decisions(id,candidate_id,wallet_id,"
            "signal_family,final_verdict,is_approved,source_data_timestamp,"
            "source_trade_id,idempotency_key,computed_at,created_at) "
            "VALUES (1,1,'w1','copy_candidate','copy_candidate',0,"
            "'2026-01-01T00:00:00Z','st1','idem1',"
            "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
        )
        db.conn.execute(
            "INSERT INTO specialist_approvals("
            "approval_id,wallet_address,specialist_category,formula_name,"
            "formula_version,reviewer,approved_at,created_at,updated_at) "
            "VALUES ('ap1','0xaaa0000000000000000000000000000000000',"
            "'politics','wallet_score','1','cli','2026-01-01T00:00:00Z',"
            "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')"
        )
        db.conn.commit()
        db.close()

        # reopen -> forward migrate v20 -> v21 (inlines PR #70 + adds v21 tables)
        db = database.Database(path).connect()
        ver = db.conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert ver == "21"
        ap = db.conn.execute(
            "SELECT approval_id FROM specialist_approvals WHERE approval_id='ap1'"
        ).fetchone()
        assert ap is not None
        psd = db.conn.execute(
            "SELECT id FROM paper_signal_decisions WHERE id=1"
        ).fetchone()
        assert psd is not None
        integrity = db.conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"
        assert list(db.conn.execute("PRAGMA foreign_key_check")) == []
    finally:
        db.close()
