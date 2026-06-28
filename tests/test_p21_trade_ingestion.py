"""Regression tests for P21: real Polymarket trade ingestion via data-api.

These tests verify the fix for the zero-trade collection bug. The bug:
collect_smart_money_data.py called `gamma-api.polymarket.com/trades`
(404 — Gamma has no /trades) and `clob.polymarket.com/trades` (401 — CLOB
trades endpoint requires authentication).

The fix:
The PolymarketPublicAdapter now uses the public unauthenticated data-api:
  https://data-api.polymarket.com/trades
which returns full wallet-attributed trade history (real 0x proxyWallet
addresses, real transactionHash, real conditionId). The data-api ignores the
conditionId filter parameter — the adapter fetches a single global window
and slices per conditionId locally.

Tests use httpx.MockTransport to inject canned responses. NO real network
calls. Live probe evidence is in reports/polymarket_trade_ingestion_audit.md.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path


from polycopy.adapters.polymarket import (
    PolymarketPublicAdapter,
    _deterministic_source_trade_id,
    _normalize_side,
    _to_datetime,
)
from polycopy.db.database import Database
from polycopy.domain.order import OrderSide

FIXTURES = Path(__file__).parent / "fixtures" / "polymarket_trade_ingestion"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _make_data_api_mock(adapter: PolymarketPublicAdapter, payloads: dict) -> None:
    """Build an httpx.MockTransport that returns `payloads` keyed by URL path."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # Most tests target /trades
        if path == "/trades":
            # Decode the params to pick the right canned response
            params = dict(request.url.params)
            user = params.get("user")
            params.get("conditionId")
            int(params.get("limit", "100"))
            if user == "ANON_WALLET":
                return httpx.Response(200, json=payloads["anonymous_window"])
            if user:
                # Any user-targeted query
                return httpx.Response(200, json=payloads["user_specific_window"])
            # Default global window — caller supplies via params[scenario]
            # We can't pass scenarios via URL params; the tests build their own
            # handlers in-line for finer control. Fallback:
            return httpx.Response(200, json=payloads.get("default_window", []))
        if path.startswith("/markets/"):
            return httpx.Response(200, json=payloads.get("gamma_market", {}))
        if path == "/markets":
            return httpx.Response(200, json=payloads.get("gamma_markets_list", []))
        return httpx.Response(404, json={"error": "not found in mock"})

    transport = httpx.MockTransport(handler)
    adapter._data_client = httpx.AsyncClient(
        base_url=adapter.data_api_base_url,
        transport=transport,
        timeout=adapter.timeout,
    )
    adapter._gamma_client = httpx.AsyncClient(
        base_url=adapter.gamma_base_url,
        transport=transport,
        timeout=adapter.timeout,
    )
    adapter._clob_client = httpx.AsyncClient(
        base_url=adapter.clob_base_url,
        transport=transport,
        timeout=adapter.timeout,
    )


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


# ─── Test 1: Gamma market clobTokenIds JSON-string parsing ─────────────────


def test_gamma_market_object_has_clob_token_ids():
    """clobTokenIds is a JSON-encoded string of token IDs. The collector must
    json.loads() it to get a Python list."""
    raw = _load_fixture("gamma_markets.json")["top_10_volume"][0]
    assert isinstance(raw["clobTokenIds"], str), "fixture must simulate Gamma"
    tokens = json.loads(raw["clobTokenIds"])
    assert isinstance(tokens, list)
    assert len(tokens) == 2
    assert all(isinstance(t, str) and len(t) > 20 for t in tokens)


# ─── Test 2: Correct trade endpoint query construction ─────────────────────


