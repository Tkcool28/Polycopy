from __future__ import annotations

import pytest

from polycopy.db.database import Database
from polycopy.engine.wallet_accounting_coverage import (
    MISSING_TRADER,
    MISSING_WALLET,
    build_wallet_accounting_coverage_report,
)


def make_db(tmp_path):
    return Database(tmp_path / "p24j.db").connect()


def source(db, id, trader, side="BUY"):
    db.conn.execute(
        """
        INSERT INTO source_trades (
            id, source, source_trade_id, market_source_id, side, outcome,
            quantity, price, trader_address, timestamp, is_sample, token_id,
            resolution_status, resolved_at, winning_token_id,
            is_winning_trade, realized_pnl, settlement_source
        ) VALUES (?, 'test', ?, 'm1', ?, 'YES', 10, 0.5, ?,
          '2026-01-01T00:00:00+00:00', 1, 'YES', 'won',
          '2026-01-02T00:00:00+00:00', 'YES', 1, 5, 'test')
        """,
        (id, f"ext-{id}", side, trader),
    )


def wallet(db, id, address):
    db.conn.execute(
        "INSERT INTO wallets (id,address,label,is_sample,created_at,canonical_address) VALUES (?,?,?,?,?,?)",
        (id, address, "w", 1, "2026-01-01T00:00:00+00:00", address.lower()),
    )


def ledger(
    db,
    id,
    trader,
    *,
    wallet_id=None,
    side="BUY",
    status="accounted",
    resolution="won",
    cost=5.0,
    payout=10.0,
    pnl=5.0,
):
    db.conn.execute(
        """
        INSERT INTO settlement_accounting_ledger (
            id, source_trade_id, wallet_id, trader_address, market_id,
            market_source_id, token_id, winning_token_id, side, outcome,
            quantity, price, cost_basis, payout, realized_pnl, roi,
            resolution_status, is_winning_trade, accounting_status,
            accounting_reason, settlement_source, resolved_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'market', 'm1', 'YES', 'YES', ?, 'YES',
          10, 0.5, ?, ?, ?, NULL, ?, NULL, ?, NULL, 'test',
          '2026-01-02T00:00:00+00:00', '2026-01-03T00:00:00+00:00',
          '2026-01-03T00:00:00+00:00')
        """,
        (f"ledger-{id}", id, wallet_id, trader, side, cost, payout, pnl, resolution, status),
    )


def row(report, key):
    return next(r for r in report.rows if r.identity_key == key)


def test_empty_db_no_trades(tmp_path):
    db = make_db(tmp_path)
    try:
        report = build_wallet_accounting_coverage_report(db)
    finally:
        db.close()
    assert report.total_source_trades == 0
    assert report.total_ledger_rows == 0
    assert report.accounting_coverage_pct is None
    assert report.rows == []


def test_source_trades_exist_ledger_empty(tmp_path):
    db = make_db(tmp_path)
    try:
        for i in range(3):
            source(db, f"a{i}", "0xaaa")
        for i in range(2):
            source(db, f"b{i}", "0xbbb")
        source(db, "missing", None)
        report = build_wallet_accounting_coverage_report(db)
    finally:
        db.close()
    assert report.total_source_trades == 6
    assert report.source_trades_with_trader_address == 5
    assert report.source_trades_missing_trader_address == 1
    assert report.total_ledger_rows == 0
    assert report.accounting_coverage_pct is None
    assert {r.identity_key for r in report.rows} == {"0xaaa", "0xbbb", MISSING_TRADER}
    assert row(report, "0xaaa").ledger_rows == 0
    assert row(report, "0xaaa").accounting_coverage_pct is None


def seed_hand(db):
    specs = [
        ("a", "accounted", "won", "BUY", 40.0, 100.0, 60.0),
        ("b", "accounted", "lost", "BUY", 35.0, 0.0, -35.0),
        ("c", "excluded_missing_token", "won", "BUY", None, None, None),
        ("d", "excluded_unresolved", "unresolved", "BUY", None, None, None),
        ("e", "excluded_unknown", "unknown", "BUY", None, None, None),
        ("f", "excluded_ambiguous", "ambiguous", "BUY", None, None, None),
        ("g", "excluded_unsupported_side", "won", "SELL", None, None, None),
    ]
    for id, status, resolution, side, cost, payout, pnl in specs:
        source(db, id, "0xcoverage", side=side)
        ledger(
            db,
            id,
            "0xcoverage",
            side=side,
            status=status,
            resolution=resolution,
            cost=cost,
            payout=payout,
            pnl=pnl,
        )


