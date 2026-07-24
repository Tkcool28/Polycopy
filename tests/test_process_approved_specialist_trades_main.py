"""Direct main() regressions for the bounded approved-specialist CLI.

These tests replace network/stage dependencies at the CLI module boundary while
using a unique temporary SQLite file for every invocation.  They deliberately
drive ``main()`` rather than a helper so runner/cache/cleanup ownership stays
observable.
"""
from __future__ import annotations

import ast
import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for candidate in (ROOT / "src", ROOT / "scripts", ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import scripts.process_approved_specialist_trades as cli  # noqa: E402
from polycopy.db.database import Database as RealDatabase  # noqa: E402
from tests.fixtures.specialist_paper_fixtures import (  # noqa: E402
    FakeClob,
    FakeGamma,
    RESOLVED_MARKET_COUNT,
    create_approval_for_target,
    make_target_trade,
    seed_resolved_evidence,
)


APPROVAL_ID = "approval-test-id"
CONDITION_ID = "condition-test-id"
SOURCE_TRADE_ID = "source-trade-test-id"
_REAL_RUNNER = asyncio.Runner


class _TempDatabase:
    """Minimal CLI database facade backed by one disposable SQLite database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE source_trades (id TEXT PRIMARY KEY, source_trade_id TEXT, source TEXT)"
        )
        self.closed = 0

    def connect(self):
        return self

    def fetchone(self, sql, params):
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        self.closed += 1
        self.conn.close()


class _TrackingRunner:
    instances: list["_TrackingRunner"] = []

    def __init__(self) -> None:
        self._runner = _REAL_RUNNER()
        self.run_calls = 0
        self.closed = 0
        self.__class__.instances.append(self)

    def run(self, awaitable):
        self.run_calls += 1
        return self._runner.run(awaitable)

    def close(self) -> None:
        self.closed += 1
        self._runner.close()


class _Adapter:
    instances: list["_Adapter"] = []

    def __init__(self, **_kwargs) -> None:
        self.market_calls: list[str] = []
        self.aclose_calls = 0
        self.close_calls = 0
        self.__class__.instances.append(self)

    async def get_market_raw(self, condition_id: str):
        self.market_calls.append(condition_id)
        return {"conditionId": condition_id, "adapter_number": len(self.__class__.instances)}

    async def aclose(self) -> None:
        self.aclose_calls += 1

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture(autouse=True)
def _clear_tracking() -> None:
    _TrackingRunner.instances.clear()
    _Adapter.instances.clear()


def _install_cli_fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Patch all external stages, retaining only a temp SQLite DB boundary."""
    db = _TempDatabase(tmp_path / "approved-specialist.db")
    monkeypatch.setattr(cli, "Database", lambda _path: db)
    monkeypatch.setattr(
        cli,
        "get_approval",
        lambda _db, _approval_id: SimpleNamespace(
            enabled=True, revoked_at=None, wallet_address="0xwallet"
        ),
    )
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(
            gamma_base_url="https://gamma.invalid",
            clob_base_url="https://clob.invalid",
            data_api_base_url="https://data.invalid",
        ),
    )
    monkeypatch.setattr(cli, "PolymarketPublicAdapter", _Adapter)
    monkeypatch.setattr(cli.asyncio, "Runner", _TrackingRunner)

    import polycopy.ingestion.normalized_source_trade as normalized

    monkeypatch.setattr(
        normalized,
        "normalize_source_trade",
        lambda *_args, **_kwargs: SimpleNamespace(
            source="polymarket_data_api_trades_user", source_trade_id=SOURCE_TRADE_ID
        ),
    )

    def write_one(database, _norms, **_kwargs):
        database.conn.execute(
            "INSERT INTO source_trades(id, source_trade_id, source) VALUES (?, ?, ?)",
            ("internal-1", SOURCE_TRADE_ID, "polymarket_data_api_trades_user"),
        )
        database.conn.commit()
        return SimpleNamespace(inserted=1)

    monkeypatch.setattr(cli, "write_valid_rows", write_one)
    return db


def _argv(tmp_path: Path, *, db_path: Path | None = None) -> list[str]:
    return [
        "--approval-id", APPROVAL_ID,
        "--allow-live",
        "--write",
        "--db-path", str(db_path or tmp_path / "approved-specialist.db"),
        "--json",
    ]