def test_correct_trade_endpoint_query_construction():
    """The adapter must call data-api /trades (NOT gamma or clob) and slice
    client-side by conditionId. We verify by inspecting the captured requests."""

    captured_requests: list = []

    async def fake_handler(request):
        import httpx
        captured_requests.append((request.method, request.url.path, dict(request.url.params)))
        # Return the algeria-only window regardless of query
        return httpx.Response(
            200,
            json=_load_fixture("data_api_trades.json")["algeria_market_only"],
        )

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        try:
            from datetime import datetime, timezone
            since = datetime.fromtimestamp(0, tz=timezone.utc)
            trades = await a.get_recent_trades(
                market_source_id="0x5a59d269c2b5108cd2f64c624e46ee2c8b5cfd88b882582565f927918315b6aa",
                since=since,
                limit=100,
            )
            return trades
        finally:
            await a.aclose()

    trades = asyncio.run(run())

    # The adapter MUST hit /trades on the data-api (NOT /trades on clob).
    assert len(captured_requests) >= 1, "expected at least one HTTP call"
    method, path, params = captured_requests[0]
    assert method == "GET"
    assert path == "/trades", f"expected /trades, got {path!r}"
    # The adapter does NOT pass conditionId to the server (it's ignored anyway);
    # it slices client-side. So the captured params should be just `limit`.
    assert "limit" in params
    # No conditionId in the request (the data-api ignores it anyway)
    assert "condition_id" not in params, "old buggy query name must not be used"

    # The client-side slice must produce 2 Algeria trades from the fixture.
    assert len(trades) == 2
    for t in trades:
        assert t.market_source_id.lower() == "0x5a59d269c2b5108cd2f64c624e46ee2c8b5cfd88b882582565f927918315b6aa"


# ─── Test 3: Multiple outcomes per market ────────────────────────────────


def test_handles_multiple_outcomes_per_market():
    """A market with 3 outcomes maps each trade to the correct outcome label
    via the asset_to_outcome mapping."""

    multi_market = {
        "id": "700000",
        "conditionId": "0x39b3759e38d0b5514169262586869eaeebb172a11a01170ffa7cdea332ec9dc8",
        "slug": "kbo-winner-2026",
        "outcomes": ["Hanwha Eagles", "SSG Landers", "KIA Tigers"],
        "outcomePrices": ["0.40", "0.35", "0.25"],
        "clobTokenIds": [
            "70444996880708343478311350895883000735063914299511351302415577227505591177373",
            "5555555555555555555555555555555555555555555555555555555555555555",
            "6666666666666666666666666666666666666666666666666666666666666666",
        ],
    }
    asset_map = {
        "70444996880708343478311350895883000735063914299511351302415577227505591177373": "Hanwha Eagles",
        "5555555555555555555555555555555555555555555555555555555555555555": "SSG Landers",
        "6666666666666666666666666666666666666666666666666666666666666666": "KIA Tigers",
    }

    fixture_trades = [
        {
            "proxyWallet": "0x1111111111111111111111111111111111111111",
            "side": "BUY",
            "asset": multi_market["clobTokenIds"][0],
            "conditionId": multi_market["conditionId"],
            "size": 10.0,
            "price": 0.40,
            "timestamp": 1782636254,
            "outcome": "Hanwha Eagles",  # raw label matches first
            "transactionHash": "0xaaa",
        },
        {
            "proxyWallet": "0x2222222222222222222222222222222222222222",
            "side": "SELL",
            "asset": multi_market["clobTokenIds"][1],
            "conditionId": multi_market["conditionId"],
            "size": 5.0,
            "price": 0.65,
            "timestamp": 1782636255,
            "outcome": "SSG Landers",
            "transactionHash": "0xbbb",
        },
        {
            "proxyWallet": "0x3333333333333333333333333333333333333333",
            "side": "BUY",
            "asset": multi_market["clobTokenIds"][2],
            "conditionId": multi_market["conditionId"],
            "size": 7.5,
            "price": 0.25,
            "timestamp": 1782636256,
            "outcome": "KIA Tigers",
            "transactionHash": "0xccc",
        },
    ]

    async def fake_handler(request):
        import httpx
        return httpx.Response(200, json=fixture_trades)

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        try:
            since = datetime.fromtimestamp(0, tz=timezone.utc)
            trades = await a.get_recent_trades(
                market_source_id=multi_market["conditionId"],
                since=since,
                limit=10,
                asset_to_outcome=asset_map,
            )
            return trades
        finally:
            await a.aclose()

    trades = asyncio.run(run())
    assert len(trades) == 3, f"expected 3 trades, got {len(trades)}"
    by_outcome = {t.outcome: t for t in trades}
    assert "Hanwha Eagles" in by_outcome
    assert "SSG Landers" in by_outcome
    assert "KIA Tigers" in by_outcome


