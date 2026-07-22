"""PR #73 corrections — end-to-end proof battery (isolated, no production).

Every test uses disposable temp DBs ONLY. Nothing here opens /root/Polycopy's
production DB, deploys, migrates, or starts services/canaries. These tests
prove the 8 required corrections and the 21 final-validation bullets.

Run:
  PYTHONPATH=src:scripts python -m pytest tests/test_pr73_corrections.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
import sys
import tempfile

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.specialist_evidence_cohort import (  # noqa: E402
    CohortRunConfig,
    run_cohort,
    ALLOWED_WRITE_TABLES,
    FORBIDDEN_WRITE_TABLES,
)
import polycopy.ingestion.specialist_evidence_watchlist as wl  # noqa: E402

COND = ["0x" + c * 64 for c in ("a", "b", "c", "d", "e", "f")]
TOK = ["0x" + c * 64 for c in ("a", "b", "c", "d", "e", "f")]
ADDR = ["0xgood0000000000000000000000000000000000" + c + c for c in ("a", "b", "c", "d", "e", "f")]
WUUID = ["uuid-wallet-0000000000000000000000000000000" + c for c in ("a", "b", "c", "d", "e", "f")]


def _open():
    p = Path(tempfile.mktemp(suffix=".db"))
    return Database(p).connect()


def _seed_wallet(db, wid, address, is_sample=0):
    db.conn.execute(
        "INSERT INTO wallets(id,address,label,is_sample,created_at) "
        "VALUES (?,?,?,?,?)",
        (wid, address, "t", is_sample, "2026-01-01T00:00:00Z"),
    )
    db.conn.commit()


def _seed(db, indices):
    wids = []
    for i in indices:
        _seed_wallet(db, WUUID[i], ADDR[i])
        wids.append(wl.add_watch(db, wallet_id=WUUID[i]))
    return wids


def _buy(tid, cond, tok, addr):
    return {
        "sourceProvidedTradeId": tid,
        "proxyWallet": addr,
        "asset": tok,
        "conditionId": cond,
        "side": "BUY",
        "outcome": "Yes",
        "price": "0.40",
        "size": "10",
        "timestamp": "2026-02-01T00:00:00Z",
    }


def _sell(tid, cond, tok, addr):
    r = _buy(tid, cond, tok, addr)
    r["side"] = "SELL"
    return r


# ── Adapters ────────────────────────────────────────────────────────────────
class FakeAdapter:
    """Production-shaped fake: ONLY async aclose(); no sync close()."""

    def __init__(self, targets=None, *, get_market_raw=None):
        self._targets = targets or {}
        self._gmr = get_market_raw or (lambda c: {"conditionId": c, "category": "Politics"})
        self.aclose_calls = 0
        self.get_trades_calls = 0

    async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
        self.get_trades_calls += 1
        return list(self._targets.get(wallet.lower(), []))[:limit]

    async def get_market_raw(self, condition_id):
        value = self._gmr(condition_id)
        if hasattr(value, "__await__"):
            return await value
        return value

    async def aclose(self):
        self.aclose_calls += 1


class FailingOnWatch3Adapter(FakeAdapter):
    async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
        if wallet.lower() == ADDR[2].lower():
            raise RuntimeError("simulated provider failure on watch 3")
        return await super().get_trades_by_address(
            wallet, since=since, limit=limit, offset=offset, return_raw=return_raw
        )


def _targets(indices, rows_per=2, include_sell=False):
    t = {}
    for i in indices:
        rows = [_buy(f"t{i}_{j}", COND[i], TOK[i], ADDR[i]) for j in range(rows_per)]
        if include_sell:
            rows.append(_sell(f"s{i}", COND[i], TOK[i], ADDR[i]))
        t[ADDR[i].lower()] = rows
    return t


def _count(db, table):
    return db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 1 — writer/uniqueness failure → full cohort rollback
# ═════════════════════════════════════════════════════════════════════════════
def test_writer_failure_full_cohort_rollback():
    db = _open()
    wids = _seed(db, [0, 1, 2, 3, 4])
    adapter = FailingOnWatch3Adapter(_targets([0, 1, 2, 3, 4]))
    cfg = CohortRunConfig()

    before = {t: _count(db, t) for t in ALLOWED_WRITE_TABLES + FORBIDDEN_WRITE_TABLES}

    res = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
    )

    after = {t: _count(db, t) for t in ALLOWED_WRITE_TABLES + FORBIDDEN_WRITE_TABLES}

    # Assertions required by the PR.
    assert res.status == "failed"
    assert res.cohort_committed is False
    # Original writer/provider error is present and preserved verbatim.
    assert "RuntimeError" in (res.error or "")
    assert "simulated provider failure on watch 3" in (res.error or "")
    # Zero delta everywhere (full rollback of staged watches 1 and 2 too).
    assert before == after, {t: (before[t], after[t]) for t in before if before[t] != after[t]}
    # Watchlist changes for watches 1 & 2 rolled back (last_collection_at NULL).
    n_updated = db.conn.execute(
        "SELECT COUNT(*) FROM specialist_evidence_watchlist "
        "WHERE last_collection_at IS NOT NULL"
    ).fetchone()[0]
    assert n_updated == 0, n_updated
    # Watches after the failing one are marked unprocessed and never called.
    statuses = [w.status for w in res.watches]
    fail_idx = next((i for i, s in enumerate(statuses) if s == "error"), None)
    assert fail_idx is not None
    assert all(s == "unprocessed" for s in statuses[fail_idx + 1:]), statuses
    # Adapter closed exactly once.
    assert adapter.aclose_calls == 1, adapter.aclose_calls
    db.close()


def test_uniqueness_preflight_failure_rolls_back():
    db = _open()
    wids = _seed(db, [0, 1, 2])
    import polycopy.ingestion.specialist_evidence_collector as coll
    from polycopy.ingestion.source_trade_writer import WriteResult

    cfg = CohortRunConfig()
    adapter = FakeAdapter(_targets([0, 1, 2]))

    orig_collect = coll.collect_evidence
    call = {"n": 0}

    async def _coll_patch(db_, **kw):
        call["n"] += 1
        if call["n"] == 2:
            wr = WriteResult(errors=1, rolled_back=False,
                             unique_constraint_present=False)
            raise coll.WriterFailure(str(wr.errors), stop_reason="writer_failure")
        return await orig_collect(db_, **kw)

    coll.collect_evidence = _coll_patch
    try:
        before = {t: _count(db, t) for t in ALLOWED_WRITE_TABLES}
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
        )
        after = {t: _count(db, t) for t in ALLOWED_WRITE_TABLES}
        assert res.status == "failed"
        assert res.cohort_committed is False
        assert before == after
        assert adapter.aclose_calls == 1
    finally:
        coll.collect_evidence = orig_collect
        db.close()


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 2 — cohort-wide honest bounds
# ═════════════════════════════════════════════════════════════════════════════
def test_record_budget_is_cohort_wide():
    db = _open()
    wids = _seed(db, [0, 1, 2, 3, 4])
    adapter = FakeAdapter(_targets([0, 1, 2, 3, 4], rows_per=10))
    cfg = CohortRunConfig(max_total_new_trades=12)
    res = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
    )
    created = _count(db, "source_trades")
    assert res.status in ("success", "failed", "partial")
    assert created == 12, (created, res.as_dict())
    assert res.totals["rows_created"] == 12
    assert res.remaining["max_total_new_trades"] == 0
    assert adapter.aclose_calls == 1
    db.close()


def test_gamma_budget_exhaustion_is_cohort_wide():
    db = _open()
    wids = _seed(db, [0, 1, 2, 3, 4])
    adapter = FakeAdapter(_targets([0, 1, 2, 3, 4]),
                          get_market_raw=lambda c: {"conditionId": c, "category": "Politics"})
    cfg = CohortRunConfig(max_gamma_requests=2, resolve_gamma=True)
    res = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
    )
    assert res.status == "failed"
    assert res.stop_reason == "gamma_budget_exhausted"
    assert res.consumption["gamma_requests"] == 2, res.consumption
    assert res.remaining["max_gamma_requests"] == 0
    assert res.limits["max_gamma_requests"] == 2
    assert adapter.aclose_calls == 1
    db.close()


def test_deadline_expiry_rolls_back():
    db = _open()
    wids = _seed(db, [0, 1, 2])
    adapter = FakeAdapter(_targets([0, 1, 2], rows_per=5))
    cfg = CohortRunConfig(timeout_seconds=-1.0, enforce_deadline=True)

    before = _count(db, "source_trades")
    res = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
    )
    after = _count(db, "source_trades")
    assert res.status == "failed"
    assert res.stop_reason == "deadline_exceeded", res.stop_reason
    assert res.cohort_committed is False
    assert before == after
    assert adapter.aclose_calls == 1
    db.close()


def test_rss_limit_enforced_fail_closed():
    db = _open()
    wids = _seed(db, [0, 1])
    adapter = FakeAdapter(_targets([0, 1]))
    cfg = CohortRunConfig(rss_mb_limit=1.0)

    import polycopy.ingestion.specialist_evidence_collector as coll
    orig_sample = coll._rss_mb

    def _over():
        return 10_000.0

    coll._rss_mb = _over
    try:
        before = _count(db, "source_trades")
        res = asyncio.run(
            run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
        )
        after = _count(db, "source_trades")
        assert res.status == "failed"
        assert res.stop_reason == "rss_limit_exceeded", res.stop_reason
        assert res.cohort_committed is False
        assert before == after
        assert adapter.aclose_calls == 1
    finally:
        coll._rss_mb = orig_sample
        db.close()


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 3 — Gamma resolved once per unique condition; replay zero-write
# ═════════════════════════════════════════════════════════════════════════════
def test_gamma_resolved_once_and_replay_zero_write():
    db = _open()
    wids = _seed(db, [0, 1])
    gmr_calls = []

    def _gmr(cond):
        gmr_calls.append(cond)
        return {"conditionId": cond, "category": "Politics", "tags": ["election"]}

    adapter = FakeAdapter(_targets([0, 1]), get_market_raw=_gmr)
    cfg = CohortRunConfig(resolve_gamma=True, max_gamma_requests=100)

    res1 = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
    )
    assert res1.status == "success"
    first_gamma = len(gmr_calls)
    assert first_gamma == 2, first_gamma

    res2 = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
    )
    assert res2.status == "success"
    # Each run creates a fresh dedupe cache, so Gamma is re-resolved per run
    # (deterministic: 2 calls/run). The idempotent replay writes ZERO new rows.
    second_gamma = len(gmr_calls) - first_gamma
    assert second_gamma == 2, second_gamma
    # Zero delta in source_trades (idempotent INSERT OR IGNORE).
    assert _count(db, "source_trades") == 4, _count(db, "source_trades")
    assert adapter.aclose_calls == 2
    db.close()


def test_gamma_failure_causes_rollback():
    db = _open()
    wids = _seed(db, [0, 1])

    def _gmr_fail(cond):
        raise RuntimeError("gamma down")

    adapter = FakeAdapter(_targets([0, 1]), get_market_raw=_gmr_fail)
    cfg = CohortRunConfig(resolve_gamma=True, max_gamma_requests=100)
    before = _count(db, "source_trades")
    res = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg)
    )
    after = _count(db, "source_trades")
    assert res.status == "failed"
    # A Gamma failure is a cohort failure (rollback). stop_reason may be a
    # gamma-specific code or a generic enrichment error — both are hard stops.
    assert res.stop_reason in ("gamma_resolution_error", "enrich_error", "gamma_budget_exhausted")
    assert before == after
    assert adapter.aclose_calls == 1
    db.close()


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 4 — async aclose() on every exit path
# ═════════════════════════════════════════════════════════════════════════════
def test_async_aclose_on_every_exit_path():
    cases = {}

    db = _open()
    wids = _seed(db, [0, 1])
    a = FakeAdapter(_targets([0, 1]))
    asyncio.run(run_cohort(db, watch_ids=wids, adapter=a, dry_run=False, config=CohortRunConfig()))
    cases["success"] = a.aclose_calls
    db.close()

    db = _open()
    wids = _seed(db, [0, 1, 2])
    a = FailingOnWatch3Adapter(_targets([0, 1, 2]))
    asyncio.run(run_cohort(db, watch_ids=wids, adapter=a, dry_run=False, config=CohortRunConfig()))
    cases["watch_fail"] = a.aclose_calls
    db.close()

    db = _open()
    wids = _seed(db, [0, 1, 2, 3, 4])
    a = FakeAdapter(_targets([0, 1, 2, 3, 4]))
    asyncio.run(run_cohort(db, watch_ids=wids, adapter=a, dry_run=False,
                           config=CohortRunConfig(max_gamma_requests=1, resolve_gamma=True)))
    cases["gamma"] = a.aclose_calls
    db.close()

    db = _open()
    wids = _seed(db, [0, 1, 2])

    class BuildFailSpec:
        def build(self):
            raise RuntimeError("adapter.build() exploded")

        def close(self):
            pass

    res = asyncio.run(run_cohort(db, watch_ids=wids, adapter=BuildFailSpec(), dry_run=False, config=CohortRunConfig()))
    assert res.status == "failed"
    db.close()

    for k, v in cases.items():
        assert v == 1, (k, v)


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 5 — structured failure on provider build() failure
# ═════════════════════════════════════════════════════════════════════════════
def test_provider_build_failure_structured():
    db = _open()
    wids = _seed(db, [0, 1, 2])
    built = {"n": 0}

    class BuildFailSpec:
        def build(self):
            built["n"] += 1
            raise ConnectionError("cannot construct provider")

        def close(self):
            pass

    res = asyncio.run(
        run_cohort(db, watch_ids=wids, adapter=BuildFailSpec(), dry_run=False, config=CohortRunConfig())
    )
    assert res.status == "failed"
    assert res.cohort_committed is False
    assert "ConnectionError" in (res.error or "")
    assert "cannot construct provider" in (res.error or "")
    assert res.watch_count_requested == 3
    assert res.watch_count_processed == 0
    assert built["n"] == 1
    db.close()


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 6 — actual CLI Phase B ordering and no-writable-open matrix
# ═════════════════════════════════════════════════════════════════════════════
def test_cli_write_phase_b_orders_lock_writable_open_adapter_then_network(tmp_path, monkeypatch):
    """Exercise ``main()`` against a disposable DB, not a fake CLI stage.

    The write branch owns Phase B: after read-only validation it must acquire
    the lock, then open writable SQLite, then construct the adapter, and only
    then fetch.  Patch symbols in the *CLI module namespace* because those are
    the bindings ``main()`` actually calls.
    """
    import scripts.collect_specialist_evidence_cohort as cli
    import polycopy.adapters.polymarket as polymarket

    db_path = tmp_path / "phase-b-ordering.db"
    seeded = Database(db_path).connect()
    try:
        watch_id = _seed(seeded, [0])[0]
    finally:
        seeded.close()

    events: list[str] = []
    real_open_writable = cli.open_writable

    @contextlib.contextmanager
    def tracked_lock(*_args, **_kwargs):
        events.append("lock_acquired")
        try:
            yield
        finally:
            events.append("lock_released")

    def tracked_open_writable(*args, **kwargs):
        events.append("writable_open")
        db = real_open_writable(*args, **kwargs)
        real_close = db.close

        def tracked_close():
            events.append("db_closed")
            real_close()

        db.close = tracked_close
        return db

    class Adapter:
        def __init__(self, **_kwargs):
            events.append("adapter_constructed")

        async def get_trades_by_address(self, *_args, **_kwargs):
            events.append("network_fetch")
            return []

        async def aclose(self):
            events.append("adapter_closed")

    monkeypatch.setattr(cli, "operational_job_lock", tracked_lock)
    monkeypatch.setattr(cli, "open_writable", tracked_open_writable)
    monkeypatch.setattr(polymarket, "PolymarketPublicAdapter", Adapter)

    assert cli.main([
        "--db-path", str(db_path),
        "--watch-id", watch_id,
        "--write",
        "--lock-path", str(tmp_path / "phase-b-ordering.lock"),
        "--json",
    ]) == 0

    assert events == [
        "lock_acquired",
        "writable_open",
        "adapter_constructed",
        "network_fetch",
        "adapter_closed",
        "db_closed",
        "lock_released",
    ], events


def test_cli_rejection_matrix_never_opens_writable_or_builds_adapter(tmp_path, monkeypatch):
    """Every pre-Phase-B rejection leaves the disposable DB unopened writable."""
    import scripts.collect_specialist_evidence_cohort as cli
    import polycopy.adapters.polymarket as polymarket

    db_path = tmp_path / "no-writable-open-matrix.db"
    seeded = Database(db_path).connect()
    try:
        valid_watch = _seed(seeded, [0])[0]
        inactive_watch = _seed(seeded, [1])[0]
        seeded.conn.execute(
            "UPDATE specialist_evidence_watchlist SET status='paused' WHERE id=?",
            (inactive_watch,),
        )
        _seed_wallet(seeded, WUUID[2], ADDR[2])
        sample_watch = wl.add_watch(seeded, wallet_id=WUUID[2])
        seeded.conn.execute("UPDATE wallets SET is_sample=1 WHERE id=?", (WUUID[2],))
        seeded.conn.commit()
    finally:
        seeded.close()

    calls = {"writable": 0, "adapter": 0}

    def forbidden_writable(*_args, **_kwargs):
        calls["writable"] += 1
        raise AssertionError("rejected CLI input must not open writable SQLite")

    class ForbiddenAdapter:
        def __init__(self, **_kwargs):
            calls["adapter"] += 1
            raise AssertionError("rejected CLI input must not build an adapter")

    monkeypatch.setattr(cli, "open_writable", forbidden_writable)
    monkeypatch.setattr(polymarket, "PolymarketPublicAdapter", ForbiddenAdapter)

    cases = {
        "empty": [],
        "too_many": sum((["--watch-id", f"wl_0000000{i}"] for i in range(1, 7)), []),
        "malformed": ["--watch-id", "not a watch id"],
        "duplicate": ["--watch-id", valid_watch, "--watch-id", valid_watch],
        "numeric_out_of_range": ["--watch-id", valid_watch, "--max-total-new-trades", "0"],
        "missing": ["--watch-id", "wl_deadbeef"],
        "inactive": ["--watch-id", inactive_watch],
        "sample_wallet": ["--watch-id", sample_watch],
    }
    for name, case_args in cases.items():
        assert cli.main(["--db-path", str(db_path), "--write", *case_args]) == 2, name

    assert calls == {"writable": 0, "adapter": 0}, calls


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 7 — strict SQL write-table set
# ═════════════════════════════════════════════════════════════════════════════
def test_exact_observed_write_table_set():
    db = _open()
    wids = _seed(db, [0, 1])
    adapter = FakeAdapter(_targets([0, 1], include_sell=True))
    MUTATING_FORMS = (
        "INSERT OR IGNORE INTO", "INSERT OR REPLACE INTO", "INSERT INTO",
        "REPLACE INTO", "UPDATE", "DELETE FROM",
    )
    unrecognized = []
    observed = set()

    def _classify(sql):
        s = sql.strip()
        up = s.upper()
        if not up:
            return None
        if up.split()[0] not in ("INSERT", "REPLACE", "UPDATE", "DELETE"):
            return None
        for form in MUTATING_FORMS:
            if up.startswith(form):
                tbl = s[len(form):].strip().split()[0].strip('"').strip("`").split("(")[0].strip().lower()
                return form, tbl
        return "UNRECOGNIZED", up

    def _trace(sql):
        c = _classify(sql)
        if c is None:
            return
        if c[0] == "UNRECOGNIZED":
            unrecognized.append(sql)
            return
        observed.add(c[1])

    db.conn.set_trace_callback(_trace)
    res = asyncio.run(run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=CohortRunConfig()))
    db.conn.set_trace_callback(None)
    assert res.status == "success"
    assert unrecognized == [], unrecognized
    assert observed == set(ALLOWED_WRITE_TABLES), observed
    db.close()


# ═════════════════════════════════════════════════════════════════════════════
# CORRECTION 8 — truthful metrics on every exit path
# ═════════════════════════════════════════════════════════════════════════════
def test_truthful_metrics_dry_run_and_write_and_replay():
    db = _open()
    wids = _seed(db, [0, 1])
    adapter = FakeAdapter(_targets([0, 1]))
    res = asyncio.run(run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=True, config=CohortRunConfig()))
    assert res.status == "success"
    assert res.cohort_committed is False
    assert res.totals["rows_created"] == 0
    assert res.totals["rows_would_create"] == 4, res.totals
    assert _count(db, "source_trades") == 0
    db.close()

    db = _open()
    wids = _seed(db, [0, 1])
    adapter = FakeAdapter(_targets([0, 1]))
    res = asyncio.run(run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=CohortRunConfig()))
    assert res.status == "success"
    assert res.cohort_committed is True
    assert res.totals["rows_created"] == 4, res.totals
    assert res.totals["rows_would_create"] == 0
    assert res.watch_count_processed == 2
    assert res.watch_count_failed == 0
    assert res.watch_count_unprocessed == 0
    db.close()


def test_truthful_metrics_writer_failure():
    db = _open()
    wids = _seed(db, [0, 1, 2])
    adapter = FailingOnWatch3Adapter(_targets([0, 1, 2]))
    res = asyncio.run(run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=CohortRunConfig()))
    assert res.status == "failed"
    # unprocessed watches are NOT counted as completed; processed+failed+unprocessed
    # must equal the requested count.
    assert res.watch_count_processed + res.watch_count_failed + res.watch_count_unprocessed == 3
    assert res.watch_count_failed == 1
    assert res.totals["rows_created"] == 0
    db.close()


# Production-shaped async Gamma regression: one actual adapter call for a
# condition shared by multiple source trades; the completed mapping—not a
# coroutine—is what reaches metadata/provenance.
def test_async_gamma_one_call_per_unique_condition_and_no_coroutine_metadata():
    db = _open()
    wids = _seed(db, [0])
    calls = []

    async def get_market_raw(condition_id):
        calls.append(condition_id)
        await asyncio.sleep(0)
        return {"conditionId": condition_id, "category": "Politics", "tokens": []}

    adapter = FakeAdapter(_targets([0], rows_per=2), get_market_raw=get_market_raw)
    try:
        result = asyncio.run(
            run_cohort(
                db,
                watch_ids=wids,
                adapter=adapter,
                dry_run=False,
                config=CohortRunConfig(resolve_gamma=True, max_gamma_requests=1),
            )
        )
        assert result.status == "success", result.as_dict()
        assert calls == [COND[0]]
        raw_metadata = db.conn.execute("SELECT metadata_json FROM source_trades").fetchall()
        raw_provenance = db.conn.execute(
            "SELECT evidence_hash, reason_codes_json FROM source_trade_enrichments"
        ).fetchall()
        assert all("coroutine" not in str(row[0]).lower() for row in raw_metadata + raw_provenance)
        assert adapter.aclose_calls == 1
    finally:
        db.close()


# Direct regression: the ingestion layer may not downgrade a structured Gamma
# failure into unavailable metadata.
def test_run_ingestion_propagates_structured_gamma_failure():
    from polycopy.ingestion.gamma_budget import GammaBudgetExhausted
    from polycopy.ingestion.ingest_pipeline import run_ingestion

    class Provider:
        made_network_call = False

        async def fetch_trades(self, wallet, *, limit, page):
            return [_buy("gamma-fail", COND[0], TOK[0], ADDR[0])] if page == 0 else []

    async def exhausted(_condition_id):
        raise GammaBudgetExhausted("test Gamma cap")

    try:
        asyncio.run(run_ingestion(Provider(), ADDR[0], gamma_resolver=exhausted))
    except GammaBudgetExhausted as exc:
        assert "test Gamma cap" in str(exc)
    else:
        raise AssertionError("GammaBudgetExhausted was silently downgraded")


# The legacy sync entrypoint is explicitly sync-resolver-only: an awaitable
# must fail deterministically and direct callers to the native async API.
def test_sync_enrichment_rejects_async_resolver_with_migration_error():
    from polycopy.ingestion.source_trade_enrichment import enrich_source_trade

    db = _open()
    try:
        _seed_wallet(db, WUUID[0], ADDR[0])
        from polycopy.ingestion.source_trade_writer import write_valid_rows
        from polycopy.ingestion.normalized_source_trade import normalize_source_trade

        row = normalize_source_trade(
            _buy("sync-reject", COND[0], TOK[0], ADDR[0]),
            requested_wallet=ADDR[0], record_index=0,
        )
        write_valid_rows(db, [row], dry_run=False)

        async def resolver(_condition_id):
            return {"conditionId": COND[0]}

        internal_id = db.conn.execute(
            "SELECT id FROM source_trades WHERE source_trade_id=?", (row.source_trade_id,)
        ).fetchone()[0]
        try:
            enrich_source_trade(db, internal_id, gamma_resolver=resolver)
        except TypeError as exc:
            assert "enrich_source_trade_async" in str(exc)
        else:
            raise AssertionError("sync API accepted an async Gamma resolver")
    finally:
        db.close()


# Strict actual-cohort replay proof: identical Gamma-enabled replay emits no DML
# and preserves metadata/provenance bytes while observing duplicates truthfully.
def test_identical_cohort_replay_is_zero_dml_and_preserves_evidence():
    db = _open()
    wids = _seed(db, [0])
    calls = []

    async def gamma(condition_id):
        calls.append(condition_id)
        return {"conditionId": condition_id, "category": "Politics"}

    adapter = FakeAdapter(_targets([0], rows_per=2), get_market_raw=gamma)
    cfg = CohortRunConfig(resolve_gamma=True, max_gamma_requests=1, max_total_new_trades=2)
    try:
        first = asyncio.run(run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg))
        assert first.status == "success", first.as_dict()
        before_meta = db.conn.execute("SELECT id, metadata_json FROM source_trades ORDER BY id").fetchall()
        before_prov = db.conn.execute(
            "SELECT source_trade_internal_id, evidence_hash, reason_codes_json FROM source_trade_enrichments "
            "ORDER BY source_trade_internal_id"
        ).fetchall()
        before_watch = db.conn.execute(
            "SELECT last_collection_at FROM specialist_evidence_watchlist WHERE id=?", (wids[0],)
        ).fetchone()[0]
        writes = []

        def trace(sql):
            statement = sql.lstrip().upper()
            if statement.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
                writes.append(sql)

        db.conn.set_trace_callback(trace)
        replay = asyncio.run(run_cohort(db, watch_ids=wids, adapter=adapter, dry_run=False, config=cfg))
        db.conn.set_trace_callback(None)
        assert replay.status == "success", replay.as_dict()
        assert writes == [], writes
        assert replay.totals["duplicate_rows_observed"] == 2
        assert replay.consumption["fresh_rows_created_or_projected"] == 0
        assert replay.remaining["max_total_new_trades"] == 2
        assert len(calls) == 2
        assert db.conn.execute("SELECT id, metadata_json FROM source_trades ORDER BY id").fetchall() == before_meta
        assert db.conn.execute(
            "SELECT source_trade_internal_id, evidence_hash, reason_codes_json FROM source_trade_enrichments "
            "ORDER BY source_trade_internal_id"
        ).fetchall() == before_prov
        assert db.conn.execute(
            "SELECT last_collection_at FROM specialist_evidence_watchlist WHERE id=?", (wids[0],)
        ).fetchone()[0] == before_watch
    finally:
        db.close()


# Mixed replay: the duplicate avoids writer DML while one fresh candidate uses
# the real writer, consumes exactly one capacity unit, and gets enrichment.
def test_mixed_replay_writes_only_fresh_row_and_reports_duplicate():
    db = _open()
    wids = _seed(db, [0])
    try:
        first = asyncio.run(run_cohort(
            db, watch_ids=wids,
            adapter=FakeAdapter(
                _targets([0], rows_per=1),
                get_market_raw=lambda c: {"conditionId": c, "category": "Politics"},
            ),
            dry_run=False, config=CohortRunConfig(resolve_gamma=True, max_gamma_requests=1, max_total_new_trades=2),
        ))
        assert first.status == "success", first.as_dict()
        old = db.conn.execute(
            "SELECT id, metadata_json FROM source_trades ORDER BY id LIMIT 1"
        ).fetchone()
        old_evidence = db.conn.execute(
            "SELECT evidence_hash, reason_codes_json FROM source_trade_enrichments "
            "WHERE source_trade_internal_id=?", (old[0],)
        ).fetchone()
        mixed = _targets([0], rows_per=1)[ADDR[0].lower()] + [
            _buy("t0_fresh", COND[0], TOK[0], ADDR[0])
        ]
        calls = []

        async def gamma(condition_id):
            calls.append(condition_id)
            return {"conditionId": condition_id, "category": "Politics"}

        result = asyncio.run(run_cohort(
            db, watch_ids=wids,
            adapter=FakeAdapter({ADDR[0].lower(): mixed}, get_market_raw=gamma),
            dry_run=False,
            config=CohortRunConfig(resolve_gamma=True, max_gamma_requests=1, max_total_new_trades=2),
        ))
        assert result.status == "success", result.as_dict()
        assert result.totals["rows_created"] == 1
        assert result.totals["duplicate_rows_observed"] == 1
        assert result.consumption["fresh_rows_created_or_projected"] == 1
        assert result.remaining["max_total_new_trades"] == 1
        assert _count(db, "source_trades") == 2
        assert db.conn.execute(
            "SELECT metadata_json FROM source_trades WHERE id=?", (old[0],)
        ).fetchone()[0] == old[1]
        assert db.conn.execute(
            "SELECT evidence_hash, reason_codes_json FROM source_trade_enrichments "
            "WHERE source_trade_internal_id=?", (old[0],)
        ).fetchone() == old_evidence
        assert db.conn.execute(
            "SELECT COUNT(*) FROM source_trade_enrichments"
        ).fetchone()[0] == 2
        assert calls == [COND[0]]
    finally:
        db.close()


# Real source-trade writer failure on watch 3 via SQLite's INSERT seam.
def test_actual_writer_sqlite_execute_failure_on_watch_three_rolls_back():
    db = _open()
    wids = _seed(db, [0, 1, 2, 3, 4])
    calls = {address.lower(): 0 for address in ADDR[:5]}

    class Adapter(FakeAdapter):
        async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
            calls[wallet.lower()] += 1
            return await super().get_trades_by_address(
                wallet, since=since, limit=limit, offset=offset, return_raw=return_raw
            )

    ordered_indices = [wids.index(wid) for wid in sorted(wids)]
    fail_index = ordered_indices[2]
    later_indices = ordered_indices[3:]
    db.conn.execute(
        "CREATE TRIGGER fail_watch_three BEFORE INSERT ON source_trades "
        "WHEN NEW.trader_address='" + ADDR[fail_index].lower() + "' "
        "BEGIN SELECT RAISE(FAIL, 'writer sqlite failure watch 3'); END"
    )
    before = {table: _count(db, table) for table in ALLOWED_WRITE_TABLES + FORBIDDEN_WRITE_TABLES}
    adapter = Adapter(_targets([0, 1, 2, 3, 4], rows_per=1))
    try:
        result = asyncio.run(run_cohort(
            db, watch_ids=wids, adapter=adapter, dry_run=False, config=CohortRunConfig(),
        ))
        after = {table: _count(db, table) for table in before}
        assert result.status == "failed", result.as_dict()
        assert "writer sqlite failure watch 3" in (result.error or "")
        assert before == after
        statuses = [watch.status for watch in result.watches]
        assert statuses[:3] == ["ok", "ok", "error"], statuses
        assert statuses[3:] == ["unprocessed", "unprocessed"], statuses
        assert all(calls[ADDR[index].lower()] == 0 for index in later_indices), calls
        assert db.conn.execute(
            "SELECT COUNT(*) FROM specialist_evidence_watchlist WHERE last_collection_at IS NOT NULL"
        ).fetchone()[0] == 0
        assert adapter.aclose_calls == 1
    finally:
        db.close()


@pytest.mark.parametrize(
    ("case", "write_result", "expected"),
    [
        ("missing_constraint", {"unique_constraint_present": False, "errors": 1, "rolled_back": True, "error_message": "UNIQUE dedupe constraint missing"}, "UNIQUE dedupe constraint missing"),
        ("writer_errors", {"unique_constraint_present": True, "errors": 2, "error_message": "writer reported errors=2"}, "writer reported errors=2"),
        ("writer_rolled_back", {"unique_constraint_present": True, "rolled_back": True, "error_message": "writer rolled_back=True"}, "writer rolled_back=True"),
        ("writer_message", {"unique_constraint_present": True, "error_message": "writer-specific returned message"}, "writer-specific returned message"),
        ("unique_flag_false", {"unique_constraint_present": False}, "unique_constraint_present=False"),
    ],
)
def test_actual_writer_result_failure_on_deterministic_watch_three_rolls_back(case, write_result, expected):
    """Patch only the writer-result seam; collector/cohort remain real."""
    from polycopy.ingestion.source_trade_writer import WriteResult
    import polycopy.ingestion.specialist_evidence_collector as collector

    db = _open()
    wids = _seed(db, [0, 1, 2, 3, 4])
    ordered_indices = [wids.index(wid) for wid in sorted(wids)]
    fail_address = ADDR[ordered_indices[2]].lower()
    later_indices = ordered_indices[3:]
    calls = {address.lower(): 0 for address in ADDR[:5]}

    class Adapter(FakeAdapter):
        async def get_trades_by_address(self, wallet, *, since, limit, offset, return_raw):
            calls[wallet.lower()] += 1
            return await super().get_trades_by_address(
                wallet, since=since, limit=limit, offset=offset, return_raw=return_raw
            )

    original_writer = collector.write_valid_rows

    def writer_seam(db_arg, rows, **kwargs):
        if rows and rows[0].trader_address == fail_address:
            return WriteResult(**write_result)
        return original_writer(db_arg, rows, **kwargs)

    collector.write_valid_rows = writer_seam
    before = {table: _count(db, table) for table in ALLOWED_WRITE_TABLES + FORBIDDEN_WRITE_TABLES}
    adapter = Adapter(_targets([0, 1, 2, 3, 4], rows_per=1))
    try:
        result = asyncio.run(run_cohort(
            db, watch_ids=wids, adapter=adapter, dry_run=False, config=CohortRunConfig(),
        ))
        after = {table: _count(db, table) for table in before}
        assert result.status == "failed", (case, result.as_dict())
        assert expected in (result.error or ""), (case, result.error)
        assert result.cohort_committed is False and result.rolled_back is True
        assert before == after
        statuses = [watch.status for watch in result.watches]
        assert statuses[:3] == ["ok", "ok", "error"], (case, statuses)
        assert statuses[3:] == ["unprocessed", "unprocessed"], (case, statuses)
        assert all(calls[ADDR[index].lower()] == 0 for index in later_indices), (case, calls)
        assert adapter.aclose_calls == 1
        assert result.watch_count_processed + result.watch_count_failed + result.watch_count_unprocessed == 5
    finally:
        collector.write_valid_rows = original_writer
        db.close()


# Fresh rows must still pass through the writer's own uniqueness preflight.
def test_fresh_candidate_uses_writer_uniqueness_preflight_after_duplicate_partition():
    import polycopy.ingestion.specialist_evidence_collector as collector

    db = _open()
    wids = _seed(db, [0])
    original_writer = collector.write_valid_rows
    observed = []

    def writer_spy(db_arg, rows, **kwargs):
        result = original_writer(db_arg, rows, **kwargs)
        observed.append((len(rows), result.unique_constraint_present, result.inserted))
        return result

    collector.write_valid_rows = writer_spy
    adapter = FakeAdapter(_targets([0], rows_per=1))
    try:
        result = asyncio.run(run_cohort(
            db, watch_ids=wids, adapter=adapter, dry_run=False,
            config=CohortRunConfig(max_total_new_trades=1),
        ))
        assert result.status == "success", result.as_dict()
        assert observed == [(1, True, 1)]
        assert result.totals["rows_created"] == 1
        assert result.consumption["fresh_rows_created_or_projected"] == 1
        assert result.remaining["max_total_new_trades"] == 0
    finally:
        collector.write_valid_rows = original_writer
        db.close()


# The collector's canonical prefilter owns existing-duplicate observations.
# Fresh rows must still reach the real writer's UNIQUE preflight, but must not
# be mislabeled as writer-recognized existing duplicates merely because their
# IDs were passed through an obsolete pre-existing-ID hint.
def test_canonical_prefilter_keeps_fresh_writer_existing_duplicate_metric_zero():
    import polycopy.ingestion.specialist_evidence_collector as collector

    db = _open()
    wids = _seed(db, [0])
    original_writer = collector.write_valid_rows
    observed = []

    def writer_spy(db_arg, rows, **kwargs):
        result = original_writer(db_arg, rows, **kwargs)
        observed.append((result.inserted, result.deduplicated, result.existing_duplicates_recognized))
        return result

    collector.write_valid_rows = writer_spy
    try:
        result = asyncio.run(run_cohort(
            db, watch_ids=wids, adapter=FakeAdapter(_targets([0], rows_per=1)),
            dry_run=False, config=CohortRunConfig(max_total_new_trades=1),
        ))
        assert result.status == "success", result.as_dict()
        assert observed == [(1, 0, 0)]
    finally:
        collector.write_valid_rows = original_writer
        db.close()


# Canonical duplicate identity is the pair (source, source_trade_id), not the
# source_trade_id alone.  An identical ID owned by another source must remain
# fresh for this collector and be inserted by the real writer.
def test_canonical_prefilter_is_single_source_scoped():
    from polycopy.ingestion.normalized_source_trade import normalize_source_trade
    from polycopy.ingestion.source_trade_writer import write_valid_rows

    db = _open()
    wids = _seed(db, [0])
    raw = _buy("shared-id", COND[0], TOK[0], ADDR[0])
    other_source = replace(
        normalize_source_trade(raw, requested_wallet=ADDR[0]), source="other_source"
    )
    try:
        seeded = write_valid_rows(db, [other_source], dry_run=False)
        assert (seeded.inserted, seeded.deduplicated) == (1, 0)

        dry_result = asyncio.run(run_cohort(
            db, watch_ids=wids,
            adapter=FakeAdapter({ADDR[0].lower(): [raw]}), dry_run=True,
            config=CohortRunConfig(max_total_new_trades=1),
        ))
        assert dry_result.status == "success", dry_result.as_dict()
        assert dry_result.totals["rows_would_create"] == 1
        assert dry_result.totals["rows_would_update"] == 0

        result = asyncio.run(run_cohort(
            db, watch_ids=wids,
            adapter=FakeAdapter({ADDR[0].lower(): [raw]}), dry_run=False,
            config=CohortRunConfig(max_total_new_trades=1),
        ))
        assert result.status == "success", result.as_dict()
        assert result.totals["rows_created"] == 1
        assert result.totals["duplicate_rows_observed"] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM source_trades WHERE source=? AND source_trade_id=?",
            ("other_source", other_source.source_trade_id),
        ).fetchone()[0] == 1
        assert db.conn.execute(
            "SELECT COUNT(*) FROM source_trades WHERE source=? AND source_trade_id=?",
            ("polymarket_data_api_trades_user", other_source.source_trade_id),
        ).fetchone()[0] == 1
    finally:
        db.close()


# Real writer race: the collector prefilter sees a fresh canonical key, a
# separate writer wins before the collector's real writer executes, and the
# collector must report exactly one writer-race duplicate without consuming a
# fresh-row slot.  Both writes use the production writer implementation.
def test_canonical_prefilter_real_writer_race_reports_writer_duplicate_once():
    import polycopy.ingestion.specialist_evidence_collector as collector
    from polycopy.ingestion.source_trade_writer import write_valid_rows as real_writer

    db = _open()
    wids = _seed(db, [0])
    original_writer = collector.write_valid_rows
    contender_results = []
    collector_results = []

    def race_writer(db_arg, rows, **kwargs):
        contender_db = Database(db_arg.db_path).connect()
        try:
            contender = real_writer(contender_db, rows, dry_run=False)
            contender_results.append((contender.inserted, contender.deduplicated))
        finally:
            contender_db.close()
        result = original_writer(db_arg, rows, **kwargs)
        collector_results.append((result.inserted, result.deduplicated))
        return result

    collector.write_valid_rows = race_writer
    try:
        result = asyncio.run(run_cohort(
            db, watch_ids=wids,
            adapter=FakeAdapter({ADDR[0].lower(): [_buy("race-real", COND[0], TOK[0], ADDR[0])]}),
            dry_run=False, config=CohortRunConfig(max_total_new_trades=1),
        ))
        assert result.status == "success", result.as_dict()
        assert contender_results == [(1, 0)]
        assert collector_results == [(0, 1)]
        assert result.totals["rows_created"] == 0
        assert result.totals["duplicate_rows_observed"] == 1
        assert result.consumption["fresh_rows_created_or_projected"] == 0
        assert result.remaining["max_total_new_trades"] == 1
        assert _count(db, "source_trades") == 1
    finally:
        collector.write_valid_rows = original_writer
        db.close()


# Two identical provider records are partitioned before the writer receives rows.
def test_same_batch_duplicate_is_partitioned_before_writer():
    import polycopy.ingestion.specialist_evidence_collector as collector

    db = _open()
    wids = _seed(db, [0])
    original_writer = collector.write_valid_rows
    seen_sizes = []

    def writer_spy(db_arg, rows, **kwargs):
        seen_sizes.append(len(rows))
        return original_writer(db_arg, rows, **kwargs)

    collector.write_valid_rows = writer_spy
    duplicate = _buy("same-batch", COND[0], TOK[0], ADDR[0])
    try:
        result = asyncio.run(run_cohort(
            db, watch_ids=wids,
            adapter=FakeAdapter({ADDR[0].lower(): [duplicate, dict(duplicate)]}),
            dry_run=False, config=CohortRunConfig(max_total_new_trades=2),
        ))
        assert result.status == "success", result.as_dict()
        assert seen_sizes == [1]
        assert result.totals["rows_created"] == 1
        assert result.totals["duplicate_rows_observed"] == 1
    finally:
        collector.write_valid_rows = original_writer
        db.close()


# Ingestion counts every additional stable-ID duplicate, not just the first.
def test_three_identical_provider_rows_report_two_duplicates():
    db = _open()
    wids = _seed(db, [0])
    raw = _buy("triple", COND[0], TOK[0], ADDR[0])
    try:
        result = asyncio.run(run_cohort(
            db, watch_ids=wids,
            adapter=FakeAdapter({ADDR[0].lower(): [raw, dict(raw), dict(raw)]}),
            dry_run=False, config=CohortRunConfig(max_total_new_trades=3),
        ))
        assert result.status == "success", result.as_dict()
        assert result.totals["rows_created"] == 1
        assert result.totals["duplicate_rows_observed"] == 2
        assert result.consumption["fresh_rows_created_or_projected"] == 1
        assert result.remaining["max_total_new_trades"] == 2
    finally:
        db.close()


# One duplicate is collapsed in ingestion and the surviving canonical candidate
# is an existing DB duplicate: two observations, no replay DML.
def test_pipeline_duplicate_plus_existing_trade_reports_two_duplicates_zero_dml():
    db = _open()
    wids = _seed(db, [0])
    raw = _buy("persisted-t", COND[0], TOK[0], ADDR[0])
    cfg = CohortRunConfig(
        resolve_gamma=True, max_gamma_requests=1,
        max_new_trades_per_wallet=2, max_total_new_trades=2,
    )
    try:
        first = asyncio.run(run_cohort(
            db, watch_ids=wids,
            adapter=FakeAdapter({ADDR[0].lower(): [raw]}, get_market_raw=lambda c: {"conditionId": c, "category": "Politics"}),
            dry_run=False, config=cfg,
        ))
        assert first.status == "success"
        before_rows = _count(db, "source_trades"), _count(db, "source_trade_enrichments")
        before_meta = db.conn.execute("SELECT metadata_json FROM source_trades").fetchone()[0]
        before_hash = db.conn.execute("SELECT evidence_hash FROM source_trade_enrichments").fetchone()[0]
        before_watch = db.conn.execute(
            "SELECT last_collection_at FROM specialist_evidence_watchlist WHERE id=?", (wids[0],)
        ).fetchone()[0]
        calls, writes = [], []

        async def gamma(condition_id):
            calls.append(condition_id)
            return {"conditionId": condition_id, "category": "Politics"}

        def trace(sql):
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
                writes.append(sql)

        db.conn.set_trace_callback(trace)
        replay = asyncio.run(run_cohort(
            db, watch_ids=wids,
            adapter=FakeAdapter({ADDR[0].lower(): [raw, dict(raw)]}, get_market_raw=gamma),
            dry_run=False, config=cfg,
        ))
        db.conn.set_trace_callback(None)
        assert replay.status == "success", replay.as_dict()
        assert replay.totals["duplicate_rows_observed"] == 2
        assert replay.totals["rows_created"] == 0
        assert replay.consumption["fresh_rows_created_or_projected"] == 0
        assert replay.remaining["max_total_new_trades"] == 2
        assert writes == [], writes
        assert calls == [COND[0]]
        assert (_count(db, "source_trades"), _count(db, "source_trade_enrichments")) == before_rows
        assert db.conn.execute("SELECT metadata_json FROM source_trades").fetchone()[0] == before_meta
        assert db.conn.execute("SELECT evidence_hash FROM source_trade_enrichments").fetchone()[0] == before_hash
        assert db.conn.execute(
            "SELECT last_collection_at FROM specialist_evidence_watchlist WHERE id=?", (wids[0],)
        ).fetchone()[0] == before_watch
    finally:
        db.close()


# The writer is the only seam patched: an otherwise-fresh candidate loses a
# concurrent uniqueness race after preflight, while ingestion saw one duplicate.
def test_pipeline_duplicate_plus_writer_race_reports_two_duplicates():
    from polycopy.ingestion.source_trade_writer import WriteResult
    import polycopy.ingestion.specialist_evidence_collector as collector

    db = _open()
    wids = _seed(db, [0])
    raw = _buy("race-t", COND[0], TOK[0], ADDR[0])
    original_writer = collector.write_valid_rows
    seen = []

    def race_writer(db_arg, rows, **kwargs):
        seen.append(len(rows))
        return WriteResult(attempted=len(rows), deduplicated=len(rows), unique_constraint_present=True)

    collector.write_valid_rows = race_writer
    try:
        result = asyncio.run(run_cohort(
            db, watch_ids=wids,
            adapter=FakeAdapter({ADDR[0].lower(): [raw, dict(raw)]}),
            dry_run=False,
            config=CohortRunConfig(max_new_trades_per_wallet=2, max_total_new_trades=2),
        ))
        assert result.status == "success", result.as_dict()
        assert seen == [1]
        assert result.totals["rows_created"] == 0
        assert result.totals["duplicate_rows_observed"] == 2
        assert result.consumption["fresh_rows_created_or_projected"] == 0
        assert result.remaining["max_total_new_trades"] == 2
        assert _count(db, "source_trades") == 0
        assert _count(db, "source_trade_enrichments") == 0
    finally:
        collector.write_valid_rows = original_writer
        db.close()


@pytest.mark.parametrize(
    ("pipeline", "preflight", "writer", "expected"),
    [(1, 1, 0, 2), (1, 0, 1, 2), (0, 1, 1, 2), (2, 3, 4, 9)],
)
def test_duplicate_observation_metric_composition(pipeline, preflight, writer, expected):
    from polycopy.ingestion.specialist_evidence_collector import _total_duplicate_observations

    assert _total_duplicate_observations(
        pipeline=pipeline, preflight=preflight, writer=writer
    ) == expected


@pytest.mark.parametrize(
    "raw",
    [
        {"not": "a production-shaped trade"},
        _sell("rejected-sell", COND[0], TOK[0], ADDR[0]),
        {**_buy("no-stable-id", COND[0], TOK[0], ADDR[0]), "sourceProvidedTradeId": None, "transactionHash": None, "timestamp": None},
        {**_buy("bad-price", COND[0], TOK[0], ADDR[0]), "price": "not-a-number"},
    ],
)
def test_non_duplicate_rejections_do_not_increment_duplicate_observations(raw):
    import polycopy.ingestion.specialist_evidence_collector as collector

    db = _open()
    wids = _seed(db, [0])
    original_writer = collector.write_valid_rows
    calls = []

    def writer_spy(db_arg, rows, **kwargs):
        calls.append(rows)
        return original_writer(db_arg, rows, **kwargs)

    collector.write_valid_rows = writer_spy
    try:
        result = asyncio.run(run_cohort(
            db, watch_ids=wids,
            adapter=FakeAdapter({ADDR[0].lower(): [raw]}), dry_run=False,
            config=CohortRunConfig(max_total_new_trades=1),
        ))
        assert result.status == "success", result.as_dict()
        assert result.totals["duplicate_rows_observed"] == 0
        assert result.totals["rows_created"] == 0
        assert result.consumption["fresh_rows_created_or_projected"] == 0
        assert calls == [[]]
        assert _count(db, "source_trades") == 0
        assert _count(db, "source_trade_enrichments") == 0
    finally:
        collector.write_valid_rows = original_writer
        db.close()
