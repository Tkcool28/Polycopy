from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from polycopy.db.database import Database
from polycopy.runtime.locks import operational_job_lock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_settlement_accounting_ledger.py"


def insert_market(db: Database, *, market_id="m1", source_id="market-1") -> None:
    db.conn.execute(
        """
        INSERT INTO markets (
            id, source_id, source, question, active, closed, resolved,
            resolution_outcome, volume_24h, end_date, fetched_at, is_sample
        ) VALUES (?, ?, 'polymarket', 'question', 0, 1, 1, 'YES', 0, NULL,
                  '2026-01-01T00:00:00+00:00', 0)
        """,
        (market_id, source_id),
    )


def insert_trade(db: Database, **overrides) -> None:
    data = {
        "id": "t1",
        "source": "polymarket_data_api",
        "source_trade_id": "external-t1",
        "market_source_id": "market-1",
        "side": "BUY",
        "outcome": "YES",
        "quantity": 100.0,
        "price": 0.4,
        "trader_address": "0xaaa",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "is_sample": 0,
        "token_id": "yes-token",
        "resolution_status": "won",
        "resolved_at": "2026-01-02T00:00:00+00:00",
        "winning_token_id": "yes-token",
        "is_winning_trade": 1,
        "realized_pnl": 60.0,
        "settlement_source": "test",
    }
    data.update(overrides)
    db.conn.execute(
        """
        INSERT INTO source_trades (
            id, source, source_trade_id, market_source_id, side, outcome,
            quantity, price, trader_address, timestamp, is_sample, token_id,
            resolution_status, resolved_at, winning_token_id,
            is_winning_trade, realized_pnl, settlement_source
        ) VALUES (
            :id, :source, :source_trade_id, :market_source_id, :side,
            :outcome, :quantity, :price, :trader_address, :timestamp,
            :is_sample, :token_id, :resolution_status, :resolved_at,
            :winning_token_id, :is_winning_trade, :realized_pnl,
            :settlement_source
        )
        """,
        data,
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "p24i-ledger.db"


@pytest.fixture
def db(db_path: Path):
    database = Database(db_path=db_path).connect()
    insert_market(database)
    yield database
    database.close()


def run_script(db_path: Path, *args: str, lock_path: Path | None = None):
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    env["POLYCOPY_DB_PATH"] = str(db_path)
    if lock_path is not None:
        env["POLYCOPY_OPERATIONAL_LOCK_PATH"] = str(lock_path)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def ledger_rows(db: Database):
    return list(db.conn.execute("SELECT * FROM settlement_accounting_ledger ORDER BY source_trade_id"))


def test_default_dry_run_writes_nothing(db: Database, db_path: Path):
    insert_trade(db)
    db.conn.commit()
    result = run_script(db_path, "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["ledger_rows_planned"] == 1
    assert ledger_rows(db) == []


def test_json_output_is_parseable(db: Database, db_path: Path):
    insert_trade(db)
    db.conn.commit()
    result = run_script(db_path, "--dry-run", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["trades_seen"] == 1
    assert payload["accounted"] == 1


def test_apply_writes_rows_and_second_apply_is_idempotent(db: Database, db_path: Path):
    insert_trade(db)
    db.conn.commit()
    first = run_script(db_path, "--apply", "--json")
    assert first.returncode == 0, first.stderr
    assert len(ledger_rows(db)) == 1
    row = ledger_rows(db)[0]
    assert row["source_trade_id"] == "t1"
    assert row["accounting_status"] == "accounted"
    assert row["realized_pnl"] == pytest.approx(60)

    second = run_script(db_path, "--apply", "--json")
    assert second.returncode == 0, second.stderr
    assert len(ledger_rows(db)) == 1
    payload = json.loads(second.stdout)
    assert payload["ledger_rows_planned"] == 1


@pytest.mark.parametrize(
    ("trade_id", "status", "ledger_status"),
    [
        ("amb", "ambiguous", "excluded_ambiguous"),
        ("unk", "unknown", "excluded_unknown"),
        ("unres", "unresolved", "excluded_unresolved"),
    ],
)
def test_excluded_status_rows_written_without_pnl(
    db: Database, db_path: Path, trade_id: str, status: str, ledger_status: str
):
    insert_trade(
        db,
        id=trade_id,
        source_trade_id=f"external-{trade_id}",
        resolution_status=status,
        realized_pnl=None,
        is_winning_trade=None,
    )
    db.conn.commit()
    result = run_script(db_path, "--apply", "--json")
    assert result.returncode == 0, result.stderr
    row = ledger_rows(db)[0]
    assert row["accounting_status"] == ledger_status
    assert row["realized_pnl"] is None


def test_missing_token_creates_missing_token_exclusion(db: Database, db_path: Path):
    insert_trade(db, id="missing-token", source_trade_id="external-missing", token_id=None)
    db.conn.commit()
    result = run_script(db_path, "--apply", "--json")
    assert result.returncode == 0, result.stderr
    row = ledger_rows(db)[0]
    assert row["accounting_status"] == "excluded_missing_token"
    assert row["realized_pnl"] is None


def test_filters_by_trader_and_market(db: Database, db_path: Path):
    insert_market(db, market_id="m2", source_id="market-2")
    insert_trade(db, id="a", source_trade_id="external-a", trader_address="0xaaa")
    insert_trade(
        db,
        id="b",
        source_trade_id="external-b",
        trader_address="0xbbb",
        market_source_id="market-2",
    )
    db.conn.commit()

    trader_result = run_script(db_path, "--dry-run", "--json", "--trader-address", "0xbbb")
    trader_payload = json.loads(trader_result.stdout)
    assert trader_payload["trades_seen"] == 1
    assert trader_payload["rows"][0]["source_trade_id"] == "b"

    market_result = run_script(db_path, "--dry-run", "--json", "--market-id", "m1")
    market_payload = json.loads(market_result.stdout)
    assert market_payload["trades_seen"] == 1
    assert market_payload["rows"][0]["market_id"] == "m1"

    wallet_result = run_script(db_path, "--dry-run", "--json", "--wallet-id", "wallet-x")
    assert json.loads(wallet_result.stdout)["trades_seen"] == 0


def test_lock_conflict_fails_cleanly_on_apply(db: Database, db_path: Path, tmp_path: Path):
    insert_trade(db)
    db.conn.commit()
    lock_path = tmp_path / "ops.lock"
    with operational_job_lock("test-holder", lock_path=lock_path, timeout=1.0):
        result = run_script(db_path, "--apply", "--lock-timeout", "0", lock_path=lock_path)
    assert result.returncode == 3
    assert "operational lock unavailable" in result.stderr
    assert ledger_rows(db) == []


def test_same_market_multi_fill_writes_two_rows_and_summary(db: Database, db_path: Path):
    insert_trade(db, id="fill1", source_trade_id="external-fill1", price=0.40, quantity=50)
    insert_trade(db, id="fill2", source_trade_id="external-fill2", price=0.55, quantity=50)
    db.conn.commit()
    result = run_script(db_path, "--apply", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(ledger_rows(db)) == 2
    assert payload["total_realized_pnl"] == pytest.approx(52.5)
    assert payload["total_cost_basis"] == pytest.approx(47.5)
    assert payload["roi"] == pytest.approx(52.5 / 47.5)


def test_sell_trade_excluded_without_fake_pnl(db: Database, db_path: Path):
    insert_trade(db, id="sell", source_trade_id="external-sell", side="SELL")
    db.conn.commit()
    result = run_script(db_path, "--apply", "--json")
    assert result.returncode == 0, result.stderr
    row = ledger_rows(db)[0]
    assert row["accounting_status"] == "excluded_unsupported_side"
    assert row["accounting_reason"] == "sell_side_accounting_not_supported_in_pr24i"
    assert row["realized_pnl"] is None