def test_hand_verified_wallet_trader_coverage_example(tmp_path):
    db = make_db(tmp_path)
    try:
        seed_hand(db)
        report = build_wallet_accounting_coverage_report(db)
    finally:
        db.close()
    r = row(report, "0xcoverage")
    assert r.source_trades == 7
    assert r.ledger_rows == 7
    assert r.accounted_trades == 2
    assert r.excluded_missing_token == 1
    assert r.excluded_unresolved == 1
    assert r.excluded_unknown == 1
    assert r.excluded_ambiguous == 1
    assert r.excluded_unsupported_side == 1
    assert r.excluded_other == 0
    assert r.buy_trades == 6
    assert r.sell_trades == 1
    assert r.buy_only_limitation is True
    assert r.total_cost_basis == 75
    assert r.total_payout == 100
    assert r.total_realized_pnl == 25
    assert r.roi == pytest.approx(25 / 75)
    assert r.win_rate == pytest.approx(0.5)
    assert r.profit_factor == pytest.approx(60 / 35)
    assert r.accounting_coverage_pct == pytest.approx(2 / 7)
    assert r.accountable_buy_coverage_pct == pytest.approx(2 / 6)
    assert report.accounting_coverage_pct == pytest.approx(2 / 7)


def test_multiple_traders_grouped_separately(tmp_path):
    db = make_db(tmp_path)
    try:
        for id in ["a1", "a2"]:
            source(db, id, "0xaaa")
            ledger(db, id, "0xaaa", status="accounted")
        source(db, "b1", "0xbbb")
        ledger(db, "b1", "0xbbb", status="accounted")
        for id in ["b2", "b3"]:
            source(db, id, "0xbbb")
            ledger(db, id, "0xbbb", status="excluded_missing_token", cost=None, payout=None, pnl=None)
        source(db, "m1", None)
        ledger(db, "m1", None, status="excluded_unresolved", resolution="unresolved", cost=None, payout=None, pnl=None)
        report = build_wallet_accounting_coverage_report(db)
    finally:
        db.close()
    assert len(report.rows) == 3
    assert row(report, "0xaaa").accounted_trades == 2
    assert row(report, "0xbbb").excluded_missing_token == 2
    assert row(report, MISSING_TRADER).excluded_unresolved == 1
    assert report.total_source_trades == sum(r.source_trades for r in report.rows)


def test_wallet_id_grouping_does_not_fabricate_mapping(tmp_path):
    db = make_db(tmp_path)
    try:
        wallet(db, "wallet-aaa", "0xaaa")
        source(db, "a", "0xaaa")
        ledger(db, "a", "0xaaa", wallet_id=None)
        report = build_wallet_accounting_coverage_report(db, group_by="wallet_id")
    finally:
        db.close()
    assert {r.identity_key for r in report.rows} == {MISSING_WALLET}
    assert row(report, MISSING_WALLET).wallet_id is None
    assert report.mapped_wallets == 0


def test_buy_only_limitation(tmp_path):
    db = make_db(tmp_path)
    try:
        source(db, "sell", "0xaaa", side="SELL")
        ledger(db, "sell", "0xaaa", side="SELL", status="excluded_unsupported_side", cost=None, payout=None, pnl=None)
        report = build_wallet_accounting_coverage_report(db)
    finally:
        db.close()
    r = row(report, "0xaaa")
    assert r.sell_trades == 1
    assert r.excluded_unsupported_side == 1
    assert r.buy_only_limitation is True
    assert report.buy_only_limitation is True


def test_coverage_math_edge_cases(tmp_path):
    db = make_db(tmp_path)
    try:
        source(db, "empty", "0xempty")
        assert build_wallet_accounting_coverage_report(db).accounting_coverage_pct is None
        source(db, "e1", "0xallx")
        ledger(db, "e1", "0xallx", status="excluded_unknown", resolution="unknown", cost=None, payout=None, pnl=None)
        all_ex = build_wallet_accounting_coverage_report(db, min_source_trades=0)
        assert all_ex.accounting_coverage_pct == 0.0
        source(db, "a1", "0xacc")
        ledger(db, "a1", "0xacc", status="accounted")
        mixed = build_wallet_accounting_coverage_report(db, min_source_trades=0)
        assert mixed.accounting_coverage_pct == pytest.approx(1 / 2)
        only = build_wallet_accounting_coverage_report(db, group_by="wallet_id", min_source_trades=0)
        assert only.accountable_buy_coverage_pct == pytest.approx(1 / 3)
    finally:
        db.close()


def test_limit_filters_rows_not_totals_and_min_source_trades(tmp_path):
    db = make_db(tmp_path)
    try:
        source(db, "a1", "0xaaa")
        source(db, "a2", "0xaaa")
        source(db, "b1", "0xbbb")
        report = build_wallet_accounting_coverage_report(db, limit=1, min_source_trades=2)
    finally:
        db.close()
    assert report.total_source_trades == 3
    assert len(report.rows) == 1
    assert report.rows[0].identity_key == "0xaaa"
