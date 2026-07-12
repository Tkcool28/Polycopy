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
from polycopy.scoring import paper_signal as paper_signal_mod

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


def test_pr25a_dry_run_clob_receives_source_token_as_token_id_and_reports_complete_would_write(tmp_path):
    """PR25A integration regression for the CLOB token_id fix.

    Using a mocked CLOB provider (no live network), assert that:
      * Gamma hydration succeeds for the source trade,
      * the bridge passes the source trade's ``token_id`` verbatim to the
        CLOB provider's ``fetch_book`` (the exact contract the fix targets),
      * valid bids/asks reach the bridge CLOB preflight,
      * the dry-run reports a complete would-write path (non-empty actions),
      * and ZERO rows are persisted (dry-run, mode 'ro').
    """
    db = _db(tmp_path)
    TOKEN = "104431860535489654020481219089291817898241901940037260095979653681449084465327"
    # Seed a trade carrying the exact token id the contract expects.
    db.execute(
        """INSERT INTO source_trades (id, source, source_trade_id, market_source_id, side, outcome, quantity, price, trader_address, timestamp, is_sample, token_id)
           VALUES ('t1', ?, 'polymarket:public-1', 'condition-1', 'BUY', 'Yes', 2, .5, ?, '2026-01-01T00:00:00Z', 0, ?)""",
        (SOURCE_NAME, WALLET, TOKEN),
    )
    db.conn.commit()

    class _RecordingBook:
        def __init__(self, book): self.book, self.received_token = book, None
        async def fetch_book(self, token_id):
            self.received_token = token_id
            return self.book

    book = _RecordingBook(_valid_book())
    gamma = _Gamma(label="Yes", token=TOKEN, condition="condition-1")
    # Snapshot counts AFTER seeding, before the dry-run executes.
    before_counts = _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES)
    report = process_approved_wallet_trades(
        db, wallet=WALLET, limit=1, dependencies=BridgeDependencies(gamma=gamma, clob=book),
    )
    # CLOB provider received the exact source token, unchanged.
    assert book.received_token == TOKEN
    # Gamma hydration + exact token->outcome mapping succeeded.
    row = report.rows[0]
    assert row["stages"]["gamma"] == "ok"
    assert row["stages"]["source_validation"] == "ok"
    # Valid bids/asks reached preflight -> complete would-write path.
    assert row["stages"]["clob_preflight"] not in ("clob_evidence_invalid", "clob_error")
    assert row["actions"], "expected a non-empty would-write action list"
    assert report.mode == "ro"
    # No persistence in dry-run.
    assert report.write_counts == {}
    assert report.forbidden_table_delta == {}
    # No rows persisted in any allowlist/forbidden table during dry-run.
    after_counts = _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES)
    assert after_counts == before_counts
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


def test_write_persists_frozen_trade_copyability_v1_once_and_replay_is_idempotent(tmp_path):
    db = _db(tmp_path); _trade(db)
    deps = BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book()))
    first = process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    assert first.rows[0]["stages"]["trade_copyability"] == "persisted"
    assert db.fetchone("SELECT COUNT(*) AS n FROM trade_copyability_decisions")["n"] == 1
    decision = db.fetchone("SELECT formula_name, formula_version, verdict FROM trade_copyability_decisions")
    assert (decision["formula_name"], decision["formula_version"]) == ("trade_copyability", "1")
    assert decision["verdict"] in {"copy_candidate", "watchlist", "skip", "incomplete"}
    assert db.fetchone("SELECT signal_reason FROM paper_signal_decisions")["signal_reason"] != "bridge_score_evidence_unavailable"
    second = process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    assert second.rows[0]["stages"]["trade_copyability"] == "persisted"
    assert db.fetchone("SELECT COUNT(*) AS n FROM trade_copyability_decisions")["n"] == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM paper_signal_decisions")["n"] == 1
    db.close()


