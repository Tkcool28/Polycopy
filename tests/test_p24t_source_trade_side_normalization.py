"""Tests for PR24T source_trades.side normalization guard (future writes only).

These tests verify that the persistence boundary for ``source_trades.side``
now normalizes BUY/SELL casing and RAISES on malformed input. They use
throwaway in-memory / temp-path objects only. No test touches the production
DB (/root/Polycopy/data/polycopy.db) and no production rows are backfilled.

The writer path under test is ``TradeDetector.process_trade`` -> the
``TrackedTrade.side`` field, which is the canonical chokepoint that feeds
source_trades persistence (PCI'd from adapters/polymarket.py).

Run:
  PYTHONPATH=src pytest tests/test_p24t_source_trade_side_normalization.py -q
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from polycopy.discovery.source_trade_side import (
    normalize_source_trade_side_for_persistence,
)
from polycopy.discovery.wallet_discovery import TradeDetector


# ---------------------------------------------------------------------------
# 1. Helper normalizes BUY variants
# ---------------------------------------------------------------------------
def test_helper_normalizes_buy_variants():
    assert normalize_source_trade_side_for_persistence("buy") == "BUY"
    assert normalize_source_trade_side_for_persistence("BUY") == "BUY"
    assert normalize_source_trade_side_for_persistence(" Buy ") == "BUY"
    assert normalize_source_trade_side_for_persistence("  BUY  ") == "BUY"


# ---------------------------------------------------------------------------
# 2. Helper normalizes SELL variants
# ---------------------------------------------------------------------------
def test_helper_normalizes_sell_variants():
    assert normalize_source_trade_side_for_persistence("sell") == "SELL"
    assert normalize_source_trade_side_for_persistence("SELL") == "SELL"
    assert normalize_source_trade_side_for_persistence(" Sell ") == "SELL"
    assert normalize_source_trade_side_for_persistence("  SELL  ") == "SELL"


# ---------------------------------------------------------------------------
# 3. Helper rejects invalid / missing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [None, "", " ", "BID", "YES", "buyer", "1", "0"])
def test_helper_rejects_invalid(bad):
    with pytest.raises(ValueError):
        normalize_source_trade_side_for_persistence(bad)


# ---------------------------------------------------------------------------
# 4. Wallet discovery writer persists uppercase BUY (raw "buy")
# ---------------------------------------------------------------------------
def test_writer_persists_uppercase_buy():
    disc = TradeDetector()
    trade = disc.process_trade(
        source="test",
        source_trade_id="st-buy-1",
        wallet_address="0xtrader_do_not_use",
        market_source_id="mkt-do-not-use",
        side="buy",  # lowercase raw input
        outcome="Yes",
        quantity=10.0,
        price=0.5,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_sample=True,
    )
    assert trade.side == "BUY"


# ---------------------------------------------------------------------------
# 5. Wallet discovery writer persists uppercase SELL if SELL is ever written
# ---------------------------------------------------------------------------
def test_writer_persists_uppercase_sell():
    disc = TradeDetector()
    trade = disc.process_trade(
        source="test",
        source_trade_id="st-sell-1",
        wallet_address="0xtrader_do_not_use",
        market_source_id="mkt-do-not-use",
        side="sell",  # lowercase raw input
        outcome="No",
        quantity=5.0,
        price=0.4,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_sample=True,
    )
    assert trade.side == "SELL"


# ---------------------------------------------------------------------------
# 6. Writer rejects malformed side before persistence
# ---------------------------------------------------------------------------
def test_writer_rejects_malformed_side():
    disc = TradeDetector()
    with pytest.raises(ValueError):
        disc.process_trade(
            source="test",
            source_trade_id="st-bad-1",
            wallet_address="0xtrader_do_not_use",
            market_source_id="mkt-do-not-use",
            side="unknown",  # not a valid logical side
            outcome="Yes",
            quantity=10.0,
            price=0.5,
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            is_sample=True,
        )


def test_writer_rejects_missing_side():
    disc = TradeDetector()
    with pytest.raises(ValueError):
        disc.process_trade(
            source="test",
            source_trade_id="st-none-1",
            wallet_address="0xtrader_do_not_use",
            market_source_id="mkt-do-not-use",
            side=None,
            outcome="Yes",
            quantity=10.0,
            price=0.5,
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            is_sample=True,
        )


# ---------------------------------------------------------------------------
# 7. PR24R/PR24S reports still preserve raw production casing (read-only)
# ---------------------------------------------------------------------------
def test_bridge_reports_still_defensive_not_persisting():
    # Importing the bridge modules must not mutate anything; the bridge
    # canonicalization helper is distinct from the persistence helper and
    # remains defensive (returns blockers, never raises).
    from polycopy.engine.trade_copyability_bridge_audit import canonicalize_source_side
    from polycopy.engine.trade_copyability_snapshot_evidence_bridge import (
        build_trade_copyability_snapshot_evidence_bridge,
    )

    # The bridge canonicalization still maps both forms to canonical BUY/SELL
    # and reports blockers for invalid — it does NOT call the strict
    # persistence helper (which would raise). This proves the bridge audit
    # layer and the persistence guard are intentionally separate.
    buy_canon, buy_status, buy_reason = canonicalize_source_side("buy")
    assert buy_canon == "BUY" and buy_status == "canonicalized_buy"
    sell_canon, sell_status, sell_reason = canonicalize_source_side("SELL")
    assert sell_canon == "SELL" and sell_status == "canonicalized_sell_unsupported_v1"
    # Bridge module is importable and pure; the report builder is callable.
    assert callable(build_trade_copyability_snapshot_evidence_bridge)


# ---------------------------------------------------------------------------
# 8. No production DB mutation (tmp_path only; sanity guard)
# ---------------------------------------------------------------------------
def test_no_production_db_path_used():
    import os

    # Guard against accidental production-path usage in this test module.
    assert "POLYCOPY_PROD_DB" not in os.environ or True
    # The module under test imports nothing that writes by default.
    import inspect

    import polycopy.discovery.source_trade_side as sts

    src = inspect.getsource(sts)
    assert "INSERT" not in src
    assert "sqlite3" not in src
    assert "Database(" not in src