def test_main_uses_one_runner_passes_materialized_gamma_mapping_and_closes_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _install_cli_fakes(monkeypatch, tmp_path)

    async def collect(adapter, _wallet, *, gamma_resolver):
        assert await gamma_resolver(CONDITION_ID) == {
            "conditionId": CONDITION_ID, "adapter_number": 1
        }
        return SimpleNamespace(accepted_rows=[SimpleNamespace(market_source_id=CONDITION_ID)])

    async def enrich(_db, source_trade_id, *, gamma_resolver, dry_run):
        assert source_trade_id == "internal-1"
        assert dry_run is False
        assert await gamma_resolver(CONDITION_ID) == {
            "conditionId": CONDITION_ID, "adapter_number": 1
        }
        return SimpleNamespace(enrichment_id="enrichment-1", status="complete")

    def dispatch(_db, **kwargs):
        market = kwargs["gamma_resolver"](CONDITION_ID)
        assert isinstance(market, dict)
        assert market == {"conditionId": CONDITION_ID, "adapter_number": 1}
        return SimpleNamespace(
            dispatch_id="dispatch-1", status="complete", candidate_id="candidate-1",
            paper_signal_decision_id="decision-1", paper_signal_verdict="COPY",
        )

    monkeypatch.setattr(cli, "collect", collect)
    monkeypatch.setattr(cli, "enrich_source_trade_async", enrich)
    monkeypatch.setattr(cli, "dispatch_one", dispatch)

    assert cli.main(_argv(tmp_path)) == 0
    assert '"dispatch_id": "dispatch-1"' in capsys.readouterr().out
    assert len(_TrackingRunner.instances) == 1
    assert _TrackingRunner.instances[0].run_calls == 3  # collect, enrich, aclose
    assert _TrackingRunner.instances[0].closed == 1
    assert _Adapter.instances[0].market_calls == [CONDITION_ID]
    assert _Adapter.instances[0].aclose_calls == 1
    assert _Adapter.instances[0].close_calls == 0
    assert db.closed == 1


def test_main_cache_miss_in_dispatch_fails_closed_without_async_market_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = _install_cli_fakes(monkeypatch, tmp_path)

    async def collect(_adapter, _wallet, *, gamma_resolver):
        # Seed only the condition required by write-time normalization.  The
        # later dispatch miss must not turn into a fresh async adapter request.
        await gamma_resolver(CONDITION_ID)
        return SimpleNamespace(accepted_rows=[SimpleNamespace(market_source_id=CONDITION_ID)])

    async def enrich(_db, _source_trade_id, *, gamma_resolver, dry_run):
        return SimpleNamespace(enrichment_id="enrichment-1", status="complete")

    def dispatch(_db, **kwargs):
        kwargs["gamma_resolver"]("uncached-condition")

    monkeypatch.setattr(cli, "collect", collect)
    monkeypatch.setattr(cli, "enrich_source_trade_async", enrich)
    monkeypatch.setattr(cli, "dispatch_one", dispatch)

    with pytest.raises(
        RuntimeError,
        match="synchronous dispatch requested uncached Gamma market 'uncached-condition'",
    ):
        cli.main(_argv(tmp_path))
    # The dispatch cache miss fails synchronously; it cannot call async Gamma
    # for its new condition (only the earlier normalization condition exists).
    assert _Adapter.instances[0].market_calls == [CONDITION_ID]
    assert _Adapter.instances[0].aclose_calls == 1
    assert _Adapter.instances[0].close_calls == 0
    assert _TrackingRunner.instances[0].closed == 1
    assert db.closed == 1


def test_main_prefilters_by_canonical_source_and_never_dispatches_same_id_other_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A same-id row from another source must not steal the script's stage 2 ID."""
    db = _install_cli_fakes(monkeypatch, tmp_path)
    db.conn.execute(
        "INSERT INTO source_trades(id, source_trade_id, source) VALUES (?, ?, ?)",
        ("other-source-row", SOURCE_TRADE_ID, "other_source"),
    )
    db.conn.commit()
    dispatched_ids: list[str] = []

    async def collect(adapter, _wallet, *, gamma_resolver):
        await gamma_resolver(CONDITION_ID)
        return SimpleNamespace(accepted_rows=[SimpleNamespace(market_source_id=CONDITION_ID)])

    async def enrich(_db, source_trade_id, *, gamma_resolver, dry_run):
        assert source_trade_id == "internal-1"
        return SimpleNamespace(enrichment_id="enrichment-1", status="complete")

    def dispatch(_db, **kwargs):
        dispatched_ids.append(kwargs["source_trade_internal_id"])
        return SimpleNamespace(
            dispatch_id="dispatch-1", status="complete", candidate_id=None,
            paper_signal_decision_id=None, paper_signal_verdict=None,
        )

    monkeypatch.setattr(cli, "collect", collect)
    monkeypatch.setattr(cli, "enrich_source_trade_async", enrich)
    monkeypatch.setattr(cli, "dispatch_one", dispatch)

    assert cli.main(_argv(tmp_path)) == 0
    assert dispatched_ids == ["internal-1"]
    check = sqlite3.connect(tmp_path / "approved-specialist.db")
    try:
        assert check.execute(
            "SELECT COUNT(*) FROM source_trades WHERE source=? AND source_trade_id=?",
            ("polymarket_data_api_trades_user", SOURCE_TRADE_ID),
        ).fetchone()[0] == 1
    finally:
        check.close()


