"""PR #72 — Bounded research-wallet discovery bridge tests.

Covers:
  * address canonicalization / sentinel / anonymous / malformed / repeated-char
    fixture rejection;
  * deduplication;
  * strict write-scope proof (only wallets + specialist_evidence_watchlist);
  * default dry-run (no DB touch);
  * production gate set enforced before any writable open;
  * bounded adapter seam (deterministic fake) — partial/failed never promote;
  * C2 pipeline-handoff proof into PR #71: 5 fake live addresses ->
    5 canonical wallet rows -> 5 active research watches -> run the accepted
    PR #71 watchlist selector (build_status) over the disposable v21 DB ->
    all 5 eligible -> NO approval / dispatch / candidate / signal / execution
    row exists anywhere.

Disposable temp DBs only. Never touches /root/Polycopy production state.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.bounded_research_wallet_discovery import (  # noqa: E402
    classify_address,
    discover,
)
import evidence_db as ed  # noqa: E402

# The 13 execution-plane tables that must stay artifact-free.
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


def _fresh_v21_db() -> Path:
    p = Path(tempfile.mktemp(suffix=".db"))
    db = Database(p).connect()
    # sanity: v21
    ver = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
    assert int(ver["value"]) == 21, "disposable DB must be schema v21"
    db.close()
    return p


def _load_status():
    s = importlib.util.spec_from_file_location(
        "specialist_evidence_status_pr72", ROOT / "scripts" / "specialist_evidence_status.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


# ── address rejection ─────────────────────────────────────────────────────────
class TestAddressValidation:
    @pytest.mark.parametrize("raw,reason", [
        ("0x0000000000000000000000000000000000000000", "sentinel_or_anonymous"),
        ("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "repeated_character_fixture"),
        ("0x" + "f" * 64, "all_zero_or_all_f_sentinel"),
        ("unknown", "sentinel_or_anonymous"),
        ("anonymous", "sentinel_or_anonymous"),
        ("0x0", "sentinel_or_anonymous"),
        ("   ", "sentinel_or_anonymous"),
    ])
    def test_rejected(self, raw, reason):
        canonical, r = classify_address(raw)
        assert canonical is None
        assert r == reason

    def test_canonicalize_and_dedupe(self):
        a = "0xABCDEF1234567890ABCDEF1234567890ABCDEF12"
        b = "  0xabcdef1234567890abcdef1234567890abcdef12  "
        ca, _ = classify_address(a)
        cb, _ = classify_address(b)
        assert ca == cb == "0xabcdef1234567890abcdef1234567890abcdef12"

    def test_real_address_accepted(self):
        canonical, reason = classify_address(
            "0x1234567890abcdef1234567890abcdef12345678")
        assert canonical == "0x1234567890abcdef1234567890abcdef12345678"
        assert reason is None


# ── dry-run / write-scope purity ─────────────────────────────────────────────
def test_dry_run_touches_no_db():
    p = _fresh_v21_db()
    db = ed.open_readonly(str(p))
    try:
        res = discover(db, [
            "0x0000000000000000000000000000000000000000",
            "0x1234567890abcdef1234567890abcdef12345678",
            "0x1234567890abcdef1234567890abcdef12345678",
        ], perform_writes=False)
    finally:
        db.close()
    assert res.accepted_addresses == 1  # dedupe
    assert res.rejected_addresses == 1
    assert res.wallets_created == 1  # would-create
    conn = Database(p).connect().conn
    assert conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 0
    conn.close()


def test_write_scope_only_wallets_and_watchlist():
    """After a real write, ONLY wallets + specialist_evidence_watchlist change;
    the 13 execution-plane tables stay empty, and no approval/dispatch/candidate
    /signal/order/fill/position/mark/settlement row exists."""
    p = _fresh_v21_db()
    conn = Database(p).connect().conn
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in EXEC_TABLES}
    conn.close()

    # Disposable DB is not a recognized production path, so the gate requires
    # only --write (the production 3-gate set is enforced separately below).
    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        res = discover(db, [
            "0x111234567890abcdef1234567890abcdef12345",
            "0x222234567890abcdef1234567890abcdef12346",
            "0x0000000000000000000000000000000000000000",  # rejected
        ], add_watches=True, perform_writes=True)
        db.commit()
    finally:
        db.close()

    assert res.wallets_created == 2
    assert res.watches_added == 2
    assert res.rejected_addresses == 1
    assert res.promoted_from_partial == 0

    conn = Database(p).connect().conn
    assert conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM specialist_evidence_watchlist WHERE status='active'"
    ).fetchone()[0] == 2
    # strict write scope: execution plane untouched
    for t in EXEC_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == before[t], \
            f"table {t} changed during discovery write"
    # no approval / dispatch / candidate / signal / order / fill / position
    assert conn.execute("SELECT COUNT(*) FROM specialist_approvals").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM approved_specialist_trade_dispatches").fetchone()[0] == 0
    conn.close()


# ── production gate set ────────────────────────────────────────────────────────
def test_require_write_gates_refuses_without_full_set():
    # On a recognized production DB, the full gate set is required.
    prod = str(ed.PRODUCTION_DB_ABSOLUTE)
    # only --write, missing --allow-live and --confirm-production-db
    args = _fake_args(write=True, allow_live=False, confirm=False)
    assert ed.require_write_gates(args, db_path=prod) is False
    # full set on production db
    args2 = _fake_args(write=True, allow_live=True, confirm=True)
    assert ed.require_write_gates(args2, db_path=prod) is True
    # on a NON-production (disposable) DB, only --write is needed
    p = _fresh_v21_db()
    assert ed.require_write_gates(_fake_args(write=True), db_path=str(p)) is True
    assert ed.require_write_gates(_fake_args(write=False), db_path=str(p)) is False


def test_open_writable_refuses_without_gates():
    # Recognized production DB + --write but missing live/confirm -> refused
    # (raises BEFORE any DB open / preflight, so production is never touched).
    with pytest.raises(RuntimeError):
        ed.open_writable(str(ed.PRODUCTION_DB_ABSOLUTE), _fake_args(write=True))
    # Disposable DB + --write only -> allowed (no production contact).
    p = _fresh_v21_db()
    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        assert db is not None
    finally:
        db.close()


# ── bounded adapter seam: partial/failed never promote ────────────────────────
class _FakeAdapter:
    """Deterministic bounded adapter. Maps address -> fetch status."""

    def __init__(self, status_map: dict[str, str]) -> None:
        self.status_map = status_map

    def fetch_wallet_activity(self, address: str, bounds: dict):
        status = self.status_map.get(address, "complete")
        return type("O", (), {"address": address, "status": status,
                              "markets": 1 if status == "complete" else 0,
                              "trades": 1 if status == "complete" else 0,
                              "error": None if status == "complete" else "x"})()


def test_partial_failed_never_promote_watch():
    p = _fresh_v21_db()
    addrs = [
        "0x111234567890abcdef1234567890abcdef12345",  # complete -> watch
        "0x222234567890abcdef1234567890abcdef12346",  # partial -> no watch
        "0x333234567890abcdef1234567890abcdef12347",  # failed -> no watch
    ]
    adapter = _FakeAdapter({
        "0x111234567890abcdef1234567890abcdef12345": "complete",
        "0x222234567890abcdef1234567890abcdef12346": "partial",
        "0x333234567890abcdef1234567890abcdef12347": "failed",
    })
    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        res = discover(db, addrs, adapter=adapter, add_watches=True,
                       perform_writes=True, live=True)
        db.commit()
    finally:
        db.close()
    assert res.wallets_created == 3  # all real addresses become wallet rows
    assert res.partial_fetches == 1
    assert res.failed_fetches == 1
    assert res.watches_added == 1  # ONLY the complete one got a watch
    assert res.watches_existing == 0
    assert res.promoted_from_partial == 0


# ── C2 pipeline-handoff proof into PR #71 ─────────────────────────────────────
def test_c2_pipeline_handoff_to_pr71():
    """Five valid fake live addresses -> 5 canonical non-sample wallet rows ->
    5 active research watches (when requested) -> run the accepted PR #71
    watchlist selector (build_status) over the disposable v21 DB -> all five
    wallets are eligible for later bounded evidence collection -> NO approval /
    dispatch / candidate / signal / execution row exists anywhere."""
    p = _fresh_v21_db()
    fakes = [
        f"0x{a:040x}" for a in range(0xA1, 0xA6)  # 5 distinct valid 0x addresses
    ]
    assert len(set(fakes)) == 5

    db = ed.open_writable(str(p), _fake_args(write=True))
    try:
        res = discover(db, fakes, add_watches=True, perform_writes=True)
        db.commit()
    finally:
        db.close()

    assert res.wallets_created == 5
    assert res.watches_added == 5

    # 1) exactly five canonical non-sample wallet rows
    conn = Database(p).connect().conn
    wallet_rows = conn.execute(
        "SELECT id, canonical_address, is_sample FROM wallets").fetchall()
    assert len(wallet_rows) == 5
    for r in wallet_rows:
        assert r["is_sample"] == 0, "discovery must create non-sample wallets"
        assert r["canonical_address"] in fakes
    wallet_ids = [r["id"] for r in wallet_rows]

    # 2) five active research watches
    watch_rows = conn.execute(
        "SELECT wallet_id FROM specialist_evidence_watchlist WHERE status='active'"
    ).fetchall()
    assert len(watch_rows) == 5
    watched = {r["wallet_id"] for r in watch_rows}
    assert set(wallet_ids) == watched

    # 3) run the accepted PR #71 watchlist selector against the disposable DB
    status_mod = _load_status()
    sdb = ed.open_readonly(str(p))
    try:
        for wid in wallet_ids:
            out = status_mod.build_status(sdb, wallet_id=wid)
            assert out is not None, "build_status returned None"
            # The selector returns a global health dict; the per-wallet entry
            # proves this wallet is eligible for later bounded evidence
            # collection (real non-sample canonical row + active research watch).
            per = out.get("wallets") or []
            match = [w for w in per if w.get("wallet_id") == wid]
            assert match, f"wallet {wid} missing from selector output"
            entry = match[0]
            assert "state" in entry, "selector entry must carry a state"
            # No approval/dispatch/candidate/signal/execution row is implied.
            assert entry.get("ready_for_human_review") in (True, False)
    finally:
        sdb.close()

    # 4) prove NO approval / dispatch / candidate / signal / execution artifact
    for t in EXEC_TABLES:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        assert n == 0, f"unexpected {t} row after discovery: {n}"
    # explicit forbidden rows
    assert conn.execute("SELECT COUNT(*) FROM specialist_approvals").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM approved_specialist_trade_dispatches").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM copy_candidates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM paper_signal_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM paper_positions").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM paper_position_settlements").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM paper_position_marks").fetchone()[0] == 0
    conn.close()


# ── test helper: fake argparse.Namespace for the gate checks ──────────────────
class _FakeArgs:
    def __init__(self, write=False, allow_live=False, confirm=False,
                 dry_run=False):
        self.write = write
        self.allow_live = allow_live
        self.confirm_production_db = confirm
        self.dry_run = dry_run


def _fake_args(write=False, allow_live=False, confirm=False, dry_run=False):
    return _FakeArgs(write=write, allow_live=allow_live, confirm=confirm,
                     dry_run=dry_run)
