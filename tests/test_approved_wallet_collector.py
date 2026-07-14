from __future__ import annotations
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from polycopy.db.database import Database
from polycopy.ingestion.source_trade_writer import write_valid_rows
from polycopy.ingestion.approved_wallet_collector import (
    APPROVED_WALLET_ENV,
    MAX_PAGES,
    MAX_RECORDS,
    UnsafeCollectorConfiguration,
    collect,
    resolve_wallet,
)

WALLET = "0xcac76b761231464900cce5da7c20233d59b20579"


def raw(**changes):
    row = {
        "sourceProvidedTradeId": "trade-1",
        "proxyWallet": WALLET,
        "asset": "0x" + "2" * 64,
        "conditionId": "0x" + "3" * 64,
        "side": "BUY",
        "price": "0.4",
        "size": "2",
        "timestamp": 1700000000,
    }
    row.update(changes)
    return row


class FakeProvider:
    made_network_call = False

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch_trades(self, wallet, *, limit, page):
        self.calls.append((wallet, limit, page))
        return self.rows if page == 0 else []


@pytest.mark.parametrize("value", [None, "", WALLET + ",0x" + "1" * 40, "bad"])
def test_requires_one_well_formed_wallet(value):
    with pytest.raises(UnsafeCollectorConfiguration):
        resolve_wallet(None, {APPROVED_WALLET_ENV: value or ""})


def test_cli_wallet_must_match_configured_wallet():
    assert resolve_wallet(WALLET.upper(), {APPROVED_WALLET_ENV: WALLET}) == WALLET
    with pytest.raises(UnsafeCollectorConfiguration):
        resolve_wallet("0x" + "1" * 40, {APPROVED_WALLET_ENV: WALLET})


def test_collector_only_accepts_buy_source_provided_identity_and_is_bounded():
    provider = FakeProvider(
        [raw(), raw(sourceProvidedTradeId="trade-2", side="SELL"), raw(sourceProvidedTradeId=None)]
    )
    result = asyncio.run(collect(provider, WALLET))
    assert provider.calls == [(WALLET, MAX_RECORDS, 0)] and MAX_PAGES == 1
    assert [r.source_trade_id for r in result.accepted_rows] == ["polymarket:trade-1"]
    assert result.sell_records_excluded == 1
    assert result.fallback_identities == 1
    assert result.legacy_aliases_used == 0
    assert result.rejected_records >= 2


def test_true_no_write_script_has_no_persistence_paths():
    text = (Path(__file__).parents[1] / "scripts/collect_approved_wallet_trades.py").read_text()
    # Dry-run branch is everything after `if not args.write:` up to its first
    # `return` (which precedes any DB/backup/write logic).
    no_write_branch = text.split("if not args.write:", 1)[1].split(
        "return 0 if not result.errors", 1
    )[0]
    for forbidden in (
        "Database(",
        "operational_job_lock",
        "write_valid_rows",
        "backup",
        "snapshot",
        "experiment",
    ):
        assert forbidden not in no_write_branch


def test_service_template_uses_safe_command_and_stays_timer_free():
    text = (
        Path(__file__).parents[1] / "deploy-units/polycopy-approved-wallet-collect.service.template"
    ).read_text()
    assert "collect_approved_wallet_trades.py --write" in text
    assert "POLYCOPY_MAX_RSS_MB=512" in text
    assert ".timer" not in text and "[Install]" not in text
    assert "PrivateTmp=true" not in text


def test_first_write_and_replay_use_single_canonical_writer(tmp_path):
    result = asyncio.run(collect(FakeProvider([raw()]), WALLET))
    db = Database(tmp_path / "collector.db")
    db.connect()
    try:
        first = write_valid_rows(db, result.accepted_rows, dry_run=False)
        replay = write_valid_rows(db, result.accepted_rows, dry_run=False)
        assert (first.attempted, first.inserted, first.deduplicated, first.committed) == (1, 1, 0, True)
        assert (replay.attempted, replay.inserted, replay.deduplicated, replay.committed) == (1, 0, 1, True)
        assert db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0] == 1
        for table in ("wallet_score_decisions", "copy_candidates", "paper_signal_decisions", "orders", "positions"):
            assert db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        db.close()


def test_write_report_recognizes_existing_source_name_row(tmp_path, monkeypatch, capsys):
    script_path = Path(__file__).parents[1] / "scripts/collect_approved_wallet_trades.py"
    monkeypatch.syspath_prepend(str(script_path.parent))
    spec = importlib.util.spec_from_file_location("approved_wallet_cli_test", script_path)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    result = asyncio.run(collect(FakeProvider([raw()]), WALLET))

    class Provider:
        async def aclose(self):
            return None

    monkeypatch.setenv(APPROVED_WALLET_ENV, WALLET)
    monkeypatch.setenv("POLYCOPY_OPERATIONAL_LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.setattr(cli, "_RealDataApiProvider", lambda timeout: Provider())
    # New async API: patch collect + asyncio.run so no network is hit.
    monkeypatch.setattr(cli, "collect", lambda *a, **k: result)
    monkeypatch.setattr(cli.asyncio, "run", lambda coro, *a, **k: coro)
    db_path = tmp_path / "collector.db"
    assert cli.main(["--write", "--db-path", str(db_path)]) == 0
    capsys.readouterr()
    assert cli.main(["--write", "--db-path", str(db_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    # First write inserted; replay recognized the existing canonical row.
    assert report["inserted"] == 0
    assert report["deduplicated"] == 1
    assert report["existing_canonical_records"] == 1
    assert report["new_canonical_records"] == 0