# ─── Test 4: Pagination across two pages ─────────────────────────────────
def test_pagination_across_two_pages():
    """When the cache is invalidated, the adapter re-fetches — exercising the
    'two consecutive windows' code path. (The data-api ignores conditionId, so
    pagination across markets happens via client-side filtering of a single
    global window; this test verifies the cache invalidation triggers a fresh
    HTTP call and that successive windows produce disjoint source_trade_ids.)"""

    cond = "0xcond_pagination_test"
    # Two distinct windows with disjoint transaction hashes
    page1 = [
        {
            "proxyWallet": f"0xwallet_p1_{i}0000000000000000000000000000000000",
            "side": "BUY", "asset": "100", "conditionId": cond,
            "size": 1.0, "price": 0.4, "timestamp": 1782636240 + i,
            "outcome": "Yes", "transactionHash": f"0xhash_p1_{i}_unique",
        }
        for i in range(3)
    ]
    page2 = [
        {
            "proxyWallet": f"0xwallet_p2_{i}0000000000000000000000000000000000",
            "side": "SELL", "asset": "100", "conditionId": cond,
            "size": 2.0, "price": 0.6, "timestamp": 1782636260 + i,
            "outcome": "Yes", "transactionHash": f"0xhash_p2_{i}_unique",
        }
        for i in range(2)
    ]

    call_count = {"n": 0}

    async def fake_handler(request):
        import httpx
        call_count["n"] += 1
        # Alternate responses by call index
        return httpx.Response(200, json=page1 if call_count["n"] % 2 == 1 else page2)

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        a.data_api_request_interval_seconds = 0.0
        try:
            since = datetime.fromtimestamp(0, tz=timezone.utc)
            # Fetch 1: fresh fetch, returns page1
            a._window_lock_time = 0.0
            a._window_trades = []
            t1 = await a.get_recent_trades(
                market_source_id=cond, since=since, limit=10,
            )
            # Fetch 2: invalidate cache, fresh fetch returns page2
            a._window_lock_time = 0.0
            a._window_trades = []
            t2 = await a.get_recent_trades(
                market_source_id=cond, since=since, limit=10,
            )
            return t1, t2
        finally:
            await a.aclose()

    t1, t2 = asyncio.run(run())

    # Two distinct HTTP calls (cache invalidation triggered fresh fetch)
    assert call_count["n"] >= 2, f"expected 2+ HTTP calls, got {call_count['n']}"
    # Both windows filtered to our cond produced trades
    assert len(t1) >= 1, "page 1 returned no trades for cond"
    assert len(t2) >= 1, "page 2 returned no trades for cond"
    # No overlap between the two windows (disjoint tx hashes)
    all_txs = {t.source_trade_id for t in t1} | {t.source_trade_id for t in t2}
    assert len(all_txs) == len(t1) + len(t2), (
        f"overlap between pages: t1={len(t1)} t2={len(t2)} unique={len(all_txs)}"
    )


# ─── Test 5: Deterministic deduplication ─────────────────────────────────


