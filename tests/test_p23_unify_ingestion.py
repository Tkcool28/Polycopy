"""Regression tests for the P2 + P3 unification of live trade ingestion.

P2: ``scripts/run_scan.py`` in ``use_sample=False`` mode no longer hits the
legacy gamma ``/trades`` endpoint (which never existed and silently produced
zero trades). It now goes through the same
:class:`polycopy.adapters.polymarket.PolymarketPublicAdapter` that
``scripts/collect_smart_money_data.py`` uses, so both ingest paths produce
identical normalized ``SourceTrade`` objects.

P3: ``scripts/collect_smart_money_data.collect_trades`` no longer writes a
provenance snapshot on every per-market call. The adapter's shared global
window caches after the first call, so an N-market run would inflate
``snapshots_saved`` by N. The collector now snapshots only on a real upstream
fetch (cache miss).

This test file uses only ``httpx.MockTransport`` (no real network calls).
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure repo + src are importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.adapters.polymarket import (  # noqa: E402
    PolymarketPublicAdapter,
    deterministic_source_trade_id_v2,
)
from polycopy.config.settings import Settings, get_settings  # noqa: E402
from polycopy.db.database import Database  # noqa: E402
from polycopy.domain.source_trade import is_sentinel_trader_address  # noqa: E402

import scripts.collect_smart_money_data as collect_mod  # noqa: E402
import scripts.run_scan as run_scan_module  # noqa: E402
import scripts._live_ingest as live_ingest  # noqa: E402


# ─── Fixtures / helpers ───────────────────────────────────────────────────────


def _make_adapter_with_transport(handler):
    """Build a PolymarketPublicAdapter wired to an httpx.MockTransport that
    funnels every request to ``handler``.

    Resets the cache state so the adapter performs a fresh fetch on first
    call. The adapter's data-api base URL is the default
    ``https://data-api.polymarket.com`` — the MockTransport intercepts every
    request regardless of host.
    """
    import httpx

    settings = Settings()
    a = PolymarketPublicAdapter(
        gamma_base_url=settings.gamma_base_url,
        clob_base_url=settings.clob_base_url,
        data_api_base_url=settings.data_api_base_url,
        timeout=5.0,
        rate_limit_rps=5.0,
        data_api_window_size=100,
        data_api_request_interval_seconds=0.0,
    )
    transport = httpx.MockTransport(handler)
    a._data_client = httpx.AsyncClient(
        base_url=a.data_api_base_url, transport=transport, timeout=5.0
    )
    # Force a fresh fetch on the first call.
    a._window_lock_time = 0.0
    a._window_trades = []
    return a


def _make_market(source_id: str = "0xMARKET_A"):
    from polycopy.domain.market import Market, MarketOutcome

    return Market(
        source_id=source_id,
        question=f"Test market {source_id}",
        outcomes=[MarketOutcome(label="Yes", price=0.6, volume=12000.0)],
        source="polymarket",
        active=True,
        closed=False,
        resolved=False,
        volume_24h=12000.0,
        fetched_at=datetime.now(timezone.utc),
        is_sample=False,
    )


def _empty_db(tmp_path: Path) -> Database:
    db_path = tmp_path / "p23.sqlite"
    if db_path.exists():
        db_path.unlink()
    return Database(db_path=db_path).connect()


def _patched_fetch_markets(monkeypatch, markets):
    async def fake_fetch_markets(db, settings, limit, result, use_sample):
        return markets

    monkeypatch.setattr(run_scan_module, "_fetch_markets", fake_fetch_markets)


def _patched_generate_signals(monkeypatch, signals):
    def fake_generate_signals(db, markets, now):
        return signals

    monkeypatch.setattr(run_scan_module, "_generate_signals", fake_generate_signals)


# A 5-trade mixed window: 2 attributed, 2 anonymous (no proxyWallet), 1
# legacy-sentinel "unknown" string. All conditionId == "0xMARKET_A".
_MIXED_5 = [
    {
        "proxyWallet": "0xaaaa0000000000000000000000000000000001",
        "side": "BUY", "asset": "A1", "conditionId": "0xMARKET_A",
        "size": 1.0, "price": 0.4, "timestamp": 1782636254,
        "outcome": "Yes", "transactionHash": "0xattributed_1_real",
    },
    {
        "side": "SELL", "asset": "A2", "conditionId": "0xMARKET_A",
        "size": 2.0, "price": 0.6, "timestamp": 1782636255,
        "outcome": "No", "transactionHash": "0xanon_2_none",
    },
    {
        "proxyWallet": "0xbbbb0000000000000000000000000000000002",
        "side": "BUY", "asset": "A3", "conditionId": "0xMARKET_A",
        "size": 3.0, "price": 0.7, "timestamp": 1782636256,
        "outcome": "Yes", "transactionHash": "0xattributed_3_real",
    },
    {
        "side": "BUY", "asset": "A4", "conditionId": "0xMARKET_A",
        "size": 4.0, "price": 0.3, "timestamp": 1782636257,
        "outcome": "No", "transactionHash": "0xanon_4_none",
    },
    {
        "proxyWallet": "unknown",
        "side": "SELL", "asset": "A5", "conditionId": "0xMARKET_A",
        "size": 5.0, "price": 0.5, "timestamp": 1782636258,
        "outcome": "Yes", "transactionHash": "0xsentinel_5_unknown",
    },
]

# A 2-trade window spread across two markets.
_TWO_MARKETS = [
    {
        "proxyWallet": "0xaaaa0000000000000000000000000000000001",
        "side": "BUY", "asset": "A1", "conditionId": "0xMARKET_A",
        "size": 1.0, "price": 0.4, "timestamp": 1782636254,
        "outcome": "Yes", "transactionHash": "0xattributed_a1",
    },
    {
        "proxyWallet": "0xbbbb0000000000000000000000000000000002",
        "side": "BUY", "asset": "B1", "conditionId": "0xMARKET_B",
        "size": 2.0, "price": 0.5, "timestamp": 1782636255,
        "outcome": "Yes", "transactionHash": "0xattributed_b1",
    },
]


def _install_adapter_in_run_scan(monkeypatch, adapter):
    """Reset the run_scan lazy singleton + helper so the next call returns
    our adapter instance."""
    monkeypatch.setattr(run_scan_module, "_get_scan_trade_adapter", lambda: adapter, raising=False)
    monkeypatch.setattr(run_scan_module, "_SCAN_TRADE_ADAPTER", adapter, raising=False)


# ─── P2: run_scan live mode uses data-api adapter ─────────────────────────────


class TestRunScanLiveUsesDataApi:
    """Verify the legacy gamma /trades endpoint is gone from run_scan."""

    def test_run_scan_no_longer_hits_gamma_trades_endpoint(self, tmp_path, monkeypatch):
        """Any request whose URL is on the gamma base + /trades must NOT be
        issued by run_scan. We assert via captured URL inspection that every
        request from run_scan goes to the data-api (the only URL the adapter
        uses)."""
        captured: list[str] = []

        async def handler(request):
            import httpx
            captured.append(str(request.url))
            return httpx.Response(200, json=_MIXED_5)

        adapter = _make_adapter_with_transport(handler)
        _install_adapter_in_run_scan(monkeypatch, adapter)
        _patched_fetch_markets(monkeypatch, [_make_market("0xMARKET_A")])
        _patched_generate_signals(monkeypatch, [])
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p23.sqlite"))
        db = _empty_db(tmp_path)

        result = asyncio.run(
            run_scan_module.run_scan(db, market_limit=1, use_sample=False)
        )

        # No request should target the gamma host for /trades.
        gamma_trade_requests = [
            u for u in captured
            if "gamma-api.polymarket.com" in u and "/trades" in u
        ]
        assert gamma_trade_requests == [], (
            f"run_scan issued gamma /trades request(s): {gamma_trade_requests}"
        )
        # Every captured request should be on the data-api host.
        for url in captured:
            assert "data-api" in url, f"unexpected request URL: {url}"
        assert result.trades_total == 5
        db.close()

    def test_run_scan_no_parse_clob_trade_legacy_helper(self):
        """Static check: any remaining ``_parse_clob_trade`` helper in
        ``run_scan.py`` must be a deprecated no-op (returns None) — never a
        real parser that fabricates ``polymarket_clob`` trades from raw
        payloads."""
        src = Path(run_scan_module.__file__).read_text()
        if "def _parse_clob_trade" in src:
            # Allow only if the function returns None immediately (no real parsing).
            # Locate the function body and verify it's a deprecated stub.
            import re

            # Match the function header through the end of its indented body.
            m = re.search(
                r"def _parse_clob_trade\([^)]*\)[^:]*:[^\n]*\n((?:[ \t]+.*\n|[ \t]*\n)*)",
                src,
            )
            assert m, "_parse_clob_trade definition not parseable"
            body = m.group(1)
            # A real parser would build a SourceTrade; the deprecated stub
            # returns None on the first executable statement.
            assert "return None" in body, (
                "_parse_clob_trade appears to be a real parser, not a deprecated stub"
            )
            # Docstring must mark it as deprecated.
            assert "deprecat" in body.lower(), (
                "_parse_clob_trade exists but isn't marked as deprecated in its body"
            )
        # Belt-and-braces: no literal "unknown" fallback in any trade-parsing
        # context.
        bad_lines = [
            line for line in src.splitlines()
            if "return" in line and '"unknown"' in line
            and "trader" in line.lower()
        ]
        assert bad_lines == [], (
            f"legacy 'unknown' fallback still present: {bad_lines}"
        )

    def test_run_scan_no_client_get_trades_call(self):
        """Static check: ``run_scan.py`` must not call
        ``client.get(\"/trades\"...)`` on a gamma client."""
        src = Path(run_scan_module.__file__).read_text()
        assert 'client.get("/trades"' not in src, (
            "run_scan.py still contains a direct client.get('/trades') call"
        )
        # Belt-and-braces: any literal "/trades" must not appear in a
        # string passed to client.get anywhere.
        offending = [
            line for line in src.splitlines()
            if 'client.get(' in line and '"/trades"' in line
        ]
        assert offending == [], (
            f"run_scan.py has client.get(... '/trades' ...) call(s): {offending}"
        )

    def test_run_scan_live_attributed_trades_reach_wallet_discovery(self, tmp_path, monkeypatch):
        async def handler(request):
            import httpx
            return httpx.Response(200, json=_MIXED_5)

        adapter = _make_adapter_with_transport(handler)
        _install_adapter_in_run_scan(monkeypatch, adapter)
        _patched_fetch_markets(monkeypatch, [_make_market("0xMARKET_A")])
        _patched_generate_signals(monkeypatch, [])
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p23.sqlite"))
        db = _empty_db(tmp_path)

        result = asyncio.run(
            run_scan_module.run_scan(db, market_limit=1, use_sample=False)
        )

        assert result.wallets_discovered == 2
        rows = db.fetchall("SELECT address FROM wallets")
        addrs = {r["address"] for r in rows if not is_sentinel_trader_address(r["address"])}
        assert "0xaaaa0000000000000000000000000000000001" in addrs
        assert "0xbbbb0000000000000000000000000000000002" in addrs
        db.close()

    def test_run_scan_live_attributed_trades_reach_trade_detector(self, tmp_path, monkeypatch):
        async def handler(request):
            import httpx
            return httpx.Response(200, json=_MIXED_5)

        adapter = _make_adapter_with_transport(handler)
        _install_adapter_in_run_scan(monkeypatch, adapter)
        _patched_fetch_markets(monkeypatch, [_make_market("0xMARKET_A")])
        _patched_generate_signals(monkeypatch, [])
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p23.sqlite"))
        db = _empty_db(tmp_path)

        result = asyncio.run(
            run_scan_module.run_scan(db, market_limit=1, use_sample=False)
        )

        # 2 attributed reach the detector; 3 are skipped (2 None + 1 sentinel).
        assert result.trades_processed == 2
        assert result.trades_total == 5
        assert result.anonymous_trades_skipped == 3
        db.close()

    def test_run_scan_live_anonymous_persists_but_excluded(self, tmp_path, monkeypatch):
        async def handler(request):
            import httpx
            return httpx.Response(200, json=_MIXED_5)

        adapter = _make_adapter_with_transport(handler)
        _install_adapter_in_run_scan(monkeypatch, adapter)
        _patched_fetch_markets(monkeypatch, [_make_market("0xMARKET_A")])
        _patched_generate_signals(monkeypatch, [])
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p23.sqlite"))
        db = _empty_db(tmp_path)

        result = asyncio.run(
            run_scan_module.run_scan(db, market_limit=1, use_sample=False)
        )

        assert result.trades_total == 5
        assert result.anonymous_trades_skipped == 3
        assert result.trades_processed == 2
        assert result.wallets_discovered == 2

        wallet_rows = db.fetchall("SELECT address FROM wallets")
        addrs = {r["address"] for r in wallet_rows if not is_sentinel_trader_address(r["address"])}
        assert None not in addrs
        assert "" not in addrs
        assert "unknown" not in addrs
        db.close()

    def test_run_scan_live_mixed_window_counts_correct(self, tmp_path, monkeypatch):
        async def handler(request):
            import httpx
            return httpx.Response(200, json=_MIXED_5)

        adapter = _make_adapter_with_transport(handler)
        _install_adapter_in_run_scan(monkeypatch, adapter)
        _patched_fetch_markets(monkeypatch, [_make_market("0xMARKET_A")])
        _patched_generate_signals(monkeypatch, [])
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p23.sqlite"))
        db = _empty_db(tmp_path)

        result = asyncio.run(
            run_scan_module.run_scan(db, market_limit=1, use_sample=False)
        )

        assert result.trades_total == 5
        assert result.anonymous_trades_skipped == 3
        assert result.trades_processed == 2
        assert result.wallets_discovered == 2
        # Conservation: total == processed + skipped.
        assert result.trades_total == result.trades_processed + result.anonymous_trades_skipped
        db.close()

    def test_run_scan_live_api_failure_does_not_crash(self, tmp_path, monkeypatch):
        async def boom_handler(request):
            import httpx
            return httpx.Response(500, text="upstream is on fire")

        adapter = _make_adapter_with_transport(boom_handler)
        _install_adapter_in_run_scan(monkeypatch, adapter)
        _patched_fetch_markets(monkeypatch, [_make_market("0xMARKET_A")])
        _patched_generate_signals(monkeypatch, [])
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p23.sqlite"))
        db = _empty_db(tmp_path)

        result = asyncio.run(
            run_scan_module.run_scan(db, market_limit=1, use_sample=False)
        )

        assert result.trades_total == 0
        assert result.trades_processed == 0
        assert result.anonymous_trades_skipped == 0
        assert result.wallets_discovered == 0
        db.close()

    def test_run_scan_sample_mode_does_not_hit_adapter(self, tmp_path, monkeypatch):
        captured: list[str] = []

        async def unexpected_handler(request):
            import httpx
            captured.append(str(request.url))
            return httpx.Response(200, json=_MIXED_5)

        adapter = _make_adapter_with_transport(unexpected_handler)
        _install_adapter_in_run_scan(monkeypatch, adapter)
        _patched_fetch_markets(monkeypatch, [_make_market("sample-market-001")])
        _patched_generate_signals(monkeypatch, [])
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p23.sqlite"))
        db = _empty_db(tmp_path)

        result = asyncio.run(
            run_scan_module.run_scan(db, market_limit=1, use_sample=True)
        )

        assert captured == [], (
            f"sample mode must not call the adapter; got {captured!r}"
        )
        assert result.trades_total == 2  # _get_sample_trades returns 2 trades per market
        # Sample addresses look like "0xSAMPLE_TRADER_*_DO_NOT_USE" — they
        # are NOT in the sentinel set so they pass through as attributed.
        # (Sample wallets would normally be filtered separately, but for
        # this test the only thing we assert is that the adapter wasn't
        # called.)
        db.close()


# ─── P2: collector + scanner produce identical source_trade_id ───────────────


class TestSharedNormalization:
    def test_collector_and_scanner_share_source_trade_ids(self, tmp_path, monkeypatch):
        """For the same raw row, run_scan (via live_ingest helper) and the
        collector path (via adapter.get_recent_trades) must produce the
        SAME ``source_trade_id``. Both go through
        ``deterministic_source_trade_id_v2``."""
        async def handler(request):
            import httpx
            return httpx.Response(200, json=_TWO_MARKETS)

        adapter = _make_adapter_with_transport(handler)

        async def via_helper() -> list[str]:
            trades = await live_ingest.fetch_recent_trades_for_market(
                adapter,
                market_source_id="0xMARKET_A",
                since=datetime.fromtimestamp(0, tz=timezone.utc),
            )
            return [t.source_trade_id for t in trades]

        async def via_direct() -> list[str]:
            trades = await adapter.get_recent_trades(
                market_source_id="0xMARKET_A",
                since=datetime.fromtimestamp(0, tz=timezone.utc),
            )
            return [t.source_trade_id for t in trades]

        ids_helper = asyncio.run(via_helper())
        ids_direct = asyncio.run(via_direct())

        # Both routes must produce the same set of IDs.
        assert sorted(ids_helper) == sorted(ids_direct), (
            f"id mismatch: helper={ids_helper} direct={ids_direct}"
        )
        # And the IDs must match the canonical v2 deterministic ID for
        # each raw row in the window that targets MARKET_A.
        market_a_raws = [r for r in _TWO_MARKETS if r.get("conditionId") == "0xMARKET_A"]
        expected = sorted(deterministic_source_trade_id_v2(r) for r in market_a_raws)
        assert sorted(ids_helper) == expected


# ─── P3: collector snapshot semantics ─────────────────────────────────────────


class TestCollectorSnapshotSemantics:
    """Round 7: collector fetches per market via ``GET /trades?market=<id>``.
    Each per-market request that hits the upstream is a real fetch and
    triggers one snapshot. Repeated calls for the SAME market hit the
    data-api again (no in-memory cache for per-market fetches) and each
    produces one snapshot — that is the honest accounting for the new
    fetch path.
    """

    def _wire_collector(self, handler):
        adapter = _make_adapter_with_transport(handler)
        collector = collect_mod.PolymarketCollector()
        collector._trade_adapter = adapter
        return collector, adapter

    def _market_filter_handler(self):
        """Return a handler that filters _TWO_MARKETS by the ``market`` query param."""
        async def handler(request):
            import httpx
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(str(request.url)).query)
            market = (qs.get("market") or [""])[0].lower()
            data = [
                r for r in _TWO_MARKETS
                if str(r.get("conditionId", "")).lower() == market
            ]
            return httpx.Response(200, json=data)
        return handler

    def test_collector_snapshots_once_per_market(self, tmp_path, monkeypatch):
        """A 2-market run produces 2 snapshots — one per per-market request."""
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p23.sqlite"))
        monkeypatch.setenv("POLYCOPY_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
        db = _empty_db(tmp_path)
        collector, _adapter = self._wire_collector(self._market_filter_handler())

        async def run():
            result = collect_mod.CollectionResult()
            await collector.collect_trades(db, "0xMARKET_A", result=result)
            await collector.collect_trades(db, "0xMARKET_B", result=result)
            return result

        result = asyncio.run(run())
        assert result.snapshots_saved == 2, (
            f"expected 1 snapshot per market (2 markets), got {result.snapshots_saved}"
        )
        n_snap = db.fetchone("SELECT COUNT(*) AS n FROM raw_snapshots")["n"]
        assert n_snap == 2, f"expected 2 snapshot rows, got {n_snap}"
        db.close()

    def test_collector_per_market_request_shape(self, tmp_path, monkeypatch):
        """Round 7 (P1 fix): the collector's request MUST include
        ``?market=<conditionId>``. Captured here for the new path."""
        captured: list[str] = []
        from urllib.parse import urlparse, parse_qs

        async def handler(request):
            import httpx
            qs = parse_qs(urlparse(str(request.url)).query)
            captured.append((qs.get("market") or [""])[0])
            market = (qs.get("market") or [""])[0].lower()
            data = [
                r for r in _TWO_MARKETS
                if str(r.get("conditionId", "")).lower() == market
            ]
            return httpx.Response(200, json=data)

        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p23.sqlite"))
        monkeypatch.setenv("POLYCOPY_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
        db = _empty_db(tmp_path)
        collector, _adapter = self._wire_collector(handler)

        async def run():
            result = collect_mod.CollectionResult()
            await collector.collect_trades(db, "0xMARKET_A", result=result)
            await collector.collect_trades(db, "0xMARKET_B", result=result)
            return result

        asyncio.run(run())
        assert captured, "expected at least one request"
        assert all(c for c in captured), f"every request must include market=<id>: {captured}"
        assert "0xMARKET_A" in captured, f"expected 0xMARKET_A in requests: {captured}"
        assert "0xMARKET_B" in captured, f"expected 0xMARKET_B in requests: {captured}"
        db.close()

    def test_collector_repeated_market_one_snapshot_each(self, tmp_path, monkeypatch):
        """Each call to ``collect_trades`` for the same market hits the API
        again (no global cache) and produces one snapshot per call."""
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p23.sqlite"))
        monkeypatch.setenv("POLYCOPY_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
        db = _empty_db(tmp_path)
        collector, _adapter = self._wire_collector(self._market_filter_handler())

        async def run():
            result = collect_mod.CollectionResult()
            t_first = await collector.collect_trades(db, "0xMARKET_A", result=result)
            t_second = await collector.collect_trades(db, "0xMARKET_A", result=result)
            return result, t_first, t_second

        result, t_first, t_second = asyncio.run(run())
        assert [t.source_trade_id for t in t_first] == [
            t.source_trade_id for t in t_second
        ]
        # Two calls → two snapshots. No global cache in round 7.
        assert result.snapshots_saved == 2, (
            f"expected 2 snapshots for 2 calls, got {result.snapshots_saved}"
        )
        db.close()

    def test_collector_snapshot_payload_is_market_window(self, tmp_path, monkeypatch):
        """The snapshot file written by ``collect_trades`` must contain
        only the rows for the requested market (round 7 per-market fetch)."""
        captured_payloads: list[list[dict]] = []
        from urllib.parse import urlparse, parse_qs

        async def handler(request):
            import httpx
            qs = parse_qs(urlparse(str(request.url)).query)
            market = (qs.get("market") or [""])[0].lower()
            data = [
                r for r in _TWO_MARKETS
                if str(r.get("conditionId", "")).lower() == market
            ]
            captured_payloads.append(data)
            return httpx.Response(200, json=data)

        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p23.sqlite"))
        snap_dir = tmp_path / "snapshots"
        monkeypatch.setenv("POLYCOPY_SNAPSHOT_DIR", str(snap_dir))
        db = _empty_db(tmp_path)
        collector, _adapter = self._wire_collector(handler)

        async def run():
            result = collect_mod.CollectionResult()
            await collector.collect_trades(db, "0xMARKET_A", result=result)
            return result

        result = asyncio.run(run())
        assert result.snapshots_saved == 1
        rows = db.fetchall(
            "SELECT file_path FROM raw_snapshots ORDER BY fetched_at"
        )
        assert len(rows) == 1
        file_path = Path(rows[0]["file_path"])
        assert file_path.exists(), f"snapshot file missing: {file_path}"
        persisted_payload = json.loads(file_path.read_text())
        assert persisted_payload == captured_payloads[0], (
            "snapshot file content does not match the per-market payload"
        )
        assert all(
            str(r.get("conditionId", "")).lower() == "0xmarket_a"
            for r in persisted_payload
        ), f"snapshot must contain only 0xMARKET_A rows: {persisted_payload}"
        db.close()


# ─── Shared-helper sanity tests ───────────────────────────────────────────────


class TestSharedHelper:
    """The shared ``scripts/_live_ingest.py`` module is the single
    construction point for the trade adapter. Sanity-check the helpers."""

    def test_build_trade_adapter_uses_settings(self, tmp_path, monkeypatch):
        # Force a fresh settings read so the env var is honored.
        monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p23.sqlite"))
        monkeypatch.setenv(
            "POLYCOPY_DATA_API_BASE_URL", "https://data-api.example.com",
        )
        # Reload settings to pick up the env var.
        s = get_settings(reload=True)
        a = live_ingest.build_trade_adapter(s)
        assert isinstance(a, PolymarketPublicAdapter)
        assert a.data_api_base_url == "https://data-api.example.com"

    def test_fetch_recent_trades_returns_empty_on_adapter_error(self):
        """If the underlying adapter raises, the shared helper returns []."""
        class _BoomAdapter:
            async def get_recent_trades(self, **kwargs):
                raise RuntimeError("network down")

        async def run():
            return await live_ingest.fetch_recent_trades_for_market(
                _BoomAdapter(),  # type: ignore[arg-type]
                market_source_id="0xX",
                since=datetime.fromtimestamp(0, tz=timezone.utc),
            )

        out = asyncio.run(run())
        assert out == []


# ─── Adapter: _fetch_global_window now returns tuple with fresh flag ─────────


class TestAdapterFreshFetchSignal:
    """The adapter's ``_fetch_global_window`` must return
    ``(window, fresh_fetch)`` so callers can distinguish a real upstream
    fetch from a cache hit. This is the API contract the collector relies on
    for P3."""

    def test_first_call_returns_fresh_true(self):
        async def handler(request):
            import httpx
            return httpx.Response(200, json=_TWO_MARKETS)

        adapter = _make_adapter_with_transport(handler)

        async def run():
            return await adapter._fetch_global_window()

        window, fresh = asyncio.run(run())
        assert isinstance(window, list)
        assert len(window) == 2
        assert fresh is True

    def test_second_call_within_window_returns_fresh_false(self):
        async def handler(request):
            import httpx
            return httpx.Response(200, json=_TWO_MARKETS)

        adapter = _make_adapter_with_transport(handler)

        async def run():
            w1, f1 = await adapter._fetch_global_window()
            w2, f2 = await adapter._fetch_global_window()
            return (w1, f1, w2, f2)

        w1, f1, w2, f2 = asyncio.run(run())
        assert f1 is True
        assert f2 is False
        assert w1 == w2

    def test_max_age_zero_forces_fresh_fetch(self):
        async def handler(request):
            import httpx
            return httpx.Response(200, json=_TWO_MARKETS)

        adapter = _make_adapter_with_transport(handler)

        async def run():
            w1, f1 = await adapter._fetch_global_window()
            w2, f2 = await adapter._fetch_global_window(max_age_seconds=0.0)
            return (f1, f2)

        f1, f2 = asyncio.run(run())
        assert f1 is True
        assert f2 is True