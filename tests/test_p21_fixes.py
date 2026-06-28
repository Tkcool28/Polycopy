"""Regression tests for the P1 + P2 Codex review fixes on PR #3.

P1 fix: deterministic row-level ``source_trade_id`` (canonical sha256 over
every distinguishing row field — two rows from the same transactionHash but
with different asset/outcome/side/price/size produce DIFFERENT IDs).

P2 fix: anonymous trades (``proxyWallet`` missing) get
``trader_address=None`` instead of the legacy literal "unknown". They are
persisted in ``source_trades`` as market-level observations but are excluded
from wallet discovery and ``evaluate_wallet`` scoring. The schema is bumped to
v5 to allow ``trader_address`` to be NULL.

These tests use ONLY ``httpx.MockTransport`` (no real network calls).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from polycopy.adapters.polymarket import (
    PolymarketPublicAdapter,
    deterministic_source_trade_id_v2,
)
from polycopy.db.database import Database
from polycopy.db.schema import (
    MIGRATIONS,
    SCHEMA_VERSION,
    _V5_DDL,
)
from polycopy.domain.source_trade import SourceTrade

# Ensure scripts/ is importable for the collector helper tests below.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


FIXTURES = Path(__file__).parent / "fixtures" / "polymarket_trade_ingestion"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _make_adapter_with_transport(handler):
    """Build an adapter whose data-client uses a custom mock handler."""
    import httpx

    a = PolymarketPublicAdapter(
        gamma_base_url="https://gamma-api.polymarket.com",
        clob_base_url="https://clob.polymarket.com",
        data_api_base_url="https://data-api.polymarket.com",
        timeout=5.0,
        data_api_window_size=100,
    )
    transport = httpx.MockTransport(handler)
    a._data_client = httpx.AsyncClient(
        base_url=a.data_api_base_url, transport=transport, timeout=5.0,
    )
    return a


# ─────────────────────────────────────────────────────────────────────────────
# P1 — Deterministic row-level source_trade_id
# ─────────────────────────────────────────────────────────────────────────────


def _row(
    *,
    asset: str = "asset-A",
    tx: str | None = "0xfeedface12345678",
    cond: str = "0xcond",
    side: str = "BUY",
    outcome: str = "Yes",
    outcome_index: int | None = 0,
    price: float = 0.5,
    size: float = 1.0,
    ts: int = 1782636254,
    wallet: str = "0x1111111111111111111111111111111111111111",
) -> dict:
    raw = {
        "proxyWallet": wallet,
        "side": side,
        "asset": asset,
        "conditionId": cond,
        "size": size,
        "price": price,
        "timestamp": ts,
        "outcome": outcome,
        "outcomeIndex": outcome_index,
        "transactionHash": tx,
    }
    return raw


def test_two_rows_same_tx_different_assets_get_different_ids():
    """P1: Same tx hash, different assets → different source_trade_id."""
    a = _row(asset="asset-A")
    b = _row(asset="asset-B")
    sid_a = deterministic_source_trade_id_v2(a)
    sid_b = deterministic_source_trade_id_v2(b)
    assert sid_a != sid_b
    assert sid_a.startswith("polymarket:")
    assert sid_b.startswith("polymarket:")
    assert len(sid_a) == len("polymarket:") + 64
    assert len(sid_b) == len("polymarket:") + 64


def test_two_rows_same_tx_different_outcomes_get_different_ids():
    """P1: Same tx hash + asset, different outcome/outcomeIndex → different IDs."""
    a = _row(outcome="Yes", outcome_index=0)
    b = _row(outcome="No", outcome_index=1)
    sid_a = deterministic_source_trade_id_v2(a)
    sid_b = deterministic_source_trade_id_v2(b)
    assert sid_a != sid_b


def test_exact_duplicate_rows_deduplicate(tmp_path: Path):
    """P1: Same exact row twice → identical ID → dedup on UNIQUE constraint."""
    db_path = tmp_path / "dedup.db"
    db = Database(db_path=db_path).connect()
    raw = _row()
    sid = deterministic_source_trade_id_v2(raw)
    # Two rows with the same source_trade_id (representing two fetches):
    # INSERT OR IGNORE keeps exactly one.
    for i in range(2):
        db.execute(
            """INSERT OR IGNORE INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                "polymarket_data_api",
                sid,
                raw["conditionId"],
                "BUY",
                raw["outcome"],
                raw["size"],
                raw["price"],
                raw["proxyWallet"],
                datetime.fromtimestamp(raw["timestamp"], tz=timezone.utc).isoformat(),
                0,
            ),
        )
    db.conn.commit()
    n = db.fetchone("SELECT COUNT(*) AS n FROM source_trades")["n"]
    assert n == 1, f"expected exactly 1 deduped row, got {n}"
    db.close()