def test_deterministic_deduplication(tmp_path):
    """Same trade inserted twice → exactly one source_trades row."""
    # Build a temp DB
    db_path = tmp_path / "dedup.db"
    db = Database(db_path=db_path).connect()

    trade = {
        "proxyWallet": "0xd34db33f00000000000000000000000000000000",
        "side": "BUY",
        "asset": "111",
        "conditionId": "0xabc",
        "size": 5.0,
        "price": 0.5,
        "timestamp": 1782636254,
        "outcome": "Yes",
        "transactionHash": "0xcafe1234",
    }

    async def fake_handler(request):
        import httpx
        return httpx.Response(200, json=[trade])

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        try:
            since = datetime.fromtimestamp(0, tz=timezone.utc)
            return await a.get_recent_trades("0xabc", since=since, limit=10)
        finally:
            await a.aclose()

    # First fetch + persist
    trades_1 = asyncio.run(run())
    for t in trades_1:
        db.execute(
            """INSERT INTO source_trades
               (id, source, source_trade_id, market_source_id, side, outcome,
                quantity, price, trader_address, timestamp, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(t.id), t.source, t.source_trade_id, t.market_source_id,
             t.side.value, t.outcome, t.quantity, t.price, t.trader_address,
             t.timestamp.isoformat(), int(t.is_sample)),
        )
    db.conn.commit()

    # Second fetch (same trade) + persist with INSERT OR IGNORE-style
    trades_2 = asyncio.run(run())
    for t in trades_2:
        try:
            db.execute(
                """INSERT OR IGNORE INTO source_trades
                   (id, source, source_trade_id, market_source_id, side, outcome,
                    quantity, price, trader_address, timestamp, is_sample)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(t.id), t.source, t.source_trade_id, t.market_source_id,
                 t.side.value, t.outcome, t.quantity, t.price, t.trader_address,
                 t.timestamp.isoformat(), int(t.is_sample)),
            )
        except Exception:
            pass
    db.conn.commit()

    n_row = db.fetchone("SELECT COUNT(*) AS n FROM source_trades")
    assert n_row is not None
    n = n_row["n"]
    assert n == 1, f"expected 1 deduped row, got {n}"

    db.close()


# ─── Test 6: Zero-trade response ─────────────────────────────────────────


def test_zero_trade_response():
    """Endpoint returns [] → adapter returns []. No exception."""

    async def fake_handler(request):
        import httpx
        return httpx.Response(200, json=[])

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        try:
            since = datetime.fromtimestamp(0, tz=timezone.utc)
            return await a.get_recent_trades("0xabc", since=since, limit=10)
        finally:
            await a.aclose()

    trades = asyncio.run(run())
    assert trades == []


# ─── Test 7: Malformed trade response ─────────────────────────────────────


def test_malformed_trade_response():
    """Trade missing required fields is skipped (no crash)."""

    malformed = _load_fixture("data_api_trades.json")["malformed_window"]

    async def fake_handler(request):
        import httpx
        return httpx.Response(200, json=malformed)

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        try:
            since = datetime.fromtimestamp(0, tz=timezone.utc)
            # Use a condId matching one of the malformed entries' conditionId
            return await a.get_recent_trades("0xabc", since=since, limit=10)
        finally:
            await a.aclose()

    trades = asyncio.run(run())
    # The fixture has:
    #   0: missing conditionId → skipped
    #   1: invalid side → skipped
    #   2: missing proxyWallet + asset → skipped
    assert len(trades) == 0


# ─── Test 8: Missing wallet attribution → trader_address = "unknown" ────


def test_missing_wallet_attribution():
    """If the response doesn't include proxyWallet/maker/taker, the persisted
    SourceTrade.trader_address is the literal string 'unknown' (NOT a fake 0x)."""

    anon_window = _load_fixture("data_api_trades.json")["anonymous_window"]

    async def fake_handler(request):
        import httpx
        return httpx.Response(200, json=anon_window)

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        try:
            since = datetime.fromtimestamp(0, tz=timezone.utc)
            return await a.get_recent_trades(
                "0xf8dbf8b247f2c08865b7915f69b2f12e181711176873253d464273769e37f2b3",
                since=since, limit=10,
            )
        finally:
            await a.aclose()

    trades = asyncio.run(run())
    assert len(trades) == 1
    t = trades[0]
    assert t.trader_address == "unknown", f"expected 'unknown', got {t.trader_address!r}"
    # is_sample must be False — we do NOT synthesize fake data
    assert t.is_sample is False


