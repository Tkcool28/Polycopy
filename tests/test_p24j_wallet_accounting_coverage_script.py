from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from polycopy.db.database import Database

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "report_wallet_accounting_coverage.py"


def make_db(path: Path):
    return Database(path).connect()


def source(db, id, trader):
    db.conn.execute(
        """
        INSERT INTO source_trades (
            id, source, source_trade_id, market_source_id, side, outcome,
            quantity, price, trader_address, timestamp, is_sample, token_id,
            resolution_status, resolved_at, winning_token_id,
            is_winning_trade, realized_pnl, settlement_source
        ) VALUES (?, 'test', ?, 'm1', 'BUY', 'YES', 10, 0.5, ?,
          '2026-01-01T00:00:00+00:00', 1, 'YES', 'won',
          '2026-01-02T00:00:00+00:00', 'YES', 1, 5, 'test')
        """,
        (id, f"ext-{id}", trader),
    )


def ledger(db, id, trader, status="accounted", wallet_id=None):
    db.conn.execute(
        """
        INSERT INTO settlement_accounting_ledger (
            id, source_trade_id, wallet_id, trader_address, market_id,
            market_source_id, token_id, winning_token_id, side, outcome,
            quantity, price, cost_basis, payout, realized_pnl, roi,
            resolution_status, is_winning_trade, accounting_status,
            accounting_reason, settlement_source, resolved_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'market', 'm1', 'YES', 'YES', 'BUY', 'YES',
          10, 0.5, 5, 10, 5, 1, 'won', 1, ?, NULL, 'test',
          '2026-01-02T00:00:00+00:00', '2026-01-03T00:00:00+00:00',
          '2026-01-03T00:00:00+00:00')
        """,
        (f"ledger-{id}", id, wallet_id, trader, status),
    )


def run(db_path: Path, *args: str):
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    env["POLYCOPY_DB_PATH"] = str(db_path)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def count_rows(db_path: Path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            "source": conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0],
            "ledger": conn.execute("SELECT COUNT(*) FROM settlement_accounting_ledger").fetchone()[0],
            "wallets": conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0],
        }
    finally:
        conn.close()


def test_json_output_parseable_and_trader_group(tmp_path):
    path = tmp_path / "script.db"
    db = make_db(path)
    try:
        source(db, "a", "0xaaa")
        ledger(db, "a", "0xaaa")
        db.conn.commit()
    finally:
        db.close()
    result = run(path, "--json", "--include-rows")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["total_source_trades"] == 1
    assert data["rows"][0]["identity_key"] == "0xaaa"


def test_human_output_contains_totals_and_buy_only_limitation(tmp_path):
    path = tmp_path / "script.db"
    db = make_db(path)
    try:
        source(db, "a", "0xaaa")
        ledger(db, "a", "0xaaa")
        db.conn.commit()
    finally:
        db.close()
    result = run(path)
    assert result.returncode == 0, result.stderr
    assert "Wallet Accounting Coverage Report" in result.stdout
    assert "Totals:" in result.stdout
    assert "BUY-only limitation" in result.stdout
    assert "Top coverage rows:" in result.stdout


def test_wallet_id_grouping_does_not_fabricate(tmp_path):
    path = tmp_path / "script.db"
    db = make_db(path)
    try:
        source(db, "a", "0xaaa")
        ledger(db, "a", "0xaaa", wallet_id=None)
        db.conn.commit()
    finally:
        db.close()
    result = run(path, "--group-by", "wallet_id", "--json", "--include-rows")
    data = json.loads(result.stdout)
    assert data["rows"][0]["identity_key"] == "missing_wallet_id"
    assert data["mapped_wallets"] == 0


def test_limit_limits_rows_not_totals_and_min_source_trades(tmp_path):
    path = tmp_path / "script.db"
    db = make_db(path)
    try:
        source(db, "a1", "0xaaa")
        source(db, "a2", "0xaaa")
        source(db, "b1", "0xbbb")
        db.conn.commit()
    finally:
        db.close()
    result = run(path, "--json", "--include-rows", "--limit", "1", "--min-source-trades", "2")
    data = json.loads(result.stdout)
    assert data["total_source_trades"] == 3
    assert len(data["rows"]) == 1
    assert data["rows"][0]["identity_key"] == "0xaaa"


def test_empty_source_and_ledger_exits_zero(tmp_path):
    path = tmp_path / "empty.db"
    db = make_db(path)
    db.close()
    result = run(path)
    assert result.returncode == 0, result.stderr
    assert "settlement_accounting_ledger has 0 rows" in result.stdout


def test_no_writes_by_script(tmp_path):
    path = tmp_path / "no-write.db"
    db = make_db(path)
    try:
        source(db, "a", "0xaaa")
        ledger(db, "a", "0xaaa")
        db.conn.commit()
    finally:
        db.close()
    before = count_rows(path)
    result = run(path, "--json", "--include-rows")
    assert result.returncode == 0, result.stderr
    after = count_rows(path)
    assert before == after
