# ruff: noqa: E701, E702
"""PR25A tmp-db safety, identity, evidence, and allowlist tests."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from polycopy.adapters.polymarket_clob import ClobBook, ClobBookLevel
from polycopy.db.database import Database
from polycopy.domain.market import Market, MarketOutcome
from polycopy.engine import approved_wallet_trade_bridge as bridge_mod
from polycopy.engine.approved_wallet_trade_bridge import (
    ALLOWED_WRITE_TABLES, FORBIDDEN_WRITE_TABLES, BridgeDependencies,
    MAX_LIMIT, _issue_write_capability, process_approved_wallet_trades,
    select_approved_source_trades, validate_limit,
)
from polycopy.ingestion.normalized_source_trade import SOURCE_NAME

WALLET = "0x" + "a" * 40


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "bridge.db").connect()


def _trade(db: Database, *, internal="t1", public="polymarket:public-1", source=SOURCE_NAME, side="BUY", sample=0, outcome="Yes", token="tok1"):
    db.execute("""INSERT INTO source_trades (id, source, source_trade_id, market_source_id, side, outcome, quantity, price, trader_address, timestamp, is_sample, token_id)
    VALUES (?, ?, ?, 'condition-1', ?, ?, 2, .5, ?, '2026-01-01T00:00:00Z', ?, ?)""", (internal, source, public, side, outcome, WALLET, sample, token))
    db.conn.commit()


class _Gamma:
    def __init__(self, *, label="Yes", token="tok1", condition="condition-1"):
        self.label, self.token, self.condition = label, token, condition
    def get_market(self, condition_id: str) -> Market:
        return Market(source_id=self.condition, source="polymarket", question="Q", outcomes=[MarketOutcome(label=self.label, price=.5, clob_token_id=self.token)], fetched_at=datetime.now(timezone.utc))


class _Book:
    def __init__(self, book=None, exc=None): self.book, self.exc, self.calls = book, exc, 0
    async def fetch_book(self, token_id):
        self.calls += 1
        if self.exc: raise self.exc
        return self.book


def _valid_book():
    return ClobBook(token_id="tok1", bids=[ClobBookLevel(.49, 10)], asks=[ClobBookLevel(.51, 10)])


def _counts(db, names):
    existing = {r["name"] for r in db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    return {n: db.fetchone(f"SELECT COUNT(*) AS n FROM {n}")["n"] for n in names if n in existing}


def test_selection_is_source_qualified_buy_only_non_sample_deterministic_and_public_id_filtered(tmp_path):
    db = _db(tmp_path)
    try:
        _trade(db, internal="t2", public="public-2")
        _trade(db, internal="t1", public="public-1")
        _trade(db, internal="sell", public="sell", side="SELL")
        _trade(db, internal="sample", public="sample", sample=1)
        _trade(db, internal="other-source", public="other", source="other")
        assert validate_limit(1) == 1
        for invalid in (0, -1, MAX_LIMIT + 1, True):
            with pytest.raises(ValueError): validate_limit(invalid)
        assert [r["id"] for r in select_approved_source_trades(db, WALLET, limit=2)] == ["t1", "t2"]
        assert [r["id"] for r in select_approved_source_trades(db, WALLET, limit=2, source_trade_id="public-2")] == ["t2"]
        assert not select_approved_source_trades(db, WALLET, limit=2, source_trade_id="t2")
    finally: db.close()


def test_dry_run_hydrates_and_preflights_but_mutates_no_tables_or_metadata(tmp_path):
    db = _db(tmp_path); _trade(db)
    path = tmp_path / "bridge.db"; before_stat = path.stat(); before = _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES)
    book = _Book(_valid_book())
    report = process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=BridgeDependencies(gamma=_Gamma(), clob=book))
    after = _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES); after_stat = path.stat()
    assert report.mode == "ro" and book.calls == 1 and report.rows[0]["actions"]
    assert before == after
    assert (before_stat.st_size, before_stat.st_mtime_ns) == (after_stat.st_size, after_stat.st_mtime_ns)
    db.close()


@pytest.mark.parametrize("book", [None, ClobBook(token_id="tok1"), ClobBook(token_id="tok1", bids=[ClobBookLevel(.49, 1)])])
def test_invalid_or_unavailable_clob_evidence_creates_no_candidate(tmp_path, book):
    db = _db(tmp_path); _trade(db)
    report = process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=BridgeDependencies(gamma=_Gamma(), clob=None if book is None else _Book(book)), write=True, write_authorization=_issue_write_capability())
    assert _counts(db, {"copy_candidates", "candidate_price_snapshots", "paper_signal_decisions"}) == {"copy_candidates": 0, "candidate_price_snapshots": 0, "paper_signal_decisions": 0}
    assert report.rows[0]["skip_reason"] in {"no_book_provider", "clob_evidence_invalid"}
    db.close()


@pytest.mark.parametrize("gamma", [_Gamma(label="No"), _Gamma(token="wrong"), _Gamma(condition="wrong")])
def test_gamma_mapping_conflicts_fail_closed_before_candidate(tmp_path, gamma):
    db = _db(tmp_path); _trade(db)
    report = process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=BridgeDependencies(gamma=gamma, clob=_Book(_valid_book())), write=True, write_authorization=_issue_write_capability())
    assert _counts(db, {"copy_candidates", "markets", "market_outcomes"}) == {"copy_candidates": 0, "markets": 0, "market_outcomes": 0}
    assert report.rows[0]["skip_reason"]
    db.close()


def test_write_authorization_wallet_preservation_allowlist_levels_and_replay(tmp_path):
    db = _db(tmp_path); _trade(db)
    with pytest.raises(PermissionError):
        process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book())), write=True)
    before_forbidden = _counts(db, FORBIDDEN_WRITE_TABLES)
    deps = BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book()))
    first = process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    mid = _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES)
    second = process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    assert first.forbidden_table_delta == second.forbidden_table_delta == {}
    assert _counts(db, FORBIDDEN_WRITE_TABLES) == before_forbidden
    assert _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES) == mid
    candidate = db.fetchone("SELECT source_trade_id, source_trade_internal_id FROM copy_candidates")
    assert (candidate["source_trade_id"], candidate["source_trade_internal_id"]) == ("polymarket:public-1", "t1")
    assert db.fetchone("SELECT COUNT(*) AS n FROM candidate_price_snapshot_levels")["n"] == 2
    assert db.fetchone("SELECT is_approved FROM paper_signal_decisions")["is_approved"] == 0
    db.close()


def test_persisted_outcome_conflict_is_rejected_without_destructive_refresh(tmp_path):
    db = _db(tmp_path); _trade(db)
    db.execute("INSERT INTO markets (id, source_id, source, question, fetched_at) VALUES ('m', 'condition-1', 'polymarket', 'old', '2026-01-01T00:00:00Z')")
    db.execute("INSERT INTO market_outcomes (market_id, label, price, volume, clob_token_id) VALUES ('m', 'No', .5, 0, 'tok1')"); db.conn.commit()
    report = process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book())), write=True, write_authorization=_issue_write_capability())
    assert report.rows[0]["skip_reason"] == "persisted_mapping_conflict"
    assert db.fetchone("SELECT question FROM markets WHERE id='m'")["question"] == "old"
    assert _counts(db, {"copy_candidates"})["copy_candidates"] == 0
    db.close()


def test_read_only_facade_rejects_direct_write(tmp_path):
    db = _db(tmp_path); _trade(db); db.close()
    conn = sqlite3.connect(f"file:{tmp_path / 'bridge.db'}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError): conn.execute("INSERT INTO wallets (id,address,created_at) VALUES ('x','x','x')")
    conn.close()


@pytest.mark.skip(reason="PR25A uses the narrow canonical paper persistence primitive")
def test_write_uses_frozen_v1_scorer_and_serialization_owner_fail_closed(monkeypatch, tmp_path):
    """Missing persisted score evidence must reach the frozen V1 scorer unchanged."""
    db = _db(tmp_path)
    _trade(db)
    scorer_calls = []
    persist_calls = []
    real_scorer = bridge_mod.compute_trade_score_v1
    real_persist = bridge_mod.persist_trade_score_v1

    def score(*args, **kwargs):
        scorer_calls.append((args, kwargs))
        return real_scorer(*args, **kwargs)

    def persist(*args, **kwargs):
        persist_calls.append((args, kwargs))
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(bridge_mod, "compute_trade_score_v1", score)
    monkeypatch.setattr(bridge_mod, "persist_trade_score_v1", persist)
    deps = BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book()))
    for _ in range(2):
        process_approved_wallet_trades(
            db,
            wallet=WALLET,
            limit=1,
            dependencies=deps,
            write=True,
            write_authorization=_issue_write_capability(),
        )

    assert len(scorer_calls) == len(persist_calls) == 2
    row = db.fetchone(
        "SELECT formula_name, formula_version, verdict, final_score, "
        "missing_essentials_json, rejection_reasons_json FROM trade_copyability_decisions"
    )
    assert dict(row) == {
        "formula_name": "trade_copyability",
        "formula_version": "1",
        "verdict": "incomplete",
        "final_score": 0.0,
        "missing_essentials_json": '["executable_depth", "seconds_to_market_end"]',
        "rejection_reasons_json": "[]",
    }
    assert db.fetchone("SELECT COUNT(*) AS n FROM trade_copyability_decisions")["n"] == 1
    db.close()


def _load_cli_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "process_approved_wallet_trades.py"
    spec = importlib.util.spec_from_file_location("pr25a_cli_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Closable:
    def close(self):
        return None

    async def aclose(self):
        return None


def test_cli_write_uses_operational_lock_and_rss_guards(monkeypatch, tmp_path):
    cli = _load_cli_module()
    events = []

    class _Db:
        def close(self):
            events.append("close")

    monkeypatch.setattr(cli, "resolve_wallet", lambda value: WALLET)
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(
            gamma_base_url="https://example.invalid",
            clob_base_url="https://example.invalid",
            clob_max_retries=1,
            clob_rpm=1,
        ),
    )
    monkeypatch.setattr(cli, "PolymarketPublicAdapter", lambda *a, **k: _Closable())
    monkeypatch.setattr(cli, "PolymarketClobClient", lambda *a, **k: _Closable())
    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda **k: _Closable())
    monkeypatch.setattr(cli, "operational_job_lock", lambda *a, **k: events.append(("lock", a, k)) or nullcontext())
    monkeypatch.setattr(cli, "check_rss_limit", lambda stage, limit: events.append(("rss", stage, limit)))
    monkeypatch.setattr(cli, "get_max_rss_mb_from_env", lambda: 321)
    monkeypatch.setattr(cli.Database, "connect", lambda self: events.append("connect") or _Db())
    monkeypatch.setattr(
        cli,
        "process_approved_wallet_trades",
        lambda *a, **k: events.append(("process", k)) or type("Report", (), {"as_dict": lambda self: {"mode": "rw", "wallet": WALLET, "limit": 1, "selected": 0, "rows": [], "failures": [], "write_counts": {}, "forbidden_table_delta": {}}})(),
    )

    assert cli.main(["--wallet", WALLET, "--limit", "1", "--write", "--db-path", str(tmp_path / "x.db"), "--json"]) == 0
    assert [event[1] for event in events if isinstance(event, tuple) and event[0] == "rss"] == ["pr25a:before-write", "pr25a:after-write"]
    assert any(isinstance(event, tuple) and event[0] == "lock" for event in events)
    assert any(event == "connect" for event in events)
    assert next(event for event in events if isinstance(event, tuple) and event[0] == "process")[1]["write"] is True


def test_cli_dry_run_is_read_only_and_never_calls_persistence(monkeypatch, tmp_path):
    cli = _load_cli_module()
    events = []

    monkeypatch.setattr(cli, "resolve_wallet", lambda value: WALLET)
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(
            gamma_base_url="https://example.invalid",
            clob_base_url="https://example.invalid",
            clob_max_retries=1,
            clob_rpm=1,
        ),
    )
    monkeypatch.setattr(cli, "PolymarketPublicAdapter", lambda *a, **k: _Closable())
    monkeypatch.setattr(cli, "PolymarketClobClient", lambda *a, **k: _Closable())
    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda **k: _Closable())
    monkeypatch.setattr(cli.Database, "connect", lambda self: pytest.fail("dry-run must not connect writable Database"))
    monkeypatch.setattr(cli, "_ReadOnlyDb", lambda path: events.append(("readonly", path)) or _Closable())
    monkeypatch.setattr(bridge_mod, "persist_bridge_incomplete_paper_signal", lambda *a, **k: pytest.fail("dry-run must not persist paper signals"))
    monkeypatch.setattr(
        cli,
        "process_approved_wallet_trades",
        lambda *a, **k: events.append(("process", k)) or type("Report", (), {"as_dict": lambda self: {"mode": "ro", "wallet": WALLET, "limit": 1, "selected": 0, "rows": [], "failures": [], "write_counts": {}, "forbidden_table_delta": {}}})(),
    )

    assert cli.main(["--wallet", WALLET, "--limit", "1", "--db-path", str(tmp_path / "x.db"), "--json"]) == 0
    process = next(event for event in events if event[0] == "process")
    assert process[1]["write"] is False
