"""T3 watchlist isolation + T4 collector tests (plan Task 5/6).

Temp/scratch DBs only. Never opens production.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path


def _tmp():
    return Path(tempfile.mktemp(suffix=".db"))

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion import specialist_evidence_watchlist as wl  # noqa: E402
from polycopy.ingestion.specialist_evidence_collector import (  # noqa: E402
    EvidenceCollectorConfig,
    collect_evidence,
)


def _open(path: Path) -> Database:
    if path.exists():
        path.unlink()
    return Database(path).connect()


def _seed_wallet(db: Database, wid: str, address: str = None, is_sample: int = 0):
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (wid, address or wid, "t", is_sample, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()


def _count(db: Database, table: str) -> int:
    return db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class FakeProvider:
    """Returns scripted raw /trades-shaped dicts for one wallet."""

    made_network_call = False

    def __init__(self, rows: list[dict]):
        self._rows = rows

    async def fetch_trades(self, wallet, *, limit, page):
        return self._rows[:limit]


class FakeMarket(Mapping):
    def __init__(self, data):
        self._d = data

    def __getitem__(self, k):
        return self._d[k]

    def __iter__(self):
        return iter(self._d)

    def __len__(self):
        return len(self._d)


def _gamma_resolver_factory(markets: dict):
    async def _resolve(condition_id: str):
        return markets.get(condition_id)
    return _resolve


# ── T3: watchlist isolation ────────────────────────────────────────────────
def test_watchlist_rejects_sample():
    db = _open(_tmp())
    _seed_wallet(db, "0xgood000000000000000000000000000000000001", is_sample=0)
    _seed_wallet(db, "0xsample0000000000000000000000000000000001", is_sample=1)
    try:
        wid = wl.add_watch(db, wallet_id="0xgood000000000000000000000000000000000001")
        assert wid
        try:
            wl.add_watch(db, wallet_id="0xsample0000000000000000000000000000000001")
            assert False, "sample wallet must be rejected"
        except ValueError as exc:
            assert "sample" in str(exc)
    finally:
        db.close()


def test_watchlist_one_active_per_wallet():
    db = _open(_tmp())
    _seed_wallet(db, "0xgood000000000000000000000000000000000002")
    try:
        w1 = wl.add_watch(db, wallet_id="0xgood000000000000000000000000000000000002")
        w2 = wl.add_watch(db, wallet_id="0xgood000000000000000000000000000000000002")
        assert w1 == w2  # idempotent; does not create a second active
        active = wl.list_watches(db, status="active")
        assert len(active) == 1
    finally:
        db.close()


def test_watchlist_pause_retire_not_active():
    db = _open(_tmp())
    _seed_wallet(db, "0xgood000000000000000000000000000000000003")
    try:
        wid = wl.add_watch(db, wallet_id="0xgood000000000000000000000000000000000003")
        assert wl.pause_watch(db, wid) is True
        assert wl.active_watch_for_wallet(db, "0xgood000000000000000000000000000000000003") is None
        assert wl.resume_watch(db, wid) is True
        assert wl.active_watch_for_wallet(db, "0xgood000000000000000000000000000000000003") == wid
        assert wl.retire_watch(db, wid) is True
        assert wl.active_watch_for_wallet(db, "0xgood000000000000000000000000000000000003") is None
    finally:
        db.close()


def test_watchlist_never_creates_approval():
    db = _open(_tmp())
    _seed_wallet(db, "0xgood000000000000000000000000000000000004")
    try:
        wl.add_watch(db, wallet_id="0xgood000000000000000000000000000000000004")
        assert _count(db, "specialist_approvals") == 0
        assert _count(db, "approved_specialist_trade_dispatches") == 0
    finally:
        db.close()


# ── T4: collector (BUY-only, idempotent, zero execution) ───────────────────
COND_A = "0x" + "a" * 64
COND_B = "0x" + "b" * 64
TOK_A = "0x" + "a" * 64
TOK_B = "0x" + "b" * 64
WALLET_ADDRESS = "0xgood000000000000000000000000000000000005"
WALLET_UUID = "uuid-0000000000000000000000000000000005"


def _buy_row(tid, cond, token, price="0.40", size="10"):
    return {
        "sourceProvidedTradeId": tid,
        "proxyWallet": WALLET_ADDRESS,
        "asset": token,
        "conditionId": cond,
        "side": "BUY",
        "outcome": "Yes",
        "price": price,
        "size": size,
        "timestamp": "2026-02-01T00:00:00Z",
    }


def _sell_row(tid, cond, token):
    r = _buy_row(tid, cond, token)
    r["side"] = "SELL"
    return r


def test_collector_buy_only_and_idempotent():
    db = _open(_tmp())
    _seed_wallet(db, WALLET_UUID, address=WALLET_ADDRESS)
    wid = wl.add_watch(db, wallet_id=WALLET_UUID)
    markets = {
        COND_A: FakeMarket({"conditionId": COND_A, "category": "Politics",
                            "tags": ["election"], "events": [{"id": "e1", "slug": "us"}]}),
        COND_B: FakeMarket({"conditionId": COND_B, "category": "Sports",
                            "tags": ["nba"], "events": [{"id": "e2", "slug": "nba"}]}),
    }
    provider = FakeProvider([
        _buy_row("t1", COND_A, TOK_A),
        _buy_row("t2", COND_B, TOK_B),
        _sell_row("t3", COND_A, TOK_A),
    ])
    gamma = _gamma_resolver_factory(markets)
    cfg = EvidenceCollectorConfig(max_new_trades_per_wallet=25, max_total_new_trades=25)

    async def run_both():
        res1 = await collect_evidence(
            db, watch_id=wid, provider=provider, gamma_resolver=gamma,
            config=cfg, dry_run=False)
        res2 = await collect_evidence(
            db, watch_id=wid, provider=provider, gamma_resolver=gamma,
            config=cfg, dry_run=False)
        return res1, res2

    try:
        res1, res2 = asyncio.run(run_both())
        assert res1.error is None, res1.error
        assert res1.inserted_rows == 2, res1.as_dict()
        assert res1.sell_excluded == 1
        # Both BUY rows carry canonical taxonomy.
        rows = db.conn.execute(
            "SELECT metadata_json FROM source_trades WHERE lower(trader_address)=? "
            "AND source_trade_id LIKE 'polymarket:%'",
            (WALLET_ADDRESS.lower(),)).fetchall()
        for r in rows:
            m = __import__("json").loads(dict(r)["metadata_json"])
            assert m["taxonomy"]["raw_category"], m
        # Replay -> no duplicates.
        assert res2.inserted_rows == 0, res2.as_dict()
        # Zero execution artifacts.
        for t in ("specialist_approvals", "approved_specialist_trade_dispatches",
                  "paper_signal_execution_authorizations", "paper_orders",
                  "paper_positions", "paper_signal_decisions", "copy_candidates"):
            assert _count(db, t) == 0, t
    finally:
        db.close()


def test_collector_paused_not_collected():
    db = _open(_tmp())
    _seed_wallet(db, "uuid-0000000000000000000000000000000006", address="0xgood000000000000000000000000000000000006")
    wid = wl.add_watch(db, wallet_id="uuid-0000000000000000000000000000000006")
    wl.pause_watch(db, wid)
    provider = FakeProvider([_buy_row("t1", COND_A, TOK_A)])
    try:
        async def run_one():
            return await collect_evidence(
                db, watch_id=wid, provider=provider, dry_run=False,
                config=EvidenceCollectorConfig())
        res = asyncio.run(run_one())
        assert res.error == "watch_not_active_or_missing"
        assert _count(db, "source_trades") == 0
    finally:
        db.close()