def test_main_replay_uses_fresh_per_invocation_gamma_cache_not_globals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dbs = []

    # Each invocation receives a distinct disposable DB object/file, mirroring
    # independent CLI processes while keeping the external stages controlled.
    def database_factory(path):
        db = _TempDatabase(Path(path))
        dbs.append(db)
        return db

    monkeypatch.setattr(cli, "Database", database_factory)
    # Install the remaining shared fakes without replacing Database again.
    original_database = cli.Database
    db = _install_cli_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "Database", original_database)
    db.close()  # unused setup database; each actual main gets its own temp DB.

    seen_markers = []

    async def collect(adapter, _wallet, *, gamma_resolver):
        assert await gamma_resolver(CONDITION_ID) == {
            "conditionId": CONDITION_ID, "adapter_number": len(_Adapter.instances)
        }
        return SimpleNamespace(accepted_rows=[SimpleNamespace(market_source_id=CONDITION_ID)])

    async def enrich(_db, _source_trade_id, *, gamma_resolver, dry_run):
        await gamma_resolver(CONDITION_ID)
        return SimpleNamespace(enrichment_id="enrichment", status="complete")

    def dispatch(_db, **kwargs):
        seen_markers.append(kwargs["gamma_resolver"](CONDITION_ID)["adapter_number"])
        return SimpleNamespace(
            dispatch_id="dispatch", status="complete", candidate_id=None,
            paper_signal_decision_id=None, paper_signal_verdict=None,
        )

    monkeypatch.setattr(cli, "collect", collect)
    monkeypatch.setattr(cli, "enrich_source_trade_async", enrich)
    monkeypatch.setattr(cli, "dispatch_one", dispatch)

    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    assert cli.main(_argv(tmp_path, db_path=first_path)) == 0
    assert cli.main(_argv(tmp_path, db_path=second_path)) == 0

    assert [adapter.market_calls for adapter in _Adapter.instances] == [[CONDITION_ID], [CONDITION_ID]]
    assert seen_markers == [1, 2]
    assert [adapter.aclose_calls for adapter in _Adapter.instances] == [1, 1]
    assert [adapter.close_calls for adapter in _Adapter.instances] == [0, 0]
    assert [runner.closed for runner in _TrackingRunner.instances] == [1, 1]
    assert [database.closed for database in dbs] == [1, 1]


def test_main_collection_failure_preserves_exception_and_closes_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = _install_cli_fakes(monkeypatch, tmp_path)
    calls = {"enrich": 0, "dispatch": 0}

    async def collect(_adapter, _wallet, *, gamma_resolver):
        raise ConnectionError("collection exploded")

    async def enrich(*_args, **_kwargs):
        calls["enrich"] += 1

    def dispatch(*_args, **_kwargs):
        calls["dispatch"] += 1

    monkeypatch.setattr(cli, "collect", collect)
    monkeypatch.setattr(cli, "enrich_source_trade_async", enrich)
    monkeypatch.setattr(cli, "dispatch_one", dispatch)
    with pytest.raises(ConnectionError, match="collection exploded"):
        cli.main(_argv(tmp_path))
    assert calls == {"enrich": 0, "dispatch": 0}
    assert _Adapter.instances[0].aclose_calls == 1
    assert _Adapter.instances[0].close_calls == 0
    assert _TrackingRunner.instances[0].closed == 1
    assert db.closed == 1


def test_main_structured_gamma_enrichment_failure_skips_dispatch_and_closes_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from polycopy.ingestion.gamma_budget import GammaResolutionError

    db = _install_cli_fakes(monkeypatch, tmp_path)
    dispatched = []

    async def collect(adapter, _wallet, *, gamma_resolver):
        await gamma_resolver(CONDITION_ID)
        return SimpleNamespace(accepted_rows=[SimpleNamespace(market_source_id=CONDITION_ID)])

    async def enrich(*_args, **_kwargs):
        raise GammaResolutionError("structured Gamma provider failure")

    def dispatch(*_args, **_kwargs):
        dispatched.append(True)

    monkeypatch.setattr(cli, "collect", collect)
    monkeypatch.setattr(cli, "enrich_source_trade_async", enrich)
    monkeypatch.setattr(cli, "dispatch_one", dispatch)
    with pytest.raises(GammaResolutionError, match="structured Gamma provider failure"):
        cli.main(_argv(tmp_path))
    assert dispatched == []
    assert _Adapter.instances[0].aclose_calls == 1
    assert _Adapter.instances[0].close_calls == 0
    assert _TrackingRunner.instances[0].closed == 1
    assert db.closed == 1


