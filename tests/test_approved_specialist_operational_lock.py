"""Operational-lock regressions for the approved-specialist processor."""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for candidate in (ROOT / "src", ROOT / "scripts", ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import scripts.process_approved_specialist_trades as cli  # noqa: E402


class _Conn:
    def __init__(self, events: list[str], held) -> None:
        self.events = events
        self.held = held

    def execute(self, _sql, _params=()):
        assert self.held()
        self.events.append("query")
        return []

    def rollback(self):
        assert self.held()
        self.events.append("rollback")


class _Database:
    def __init__(self, events: list[str], held) -> None:
        self.events = events
        self.held = held
        self.conn = _Conn(events, held)

    def connect(self):
        assert self.held()
        self.events.append("db-connect")
        return self

    def fetchone(self, _sql, _params):
        assert self.held()
        self.events.append("fetchone")
        return None

    def close(self):
        assert self.held()
        self.events.append("db-close")


class _Runner:
    def __init__(self, events: list[str], held) -> None:
        self.events = events
        self.held = held
        self._runner = asyncio.Runner()

    def run(self, awaitable):
        assert self.held()
        return self._runner.run(awaitable)

    def close(self):
        assert self.held()
        self.events.append("runner-close")
        self._runner.close()


class _Adapter:
    def __init__(self, events: list[str], held, **_kwargs) -> None:
        self.events = events
        self.held = held

    async def get_market_raw(self, condition_id):
        assert self.held()
        return {"conditionId": condition_id}

    async def aclose(self):
        assert self.held()
        self.events.append("adapter-close")


def _install(monkeypatch, events: list[str], *, fail_collect: bool = False):
    state = {"held": False}

    @contextmanager
    def lock(_job_name, *, timeout):
        assert timeout == 7.0
        events.append("lock-enter")
        state["held"] = True
        try:
            yield object()
        finally:
            events.append("lock-exit")
            state["held"] = False

    held = lambda: state["held"]
    db = _Database(events, held)
    monkeypatch.setattr(cli, "_is_production_db", lambda _path: True)
    monkeypatch.setattr(cli, "operational_job_lock", lock)
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
            gamma_base_url="gamma", clob_base_url="clob", data_api_base_url="data"
        ),
    )
    monkeypatch.setattr(cli, "PolymarketPublicAdapter", lambda **kwargs: _Adapter(events, held, **kwargs))
    monkeypatch.setattr(cli.asyncio, "Runner", lambda: _Runner(events, held))

    async def collect(_adapter, _wallet, *, gamma_resolver):
        assert held()
        events.append("collect")
        if fail_collect:
            raise RuntimeError("collect failed")
        return SimpleNamespace(accepted_rows=[])

    monkeypatch.setattr(cli, "collect", collect)
    return state


def _argv(*extra: str) -> list[str]:
    return [
        "--approval-id",
        "approval-id",
        "--allow-live",
        "--write",
        "--confirm-production-db",
        "--lock-timeout",
        "7",
        "--db-path",
        "/production/polycopy.db",
        *extra,
    ]


def test_production_write_lock_wraps_database_open_writes_cleanup_and_close(monkeypatch):
    events: list[str] = []
    state = _install(monkeypatch, events)

    assert cli.main(_argv()) == 0
    assert state["held"] is False
    assert events[0:2] == ["lock-enter", "db-connect"]
    assert events.index("collect") < events.index("adapter-close")
    assert events.index("adapter-close") < events.index("runner-close")
    assert events.index("runner-close") < events.index("db-close")
    assert events[-1] == "lock-exit"


def test_exception_rolls_back_and_closes_before_lock_release(monkeypatch):
    events: list[str] = []
    _install(monkeypatch, events, fail_collect=True)

    with pytest.raises(RuntimeError, match="collect failed"):
        cli.main(_argv())

    assert "rollback" in events
    assert events.index("rollback") < events.index("adapter-close")
    assert events.index("adapter-close") < events.index("runner-close")
    assert events.index("runner-close") < events.index("db-close")
    assert events.index("db-close") < events.index("lock-exit")