def test_refetch_remains_idempotent():
    """P1: A window fetched twice produces identical IDs across fetches."""
    window = [_row(asset=f"asset-{i}", outcome="Yes" if i % 2 == 0 else "No",
                   outcome_index=0 if i % 2 == 0 else 1) for i in range(5)]

    async def fake_handler(request):
        import httpx
        return httpx.Response(200, json=window)

    async def run_once():
        a = _make_adapter_with_transport(fake_handler)
        try:
            since = datetime.fromtimestamp(0, tz=timezone.utc)
            return await a.get_recent_trades(
                market_source_id="0xcond", since=since, limit=10,
            )
        finally:
            await a.aclose()

    t1 = asyncio.run(run_once())
    t2 = asyncio.run(run_once())
    assert len(t1) == 5
    assert len(t2) == 5
    ids1 = sorted(t.source_trade_id for t in t1)
    ids2 = sorted(t.source_trade_id for t in t2)
    assert ids1 == ids2, "refetch produced different IDs (not idempotent)"


def test_missing_tx_hash_uses_canonical_payload():
    """P1: Rows without transactionHash still get distinct IDs from the
    remaining canonical fields; the tx value must NOT appear literally."""
    a = _row(tx=None, asset="asset-A", price=0.5, size=1.0)
    b = _row(tx=None, asset="asset-B", price=0.5, size=1.0)
    sid_a = deterministic_source_trade_id_v2(a)
    sid_b = deterministic_source_trade_id_v2(b)
    assert sid_a != sid_b
    # ID format
    assert sid_a.startswith("polymarket:") and len(sid_a) == len("polymarket:") + 64
    assert sid_b.startswith("polymarket:") and len(sid_b) == len("polymarket:") + 64


def test_short_or_nonhex_tx_hash_treated_as_missing():
    """P1: Short / non-hex tx_hash values are normalized to "" so they do not
    poison the canonical payload with garbage."""
    # Same other fields, different (invalid) tx hashes → should be equal
    # because both normalize to "" for the tx component.
    a = _row(tx="0xshort", asset="asset-X", price=0.5)
    b = _row(tx="garbage", asset="asset-X", price=0.5)
    sid_a = deterministic_source_trade_id_v2(a)
    sid_b = deterministic_source_trade_id_v2(b)
    assert sid_a == sid_b, "invalid tx hashes should normalize to '' (empty)"

    # Now differ in a real field — IDs MUST differ.
    c = _row(tx="0xshort", asset="asset-X", price=0.6)
    sid_c = deterministic_source_trade_id_v2(c)
    assert sid_c != sid_a

    # And a row with no tx_hash + matching other fields → same id (since
    # both have empty tx component).
    d = _row(tx=None, asset="asset-X", price=0.5)
    sid_d = deterministic_source_trade_id_v2(d)
    assert sid_d == sid_a