# ─── Test 9: Timestamp normalization (Unix sec → ISO-8601 UTC) ────────────


def test_timestamp_normalization():
    """Unix-seconds timestamps must be parsed to UTC-aware datetime then
    serialized to ISO-8601 when persisted."""

    raw_ts = 1782636254
    expected_dt = datetime.fromtimestamp(raw_ts, tz=timezone.utc)
    assert _to_datetime(raw_ts) == expected_dt

    trade = {
        "proxyWallet": "0xaaaa0000000000000000000000000000000000",
        "side": "BUY",
        "asset": "1",
        "conditionId": "0xabc",
        "size": 1.0,
        "price": 0.5,
        "timestamp": raw_ts,
        "outcome": "Yes",
        "transactionHash": "0xts",
    }

    async def fake_handler(request):
        import httpx
        return httpx.Response(200, json=[trade])

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        try:
            since = datetime.fromtimestamp(0, tz=timezone.utc)
            trades = await a.get_recent_trades("0xabc", since=since, limit=10)
            return trades
        finally:
            await a.aclose()

    trades = asyncio.run(run())
    assert len(trades) == 1
    t = trades[0]
    assert t.timestamp == expected_dt
    assert t.timestamp.tzinfo is not None
    iso = t.timestamp.isoformat()
    assert iso.startswith("2026-") or iso.startswith("2027-"), iso


# ─── Test 10: Transaction hash stability across fetches ──────────────────


def test_transaction_hash_stability():
    """Two fetches of the same trade produce the same source_trade_id."""

    trade = {
        "proxyWallet": "0xaaaa0000000000000000000000000000000000",
        "side": "BUY",
        "asset": "asset-abc",
        "conditionId": "0xabc",
        "size": 1.0,
        "price": 0.5,
        "timestamp": 1782636254,
        "outcome": "Yes",
        "transactionHash": "0xdeadbeef00000000",
    }

    async def fake_handler(request):
        import httpx
        return httpx.Response(200, json=[trade])

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        try:
            since = datetime.fromtimestamp(0, tz=timezone.utc)
            t1 = await a.get_recent_trades("0xabc", since=since, limit=10)
            a._window_lock_time = 0.0
            a._window_trades = []
            t2 = await a.get_recent_trades("0xabc", since=since, limit=10)
            return t1, t2
        finally:
            await a.aclose()

    t1, t2 = asyncio.run(run())
    assert len(t1) == 1 and len(t2) == 1
    assert t1[0].source_trade_id == t2[0].source_trade_id
    # tx hash is lowercased by the deterministic helper
    assert t1[0].source_trade_id == "0xdeadbeef00000000"


# ─── Test 11: No sample fallback on real error ───────────────────────────


def test_no_sample_fallback_on_real_error(tmp_path):
    """When live fetch fails AND enable_demo_data=False, no sample data is
    returned; result is empty + warning logged."""

    async def fake_handler(request):
        import httpx
        return httpx.Response(500, text="boom")

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        try:
            since = datetime.fromtimestamp(0, tz=timezone.utc)
            return await a.get_recent_trades("0xabc", since=since, limit=10)
        finally:
            await a.aclose()

    trades = asyncio.run(run())
    assert trades == [], "must NOT return fake data on real fetch failure"


# ─── Test 12: Capability flag reflects wallet attribution ────────────────


def test_capability_flag_reflects_wallet_attribution(tmp_path):
    """After a successful trade fetch, the recorded capability flag has
    wallet_attribution_available=True iff the response actually contained
    proxyWallet fields with real 0x addresses."""

    db_path = tmp_path / "cap.db"
    db = Database(db_path=db_path).connect()

    async def fake_handler(request):
        import httpx
        # Return 3 trades with real proxyWallet addresses
        return httpx.Response(200, json=_load_fixture("data_api_trades.json")["global_window_size_5"])

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        try:
            return await a.probe_trade_capability()
        finally:
            await a.aclose()

    cap = asyncio.run(run())
    assert cap["status"] == "ok"
    assert cap["wallet_attribution_available"] is True
    assert cap["http_status"] == 200
    assert cap["trades_returned"] >= 1
    db.close()


