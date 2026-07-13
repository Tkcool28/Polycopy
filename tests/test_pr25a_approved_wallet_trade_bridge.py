# ruff: noqa: E701, E702
"""PR25A tmp-db safety, identity, evidence, and allowlist tests."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any

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

TOKENS = [
    "104431860535489654020481219089291817898241901940037260095979653681449084465327",
    "1970496541508335019913900195809032484597886384784144327835472760880523550630",
    "462547474504332232595082342285851716602015351553019365447058575920118967359469",
]


def _sqlite_readonly(path: str):
    """Read-only sqlite facade matching the CLI's real _ReadOnlyDb interface
    (fetchall/fetchone/close), so tests never touch Database().connect()."""
    class _Facade:
        def __init__(self, p: str) -> None:
            self.conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
        def fetchall(self, sql: str, params: tuple = ()) -> list:
            return list(self.conn.execute(sql, params).fetchall())
        def fetchone(self, sql: str, params: tuple = ()) -> "object":
            return self.conn.execute(sql, params).fetchone()
        def close(self) -> None:
            self.conn.close()
    return _Facade(path)


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "bridge.db").connect()


def _trade(db: Database, *, internal="t1", public="polymarket:public-1", source=SOURCE_NAME, side="BUY", sample=0, outcome="Yes", token="tok1", timestamp="2026-01-01T00:00:00Z"):
    db.execute("""INSERT INTO source_trades (id, source, source_trade_id, market_source_id, side, outcome, quantity, price, trader_address, timestamp, is_sample, token_id)
    VALUES (?, ?, ?, 'condition-1', ?, ?, 2, .5, ?, ?, ?, ?)""", (internal, source, public, side, outcome, WALLET, timestamp, sample, token))
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


def test_selection_skips_already_bridged_trades_anti_replay(tmp_path):
    """PR25A anti-replay: a plain --limit N must advance past bridged trades.

    The bridge persists ``copy_candidates.source_trade_internal_id`` from
    ``source_trades.id``. Selection must exclude any source trade already
    represented by a copy_candidate, without altering the canonical
    ``timestamp ASC, source_trade_id ASC, id ASC`` ordering.

    Uses the real production write path to create the copy_candidate so the
    anti-replay exclusion is exercised against the exact row shape the bridge
    emits (no hand-built INSERT that could drift from schema NOT NULLs).
    """
    db = _db(tmp_path)
    try:
        _trade(db, internal="t1", public="polymarket:public-1",
               timestamp="2026-01-01T00:00:00Z")
        _trade(db, internal="t2", public="polymarket:public-2",
               timestamp="2026-01-02T00:00:00Z")
        _trade(db, internal="t3", public="polymarket:public-3",
               timestamp="2026-01-03T00:00:00Z")
        # Bridge t1 for real (write mode) so a genuine copy_candidate exists.
        deps = BridgeDependencies(gamma=_Gamma(), clob=_Book(_valid_book()))
        first = process_approved_wallet_trades(
            db, wallet=WALLET, limit=1, dependencies=deps,
            write=True, write_authorization=_issue_write_capability(),
        )
        assert first.rows and first.rows[0]["stages"]["trade_copyability"] == "persisted"
        assert db.fetchone("SELECT COUNT(*) AS n FROM copy_candidates")["n"] == 1
        # limit=2 must now skip t1 and return the two fresh trades in order.
        selected = [r["id"] for r in select_approved_source_trades(db, WALLET, limit=2)]
        assert selected == ["t2", "t3"], selected
        # Explicit --source-trade-id still bypasses the exclusion (targeted re-run).
        targeted = [r["id"] for r in select_approved_source_trades(
            db, WALLET, limit=2, source_trade_id="polymarket:public-1")]
        assert targeted == ["t1"], targeted
    finally:
        db.close()


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
    second = process_approved_wallet_trades(
        db, wallet=WALLET, limit=1, dependencies=deps, write=True,
        write_authorization=_issue_write_capability(),
        source_trade_id="polymarket:public-1",
    )
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


class _CallCountingGamma:
    """Async Gamma that returns a market whose outcome's clob_token_id matches
    the requested condition's source token (so exact token->outcome mapping
    succeeds per row) and raises on a chosen 1-based call index (default: never)."""
    def __init__(self, *, tokens=TOKENS, raise_on_call=None, exc=None):
        self.tokens = tokens
        self.raise_on_call = raise_on_call
        self.exc = exc or RuntimeError("gamma boom")
        self.calls = 0
    async def get_market(self, condition_id: str):
        self.calls += 1
        if self.raise_on_call is not None and self.calls == self.raise_on_call:
            raise self.exc
        # Map condition-N -> TOKENS[N] so the exact token mapping resolves.
        idx = 0
        if condition_id.startswith("condition-"):
            try:
                idx = int(condition_id.split("-", 1)[1])
            except ValueError:
                idx = 0
        tok = self.tokens[idx] if 0 <= idx < len(self.tokens) else (self.tokens[0] if self.tokens else "tok")
        return Market(source_id=condition_id, source="polymarket", question="Q",
                      outcomes=[MarketOutcome(label="Yes", price=.5, clob_token_id=tok)],
                      fetched_at=datetime.now(timezone.utc))


class _CallCountingBook:
    """Async CLOB that raises on a chosen 1-based call index (default: never)."""
    def __init__(self, book, *, raise_on_call=None, exc=None):
        self.book = book
        self.raise_on_call = raise_on_call
        self.exc = exc or RuntimeError("clob boom")
        self.calls = 0
        self.seen = []
    async def fetch_book(self, token_id: str):
        self.calls += 1
        self.seen.append(token_id)
        if self.raise_on_call is not None and self.calls == self.raise_on_call:
            raise self.exc
        return self.book


def _seed_three(db, *, tokens=TOKENS):
    for i, tok in enumerate(tokens):
        db.execute(
            """INSERT INTO source_trades (id, source, source_trade_id, market_source_id, side, outcome, quantity, price, trader_address, timestamp, is_sample, token_id)
               VALUES (?, ?, ?, ?, 'BUY', 'Yes', 2, .5, ?, '2026-01-01T00:00:00Z', 0, ?)""",
            (f"t{i}", SOURCE_NAME, f"polymarket:public-{i}", f"condition-{i}", WALLET, tok),
        )
    db.conn.commit()


def _assert_full_path(row):
    assert row["stages"]["source_validation"] == "ok"
    assert row["stages"]["gamma"] == "ok"
    assert row["stages"]["clob_preflight"] == "ok"
    assert row["stages"].get("trade_copyability") in {
        "copy_candidate", "watchlist", "skip", "incomplete",
    }
    assert row["stages"].get("paper") == "evaluated"
    assert row["actions"]
    assert "trade_copyability_v1" in row["actions"]
    assert "canonical_paper" in row["actions"]


def test_three_successful_rows_complete_on_one_loop(tmp_path):
    db = _db(tmp_path)
    _seed_three(db)
    gamma = _CallCountingGamma()
    book = _CallCountingBook(_valid_book())
    report = process_approved_wallet_trades(
        db, wallet=WALLET, limit=3,
        dependencies=BridgeDependencies(gamma=gamma, clob=book),
    )
    assert report.selected == 3
    assert report.failures == [], report.failures
    assert len(report.rows) == 3
    for row in report.rows:
        _assert_full_path(row)
    # Gamma + CLOB each called exactly once per row, in order, on one loop.
    assert gamma.calls == 3
    assert book.calls == 3
    assert book.seen == TOKENS
    db.close()


def test_middle_row_gamma_raises_isolates_failure_and_skips_clob(tmp_path):
    db = _db(tmp_path)
    _seed_three(db)
    gamma = _CallCountingGamma(raise_on_call=2)
    book = _CallCountingBook(_valid_book())
    report = process_approved_wallet_trades(
        db, wallet=WALLET, limit=3,
        dependencies=BridgeDependencies(gamma=gamma, clob=book),
    )
    # Rows 1 and 3 still complete; row 2 records gamma_error.
    assert report.selected == 3
    assert any(r["skip_reason"] == "gamma_error:RuntimeError" for r in report.rows)
    assert any(r["source_trade_id_prefix"].endswith("public-0") for r in report.rows if "trade_copyability" in r["stages"])
    assert any(r["source_trade_id_prefix"].endswith("public-2") for r in report.rows if "trade_copyability" in r["stages"])
    # CLOB is never called for row 2 (Gamma failed first).
    assert book.seen == [TOKENS[0], TOKENS[2]], book.seen
    # Rows 1 and 3 must have completed the full dry-run path.
    completed = [r for r in report.rows if "trade_copyability" in r["stages"]]
    assert len(completed) == 2, [r["source_trade_id_prefix"] for r in report.rows]
    for r in completed:
        _assert_full_path(r)
    db.close()


def test_middle_row_clob_raises_isolates_failure(tmp_path):
    db = _db(tmp_path)
    _seed_three(db)
    gamma = _CallCountingGamma()
    book = _CallCountingBook(_valid_book(), raise_on_call=2)
    report = process_approved_wallet_trades(
        db, wallet=WALLET, limit=3,
        dependencies=BridgeDependencies(gamma=gamma, clob=book),
    )
    assert report.selected == 3
    assert any(r["skip_reason"] == "clob_error:RuntimeError" for r in report.rows)
    # Rows 1 and 3 still complete the full path.
    completed = [r for r in report.rows if "trade_copyability" in r["stages"]]
    assert len(completed) == 2
    for r in completed:
        _assert_full_path(r)
    # Gamma called for all 3 (it precedes CLOB); CLOB attempted for all 3 but
    # the 2nd raised.
    assert gamma.calls == 3
    assert book.calls == 3
    db.close()


def test_source_invalid_row_calls_neither_gamma_nor_clob(tmp_path):
    db = _db(tmp_path)
    # One source-invalid row (non-finite price).
    db.execute(
        """INSERT INTO source_trades (id, source, source_trade_id, market_source_id, side, outcome, quantity, price, trader_address, timestamp, is_sample, token_id)
           VALUES ('bad', ?, 'polymarket:bad', 'condition-1', 'BUY', 'Yes', 2, 0, ?, '2026-01-01T00:00:00Z', 0, 'tok-bad')""",
        (SOURCE_NAME, WALLET),
    )
    db.conn.commit()
    gamma = _CallCountingGamma(tokens=["tok-bad"])
    book = _CallCountingBook(_valid_book())
    report = process_approved_wallet_trades(
        db, wallet=WALLET, limit=3,
        dependencies=BridgeDependencies(gamma=gamma, clob=book),
    )
    assert report.selected == 1
    assert report.failures and report.failures[0]["reason"] == "invalid_price_or_quantity"
    # Neither Gamma nor CLOB was contacted for the invalid row.
    assert gamma.calls == 0, gamma.calls
    assert book.calls == 0, book.calls
    db.close()


def test_dry_run_full_tc_paper_evaluation_zero_writes(tmp_path):
    db = _db(tmp_path)
    _seed_three(db)
    gamma = _CallCountingGamma()
    book = _CallCountingBook(_valid_book())
    before_counts = _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES)
    report = process_approved_wallet_trades(
        db, wallet=WALLET, limit=3,
        dependencies=BridgeDependencies(gamma=gamma, clob=book),
    )
    assert report.mode == "ro"
    for row in report.rows:
        _assert_full_path(row)
    assert report.write_counts == {}
    assert report.forbidden_table_delta == {}
    after_counts = _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES)
    assert after_counts == before_counts
    db.close()


def test_write_path_guarded_ordering_one_failure_does_not_abort_batch(tmp_path):
    db = _db(tmp_path)
    # Row 0: valid, should persist through write path.
    # Row 1: invalid source -> skipped before any write, no Gamma/CLOB.
    # Row 2: valid, should persist through write path.
    _seed_three(db)
    # Make row 1's source invalid by overwriting its price after seeding.
    db.execute("UPDATE source_trades SET price=0 WHERE id='t1'")
    db.conn.commit()
    gamma = _CallCountingGamma()
    book = _CallCountingBook(_valid_book())
    auth = _issue_write_capability()
    report = process_approved_wallet_trades(
        db, wallet=WALLET, limit=3, write=True, write_authorization=auth,
        dependencies=BridgeDependencies(gamma=gamma, clob=book),
    )
    # Rows 0 and 2 persisted (copy_candidate in actions), row 1 skipped on source.
    persisted = [r for r in report.rows if "candidate" in r["actions"]]
    assert len(persisted) == 2, [r["source_trade_id_prefix"] for r in report.rows]
    skipped = [r for r in report.rows if r["skip_reason"] == "invalid_price_or_quantity"]
    assert len(skipped) == 1
    # The only failure is the single source-invalid row; the batch was NOT
    # aborted before reporting/rolling back the other rows.
    assert report.failures == [
        {"source_trade_id": skipped[0]["source_trade_id_prefix"], "reason": "invalid_price_or_quantity"}
    ], report.failures
    # Exactly the allowlisted write tables changed; forbidden unchanged.
    assert report.forbidden_table_delta == {}
    assert report.write_counts.get("copy_candidates", 0) == 2
    # Gamma called only for the two valid rows (source-invalid row skipped first).
    assert gamma.calls == 2, gamma.calls
    assert book.calls == 2, book.calls
    db.close()


# ── PR25A CLI async client lifecycle fix ────────────────────────────────────

class _LoopRecordingGamma:
    """Gamma mock that records the running loop on each async request."""

    def __init__(self):
        self.loops: list = []
        self.calls = 0

    async def get_market(self, condition_id: str) -> "Any":
        import asyncio as _asyncio
        self.loops.append(_asyncio.get_running_loop())
        self.calls += 1
        return _make_gamma_market(condition_id)


class _LoopRecordingBook:
    """CLOB mock that records the running loop on each async fetch."""

    def __init__(self):
        self.loops: list = []
        self.calls = 0

    async def fetch_book(self, token_id: str) -> "Any":
        import asyncio as _asyncio
        self.loops.append(_asyncio.get_running_loop())
        self.calls += 1
        return _valid_book()


class _LoopRecordingClosable:
    """Async closable that records the loop its aclose ran on."""

    def __init__(self):
        self.aclose_loops: list = []
        self.aclose_calls = 0

    async def aclose(self) -> None:
        import asyncio as _asyncio
        self.aclose_loops.append(_asyncio.get_running_loop())
        self.aclose_calls += 1

    def __call__(self, loop: "Any") -> "Any":
        return self.aclose()


def _make_gamma_market(condition_id: str = "condition-0") -> "Any":
    """Return a real Market whose clob_token_id matches the seeded token for the
    requested condition, so the exact token->outcome mapping succeeds."""
    from polycopy.domain.market import Market, MarketOutcome

    idx = 0
    if condition_id.startswith("condition-"):
        try:
            idx = int(condition_id.split("-", 1)[1])
        except ValueError:
            idx = 0
    tok = TOKENS[idx] if 0 <= idx < len(TOKENS) else (TOKENS[0] if TOKENS else "tok")
    return Market(
        source_id=condition_id, source="polymarket", question="Q",
        outcomes=[MarketOutcome(label="Yes", price=0.5, clob_token_id=tok)],
        fetched_at=datetime.now(timezone.utc),
    )


def test_bridge_closes_client_hooks_on_same_loop_as_requests(tmp_path):
    """REQUIRED #1-3: the same single event loop drives Gamma/CLOB requests AND
    client aclose. Proves no second-loop aclose, so asyncio.run(aclose) on a
    fresh loop is never needed."""
    import asyncio as _asyncio
    db = _db(tmp_path)
    _seed_three(db)
    gamma = _LoopRecordingGamma()
    book = _LoopRecordingBook()
    closable = _LoopRecordingClosable()
    before_counts = _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES)
    report = process_approved_wallet_trades(
        db, wallet=WALLET, limit=3,
        dependencies=BridgeDependencies(gamma=gamma, clob=book),
        client_close_hooks=[closable],
    )
    assert report.failures == [], report.failures
    # Aclose ran exactly once, on a captured loop.
    assert closable.aclose_calls == 1, closable.aclose_calls
    assert len(closable.aclose_loops) == 1
    # Request loop identity == cleanup loop identity (the bridge's batch loop).
    request_loop = gamma.loops[0]
    cleanup_loop = closable.aclose_loops[0]
    assert isinstance(request_loop, _asyncio.AbstractEventLoop)
    assert request_loop is cleanup_loop, "request loop must be the SAME object as cleanup loop"
    # CLOB ran on the same loop too.
    assert book.loops[0] is request_loop
    # Successful path records no cleanup errors and writes nothing.
    assert report.cleanup_errors == []
    assert _counts(db, ALLOWED_WRITE_TABLES | FORBIDDEN_WRITE_TABLES) == before_counts
    db.close()


def test_bridge_records_cleanup_error_without_raising_report(tmp_path):
    """Required design: a failing client hook is recorded on the report's
    cleanup_errors and the BridgeReport is STILL returned (not erased)."""
    db = _db(tmp_path)
    _seed_three(db)
    gamma = _CallCountingGamma()
    book = _CallCountingBook(_valid_book())

    def _boom(loop):
        raise RuntimeError("simulated aclose failure")

    report = process_approved_wallet_trades(
        db, wallet=WALLET, limit=3,
        dependencies=BridgeDependencies(gamma=gamma, clob=book),
        client_close_hooks=[_boom],
    )
    # Report is returned with rows produced; cleanup error recorded separately.
    assert report.selected == 3
    assert report.cleanup_errors, "cleanup error must be recorded, not raised"
    assert report.cleanup_errors[0]["type"] == "RuntimeError"
    assert "simulated aclose failure" in report.cleanup_errors[0]["error"]
    db.close()


def test_cli_dry_run_cleanup_failure_still_prints_json(tmp_path, capsys, monkeypatch):
    """REQUIRED #4: cleanup hook raises -> valid JSON STILL on stdout, report has
    selected rows/stages, stderr reports cleanup failure, exit code = 1."""
    cli = _load_cli_module()
    monkeypatch.setattr(cli, "resolve_wallet", lambda value: WALLET)
    monkeypatch.setattr(
        cli, "Settings",
        lambda: SimpleNamespace(
            gamma_base_url="https://example.invalid", clob_base_url="https://example.invalid",
            clob_max_retries=1, clob_rpm=1,
        ),
    )
    monkeypatch.setattr(cli, "PolymarketPublicAdapter", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(cli, "PolymarketClobClient", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda **k: SimpleNamespace())
    from polycopy.db.database import Database as _DB
    real_db = _DB(tmp_path / "cli.db").connect()
    _trade(real_db)
    real_db.close()
    monkeypatch.setattr(cli.Database, "connect", lambda self: pytest.fail("dry-run must not connect writable Database"))
    monkeypatch.setattr(cli, "_ReadOnlyDb", lambda path: _sqlite_readonly(str(path)))
    # Stub returns a completed report WITH a recorded cleanup error (the bridge's
    # cleanup_errors transport) so we exercise the CLI's print-then-stderr path.
    def _stub_bridge(*a, **k):
        return type(
            "Report", (),
            {"cleanup_errors": [{"type": "RuntimeError", "error": "simulated aclose failure"}],
             "as_dict": lambda self: {
                 "mode": "ro", "wallet": WALLET, "limit": 1, "selected": 1,
                 "rows": [{"source_trade_id_prefix": "pm:x", "stages": {"source_validation": "ok"}, "actions": []}],
                 "failures": [], "write_counts": {}, "forbidden_table_delta": {},
                 "cleanup_errors": [{"type": "RuntimeError", "error": "simulated aclose failure"}],
             }},
        )()
    monkeypatch.setattr(cli, "process_approved_wallet_trades", _stub_bridge)

    rc = cli.main(["--wallet", WALLET, "--limit", "1", "--db-path", str(tmp_path / "cli.db"), "--json"])
    captured = capsys.readouterr()
    import json as _json
    payload = _json.loads(captured.out)  # valid JSON on stdout
    assert payload["selected"] == 1
    assert payload["rows"][0]["stages"]["source_validation"] == "ok"
    assert payload["cleanup_errors"], "cleanup error must appear in the JSON"
    assert "Event loop is closed" not in captured.err, captured.err
    assert "cleanup failed" in captured.err, captured.err  # stderr reported it
    assert rc == 1, captured.err  # exit 1 because cleanup failed
    assert payload["write_counts"] == {}


def test_cli_dry_run_row_failure_still_prints_json(tmp_path, capsys, monkeypatch):
    """REQUIRED #5: a row-level failure still prints JSON and exits 1."""
    cli = _load_cli_module()
    monkeypatch.setattr(cli, "resolve_wallet", lambda value: WALLET)
    monkeypatch.setattr(
        cli, "Settings",
        lambda: SimpleNamespace(
            gamma_base_url="https://example.invalid", clob_base_url="https://example.invalid",
            clob_max_retries=1, clob_rpm=1,
        ),
    )
    monkeypatch.setattr(cli, "PolymarketPublicAdapter", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(cli, "PolymarketClobClient", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda **k: SimpleNamespace())
    from polycopy.db.database import Database as _DB
    real_db = _DB(tmp_path / "cli.db").connect()
    _trade(real_db)
    real_db.close()
    monkeypatch.setattr(cli.Database, "connect", lambda self: pytest.fail("dry-run must not connect writable Database"))
    monkeypatch.setattr(cli, "_ReadOnlyDb", lambda path: _sqlite_readonly(str(path)))
    monkeypatch.setattr(
        cli, "process_approved_wallet_trades",
        lambda *a, **k: type(
            "Report", (),
            {"cleanup_errors": [],
             "as_dict": lambda self: {
                 "mode": "ro", "wallet": WALLET, "limit": 1, "selected": 1,
                 "rows": [{"source_trade_id_prefix": "pm:x", "stages": {}, "actions": []}],
                 "failures": [{"source_trade_id": "pm:x", "reason": "clob_evidence_invalid"}],
                 "write_counts": {}, "forbidden_table_delta": {}, "cleanup_errors": [],
             }},
        )(),
    )
    rc = cli.main(["--wallet", WALLET, "--limit", "1", "--db-path", str(tmp_path / "cli.db"), "--json"])
    captured = capsys.readouterr()
    import json as _json
    payload = _json.loads(captured.out)
    assert payload["failures"], "row-level failure should remain visible"
    assert "Event loop is closed" not in captured.err, captured.err
    assert rc == 1, captured.err
    assert payload["write_counts"] == {}


def test_cli_dry_run_success_prints_json_no_event_loop_closed(tmp_path, capsys, monkeypatch):
    """REQUIRED #6: successful dry-run prints JSON, no Event loop is closed, exit
    follows report failures (0 here)."""
    cli = _load_cli_module()
    monkeypatch.setattr(cli, "resolve_wallet", lambda value: WALLET)
    monkeypatch.setattr(
        cli, "Settings",
        lambda: SimpleNamespace(
            gamma_base_url="https://example.invalid", clob_base_url="https://example.invalid",
            clob_max_retries=1, clob_rpm=1,
        ),
    )
    monkeypatch.setattr(cli, "PolymarketPublicAdapter", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(cli, "PolymarketClobClient", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda **k: SimpleNamespace())
    from polycopy.db.database import Database as _DB
    real_db = _DB(tmp_path / "cli.db").connect()
    _trade(real_db)
    real_db.close()
    monkeypatch.setattr(cli.Database, "connect", lambda self: pytest.fail("dry-run must not connect writable Database"))
    monkeypatch.setattr(cli, "_ReadOnlyDb", lambda path: _sqlite_readonly(str(path)))
    monkeypatch.setattr(
        cli, "process_approved_wallet_trades",
        lambda *a, **k: type(
            "Report", (),
            {"cleanup_errors": [],
             "as_dict": lambda self: {
                 "mode": "ro", "wallet": WALLET, "limit": 1, "selected": 1, "rows": [],
                 "failures": [], "write_counts": {}, "forbidden_table_delta": {}, "cleanup_errors": [],
             }},
        )(),
    )
    rc = cli.main(["--wallet", WALLET, "--limit", "1", "--db-path", str(tmp_path / "cli.db"), "--json"])
    captured = capsys.readouterr()
    import json as _json
    payload = _json.loads(captured.out)
    assert payload["mode"] == "ro"
    assert "Event loop is closed" not in captured.err, captured.err
    assert rc == 0, captured.err
    assert payload["write_counts"] == {}


def test_cli_dry_run_zero_db_writes_and_stdout_report(tmp_path, capsys, monkeypatch):
    """REQUIRED #7 + #8: write_counts stays {} and the DB is untouched."""
    cli = _load_cli_module()
    monkeypatch.setattr(cli, "resolve_wallet", lambda value: WALLET)
    monkeypatch.setattr(
        cli, "Settings",
        lambda: SimpleNamespace(
            gamma_base_url="https://example.invalid", clob_base_url="https://example.invalid",
            clob_max_retries=1, clob_rpm=1,
        ),
    )
    monkeypatch.setattr(cli, "PolymarketPublicAdapter", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(cli, "PolymarketClobClient", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda **k: SimpleNamespace())
    # Seed the readable DB BEFORE monkeypatching Database.connect (which would
    # refuse any connect, including our own seed).
    from polycopy.db.database import Database as _DB
    real_db = _DB(tmp_path / "cli.db").connect()
    _trade(real_db)
    real_db.close()
    before = (tmp_path / "cli.db").stat()
    monkeypatch.setattr(cli.Database, "connect", lambda self: pytest.fail("dry-run must not connect writable Database"))
    monkeypatch.setattr(cli, "_ReadOnlyDb", lambda path: _sqlite_readonly(str(path)))
    monkeypatch.setattr(
        cli, "process_approved_wallet_trades",
        lambda *a, **k: type(
            "Report", (),
            {"cleanup_errors": [],
             "as_dict": lambda self: {
                 "mode": "ro", "wallet": WALLET, "limit": 1, "selected": 1, "rows": [],
                 "failures": [], "write_counts": {}, "forbidden_table_delta": {}, "cleanup_errors": [],
             }},
        )(),
    )
    rc = cli.main(["--wallet", WALLET, "--limit", "1", "--db-path", str(tmp_path / "cli.db"), "--json"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    after = (tmp_path / "cli.db").stat()
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    assert "Event loop is closed" not in captured.err, captured.err
