"""No-network regressions for PR #73's safe provider lower-bound sentinel."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from polycopy.adapters.polymarket import PolymarketPublicAdapter
from polycopy.ingestion.ingest_pipeline import run_ingestion
from polycopy.ingestion.specialist_evidence_cohort import _CliProvider


class _RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
        self.calls.append(
            {
                "wallet": wallet,
                "since": since,
                "limit": limit,
                "offset": offset,
                "return_raw": return_raw,
            }
        )
        return []


def test_cli_provider_passes_utc_epoch_and_preserves_page_zero_shape():
    adapter = _RecordingAdapter()
    rows = asyncio.run(_CliProvider(adapter, timeout=1).fetch_trades("wallet", limit=25, page=0))

    assert rows == []
    assert adapter.calls == [
        {
            "wallet": "wallet",
            "since": datetime.fromtimestamp(0, tz=timezone.utc),
            "limit": 25,
            "offset": 0,
            "return_raw": True,
        }
    ]
    since = adapter.calls[0]["since"]
    assert since.tzinfo is timezone.utc
    assert since.utcoffset() == timedelta(0)
    assert since.timestamp() == 0.0


def test_cli_provider_preserves_nonzero_page_offset():
    adapter = _RecordingAdapter()
    asyncio.run(_CliProvider(adapter, timeout=1).fetch_trades("wallet", limit=25, page=1))

    assert adapter.calls[0]["offset"] == 25
    assert adapter.calls[0]["limit"] == 25
    assert adapter.calls[0]["return_raw"] is True


def test_epoch_bound_reaches_one_fake_data_api_get_without_year_zero_failure():
    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json=[])

    async def run() -> list[dict]:
        adapter = PolymarketPublicAdapter(
            "https://gamma.invalid",
            "https://clob.invalid",
            "https://data.invalid",
            timeout=1,
            data_api_request_interval_seconds=0,
        )
        adapter._data_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://data.invalid"
        )
        try:
            return await _CliProvider(adapter, timeout=1).fetch_trades(
                "0x0000000000000000000000000000000000000001", limit=25, page=0
            )
        finally:
            await adapter.aclose()

    assert asyncio.run(run()) == []
    assert len(observed) == 1
    request = observed[0]
    assert request.url.path == "/trades"
    assert dict(request.url.params) == {
        "user": "0x0000000000000000000000000000000000000001",
        "limit": "25",
        "offset": "0",
    }


def test_genuine_provider_error_remains_page_specific_ingestion_failure():
    class FailingAdapter:
        async def get_trades_by_address(self, *_args, **_kwargs):
            raise RuntimeError("upstream unavailable")

    result = asyncio.run(
        run_ingestion(
            _CliProvider(FailingAdapter(), timeout=1),
            "wallet",
            record_limit=25,
            max_pages=1,
        )
    )

    assert result.error == "provider error on page 0: RuntimeError: upstream unavailable"