def test_nested_stage_calls_run_under_outer_lock_without_reacquiring(monkeypatch):
    events: list[str] = []
    state = {"held": False, "entries": 0}

    @contextmanager
    def lock(_job_name, *, timeout):
        state["entries"] += 1
        state["held"] = True
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")
            state["held"] = False

    class Db(_Database):
        def fetchone(self, _sql, _params):
            assert state["held"]
            return {"id": "internal-1"}

    db = Db(events, lambda: state["held"])
    monkeypatch.setattr(cli, "_is_production_db", lambda _path: True)
    monkeypatch.setattr(cli, "operational_job_lock", lock)
    monkeypatch.setattr(cli, "Database", lambda _path: db)
    monkeypatch.setattr(
        cli,
        "get_approval",
        lambda *_args: SimpleNamespace(enabled=True, revoked_at=None, wallet_address="0xwallet"),
    )
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(gamma_base_url="g", clob_base_url="c", data_api_base_url="d"),
    )
    monkeypatch.setattr(cli, "PolymarketPublicAdapter", lambda **kwargs: _Adapter(events, lambda: state["held"], **kwargs))
    monkeypatch.setattr(cli.asyncio, "Runner", lambda: _Runner(events, lambda: state["held"]))

    trade = SimpleNamespace(market_source_id="condition")

    async def collect(_adapter, _wallet, *, gamma_resolver):
        await gamma_resolver("condition")
        return SimpleNamespace(accepted_rows=[trade])

    async def enrich(*_args, **_kwargs):
        assert state["held"]
        events.append("enrich")
        return SimpleNamespace(enrichment_id="enrichment", status="complete")

    def dispatch(*_args, **_kwargs):
        assert state["held"]
        events.append("dispatch")
        return SimpleNamespace(
            dispatch_id="dispatch",
            status="complete",
            candidate_id=1,
            paper_signal_decision_id=2,
            paper_signal_verdict="copy_candidate",
        )

    monkeypatch.setattr(cli, "collect", collect)
    monkeypatch.setattr(cli, "enrich_source_trade_async", enrich)
    monkeypatch.setattr(cli, "dispatch_one", dispatch)
    monkeypatch.setattr(
        cli,
        "write_valid_rows",
        lambda *_args, **_kwargs: (
            events.append("write") or SimpleNamespace(inserted=1)
        ),
    )

    import polycopy.ingestion.normalized_source_trade as normalized

    monkeypatch.setattr(
        normalized,
        "normalize_source_trade",
        lambda *_args, **_kwargs: SimpleNamespace(source="source", source_trade_id="trade-id"),
    )

    assert cli.main(_argv()) == 0
    assert state["entries"] == 1
    assert events.index("write") < events.index("enrich") < events.index("dispatch")
    assert events.index("dispatch") < events.index("lock-exit")


def test_dry_run_does_not_acquire_operational_lock_or_write(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(cli, "_is_production_db", lambda _path: True)
    monkeypatch.setattr(
        cli,
        "operational_job_lock",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not acquire write lock"),
    )
    monkeypatch.setattr(cli, "Database", lambda _path: _Database(events, lambda: True))
    monkeypatch.setattr(
        cli,
        "get_approval",
        lambda *_args: SimpleNamespace(enabled=True, revoked_at=None, wallet_address="0xwallet"),
    )
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(gamma_base_url="g", clob_base_url="c", data_api_base_url="d"),
    )
    monkeypatch.setattr(cli, "PolymarketPublicAdapter", lambda **kwargs: _Adapter(events, lambda: True, **kwargs))
    monkeypatch.setattr(cli.asyncio, "Runner", lambda: _Runner(events, lambda: True))

    async def collect(*_args, **_kwargs):
        return SimpleNamespace(accepted_rows=[])

    monkeypatch.setattr(cli, "collect", collect)
    monkeypatch.setattr(cli, "write_valid_rows", lambda *_args, **_kwargs: pytest.fail("dry-run wrote"))

    assert cli.main(["--approval-id", "approval-id", "--allow-live", "--db-path", "/production/polycopy.db"]) == 0


@pytest.mark.parametrize(
    "argv, missing",
    [
        (["--write", "--confirm-production-db"], "--allow-live"),
        (["--write", "--allow-live"], "--confirm-production-db"),
    ],
)
def test_production_confirmation_flags_remain_mandatory(monkeypatch, capsys, argv, missing):
    monkeypatch.setattr(cli, "_is_production_db", lambda _path: True)
    rc = cli.main(["--approval-id", "approval-id", "--db-path", "/production/polycopy.db", *argv])
    assert rc == 2
    assert missing in capsys.readouterr().err