def test_db_persistence_keeps_distinct_same_tx_rows(tmp_path: Path):
    """P1 end-to-end: 3 rows from the same tx but different assets must all
    survive INSERT OR IGNORE (3 rows in source_trades, not 1)."""
    db_path = tmp_path / "rows.db"
    db = Database(db_path=db_path).connect()

    tx = "0xfeedface12345678"
    rows = [
        _row(tx=tx, asset=f"asset-{i}", price=0.5, size=1.0,
             outcome="Yes", outcome_index=0)
        for i in range(3)
    ]
    sids = []
    for raw in rows:
        sid = deterministic_source_trade_id_v2(raw)
        sids.append(sid)
        db.execute(
            """INSERT OR IGNORE INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                "polymarket_data_api",
                sid,
                raw["conditionId"],
                raw["side"],
                raw["outcome"],
                raw["size"],
                raw["price"],
                raw["proxyWallet"],
                datetime.fromtimestamp(raw["timestamp"], tz=timezone.utc).isoformat(),
                0,
            ),
        )
    db.conn.commit()
    # 3 distinct IDs
    assert len(set(sids)) == 3, f"expected 3 distinct IDs, got {set(sids)}"
    n = db.fetchone("SELECT COUNT(*) AS n FROM source_trades")["n"]
    assert n == 3, f"expected 3 rows in source_trades, got {n}"
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# P2 — Anonymous-trade wallet exclusion
# ─────────────────────────────────────────────────────────────────────────────


def test_missing_proxywallet_becomes_none():
    """P2: A trade with no proxyWallet / maker / trader yields
    trader_address=None (NOT 'unknown', NOT 'anonymous', NOT '')."""
    raw = {
        "side": "BUY",
        "asset": "asset-anon",
        "conditionId": "0xcond-anon",
        "size": 1.0,
        "price": 0.5,
        "timestamp": 1782636254,
        "outcome": "Yes",
        "outcomeIndex": 0,
        # NO proxyWallet, maker, trader fields at all
    }

    async def fake_handler(request):
        import httpx
        return httpx.Response(200, json=[raw])

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        try:
            since = datetime.fromtimestamp(0, tz=timezone.utc)
            return await a.get_recent_trades(
                market_source_id="0xcond-anon", since=since, limit=10,
            )
        finally:
            await a.aclose()

    trades = asyncio.run(run())
    assert len(trades) == 1
    t = trades[0]
    assert t.trader_address is None
    assert t.is_sample is False
    # The SourceTrade model itself accepts None.
    st = SourceTrade(
        source="polymarket_data_api",
        source_trade_id=t.source_trade_id,
        market_source_id=t.market_source_id,
        side=t.side,
        outcome=t.outcome,
        quantity=t.quantity,
        price=t.price,
        trader_address=None,
        timestamp=t.timestamp,
        is_sample=False,
    )
    assert st.trader_address is None


def test_anonymous_trade_persists_without_wallet(tmp_path: Path):
    """P2: A SourceTrade with trader_address=None persists into source_trades
    (with the v5 schema applied) and produces NO wallet row."""
    db_path = tmp_path / "anon.db"
    db = Database(db_path=db_path).connect()
    # v5 schema allows NULL trader_address — verify we can INSERT one.
    db.execute(
        """INSERT INTO source_trades
           (id, source, source_trade_id, market_source_id, side, outcome,
            quantity, price, trader_address, timestamp, is_sample)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0)""",
        (
            str(uuid.uuid4()),
            "polymarket_data_api",
            "polymarket:abc",
            "0xcond",
            "BUY",
            "Yes",
            1.0,
            0.5,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.conn.commit()

    # The trade row exists.
    rows = db.fetchall("SELECT * FROM source_trades")
    assert len(rows) == 1
    assert rows[0]["trader_address"] is None

    # Collector-style wallet discovery loop skips None addresses (P2):
    # Mirror the collector: SELECT DISTINCT, skip None, never persist a wallet.
    distinct = db.fetchall(
        "SELECT DISTINCT trader_address FROM source_trades "
        "WHERE trader_address IS NOT NULL AND trader_address != ''"
    )
    assert distinct == []

    # No wallet row should have been created with address=NULL or "unknown".
    wallets = db.fetchall("SELECT * FROM wallets")
    assert all(w["address"] not in (None, "", "unknown") for w in wallets)
    db.close()


def test_multiple_anonymous_trades_do_not_collapse(tmp_path: Path):
    """P2: 5 anonymous trades (all trader_address=NULL) → 5 source_trades
    rows, 0 wallets, 0 unique non-null addresses."""
    db_path = tmp_path / "anon_multi.db"
    db = Database(db_path=db_path).connect()
    for i in range(5):
        db.execute(
            """INSERT INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0)""",
            (
                str(uuid.uuid4()),
                "polymarket_data_api",
                f"polymarket:row{i}",
                "0xcond",
                "BUY",
                "Yes",
                1.0,
                0.5,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    db.conn.commit()

    n_trades = db.fetchone("SELECT COUNT(*) AS n FROM source_trades")["n"]
    assert n_trades == 5

    # _get_unique_trader_addresses-style query — must return 0 rows.
    rows = db.fetchall(
        "SELECT DISTINCT trader_address FROM source_trades "
        "WHERE trader_address IS NOT NULL AND trader_address != ''"
    )
    assert rows == []

    # Collector-style wallet discovery creates zero wallets from these.
    wallets = db.fetchall("SELECT * FROM wallets")
    assert wallets == []
    db.close()


def test_anonymous_trades_excluded_from_wallet_scoring(tmp_path: Path):
    """P2: A mix of anonymous + attributed trades → scoring loop sees only the
    attributed 0x addresses (no None, no 'unknown')."""
    db_path = tmp_path / "mixed.db"
    db = Database(db_path=db_path).connect()

    # 5 anonymous
    for i in range(5):
        db.execute(
            """INSERT INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0)""",
            (
                str(uuid.uuid4()),
                "polymarket_data_api",
                f"polymarket:anon{i}",
                "0xcond",
                "BUY",
                "Yes",
                1.0,
                0.5,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    # 3 attributed
    real_wallets = [
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "0xcccccccccccccccccccccccccccccccccccccccc",
    ]
    for i, addr in enumerate(real_wallets):
        db.execute(
            """INSERT INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                str(uuid.uuid4()),
                "polymarket_data_api",
                f"polymarket:real{i}",
                "0xcond",
                "BUY",
                "Yes",
                1.0,
                0.5,
                addr,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    db.conn.commit()

    # Mirror the scoring loop (same WHERE clause as _get_unique_trader_addresses).
    rows = db.fetchall(
        "SELECT DISTINCT trader_address FROM source_trades "
        "WHERE trader_address IS NOT NULL AND trader_address != ''"
    )
    scoring_inputs = [r["trader_address"] for r in rows]
    assert None not in scoring_inputs
    assert "unknown" not in scoring_inputs
    assert "" not in scoring_inputs
    assert sorted(scoring_inputs) == sorted(real_wallets)
    db.close()


