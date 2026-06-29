"""Regression tests for Data API parser sentinel trader normalization."""

from __future__ import annotations

from pathlib import Path

import pytest

import sys

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

import polycopy.adapters.polymarket as polymarket_mod  # noqa: E402
from polycopy.adapters.polymarket import PolymarketPublicAdapter  # noqa: E402


def _adapter() -> PolymarketPublicAdapter:
    return PolymarketPublicAdapter(
        gamma_base_url="https://gamma.example.test",
        clob_base_url="https://clob.example.test",
        data_api_base_url="https://data-api.example.test",
        data_api_request_interval_seconds=0.0,
    )


def _raw_with_wallet(wallet) -> dict:
    return {
        "proxyWallet": wallet,
        "side": "BUY",
        "asset": "asset-yes",
        "conditionId": "0xMARKET_A",
        "size": 7.0,
        "price": 0.51,
        "timestamp": 1_782_636_254,
        "outcome": "Yes",
        "transactionHash": "0x123456789abcdef0",
    }


@pytest.mark.parametrize(
    "wallet",
    [
        None,
        "",
        "   ",
        "\t\n",
        "unknown",
        " Unknown ",
        "UNKNOWN",
        "anonymous",
        " Anonymous ",
        "missing",
        " MiSsInG ",
        "0x",
        " 0X ",
        "0x0",
        " 0X0 ",
    ],
)
def test_parse_data_api_trade_normalizes_all_sentinel_wallets_to_none(wallet):
    trade = _adapter()._parse_data_api_trade(_raw_with_wallet(wallet))  # noqa: SLF001

    assert trade is not None
    assert trade.trader_address is None


def test_parse_data_api_trade_preserves_real_wallets_in_canonical_lowercase():
    # Round-8: legitimate wallets are normalized to canonical lowercase
    # at the parser boundary so that discovery and scoring see a single
    # identity. This supersedes the older "byte-for-byte" contract
    # which produced mixed-case rows that didn't match the lowercase
    # keys used by WalletDiscovery.
    wallet = "  0xReal_But_Padded  "

    trade = _adapter()._parse_data_api_trade(_raw_with_wallet(wallet))  # noqa: SLF001

    assert trade is not None
    assert trade.trader_address == "0xreal_but_padded"


def test_parse_data_api_trade_uses_shared_sentinel_helper(monkeypatch):
    """Pin that parser delegates sentinel decisions to the shared helper."""
    calls: list[str | None] = []
    original = polymarket_mod.is_sentinel_trader_address

    def spy(value: str | None) -> bool:
        calls.append(value)
        return original(value)

    monkeypatch.setattr(polymarket_mod, "is_sentinel_trader_address", spy)

    padded_sentinel = "  AnOnYmOuS  "
    trade = _adapter()._parse_data_api_trade(_raw_with_wallet(padded_sentinel))  # noqa: SLF001

    assert trade is not None
    assert trade.trader_address is None
    assert calls == [padded_sentinel.strip()]