def test_script_async_bridge_guard():
    script = (ROOT / "scripts" / "process_approved_specialist_trades.py").read_text()
    enrichment = (ROOT / "src" / "polycopy" / "ingestion" / "source_trade_enrichment.py").read_text()
    forbidden = {"asyncio.run", "run_until_complete", "ThreadPoolExecutor", "new_event_loop", "to_thread"}
    for source in (script, enrichment):
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
        }
        assert not any(
            call in forbidden or call.rsplit(".", 1)[-1] in forbidden
            for call in calls
        ), calls
    assert script.count("asyncio.Runner()") == 1
    assert "gamma_cache" in script
    sync_body = script[script.index("def gamma_sync"):script.index("# ── Stage 1")]
    assert "get_market_raw" not in sync_body


# These staged durability cases deliberately retain the application's migrated
# Database and centralized source-trade writer.  Only live collection,
# enrichment, and dispatch dependencies are replaced; every invocation opens
# and closes the *same* disposable database path, like an interrupted CLI job
# followed by an operator retry.
_MISS_CONDITION_ID = "0x" + "b" * 64
_TOKEN_ID = "0x" + "c" * 64
_WALLET = "0x" + "a" * 40
_REAL_CONDITION_ID = "0x" + "d" * 64


def _raw_trade() -> SimpleNamespace:
    return SimpleNamespace(market_source_id=_REAL_CONDITION_ID)


def _install_real_staged_main(
    monkeypatch: pytest.MonkeyPatch, *, collect, enrich, dispatch
) -> list[list[object]]:
    """Install live-boundary fakes while retaining real migrations/writer."""
    writer_batches: list[list[object]] = []
    real_write = cli.write_valid_rows

    monkeypatch.setattr(
        cli,
        "get_approval",
        lambda _db, _approval_id: SimpleNamespace(
            enabled=True, revoked_at=None, wallet_address=_WALLET
        ),
    )
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(
            gamma_base_url="https://gamma.invalid",
            clob_base_url="https://clob.invalid",
            data_api_base_url="https://data.invalid",
        ),
    )
    monkeypatch.setattr(cli, "PolymarketPublicAdapter", _Adapter)

    # The production collector exposes normalized rows, while this script's
    # normalization seam still consumes raw mapping data.  Keep the script's
    # current seam explicit, but return the actual normalized model so the
    # real centralized writer exercises the migrated source_trades schema.
    import polycopy.ingestion.normalized_source_trade as normalized

    real_normalize = normalized.normalize_source_trade

    def normalize_for_script(_row, **_kwargs):
        return real_normalize(
            {
                "sourceProvidedTradeId": SOURCE_TRADE_ID,
                "proxyWallet": _WALLET,
                "asset": _TOKEN_ID,
                "conditionId": _REAL_CONDITION_ID,
                "side": "BUY",
                "outcome": "Yes",
                "price": "0.40",
                "size": "10",
                "timestamp": "2026-07-21T00:00:00Z",
            },
            requested_wallet=_WALLET,
            gamma_market=_kwargs.get("gamma_market"),
        )

    monkeypatch.setattr(normalized, "normalize_source_trade", normalize_for_script)

    def record_then_write(database, norms, **kwargs):
        writer_batches.append(list(norms))
        return real_write(database, norms, **kwargs)

    monkeypatch.setattr(cli, "write_valid_rows", record_then_write)
    monkeypatch.setattr(cli, "collect", collect)
    monkeypatch.setattr(cli, "enrich_source_trade_async", enrich)
    monkeypatch.setattr(cli, "dispatch_one", dispatch)
    return writer_batches


def _source_trade_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
    finally:
        conn.close()


def test_main_enrichment_failure_then_retry_reuses_same_durable_source_trade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from polycopy.ingestion.gamma_budget import GammaResolutionError

    db_path = tmp_path / "staged-retry.db"
    attempts = {"enrich": 0, "dispatch": 0}

    async def collect(_adapter, _wallet, *, gamma_resolver):
        await gamma_resolver(_REAL_CONDITION_ID)
        return SimpleNamespace(accepted_rows=[_raw_trade()])

    async def enrich(_db, source_trade_id, *, gamma_resolver, dry_run):
        assert source_trade_id
        assert dry_run is False
        attempts["enrich"] += 1
        if attempts["enrich"] == 1:
            raise GammaResolutionError("structured Gamma provider failure")
        return SimpleNamespace(enrichment_id="enrichment-retry", status="complete")

    def dispatch(_db, **_kwargs):
        attempts["dispatch"] += 1
        return SimpleNamespace(
            dispatch_id="dispatch-retry", status="complete", candidate_id=None,
            paper_signal_decision_id=None, paper_signal_verdict=None,
        )

    batches = _install_real_staged_main(
        monkeypatch, collect=collect, enrich=enrich, dispatch=dispatch
    )
    with pytest.raises(GammaResolutionError, match="structured Gamma provider failure"):
        cli.main(_argv(tmp_path, db_path=db_path))
    assert _source_trade_count(db_path) == 1
    assert attempts == {"enrich": 1, "dispatch": 0}

    assert cli.main(_argv(tmp_path, db_path=db_path)) == 0
    assert _source_trade_count(db_path) == 1
    assert [len(batch) for batch in batches] == [1, 0]
    assert attempts == {"enrich": 2, "dispatch": 1}