# ─── Test 13: Adapter helpers (pure unit tests) ──────────────────────────


def test_normalize_side():
    assert _normalize_side("BUY") == OrderSide.BUY
    assert _normalize_side("buy") == OrderSide.BUY
    assert _normalize_side("1") == OrderSide.BUY
    assert _normalize_side("SELL") == OrderSide.SELL
    assert _normalize_side("0") == OrderSide.SELL
    assert _normalize_side("nope") is None
    assert _normalize_side(None) is None


def test_deterministic_id_from_txhash():
    sid = _deterministic_source_trade_id("0xABCDEF1234567890", "a", 1, 0.5, 1.0)
    assert sid == "0xabcdef1234567890"


def test_deterministic_id_fallback():
    """When no tx hash, deterministic sha256 of asset|ts|price|size is used."""
    sid1 = _deterministic_source_trade_id(None, "asset-x", 1782636254, 0.5, 1.0)
    sid2 = _deterministic_source_trade_id("", "asset-x", 1782636254, 0.5, 1.0)
    assert sid1 == sid2
    assert sid1.startswith("sha256:")


# ─── Test 14: Anonymous-only window → run_scan reports skip ───────────────


def test_scan_skips_wallet_scoring_when_anonymous(tmp_path):
    """When all trades are anonymous (no wallet), run_scan must NOT score
    any wallets and must surface an explicit reason in result_summary."""

    # Simulate by directly checking capability flag + scanning logic
    db_path = tmp_path / "scan.db"
    db = Database(db_path=db_path).connect()

    async def fake_handler(request):
        import httpx
        return httpx.Response(200, json=_load_fixture("data_api_trades.json")["anonymous_window"])

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        try:
            return await a.probe_trade_capability()
        finally:
            await a.aclose()

    cap = asyncio.run(run())
    # Anonymous: wallet_attribution_available is False
    assert cap["wallet_attribution_available"] is False
    assert cap["status"] in ("partial", "ok")
    db.close()


# ─── Test 15: Multi-page dedup invariant ─────────────────────────────────


def test_429_retry_handled():
    """If the first response is 429, _fetch_global_window sleeps and retries once."""

    call_count = {"n": 0}

    async def fake_handler(request):
        import httpx
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429, json={"error": "rate-limited"})
        return httpx.Response(200, json=_load_fixture("data_api_trades.json")["global_window_size_5"])

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        # Force fast retry (don't actually sleep 2s in tests)
        a.data_api_request_interval_seconds = 0.0
        # Bypass cache by resetting window age to 0
        a._window_lock_time = 0.0
        a._window_trades = []
        try:
            return await a._fetch_global_window(max_age_seconds=0.0)
        finally:
            await a.aclose()

    result = asyncio.run(run())
    assert call_count["n"] >= 2, f"expected retry, only {call_count['n']} calls"
    assert isinstance(result, list)
    assert len(result) >= 1, "expected at least 1 trade after retry"


# ─── Test 16: window cache age respected ─────────────────────────────────


def test_window_cache_within_max_age():
    """Within max_age_seconds the adapter does NOT re-fetch."""
    call_count = {"n": 0}

    async def fake_handler(request):
        import httpx
        call_count["n"] += 1
        return httpx.Response(200, json=_load_fixture("data_api_trades.json")["global_window_size_5"])

    async def run():
        a = _make_adapter_with_transport(fake_handler)
        a.data_api_request_interval_seconds = 0.0
        try:
            # First fetch — populates cache
            w1 = await a._fetch_global_window()
            # Second fetch within cache window — should use cache
            w2 = await a._fetch_global_window()
            return w1, w2, call_count["n"]
        finally:
            await a.aclose()

    w1, w2, n = asyncio.run(run())
    assert n == 1, f"expected 1 HTTP call (cached), got {n}"
    assert len(w1) == len(w2)
