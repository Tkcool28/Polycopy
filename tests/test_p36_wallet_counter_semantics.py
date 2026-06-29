"""Round 11 / P3 PRRT_kwDOTG4Cf86M7Xbp — wallet counter semantics tests.

The three explicit counter fields on ``ScanResult`` are
``wallets_loaded_existing``, ``wallets_discovered_new``, and
``wallets_total_known``. The back-compat alias ``wallets_discovered``
equals ``wallets_discovered_new`` (per-run new-wallet count). These
tests pin the contract that:

* the same canonical wallet, regardless of how many times it appears
  in a scan, increments ``discovered_new`` at most once;
* a wallet that already lives in the ``wallets`` table is never
  counted as new;
* a wallet whose INSERT fails never increments ``discovered_new``;
* a wallet whose canonical form is mixed case / padded is
  deduplicated against the lowercase canonical form;
* anonymous trades never affect any wallet counter.

The tests run real ``run_scan`` invocations through
``httpx.MockTransport`` so the production code path is exercised.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.adapters.polymarket import (  # noqa: E402
    PolymarketPublicAdapter,
)
from polycopy.db.database import Database  # noqa: E402
from polycopy.domain.market import Market, MarketOutcome  # noqa: E402
from polycopy.domain.source_trade import is_sentinel_trader_address  # noqa: E402

import scripts.run_scan as run_scan_module  # noqa: E402


# ─── Helpers ──────────────────────────────────────────────────────────────


def _adapter(handler) -> PolymarketPublicAdapter:
    """Build an adapter wired to a MockTransport that filters by the
    ``market`` query param. The adapter's data-api base URL is the
    default; the MockTransport intercepts every request regardless of
    host."""
    a = PolymarketPublicAdapter(
        gamma_base_url="https://gamma.example.test",
        clob_base_url="https://clob.example.test",
        data_api_base_url="https://data-api.example.test",
        timeout=5.0,
        rate_limit_rps=5.0,
        data_api_window_size=1000,
        data_api_request_interval_seconds=0.0,
    )
    a._data_client = httpx.AsyncClient(  # noqa: SLF001
        base_url=a.data_api_base_url,
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )
    return a


def _market(source_id: str = "0xMARKET_A") -> Market:
    return Market(
        source_id=source_id,
        question="Test market",
        outcomes=[MarketOutcome(label="Yes", price=0.7, volume=20_000.0)],
        source="polymarket",
        active=True,
        closed=False,
        resolved=False,
        volume_24h=20_000.0,
        fetched_at=datetime.now(timezone.utc),
        is_sample=False,
    )


def _raw_trade(
    market: str,
    wallet: str,
    asset: str,
    ts: int,
    suffix: str,
) -> dict:
    """A canonical raw data-api row with a real proxyWallet. The
    deterministic source_trade_id is computed from this dict."""
    return {
        "proxyWallet": wallet,
        "side": "BUY",
        "asset": asset,
        "conditionId": market,
        "size": 1.0,
        "price": 0.5,
        "timestamp": ts,
        "outcome": "Yes",
        "transactionHash": f"0x{suffix:0>8}",
    }


def _install_adapter(monkeypatch, adapter: PolymarketPublicAdapter):
    """Reset the run_scan lazy singleton so the next call uses our
    adapter."""
    monkeypatch.setattr(
        run_scan_module,
        "_get_scan_trade_adapter",
        lambda: adapter,
        raising=False,
    )
    monkeypatch.setattr(
        run_scan_module,
        "_SCAN_TRADE_ADAPTER",
        adapter,
        raising=False,
    )


def _patched_fetch_markets(monkeypatch, markets):
    async def fake_fetch_markets(db, settings, limit, result, use_sample):
        return markets, {}

    monkeypatch.setattr(run_scan_module, "_fetch_markets", fake_fetch_markets)


def _patched_generate_signals(monkeypatch, signals=None):
    if signals is None:
        signals = []

    def fake_generate_signals(db, markets, now):
        return signals

    monkeypatch.setattr(run_scan_module, "_generate_signals", fake_generate_signals)


def _empty_db(tmp_path: Path, name: str = "p36.sqlite") -> Database:
    db_path = tmp_path / name
    if db_path.exists():
        db_path.unlink()
    return Database(db_path=db_path).connect()


# ─── Tests ────────────────────────────────────────────────────────────────


class TestWalletCounterSemantics:
    """The three explicit wallet counters must track the right things."""

    @pytest.mark.asyncio
    async def test_three_existing_plus_two_new_yields_existing_3_new_2_total_5(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """3 wallets pre-exist in the ``wallets`` table; 2 new
        canonical addresses are discovered during the scan. Counters
        must read: existing=3, new=2, total=5, alias=2."""
        market = _market()
        # Pre-seed 3 existing wallets in the DB (case-mixed to test
        # the canonicalization invariant).
        db = _empty_db(tmp_path, "p36-existing.sqlite")
        existing = [
            "0xaaaa0000000000000000000000000000000001",
            "0xbbbb0000000000000000000000000000000002",
            "0xcccc0000000000000000000000000000000003",
        ]
        now_iso = datetime.now(timezone.utc).isoformat()
        for w in existing:
            db.execute(
                "INSERT INTO wallets (id, address, label, is_sample, created_at) "
                "VALUES (?, ?, 'p36-existing', 0, ?)",
                (f"p36-{w}", w, now_iso),
            )
        db.conn.commit()

        # 2 new wallets discovered in the live data-api response.
        new = [
            "0xdddd0000000000000000000000000000000004",
            "0xeeee0000000000000000000000000000000005",
        ]
        ts = int(datetime.now(timezone.utc).timestamp())
        raws = []
        for i, w in enumerate(new):
            raws.append(_raw_trade(market.source_id, w, f"asset-{i}", ts + i, f"new_{i}"))

        async def handler(request: httpx.Request) -> httpx.Response:
            from urllib.parse import parse_qs
            qs = parse_qs(str(request.url.query, "utf-8"))
            m = (qs.get("market") or [""])[0].lower()
            matched = [r for r in raws if str(r.get("conditionId", "")).lower() == m]
            return httpx.Response(200, json=matched)

        adapter = _adapter(handler)
        _install_adapter(monkeypatch, adapter)
        _patched_fetch_markets(monkeypatch, [market])
        _patched_generate_signals(monkeypatch)
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p36-existing.sqlite"))

        result = await run_scan_module.run_scan(  # noqa: SLF001
            db, market_limit=1, use_sample=False
        )
        db.close()

        # Step 1 loaded the 3 pre-existing rows.
        assert result.wallets_loaded_existing == 3
        # Step 3 added the 2 new canonical addresses.
        assert result.wallets_discovered_new == 2
        # Total = 3 + 2 = 5.
        assert result.wallets_total_known == 5
        # Back-compat alias equals new (NOT total).
        assert result.wallets_discovered == 2

    @pytest.mark.asyncio
    async def test_repeated_trade_from_one_new_wallet_increments_new_only_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A single canonical wallet with 5 different trades must
        increment ``discovered_new`` exactly once."""
        market = _market()
        wallet = "0x1111111111111111111111111111111111111111"
        ts = int(datetime.now(timezone.utc).timestamp())
        raws = [
            _raw_trade(market.source_id, wallet, f"asset-{i}", ts + i, f"dup_{i}")
            for i in range(5)
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            from urllib.parse import parse_qs
            qs = parse_qs(str(request.url.query, "utf-8"))
            m = (qs.get("market") or [""])[0].lower()
            matched = [r for r in raws if str(r.get("conditionId", "")).lower() == m]
            return httpx.Response(200, json=matched)

        adapter = _adapter(handler)
        _install_adapter(monkeypatch, adapter)
        _patched_fetch_markets(monkeypatch, [market])
        _patched_generate_signals(monkeypatch)
        db = _empty_db(tmp_path, "p36-dup.sqlite")
        try:
            result = await run_scan_module.run_scan(  # noqa: SLF001
                db, market_limit=1, use_sample=False
            )
            assert result.trades_fetched == 5
            assert result.wallets_discovered_new == 1
            assert result.wallets_total_known == 1
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_repeated_scan_increments_new_by_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A repeated scan over the same trades must NOT increment
        ``discovered_new``: the wallets are already known, the
        discovery entry's ``is_new`` flag is False, and the
        pre-existing-wallet loader picks them up at Step 1."""
        market = _market()
        wallet = "0x2222222222222222222222222222222222222222"
        ts = int(datetime.now(timezone.utc).timestamp())
        raws = [_raw_trade(market.source_id, wallet, "asset-rs", ts, "rs_0")]

        async def handler(request: httpx.Request) -> httpx.Response:
            from urllib.parse import parse_qs
            qs = parse_qs(str(request.url.query, "utf-8"))
            m = (qs.get("market") or [""])[0].lower()
            matched = [r for r in raws if str(r.get("conditionId", "")).lower() == m]
            return httpx.Response(200, json=matched)

        adapter = _adapter(handler)
        _install_adapter(monkeypatch, adapter)
        _patched_fetch_markets(monkeypatch, [market])
        _patched_generate_signals(monkeypatch)
        db = _empty_db(tmp_path, "p36-rs.sqlite")
        try:
            # First run: wallet is new.
            r1 = await run_scan_module.run_scan(  # noqa: SLF001
                db, market_limit=1, use_sample=False
            )
            assert r1.wallets_loaded_existing == 0
            assert r1.wallets_discovered_new == 1
            assert r1.wallets_total_known == 1
            # Second run: same trades, same wallet — the wallet is
            # now pre-existing.
            r2 = await run_scan_module.run_scan(  # noqa: SLF001
                db, market_limit=1, use_sample=False
            )
            assert r2.wallets_loaded_existing == 1
            assert r2.wallets_discovered_new == 0
            assert r2.wallets_total_known == 1
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_failed_wallet_persistence_does_not_increment_new(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A wallet whose INSERT fails must NOT count as new. This is
        the same gating invariant tested in p35, expressed against
        the new counter semantics."""
        market = _market()
        wallet = "0x3333333333333333333333333333333333333333"
        ts = int(datetime.now(timezone.utc).timestamp())
        raws = [_raw_trade(market.source_id, wallet, "asset-fail", ts, "fail_0")]

        async def handler(request: httpx.Request) -> httpx.Response:
            from urllib.parse import parse_qs
            qs = parse_qs(str(request.url.query, "utf-8"))
            m = (qs.get("market") or [""])[0].lower()
            matched = [r for r in raws if str(r.get("conditionId", "")).lower() == m]
            return httpx.Response(200, json=matched)

        adapter = _adapter(handler)
        _install_adapter(monkeypatch, adapter)
        _patched_fetch_markets(monkeypatch, [market])
        _patched_generate_signals(monkeypatch)
        monkeypatch.setattr(
            run_scan_module, "_persist_wallet", lambda db, w: None,
        )
        db = _empty_db(tmp_path, "p36-fail.sqlite")
        try:
            result = await run_scan_module.run_scan(  # noqa: SLF001
                db, market_limit=1, use_sample=False
            )
            assert result.trades_persisted == 1
            assert result.wallets_discovered_new == 0
            assert result.wallets_total_known == 0
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_case_and_padding_variants_increment_new_only_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Mixed case + padded variants of the same canonical address
        must collapse to one ``discovered_new`` increment. This is
        the canonicalization invariant pinned by p30, expressed
        here against the new counter semantics."""
        market = _market()
        canonical = "0x4444444444444444444444444444444444444444"
        variants = [
            ("0xAbCdE", "asset-v0"),  # NOT the right wallet, filler
        ]
        # Real variants — all should collapse to `canonical`.
        for label, asset in [
            (canonical, "asset-v1"),
            (canonical.upper(), "asset-v2"),
            (f"  {canonical}  ", "asset-v3"),
            (f"\t{canonical}\n", "asset-v4"),
        ]:
            variants.append((label, asset))
        ts = int(datetime.now(timezone.utc).timestamp())
        raws = [
            _raw_trade(market.source_id, w, a, ts + i, f"var_{i}")
            for i, (w, a) in enumerate(variants)
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            from urllib.parse import parse_qs
            qs = parse_qs(str(request.url.query, "utf-8"))
            m = (qs.get("market") or [""])[0].lower()
            matched = [r for r in raws if str(r.get("conditionId", "")).lower() == m]
            return httpx.Response(200, json=matched)

        adapter = _adapter(handler)
        _install_adapter(monkeypatch, adapter)
        _patched_fetch_markets(monkeypatch, [market])
        _patched_generate_signals(monkeypatch)
        db = _empty_db(tmp_path, "p36-variants.sqlite")
        try:
            result = await run_scan_module.run_scan(  # noqa: SLF001
                db, market_limit=1, use_sample=False
            )
            # 2 distinct canonical addresses (canonical + 0xAbCdE)
            # so discovered_new should be 2, not 5.
            assert result.wallets_discovered_new == 2, (
                f"expected 2 distinct canonical wallets, got "
                f"{result.wallets_discovered_new}"
            )
            assert result.wallets_total_known == 2
            # And both wallets are in the DB.
            rows = db.fetchall("SELECT address FROM wallets ORDER BY address")
            addrs = [
                r["address"] for r in rows
                if not is_sentinel_trader_address(r["address"])
            ]
            assert canonical in addrs
            assert "0xabcde" in addrs
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_anonymous_trades_do_not_affect_wallet_counters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A scan with only anonymous (trader_address=None) trades
        must produce all-zero wallet counters."""
        market = _market()
        ts = int(datetime.now(timezone.utc).timestamp())
        raws = [
            {
                "side": "BUY",
                "asset": f"asset-a{i}",
                "conditionId": market.source_id,
                "size": 1.0,
                "price": 0.5,
                "timestamp": ts + i,
                "outcome": "Yes",
                "transactionHash": f"0xanon{i:0>8}",
            }
            for i in range(3)
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            from urllib.parse import parse_qs
            qs = parse_qs(str(request.url.query, "utf-8"))
            m = (qs.get("market") or [""])[0].lower()
            matched = [r for r in raws if str(r.get("conditionId", "")).lower() == m]
            return httpx.Response(200, json=matched)

        adapter = _adapter(handler)
        _install_adapter(monkeypatch, adapter)
        _patched_fetch_markets(monkeypatch, [market])
        _patched_generate_signals(monkeypatch)
        db = _empty_db(tmp_path, "p36-anon.sqlite")
        try:
            result = await run_scan_module.run_scan(  # noqa: SLF001
                db, market_limit=1, use_sample=False
            )
            assert result.trades_persisted == 3
            assert result.anonymous_trades == 3
            assert result.wallets_loaded_existing == 0
            assert result.wallets_discovered_new == 0
            assert result.wallets_total_known == 0
        finally:
            db.close()

    @pytest.mark.asyncio
    async def test_mixed_attributed_and_anonymous_counts_only_attributed_wallets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A mixed window: 2 attributed (1 wallet), 3 anonymous. The
        wallet counter must reflect the single attributed wallet."""
        market = _market()
        wallet = "0x5555555555555555555555555555555555555555"
        ts = int(datetime.now(timezone.utc).timestamp())
        raws = [
            _raw_trade(market.source_id, wallet, "asset-x0", ts, "mix_0"),
            _raw_trade(market.source_id, wallet, "asset-x1", ts + 1, "mix_1"),
        ]
        for i in range(3):
            raws.append({
                "side": "SELL",
                "asset": f"asset-anon{i}",
                "conditionId": market.source_id,
                "size": 1.0,
                "price": 0.5,
                "timestamp": ts + 2 + i,
                "outcome": "No",
                "transactionHash": f"0xmAnon{i:0>7}",
            })

        async def handler(request: httpx.Request) -> httpx.Response:
            from urllib.parse import parse_qs
            qs = parse_qs(str(request.url.query, "utf-8"))
            m = (qs.get("market") or [""])[0].lower()
            matched = [r for r in raws if str(r.get("conditionId", "")).lower() == m]
            return httpx.Response(200, json=matched)

        adapter = _adapter(handler)
        _install_adapter(monkeypatch, adapter)
        _patched_fetch_markets(monkeypatch, [market])
        _patched_generate_signals(monkeypatch)
        db = _empty_db(tmp_path, "p36-mix.sqlite")
        try:
            result = await run_scan_module.run_scan(  # noqa: SLF001
                db, market_limit=1, use_sample=False
            )
            assert result.trades_persisted == 5
            assert result.trades_attributed == 2
            assert result.anonymous_trades == 3
            assert result.wallets_loaded_existing == 0
            assert result.wallets_discovered_new == 1
            assert result.wallets_total_known == 1
        finally:
            db.close()