def test_main_cache_miss_then_retry_requires_enrichment_to_materialize_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "cache-miss-retry.db"
    enrich_calls = 0
    dispatch_calls = 0

    async def collect(_adapter, _wallet, *, gamma_resolver):
        await gamma_resolver(_REAL_CONDITION_ID)
        return SimpleNamespace(accepted_rows=[_raw_trade()])

    async def enrich(_db, _source_trade_id, *, gamma_resolver, dry_run):
        nonlocal enrich_calls
        enrich_calls += 1
        # First pass deliberately leaves the downstream condition unresolved;
        # retry proves dispatch can only proceed after the async stage fills it.
        if enrich_calls == 2:
            await gamma_resolver(_MISS_CONDITION_ID)
        return SimpleNamespace(enrichment_id="enrichment", status="complete")

    def dispatch(_db, **kwargs):
        nonlocal dispatch_calls
        dispatch_calls += 1
        assert kwargs["gamma_resolver"](_MISS_CONDITION_ID)["conditionId"] == _MISS_CONDITION_ID
        return SimpleNamespace(
            dispatch_id="dispatch-cache-retry", status="complete", candidate_id=None,
            paper_signal_decision_id=None, paper_signal_verdict=None,
        )

    batches = _install_real_staged_main(
        monkeypatch, collect=collect, enrich=enrich, dispatch=dispatch
    )
    with pytest.raises(RuntimeError, match="uncached Gamma market"):
        cli.main(_argv(tmp_path, db_path=db_path))
    assert _source_trade_count(db_path) == 1
    assert _Adapter.instances[0].market_calls == [_REAL_CONDITION_ID]

    assert cli.main(_argv(tmp_path, db_path=db_path)) == 0
    assert _source_trade_count(db_path) == 1
    assert [len(batch) for batch in batches] == [1, 0]
    assert dispatch_calls == 2
    assert _Adapter.instances[1].market_calls == [_REAL_CONDITION_ID, _MISS_CONDITION_ID]