@pytest.mark.parametrize("stage", ["snapshot", "depth", "copyability", "paper"])
def test_late_stage_failure_rolls_back_entire_trade(monkeypatch, tmp_path, stage):
    db = _db(tmp_path); _trade(db)
    before = _counts(db, ALLOWED_WRITE_TABLES)
    if stage == "snapshot":
        monkeypatch.setattr(bridge_mod, "persist_price_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("snapshot boom")))
    elif stage == "depth":
        monkeypatch.setattr(bridge_mod, "persist_depth_levels", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("depth boom")))
    elif stage == "copyability":
        monkeypatch.setattr(paper_signal_mod, "persist_trade_score_v1", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("copyability boom")))
    else:
        monkeypatch.setattr(paper_signal_mod, "persist_paper_signal", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("paper boom")))
    report = process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book())), write=True, write_authorization=_issue_write_capability())
    assert report.rows[0]["skip_reason"]
    assert _counts(db, ALLOWED_WRITE_TABLES) == before
    db.close()


def test_oversized_depth_is_capped_before_persistence_and_malformed_fails_closed(monkeypatch, tmp_path):
    db = _db(tmp_path); _trade(db)
    bids = [ClobBookLevel(.49 - i / 1000, 1) for i in range(30)]
    asks = [ClobBookLevel(.51 + i / 1000, 1) for i in range(30)]
    seen = []
    real = bridge_mod.persist_depth_levels
    def bounded(*args, **kwargs):
        seen.append((len(args[2]), len(args[3])))
        return real(*args, **kwargs)
    monkeypatch.setattr(bridge_mod, "persist_depth_levels", bounded)
    deps = BridgeDependencies(gamma=_Gamma(), clob=_Book(ClobBook(token_id="tok1", bids=bids, asks=asks)))
    process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    assert seen == [(bridge_mod.BRIDGE_MAX_DEPTH_LEVELS_PER_SIDE, bridge_mod.BRIDGE_MAX_DEPTH_LEVELS_PER_SIDE)]
    assert db.fetchone("SELECT COUNT(*) AS n FROM candidate_price_snapshot_levels")["n"] == 2 * bridge_mod.BRIDGE_MAX_DEPTH_LEVELS_PER_SIDE
    process_approved_wallet_trades(db, wallet=WALLET, limit=1, dependencies=deps, write=True, write_authorization=_issue_write_capability())
    assert db.fetchone("SELECT COUNT(*) AS n FROM candidate_price_snapshot_levels")["n"] == 2 * bridge_mod.BRIDGE_MAX_DEPTH_LEVELS_PER_SIDE
    db.close()

    malformed = _db(tmp_path / "malformed"); _trade(malformed)
    bad = ClobBook(token_id="tok1", bids=[SimpleNamespace(price="NaN", size=1)], asks=[ClobBookLevel(.51, 1)])
    report = process_approved_wallet_trades(malformed, wallet=WALLET, limit=1, dependencies=BridgeDependencies(gamma=_Gamma(), clob=_Book(bad)), write=True, write_authorization=_issue_write_capability())
    assert report.rows[0]["skip_reason"] == "DEPTH_LEVELS_MALFORMED"
    assert _counts(malformed, ALLOWED_WRITE_TABLES)["copy_candidates"] == 0
    malformed.close()


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
    monkeypatch.setattr(bridge_mod, "persist_bridge_trade_copyability_v1", lambda *a, **k: pytest.fail("dry-run must not persist paper signals"))
    monkeypatch.setattr(
        cli,
        "process_approved_wallet_trades",
        lambda *a, **k: events.append(("process", k)) or type("Report", (), {"as_dict": lambda self: {"mode": "ro", "wallet": WALLET, "limit": 1, "selected": 0, "rows": [], "failures": [], "write_counts": {}, "forbidden_table_delta": {}}})(),
    )

    assert cli.main(["--wallet", WALLET, "--limit", "1", "--db-path", str(tmp_path / "x.db"), "--json"]) == 0
    process = next(event for event in events if event[0] == "process")
    assert process[1]["write"] is False


