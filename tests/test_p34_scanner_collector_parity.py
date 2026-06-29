"""Round 11 / Codex P2 PRRT_kwDOTG4Cf86M7Xbp — scanner/collector parity tests.

The Codex finding is that the scanner (``scripts/run_scan._fetch_trades``) and
the collector (``scripts.collect_smart_money_data.PolymarketCollector.collect_trades``)
disagree on the canonical ``outcome`` label, the ``source_trade_id``, the
persisted ``source_trades`` row, the dedup outcome, and the scoring input
for any raw Data API row whose ``outcome`` field is stale, denormalized, or
mismatched with the row's ``asset``. The two paths must produce identical
``SourceTrade`` objects for the same raw row + asset-to-outcome map.

These tests pin the contract that the fix must hold. They use a tiny
in-memory mock adapter that returns a canned payload to both paths and
asserts elementwise equality of the normalized ``SourceTrade`` lists and
the persisted ``source_trades`` rows.

Test classes
------------
* :class:`TestOutcomeMapParity` — same map, same raw row → same normalized trade
* :class:`TestOutcomeMapFallbackParity` — missing/empty map and unknown asset
  both fall back to the raw outcome identically
* :class:`TestSourceTradeIdParity` — same raw row → same deterministic id
  regardless of which path produced it
* :class:`TestPersistenceParity` — both paths persist identical rows;
  repeated invocations are idempotent; one path cannot overwrite the other
* :class:`TestAssetMapBuilding` — both paths build the asset map from the
  same Gamma ``clobTokenIds`` / ``outcomes`` payload
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.adapters.polymarket import (  # noqa: E402
    PolymarketPublicAdapter,
    deterministic_source_trade_id_v2,
)
from polycopy.db.database import Database  # noqa: E402
from polycopy.domain.market import Market, MarketOutcome  # noqa: E402
from polycopy.domain.source_trade import SourceTrade  # noqa: E402

import scripts.collect_smart_money_data as collect_mod  # noqa: E402
import scripts.run_scan as run_scan_module  # noqa: E402
import scripts._live_ingest as live_ingest  # noqa: E402


# ─── Helpers ──────────────────────────────────────────────────────────────


def _random_hex(n: int) -> str:
    import secrets

    return secrets.token_hex(n)


def _raw_with_outcome_mismatch(
    wallet: str = "0xaaaa0000000000000000000000000000000001",
    raw_outcome: str = "Denormalized",
    correct_outcome: str = "Yes",
    asset: str = "asset-yes",
    condition: str = "0xMARKET_A",
) -> dict:
    """A raw Data API row whose ``outcome`` is WRONG for its asset.

    The ``asset`` corresponds to the YES outcome (per the asset-to-outcome
    map), but the raw ``outcome`` field says "Denormalized". After the
    asset-map rewrite, the normalized SourceTrade should have
    ``outcome="Yes"``.
    """
    return {
        "proxyWallet": wallet,
        "side": "BUY",
        "asset": asset,
        "conditionId": condition,
        "size": 10.0,
        "price": 0.55,
        "timestamp": 1_782_636_254,
        "outcome": raw_outcome,
        "transactionHash": f"0x{_random_hex(8)}",
    }


def _adapter_with_raw(raws: list[dict]) -> PolymarketPublicAdapter:
    """Adapter wired to a MockTransport that returns the same raws for every
    request to /trades. The data_api_window_size is reduced so a single
    page returns all rows."""
    import httpx

    a = PolymarketPublicAdapter(
        gamma_base_url="https://gamma.example.test",
        clob_base_url="https://clob.example.test",
        data_api_base_url="https://data-api.example.test",
        timeout=5.0,
        rate_limit_rps=5.0,
        data_api_window_size=1000,
        data_api_request_interval_seconds=0.0,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qs
        qs = parse_qs(str(request.url.query, "utf-8"))
        market = (qs.get("market") or [""])[0].lower()
        matched = [r for r in raws if str(r.get("conditionId", "")).lower() == market]
        return httpx.Response(200, json=matched)

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


def _asset_to_outcome_for(market: Market) -> dict[str, str]:
    """Build the asset-to-outcome map the Gamma payload would yield for
    ``market``. Mirrors the construction in
    :func:`scripts.run_scan._build_asset_to_outcome_map` and
    :meth:`PolymarketCollector._build_asset_to_outcome_map`."""
    return {"asset-yes": "Yes", "asset-no": "No"}


def _scanner_fetch(
    adapter: PolymarketPublicAdapter,
    market: Market,
    asset_to_outcome: dict[str, str] | None,
):
    """Drive the scanner path: ``fetch_recent_trades_for_market`` with the
    asset map threaded through. Returns the normalized trades (the
    ``MarketTradeFetchResult`` is list-iterable, so we treat it as a list)."""
    return asyncio_run(
        live_ingest.fetch_recent_trades_for_market(
            adapter,
            market_source_id=market.source_id,
            since=datetime.fromtimestamp(0, tz=timezone.utc),
            asset_to_outcome=asset_to_outcome,
        )
    )


def _collector_fetch(
    adapter: PolymarketPublicAdapter,
    market: Market,
    asset_to_outcome: dict[str, str] | None,
):
    """Drive the collector path: ``adapter.fetch_trades_for_market`` with the
    same asset map. Returns the normalized trades."""
    return asyncio_run(
        adapter.fetch_trades_for_market(
            market_source_id=market.source_id,
            since=datetime.fromtimestamp(0, tz=timezone.utc),
            asset_to_outcome=asset_to_outcome,
        )
    )


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def _trade_dicts_equal(a: SourceTrade, b: SourceTrade) -> bool:
    """Equality of every distinguishing SourceTrade field."""
    return (
        a.source == b.source
        and a.source_trade_id == b.source_trade_id
        and a.market_source_id == b.market_source_id
        and a.outcome == b.outcome
        and str(a.side.value if hasattr(a.side, "value") else a.side)
        == str(b.side.value if hasattr(b.side, "value") else b.side)
        and a.quantity == b.quantity
        and a.price == b.price
        and a.trader_address == b.trader_address
        and a.timestamp == b.timestamp
        and a.is_sample == b.is_sample
    )


# ─── Tests ────────────────────────────────────────────────────────────────


class TestOutcomeMapParity:
    """The asset-to-outcome map must rewrite a denormalized raw outcome
    identically in the scanner and collector paths."""

    def test_collector_and_scanner_rewrite_denormalized_outcome_identically(self):
        raw = _raw_with_outcome_mismatch(
            wallet="0xaaaa0000000000000000000000000000000001",
            raw_outcome="Denormalized",
            correct_outcome="Yes",
            asset="asset-yes",
        )
        market = _market("0xMARKET_A")
        adapter_scanner = _adapter_with_raw([raw])
        adapter_collector = _adapter_with_raw([raw])
        asset_to_outcome = _asset_to_outcome_for(market)

        scanner_trades = _scanner_fetch(adapter_scanner, market, asset_to_outcome)
        collector_trades = _collector_fetch(adapter_collector, market, asset_to_outcome)

        assert len(scanner_trades) == 1
        assert len(collector_trades) == 1
        # Both rewrite "Denormalized" → "Yes".
        assert scanner_trades[0].outcome == "Yes"
        assert collector_trades[0].outcome == "Yes"
        # The two SourceTrade objects must be byte-equal in every distinguishing field.
        assert _trade_dicts_equal(scanner_trades[0], collector_trades[0])


class TestOutcomeMapFallbackParity:
    """Missing map / empty map / unknown asset must all fall back to the
    raw outcome identically in both paths."""

    def test_missing_map_falls_back_to_raw_outcome_identically(self):
        raw = _raw_with_outcome_mismatch(
            raw_outcome="OriginalLabel",
            asset="asset-yes",
        )
        market = _market("0xMARKET_A")
        adapter_scanner = _adapter_with_raw([raw])
        adapter_collector = _adapter_with_raw([raw])

        scanner_trades = _scanner_fetch(adapter_scanner, market, asset_to_outcome=None)
        collector_trades = _collector_fetch(adapter_collector, market, asset_to_outcome={})

        # Both fall back to the raw outcome label.
        assert scanner_trades[0].outcome == "OriginalLabel"
        assert collector_trades[0].outcome == "OriginalLabel"
        assert _trade_dicts_equal(scanner_trades[0], collector_trades[0])

    def test_empty_map_falls_back_to_raw_outcome_identically(self):
        raw = _raw_with_outcome_mismatch(raw_outcome="OriginalLabel")
        market = _market("0xMARKET_A")
        adapter_scanner = _adapter_with_raw([raw])
        adapter_collector = _adapter_with_raw([raw])

        scanner_trades = _scanner_fetch(adapter_scanner, market, asset_to_outcome={})
        collector_trades = _collector_fetch(adapter_collector, market, asset_to_outcome={})

        assert scanner_trades[0].outcome == "OriginalLabel"
        assert collector_trades[0].outcome == "OriginalLabel"
        assert _trade_dicts_equal(scanner_trades[0], collector_trades[0])

    def test_unknown_asset_falls_back_to_raw_outcome_identically(self):
        raw = _raw_with_outcome_mismatch(
            raw_outcome="WhateverRaw",
            asset="asset-not-in-map",
        )
        market = _market("0xMARKET_A")
        asset_to_outcome = _asset_to_outcome_for(market)  # does not contain "asset-not-in-map"
        adapter_scanner = _adapter_with_raw([raw])
        adapter_collector = _adapter_with_raw([raw])

        scanner_trades = _scanner_fetch(adapter_scanner, market, asset_to_outcome)
        collector_trades = _collector_fetch(adapter_collector, market, asset_to_outcome)

        assert scanner_trades[0].outcome == "WhateverRaw"
        assert collector_trades[0].outcome == "WhateverRaw"
        assert _trade_dicts_equal(scanner_trades[0], collector_trades[0])


class TestSourceTradeIdParity:
    """The deterministic source_trade_id is computed from the raw dict via
    :func:`deterministic_source_trade_id_v2` and must be IDENTICAL across
    paths. This is the UNIQUE constraint key in ``source_trades`` — if the
    two paths ever disagree, a later run can overwrite or skip a row from
    the earlier run."""

    def test_same_raw_row_yields_same_source_trade_id_in_both_paths(self):
        raw = _raw_with_outcome_mismatch()
        market = _market("0xMARKET_A")
        asset_to_outcome = _asset_to_outcome_for(market)
        adapter_scanner = _adapter_with_raw([raw])
        adapter_collector = _adapter_with_raw([raw])

        scanner_trades = _scanner_fetch(adapter_scanner, market, asset_to_outcome)
        collector_trades = _collector_fetch(adapter_collector, market, asset_to_outcome)

        assert scanner_trades[0].source_trade_id == collector_trades[0].source_trade_id
        # The id must be the canonical v2 hash of the raw row.
        assert scanner_trades[0].source_trade_id == deterministic_source_trade_id_v2(raw)
        assert collector_trades[0].source_trade_id == deterministic_source_trade_id_v2(raw)

    def test_wallet_canonicalization_keeps_source_trade_id_stable_across_paths(self):
        """The wallet is lowercased before the v2 hash, so a mixed-case
        raw ``proxyWallet`` must produce the same id in both paths."""
        market = _market("0xMARKET_A")
        base = {
            "side": "BUY",
            "asset": "asset-yes",
            "conditionId": market.source_id,
            "size": 10.0,
            "price": 0.55,
            "timestamp": 1_782_636_254,
            "outcome": "Yes",
            "transactionHash": "0xabcdef0123456789",
        }
        raw_mixed = dict(base, proxyWallet="0xAbCdEf0000000000000000000000000000000011")
        raw_lower = dict(base, proxyWallet="0xabcdef0000000000000000000000000000000011")
        adapter_scanner = _adapter_with_raw([raw_mixed])
        adapter_collector = _adapter_with_raw([raw_lower])
        asset_to_outcome = _asset_to_outcome_for(market)

        scanner_trades = _scanner_fetch(adapter_scanner, market, asset_to_outcome)
        collector_trades = _collector_fetch(adapter_collector, market, asset_to_outcome)

        # Both paths must produce the same id regardless of input case.
        assert scanner_trades[0].source_trade_id == collector_trades[0].source_trade_id


class TestPersistenceParity:
    """The two paths must persist the same source_trades row, must be
    idempotent across re-runs, and must not overwrite each other with
    a different outcome."""

    def test_persisted_row_is_byte_equal_across_paths(self, tmp_path: Path):
        raw = _raw_with_outcome_mismatch(raw_outcome="Denormalized")
        market = _market("0xMARKET_A")
        asset_to_outcome = _asset_to_outcome_for(market)
        adapter_scanner = _adapter_with_raw([raw])
        adapter_collector = _adapter_with_raw([raw])

        scanner_trades = _scanner_fetch(adapter_scanner, market, asset_to_outcome)
        collector_trades = _collector_fetch(adapter_collector, market, asset_to_outcome)

        db_path = tmp_path / "p34.sqlite"
        db = Database(db_path=db_path).connect()
        try:
            # Drive the same persistence helper the two scripts use.
            ok1 = run_scan_module._persist_trade(db, scanner_trades[0])  # noqa: SLF001
            ok2 = collect_mod.PolymarketCollector()._persist_trade(  # noqa: SLF001
                db, collector_trades[0]
            )
            assert ok1 is True
            assert ok2 is False  # collector's helper hits UNIQUE → no double-insert
        finally:
            db.close()

        db = Database(db_path=db_path).connect()
        try:
            rows = db.fetchall(
                "SELECT source, source_trade_id, market_source_id, outcome, "
                "side, quantity, price, trader_address, is_sample "
                "FROM source_trades"
            )
            assert len(rows) == 1
            row = rows[0]
            # outcome must be the REWRITTEN ("Yes"), not the raw ("Denormalized").
            assert row["outcome"] == "Yes"
            assert row["source_trade_id"] == scanner_trades[0].source_trade_id
            assert row["trader_address"] == "0xaaaa0000000000000000000000000000000001"
        finally:
            db.close()

    def test_second_path_is_idempotent(self, tmp_path: Path):
        """Re-running either path with the same raw row must NOT create a
        duplicate source_trades row and must NOT overwrite the original."""
        raw = _raw_with_outcome_mismatch()
        market = _market("0xMARKET_A")
        asset_to_outcome = _asset_to_outcome_for(market)
        adapter_a = _adapter_with_raw([raw])
        adapter_b = _adapter_with_raw([raw])

        trades_a = _scanner_fetch(adapter_a, market, asset_to_outcome)
        trades_b = _collector_fetch(adapter_b, market, asset_to_outcome)

        db_path = tmp_path / "p34-idem.sqlite"
        db = Database(db_path=db_path).connect()
        try:
            assert run_scan_module._persist_trade(db, trades_a[0]) is True  # noqa: SLF001
            assert (
                collect_mod.PolymarketCollector()._persist_trade(db, trades_b[0])  # noqa: SLF001
                is False
            )  # UNIQUE hit
        finally:
            db.close()

        db = Database(db_path=db_path).connect()
        try:
            rows = db.fetchall("SELECT source_trade_id FROM source_trades")
            assert [r["source_trade_id"] for r in rows] == [trades_a[0].source_trade_id]
        finally:
            db.close()

    def test_mismatched_outcome_does_not_overwrite_first_row(self, tmp_path: Path):
        """Two paths that disagree on outcome MUST NOT overwrite each
        other — but the parity test above proves the two paths
        AGREE, so this test asserts the inverse: a path that DID
        produce a different outcome would create a SEPARATE row only
        if its id were also different. With the fix, the id is the same
        and the second insert is a no-op."""
        raw_a = _raw_with_outcome_mismatch(
            raw_outcome="Denormalized",
            asset="asset-yes",
        )
        raw_b = dict(raw_a)  # identical raw row
        market = _market("0xMARKET_A")
        # Two completely different asset maps.
        asset_map_a = {"asset-yes": "Yes"}
        asset_map_b = {"asset-yes": "DefinitelyNotYes"}  # divergent
        adapter_a = _adapter_with_raw([raw_a])
        adapter_b = _adapter_with_raw([raw_b])

        trades_a = _scanner_fetch(adapter_a, market, asset_map_a)
        trades_b = _collector_fetch(adapter_b, market, asset_map_b)

        # The deterministic id is computed from the RAW row, not from the
        # asset map, so two divergent maps over the same raw row must
        # produce the SAME source_trade_id. The persistence layer then
        # blocks the second insert.
        assert trades_a[0].source_trade_id == trades_b[0].source_trade_id

        db_path = tmp_path / "p34-divergent.sqlite"
        db = Database(db_path=db_path).connect()
        try:
            assert run_scan_module._persist_trade(db, trades_a[0]) is True  # noqa: SLF001
            assert (
                collect_mod.PolymarketCollector()._persist_trade(db, trades_b[0])  # noqa: SLF001
                is False
            )
        finally:
            db.close()

        db = Database(db_path=db_path).connect()
        try:
            rows = db.fetchall("SELECT outcome FROM source_trades")
            # The first write wins: outcome remains the first path's "Yes",
            # NOT the divergent map's "DefinitelyNotYes".
            assert [r["outcome"] for r in rows] == ["Yes"]
        finally:
            db.close()


class TestAssetMapBuilding:
    """The asset-to-outcome map is built from the Gamma payload's
    ``clobTokenIds`` and ``outcomes`` fields. The scanner's
    :func:`scripts.run_scan._build_asset_to_outcome_map` and the
    collector's
    :meth:`PolymarketCollector._build_asset_to_outcome_map` must produce
    the same map for the same input."""

    def test_gamma_payload_yields_identical_maps_in_both_paths(self):
        payload = {
            "conditionId": "0xMARKET_A",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["asset-yes", "asset-no"]',
        }
        scanner_map = run_scan_module._build_asset_to_outcome_map(payload)  # noqa: SLF001
        collector_map = collect_mod.PolymarketCollector._build_asset_to_outcome_map(payload)
        assert scanner_map == collector_map == {
            "asset-yes": "Yes",
            "asset-no": "No",
        }

    def test_malformed_gamma_payload_yields_empty_map_in_both_paths(self):
        payload = {
            "conditionId": "0xMARKET_A",
            "outcomes": "not-json",
            "clobTokenIds": "also-not-json",
        }
        scanner_map = run_scan_module._build_asset_to_outcome_map(payload)  # noqa: SLF001
        collector_map = collect_mod.PolymarketCollector._build_asset_to_outcome_map(payload)
        assert scanner_map == {} == collector_map

    def test_missing_clob_token_ids_yields_empty_map_in_both_paths(self):
        payload = {
            "conditionId": "0xMARKET_A",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": "[]",
        }
        scanner_map = run_scan_module._build_asset_to_outcome_map(payload)  # noqa: SLF001
        collector_map = collect_mod.PolymarketCollector._build_asset_to_outcome_map(payload)
        assert scanner_map == {} == collector_map


class TestAdapterFactoryParity:
    """Both paths must construct the adapter through the shared factory
    :func:`scripts._live_ingest.build_trade_adapter` so a future change
    to base URLs / timeout / rate limit / window size is applied to both
    paths simultaneously."""

    def test_run_scan_uses_factory(self, monkeypatch):
        """``run_scan``'s ``_get_scan_trade_adapter`` must call
        ``build_trade_adapter`` (after the P3 round-11 refactor)."""
        from scripts import _live_ingest as live_ingest_mod
        calls: list[dict] = []
        original = live_ingest_mod.build_trade_adapter

        def spy(settings=None):
            calls.append({"settings": settings})
            return original(settings)

        # Patch the BOTH the original module and the alias on the run_scan module.
        monkeypatch.setattr(live_ingest_mod, "build_trade_adapter", spy)
        monkeypatch.setattr(run_scan_module, "build_trade_adapter", spy)
        # Reset the lazy singleton.
        run_scan_module._SCAN_TRADE_ADAPTER = None  # noqa: SLF001
        adapter = run_scan_module._get_scan_trade_adapter()  # noqa: SLF001
        assert isinstance(adapter, PolymarketPublicAdapter)
        assert len(calls) == 1
        # Second call returns the cached singleton, no second factory invocation.
        run_scan_module._get_scan_trade_adapter()  # noqa: SLF001
        assert len(calls) == 1

    def test_collector_uses_factory(self, monkeypatch):
        """``PolymarketCollector._get_trade_adapter`` must call
        ``build_trade_adapter`` (after the P3 round-11 refactor)."""
        # Some earlier tests (e.g. test_p24_direct_exec_pagination)
        # do ``sys.modules.pop("scripts._live_ingest", None)`` as part
        # of exercising a direct-execution path. After such a test,
        # the ``scripts._live_ingest`` module is no longer in
        # ``sys.modules``, so a fresh ``import scripts._live_ingest``
        # would re-create it (with a fresh ``build_trade_adapter``).
        # Always re-bind ``live_ingest_mod`` from ``sys.modules`` so
        # the spy we install below is the one the collector's
        # function-local ``from scripts._live_ingest import
        # build_trade_adapter`` will actually see.
        if "scripts._live_ingest" not in sys.modules:
            import importlib
            importlib.import_module("scripts._live_ingest")
        live_ingest_mod = sys.modules["scripts._live_ingest"]
        calls: list[dict] = []
        original = live_ingest_mod.build_trade_adapter

        def spy(settings=None):
            calls.append({"settings": settings})
            return original(settings)

        # Patch the source module (the collector's first import
        # attempt uses ``from scripts._live_ingest import build_trade_adapter``
        # — this is the same object reference as
        # ``scripts._live_ingest``).
        monkeypatch.setattr(live_ingest_mod, "build_trade_adapter", spy)
        # Reset the run_scan singleton so any cross-test contamination
        # of run_scan's bound name doesn't change the outcome.
        monkeypatch.setattr(run_scan_module, "_SCAN_TRADE_ADAPTER", None)
        monkeypatch.setattr(run_scan_module, "build_trade_adapter", spy)
        collector = collect_mod.PolymarketCollector()
        # Pre-set the trade_adapter to None to force a factory call.
        collector._trade_adapter = None
        # The collector imports build_trade_adapter INSIDE
        # ``_get_trade_adapter`` via ``from scripts._live_ingest import
        # build_trade_adapter``. The function-local import is bound
        # at call time, so the monkeypatch on the module attribute is
        # picked up on the next call.
        adapter = asyncio_run(collector._get_trade_adapter())
        assert isinstance(adapter, PolymarketPublicAdapter)
        assert len(calls) == 1, f"expected 1 factory call, got {len(calls)}: {calls}"
        # Second call returns the cached singleton.
        asyncio_run(collector._get_trade_adapter())
        assert len(calls) == 1

    def test_factory_passes_same_settings_to_both_paths(self, monkeypatch):
        """Both paths must call ``build_trade_adapter`` with the same
        active Settings instance so neither path drifts on base URLs,
        timeout, rate limit, window size, or request interval."""
        from polycopy.config.settings import get_settings
        # Re-import the live_ingest module if a prior test popped it.
        if "scripts._live_ingest" not in sys.modules:
            import importlib
            importlib.import_module("scripts._live_ingest")
        live_ingest_mod = sys.modules["scripts._live_ingest"]
        settings = get_settings()
        calls: list = []

        def spy(settings_arg=None):
            calls.append(settings_arg)
            return live_ingest_mod.PolymarketPublicAdapter(
                gamma_base_url=settings_arg.gamma_base_url,
                clob_base_url=settings_arg.clob_base_url,
                data_api_base_url=settings_arg.data_api_base_url,
                timeout=settings_arg.http_timeout_seconds,
                rate_limit_rps=settings_arg.http_rate_limit_rps,
                data_api_window_size=settings_arg.data_api_window_size,
                data_api_request_interval_seconds=settings_arg.data_api_request_interval_seconds,
            )

        monkeypatch.setattr(live_ingest_mod, "build_trade_adapter", spy)
        monkeypatch.setattr(run_scan_module, "build_trade_adapter", spy)
        # Reset lazy singletons on BOTH sides so each path is forced
        # to call the factory in this test (an earlier test in the
        # same class may have populated them).
        run_scan_module._SCAN_TRADE_ADAPTER = None  # noqa: SLF001
        # Run scan side.
        run_scan_module._get_scan_trade_adapter()  # noqa: SLF001
        # Collector side — fresh collector with cleared cache.
        collector = collect_mod.PolymarketCollector(settings=settings)
        collector._trade_adapter = None
        asyncio_run(collector._get_trade_adapter())
        assert len(calls) == 2
        # Both calls passed the same active Settings instance.
        assert calls[0] is calls[1]
        # And the Settings object is the same module-level singleton.
        assert calls[0] is settings

    def test_factory_produces_adapter_with_settings_url_and_timeout(self, monkeypatch):
        """A spy proves the adapter's gamma_base_url, clob_base_url,
        data_api_base_url, timeout, rate_limit_rps, data_api_window_size,
        and data_api_request_interval_seconds all came from the passed
        Settings object."""
        from polycopy.config.settings import Settings
        from scripts import _live_ingest as live_ingest_mod
        s = Settings(
            gamma_base_url="https://gamma-spy.example",
            clob_base_url="https://clob-spy.example",
            data_api_base_url="https://data-spy.example",
            http_timeout_seconds=7.0,
            http_rate_limit_rps=3.5,
            data_api_window_size=42,
            data_api_request_interval_seconds=0.125,
        )
        adapter = live_ingest_mod.build_trade_adapter(s)
        assert adapter.gamma_base_url == "https://gamma-spy.example"
        assert adapter.clob_base_url == "https://clob-spy.example"
        assert adapter.data_api_base_url == "https://data-spy.example"
        assert adapter.timeout == 7.0
        assert adapter.rate_limit_rps == 3.5
        assert adapter.data_api_window_size == 42
        assert adapter.data_api_request_interval_seconds == 0.125