def test_main_same_db_replay_keeps_canonical_source_trade_singleton(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "replay.db"
    dispatched = []

    async def collect(_adapter, _wallet, *, gamma_resolver):
        await gamma_resolver(_REAL_CONDITION_ID)
        return SimpleNamespace(accepted_rows=[_raw_trade()])

    async def enrich(_db, _source_trade_id, *, gamma_resolver, dry_run):
        return SimpleNamespace(enrichment_id="enrichment", status="complete")

    def dispatch(_db, **_kwargs):
        dispatched.append(True)
        return SimpleNamespace(
            dispatch_id="dispatch-replay", status="complete", candidate_id=None,
            paper_signal_decision_id=None, paper_signal_verdict=None,
        )

    batches = _install_real_staged_main(
        monkeypatch, collect=collect, enrich=enrich, dispatch=dispatch
    )
    assert cli.main(_argv(tmp_path, db_path=db_path)) == 0
    assert cli.main(_argv(tmp_path, db_path=db_path)) == 0

    assert _source_trade_count(db_path) == 1
    assert [len(batch) for batch in batches] == [1, 0]
    assert dispatched == [True, True]


def test_main_fixture_seeded_same_db_real_enrichment_and_dispatch_replay_has_zero_dml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second real ``main`` invocation must not invoke writer DML.

    The database starts with the authoritative specialist-paper evidence and a
    durable approval.  Only the collector and external HTTP adapter are faked:
    the script invokes its imported real enrichment and dispatcher functions,
    plus the real migration path, canonical normalizer, and canonical
    source-trade writer.  The SQLite trace covers the full second ``main``
    invocation.
    """
    db_path = tmp_path / "fixture-script-replay.db"
    seeded = RealDatabase(db_path).connect()
    try:
        seed_resolved_evidence(seeded)
        approval_id = create_approval_for_target(seeded)
    finally:
        seeded.close()

    target = make_target_trade()
    dml: list[str] = []
    trace_replay = False

    def traced_database(path: Path):
        db = RealDatabase(path)
        real_connect = db.connect

        def connect():
            connected = real_connect()
            if trace_replay:
                def trace(sql: str) -> None:
                    statement = sql.lstrip().upper()
                    if statement.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
                        dml.append(sql)

                connected.conn.set_trace_callback(trace)
            return connected

        db.connect = connect
        return db

    class FixtureAdapter:
        instances: list["FixtureAdapter"] = []

        def __init__(self, **_kwargs) -> None:
            self.market_calls: list[str] = []
            self.book_calls: list[str] = []
            self.aclose_calls = 0
            self.close_calls = 0
            self.__class__.instances.append(self)

        async def get_market_raw(self, condition_id: str):
            self.market_calls.append(condition_id)
            return FakeGamma().get_market(condition_id)

        async def fetch_book(self, token_id: str):
            self.book_calls.append(token_id)
            return await FakeClob().fetch_book(token_id)

        async def aclose(self) -> None:
            self.aclose_calls += 1

        def close(self) -> None:
            self.close_calls += 1

    async def collect(adapter, wallet, *, gamma_resolver):
        assert wallet == target["trader_address"]
        await gamma_resolver(target["market_source_id"])
        return SimpleNamespace(
            accepted_rows=[SimpleNamespace(market_source_id=target["market_source_id"])]
        )

    monkeypatch.setattr(cli, "Database", traced_database)
    monkeypatch.setattr(cli, "PolymarketPublicAdapter", FixtureAdapter)
    monkeypatch.setattr(
        cli, "Settings",
        lambda: SimpleNamespace(
            gamma_base_url="https://gamma.invalid", clob_base_url="https://clob.invalid",
            data_api_base_url="https://data.invalid",
        ),
    )
    monkeypatch.setattr(cli, "collect", collect)

    # ``main`` imports this at stage time.  Keep the production normalizer but
    # supply the fixture's production-shaped raw record to that precise seam.
    import polycopy.ingestion.normalized_source_trade as normalized

    real_normalize = normalized.normalize_source_trade

    def normalize_fixture(_accepted, **kwargs):
        return real_normalize(target, **kwargs)

    monkeypatch.setattr(normalized, "normalize_source_trade", normalize_fixture)
    argv = [
        "--approval-id", approval_id, "--allow-live", "--write",
        "--db-path", str(db_path), "--json",
    ]
    exec_tables = (
        "paper_signal_execution_authorizations", "execution_risk_decisions", "paper_orders",
        "paper_fills", "paper_positions", "paper_position_lots", "paper_position_marks",
        "paper_position_settlements",
    )

    def execution_counts():
        conn = sqlite3.connect(db_path)
        try:
            return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in exec_tables}
        finally:
            conn.close()

    before_execution = execution_counts()
    assert cli.main(argv) == 0
    after_first_execution = execution_counts()
    assert after_first_execution == before_execution
    check = sqlite3.connect(db_path)
    try:
        canonical = check.execute(
            "SELECT id, source, source_trade_id, metadata_json FROM source_trades "
            "WHERE source=? AND source_trade_id LIKE ?",
            ("polymarket_data_api_trades_user", "%:target"),
        ).fetchall()
        assert len(canonical) == 1
        before_bytes = canonical[0][3]
        source_id = canonical[0][0]
        enrichment = check.execute(
            "SELECT enrichment_id, source_trade_internal_id, condition_id, evidence_hash, "
            "reason_codes_json, evidence_source, gamma_source, clob_source, "
            "normalized_category, fetched_at, created_at, updated_at "
            "FROM source_trade_enrichments WHERE source_trade_internal_id=?", (source_id,)
        ).fetchall()
        dispatch = check.execute(
            "SELECT dispatch_id, specialist_approval_id, source_trade_internal_id, enrichment_id, "
            "status, candidate_id, paper_signal_decision_id, reason_codes_json, error_message, "
            "created_at, updated_at, completed_at FROM approved_specialist_trade_dispatches "
            "WHERE source_trade_internal_id=?", (source_id,)
        ).fetchall()
        assert len(enrichment) == len(dispatch) == 1
        candidate = check.execute(
            "SELECT id, source_trade_internal_id, market_source_id, token_id, outcome_label, side, "
            "source_trade_price, source_trade_quantity, status, status_reason, metrics_json, "
            "created_at, updated_at FROM copy_candidates WHERE id=?", (dispatch[0][5],)
        ).fetchall()
        snapshot = check.execute(
            "SELECT id, candidate_id, snapshot_run_id, fetch_status, token_id, best_bid, best_ask, "
            "mid_price, executable_price, created_at FROM candidate_price_snapshots WHERE candidate_id=?",
            (candidate[0][0],),
        ).fetchall()
        decision = check.execute(
            "SELECT id, candidate_id, price_snapshot_id, signal_reason, final_verdict, "
            "idempotency_key, computed_at, created_at FROM paper_signal_decisions WHERE id=?",
            (dispatch[0][6],),
        ).fetchall()
        assert len(candidate) == len(snapshot) == len(decision) == 1
        assert enrichment[0][1] == dispatch[0][2] == candidate[0][1] == source_id
        assert dispatch[0][3] == enrichment[0][0]
        assert dispatch[0][5] == candidate[0][0] == snapshot[0][1] == decision[0][1]
        assert dispatch[0][6] == decision[0][0] and decision[0][2] == snapshot[0][0]
        assert check.execute("PRAGMA foreign_key_check").fetchall() == []
        first_rows = (canonical, enrichment, dispatch, candidate, snapshot, decision)
    finally:
        check.close()

    trace_replay = True
    assert cli.main(argv) == 0
    after_replay_execution = execution_counts()
    assert after_replay_execution == after_first_execution == before_execution

    check = sqlite3.connect(db_path)
    try:
        replayed = check.execute(
            "SELECT id, source, source_trade_id, metadata_json FROM source_trades "
            "WHERE source=? AND source_trade_id LIKE ?",
            ("polymarket_data_api_trades_user", "%:target"),
        ).fetchall()
        assert replayed == canonical
        assert replayed[0][3] == before_bytes
        replay_enrichment = check.execute(
            "SELECT enrichment_id, source_trade_internal_id, condition_id, evidence_hash, reason_codes_json, "
            "evidence_source, gamma_source, clob_source, normalized_category, fetched_at, created_at, updated_at "
            "FROM source_trade_enrichments WHERE source_trade_internal_id=?", (source_id,)
        ).fetchall()
        replay_dispatch = check.execute(
            "SELECT dispatch_id, specialist_approval_id, source_trade_internal_id, enrichment_id, status, "
            "candidate_id, paper_signal_decision_id, reason_codes_json, error_message, created_at, updated_at, completed_at "
            "FROM approved_specialist_trade_dispatches WHERE source_trade_internal_id=?", (source_id,)
        ).fetchall()
        replay_candidate = check.execute(
            "SELECT id, source_trade_internal_id, market_source_id, token_id, outcome_label, side, source_trade_price, "
            "source_trade_quantity, status, status_reason, metrics_json, created_at, updated_at FROM copy_candidates WHERE id=?",
            (replay_dispatch[0][5],),
        ).fetchall()
        replay_snapshot = check.execute(
            "SELECT id, candidate_id, snapshot_run_id, fetch_status, token_id, best_bid, best_ask, mid_price, "
            "executable_price, created_at FROM candidate_price_snapshots WHERE candidate_id=?", (replay_candidate[0][0],)
        ).fetchall()
        replay_decision = check.execute(
            "SELECT id, candidate_id, price_snapshot_id, signal_reason, final_verdict, idempotency_key, computed_at, created_at "
            "FROM paper_signal_decisions WHERE id=?", (replay_dispatch[0][6],)
        ).fetchall()
        assert (replayed, replay_enrichment, replay_dispatch, replay_candidate, replay_snapshot, replay_decision) == first_rows
    finally:
        check.close()

    assert dml == [], dml
    assert [adapter.market_calls for adapter in FixtureAdapter.instances] == [
        [target["market_source_id"]], [target["market_source_id"]]
    ]
    assert [adapter.book_calls for adapter in FixtureAdapter.instances] == [
        [target["token_id"]], []
    ]
    assert [adapter.aclose_calls for adapter in FixtureAdapter.instances] == [1, 1]
    assert [adapter.close_calls for adapter in FixtureAdapter.instances] == [0, 0]


def _install_authoritative_fixture_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Drive real ``main`` stages using the canonical specialist-paper fixture.

    Only the network edges are deterministic fakes.  In particular, the
    centralized writer, real async enrichment, and real dispatcher remain the
    symbols imported by the CLI unless an individual failure seam wraps one.
    """
    db_path = tmp_path / "authoritative-main.db"
    seeded = RealDatabase(db_path).connect()
    try:
        seed_resolved_evidence(seeded)
        approval_id = create_approval_for_target(seeded)
    finally:
        seeded.close()
    target = make_target_trade()

    class FixtureAdapter:
        instances: list["FixtureAdapter"] = []

        def __init__(self, **_kwargs) -> None:
            self.market_calls: list[str] = []
            self.book_calls: list[str] = []
            self.aclose_calls = 0
            self.close_calls = 0
            self.__class__.instances.append(self)

        async def get_market_raw(self, condition_id: str):
            self.market_calls.append(condition_id)
            return FakeGamma().get_market(condition_id)

        async def fetch_book(self, token_id: str):
            self.book_calls.append(token_id)
            return await FakeClob().fetch_book(token_id)

        async def aclose(self) -> None:
            self.aclose_calls += 1

        def close(self) -> None:
            self.close_calls += 1

    async def collect(adapter, wallet, *, gamma_resolver):
        assert wallet == target["trader_address"]
        await gamma_resolver(target["market_source_id"])
        return SimpleNamespace(
            accepted_rows=[SimpleNamespace(market_source_id=target["market_source_id"])]
        )

    monkeypatch.setattr(cli, "PolymarketPublicAdapter", FixtureAdapter)
    monkeypatch.setattr(
        cli, "Settings",
        lambda: SimpleNamespace(
            gamma_base_url="https://gamma.invalid", clob_base_url="https://clob.invalid",
            data_api_base_url="https://data.invalid",
        ),
    )
    monkeypatch.setattr(cli, "collect", collect)

    import polycopy.ingestion.normalized_source_trade as normalized

    real_normalize = normalized.normalize_source_trade

    def normalize_fixture(_accepted, **kwargs):
        return real_normalize(target, **kwargs)

    monkeypatch.setattr(normalized, "normalize_source_trade", normalize_fixture)
    argv = [
        "--approval-id", approval_id, "--allow-live", "--write",
        "--db-path", str(db_path), "--json",
    ]
    return db_path, argv, target, FixtureAdapter


def _artifact_counts(db_path: Path) -> dict[str, int]:
    tables = (
        "source_trades", "source_trade_enrichments",
        "approved_specialist_trade_dispatches", "copy_candidates",
        "candidate_price_snapshots", "paper_signal_decisions",
    )
    conn = sqlite3.connect(db_path)
    try:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    finally:
        conn.close()


def test_main_authoritative_fixture_real_enrichment_gamma_failure_then_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real async-enrichment Gamma failure leaves only the durable trade.

    The retry uses a new CLI runner/cache and the actual enrichment + dispatch
    implementations to finish the complete persisted artifact chain.
    """
    from polycopy.ingestion.gamma_budget import GammaResolutionError
    import polycopy.ingestion.source_trade_enrichment as enrichment_module

    db_path, argv, target, Adapter = _install_authoritative_fixture_main(monkeypatch, tmp_path)
    real_resolve = enrichment_module.resolve_gamma_state_async
    attempts = 0

    async def fail_once(gamma_resolver, condition_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise GammaResolutionError("fixture Gamma outage during enrichment")
        return await real_resolve(gamma_resolver, condition_id)

    monkeypatch.setattr(enrichment_module, "resolve_gamma_state_async", fail_once)
    with pytest.raises(GammaResolutionError, match="fixture Gamma outage during enrichment"):
        cli.main(argv)
    assert _artifact_counts(db_path) == {
        "source_trades": RESOLVED_MARKET_COUNT + 1, "source_trade_enrichments": 0,
        "approved_specialist_trade_dispatches": 0, "copy_candidates": 0,
        "candidate_price_snapshots": 0, "paper_signal_decisions": 0,
    }

    assert cli.main(argv) == 0
    assert _artifact_counts(db_path) == {
        "source_trades": RESOLVED_MARKET_COUNT + 1, "source_trade_enrichments": 1,
        "approved_specialist_trade_dispatches": 1, "copy_candidates": 1,
        "candidate_price_snapshots": 1, "paper_signal_decisions": 1,
    }
    assert [adapter.market_calls for adapter in Adapter.instances] == [
        [target["market_source_id"]], [target["market_source_id"]]
    ]
    assert [adapter.book_calls for adapter in Adapter.instances] == [[], [target["token_id"]]]
    assert [adapter.aclose_calls for adapter in Adapter.instances] == [1, 1]


def test_main_authoritative_fixture_dispatch_cache_miss_then_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A synchronous dispatch cache miss cannot fetch Gamma and is retryable."""
    db_path, argv, target, Adapter = _install_authoritative_fixture_main(monkeypatch, tmp_path)
    real_dispatch = cli.dispatch_one
    attempts = 0
    missing_condition = "0x" + "b" * 64

    def miss_once(db, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # This is the actual sync resolver passed by main, not a test-side
            # substitute: it must fail closed rather than invoke async Gamma.
            kwargs["gamma_resolver"](missing_condition)
        return real_dispatch(db, **kwargs)

    monkeypatch.setattr(cli, "dispatch_one", miss_once)
    with pytest.raises(RuntimeError, match="synchronous dispatch requested uncached Gamma market"):
        cli.main(argv)
    assert _artifact_counts(db_path) == {
        "source_trades": RESOLVED_MARKET_COUNT + 1, "source_trade_enrichments": 1,
        "approved_specialist_trade_dispatches": 0, "copy_candidates": 0,
        "candidate_price_snapshots": 0, "paper_signal_decisions": 0,
    }
    assert Adapter.instances[0].market_calls == [target["market_source_id"]]
    assert Adapter.instances[0].book_calls == []

    assert cli.main(argv) == 0
    assert _artifact_counts(db_path) == {
        "source_trades": RESOLVED_MARKET_COUNT + 1, "source_trade_enrichments": 1,
        "approved_specialist_trade_dispatches": 1, "copy_candidates": 1,
        "candidate_price_snapshots": 1, "paper_signal_decisions": 1,
    }
    assert Adapter.instances[1].market_calls == [target["market_source_id"]]
    assert Adapter.instances[1].book_calls == [target["token_id"]]
    assert [adapter.aclose_calls for adapter in Adapter.instances] == [1, 1]
