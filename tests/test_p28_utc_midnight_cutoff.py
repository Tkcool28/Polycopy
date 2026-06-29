"""Focused regressions for PR #3 UTC-midnight trade cutoff.

The collector default must not convert ``since=None`` into the current UTC
calendar day's midnight.  A no-``since`` collection should keep the bounded
per-market Data API window exactly as served by the adapter; explicit ``since``
still applies the lower-bound filter.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.adapters.polymarket import PolymarketPublicAdapter  # noqa: E402
from polycopy.config.settings import Settings, get_settings  # noqa: E402
from polycopy.db.database import Database  # noqa: E402
from polycopy.domain.market import Market, MarketOutcome  # noqa: E402
from polycopy.domain.source_trade import is_sentinel_trader_address  # noqa: E402

import scripts.collect_smart_money_data as collect_mod  # noqa: E402

MARKET_A = "0xMARKET_A"
MARKET_LOW_VOLUME = "0xLOW_VOLUME"
ATTRIBUTED = "0x1111111111111111111111111111111111111111"
ATTRIBUTED_2 = "0x2222222222222222222222222222222222222222"
BEFORE_MIDNIGHT = int(datetime(2026, 1, 1, 23, 59, 30, tzinfo=timezone.utc).timestamp())
AFTER_MIDNIGHT = int(datetime(2026, 1, 2, 0, 0, 30, tzinfo=timezone.utc).timestamp())


def _db(tmp_path: Path) -> Database:
    return Database(db_path=tmp_path / "p28.sqlite").connect()


def _market(source_id: str = MARKET_A, *, volume_24h: float = 20_000.0) -> Market:
    return Market(
        source_id=source_id,
        question=f"Test market {source_id}",
        outcomes=[MarketOutcome(label="Yes", price=0.55, volume=volume_24h)],
        source="polymarket",
        active=True,
        closed=False,
        resolved=False,
        volume_24h=volume_24h,
        fetched_at=datetime(2026, 1, 2, 0, 1, tzinfo=timezone.utc),
        is_sample=False,
    )


def _raw_trade(
    market: str = MARKET_A,
    suffix: str = "before",
    *,
    ts: int = BEFORE_MIDNIGHT,
    wallet: str | None = ATTRIBUTED,
    size: float = 3.0,
) -> dict:
    row = {
        "side": "BUY",
        "asset": f"asset-{suffix}",
        "conditionId": market,
        "size": size,
        "price": 0.42,
        "timestamp": ts,
        "outcome": "Yes",
        "transactionHash": f"0x{suffix}",
    }
    if wallet is not None:
        row["proxyWallet"] = wallet
    return row


def _adapter(handler) -> PolymarketPublicAdapter:
    adapter = PolymarketPublicAdapter(
        gamma_base_url="https://gamma.example.test",
        clob_base_url="https://clob.example.test",
        data_api_base_url="https://data-api.example.test",
        timeout=5.0,
        data_api_request_interval_seconds=0.0,
    )
    adapter._data_client = httpx.AsyncClient(  # noqa: SLF001 - intentional test wiring
        base_url=adapter.data_api_base_url,
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )
    return adapter


def _collector_with_adapter(adapter: PolymarketPublicAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p28.sqlite"))
    monkeypatch.setenv("POLYCOPY_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    settings = get_settings(reload=True)
    collector = collect_mod.PolymarketCollector(settings)
    collector._trade_adapter = adapter  # noqa: SLF001 - intentional test wiring
    return collector


async def _no_snapshot(*args, **kwargs) -> None:
    return None


class FrozenMidnightDateTime(datetime):
    """Freeze collector wall clock shortly after UTC midnight.

    Old buggy code used ``datetime.now(timezone.utc).replace(hour=0, ...)`` in
    ``collect_trades`` and would have dropped 2026-01-01T23:59:30Z when this
    clock reads 2026-01-02T00:01:00Z.
    """

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - datetime-compatible test shim
        fixed = datetime(2026, 1, 2, 0, 1, tzinfo=timezone.utc)
        return fixed if tz is not None else fixed.replace(tzinfo=None)


def _market_filter_handler(rows: list[dict], *, full_pages: bool = False):
    async def handler(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(str(request.url.query, "utf-8"))
        market = (qs.get("market") or [""])[0].lower()
        offset = int((qs.get("offset") or ["0"])[0])
        limit = int((qs.get("limit") or ["200"])[0])
        filtered = [r for r in rows if str(r.get("conditionId", "")).lower() == market]
        if full_pages:
            page = filtered[offset : offset + limit]
        else:
            page = filtered
        return httpx.Response(200, json=page)

    return handler


@pytest.mark.asyncio
async def test_collect_trades_without_since_passes_none_through(tmp_path, monkeypatch):
    seen_since = object()

    class RecordingAdapter:
        async def fetch_trades_for_market(self, **kwargs):
            nonlocal seen_since
            seen_since = kwargs["since"]
            from polycopy.adapters.polymarket import MarketTradeFetchResult
            return MarketTradeFetchResult(
                trades=[],
                status="complete",
                pages_fetched=0,
                rows_fetched=0,
                market_source_id=MARKET_A,
            )

    db = _db(tmp_path)
    collector = collect_mod.PolymarketCollector(Settings())
    collector._trade_adapter = RecordingAdapter()  # type: ignore[assignment]  # noqa: SLF001
    monkeypatch.setattr(collector, "_snapshot_market_first_page", _no_snapshot)
    try:
        await collector.collect_trades(db, MARKET_A)
    finally:
        db.close()

    assert seen_since is None


@pytest.mark.asyncio
async def test_trade_shortly_before_utc_midnight_retained_when_run_shortly_after_midnight(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(collect_mod, "datetime", FrozenMidnightDateTime)
    rows = [_raw_trade(MARKET_A, "pre_midnight", ts=BEFORE_MIDNIGHT)]
    adapter = _adapter(_market_filter_handler(rows))
    db = _db(tmp_path)
    collector = _collector_with_adapter(adapter, tmp_path, monkeypatch)
    try:
        persisted = await collector.collect_trades(db, MARKET_A)
    finally:
        await adapter.aclose()
        db.close()

    assert [t.source_trade_id for t in persisted]
    assert persisted[0].timestamp == datetime.fromtimestamp(BEFORE_MIDNIGHT, tz=timezone.utc)


@pytest.mark.asyncio
async def test_lower_volume_market_trade_from_previous_utc_date_retained(tmp_path, monkeypatch):
    rows = [_raw_trade(MARKET_LOW_VOLUME, "lowvol_prev_date", ts=BEFORE_MIDNIGHT)]
    adapter = _adapter(_market_filter_handler(rows))
    db = _db(tmp_path)
    collector = _collector_with_adapter(adapter, tmp_path, monkeypatch)
    # Low market volume is intentional: no default calendar cutoff or volume
    # gate may discard the previous-UTC-date trade.
    collector._asset_to_outcome[MARKET_LOW_VOLUME] = {}  # noqa: SLF001
    try:
        persisted = await collector.collect_trades(db, _market(MARKET_LOW_VOLUME, volume_24h=12.0).source_id)
    finally:
        await adapter.aclose()
        db.close()

    assert len(persisted) == 1
    assert persisted[0].market_source_id == MARKET_LOW_VOLUME


@pytest.mark.asyncio
async def test_explicit_since_still_filters_older_trades():
    rows = [
        _raw_trade(MARKET_A, "older", ts=BEFORE_MIDNIGHT),
        _raw_trade(MARKET_A, "newer", ts=AFTER_MIDNIGHT, wallet=ATTRIBUTED_2),
    ]
    adapter = _adapter(_market_filter_handler(rows))
    try:
        trades = await adapter.fetch_trades_for_market(
            MARKET_A,
            since=datetime.fromtimestamp(AFTER_MIDNIGHT, tz=timezone.utc),
            limit=10,
        )
    finally:
        await adapter.aclose()

    assert [t.trader_address for t in trades] == [ATTRIBUTED_2]
    assert trades[0].timestamp == datetime.fromtimestamp(AFTER_MIDNIGHT, tz=timezone.utc)


@pytest.mark.asyncio
async def test_since_none_does_not_disable_max_pages():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        qs = parse_qs(str(request.url.query, "utf-8"))
        offset = int(qs["offset"][0])
        limit = int(qs["limit"][0])
        assert qs["market"] == [MARKET_A]
        # Always return a full page so only max_pages can stop pagination.
        return httpx.Response(
            200,
            json=[_raw_trade(MARKET_A, f"page{offset + i}", ts=BEFORE_MIDNIGHT + offset + i) for i in range(limit)],
        )

    adapter = _adapter(handler)
    try:
        trades = await adapter.fetch_trades_for_market(MARKET_A, since=None, limit=2, max_pages=3)
    finally:
        await adapter.aclose()

    assert calls == 3
    assert len(trades) == 6


@pytest.mark.asyncio
async def test_since_none_does_not_disable_max_rows():
    rows = [_raw_trade(MARKET_A, f"row{i}", ts=BEFORE_MIDNIGHT + i) for i in range(5)]
    adapter = _adapter(_market_filter_handler(rows))
    try:
        trades = await adapter.fetch_trades_for_market(MARKET_A, since=None, limit=10, max_rows=2)
    finally:
        await adapter.aclose()

    assert len(trades) == 2


@pytest.mark.asyncio
async def test_exact_reruns_remain_idempotent(tmp_path, monkeypatch):
    rows = [_raw_trade(MARKET_A, "idempotent", ts=BEFORE_MIDNIGHT)]
    adapter = _adapter(_market_filter_handler(rows))
    db = _db(tmp_path)
    collector = _collector_with_adapter(adapter, tmp_path, monkeypatch)
    try:
        first = await collector.collect_trades(db, MARKET_A)
        second = await collector.collect_trades(db, MARKET_A)
        count_row = db.fetchone("SELECT COUNT(*) AS n FROM source_trades")
        assert count_row is not None
        n = count_row["n"]
    finally:
        await adapter.aclose()
        db.close()

    assert len(first) == 1
    assert len(second) == 1
    assert n == 1


@pytest.mark.asyncio
async def test_retained_prior_date_trades_persist_into_source_trades(tmp_path, monkeypatch):
    rows = [_raw_trade(MARKET_A, "persisted_prev_date", ts=BEFORE_MIDNIGHT)]
    adapter = _adapter(_market_filter_handler(rows))
    db = _db(tmp_path)
    collector = _collector_with_adapter(adapter, tmp_path, monkeypatch)
    try:
        await collector.collect_trades(db, MARKET_A)
        stored = db.fetchall(
            "SELECT market_source_id, trader_address, timestamp FROM source_trades"
        )
    finally:
        await adapter.aclose()
        db.close()

    assert len(stored) == 1
    assert stored[0]["market_source_id"] == MARKET_A
    assert stored[0]["trader_address"] == ATTRIBUTED
    assert stored[0]["timestamp"].startswith("2026-01-01T23:59:30")


@pytest.mark.asyncio
async def test_retained_attributed_trades_participate_in_wallet_discovery_and_scoring(
    tmp_path, monkeypatch
):
    rows = [_raw_trade(MARKET_A, "scored_prev_date", ts=BEFORE_MIDNIGHT, wallet=ATTRIBUTED)]
    adapter = _adapter(_market_filter_handler(rows))
    scored: list[str] = []

    async def fake_probe(self, db):
        return {"status": "ok", "wallet_attribution_available": True, "trades_returned": 1, "http_status": 200, "error": None}

    async def fake_collect_markets(self, db, limit, result):
        return [_market(MARKET_A)]

    def fake_evaluate_wallet(wallet_address: str, source: str, is_sample: bool):
        scored.append(wallet_address)
        return "score-id", "ok"

    async def fake_get_trade_adapter(self):
        return adapter

    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p28.sqlite"))
    monkeypatch.setenv("POLYCOPY_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    get_settings(reload=True)
    monkeypatch.setattr(collect_mod.PolymarketCollector, "probe_and_record_capability", fake_probe)
    monkeypatch.setattr(collect_mod.PolymarketCollector, "collect_markets", fake_collect_markets)
    monkeypatch.setattr(collect_mod.PolymarketCollector, "_get_trade_adapter", fake_get_trade_adapter)
    monkeypatch.setattr(collect_mod, "evaluate_wallet", fake_evaluate_wallet)

    db = _db(tmp_path)
    try:
        result = await collect_mod.run_collection(db, limit=1, skip_trades=False)
        wallet_rows = db.fetchall("SELECT address FROM wallets")
    finally:
        await adapter.aclose()
        db.close()

    assert result.trades_fetched == 1
    assert result.wallets_discovered == 1
    real_wallet_rows = [
        r for r in wallet_rows if not is_sentinel_trader_address(r["address"])
    ]
    assert [r["address"] for r in real_wallet_rows] == [ATTRIBUTED]
    assert scored == [ATTRIBUTED]


@pytest.mark.asyncio
async def test_anonymous_prior_date_trades_persist_null_and_do_not_become_wallets(
    tmp_path, monkeypatch
):
    rows = [_raw_trade(MARKET_A, "anon_prev_date", ts=BEFORE_MIDNIGHT, wallet=None)]
    adapter = _adapter(_market_filter_handler(rows))
    scored: list[str] = []

    async def fake_probe(self, db):
        return {"status": "ok", "wallet_attribution_available": False, "trades_returned": 1, "http_status": 200, "error": None}

    async def fake_collect_markets(self, db, limit, result):
        return [_market(MARKET_A)]

    def fake_evaluate_wallet(wallet_address: str, source: str, is_sample: bool):
        scored.append(wallet_address)
        return "score-id", "ok"

    async def fake_get_trade_adapter(self):
        return adapter

    monkeypatch.setenv("POLYCOPY_DB_PATH", str(tmp_path / "p28.sqlite"))
    monkeypatch.setenv("POLYCOPY_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    get_settings(reload=True)
    monkeypatch.setattr(collect_mod.PolymarketCollector, "probe_and_record_capability", fake_probe)
    monkeypatch.setattr(collect_mod.PolymarketCollector, "collect_markets", fake_collect_markets)
    monkeypatch.setattr(collect_mod.PolymarketCollector, "_get_trade_adapter", fake_get_trade_adapter)
    monkeypatch.setattr(collect_mod, "evaluate_wallet", fake_evaluate_wallet)

    db = _db(tmp_path)
    try:
        result = await collect_mod.run_collection(db, limit=1, skip_trades=False)
        stored = db.fetchall("SELECT trader_address FROM source_trades")
        wallet_rows = db.fetchall("SELECT address FROM wallets")
    finally:
        await adapter.aclose()
        db.close()

    assert result.trades_fetched == 1
    assert result.anonymous_trades_skipped == 1
    assert [r["trader_address"] for r in stored] == [None]
    assert wallet_rows == []
    assert scored == []