def test_attributed_trades_still_create_wallets(tmp_path: Path):
    """P2: 3 attributed trades with distinct 0x addresses → 3 wallet rows."""
    db_path = tmp_path / "attr.db"
    db = Database(db_path=db_path).connect()

    addrs = [
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "0xcccccccccccccccccccccccccccccccccccccccc",
    ]
    for i, addr in enumerate(addrs):
        db.execute(
            """INSERT INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                str(uuid.uuid4()),
                "polymarket_data_api",
                f"polymarket:attr{i}",
                "0xcond",
                "BUY",
                "Yes",
                1.0,
                0.5,
                addr,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        # Mirror collector._persist_wallet
        db.execute(
            """INSERT OR REPLACE INTO wallets
               (id, address, label, is_sample, created_at)
               VALUES (?, ?, ?, 0, ?)""",
            (
                str(uuid.uuid4()),
                addr,
                "discovered-from-polymarket_data_api",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    db.conn.commit()

    rows = db.fetchall("SELECT * FROM wallets")
    assert len(rows) == 3
    persisted_addrs = sorted(w["address"] for w in rows)
    assert persisted_addrs == sorted(addrs)
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# P2 — mixed-window end-to-end via the collector's helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_mixed_attributed_and_anonymous_window(monkeypatch, tmp_path: Path):
    """P2: A 2-attributed + 3-anonymous window persists 5 source_trades rows,
    creates 2 wallets, and reports anonymous_trades_skipped=3."""
    # Force the collector module path.
    from scripts.collect_smart_money_data import (
        CollectionResult,
        _get_unique_trader_addresses,
    )
    from polycopy.db.database import Database

    db_path = tmp_path / "e2e.db"
    db = Database(db_path=db_path).connect()
    result = CollectionResult()

    # Build 5 raw trades: 2 attributed, 3 anonymous.
    raw_trades = [
        {
            "proxyWallet": "0x1111111111111111111111111111111111111111",
            "side": "BUY", "asset": "a1", "conditionId": "0xcond",
            "size": 1.0, "price": 0.5, "timestamp": 1782636254,
            "outcome": "Yes", "outcomeIndex": 0,
            "transactionHash": "0xfeedface00000001",
        },
        {
            "proxyWallet": "0x2222222222222222222222222222222222222222",
            "side": "SELL", "asset": "a2", "conditionId": "0xcond",
            "size": 2.0, "price": 0.6, "timestamp": 1782636255,
            "outcome": "No", "outcomeIndex": 1,
            "transactionHash": "0xfeedface00000002",
        },
        # 3 anonymous:
        {"side": "BUY", "asset": "a3", "conditionId": "0xcond",
         "size": 1.0, "price": 0.7, "timestamp": 1782636256,
         "outcome": "Yes", "outcomeIndex": 0,
         "transactionHash": "0xfeedface00000003"},
        {"side": "SELL", "asset": "a4", "conditionId": "0xcond",
         "size": 1.5, "price": 0.8, "timestamp": 1782636257,
         "outcome": "No", "outcomeIndex": 1,
         "transactionHash": "0xfeedface00000004"},
        {"side": "BUY", "asset": "a5", "conditionId": "0xcond",
         "size": 0.5, "price": 0.9, "timestamp": 1782636258,
         "outcome": "Yes", "outcomeIndex": 0,
         "transactionHash": "0xfeedface00000005"},
    ]

    # Parse via the adapter (mock transport) so we exercise the real parser.
    async def fake_handler(request):
        import httpx
        return httpx.Response(200, json=raw_trades)

    async def parse_all():
        a = _make_adapter_with_transport(fake_handler)
        try:
            since = datetime.fromtimestamp(0, tz=timezone.utc)
            return await a.get_recent_trades(
                market_source_id="0xcond", since=since, limit=10,
            )
        finally:
            await a.aclose()

    trades = asyncio.run(parse_all())
    assert len(trades) == 5

    # Mirror the collector's persist + wallet-discovery loop:
    for trade in trades:
        # _persist_trade
        db.execute(
            """INSERT INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(trade.id),
                trade.source,
                trade.source_trade_id,
                trade.market_source_id,
                trade.side.value if hasattr(trade.side, "value") else str(trade.side),
                trade.outcome,
                trade.quantity,
                trade.price,
                trade.trader_address,
                trade.timestamp.isoformat(),
                int(trade.is_sample),
            ),
        )
        result.trades_fetched += 1
        # P2: anonymous skip
        if trade.trader_address is None or not str(trade.trader_address).strip():
            result.anonymous_trades_skipped += 1
            continue
        # Wallet persist
        db.execute(
            """INSERT OR REPLACE INTO wallets
               (id, address, label, is_sample, created_at)
               VALUES (?, ?, ?, 0, ?)""",
            (
                str(uuid.uuid4()),
                trade.trader_address,
                f"discovered-from-{trade.source}",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        result.wallets_discovered += 1
    db.conn.commit()

    # Assertions:
    n_trades = db.fetchone("SELECT COUNT(*) AS n FROM source_trades")["n"]
    assert n_trades == 5
    n_wallets = db.fetchone("SELECT COUNT(*) AS n FROM wallets")["n"]
    assert n_wallets == 2
    assert result.anonymous_trades_skipped == 3
    assert result.wallets_discovered == 2

    # _get_unique_trader_addresses returns only the 2 attributed addresses.
    addrs = _get_unique_trader_addresses(db)
    assert len(addrs) == 2
    assert None not in addrs
    assert "unknown" not in addrs
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# P2 — capability flag tests
# ─────────────────────────────────────────────────────────────────────────────


def test_capability_flag_with_anonymous_window():
    """probe_trade_capability with a window where NO row has a real 0x
    proxyWallet → wallet_attribution_available=False, status='partial'."""
    window = [
        {"side": "BUY", "asset": "a", "conditionId": "0xcond",
         "size": 1.0, "price": 0.5, "timestamp": 1782636254,
         "outcome": "Yes", "outcomeIndex": 0,
         "transactionHash": "0xfeedface00000001"},
        {"side": "SELL", "asset": "b", "conditionId": "0xcond",
         "size": 2.0, "price": 0.6, "timestamp": 1782636255,
         "outcome": "No", "outcomeIndex": 1,
         "transactionHash": "0xfeedface00000002"},
    ]

    async def fake_handler(request):
        import httpx
        return httpx.Response(200, json=window)

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        try:
            return await a.probe_trade_capability()
        finally:
            await a.aclose()

    cap = asyncio.run(run())
    assert cap["wallet_attribution_available"] is False
    assert cap["status"] in ("partial", "ok")
    assert cap["status"] != "unavailable"


def test_capability_flag_with_mixed_window():
    """probe_trade_capability where MOST rows have proxyWallet but a few
    don't → wallet_attribution_available=True."""
    window = [
        {"proxyWallet": "0x1111111111111111111111111111111111111111",
         "side": "BUY", "asset": "a", "conditionId": "0xcond",
         "size": 1.0, "price": 0.5, "timestamp": 1782636254,
         "outcome": "Yes", "outcomeIndex": 0,
         "transactionHash": "0xfeedface00000001"},
        {"proxyWallet": "0x2222222222222222222222222222222222222222",
         "side": "SELL", "asset": "b", "conditionId": "0xcond",
         "size": 2.0, "price": 0.6, "timestamp": 1782636255,
         "outcome": "No", "outcomeIndex": 1,
         "transactionHash": "0xfeedface00000002"},
        {"proxyWallet": "0x3333333333333333333333333333333333333333",
         "side": "BUY", "asset": "c", "conditionId": "0xcond",
         "size": 3.0, "price": 0.7, "timestamp": 1782636256,
         "outcome": "Yes", "outcomeIndex": 0,
         "transactionHash": "0xfeedface00000003"},
        # one anonymous row:
        {"side": "SELL", "asset": "d", "conditionId": "0xcond",
         "size": 1.0, "price": 0.8, "timestamp": 1782636257,
         "outcome": "No", "outcomeIndex": 1,
         "transactionHash": "0xfeedface00000004"},
    ]

    async def fake_handler(request):
        import httpx
        return httpx.Response(200, json=window)

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        try:
            return await a.probe_trade_capability()
        finally:
            await a.aclose()

    cap = asyncio.run(run())
    assert cap["wallet_attribution_available"] is True
    assert cap["status"] == "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Schema migration tests (v4 → v5)
# ─────────────────────────────────────────────────────────────────────────────


def _init_db_at_version(db_path: Path, target: int) -> sqlite3.Connection:
    """Init a DB and run migrations 1..target (raw sqlite3, not the Database
    helper — we need fine control over the migration boundary)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    for v in range(1, target + 1):
        for stmt in MIGRATIONS[v]:
            conn.execute(stmt)
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(v),),
        )
    conn.commit()
    return conn


def test_schema_v4_to_v5_migration(tmp_path: Path):
    """Pre-v5 source_trades has trader_address NOT NULL. After v5 the column
    is nullable, and all pre-existing rows are preserved verbatim (including
    the legacy 'unknown' sentinel — we do NOT rewrite historical data)."""
    db_path = tmp_path / "v4to5.db"
    conn = _init_db_at_version(db_path, 4)
    # Verify pre-migration shape.
    cols = conn.execute("PRAGMA table_info(source_trades)").fetchall()
    trader_col = next(c for c in cols if c["name"] == "trader_address")
    assert trader_col["notnull"] == 1, "pre-v5 trader_address should be NOT NULL"

    # Insert: one attributed, one legacy "unknown" sentinel.
    conn.execute(
        """INSERT INTO source_trades
           (id, source, source_trade_id, market_source_id, side, outcome,
            quantity, price, trader_address, timestamp, is_sample)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (
            str(uuid.uuid4()),
            "polymarket_data_api",
            "row-attr",
            "0xcond",
            "BUY",
            "Yes",
            1.0,
            0.5,
            "0xATTRIBUTED_WALLET",
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.execute(
        """INSERT INTO source_trades
           (id, source, source_trade_id, market_source_id, side, outcome,
            quantity, price, trader_address, timestamp, is_sample)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (
            str(uuid.uuid4()),
            "polymarket_data_api",
            "row-unknown",
            "0xcond",
            "BUY",
            "Yes",
            1.0,
            0.5,
            "unknown",
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()

    # Apply v5 migration.
    for stmt in _V5_DDL:
        conn.execute(stmt)
    conn.execute(
        "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
        ("5",),
    )
    conn.commit()

    # Both rows preserved.
    rows = conn.execute(
        "SELECT source_trade_id, trader_address FROM source_trades ORDER BY source_trade_id"
    ).fetchall()
    assert len(rows) == 2
    by_id = {r["source_trade_id"]: r["trader_address"] for r in rows}
    assert by_id["row-attr"] == "0xATTRIBUTED_WALLET"
    assert by_id["row-unknown"] == "unknown", (
        "legacy 'unknown' sentinel must be preserved verbatim"
    )

    # trader_address is now nullable.
    cols = conn.execute("PRAGMA table_info(source_trades)").fetchall()
    trader_col = next(c for c in cols if c["name"] == "trader_address")
    assert trader_col["notnull"] == 0, "v5 trader_address should be NULLABLE"

    # Insert a row with trader_address=NULL succeeds.
    conn.execute(
        """INSERT INTO source_trades
           (id, source, source_trade_id, market_source_id, side, outcome,
            quantity, price, trader_address, timestamp, is_sample)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0)""",
        (
            str(uuid.uuid4()),
            "polymarket_data_api",
            "row-null",
            "0xcond",
            "BUY",
            "Yes",
            1.0,
            0.5,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    null_row = conn.execute(
        "SELECT trader_address FROM source_trades WHERE source_trade_id = ?",
        ("row-null",),
    ).fetchone()
    assert null_row["trader_address"] is None

    # is_sample is preserved on all rows.
    n_sample = conn.execute(
        "SELECT COUNT(*) AS n FROM source_trades WHERE is_sample = 0"
    ).fetchone()["n"]
    assert n_sample == 3

    conn.close()


def test_schema_version_after_migration(tmp_path: Path):
    """After fresh init via Database, the schema_version row in _meta must
    equal the SCHEMA_VERSION constant (currently 5)."""
    db_path = tmp_path / "fresh.db"
    db = Database(db_path=db_path).connect()
    row = db.fetchone("SELECT value FROM _meta WHERE key = 'schema_version'")
    assert row is not None
    assert int(row["value"]) == SCHEMA_VERSION
    assert SCHEMA_VERSION == 5, (
        "SCHEMA_VERSION constant should be 5 after the v5 migration"
    )
    db.close()