def test_pr25a_dry_run_processes_three_rows_in_one_event_loop_with_full_tc_and_paper_evaluation(tmp_path):
    """Final PR25A harness consolidation regression.

    Runs a 3-row batch through the dry-run with SHARED async Gamma/CLOB
    clients (both providers are genuinely async, so the batch exercises the
    single-event-loop fetch path). All three rows must complete the full
    Gamma -> exact token mapping -> CLOB -> depth normalization -> Trade
    Copyability v1 -> paper-signal evaluation path with NO RuntimeError and
    NO closed event loop, and report non-empty would-write actions. The
    dry-run must still persist ZERO rows and leave every allowlisted and
    forbidden table unchanged.
    """
    db = _db(tmp_path)
    TOKENS = [
        "104431860535489654020481219089291817898241901940037260095979653681449084465327",
        "1970496541508335019913900195809032484597886384784144327835472760880523550630",
        "462547474504332232595082342285851716602015351553019365447058575920118967359469",
    ]
    for i, tok in enumerate(TOKENS):
        db.execute(
            """INSERT INTO source_trades (id, source, source_trade_id, market_source_id, side, outcome, quantity, price, trader_address, timestamp, is_sample, token_id)
               VALUES (?, ?, ?, 'condition-1', 'BUY', 'Yes', 2, .5, ?, '2026-01-01T00:00:00Z', 0, ?)""",
            (f"t{i}", SOURCE_NAME, f"polymarket:public-{i}", WALLET, tok),
        )
    db.conn.commit()

    class _AsyncGamma:
        async def get_market(self, condition_id: str):
            # Async provider -> exercises the single shared event loop.
            # Return one outcome per source token so the exact token->outcome
            # mapping succeeds for every selected row.
            return Market(source_id="condition-1", source="polymarket", question="Q",
                          outcomes=[MarketOutcome(label="Yes", price=.5, clob_token_id=tok)
                                    for tok in TOKENS],
                          fetched_at=datetime.now(timezone.utc))

    class _RecordingBook:
        def __init__(self, book, received):
            self.book, self.received = book, received
        async def fetch_book(self, token_id):
            self.received.append(token_id)
            return self.book

    received_tokens: list[str] = []
    book = _RecordingBook(_valid_book(), received_tokens)
    before_counts = _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES)
    before_stat = (tmp_path / "bridge.db").stat()

    report = process_approved_wallet_trades(
        db, wallet=WALLET, limit=3,
        dependencies=BridgeDependencies(gamma=_AsyncGamma(), clob=book),
    )

    # No RuntimeError / closed-loop failure leaked into skip reasons.
    assert report.failures == [], report.failures
    assert report.selected == 3
    assert len(report.rows) == 3
    # Shared async client received the exact source tokens, in order, once each.
    assert received_tokens == TOKENS, received_tokens

    tc_evals = 0
    paper_evals = 0
    for row in report.rows:
        assert row["stages"]["source_validation"] == "ok"
        assert row["stages"]["gamma"] == "ok"
        assert row["stages"]["clob_preflight"] == "ok"
        # Exact token mapping succeeded.
        assert row["stages"].get("trade_copyability") in {
            "copy_candidate", "watchlist", "skip", "incomplete",
        }
        assert row["stages"].get("paper") == "evaluated"
        tc_evals += 1
        paper_evals += 1
        # Non-empty would-write actions for every row.
        assert row["actions"], "expected non-empty would-write actions"
        assert "trade_copyability_v1" in row["actions"]
        assert "canonical_paper" in row["actions"]

    assert tc_evals == 3
    assert paper_evals == 3
    # Dry-run persists nothing.
    assert report.mode == "ro"
    assert report.write_counts == {}
    assert report.forbidden_table_delta == {}
    after_counts = _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES)
    assert after_counts == before_counts
    after_stat = (tmp_path / "bridge.db").stat()
    assert (before_stat.st_size, before_stat.st_mtime_ns) == (after_stat.st_size, after_stat.st_mtime_ns)
    db.close()
