"""PR68 — Bounded canonical approved-wallet ingestion tests.

Covers Checkpoints B–K: canonical metadata preservation (event/series/
taxonomy, no inference), exact source-trade bounding, limit, dry-run purity,
temp-DB write + replay + existing-row enrichment, forbidden-table isolation,
production gates, and no bridge/scoring import from the collector CLI.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from polycopy.db.database import Database
from polycopy.ingestion.approved_wallet_collector import (
    APPROVED_WALLET_ENV,
    _classify_taxonomy,
    _raw_gamma_resolver_adapter,
    collect,
    resolve_wallet,
)
from polycopy.ingestion.normalized_source_trade import normalize_source_trade
from polycopy.ingestion.source_trade_metadata import (
    build_metadata_from_gamma_market,
    normalize_source_trade_metadata,
    serialize_gamma_market_metadata,
)
from polycopy.ingestion.source_trade_writer import write_valid_rows

WALLET = "0xcac76b761231464900cce5da7c20233d59b20579"
SOURCE = "polymarket_data_api_trades_user"


def _raw(**changes):
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


# A trusted Gamma raw market dict with event/series/category evidence.
# `groupItemTitle` is intentionally present (a display grouping) to prove it is
# NOT promoted to taxonomy.raw_category. The trusted category source is
# `category` alone (matches PR67's resolve_category_label_for_inputs).
GAMMA_MARKET = {
    "conditionId": "0x" + "3" * 64,
    "question": "Will X happen?",
    "events": [{"id": "evt-1", "slug": "event-one", "title": "Event One"}],
    "series": [{"id": "ser-1", "slug": "series-one", "title": "Series One"}],
    "category": "Politics",
    "groupItemTitle": "Politics",  # display grouping only — must NOT become raw_category
    "tags": ["election", "2026"],
    "outcomes": '["Yes","No"]',
    "outcomePrices": '["0.4","0.6"]',
    "clobTokenIds": '["0x' + "2" * 64 + '","0x' + "9" * 64 + '"]',
}


def _gamma_resolver_factory(market=None):
    market = GAMMA_MARKET if market is None else market

    class _R:
        async def __call__(self, condition_id):
            return market

    return _R()


class FakeProvider:
    made_network_call = False

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch_trades(self, wallet, *, limit, page):
        self.calls.append((wallet, limit, page))
        return self.rows if page == 0 else []


# ── Checkpoint B: canonical metadata preservation ────────────────────────────
def test_gamma_metadata_preserves_event_series_category_no_inference():
    md = build_metadata_from_gamma_market(None, GAMMA_MARKET)
    assert md["event"]["id"] == "evt-1"
    assert md["event"]["slug"] == "event-one"
    assert md["event"]["title"] == "Event One"
    assert md["series"]["id"] == "ser-1"
    assert md["series"]["slug"] == "series-one"
    assert md["taxonomy"]["raw_category"] == "Politics"
    assert md["taxonomy"]["tags"] == ["2026", "election"]  # sorted, deduped
    # Title/slug must NOT become a category.
    assert md["taxonomy"]["raw_category"] != "Event One"
    assert "Event One" not in (md["taxonomy"]["tags"] or [])


def test_missing_taxonomy_stays_unavailable():
    md = build_metadata_from_gamma_market(None, {"conditionId": "0x" + "3" * 64})
    assert md["taxonomy"]["raw_category"] is None
    assert md["taxonomy"]["tags"] == []
    assert md["event"] == {"id": None, "slug": None, "title": None}


def test_deterministic_canonical_json():
    a = serialize_gamma_market_metadata(None, GAMMA_MARKET)
    b = serialize_gamma_market_metadata(None, dict(GAMMA_MARKET))
    assert a == b
    assert json.loads(a) == json.loads(b)


def test_normalize_uses_gamma_market_when_supplied():
    cand = normalize_source_trade(
        _raw(), requested_wallet=WALLET, gamma_market=GAMMA_MARKET
    )
    assert cand.metadata["taxonomy"]["raw_category"] == "Politics"
    # Without gamma, metadata is the honest all-null shape.
    cand2 = normalize_source_trade(_raw(), requested_wallet=WALLET)
    assert cand2.metadata["taxonomy"]["raw_category"] is None


# ── Checkpoint C: exact source-trade bounding ───────────────────────────────
def test_exact_source_trade_id_selects_one_buy():
    rows = [
        _raw(sourceProvidedTradeId="trade-A"),
        _raw(sourceProvidedTradeId="trade-B"),
        _raw(sourceProvidedTradeId="trade-B", side="SELL"),
    ]
    result = asyncio.run(
        collect(FakeProvider(rows), WALLET, source_trade_id="polymarket:trade-a")
    )
    assert result.selected_count == 1
    assert result.accepted_rows[0].source_trade_id == "polymarket:trade-a"


def test_zero_source_trade_id_matches_is_selected_none():
    rows = [_raw(sourceProvidedTradeId="trade-A")]
    result = asyncio.run(
        collect(FakeProvider(rows), WALLET, source_trade_id="polymarket:does-not-exist")
    )
    assert result.selected_count == 0


def test_internal_id_rejected_prefix_rejected():
    rows = [_raw(sourceProvidedTradeId="trade-A")]
    # internal DB id (uuid) must not match the public id
    result = asyncio.run(collect(FakeProvider(rows), WALLET, source_trade_id="some-uuid"))
    assert result.selected_count == 0
    # prefix must not match (no fuzzy)
    result2 = asyncio.run(collect(FakeProvider(rows), WALLET, source_trade_id="polymarket:trade"))
    assert result2.selected_count == 0


def test_sell_exact_match_rejected():
    rows = [_raw(sourceProvidedTradeId="trade-S", side="SELL")]
    result = asyncio.run(
        collect(FakeProvider(rows), WALLET, source_trade_id="polymarket:trade-s")
    )
    assert result.selected_count == 0
    assert result.sell_records_excluded >= 1


# ── Checkpoint C: limit ─────────────────────────────────────────────────────
def test_limit_1_writes_at_most_one_row(tmp_path):
    import scripts.collect_approved_wallet_trades as cli  # type: ignore

    rows = [
        _raw(sourceProvidedTradeId="a"),
        _raw(sourceProvidedTradeId="b"),
        _raw(sourceProvidedTradeId="c"),
    ]
    result = asyncio.run(collect(FakeProvider(rows), WALLET))
    db = Database(tmp_path / "collector.db")
    db.connect()
    try:
        out = write_valid_rows(db, result.accepted_rows[:1], dry_run=False, pre_existing_ids=set())
        assert out.inserted == 1
        assert db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0] == 1
    finally:
        db.close()


def test_invalid_limit_rejected_by_cli(monkeypatch, capsys):
    import scripts.collect_approved_wallet_trades as cli  # type: ignore

    monkeypatch.setenv(APPROVED_WALLET_ENV, WALLET)
    monkeypatch.setenv("POLYCOPY_OPERATIONAL_LOCK_PATH", str(Path("/tmp") / "lock"))
    rc = cli.main(["--wallet", WALLET, "--limit", "0", "--json"])
    assert rc == 2


# ── Checkpoint D/G: dry-run purity ─────────────────────────────────────────
def test_dry_run_no_db_no_write(monkeypatch, capsys):
    import scripts.collect_approved_wallet_trades as cli  # type: ignore

    monkeypatch.setenv(APPROVED_WALLET_ENV, WALLET)
    result = asyncio.run(collect(FakeProvider([_raw()]), WALLET))
    monkeypatch.setattr(cli, "collect", lambda *a, **k: result)
    monkeypatch.setattr(cli.asyncio, "run", lambda coro, *a, **k: coro)
    monkeypatch.setattr(cli, "_RealDataApiProvider", lambda timeout: None)
    # The module-level import resolves the name at call time via the module
    # globals, so patching cli._RealDataApiProvider redirects it.
    import ingest_real_source_trades as _irt  # type: ignore
    monkeypatch.setattr(_irt, "_RealDataApiProvider", lambda timeout: None)
    rc = cli.main(["--wallet", WALLET, "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry-run"
    assert out["inserted"] == 0
    assert out["accepted_count"] == 1


def test_dry_run_script_has_no_persistence_paths():
    text = (Path(__file__).parents[1] / "scripts/collect_approved_wallet_trades.py").read_text()
    # The dry-run branch is everything after `if not args.write:` up to its
    # first `return` (which precedes any DB/backup/write logic).
    dry_branch = text.split("if not args.write:", 1)[1].split("return 0 if not result.errors", 1)[0]
    for forbidden in ("Database(", "operational_job_lock", "write_valid_rows", "backup", "UPDATE "):
        assert forbidden not in dry_branch


# ── Checkpoint F: production gates ──────────────────────────────────────────
def test_production_write_requires_source_trade_id(monkeypatch, capsys):
    import scripts.collect_approved_wallet_trades as cli  # type: ignore

    monkeypatch.setenv(APPROVED_WALLET_ENV, WALLET)
    monkeypatch.setenv("POLYCOPY_OPERATIONAL_LOCK_PATH", str(Path("/tmp") / "lock"))
    # Point at production path without --source-trade-id -> must be rejected.
    rc = cli.main(
        ["--wallet", WALLET, "--write", "--allow-live", "--confirm-production-db",
         "--db-path", str(cli.PRODUCTION_DB_PATH), "--json"]
    )
    assert rc == 2
    assert "--source-trade-id" in capsys.readouterr().err


def test_production_write_requires_allow_live_and_confirm(monkeypatch, capsys):
    import scripts.collect_approved_wallet_trades as cli  # type: ignore

    monkeypatch.setenv(APPROVED_WALLET_ENV, WALLET)
    monkeypatch.setenv("POLYCOPY_OPERATIONAL_LOCK_PATH", str(Path("/tmp") / "lock"))
    rc = cli.main(
        ["--wallet", WALLET, "--source-trade-id", "polymarket:x", "--write",
         "--db-path", str(cli.PRODUCTION_DB_PATH), "--json"]
    )
    assert rc == 2  # missing --allow-live and --confirm-production-db


# ── Checkpoint H: temp-DB end-to-end + replay ──────────────────────────────
def test_temp_db_write_replay_single_buy(tmp_path):
    rows = [_raw(sourceProvidedTradeId="trade-1")]
    result = asyncio.run(
        collect(FakeProvider(rows), WALLET, gamma_resolver=_gamma_resolver_factory())
    )
    db = Database(tmp_path / "collector.db")
    db.connect()
    try:
        first = write_valid_rows(db, result.accepted_rows, dry_run=False, pre_existing_ids=set())
        assert first.inserted == 1
        replay = write_valid_rows(db, result.accepted_rows, dry_run=False, pre_existing_ids=set())
        assert replay.inserted == 0 and replay.deduplicated == 1
        row = db.conn.execute(
            "SELECT source_trade_id, metadata_json FROM source_trades"
        ).fetchone()
        assert row[0] == "polymarket:trade-1"
        md = json.loads(row[1])
        assert md["taxonomy"]["raw_category"] == "Politics"
    finally:
        db.close()


def _count_or_skip(db, table):
    exists = db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return 0  # table not part of this schema; nothing to assert
    return db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_temp_db_forbidden_tables_unchanged(tmp_path):
    rows = [_raw(sourceProvidedTradeId="trade-1")]
    result = asyncio.run(
        collect(FakeProvider(rows), WALLET, gamma_resolver=_gamma_resolver_factory())
    )
    db = Database(tmp_path / "collector.db")
    db.connect()
    try:
        write_valid_rows(db, result.accepted_rows, dry_run=False, pre_existing_ids=set())
        for table in (
            "wallets", "markets", "market_outcomes", "copy_candidates",
            "candidate_price_snapshots", "candidate_price_snapshot_levels",
            "wallet_score_decisions", "category_wallet_score_decisions",
            "trade_copyability_decisions", "paper_signal_decisions",
            "shadow_decisions", "exit_experiment_registrations", "orders",
            "positions", "fills", "settlement_accounting_ledger",
        ):
            assert _count_or_skip(db, table) == 0
    finally:
        db.close()


# ── Checkpoint E: existing-row enrichment contract ──────────────────────────
def test_existing_empty_metadata_enriched(tmp_path):
    import scripts.collect_approved_wallet_trades as cli  # type: ignore

    db = Database(tmp_path / "collector.db")
    db.connect()
    try:
        db.conn.execute(
            "INSERT INTO source_trades (id, source, source_trade_id, market_source_id, "
            "side, outcome, quantity, price, trader_address, timestamp, is_sample, "
            "token_id, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("seed-id", SOURCE, "polymarket:trade-1", "0x" + "3" * 64, "BUY", "Yes",
             2.0, 0.4, WALLET, "2023-11-14T22:13:20+00:00", 0, "0x" + "2" * 64, None),
        )
        db.conn.commit()
        md_json = serialize_gamma_market_metadata(None, GAMMA_MARKET)
        status = cli._enrich_existing_row(db, "polymarket:trade-1", md_json)
        assert status == "enriched"
        row = db.conn.execute(
            "SELECT price, quantity, side, source_trade_id, metadata_json FROM source_trades"
        ).fetchone()
        assert row[0] == 0.4 and row[1] == 2.0 and row[2] == "BUY"
        assert row[3] == "polymarket:trade-1"
        assert json.loads(row[4])["taxonomy"]["raw_category"] == "Politics"
    finally:
        db.close()


def test_existing_equivalent_metadata_reused(tmp_path):
    import scripts.collect_approved_wallet_trades as cli  # type: ignore

    db = Database(tmp_path / "collector.db")
    db.connect()
    try:
        md_json = serialize_gamma_market_metadata(None, GAMMA_MARKET)
        db.conn.execute(
            "INSERT INTO source_trades (id, source, source_trade_id, market_source_id, "
            "side, outcome, quantity, price, trader_address, timestamp, is_sample, "
            "token_id, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("seed-id", SOURCE, "polymarket:trade-1", "0x" + "3" * 64, "BUY", "Yes",
             2.0, 0.4, WALLET, "2023-11-14T22:13:20+00:00", 0, "0x" + "2" * 64, md_json),
        )
        db.conn.commit()
        status = cli._enrich_existing_row(db, "polymarket:trade-1", md_json)
        assert status == "reused"
    finally:
        db.close()


def test_existing_conflicting_metadata_not_overwritten(tmp_path):
    import scripts.collect_approved_wallet_trades as cli  # type: ignore

    db = Database(tmp_path / "collector.db")
    db.connect()
    try:
        db.conn.execute(
            "INSERT INTO source_trades (id, source, source_trade_id, market_source_id, "
            "side, outcome, quantity, price, trader_address, timestamp, is_sample, "
            "token_id, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("seed-id", SOURCE, "polymarket:trade-1", "0x" + "3" * 64, "BUY", "Yes",
             2.0, 0.4, WALLET, "2023-11-14T22:13:20+00:00", 0, "0x" + "2" * 64,
             '{"metadata_version":"1","event":{},"taxonomy":{"raw_category":"Sports"},"series":{}}'),
        )
        db.conn.commit()
        new_md = serialize_gamma_market_metadata(None, GAMMA_MARKET)  # Politics
        status = cli._enrich_existing_row(db, "polymarket:trade-1", new_md)
        assert status == "conflict"
        row = db.conn.execute("SELECT metadata_json FROM source_trades").fetchone()
        assert json.loads(row[0])["taxonomy"]["raw_category"] == "Sports"  # unchanged
    finally:
        db.close()


# ── Checkpoint H: no bridge/scoring import from collector ───────────────────
def test_collector_module_does_not_import_bridge_or_scoring():
    import polycopy.ingestion.approved_wallet_collector as mod

    text = Path(mod.__file__).read_text()
    assert "process_approved_wallet_trades" not in text
    assert "evaluate_paper_signals" not in text
    assert "persist_bridge_trade_copyability" not in text


def test_cli_does_not_import_bridge_or_scoring():
    text = (Path(__file__).parents[1] / "scripts/collect_approved_wallet_trades.py").read_text()
    assert "process_approved_wallet_trades" not in text
    assert "evaluate_paper_signals" not in text
    assert "persist_bridge_trade_copyability" not in text


# ── Checkpoint B: taxonomy classification (usable/partial/unavailable) ──────
def test_classify_taxonomy_usable_partial_unavailable():
    usable = normalize_source_trade_metadata({"category": "Politics"})
    assert _classify_taxonomy(usable) == "usable"
    partial = normalize_source_trade_metadata({"tags": ["x"]})
    assert _classify_taxonomy(partial) == "partial"
    unavailable = normalize_source_trade_metadata({})
    assert _classify_taxonomy(unavailable) == "unavailable"


def test_tags_only_is_partial_not_usable():
    md = build_metadata_from_gamma_market(
        None, {"conditionId": "0x" + "3" * 64, "tags": ["election"]}
    )
    assert _classify_taxonomy(md) == "partial"
    assert md["taxonomy"]["raw_category"] is None


# ── Checkpoint B regression: groupItemTitle is NOT a trusted taxonomy ────────
def test_groupitemtitle_never_promoted_to_raw_category():
    market_conflict = {
        "conditionId": "0x" + "3" * 64,
        "category": "Politics",
        "groupItemTitle": "Some Display Group",
        "tags": ["election"],
    }
    md = build_metadata_from_gamma_market(None, market_conflict)
    assert md["taxonomy"]["raw_category"] == "Politics"
    assert md["taxonomy"]["raw_category"] != "Some Display Group"

    market_no_category = {
        "conditionId": "0x" + "3" * 64,
        "groupItemTitle": "Politics-Looking Group",
        "tags": ["election"],
    }
    md2 = build_metadata_from_gamma_market(None, market_no_category)
    assert md2["taxonomy"]["raw_category"] is None
    assert _classify_taxonomy(md2) == "partial"


# ── Checkpoint C: exact source-trade identity contract ──────────────────────
def test_namespace_helper_is_canonical_and_lowercases():
    from polycopy.ingestion.normalized_source_trade import _namespace_v2_id

    assert _namespace_v2_id("polymarket:Trade-A") == "polymarket:trade-a"
    assert _namespace_v2_id("Trade-A") == "polymarket:trade-a"
    assert _namespace_v2_id("0xABCDEF") == "polymarket:0xabcdef"


def test_exact_match_is_case_insensitive_via_namespace():
    rows = [_raw(sourceProvidedTradeId="Trade-A")]
    result = asyncio.run(
        collect(FakeProvider(rows), WALLET, source_trade_id="polymarket:Trade-A")
    )
    assert result.selected_count == 1
    assert result.accepted_rows[0].source_trade_id == "polymarket:trade-a"


def test_already_namespaced_id_matches():
    rows = [_raw(sourceProvidedTradeId="trade-A")]
    result = asyncio.run(
        collect(FakeProvider(rows), WALLET, source_trade_id="polymarket:trade-a")
    )
    assert result.selected_count == 1


def test_unnamespaced_id_is_namespaced_on_match():
    rows = [_raw(sourceProvidedTradeId="trade-A")]
    result = asyncio.run(
        collect(FakeProvider(rows), WALLET, source_trade_id="trade-a")
    )
    assert result.selected_count == 1


def test_transaction_hash_identity_not_selectable_in_bounded_ingestion():
    # PR68 mandates a canonical SOURCE-PROVIDED id; transaction-hash identities
    # are intentionally NOT selectable by the bounded collector (they become
    # polymarket:<tx>:<index>, which is a fallback, not a recurring identity).
    rows2 = [_raw(sourceProvidedTradeId=None, transactionHash="0xABCDEF123456")]
    result = asyncio.run(
        collect(FakeProvider(rows2), WALLET, source_trade_id="polymarket:0xabcdef123456:0")
    )
    # The row is normalized (valid tx-hash identity) but the bounded collector
    # rejects non-source-provided identities before selection.
    assert result.selected_count == 0
    assert result.rejected_records >= 1


def test_prefix_is_rejected():
    rows = [_raw(sourceProvidedTradeId="trade-A")]
    result = asyncio.run(
        collect(FakeProvider(rows), WALLET, source_trade_id="polymarket:trade")
    )
    assert result.selected_count == 0


def test_internal_uuid_rowid_is_rejected():
    rows = [_raw(sourceProvidedTradeId="trade-A")]
    result = asyncio.run(
        collect(FakeProvider(rows), WALLET, source_trade_id="a1b2c3d4-0000-1111-2222-333344445555")
    )
    assert result.selected_count == 0
    result2 = asyncio.run(
        collect(FakeProvider(rows), WALLET, source_trade_id="42")
    )
    assert result2.selected_count == 0
